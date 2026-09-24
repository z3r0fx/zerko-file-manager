"""What the image model costs: every painting is written down with what it
probably cost (from the service's price per picture or per megapixel), which
photo and property it was for, and what it was (a look, a quick preview, a
staging, a test). Manage > AI shows the month, a property shows its own.

The prices are estimates the owner can correct in Manage > AI: fal bills per
megapixel, Gemini and OpenAI per picture, the own graphics card is free."""
from __future__ import annotations

import contextvars
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, Float, Integer, String
from sqlalchemy.orm import Session

from auth import get_current_user, get_admin_user
from database import Base, SessionLocal, User, Video, engine, get_db

router = APIRouter(prefix="/api/ai/usage", tags=["ai"])

# (per "mp" or per "img", US dollars) - what each service charged in September 2026
DEFAULT_PRICES = {"comfyui": ("mp", 0.0), "fal": ("mp", 0.03), "gemini": ("img", 0.039), "openai": ("img", 0.04)}

# what the painting now under way is for: set by the look job around each painting
context: contextvars.ContextVar[dict] = contextvars.ContextVar("ai_usage_context", default={})


class AiUsage(Base):
    __tablename__ = "ai_usage"
    id = Column(Integer, primary_key=True)
    at = Column(DateTime, default=datetime.utcnow, index=True)
    backend = Column(String, nullable=False)
    model = Column(String, nullable=True)
    megapixels = Column(Float, nullable=False, default=0)
    cost = Column(Float, nullable=False, default=0)
    kind = Column(String, nullable=True)           # look | preview | stage | test | retry
    video_id = Column(Integer, nullable=True, index=True)
    shoot_id = Column(Integer, nullable=True, index=True)
    look = Column(String, nullable=True)
    user = Column(String, nullable=True)


def prices() -> dict:
    """The price table with the owner's corrections from Manage > AI."""
    import ai_image
    own = ai_image.settings().get("prices") or {}
    out = {}
    for b, (unit, p) in DEFAULT_PRICES.items():
        try:
            out[b] = (unit, float(own.get(b, p)))
        except (TypeError, ValueError):
            out[b] = (unit, p)
    return out


def estimate(backend: str, megapixels: float) -> float:
    unit, p = prices().get(backend, ("img", 0.0))
    return round(p * (megapixels if unit == "mp" else 1.0), 4)


def record(backend: str, model: str, width: int, height: int) -> None:
    """One painting done. Never lets a bookkeeping failure spoil the painting."""
    try:
        ctx = dict(context.get() or {})
        mp = width * height / 1e6
        db = SessionLocal()
        try:
            shoot_id = ctx.get("shoot_id")
            if shoot_id is None and ctx.get("video_id"):
                shoot_id = _shoot_of(db, ctx["video_id"])
            db.add(AiUsage(backend=backend, model=model, megapixels=round(mp, 3), cost=estimate(backend, mp),
                           kind=ctx.get("kind") or "look", video_id=ctx.get("video_id"), shoot_id=shoot_id,
                           look=(ctx.get("look") or "")[:80], user=ctx.get("user")))
            db.commit()
        finally:
            db.close()
    except Exception as e:
        print(f"ai_usage: could not write down a painting: {e}", flush=True)


def _shoot_of(db: Session, video_id: int) -> Optional[int]:
    try:
        import shoots
        v = db.query(Video).filter(Video.id == video_id).first()
        s = shoots.shoot_for_path(db, v.filepath) if v else None
        return s.id if s else None
    except Exception:
        return None


def _sum(rows) -> dict:
    return {"paintings": len(rows), "cost": round(sum(r.cost for r in rows), 2),
            "megapixels": round(sum(r.megapixels for r in rows), 1)}


@router.get("")
def summary(days: int = 31, shoot_id: Optional[int] = None, db: Session = Depends(get_db),
            current_user: User = Depends(get_current_user)):
    """This month, the last `days` days by property, and the latest paintings."""
    now = datetime.utcnow()
    q = db.query(AiUsage)
    if shoot_id is not None:
        q = q.filter(AiUsage.shoot_id == shoot_id)
    month = q.filter(AiUsage.at >= now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)).all()
    recent = q.filter(AiUsage.at >= now - timedelta(days=max(1, min(days, 400)))).all()
    by_shoot: dict = {}
    for r in recent:
        by_shoot.setdefault(r.shoot_id, []).append(r)
    names = {}
    try:
        import shoots
        ids = [k for k in by_shoot if k]
        for s in db.query(shoots.Shoot).filter(shoots.Shoot.id.in_(ids)).all() if ids else []:
            names[s.id] = s.address
    except Exception:
        pass
    per = sorted(({"shoot_id": k, "address": names.get(k) or ("Not on a property" if not k else f"Property {k}"),
                   **_sum(v)} for k, v in by_shoot.items()), key=lambda x: -x["cost"])
    kinds: dict = {}
    for r in recent:
        kinds.setdefault(r.kind or "look", []).append(r)
    last = q.order_by(AiUsage.at.desc()).limit(20).all()
    return {
        "month": _sum(month), "recent": _sum(recent), "days": days,
        "by_property": per[:50],
        "by_kind": {k: _sum(v) for k, v in kinds.items()},
        "latest": [{"at": r.at.isoformat() + "Z", "backend": r.backend, "kind": r.kind, "look": r.look,
                    "video_id": r.video_id, "cost": r.cost} for r in last],
        "prices": {b: {"unit": u, "price": p} for b, (u, p) in prices().items()},
        "credit": _credit(db),
    }


def _credit(db: Session) -> Optional[dict]:
    """What the owner said was on the image service's account, less what was painted since."""
    import ai_image
    c = ai_image.settings().get("credit") or {}
    try:
        amount, at = float(c["amount"]), datetime.fromisoformat(str(c["at"]).rstrip("Z"))
    except (KeyError, TypeError, ValueError):
        return None
    spent = sum(r.cost for r in db.query(AiUsage.cost).filter(AiUsage.at >= at, AiUsage.backend != "comfyui").all())
    return {"amount": amount, "at": at.isoformat() + "Z", "spent": round(spent, 2), "left": round(amount - spent, 2)}


class CreditBody(BaseModel):
    amount: Optional[float] = Field(None, ge=0, le=100000)


@router.put("/credit")
def set_credit(body: CreditBody, db: Session = Depends(get_db), current_user: User = Depends(get_admin_user)):
    """After a top-up: what the account shows now (empty clears it)."""
    import ai_image
    d = ai_image.settings()
    if body.amount is None:
        d.pop("credit", None)
    else:
        d["credit"] = {"amount": round(body.amount, 2), "at": datetime.utcnow().isoformat() + "Z"}
    ai_image._save_settings(d)
    return {"credit": _credit(db)}


class PricesBody(BaseModel):
    prices: dict = Field(default_factory=dict)


@router.put("/prices")
def set_prices(body: PricesBody, current_user: User = Depends(get_admin_user)):
    import ai_image
    d = ai_image.settings()
    own = dict(d.get("prices") or {})
    for b, v in (body.prices or {}).items():
        if b in DEFAULT_PRICES:
            try:
                own[b] = max(0.0, min(5.0, float(v)))
            except (TypeError, ValueError):
                pass
    d["prices"] = own
    ai_image._save_settings(d)
    return {"prices": {b: {"unit": u, "price": p} for b, (u, p) in prices().items()}}


def install(app):
    Base.metadata.create_all(bind=engine, tables=[AiUsage.__table__])
    app.include_router(router)
