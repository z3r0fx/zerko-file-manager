"""My style (replaces Aftershoot Edits / Imagen): edit a new photo the way you edited
the most similar photo you have already done.

Every photo you have really edited (its light and colour moved, not only
straightened) is a lesson. A new photo is matched to them by what it shows
(the CLIP picture fingerprints from clip_search: a bathroom finds your
bathrooms, a twilight front finds your twilight fronts), and it gets the
nearest one's light and colour - never its crop, straightening, lens, masks,
patches or looks, which belong to that photo. The exposure is then evened out
so the result lands as bright as your edit of that photo did.

Photos My style edited are not learned from until you change them yourself.
"""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import Session

from auth import get_current_user
from database import Base, SessionLocal, User, Video, engine, get_db

router = APIRouter(prefix="/api/style", tags=["style"])

MIN_SIMILAR = 0.66          # CLIP cosine for a photo of the same kind of room to count as similar
MIN_ANY = 0.86              # ... and for one of another kind (nearly the same picture)
KINDS = {
    "bathroom": "a photo of a bathroom", "bedroom": "a photo of a bedroom", "kitchen": "a photo of a kitchen",
    "living": "a photo of a living room", "dining": "a photo of a dining room", "study": "a photo of a home office",
    "passage": "a photo of a hallway or entrance", "garage": "a photo of a garage", "laundry": "a photo of a laundry room",
    "patio": "a photo of a patio or balcony", "garden": "a photo of a garden", "pool": "a photo of a swimming pool",
    "front": "a photo of the front of a house in daylight", "dusk": "a photo of a house at dusk with the lights on",
    "aerial": "an aerial drone photo of houses", "view": "a photo of a view over a city or the sea",
}
MIN_CHANGED = 3             # develop settings moved from their defaults for a photo to count as edited


class StyleMark(Base):
    """A photo My style edited, and the edit it put on (so it is not learned from unless changed since)."""
    __tablename__ = "style_marks"
    video_id = Column(Integer, primary_key=True)
    source_id = Column(Integer, nullable=False)
    recipe_hash = Column(String, nullable=False)


def _skip() -> set:
    import pipeline
    return set(pipeline.KEEP_ON_PRESET) | {"fixes", "wm_index", "wm_margin", "wm_opacity", "wm_position", "wm_scale"}


def _defaults() -> dict:
    import photo_edit as pe
    return pe.Recipe().model_dump()


def changed(rec: dict) -> int:
    """How many light-and-colour settings this edit moved from their defaults."""
    d, skip = _defaults(), _skip()
    return sum(1 for k, v in rec.items() if k in d and k not in skip and v != d[k])


def _hash(rec: dict) -> str:
    """The light-and-colour part of an edit, rounded, so the same edit saved by the editor matches."""
    import photo_edit as pe
    full, skip = pe.Recipe(**rec).model_dump(), _skip()

    def r(v):
        if isinstance(v, float):
            return round(v, 3)
        if isinstance(v, list):
            return [r(x) for x in v]
        if isinstance(v, dict):
            return {k: r(x) for k, x in v.items()}
        return v
    dev = {k: r(v) for k, v in full.items() if k not in skip}
    return hashlib.sha1(json.dumps(dev, sort_keys=True).encode()).hexdigest()[:16]


def lessons(db: Session, exclude: Optional[set] = None) -> Dict[int, dict]:
    """video id -> its edit, for every photo that was really edited by hand."""
    import photo_edit as pe
    marks = {m.video_id: m.recipe_hash for m in db.query(StyleMark).all()}
    out = {}
    for row in db.query(pe.PhotoEdit.video_id, pe.PhotoEdit.recipe).order_by(pe.PhotoEdit.updated_at.desc()).limit(6000).all():
        if exclude and row.video_id in exclude:
            continue
        try:
            rec = json.loads(row.recipe or "{}")
        except Exception:
            continue
        if changed(rec) < MIN_CHANGED:
            continue
        if marks.get(row.video_id) == _hash(rec):       # My style's own work, untouched since
            continue
        out[row.video_id] = rec
    return out


