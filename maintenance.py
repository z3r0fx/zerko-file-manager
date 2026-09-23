"""One-off repairs that run once, in the background, after an update.

Each job has a name; when it finishes, the name is written to
.maintenance-done next to the app, so it never runs twice. A job that fails
is not marked done and gets another go at the next start.
"""

import os
import threading
import time
from pathlib import Path

DONE_FILE = Path(__file__).resolve().parent / ".maintenance-done"


def _done() -> set:
    try:
        return {l.strip() for l in DONE_FILE.read_text().splitlines() if l.strip()}
    except OSError:
        return set()


def _mark(name: str):
    with open(DONE_FILE, "a", encoding="utf-8") as f:
        f.write(name + "\n")


def trash_scratch_files(resolve, move_to_trash) -> dict:
    """Scratch files the app used to leave in shoot folders - the editor's
    `.edittmp-`, the HDR merge's `.hdrtmp-`, half-written `.part-` previews -
    were catalogued as photos. Their rows go to the trash like anything else
    deleted from the library (restorable, emptied by an admin), and the file
    goes with them if it is still there."""
    from database import SessionLocal, Video
    from media_type_utils import is_junk_name
    db = SessionLocal()
    moved = rows = 0
    try:
        for v in db.query(Video).filter(Video.is_active.isnot(False)).all():
            if not is_junk_name(v.filename or ""):
                continue
            real = resolve(v.filepath) if v.filepath else None
            try:
                if real and os.path.exists(real):
                    v.original_path = v.original_path or v.filepath
                    v.filepath = move_to_trash(real)
                    moved += 1
            except Exception as e:
                print(f"  [maintenance] could not move {v.filename} to the trash: {e}", flush=True)
            v.is_active = False
            from datetime import datetime
            v.trashed_at = datetime.utcnow()
            rows += 1
        db.commit()
    finally:
        db.close()
    return {"rows": rows, "files": moved}


def redraw_photo_thumbnails(resolve, thumbs_dir: Path) -> dict:
    """Draw every photo's thumbnail again. RAW files were drawn from raw
    sensor data (green, with a stripe of unused sensor columns on the right),
    and a thumbnail is only ever drawn once, so the bad ones never healed."""
    import previews
    from database import SessionLocal, Video
    from indexer import thumb_name
    db = SessionLocal()
    done = failed = 0
    try:
        ids = [v.id for v in db.query(Video.id).filter(Video.media_type == "photo",
                                                        Video.is_active.isnot(False)).all()]
    finally:
        db.close()
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    for i, vid in enumerate(ids):
        db = SessionLocal()
        try:
            v = db.query(Video).filter(Video.id == vid).first()
            real = resolve(v.filepath) if v and v.filepath else None
            if not real or not os.path.exists(real):
                continue
            name = thumb_name(real)
            ok, _ = previews.make_image_jpeg(real, str(thumbs_dir / name), 640)
            if ok:
                v.thumbnail_path = f"/thumbnails/{name}"
                db.commit()
                done += 1
            else:
                failed += 1
        except Exception:
            failed += 1
        finally:
            db.close()
        if i % 50 == 49:
            time.sleep(0.2)          # leave room for people using the app
    return {"redrawn": done, "failed": failed}


def start(resolve, move_to_trash, media_root: Path):
    jobs = [
        ("2026-09-trash-scratch-files", lambda: trash_scratch_files(resolve, move_to_trash)),
        ("2026-09-redraw-photo-thumbnails", lambda: redraw_photo_thumbnails(resolve, Path(media_root) / "thumbnails")),
    ]
    todo = [(n, fn) for n, fn in jobs if n not in _done()]
    if not todo:
        return

    def run():
        time.sleep(20)               # let the server finish starting first
        for name, fn in todo:
            try:
                print(f"  [maintenance] {name}: starting", flush=True)
                result = fn()
                _mark(name)
                print(f"  [maintenance] {name}: {result}", flush=True)
            except Exception as e:
                print(f"  [maintenance] {name} failed, will retry next start: {e}", flush=True)

    threading.Thread(target=run, daemon=True, name="maintenance").start()
