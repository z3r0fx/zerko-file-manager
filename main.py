from fastapi import FastAPI, Depends, HTTPException, Request, File, UploadFile, Form, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import func, select, or_, case, text
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool
import os
import mimetypes
import re
import subprocess
import threading
import time
import uuid
import hashlib
import shutil
import json
from filelock import FileLock
from datetime import timedelta, datetime, date
from pathlib import Path
from typing import Optional, List
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse, Response
from PIL import Image

import permissions
from database import init_db, get_db, SessionLocal, User, IndexedFolder, Video, Tag, Note, video_tags, TranscriptionSegment, Share, ShareSelect
from auth import (
    get_password_hash, verify_password, create_access_token,
    get_current_user, get_admin_user, get_user_from_token,
    ACCESS_TOKEN_EXPIRE_MINUTES, revoke_sessions
)
from video_processor import scan_folder, format_file_size, format_duration, generate_thumbnail, get_video_duration, get_duration_fast, get_technical_metadata, VIDEO_EXTENSIONS, MEDIA_EXTENSIONS
import previews
import files_api
import photo_proxy
import activity
import library_check
import hdr
import photo_edit
import projects
import shoots
import access
import delivery
import subclips
import video_edit
import ai
import ai_photo
import ai_image
from media_type_utils import get_media_type
from job_manager import job_manager

# Compute paths at module level for Docker compatibility
PROJECT_ROOT = Path(__file__).parent.resolve()

# Media root is configurable so the same code runs under WSL and natively on
# Windows. Set MEDIA_ROOT to override (e.g. MEDIA_ROOT=E:\\Shared on Windows).
# Set by the setup wizard on first run; no sensible default exists
# before someone tells us where their footage lives.
_DEFAULT_MEDIA_ROOT = os.environ.get("MEDIA_ROOT") or str(Path.home() / "Videos")
UPLOAD_ROOT = Path(os.environ.get("MEDIA_ROOT", _DEFAULT_MEDIA_ROOT))
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"

# Read/write buffer for file copies. The default shutil buffer is 64KB, which
# means ~16,000 syscalls per GB. 4MB cuts that by 64x.
COPY_BUFFER = 4 * 1024 * 1024

def _build_root_markers():
    """Path prefixes that mean "this was the media root at the time".

    Rows hold absolute paths from whenever they were indexed. When the library
    moves - a new drive letter, E:\\Shared to D:\\Media, WSL to native Windows -
    those stored paths stop resolving, and every clip 404s until someone
    reindexes.

    This list used to be hardcoded to the original E:\\Shared, so it silently
    stopped covering the current root after the move to D:\\Media. Deriving it
    from MEDIA_ROOT means it keeps working through the next move too.
    """
    markers = []
    cur = str(UPLOAD_ROOT).replace("\\", "/").rstrip("/")
    if cur:
        markers.append(cur.lower() + "/")
        # The same folder seen from the other side of the WSL boundary:
        # e.g. /mnt/d/Media <-> D:/Media
        m = re.match(r"^/mnt/([a-z])/(.*)$", cur, re.I)
        if m:
            markers.append(f"{m.group(1).lower()}:/{m.group(2).lower()}/")
        m = re.match(r"^([a-z]):/(.*)$", cur, re.I)
        if m:
            markers.append(f"/mnt/{m.group(1).lower()}/{m.group(2).lower()}/")
    # Roots this library has lived under before.
    # Roots this installation has previously used, if it has moved.
    for prev in (os.environ.get("MEDIA_ROOT_PREVIOUS") or "").split(","):
        prev = prev.strip().replace("\\", "/").rstrip("/").lower()
        if prev:
            markers.append(prev + "/")
    seen, out = set(), []
    for x in markers:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return tuple(out)


_MEDIA_ROOT_MARKERS = _build_root_markers()


def resolve_thumbnail_path(stored):
    """Turn a stored thumbnail reference into a real file on disk.

    thumbnail_path holds a URL path like "/thumbnails/abc.jpg", not a
    filesystem path. The signed-in app never noticed because a StaticFiles
    mount serves that URL directly - but any code that needs the actual FILE
    has to map it, and resolve_media_path() cannot: it looks for media-root
    markers, finds none, and falls back to UPLOAD_ROOT/<basename>, which drops
    the "thumbnails" folder and 404s every time.
    """
    if not stored:
        return None
    name = os.path.basename(str(stored).replace("\\", "/"))
    if not name:
        return None
    candidate = UPLOAD_ROOT / "thumbnails" / name
    if candidate.exists():
        return str(candidate)
    # Absolute paths are stored by some older rows; honour them if real.
    if os.path.isabs(str(stored)) and os.path.exists(str(stored)):
        return str(stored)
    return None


def unique_destination(directory: Path, filename: str) -> Path:
    """A path that does not collide with an existing file.

    Cameras reuse filenames constantly (DJI_0001.MOV, Clip00745945.mov), and
    the old code opened the destination with "wb" - silently destroying the
    existing file, then failing on the unique filepath constraint anyway.
    """
    safe = os.path.basename(filename or "upload")
    stem, ext = os.path.splitext(safe)
    candidate = directory / safe
    n = 2
    while candidate.exists():
        candidate = directory / f"{stem} ({n}){ext}"
        n += 1
    return candidate


def safe_relative_dir(relative_path: str):
    """The folder part of a browser's webkitRelativePath, made safe.

    "MyShoot/Drone/DJI_0001.MOV" -> Path("MyShoot/Drone")
    Anything trying to climb out (.., absolute paths, drive letters) is dropped.
    """
    if not relative_path:
        return None
    cleaned = str(relative_path).replace("\\", "/")
    parts = []
    for seg in cleaned.split("/")[:-1]:          # drop the filename itself
        seg = seg.strip()
        if not seg or seg in (".", "..") or ":" in seg:
            continue
        parts.append(re.sub(r'[<>:"|?*]', "_", seg))
    return Path(*parts) if parts else None


def ensure_folder_row(db, directory: Path):
    """Get (or create) the IndexedFolder for a directory, parents included."""
    root = UPLOAD_ROOT.resolve()
    directory = Path(directory).resolve()
    if directory == root:
        return None
    try:
        rel_parts = directory.relative_to(root).parts
    except Exception:
        return None

    parent_id, row = None, None
    for i in range(1, len(rel_parts) + 1):
        rel = "/".join(rel_parts[:i])
        path = str(root / Path(*rel_parts[:i]))
        row = db.query(IndexedFolder).filter(IndexedFolder.path == path).first()
        if not row:
            row = IndexedFolder(name=rel, path=path, relative_path=rel,
                                parent_id=parent_id, added_at=datetime.utcnow())
            db.add(row)
            db.commit()
            db.refresh(row)
        parent_id = row.id
    return row


def upload_destination_dir(db, folder_id):
    """Where an upload should land: the folder being browsed, if it's real."""
    if folder_id:
        folder = db.query(IndexedFolder).filter(IndexedFolder.id == folder_id).first()
        # Self-heal folders created before create_folder made real directories:
        # they carry path="" and used to silently divert uploads to the media
        # root, leaving the project looking empty.
        if folder and not folder.path and folder.name:
            safe = re.sub(r'[<>:"|?*/\\]', "_", folder.name).strip() or f"folder-{folder.id}"
            healed = UPLOAD_ROOT / safe
            try:
                healed.mkdir(parents=True, exist_ok=True)
                folder.path = str(healed)
                folder.relative_path = safe
                folder.name = safe
                db.commit()
                print(f"[folders] gave '{safe}' a real directory on disk", flush=True)
            except Exception as e:
                print(f"[folders] could not create a directory for '{folder.name}': {e}", flush=True)
        if folder and folder.path:
            try:
                target = Path(folder.path).resolve()
                root = UPLOAD_ROOT.resolve()
                # never write outside the media root
                if target == root or root in target.parents:
                    if target.exists():
                        return target
            except Exception:
                pass
    return UPLOAD_ROOT


TRASH_DIRNAME = "_Trash"
SIDECAR_SUFFIXES = {'.xml', '.lrf', '.srt', '.dat', '.thm', '.cpi', '.rmd', '.moff', '.modd'}


def trash_root() -> Path:
    return UPLOAD_ROOT / TRASH_DIRNAME


def move_to_trash(real_path: str) -> str:
    """Move a file into D:\\Media\\_Trash, keeping its folder structure.

    Space is not reclaimed until the trash is emptied - that is the point. It
    makes pruning safe to do quickly, and the freed total is shown before you
    commit to it.
    """
    src = Path(real_path)
    root = UPLOAD_ROOT.resolve()
    try:
        rel = src.resolve().relative_to(root)
    except Exception:
        rel = Path(src.name)
    dest = trash_root() / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    stem, ext = dest.stem, dest.suffix
    n = 2
    while dest.exists():
        dest = dest.with_name(f"{stem} ({n}){ext}")
        n += 1
    shutil.move(str(src), str(dest))
    return str(dest)


def resolve_media_path(stored_path):
    """Map a path stored in the DB onto wherever the media root is now.

    Existing rows hold WSL-style paths like /mnt/e/Shared/clip.mp4. If the
    server is later run natively on Windows those stop resolving, so re-root
    them onto UPLOAD_ROOT instead of 404-ing.
    """
    if not stored_path:
        return stored_path
    if os.path.exists(stored_path):
        return stored_path
    normalised = str(stored_path).replace("\\", "/")
    lowered = normalised.lower()
    for marker in _MEDIA_ROOT_MARKERS:
        if marker in lowered:
            tail = normalised[lowered.index(marker) + len(marker):]
            candidate = UPLOAD_ROOT / tail
            if candidate.exists():
                return str(candidate)
    candidate = UPLOAD_ROOT / os.path.basename(normalised)
    if candidate.exists():
        return str(candidate)
    return stored_path

app = FastAPI(title="Zerko File Manager")

# Bring the database up to date on import, not only when main.py is run
# directly. Starting the app any other way (uvicorn, gunicorn, a service
# wrapper) used to skip the migrations entirely, and the first request that
# touched a new column failed with "no such column". init_db() is idempotent.
try:
    init_db()
except Exception as _e:          # a broken migration must not stop the app
    print(f"init_db at import failed: {_e}", flush=True)

# SSE events queue for listeners
import asyncio

@app.get("/api/events")
async def sse_events(request: Request):
    """Server-Sent Events endpoint for live job progress."""
    # Each client gets its own queue, so every open tab sees every event.
    subscriber = job_manager.subscribe()

    async def event_generator():
        idle = 0
        try:
            while True:
                if await request.is_disconnected():
                    break

                # Non-blocking - draining several at once keeps bursts snappy
                sent = 0
                while sent < 200:
                    data = job_manager.get_event(subscriber)
                    if not data:
                        break
                    yield f"event: {data['type']}\ndata: {json.dumps(data)}\n\n"
                    sent += 1

                if sent:
                    idle = 0
                else:
                    idle += 1
                    # keep-alive comment so proxies don't drop an idle stream
                    if idle % 30 == 0:
                        yield ": keep-alive\n\n"
                    await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            pass
        finally:
            job_manager.unsubscribe(subscriber)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.get("/api/video-proxy/{video_id}")
def serve_proxy(
    request: Request,
    video_id: int,
    token: Optional[str] = None,
    db: Session = Depends(get_db)
):
    if token:
        get_user_from_token(token, db, check_session=False)
    else:
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
             raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
        token = auth_header[7:]
        get_user_from_token(token, db)
    
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    proxy_path = resolve_media_path(video.proxy_path)
    if not proxy_path or not os.path.exists(proxy_path):
        # Fallback to original if proxy not ready
        original = resolve_media_path(video.filepath)
        if not os.path.exists(original):
            raise HTTPException(status_code=404, detail="Original video file not found")
        # Someone wants to watch this and there is no proxy: make one now.
        # Phone HEVC and camera ProRes play as sound only in most browsers,
        # so waiting for the nightly sweep means a black box until tomorrow.
        if (video.media_type or "video") == "video" and video.proxy_status not in ("queued", "processing"):
            try:
                job_manager.add_job(video.id, "proxy")
            except Exception as e:
                print(f"[proxy] could not queue {video.id} on view: {e}", flush=True)
        return FileResponse(original, filename=video.filename, media_type='application/octet-stream',
                            headers={"Accept-Ranges": "bytes"})

    return FileResponse(proxy_path, filename=f"proxy_{video.filename}", media_type='application/octet-stream',
                        headers={"Accept-Ranges": "bytes"})

# Removed download token logic.

import secrets
# In-memory storage for download tokens (expires in 60s)
download_tokens = {}

# Background media-scan state
_rescan_state = {"running": False, "message": "idle", "stats": None}

# --- photo previews ----------------------------------------------------------
# Browsers cannot draw camera RAW (DNG, ARW, CR2, NEF...), HEIC or most TIFFs,
# so opening one in the viewer showed a broken image. This returns a JPEG the
# browser can show, made once and kept. The original is never touched and is
# still what "download" delivers.
_preview_lock = threading.Lock()


@app.get("/api/photo-preview/{video_id}")
def photo_preview(request: Request, video_id: int, token: Optional[str] = None,
                  w: int = 2400, db: Session = Depends(get_db)):
    if token:
        get_user_from_token(token, db)
    else:
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
        get_user_from_token(auth_header[7:], db)

    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Photo not found")
    path = resolve_media_path(video.filepath)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="The photo file is not on disk")

    if Path(path).suffix.lower() in previews.BROWSER_IMAGE_EXT:
        return FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})

    w = max(400, min(int(w), 4000))
    st = os.stat(path)
    out = UPLOAD_ROOT / ".previews" / f"{video.id}_{int(st.st_mtime)}_{st.st_size}_{w}.jpg"
    if not out.exists() or out.stat().st_size == 0:
        out.parent.mkdir(parents=True, exist_ok=True)
        with _preview_lock:
            if not out.exists():
                ok, why = previews.make_image_jpeg(path, str(out), w)
                if not ok:
                    raise HTTPException(
                        status_code=415,
                        detail=f"Could not make a viewable copy of this photo: {why}")
    return FileResponse(str(out), media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/videos/{video_id}/download-token")
def get_download_token(video_id: int, current_user: User = Depends(get_current_user)):
    token = secrets.token_urlsafe(32)
    download_tokens[token] = {"video_id": video_id, "expires": datetime.utcnow() + timedelta(seconds=60)}
    return {"token": token}

@app.get("/api/video-file/{video_id}")
def download_video(
    request: Request,
    video_id: int,
    token: Optional[str] = None,
    db: Session = Depends(get_db)
):
    # 1. Check for one-time download token
    if token and token in download_tokens:
        token_data = download_tokens.pop(token)
        if token_data["video_id"] == video_id and token_data["expires"] > datetime.utcnow():
            video = db.query(Video).filter(Video.id == video_id).first()
            if video and os.path.exists(resolve_media_path(video.filepath)):
                return FileResponse(
                    path=resolve_media_path(video.filepath),
                    filename=video.filename,
                    media_type='application/octet-stream',
                    headers={"Accept-Ranges": "bytes"}
                )
            
    # 2. Try treating token query param as JWT
    try:
        if token:
            get_user_from_token(token, db, check_session=False)
        else:
            # 3. Fallback: Authenticate using Bearer token (JWT) from Authorization header
            auth_header = request.headers.get("Authorization")
            if not auth_header or not auth_header.startswith("Bearer "):
                raise HTTPException(status_code=401, detail="Missing authorization")
            get_user_from_token(auth_header[7:], db)
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video or not os.path.exists(resolve_media_path(video.filepath)):
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        path=resolve_media_path(video.filepath),
        filename=video.filename,
        media_type='application/octet-stream',
        headers={"Accept-Ranges": "bytes"}
    )
