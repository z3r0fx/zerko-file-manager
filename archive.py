"""Archiving finished jobs: free the space a done property no longer needs, and bring it back on demand.

A property is finished when it is Posted and Paid and that was a while ago (Manage > Archive sets how
long). What it no longer needs: the photos that did not make the set - brackets merged into HDRs,
rejects, the frames nobody edited - and the video proxies (made again on their own when a clip is
played). What always stays: every edited photo (its edit lives on the original, RAW or not), every
export, every video.

The extra photos are moved, not deleted: to the archive folder (another drive, a NAS), in the same
folders as before under the property's name, checked by size before the original goes. They leave the
library while archived; Bring back moves them home again. Nothing is archived when a set has no edits
yet (there is no telling the keepers from the rest)."""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.orm import Session

from auth import get_current_user
from database import Base, SessionLocal, User, Video, engine, get_db

router = APIRouter(prefix="/api/archive", tags=["archive"])
_resolve: Callable = lambda p: p
_media_root: Optional[Path] = None
JOBS: Dict[str, dict] = {}
_lock = threading.Lock()
DEFAULTS = {"archive_root": "", "archive_months": 3}


class ArchivedFile(Base):
    __tablename__ = "archived_files"
    id = Column(Integer, primary_key=True)
    shoot_id = Column(Integer, nullable=False, index=True)
    video_id = Column(Integer, nullable=False, index=True)
    from_path = Column(String, nullable=False)
    to_path = Column(String, nullable=False)
    size = Column(Integer, default=0)
    at = Column(DateTime, default=datetime.utcnow)


def _admin(user: User):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrators only")


def _settings(db: Session) -> dict:
    import business
    out = dict(DEFAULTS)
    for row in db.query(business.BusinessSetting).filter(business.BusinessSetting.key.in_(list(DEFAULTS))).all():
        try:
            out[row.key] = json.loads(row.value)
        except (TypeError, ValueError):
            pass
    return out


def server_path(p: str) -> str:
    """A folder as typed on Windows (E:\\Archive) becomes the path this server sees (/mnt/e/Archive)."""
    p = (p or "").strip().strip('"')
    m = re.match(r"^([A-Za-z]):[\\/]*(.*)$", p)
    if m and os.name != "nt":
        return f"/mnt/{m.group(1).lower()}/" + m.group(2).replace("\\", "/")
    return p


def _safe_part(s: str) -> str:
    return re.sub(r'[<>:"|?*\\/\x00-\x1f]', "_", s or "Property").strip(" .")[:80] or "Property"


# --------------------------------------------------------------------------
# what a property can give up
# --------------------------------------------------------------------------

EXPORT_DIR = re.compile(r"(^|[\\/])(by room|listing pack|\d*\s*exports?)([\\/]|$)", re.I)


def _plan(db: Session, shoot) -> dict:
    """{extra: [Video], proxies: [(Video, path, size)], extra_bytes, proxy_bytes, kept}"""
    import photo_edit as pe
    import shoots
    paths = [f.path for f in db.query(shoots.ShootFolder).filter(shoots.ShootFolder.shoot_id == shoot.id).all()]
    photos, videos = [], []
    seen = set()
    for p in paths:
        base = p.rstrip("/\\")
        for v in (db.query(Video).filter(Video.is_active.isnot(False), (Video.filepath == p) | Video.filepath.like(base + "/%")
                                         | Video.filepath.like(base + "\\%")).all()):
            if v.id in seen:
                continue
            seen.add(v.id)
            (photos if v.media_type == "photo" else videos if v.media_type == "video" else []).append(v)
    rows = db.query(pe.PhotoEdit.video_id, pe.PhotoEdit.pick).filter(pe.PhotoEdit.video_id.in_([v.id for v in photos])).all() if photos else []
    rejects = {r.video_id for r in rows if r.pick == -1}
    edited = {r.video_id for r in rows} - rejects
    extra = []
    if edited:
        for v in photos:
            if v.id in edited or EXPORT_DIR.search(os.path.dirname(v.filepath or "")):
                continue
            extra.append(v)
    proxies = []
    for v in videos:
        if v.proxy_status == "completed" and v.proxy_path:
            pp = _resolve(v.proxy_path)
            if pp and os.path.isfile(pp):
                proxies.append((v, pp, os.path.getsize(pp)))
    size = lambda v: (v.file_size or 0) or (os.path.getsize(_resolve(v.filepath)) if _resolve(v.filepath) and os.path.isfile(_resolve(v.filepath)) else 0)
    return {"extra": extra, "proxies": proxies, "extra_bytes": sum(size(v) for v in extra),
            "proxy_bytes": sum(x[2] for x in proxies), "kept": len(photos) - len(extra) + len(videos), "edited": len(edited)}


