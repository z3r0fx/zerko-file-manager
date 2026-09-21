"""First-run setup.

Mounted ahead of the rest of the app so it can answer before anything that
needs an account. Three things happen here that cannot happen anywhere else:

  1. Creating the very first user. Every other account-creating route requires
     an admin to already exist, so a fresh install would be unreachable.
  2. Choosing where the media lives. A browser cannot open a file dialog for a
     folder on the *server*, so the server has to offer the folder tree itself.
  3. Writing that choice somewhere the launcher will read next time.

Everything here refuses to run once setup is finished - see `_guard`. That
matters because /api/setup/browse can list any directory on the machine, which
is fine for the person installing it and nobody else.
"""

import json
import os
import secrets
import shutil
import string
import threading
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Request
from sqlalchemy.orm import Session

from database import SessionLocal, User
from auth import get_password_hash

router = APIRouter(prefix="/api/setup", tags=["setup"])

CONFIG_PATH = Path(os.environ.get("ZERKO_CONFIG", "zerko.config.json"))

# Directories nobody is looking for their footage in, and which make the
# browser noisy. Not a security boundary - `_guard` is.
SKIP_DIRS = {
    "$recycle.bin", "system volume information", "windows", "program files",
    "program files (x86)", "programdata", "appdata", "$windows.~ws",
    "recovery", "perflogs", "node_modules", "__pycache__", ".git",
}

MEDIA_EXT = {
    ".mp4", ".mov", ".avi", ".mkv", ".mxf", ".r3d", ".braw", ".mts", ".m2ts",
    ".insv", ".m4v", ".wmv", ".webm",
    ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".dng", ".arw", ".cr2", ".cr3",
    ".nef", ".raf", ".heic", ".heif",
    ".mp3", ".wav", ".aac", ".flac", ".m4a", ".aiff",
}


def setup_needed() -> bool:
    """True until somebody has an account. That is the whole test: a database
    with no users cannot be logged into, so it must be a fresh install."""
    db = SessionLocal()
    try:
        return db.query(User).count() == 0
    except Exception:
        # A database that cannot be read yet is a fresh install too.
        return True
    finally:
        db.close()


# Until the first account exists, whoever reaches this page first becomes the
# admin - and can browse the server's folders on the way. On the machine itself
# that is fine. From anywhere else on the network it is not, so a one-off code
# is printed in the Zerko window (which only the person at the machine can see)
# and required from any other address. Regenerated on every start.
_SETUP_CODE = secrets.token_urlsafe(6)
_code_failures = 0
_MAX_CODE_FAILURES = 20


def setup_code() -> str:
    return _SETUP_CODE


def _is_local(request: Request) -> bool:
    """A direct connection from this machine, not relayed by a proxy."""
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1"):
        return False
    return not any(h in request.headers
                   for h in ("x-forwarded-for", "forwarded", "x-real-ip"))


def _guard(request: Request):
    global _code_failures
    if not setup_needed():
        raise HTTPException(
            status_code=403,
            detail="Setup has already been completed on this installation.",
        )
    if _is_local(request):
        return
    if _code_failures >= _MAX_CODE_FAILURES:
        raise HTTPException(
            status_code=429,
            detail="Too many wrong setup codes. Restart Zerko to try again.")
    supplied = request.headers.get("x-setup-code", "")
    if not secrets.compare_digest(supplied.encode(), _SETUP_CODE.encode()):
        if supplied:
            _code_failures += 1
        raise HTTPException(
            status_code=401,
            detail="Enter the setup code shown in the Zerko window.")


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


@router.get("/status")
def status(request: Request):
    """Polled by the frontend before it decides to show login or the wizard."""
    cfg = load_config()
    needs = setup_needed()
    return {
        "needs_setup": needs,
        # Tells the wizard whether to ask for the code shown in the console.
        "code_required": bool(needs and not _is_local(request)),
        "media_root": cfg.get("media_root"),
        "platform": "windows" if os.name == "nt" else "linux",
        "app_name": cfg.get("app_name", "Zerko File Manager"),
    }


@router.get("/drives")
def drives(request: Request):
    """Every drive with its free space, so the choice is informed."""
    _guard(request)
    out = []
    if os.name == "nt":
        candidates = [f"{d}:\\" for d in string.ascii_uppercase]
    else:
        candidates = ["/"] + [
            os.path.join(base, d)
            for base in ("/mnt", "/media", "/Volumes")
            if os.path.isdir(base)
            for d in os.listdir(base)
        ]
    for path in candidates:
        if not os.path.isdir(path):
            continue
        try:
            usage = shutil.disk_usage(path)
        except (OSError, PermissionError):
            continue
        out.append({
            "path": path,
            "label": path,
            "free": usage.free,
            "total": usage.total,
            "free_formatted": _human(usage.free),
            "total_formatted": _human(usage.total),
        })
    return {"drives": out}


