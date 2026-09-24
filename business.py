"""The business side of a job: the studio's details, its terms and how clients pay.

Terms: with them switched on (Manage > Business), a client's portal opens on the studio's
terms first. The viewer types their name and ticks "I agree"; only then do the photos show.
Every agreement is kept with the exact words agreed to, the name, the time, the internet
address and the browser, so it stands as a signed contract. You see it on the property.

Paying: a portal of a property that is not paid yet has a Pay now tab with the amount, the
reference and a button to the studio's own payment page (a PayFast, Yoco, SnapScan or bank
link - Zerko never takes the money itself), plus the bank details for a transfer. The client
can say "I have paid", which tells you; switching Paid on for the property (as before) takes
the watermarks off and opens the downloads.

The settings live in the database (not a file next to the code), so a test copy of Zerko
has its own."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.orm import Session

import permissions
from auth import get_current_user
from database import Base, Share, User, engine, get_db

router = APIRouter(prefix="/api/business", tags=["business"])

DEFAULT_TERMS = """1. What you get
{studio} took these photos and videos to market this property. Once the invoice is paid, you may use them to advertise, sell or let this property: on listing sites, on social media, in print and in your agency's own marketing.

2. Before payment
Until payment is received, the photos stay watermarked and may not be downloaded, screenshotted, copied, edited or used anywhere. Using them before paying is using them without permission, and the full fee plus any costs of recovering it are due.

3. Copyright
{studio} keeps the copyright. The photos may not be sold, given to another agency, used for another property or used for anything else without asking us first. We may show them in our own portfolio.

4. Changes
Please do not crop out our marks, or edit the photos in a way that changes what the property looks like.

5. Payment
The invoice is due within {due_days} days of delivery.

6. Cancelling
A shoot cancelled less than 24 hours before its time may be charged a call-out fee.