def _finished(shoot, months: int) -> bool:
    if shoot.status != "posted" or not shoot.paid_at:
        return False
    since = max(x for x in [shoot.posted_at, shoot.paid_at, shoot.created_at] if x)
    return since < datetime.utcnow() - timedelta(days=30 * max(0, months))


def _card(db: Session, shoot, plan: Optional[dict] = None) -> dict:
    arch = db.query(ArchivedFile).filter(ArchivedFile.shoot_id == shoot.id).all()
    out = {"id": shoot.id, "address": shoot.address, "suburb": shoot.suburb, "posted_at": shoot.posted_at.isoformat() if shoot.posted_at else None,
           "paid_at": shoot.paid_at.isoformat() if shoot.paid_at else None, "archived": len(arch),
           "archived_bytes": sum(a.size or 0 for a in arch), "archived_at": max((a.at for a in arch), default=None)}
    if out["archived_at"]:
        out["archived_at"] = out["archived_at"].isoformat() + "Z"
    if plan is not None:
        out.update({"extra": len(plan["extra"]), "extra_bytes": plan["extra_bytes"], "proxies": len(plan["proxies"]),
                    "proxy_bytes": plan["proxy_bytes"], "kept": plan["kept"], "edited": plan["edited"]})
    return out


@router.get("")
def overview(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    import shoots
    _admin(current_user)
    s = _settings(db)
    ready, archived = [], []
    arch_ids = {r[0] for r in db.query(ArchivedFile.shoot_id).distinct().all()}
    for sh in db.query(shoots.Shoot).all():
        if sh.id in arch_ids:
            archived.append(_card(db, sh))
        elif _finished(sh, int(s.get("archive_months") or 0)):
            plan = _plan(db, sh)
            c = _card(db, sh, plan)
            if c["extra"] or c["proxies"]:
                ready.append(c)
    ready.sort(key=lambda c: -(c["extra_bytes"] + c["proxy_bytes"]))
    archived.sort(key=lambda c: c["archived_at"] or "", reverse=True)
    root = server_path(s.get("archive_root") or "")
    return {"settings": s, "root_ok": bool(root) and os.path.isdir(root), "ready": ready, "archived": archived,
            "jobs": [j for j in JOBS.values() if j.get("running")]}


class ArchiveSettings(BaseModel):
    archive_root: Optional[str] = Field(None, max_length=500)
    archive_months: Optional[int] = Field(None, ge=0, le=60)


@router.put("/settings")
def put_settings(body: ArchiveSettings, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    import booking
    _admin(current_user)
    vals = body.model_dump(exclude_none=True)
    if vals.get("archive_root"):
        root = Path(server_path(vals["archive_root"]))
        if not root.is_dir():
            raise HTTPException(status_code=400, detail="That folder does not exist (or the drive is not connected).")
        if _media_root and (root.resolve() == _media_root.resolve() or _media_root.resolve() in root.resolve().parents):
            raise HTTPException(status_code=400, detail="The archive folder must be outside the library, or the next scan brings everything back.")
        try:
            probe = root / ".zerko-archive-test"
            probe.write_text("ok")
            probe.unlink()
        except OSError:
            raise HTTPException(status_code=400, detail="Zerko cannot write to that folder.")
    booking._put(db, vals)
    return overview(db, current_user)


# --------------------------------------------------------------------------
# archive and bring back (background jobs, a file at a time, checked)
# --------------------------------------------------------------------------

def _move(src: str, dst: str) -> int:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    size = os.path.getsize(src)
    shutil.copy2(src, dst)
    if os.path.getsize(dst) != size:
        os.unlink(dst)
        raise OSError(f"the copy of {os.path.basename(src)} came out the wrong size")
    os.unlink(src)
    return size


def _job(kind: str, shoot_id: int) -> dict:
    jid = f"{kind}-{shoot_id}-{int(time.time())}"
    j = {"id": jid, "kind": kind, "shoot_id": shoot_id, "running": True, "done": 0, "total": 0, "bytes": 0, "errors": [], "message": ""}
    with _lock:
        JOBS[jid] = j
    return j


def _run_archive(j: dict, shoot_id: int, proxies_too: bool):
    import shoots
    db = SessionLocal()
    try:
        s = _settings(db)
        root = Path(server_path(s.get("archive_root") or ""))
        sh = db.query(shoots.Shoot).filter(shoots.Shoot.id == shoot_id).first()
        plan = _plan(db, sh)
        j["total"] = len(plan["extra"]) + (len(plan["proxies"]) if proxies_too else 0)
        dest_base = root / f"{_safe_part(sh.address)} ({sh.id})"
        mroot = str(_media_root) if _media_root else ""
        for v in plan["extra"]:
            src = _resolve(v.filepath)
            try:
                if not src or not os.path.isfile(src):
                    raise OSError("not on disk")
                rel = os.path.relpath(src, mroot) if mroot and src.startswith(mroot) else os.path.basename(src)
                dst = str(dest_base / rel)
                if os.path.exists(dst):
                    dst = f"{os.path.splitext(dst)[0]}_{v.id}{os.path.splitext(dst)[1]}"
                size = _move(src, dst)
                db.add(ArchivedFile(shoot_id=sh.id, video_id=v.id, from_path=v.filepath, to_path=dst, size=size))
                v.is_active = False
                db.commit()
                j["bytes"] += size
            except Exception as e:
                db.rollback()
                j["errors"].append(f"{v.filename}: {e}")
            j["done"] += 1
        if proxies_too:
            for v, pp, size in plan["proxies"]:
                try:
                    os.unlink(pp)
                    v.proxy_status, v.proxy_path = "not_generated", None
                    db.commit()
                    j["bytes"] += size
                except Exception as e:
                    db.rollback()
                    j["errors"].append(f"{v.filename} proxy: {e}")
                j["done"] += 1
        j["message"] = "Archived"
    except Exception as e:
        j["errors"].append(str(e))
        j["message"] = f"Stopped: {e}"
    finally:
        j["running"] = False
        db.close()


def _run_restore(j: dict, shoot_id: int):
    db = SessionLocal()
    try:
        rows = db.query(ArchivedFile).filter(ArchivedFile.shoot_id == shoot_id).all()
        j["total"] = len(rows)
        for a in rows:
            try:
                home = a.from_path
                if not os.path.isfile(a.to_path):
                    raise OSError("not in the archive folder (is the drive connected?)")
                if os.path.exists(home):
                    raise OSError("a file with that name is back in its place already")
                j["bytes"] += _move(a.to_path, home)
                v = db.query(Video).filter(Video.id == a.video_id).first()
                if v:
                    v.is_active = True
                db.delete(a)
                db.commit()
            except Exception as e:
                db.rollback()
                j["errors"].append(f"{os.path.basename(a.from_path)}: {e}")
            j["done"] += 1
        j["message"] = "Brought back"
    finally:
        j["running"] = False
        db.close()


class ArchiveBody(BaseModel):
    proxies: bool = True


@router.post("/{shoot_id}")
def archive(shoot_id: int, body: ArchiveBody, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    import shoots
    _admin(current_user)
    s = _settings(db)
    root = server_path(s.get("archive_root") or "")
    if not root or not os.path.isdir(root):
        raise HTTPException(status_code=400, detail="Choose an archive folder first (and make sure its drive is connected).")
    if not db.query(shoots.Shoot).filter(shoots.Shoot.id == shoot_id).first():
        raise HTTPException(status_code=404, detail="No such property")
    if any(j["running"] and j["shoot_id"] == shoot_id for j in JOBS.values()):
        raise HTTPException(status_code=409, detail="That property is being archived already.")
    j = _job("archive", shoot_id)
    threading.Thread(target=_run_archive, args=(j, shoot_id, body.proxies), daemon=True).start()
    return j


@router.post("/{shoot_id}/restore")
def restore(shoot_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _admin(current_user)
    if not db.query(ArchivedFile).filter(ArchivedFile.shoot_id == shoot_id).first():
        raise HTTPException(status_code=404, detail="Nothing of that property is archived.")
    j = _job("restore", shoot_id)
    threading.Thread(target=_run_restore, args=(j, shoot_id), daemon=True).start()
    return j


@router.get("/jobs/{job_id}")
def job(job_id: str, current_user: User = Depends(get_current_user)):
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="No such job")
    return j


def install(app, resolve: Callable, media_root):
    global _resolve, _media_root
    _resolve, _media_root = resolve, Path(media_root) if media_root else None
    Base.metadata.create_all(bind=engine, tables=[ArchivedFile.__table__])
    app.include_router(router)