# CORS - allow frontend dev server
CORS_ORIGINS = os.environ.get(
    "CORS_ORIGINS",
    # Local only by default. Set the CORS_ORIGINS environment
    # variable if you serve this under your own hostname.
    "http://localhost:9600,http://localhost:5173",
).split(",")
CORS_ORIGINS = [o.strip() for o in CORS_ORIGINS if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
os.makedirs(UPLOAD_ROOT / "thumbnails", exist_ok=True)
app.mount("/thumbnails", StaticFiles(directory=str(UPLOAD_ROOT / "thumbnails")), name="thumbnails")
if (FRONTEND_DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")

os.makedirs("templates", exist_ok=True)
templates = Jinja2Templates(directory="templates")

# Login
class LoginRequest(BaseModel):
    username: str
    password: str

class RenameRequest(BaseModel):
    name: str


class UserCreate(BaseModel):
    username: str
    # Optional: an agent or a client often has no reason to give one, and an
    # empty string would collide with the next person's under the unique index.
    email: Optional[str] = None
    password: str
    role: str = "user"
# --- brute-force protection -------------------------------------------------
# Once this server is reachable from the internet the login page WILL be found
# by scanners within hours. Without a limiter, "admin" + a password list is a
# matter of time.
_login_attempts = {}          # ip -> [failure timestamps]
_LOGIN_WINDOW = 900           # 15 minutes
_LOGIN_MAX_FAILS = 8


# X-Forwarded-For is a header the CLIENT writes. Believing it from anyone lets an
# attacker send a new made-up address with every guess and never reach the
# limit. It is only trusted when the connection really comes from a proxy we
# run: Caddy on this machine, seen here as loopback or as the WSL/Docker
# gateway (172.16.0.0/12). Add other proxies, comma-separated addresses or
# CIDR ranges, in the TRUSTED_PROXIES environment variable.
import ipaddress
import contextvars


def _parse_nets(items):
    nets = []
    for it in items:
        try:
            nets.append(ipaddress.ip_network(it.strip(), strict=False))
        except ValueError:
            pass
    return nets


_TRUSTED_NETS = _parse_nets(
    ["127.0.0.0/8", "::1/128", "172.16.0.0/12"]
    + [p for p in os.environ.get("TRUSTED_PROXIES", "").split(",") if p.strip()])


def _is_trusted_proxy(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in n for n in _TRUSTED_NETS if n.version == ip.version)


def _client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    if _is_trusted_proxy(peer):
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            # The proxy appends the address it saw, so the real client is the
            # right-most entry that is not itself one of our proxies.
            for hop in reversed([h.strip() for h in fwd.split(",") if h.strip()]):
                if not _is_trusted_proxy(hop):
                    return hop
    return peer


# The share endpoints do not take a Request, so the caller's address is
# remembered per request here for their password limiter to read.
_request_ip = contextvars.ContextVar("request_ip", default="unknown")


# "Preview as the client": staff open a client page with ?preview=<their own
# sign-in token>. It gets in without the client's password, and nothing it
# does counts - no view, no download, no picks saved.
_staff_preview = contextvars.ContextVar("staff_preview", default=False)


@app.middleware("http")
async def _remember_client_ip(request: Request, call_next):
    _request_ip.set(_client_ip(request))
    preview = request.query_params.get("preview")
    ok = False
    if preview and request.url.path.startswith("/api/public/share/"):
        db = SessionLocal()
        try:
            u = get_user_from_token(preview, db, check_session=False)
            ok = bool(u) and permissions.can(u.role, permissions.SHARES)
        except Exception:
            ok = False
        finally:
            db.close()
        if ok and request.method != "GET":
            return JSONResponse(status_code=403, content={"detail": "This is a preview - nothing is saved."})
    _staff_preview.set(ok)
    return await call_next(request)


def _check_rate_limit(ip: str):
    now = datetime.utcnow().timestamp()
    fails = [ts for ts in _login_attempts.get(ip, []) if now - ts < _LOGIN_WINDOW]
    _login_attempts[ip] = fails
    if len(fails) >= _LOGIN_MAX_FAILS:
        wait = int((_LOGIN_WINDOW - (now - fails[0])) / 60) + 1
        raise HTTPException(status_code=429,
                            detail=f"Too many failed sign-ins. Try again in {wait} minute(s).")


def _record_failure(ip: str):
    _login_attempts.setdefault(ip, []).append(datetime.utcnow().timestamp())
    if len(_login_attempts) > 5000:            # keep the dict from growing forever
        cutoff = datetime.utcnow().timestamp() - _LOGIN_WINDOW
        for k in [k for k, v in _login_attempts.items() if not v or v[-1] < cutoff]:
            _login_attempts.pop(k, None)


@app.post("/api/login")
def login(request: LoginRequest, http_request: Request, db: Session = Depends(get_db)):
    ip = _client_ip(http_request)
    _check_rate_limit(ip)

    user = db.query(User).filter(User.username == request.username).first()
    if (not user or not verify_password(request.password, user.hashed_password)
            or user.is_active is False):
        _record_failure(ip)
        remaining = _LOGIN_MAX_FAILS - len(_login_attempts.get(ip, []))
        print(f"[auth] failed sign-in for '{request.username}' from {ip} "
              f"({max(remaining, 0)} attempt(s) left)", flush=True)
        raise HTTPException(status_code=401, detail="Invalid username or password")

    _login_attempts.pop(ip, None)
    try:
        user.last_login = datetime.utcnow()
        db.commit()
    except Exception:
        db.rollback()
    token_data = create_access_token({"sub": user.username})
    return {"token": token_data["token"], "user": {"id": user.id, "username": user.username, "role": user.role}}


@app.post("/api/me/password")
def change_my_password(payload: dict = Body(...), db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    current = (payload or {}).get("current_password") or ""
    new = (payload or {}).get("new_password") or ""
    if not verify_password(current, current_user.hashed_password):
        raise HTTPException(status_code=403, detail="Current password is wrong")
    if len(new) < 10:
        raise HTTPException(status_code=400, detail="Use at least 10 characters")
    if new.lower() in ("admin123", "password", "123456789", "changeme"):
        raise HTTPException(status_code=400, detail="That password is far too common")
    current_user.hashed_password = get_password_hash(new)
    revoke_sessions(current_user)              # force a fresh sign-in everywhere
    db.commit()
    return {"message": "Password changed. Sign in again."}


@app.post("/api/users/{user_id}/password")
def admin_set_password(user_id: int, payload: dict = Body(...), db: Session = Depends(get_db),
                       current_user: User = Depends(get_admin_user)):
    new = (payload or {}).get("new_password") or ""
    if len(new) < 10:
        raise HTTPException(status_code=400, detail="Use at least 10 characters")
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    target.hashed_password = get_password_hash(new)
    revoke_sessions(target)
    db.commit()
    return {"message": f"Password reset for {target.username}"}

@app.get("/api/me")
def get_me(current_user: User = Depends(get_current_user)):
    # The capability list is what the UI hides buttons with. It is a
    # convenience, never the guard - the middleware is the guard.
    return {
        "id": current_user.id,
        "username": current_user.username,
        "role": current_user.role,
        "role_label": permissions.ROLE_LABELS.get(current_user.role, current_user.role),
        "capabilities": sorted(permissions.caps_for(current_user.role)),
    }

def _queue_all_transcripts(retry_failed: bool = False) -> int:
    s2 = SessionLocal()
    try:
        states = [None, "not_started"] + (["failed"] if retry_failed else [])
        rows = (s2.query(Video.id).filter(Video.is_active != False, Video.media_type.in_(["video", "audio"]),
                                          or_(Video.transcription_status.is_(None), Video.transcription_status.in_([x for x in states if x])))
                .all())
        for (vid,) in rows:
            job_manager.add_job(vid, "transcribe")
        return len(rows)
    finally:
        s2.close()


@app.post("/api/videos/transcribe-all")
def transcribe_all(payload: Optional[dict] = Body(None), current_user: User = Depends(get_current_user)):
    """Queue every clip with sound that has no transcript yet (and, when asked, the ones that failed)."""
    return {"queued": _queue_all_transcripts(bool((payload or {}).get("retry_failed")))}


@app.post("/api/videos/batch-transcribe")
def batch_transcribe(request: dict, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    all_video_ids = request.get("video_ids", [])
    videos_to_process = db.query(Video).filter(
        Video.id.in_(all_video_ids),
        Video.media_type.in_(["video", "audio"]),          # photos have no audio
        or_(Video.transcription_status == None,
            Video.transcription_status == "not_started")
    ).all()
    video_ids = [v.id for v in videos_to_process]
    skipped_count = len(all_video_ids) - len(video_ids)
    for vid in video_ids: job_manager.add_job(vid, "transcribe")
    return {"message": "Jobs queued", "queued": len(video_ids), "skipped": skipped_count}

@app.post("/api/upload")
def upload_file(
    file: UploadFile = File(...),
    folder_id: Optional[int] = Form(None),
    relative_path: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    os.makedirs(UPLOAD_ROOT, exist_ok=True)
    access.require_folder(db, current_user, folder_id, "edit")
    dest_dir = upload_destination_dir(db, folder_id)
    # Uploading a whole directory: recreate its structure under the target
    sub = safe_relative_dir(relative_path)
    if sub:
        dest_dir = dest_dir / sub
    os.makedirs(dest_dir, exist_ok=True)
    file_path = unique_destination(dest_dir, file.filename)
    # This handler is a sync `def`, so it runs in a worker thread and this
    # blocking copy no longer freezes the server for everyone else.
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer, COPY_BUFFER)
    os.makedirs(UPLOAD_ROOT / "thumbnails", exist_ok=True)
    media_type = get_media_type(file.filename)
    thumbnail_filename = f"{uuid.uuid4()}.jpg"
    thumb_path = UPLOAD_ROOT / "thumbnails" / thumbnail_filename
    
    # Generate thumbnail in background. Wrapped, because an unreadable or
    # unsupported file used to kill this thread with a raw traceback.
    def generate_thumb_bg():
        try:
            if media_type in ('video', 'photo'):
                ok, why = previews.make_thumbnail(str(file_path), str(thumb_path), media_type)
                if not ok:
                    print(f"Thumbnail failed for {file_path}: {why}")
        except Exception as e:
            print(f"Thumbnail generation failed for {file_path}: {e}")
    
    thread = threading.Thread(target=generate_thumb_bg)
    thread.daemon = True
    thread.start()

    stored_name = file_path.name
    if sub:
        row = ensure_folder_row(db, dest_dir)
        if row:
            folder_id = row.id
    video = Video(filename=stored_name, filepath=str(file_path), file_size=os.path.getsize(file_path), duration=get_duration_fast(str(file_path)) if media_type == "video" else 0.0, thumbnail_path=(f"/thumbnails/{thumbnail_filename}" if media_type in ("video", "photo") else None), folder_id=folder_id, uploaded_by=current_user.username, uploaded_at=datetime.utcnow(), status="raw", media_type=media_type)
    db.add(video)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.query(Video).filter(Video.filepath == str(file_path)).first()
        if existing:
            return {"job_id": None, "status": "complete", "progress": 100,
                    "video": {"id": existing.id, "filename": existing.filename,
                              "media_type": existing.media_type}}
        raise HTTPException(status_code=409, detail="Could not save upload")
    db.refresh(video)
    job_id = str(uuid.uuid4())
    if video.media_type == "video": job_manager.add_job(video.id, "proxy")
    return {"job_id": job_id, "status": "complete", "progress": 100, "video": {"id": video.id, "filename": video.filename, "media_type": video.media_type}}

@app.get("/api/videos")
def list_videos(search: Optional[str] = None, tags: Optional[str] = None, sort_by: Optional[str] = None, sort_order: Optional[str] = "desc", folder_id: Optional[int] = None, include_subfolders: bool = False, date_range: Optional[str] = None, rating: Optional[str] = None, media_type: Optional[str] = None, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    from database import TranscriptionSegment
    query = db.query(Video).filter((Video.is_active == True) | (Video.is_active == None))
    # Client and viewer accounts see only what was shared with them (access.py).
    _vis = access.visible_folder_ids(db, current_user)
    if _vis is not None: query = query.filter(Video.folder_id.in_(_vis))
    if media_type and media_type != 'all': query = query.filter(Video.media_type == media_type)
    
    search_results = []
    if search:
        terms = search.split()
        scores = {}
        for term in terms:
            pattern = f"%{term}%"
            filenames = db.query(Video.id).filter(Video.filename.ilike(pattern)).all()
            for (vid,) in filenames: scores[vid] = scores.get(vid, {"score": 0, "matches": []}); scores[vid]["score"] += 10
            segments = db.query(TranscriptionSegment).filter(TranscriptionSegment.text.ilike(pattern)).all()
            for seg in segments: 
                scores[seg.video_id] = scores.get(seg.video_id, {"score": 0, "matches": []}); scores[seg.video_id]["score"] += 5
                snippet = {"text": seg.text, "start": seg.start_time, "end": seg.end_time}
                if snippet not in scores[seg.video_id]["matches"]: scores[seg.video_id]["matches"].append(snippet)
        if not scores: return []
        query = query.filter(Video.id.in_(scores.keys()))
        videos = query.all()
        for v in videos: search_results.append({"video": v, "score": scores[v.id]["score"], "matches": scores[v.id]["matches"]})
        search_results.sort(key=lambda x: x["score"], reverse=True)
        videos = [res["video"] for res in search_results]
    else:
        if tags: query = query.join(Video.tags).filter(Tag.name.in_([t.strip() for t in tags.split(',')]))
        if folder_id is not None:
            if include_subfolders:
                query = query.filter(Video.folder_id.in_(descendant_folder_ids(db, folder_id)))
            else:
                query = query.filter(Video.folder_id == folder_id)
        if rating and rating != 'null': query = query.filter(Video.rating >= int(rating))
        if date_range and date_range != "all":
            now = datetime.utcnow()
            cutoff = {"24h": now - timedelta(hours=24), "7d": now - timedelta(days=7), "30d": now - timedelta(days=30)}.get(date_range)
            if cutoff: query = query.filter((Video.created_at >= cutoff) | (Video.uploaded_at >= cutoff))
        if sort_by:
            # The UI sends date/size/duration/name. getattr(Video, "size") and
            # getattr(Video, "name") are both None - the columns are file_size
            # and filename - so sorting by Size or Name silently did nothing and
            # the list stayed in insertion order.
            SORT_COLUMNS = {
                "date": Video.uploaded_at,
                "created": Video.created_at,
                "created_at": Video.created_at,
                "size": Video.file_size,
                "file_size": Video.file_size,
                "duration": Video.duration,
                "name": func.lower(Video.filename),
                "filename": func.lower(Video.filename),
                "rating": Video.rating,
                "status": Video.status,
            }
            sort_column = SORT_COLUMNS.get(sort_by, getattr(Video, sort_by, None))
            if sort_column is not None:
                direction = sort_column.asc() if sort_order == "asc" else sort_column.desc()
                # Clips with no duration/rating shouldn't bubble to the top
                query = query.order_by(direction.nullslast() if hasattr(direction, "nullslast") else direction,
                                       Video.id.asc())
        videos = query.all()

    # the editor's picks and whether a photo has been edited, for the filters
    edits = {}
    try:
        import photo_edit
        ids = [v.id for v in videos if v.media_type == "photo"]
        for i in range(0, len(ids), 900):
            for vid, pick, rec in db.query(photo_edit.PhotoEdit.video_id, photo_edit.PhotoEdit.pick,
                                           photo_edit.PhotoEdit.recipe).filter(
                    photo_edit.PhotoEdit.video_id.in_(ids[i:i + 900])).all():
                edits[vid] = (pick or 0, bool(rec and rec not in ("{}", "")))
    except Exception as e:
        print(f"list_videos: could not read edits: {e}", flush=True)
    res = []
    for v in videos:
        matches = next((sr["matches"] for sr in search_results if sr["video"].id == v.id), [])
        pick, edited = edits.get(v.id, (0, False))
        res.append({
            "id": v.id, "filename": v.filename, "filepath": v.filepath, "file_size": v.file_size,
            "file_size_formatted": format_file_size(v.file_size), "duration": v.duration,
            "duration_formatted": format_duration(v.duration), "thumbnail_path": v.thumbnail_path,
            "folder_name": v.folder.name if v.folder else None, "folder_id": v.folder_id,
            "tags": [{"id": t.id, "name": t.name} for t in v.tags],
            "created_at": v.created_at.isoformat() if v.created_at else None,
            "uploaded_at": v.uploaded_at.isoformat() if v.uploaded_at else None,
            "uploaded_by": v.uploaded_by, "status": v.status or "raw", "media_type": v.media_type,
            # Full transcripts are NOT sent in the list payload - with 1,200 clips
            # that is megabytes of JSON the grid never displays. The detail
            # endpoint returns it, and search_matches carries the snippet.
            "has_transcription": bool(v.transcription), "transcription_status": v.transcription_status,
            "proxy_status": v.proxy_status, "has_proxy": v.proxy_path is not None and os.path.exists(v.proxy_path),
            "rating": v.rating, "shoot_date": v.shoot_date.isoformat() if v.shoot_date else None,
            "camera_make": v.camera_make, "camera_model": v.camera_model, "video_codec": v.video_codec,
            "frame_rate": v.frame_rate, "resolution": v.resolution, "audio_codec": v.audio_codec,
            "search_matches": matches, "pick": pick, "edited": edited, "label": v.color_label,
        })
    return res if isinstance(res, list) else []


@app.get("/api/videos/{video_id}/notes")
def get_video_notes(video_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    notes = db.query(Note).filter(Note.media_id == video_id).order_by(Note.created_at.asc()).all()
    return [{"id": n.id, "content": n.content, "author": n.author,
             "created_at": n.created_at.isoformat() if n.created_at else None} for n in notes]

@app.get("/api/video-access-token")
def get_video_access_token(current_user: User = Depends(get_current_user)):
    token_data = create_access_token({"sub": current_user.username}, expires_delta=timedelta(minutes=60))
    return {"token": token_data["token"]}

@app.get("/api/users")
def list_users(db: Session = Depends(get_db), current_user: User = Depends(get_admin_user)):
    # Explicit fields: returning the ORM rows sends the password hash and the
    # session marker to the browser along with everything else.
    uploads = dict(db.query(Video.uploaded_by, func.count(Video.id)).filter(Video.uploaded_by.isnot(None)).group_by(Video.uploaded_by).all())
    notes = dict(db.query(Note.author, func.count(Note.id)).group_by(Note.author).all())
    return [{
        "id": u.id, "username": u.username, "email": u.email, "role": u.role,
        "created_at": u.created_at, "last_login": u.last_login,
        "is_active": u.is_active,
        "uploads": uploads.get(u.username, 0), "notes": notes.get(u.username, 0),
    } for u in db.query(User).all()]


# One-time "set your password" links an admin can hand someone: no need to
# say a password out loud. Only a hash of the token is kept.
RESETS_FILE = Path(__file__).resolve().parent / "password_resets.json"


def _resets() -> dict:
    try:
        return json.loads(RESETS_FILE.read_text())
    except Exception:
        return {}


@app.post("/api/users/{user_id}/reset-link")
def make_reset_link(user_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_admin_user)):
    import hashlib, secrets as _secrets
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    token = _secrets.token_urlsafe(24)
    r = {k: v for k, v in _resets().items() if v.get("exp", 0) > time.time() and v.get("user_id") != user_id}
    r[hashlib.sha256(token.encode()).hexdigest()] = {"user_id": user_id, "exp": time.time() + 48 * 3600}
    RESETS_FILE.write_text(json.dumps(r))
    return {"url": f"/v2/reset/{token}", "expires_hours": 48, "username": target.username}


PREFS_FILE = Path(__file__).resolve().parent / "user_prefs.json"
_prefs_lock = threading.Lock()


def _prefs() -> dict:
    try:
        return json.loads(PREFS_FILE.read_text())
    except Exception:
        return {}


@app.get("/api/me/appearance")
def get_my_appearance(current_user: User = Depends(get_current_user)):
    """Appearance kept on the account, so it follows you to other computers."""
    return {"appearance": _prefs().get(current_user.username, {}).get("appearance")}


@app.put("/api/me/appearance")
def put_my_appearance(payload: dict = Body(...), current_user: User = Depends(get_current_user)):
    a = (payload or {}).get("appearance")
    if a is not None and (not isinstance(a, dict) or len(json.dumps(a)) > 8000):
        raise HTTPException(status_code=400, detail="Not an appearance")
    with _prefs_lock:
        p = _prefs()
        p.setdefault(current_user.username, {})["appearance"] = a
        PREFS_FILE.write_text(json.dumps(p))
    return {"ok": True}


_PREF_KEYS = {"export_presets", "sync_groups", "room_export_types"}


@app.get("/api/me/prefs/{key}")
def get_my_pref(key: str, current_user: User = Depends(get_current_user)):
    if key not in _PREF_KEYS:
        raise HTTPException(status_code=404, detail="Unknown setting")
    return {"value": _prefs().get(current_user.username, {}).get(key)}


@app.put("/api/me/prefs/{key}")
def put_my_pref(key: str, payload: dict = Body(...), current_user: User = Depends(get_current_user)):
    if key not in _PREF_KEYS:
        raise HTTPException(status_code=404, detail="Unknown setting")
    value = (payload or {}).get("value")
    if len(json.dumps(value)) > 20000:
        raise HTTPException(status_code=400, detail="Too big")
    with _prefs_lock:
        p = _prefs()
        p.setdefault(current_user.username, {})[key] = value
        PREFS_FILE.write_text(json.dumps(p))
    return {"value": value}


@app.get("/api/me/views")
def get_my_views(current_user: User = Depends(get_current_user)):
    """Saved library views ("smart collections"): a name and the filters."""
    return {"views": _prefs().get(current_user.username, {}).get("views", [])}


@app.put("/api/me/views")
def put_my_views(payload: dict = Body(...), current_user: User = Depends(get_current_user)):
    views = (payload or {}).get("views") or []
    if not isinstance(views, list) or len(views) > 60:
        raise HTTPException(status_code=400, detail="Too many saved views")
    clean = []
    for v in views:
        if isinstance(v, dict) and isinstance(v.get("name"), str) and isinstance(v.get("qs"), str):
            clean.append({"name": v["name"].strip()[:60], "qs": v["qs"][:1000]})
    with _prefs_lock:
        p = _prefs()
        p.setdefault(current_user.username, {})["views"] = clean
        PREFS_FILE.write_text(json.dumps(p))
    return {"views": clean}


@app.post("/api/password-reset")
def use_reset_link(payload: dict = Body(...), db: Session = Depends(get_db)):
    import hashlib
    token = str((payload or {}).get("token") or "")
    new = str((payload or {}).get("new_password") or "")
    key = hashlib.sha256(token.encode()).hexdigest()
    r = _resets()
    entry = r.get(key)
    if not entry or entry.get("exp", 0) < time.time():
        raise HTTPException(status_code=400, detail="This link has expired or was already used. Ask for a new one.")
    if len(new) < 10:
        raise HTTPException(status_code=400, detail="Use at least 10 characters")
    if new.lower() in ("admin123", "password", "123456789", "changeme", "password123"):
        raise HTTPException(status_code=400, detail="That password is far too common")
    target = db.query(User).filter(User.id == entry["user_id"]).first()
    if not target:
        raise HTTPException(status_code=400, detail="That account no longer exists")
    target.hashed_password = get_password_hash(new)
    revoke_sessions(target)
    db.commit()
    r.pop(key, None)
    RESETS_FILE.write_text(json.dumps(r))
    return {"message": "Password set. You can sign in now.", "username": target.username}

@app.post("/api/register")
def register_user(user: UserCreate, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    """Create an account.

    Admins may create any role. Editors may create clients and viewers only -
    an account that can mint its own peers can mint an admin one persuaded
    colleague later, so the ceiling is deliberate and checked here rather than
    in the browser.
    """
    role = (user.role or "client").lower()
    if role not in permissions.VALID_ROLES:
        raise HTTPException(status_code=400,
                            detail=f"Role must be one of {', '.join(permissions.VALID_ROLES)}")

    if current_user.role != "admin":
        if not permissions.can(current_user.role, permissions.CREATE_CLIENTS):
            raise HTTPException(status_code=403, detail="Your account cannot create other accounts.")
        if role not in permissions.EDITOR_MAY_CREATE:
            raise HTTPException(
                status_code=403,
                detail="You can create client and view-only accounts. "
                       "Ask an administrator for anything above that.")

    username = (user.username or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")
    # Same floor as changing your own password - a new account should not be
    # allowed to start weaker than an existing one is allowed to become.
    if len(user.password or "") < 10:
        raise HTTPException(status_code=400, detail="Password must be at least 10 characters")
    # Without this the unique index raises an IntegrityError and the caller
    # gets a 500 that says nothing.
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=400, detail="That username is already taken")
    if user.email and db.query(User).filter(User.email == user.email).first():
        raise HTTPException(status_code=400, detail="That email is already in use")

    new_user = User(username=username, email=(user.email or "").strip() or None,
                    hashed_password=get_password_hash(user.password), role=role)
    db.add(new_user)
    db.commit()
    return {"message": "User created successfully", "id": new_user.id,
            "username": new_user.username, "role": new_user.role}

@app.delete("/api/users/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_admin_user)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user: raise HTTPException(status_code=404, detail="User not found")
    if user.id == current_user.id: raise HTTPException(status_code=400, detail="Cannot delete self")
    db.delete(user)
    db.commit()
    return {"message": "User deleted"}

@app.post("/api/admin/users/{user_id}/logout")
def force_logout(user_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_admin_user)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user: raise HTTPException(status_code=404, detail="User not found")
    revoke_sessions(user)
    db.commit()
    return {"message": "User logged out"}

@app.get("/api/admin/users-usage")
def get_users_usage(db: Session = Depends(get_db), current_user: User = Depends(get_admin_user)):
    usage = db.query(User.username, func.count(Video.id).label("video_count"), func.sum(Video.file_size).label("total_size")).outerjoin(Video, User.username == Video.uploaded_by).group_by(User.username).all()
    return [{"username": u.username, "video_count": u.video_count, "total_size": u.total_size or 0, "total_size_formatted": format_file_size(u.total_size or 0)} for u in usage]
@app.get("/api/tags")
def list_tags(folder_id: Optional[int] = None, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    query = db.query(Tag)
    if folder_id is not None:
        query = query.filter(Tag.folder_id == folder_id)
    return query.all()

@app.get("/api/tags/tree")
def list_tags_tree(folder_id: Optional[int] = None, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    query = db.query(Tag)
    if folder_id is not None:
        query = query.filter(Tag.folder_id == folder_id)
    tags = query.all()
    root_tags = [t for t in tags if t.parent_id is None]
    def build_node(tag):
        return {"id": tag.id, "name": tag.name, "children": [build_node(child) for child in tag.children]}
    return [build_node(t) for t in root_tags]

@app.post("/api/tags")
def create_tag(payload: dict = Body(...), db: Session = Depends(get_db), current_user: User = Depends(get_admin_user)):
    data = payload
    new_tag = Tag(name=data["name"], parent_id=data.get("parent_id"), folder_id=data.get("folder_id"))
    db.add(new_tag)
    db.commit()
    db.refresh(new_tag)
    return {"id": new_tag.id, "name": new_tag.name, "folder_id": new_tag.folder_id}


@app.get("/api/folders")
def list_folders(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    video_count_subquery = select(Video.folder_id, func.count(Video.id).label("count")).group_by(Video.folder_id).subquery()
    folders_with_count = db.query(IndexedFolder, video_count_subquery.c.count).outerjoin(video_count_subquery, IndexedFolder.id == video_count_subquery.c.folder_id).order_by(IndexedFolder.order.asc(), IndexedFolder.name.asc()).all()
    _vis = access.visible_folder_ids(db, current_user)
    if _vis is not None:
        folders_with_count = [(f, c) for f, c in folders_with_count if f.id in _vis]
    return [{"id": f.id, "name": f.name, "path": f.path, "relative_path": f.relative_path,
             "parent_id": f.parent_id,
             "added_at": f.added_at.isoformat() if f.added_at else None,
             "last_scanned": f.last_scanned.isoformat() if f.last_scanned else None,
             "video_count": count or 0} for f, count in folders_with_count]


def descendant_folder_ids(db: Session, folder_id: int):
    """That folder plus every folder nested underneath it."""
    ids, frontier = [folder_id], [folder_id]
    while frontier:
        kids = [r[0] for r in db.query(IndexedFolder.id)
                .filter(IndexedFolder.parent_id.in_(frontier)).all()]
        kids = [k for k in kids if k not in ids]
        if not kids:
            break
        ids.extend(kids)
        frontier = kids
    return ids


@app.get("/api/folders/tree")
def list_folders_tree(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Folders as a nested tree, mirroring the directory structure on disk."""
    counts = dict(db.query(Video.folder_id, func.count(Video.id)).group_by(Video.folder_id).all())
    # how many of each kind (video / photo / audio) each folder holds directly
    by_type = {}
    for fid, mt, n in (db.query(Video.folder_id, Video.media_type, func.count(Video.id))
                       .filter((Video.is_active == True) | (Video.is_active == None))
                       .group_by(Video.folder_id, Video.media_type).all()):
        by_type.setdefault(fid, {})[mt or "video"] = n
    folders = db.query(IndexedFolder).order_by(IndexedFolder.name.asc()).all()
    # A client sees only the folders shared with them; each one's top shows as a root.
    _vis = access.visible_folder_ids(db, current_user)
    if _vis is not None:
        folders = [f for f in folders if f.id in _vis]

    by_parent = {}
    for f in folders:
        by_parent.setdefault(f.parent_id if _vis is None or f.parent_id in _vis else None, []).append(f)

    def build(folder):
        kids = [build(c) for c in by_parent.get(folder.id, [])]
        own = counts.get(folder.id, 0)
        own_types = by_type.get(folder.id, {})
        total_types = dict(own_types)
        for k in kids:
            for t, n in k["total_by_type"].items():
                total_types[t] = total_types.get(t, 0) + n
        return {
            "id": folder.id,
            "name": (folder.relative_path or folder.name or "").split("/")[-1] or folder.name,
            "full_name": folder.name,
            "relative_path": folder.relative_path,
            "path": folder.path,
            "video_count": own,
            "total_count": own + sum(k["total_count"] for k in kids),
            "by_type": own_types,
            "total_by_type": total_types,
            "created_in_app": bool(folder.created_in_app),
            "children": kids,
        }

    return [build(f) for f in by_parent.get(None, [])]


_thumb_repair = {"running": False, "done": 0, "fixed": 0, "total": 0,
                 "message": "idle", "failures": []}


@app.post("/api/rescan")
def rescan_media_root(payload: Optional[dict] = Body(None), db: Session = Depends(get_db),
                      current_user: User = Depends(get_admin_user)):
    """Index anything new under the media root. Runs in the background."""
    opts = payload or {}
    queue_proxies = bool(opts.get("queue_proxies"))
    transcribe = bool(opts.get("transcribe"))

    if _rescan_state["running"]:
        return {"status": "already_running", "message": _rescan_state["message"]}

    def run():
        from indexer import index_tree
        _rescan_state.update(running=True, message="Starting scan...", stats=None)
        try:
            def progress(msg, stats):
                _rescan_state["message"] = msg
                _rescan_state["stats"] = dict(stats)
            result = index_tree(queue_proxies=queue_proxies, progress=progress)
            _rescan_state["stats"] = result
            _rescan_state["message"] = "Scan complete"

            # Anything that failed to thumbnail during the walk gets another
            # go - a file still being copied in is the common case.
            try:
                from indexer import make_thumbnail, thumb_name
                thumbs_dir = UPLOAD_ROOT / "thumbnails"
                thumbs_dir.mkdir(parents=True, exist_ok=True)
                s2 = SessionLocal()
                redrawn = 0
                try:
                    for v in (s2.query(Video)
                              .filter(Video.is_active != False,
                                      Video.media_type.in_(("video", "photo")),
                                      Video.thumbnail_path.is_(None)).all()):
                        real = resolve_media_path(v.filepath)
                        if not real or not os.path.exists(real):
                            continue
                        name = thumb_name(real)
                        if make_thumbnail(real, str(thumbs_dir / name), v.media_type):
                            v.thumbnail_path = f"/thumbnails/{name}"
                            redrawn += 1
                    s2.commit()
                finally:
                    s2.close()
                if redrawn:
                    print(f"[rescan] redrew {redrawn} thumbnail(s)", flush=True)
            except Exception as e:
                print(f"[rescan] thumbnail pass failed: {e}", flush=True)

            # Tag what just arrived. Tagging otherwise only ran at setup and
            # after a transcription, so footage copied in sat untagged until
            # something else happened to it - and tags are the only way most
            # of this library is findable.
            try:
                _rescan_state["message"] = "Scan complete - tagging new files..."
                import enrich
                tagged = enrich.run_tagging(
                    progress=lambda m: _rescan_state.update(message=f"Tagging: {m}"),
                    include_transcripts=True)
                _rescan_state["message"] = "Scan complete"
                if isinstance(tagged, dict) and tagged.get("tagged"):
                    _rescan_state["message"] = (
                        f"Scan complete - {tagged['tagged']} files tagged")
            except Exception as e:
                # A tagging failure must not make a successful scan look failed.
                print(f"[rescan] tagging after scan failed: {e}", flush=True)
                _rescan_state["message"] = "Scan complete (tagging failed)"
            # "Scan and transcribe everything" (Manage > Jobs): every clip with sound not done yet
            if transcribe:
                n = _queue_all_transcripts()
                _rescan_state["message"] += f" - {n} clips queued to transcribe" if n else " - nothing left to transcribe"
        except Exception as e:
            _rescan_state["message"] = f"Scan failed: {e}"
        finally:
            _rescan_state["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return {"status": "started"}


@app.post("/api/thumbnails/repair")
def repair_thumbnails(db: Session = Depends(get_db),
                      current_user: User = Depends(get_admin_user)):
    """Redraw thumbnails that are missing, and say why any cannot be.

    A thumbnail can fail the first time for ordinary reasons - the file was
    still being copied, ffmpeg timed out on a huge clip - and nothing ever
    tried again. This finds rows with no thumbnail, rows pointing at a file
    that is gone, and empty leftovers, tries several methods on each, refreshes
    the size and length of clips that were indexed while still incomplete, and
    keeps the reason for every one it could not fix so the panel can show it.
    """
    if _thumb_repair["running"]:
        return {"status": "already_running", "message": _thumb_repair["message"]}
    _thumb_repair.update(running=True, done=0, fixed=0, total=0,
                         message="Looking...", failures=[])

    def run():
        from indexer import thumb_name
        s = SessionLocal()
        try:
            thumbs_dir = UPLOAD_ROOT / "thumbnails"
            thumbs_dir.mkdir(parents=True, exist_ok=True)
            rows = (s.query(Video)
                    .filter(or_(Video.is_active == True, Video.is_active == None),
                            Video.media_type.in_(("video", "photo")))
                    .all())
            todo = []
            for v in rows:
                if not v.thumbnail_path:
                    todo.append(v)
                    continue
                try:
                    good = (thumbs_dir / os.path.basename(v.thumbnail_path)).stat().st_size > 0
                except OSError:
                    good = False
                if not good:
                    todo.append(v)

            _thumb_repair["total"] = len(todo)
            for v in todo:
                real = resolve_media_path(v.filepath)
                name = thumb_name(real or v.filepath)
                out = thumbs_dir / name
                try:
                    if out.exists() and out.stat().st_size == 0:
                        out.unlink()               # an empty leftover counts as missing
                except OSError:
                    pass
                if not real or not os.path.exists(real):
                    ok, why = False, ("the file is not where the catalog expects it "
                                      "(moved, renamed, or its drive is not connected)")
                else:
                    ok, why = previews.make_thumbnail(real, str(out), v.media_type)
                if ok:
                    v.thumbnail_path = f"/thumbnails/{name}"
                    _thumb_repair["fixed"] += 1
                    # A clip indexed while it was still being copied has the
                    # size and length it had THEN. It is readable now, so update.
                    try:
                        if v.media_type == "video":
                            v.file_size = os.path.getsize(real)
                            if not v.duration:
                                v.duration = get_duration_fast(real) or 0.0
                    except Exception:
                        pass
                elif len(_thumb_repair["failures"]) < 100:
                    _thumb_repair["failures"].append(
                        {"id": v.id, "filename": v.filename, "reason": why})
                _thumb_repair["done"] += 1
                _thumb_repair["message"] = f"{_thumb_repair['done']}/{len(todo)}"
                if _thumb_repair["done"] % 20 == 0:
                    s.commit()
            s.commit()
            bad = len(todo) - _thumb_repair["fixed"]
            if not todo:
                _thumb_repair["message"] = "Every thumbnail is already there"
            elif bad:
                _thumb_repair["message"] = (
                    f"Redrew {_thumb_repair['fixed']} of {len(todo)}. "
                    f"{bad} could not be drawn - see why below.")
            else:
                _thumb_repair["message"] = f"Redrew all {len(todo)} missing thumbnails"
        except Exception as e:
            _thumb_repair["message"] = f"Failed: {e}"
        finally:
            s.close()
            _thumb_repair["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return {"status": "started"}


@app.get("/api/thumbnails/repair/status")
def repair_thumbnails_status(current_user: User = Depends(get_current_user)):
    return _thumb_repair


@app.get("/api/rescan/status")
def rescan_status(current_user: User = Depends(get_current_user)):
    return _rescan_state

@app.post("/api/folders/reorder")
def reorder_folders(folder_ids: List[int], db: Session = Depends(get_db), current_user: User = Depends(get_admin_user)):
    for index, folder_id in enumerate(folder_ids):
        folder = db.query(IndexedFolder).filter(IndexedFolder.id == folder_id).first()
        if folder:
            folder.order = index
    db.commit()
    return {"message": "Order updated"}

@app.get("/api/stats")
def get_stats(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    total_videos = db.query(Video).count()
    total_storage = db.query(func.sum(Video.file_size)).scalar() or 0
    avg_duration = db.query(func.avg(Video.duration)).scalar() or 0
    proxy_counts = db.query(Video.proxy_status, func.count(Video.id)).group_by(Video.proxy_status).all()
    trans_counts = db.query(Video.transcription_status, func.count(Video.id)).group_by(Video.transcription_status).all()
    total_projects = db.query(IndexedFolder).count()
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    uploads_today = db.query(Video).filter(Video.uploaded_at >= today).count()
    week_ago = datetime.utcnow() - timedelta(days=7)
    uploads_week = db.query(Video).filter(Video.uploaded_at >= week_ago).count()
    tag_counts = db.query(Tag.name, func.count(video_tags.c.tag_id)).join(video_tags).group_by(Tag.name).all()
    tag_counts.sort(key=lambda x: x[1], reverse=True)
    top_tags = [{"name": name, "count": count} for name, count in tag_counts[:5]]
    result = {
        "total_videos": total_videos, "total_projects": total_projects, "total_storage": total_storage,
        "total_storage_formatted": format_file_size(total_storage), "avg_duration": avg_duration,
        "avg_duration_formatted": format_duration(avg_duration), "uploads_today": uploads_today,
        "uploads_week": uploads_week, "top_tags": top_tags,
        "jobs": {"proxy": dict(proxy_counts), "transcription": dict(trans_counts)}
    }
    if current_user.role == "admin":
        users_usage = db.query(User.username, func.count(Video.id).label("video_count"), func.sum(Video.file_size).label("total_size")).outerjoin(Video, User.username == Video.uploaded_by).group_by(User.username).all()
        result["users_usage"] = [{"username": row.username, "video_count": row.video_count, "total_size": row.total_size, "total_size_formatted": format_file_size(row.total_size or 0)} for row in users_usage]
    return result

@app.get("/api/overview")
def overview(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """The Overview page's extras: what came in each month, where the
    properties stand, how much has been edited."""
    now = datetime.utcnow()
    months = []
    y, m = now.year, now.month
    for _ in range(12):
        months.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    months.reverse()
    start = datetime(months[0][0], months[0][1], 1)
    rows = (db.query(Video.created_at, Video.file_size, Video.media_type)
            .filter(Video.is_active.isnot(False), Video.created_at >= start).all())
    buckets = {k: {"files": 0, "bytes": 0, "photos": 0, "videos": 0} for k in months}
    for at, size, kind in rows:
        if not at:
            continue
        b = buckets.get((at.year, at.month))
        if b is None:
            continue
        b["files"] += 1
        b["bytes"] += size or 0
        if kind == "photo":
            b["photos"] += 1
        elif kind == "video":
            b["videos"] += 1
    monthly = [{"month": f"{y:04d}-{m:02d}", **buckets[(y, m)],
                "formatted": format_file_size(buckets[(y, m)]["bytes"])} for y, m in months]
    props = {}
    try:
        import shoots
        for status, n in db.query(shoots.Shoot.status, func.count(shoots.Shoot.id)).group_by(shoots.Shoot.status).all():
            props[status or "shot"] = n
    except Exception:
        pass
    edited = 0
    try:
        import photo_edit
        edited = db.query(photo_edit.PhotoEdit).count()
    except Exception:
        pass
    week = now - timedelta(days=7)
    return {
        "monthly": monthly,
        "properties": props,
        "edited": edited,
        "added_week": db.query(Video).filter(Video.is_active.isnot(False), Video.created_at >= week).count(),
        "photos": db.query(Video).filter(Video.is_active.isnot(False), Video.media_type == "photo").count(),
        "videos": db.query(Video).filter(Video.is_active.isnot(False), Video.media_type == "video").count(),
    }


_BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _clean_name(name: str) -> str:
    name = _BAD_NAME.sub("-", (name or "").strip()).strip(" .")
    return re.sub(r"\s+", " ", name)[:180]


def _rename_on_disk(v: Video, new_name: str) -> str:
    """Rename the file itself (same folder, same extension), and the row.
    Returns the name it got - a clash gets " (2)" rather than overwriting."""
    ext = os.path.splitext(v.filename or "")[1]
    stem = _clean_name(os.path.splitext(new_name)[0] if new_name.lower().endswith(ext.lower()) and ext else new_name)
    if not stem:
        raise ValueError("That name is empty once the characters Windows does not allow are taken out")
    real = resolve_media_path(v.filepath) if v.filepath else None
    final = stem + ext
    if real and os.path.exists(real):
        folder = os.path.dirname(real)
        target = os.path.join(folder, final)
        n = 2
        while os.path.exists(target) and os.path.normcase(os.path.abspath(target)) != os.path.normcase(os.path.abspath(real)):
            final = f"{stem} ({n}){ext}"
            target = os.path.join(folder, final)
            n += 1
        if target != real:
            os.rename(real, target)
            # the stored path keeps the library's own spelling (Windows or WSL)
            sep = "\\" if "\\" in (v.filepath or "") else "/"
            v.filepath = v.filepath.rsplit(sep, 1)[0] + sep + final if sep in (v.filepath or "") else final
    v.filename = final
    return final


@app.post("/api/videos/{video_id}/rename")
def rename_video(video_id: int, request: RenameRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if not request.name:
        raise HTTPException(status_code=400, detail="Name is required")
    try:
        final = _rename_on_disk(video, request.name)
    except (OSError, ValueError) as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Could not rename: {e}")
    db.commit()
    return {"message": "Renamed", "name": final}


class BatchRename(BaseModel):
    video_ids: List[int]
    pattern: str = "{name}"
    start: int = 1
    digits: int = Field(3, ge=1, le=6)
    dry_run: bool = True


@app.post("/api/videos/rename-batch")
def rename_batch(body: BatchRename, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Rename many files from one pattern, in the order given. Words in
    braces are filled in: {name} the current name, {n} a counter, {date} the
    shoot date, {folder} the folder, {property} the property's address.
    dry_run shows what would happen and changes nothing."""
    rows = {v.id: v for v in db.query(Video).filter(Video.id.in_(body.video_ids)).all()}
    prop_cache: dict = {}

    def prop_of(v):
        if v.folder_id in prop_cache:
            return prop_cache[v.folder_id]
        name = ""
        try:
            import shoots
            sh = shoots.shoot_for_path(db, v.filepath)
            name = sh.address if sh else ""
        except Exception:
            pass
        prop_cache[v.folder_id] = name
        return name

    out, n = [], body.start
    for vid in body.video_ids:
        v = rows.get(vid)
        if not v:
            continue
        stem = os.path.splitext(v.filename or "")[0]
        date = (v.shoot_date or v.created_at)
        text = (body.pattern
                .replace("{name}", stem)
                .replace("{n}", str(n).zfill(body.digits))
                .replace("{date}", date.strftime("%Y-%m-%d") if date else "")
                .replace("{folder}", v.folder.name if v.folder else "")
                .replace("{property}", prop_of(v) if "{property}" in body.pattern else ""))
        new = _clean_name(text) + os.path.splitext(v.filename or "")[1]
        out.append({"id": v.id, "old": v.filename, "new": new})
        n += 1
    if body.dry_run:
        return {"preview": out}
    if not permissions.can(current_user.role, permissions.ORGANISE):
        raise HTTPException(status_code=403, detail="Your account cannot move or rename files.")
    done, failed = 0, []
    for r in out:
        v = rows[r["id"]]
        try:
            r["new"] = _rename_on_disk(v, r["new"])
            db.commit()
            done += 1
        except Exception as e:
            db.rollback()
            failed.append(f"{r['old']}: {e}")
    return {"renamed": done, "failed": failed[:20], "preview": out}

@app.get("/api/upload/status/{job_id}")
def get_upload_status(job_id: str, current_user: User = Depends(get_current_user)):
    status = job_manager.active_jobs.get(job_id)
    if not status:
        return {"job_id": job_id, "status": "complete", "progress": 100}
    return status


@app.get("/api/videos/transcript-search")
def transcript_search(q: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    # Same matching as /api/search/spoken - every word, any order, punctuation
    # ignored, "quoted phrases" exact - kept in this response shape because
    # the library search box merges it by video id. Trashed clips no longer
    # appear, and the result is bounded.
    import subclips as _sc
    results = {}
    for r in _sc.spoken_search(db, q, limit=200, per_clip=20):
        results[r["video_id"]] = [{"text": m["text"], "start": m["start"], "end": m["end"]}
                                  for m in r["moments"]]
    return results

@app.get("/api/videos/{video_id}")
def get_video(
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    return {
        "id": video.id,
        "filename": video.filename,
        "filepath": video.filepath,
        "file_size": video.file_size,
        "file_size_formatted": format_file_size(video.file_size),
        "duration": video.duration,
        "duration_formatted": format_duration(video.duration),
        "thumbnail_path": video.thumbnail_path,
        "folder_name": video.folder.name if video.folder else None,
        "folder_id": video.folder_id,
        "tags": [{"id": t.id, "name": t.name} for t in video.tags],
        "created_at": video.created_at.isoformat() if video.created_at else None,
        "uploaded_at": video.uploaded_at.isoformat() if video.uploaded_at else None,
        "uploaded_by": video.uploaded_by,
        "status": video.status or "raw",
        "media_type": video.media_type,
        "transcription": video.transcription,
        "transcription_status": video.transcription_status,
        "proxy_status": video.proxy_status,
        "has_proxy": video.proxy_path is not None and os.path.exists(video.proxy_path),
        "rating": video.rating,
        "label": video.color_label,
        "shoot_date": video.shoot_date.isoformat() if video.shoot_date else None,
        "camera_make": video.camera_make,
        "camera_model": video.camera_model,
        "video_codec": video.video_codec,
        "frame_rate": video.frame_rate,
        "resolution": video.resolution,
        "audio_codec": video.audio_codec,
        "segments": [{
            "text": s.text,
            "start": s.start_time,
            "end": s.end_time
        } for s in sorted(video.segments, key=lambda x: x.start_time)]
    }
@app.post("/api/check-duplicate")
def check_duplicate(payload: dict = Body(...), db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    data = payload
    filename = data.get("filename")
    file_size = data.get("file_size")
    exists = db.query(Video).filter(Video.filename == filename, Video.file_size == file_size).first()
    return {"is_duplicate": exists is not None}

@app.put("/api/folders/{folder_id}")
def rename_folder(folder_id: int, payload: dict = Body(...), db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Rename a folder.

    Folders mirror the directory tree on disk, so for a disk-backed folder this
    renames the real directory too and repoints every clip and subfolder under
    it. Renaming only the label would be silently undone by the next rescan.
    """
    name = (payload or {}).get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    if any(ch in name for ch in ("/", "\\", "..")) or name in (".", ".."):
        raise HTTPException(status_code=400, detail="Name cannot contain slashes or ..")

    folder = db.query(IndexedFolder).filter(IndexedFolder.id == folder_id).first()
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")

    old_path = resolve_media_path(folder.path) if folder.path else None
    renamed_on_disk = False

    if old_path and os.path.isdir(old_path):
        root = UPLOAD_ROOT.resolve()
        current = Path(old_path).resolve()
        if not (current == root or root in current.parents):
            raise HTTPException(status_code=400, detail="Folder is outside the media root")
        new_path = current.parent / name
        if str(new_path) != str(current):
            if new_path.exists():
                raise HTTPException(status_code=409, detail=f"'{name}' already exists there")
            try:
                os.rename(current, new_path)
                renamed_on_disk = True
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Could not rename on disk: {e}")

            old_s, new_s = str(current), str(new_path)
            # repoint this folder, everything nested under it, and every clip
            for f in db.query(IndexedFolder).filter(IndexedFolder.path.like(old_s + "%")).all():
                f.path = new_s + f.path[len(old_s):]
                if f.relative_path:
                    try:
                        f.relative_path = str(Path(f.path).relative_to(root)).replace(os.sep, "/")
                    except Exception:
                        pass
                if f.id != folder.id and f.name:
                    f.name = f.relative_path or f.name
            for v in db.query(Video).filter(Video.filepath.like(old_s + "%")).all():
                v.filepath = new_s + v.filepath[len(old_s):]

    folder.name = name
    db.commit()
    return {"message": "Folder renamed", "renamed_on_disk": renamed_on_disk, "name": name}

@app.post("/api/folders")
def create_folder(payload: dict = Body(...), db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Create a project folder - as a REAL directory on disk.

    This used to insert a row with path="" - a label with no directory behind
    it. Uploads into such a folder fell back to the media root
    (upload_destination_dir treats an empty path as "no folder"), so files
    silently landed somewhere else entirely while the project stayed empty.
    """
    name = (payload or {}).get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="A folder name is required")
    if any(ch in name for ch in ("/", "\\", "..")) or name in (".", ".."):
        raise HTTPException(status_code=400, detail="Name cannot contain slashes or ..")
    name = re.sub(r'[<>:"|?*]', "_", name)

    parent_id = (payload or {}).get("parent_id")
    base = UPLOAD_ROOT
    parent = None
    if parent_id:
        parent = db.query(IndexedFolder).filter(IndexedFolder.id == parent_id).first()
        if parent and parent.path:
            candidate = Path(resolve_media_path(parent.path))
            if candidate.is_dir():
                base = candidate

    target = base / name
    existing = db.query(IndexedFolder).filter(IndexedFolder.path == str(target)).first()
    if existing:
        return {"id": existing.id, "name": existing.name, "path": existing.path, "existed": True}

    try:
        target.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not create the folder on disk: {e}")

    row = ensure_folder_row(db, target)
    if not row:
        raise HTTPException(status_code=500, detail="Folder created on disk but could not be indexed")
    # Remember that a person made this one, so the sidebar keeps showing it
    # while it is still empty (see /api/folders/tree).
    row.created_in_app = datetime.utcnow()
    db.commit()
    return {"id": row.id, "name": row.name, "path": row.path,
            "parent_id": row.parent_id, "created_in_app": True}

@app.post("/api/videos/copy")
def copy_videos_to_folder(payload: dict = Body(...), db: Session = Depends(get_db),
                          current_user: User = Depends(get_current_user)):
    """Copy files into a folder - the paste half of copy and paste.

    A real copy on disk, indexed as new entries, so the original stays where
    it was. Moving is the other endpoint; this one exists because "put these
    in the project as well" is a different intent from "put them there".
    """
    ids = [int(i) for i in (payload or {}).get("video_ids", [])][:2000]
    folder_id = (payload or {}).get("folder_id")
    if not ids:
        raise HTTPException(status_code=400, detail="Nothing to copy")

    folder = db.query(IndexedFolder).filter(IndexedFolder.id == folder_id).first() if folder_id else None
    target = Path(resolve_media_path(folder.path)) if folder and folder.path else UPLOAD_ROOT
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="That folder is not on disk")

    made, failed = [], []
    for vid in ids:
        v = db.query(Video).filter(Video.id == vid).first()
        if not v:
            continue
        src = resolve_media_path(v.filepath)
        if not src or not os.path.exists(src):
            failed.append(f"{v.filename}: not on disk")
            continue
        dest = target / os.path.basename(v.filename or os.path.basename(src))
        stem, ext = os.path.splitext(dest.name)
        n = 2
        while dest.exists():
            dest = target / f"{stem} ({n}){ext}"
            n += 1
        try:
            shutil.copy2(src, dest)
        except Exception as e:
            failed.append(f"{v.filename}: {e}")
            continue

        now = datetime.utcnow()
        row = Video(filename=dest.name, filepath=str(dest),
                    file_size=os.path.getsize(dest), duration=v.duration or 0.0,
                    thumbnail_path=v.thumbnail_path, folder_id=folder.id if folder else None,
                    media_type=v.media_type, status=v.status or "raw",
                    created_at=v.created_at or now, uploaded_at=now,
                    shoot_date=v.shoot_date, uploaded_by="copy")
        db.add(row)
        db.commit()
        made.append(row.id)

    return {"copied": len(made), "ids": made, "failed": failed[:20]}


@app.post("/api/videos/{video_id}/folder")
def move_video_to_folder(video_id: int, payload: dict = Body(...), db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    body = payload
    folder_id = body.get("folder_id")
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    folder = db.query(IndexedFolder).filter(IndexedFolder.id == folder_id).first() if folder_id else None
    try:
        moved = move_media_file(db, video, folder)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Could not move the file: {e}")
    db.commit()
    return {"message": "Video moved", "file_moved": moved, "path": video.filepath}


def _meta_rating(v):
    if v in (None, "", 0, "0"):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="A rating is a whole number from 0 to 5")
    if not 0 <= n <= 5:
        raise HTTPException(status_code=400, detail="A rating is a whole number from 0 to 5")
    return n or None


def _meta_text(limit):
    def check(v):
        if v is None:
            return None
        if not isinstance(v, str) or len(v) > limit:
            raise HTTPException(status_code=400, detail=f"Use plain text of at most {limit} characters")
        return v.strip() or None
    return check


def _meta_status(v):
    s = _meta_text(32)(v)
    return s or "raw"


# field -> how to clean the value. Paths, owners, sizes and job state are not here.
_EDITABLE_METADATA = {
    "rating": _meta_rating,
    "status": _meta_status,
    "camera_make": _meta_text(80),
    "camera_model": _meta_text(80),
}


@app.post("/api/videos/{video_id}/metadata")
def update_video_metadata(video_id: int, payload: dict = Body(...), db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Change the descriptive fields of one file.

    This used to setattr() every key it was given, so any account with edit
    rights could rewrite a row's filepath, owner or trash state and point the
    catalogue at any file on the machine. Only the fields a person is meant to
    edit are accepted now; everything else is refused by name.
    """
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    body = payload if isinstance(payload, dict) else {}
    refused = sorted(k for k in body if k not in _EDITABLE_METADATA)
    if refused:
        raise HTTPException(status_code=400,
                            detail=f"These fields cannot be changed here: {', '.join(refused)}")
    for key, value in body.items():
        setattr(video, key, _EDITABLE_METADATA[key](value))
    db.commit()
    return {"message": "Metadata updated successfully"}

@app.post("/api/videos/{video_id}/rating")
def update_video_rating(video_id: int, payload: dict = Body(...), db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    body = payload
    rating = body.get("rating")
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    video.rating = rating
    db.commit()
    return {"message": "Rating updated successfully"}


@app.post("/api/videos/{video_id}/status")
def update_video_status(video_id: int, payload: dict = Body(...), db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    body = payload
    status = body.get("status")
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    video.status = status
    db.commit()
    return {"message": "Status updated successfully"}


@app.post("/api/videos/{video_id}/tags")
def update_video_tags(video_id: int, payload: dict = Body(...), db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    body = payload
    tag_ids = body.get("tag_ids", [])
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    tags = db.query(Tag).filter(Tag.id.in_(tag_ids)).all()
    video.tags = tags
    db.commit()
    return {"message": "Tags updated successfully"}


@app.delete("/api/videos/{video_id}/tags/{tag_id}")
def remove_video_tag(video_id: int, tag_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    tag = db.query(Tag).filter(Tag.id == tag_id).first()
    if tag and tag in video.tags:
        video.tags.remove(tag)
        db.commit()
    return {"message": "Tag removed successfully"}

# ---------------------------------------------------------------------------
# Endpoints the frontend has always called but which were never implemented.
# Every one of these returned 404/405 before now.
# ---------------------------------------------------------------------------

@app.delete("/api/videos/{video_id}")
def delete_video(video_id: int, delete_file: bool = False, permanent: bool = False,
                 db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Remove a clip from the library.

    The file on disk is left alone by default - these are master files, and a
    misclick must never destroy 20GB of footage. Admins can pass
    delete_file=true to also remove it from disk.
    """
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    real = resolve_media_path(video.filepath)

    # Default: move the file into _Trash and keep the record, so it can be put
    # back. Only an explicit permanent delete destroys anything.
    if delete_file and not permanent:
        if not permissions.can(current_user.role, permissions.TRASH):
            raise HTTPException(status_code=403, detail="Your account cannot delete files.")
        try:
            if real and os.path.exists(real):
                video.original_path = video.original_path or video.filepath
                video.filepath = move_to_trash(real)
            video.is_active = False
            video.trashed_at = datetime.utcnow()
            db.commit()
            return {"message": "Moved to trash", "trashed": True}
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail=f"Could not move to trash: {e}")

    removed_file = False
    if permanent:
        if not permissions.can(current_user.role, permissions.HARD_DELETE):
            raise HTTPException(
                status_code=403,
                detail="Only an administrator can delete files permanently. "
                       "Move them to the trash instead.")
        try:
            if real and os.path.exists(real):
                os.remove(real)
                removed_file = True
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Could not delete file: {e}")

    db.query(TranscriptionSegment).filter(TranscriptionSegment.video_id == video_id).delete()
    db.query(Note).filter(Note.media_id == video_id).delete()
    video.tags = []
    db.delete(video)
    db.commit()
    return {"message": "Deleted", "file_removed": removed_file}


@app.post("/api/videos/{video_id}/transcribe")
def transcribe_video(video_id: int, db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if video.media_type not in ("video", "audio"):
        raise HTTPException(status_code=400, detail="Only video and audio can be transcribed")
    job_manager.add_job(video.id, "transcribe")
    return {"message": "Transcription queued", "job_id": f"transcribe_{video.id}"}


@app.get("/api/videos/{video_id}/suggest-tags")
def suggest_tags(video_id: int, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    """Tag suggestions from the transcript: existing tags that appear in it,
    plus the most frequent meaningful words."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    text = (video.transcription or "")
    if not text:
        segs = db.query(TranscriptionSegment).filter(TranscriptionSegment.video_id == video_id).all()
        text = " ".join(s.text or "" for s in segs)
    if not text.strip():
        return []

    lowered = text.lower()
    suggestions = [t.name for t in db.query(Tag).all() if t.name and t.name.lower() in lowered]

    stop = {"the","and","for","that","this","with","you","your","are","was","have","has","but","not",
            "from","they","there","their","what","when","which","will","would","can","could","just",
            "about","into","out","its","it's","we","i","a","an","of","to","in","is","on","at","as",
            "be","by","so","or","if","do","got","get","going","really","like","one","all","its"}
    words = re.findall(r"[a-z][a-z'-]{3,}", lowered)
    counts = {}
    for w in words:
        if w in stop:
            continue
        counts[w] = counts.get(w, 0) + 1
    top = [w for w, n in sorted(counts.items(), key=lambda kv: -kv[1])[:10] if n > 1]
    for w in top:
        if w not in [s.lower() for s in suggestions]:
            suggestions.append(w)
    return suggestions[:12]


@app.post("/api/videos/{video_id}/notes")
def add_note(video_id: int, payload: dict = Body(...), db: Session = Depends(get_db),
             current_user: User = Depends(get_current_user)):
    content = (payload or {}).get("content", "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Note content is required")
    if not db.query(Video.id).filter(Video.id == video_id).first():
        raise HTTPException(status_code=404, detail="Video not found")
    if len(content) > 5000:
        raise HTTPException(status_code=400, detail="Keep a note under 5000 characters")
    # The author is what lets the bell leave your own notes out of your count.
    note = Note(media_id=video_id, content=content, author=current_user.username,
                created_at=datetime.utcnow())
    db.add(note); db.commit(); db.refresh(note)
    return {"id": note.id, "content": note.content, "author": note.author,
            "created_at": note.created_at.isoformat()}


@app.delete("/api/notes/{note_id}")
def delete_note(note_id: int, db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    note = db.query(Note).filter(Note.id == note_id).first()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    # Your own notes, or anyone's if you are an admin. Notes from before
    # authors were recorded have none, so they fall to an editor or admin.
    mine = note.author and note.author == current_user.username
    if not (mine or current_user.role == "admin"
            or (not note.author and permissions.can(current_user.role, permissions.ORGANISE))):
        raise HTTPException(status_code=403, detail="You can only delete your own notes")
    db.delete(note); db.commit()
    return {"message": "Note deleted"}


@app.delete("/api/tags/{tag_id}")
def delete_tag(tag_id: int, db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    tag = db.query(Tag).filter(Tag.id == tag_id).first()
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    for child in db.query(Tag).filter(Tag.parent_id == tag_id).all():
        child.parent_id = tag.parent_id          # re-parent, don't orphan
    db.execute(video_tags.delete().where(video_tags.c.tag_id == tag_id))
    db.delete(tag); db.commit()
    return {"message": "Tag deleted"}


@app.delete("/api/folders/{folder_id}")
def delete_folder(folder_id: int, delete_files: bool = False, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    """Remove a folder from the library.

    By default the directory and its clips stay on disk and simply become
    unfiled. delete_files=true also removes the directory - admins only.
    """
    folder = db.query(IndexedFolder).filter(IndexedFolder.id == folder_id).first()
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")

    removed_from_disk = False
    if delete_files:
        if current_user.role != "admin":
            raise HTTPException(status_code=403, detail="Only admins can delete files from disk")
        target = resolve_media_path(folder.path) if folder.path else None
        if target and os.path.isdir(target):
            root = UPLOAD_ROOT.resolve()
            current = Path(target).resolve()
            if current == root or root not in current.parents:
                raise HTTPException(status_code=400, detail="Refusing to delete the media root")
            ids = descendant_folder_ids(db, folder_id)
            vids = db.query(Video).filter(Video.folder_id.in_(ids)).all()
            for v in vids:
                db.query(TranscriptionSegment).filter(TranscriptionSegment.video_id == v.id).delete()
                db.query(Note).filter(Note.media_id == v.id).delete()
                v.tags = []
                db.delete(v)
            db.commit()
            try:
                shutil.rmtree(current)
                removed_from_disk = True
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Could not delete folder: {e}")
            for fid in ids:
                sub = db.query(IndexedFolder).filter(IndexedFolder.id == fid).first()
                if sub and sub.id != folder.id:
                    db.delete(sub)
            db.commit()

    if not removed_from_disk:
        db.query(Video).filter(Video.folder_id == folder_id)\
            .update({Video.folder_id: None}, synchronize_session=False)
        db.query(IndexedFolder).filter(IndexedFolder.parent_id == folder_id)\
            .update({IndexedFolder.parent_id: folder.parent_id}, synchronize_session=False)
        db.commit()

    # Bulk delete, NOT db.delete(folder) - the relationship cascade would take
    # every clip in the folder (and its transcript) down with it.
    db.expire_all()
    db.query(IndexedFolder).filter(IndexedFolder.id == folder_id).delete(synchronize_session=False)
    db.commit()
    return {"message": "Folder deleted", "removed_from_disk": removed_from_disk}


COLOR_LABELS = ("red", "yellow", "green", "blue", "purple")


@app.post("/api/videos/bulk")
def bulk_action(payload: dict = Body(...), db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    action = (payload or {}).get("action")
    video_ids = (payload or {}).get("video_ids") or []
    if not action or not video_ids:
        raise HTTPException(status_code=400, detail="action and video_ids are required")

    videos = db.query(Video).filter(Video.id.in_(video_ids)).all()
    affected = 0

    if action == "tag":
        tags = db.query(Tag).filter(Tag.id.in_(payload.get("tag_ids") or [])).all()
        for v in videos:
            for tag in tags:
                if tag not in v.tags:
                    v.tags.append(tag)
            affected += 1
    elif action == "untag":
        tag_ids = set(payload.get("tag_ids") or [])
        for v in videos:
            v.tags = [tg for tg in v.tags if tg.id not in tag_ids]
            affected += 1
    elif action == "move":
        if not permissions.can(current_user.role, permissions.ORGANISE):
            raise HTTPException(status_code=403, detail="Your account cannot move or rename files.")
        folder_id = payload.get("folder_id")
        folder = db.query(IndexedFolder).filter(IndexedFolder.id == folder_id).first() if folder_id else None
        for v in videos:
            try:
                move_media_file(db, v, folder)
                affected += 1
            except Exception as e:
                print(f"Move failed for {v.filename}: {e}")
    elif action == "label":
        label = (payload.get("label") or "").strip().lower() or None
        if label not in (None, *COLOR_LABELS):
            raise HTTPException(status_code=400, detail=f"A label is one of: {', '.join(COLOR_LABELS)}")
        for v in videos:
            v.color_label = label
            affected += 1
    elif action == "status":
        new_status = payload.get("status")
        if not new_status:
            raise HTTPException(status_code=400, detail="status is required")
        for v in videos:
            v.status = new_status
            affected += 1
    elif action == "delete":
        # Bulk delete used to drop the database rows and leave the files on
        # disk - which meant the footage still ate the drive but no longer
        # appeared anywhere, and nothing could put it back. It moves things to
        # the trash instead; emptying the trash is the permanent step, and
        # that is an administrator's to take.
        if not permissions.can(current_user.role, permissions.TRASH):
            raise HTTPException(status_code=403, detail="Your account cannot delete files.")
        for v in videos:
            try:
                real = resolve_media_path(v.filepath)
                if real and os.path.exists(real):
                    v.original_path = v.original_path or v.filepath
                    v.filepath = move_to_trash(real)
                v.is_active = False
                v.trashed_at = datetime.utcnow()
                affected += 1
            except Exception as e:
                print(f"Trash failed for {v.filename}: {e}")
    elif action == "transcribe":
        if not permissions.can(current_user.role, permissions.TRANSCRIBE):
            raise HTTPException(status_code=403, detail="Your account cannot start transcriptions.")
        for v in videos:
            if v.media_type in ("video", "audio"):
                job_manager.add_job(v.id, "transcribe")
                affected += 1
    elif action == "proxy":
        if not permissions.can(current_user.role, permissions.PROXY):
            raise HTTPException(
                status_code=403,
                detail="Only an administrator can start or stop proxy generation.")
        for v in videos:
            if v.media_type == "video":
                job_manager.add_job(v.id, "proxy")
                affected += 1
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    db.commit()
    return {"message": f"{action} applied", "affected": affected}


@app.api_route("/api/videos/jobs/status", methods=["GET", "POST"])
def jobs_status(current_user: User = Depends(get_current_user)):
    """Status of every background job the manager knows about."""
    jobs = []
    for job_id, status in list(job_manager.active_jobs.items()):
        s = dict(status)
        s["in_progress"] = s.get("status") in ("queued", "processing")
        jobs.append(s)

    total = len(jobs)
    done = sum(1 for j in jobs if j.get("status") == "completed")
    failed = sum(1 for j in jobs if j.get("status") == "failed")
    for j in jobs:
        j.setdefault("total", total)
        j.setdefault("completed", done)
        j.setdefault("failed", failed)

    # With a full-library transcribe this list is ~1,100 entries. Sending all of
    # them every few seconds is pointless - the panel shows what is moving.
    running = [j for j in jobs if j["in_progress"]]
    recent = [j for j in jobs if not j["in_progress"]][-20:]
    return (running[:40] + recent)


@app.get("/api/videos/{video_id}/location")
def video_location(video_id: int, db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    """Where this clip actually lives on disk."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    real = resolve_media_path(video.filepath)
    windows_path = real
    try:
        if real and real.startswith("/mnt/"):
            r = subprocess.run(["wslpath", "-w", real], capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                windows_path = r.stdout.strip()
    except Exception:
        pass
    folder = db.query(IndexedFolder).filter(IndexedFolder.id == video.folder_id).first()
    return {
        "path": real,
        "windows_path": windows_path,
        "folder": os.path.dirname(windows_path or real or ""),
        "folder_id": video.folder_id,
        "relative_path": folder.relative_path if folder else None,
        "exists": bool(real and os.path.exists(real)),
    }


@app.post("/api/videos/{video_id}/reveal")
def reveal_in_explorer(video_id: int, db: Session = Depends(get_db),
                       current_user: User = Depends(get_admin_user)):
    """Open Windows Explorer with the file selected.

    This opens on the machine running the SERVER, so it is only useful to
    whoever is sitting at it. The UI only offers it when you are on localhost.
    """
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    real = resolve_media_path(video.filepath)
    if not real or not os.path.exists(real):
        raise HTTPException(status_code=404, detail="File not found on disk")
    try:
        win = real
        if real.startswith("/mnt/"):
            r = subprocess.run(["wslpath", "-w", real], capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                win = r.stdout.strip()
        subprocess.Popen(["explorer.exe", f"/select,{win}"])
        return {"opened": True, "path": win}
    except FileNotFoundError:
        raise HTTPException(status_code=501, detail="Explorer is not reachable from this server")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def move_media_file(db: Session, video: Video, folder):
    """Physically move a clip (and its sidecars) into a folder's directory.

    Setting folder_id alone would leave the file where it was, so the library
    would show it somewhere it isn't. Returns True if the file moved.
    """
    if folder is None or not folder.path:
        video.folder_id = folder.id if folder else None
        return False

    dest_dir = Path(resolve_media_path(folder.path) if folder.path else folder.path)
    if not dest_dir.is_dir():
        video.folder_id = folder.id
        return False

    src = Path(resolve_media_path(video.filepath))
    if not src.exists() or src.parent.resolve() == dest_dir.resolve():
        video.folder_id = folder.id
        return False

    dest = dest_dir / src.name
    stem, ext, n = dest.stem, dest.suffix, 2
    while dest.exists():
        dest = dest.with_name(f"{stem} ({n}){ext}")
        n += 1

    sidecars = []
    if src.parent.exists():
        for sib in src.parent.iterdir():
            if not sib.is_file() or sib.suffix.lower() not in SIDECAR_SUFFIXES:
                continue
            s, m = sib.stem.lower(), src.stem.lower()
            if s == m or (s.startswith(m) and re.match(r'^([._-]|[A-Za-z]?\d{2})$', sib.stem[len(src.stem):])):
                sidecars.append(sib)

    shutil.move(str(src), str(dest))
    for sc in sidecars:
        sd = dest_dir / sc.name
        k = 2
        while sd.exists():
            sd = sd.with_name(f"{sc.stem} ({k}){sc.suffix}")
            k += 1
        try:
            shutil.move(str(sc), str(sd))
        except Exception:
            pass

    video.filepath = str(dest)
    video.folder_id = folder.id
    return True


def resync_folders(db: Session):
    """Make the folder table match what is actually on disk.

    Rows whose path still exists KEEP their id, so anything the UI has selected
    stays valid. Rows for vanished directories go, and every clip is re-pointed
    at the folder its file now lives in.
    """
    skip = {"thumbnails", "proxies", "transcriptions", "_catalog-backup", "_Trash"}
    existing = {f.path: f for f in db.query(IndexedFolder).all()}
    seen, ids = set(), {}

    for current, dirs, files in os.walk(UPLOAD_ROOT):
        dirs[:] = sorted(d for d in dirs if d not in skip and not d.startswith("."))
        rel = os.path.relpath(current, UPLOAD_ROOT).replace(os.sep, "/")
        if rel == ".":
            continue
        parent_rel = os.path.dirname(rel)
        parent_id = ids.get(parent_rel) if parent_rel else None
        row = existing.get(current)
        if row:
            row.name, row.relative_path, row.parent_id = rel, rel, parent_id
        else:
            row = IndexedFolder(name=rel, path=current, relative_path=rel,
                                parent_id=parent_id, added_at=datetime.utcnow())
            db.add(row); db.flush()
        ids[rel] = row.id
        seen.add(current)
    db.commit()

    stale_ids = [row.id for path, row in existing.items() if path not in seen]
    if stale_ids:
        # Detach the clips FIRST, then remove the folder rows with a bulk delete.
        # db.delete(folder) would cascade through IndexedFolder.videos
        # (cascade="all, delete-orphan") and destroy the clips and their
        # transcriptions along with the folder.
        db.query(Video).filter(Video.folder_id.in_(stale_ids))\
            .update({Video.folder_id: None}, synchronize_session=False)
        db.commit()
        db.expire_all()
        db.query(IndexedFolder).filter(IndexedFolder.id.in_(stale_ids))\
            .delete(synchronize_session=False)
    db.commit()

    for v in db.query(Video).filter((Video.is_active == True) | (Video.is_active == None)).all():
        parent = os.path.dirname(v.filepath or "")
        fid = ids.get(os.path.relpath(parent, UPLOAD_ROOT).replace(os.sep, "/")) if parent else None
        if fid and v.folder_id != fid:
            v.folder_id = fid
    db.commit()
    return len(ids)


@app.post("/api/folders/merge")
def merge_folders(payload: dict = Body(...), db: Session = Depends(get_db),
                  current_user: User = Depends(get_admin_user)):
    """Merge several folders into one, on disk.

    Subfolders of the same name are combined - merging three shoot-day folders
    that each hold Drone/ and Videos/ gives you one folder with one Drone/ and
    one Videos/ holding everything. Nothing is overwritten.
    """
    ids = [int(i) for i in (payload or {}).get("folder_ids", [])]
    name = ((payload or {}).get("name") or "").strip()
    if len(ids) < 2:
        raise HTTPException(status_code=400, detail="Pick at least two folders")
    if not name:
        raise HTTPException(status_code=400, detail="A name for the merged folder is required")
    if any(ch in name for ch in ("/", "\\", "..")) or name in (".", ".."):
        raise HTTPException(status_code=400, detail="Name cannot contain slashes or ..")

    folders = db.query(IndexedFolder).filter(IndexedFolder.id.in_(ids)).all()
    if len(folders) != len(ids):
        raise HTTPException(status_code=404, detail="One of those folders no longer exists")

    root = UPLOAD_ROOT.resolve()
    sources = []
    for f in folders:
        real = resolve_media_path(f.path) if f.path else None
        if not real or not os.path.isdir(real):
            raise HTTPException(status_code=404, detail=f"'{f.name}' is not on disk")
        rp = Path(real).resolve()
        if rp == root or root not in rp.parents:
            raise HTTPException(status_code=400, detail="Folder is outside the media root")
        sources.append(rp)

    # merge into the shallowest common parent, so top-level folders stay top-level
    depth = min(len(s.relative_to(root).parts) for s in sources)
    base = sources[0].parents[len(sources[0].relative_to(root).parts) - depth] if depth > 1 else root
    target = base / name

    for s in sources:
        if target == s or target in s.parents:
            continue
        if s in target.parents:
            raise HTTPException(status_code=400, detail="Cannot merge a folder into its own child")

    target.mkdir(parents=True, exist_ok=True)
    moved, skipped = 0, 0

    for src in sources:
        if src == target:
            continue
        for current, dirs, files in os.walk(src):
            rel = os.path.relpath(current, src)
            dest_dir = target if rel == "." else target / rel
            dest_dir.mkdir(parents=True, exist_ok=True)
            for fn in files:
                s_file = Path(current) / fn
                d_file = dest_dir / fn
                stem, ext, n = d_file.stem, d_file.suffix, 2
                while d_file.exists():
                    d_file = d_file.with_name(f"{stem} ({n}){ext}")
                    n += 1
                try:
                    shutil.move(str(s_file), str(d_file))
                    db.query(Video).filter(Video.filepath == str(s_file))\
                        .update({Video.filepath: str(d_file)})
                    moved += 1
                except Exception:
                    skipped += 1
        db.commit()
        try:
            shutil.rmtree(src, ignore_errors=True)
        except Exception:
            pass

    folder_count = resync_folders(db)
    new_row = db.query(IndexedFolder).filter(IndexedFolder.path == str(target)).first()
    return {"merged_into": name, "files_moved": moved, "skipped": skipped,
            "folders": folder_count, "folder_id": new_row.id if new_row else None}


@app.post("/api/folders/resync")
def folders_resync(db: Session = Depends(get_db), current_user: User = Depends(get_admin_user)):
    return {"folders": resync_folders(db)}


@app.get("/api/trash")
def list_trash(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    rows = db.query(Video).filter(Video.is_active == False).order_by(Video.file_size.desc()).all()
    total = sum(v.file_size or 0 for v in rows)
    return {
        "count": len(rows),
        "total_size": total,
        "total_size_formatted": format_file_size(total),
        "items": [{
            "id": v.id, "filename": v.filename, "file_size": v.file_size,
            "file_size_formatted": format_file_size(v.file_size or 0),
            "media_type": v.media_type, "thumbnail_path": v.thumbnail_path,
            "original_path": v.original_path or v.filepath,
            "trashed_at": v.trashed_at.isoformat() + "Z" if v.trashed_at else None,
        } for v in rows],
        "auto_days": _trash_settings().get("auto_days", 0),
    }


@app.post("/api/videos/{video_id}/restore")
def restore_video(video_id: int, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    src = resolve_media_path(video.filepath)
    target = video.original_path
    if target and src and os.path.exists(src):
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            dest = Path(target)
            stem, ext = dest.stem, dest.suffix
            n = 2
            while dest.exists():
                dest = dest.with_name(f"{stem} ({n}){ext}")
                n += 1
            shutil.move(src, str(dest))
            video.filepath = str(dest)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Could not restore: {e}")
    video.is_active = True
    video.original_path = None
    video.trashed_at = None
    db.commit()
    return {"message": "Restored", "path": video.filepath}


def _purge(db, rows) -> dict:
    """Delete these trashed files for good: the file, and its catalogue row."""
    freed, removed, errors = 0, 0, []
    for v in rows:
        real = resolve_media_path(v.filepath)
        try:
            if real and os.path.exists(real):
                freed += os.path.getsize(real)
                os.remove(real)
            db.query(TranscriptionSegment).filter(TranscriptionSegment.video_id == v.id).delete()
            db.query(Note).filter(Note.media_id == v.id).delete()
            v.tags = []
            db.delete(v)
            removed += 1
        except Exception as e:
            errors.append(f"{v.filename}: {e}")
    db.commit()
    # tidy up the empty directory skeleton left behind
    try:
        for dirpath, dirnames, filenames in os.walk(trash_root(), topdown=False):
            if not dirnames and not filenames and Path(dirpath) != trash_root():
                os.rmdir(dirpath)
    except Exception:
        pass
    return {"removed": removed, "freed": freed,
            "freed_formatted": format_file_size(freed), "errors": errors[:10]}


@app.post("/api/trash/empty")
def empty_trash(db: Session = Depends(get_db), current_user: User = Depends(get_admin_user)):
    return _purge(db, db.query(Video).filter(Video.is_active == False).all())


@app.post("/api/trash/restore-all")
def restore_all(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    ids = [v.id for v in db.query(Video.id).filter(Video.is_active == False).all()]
    done, failed = 0, []
    for vid in ids:
        try:
            restore_video(vid, db, current_user)
            done += 1
        except HTTPException as e:
            failed.append(str(e.detail))
    return {"restored": done, "failed": failed[:10]}


TRASH_SETTINGS = Path(__file__).resolve().parent / "trash_settings.json"


def _trash_settings() -> dict:
    try:
        return json.loads(TRASH_SETTINGS.read_text())
    except Exception:
        return {"auto_days": 0}


class TrashSettings(BaseModel):
    auto_days: int = Field(0, ge=0, le=365)   # 0 = never empty by itself


@app.post("/api/trash/settings")
def set_trash_settings(body: TrashSettings, current_user: User = Depends(get_admin_user)):
    TRASH_SETTINGS.write_text(json.dumps(body.model_dump()))
    return body.model_dump()


def _trash_auto_loop():
    """Once an hour: date anything trashed before dates were kept, then delete
    for good what has sat in the trash longer than the setting allows."""
    while True:
        try:
            days = int(_trash_settings().get("auto_days", 0) or 0)
            db = SessionLocal()
            try:
                undated = db.query(Video).filter(Video.is_active == False, Video.trashed_at.is_(None)).all()
                for v in undated:
                    v.trashed_at = datetime.utcnow()
                if undated:
                    db.commit()
                if days > 0:
                    cut = datetime.utcnow() - timedelta(days=days)
                    old = db.query(Video).filter(Video.is_active == False, Video.trashed_at < cut).all()
                    if old:
                        r = _purge(db, old)
                        print(f"Trash: emptied {r['removed']} files older than {days} days ({r['freed_formatted']})", flush=True)
            finally:
                db.close()
        except Exception as e:
            print(f"Trash auto-empty: {e}", flush=True)
        time.sleep(3600)


threading.Thread(target=_trash_auto_loop, daemon=True, name="trash-auto").start()


@app.post("/api/videos/jobs/clear")
def clear_finished_jobs(current_user: User = Depends(get_current_user)):
    """Drop completed/failed entries so the panel only shows live work."""
    removed = 0
    for job_id, status in list(job_manager.active_jobs.items()):
        if status.get("status") in ("completed", "failed"):
            job_manager.active_jobs.pop(job_id, None)
            removed += 1
    return {"cleared": removed, "remaining": len(job_manager.active_jobs)}


@app.get("/api/server-stats")
def server_stats(current_user: User = Depends(get_current_user)):
    out = {"media_root": str(UPLOAD_ROOT)}
    try:
        import psutil
        out["cpu_percent"] = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        out["memory_percent"] = mem.percent
        out["memory_used"] = mem.used
        out["memory_total"] = mem.total
    except Exception:
        pass
    try:
        usage = shutil.disk_usage(str(UPLOAD_ROOT))
        out.update({
            "disk_total": usage.total, "disk_used": usage.used, "disk_free": usage.free,
            "disk_total_formatted": format_file_size(usage.total),
            "disk_free_formatted": format_file_size(usage.free),
            "disk_percent": round(usage.used / usage.total * 100, 1) if usage.total else 0,
        })
    except Exception:
        pass
    out["active_jobs"] = len([j for j in job_manager.active_jobs.values()
                              if j.get("status") in ("queued", "processing")])
    out["scan_running"] = _rescan_state["running"]
    return out


_upload_locks = {}


@app.post("/api/upload/chunk")
def upload_chunk(
    upload_id: str = Form(...),
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),
    file: UploadFile = File(...),
    folder_id: Optional[int] = Form(None),
    relative_path: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Resumable chunked upload. The frontend has had a chunkedUpload() helper
    all along, pointing at this endpoint, which did not exist."""
    access.require_folder(db, current_user, folder_id, "edit")
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "", upload_id)[:64]
    if not safe_id:
        raise HTTPException(status_code=400, detail="Invalid upload_id")

    tmp_dir = UPLOAD_ROOT / ".uploads_tmp" / safe_id
    tmp_dir.mkdir(parents=True, exist_ok=True)

    if not (0 < int(total_chunks) <= 200000 and 0 <= int(chunk_index) < int(total_chunks)):
        raise HTTPException(status_code=400, detail="Bad chunk numbers")

    # Write under a temporary name and rename, so a chunk that is still
    # arriving can never be counted - or read - as if it were complete. Counting
    # part files as they appear made the LAST request to finish assemble the
    # file while other chunks were still being written, and delete the folder
    # under them: the result was a video cut short at a chunk boundary.
    part = tmp_dir / f"{int(chunk_index):06d}.part"
    incoming = tmp_dir / f"{int(chunk_index):06d}.incoming-{uuid.uuid4().hex[:8]}"
    with open(incoming, "wb") as out:
        shutil.copyfileobj(file.file, out, COPY_BUFFER)
    os.replace(incoming, part)

    # Exactly one request may assemble, and only when every chunk is present.
    lock = _upload_locks.setdefault(safe_id, threading.Lock())
    with lock:
        have = {p.name for p in tmp_dir.glob("*.part")}
        complete = all(f"{i:06d}.part" in have for i in range(int(total_chunks)))
        claimed = (tmp_dir / ".assembling").exists()
        if not complete or claimed:
            return {"status": "chunk_received", "received": len(have), "total": total_chunks}
        (tmp_dir / ".assembling").touch()
    received = len(have)

    # last chunk in - assemble
    dest_dir = upload_destination_dir(db, folder_id)
    sub = safe_relative_dir(relative_path)
    if sub:
        dest_dir = dest_dir / sub
    dest_dir.mkdir(parents=True, exist_ok=True)
    if sub:
        row = ensure_folder_row(db, dest_dir)
        if row:
            folder_id = row.id
    final_path = unique_destination(dest_dir, file.filename)
    try:
        expected = sum(p.stat().st_size for p in tmp_dir.glob("*.part"))
        with open(final_path, "wb") as out:
            for chunk_file in sorted(tmp_dir.glob("*.part")):
                with open(chunk_file, "rb") as src:
                    shutil.copyfileobj(src, out, COPY_BUFFER)
        if os.path.getsize(final_path) != expected:
            os.remove(final_path)
            raise HTTPException(status_code=500, detail="Upload was assembled incorrectly - please try again")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        _upload_locks.pop(safe_id, None)

    media_type = get_media_type(final_path.name)
    thumbnail_filename = f"{uuid.uuid4()}.jpg"
    thumb_path = UPLOAD_ROOT / "thumbnails" / thumbnail_filename
    os.makedirs(UPLOAD_ROOT / "thumbnails", exist_ok=True)

    def thumb_bg():
        try:
            if media_type in ('video', 'photo'):
                ok, why = previews.make_thumbnail(str(final_path), str(thumb_path), media_type)
                if not ok:
                    print(f"Thumbnail failed for {final_path}: {why}")
        except Exception as e:
            print(f"Thumbnail generation failed for {final_path}: {e}")

    threading.Thread(target=thumb_bg, daemon=True).start()

    video = Video(
        filename=final_path.name, filepath=str(final_path),
        file_size=os.path.getsize(final_path),
        duration=get_duration_fast(str(final_path)) if media_type == "video" else 0.0,
        thumbnail_path=(f"/thumbnails/{thumbnail_filename}" if media_type in ("video", "photo") else None),
        folder_id=folder_id,
        uploaded_by=current_user.username, uploaded_at=datetime.utcnow(),
        status="raw", media_type=media_type,
    )
    db.add(video)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.query(Video).filter(Video.filepath == str(final_path)).first()
        if existing:
            return {"status": "complete", "video": {"id": existing.id, "filename": existing.filename,
                                                    "media_type": existing.media_type}}
        raise HTTPException(status_code=409, detail="Could not save upload")
    db.refresh(video)
    if video.media_type == "video":
        job_manager.add_job(video.id, "proxy")

    return {"status": "complete", "received": received, "total": total_chunks,
            "video": {"id": video.id, "filename": video.filename, "media_type": video.media_type}}




# ---------------------------------------------------------------------------
# ROLE ENFORCEMENT
# ---------------------------------------------------------------------------
# Three roles: admin (everything), user (can edit), viewer (read-only).
#
# This is middleware rather than a dependency on each endpoint on purpose.
# There are ~35 mutating routes and more get added over time; a per-route check
# is one forgotten decorator away from an unprotected delete. A single choke
# point on the request path cannot be forgotten, and there is one place to
# audit when asking "what can a viewer actually do?".

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# What each role may do now lives in permissions.py, as one table rather than
# an allowlist of exceptions to a single hardcoded role.


def _role_from_request(request: Request) -> Optional[str]:
    """Read the caller's role straight off the bearer token. Returns None when
    there is no usable token - those requests are rejected by the endpoint's
    own dependency, not here."""
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None
    token = auth.split(" ", 1)[1].strip()
    db = SessionLocal()
    try:
        user = get_user_from_token(token, db)
        return user.role
    except Exception:
        return None
    finally:
        db.close()


@app.middleware("http")
async def enforce_role_permissions(request: Request, call_next):
    """The single place roles are enforced.

    Every /api/ request is matched against the rule table in permissions.py.
    Requests with no usable token fall through untouched - those are rejected
    by the endpoint's own dependency, which is what produces a 401 rather than
    a confusing 403.
    """
    path = request.url.path
    if path.startswith("/api/"):
        matched, cap = permissions.required_capability(request.method, path)
        if matched and cap is not None and cap not in permissions.UNIVERSAL_CAPS:
            role = _role_from_request(request)
            if role is not None and not permissions.can(role, cap):
                return JSONResponse(
                    status_code=403,
                    content={"detail": permissions.DENIALS.get(
                        cap, "Your account does not have access to that.")},
                )
    return await call_next(request)

# ---------------------------------------------------------------------------
# FEATURE ROUTES
# Everything below is added ABOVE the React catch-all on purpose: the catch-all
# matches /{full_path} and must stay the last route registered, or it swallows
# every API path declared after it.
# ---------------------------------------------------------------------------

def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """Admin-only dependency. Role checks used to be copy-pasted inline, which
    is how endpoints end up silently unprotected."""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return current_user



# --- roles and account security -------------------------------------------

VALID_ROLES = permissions.VALID_ROLES


@app.post("/api/users/{user_id}/role")
def set_user_role(user_id: int, payload: dict = Body(...),
                  db: Session = Depends(get_db),
                  current_user: User = Depends(require_admin)):
    role = (payload or {}).get("role")
    if role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Role must be one of {', '.join(VALID_ROLES)}")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    # Refuse to remove the last admin: an account system with no administrator
    # cannot be repaired from inside the app.
    if user.role == "admin" and role != "admin":
        admins = db.query(User).filter(User.role == "admin").count()
        if admins <= 1:
            raise HTTPException(status_code=400, detail="That is the only admin account")
    user.role = role
    db.commit()
    return {"status": "ok", "id": user.id, "username": user.username, "role": user.role}


@app.get("/api/security-check")
def security_check(db: Session = Depends(get_db),
                   current_user: User = Depends(require_admin)):
    """Things worth knowing about before this is reachable from the internet."""
    warnings = []

    # Default credentials. The server is port-forwarded, so this is the
    # difference between "my media server" and "everyone's media server".
    for u in db.query(User).all():
        for guess in ("admin123", "password", "admin", "123456", "changeme"):
            try:
                if verify_password(guess, u.hashed_password):
                    warnings.append({
                        "level": "critical",
                        "code": "default_password",
                        "message": f"Account '{u.username}' is still using a default password.",
                        "action": "Change it under Change password.",
                    })
                    break
            except Exception:
                pass

    if os.environ.get("SECRET_KEY", "").startswith("your-super-secret"):
        warnings.append({
            "level": "critical", "code": "default_secret",
            "message": "SECRET_KEY is still the placeholder - anyone who knows it can forge an admin session.",
            "action": "Set SECRET_KEY in the .env file and restart.",
        })

    admins = [u.username for u in db.query(User).filter(User.role == "admin").all()]
    if len(admins) > 1:
        warnings.append({
            "level": "warning", "code": "many_admins",
            "message": f"{len(admins)} accounts have full admin rights ({', '.join(admins)}).",
            "action": "Set collaborators to 'user', or 'viewer' for read-only.",
        })

    return {"warnings": warnings, "checked": datetime.utcnow().isoformat()}





# --- proxy backlog ---------------------------------------------------------

@app.get("/api/proxies/status")
def proxy_status(db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    base = db.query(Video).filter(Video.media_type == "video",
                                  Video.is_active.isnot(False))
    done = base.filter(Video.proxy_status == "completed").count()
    total = base.count()
    return {
        "total": total,
        "completed": done,
        "missing": base.filter(Video.proxy_status.notin_(["completed"])).count(),
        "queued": base.filter(Video.proxy_status.in_(["queued", "processing"])).count(),
        "failed": base.filter(Video.proxy_status == "failed").count(),
        "gave_up": base.filter(Video.proxy_status == "failed",
                               Video.proxy_attempts >= 3).count(),
        "percent": round(done / total * 100) if total else 100,
        "sweeper": __import__("proxy_sweeper").status(),
    }


@app.post("/api/proxies/sweep")
def proxy_sweep_now(current_user: User = Depends(require_admin)):
    """Run the background sweep now rather than waiting for the next pass."""
    import proxy_sweeper
    return proxy_sweeper.sweep(resolve=resolve_media_path)


@app.post("/api/proxies/generate-missing")
def generate_missing_proxies(payload: dict = Body(default={}),
                             db: Session = Depends(get_db),
                             current_user: User = Depends(require_admin)):
    """Queue every video that has no proxy yet.

    This is a long job - it re-encodes the library - so it is never started
    automatically. Proxies are what make the site usable for someone on the
    far end of an upload link, so it is worth running once and leaving.
    """
    limit = (payload or {}).get("limit")
    q = db.query(Video).filter(
        Video.media_type == "video",
        Video.is_active.isnot(False),
        Video.proxy_status.notin_(["completed", "queued", "processing"]),
    )
    if limit:
        q = q.limit(int(limit))
    vids = q.all()
    for v in vids:
        v.proxy_status = "queued"
    db.commit()
    for v in vids:
        job_manager.add_job(v.id, "proxy")
    return {"status": "ok", "queued": len(vids)}


@app.post("/api/proxies/cancel")
def cancel_proxy_backlog(db: Session = Depends(get_db),
                         current_user: User = Depends(require_admin)):
    """Stop the backlog - properly.

    This used to only reset the database column, which did nothing useful:
    the in-memory queue still held every job and the worker carried on
    through all of them. Now the queue is drained and the running encode is
    killed, so pressing Stop actually gives you your machine back.
    """
    result = job_manager.cancel_type("proxy")

    n = db.query(Video).filter(Video.proxy_status.in_(["queued", "processing"])).update(
        {Video.proxy_status: "not_generated", Video.proxy_error: None},
        synchronize_session=False)
    db.commit()
    return {"status": "ok", "cancelled": n,
            "dropped_from_queue": result.get("dropped", 0),
            "running_job_killed": result.get("killed", False)}





# --- storage breakdown -----------------------------------------------------

@app.get("/api/storage/breakdown")
def storage_breakdown(db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    """Where the space is actually going.

    Three questions, in the order they matter:
      1. how full is the drive, and how much of that is this library
      2. which SOURCES are eating it (masters vs renders vs drone)
      3. which folders are the heavy ones

    Average file size is included because it is often the real story - a
    handful of recorder files can outweigh hundreds of phone clips.
    """
    def rows(sql, label_null="unknown"):
        out = []
        for name, count, total in db.execute(text(sql)).fetchall():
            out.append({
                "name": name or label_null,
                "files": count or 0,
                "bytes": int(total or 0),
                "formatted": format_file_size(int(total or 0)),
                "avg_bytes": int((total or 0) / count) if count else 0,
                "avg_formatted": format_file_size(int((total or 0) / count)) if count else "0 B",
            })
        return out

    by_type = rows("""select media_type, count(*), coalesce(sum(file_size),0)
                      from videos where is_active is not 0
                      group by media_type order by 3 desc""")

    by_source = rows("""select coalesce(camera_model, 'Unidentified'), count(*),
                               coalesce(sum(file_size),0)
                        from videos where media_type='video' and is_active is not 0
                        group by 1 order by 3 desc""")

    by_folder = rows("""select coalesce(f.relative_path, f.name), count(*),
                               coalesce(sum(v.file_size),0)
                        from videos v join indexed_folders f on f.id = v.folder_id
                        where v.is_active is not 0
                        group by 1 order by 3 desc limit 12""")

    library_bytes = int(db.query(func.coalesce(func.sum(Video.file_size), 0))
                          .filter(Video.is_active.isnot(False)).scalar() or 0)

    # The drive itself, not just what we catalogued.
    disk = {}
    try:
        usage = shutil.disk_usage(str(UPLOAD_ROOT))
        other = max(0, usage.used - library_bytes)
        disk = {
            "total": usage.total, "used": usage.used, "free": usage.free,
            "total_formatted": format_file_size(usage.total),
            "used_formatted": format_file_size(usage.used),
            "free_formatted": format_file_size(usage.free),
            "percent_used": round(usage.used / usage.total * 100) if usage.total else 0,
            "library_bytes": library_bytes,
            "library_formatted": format_file_size(library_bytes),
            "library_percent": round(library_bytes / usage.total * 100, 1) if usage.total else 0,
            "other_bytes": other,
            "other_formatted": format_file_size(other),
            "path": str(UPLOAD_ROOT),
        }
    except Exception as e:
        disk = {"error": str(e), "library_bytes": library_bytes,
                "library_formatted": format_file_size(library_bytes)}

    # Space that could be recovered without losing anything.
    reclaimable = 0
    try:
        hashes = {}
        for v in db.query(Video).filter(
                Video.file_hash.isnot(None),
                Video.file_hash.notin_(["missing", "error"]),
                Video.is_active.isnot(False)).all():
            hashes.setdefault(v.file_hash, []).append(v.file_size or 0)
        reclaimable = sum(sizes[0] * (len(sizes) - 1)
                          for sizes in hashes.values() if len(sizes) > 1)
    except Exception:
        pass

    proxy_bytes = 0
    try:
        pdir = UPLOAD_ROOT / "proxies"
        if pdir.is_dir():
            proxy_bytes = sum(f.stat().st_size for f in pdir.glob("*") if f.is_file())
    except Exception:
        pass

    return {
        "disk": disk,
        "by_type": by_type,
        "by_source": by_source,
        "by_folder": by_folder,
        "reclaimable": reclaimable,
        "reclaimable_formatted": format_file_size(reclaimable),
        "proxy_bytes": proxy_bytes,
        "proxy_formatted": format_file_size(proxy_bytes),
        "total_files": sum(r["files"] for r in by_type),
    }


# --- updates ---------------------------------------------------------------

_update_state = {"checking": False, "last": None, "downloading": False,
                 "message": "", "staged": None}


def _refresh_update_check():
    import updater
    _update_state["checking"] = True
    try:
        _update_state["last"] = updater.check()
    except Exception as e:
        _update_state["last"] = {"ok": False, "error": str(e)}
    finally:
        _update_state["checking"] = False
    return _update_state["last"]


@app.get("/api/updates/status")
def update_status(current_user: User = Depends(get_current_user)):
    import updater
    return {
        "current_version": updater.current_version(),
        "settings": updater.settings(),
        "checking": _update_state["checking"],
        "downloading": _update_state["downloading"],
        "message": _update_state["message"],
        "staged_version": updater.staged_version() if updater.has_staged() else None,
        "last_check": _update_state["last"],
    }


@app.post("/api/updates/check")
def update_check(current_user: User = Depends(require_admin)):
    return _refresh_update_check()


@app.post("/api/updates/settings")
def update_settings(payload: dict = Body(...),
                    current_user: User = Depends(require_admin)):
    import updater
    try:
        return updater.save_settings(
            auto_check=payload.get("auto_check"),
            auto_apply=payload.get("auto_apply"),
            owner=(payload.get("owner") or "").strip() or None,
            repo=(payload.get("repo") or "").strip() or None,
            check_minutes=payload.get("check_minutes"),
        )
    except updater.SettingsRefused as e:
        raise HTTPException(status_code=403, detail=str(e))


@app.post("/api/updates/download")
def update_download(current_user: User = Depends(require_admin)):
    """Fetch and stage the update. Nothing is replaced until a restart."""
    import updater
    if _update_state["downloading"]:
        raise HTTPException(status_code=409, detail="Already downloading")

    def work():
        _update_state.update({"downloading": True, "message": "Downloading…"})
        try:
            res = updater.download(_update_state.get("last"))
            _update_state["message"] = (
                f"Version {res.get('version')} ready — restart to apply"
                if res.get("ok") else f"Download failed: {res.get('error')}"
            )
            _update_state["staged"] = res
        except Exception as e:
            _update_state["message"] = f"Download failed: {e}"
        finally:
            _update_state["downloading"] = False

    threading.Thread(target=work, daemon=True, name="update-download").start()
    return {"status": "started"}


@app.post("/api/updates/apply")
def update_apply(current_user: User = Depends(require_admin)):
    """Restart into the staged update.

    Exiting with 42 is the signal start.sh watches for: it applies the staged
    files while nothing is running, then starts the app again. Replacing a
    running program's own files is the thing this deliberately avoids.
    """
    import updater
    if not updater.has_staged():
        raise HTTPException(status_code=400, detail="No update has been downloaded yet")

    version = updater.staged_version()

    def bye():
        time.sleep(1.5)          # let this response reach the browser first
        os._exit(42)

    threading.Thread(target=bye, daemon=True).start()
    return {"status": "restarting", "version": version,
            "message": "Applying the update and restarting. This page will "
                       "reconnect in about a minute."}


def _start_update_watcher():
    """Check for updates on a timer, and optionally stage them."""
    import updater

    def loop():
        time.sleep(30)           # let the app finish starting
        while True:
            try:
                s = updater.settings()
                if s["auto_check"] and s["owner"]:
                    info = _refresh_update_check()
                    if (info.get("update_available") and s["auto_apply"]
                            and not updater.has_staged()):
                        # Download only. Applying still waits for a restart,
                        # so an update never interrupts an upload or a render.
                        updater.download(info)
                        _update_state["message"] = (
                            f"Version {info.get('latest')} downloaded — "
                            f"will be applied on the next restart")
                minutes = max(10, int(updater.settings().get("check_minutes", 60)))
            except Exception:
                minutes = 60
            time.sleep(minutes * 60)

    threading.Thread(target=loop, daemon=True, name="update-watcher").start()


# --- auto-tagging ----------------------------------------------------------

_tagging_state = {"running": False, "message": "idle", "stats": None,
                  "started_at": None, "finished_at": None}


@app.get("/api/tags/overview")
def tag_overview(db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    """What tags exist, how much each holds, and where they came from."""
    from transcript_tags import OWNED as TRANSCRIPT_TAGS
    try:
        import tag_rules
        TRANSCRIPT_TAGS = set(TRANSCRIPT_TAGS) | {r.tag for r in db.query(tag_rules.CustomTagRule).all()}
    except Exception:
        pass
    try:
        from enrich import CAMERA_TAG_BY_MAKE
        camera_tags = set(CAMERA_TAG_BY_MAKE.values())
    except Exception:
        camera_tags = set()

    counts = dict(
        db.query(video_tags.c.tag_id, func.count(video_tags.c.video_id))
          .group_by(video_tags.c.tag_id).all()
    )

    STAGE = {"raw", "graded", "edited", "rendered", "draft", "proxy"}
    out = []
    for t in db.query(Tag).all():
        name = (t.name or "")
        low = name.lower()
        if low in TRANSCRIPT_TAGS:
            source = "spoken"
        elif low in camera_tags:
            source = "camera"
        elif low in STAGE:
            source = "stage"
        else:
            source = "folder"
        out.append({"id": t.id, "name": name,
                    "count": counts.get(t.id, 0), "source": source})

    out.sort(key=lambda x: (-x["count"], x["name"].lower()))

    total = db.query(Video).count()
    tagged = db.query(func.count(func.distinct(video_tags.c.video_id))).scalar() or 0
    with_transcript = db.query(Video).filter(Video.transcription.isnot(None)).count()

    return {
        "tags": out,
        "total_tags": len(out),
        "total_assignments": sum(counts.values()),
        "files_total": total,
        "files_tagged": tagged,
        "files_untagged": max(0, total - tagged),
        "files_with_transcript": with_transcript,
        "running": _tagging_state["running"],
        "message": _tagging_state["message"],
        "last_stats": _tagging_state["stats"],
    }


@app.get("/api/tags/rules")
def tag_rules(current_user: User = Depends(get_current_user)):
    """The rules behind the tags, so they are not a black box."""
    rules = {"spoken": [], "camera": [], "folder": []}
    try:
        from transcript_tags import RULES as T_RULES
        rules["spoken"] = [{"tag": n, "matches": p} for n, p, _ in T_RULES]
    except Exception:
        pass
    try:
        from enrich import CAMERA_TAG_BY_MAKE, SOURCE_RULES, STAGE_RULES
        rules["camera"] = [{"make": k, "tag": v} for k, v in CAMERA_TAG_BY_MAKE.items()]
        rules["folder"] = ([{"tag": n, "kind": "source"} for n, _, _ in SOURCE_RULES]
                           + [{"tag": n, "kind": "stage"} for n, _, _ in STAGE_RULES])
    except Exception:
        pass
    return rules


@app.post("/api/tags/auto-generate")
def auto_generate_tags(payload: dict = Body(default={}),
                       current_user: User = Depends(require_admin)):
    """Run the tagging pass over the whole library, in the background."""
    if _tagging_state["running"]:
        raise HTTPException(status_code=409, detail="Tagging is already running")

    include_transcripts = bool((payload or {}).get("include_transcripts", True))

    def work():
        import enrich
        _tagging_state.update({"running": True, "message": "starting",
                               "started_at": datetime.utcnow().isoformat(),
                               "finished_at": None})
        try:
            stats = enrich.run_tagging(
                progress=lambda m: _tagging_state.update({"message": m}),
                include_transcripts=include_transcripts,
            )
            _tagging_state["stats"] = stats
            _tagging_state["message"] = (
                f"Tagged {stats['tagged']} files, {stats['tags_created']} new tags"
            )
        except Exception as e:
            _tagging_state["message"] = f"failed: {e}"
        finally:
            _tagging_state["running"] = False
            _tagging_state["finished_at"] = datetime.utcnow().isoformat()

    threading.Thread(target=work, daemon=True, name="auto-tag").start()
    return {"status": "started"}


@app.delete("/api/tags/{tag_id}/assignments")
def clear_tag_assignments(tag_id: int, db: Session = Depends(get_db),
                          current_user: User = Depends(require_admin)):
    """Unhook a tag from everything without deleting the tag itself.

    Useful when a rule turns out to be too eager: clear it, fix the rule,
    run the pass again.
    """
    tag = db.query(Tag).filter(Tag.id == tag_id).first()
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    n = len(tag.videos)
    tag.videos.clear()
    db.commit()
    return {"status": "ok", "cleared": n, "tag": tag.name}


# --- duplicate detection ---------------------------------------------------


@app.get("/api/videos/{video_id}/tag-moments")
def tag_moments(video_id: int, tags: str = "", db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    """Where in this clip the given tags are actually talked about.

    The tags were derived from the transcript in the first place, so the same
    patterns can point back at the moment that caused them. Filtering to
    "pool" and then opening a four-minute walkthrough at 00:00 makes you hunt
    for the bit you asked for; this hands you the timecode.
    """
    from database import TranscriptionSegment
    try:
        from transcript_tags import COMPILED
    except Exception:
        return {"moments": []}

    wanted = [t.strip().lower() for t in tags.split(",") if t.strip()]
    if not wanted:
        return {"moments": []}

    patterns = [(name, rx) for name, rx, _ in COMPILED if name.lower() in wanted]
    if not patterns:
        return {"moments": []}

    segments = (db.query(TranscriptionSegment)
                  .filter(TranscriptionSegment.video_id == video_id)
                  .order_by(TranscriptionSegment.start_time)
                  .all())

    moments = []
    for seg in segments:
        text = seg.text or ""
        hit = [name for name, rx in patterns if rx.search(text)]
        if hit:
            moments.append({
                "start": seg.start_time,
                "end": seg.end_time,
                "text": text.strip(),
                "tags": hit,
            })
    return {"moments": moments, "count": len(moments)}


@app.get("/api/duplicates")
def list_duplicates(db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    """Groups of files whose content matches. Nothing is deleted here - this
    only reports, because "looks like a duplicate" and "is safe to delete" are
    different claims and only you can make the second one."""
    rows = db.query(Video).filter(
        Video.file_hash.isnot(None),
        Video.file_hash.notin_(["missing", "error"]),
        Video.is_active.isnot(False),
    ).all()

    by_hash = {}
    for v in rows:
        by_hash.setdefault(v.file_hash, []).append(v)

    groups, reclaimable = [], 0
    for h, vs in by_hash.items():
        if len(vs) < 2:
            continue
        size = vs[0].file_size or 0
        waste = size * (len(vs) - 1)
        reclaimable += waste
        folders = {}
        for v in vs:
            f = db.query(IndexedFolder).filter(IndexedFolder.id == v.folder_id).first()
            folders[v.id] = f.relative_path if f and f.relative_path else (f.name if f else "")
        groups.append({
            "hash": h,
            "size": size,
            "size_formatted": format_file_size(size),
            "reclaimable": waste,
            "reclaimable_formatted": format_file_size(waste),
            "verified": h.startswith("f:"),
            "files": [{
                "id": v.id, "filename": v.filename, "filepath": v.filepath,
                "folder": folders.get(v.id, ""), "thumbnail_path": v.thumbnail_path,
                "uploaded_at": v.uploaded_at.isoformat() if v.uploaded_at else None,
                "media_type": v.media_type,
            } for v in sorted(vs, key=lambda x: x.id)],
        })

    groups.sort(key=lambda g: -g["reclaimable"])
    unhashed = db.query(Video).filter(Video.file_hash.is_(None),
                                      Video.is_active.isnot(False)).count()
    return {
        "groups": groups,
        "group_count": len(groups),
        "reclaimable": reclaimable,
        "reclaimable_formatted": format_file_size(reclaimable),
        "unhashed": unhashed,
    }



def _dup_groups(db):
    """Duplicate groups as {hash: [Video, ...]}, active files only."""
    rows = db.query(Video).filter(
        Video.file_hash.isnot(None),
        Video.file_hash.notin_(["missing", "error"]),
        Video.is_active.isnot(False),
    ).all()
    by = {}
    for v in rows:
        by.setdefault(v.file_hash, []).append(v)
    return {h: vs for h, vs in by.items() if len(vs) > 1}


@app.get("/api/duplicates/plans")
def duplicate_plans(db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    """Group the duplicate groups by which FOLDERS are involved.

    Reviewing 202 groups one at a time is unreasonable when they are not
    random: the same two folders duplicate each other over and over, because
    of how an export or a copy was done once. Deciding "keep this folder, drop
    that one" resolves a hundred groups in one go, while still leaving every
    individual group open to a different choice.
    """
    folders = {f.id: (f.relative_path or f.name or "") for f in db.query(IndexedFolder).all()}
    groups = _dup_groups(db)

    plans = {}
    for h, vs in groups.items():
        names = tuple(sorted({folders.get(v.folder_id, "") for v in vs}))
        key = " | ".join(names)
        p = plans.setdefault(key, {
            "key": key, "folders": list(names), "groups": 0, "files": 0,
            "reclaimable": 0, "single_folder": len(names) == 1,
            "hashes": [],
        })
        p["groups"] += 1
        p["files"] += len(vs)
        p["reclaimable"] += (vs[0].file_size or 0) * (len(vs) - 1)
        p["hashes"].append(h)

    out = sorted(plans.values(), key=lambda x: -x["reclaimable"])
    for p in out:
        p["reclaimable_formatted"] = format_file_size(p["reclaimable"])
    return {"plans": out, "total_groups": len(groups)}


@app.post("/api/duplicates/preview")
def preview_duplicate_rule(payload: dict = Body(...), db: Session = Depends(get_db),
                           current_user: User = Depends(get_current_user)):
    """Work out exactly what a rule would delete - without deleting anything.

    Nobody should press a button that removes 87 files without seeing the list
    first.
    """
    folders = {f.id: (f.relative_path or f.name or "") for f in db.query(IndexedFolder).all()}
    groups = _dup_groups(db)

    keep_folder = payload.get("keep_folder")
    hashes = set(payload.get("hashes") or [])
    rule = payload.get("rule")          # "prefer_folder" | "drop_numbered" | "oldest"

    def is_numbered(v):
        """True for the "Clip (2).mov" kind of copy Windows and our own
        collision-avoidance produce."""
        return bool(re.search(r"\(\d+\)\s*$", os.path.splitext(v.filename or "")[0]))

    def best_of(candidates):
        """Which copy to keep, given a choice.

        Prefers a clean filename over a numbered one, then the shorter name,
        then the earliest indexed. Without this, a group holding both
        "Clip.mov" and "Clip (2).mov" in the SAME folder could keep the
        numbered one and delete the clean original - technically correct,
        obviously wrong.
        """
        return sorted(candidates, key=lambda v: (is_numbered(v), len(v.filename or ""), v.id))[0]

    to_delete, kept, skipped = [], 0, 0
    for h, vs in groups.items():
        if hashes and h not in hashes:
            continue

        survivor = None
        doomed = None

        if rule == "drop_numbered":
            numbered = [v for v in vs if is_numbered(v)]
            plain = [v for v in vs if not is_numbered(v)]
            # Only meaningful when there is BOTH a numbered copy and a clean
            # one. Applying it to a group of clean names would quietly turn
            # into "delete all but one", which is not what the rule says.
            if numbered and plain:
                survivor = best_of(plain)
                doomed = [v for v in vs if v.id != survivor.id]
        elif rule == "oldest":
            survivor = sorted(vs, key=lambda v: v.id)[0]
            doomed = [v for v in vs if v.id != survivor.id]
        else:  # prefer_folder
            matches = [v for v in vs if folders.get(v.folder_id, "") == keep_folder]
            if matches:
                survivor = best_of(matches)
                doomed = [v for v in vs if v.id != survivor.id]

        # Never resolve a group we cannot decide safely - leave it for manual
        # review rather than guessing which copy matters.
        if survivor is None or not doomed:
            skipped += 1
            continue

        kept += 1
        for v in doomed:
            to_delete.append({
                "id": v.id, "filename": v.filename,
                "folder": folders.get(v.folder_id, ""),
                "size": v.file_size or 0,
                "keeping": survivor.filename,
                "keeping_folder": folders.get(survivor.folder_id, ""),
            })

    total = sum(x["size"] for x in to_delete)
    return {
        "delete_count": len(to_delete),
        "keep_count": kept,
        "skipped": skipped,
        "reclaimable": total,
        "reclaimable_formatted": format_file_size(total),
        "files": to_delete[:400],
        "truncated": len(to_delete) > 400,
    }


@app.post("/api/duplicates/resolve")
def resolve_duplicates(payload: dict = Body(...), db: Session = Depends(get_db),
                       current_user: User = Depends(require_admin)):
    """Delete the ids given, but only ones that are genuinely redundant.

    Every id is re-checked server-side: it must belong to a group that still
    has more than one copy, and at least one copy of that content must survive
    the request. A UI bug, a stale page or a double-click must not be able to
    delete the last copy of anything.
    """
    ids = payload.get("ids") or []
    permanent = bool(payload.get("permanent", False))
    if not ids:
        raise HTTPException(status_code=400, detail="Nothing selected")

    groups = _dup_groups(db)
    by_id = {v.id: (h, vs) for h, vs in groups.items() for v in vs}

    doomed, refused = [], []
    surviving = {h: {v.id for v in vs} for h, vs in groups.items()}

    for vid in ids:
        entry = by_id.get(vid)
        if not entry:
            refused.append({"id": vid, "reason": "not part of a duplicate group"})
            continue
        h, _ = entry
        if len(surviving[h]) <= 1:
            refused.append({"id": vid, "reason": "would delete the last copy"})
            continue
        surviving[h].discard(vid)
        doomed.append(vid)

    freed = 0
    deleted = 0
    for vid in doomed:
        v = db.query(Video).filter(Video.id == vid).first()
        if not v:
            continue
        freed += v.file_size or 0
        try:
            real = resolve_media_path(v.filepath)
            if permanent:
                if real and os.path.exists(real):
                    os.remove(real)
                db.delete(v)
            else:
                # Same path the single-file delete takes: move to _Trash and
                # keep the row, so Restore works and nothing is unrecoverable.
                if real and os.path.exists(real):
                    v.original_path = v.original_path or v.filepath
                    v.filepath = move_to_trash(real)
                v.is_active = False
                v.trashed_at = datetime.utcnow()
            deleted += 1
        except Exception as e:
            refused.append({"id": vid, "reason": str(e)})
    db.commit()

    return {
        "status": "ok",
        "deleted": deleted,
        "refused": refused,
        "freed": freed,
        "freed_formatted": format_file_size(freed),
        "permanent": permanent,
    }


@app.post("/api/duplicates/scan")
def scan_duplicates(payload: dict = Body(default={}),
                    current_user: User = Depends(require_admin)):
    """Hash anything that has not been hashed yet. Runs on a worker thread so
    the request returns immediately."""
    import threading

    def work():
        try:
            import dedupe
            dedupe.scan(limit=(payload or {}).get("limit"))
        except Exception as e:
            print(f"  [dedupe] scan failed: {e}")

    threading.Thread(target=work, daemon=True, name="dedupe-scan").start()
    return {"status": "started"}


# --- client share links ----------------------------------------------------
# A share is a long random token that grants read access to a folder or a fixed
# set of clips, with no account. Everything a share exposes is resolved
# server-side from the token: the viewer never sends an id we then trust.

def _share_videos(db: Session, share: Share):
    """The exact set of clips a share grants access to."""
    q = db.query(Video).filter(Video.is_active.isnot(False))
    if share.video_ids:
        try:
            ids = [int(x) for x in share.video_ids.split(",") if x.strip()]
        except ValueError:
            ids = []
        if not ids:
            return []
        return q.filter(Video.id.in_(ids)).all()
    if share.folder_id is not None:
        if share.include_subfolders:
            ids = descendant_folder_ids(db, share.folder_id)
        else:
            ids = [share.folder_id]
        return q.filter(Video.folder_id.in_(ids)).all()
    return []


_share_fails = {}             # (ip, share token) -> [failure timestamps]
_SHARE_MAX_FAILS = 10


def _share_state(db: Session, token: str, password: Optional[str] = None) -> Share:
    """Resolve a token to a live share, or refuse with a reason."""
    share = db.query(Share).filter(Share.token == token).first()
    if not share or share.revoked:
        raise HTTPException(status_code=404, detail="This link is no longer available")
    if share.expires_at and datetime.utcnow() > share.expires_at:
        raise HTTPException(status_code=410, detail="This link has expired")
    if share.password_hash and not _staff_preview.get():
        if not password:
            raise HTTPException(status_code=401, detail="Password required")
        # A wrong guess counts against this address on this link. Without a
        # limit a short share password falls to a script in minutes.
        key = (_request_ip.get(), token)
        now = datetime.utcnow().timestamp()
        fails = [t for t in _share_fails.get(key, []) if now - t < _LOGIN_WINDOW]
        if len(fails) >= _SHARE_MAX_FAILS:
            _share_fails[key] = fails
            raise HTTPException(status_code=429,
                                detail="Too many wrong passwords. Try again later.")
        if not verify_password(password, share.password_hash):
            fails.append(now)
            _share_fails[key] = fails
            if len(_share_fails) > 5000:
                for k in [k for k, v in _share_fails.items() if not v or v[-1] < now - _LOGIN_WINDOW]:
                    _share_fails.pop(k, None)
            raise HTTPException(status_code=401, detail="Password required")
        _share_fails.pop(key, None)
    return share


def _share_open(db: Session, token: str, password: Optional[str] = None) -> Share:
    """A live share whose viewer has agreed to the studio's terms (when it asks for them):
    what every route that shows or takes something of the portal goes through."""
    share = _share_state(db, token, password)
    if not _staff_preview.get():
        import business
        if business.terms_pending(db, share):
            raise HTTPException(status_code=403, detail="Agree to the terms first.")
    return share


def _intake_folder(db: Session, title: str):
    """A holding folder for one upload link, under _Incoming.

    Kept out of the library's own folder tree by the leading underscore, the
    same convention _Trash uses, so it sorts to the top and is obviously not
    a project.
    """
    safe = re.sub(r'[<>:"|?*/\\]', "_", (title or "Uploads")).strip() or "Uploads"
    base = UPLOAD_ROOT / "_Incoming"
    target = base / safe
    n = 2
    while target.exists() and db.query(IndexedFolder).filter(
            IndexedFolder.path == str(target)).first():
        target = base / f"{safe} ({n})"
        n += 1
    target.mkdir(parents=True, exist_ok=True)
    row = ensure_folder_row(db, target)
    return row.id if row else None


def _share_folder_name(db: Session, share: Share) -> Optional[str]:
    """Where an upload link drops files, in words the viewer can recognise.
    Share has no folder relationship, so this is a plain lookup rather than
    an attribute that would quietly be None."""
    if not share.folder_id:
        return None
    row = db.query(IndexedFolder).filter(IndexedFolder.id == share.folder_id).first()
    return row.name if row else None


UNPAID_WORDS = "Downloads open once payment is received."


def _unpaid(db: Session, share: Share) -> bool:
    """The property this portal delivers is not paid for yet: watermarked pictures, no downloads."""
    import shoots as _shoots
    try:
        return _shoots.unpaid(db, share.shoot_id)
    except Exception:
        return False


def _marked(db: Session, share: Share) -> bool:
    return bool(share.watermark_previews) or _unpaid(db, share)


def _share_payload(db: Session, share: Share):
    import business
    vids = _share_videos(db, share)
    unpaid = _unpaid(db, share)
    terms = business.portal_terms(db, share)
    # nothing of the portal until its terms are agreed (the studio's own preview sees it all)
    locked = bool(terms and not terms["agreed"]) and not _staff_preview.get()
    count = len(vids)
    if locked:
        vids = []
    return {
        "terms": terms,
        "terms_pending": locked,
        "pay": business.portal_pay(db, share),
        "title": share.title or "Shared media",
        "message": share.message,
        "allow_download": bool(share.allow_download) and not unpaid,
        "unpaid": unpaid,
        "allow_selects": bool(share.allow_selects),
        "allow_upload": bool(share.allow_upload),
        "kind": share.kind or ("both" if share.allow_upload else "send"),
        "confirmed_at": share.confirmed_at.isoformat() if share.confirmed_at else None,
        "allow_zip": True if share.allow_zip is None else bool(share.allow_zip),
        "intake_folder_id": share.intake_folder_id,
        "upload_folder": _share_folder_name(db, share),
        "expires_at": share.expires_at.isoformat() if share.expires_at else None,
        "count": count,
        "videos": [{
            "id": v.id,
            "filename": v.filename,
            "media_type": v.media_type,
            "duration": v.duration,
            "thumbnail_path": v.thumbnail_path,
            "file_size_formatted": format_file_size(v.file_size or 0),
            "resolution": v.resolution,
            "has_proxy": v.proxy_status == "completed",
        } for v in vids],
    }


def _wm_name_or_400(name):
    name = os.path.basename((name or "").strip())
    if not name:
        return None
    if not delivery.watermark_exists(name):
        raise HTTPException(status_code=400, detail="There is no watermark with that name.")
    return name


@app.post("/api/shares")
def create_share(payload: dict = Body(...), db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    if current_user.role == "viewer":
        raise HTTPException(status_code=403, detail="Your account has view-only access.")

    folder_id = payload.get("folder_id")
    video_ids = payload.get("video_ids") or []
    # A receiving portal is legitimate with neither: it starts empty and fills
    # up from whoever you send it to, so it needs no folder and no selection.
    receiving = (bool(payload.get("allow_upload"))
                 or (payload.get("kind") or "").strip().lower() == "receive")
    if folder_id is None and not video_ids and not receiving:
        raise HTTPException(status_code=400, detail="Share a folder or a selection")
    # An upload link gets a holding folder of its own rather than writing
    # straight into the library. Whatever a phone sends stays there until
    # someone at a desk looks at it and decides where it belongs - which is
    # the difference between an inbox and a mess.
    # A portal is one of three things. Older callers only sent allow_upload /
    # allow_download, so infer the kind when it is not stated.
    kind = (payload.get("kind") or "").strip().lower()
    if kind not in ("send", "receive", "both"):
        if payload.get("allow_upload") and (folder_id is None and not video_ids):
            kind = "receive"
        elif payload.get("allow_upload"):
            kind = "both"
        else:
            kind = "send"
    # A receiving portal shows nothing of the library, whatever else was asked
    # for - that is the whole point of it.
    if kind == "receive":
        payload = {**payload, "allow_upload": True,
                   "allow_download": False, "allow_selects": False}

    intake_id = None
    if payload.get("allow_upload"):
        intake_id = _intake_folder(db, payload.get("title") or "Uploads")
        if folder_id is None:
            folder_id = intake_id

    days = payload.get("expires_days")
    expires = None
    if days:
        try:
            expires = datetime.utcnow() + timedelta(days=int(days))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="expires_days must be a number")

    pw = (payload.get("password") or "").strip()

    share = Share(
        # 32 url-safe bytes: not guessable by brute force, which matters
        # because the token IS the access control.
        token=secrets.token_urlsafe(32),
        title=payload.get("title") or None,
        message=payload.get("message") or None,
        folder_id=folder_id,
        include_subfolders=bool(payload.get("include_subfolders", True)),
        video_ids=",".join(str(int(v)) for v in video_ids) if video_ids else None,
        password_hash=get_password_hash(pw) if pw else None,
        expires_at=expires,
        allow_download=bool(payload.get("allow_download", False)),
        allow_selects=bool(payload.get("allow_selects", True)),
        allow_upload=bool(payload.get("allow_upload", False)),
        kind=kind,
        # Default on for a desktop audience; a portal meant for a phone is
        # better without it, because a zip lands in Files, not the camera roll.
        allow_zip=bool(payload.get("allow_zip", True)),
        watermark_previews=bool(payload.get("watermark_previews", False)),
        watermark_name=_wm_name_or_400(payload.get("watermark_name")),
        intake_folder_id=intake_id,
        created_by=current_user.username,
    )
    db.add(share)
    db.commit()
    db.refresh(share)
    return {"status": "ok", "token": share.token, "url": f"/s/{share.token}",
            "id": share.id, "count": len(_share_videos(db, share))}


@app.get("/api/shares")
def list_shares(db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    q = db.query(Share).order_by(Share.created_at.desc())
    if current_user.role != "admin":
        q = q.filter(Share.created_by == current_user.username)
    out = []
    for sh in q.all():
        folder = db.query(IndexedFolder).filter(IndexedFolder.id == sh.folder_id).first() if sh.folder_id else None
        picked = sum(1 for x in sh.selects if x.picked)
        out.append({
            "id": sh.id, "token": sh.token, "url": f"/s/{sh.token}",
            "title": sh.title, "folder": folder.name if folder else None,
            "folder_id": sh.folder_id,
            "message": sh.message,
            "allow_selects": bool(sh.allow_selects),
            "include_subfolders": bool(sh.include_subfolders),
            "count": len(_share_videos(db, sh)),
            "has_password": bool(sh.password_hash),
            "allow_download": bool(sh.allow_download),
            "expires_at": sh.expires_at.isoformat() if sh.expires_at else None,
            "expired": bool(sh.expires_at and datetime.utcnow() > sh.expires_at),
            "revoked": bool(sh.revoked),
            "views": sh.view_count or 0,
            "last_viewed_at": sh.last_viewed_at.isoformat() if sh.last_viewed_at else None,
            "selects": picked,
            "allow_upload": bool(sh.allow_upload),
            "kind": sh.kind or ("both" if sh.allow_upload else "send"),
            "confirmed_at": sh.confirmed_at.isoformat() if sh.confirmed_at else None,
            "confirmed_by": sh.confirmed_by,
            "allow_zip": True if sh.allow_zip is None else bool(sh.allow_zip),
            "watermark_previews": bool(sh.watermark_previews),
            "watermark_name": sh.watermark_name,
            "shoot_id": sh.shoot_id,
            "unpaid": _unpaid(db, sh),
            "ask_terms": sh.ask_terms is not False,
            "uploads": sh.upload_count or 0,
            "intake_folder_id": sh.intake_folder_id,
            # How much is sitting in the inbox right now - uploads counts
            # everything ever sent, this counts what is still unfiled.
            "intake_waiting": (
                db.query(func.count(Video.id))
                  .filter(Video.folder_id == sh.intake_folder_id,
                          Video.is_active != False).scalar()
                if sh.intake_folder_id else 0),
            "created_by": sh.created_by,
            "created_at": sh.created_at.isoformat() if sh.created_at else None,
        })
    return out


@app.delete("/api/shares/{share_id}")
def revoke_share(share_id: int, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    sh = db.query(Share).filter(Share.id == share_id).first()
    if not sh:
        raise HTTPException(status_code=404, detail="Share not found")
    if current_user.role != "admin" and sh.created_by != current_user.username:
        raise HTTPException(status_code=403, detail="Not your share")
    # Revoked, not deleted: the selects the client already made are worth
    # keeping even after the link is switched off.
    sh.revoked = True
    db.commit()
    return {"status": "ok"}


@app.get("/api/shares/{share_id}/selects")
def share_selects(share_id: int, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    sh = db.query(Share).filter(Share.id == share_id).first()
    if not sh:
        raise HTTPException(status_code=404, detail="Share not found")
    if current_user.role != "admin" and sh.created_by != current_user.username:
        raise HTTPException(status_code=403, detail="Not your share")
    out = []
    for sel in sh.selects:
        v = db.query(Video).filter(Video.id == sel.video_id).first()
        if not v:
            continue
        # Rows with a note but no heart matter too: "the lighting is off here"
        # is feedback worth having, and filtering to picked-only would throw it
        # away silently.
        if not sel.picked and not (sel.comment or "").strip():
            continue
        folder = db.query(IndexedFolder).filter(IndexedFolder.id == v.folder_id).first()
        out.append({
            "video_id": v.id, "filename": v.filename, "picked": bool(sel.picked),
            "comment": sel.comment, "viewer_name": sel.viewer_name,
            "thumbnail_path": v.thumbnail_path, "duration": v.duration,
            "created_at": sel.created_at.isoformat() if sel.created_at else None,
            # So the owner can jump straight from a pick to the clip in the
            # library, instead of hunting for it by name.
            "folder_id": v.folder_id,
            "folder_name": folder.name if folder else None,
            "media_type": v.media_type,
        })
    # Picked first, then notes-only.
    out.sort(key=lambda x: (not x["picked"], x["filename"]))
    return {"share": {"id": sh.id, "title": sh.title, "token": sh.token},
            "selects": out,
            "picked_count": sum(1 for x in out if x["picked"]),
            "note_count": sum(1 for x in out if (x["comment"] or "").strip())}



# --- exporting picks back into the edit ------------------------------------
# The point of a selects pass is not a list - it is a bin in Resolve. FCPXML is
# the interchange Resolve imports cleanly and it carries the original file
# paths, so the clips relink to the real media rather than the proxies.

def _xml_escape(t):
    return (str(t or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _share_for_owner(db, share_id, current_user):
    sh = db.query(Share).filter(Share.id == share_id).first()
    if not sh:
        raise HTTPException(status_code=404, detail="Share not found")
    if current_user.role != "admin" and sh.created_by != current_user.username:
        raise HTTPException(status_code=403, detail="Not your share")
    return sh


def _picked_videos(db, sh):
    ids = [s.video_id for s in sh.selects if s.picked]
    if not ids:
        return []
    order = {vid: i for i, vid in enumerate(ids)}
    vids = db.query(Video).filter(Video.id.in_(ids)).all()
    return sorted(vids, key=lambda v: order.get(v.id, 0))


@app.get("/api/shares/{share_id}/export.fcpxml")
def export_picks_fcpxml(share_id: int, token: Optional[str] = None,
                        db: Session = Depends(get_db)):
    # Token in the query string because this is triggered by a plain browser
    # navigation (a download), which cannot carry an Authorization header.
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")
    current_user = get_user_from_token(token, db)
    sh = _share_for_owner(db, share_id, current_user)
    vids = _picked_videos(db, sh)
    if not vids:
        raise HTTPException(status_code=404, detail="Nothing picked yet")

    TB = 1000  # timebase; durations are approximate until the NLE conforms them
    assets, clips = [], []
    offset = 0
    for i, v in enumerate(vids, 1):
        dur = int(round((v.duration or 5) * TB))
        path = resolve_media_path(v.filepath) or v.filepath
        url = "file://" + str(path).replace("\\", "/").replace(" ", "%20")
        assets.append(
            f'        <asset id="r{i}" name="{_xml_escape(v.filename)}" '
            f'src="{_xml_escape(url)}" start="0s" duration="{dur}/{TB}s" '
            f'hasVideo="1" hasAudio="1" format="r0"/>'
        )
        clips.append(
            f'                    <asset-clip name="{_xml_escape(v.filename)}" '
            f'ref="r{i}" offset="{offset}/{TB}s" duration="{dur}/{TB}s" start="0s"/>'
        )
        offset += dur

    title = _xml_escape(sh.title or "Selects")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.8">
    <resources>
        <format id="r0" name="FFVideoFormat1080p25" frameDuration="1/25s" width="1920" height="1080"/>
{chr(10).join(assets)}
    </resources>
    <library>
        <event name="{title}">
            <project name="{title} - selects">
                <sequence format="r0" duration="{offset}/{TB}s">
                    <spine>
{chr(10).join(clips)}
                    </spine>
                </sequence>
            </project>
        </event>
    </library>
</fcpxml>
"""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", (sh.title or "selects"))[:60]
    return Response(
        content=xml, media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{safe}_selects.fcpxml"'},
    )


@app.get("/api/shares/{share_id}/export.csv")
def export_picks_csv(share_id: int, token: Optional[str] = None,
                     db: Session = Depends(get_db)):
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")
    current_user = get_user_from_token(token, db)
    sh = _share_for_owner(db, share_id, current_user)

    by_id = {s.video_id: s for s in sh.selects if s.picked}
    rows = ["filename,duration_seconds,path,picked_by,comment"]
    for v in _picked_videos(db, sh):
        sel = by_id.get(v.id)
        def q(x):
            x = str(x or "").replace('"', '""')
            return f'"{x}"'
        rows.append(",".join([
            q(v.filename), str(round(v.duration or 0, 2)), q(v.filepath),
            q(sel.viewer_name if sel else ""), q(sel.comment if sel else ""),
        ]))
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", (sh.title or "selects"))[:60]
    return Response(
        content="\n".join(rows), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{safe}_selects.csv"'},
    )

# --- public endpoints (no account) -----------------------------------------

@app.get("/api/upload/destinations")
def upload_destinations(current_user: User = Depends(get_current_user)):
    """Where an upload can go, so the Upload page can offer a choice.

    Two stores, deliberately: the media drive is the catalogued library -
    everything there is indexed, thumbnailed, tagged and transcribed. The file
    drive is a plain shared store for documents, archives and anything that is
    not footage, where none of that machinery would make sense.
    """
    out = [{
        "id": "media",
        "name": "Media drive",
        "path": str(UPLOAD_ROOT),
        "description": ("Indexed, thumbnailed, tagged and transcribed. "
                        "Video, photos and audio belong here."),
        "auto_processing": True,
    }]
    try:
        import files_api
        out.append({
            "id": "files",
            "name": "File drive",
            "path": str(files_api.files_root()),
            "description": ("A plain shared store. Documents, archives, "
                            "anything that is not footage."),
            "auto_processing": False,
            "api": "/api/files",
        })
    except Exception as e:
        print(f"[upload] file drive unavailable: {e}", flush=True)
    return {"destinations": out, "default": "media"}


def _process_new_upload(video_id: int, media_type: str, auto: bool = True):
    """Everything a newly arrived file should have done to it, in the
    background.

    Without this a file that comes in through a portal sits untagged and
    untranscribed until somebody happens to run a scan - which is exactly the
    footage most likely to be forgotten, because nobody at a desk watched it
    arrive.
    """
    if not auto:
        return

    def run():
        try:
            if media_type in ("video", "audio"):
                job_manager.add_job(video_id, "transcribe")
            if media_type == "video":
                job_manager.add_job(video_id, "proxy")
        except Exception as e:
            print(f"[upload] could not queue jobs for {video_id}: {e}", flush=True)
        try:
            import enrich
            enrich.run_tagging(progress=None, include_transcripts=False)
        except Exception as e:
            print(f"[upload] tagging after upload failed: {e}", flush=True)

    threading.Thread(target=run, daemon=True).start()


def _looks_like_a_phone(request: Request) -> bool:
    """A phone and a desktop want different things from the same link.

    On a desktop a zip of the whole folder is the sensible offer. On a phone
    it is the wrong one: iOS drops a zip into Files, not the camera roll, so
    the useful action there is saving clips one at a time. The page can only
    make that choice if it knows, and the User-Agent is the only signal a
    plain link carries.
    """
    ua = (request.headers.get("user-agent") or "").lower()
    if "ipad" in ua or "tablet" in ua:
        return False
    return any(s in ua for s in
               ("iphone", "ipod", "android", "mobile", "windows phone"))


@app.get("/api/public/share/{token}")
def public_share(token: str, request: Request, password: Optional[str] = None,
                 db: Session = Depends(get_db)):
    share = _share_state(db, token, password)
    if not _staff_preview.get():
        share.view_count = (share.view_count or 0) + 1
        share.last_viewed_at = datetime.utcnow()
        db.commit()
        delivery.log(db, share, delivery.VIEWED, request=request)
    payload = _share_payload(db, share)
    phone = _looks_like_a_phone(request)
    payload["on_phone"] = phone
    # What the page should actually offer. A zip is pointless on a phone even
    # when the portal allows one, so the answer is not simply allow_zip.
    payload["offer_zip"] = bool(payload.get("allow_zip", True)) and not phone
    payload["offer_single_files"] = bool(payload.get("allow_download"))
    payload["watermark_previews"] = _marked(db, share)
    # What has been picked and said on this link so far, so coming back to it
    # (another day, another device) shows the same picks instead of a blank page.
    payload["picks"] = {
        str(sel.video_id): {"picked": bool(sel.picked), "comment": sel.comment or ""}
        for sel in share.selects if sel.picked or (sel.comment or "").strip()
    } if share.allow_selects else {}
    payload["confirmed_by"] = share.confirmed_by
    if payload["watermark_previews"]:
        # Opening the portal is the moment someone will want to play things,
        # so this is when the marked videos start being made.
        by_id = {v.id: v for v in _share_videos(db, share)}
        for item in payload.get("videos", []):
            v = by_id.get(item["id"])
            if v is None or (v.media_type or "video") != "video":
                continue
            src = _marked_source(v)
            delivery.watermarked_video(src, str(_WM_CACHE_DIR), f"v{v.id}",
                                       mark_name=share.watermark_name)
            item["preview_state"] = delivery.video_state(src, str(_WM_CACHE_DIR), f"v{v.id}",
                                                         mark_name=share.watermark_name)
    return payload


@app.get("/api/public/share/{token}/scenes")
def public_share_scenes(token: str, password: Optional[str] = None, db: Session = Depends(get_db)):
    """3D walk-throughs shared with the client for this portal's property."""
    share = _share_open(db, token, password)
    try:
        import splat
        return {"scenes": splat.shared_for_shoot(share.shoot_id)}
    except Exception:
        return {"scenes": []}


@app.get("/api/public/share/{token}/thumb/{video_id}")
def public_thumb(token: str, video_id: int, password: Optional[str] = None,
                 db: Session = Depends(get_db)):
    share = _share_open(db, token, password)
    if video_id not in {v.id for v in _share_videos(db, share)}:
        raise HTTPException(status_code=404, detail="Not in this share")
    v = db.query(Video).filter(Video.id == video_id).first()
    path = resolve_thumbnail_path(v.thumbnail_path) if v else None
    if not path:
        raise HTTPException(status_code=404, detail="No thumbnail")
    # A delivery lets someone see the whole set before anything is paid for.
    # The preview carries the mark; the download, which is gated separately,
    # does not.
    if _marked(db, share):
        path = delivery.watermarked_preview(path, mark_name=share.watermark_name)
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})


_WM_CACHE_DIR = UPLOAD_ROOT / "thumbnails"


def _marked_source(v):
    """What a marked copy is made from: the proxy when there is one (smaller,
    and already decodes everywhere), otherwise the original."""
    if (v.media_type or "video") == "video" and v.proxy_status == "completed" and v.proxy_path:
        p = resolve_media_path(v.proxy_path)
        if p and os.path.exists(p):
            return p
    return resolve_media_path(v.filepath)


def _marked_stream(v, mark_name=None):
    """The response for a watermarked portal's viewer, or None to fall through
    (audio and documents carry no mark). Fails closed for photos and video."""
    kind = v.media_type or "video"
    key = f"v{v.id}"
    if kind == "photo":
        path = delivery.watermarked_display(resolve_media_path(v.filepath),
                                            str(_WM_CACHE_DIR), key, mark_name=mark_name)
        if not path:
            thumb = resolve_thumbnail_path(v.thumbnail_path)
            marked = delivery.watermarked_preview(thumb, mark_name=mark_name) if thumb else None
            if not marked or marked == thumb:
                raise HTTPException(status_code=404, detail="No preview available")
            path = marked
        return FileResponse(path, media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=3600"})
    if kind == "video":
        path = delivery.watermarked_video(_marked_source(v), str(_WM_CACHE_DIR), key,
                                          mark_name=mark_name)
        if not path:
            raise HTTPException(status_code=503,
                                detail="The preview is still being prepared.",
                                headers={"Retry-After": "30"})
        return FileResponse(path, media_type="video/mp4",
                            headers={"Accept-Ranges": "bytes",
                                     "Content-Disposition": "inline"})
    return None


@app.get("/api/public/share/{token}/stream/{video_id}")
def public_stream(token: str, video_id: int, password: Optional[str] = None,
                  download: bool = False, db: Session = Depends(get_db)):
    share = _share_open(db, token, password)
    if video_id not in {v.id for v in _share_videos(db, share)}:
        raise HTTPException(status_code=404, detail="Not in this share")
    if download and not share.allow_download:
        raise HTTPException(status_code=403, detail="Downloads are off for this link")
    marked = _marked(db, share)
    if download and _unpaid(db, share):
        raise HTTPException(status_code=403, detail=UNPAID_WORDS)
    if download:
        if not _staff_preview.get():
            delivery.log(db, share, delivery.DOWNLOADED, video_id=video_id)

    v = db.query(Video).filter(Video.id == video_id).first()
    if not v:
        raise HTTPException(status_code=404, detail="Not found")

    # A watermarked portal never streams an unmarked original to the viewer.
    # This is the path the lightbox uses, so it matters more than the thumb.
    if marked and not download:
        marked_resp = _marked_stream(v, share.watermark_name)
        if marked_resp is not None:
            return marked_resp

    # Always prefer the proxy for a share: the viewer is remote and on the
    # wrong end of an asymmetric line. Originals only on explicit download.
    path = None
    if not download and v.proxy_status == "completed" and v.proxy_path:
        path = resolve_media_path(v.proxy_path)
        if path and not os.path.exists(path):
            path = None
    if not path:
        path = resolve_media_path(v.filepath)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found")

    # Content type matters more than it looks. FileResponse(filename=...) sets
    # Content-Disposition: attachment, and octet-stream tells the browser "this
    # is a blob" - between them, a <video> on iOS Safari refuses to play and
    # the clip downloads instead. So: a real media type and inline when the
    # viewer is watching, attachment only when they asked to keep it.
    ctype = mimetypes.guess_type(v.filename or path)[0]
    if not ctype:
        ctype = "video/mp4" if (v.media_type or "video") == "video" else "application/octet-stream"

    if download:
        return FileResponse(path, filename=v.filename,
                            media_type="application/octet-stream",
                            headers={"Accept-Ranges": "bytes"})

    return FileResponse(
        path,
        media_type=ctype,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Disposition": f'inline; filename="{os.path.basename(v.filename or "clip")}"',
        },
    )



def _zip_stream(entries, download_name: str):
    """Stream a list of (path, name) out as one ZIP, a chunk at a time.

    These are 250MB+ masters and a selection can be twenty of them: building
    the archive in memory, or writing a temp file first, would mean gigabytes
    and a browser sitting on nothing for minutes. This starts sending at once
    and holds a single chunk.

    ZIP_STORED because photos and video are already compressed - deflate would
    burn CPU for roughly no saving, and stored entries stream cleanly.
    """
    import zipfile

    class _Sink:
        """Collects what ZipFile writes so the generator can hand it out."""
        def __init__(self):
            self.buf = bytearray()
        def write(self, data):
            self.buf.extend(data)
            return len(data)
        def flush(self):
            pass
        def take(self):
            out = bytes(self.buf)
            self.buf.clear()
            return out

    def stream():
        sink = _Sink()
        zf = zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True)
        try:
            for path, name in entries:
                try:
                    with zf.open(name, "w") as dest, open(path, "rb") as src:
                        while True:
                            chunk = src.read(1024 * 1024)
                            if not chunk:
                                break
                            dest.write(chunk)
                            data = sink.take()
                            if data:
                                yield data
                except (OSError, ValueError) as e:
                    print(f"  [zip] skipped {name}: {e}")
                    continue
                data = sink.take()
                if data:
                    yield data
        finally:
            zf.close()
            tail = sink.take()
            if tail:
                yield tail

    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", download_name or "files")[:60]
    return StreamingResponse(
        stream(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{safe}.zip"',
            "X-Accel-Buffering": "no",
        },
    )


def _zip_entries(videos):
    """(path, name) for each file that is really on disk, names made unique."""
    entries, used = [], set()
    for v in videos:
        path = resolve_media_path(v.filepath)
        if not path or not os.path.exists(path):
            continue
        name = os.path.basename(v.filename or f"clip_{v.id}")
        base, ext = os.path.splitext(name)
        n = 2
        while name.lower() in used:
            name = f"{base} ({n}){ext}"
            n += 1
        used.add(name.lower())
        entries.append((path, name))
    return entries


@app.get("/api/download-zip")
def download_selection_zip(request: Request, ids: str, token: Optional[str] = None,
                           db: Session = Depends(get_db)):
    """Several files as one ZIP - what a multi-item drag out of the library grabs.

    The token rides in the query string because the thing fetching this is
    Windows Explorer or Finder, not our own fetch(), and neither of them will
    send an Authorization header.
    """
    if token:
        get_user_from_token(token, db, check_session=False)
    else:
        auth = request.headers.get("Authorization")
        if not auth or not auth.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
        get_user_from_token(auth[7:], db)

    try:
        wanted = [int(x) for x in ids.split(",") if x.strip()][:500]
    except ValueError:
        raise HTTPException(status_code=400, detail="Bad id list")
    if not wanted:
        raise HTTPException(status_code=400, detail="Nothing to download")

    found = {v.id: v for v in db.query(Video).filter(Video.id.in_(wanted)).all()}
    videos = [found[i] for i in wanted if i in found]
    entries = _zip_entries(videos)
    if not entries:
        raise HTTPException(status_code=404, detail="None of those files are on disk")
    return _zip_stream(entries, f"zerko-{len(entries)}-files")


@app.get("/api/public/share/{token}/download-zip")
def public_download_zip(token: str, ids: Optional[str] = None,
                        password: Optional[str] = None,
                        db: Session = Depends(get_db)):
    """Stream several clips from a share as one ZIP.

    Written as a generator on purpose. These are 250MB+ masters and a client
    might select twenty of them - building the archive in memory, or writing a
    temp file first, would mean gigabytes of RAM or disk and a browser sitting
    on a blank page for minutes before anything happens. This starts sending
    immediately and holds only one chunk at a time.

    ZIP_STORED (no compression) because video is already compressed: deflate
    would burn CPU for roughly zero saving, and stored entries stream cleanly.
    """
    share_for_zip = _share_open(db, token, password)
    if not (True if share_for_zip.allow_zip is None else share_for_zip.allow_zip):
        raise HTTPException(
            status_code=403,
            detail="This portal hands out files one at a time, not as an archive.")

    import zipfile

    share = _share_open(db, token, password)
    if not share.allow_download:
        raise HTTPException(status_code=403, detail="Downloads are off for this link")
    if _unpaid(db, share):
        raise HTTPException(status_code=403, detail=UNPAID_WORDS)

    if not _staff_preview.get():
        delivery.log(db, share, delivery.DOWNLOADED_ZIP,
                     detail=(f"{len(ids.split(','))} selected" if ids else "everything"))

    allowed = {v.id: v for v in _share_videos(db, share)}
    if ids:
        try:
            wanted = [int(x) for x in ids.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(status_code=400, detail="Bad id list")
        videos = [allowed[i] for i in wanted if i in allowed]
    else:
        videos = list(allowed.values())

    if not videos:
        raise HTTPException(status_code=404, detail="Nothing to download")

    # Resolve up front so a missing file is an error before we start streaming
    # (once bytes are flowing we can no longer send a clean HTTP error).
    entries = []
    used = set()
    for v in videos:
        path = resolve_media_path(v.filepath)
        if not path or not os.path.exists(path):
            continue
        name = os.path.basename(v.filename or f"clip_{v.id}")
        # Same filename twice in one archive would silently overwrite.
        base, ext = os.path.splitext(name)
        n = 2
        while name.lower() in used:
            name = f"{base} ({n}){ext}"
            n += 1
        used.add(name.lower())
        entries.append((path, name))

    if not entries:
        raise HTTPException(status_code=404, detail="None of those files are on disk")

    class _Sink:
        """Collects what ZipFile writes so the generator can hand it out."""
        def __init__(self):
            self.buf = bytearray()
        def write(self, data):
            self.buf.extend(data)
            return len(data)
        def flush(self):
            pass
        def take(self):
            out = bytes(self.buf)
            self.buf.clear()
            return out

    def stream():
        sink = _Sink()
        # allowZip64: a selection of 4K masters passes 4GB easily.
        zf = zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True)
        try:
            for path, name in entries:
                try:
                    with zf.open(name, "w") as dest, open(path, "rb") as src:
                        while True:
                            chunk = src.read(1024 * 1024)
                            if not chunk:
                                break
                            dest.write(chunk)
                            data = sink.take()
                            if data:
                                yield data
                except (OSError, ValueError) as e:
                    # One unreadable file must not kill the whole download.
                    print(f"  [zip] skipped {name}: {e}")
                    continue
                data = sink.take()
                if data:
                    yield data
        finally:
            zf.close()
            tail = sink.take()
            if tail:
                yield tail

    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", (share.title or "clips"))[:60]
    return StreamingResponse(
        stream(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{safe}.zip"',
            # Length is unknown while streaming; say so rather than lying.
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/shares/{share_id}/intake")
def share_intake(share_id: int, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    """What has arrived through this link and not been filed yet."""
    # Same rule as the portal list: an admin, or whoever made the link.
    share = _share_for_owner(db, share_id, current_user)
    if not share.intake_folder_id:
        return {"folder_id": None, "folder": None, "count": 0, "items": []}

    folder = db.query(IndexedFolder).filter(
        IndexedFolder.id == share.intake_folder_id).first()
    rows = (db.query(Video)
            .filter(Video.folder_id == share.intake_folder_id, Video.is_active != False)
            .order_by(Video.uploaded_at.desc())
            .all())
    return {
        "folder_id": share.intake_folder_id,
        "folder": folder.name if folder else None,
        "count": len(rows),
        "items": [{
            "id": v.id,
            "filename": v.filename,
            "media_type": v.media_type,
            "file_size": v.file_size,
            "duration": v.duration,
            "thumbnail_path": v.thumbnail_path,
            "uploaded_by": v.uploaded_by,
            "uploaded_at": v.uploaded_at.isoformat() if v.uploaded_at else None,
        } for v in rows],
    }


@app.post("/api/shares/{share_id}/intake/file")
def file_intake(share_id: int, payload: dict = Body(...),
                db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    """Move some or all of an inbox into a folder - an existing one, or a new
    project created on the spot. This is the deliberate step: nothing a phone
    sends reaches the library proper until someone does this."""
    if not permissions.can(current_user.role, permissions.ORGANISE):
        raise HTTPException(status_code=403, detail="Your account cannot move files.")

    share = _share_for_owner(db, share_id, current_user)
    if not share.intake_folder_id:
        raise HTTPException(status_code=400, detail="This link has no inbox")

    video_ids = (payload or {}).get("video_ids")
    folder_id = (payload or {}).get("folder_id")
    new_name = ((payload or {}).get("new_folder") or "").strip()

    if new_name:
        if any(ch in new_name for ch in ("/", "\\", "..")) or new_name in (".", ".."):
            raise HTTPException(status_code=400, detail="Name cannot contain slashes or ..")
        safe = re.sub(r'[<>:"|?*]', "_", new_name)
        target_dir = UPLOAD_ROOT / safe
        target_dir.mkdir(parents=True, exist_ok=True)
        row = ensure_folder_row(db, target_dir)
        if not row:
            raise HTTPException(status_code=500, detail="Could not create that folder")
        folder_id = row.id

    if not folder_id:
        raise HTTPException(status_code=400, detail="Choose a folder, or name a new one")

    folder = db.query(IndexedFolder).filter(IndexedFolder.id == folder_id).first()
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")

    q = db.query(Video).filter(Video.folder_id == share.intake_folder_id,
                               Video.is_active != False)
    if video_ids:
        q = q.filter(Video.id.in_([int(v) for v in video_ids]))
    rows = q.all()

    moved, failed = 0, []
    for v in rows:
        try:
            move_media_file(db, v, folder)
            moved += 1
        except Exception as e:
            failed.append(f"{v.filename}: {e}")
    db.commit()
    return {"status": "ok", "moved": moved, "folder": folder.name,
            "folder_id": folder.id, "failed": failed}


@app.post("/api/public/share/{token}/confirm")
def public_confirm(token: str, payload: dict = Body(None),
                   db: Session = Depends(get_db)):
    """The viewer saying they have finished.

    Picks and notes were already saved the moment they were made - but nothing
    ever told the viewer that, so they had no way of knowing whether a tick or
    a typed note had reached anybody. This is the receipt: it records who
    finished and when, and gives the page something definite to show.
    """
    share = _share_open(db, token, (payload or {}).get("password"))
    who = ((payload or {}).get("viewer_name") or "").strip()[:60] or None
    share.confirmed_at = datetime.utcnow()
    share.confirmed_by = who
    db.commit()
    delivery.log(db, share, delivery.CONFIRMED, viewer_name=who)

    picks = sum(1 for s in share.selects if s.picked)
    notes = sum(1 for s in share.selects if (s.comment or "").strip())
    return {"status": "ok", "confirmed_at": share.confirmed_at.isoformat(),
            "picks": picks, "notes": notes,
            "message": f"{picks} pick{'' if picks == 1 else 's'}"
                       f" and {notes} note{'' if notes == 1 else 's'} sent."}


@app.post("/api/public/share/{token}/upload")
def public_upload(token: str,
                  request: Request,
                  file: UploadFile = File(...),
                  password: Optional[str] = Form(None),
                  sender: Optional[str] = Form(None),
                  db: Session = Depends(get_db)):
    """Accept a file through a share link that has uploads switched on.

    This is how a phone gets footage into the library without an account: the
    token is the credential, it can expire, and it can only ever write into
    the one folder the link was made for. Nothing here trusts a path from the
    request - the filename is stripped to a basename and made unique inside
    the destination, so '../' in a filename goes nowhere.
    """
    share = _share_state(db, token, password)
    if not share.allow_upload:
        raise HTTPException(status_code=403, detail="This link does not accept uploads")
    target_folder_id = share.intake_folder_id or share.folder_id
    if target_folder_id is None:
        raise HTTPException(status_code=400, detail="This link has no destination folder")

    os.makedirs(UPLOAD_ROOT, exist_ok=True)
    dest_dir = upload_destination_dir(db, target_folder_id)
    os.makedirs(dest_dir, exist_ok=True)

    safe_name = os.path.basename(file.filename or "upload")
    if not safe_name or safe_name in (".", ".."):
        raise HTTPException(status_code=400, detail="That file has no usable name")
    file_path = unique_destination(dest_dir, safe_name)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer, COPY_BUFFER)

    media_type = get_media_type(safe_name)
    os.makedirs(UPLOAD_ROOT / "thumbnails", exist_ok=True)
    thumbnail_filename = f"{uuid.uuid4()}.jpg"
    thumb_path = UPLOAD_ROOT / "thumbnails" / thumbnail_filename

    def generate_thumb_bg():
        try:
            if media_type in ('video', 'photo'):
                ok, why = previews.make_thumbnail(str(file_path), str(thumb_path), media_type)
                if not ok:
                    print(f"Thumbnail failed for {file_path}: {why}")
        except Exception as e:
            print(f"Thumbnail generation failed for {file_path}: {e}")

    threading.Thread(target=generate_thumb_bg, daemon=True).start()

    who = (sender or "").strip()[:60] or "share link"
    video = Video(
        filename=file_path.name,
        filepath=str(file_path),
        file_size=os.path.getsize(file_path),
        duration=get_duration_fast(str(file_path)) if media_type == "video" else 0.0,
        thumbnail_path=(f"/thumbnails/{thumbnail_filename}" if media_type in ("video", "photo") else None),
        folder_id=target_folder_id,
        uploaded_by=f"{who} (link)",
        uploaded_at=datetime.utcnow(),
        status="raw",
        media_type=media_type,
    )
    db.add(video)
    share.upload_count = (share.upload_count or 0) + 1
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Could not save that file")
    db.refresh(video)
    delivery.log(db, share, delivery.UPLOADED,
                 detail=file_path.name, viewer_name=(sender or None), request=request)
    _process_new_upload(video.id, video.media_type)
    return {"status": "ok", "id": video.id, "filename": video.filename,
            "media_type": video.media_type, "size": video.file_size}


@app.post("/api/public/share/{token}/select")
def public_select(token: str, payload: dict = Body(...),
                  db: Session = Depends(get_db)):
    share = _share_open(db, token, (payload or {}).get("password"))
    if not share.allow_selects:
        raise HTTPException(status_code=403, detail="Picking is off for this link")

    video_id = payload.get("video_id")
    if video_id not in {v.id for v in _share_videos(db, share)}:
        raise HTTPException(status_code=404, detail="Not in this share")

    sel = db.query(ShareSelect).filter(
        ShareSelect.share_id == share.id,
        ShareSelect.video_id == video_id).first()
    if not sel:
        sel = ShareSelect(share_id=share.id, video_id=video_id)
        db.add(sel)
    sel.picked = bool(payload.get("picked", True))
    if payload.get("comment") is not None:
        sel.comment = (payload.get("comment") or "").strip()[:2000] or None
    if payload.get("viewer_name"):
        sel.viewer_name = str(payload["viewer_name"])[:80]
    db.commit()
    return {"status": "ok", "video_id": video_id, "picked": sel.picked}

# --- catalog backups -------------------------------------------------------

@app.get("/api/admin/backups")
def list_backups(current_user: User = Depends(require_admin)):
    import catalog_backup
    out = []
    try:
        for f in sorted(catalog_backup.backup_dir().glob("catalog-*.db.gz"),
                        key=lambda x: x.stat().st_mtime, reverse=True):
            st = f.stat()
            out.append({
                "name": f.name,
                "size": st.st_size,
                "size_formatted": f"{st.st_size / 1024:.0f} KB",
                "created_at": datetime.fromtimestamp(st.st_mtime).isoformat(),
            })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"directory": str(catalog_backup.backup_dir()), "keep": catalog_backup.KEEP,
            "backups": out}


@app.post("/api/admin/backups")
def create_backup(current_user: User = Depends(require_admin)):
    import catalog_backup
    path = catalog_backup.run_once("manual", do_verify=True)
    if not path:
        raise HTTPException(status_code=500, detail="Backup failed - check the server log")
    return {"status": "ok", "name": path.name,
            "size": path.stat().st_size,
            "directory": str(catalog_backup.backup_dir())}


@app.post("/api/admin/backups/verify")
def verify_latest_backup(current_user: User = Depends(require_admin)):
    import catalog_backup
    latest = catalog_backup.latest()
    if not latest:
        raise HTTPException(status_code=404, detail="No backups yet")
    ok = catalog_backup.verify(latest)
    return {"status": "ok" if ok else "failed", "name": latest.name}


# Files: a general shared file store (routes, tables and storage location).
files_api.install(app, UPLOAD_ROOT)

# Photo export: 16:9 copies of photos as a ZIP, plus saved per-photo framing.
photo_proxy.install(app, UPLOAD_ROOT, resolve_media_path)
activity.install(app)
import tag_rules
tag_rules.install(app)
library_check.install(app, UPLOAD_ROOT, resolve_media_path)
hdr.install(app, UPLOAD_ROOT, resolve_media_path)
photo_edit.install(app, UPLOAD_ROOT, resolve_media_path)
projects.install(app, UPLOAD_ROOT, resolve_media_path, ensure_folder_row)
shoots.install(app, UPLOAD_ROOT, resolve_media_path)
import business
business.install(app, _share_state)
import booking
booking.install(app)
import rooms
rooms.install(app)
delivery.install(app)
# In/out ranges on clips, rendered on request; searching by spoken word.
subclips.install(app, UPLOAD_ROOT, resolve_media_path, ensure_folder_row)
video_edit.install(app, UPLOAD_ROOT, resolve_media_path, ensure_folder_row)
ai.install(app)
ai_photo.install(app)
ai_image.install(app)
import ai_usage
ai_usage.install(app)
import pipeline
pipeline.install(app)
import cull
cull.install(app)
import ingest
ingest.install(app, UPLOAD_ROOT, resolve_media_path)
import fixes
fixes.install(app)
import enhance
enhance.install(app)
import clip_search
clip_search.install(app, UPLOAD_ROOT, resolve_media_path)
import style
style.install(app)
import splat
splat.install(app, resolve_media_path)
import proofing
proofing.install(app, _share_open, _share_videos)
# Groups, who sees which folders and properties, profiles (Discord-style roles).
access.install(app, UPLOAD_ROOT)
# Who is online, channels and direct messages.
import team
team.install(app)
import listing_pack
listing_pack.install(app)
import print_marketing
print_marketing.install(app)
import listing_site
listing_site.install(app, resolve_media_path, UPLOAD_ROOT)
import invoices
invoices.install(app)
import archive
archive.install(app, resolve_media_path, UPLOAD_ROOT)
import assistant
assistant.install(app, UPLOAD_ROOT, resolve_media_path, ensure_folder_row)


# The new frontend (web/), served at /v2 beside the legacy app while it is
# built page by page. Its assets are content-hashed, index.html never cached.
WEB_DIST = Path(os.environ.get("ZK_WEB_DIST") or (PROJECT_ROOT / "web" / "dist"))   # a test copy can serve its own build


# The new interface is the front door once it is built. The classic app
# stays at /browse (and every other old path) for what v2 does not do yet -
# the photo editor, Files, admin - and client portals (/s/...) are untouched.
@app.get("/", include_in_schema=False)
def front_door(request: Request):
    from fastapi.responses import RedirectResponse
    if (WEB_DIST / "index.html").exists():
        return RedirectResponse("/v2/library", status_code=302)
    return serve_frontend("", request)


@app.api_route("/v2", methods=["GET"])
@app.api_route("/v2/{full_path:path}", methods=["GET"])
def serve_web_v2(full_path: str = ""):
    if not (WEB_DIST / "index.html").exists():
        raise HTTPException(status_code=404,
                            detail="The new interface has not been built yet (web/dist).")
    if full_path:
        candidate = (WEB_DIST / full_path).resolve()
        try:
            candidate.relative_to(WEB_DIST.resolve())
        except ValueError:
            raise HTTPException(status_code=404, detail="Not found")
        if candidate.is_file():
            cache = ("public, max-age=31536000, immutable"
                     if full_path.startswith("assets/") else "no-cache")
            return FileResponse(str(candidate), headers={"Cache-Control": cache})
    return FileResponse(str(WEB_DIST / "index.html"),
                        headers={"Cache-Control": "no-store"})


# The classic app's pages, and where each one lives in the new interface.
# Old bookmarks and links land in the right place instead of in the old app,
# which had no way back. ?classic=1 still opens the old page, as an escape
# hatch. Client links (/s/..., /f/...) are not here: they stay as they are.
_CLASSIC_TO_V2 = {
    "browse": "/v2/library",
    "upload": "/v2/library",
    "files": "/v2/files",
    "shares": "/v2/portals",
    "portals": "/v2/portals",
    "dashboard": "/v2/manage/overview",
    "duplicates": "/v2/manage/duplicates",
    "tags": "/v2/manage/tagging",
    "admin": "/v2/manage/updates",
}


def _classic_redirect(request: Request, full_path: str):
    from fastapi.responses import RedirectResponse
    q = request.query_params
    if q.get("classic") or not (WEB_DIST / "index.html").exists():
        return None
    head = full_path.strip("/").split("/")[0]
    target = _CLASSIC_TO_V2.get(head)
    if not target:
        return None
    # /browse?video=ID&t=SEC was the deep link to a clip: open it there instead.
    if head == "browse" and q.get("video", "").isdigit():
        vid = int(q["video"])
        t = q.get("t", "")
        target += f"?open={vid}&sel={vid}" + (f"&t={t}" if re.fullmatch(r"[0-9.]+", t or "") else "")
    return RedirectResponse(target, status_code=302)


# --- first-run setup -------------------------------------------------------
# Registered here, ahead of the catch-all, because the catch-all matches
# /{full_path} and would otherwise swallow every /api/setup/* call.
try:
    # init_db() is otherwise only called under __main__. On a brand new
    # install the tables would not exist yet when the first /api/setup call
    # arrives, so create them here. It is idempotent.
    init_db()
except Exception as _e:
    print(f"  [!] Could not initialise the database: {_e}")

try:
    from setup_routes import (
        router as _setup_router,
        page_router as _setup_page_router,
        install_setup_gate,
        setup_needed,
        setup_code,
    )
    app.include_router(_setup_router)
    app.include_router(_setup_page_router)
    install_setup_gate(app)
    if setup_needed():
        print("")
        print("  No account yet - open the app in your browser to set it up.")
        print(f"  Setup code: {setup_code()}   (only asked for if you open it from another device)")
except Exception as _e:
    print(f"  [!] Setup routes unavailable: {_e}")

# Catch-all: serve index.html for all non-API routes (React Router)
# Client links open in the new interface too (it has pages for them that
# need no account). ?classic=1 still gets the old page.
@app.get("/book", include_in_schema=False)
@app.get("/booked/{token}", include_in_schema=False)
@app.get("/l/{token}", include_in_schema=False)
@app.get("/s/{token}", include_in_schema=False)
@app.get("/f/{token}", include_in_schema=False)
@app.get("/3d/{token}", include_in_schema=False)
def client_link_page(request: Request, token: str = ""):
    if request.query_params.get("classic") or not (WEB_DIST / "index.html").exists():
        return serve_frontend(request.url.path.lstrip("/"), request)
    return FileResponse(str(WEB_DIST / "index.html"), media_type="text/html",
                        headers={"Cache-Control": "no-cache", "X-Robots-Tag": "noindex"})


@app.api_route("/{full_path:path}", methods=["GET"])
def serve_frontend(full_path: str, request: Request):
    if full_path.startswith("api/"): raise HTTPException(status_code=404, detail="Not found")
    moved = _classic_redirect(request, full_path)
    if moved is not None:
        return moved
    index_path = str(FRONTEND_DIST / "index.html")
    if os.path.exists(index_path):
        # index.html must never be cached. Asset filenames are content-hashed
        # by Vite, so they cache forever safely - but if the browser holds a
        # stale index.html it keeps loading the OLD hashed bundle and a rebuilt
        # UI silently never appears.
        return FileResponse(index_path, headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        })
    return {"detail": "Frontend not found"}

if __name__ == "__main__":
    import uvicorn

    init_db()

    # Anything the database still lists as queued/processing goes back on the
    # queue, so a restart mid-run picks up where it left off.
    # Photos can never be transcribed - stop them sitting in the counts as
    # failures after being queued by an over-eager "Transcribe All".
    try:
        from database import SessionLocal as _S
        _db = _S()
        fixed = _db.query(Video).filter(
            Video.media_type.notin_(["video", "audio"]),
            Video.transcription_status.in_(["queued", "processing", "failed", "not_started"])
        ).update({Video.transcription_status: "not_applicable"}, synchronize_session=False)
        _db.commit(); _db.close()
        if fixed:
            print(f"  Marked {fixed} non-AV files as not transcribable")
    except Exception as e:
        print(f"  [!] Could not normalise transcription statuses: {e}")

    try:
        job_manager.resume_pending()
    except Exception as e:
        print(f"  [!] Could not resume pending jobs: {e}")

    # Proxies as a real background job: new footage, retries, repairs.
    try:
        import proxy_sweeper
        proxy_sweeper.start(resolve=resolve_media_path)
        print(f"  Proxy sweeper on (mode: {proxy_sweeper.mode()})", flush=True)
    except Exception as e:
        print(f"  [!] Could not start the proxy sweeper: {e}")

    # Nightly catalog snapshot. The transcripts represent far more work than
    # the database file's size suggests, and they only exist in one place.
    try:
        import catalog_backup
        catalog_backup.start_scheduler()
    except Exception as e:
        print(f"  [!] Could not start catalog backups: {e}")

    # A test copy (tools/dev-test/wsl_testbed.sh) shares this folder with the
    # live install: it must not mark repairs done, update the DNS record or
    # watch for updates on the live one's behalf.
    test_copy = bool(os.environ.get("ZK_TEST_COPY"))

    # One-off repairs after an update, in the background, each only once.
    try:
        if test_copy:
            raise RuntimeError("skipped in a test copy")
        import maintenance
        maintenance.start(resolve_media_path, move_to_trash, UPLOAD_ROOT)
    except Exception as e:
        print(f"  [!] Could not start maintenance: {e}")

    try:
        if test_copy:
            raise RuntimeError("skipped in a test copy")
        _start_update_watcher()
    except Exception as e:
        print(f"  [!] Could not start the update watcher: {e}")

    # Dynamic DNS, in-process. The scheduled task this replaces opened a
    # console window every fifteen minutes and took focus with it.
    try:
        if test_copy:
            raise RuntimeError("skipped in a test copy")
        import dns_refresh
        if dns_refresh.start_scheduler():
            print("  Dynamic DNS refresh: on (every 15 minutes)", flush=True)
    except Exception as e:
        print(f"  [!] Could not start the DNS refresh: {e}")

    port = int(os.environ.get("PORT", 9600))
    host = os.environ.get("HOST", "0.0.0.0")

    print("")
    print("  Zerko File Manager")
    print("  ------------------")
    print(f"  Media root : {UPLOAD_ROOT}")
    print(f"  Listening  : http://{host}:{port}")
    print("")

    # NOTE: stays at a single worker on purpose - job_manager runs in-process,
    # and multiple workers would each start their own copy and duplicate every
    # proxy/transcode job. Concurrency now comes from the threadpool instead.
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=os.environ.get("LOG_LEVEL", "info"),
        timeout_keep_alive=75,
    )
