"""Cull: go through a shoot fast, the way Aftershoot or Photo Mechanic would.

Every photo is measured once (sharpness, exposure, a small fingerprint of what
it shows, when it was taken) and remembered. Shots of the same view taken one
after another are stacked into a group; in each group the sharpest, best
exposed frame is suggested as the one to keep. Bracketed sets (the same view
at different exposures, for merging) stay together as one set and are never
thinned out. Nothing is deleted: keeping and rejecting are the library's picks.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Column, Float, Integer, String
from sqlalchemy.orm import Session

from auth import get_current_user
from database import Base, IndexedFolder, SessionLocal, User, Video, engine, get_db

router = APIRouter(prefix="/api/cull", tags=["cull"])

MEASURE_W = 1200           # the editor's quick base picture size is reused where it is cached
BLURRY = 0.075             # below this the strongest edges are soft (see step_cull in pipeline.py)
SAME_VIEW = 12             # fingerprint bits that may differ for two shots of the same view
SAME_BURST_S = 180         # shots of one view further apart than this are separate groups


class CullInfo(Base):
    __tablename__ = "cull_info"
    video_id = Column(Integer, primary_key=True)
    stamp = Column(String, nullable=False)        # file size and time: measured again when the file changes
    sharp = Column(Float, nullable=False)
    mean = Column(Float, nullable=False)
    bright = Column(Float, nullable=False)
    dark = Column(Float, nullable=False)
    fp = Column(String, nullable=False)           # 64-bit fingerprint of the view (exposure-independent)
    taken = Column(Float, nullable=True)          # capture time, seconds


def _stamp(path: str) -> str:
    st = os.stat(path)
    return f"{st.st_size}:{int(st.st_mtime)}"


def _taken(path: str, v: Video) -> Optional[float]:
    try:
        import hdr
        t = hdr._exif_capture(path)[0]
        if t:
            return t.timestamp()
    except Exception:
        pass
    if v.shoot_date:
        return v.shoot_date.timestamp()
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def measure(path: str) -> dict:
    """Sharpness, exposure and a fingerprint of the view for one photo."""
    import cv2
    import photo_edit as pe
    rgb = pe._read_rgb(path, max_dim=MEASURE_W)
    g = cv2.cvtColor((np.clip(rgb, 0, 1) * 255).astype(np.uint8), cv2.COLOR_RGB2GRAY)
    gf = g.astype(np.float32)
    gx, gy = cv2.Sobel(gf, cv2.CV_32F, 1, 0), cv2.Sobel(gf, cv2.CV_32F, 0, 1)
    gm = np.sqrt(gx * gx + gy * gy)
    top = gm > np.percentile(gm, 97)
    # how crisp the strongest edges are: a plain white room with few edges is still sharp
    sharp = float(np.abs(cv2.Laplacian(gf, cv2.CV_32F))[top].mean() / (gm[top].mean() + 1e-6)) if top.any() else 0.0
    # the fingerprint ignores exposure (equalised first), so a bracket set looks like one view
    eq = cv2.equalizeHist(cv2.resize(g, (160, int(160 * g.shape[0] / g.shape[1])), interpolation=cv2.INTER_AREA))
    small = cv2.resize(eq, (9, 8), interpolation=cv2.INTER_AREA).astype(np.int16)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    fp = int("".join("1" if b else "0" for b in bits), 2)
    return {"sharp": sharp, "mean": float(g.mean()), "bright": float((g > 250).mean()), "dark": float((g < 8).mean()),
            "fp": f"{fp:016x}"}


def _photos(db: Session, folder_id: int, sub: bool) -> List[Video]:
    import listing_pack
    f = db.query(IndexedFolder).filter(IndexedFolder.id == folder_id).first()
    if not f:
        raise HTTPException(status_code=404, detail="No such folder")
    q = db.query(Video).filter(Video.media_type == "photo", Video.is_active.isnot(False))
    if sub:
        p = f.path.rstrip("/\\")
        ids = [x.id for x in db.query(IndexedFolder.id).filter(
            (IndexedFolder.path == p) | IndexedFolder.path.like(p + "/%") | IndexedFolder.path.like(p + "\\%")).all()]
        q = q.filter(Video.folder_id.in_(ids))
    else:
        q = q.filter(Video.folder_id == folder_id)
    out = [v for v in q.order_by(Video.filename.asc()).all()
           if not listing_pack.EXPORT_DIR.search(os.path.dirname(v.filepath or ""))]
    return out


# --------------------------------------------------------------------------
# measuring, in the background
# --------------------------------------------------------------------------

JOBS: Dict[str, dict] = {}
_lock = threading.Lock()


def _measure_run(j: dict, ids: List[int]):
    import photo_edit as pe
    db = SessionLocal()
    try:
        for n, vid in enumerate(ids):
            j["done"] = n
            if j.get("cancel"):
                break
            v = db.query(Video).filter(Video.id == vid).first()
            path = pe._resolve(v.filepath) if v else None
            if not path or not os.path.exists(path):
                continue
            try:
                stamp = _stamp(path)
                row = db.query(CullInfo).filter(CullInfo.video_id == vid).first()
                if row and row.stamp == stamp:
                    continue
                m = measure(path)
                row = row or CullInfo(video_id=vid)
                row.stamp, row.taken = stamp, _taken(path, v)
                row.sharp, row.mean, row.bright, row.dark, row.fp = m["sharp"], m["mean"], m["bright"], m["dark"], m["fp"]
                db.merge(row)
                db.commit()
            except Exception as e:
                db.rollback()
                j["errors"].append(f"{v.filename}: {e}"[:200])
        j["done"] = len(ids)
        j["state"] = "done"
    except Exception as e:
        j["state"], j["error"] = "error", str(e)[:300]
    finally:
        db.close()


def measure_ids(ids: List[int], user: str = "") -> dict:
    """Start measuring these photos (those not measured yet, or changed since)."""
    j = {"id": uuid.uuid4().hex[:12], "state": "running", "done": 0, "total": len(ids), "errors": [], "error": "",
         "user": user}
    with _lock:
        JOBS[j["id"]] = j
    threading.Thread(target=_measure_run, args=(j, ids), daemon=True).start()
    return j


class AnalyseBody(BaseModel):
    folder_id: int
    sub: bool = True


@router.post("/analyse")
def analyse(body: AnalyseBody, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    ids = [v.id for v in _photos(db, body.folder_id, body.sub)]
    if not ids:
        raise HTTPException(status_code=400, detail="There are no photos in this folder")
    return measure_ids(ids, current_user.username)


@router.get("/jobs/{job_id}")
def job(job_id: str, current_user: User = Depends(get_current_user)):
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="No such job")
    return j


# --------------------------------------------------------------------------
# groups and suggestions
# --------------------------------------------------------------------------

def _ham(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def groups_of(photos: List[Video], info: Dict[int, CullInfo]) -> List[dict]:
    """Shots of the same view one after another -> a group; the one to keep in each; why others are weak."""
    measured = [v for v in photos if v.id in info]
    measured.sort(key=lambda v: (info[v.id].taken or 0, v.filename or ""))
    runs: List[List[Video]] = []
    for v in measured:
        x = info[v.id]
        home = None
        for g in reversed(runs[-40:]):
            if g[-1].folder_id != v.folder_id:
                continue
            last = info[g[-1].id]
            dt = abs(x.taken - last.taken) if x.taken is not None and last.taken is not None else 0.0
            d = min(_ham(x.fp, info[m.id].fp) for m in g[-4:])
            # the next frame of a bracket: seconds later, a very different exposure (blown windows move the fingerprint)
            bracket_step = dt <= 5 and abs(x.mean - last.mean) > 40 and d <= 24
            # the same view shot again: straight after, or even much later in the same session when nearly identical
            if (dt <= SAME_BURST_S and d <= SAME_VIEW) or bracket_step or (dt <= 3 * 3600 and d <= 4):
                home = g
                break
        if home is not None:
            home.append(v)
        else:
            runs.append([v])
    out = []
    for g in runs:
        xs = [info[v.id] for v in g]
        means = [x.mean for x in xs]
        times = [x.taken for x in xs if x.taken is not None]
        # a bracket set: the same view at clearly different exposures within seconds
        bracket = len(g) >= 2 and (max(means) - min(means)) > 40 and (not times or max(times) - min(times) <= 20)
        top = max(x.sharp for x in xs)
        items, best, best_score = [], None, -1e9
        for v, x in zip(g, xs):
            why = []
            if x.sharp < BLURRY or (len(g) > 1 and not bracket and x.sharp < 0.6 * top):
                why.append("Blurry")
            if not bracket:
                if x.mean < 22 or x.dark > 0.6:
                    why.append("Too dark")
                elif x.bright > 0.5:
                    why.append("Too bright")
            score = x.sharp / (top + 1e-6) - 0.5 * len(why) - abs(x.mean - 118) / 400.0
            if score > best_score:
                best, best_score = v.id, score
            items.append({"id": v.id, "name": v.filename, "thumb": v.thumbnail_path, "rating": v.rating or 0,
                          "sharp": round(x.sharp / (top + 1e-6), 3), "why": why})
        for it in items:
            if not bracket and len(g) > 1 and it["id"] != best and "Blurry" not in it["why"]:
                it["why"].append("Another shot of the same view")
        out.append({"key": f"g{g[0].id}", "kind": "bracket" if bracket else ("group" if len(g) > 1 else "single"),
                    "best": None if bracket else best, "items": items})
    return out


@router.get("/folder/{folder_id}")
def folder(folder_id: int, sub: bool = True, db: Session = Depends(get_db),
           current_user: User = Depends(get_current_user)):
    import photo_edit as pe
    photos = _photos(db, folder_id, sub)
    ids = [v.id for v in photos]
    info = {r.video_id: r for r in db.query(CullInfo).filter(CullInfo.video_id.in_(ids)).all()} if ids else {}
    picks = {r.video_id: r.pick or 0 for r in db.query(pe.PhotoEdit.video_id, pe.PhotoEdit.pick)
             .filter(pe.PhotoEdit.video_id.in_(ids)).all()} if ids else {}
    gs = groups_of(photos, info)
    for g in gs:
        for it in g["items"]:
            it["pick"] = picks.get(it["id"], 0)
    return {"groups": gs, "photos": len(photos), "measured": len(info),
            "unmeasured": [v.id for v in photos if v.id not in info]}


class PicksBody(BaseModel):
    picks: Dict[int, int] = Field(default_factory=dict)          # video id -> 1 keep, -1 reject, 0 neither


@router.post("/picks")
def set_picks(body: PicksBody, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Many picks at once ("keep the best of every group")."""
    import photo_edit as pe
    if current_user.role == "viewer":
        raise HTTPException(status_code=403, detail="Your account has view-only access.")
    if len(body.picks) > 5000:
        raise HTTPException(status_code=400, detail="Too many at once")
    rows = {r.video_id: r for r in db.query(pe.PhotoEdit).filter(pe.PhotoEdit.video_id.in_(list(body.picks))).all()}
    for vid, p in body.picks.items():
        p = max(-1, min(1, int(p)))
        row = rows.get(vid)
        if not row:
            row = pe.PhotoEdit(video_id=vid, recipe="{}", updated_at=datetime.utcnow())
            db.add(row)
        row.pick = p or None
    db.commit()
    return {"ok": True, "count": len(body.picks)}


def install(app):
    Base.metadata.create_all(bind=engine, tables=[CullInfo.__table__])
    app.include_router(router)
