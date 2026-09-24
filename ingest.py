"""Import from the camera card (replaces Photo Mechanic's ingest and Lightroom's import).

Zerko runs in WSL, which does not see a card put in after it started, so the card
is read by Windows itself: PowerShell finds the cards (removable drives, or any
drive with a DCIM folder), lists the photos and videos on them, copies the ones
you tick into the property's folder, reads every copy back to check it matches
the card byte for byte (SHA-256), and makes a second checked copy on a backup
drive when one is set. The copies are then added to the library, and the
property's pipeline can start straight away. Nothing on the card is changed.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import get_current_user
from database import IndexedFolder, SessionLocal, User, Video, get_db

router = APIRouter(prefix="/api/ingest", tags=["ingest"])
_resolve = None
_media_root: Optional[Path] = None


def _ps(script: str, timeout: float = 120) -> str:
    """Run a PowerShell script on the Windows side and hand back what it printed."""
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                       capture_output=True, text=True, timeout=timeout)
    return r.stdout or ""


def _q(s: str) -> str:
    """A string for a PowerShell single-quoted literal."""
    return "'" + s.replace("'", "''") + "'"


def _win(p: Path) -> str:
    return subprocess.run(["wslpath", "-w", str(p)], capture_output=True, text=True, timeout=10).stdout.strip()


def _json(out: str):
    out = out.strip()
    if not out:
        return []
    d = json.loads(out)
    return d if isinstance(d, list) else [d]


WIN_PATH = re.compile(r"^[A-Za-z]:\\[^<>\"|?*]*$")


def _exts() -> set:
    import indexer
    return {e.lower() for e in indexer.MEDIA_EXTENSIONS}


# --------------------------------------------------------------------------
# the cards
# --------------------------------------------------------------------------

@router.get("/cards")
def cards(current_user: User = Depends(get_current_user)):
    """Removable drives and any drive with a DCIM folder (a camera card, a phone, a drone)."""
    script = r"""
$ErrorActionPreference = 'SilentlyContinue'
Get-CimInstance Win32_LogicalDisk | Where-Object { $_.DriveType -eq 2 -or (Test-Path ($_.DeviceID + '\DCIM')) } |
  ForEach-Object { [pscustomobject]@{ drive = $_.DeviceID; name = $_.VolumeName; size = $_.Size; free = $_.FreeSpace;
                                       dcim = (Test-Path ($_.DeviceID + '\DCIM')) } } | ConvertTo-Json -Compress
"""
    try:
        found = _json(_ps(script, 60))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not ask Windows for the cards: {e}")
    return {"cards": [{"path": (c["drive"] + "\\DCIM") if c.get("dcim") else c["drive"] + "\\", "drive": c["drive"],
                       "name": c.get("name") or "", "size": c.get("size") or 0, "free": c.get("free") or 0}
                      for c in found if c.get("drive") and c["drive"].upper() != "C:"]}


class ScanBody(BaseModel):
    path: str = Field(..., max_length=400)


def _check_source(path: str, user: User) -> str:
    p = path.strip().rstrip("\\") + "\\"
    if not WIN_PATH.match(p):
        raise HTTPException(status_code=400, detail="That is not a Windows folder (like E:\\DCIM)")
    # any folder on the computer can hold photos, but only an administrator may read an arbitrary one
    if not re.match(r"^[A-Za-z]:\\DCIM\\", p, re.I) and user.role != "admin":
        raise HTTPException(status_code=403, detail="Only an administrator can import from a folder that is not a card")
    return p


@router.post("/scan")
def scan(body: ScanBody, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """What is on the card: photos and videos, newest day first, and which are already in the library."""
    src = _check_source(body.path, current_user)
    script = f"""
$ErrorActionPreference = 'SilentlyContinue'
Get-ChildItem -LiteralPath {_q(src)} -Recurse -File |
  ForEach-Object {{ [pscustomobject]@{{ p = $_.FullName; n = $_.Name; s = $_.Length; t = $_.LastWriteTime.ToString('s') }} }} |
  ConvertTo-Json -Compress
