"""Photo export: 16:9 copies of your photos, ready to hand to a listing site.

Originals are never touched. Two things live here:

  * a saved *framing* per photo (`photo_crops`): how far up or down a 4:3 photo
    sits inside its 16:9 window. The library grid and the viewer use it so the
    photo you see there is the photo you will export; and
  * an export job: pick photos, a size and a quality, get back one ZIP of
    16:9 JPEGs. The work runs in a background thread and the browser polls.

Framing is a single number `y` from 0 to 1. For a photo taller than 16:9 (the
usual 4:3 or 3:2 camera frame) it is the vertical position of the window:
0 keeps the top, 1 keeps the bottom, 0.5 is centred. For a photo wider than
16:9 (a panorama) it is the horizontal position the same way. A photo that is
already 16:9 has nothing to choose.
"""

import io
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, Float, Integer
from sqlalchemy.orm import Session

import previews
from auth import get_current_user, get_user_from_token
from database import Base, SessionLocal, User, Video, engine, get_db

router = APIRouter(prefix="/api", tags=["photo-export"])

RATIO = 16 / 9
MAX_PHOTOS = 2000
KEEP_HOURS = 24
MIN_WIDTH, MAX_WIDTH = 160, 8000


class PhotoCrop(Base):
    __tablename__ = "photo_crops"
    video_id = Column(Integer, primary_key=True)
    offset_y = Column(Float, nullable=False, default=0.5)
    updated_at = Column(DateTime, default=datetime.utcnow)


_media_root: Optional[Path] = None
_resolve = lambda p: p            # replaced by install()
JOBS: Dict[str, dict] = {}
_jobs_lock = threading.Lock()
_slots = threading.Semaphore(2)   # two exports at a time is plenty on a home server


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------
def _clamp01(v) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.5
    if v != v:                    # NaN
        return 0.5
    return max(0.0, min(1.0, v))


class CropsIn(BaseModel):
    # {"12": 0.3, "13": null}  - null forgets the saved framing (back to centred)
    crops: Dict[str, Optional[float]]


