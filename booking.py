"""Booking a shoot, and a property's documents (the mandate first of all).

An agent books on the studio's booking page (/book - no account): who they are, the property and its
owner, the services, a day and a time the studio has free, and the mandate the owner signed. Booking
makes (or re-books) the property as Booked, keeps the mandate with it, and tells everyone: a notice in
the team chat and the bell, an email with a calendar invite to the agent, the owner and the studio when
email is set up (Manage > Business), and a calendar file on the confirmation page. The studio's own
calendar is also a feed a phone can subscribe to (/api/public/calendar/<key>.ics).

Documents live in MEDIA_ROOT/_documents/<property id>/, one row each, so a mandate, an offer to
purchase or floor measurements stay with their property."""
from __future__ import annotations

import json
import os
import re
import secrets
import smtplib
import ssl
import time
import uuid
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.orm import Session

import business
import permissions
import shoots
from auth import get_current_user
from database import Base, SessionLocal, User, engine, get_db

router = APIRouter(tags=["booking"])

DOC_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".heic", ".webp", ".doc", ".docx")
MAX_DOC = 25 * 1024 * 1024
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# what the booking page offers, in Manage > Business (kept with the other business settings)
BOOKING_DEFAULTS = {
    "booking_on": False,
    "book_days": [0, 1, 2, 3, 4, 5],          # Monday .. Saturday
    "book_start": "08:00",
    "book_end": "17:00",
    "book_minutes": 90,                        # one shoot, with travel
    "book_services": [
        {"name": "Photos", "price": 0, "minutes": 60},
        {"name": "Drone photos", "price": 0, "minutes": 30},
        {"name": "Video", "price": 0, "minutes": 60},
    ],
    "mandate_required": True,
    "book_note": "",
    "calendar_key": "",
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from": "",
}


class ShootDocument(Base):
    __tablename__ = "shoot_documents"
    id = Column(Integer, primary_key=True)
    shoot_id = Column(Integer, nullable=False, index=True)
    kind = Column(String, nullable=False, default="other")     # mandate | other
    name = Column(String, nullable=False)
    file = Column(String, nullable=False)                       # under _documents/<shoot id>/
    size = Column(Integer, nullable=True)
    at = Column(DateTime, default=datetime.utcnow)
    by = Column(String, nullable=True)


def _docs_root() -> Path:
    root = shoots._media_root or Path(os.environ.get("MEDIA_ROOT", "."))
    return root / "_documents"


def _safe_name(name: str) -> str:
    base = os.path.basename(name or "document").strip() or "document"
    return re.sub(r'[<>:"|?*\\/\x00-\x1f]', "_", base)[:120]


def save_document(db: Session, shoot_id: int, up: UploadFile, kind: str, by: str) -> ShootDocument:
    name = _safe_name(up.filename or "document")
    if not name.lower().endswith(DOC_EXT):
        raise HTTPException(status_code=400, detail="A document should be a PDF, a photo (JPEG, PNG, HEIC) or a Word file.")
    folder = _docs_root() / str(shoot_id)
    folder.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid.uuid4().hex[:10]}_{name}"
    dest = folder / fname
    size = 0
    with open(dest, "wb") as out:
        while True:
            chunk = up.file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_DOC:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="That file is over 25 MB.")
            out.write(chunk)
    doc = ShootDocument(shoot_id=shoot_id, kind=kind if kind in ("mandate", "other") else "other", name=name,
                        file=fname, size=size, by=by)
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def _doc_dict(d: ShootDocument) -> dict:
    return {"id": d.id, "shoot_id": d.shoot_id, "kind": d.kind, "name": d.name, "size": d.size,
            "at": d.at.isoformat() + "Z" if d.at else None, "by": d.by}


# --------------------------------------------------------------------------
# the booking settings (merged into the business settings)
# --------------------------------------------------------------------------

