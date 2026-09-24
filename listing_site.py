"""A listing website per property: one public page (/l/<name>) with the photos, the video, the 3D
walk-through, the facts, the words, a map, the agent's card and a QR code to share it.

The studio switches it on from the property (Share > Listing page), picks the photos and what shows
(the price, the street address), and sends the link to the agent - for their own site, WhatsApp
groups, the window card's QR code. No account and no password: it is meant to be shared. Photos are
served with their edits at web size, never the originals; nothing else of the library is reachable."""
from __future__ import annotations

import io
import json
import mimetypes
import os
import re
import secrets
from datetime import datetime
from typing import Callable, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.orm import Session

import permissions
from auth import get_current_user
from database import Base, User, Video, engine, get_db

router = APIRouter(tags=["listing-site"])
_resolve: Callable = lambda p: p
_upload_root = None


class ListingSite(Base):
    __tablename__ = "listing_sites"
    id = Column(Integer, primary_key=True)
    shoot_id = Column(Integer, nullable=False, unique=True, index=True)
    slug = Column(String, nullable=False, unique=True, index=True)
    on = Column(Boolean, default=False)
    photos = Column(Text, nullable=True)            # JSON [video id], in order; empty: the property's edited set
    video_id = Column(Integer, nullable=True)
    scene = Column(String, nullable=True)           # a shared 3D walk-through, "/3d/<token>"
    headline = Column(String, nullable=True)
    text = Column(Text, nullable=True)              # empty: the newest listing caption
    show_price = Column(Boolean, default=True)
    show_address = Column(Boolean, default=True)
    accent = Column(String, nullable=True)
    views = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


def _slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s[:60].strip("-") or "property"


def _may(user: User):
    if not permissions.can(user.role, permissions.SHOOTS):
        raise HTTPException(status_code=403, detail="Not allowed")


def _shoot(db: Session, shoot_id: int):
    import shoots
    s = db.query(shoots.Shoot).filter(shoots.Shoot.id == shoot_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="No such property")
    return s


def _site_for(db: Session, shoot_id: int, make: bool = True) -> Optional[ListingSite]:
    site = db.query(ListingSite).filter(ListingSite.shoot_id == shoot_id).first()
    if site or not make:
        return site
    s = _shoot(db, shoot_id)
    base = _slugify(" ".join(x for x in [s.address, s.suburb if s.suburb and s.suburb not in (s.address or "") else ""] if x))
    slug = base
    while db.query(ListingSite).filter(ListingSite.slug == slug).first():
        slug = f"{base}-{secrets.token_hex(2)}"
    site = ListingSite(shoot_id=shoot_id, slug=slug, on=False)
    db.add(site)
    db.commit()
    db.refresh(site)
    return site


def _photo_ids(db: Session, site: ListingSite, s) -> List[int]:
    try:
        ids = [int(x) for x in json.loads(site.photos or "[]")]
    except (TypeError, ValueError):
        ids = []
    if ids:
        return ids
    import listing_pack
    import shoots
    paths = [f.path for f in db.query(shoots.ShootFolder).filter(shoots.ShootFolder.shoot_id == s.id).all()]
    out = [v.id for v in listing_pack._photos(db, paths)]
    if s.cover_video_id in out:
        out.remove(s.cover_video_id)
        out.insert(0, s.cover_video_id)
    return out[:60]


def _videos_of(db: Session, s) -> List[Video]:
    import shoots
    paths = [f.path for f in db.query(shoots.ShootFolder).filter(shoots.ShootFolder.shoot_id == s.id).all()]
    out = []
    for p in paths:
        base = p.rstrip("/\\")
        out += (db.query(Video).filter(Video.media_type == "video", Video.is_active.isnot(False),
                                       Video.filepath.like(base + "/%") | Video.filepath.like(base + "\\%"))
                .order_by(Video.filename.asc()).all())
    return out