def _vec(db: Session, vid: int) -> Optional[np.ndarray]:
    import cv2
    import clip_search
    v = clip_search.vectors(db, [vid]).get(vid)
    if v is not None:
        return v
    row = db.query(Video).filter(Video.id == vid).first()
    t = clip_search._thumb_path(row) if row else None
    if not t:
        return None
    bgr = cv2.imread(t, cv2.IMREAD_COLOR)
    return clip_search.image_vec(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)) if bgr is not None else None


_kind_vecs: dict = {}


def _kind(v: np.ndarray) -> str:
    """What kind of photo this is (room, front, dusk, aerial), by CLIP."""
    import clip_search
    if not _kind_vecs:
        for k, t in KINDS.items():
            _kind_vecs[k] = clip_search.text_vec(t)
    ks = list(_kind_vecs)
    return ks[int(np.argmax(np.stack([_kind_vecs[k] for k in ks]) @ v))]


def _brightness(vid: int, rec: dict) -> float:
    """The mean linear luminance of the photo developed with this edit, small (without any painted look
    or patch on it: those are the photo's own and the exposure should not fight them)."""
    rec = {**rec, "gen_ref": "", "removes": []}
    import ai_photo
    import photo_edit as pe
    db = SessionLocal()
    try:
        path = ai_photo._path(ai_photo._video(db, vid))
    finally:
        db.close()
    dev = ai_photo.developed(vid, path, rec, 480)
    lin = pe._srgb_to_linear(np.clip(dev, 0, 1))
    return float((lin @ np.array([0.2126, 0.7152, 0.0722], np.float32)).mean())


def suggest(db: Session, vid: int) -> dict:
    """The edit My style would give this photo, and which of your photos it learned it from."""
    import ai_photo
    import clip_search
    import photo_edit as pe
    import pipeline
    if not clip_search.ready():
        raise HTTPException(status_code=400, detail="My style needs the search model: search the library once (it is fetched then), then try again.")
    lib = lessons(db, exclude={vid})
    if not lib:
        raise HTTPException(status_code=400, detail="Nothing to learn from yet: edit a few photos by hand first (light and colour).")
    me = _vec(db, vid)
    if me is None:
        raise HTTPException(status_code=400, detail="This photo has no thumbnail to look at yet.")
    vecs = clip_search.vectors(db, list(lib))
    missing = [i for i in lib if i not in vecs][:200]
    for i in missing:
        v = _vec(db, i)
        if v is not None:
            vecs[i] = v
    if not vecs:
        raise HTTPException(status_code=400, detail="Your edited photos have not been looked at yet - try again in a minute.")
    ids = list(vecs)
    sims = np.stack([vecs[i] for i in ids]) @ me
    mine_kind = _kind(me)
    same = np.array([_kind(vecs[i]) == mine_kind for i in ids])
    ok = (same & (sims >= MIN_SIMILAR)) | (sims >= MIN_ANY)
    if not ok.any():
        raise HTTPException(status_code=400, detail="None of the photos you have edited is like this one yet.")
    score = np.where(ok, sims + 0.1 * same, -1.0)
    best = int(np.argmax(score))
    src, sim = ids[best], float(sims[best])
    mine = ai_photo.saved_recipe(db, vid)
    look = lib[src]
    merged = pe._for_photo(look, mine)
    for k in pipeline.KEEP_ON_PRESET:                    # this photo's own straightening, crop, lens, patches, looks
        if k in mine:
            merged[k] = mine[k]
        elif k in merged:
            merged[k] = pe.Recipe.model_fields[k].default
    # even out the exposure: the same brightness your edit of the other photo ended at
    try:
        want = _brightness(src, look)
        for _ in range(2):
            have = _brightness(vid, merged)
            if have <= 1e-4 or want <= 1e-4:
                break
            step = float(np.clip(np.log2(want / have), -1.5, 1.5))
            if abs(step) < 0.05:
                break
            base = float(look.get("exposure", 0))
            # at most three quarters of a stop from your own edit: a darker, moodier room stays darker
            merged["exposure"] = float(np.clip(float(merged.get("exposure", 0)) + step * 0.9, base - 0.75, base + 0.75))
    except Exception as e:
        print(f"style: exposure match skipped for {vid}: {e}", flush=True)
    merged = pe.Recipe(**merged).model_dump()
    v = db.query(Video.filename).filter(Video.id == src).first()
    return {"recipe": merged, "from": src, "filename": v.filename if v else "", "similar": round(sim, 3)}


