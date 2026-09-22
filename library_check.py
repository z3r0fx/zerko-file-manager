"""Does the library still match the disk?

The index is a picture of the media folder taken at scan time. Anything that
moves, gets renamed or is deleted by another program - Explorer, Resolve, a
sync client - leaves the two out of step, and you only find out when a
thumbnail breaks months later.

This walks both sides and says what does not line up:

  * missing  - a row in the library whose file is not on disk any more. Where
               a file of the same name (and size) turned up somewhere else,
               that is offered as a relink.
  * unindexed - a media file sitting in the folder that the library has never
               seen. Those are handled by the normal scan, so they are only
               counted and listed here as a prompt to run it.

Nothing is changed by the scan itself. Relinking and clearing are separate,
explicit steps.
"""

import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import Note, TranscriptionSegment, User, Video, get_db
from auth import get_current_user
from indexer import EXCLUDED_DIRS
from video_processor import MEDIA_EXTENSIONS

router = APIRouter(prefix="/api/library-check", tags=["library-check"])

_media_root: Optional[Path] = None
_resolve = lambda p: p                       # replaced by install()
_lock = threading.Lock()

STATE: dict = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "checked": 0,
    "total": 0,
    "message": "Not run yet",
    "missing": [],       # [{id, filename, filepath, size, suggestion}]
    "unindexed": [],     # [path, ...] capped
    "unindexed_count": 0,
    "error": None,
}

MAX_LIST = 500


class Apply(BaseModel):
    relink: List[dict] = []      # [{id, path}]
    clear: List[int] = []        # ids whose file is gone for good


def _walk_disk() -> Dict[str, List[str]]:
    """Every media file under the root, grouped by lower-cased file name."""
    found: Dict[str, List[str]] = {}
    if not _media_root or not _media_root.exists():
        return found
    for dirpath, dirnames, filenames in os.walk(_media_root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS and not d.startswith(".")]
        for name in filenames:
            if Path(name).suffix.lower() in MEDIA_EXTENSIONS:
                found.setdefault(name.lower(), []).append(os.path.join(dirpath, name))
    return found


def _scan():
    db = None
    try:
        from database import SessionLocal
        db = SessionLocal()
        rows = db.query(Video).filter(Video.is_active.is_(True)).all()
        with _lock:
            STATE["total"] = len(rows)
            STATE["message"] = "Checking files…"

        missing_rows = []
        indexed_paths = set()
        for i, v in enumerate(rows):
            real = _resolve(v.filepath)
            if real:
                indexed_paths.add(os.path.normcase(os.path.abspath(real)))
            if not real or not os.path.exists(real):
                missing_rows.append(v)
            if i % 50 == 0:
                with _lock:
                    STATE["checked"] = i
        with _lock:
            STATE["checked"] = len(rows)
            STATE["message"] = "Looking through the folder…"

        on_disk = _walk_disk()

        # A missing file with exactly one same-named candidate elsewhere, of
        # the same size where we know it, is almost certainly the same file
        # that somebody moved.
        missing = []
        for v in missing_rows:
            name = Path(v.filepath or "").name
            candidates = [p for p in on_disk.get(name.lower(), [])
                          if os.path.normcase(os.path.abspath(p)) not in indexed_paths]
            if v.file_size:
                sized = [p for p in candidates if _size(p) == v.file_size]
                if sized:
                    candidates = sized
            missing.append({
                "id": v.id,
                "filename": v.filename,
                "filepath": v.filepath,
                "size": v.file_size,
                "suggestion": candidates[0] if len(candidates) == 1 else None,
            })

        unindexed = [p for paths in on_disk.values() for p in paths
                     if os.path.normcase(os.path.abspath(p)) not in indexed_paths]

        with _lock:
            STATE["missing"] = missing[:MAX_LIST]
            STATE["unindexed"] = sorted(unindexed)[:50]
            STATE["unindexed_count"] = len(unindexed)
            relinkable = sum(1 for m in missing if m["suggestion"])
            if not missing and not unindexed:
                STATE["message"] = f"All {len(rows)} files are where the library says they are"
            else:
                bits = []
                if missing:
                    bits.append(f"{len(missing)} missing"
                                + (f" ({relinkable} can be relinked)" if relinkable else ""))
                if unindexed:
                    bits.append(f"{len(unindexed)} on disk but not in the library")
                STATE["message"] = " · ".join(bits)
    except Exception as e:                         # a scan must never wedge the app
        with _lock:
            STATE["error"] = str(e)
            STATE["message"] = f"Check failed: {e}"
    finally:
        if db:
            db.close()
        with _lock:
            STATE["running"] = False
            STATE["finished_at"] = datetime.utcnow().isoformat()


def _size(path: str) -> Optional[int]:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


@router.post("")
@router.post("/")
def start(current_user: User = Depends(get_current_user)):
    with _lock:
        if STATE["running"]:
            return {"status": "already_running"}
        STATE.update(running=True, started_at=datetime.utcnow().isoformat(), finished_at=None,
                     checked=0, total=0, missing=[], unindexed=[], unindexed_count=0,
                     error=None, message="Starting…")
    threading.Thread(target=_scan, daemon=True).start()
    return {"status": "started"}


@router.get("/status")
def status(current_user: User = Depends(get_current_user)):
    with _lock:
        return dict(STATE)


@router.post("/apply")
def apply(body: Apply, db: Session = Depends(get_db),
          current_user: User = Depends(get_current_user)):
    relinked, cleared, errors = 0, 0, []

    for item in body.relink:
        vid, path = item.get("id"), item.get("path")
        video = db.query(Video).filter(Video.id == vid).first()
        if not video or not path:
            errors.append(f"{vid}: not found")
            continue
        target = os.path.abspath(path)
        # Only ever point a row at something inside the media folder.
        if _media_root and not target.startswith(os.path.abspath(str(_media_root))):
            errors.append(f"{video.filename}: outside the media folder")
            continue
        if not os.path.exists(target):
            errors.append(f"{video.filename}: {path} is not there")
            continue
        video.filepath = target
        video.filename = os.path.basename(target)
        relinked += 1

    for vid in body.clear:
        video = db.query(Video).filter(Video.id == vid).first()
        if not video:
            continue
        real = _resolve(video.filepath)
        # Refuse to clear a row whose file is actually present - that would be
        # throwing away the record of a file you still have.
        if real and os.path.exists(real):
            errors.append(f"{video.filename}: the file is there, not clearing it")
            continue
        db.query(TranscriptionSegment).filter(TranscriptionSegment.video_id == video.id).delete()
        db.query(Note).filter(Note.media_id == video.id).delete()
        db.delete(video)
        cleared += 1

    db.commit()
    if relinked or cleared:
        with _lock:
            done = {i.get("id") for i in body.relink} | set(body.clear)
            STATE["missing"] = [m for m in STATE["missing"] if m["id"] not in done]
    return {"relinked": relinked, "cleared": cleared, "errors": errors}


def install(app, media_root, resolve_media_path):
    global _media_root, _resolve
    _media_root = Path(media_root) if media_root else None
    _resolve = resolve_media_path
    app.include_router(router)
