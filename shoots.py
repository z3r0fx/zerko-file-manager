"""Shoots, keyed by the property's address.

A folder tells you where files are; it does not tell you whose house it was,
whether the pictures went out, or what is still owed. A shoot does. The
address IS the identity here - one property, one shoot, however many folders
of stills, video and exports end up hanging off it.

Addresses are matched on a normalised key, so "12 Ocean View Dr" and
"12 Ocean View Drive" are the same property and cannot be entered twice.
"""

import os
import re
from datetime import datetime, date
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import (Column, Date, DateTime, Float, ForeignKey, Integer, String,
                        Text, func)
from sqlalchemy.orm import Session

import permissions
from auth import get_current_user
from database import Base, IndexedFolder, User, Video, engine, get_db, SessionLocal

router = APIRouter(prefix="/api/shoots", tags=["shoots"])

_media_root: Optional[Path] = None
_resolve = lambda p: p

# Where a job is up to. Deliberately short: a longer list is a list nobody
# keeps up to date.
# Delivered is the end of the line: payments are not handled here.
STATUSES = ["booked", "shot", "editing", "posted"]
DEFAULT_STATUS = "shot"


# --------------------------------------------------------------------------
# the address key
# --------------------------------------------------------------------------

# Only the ones that actually turn up in South African addresses. Anything
# not on this list is left alone rather than guessed at.
_ABBREV = {
    "st": "street", "str": "street", "rd": "road", "ave": "avenue",
    "av": "avenue", "dr": "drive", "cres": "crescent", "cl": "close",
    "ln": "lane", "pl": "place", "sq": "square", "ter": "terrace",
    "blvd": "boulevard", "hwy": "highway", "apt": "apartment",
    "no": "", "nr": "",
}


def _no_ward(s):
    """A map lookup sometimes gives a municipal ward ("Cape Town Ward 115")
    where the suburb should be; that is no use as a suburb."""
    if isinstance(s, str) and re.search(r"\bWard\s*\d+", s):
        return None
    return s


def address_key(address: str) -> str:
    """A form of the address that survives how people type it.

    Case, punctuation, doubled spaces and the usual abbreviations all come
    out, so the same property entered twice collides instead of becoming two
    shoots that each hold half the job.
    """
    s = (address or "").lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    words = [w for w in s.split() if w]
    out = []
    for w in words:
        w = _ABBREV.get(w, w)
        if w:
            out.append(w)
    return " ".join(out)


class Shoot(Base):
    __tablename__ = "shoots"
    id = Column(Integer, primary_key=True)
    # What you would write on an invoice.
    address = Column(String, nullable=False)
    # What we match on. Unique, so the same place cannot be booked twice.
    address_key = Column(String, nullable=False, unique=True, index=True)
    suburb = Column(String, nullable=True)
    agent = Column(String, nullable=True)
    status = Column(String, nullable=False, default=DEFAULT_STATUS)
    shoot_date = Column(Date, nullable=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(String, nullable=True)
    # Where it is, for the map. Set from an address search or a dropped pin.
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)
    # The photo that stands for the property on the map and in the list.
    cover_video_id = Column(Integer, nullable=True)
    # When the shoot is booked for ("09:30"), for the calendar.
    shoot_time = Column(String, nullable=True)
    # When it moved to Editing and to Posted - how long jobs take, per agent.
    editing_at = Column(DateTime, nullable=True)
    posted_at = Column(DateTime, nullable=True)
    # What the listing says: fed to the caption writer.
    listing = Column(String, nullable=True)          # "sale" | "rent"
    kind = Column(String, nullable=True)             # house | apartment | townhouse | plot | commercial
    price = Column(Integer, nullable=True)
    beds = Column(Float, nullable=True)
    baths = Column(Float, nullable=True)
    parking = Column(Integer, nullable=True)
    floor_m2 = Column(Integer, nullable=True)
    erf_m2 = Column(Integer, nullable=True)
    features = Column(Text, nullable=True)           # one per line
    # When the client paid. Until then the property's portals show the photos
    # watermarked and refuse downloads. Zerko never takes the payment itself.
    paid_at = Column(DateTime, nullable=True)
    # What the client owes for the shoot, and the reference they pay with (Manage > Business
    # makes one from the invoice prefix when this is empty). The client may say "I have paid"
    # on the portal; it opens once you switch Paid on.
    fee = Column(Float, nullable=True)
    invoice_no = Column(String, nullable=True)
    pay_claimed_at = Column(DateTime, nullable=True)
    # a booking (booking.py): the owner, who booked (the agent's contacts), what was booked, and the link
    # of its confirmation page
    owner_name = Column(String, nullable=True)
    owner_phone = Column(String, nullable=True)
    owner_email = Column(String, nullable=True)
    agency = Column(String, nullable=True)
    booked_email = Column(String, nullable=True)
    booked_phone = Column(String, nullable=True)
    services = Column(Text, nullable=True)            # JSON [{name, price, minutes}]
    booked_at = Column(DateTime, nullable=True)
    book_token = Column(String, nullable=True, index=True)