"""
    try:
        files = _json(_ps(script, 300))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not read the card: {e}")
    exts = _exts()
    files = [f for f in files if os.path.splitext(f.get("n") or "")[1].lower() in exts]
    if len(files) > 20000:
        raise HTTPException(status_code=400, detail="More than 20 000 files: pick a smaller folder")
    # already imported: the same name and size somewhere in the library
    names = {f["n"] for f in files}
    have = set()
    for chunk in [list(names)[i:i + 800] for i in range(0, len(names), 800)]:
        have |= {(v.filename, v.file_size) for v in db.query(Video.filename, Video.file_size).filter(Video.filename.in_(chunk)).all()}
    out = [{"path": f["p"], "name": f["n"], "size": int(f.get("s") or 0), "taken": f.get("t"),
            "imported": (f["n"], int(f.get("s") or 0)) in have} for f in files]
    out.sort(key=lambda f: (f["taken"] or "", f["name"]))
    return {"path": src, "files": out}


# --------------------------------------------------------------------------
# importing
# --------------------------------------------------------------------------

JOBS: Dict[str, dict] = {}
_lock = threading.Lock()


class ImportBody(BaseModel):
    source: str = Field(..., max_length=400)
    files: List[str] = Field(..., min_length=1, max_length=20000)
    shoot_id: Optional[int] = None
    folder_id: Optional[int] = None
    subfolder: str = Field("", max_length=80)
    rename: bool = False                       # "{address} 001.NEF" instead of the camera's names
    backup: str = Field("", max_length=400)    # a second copy, e.g. F:\\Backup (a folder per property inside)
    run_pipeline: bool = False


def _dest_dir(db: Session, body: ImportBody) -> tuple:
    """(folder on disk, property or None) where the copies go."""
    import shoots
    shoot = db.query(shoots.Shoot).filter(shoots.Shoot.id == body.shoot_id).first() if body.shoot_id else None
    if body.shoot_id and not shoot:
        raise HTTPException(status_code=404, detail="No such property")
    base = None
    if body.folder_id:
        f = db.query(IndexedFolder).filter(IndexedFolder.id == body.folder_id).first()
        base = Path(_resolve(f.path)) if f and _resolve else (Path(f.path) if f else None)
    elif shoot:
        paths = [x.path for x in db.query(shoots.ShootFolder).filter(shoots.ShootFolder.shoot_id == shoot.id)
                 .order_by(shoots.ShootFolder.path).all()]
        if paths:
            base = Path(_resolve(paths[0]) if _resolve else paths[0])
        else:
            # a property without a folder yet gets one, named after it, and it is linked
            name = re.sub(r'[\\/:*?"<>|]+', " ", shoot.address).strip()[:80] or f"Property {shoot.id}"
            base = (_media_root or Path(".")) / name
            base.mkdir(parents=True, exist_ok=True)
            shoots._attach(db, shoot, str(base))
            db.commit()
    if not base:
        raise HTTPException(status_code=400, detail="Choose the property or folder the photos go to")
    sub = re.sub(r'[\\/:*?"<>|]+', " ", body.subfolder or "").strip().strip(".")
    d = base / sub if sub else base
    d.mkdir(parents=True, exist_ok=True)
    return d, shoot


def _folder_row(db: Session, d: Path) -> IndexedFolder:
    """The library's folder for a directory, made (with its parents) when new."""
    root = _media_root or d
    f = db.query(IndexedFolder).filter(IndexedFolder.path == str(d)).first()
    if f:
        return f
    parent = _folder_row(db, d.parent) if d.parent != root and str(d.parent).startswith(str(root)) else None
    rel = os.path.relpath(d, root).replace(os.sep, "/")
    f = IndexedFolder(name=rel, path=str(d), relative_path=rel, parent_id=parent.id if parent else None,
                      added_at=datetime.utcnow(), created_in_app=datetime.utcnow())
    db.add(f)
    db.commit()
    db.refresh(f)
    return f


def _add_to_library(db: Session, folder: IndexedFolder, path: Path, user: str) -> Optional[int]:
    import indexer
    if db.query(Video.id).filter(Video.filepath == str(path)).first():
        return None
    mtype = indexer.get_media_type(path.name)
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    tdir = (_media_root or path.parent) / "thumbnails"
    tdir.mkdir(parents=True, exist_ok=True)
    tname = indexer.thumb_name(str(path))
    got = indexer.make_thumbnail(str(path), str(tdir / tname), mtype)
    v = Video(filename=path.name, filepath=str(path), file_size=path.stat().st_size,
              duration=(indexer.get_duration_fast(str(path)) or 0.0) if mtype in ("video", "audio") else 0.0,
              thumbnail_path=f"/thumbnails/{tname}" if got else None, folder_id=folder.id, media_type=mtype,
              status="raw", created_at=mtime, uploaded_at=datetime.utcnow(), shoot_date=mtime, uploaded_by=user)
    db.add(v)
    db.commit()
    return v.id


COPY_PS = r"""
$ErrorActionPreference = 'Stop'
$jobs = Get-Content -LiteralPath $args[0] -Raw | ConvertFrom-Json
foreach ($j in $jobs) {
  try {
    $srcHash = (Get-FileHash -LiteralPath $j.src -Algorithm SHA256).Hash
    foreach ($to in @($j.dst, $j.bak)) {
      if (-not $to) { continue }
      $dir = Split-Path -Parent $to
      if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
      Copy-Item -LiteralPath $j.src -Destination $to -Force
      (Get-Item -LiteralPath $to).LastWriteTime = (Get-Item -LiteralPath $j.src).LastWriteTime
      $h = (Get-FileHash -LiteralPath $to -Algorithm SHA256).Hash
      if ($h -ne $srcHash) { throw "the copy at $to does not match the card" }
    }
    Write-Output ('OK ' + $j.i)
  } catch {
    Write-Output ('ERR ' + $j.i + ' ' + $_.Exception.Message)
  }
  [Console]::Out.Flush()
}
"""


def _win_temp() -> Path:
    w = subprocess.run(["cmd.exe", "/c", "echo %TEMP%"], capture_output=True, text=True, timeout=15).stdout.strip()
    return Path(subprocess.run(["wslpath", "-u", w], capture_output=True, text=True, timeout=10).stdout.strip())