def apply(db: Session, vid: int, user: str = "") -> dict:
    """Suggest and save, with the edit before it kept as a snapshot ("Before My style")."""
    import ai_photo
    import photo_edit as pe
    s = suggest(db, vid)
    before = ai_photo.saved_recipe(db, vid)
    row = db.query(pe.PhotoEdit).filter(pe.PhotoEdit.video_id == vid).first()
    if row and before:
        snaps = [x for x in pe._snaps(row) if x.get("name") != "Before My style"]
        snaps.append({"name": "Before My style", "at": datetime.utcnow().isoformat() + "Z", "by": user,
                      "recipe": pe.Recipe(**before).model_dump()})
        row.snapshots = json.dumps(snaps[-50:])
        db.commit()
    ai_photo.save_recipe(db, vid, s["recipe"])
    db.merge(StyleMark(video_id=vid, source_id=s["from"], recipe_hash=_hash(ai_photo.saved_recipe(db, vid))))
    db.commit()
    return {k: v for k, v in s.items() if k != "recipe"}


# --------------------------------------------------------------------------
# the API
# --------------------------------------------------------------------------

@router.get("/status")
def status(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return {"lessons": len(lessons(db))}


@router.post("/suggest/{video_id}")
def suggest_route(video_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """For the editor: the edit, not saved (the editor puts it on, so undo works)."""
    s = suggest(db, video_id)
    db.merge(StyleMark(video_id=video_id, source_id=s["from"], recipe_hash=_hash(s["recipe"])))
    db.commit()
    return s


JOBS: Dict[str, dict] = {}


class ApplyBody(BaseModel):
    video_ids: List[int] = Field(..., min_length=1, max_length=3000)


def _run(j: dict, ids: List[int]):
    db = SessionLocal()
    try:
        for n, vid in enumerate(ids):
            try:
                r = apply(db, vid, j["user"])
                j["done_ids"].append(vid)
                j["from"][str(vid)] = r["filename"]
            except HTTPException as e:
                j["skipped"].append({"id": vid, "why": e.detail})
            except Exception as e:
                db.rollback()
                j["skipped"].append({"id": vid, "why": str(e)[:200]})
            j["done"] = n + 1
        j["state"] = "done"
    finally:
        db.close()


@router.post("/apply")
def apply_route(body: ApplyBody, current_user: User = Depends(get_current_user)):
    if current_user.role == "viewer":
        raise HTTPException(status_code=403, detail="Your account has view-only access.")
    j = {"id": uuid.uuid4().hex[:12], "state": "running", "done": 0, "total": len(body.video_ids), "done_ids": [],
         "skipped": [], "from": {}, "user": current_user.username}
    JOBS[j["id"]] = j
    threading.Thread(target=_run, args=(j, body.video_ids), daemon=True).start()
    return j


@router.get("/jobs/{job_id}")
def job(job_id: str, current_user: User = Depends(get_current_user)):
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="No such job")
    return j


def install(app):
    Base.metadata.create_all(bind=engine, tables=[StyleMark.__table__])
    app.include_router(router)
