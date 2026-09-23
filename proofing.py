"""Proofing pins: a client clicks a spot on a photo in their portal and says
what should change there ("remove the hose", "brighter here"). Each pin shows
up in the editor as a to-do on that photo, and is ticked off there.
"""
from datetime import datetime
from typing import Callable, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Session

from auth import get_current_user
from database import Base, Share, User, engine, get_db

router = APIRouter(tags=["proofing"])


class SharePin(Base):
    __tablename__ = "share_pins"
    id = Column(Integer, primary_key=True, index=True)
    share_id = Column(Integer, ForeignKey("shares.id"), nullable=False, index=True)
    video_id = Column(Integer, ForeignKey("videos.id"), nullable=False, index=True)
    x = Column(Float, nullable=False)          # 0..1 of the photo as delivered
    y = Column(Float, nullable=False)
    text = Column(String, nullable=False)
    viewer_name = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    resolved = Column(Boolean, default=False)
    resolved_by = Column(String, nullable=True)
    resolved_at = Column(DateTime, nullable=True)


def _pin(p: SharePin, share: Optional[Share] = None) -> dict:
    out = {"id": p.id, "video_id": p.video_id, "x": p.x, "y": p.y, "text": p.text, "viewer_name": p.viewer_name,
           "created_at": p.created_at.isoformat() if p.created_at else None, "resolved": bool(p.resolved),
           "resolved_by": p.resolved_by}
    if share is not None:
        out["share"] = {"id": share.id, "title": getattr(share, "title", None) or getattr(share, "name", None) or f"Link {share.id}"}
    return out


class PatchPin(BaseModel):
    resolved: bool


@router.get("/api/proofing/video/{video_id}")
def pins_for_video(video_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Every client pin on this photo, from every portal it was sent in."""
    rows = db.query(SharePin).filter(SharePin.video_id == video_id).order_by(SharePin.created_at.asc()).all()
    shares = {s.id: s for s in db.query(Share).filter(Share.id.in_({r.share_id for r in rows})).all()} if rows else {}
    return {"pins": [_pin(r, shares.get(r.share_id)) for r in rows]}


@router.post("/api/proofing/open")
def open_counts(body: dict = Body(...), db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Open pins per photo, for a set of photos (the filmstrip's badges)."""
    ids = [int(i) for i in (body.get("video_ids") or [])][:5000]
    if not ids:
        return {"open": {}}
    from sqlalchemy import func
    rows = (db.query(SharePin.video_id, func.count(SharePin.id))
            .filter(SharePin.video_id.in_(ids), SharePin.resolved.isnot(True))
            .group_by(SharePin.video_id).all())
    return {"open": {str(v): n for v, n in rows}}


@router.patch("/api/proofing/pins/{pin_id}")
def patch_pin(pin_id: int, body: PatchPin, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    p = db.query(SharePin).filter(SharePin.id == pin_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="No such pin")
    p.resolved = body.resolved
    p.resolved_by = current_user.username if body.resolved else None
    p.resolved_at = datetime.utcnow() if body.resolved else None
    db.commit()
    return _pin(p)


class NewPin(BaseModel):
    video_id: int
    x: float = Field(..., ge=0, le=1)
    y: float = Field(..., ge=0, le=1)
    text: str = Field(..., min_length=1, max_length=1000)
    viewer_name: Optional[str] = Field(None, max_length=80)
    password: Optional[str] = None


def install(app, share_state: Callable, share_videos: Callable):
    """share_state / share_videos: main's own checks for a public link, so a
    pin can only be put on a photo that link really shows."""
    Base.metadata.create_all(bind=engine, tables=[SharePin.__table__])

    @app.get("/api/public/share/{token}/pins")
    def public_pins(token: str, password: Optional[str] = None, db: Session = Depends(get_db)):
        share = share_state(db, token, password)
        rows = db.query(SharePin).filter(SharePin.share_id == share.id).order_by(SharePin.created_at.asc()).all()
        return {"pins": [_pin(r) for r in rows]}

    @app.post("/api/public/share/{token}/pins")
    def public_add_pin(token: str, body: NewPin, db: Session = Depends(get_db)):
        share = share_state(db, token, body.password)
        if not share.allow_selects:
            raise HTTPException(status_code=403, detail="Notes are off for this link")
        if body.video_id not in {v.id for v in share_videos(db, share)}:
            raise HTTPException(status_code=404, detail="Not in this link")
        if db.query(SharePin).filter(SharePin.share_id == share.id).count() >= 2000:
            raise HTTPException(status_code=400, detail="Too many notes on this link")
        p = SharePin(share_id=share.id, video_id=body.video_id, x=body.x, y=body.y, text=body.text.strip(),
                     viewer_name=(body.viewer_name or "").strip() or None)
        db.add(p)
        db.commit()
        return _pin(p)

    @app.delete("/api/public/share/{token}/pins/{pin_id}")
    def public_delete_pin(token: str, pin_id: int, password: Optional[str] = None, db: Session = Depends(get_db)):
        share = share_state(db, token, password)
        p = db.query(SharePin).filter(SharePin.id == pin_id, SharePin.share_id == share.id).first()
        if not p:
            raise HTTPException(status_code=404, detail="No such note")
        if p.resolved:
            raise HTTPException(status_code=400, detail="That note has already been dealt with")
        db.delete(p)
        db.commit()
        return {"status": "ok"}

    app.include_router(router)
