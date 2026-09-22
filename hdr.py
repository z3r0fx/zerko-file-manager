"""HDR from brackets.

A property shoot comes home as sets of three, five or seven frames of the same
room at different exposures. This finds those sets and fuses each one into a
single natural-looking photo, in bulk, without anybody opening Lightroom.

The fusion is Mertens exposure fusion, not tone-mapped HDR: it picks the
best-exposed, best-contrast pixels from each frame and blends them in a
pyramid. That is what gives the clean, believable look estate agents ask for -
bright windows that still show the view, no halos, no grey mush - and it needs
no camera response curve, so it works with JPEG, TIFF and RAW alike.

Nothing is overwritten. Results land in a subfolder (default "HDR") beside the
frames they came from, and are added to the library so the existing proxy
export can take them straight to portal sizes.
"""

import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import previews
from auth import get_admin_user, get_current_user
from database import IndexedFolder, SessionLocal, User, Video, get_db
from media_type_utils import get_media_type

router = APIRouter(prefix="/api/hdr", tags=["hdr"])

_media_root: Optional[Path] = None
_resolve = lambda p: p              # replaced by install()

JOBS: Dict[str, dict] = {}
_jobs_lock = threading.Lock()
_slots = threading.Semaphore(1)     # fusing is memory-hungry; one at a time

MAX_GROUP = 9
KEEP_HOURS = 12
# 6000px on the long edge is ~24MP: more than any property portal will ever
# show, and it keeps three frames of pyramid maths inside sane memory.
DEFAULT_MAX_DIM = 6000


# --------------------------------------------------------------------------
# reading photos
# --------------------------------------------------------------------------

def _cv2():
    import cv2                       # imported lazily: the app boots without it
    return cv2


def _exif_capture(path: str) -> Tuple[Optional[datetime], Optional[float], Optional[float]]:
    """(taken_at, exposure_bias, shutter). Falls back to the file's own time."""
    taken = bias = shutter = None
    try:
        from PIL import Image, ExifTags
        with Image.open(path) as im:
            exif = im.getexif()
            if exif:
                tags = {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}
                raw = tags.get("DateTimeOriginal") or tags.get("DateTime")
                if raw:
                    try:
                        taken = datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")
                    except ValueError:
                        pass
                ifd = exif.get_ifd(0x8769) if hasattr(exif, "get_ifd") else {}
                sub = {ExifTags.TAGS.get(k, k): v for k, v in (ifd or {}).items()}
                raw = sub.get("DateTimeOriginal")
                if raw and not taken:
                    try:
                        taken = datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")
                    except ValueError:
                        pass
                for key, into in (("ExposureBiasValue", "bias"), ("ExposureTime", "shutter")):
                    val = sub.get(key, tags.get(key))
                    if val is None:
                        continue
                    try:
                        num = float(val)
                    except (TypeError, ValueError):
                        continue
                    if into == "bias":
                        bias = num
                    else:
                        shutter = num
    except Exception:
        pass
    if taken is None:
        try:
            taken = datetime.fromtimestamp(os.path.getmtime(path))
        except OSError:
            taken = None
    return taken, bias, shutter


