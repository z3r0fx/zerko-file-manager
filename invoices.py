"""Invoices: numbered, as a PDF, with a reminder when one is late.

An invoice belongs to a property. Its lines start from what was booked (the booking page's services)
or the property's amount due; the number is the invoice prefix from Manage > Business and a counter
that only goes up. The PDF carries the studio's details, the VAT number and rate, the bank details,
the reference and a QR code to the payment page. Marking an invoice paid marks the property paid (its
portals unlock); a late one shows in the bell and, with email set up, the client gets a polite
reminder once a week, three times at most."""
from __future__ import annotations

import io
import json
import threading
import time
from datetime import date, datetime, timedelta
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from PIL import Image, ImageDraw
from pydantic import BaseModel, Field
from sqlalchemy import Column, Date, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Session

import permissions
from auth import get_current_user
from database import Base, SessionLocal, User, engine, get_db

router = APIRouter(tags=["invoices"])
REMIND_EVERY = 7          # days between reminders
REMIND_MAX = 3


class Invoice(Base):
    __tablename__ = "invoices"
    id = Column(Integer, primary_key=True)
    shoot_id = Column(Integer, nullable=False, index=True)
    number = Column(String, nullable=False, unique=True)
    issued = Column(Date, nullable=False)
    due = Column(Date, nullable=False)
    lines = Column(Text, nullable=False, default="[]")     # [{name, qty, price}]
    vat_rate = Column(Float, default=0)
    total = Column(Float, default=0)
    status = Column(String, default="open")                # open | paid | void
    paid_at = Column(DateTime, nullable=True)
    to_name = Column(String, nullable=True)
    to_email = Column(String, nullable=True)
    to_extra = Column(Text, nullable=True)                 # agency, phone - as it was on the day
    note = Column(Text, nullable=True)
    sent_at = Column(DateTime, nullable=True)
    reminded_at = Column(DateTime, nullable=True)
    reminders = Column(Integer, default=0)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


def _may(user: User):
    if not permissions.can(user.role, permissions.SHOOTS):
        raise HTTPException(status_code=403, detail="Not allowed")


def _sums(lines: list, vat_rate: float) -> dict:
    sub = round(sum(float(x.get("qty") or 1) * float(x.get("price") or 0) for x in lines), 2)
    vat = round(sub * (vat_rate or 0) / 100, 2)
    return {"subtotal": sub, "vat": vat, "total": round(sub + vat, 2)}


def _late(inv: Invoice) -> bool:
    return inv.status == "open" and inv.due < date.today()


def _out(inv: Invoice) -> dict:
    lines = json.loads(inv.lines or "[]")
    return {"id": inv.id, "shoot_id": inv.shoot_id, "number": inv.number, "issued": inv.issued.isoformat(),
            "due": inv.due.isoformat(), "lines": lines, "vat_rate": inv.vat_rate or 0, **_sums(lines, inv.vat_rate or 0),
            "status": inv.status, "late": _late(inv), "paid_at": inv.paid_at.isoformat() + "Z" if inv.paid_at else None,
            "to_name": inv.to_name, "to_email": inv.to_email, "note": inv.note,
            "sent_at": inv.sent_at.isoformat() + "Z" if inv.sent_at else None,
            "reminded_at": inv.reminded_at.isoformat() + "Z" if inv.reminded_at else None, "reminders": inv.reminders or 0}


def _next_number(db: Session) -> str:
    import business
    import booking
    b = business.settings(db)
    row = db.query(business.BusinessSetting).filter(business.BusinessSetting.key == "invoice_next").first()
    n = int(json.loads(row.value)) if row and row.value else 1
    prefix = b.get("invoice_prefix") or "INV-"
    while db.query(Invoice).filter(Invoice.number == f"{prefix}{n:04d}").first():
        n += 1
    booking._put(db, {"invoice_next": n + 1})
    return f"{prefix}{n:04d}"


def _default_lines(s) -> list:
    try:
        svc = json.loads(s.services or "[]")
    except (TypeError, ValueError):
        svc = []
    lines = [{"name": f"{x.get('name')}", "qty": 1, "price": float(x.get("price") or 0)} for x in svc if x.get("name")]
    if not lines or not any(x["price"] for x in lines):
        lines = [{"name": "Photography", "qty": 1, "price": float(s.fee or 0)}]
    return lines


def _who(db: Session, s) -> dict:
    import shoots
    a = db.query(shoots.Agent).filter(shoots.Agent.name == s.agent).first() if s.agent else None
    return {"name": s.agent or s.owner_name or "", "email": (a.email if a else None) or s.booked_email or s.owner_email or "",
            "extra": "\n".join(x for x in [(a.agency if a else None) or s.agency, (a.phone if a else None) or s.booked_phone] if x)}


