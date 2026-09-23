"""One-click listing pack: everything a property's listing needs, in one ZIP.

Photos developed with their edits and named after their rooms (Portal size
and a 4:5 Social crop), a vertical reel of the set, and the listing text for
the portals and social posts. The photos also land in the project's exports
folder ("Listing pack"), like Export by room.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import get_current_user, get_user_from_token
from database import SessionLocal, User, Video, get_db

router = APIRouter(tags=["listing-pack"])
JOBS: Dict[str, dict] = {}
_lock = threading.Lock()

EXPORT_DIR = re.compile(r"(^|[\\/])(by room|listing pack|\d*\s*exports)([\\/]|$)", re.I)


class PackBody(BaseModel):
    rooms: bool = True          # sort photos into rooms with AI when they are not sorted yet
    social: bool = True         # a 4:5 crop for Instagram / Facebook
    reel: bool = True           # a vertical slideshow video
    text: bool = True           # listing and social captions
    portal_width: int = Field(2048, ge=800, le=6000)
    portal_kb: int = Field(1000, ge=0, le=20000)


def _camel(name: str, n: int) -> str:
    words = [w for w in re.split(r"[\s_-]+", name.strip()) if w]
    return "".join(w[0].upper() + w[1:] for w in words) + str(n)


def _photos(db: Session, paths: List[str]) -> List[Video]:
    out, seen = [], set()
    for p in paths:
        rows = (db.query(Video).filter(Video.media_type == "photo", Video.is_active.isnot(False),
                                       (Video.filepath == p) | Video.filepath.like(p.rstrip("/\\") + "/%")
                                       | Video.filepath.like(p.rstrip("/\\") + "\\%"))
                .order_by(Video.filename.asc()).all())
        for v in rows:
            parent = os.path.dirname(v.filepath or "")
            if v.id in seen or EXPORT_DIR.search(parent):
                continue
            seen.add(v.id)
            out.append(v)
    # when some are edited, the edited ones are the set (the rest are brackets and rejects)
    try:
        import photo_edit as pe
        edited = {r.video_id for r in db.query(pe.PhotoEdit.video_id).filter(pe.PhotoEdit.video_id.in_([v.id for v in out])).all()}
        if edited:
            out = [v for v in out if v.id in edited]
    except Exception:
        pass
    return out


def _set(j: dict, **kw):
    with _lock:
        j.update(kw)


def _reel(images: List[Path], out: Path, per: float = 2.6, fade: float = 0.5) -> bool:
    """A 1080x1920 slideshow: each photo over a blurred copy of itself, with
    cross-fades. Up to 16 photos (about 35 seconds)."""
    images = images[:16]
    if len(images) < 2 or not shutil.which("ffmpeg"):
        return False
    args = ["ffmpeg", "-y", "-loglevel", "error"]
    for im in images:
        args += ["-loop", "1", "-t", f"{per + fade:.2f}", "-i", str(im)]
    parts = []
    for i in range(len(images)):
        parts.append(
            f"[{i}:v]split[a{i}][b{i}];"
            f"[a{i}]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=30:3,eq=brightness=-0.08[bg{i}];"
            f"[b{i}]scale=1080:1920:force_original_aspect_ratio=decrease[fg{i}];"
            f"[bg{i}][fg{i}]overlay=(W-w)/2:(H-h)/2,fps=30,format=yuv420p,setsar=1[v{i}]")
    chain, last = [], "v0"
    t = per
    for i in range(1, len(images)):
        nxt = f"x{i}"
        chain.append(f"[{last}][v{i}]xfade=transition=fade:duration={fade}:offset={t:.2f}[{nxt}]")
        last = nxt
        t += per
    graph = ";".join(parts + chain)
    args += ["-filter_complex", graph, "-map", f"[{last}]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    try:
        subprocess.run(args, check=True, timeout=900, capture_output=True)
        return out.exists() and out.stat().st_size > 0
    except Exception as e:
        print(f"listing_pack: reel failed: {getattr(e, 'stderr', b'')[-400:]!r}", flush=True)
        return False


def _run(j: dict, shoot_id: int, body: PackBody):
    import photo_edit as pe
    import shoots
    db = SessionLocal()
    try:
        s = db.query(shoots.Shoot).filter(shoots.Shoot.id == shoot_id).first()
        paths = [f.path for f in db.query(shoots.ShootFolder).filter(shoots.ShootFolder.shoot_id == shoot_id).all()]
        photos = _photos(db, paths)
        if not photos:
            raise RuntimeError("This property has no photos yet")
        _set(j, total=len(photos), step="Sorting the photos into rooms")

        # --- names from the rooms --------------------------------------------
        import rooms as rooms_mod
        fids = [f for f in shoots._folder_ids(db, paths) if f]
        key = f"folder:{fids[0]}" if fids else None
        plan = rooms_mod.get_plan(key, db, None) if key else {"rooms": []}
        ids = {v.id for v in photos}
        order: List[tuple] = []           # (video id, room name)
        placed = set()
        for r in plan.get("rooms", []):
            for i in r.get("ids", []):
                if i in ids and i not in placed:
                    order.append((i, r["name"]))
                    placed.add(i)
        rest = [v.id for v in photos if v.id not in placed]
        if rest and body.rooms:
            try:
                import ai
                import ai_photo
                if ai.enabled():
                    sj = ai_photo._job("rooms", len(rest), j.get("user") or "pack")
                    ai_photo._sort_run(sj, ai_photo.SortBody(key=key or f"shoot:{shoot_id}", video_ids=rest, merge=bool(key)))
                    by_room = {vid: name for name, vids in sj["rooms"].items() for vid in vids}
                    for name in ai_photo.ROOM_TYPES:
                        for vid in [v for v in rest if by_room.get(v) == name]:
                            order.append((vid, name))
                            placed.add(vid)
                    rest = [v for v in rest if v not in placed]
            except Exception as e:
                print(f"listing_pack: room sort skipped: {e}", flush=True)
        order += [(vid, "Photo") for vid in rest]
        count: Dict[str, int] = {}
        items = []
        for vid, name in order:
            count[name] = count.get(name, 0) + 1
            items.append(pe.SetItem(id=vid, name=_camel(name, count[name])))

        # --- the photos ------------------------------------------------------
        outputs = [pe.SetOutput(label="Portal", aspect=0, width=body.portal_width, max_kb=body.portal_kb, quality=90, sharpen="screen")]
        if body.social:
            outputs.append(pe.SetOutput(label="Social", aspect=0.8, width=1080, max_kb=0, quality=90, sharpen="screen"))
        ejid = "lp" + uuid.uuid4().hex[:10]
        with pe._export_lock:
            pe.EXPORT_JOBS[ejid] = {"id": ejid, "running": True, "done": 0, "total": len(items), "current": "", "results": [],
                                    "errors": [], "skipped": 0, "started_at": time.time(), "finished_at": None,
                                    "cancelled": False, "user": j.get("user")}
        _set(j, step="Developing the photos")
        t = threading.Thread(target=pe._run_set, args=(ejid, pe.SetExport(items=items, outputs=outputs, frames=[{}, {}][:len(outputs)],
                                                                            into="Listing pack", zip=False)), daemon=True)
        t.start()
        while t.is_alive():
            with pe._export_lock:
                ej = dict(pe.EXPORT_JOBS.get(ejid) or {})
            _set(j, done=ej.get("done", 0), current=ej.get("current", ""))
            time.sleep(0.8)
        with pe._export_lock:
            ej = dict(pe.EXPORT_JOBS.get(ejid) or {})
        root = Path(ej.get("folder") or "")
        if not root.is_dir():
            raise RuntimeError("The photos could not be exported: " + "; ".join(ej.get("errors", [])[:3]))
        _set(j, errors=ej.get("errors", []), folder=str(root))

        # --- the reel --------------------------------------------------------
        if body.reel:
            _set(j, step="Making the reel")
            at = {it.name: k for k, it in enumerate(items)}
            src = sorted((root / ("Social" if body.social else "Portal")).glob("*.jpg"), key=lambda p: at.get(p.stem, 999))
            (root / "Reel").mkdir(exist_ok=True)
            if _reel(src, root / "Reel" / "Reel.mp4"):
                j["reel"] = True

        # --- the words -------------------------------------------------------
        if body.text and s is not None:
            _set(j, step="Writing the listing text")
            import captions
            facts, place = shoots.facts_of(s), captions._place(db, s)
            settings = captions.load_settings()
            (root / "Text").mkdir(exist_ok=True)
            for platform, fname in (("listing", "Listing description.txt"), ("instagram", "Instagram.txt"), ("facebook", "Facebook.txt")):
                o = captions.Opts(platform=platform, tone="warm", clip="photos", address=platform == "listing", emoji=False)
                text = ""
                if settings.get("ai_key") or captions._shared_ai():
                    try:
                        text = captions.write_ai(facts, place, o, [], settings)
                    except Exception as e:
                        print(f"listing_pack: AI text skipped: {e}", flush=True)
                if not text:
                    text, _ = captions.write(facts, place, o, [], settings=settings)
                (root / "Text" / fname).write_text(text.strip() + "\n", encoding="utf-8")

        # --- the ZIP ---------------------------------------------------------
        _set(j, step="Packing the ZIP")
        label = re.sub(r"[^\w\- ]+", "", (s.address if s else "Listing"))[:60].strip() or "Listing"
        zdir = Path(pe._media_root or ".") / ".proxies_export" / j["id"]
        zdir.mkdir(parents=True, exist_ok=True)
        zpath = zdir / f"{label} - listing pack.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
            for f in sorted(root.rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(root).as_posix())
        _set(j, zip=str(zpath), state="done", step="Ready")
    except Exception as e:
        _set(j, state="error", error=str(e))
    finally:
        db.close()


@router.post("/api/shoots/{shoot_id}/listing-pack")
def start_pack(shoot_id: int, body: PackBody, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    import shoots
    if current_user.role == "viewer":
        raise HTTPException(status_code=403, detail="Your account has view-only access.")
    if not db.query(shoots.Shoot).filter(shoots.Shoot.id == shoot_id).first():
        raise HTTPException(status_code=404, detail="No such property")
    jid = uuid.uuid4().hex[:12]
    j = {"id": jid, "state": "running", "step": "Starting", "done": 0, "total": 0, "current": "", "errors": [],
         "error": "", "user": current_user.username, "reel": False}
    with _lock:
        JOBS[jid] = j
    threading.Thread(target=_run, args=(j, shoot_id, body), daemon=True).start()
    return j


@router.get("/api/listing-pack/{job_id}")
def pack_state(job_id: str, current_user: User = Depends(get_current_user)):
    with _lock:
        j = JOBS.get(job_id)
        if not j:
            raise HTTPException(status_code=404, detail="That listing pack is not being made any more")
        return {k: v for k, v in j.items() if k not in ("zip",)} | {"ready": bool(j.get("zip"))}


@router.get("/api/listing-pack/{job_id}/zip")
def pack_zip(request: Request, job_id: str, token: Optional[str] = None, db: Session = Depends(get_db)):
    if token:
        get_user_from_token(token, db)
    else:
        auth = request.headers.get("Authorization") or ""
        if not auth.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
        get_user_from_token(auth[7:], db)
    with _lock:
        j = JOBS.get(job_id)
    if not j or not j.get("zip") or not os.path.exists(j["zip"]):
        raise HTTPException(status_code=404, detail="That listing pack is not ready")
    return FileResponse(j["zip"], media_type="application/zip", filename=os.path.basename(j["zip"]))


def install(app):
    app.include_router(router)