def _load(path: str, max_dim: int) -> Tuple[np.ndarray, str]:
    """Any photo we can read, as 8-bit BGR, no bigger than max_dim.

    Returns the image and how it was decoded: "full" when the file was read
    properly, or "preview" when all we could get was the small JPEG the camera
    buried inside the RAW. That difference matters enormously - fusing three
    embedded previews gives you a soft, blocky photo that looks nothing like
    what the camera captured - so the caller gets told rather than guessing.
    """
    cv2 = _cv2()
    ext = Path(path).suffix.lower()
    img = None
    how = "full"

    if ext in previews.RAW_EXT:
        try:
            import rawpy
            with rawpy.imread(path) as raw:
                # Camera white balance, and NO auto brightness: every frame in
                # the bracket has to keep its own exposure or there is nothing
                # left to fuse.
                rgb = raw.postprocess(use_camera_wb=True, no_auto_bright=True,
                                      output_bps=8)
            img = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        except Exception:
            img = None

    # Deliberately NOT cv2.imread for RAW: OpenCV will happily open a DNG as a
    # plain TIFF and hand back the undemosaiced sensor data, which looks like a
    # badly upscaled photo - aliased, blocky, wrong colour. Better to fall
    # through to the embedded preview and say so.
    if img is None and ext not in previews.RAW_EXT:
        img = cv2.imread(path, cv2.IMREAD_COLOR)

    if img is None:                       # last resort: whatever previews can do
        how = "preview" if ext in previews.RAW_EXT else "full"
        tmp = str(Path(path).with_suffix("")) + f".hdrtmp-{uuid.uuid4().hex[:6]}.jpg"
        ok, why = previews.make_image_jpeg(path, tmp, width=max_dim)
        if ok:
            img = cv2.imread(tmp, cv2.IMREAD_COLOR)
        try:
            os.remove(tmp)
        except OSError:
            pass
        if img is None:
            raise RuntimeError(why or f"could not read {os.path.basename(path)}")

    h, w = img.shape[:2]
    longest = max(h, w)
    # Only ever downscale. Enlarging a frame to hit a requested size invents
    # detail that was never there, which is what made "full resolution" the
    # worst-looking option rather than the best.
    if max_dim and longest > max_dim:
        scale = max_dim / float(longest)
        img = cv2.resize(img, (int(round(w * scale)), int(round(h * scale))),
                         interpolation=cv2.INTER_AREA)
    return img, how


# --------------------------------------------------------------------------
# finding the brackets
# --------------------------------------------------------------------------

class ScanBody(BaseModel):
    folder_id: Optional[int] = None
    video_ids: List[int] = []
    gap_seconds: float = Field(4.0, ge=0.5, le=120)
    fixed_size: int = Field(0, ge=0, le=MAX_GROUP)    # 0 = work it out


def _group(items: List[dict], gap: float, fixed: int) -> List[List[dict]]:
    """Split a time-ordered list of frames into brackets.

    Fixed size is the safe option when you always shoot the same number. On
    auto, a set ends when the next frame is far away in time, or when the
    exposure compensation starts over - which is exactly what a bracket does
    when the camera rolls round to the next set.
    """
    groups: List[List[dict]] = []
    current: List[dict] = []

    def flush():
        if len(current) >= 2:
            groups.append(list(current))
        current.clear()

    for item in items:
        if not current:
            current.append(item)
            continue
        if fixed:
            current.append(item)
            if len(current) == fixed:
                flush()
            continue

        prev = current[-1]
        apart = None
        if item["taken_at"] and prev["taken_at"]:
            apart = abs((item["taken_at"] - prev["taken_at"]).total_seconds())
        restarted = (
            item.get("bias") is not None
            and current[0].get("bias") is not None
            and len(current) > 1
            and abs(item["bias"] - current[0]["bias"]) < 1e-6
        )
        if (apart is not None and apart > gap) or restarted or len(current) >= MAX_GROUP:
            flush()
        current.append(item)
    flush()
    return groups


@router.post("/scan")
def scan(body: ScanBody, db: Session = Depends(get_db),
         current_user: User = Depends(get_current_user)):
    q = db.query(Video).filter(Video.is_active.is_(True), Video.media_type == "photo")
    if body.video_ids:
        q = q.filter(Video.id.in_(body.video_ids[:2000]))
    elif body.folder_id is not None:
        q = q.filter(Video.folder_id == body.folder_id)
    else:
        raise HTTPException(status_code=400, detail="Pick a folder or some photos first")

    rows = q.all()
    if not rows:
        return {"groups": [], "ungrouped": 0, "message": "No photos here"}

    items = []
    for v in rows:
        real = _resolve(v.filepath)
        if not real or not os.path.exists(real):
            continue
        taken, bias, shutter = _exif_capture(real)
        items.append({
            "id": v.id, "filename": v.filename, "path": real,
            "thumbnail_path": v.thumbnail_path,
            "taken_at": taken, "bias": bias, "shutter": shutter,
        })
    items.sort(key=lambda i: (i["taken_at"] or datetime.min, i["filename"]))

    groups = _group(items, body.gap_seconds, body.fixed_size)
    grouped_ids = {i["id"] for g in groups for i in g}

    def out(i):
        return {
            "id": i["id"], "filename": i["filename"],
            "thumbnail_path": i["thumbnail_path"],
            "taken_at": i["taken_at"].isoformat() if i["taken_at"] else None,
            "bias": i["bias"],
        }

    return {
        "groups": [{"key": f"g{n}", "items": [out(i) for i in g]} for n, g in enumerate(groups)],
        "ungrouped": len([i for i in items if i["id"] not in grouped_ids]),
        "message": f"{len(groups)} bracket{'' if len(groups) == 1 else 's'} found",
    }