class Agent(Base):
    """The estate agents you shoot for, picked from a list on each property
    instead of typed afresh (and spelt three ways). A property keeps the
    agent's name as text, so renaming an agent renames it on its properties."""
    __tablename__ = "agents"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True, index=True)
    agency = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    email = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ShootFolder(Base):
    """A folder that belongs to a shoot. One shoot, many folders - stills and
    video from the same property usually live apart."""
    __tablename__ = "shoot_folders"
    id = Column(Integer, primary_key=True)
    shoot_id = Column(Integer, ForeignKey("shoots.id"), nullable=False, index=True)
    path = Column(String, nullable=False, unique=True)
    added_at = Column(DateTime, default=datetime.utcnow)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _norm_path(p: str) -> str:
    return str(p or "").replace("\\", "/").rstrip("/")


def shoot_for_path(db: Session, path: str) -> Optional[Shoot]:
    """The shoot a file or folder sits under, if any.

    Walks up the tree, so a photo three folders deep still finds its shoot.
    """
    p = _norm_path(path)
    while p:
        row = db.query(ShootFolder).filter(ShootFolder.path == p).first()
        if row:
            return db.query(Shoot).filter(Shoot.id == row.shoot_id).first()
        cut = p.rfind("/")
        if cut <= 0:
            break
        p = p[:cut]
    return None


def _counts(db: Session, shoot: Shoot) -> dict:
    paths = [f.path for f in
             db.query(ShootFolder).filter(ShootFolder.shoot_id == shoot.id).all()]
    if not paths:
        return {"folders": 0, "files": 0, "photos": 0, "videos": 0}
    files = photos = videos = 0
    for p in paths:
        like = p + "/%"
        rows = db.query(Video.media_type, func.count(Video.id)) \
                 .filter((Video.filepath == p) | (Video.filepath.like(like))) \
                 .group_by(Video.media_type).all()
        for media_type, n in rows:
            files += n
            if media_type == "photo":
                photos += n
            elif media_type == "video":
                videos += n
    return {"folders": len(paths), "files": files,
            "photos": photos, "videos": videos}


def _folder_ids(db: Session, paths: List[str]) -> List[Optional[int]]:
    """The library's folder id for each attached path (None if not indexed)."""
    out = []
    for p in paths:
        row = (db.query(IndexedFolder).filter(IndexedFolder.path == p).first()
               or db.query(IndexedFolder).filter(IndexedFolder.path == p.replace("/", "\\")).first())
        out.append(row.id if row else None)
    return out


def _cover(db: Session, shoot: Shoot, paths: List[str]) -> Optional[dict]:
    """The chosen cover, or else the best-rated photo on the shoot."""
    v = None
    if shoot.cover_video_id:
        v = db.query(Video).filter(Video.id == shoot.cover_video_id).first()
    if v is None:
        for p in paths:
            v = (db.query(Video)
                 .filter(Video.media_type == "photo", Video.is_active.isnot(False),
                         (Video.filepath == p) | Video.filepath.like(p + "/%"))
                 .order_by(Video.rating.desc().nullslast(), Video.id.asc()).first())
            if v:
                break
    if v is None:
        # no photos yet: a video's thumbnail is better than nothing
        for p in paths:
            v = (db.query(Video)
                 .filter(Video.thumbnail_path.isnot(None), Video.is_active.isnot(False),
                         (Video.filepath == p) | Video.filepath.like(p + "/%"))
                 .order_by(Video.rating.desc().nullslast(), Video.id.asc()).first())
            if v:
                break
    if not v:
        return None
    return {"id": v.id, "filename": v.filename, "thumbnail_path": v.thumbnail_path,
            "folder_id": v.folder_id}


def _json_list(s) -> list:
    import json
    try:
        v = json.loads(s or "[]")
        return v if isinstance(v, list) else []
    except ValueError:
        return []