def book_settings(db: Session) -> dict:
    out = dict(BOOKING_DEFAULTS)
    for row in db.query(business.BusinessSetting).filter(business.BusinessSetting.key.in_(list(BOOKING_DEFAULTS))).all():
        try:
            out[row.key] = json.loads(row.value) if row.value is not None else BOOKING_DEFAULTS[row.key]
        except ValueError:
            pass
    if not out.get("calendar_key"):
        out["calendar_key"] = secrets.token_urlsafe(18)
        _put(db, {"calendar_key": out["calendar_key"]})
    return out


def _put(db: Session, values: dict):
    for k, v in values.items():
        row = db.query(business.BusinessSetting).filter(business.BusinessSetting.key == k).first()
        if not row:
            row = business.BusinessSetting(key=k)
            db.add(row)
        row.value = json.dumps(v)
    db.commit()


def _hm(s: str) -> int:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", (s or "").strip())
    if not m:
        raise ValueError(s)
    return int(m.group(1)) * 60 + int(m.group(2))


class Service(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    price: float = Field(0, ge=0, le=1_000_000)
    minutes: int = Field(0, ge=0, le=600)


class BookSettingsBody(BaseModel):
    booking_on: Optional[bool] = None
    book_days: Optional[List[int]] = None
    book_start: Optional[str] = Field(None, max_length=5)
    book_end: Optional[str] = Field(None, max_length=5)
    book_minutes: Optional[int] = Field(None, ge=15, le=600)
    book_services: Optional[List[Service]] = Field(None, max_length=20)
    mandate_required: Optional[bool] = None
    book_note: Optional[str] = Field(None, max_length=2000)
    smtp_host: Optional[str] = Field(None, max_length=200)
    smtp_port: Optional[int] = Field(None, ge=1, le=65535)
    smtp_user: Optional[str] = Field(None, max_length=200)
    smtp_password: Optional[str] = Field(None, max_length=300)
    smtp_from: Optional[str] = Field(None, max_length=200)
    new_calendar_key: bool = False


def _book_out(s: dict) -> dict:
    out = {k: s[k] for k in BOOKING_DEFAULTS if k != "smtp_password"}
    out["smtp_password_set"] = bool(s.get("smtp_password"))
    out["calendar_path"] = f"/api/public/calendar/{s['calendar_key']}.ics"
    return out


@router.get("/api/business/booking")
def get_booking(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if not permissions.can(current_user.role, permissions.SHOOTS):
        raise HTTPException(status_code=403, detail="Not allowed")
    return _book_out(book_settings(db))


@router.put("/api/business/booking")
def put_booking(body: BookSettingsBody, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrators only")
    vals = body.model_dump(exclude_none=True, exclude={"new_calendar_key"})
    try:
        if "book_start" in vals or "book_end" in vals:
            s0 = _hm(vals.get("book_start", book_settings(db)["book_start"]))
            s1 = _hm(vals.get("book_end", book_settings(db)["book_end"]))
            if s1 <= s0:
                raise HTTPException(status_code=400, detail="The day should end after it starts.")
    except ValueError:
        raise HTTPException(status_code=400, detail="Times look like 08:00.")
    if "book_days" in vals:
        vals["book_days"] = sorted({d for d in vals["book_days"] if 0 <= d <= 6})
    if "book_services" in vals:
        vals["book_services"] = [x if isinstance(x, dict) else x.model_dump() for x in vals["book_services"]]
    if vals.get("smtp_password") == "":
        vals.pop("smtp_password")                  # an empty box keeps the saved password
    if body.new_calendar_key:
        vals["calendar_key"] = secrets.token_urlsafe(18)
    _put(db, vals)
    return _book_out(book_settings(db))


# --------------------------------------------------------------------------
# email (optional) and calendar files
# --------------------------------------------------------------------------

def _ics_text(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _when(shoot) -> Optional[tuple]:
    """(start, end) as naive local datetimes, or None without a date."""
    if not shoot.shoot_date:
        return None
    try:
        hm = _hm(shoot.shoot_time or "09:00")
    except ValueError:
        hm = 9 * 60
    start = datetime.combine(shoot.shoot_date, datetime.min.time()) + timedelta(minutes=hm)
    mins = 0
    try:
        mins = sum(int(x.get("minutes") or 0) for x in json.loads(shoot.services or "[]"))
    except (TypeError, ValueError):
        pass
    return start, start + timedelta(minutes=max(mins, 60))


def ics_event(shoot, studio: str) -> str:
    w = _when(shoot)
    if not w:
        return ""
    start, end = w
    who = ", ".join(x for x in [shoot.owner_name and f"Owner: {shoot.owner_name} {shoot.owner_phone or ''}".strip(),
                                shoot.agent and f"Agent: {shoot.agent} {shoot.booked_phone or ''}".strip()] if x)
    services = ""
    try:
        services = ", ".join(x.get("name", "") for x in json.loads(shoot.services or "[]"))
    except (TypeError, ValueError):
        pass
    desc = "\n".join(x for x in [services, who, shoot.note or ""] if x)
    return "\r\n".join([
        "BEGIN:VEVENT",
        f"UID:zerko-shoot-{shoot.id}@zerko",
        f"DTSTAMP:{datetime.utcnow():%Y%m%dT%H%M%SZ}",
        f"DTSTART:{start:%Y%m%dT%H%M%S}",
        f"DTEND:{end:%Y%m%dT%H%M%S}",
        f"SUMMARY:{_ics_text(f'{studio} shoot: {shoot.address}')}",
        f"LOCATION:{_ics_text(', '.join(x for x in [shoot.address, shoot.suburb] if x))}",
        f"DESCRIPTION:{_ics_text(desc)}",
        "END:VEVENT",
    ])


def ics_file(events: List[str], name: str) -> str:
    return "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Zerko//Bookings//EN", "CALSCALE:GREGORIAN",
                        f"X-WR-CALNAME:{_ics_text(name)}", *[e for e in events if e], "END:VCALENDAR"]) + "\r\n"


def send_mail(db: Session, to: List[str], subject: str, text: str, attachments: List[tuple] = ()) -> bool:
    """Email through the studio's own mail server, when one is set up. Never raises."""
    s = book_settings(db)
    to = [t for t in {x.strip() for x in to if x and "@" in x}]
    if not (s.get("smtp_host") and s.get("smtp_from") and to):
        return False
    try:
        msg = EmailMessage()
        msg["From"] = s["smtp_from"]
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        msg.set_content(text)
        for fname, data, mime in attachments:
            main, sub = mime.split("/", 1)
            msg.add_attachment(data, maintype=main, subtype=sub, filename=fname)
        port = int(s.get("smtp_port") or 587)
        ctx = ssl.create_default_context()
        if port == 465:
            srv = smtplib.SMTP_SSL(s["smtp_host"], port, context=ctx, timeout=20)
        else:
            srv = smtplib.SMTP(s["smtp_host"], port, timeout=20)
            srv.starttls(context=ctx)
        with srv:
            if s.get("smtp_user"):
                srv.login(s["smtp_user"], s.get("smtp_password") or "")
            srv.send_message(msg)
        return True
    except Exception as e:
        print(f"booking: email not sent: {e}", flush=True)
        return False


@router.post("/api/business/booking/test-email")
def test_email(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrators only")
    b = business.settings(db)
    to = b.get("studio_email") or book_settings(db).get("smtp_from")
    if not to:
        raise HTTPException(status_code=400, detail="Fill in the studio's email first.")
    if not send_mail(db, [to], "Zerko test email", "Email from Zerko works. Booking confirmations will be sent like this."):
        raise HTTPException(status_code=400, detail="Could not send - check the mail server, port, name and password.")
    return {"ok": True, "to": to}


# --------------------------------------------------------------------------
# the booking page (public)
# --------------------------------------------------------------------------

def _busy(db: Session, days_ahead: int = 90) -> list:
    """What is already booked from today on: [{date, start, end}] in minutes of the day."""
    s = book_settings(db)
    today = date.today()
    rows = db.query(shoots.Shoot).filter(shoots.Shoot.shoot_date >= today,
                                        shoots.Shoot.shoot_date <= today + timedelta(days=days_ahead)).all()
    out = []
    for r in rows:
        try:
            st = _hm(r.shoot_time or "")
        except ValueError:
            out.append({"date": r.shoot_date.isoformat(), "start": 0, "end": 24 * 60, "all_day": True})
            continue
        w = _when(r)
        mins = max(int(s.get("book_minutes") or 90), int((w[1] - w[0]).total_seconds() // 60) if w else 0)
        out.append({"date": r.shoot_date.isoformat(), "start": st, "end": st + mins})
    return out


@router.get("/api/public/book/info")
def book_info(db: Session = Depends(get_db)):
    s, b = book_settings(db), business.settings(db)
    if not s.get("booking_on"):
        raise HTTPException(status_code=404, detail="Booking online is switched off. Please phone or email the studio.")
    terms = None
    if b.get("terms_on"):
        title, text = b.get("terms_title") or "Terms of use", business.terms_words(b)
        terms = {"title": title, "text": text, "version": business._fingerprint(title, text)}
    return {
        "studio": business.studio_name(b), "email": b.get("studio_email") or "", "phone": b.get("studio_phone") or "",
        "note": s.get("book_note") or "", "currency": b.get("currency") or "R",
        "services": s.get("book_services") or [], "days": s.get("book_days") or [],
        "start": s.get("book_start"), "end": s.get("book_end"), "minutes": s.get("book_minutes"),
        "mandate_required": bool(s.get("mandate_required")), "busy": _busy(db), "terms": terms,
        "today": date.today().isoformat(),
    }


_book_hits: dict = {}
_addr_hits: dict = {}


@router.get("/api/public/book/address")
def book_address(q: str, request: Request, db: Session = Depends(get_db)):
    """The address search on the booking page: the same OpenStreetMap search the studio uses, asked by
    this server, only while booking is on, and a few dozen times an hour per visitor."""
    if not book_settings(db).get("booking_on"):
        raise HTTPException(status_code=404, detail="Booking online is switched off.")
    ip = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or (request.client.host if request.client else "")
    now = time.time()
    hits = [t for t in _addr_hits.get(ip, []) if now - t < 3600]
    if len(hits) >= 60:
        raise HTTPException(status_code=429, detail="Too many searches - type the address in full instead.")
    _addr_hits[ip] = hits + [now]
    return shoots.geocode(q, None, None, None)




class BookBody(BaseModel):
    agent_name: str = Field(..., min_length=2, max_length=120)
    agency: Optional[str] = Field(None, max_length=120)
    agent_email: Optional[str] = Field(None, max_length=200)
    agent_phone: str = Field(..., min_length=6, max_length=40)
    address: str = Field(..., min_length=5, max_length=300)
    suburb: Optional[str] = Field(None, max_length=120)
    lat: Optional[float] = None
    lng: Optional[float] = None
    owner_name: Optional[str] = Field(None, max_length=120)
    owner_phone: Optional[str] = Field(None, max_length=40)
    owner_email: Optional[str] = Field(None, max_length=200)
    services: List[str] = Field(default_factory=list, max_length=20)
    date: str
    time: str
    note: Optional[str] = Field(None, max_length=2000)
    agree: bool = False
    terms_version: Optional[str] = Field(None, max_length=40)


@router.post("/api/public/book")
async def book(request: Request, data: str = Form(...), mandate: Optional[UploadFile] = File(None),
               db: Session = Depends(get_db)):
    ip = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or (request.client.host if request.client else "")
    now = time.time()
    hits = [t for t in _book_hits.get(ip, []) if now - t < 3600]
    if len(hits) >= 12:
        raise HTTPException(status_code=429, detail="Too many bookings from here in an hour - please phone the studio.")
    _book_hits[ip] = hits + [now]
    try:
        body = BookBody(**json.loads(data))
    except Exception:
        raise HTTPException(status_code=400, detail="Some of the booking is missing or not filled in right.")
    s, b = book_settings(db), business.settings(db)
    if not s.get("booking_on"):
        raise HTTPException(status_code=404, detail="Booking online is switched off.")
    # the day and time: a working day, inside the hours, not taken
    try:
        day = date.fromisoformat(body.date)
        start = _hm(body.time)
    except ValueError:
        raise HTTPException(status_code=400, detail="Choose a day and a time.")
    if day < date.today() or day > date.today() + timedelta(days=120):
        raise HTTPException(status_code=400, detail="Choose a day from today up to four months ahead.")
    if day.weekday() not in (s.get("book_days") or []):
        raise HTTPException(status_code=400, detail=f"The studio does not shoot on {DAY_NAMES[day.weekday()]}s.")
    chosen = [x for x in (s.get("book_services") or []) if x.get("name") in body.services]
    mins = max(int(s.get("book_minutes") or 90), sum(int(x.get("minutes") or 0) for x in chosen))
    if start < _hm(s["book_start"]) or start + min(mins, 60) > _hm(s["book_end"]):
        raise HTTPException(status_code=400, detail=f"Shoots are between {s['book_start']} and {s['book_end']}.")
    for x in _busy(db):
        if x["date"] == day.isoformat() and start < x["end"] and x["start"] < start + mins:
            raise HTTPException(status_code=409, detail="That time was just taken - choose another.")
    if s.get("mandate_required") and not (mandate and mandate.filename):
        raise HTTPException(status_code=400, detail="Attach the mandate the owner signed.")
    terms_needed = bool(b.get("terms_on"))
    if terms_needed:
        title, text = b.get("terms_title") or "Terms of use", business.terms_words(b)
        ver = business._fingerprint(title, text)
        if not body.agree:
            raise HTTPException(status_code=400, detail="Tick the box to agree to the terms.")
        if body.terms_version and body.terms_version != ver:
            raise HTTPException(status_code=409, detail="The terms were just changed - read them again.")
    # the property: a new one, or one shot before (booked again)
    key = shoots.address_key(body.address)
    if not key:
        raise HTTPException(status_code=400, detail="That is not an address.")
    shoot = db.query(shoots.Shoot).filter(shoots.Shoot.address_key == key).first()
    again = shoot is not None
    if not shoot:
        if body.lat is not None and body.lng is not None:
            shoots._check_location(body.lat, body.lng)
        shoot = shoots.Shoot(address=body.address.strip(), address_key=key, created_by=f"{body.agent_name} (booking)",
                             lat=body.lat if body.lng is not None else None, lng=body.lng if body.lat is not None else None,
                             suburb=(body.suburb or "").strip() or None)
        db.add(shoot)
    shoot.status = "booked"
    shoot.shoot_date, shoot.shoot_time = day, f"{start // 60:02d}:{start % 60:02d}"
    shoot.agent = body.agent_name.strip()
    shoot.agency = (body.agency or "").strip() or None
    shoot.booked_email = (body.agent_email or "").strip() or None
    shoot.booked_phone = body.agent_phone.strip()
    shoot.owner_name = (body.owner_name or "").strip() or None
    shoot.owner_phone = (body.owner_phone or "").strip() or None
    shoot.owner_email = (body.owner_email or "").strip() or None
    shoot.services = json.dumps(chosen)
    shoot.booked_at = datetime.utcnow()
    shoot.book_token = secrets.token_urlsafe(16)
    if body.note and body.note.strip():
        stamp = f"Booking note ({day:%d %b}): {body.note.strip()}"
        shoot.note = f"{shoot.note}\n{stamp}" if shoot.note else stamp
    total = sum(float(x.get("price") or 0) for x in chosen)
    if total and not shoot.fee:
        shoot.fee = total
    shoots._remember_agent(db, shoot.agent)
    db.commit()
    db.refresh(shoot)
    if mandate and mandate.filename:
        save_document(db, shoot.id, mandate, "mandate", f"{body.agent_name} (booking)")
    if terms_needed:
        db.add(business.TermsAgreement(share_id=0, shoot_id=shoot.id, name=body.agent_name.strip(),
                                       email=(body.agent_email or "").strip() or None, ip=ip,
                                       browser=(request.headers.get("user-agent") or "")[:300], version=ver,
                                       title=f"{title} (at booking)", text=text))
        db.commit()
    _tell_everyone(db, shoot, again)
    return {"token": shoot.book_token, "again": again}


def _when_words(shoot) -> str:
    if not shoot.shoot_date:
        return ""
    return f"{DAY_NAMES[shoot.shoot_date.weekday()]} {shoot.shoot_date.day} {shoot.shoot_date:%B %Y} at {shoot.shoot_time or ''}".strip()


def _tell_everyone(db: Session, shoot, again: bool):
    b = business.settings(db)
    studio = business.studio_name(b)
    when = _when_words(shoot)
    # the team: a line in the general chat (the bell picks the booking up on its own)
    try:
        import team
        ch = db.query(team.ChatChannel).filter(team.ChatChannel.kind == "channel", team.ChatChannel.name == "general").first()
        if ch:
            svc = ", ".join(x.get("name", "") for x in json.loads(shoot.services or "[]"))
            text = (f"New booking{' (booked again)' if again else ''}: {shoot.address} - {when}"
                    f"{f' - {svc}' if svc else ''}. Booked by {shoot.agent}"
                    f"{f' ({shoot.agency})' if shoot.agency else ''}, {shoot.booked_phone or ''}"
                    f"{f'. Owner {shoot.owner_name} {shoot.owner_phone or ''}' if shoot.owner_name else ''}.")
            m = team.ChatMessage(channel_id=ch.id, user_id=0, body=text, attachments="[]")
            db.add(m)
            db.commit()
            for uid in team.audience(db, ch):
                u = db.query(User).filter(User.id == uid).first()
                if u:
                    team._push({uid}, {"type": "message", "message": team._msg_out(db, m, u), "channel_kind": ch.kind})
    except Exception as e:
        print(f"booking: chat notice: {e}", flush=True)
    # email, when the studio has a mail server set up
    ics = ics_file([ics_event(shoot, studio)], f"{studio} shoot").encode()
    lines = [f"{studio} will photograph {shoot.address} on {when}."]
    try:
        svc = [x.get("name", "") for x in json.loads(shoot.services or "[]")]
        if svc:
            lines.append("Booked: " + ", ".join(svc) + ".")
    except (TypeError, ValueError):
        pass
    lines += ["", "Please have the property ready: lights on, blinds open, cars out of the driveway, counters clear.",
              "", f"Booked by {shoot.agent}{f' ({shoot.agency})' if shoot.agency else ''}, {shoot.booked_phone or ''}.",
              f"{studio}: {b.get('studio_phone') or ''} {b.get('studio_email') or ''}".strip()]
    body = "\n".join(lines)
    att = [("shoot.ics", ics, "text/calendar")]
    send_mail(db, [shoot.booked_email or "", shoot.owner_email or ""], f"Shoot booked: {shoot.address}, {when}", body, att)
    if b.get("studio_email"):
        docs = db.query(ShootDocument).filter(ShootDocument.shoot_id == shoot.id, ShootDocument.kind == "mandate").all()
        files = list(att)
        for d in docs[-1:]:
            p = _docs_root() / str(shoot.id) / d.file
            if p.is_file() and p.stat().st_size < 10 * 1024 * 1024:
                files.append((d.name, p.read_bytes(), "application/octet-stream"))
        send_mail(db, [b["studio_email"]], f"New booking: {shoot.address}, {when}",
                  body + f"\n\nOwner: {shoot.owner_name or '-'} {shoot.owner_phone or ''} {shoot.owner_email or ''}", files)


def _by_token(db: Session, token: str):
    shoot = db.query(shoots.Shoot).filter(shoots.Shoot.book_token == token).first() if token else None
    if not shoot:
        raise HTTPException(status_code=404, detail="No such booking")
    return shoot


@router.get("/api/public/booking/{token}")
def booking_done(token: str, db: Session = Depends(get_db)):
    shoot = _by_token(db, token)
    b = business.settings(db)
    return {"address": shoot.address, "suburb": shoot.suburb, "date": shoot.shoot_date.isoformat() if shoot.shoot_date else None,
            "time": shoot.shoot_time, "when": _when_words(shoot), "agent": shoot.agent, "owner": shoot.owner_name,
            "services": json.loads(shoot.services or "[]"), "studio": business.studio_name(b),
            "studio_phone": b.get("studio_phone") or "", "studio_email": b.get("studio_email") or "",
            "status": shoot.status}


@router.get("/api/public/booking/{token}/shoot.ics")
def booking_ics(token: str, db: Session = Depends(get_db)):
    shoot = _by_token(db, token)
    studio = business.studio_name(business.settings(db))
    return Response(ics_file([ics_event(shoot, studio)], f"{studio} shoot"), media_type="text/calendar",
                    headers={"Content-Disposition": 'attachment; filename="shoot.ics"'})


@router.get("/api/public/calendar/{key}.ics")
def calendar_feed(key: str, db: Session = Depends(get_db)):
    s = book_settings(db)
    if not key or not secrets.compare_digest(key, s.get("calendar_key") or ""):
        raise HTTPException(status_code=404, detail="Not found")
    since = date.today() - timedelta(days=60)
    rows = db.query(shoots.Shoot).filter(shoots.Shoot.shoot_date >= since).all()
    studio = business.studio_name(business.settings(db))
    return Response(ics_file([ics_event(r, studio) for r in rows], f"{studio} shoots"), media_type="text/calendar")


# --------------------------------------------------------------------------
# a property's documents (staff)
# --------------------------------------------------------------------------

def _may(user: User):
    if not permissions.can(user.role, permissions.SHOOTS):
        raise HTTPException(status_code=403, detail="Not allowed")


@router.get("/api/shoots/{shoot_id}/documents")
def list_docs(shoot_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _may(current_user)
    rows = db.query(ShootDocument).filter(ShootDocument.shoot_id == shoot_id).order_by(ShootDocument.at.desc()).all()
    return {"documents": [_doc_dict(d) for d in rows]}


@router.post("/api/shoots/{shoot_id}/documents")
def add_doc(shoot_id: int, file: UploadFile = File(...), kind: str = Form("other"), db: Session = Depends(get_db),
            current_user: User = Depends(get_current_user)):
    _may(current_user)
    if not db.query(shoots.Shoot).filter(shoots.Shoot.id == shoot_id).first():
        raise HTTPException(status_code=404, detail="No such property")
    return _doc_dict(save_document(db, shoot_id, file, kind, current_user.username))


@router.get("/api/shoots/{shoot_id}/documents/{doc_id}")
def get_doc(shoot_id: int, doc_id: int, token: Optional[str] = None, request: Request = None, db: Session = Depends(get_db)):
    from auth import get_user_from_token
    tok = token or (request.headers.get("authorization") or "").replace("Bearer ", "").strip()
    user = get_user_from_token(tok, db)
    _may(user)
    d = db.query(ShootDocument).filter(ShootDocument.id == doc_id, ShootDocument.shoot_id == shoot_id).first()
    p = _docs_root() / str(shoot_id) / d.file if d else None
    if not d or not p.is_file():
        raise HTTPException(status_code=404, detail="That document is gone")
    return FileResponse(str(p), filename=d.name)


@router.delete("/api/shoots/{shoot_id}/documents/{doc_id}")
def del_doc(shoot_id: int, doc_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _may(current_user)
    d = db.query(ShootDocument).filter(ShootDocument.id == doc_id, ShootDocument.shoot_id == shoot_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="No such document")
    p = _docs_root() / str(shoot_id) / d.file
    # kept in the library's trash rather than gone for good
    try:
        if p.is_file():
            trash = (shoots._media_root or _docs_root().parent) / "_Trash" / "_documents"
            trash.mkdir(parents=True, exist_ok=True)
            p.replace(trash / f"{shoot_id}_{d.file}")
    except OSError as e:
        print(f"booking: could not move document to trash: {e}", flush=True)
    db.delete(d)
    db.commit()
    return {"ok": True}


def install(app):
    Base.metadata.create_all(bind=engine, tables=[ShootDocument.__table__])
    app.include_router(router)