def _shoot(db: Session, shoot_id: int):
    import shoots
    s = db.query(shoots.Shoot).filter(shoots.Shoot.id == shoot_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="No such property")
    return s


class Line(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    qty: float = Field(1, ge=0, le=10000)
    price: float = Field(0, ge=-10_000_000, le=10_000_000)


class InvoiceBody(BaseModel):
    lines: Optional[List[Line]] = Field(None, max_length=40)
    vat_rate: Optional[float] = Field(None, ge=0, le=50)
    to_name: Optional[str] = Field(None, max_length=160)
    to_email: Optional[str] = Field(None, max_length=200)
    note: Optional[str] = Field(None, max_length=1000)
    due: Optional[str] = None
    status: Optional[str] = None


def _set_shoot_money(db: Session, inv: Invoice):
    """The property follows its newest invoice: its amount due, its reference, and Paid."""
    s = _shoot(db, inv.shoot_id)
    newest = db.query(Invoice).filter(Invoice.shoot_id == s.id, Invoice.status != "void").order_by(Invoice.id.desc()).first()
    if newest:
        s.invoice_no = newest.number
        s.fee = _sums(json.loads(newest.lines or "[]"), newest.vat_rate or 0)["total"] or None
        if newest.status == "paid" and not s.paid_at:
            s.paid_at = newest.paid_at or datetime.utcnow()
        elif newest.status == "open" and inv.id == newest.id and inv.status == "open" and s.paid_at and inv.paid_at is None:
            pass                                    # a property paid by hand stays paid
    db.commit()


@router.get("/api/shoots/{shoot_id}/invoices")
def list_for_shoot(shoot_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _may(current_user)
    rows = db.query(Invoice).filter(Invoice.shoot_id == shoot_id).order_by(Invoice.id.desc()).all()
    return {"invoices": [_out(r) for r in rows]}


@router.get("/api/shoots/{shoot_id}/invoices/draft")
def draft(shoot_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """What a new invoice would start with."""
    import business
    _may(current_user)
    s = _shoot(db, shoot_id)
    b = business.settings(db)
    who = _who(db, s)
    return {"lines": _default_lines(s), "vat_rate": float(b.get("vat_rate") or 0), "to_name": who["name"], "to_email": who["email"],
            "due_days": int(b.get("due_days") or 7), "prefix": b.get("invoice_prefix") or "INV-"}


@router.post("/api/shoots/{shoot_id}/invoices")
def create(shoot_id: int, body: InvoiceBody, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    import business
    _may(current_user)
    s = _shoot(db, shoot_id)
    b = business.settings(db)
    who = _who(db, s)
    lines = [x.model_dump() for x in body.lines] if body.lines else _default_lines(s)
    today = date.today()
    try:
        due = date.fromisoformat(body.due) if body.due else today + timedelta(days=int(b.get("due_days") or 7))
    except ValueError:
        raise HTTPException(status_code=400, detail="The due date looks wrong.")
    inv = Invoice(shoot_id=s.id, number=_next_number(db), issued=today, due=due, lines=json.dumps(lines),
                  vat_rate=body.vat_rate if body.vat_rate is not None else float(b.get("vat_rate") or 0),
                  to_name=(body.to_name if body.to_name is not None else who["name"]) or None,
                  to_email=(body.to_email if body.to_email is not None else who["email"]) or None,
                  to_extra=who["extra"] or None, note=body.note, created_by=current_user.username)
    inv.total = _sums(lines, inv.vat_rate)["total"]
    db.add(inv)
    db.commit()
    db.refresh(inv)
    _set_shoot_money(db, inv)
    return _out(inv)


def _get(db: Session, inv_id: int) -> Invoice:
    inv = db.query(Invoice).filter(Invoice.id == inv_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="No such invoice")
    return inv


@router.put("/api/invoices/{inv_id}")
def update(inv_id: int, body: InvoiceBody, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _may(current_user)
    inv = _get(db, inv_id)
    if body.status is not None:
        if body.status not in ("open", "paid", "void"):
            raise HTTPException(status_code=400, detail="Status is open, paid or void.")
        inv.status = body.status
        inv.paid_at = (inv.paid_at or datetime.utcnow()) if body.status == "paid" else None
    if inv.status == "open":           # a paid or void invoice keeps its words
        if body.lines is not None:
            inv.lines = json.dumps([x.model_dump() for x in body.lines])
        if body.vat_rate is not None:
            inv.vat_rate = body.vat_rate
        if body.to_name is not None:
            inv.to_name = body.to_name.strip() or None
        if body.to_email is not None:
            inv.to_email = body.to_email.strip() or None
        if body.note is not None:
            inv.note = body.note.strip() or None
        if body.due:
            try:
                inv.due = date.fromisoformat(body.due)
            except ValueError:
                raise HTTPException(status_code=400, detail="The due date looks wrong.")
    inv.total = _sums(json.loads(inv.lines or "[]"), inv.vat_rate or 0)["total"]
    db.commit()
    _set_shoot_money(db, inv)
    if body.status == "paid":
        s = _shoot(db, inv.shoot_id)
        if not s.paid_at:
            s.paid_at = datetime.utcnow()
            db.commit()
    return _out(inv)


# --------------------------------------------------------------------------
# the PDF
# --------------------------------------------------------------------------

def _money(cur: str, n: float) -> str:
    s = f"{abs(n):,.2f}".replace(",", " ")
    return f"{'-' if n < 0 else ''}{cur}{' ' if len(cur) > 1 else ''}{s}"


def render_pdf(db: Session, inv: Invoice) -> bytes:
    import business
    from print_marketing import A4, INK, SOFT, FAINT, font, mm, qr_image, text_block
    b = business.settings(db)
    s = _shoot(db, inv.shoot_id)
    cur = b.get("currency") or "R"
    lines = json.loads(inv.lines or "[]")
    sums = _sums(lines, inv.vat_rate or 0)
    page = Image.new("RGB", (mm(A4[0]), mm(A4[1])), (255, 255, 255))
    d = ImageDraw.Draw(page, "RGBA")
    W, H = page.size
    M = mm(18)
    accent = (31, 41, 55)
    # the studio, top left; the invoice, top right
    y = M
    d.text((M, y), business.studio_name(b), font=font("bold", 18), fill=INK)
    y += mm(9)
    for ln in [x for x in (b.get("studio_address") or "").splitlines() if x.strip()] + [x for x in [b.get("studio_phone"), b.get("studio_email")] if x]:
        d.text((M, y), ln.strip(), font=font("regular", 9), fill=SOFT)
        y += mm(4.6)
    if b.get("vat_no"):
        d.text((M, y), f"VAT no. {b['vat_no']}", font=font("regular", 9), fill=SOFT)
        y += mm(4.6)
    title = "TAX INVOICE" if b.get("vat_no") and (inv.vat_rate or 0) > 0 else "INVOICE"
    d.text((W - M, M), title, font=font("bold", 22), fill=accent, anchor="ra")
    ry = M + mm(12)
    for label, val in (("Number", inv.number), ("Date", f"{inv.issued:%d %B %Y}"), ("Due", f"{inv.due:%d %B %Y}")):
        d.text((W - M - mm(38), ry), label, font=font("regular", 9), fill=SOFT)
        d.text((W - M, ry), val, font=font("medium", 9), fill=INK, anchor="ra")
        ry += mm(5)
    y = max(y, ry) + mm(10)
    # to whom, and for which property
    d.text((M, y), "BILL TO", font=font("medium", 8), fill=FAINT)
    d.text((W / 2, y), "PROPERTY", font=font("medium", 8), fill=FAINT)
    y += mm(5)
    yy = y
    for ln in [inv.to_name] + (inv.to_extra or "").splitlines() + [inv.to_email]:
        if ln:
            d.text((M, yy), ln, font=font("medium" if ln == inv.to_name else "regular", 10), fill=INK if ln == inv.to_name else SOFT)
            yy += mm(5)
    py = y
    for ln in [s.address, s.suburb if s.suburb and s.suburb not in (s.address or "") else None,
               f"Shot {s.shoot_date:%d %B %Y}" if s.shoot_date else None]:
        if ln:
            d.text((W / 2, py), ln, font=font("medium" if ln == s.address else "regular", 10), fill=INK if ln == s.address else SOFT)
            py += mm(5)
    y = max(yy, py) + mm(10)
    # the lines
    cols = (M, W - M - mm(70), W - M - mm(40), W - M)
    d.rectangle((M, y, W - M, y + mm(8)), fill=(244, 244, 245))
    hf = font("medium", 8)
    d.text((cols[0] + mm(3), y + mm(4)), "DESCRIPTION", font=hf, fill=SOFT, anchor="lm")
    d.text((cols[1] + mm(12), y + mm(4)), "QTY", font=hf, fill=SOFT, anchor="rm")
    d.text((cols[2] + mm(12), y + mm(4)), "PRICE", font=hf, fill=SOFT, anchor="rm")
    d.text((cols[3] - mm(3), y + mm(4)), "AMOUNT", font=hf, fill=SOFT, anchor="rm")
    y += mm(11)
    lf = font("regular", 10)
    for x in lines:
        qty, price = float(x.get("qty") or 1), float(x.get("price") or 0)
        y2 = text_block(d, (cols[0] + mm(3), y), x.get("name") or "", lf, cols[1] - cols[0] - mm(20), INK, leading=1.3)
        d.text((cols[1] + mm(12), y), f"{qty:g}", font=lf, fill=INK, anchor="ra")
        d.text((cols[2] + mm(12), y), _money(cur, price), font=lf, fill=INK, anchor="ra")
        d.text((cols[3] - mm(3), y), _money(cur, qty * price), font=lf, fill=INK, anchor="ra")
        y = max(y2, y + mm(6)) + mm(2)
        d.line((M, y - mm(1), W - M, y - mm(1)), fill=(228, 228, 231), width=max(1, mm(0.25)))
    # the sums
    y += mm(4)
    rows = [("Subtotal", sums["subtotal"])]
    if inv.vat_rate:
        rows.append((f"VAT {inv.vat_rate:g}%", sums["vat"]))
    for label, val in rows:
        d.text((cols[2] - mm(10), y), label, font=font("regular", 10), fill=SOFT)
        d.text((cols[3] - mm(3), y), _money(cur, val), font=font("regular", 10), fill=INK, anchor="ra")
        y += mm(6)
    d.rectangle((cols[2] - mm(14), y, W - M, y + mm(11)), fill=accent)
    d.text((cols[2] - mm(10), y + mm(5.5)), "Total due", font=font("bold", 11), fill=(255, 255, 255), anchor="lm")
    d.text((cols[3] - mm(3), y + mm(5.5)), _money(cur, sums["total"]), font=font("bold", 12), fill=(255, 255, 255), anchor="rm")
    y += mm(20)
    # how to pay
    link = (b.get("pay_link") or "").replace("{amount}", f"{sums['total']:.2f}").replace("{ref}", quote(inv.number))
    qr = qr_image(link, mm(30)) if link and inv.status == "open" else None
    d.text((M, y), "HOW TO PAY", font=font("medium", 8), fill=FAINT)
    y += mm(5)
    right = W - M - (mm(36) if qr else 0)
    if qr:
        page.paste(qr, (W - M - qr.size[0], y))
        d.text((W - M - qr.size[0] // 2, y + qr.size[1] + mm(1.5)), "Scan to pay", font=font("regular", 7), fill=SOFT, anchor="mt")
    d.text((M, y), f"Please use {inv.number} as the reference.", font=font("medium", 10), fill=INK)
    y += mm(6)
    if b.get("pay_note"):
        y = text_block(d, (M, y), b["pay_note"], font("regular", 9), right - M, SOFT, leading=1.45)
    if link:
        y = text_block(d, (M, y + mm(1)), f"Or pay online: {link}", font("regular", 9), right - M, SOFT, leading=1.4)
    if inv.note:
        y = text_block(d, (M, y + mm(4)), inv.note, font("regular", 9), right - M, SOFT, leading=1.45)
    if inv.status == "paid":
        stamp = Image.new("RGBA", (mm(70), mm(26)), (0, 0, 0, 0))
        sd = ImageDraw.Draw(stamp)
        sd.rounded_rectangle((mm(1), mm(1), stamp.size[0] - mm(1), stamp.size[1] - mm(1)), mm(3), outline=(22, 163, 74, 230), width=mm(1))
        sd.text((stamp.size[0] // 2, stamp.size[1] // 2), "PAID", font=font("bold", 30), fill=(22, 163, 74, 230), anchor="mm")
        stamp = stamp.rotate(12, expand=True, resample=Image.BICUBIC)
        page.paste(stamp, (W - M - stamp.size[0], mm(58)), stamp)
    elif inv.status == "void":
        d.text((W / 2, H / 2), "VOID", font=font("bold", 90), fill=(220, 38, 38, 60), anchor="mm")
    d.text((W / 2, H - mm(12)), f"Thank you for your business. {business.studio_name(b)}", font=font("light", 8), fill=FAINT, anchor="mm")
    buf = io.BytesIO()
    page.save(buf, "PDF", resolution=200, quality=92)
    return buf.getvalue()


@router.get("/api/invoices/{inv_id}/pdf")
def pdf(inv_id: int, request: Request, token: Optional[str] = None, db: Session = Depends(get_db)):
    from auth import get_user_from_token
    tok = token or (request.headers.get("authorization") or "").replace("Bearer ", "").strip()
    _may(get_user_from_token(tok, db))
    inv = _get(db, inv_id)
    return Response(render_pdf(db, inv), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{inv.number}.pdf"'})


def _mail_words(db: Session, inv: Invoice, reminder: bool) -> tuple:
    import business
    b = business.settings(db)
    s = _shoot(db, inv.shoot_id)
    cur = b.get("currency") or "R"
    total = _money(cur, _sums(json.loads(inv.lines or "[]"), inv.vat_rate or 0)["total"])
    first = (inv.to_name or "").split(" ")[0] or "there"
    if reminder:
        subj = f"Reminder: invoice {inv.number} for {s.address}"
        body = (f"Hi {first},\n\nA friendly reminder that invoice {inv.number} for the photos of {s.address} ({total}) "
                f"was due on {inv.due:%d %B}. If you have paid already, thank you - please ignore this.\n")
    else:
        subj = f"Invoice {inv.number} for {s.address}"
        body = f"Hi {first},\n\nHere is invoice {inv.number} for the photos of {s.address}: {total}, due on {inv.due:%d %B %Y}.\n"
    link = (b.get("pay_link") or "").replace("{amount}", f"{inv.total:.2f}").replace("{ref}", quote(inv.number))
    body += f"\nPlease use {inv.number} as the reference." + (f"\nPay online: {link}" if link else "")
    if b.get("pay_note"):
        body += f"\n\n{b['pay_note']}"
    body += f"\n\nThank you,\n{business.studio_name(b)}"
    return subj, body


@router.post("/api/invoices/{inv_id}/send")
def send(inv_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Email it with the PDF attached when a mail server is set up; otherwise the words for the studio's own email."""
    import booking
    _may(current_user)
    inv = _get(db, inv_id)
    subj, body = _mail_words(db, inv, reminder=_late(inv))
    sent = False
    if inv.to_email:
        sent = booking.send_mail(db, [inv.to_email], subj, body, [(f"{inv.number}.pdf", render_pdf(db, inv), "application/pdf")])
    if sent:
        inv.sent_at = datetime.utcnow()
        db.commit()
    return {"sent": sent, "to": inv.to_email, "subject": subj, "body": body, **_out(inv)}


@router.get("/api/invoices")
def all_invoices(status: Optional[str] = None, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Every invoice, newest first, with its property's address (for the money overview)."""
    import shoots
    _may(current_user)
    q = db.query(Invoice)
    if status in ("open", "paid", "void"):
        q = q.filter(Invoice.status == status)
    rows = q.order_by(Invoice.id.desc()).limit(500).all()
    addr = {s.id: s.address for s in db.query(shoots.Shoot).filter(shoots.Shoot.id.in_({r.shoot_id for r in rows})).all()} if rows else {}
    return {"invoices": [{**_out(r), "address": addr.get(r.shoot_id, "")} for r in rows]}


# --------------------------------------------------------------------------
# late ones
# --------------------------------------------------------------------------

def late_invoices(db: Session) -> List[Invoice]:
    return db.query(Invoice).filter(Invoice.status == "open", Invoice.due < date.today()).all()


def _remind_once():
    import booking
    db = SessionLocal()
    try:
        for inv in late_invoices(db):
            if not inv.to_email or (inv.reminders or 0) >= REMIND_MAX:
                continue
            if inv.reminded_at and inv.reminded_at > datetime.utcnow() - timedelta(days=REMIND_EVERY):
                continue
            if (date.today() - inv.due).days < 1:
                continue
            subj, body = _mail_words(db, inv, reminder=True)
            if booking.send_mail(db, [inv.to_email], subj, body, [(f"{inv.number}.pdf", render_pdf(db, inv), "application/pdf")]):
                inv.reminded_at = datetime.utcnow()
                inv.reminders = (inv.reminders or 0) + 1
                db.commit()
    except Exception as e:
        print(f"invoices: reminders: {e}", flush=True)
    finally:
        db.close()


def _reminder_loop():
    time.sleep(120)
    while True:
        _remind_once()
        time.sleep(3 * 3600)


def install(app):
    import os
    Base.metadata.create_all(bind=engine, tables=[Invoice.__table__])
    app.include_router(router)
    if not os.environ.get("ZK_TEST_COPY"):
        threading.Thread(target=_reminder_loop, daemon=True, name="invoice-reminders").start()