def _as_dict(db: Session, shoot: Shoot, with_counts: bool = True) -> dict:
    paths = [f.path for f in db.query(ShootFolder)
             .filter(ShootFolder.shoot_id == shoot.id)
             .order_by(ShootFolder.path).all()]
    out = {
        "id": shoot.id,
        "address": shoot.address,
        "suburb": shoot.suburb,
        "agent": shoot.agent,
        "status": shoot.status,
        "shoot_date": shoot.shoot_date.isoformat() if shoot.shoot_date else None,
        "note": shoot.note,
        "created_at": shoot.created_at.isoformat() if shoot.created_at else None,
        "created_by": shoot.created_by,
        "folders": paths,
        "folder_ids": _folder_ids(db, paths),
        "lat": shoot.lat,
        "lng": shoot.lng,
        "shoot_time": shoot.shoot_time,
        "editing_at": shoot.editing_at.isoformat() if shoot.editing_at else None,
        "posted_at": shoot.posted_at.isoformat() if shoot.posted_at else None,
        "facts": facts_of(shoot),
        "paid_at": shoot.paid_at.isoformat() if shoot.paid_at else None,
        "fee": shoot.fee,
        "invoice_no": shoot.invoice_no,
        "pay_claimed_at": shoot.pay_claimed_at.isoformat() if shoot.pay_claimed_at else None,
        "owner": {"name": shoot.owner_name, "phone": shoot.owner_phone, "email": shoot.owner_email},
        "booking": ({"agency": shoot.agency, "email": shoot.booked_email, "phone": shoot.booked_phone,
                     "services": _json_list(shoot.services), "at": shoot.booked_at.isoformat() if shoot.booked_at else None,
                     "token": shoot.book_token} if shoot.booked_at else None),
    }
    if with_counts:
        out["counts"] = _counts(db, shoot)
        out["cover"] = _cover(db, shoot, paths)
        try:
            import projects
            proj = None
            for p in paths:
                proj = db.query(projects.Project).filter(
                    (projects.Project.path == p) | (projects.Project.path == p.replace("/", "\\"))).first()
                if proj:
                    break
            out["project_id"] = proj.id if proj else None
        except Exception:
            out["project_id"] = None
    return out


FACT_KEYS = ("kind", "listing", "price", "beds", "baths", "parking", "floor_m2", "erf_m2")


def facts_of(shoot: Shoot) -> dict:
    """What the listing says, as the caption writer and the form see it."""
    out = {k: getattr(shoot, k) for k in FACT_KEYS}
    out["features"] = [f.strip() for f in (shoot.features or "").splitlines() if f.strip()]
    return out


def _time(v: Optional[str]) -> Optional[str]:
    v = (v or "").strip()
    if not v:
        return None
    m = re.match(r"^(\d{1,2})[:h.](\d{2})$", v)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise HTTPException(status_code=400, detail="The time should look like 09:30.")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _set_status(shoot: Shoot, status: str) -> None:
    """Move a property along, remembering when it got to each step (the
    first time: going back and forth does not reset the clock)."""
    if status not in STATUSES:
        raise HTTPException(status_code=400,
                            detail=f"Status should be one of: {', '.join(STATUSES)}")
    now = datetime.utcnow()
    if status in ("editing", "posted") and not shoot.editing_at:
        shoot.editing_at = now
    if status == "posted" and not shoot.posted_at:
        shoot.posted_at = now
    shoot.status = status


class Facts(BaseModel):
    kind: Optional[str] = None
    listing: Optional[str] = None
    price: Optional[int] = Field(None, ge=0)
    beds: Optional[float] = Field(None, ge=0, le=100)
    baths: Optional[float] = Field(None, ge=0, le=100)
    parking: Optional[int] = Field(None, ge=0, le=100)
    floor_m2: Optional[int] = Field(None, ge=0)
    erf_m2: Optional[int] = Field(None, ge=0)
    features: Optional[List[str]] = None


def _check_location(lat, lng):
    if (lat is None) != (lng is None):
        raise HTTPException(status_code=400, detail="A location needs both a latitude and a longitude.")
    if lat is not None and not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise HTTPException(status_code=400, detail="That location is off the map.")


def _may_edit(user: User):
    if not permissions.can(user.role, permissions.SHOOTS):
        raise HTTPException(status_code=403,
                            detail="Your account cannot change properties.")


def _parse_date(value) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400,
                            detail="The date should look like 2026-09-22.")


# --------------------------------------------------------------------------
# api
# --------------------------------------------------------------------------

class NewShoot(BaseModel):
    address: str = Field(..., min_length=3, max_length=300)
    suburb: Optional[str] = Field(None, max_length=120)
    agent: Optional[str] = Field(None, max_length=120)
    shoot_date: Optional[str] = None
    note: Optional[str] = Field(None, max_length=2000)
    folders: List[str] = []
    lat: Optional[float] = None
    lng: Optional[float] = None
    shoot_time: Optional[str] = None


class EditShoot(BaseModel):
    address: Optional[str] = Field(None, min_length=3, max_length=300)
    suburb: Optional[str] = Field(None, max_length=120)
    agent: Optional[str] = Field(None, max_length=120)
    status: Optional[str] = None
    shoot_date: Optional[str] = None
    note: Optional[str] = Field(None, max_length=2000)
    lat: Optional[float] = None
    lng: Optional[float] = None
    clear_location: bool = False
    cover_video_id: Optional[int] = None
    shoot_time: Optional[str] = None
    # the listing facts; a key sent as null clears it
    facts: Optional[dict] = None
    # paid (downloads open on its portals) or not paid yet
    paid: Optional[bool] = None
    # what the client owes (0 or less clears it) and the invoice / payment reference
    fee: Optional[float] = Field(None, ge=-1, le=10_000_000)
    invoice_no: Optional[str] = Field(None, max_length=40)
    # the owner's contacts ({name, phone, email}; a key sent as "" clears it)
    owner: Optional[dict] = None


class FolderRef(BaseModel):
    path: str