def _run(j: dict, body: ImportBody, dest: Path, shoot_id: Optional[int], label: str):
    db = SessionLocal()
    try:
        # where each file goes (names made unique; the card's own order)
        plan, used = [], set()
        for n, src in enumerate(body.files):
            name = src.replace("/", "\\").split("\\")[-1]
            stem, ext = os.path.splitext(name)
            if body.rename:
                stem = f"{label} {n + 1:03d}"
            cand, k = f"{stem}{ext}", 2
            while cand.lower() in used or (dest / cand).exists():
                cand, k = f"{stem}_{k}{ext}", k + 1
            used.add(cand.lower())
            bak = ""
            if body.backup:
                bak = body.backup.rstrip("\\") + "\\" + re.sub(r'[\\/:*?"<>|]+', " ", label).strip() + "\\" + \
                      ((re.sub(r'[\\/:*?"<>|]+', " ", body.subfolder).strip() + "\\") if body.subfolder.strip() else "") + cand
            plan.append({"i": n, "src": src, "dst": _win(dest) + "\\" + cand, "bak": bak, "local": str(dest / cand)})
        tmp = _win_temp() / f"zerko-import-{j['id']}.json"
        tmp.write_text(json.dumps([{k: p[k] for k in ("i", "src", "dst", "bak")} for p in plan]), encoding="utf-8")
        ps = tmp.with_suffix(".ps1")
        ps.write_text(COPY_PS, encoding="utf-8")
        j["step"] = "Copying and checking"
        p = subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                              "-File", _win(ps), _win(tmp)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        j["_proc"] = p
        folder = _folder_row(db, dest)
        for line in p.stdout:
            line = line.strip()
            if line.startswith("OK "):
                i = int(line.split()[1])
                j["copied"] += 1
                try:
                    vid = _add_to_library(db, folder, Path(plan[i]["local"]), j["user"])
                    if vid:
                        j["video_ids"].append(vid)
                except Exception as e:
                    db.rollback()
                    j["errors"].append(f"{plan[i]['src']}: copied, but not added to the library ({e})"[:300])
            elif line.startswith("ERR "):
                parts = line.split(" ", 2)
                i = int(parts[1])
                j["errors"].append(f"{plan[i]['src']}: {parts[2] if len(parts) > 2 else 'failed'}"[:300])
            j["done"] = j["copied"] + len(j["errors"])
            if j.get("cancel"):
                p.kill()
                break
        p.wait()
        for f in (tmp, ps):
            try:
                f.unlink()
            except OSError:
                pass
        j["folder_id"] = folder.id
        if body.run_pipeline and shoot_id and j["video_ids"] and not j.get("cancel"):
            j["step"] = "Starting the pipeline"
            import pipeline
            user = db.query(User).filter(User.username == j["user"]).first()
            try:
                r = pipeline.start_run(pipeline.RunBody(shoot_id=shoot_id), db, user)
                j["pipeline_run"] = r["id"]
            except HTTPException as e:
                j["errors"].append(f"The pipeline did not start: {e.detail}")
        j["state"] = "cancelled" if j.get("cancel") else "done"
        j["step"] = "Done"
    except Exception as e:
        j["state"], j["error"] = "error", str(e)[:400]
    finally:
        j.pop("_proc", None)
        db.close()


@router.post("/start")
def start(body: ImportBody, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if current_user.role == "viewer":
        raise HTTPException(status_code=403, detail="Your account has view-only access.")
    src = _check_source(body.source, current_user)
    for f in body.files:
        if not f.lower().startswith(src.lower()) or ".." in f:
            raise HTTPException(status_code=400, detail="A file is not on that card")
    if body.backup and not WIN_PATH.match(body.backup.rstrip("\\") + "\\"):
        raise HTTPException(status_code=400, detail="The backup should be a Windows folder, like F:\\Backup")
    dest, shoot = _dest_dir(db, body)
    label = (shoot.address if shoot else dest.name) or "Import"
    j = {"id": uuid.uuid4().hex[:12], "state": "running", "step": "Starting", "done": 0, "copied": 0,
         "total": len(body.files), "errors": [], "error": "", "video_ids": [], "user": current_user.username,
         "dest": str(dest), "shoot_id": shoot.id if shoot else None}
    with _lock:
        JOBS[j["id"]] = j
    threading.Thread(target=_run, args=(j, body, dest, shoot.id if shoot else None, label), daemon=True).start()
    return {k: v for k, v in j.items() if not k.startswith("_")}


@router.get("/jobs/{job_id}")
def job(job_id: str, current_user: User = Depends(get_current_user)):
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="No such import")
    return {k: v for k, v in j.items() if not k.startswith("_")}


@router.post("/jobs/{job_id}/cancel")
def cancel(job_id: str, current_user: User = Depends(get_current_user)):
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="No such import")
    j["cancel"] = True
    return {"ok": True}


def install(app, media_root, resolve_media_path):
    global _media_root, _resolve
    _media_root = Path(media_root) if media_root else None
    _resolve = resolve_media_path
    app.include_router(router)
