"""Index an existing media tree into Oblivion Drive, in place.

Nothing is copied or moved. The directory structure on disk is mirrored into
nested IndexedFolder rows, and every media file becomes a Video row pointing at
where it already lives.
"""
import os
import hashlib
import subprocess
import previews
from datetime import datetime
from pathlib import Path

from database import SessionLocal, Video, IndexedFolder
from video_processor import (
    VIDEO_EXTENSIONS, IMAGE_EXTENSIONS, AUDIO_EXTENSIONS,
    MEDIA_EXTENSIONS, SIDECAR_EXTENSIONS, get_duration_fast,
)
from media_type_utils import get_media_type, is_junk_name

# Folders the app itself writes into - never index these back in
EXCLUDED_DIRS = {"thumbnails", "proxies", "transcriptions", "_catalog-backup", "_Trash",
                 "$RECYCLE.BIN", "System Volume Information", ".oblivion"}


def media_root() -> Path:
    default = str(Path.home() / "Videos")
    return Path(os.environ.get("MEDIA_ROOT") or os.environ.get("MEDIA_UPLOAD_DIR") or default)


def thumb_name(filepath: str) -> str:
    """Stable thumbnail name.

    The old code used Python's hash(), which is randomised per process - every
    rescan produced different names and orphaned every previous thumbnail.
    """
    return hashlib.md5(str(filepath).encode("utf-8")).hexdigest() + ".jpg"


def make_thumbnail(filepath: str, out_path: str, media_type: str) -> bool:
    """Draw a thumbnail. An empty file left by an earlier failure does not
    count as one, and the reason for a failure is printed rather than lost."""
    try:
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return True
    except OSError:
        pass
    ok, why = previews.make_thumbnail(filepath, out_path, media_type)
    if not ok:
        print(f"    thumbnail failed: {os.path.basename(filepath)}: {why}")
    return ok


def index_tree(root=None, queue_proxies=False, progress=None, db=None):
    """Walk the media root and index everything under it."""
    root = Path(root) if root else media_root()
    owns_db = db is None
    db = db or SessionLocal()

    stats = {"folders": 0, "added": 0, "skipped": 0, "sidecars": 0,
             "thumbs": 0, "errors": 0, "bytes": 0}

    if not root.exists():
        return {"error": f"Media root does not exist: {root}"}

    thumbs_dir = root / "thumbnails"
    thumbs_dir.mkdir(parents=True, exist_ok=True)

    folder_ids = {}          # relative path -> IndexedFolder.id

    def say(msg):
        print(msg, flush=True)
        if progress:
            progress(msg, stats)

    try:
        for current, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIRS and not d.startswith('.'))
            rel = os.path.relpath(current, root)
            if rel == ".":
                continue

            rel = rel.replace(os.sep, "/")
            media_files = [f for f in sorted(files)
                           if Path(f).suffix.lower() in MEDIA_EXTENSIONS and not is_junk_name(f)]
            stats["sidecars"] += sum(1 for f in files
                                     if Path(f).suffix.lower() in SIDECAR_EXTENSIONS)

            parent_rel = os.path.dirname(rel)
            parent_id = folder_ids.get(parent_rel) if parent_rel else None

            folder = db.query(IndexedFolder).filter(IndexedFolder.path == str(current)).first()
            if not folder:
                folder = IndexedFolder(
                    name=rel, path=str(current), relative_path=rel,
                    parent_id=parent_id, added_at=datetime.utcnow(),
                )
                db.add(folder)
                db.commit()
                db.refresh(folder)
                stats["folders"] += 1
            else:
                folder.parent_id = parent_id
                folder.relative_path = rel
                db.commit()
            folder_ids[rel] = folder.id

            if media_files:
                say(f"  {rel}  ({len(media_files)} files)")

            for filename in media_files:
                filepath = os.path.join(current, filename)
                try:
                    if db.query(Video.id).filter(Video.filepath == filepath).first():
                        stats["skipped"] += 1
                        continue

                    size = os.path.getsize(filepath)
                    mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
                    mtype = get_media_type(filename)
                    duration = get_duration_fast(filepath) if mtype in ("video", "audio") else 0.0

                    tname = thumb_name(filepath)
                    got_thumb = make_thumbnail(filepath, str(thumbs_dir / tname), mtype)
                    if got_thumb:
                        stats["thumbs"] += 1

                    db.add(Video(
                        filename=filename, filepath=filepath, file_size=size,
                        duration=duration or 0.0,
                        thumbnail_path=f"/thumbnails/{tname}" if got_thumb else None,
                        folder_id=folder.id, media_type=mtype, status="raw",
                        created_at=mtime, uploaded_at=mtime, shoot_date=mtime,
                        uploaded_by="indexed",
                    ))
                    db.commit()
                    stats["added"] += 1
                    stats["bytes"] += size

                except Exception as e:
                    db.rollback()
                    stats["errors"] += 1
                    print(f"    ERROR {filename}: {e}", flush=True)

            folder.last_scanned = datetime.utcnow()
            db.commit()

        if queue_proxies:
            from job_manager import job_manager
            pending = db.query(Video).filter(
                Video.media_type == "video",
                (Video.proxy_status == None) | (Video.proxy_status == "not_generated"),
            ).all()
            for v in pending:
                job_manager.add_job(v.id, "proxy")
            stats["proxies_queued"] = len(pending)

    finally:
        if owns_db:
            db.close()

    return stats