# --------------------------------------------------------------------------
# fusing
# --------------------------------------------------------------------------

class MergeBody(BaseModel):
    groups: List[List[int]]
    align: bool = True                       # handheld: line the frames up first
    max_dim: int = Field(DEFAULT_MAX_DIM, ge=1000, le=20000)
    fmt: str = Field("jpg", pattern="^(jpg|tif)$")
    quality: int = Field(92, ge=60, le=100)
    subfolder: str = "HDR"
    add_to_library: bool = True
    # A little contrast and warmth back after fusion, which flattens slightly.
    polish: bool = True
    # Fuse RAW even when only the camera's embedded preview can be read. Off,
    # because the result is far softer than the frames deserve.
    allow_preview_raw: bool = False


def _polish(img: np.ndarray) -> np.ndarray:
    """A gentle S-curve and a touch of saturation - what fusion takes out."""
    cv2 = _cv2()
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l = cv2.createCLAHE(clipLimit=1.4, tileGridSize=(8, 8)).apply(l)
    img = cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV).astype(np.float32)
    hsv[..., 1] *= 1.06
    np.clip(hsv[..., 1], 0, 255, out=hsv[..., 1])
    return cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)


def _fuse(paths: List[str], body: MergeBody) -> np.ndarray:
    cv2 = _cv2()
    loaded = [_load(p, body.max_dim) for p in paths]
    frames = [img for img, _ in loaded]

    degraded = [os.path.basename(p) for p, (_, how) in zip(paths, loaded) if how == "preview"]
    if degraded and not body.allow_preview_raw:
        raise RuntimeError(
            "no RAW decoder is installed, so only the small preview inside "
            f"{degraded[0]} could be read - fusing that would throw away most "
            "of the detail. Install one with:  venv/bin/pip install rawpy  "
            "(or tick 'fuse anyway' to accept the lower quality)")

    # Frames that differ in size (one shot portrait by mistake) cannot fuse.
    shape = frames[0].shape
    frames = [f for f in frames if f.shape == shape]
    if len(frames) < 2:
        raise RuntimeError("the frames in this set are not the same size")

    if body.align:
        try:
            cv2.createAlignMTB().process(frames, frames)
        except Exception:
            pass                            # tripod shots do not need it anyway

    merged = cv2.createMergeMertens().process(frames)      # float32 0..1
    out = np.clip(merged * 255.0, 0, 255).astype(np.uint8)
    return _polish(out) if body.polish else out