@router.get("/photos/crops")
def list_crops(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return {str(r.video_id): round(_clamp01(r.offset_y), 4) for r in db.query(PhotoCrop).all()}


@router.put("/photos/crops")
def save_crops(body: CropsIn, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if len(body.crops) > 5000:
        raise HTTPException(status_code=413, detail="Too many photos in one request")
    saved = removed = 0
    for key, val in body.crops.items():
        try:
            vid = int(key)
        except ValueError:
            continue
        row = db.query(PhotoCrop).filter(PhotoCrop.video_id == vid).first()
        if val is None or abs(_clamp01(val) - 0.5) < 0.0005:
            # centred is the default, so there is nothing to remember
            if row:
                db.delete(row)
                removed += 1
            continue
        if row:
            row.offset_y = _clamp01(val)
            row.updated_at = datetime.utcnow()
        else:
            db.add(PhotoCrop(video_id=vid, offset_y=_clamp01(val)))
        saved += 1
    db.commit()
    return {"saved": saved, "removed": removed}


def frame_box(w: int, h: int, y: float):
    """The 16:9 window (left, top, right, bottom) inside a w x h photo."""
    y = _clamp01(y)
    if w * 9 == h * 16:
        return 0, 0, w, h
    if w / h < RATIO:                                   # taller than 16:9: slide up/down
        ch = max(1, round(w * 9 / 16))
        top = round((h - ch) * y)
        return 0, top, w, top + ch
    cw = max(1, round(h * 16 / 9))                      # wider than 16:9: slide left/right
    left = round((w - cw) * y)
    return left, 0, left + cw, h


# ---------------------------------------------------------------------------
# Rendering one photo
# ---------------------------------------------------------------------------
def _open_photo(src: str):
    """A Pillow image, upright and in RGB, from anything we can decode."""
    from PIL import Image, ImageOps
    ext = Path(src).suffix.lower()
    if ext not in previews.RAW_EXT:
        try:
            try:
                import pillow_heif
                pillow_heif.register_heif_opener()
            except Exception:
                pass
            with Image.open(src) as im:
                im.load()
                icc = im.info.get("icc_profile")
                im = ImageOps.exif_transpose(im)
                im.info["icc_profile"] = icc
                return _rgb(im)
        except Exception:
            pass
    # RAW, or something Pillow can't read: let the preview code (rawpy / ffmpeg) do it
    fd, tmp = tempfile.mkstemp(suffix=".jpg", prefix="pxexp-")
    os.close(fd)
    try:
        ok, why = previews.make_image_jpeg(src, tmp, 6000)
        if not ok:
            raise RuntimeError(why or "could not read this photo")
        with Image.open(tmp) as im:
            im.load()
            return _rgb(im.copy())
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _rgb(im):
    from PIL import Image
    if im.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", im.size, (255, 255, 255))
        rgba = im.convert("RGBA")
        bg.paste(rgba, mask=rgba.split()[3])
        icc = im.info.get("icc_profile")
        bg.info["icc_profile"] = icc
        return bg
    if im.mode != "RGB":
        icc = im.info.get("icc_profile")
        im = im.convert("RGB")
        im.info["icc_profile"] = icc
    return im


def _encode(im, quality: int, icc) -> bytes:
    buf = io.BytesIO()
    kw = dict(format="JPEG", quality=quality, optimize=True, progressive=True)
    if icc:
        kw["icc_profile"] = icc
    im.save(buf, **kw)
    return buf.getvalue()


def render(src: str, y: float, width: Optional[int], quality: int, max_kb: Optional[int]):
    """-> (jpeg bytes, (w, h), notes). `width` None = native (no resizing)."""
    from PIL import Image
    im = _open_photo(src)
    icc = im.info.get("icc_profile")
    notes = []
    box = frame_box(im.width, im.height, y)
    crop = im.crop(box)
    if width is not None:
        if crop.width < width:
            notes.append(f"the photo is only {crop.width}px wide at 16:9, so it was not enlarged")
            width = crop.width
        if crop.width != width:
            height = max(1, round(width * 9 / 16))
            crop = crop.resize((width, height), Image.Resampling.LANCZOS)
    if im.height > im.width * 1.05:
        notes.append("portrait photo - a 16:9 window keeps only a thin strip of it")
    q = max(30, min(98, int(quality)))
    data = _encode(crop, q, icc)
    if max_kb:
        limit = int(max_kb) * 1024
        if len(data) > limit:
            lo, hi, best = 30, q, None
            while lo <= hi:                             # highest quality that still fits
                mid = (lo + hi) // 2
                trial = _encode(crop, mid, icc)
                if len(trial) <= limit:
                    best, lo = trial, mid + 1
                else:
                    hi = mid - 1
            if best is None:
                data = _encode(crop, 30, icc)
                notes.append(f"still {len(data) // 1024} KB at the lowest quality - choose a smaller size to get under {max_kb} KB")
            else:
                data = best
                notes.append("quality lowered to fit the size limit")
    return data, crop.size, notes


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
class GenerateIn(BaseModel):
    ids: List[int] = Field(..., min_length=1)
    width: Optional[int] = None            # None or 0 = native
    quality: int = 85
    max_kb: Optional[int] = None
    crops: Dict[str, Optional[float]] = {}  # framing chosen in this session, wins over saved
    numbered: bool = False                  # photo-01.jpg, photo-02.jpg ... in the order given
    prefix: str = "photo"


def _safe(stem: str) -> str:
    s = re.sub(r"[^\w\-. ]+", "_", stem, flags=re.UNICODE).strip(" ._")
    return s[:80] or "photo"


def _purge_old():
    cutoff = time.time() - KEEP_HOURS * 3600
    with _jobs_lock:
        old = [j for j, v in JOBS.items() if v["created"] < cutoff and v["state"] != "running"]
        for j in old:
            JOBS.pop(j, None)
    if _media_root:
        base = _media_root / ".proxies_export"
        if base.is_dir():
            for d in base.iterdir():
                try:
                    if d.is_dir() and d.stat().st_mtime < cutoff and d.name not in JOBS:
                        shutil.rmtree(d, ignore_errors=True)
                except OSError:
                    pass


def _run(job_id: str, items: list, opts: dict):
    job = JOBS[job_id]
    out_dir: Path = job["dir"]
    used = set()
    written = []
    try:
        with _slots:
            for i, it in enumerate(items):
                job["current"] = it["filename"]
                try:
                    src = _resolve(it["filepath"])
                    if not src or not os.path.exists(src):
                        raise RuntimeError("the file is not on disk")
                    data, size, notes = render(src, it["y"], opts["width"], opts["quality"], opts["max_kb"])
                    if opts["numbered"]:
                        stem = f"{opts['prefix']}-{i + 1:02d}"
                    else:
                        stem = f"{_safe(Path(it['filename']).stem)}_{size[0]}x{size[1]}"
                    name, n = f"{stem}.jpg", 2
                    while name.lower() in used:
                        name = f"{stem}_{n}.jpg"
                        n += 1
                    used.add(name.lower())
                    (out_dir / name).write_bytes(data)
                    written.append(name)
                    job["ok"].append({"id": it["id"], "name": name, "width": size[0], "height": size[1],
                                      "kb": max(1, len(data) // 1024)})
                    for note in notes:
                        job["warnings"].append({"id": it["id"], "filename": it["filename"], "note": note})
                except Exception as e:                  # one bad photo must not sink the batch
                    job["failed"].append({"id": it["id"], "filename": it["filename"], "reason": str(e)[:200] or "failed"})
                job["done"] = i + 1
            if not written:
                job["state"] = "error"
                job["error"] = "None of the photos could be exported."
                return
            job["current"] = "Packing the ZIP"
            first = job["ok"][0]
            label = f"{first['width']}x{first['height']}" if opts["width"] else "native"
            zip_name = f"photos_16x9_{label}_{datetime.now():%Y%m%d-%H%M}.zip"
            zip_path = out_dir / zip_name
            tmp_zip = out_dir / ".building.zip"
            with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_STORED) as z:     # JPEGs don't shrink, so don't waste CPU
                for name in written:
                    z.write(out_dir / name, name)
            os.replace(tmp_zip, zip_path)
            for name in written:                                              # the ZIP is the deliverable
                try:
                    (out_dir / name).unlink()
                except OSError:
                    pass
            job["zip_name"], job["zip_path"] = zip_name, zip_path
            job["zip_kb"] = max(1, zip_path.stat().st_size // 1024)
            job["state"] = "done"
            job["current"] = ""
    except Exception as e:
        job["state"] = "error"
        job["error"] = str(e)[:300]


def _public(job: dict) -> dict:
    return {
        "id": job["id"], "state": job["state"], "total": job["total"], "done": job["done"],
        "current": job.get("current", ""), "ok": len(job["ok"]), "failed": job["failed"],
        "warnings": job["warnings"][:200], "error": job.get("error"),
        "zip_name": job.get("zip_name"), "zip_kb": job.get("zip_kb"),
        "files": job["ok"][:5],
    }


@router.post("/photo-export/generate")
def generate(body: GenerateIn, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if len(body.ids) > MAX_PHOTOS:
        raise HTTPException(status_code=413, detail=f"Export up to {MAX_PHOTOS} photos at a time")
    width = body.width if body.width else None
    if width is not None and not (MIN_WIDTH <= width <= MAX_WIDTH):
        raise HTTPException(status_code=422, detail=f"Width must be between {MIN_WIDTH} and {MAX_WIDTH} px")
    if body.max_kb is not None and body.max_kb < 20:
        raise HTTPException(status_code=422, detail="Size limit must be at least 20 KB")
    ids = list(dict.fromkeys(body.ids))                # keep order, drop repeats
    rows = {v.id: v for v in db.query(Video).filter(Video.id.in_(ids)).all()}
    saved = {r.video_id: r.offset_y for r in db.query(PhotoCrop).filter(PhotoCrop.video_id.in_(ids)).all()}
    override = {}
    for k, v in body.crops.items():
        try:
            override[int(k)] = 0.5 if v is None else _clamp01(v)
        except ValueError:
            pass
    items = []
    for i in ids:
        v = rows.get(i)
        if not v or v.media_type != "photo" or v.is_active is False:
            continue
        items.append({"id": v.id, "filename": v.filename or f"photo{v.id}", "filepath": v.filepath,
                      "y": override.get(v.id, saved.get(v.id, 0.5))})
    if not items:
        raise HTTPException(status_code=404, detail="None of those are photos in the library")

    _purge_old()
    job_id = uuid.uuid4().hex[:16]
    out_dir = (_media_root or Path(tempfile.gettempdir())) / ".proxies_export" / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    job = {"id": job_id, "state": "running", "total": len(items), "done": 0, "current": "", "ok": [],
           "failed": [], "warnings": [], "created": time.time(), "dir": out_dir, "user": current_user.username}
    with _jobs_lock:
        JOBS[job_id] = job
    opts = {"width": width, "quality": body.quality, "max_kb": body.max_kb,
            "numbered": body.numbered, "prefix": _safe(body.prefix) or "photo"}
    threading.Thread(target=_run, args=(job_id, items, opts), daemon=True, name=f"photo-export-{job_id}").start()
    return _public(job)


def _job_for(job_id: str, user: User) -> dict:
    job = JOBS.get(job_id)
    if not job or (job["user"] != user.username and user.role != "admin"):
        raise HTTPException(status_code=404, detail="That export has expired - generate it again")
    return job


@router.get("/photo-export/jobs/{job_id}")
def job_status(job_id: str, current_user: User = Depends(get_current_user)):
    return _public(_job_for(job_id, current_user))


@router.get("/photo-export/jobs/{job_id}/download")
def job_download(request: Request, job_id: str, token: Optional[str] = None, db: Session = Depends(get_db)):
    # A plain browser download can't send an Authorization header, so the link
    # carries the token like the video and photo URLs do.
    if token:
        user = get_user_from_token(token, db)
    else:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
        user = get_user_from_token(auth[7:], db)
    job = _job_for(job_id, user)
    if job["state"] != "done" or not job.get("zip_path") or not Path(job["zip_path"]).exists():
        raise HTTPException(status_code=409, detail="The ZIP is not ready")
    return FileResponse(str(job["zip_path"]), media_type="application/zip", filename=job["zip_name"],
                        headers={"Cache-Control": "no-store"})


def install(app, media_root, resolve_media_path):
    global _media_root, _resolve
    _media_root = Path(media_root) if media_root else None
    _resolve = resolve_media_path
    Base.metadata.create_all(bind=engine, tables=[PhotoCrop.__table__])
    app.include_router(router)
    try:
        _purge_old()
    except Exception:
        pass