def _out(db: Session, site: ListingSite) -> dict:
    import splat
    s = _shoot(db, site.shoot_id)
    try:
        scenes = splat.shared_for_shoot(s.id)
    except Exception:
        scenes = []
    return {
        "slug": site.slug, "url": f"/l/{site.slug}", "on": bool(site.on), "photos": json.loads(site.photos or "[]"),
        "video_id": site.video_id, "scene": site.scene or "", "headline": site.headline or "", "text": site.text or "",
        "show_price": site.show_price is not False, "show_address": site.show_address is not False,
        "accent": site.accent or "", "views": site.views or 0,
        "videos": [{"id": v.id, "name": v.filename, "thumb": v.thumbnail_path} for v in _videos_of(db, s)[:40]],
        "scenes": scenes,
    }


class SiteBody(BaseModel):
    on: Optional[bool] = None
    slug: Optional[str] = Field(None, max_length=70)
    photos: Optional[List[int]] = Field(None, max_length=80)
    video_id: Optional[int] = None
    scene: Optional[str] = Field(None, max_length=200)
    headline: Optional[str] = Field(None, max_length=120)
    text: Optional[str] = Field(None, max_length=5000)
    show_price: Optional[bool] = None
    show_address: Optional[bool] = None
    accent: Optional[str] = Field(None, max_length=9)