def _index(out_path: str, source: Video, db: Session):
    """Put the finished photo in the library, in a folder row of its own."""
    parent_dir = str(Path(out_path).parent)
    folder = db.query(IndexedFolder).filter(IndexedFolder.path == parent_dir).first()
    if not folder:
        rel = parent_dir
        try:
            if _media_root:
                rel = str(Path(parent_dir).relative_to(_media_root))
        except ValueError:
            pass
        parent = db.query(IndexedFolder).filter(
            IndexedFolder.path == str(Path(parent_dir).parent)).first()
        folder = IndexedFolder(path=parent_dir, name=Path(parent_dir).name,
                               relative_path=rel,
                               parent_id=parent.id if parent else None,
                               added_at=datetime.utcnow())
        db.add(folder)
        db.commit()
        db.refresh(folder)

    if db.query(Video.id).filter(Video.filepath == out_path).first():
        return
    name = os.path.basename(out_path)
    thumb_rel = None
    try:
        import indexer
        tname = indexer.thumb_name(out_path)
        thumbs = Path(_media_root) / "thumbnails" if _media_root else None
        if thumbs:
            thumbs.mkdir(parents=True, exist_ok=True)
            ok, _ = previews.make_image_jpeg(out_path, str(thumbs / tname), width=640)
            if ok:
                thumb_rel = f"/thumbnails/{tname}"
    except Exception:
        pass
    now = datetime.utcnow()
    db.add(Video(
        filename=name, filepath=out_path, file_size=os.path.getsize(out_path),
        duration=0.0, thumbnail_path=thumb_rel, folder_id=folder.id,
        media_type=get_media_type(name) or "photo", status=source.status or "raw",
        created_at=now, uploaded_at=now, uploaded_by="hdr",
    ))
    db.commit()