@router.get("/browse")
def browse(request: Request, path: str = ""):
    """Subfolders of `path`, plus a count of media sitting directly inside.

    The count is what tells someone they have picked the right folder before
    committing to indexing it, so it is worth the extra listdir.
    """
    _guard(request)
    if not path:
        return drives(request)

    target = Path(path)
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="That folder does not exist")

    folders = []
    direct_media = 0
    try:
        with os.scandir(target) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.lower() in SKIP_DIRS or entry.name.startswith("."):
                            continue
                        folders.append({"name": entry.name, "path": entry.path})
                    elif entry.is_file(follow_symlinks=False):
                        if os.path.splitext(entry.name)[1].lower() in MEDIA_EXT:
                            direct_media += 1
                except (OSError, PermissionError):
                    continue
    except PermissionError:
        raise HTTPException(status_code=403, detail="No permission to read that folder")

    folders.sort(key=lambda f: f["name"].lower())
    parent = str(target.parent) if target.parent != target else None
    return {
        "path": str(target),
        "parent": parent,
        "folders": folders,
        "direct_media": direct_media,
    }


@router.get("/scan-preview")
def scan_preview(request: Request, path: str, max_files: int = 40000):
    """Walk the tree and report what indexing would actually find.

    Capped, because pointing this at C:\\ should not hang the wizard.
    """
    _guard(request)
    root = Path(path)
    if not root.is_dir():
        raise HTTPException(status_code=404, detail="That folder does not exist")

    count = 0
    total_bytes = 0
    kinds = {"video": 0, "photo": 0, "audio": 0}
    truncated = False
    video_ext = {".mp4", ".mov", ".avi", ".mkv", ".mxf", ".r3d", ".braw",
                 ".mts", ".m2ts", ".insv", ".m4v", ".wmv", ".webm"}
    audio_ext = {".mp3", ".wav", ".aac", ".flac", ".m4a", ".aiff"}

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            if ext not in MEDIA_EXT:
                continue
            count += 1
            if ext in video_ext:
                kinds["video"] += 1
            elif ext in audio_ext:
                kinds["audio"] += 1
            else:
                kinds["photo"] += 1
            try:
                total_bytes += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
            if count >= max_files:
                truncated = True
                break
        if truncated:
            break

    return {
        "path": str(root),
        "files": count,
        "truncated": truncated,
        "kinds": kinds,
        "total_bytes": total_bytes,
        "total_formatted": _human(total_bytes),
    }


@router.post("/complete")
def complete(request: Request, payload: dict = Body(...)):
    """Create the first account, save the config, kick off the first index."""
    _guard(request)

    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    media_root = (payload.get("media_root") or "").strip()

    if not username or len(username) < 2:
        raise HTTPException(status_code=400, detail="Pick a username")
    if len(password) < 10:
        raise HTTPException(status_code=400, detail="Use at least 10 characters")
    if password.lower() in ("admin123", "password", "123456789", "changeme", "zerko12345"):
        raise HTTPException(status_code=400, detail="That password is far too common")
    if not media_root or not os.path.isdir(media_root):
        raise HTTPException(status_code=400, detail="Choose a folder that exists")

    db: Session = SessionLocal()
    try:
        # Re-check inside the transaction: two browser tabs racing through the
        # wizard must not both create an admin.
        if db.query(User).count() > 0:
            raise HTTPException(status_code=403, detail="Setup already completed")
        user = User(
            username=username,
            email=(payload.get("email") or f"{username}@local").strip(),
            hashed_password=get_password_hash(password),
            role="admin",
            created_at=datetime.utcnow(),
        )
        db.add(user)
        db.commit()
    finally:
        db.close()

    print(f"  [setup] admin account '{username}' created", flush=True)
    cfg = load_config()
    cfg.update({
        "media_root": media_root,
        "generate_proxies": bool(payload.get("generate_proxies", True)),
        "transcribe": bool(payload.get("transcribe", False)),
        "setup_completed_at": datetime.utcnow().isoformat(),
    })
    save_config(cfg)

    # MEDIA_ROOT is read at import time by the app, so this run already has the
    # right value only if the launcher supplied it. Set it anyway so the first
    # index works before the restart.
    # Compare BEFORE assigning: the app read MEDIA_ROOT once, at import, so a
    # different value here means the running process is still using the old one.
    restart_required = (os.environ.get("MEDIA_ROOT") or "") != media_root
    os.environ["MEDIA_ROOT"] = media_root

    started = {"indexing": False}
    if payload.get("index_now", True):
        def run_index():
            try:
                import indexer
                # index_tree calls progress(message, stats) - TWO arguments.
                # A one-argument lambda here threw immediately and took the
                # whole first index down with it, leaving a finished setup and
                # an empty library.
                def _say(msg, stats=None):
                    print(f"  [setup] {msg}", flush=True)

                indexer.index_tree(
                    root=media_root,
                    queue_proxies=bool(payload.get("generate_proxies", True)),
                    progress=_say,
                )
            except Exception as e:
                print(f"  [setup] initial index failed: {e}")
                return

            # Tag straight after indexing.
            #
            # Indexing alone produces NO tags - it only catalogues files. The
            # tagging pass is a separate script, which means a new install
            # would finish setup with thousands of files and an empty Tags
            # list, and no obvious reason why. Running it here is the
            # difference between the feature existing and the feature working.
            try:
                import enrich
                print("  [setup] tagging from folders, filenames and camera...")
                enrich.main_programmatic()
                print("  [setup] tagging done")
            except Exception as e:
                print(f"  [setup] tagging failed (the library still works): {e}")
        threading.Thread(target=run_index, daemon=True, name="setup-index").start()
        started["indexing"] = True

    return {
        "status": "ok",
        "username": username,
        "media_root": media_root,
        "restart_required": restart_required,
        **started,
    }


def _human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