@router.get("/api/shoots/{shoot_id}/site")
def get_site(shoot_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _may(current_user)
    return _out(db, _site_for(db, shoot_id))


@router.put("/api/shoots/{shoot_id}/site")
def put_site(shoot_id: int, body: SiteBody, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _may(current_user)
    site = _site_for(db, shoot_id)
    vals = body.model_dump(exclude_unset=True)
    if "slug" in vals:
        slug = _slugify(vals.pop("slug") or "")
        if slug != site.slug:
            if db.query(ListingSite).filter(ListingSite.slug == slug, ListingSite.id != site.id).first():
                raise HTTPException(status_code=409, detail="Another property already has that web address.")
            site.slug = slug
    if "photos" in vals:
        site.photos = json.dumps(vals.pop("photos") or [])
    if "scene" in vals:
        sc = (vals.pop("scene") or "").strip()
        if sc and not re.fullmatch(r"/3d/[A-Za-z0-9_-]+", sc):
            raise HTTPException(status_code=400, detail="That is not a shared 3D walk-through.")
        site.scene = sc or None
    if "accent" in vals:
        a = (vals.pop("accent") or "").strip()
        site.accent = a if re.fullmatch(r"#[0-9a-fA-F]{6}", a) else None
    for k, v in vals.items():
        setattr(site, k, v.strip() if isinstance(v, str) else v)
    db.commit()
    return _out(db, site)


# --------------------------------------------------------------------------
# the public page
# --------------------------------------------------------------------------

def _public_site(db: Session, slug: str):
    site = db.query(ListingSite).filter(ListingSite.slug == slug).first() if slug else None
    if not site or not site.on:
        raise HTTPException(status_code=404, detail="This listing is not online.")
    return site, _shoot(db, site.shoot_id)


@router.get("/api/public/listing/{slug}")
def public_listing(slug: str, db: Session = Depends(get_db)):
    import business
    import captions
    import shoots
    site, s = _public_site(db, slug)
    site.views = (site.views or 0) + 1
    db.commit()
    facts = shoots.facts_of(s)
    rent = facts.get("listing") == "rent"
    text = site.text
    if not text:
        c = (db.query(captions.Caption).filter(captions.Caption.shoot_id == s.id, captions.Caption.platform == "listing")
             .order_by(captions.Caption.copied_at.is_(None), captions.Caption.id.desc()).first())
        text = c.text if c else ""
    beds = facts.get("beds")
    kind = captions.kind_word(facts)
    head = site.headline or ((f"{int(beds)} bedroom {kind}" if beds else kind.capitalize()) + (f" in {s.suburb}" if s.suburb else ""))
    agent = None
    if s.agent:
        a = db.query(shoots.Agent).filter(shoots.Agent.name == s.agent).first()
        agent = {"name": s.agent, "agency": (a.agency if a else None) or s.agency,
                 "phone": (a.phone if a else None) or s.booked_phone, "email": (a.email if a else None) or s.booked_email}
    lat, lng = s.lat, s.lng
    if lat is not None and lng is not None and site.show_address is False:
        lat, lng = round(lat, 2), round(lng, 2)          # the area, not the house
    b = business.settings(db)
    video = None
    if site.video_id:
        v = db.query(Video).filter(Video.id == site.video_id).first()
        if v:
            video = {"id": v.id, "poster": f"/api/public/listing/{site.slug}/poster"}
    return {
        "headline": head[0].upper() + head[1:], "text": text or "", "facts": {k: facts.get(k) for k in ("kind", "listing", "beds", "baths", "parking", "floor_m2", "erf_m2")},
        "features": facts.get("features") or [], "listing": "To rent" if rent else ("For sale" if facts.get("listing") else ""),
        "price": captions.money(facts.get("price"), rent) if site.show_price is not False else "",
        "address": s.address if site.show_address is not False else "", "suburb": s.suburb or "",
        "lat": lat, "lng": lng, "exact": site.show_address is not False,
        "photos": _photo_ids(db, site, s), "video": video, "scene": site.scene or "",
        "agent": agent, "studio": business.studio_name(b), "accent": site.accent or "",
    }


def _in_site(db: Session, site: ListingSite, s, vid: int):
    if vid not in _photo_ids(db, site, s):
        raise HTTPException(status_code=404, detail="Not on this listing")


@router.get("/api/public/listing/{slug}/photo/{video_id}")
def public_photo(slug: str, video_id: int, w: int = 1600, db: Session = Depends(get_db)):
    import photo_edit as pe
    site, s = _public_site(db, slug)
    _in_site(db, site, s, video_id)
    w = 800 if w <= 800 else 1600 if w <= 1600 else 2400        # a few sizes, so the cache stays small
    p = pe.developed_file(db, video_id, w)
    return FileResponse(str(p), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@router.get("/api/public/listing/{slug}/video")
def public_video(slug: str, db: Session = Depends(get_db)):
    site, s = _public_site(db, slug)
    v = db.query(Video).filter(Video.id == site.video_id).first() if site.video_id else None
    if not v:
        raise HTTPException(status_code=404, detail="No video on this listing")
    path = None
    if v.proxy_status == "completed" and v.proxy_path:
        path = _resolve(v.proxy_path)
        if path and not os.path.exists(path):
            path = None
    if not path:
        path = _resolve(v.filepath)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="The video file is missing")
    ctype = mimetypes.guess_type(path)[0] or "video/mp4"
    return FileResponse(path, media_type=ctype, headers={"Accept-Ranges": "bytes", "Content-Disposition": "inline"})


@router.get("/api/public/listing/{slug}/poster")
def public_poster(slug: str, db: Session = Depends(get_db)):
    site, s = _public_site(db, slug)
    v = db.query(Video).filter(Video.id == site.video_id).first() if site.video_id else None
    thumbs = str(_upload_root / "thumbnails" / os.path.basename(v.thumbnail_path)) if v and v.thumbnail_path and _upload_root else None
    if not thumbs or not os.path.exists(thumbs):
        raise HTTPException(status_code=404, detail="No poster")
    return FileResponse(thumbs, media_type="image/jpeg")


@router.get("/api/public/listing/{slug}/qr.png")
def public_qr(slug: str, url: Optional[str] = None, db: Session = Depends(get_db)):
    """The page's QR code; `url` is the full address as the browser sees it (the server may be behind a proxy)."""
    import print_marketing
    site, s = _public_site(db, slug)
    target = url if url and url.rstrip("/").endswith(f"/l/{site.slug}") and url.startswith(("http://", "https://")) else f"/l/{site.slug}"
    im = print_marketing.qr_image(target, 600)
    if im is None:
        raise HTTPException(status_code=404, detail="QR codes are not available")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return Response(buf.getvalue(), media_type="image/png", headers={"Content-Disposition": f'inline; filename="{site.slug}-qr.png"'})


def install(app, resolve: Callable, upload_root):
    global _resolve, _upload_root
    _resolve, _upload_root = resolve, upload_root
    Base.metadata.create_all(bind=engine, tables=[ListingSite.__table__])
    app.include_router(router)