def _run(job_id: str, body: MergeBody):
    with _slots:
        db = SessionLocal()
        try:
            for n, ids in enumerate(body.groups):
                with _jobs_lock:
                    if JOBS[job_id].get("cancelled"):
                        break
                    JOBS[job_id]["current"] = f"Set {n + 1} of {len(body.groups)}"
                rows = db.query(Video).filter(Video.id.in_(ids)).all()
                rows.sort(key=lambda v: ids.index(v.id))
                paths = []
                for v in rows:
                    real = _resolve(v.filepath)
                    if real and os.path.exists(real):
                        paths.append(real)
                try:
                    if len(paths) < 2:
                        raise RuntimeError("fewer than two frames could be read")
                    img = _fuse(paths, body)

                    middle = rows[len(rows) // 2]
                    # In a project, every stage has its own folder and the
                    # merge belongs in the HDR one - that is what makes the
                    # next step see only the fused frames. Outside a project,
                    # results land beside the originals as before.
                    out_dir = None
                    try:
                        import projects
                        out_dir = projects.stage_dir(db, paths[0], "hdr")
                    except Exception:
                        out_dir = None
                    if out_dir is None:
                        out_dir = Path(paths[0]).parent / (body.subfolder or "HDR")
                    out_dir.mkdir(parents=True, exist_ok=True)
                    stem = Path(middle.filename).stem
                    ext = ".jpg" if body.fmt == "jpg" else ".tif"
                    out_path = out_dir / f"{stem}_HDR{ext}"
                    k = 2
                    while out_path.exists():
                        out_path = out_dir / f"{stem}_HDR ({k}){ext}"
                        k += 1

                    cv2 = _cv2()
                    params = ([cv2.IMWRITE_JPEG_QUALITY, body.quality]
                              if body.fmt == "jpg" else [])
                    if not cv2.imwrite(str(out_path), img, params):
                        raise RuntimeError("could not write the finished photo")

                    if body.add_to_library:
                        try:
                            _index(str(out_path), middle, db)
                        except Exception as e:
                            db.rollback()
                            with _jobs_lock:
                                JOBS[job_id]["errors"].append(
                                    f"{out_path.name}: saved, but not added to the library ({e})")

                    with _jobs_lock:
                        JOBS[job_id]["results"].append({
                            "path": str(out_path), "filename": out_path.name,
                            "frames": len(paths),
                        })
                except Exception as e:
                    with _jobs_lock:
                        first = rows[0].filename if rows else f"set {n + 1}"
                        JOBS[job_id]["errors"].append(f"{first}: {e}")
                finally:
                    with _jobs_lock:
                        JOBS[job_id]["done"] = n + 1
        finally:
            db.close()
            with _jobs_lock:
                JOBS[job_id]["running"] = False
                JOBS[job_id]["finished_at"] = time.time()
                JOBS[job_id]["current"] = None


@router.post("/merge")
def merge(body: MergeBody, current_user: User = Depends(get_current_user)):
    groups = [g for g in body.groups if len(g) >= 2]
    if not groups:
        raise HTTPException(status_code=400, detail="Nothing to fuse")
    body.groups = groups
    _purge_old()
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        JOBS[job_id] = {
            "id": job_id, "running": True, "done": 0, "total": len(groups),
            "current": "Starting…", "results": [], "errors": [],
            "started_at": time.time(), "finished_at": None, "cancelled": False,
        }
    threading.Thread(target=_run, args=(job_id, body), daemon=True).start()
    return {"job_id": job_id, "total": len(groups)}


@router.get("/jobs/{job_id}")
def job(job_id: str, current_user: User = Depends(get_current_user)):
    with _jobs_lock:
        state = JOBS.get(job_id)
        if not state:
            raise HTTPException(status_code=404, detail="No such job")
        return dict(state)


@router.post("/jobs/{job_id}/cancel")
def cancel(job_id: str, current_user: User = Depends(get_current_user)):
    with _jobs_lock:
        if job_id in JOBS:
            JOBS[job_id]["cancelled"] = True
    return {"status": "cancelling"}


def _purge_old():
    cutoff = time.time() - KEEP_HOURS * 3600
    with _jobs_lock:
        for jid in [k for k, v in JOBS.items()
                    if not v["running"] and (v.get("finished_at") or 0) < cutoff]:
            JOBS.pop(jid, None)


def install(app, media_root, resolve_media_path):
    global _media_root, _resolve
    _media_root = Path(media_root) if media_root else None
    _resolve = resolve_media_path
    app.include_router(router)


# --------------------------------------------------------------------------
# RAW support
# --------------------------------------------------------------------------

def raw_decoder_state() -> dict:
    """Is there anything here that can develop a RAW file properly?"""
    try:
        import rawpy  # noqa: F401
        return {"installed": True, "name": "rawpy"}
    except Exception:
        pass
    wheels = []
    try:
        here = Path(__file__).resolve().parent / "wheels"
        wheels = sorted(str(p) for p in here.glob("rawpy-*.whl"))
    except Exception:
        pass
    return {"installed": False, "name": None, "offline_wheel": bool(wheels)}


@router.get("/raw-support")
def raw_support(current_user: User = Depends(get_current_user)):
    return raw_decoder_state()


@router.post("/raw-support/install")
def install_raw_support(current_user: User = Depends(get_admin_user)):
    """Install the RAW decoder from here, so nobody has to open a terminal.

    Tries the wheel shipped next to the app first - that works with no
    internet at all - and falls back to PyPI. pip runs with the very
    interpreter this app is running on, so it lands in the right environment.
    """
    import subprocess
    import sys

    state = raw_decoder_state()
    if state["installed"]:
        return {"installed": True, "message": "RAW support is already installed"}

    here = Path(__file__).resolve().parent / "wheels"
    wheels = sorted(str(p) for p in here.glob("rawpy-*.whl")) if here.is_dir() else []
    attempts = ([[sys.executable, "-m", "pip", "install", "--no-index", wheels[-1]]] if wheels else [])
    attempts.append([sys.executable, "-m", "pip", "install", "rawpy"])

    log = []
    for cmd in attempts:
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            log.append((p.stdout or "")[-1500:] + (p.returncode and (p.stderr or "")[-1500:] or ""))
            if p.returncode == 0:
                break
        except Exception as e:
            log.append(str(e)[:500])

    # Check by asking for it, not by trusting pip's exit code.
    import importlib
    try:
        importlib.invalidate_caches()
        importlib.import_module("rawpy")
        ok = True
    except Exception as e:
        ok = False
        log.append(f"still cannot import rawpy: {str(e)[:300]}")

    if not ok:
        raise HTTPException(
            status_code=500,
            detail="Could not install RAW support automatically. " + (log[-1] if log else ""))
    return {"installed": True, "message": "RAW support installed - re-run the merge",
            "log": log[-1][-400:] if log else ""}