@router.get("")
def list_shoots(q: str = "", status: str = "", limit: int = 200,
                db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    """Every shoot, newest first. `q` matches the address the way a person
    would type it, so "ocean view dr" finds "12 Ocean View Drive"."""
    query = db.query(Shoot)
    if status:
        if status not in STATUSES:
            raise HTTPException(status_code=400,
                                detail=f"Status should be one of: {', '.join(STATUSES)}")
        query = query.filter(Shoot.status == status)
    if q.strip():
        key = address_key(q)
        if key:
            query = query.filter(Shoot.address_key.like(f"%{key}%"))
    rows = query.order_by(Shoot.shoot_date.desc().nullslast(),
                          Shoot.created_at.desc()).limit(max(1, min(1000, limit))).all()
    return {"shoots": [_as_dict(db, s) for s in rows],
            "statuses": STATUSES}


@router.post("")
def create_shoot(body: NewShoot, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    _may_edit(current_user)
    key = address_key(body.address)
    if not key:
        raise HTTPException(status_code=400, detail="That is not an address.")
    existing = db.query(Shoot).filter(Shoot.address_key == key).first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"{existing.address} is already registered as a property.")

    _check_location(body.lat, body.lng)
    shoot = Shoot(address=body.address.strip(), address_key=key, lat=body.lat, lng=body.lng,
                  suburb=(body.suburb or "").strip() or None,
                  agent=(body.agent or "").strip() or None,
                  status=DEFAULT_STATUS,
                  shoot_date=_parse_date(body.shoot_date),
                  shoot_time=_time(body.shoot_time),
                  note=(body.note or "").strip() or None,
                  created_by=current_user.username)
    db.add(shoot)
    _remember_agent(db, shoot.agent)
    db.commit()
    db.refresh(shoot)
    for p in body.folders:
        _attach(db, shoot, p)
    db.commit()
    return _as_dict(db, shoot)


@router.get("/{shoot_id}")
def get_shoot(shoot_id: int, db: Session = Depends(get_db),
              current_user: User = Depends(get_current_user)):
    shoot = db.query(Shoot).filter(Shoot.id == shoot_id).first()
    if not shoot:
        raise HTTPException(status_code=404, detail="No such shoot")
    return _as_dict(db, shoot)


@router.patch("/{shoot_id}")
def edit_shoot(shoot_id: int, body: EditShoot, db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    _may_edit(current_user)
    shoot = db.query(Shoot).filter(Shoot.id == shoot_id).first()
    if not shoot:
        raise HTTPException(status_code=404, detail="No such shoot")

    if body.address is not None:
        key = address_key(body.address)
        if not key:
            raise HTTPException(status_code=400, detail="That is not an address.")
        clash = db.query(Shoot).filter(Shoot.address_key == key,
                                       Shoot.id != shoot.id).first()
        if clash:
            raise HTTPException(status_code=409,
                                detail=f"{clash.address} is already registered as a property.")
        shoot.address = body.address.strip()
        shoot.address_key = key
    if body.status is not None:
        _set_status(shoot, body.status)
    if body.shoot_time is not None:
        shoot.shoot_time = _time(body.shoot_time)
    if body.facts is not None:
        f = Facts(**body.facts)
        for k in body.facts:
            if k == "features":
                shoot.features = "\n".join(x.strip() for x in (f.features or []) if x and x.strip())[:4000] or None
            elif k == "listing":
                if f.listing not in (None, "", "sale", "rent"):
                    raise HTTPException(status_code=400, detail="Listing should be sale or rent.")
                shoot.listing = f.listing or None
            elif k == "kind":
                if f.kind not in (None, "", "house", "apartment", "townhouse", "plot", "commercial"):
                    raise HTTPException(status_code=400, detail="Unknown kind of property.")
                shoot.kind = f.kind or None
            elif k in FACT_KEYS:
                setattr(shoot, k, getattr(f, k))
    if body.suburb is not None:
        shoot.suburb = body.suburb.strip() or None
    if body.agent is not None:
        shoot.agent = body.agent.strip() or None
        _remember_agent(db, shoot.agent)
    if body.note is not None:
        shoot.note = body.note.strip() or None
    if body.shoot_date is not None:
        shoot.shoot_date = _parse_date(body.shoot_date)
    if body.clear_location:
        shoot.lat = shoot.lng = None
    elif body.lat is not None or body.lng is not None:
        _check_location(body.lat, body.lng)
        shoot.lat, shoot.lng = body.lat, body.lng
    if body.cover_video_id is not None:
        shoot.cover_video_id = body.cover_video_id or None
    if body.paid is not None:
        shoot.paid_at = (shoot.paid_at or datetime.utcnow()) if body.paid else None
    if body.fee is not None:
        shoot.fee = round(body.fee, 2) if body.fee > 0 else None
    if body.invoice_no is not None:
        shoot.invoice_no = body.invoice_no.strip() or None
    if body.owner is not None:
        for k in ("name", "phone", "email"):
            if k in body.owner:
                setattr(shoot, f"owner_{k}", (str(body.owner[k] or "").strip()[:200]) or None)

    db.commit()
    db.refresh(shoot)
    return _as_dict(db, shoot)


def _attach(db: Session, shoot: Shoot, path: str) -> bool:
    p = _norm_path(path)
    if not p:
        return False
    held = db.query(ShootFolder).filter(ShootFolder.path == p).first()
    if held:
        if held.shoot_id == shoot.id:
            return False
        raise HTTPException(
            status_code=409,
            detail="That folder already belongs to another property.")
    db.add(ShootFolder(shoot_id=shoot.id, path=p))
    return True


@router.post("/{shoot_id}/folders")
def add_folder(shoot_id: int, body: FolderRef, db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    _may_edit(current_user)
    shoot = db.query(Shoot).filter(Shoot.id == shoot_id).first()
    if not shoot:
        raise HTTPException(status_code=404, detail="No such shoot")
    _attach(db, shoot, body.path)
    db.commit()
    return _as_dict(db, shoot)


@router.delete("/{shoot_id}/folders")
def remove_folder(shoot_id: int, path: str, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    """Unlink a folder. The files are not touched - this only forgets that
    they belonged to this address."""
    _may_edit(current_user)
    p = _norm_path(path)
    row = db.query(ShootFolder).filter(ShootFolder.shoot_id == shoot_id,
                                       ShootFolder.path == p).first()
    if not row:
        raise HTTPException(status_code=404, detail="That folder is not on this shoot")
    db.delete(row)
    db.commit()
    shoot = db.query(Shoot).filter(Shoot.id == shoot_id).first()
    return _as_dict(db, shoot)


@router.delete("/{shoot_id}")
def delete_shoot(shoot_id: int, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    """Forget the shoot. Files and folders stay exactly where they are."""
    _may_edit(current_user)
    shoot = db.query(Shoot).filter(Shoot.id == shoot_id).first()
    if not shoot:
        raise HTTPException(status_code=404, detail="No such shoot")
    db.query(ShootFolder).filter(ShootFolder.shoot_id == shoot.id).delete()
    db.delete(shoot)
    db.commit()
    return {"status": "ok"}


_geo_last = [0.0]

# Where the work is, for addresses typed without a town: results near here come
# first, and the search looks here before it looks at the whole country.
# GEOCODE_COUNTRIES limits the search ("za" by default; empty = anywhere).
_HOME = (-33.93, 18.42)   # Cape Town
_COUNTRIES = os.environ.get("GEOCODE_COUNTRIES", "za").strip()
_UA = "ZerkoFileManager/1.3 (self-hosted property media library)"


def _km(a_lat, a_lng, b_lat, b_lng) -> float:
    import math
    r = math.radians
    d = (math.sin(r(b_lat - a_lat) / 2) ** 2
         + math.cos(r(a_lat)) * math.cos(r(b_lat)) * math.sin(r(b_lng - a_lng) / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(min(1.0, d)))


def _get_json(url: str):
    import json
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


def _short_label(parts: List[Optional[str]]) -> str:
    seen, out = set(), []
    for p in parts:
        p = (p or "").strip()
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return ", ".join(out)


def _nominatim(q: str, near, bounded: bool) -> list:
    import time
    import urllib.parse
    wait = 1.0 - (time.time() - _geo_last[0])     # their policy: one request a second
    if wait > 0:
        time.sleep(wait)
    _geo_last[0] = time.time()
    params = {"format": "jsonv2", "addressdetails": "1", "limit": "8", "q": q}
    if _COUNTRIES:
        params["countrycodes"] = _COUNTRIES
    if bounded:
        lat, lng = near
        params.update(viewbox=f"{lng - 0.6},{lat + 0.5},{lng + 0.6},{lat - 0.5}", bounded="1")
    out = []
    for d in _get_json("https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params)):
        a = d.get("address") or {}
        street = " ".join(x for x in (a.get("house_number"), a.get("road")) if x) or d.get("name")
        suburb = a.get("suburb") or a.get("neighbourhood") or a.get("quarter")
        town = a.get("city") or a.get("town") or a.get("village") or a.get("municipality")
        out.append({
            "label": _short_label([street, suburb, town]) or d.get("display_name"),
            "detail": d.get("display_name"),
            "lat": float(d["lat"]), "lng": float(d["lon"]),
            "suburb": _no_ward(suburb) or _no_ward(a.get("city_district")) or town,
            "exact": bool(a.get("house_number")),
        })
    return out


def _photon(q: str, near) -> list:
    """Photon (OpenStreetMap data, komoot's server): forgiving with typing and
    house numbers, and it ranks by distance from where the work is."""
    import urllib.parse
    lat, lng = near
    params = {"q": q, "lat": f"{lat:.4f}", "lon": f"{lng:.4f}", "limit": "8", "lang": "en"}
    out = []
    for f in (_get_json("https://photon.komoot.io/api/?" + urllib.parse.urlencode(params)).get("features") or []):
        p = f.get("properties") or {}
        if _COUNTRIES and (p.get("countrycode") or "").lower() not in _COUNTRIES.lower().split(","):
            continue
        lng2, lat2 = (f.get("geometry") or {}).get("coordinates", [None, None])[:2]
        if lat2 is None:
            continue
        street = " ".join(x for x in (p.get("housenumber"), p.get("street") or p.get("name")) if x)
        suburb = p.get("district") or p.get("locality")
        town = p.get("city") or p.get("county")
        out.append({
            "label": _short_label([street, suburb, town]),
            "detail": _short_label([street, suburb, town, p.get("state"), p.get("postcode")]),
            "lat": float(lat2), "lng": float(lng2),
            "suburb": _no_ward(suburb) or town,
            "exact": bool(p.get("housenumber")),
        })
    return out


@router.get("/geocode/search")
def geocode(q: str, lat: Optional[float] = None, lng: Optional[float] = None,
            current_user: User = Depends(get_current_user)):
    """Addresses matching what was typed, nearest to the work first.

    `lat`/`lng` say where to look first (the browser sends the middle of the
    properties already on the map); without them, Cape Town. Two OpenStreetMap
    searches are asked - Nominatim, first around there and then country-wide,
    and Photon - and the answers merged, so "31 Riana Street" finds the one
    down the road before the one in the Northern Cape. Asked by this server,
    so the browser never talks to a third party. Nothing is stored; a wrong
    result is fixed by dragging the pin.
    """
    q = (q or "").strip()
    if len(q) < 3:
        return {"results": []}
    near = (lat, lng) if lat is not None and lng is not None and -90 <= lat <= 90 and -180 <= lng <= 180 else _HOME
    found, errors = [], []
    for name, ask in (("photon", lambda: _photon(q, near)),
                      ("nearby", lambda: _nominatim(q, near, bounded=True))):
        try:
            found += ask()
        except Exception as e:
            errors.append(f"{name}: {e}")
    if not any(r["exact"] for r in found):
        try:
            found += _nominatim(q, near, bounded=False)
        except Exception as e:
            errors.append(f"nominatim: {e}")
    if not found and errors:
        raise HTTPException(status_code=502,
                            detail=f"The address search is not reachable right now ({errors[0]}). Drop the pin on the map instead.")
    # one line per place: the same spot from both services is kept once
    merged = []
    for r in found:
        r["km"] = round(_km(near[0], near[1], r["lat"], r["lng"]), 1)
        if any(_km(r["lat"], r["lng"], m["lat"], m["lng"]) < 0.03 for m in merged):
            continue
        merged.append(r)
    # a house number match first, then whatever is nearest
    merged.sort(key=lambda r: (not r["exact"], r["km"]))
    return {"results": merged[:8]}


@router.get("/geocode/reverse")
def reverse_geocode(lat: float, lng: float, current_user: User = Depends(get_current_user)):
    """The address at a spot on the map, for a pin dropped before the address
    was typed. Never an error: a pin with no address is still a property - the
    house sits where the shoot was - so a failed lookup is just `result: null`."""
    import time
    import urllib.parse
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise HTTPException(status_code=400, detail="That location is off the map.")
    best = None
    try:
        params = {"lat": f"{lat:.6f}", "lon": f"{lng:.6f}", "lang": "en", "limit": "1"}
        for f in (_get_json("https://photon.komoot.io/reverse?" + urllib.parse.urlencode(params)).get("features") or []):
            p = f.get("properties") or {}
            street = " ".join(x for x in (p.get("housenumber"), p.get("street") or p.get("name")) if x)
            if street:
                best = {"address": street, "suburb": _no_ward(p.get("district")) or _no_ward(p.get("locality")) or p.get("city"),
                        "town": p.get("city") or p.get("county"), "exact": bool(p.get("housenumber"))}
                break
    except Exception as e:
        print(f"shoots: reverse lookup (photon) failed: {e}", flush=True)
    if best is None or not best["exact"]:
        try:
            wait = 1.0 - (time.time() - _geo_last[0])
            if wait > 0:
                time.sleep(wait)
            _geo_last[0] = time.time()
            params = {"format": "jsonv2", "addressdetails": "1", "zoom": "18",
                      "lat": f"{lat:.6f}", "lon": f"{lng:.6f}"}
            d = _get_json("https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(params)) or {}
            a = d.get("address") or {}
            street = " ".join(x for x in (a.get("house_number"), a.get("road")) if x)
            if street and (best is None or a.get("house_number")):
                best = {"address": street,
                        "suburb": _no_ward(a.get("suburb")) or _no_ward(a.get("neighbourhood")) or _no_ward(a.get("quarter"))
                                  or _no_ward(a.get("city_district")) or a.get("town"),
                        "town": a.get("city") or a.get("town") or a.get("village"),
                        "exact": bool(a.get("house_number"))}
        except Exception as e:
            print(f"shoots: reverse lookup (nominatim) failed: {e}", flush=True)
    return {"result": best}


@router.get("/for-path/")
def shoot_for(path: str, db: Session = Depends(get_db),
              current_user: User = Depends(get_current_user)):
    """Which shoot, if any, this file or folder belongs to."""
    shoot = shoot_for_path(db, path)
    return {"shoot": _as_dict(db, shoot, with_counts=False) if shoot else None}


@router.get("/suggest/folders")
def suggest(limit: int = 60, db: Session = Depends(get_db),
            current_user: User = Depends(get_current_user)):
    """Top-level folders that are not on a shoot yet, with the folder name
    offered as the address to start from. Nothing is created - this only
    saves typing."""
    taken = {f.path for f in db.query(ShootFolder).all()}
    rows = db.query(IndexedFolder).order_by(IndexedFolder.path).all()
    out = []
    for f in rows:
        p = _norm_path(f.path)
        if not p or p in taken:
            continue
        name = p.rsplit("/", 1)[-1]
        if name.startswith("_") or name.startswith("."):
            continue
        key = address_key(name)
        if key and db.query(Shoot).filter(Shoot.address_key == key).first():
            continue
        out.append({"path": p, "suggested_address": name})
        if len(out) >= max(1, min(500, limit)):
            break
    return {"suggestions": out}


_UNNAMED = ("shoot at ", "shoot in ")


def _unnamed(shoot) -> bool:
    return (shoot.address or "").strip().lower().startswith(_UNNAMED)


def _name_from_map(db: Session, shoot) -> bool:
    """Give a pin-only property its street address, if the map knows it."""
    if shoot.lat is None or shoot.lng is None:
        return False
    r = reverse_geocode(shoot.lat, shoot.lng, None).get("result")
    if not r or not r.get("address"):
        return False
    shoot.address = r["address"]
    if not shoot.suburb and r.get("suburb"):
        shoot.suburb = r["suburb"]
    db.commit()
    return True


@router.post("/{shoot_id}/lookup-address")
def lookup_address(shoot_id: int, db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    shoot = db.query(Shoot).filter(Shoot.id == shoot_id).first()
    if not shoot:
        raise HTTPException(status_code=404, detail="Property not found")
    if not _name_from_map(db, shoot):
        raise HTTPException(status_code=502, detail="The map has no street address there yet - type it in, or try again later.")
    return _as_dict(db, shoot)


def _address_loop():
    """Every half hour: pins dropped without an address (offline, or in a
    new street) get one as soon as the map service knows it."""
    import time
    time.sleep(60)
    while True:
        db = SessionLocal()
        try:
            for shoot in db.query(Shoot).all():
                if _unnamed(shoot):
                    try:
                        if _name_from_map(db, shoot):
                            print(f"shoots: found the address for a pin: {shoot.address}", flush=True)
                    except Exception as e:
                        print(f"shoots: address lookup failed: {e}", flush=True)
                    time.sleep(1.5)   # be polite to the free map services
        except Exception as e:
            print(f"shoots: address loop: {e}", flush=True)
        finally:
            db.close()
        time.sleep(1800)


# --------------------------------------------------------------------------
# agents
# --------------------------------------------------------------------------

agents_router = APIRouter(prefix="/api/shoots/agents", tags=["agents"])


class AgentBody(BaseModel):
    name: Optional[str] = Field(None, max_length=120)
    agency: Optional[str] = Field(None, max_length=120)
    phone: Optional[str] = Field(None, max_length=60)
    email: Optional[str] = Field(None, max_length=200)


def _agent_dict(db: Session, a: Agent, counts: Optional[dict] = None) -> dict:
    n = counts.get(a.name, 0) if counts is not None else \
        db.query(Shoot).filter(Shoot.agent == a.name).count()
    return {"id": a.id, "name": a.name, "agency": a.agency, "phone": a.phone,
            "email": a.email, "properties": n}


def _clean(v: Optional[str]) -> Optional[str]:
    v = (v or "").strip()
    return v or None


@agents_router.get("")
def list_agents(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    from sqlalchemy import func
    counts = dict(db.query(Shoot.agent, func.count(Shoot.id)).group_by(Shoot.agent).all())
    rows = db.query(Agent).order_by(func.lower(Agent.name)).all()
    return {"agents": [_agent_dict(db, a, counts) for a in rows]}


@agents_router.post("")
def add_agent(body: AgentBody, db: Session = Depends(get_db),
              current_user: User = Depends(get_current_user)):
    name = _clean(body.name)
    if not name:
        raise HTTPException(status_code=400, detail="The agent needs a name.")
    from sqlalchemy import func
    old = db.query(Agent).filter(func.lower(Agent.name) == name.lower()).first()
    if old:
        return _agent_dict(db, old)          # already on the list: just use it
    a = Agent(name=name, agency=_clean(body.agency), phone=_clean(body.phone), email=_clean(body.email))
    db.add(a)
    db.commit()
    db.refresh(a)
    return _agent_dict(db, a)


@agents_router.patch("/{agent_id}")
def edit_agent(agent_id: int, body: AgentBody, db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    a = db.query(Agent).filter(Agent.id == agent_id).first()
    if not a:
        raise HTTPException(status_code=404, detail="No such agent")
    if body.name is not None:
        name = _clean(body.name)
        if not name:
            raise HTTPException(status_code=400, detail="The agent needs a name.")
        if name != a.name:
            if db.query(Agent).filter(Agent.name == name, Agent.id != a.id).first():
                raise HTTPException(status_code=409, detail="There is already an agent with that name.")
            db.query(Shoot).filter(Shoot.agent == a.name).update({Shoot.agent: name})
            a.name = name
    for k in ("agency", "phone", "email"):
        v = getattr(body, k)
        if v is not None:
            setattr(a, k, _clean(v))
    db.commit()
    return _agent_dict(db, a)


@agents_router.delete("/{agent_id}")
def delete_agent(agent_id: int, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    """Off the list. Properties already shot for them keep the name."""
    a = db.query(Agent).filter(Agent.id == agent_id).first()
    if a:
        db.delete(a)
        db.commit()
    return {"status": "ok"}


def _seed_agents():
    """First run: the agents already typed on properties become the list."""
    db = SessionLocal()
    try:
        if db.query(Agent).count():
            return
        seen = set()
        for (name,) in db.query(Shoot.agent).filter(Shoot.agent.isnot(None)).distinct().all():
            n = (name or "").strip()
            if n and n.lower() not in seen:
                seen.add(n.lower())
                db.add(Agent(name=n))
        db.commit()
    except Exception as e:
        print(f"shoots: could not list the agents: {e}", flush=True)
    finally:
        db.close()


def _remember_agent(db: Session, name: Optional[str]) -> None:
    """An agent typed on a property goes on the list too."""
    n = _clean(name)
    if not n:
        return
    from sqlalchemy import func
    if not db.query(Agent).filter(func.lower(Agent.name) == n.lower()).first():
        db.add(Agent(name=n))


def unpaid(db: Session, shoot_id: Optional[int]) -> bool:
    """True when this property is not paid for yet: its portals stay watermarked, no downloads."""
    if not shoot_id:
        return False
    s = db.query(Shoot.paid_at).filter(Shoot.id == shoot_id).first()
    return bool(s) and s.paid_at is None


def install(app, media_root, resolve_media_path):
    global _media_root, _resolve
    _media_root = Path(media_root) if media_root else None
    _resolve = resolve_media_path
    Base.metadata.create_all(bind=engine,
                             tables=[Shoot.__table__, ShootFolder.__table__, Agent.__table__])
    # Shoots made before the map existed need the location and cover columns.
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(shoots)"))]
            for name, kind in (("lat", "FLOAT"), ("lng", "FLOAT"), ("cover_video_id", "INTEGER"),
                               ("shoot_time", "VARCHAR"), ("editing_at", "DATETIME"), ("posted_at", "DATETIME"),
                               ("listing", "VARCHAR"), ("price", "INTEGER"), ("beds", "FLOAT"),
                               ("baths", "FLOAT"), ("parking", "INTEGER"), ("floor_m2", "INTEGER"),
                               ("erf_m2", "INTEGER"), ("features", "TEXT"), ("kind", "VARCHAR"),
                               ("fee", "FLOAT"), ("invoice_no", "VARCHAR"), ("pay_claimed_at", "DATETIME"),
                               ("owner_name", "VARCHAR"), ("owner_phone", "VARCHAR"), ("owner_email", "VARCHAR"),
                               ("agency", "VARCHAR"), ("booked_email", "VARCHAR"), ("booked_phone", "VARCHAR"),
                               ("services", "TEXT"), ("booked_at", "DATETIME"), ("book_token", "VARCHAR")):
                if name not in cols:
                    conn.execute(text(f"ALTER TABLE shoots ADD COLUMN {name} {kind}"))
            if "paid_at" not in cols:
                # Downloads lock until a property is paid. Properties from before the lock
                # count as paid, so links already with clients keep working.
                conn.execute(text("ALTER TABLE shoots ADD COLUMN paid_at DATETIME"))
                conn.execute(text("UPDATE shoots SET paid_at = COALESCE(posted_at, created_at, CURRENT_TIMESTAMP)"))
            # "invoiced" was dropped as a status; those jobs were delivered.
            # "delivered" became "posted" (shot -> editing -> posted)
            conn.execute(text("UPDATE shoots SET status = 'posted' WHERE status IN ('invoiced', 'delivered')"))
            conn.commit()
    except Exception as e:
        print(f"shoots: could not add the map columns: {e}", flush=True)
    _seed_agents()
    import captions
    captions.install(app)
    app.include_router(agents_router)       # before /api/shoots/{id}
    app.include_router(router)
    import threading
    threading.Thread(target=_address_loop, daemon=True, name="shoot-addresses").start()