7. Agreeing
Typing your name and ticking the box counts as signing these terms. We keep a record of your name, the time, your internet address and the terms you agreed to."""

DEFAULTS = {
    "studio_name": "",
    "studio_email": "",
    "studio_phone": "",
    "studio_address": "",
    "vat_no": "",
    "currency": "R",
    "terms_on": False,
    "terms_title": "Terms of use",
    "terms_text": DEFAULT_TERMS,
    "pay_link": "",          # may hold {amount} and {ref}
    "pay_note": "",          # bank details for a transfer
    "invoice_prefix": "INV-",
    "due_days": 7,
    "vat_rate": 0,           # percent on invoices (15 for a VAT-registered studio in South Africa)
}


class BusinessSetting(Base):
    __tablename__ = "business_settings"
    key = Column(String, primary_key=True)
    value = Column(Text, nullable=True)


class TermsAgreement(Base):
    """One person agreeing to the terms on a portal: the contract, as it was signed."""
    __tablename__ = "terms_agreements"
    id = Column(Integer, primary_key=True)
    share_id = Column(Integer, nullable=False, index=True)
    shoot_id = Column(Integer, nullable=True, index=True)
    name = Column(String, nullable=False)
    email = Column(String, nullable=True)
    at = Column(DateTime, default=datetime.utcnow, nullable=False)
    ip = Column(String, nullable=True)
    browser = Column(String, nullable=True)
    version = Column(String, nullable=True)       # a short fingerprint of the words
    title = Column(String, nullable=True)
    text = Column(Text, nullable=False)           # exactly what they agreed to


def settings(db: Session) -> dict:
    out = dict(DEFAULTS)
    for row in db.query(BusinessSetting).all():
        if row.key in DEFAULTS:
            try:
                out[row.key] = json.loads(row.value) if row.value is not None else DEFAULTS[row.key]
            except ValueError:
                pass
    return out


def studio_name(s: dict) -> str:
    return (s.get("studio_name") or "").strip() or "The studio"


def terms_words(s: dict) -> str:
    text = (s.get("terms_text") or "").strip() or DEFAULT_TERMS
    return text.replace("{studio}", studio_name(s)).replace("{due_days}", str(s.get("due_days") or 7))


def _fingerprint(title: str, text: str) -> str:
    return hashlib.sha256(f"{title}\n{text}".encode("utf-8")).hexdigest()[:12]


def terms_needed(db: Session, share: Share, s: Optional[dict] = None) -> bool:
    """Does this portal ask for the terms at all (on in Manage > Business, a portal that shows
    something, and not switched off on the portal)?"""
    s = s or settings(db)
    return bool(s.get("terms_on")) and (share.kind or "send") != "receive" and share.ask_terms is not False


def agreement(db: Session, share: Share) -> Optional[TermsAgreement]:
    return (db.query(TermsAgreement).filter(TermsAgreement.share_id == share.id)
            .order_by(TermsAgreement.at.desc()).first())


def terms_pending(db: Session, share: Share) -> bool:
    """True while the viewer still has to agree before anything of the portal shows."""
    try:
        return terms_needed(db, share) and agreement(db, share) is None
    except Exception as e:                                   # never lock a portal by accident
        print(f"business: terms check: {e}", flush=True)
        return False


def portal_terms(db: Session, share: Share) -> Optional[dict]:
    s = settings(db)
    if not terms_needed(db, share, s):
        return None
    a = agreement(db, share)
    title, text = s.get("terms_title") or "Terms of use", terms_words(s)
    return {"title": title, "text": text, "studio": studio_name(s), "version": _fingerprint(title, text),
            "agreed": {"name": a.name, "at": a.at.isoformat() + "Z"} if a else None}


def reference(s: dict, shoot) -> str:
    return (shoot.invoice_no or "").strip() or f"{s.get('invoice_prefix') or ''}{shoot.id}"


def money(s: dict, amount: Optional[float]) -> str:
    if not amount:
        return ""
    cur = s.get("currency") or ""
    whole = abs(amount - round(amount)) < 0.005
    num = f"{amount:,.0f}" if whole else f"{amount:,.2f}"
    return f"{cur} {num.replace(',', ' ')}".strip()


def pay_link(s: dict, shoot) -> str:
    link = (s.get("pay_link") or "").strip()
    if not link:
        return ""
    amt = f"{shoot.fee:.2f}" if shoot.fee else ""
    return link.replace("{amount}", quote(amt)).replace("{ref}", quote(reference(s, shoot)))


def portal_pay(db: Session, share: Share) -> Optional[dict]:
    """The Pay now tab: only on a portal of a property that is not paid yet."""
    if not share.shoot_id:
        return None
    import shoots
    shoot = db.query(shoots.Shoot).filter(shoots.Shoot.id == share.shoot_id).first()
    if not shoot or shoot.paid_at is not None:
        return None
    s = settings(db)
    return {"amount": shoot.fee, "amount_text": money(s, shoot.fee), "reference": reference(s, shoot),
            "link": pay_link(s, shoot), "bank": (s.get("pay_note") or "").strip(),
            "due_days": s.get("due_days") or 7, "studio": studio_name(s),
            "claimed_at": shoot.pay_claimed_at.isoformat() + "Z" if shoot.pay_claimed_at else None}


# --------------------------------------------------------------------------
# the studio's side
# --------------------------------------------------------------------------

class SettingsBody(BaseModel):
    studio_name: Optional[str] = Field(None, max_length=120)
    studio_email: Optional[str] = Field(None, max_length=200)
    studio_phone: Optional[str] = Field(None, max_length=60)
    studio_address: Optional[str] = Field(None, max_length=400)
    vat_no: Optional[str] = Field(None, max_length=40)
    currency: Optional[str] = Field(None, max_length=8)
    terms_on: Optional[bool] = None
    terms_title: Optional[str] = Field(None, max_length=120)
    terms_text: Optional[str] = Field(None, max_length=20000)
    pay_link: Optional[str] = Field(None, max_length=1000)
    pay_note: Optional[str] = Field(None, max_length=2000)
    invoice_prefix: Optional[str] = Field(None, max_length=20)
    due_days: Optional[int] = Field(None, ge=0, le=365)
    vat_rate: Optional[float] = Field(None, ge=0, le=50)


@router.get("/settings")
def get_settings(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    s = settings(db)
    return {**s, "default_terms": DEFAULT_TERMS}


@router.put("/settings")
def put_settings(body: SettingsBody, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrators only")
    link = (body.pay_link or "").strip()
    if link and not link.lower().startswith(("https://", "http://")):
        raise HTTPException(status_code=400, detail="The payment link should start with https://")
    for k, v in body.model_dump(exclude_none=True).items():
        if isinstance(v, str):
            v = v.strip()
        row = db.query(BusinessSetting).filter(BusinessSetting.key == k).first()
        if not row:
            row = BusinessSetting(key=k)
            db.add(row)
        row.value = json.dumps(v)
    db.commit()
    return get_settings(db, current_user)


def _agreement_dict(a: TermsAgreement, share_title: Optional[str] = None) -> dict:
    return {"id": a.id, "share_id": a.share_id, "shoot_id": a.shoot_id, "name": a.name, "email": a.email,
            "at": a.at.isoformat() + "Z", "ip": a.ip, "browser": a.browser, "version": a.version,
            "title": a.title, "portal": share_title}


@router.get("/agreements")
def list_agreements(shoot_id: Optional[int] = None, share_id: Optional[int] = None, db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    if not permissions.can(current_user.role, permissions.SHARES):
        raise HTTPException(status_code=403, detail="Not allowed")
    q = db.query(TermsAgreement)
    if shoot_id is not None:
        # a property's portals, also ones linked to it after the agreement was made
        ids = [x.id for x in db.query(Share.id).filter(Share.shoot_id == shoot_id).all()]
        q = q.filter((TermsAgreement.shoot_id == shoot_id) | (TermsAgreement.share_id.in_(ids or [0])))
    if share_id is not None:
        q = q.filter(TermsAgreement.share_id == share_id)
    rows = q.order_by(TermsAgreement.at.desc()).limit(200).all()
    titles = {s.id: s.title for s in db.query(Share).filter(Share.id.in_({a.share_id for a in rows} or {0})).all()}
    return {"agreements": [_agreement_dict(a, titles.get(a.share_id)) for a in rows]}


@router.get("/agreements/{agreement_id}")
def get_agreement(agreement_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if not permissions.can(current_user.role, permissions.SHARES):
        raise HTTPException(status_code=403, detail="Not allowed")
    a = db.query(TermsAgreement).filter(TermsAgreement.id == agreement_id).first()
    if not a:
        raise HTTPException(status_code=404, detail="No such agreement")
    return {**_agreement_dict(a), "text": a.text}


# --------------------------------------------------------------------------
# the client's side (on the portal; the portal's own token and password)
# --------------------------------------------------------------------------

class AgreeBody(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    email: Optional[str] = Field(None, max_length=200)
    agree: bool = False
    version: Optional[str] = Field(None, max_length=40)


def install(app, share_state):
    """share_state(db, token, password) -> Share: main's check of a portal link."""
    Base.metadata.create_all(bind=engine, tables=[BusinessSetting.__table__, TermsAgreement.__table__])
    app.include_router(router)

    @app.get("/api/public/studio")
    def public_studio(db: Session = Depends(get_db)):
        """The studio's name for the header of client pages (empty until it is filled in)."""
        return {"name": (settings(db).get("studio_name") or "").strip()}

    @app.post("/api/public/share/{token}/agree")
    def agree(token: str, request: Request, body: AgreeBody, password: Optional[str] = None,
              db: Session = Depends(get_db)):
        import delivery
        share = share_state(db, token, password)
        s = settings(db)
        if not terms_needed(db, share, s):
            return {"ok": True}
        if not body.agree:
            raise HTTPException(status_code=400, detail="Tick the box to agree to the terms.")
        title, text = s.get("terms_title") or "Terms of use", terms_words(s)
        ver = _fingerprint(title, text)
        if body.version and body.version != ver:
            raise HTTPException(status_code=409, detail="The terms were just changed - read them again before agreeing.")
        fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        ip = fwd or (request.client.host if request.client else None)
        a = TermsAgreement(share_id=share.id, shoot_id=share.shoot_id, name=body.name.strip(),
                           email=(body.email or "").strip() or None, ip=ip,
                           browser=(request.headers.get("user-agent") or "")[:300], version=ver, title=title, text=text)
        db.add(a)
        db.commit()
        delivery.log(db, share, delivery.TERMS, detail=title, viewer_name=a.name, request=request)
        return {"ok": True, "agreed": {"name": a.name, "at": a.at.isoformat() + "Z"}}

    @app.post("/api/public/share/{token}/paid")
    def say_paid(token: str, request: Request, payload: dict = Body(default={}), password: Optional[str] = None,
                 db: Session = Depends(get_db)):
        import delivery
        import shoots
        share = share_state(db, token, password)
        if terms_pending(db, share):
            raise HTTPException(status_code=403, detail="Agree to the terms first.")
        shoot = db.query(shoots.Shoot).filter(shoots.Shoot.id == share.shoot_id).first() if share.shoot_id else None
        if not shoot:
            raise HTTPException(status_code=400, detail="This link is not for a property.")
        if shoot.paid_at is None:
            shoot.pay_claimed_at = datetime.utcnow()
            db.commit()
            who = str((payload or {}).get("name") or "")[:120] or None
            delivery.log(db, share, delivery.PAID_CLAIM, detail=reference(settings(db), shoot), viewer_name=who, request=request)
        return {"ok": True, "claimed_at": shoot.pay_claimed_at.isoformat() + "Z" if shoot.pay_claimed_at else None}


def terms_version(db: Session) -> str:
    s = settings(db)
    return _fingerprint(s.get("terms_title") or "Terms of use", terms_words(s))
