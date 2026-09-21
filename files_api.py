"""Zerko Files - a general-purpose shared file store.

The media library is for footage: it is indexed, tagged, given proxies and
watched by scanners. This is the other thing - a plain place to put anything
(game installers, project backups, documents, zips) and hand it to a friend.
The filesystem is the source of truth: what you see is what is on the disk, in
ordinary folders, so it can be backed up or browsed without Zerko at all.

Layout on disk (FILES_ROOT, by default a sibling of the media folder):

    <root>/                 your files and folders, exactly as shown in the app
    <root>/.zerko/uploads   half-received uploads (resumable)
    <root>/.zerko/trash     deleted items, kept 30 days
    <root>/.zerko/thumbs    cached thumbnails

Everything is addressed by a slash-separated path RELATIVE to the root. Every
path from a client passes through `abs_path`, which refuses anything that could
leave the root (.., absolute paths, symlinks pointing outside, the internal
folder) - it is the one door, so it is the one place to audit.

Access follows the roles in permissions.py: everyone signed in sees the same
storage. Viewers can browse and download; anyone who may upload can add,
rename, move and trash; only an administrator deletes for good; share links
need the sharing permission.
"""

import hashlib
import mimetypes
import os
import re
import secrets
import shutil
import threading
import time
import uuid
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import BigInteger, Boolean, Column, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

import permissions
import previews
import hmac
from auth import SECRET_KEY, get_password_hash, get_user_from_token, verify_password
from database import Base, SessionLocal, User, engine, get_db

router = APIRouter(prefix="/api/files", tags=["files"])
public = APIRouter(prefix="/api/public/files", tags=["files-public"])

INTERNAL = ".zerko"
CHUNK_SIZE = 8 * 1024 * 1024          # what the browser is told to send per request
TRASH_DAYS = 30
MAX_LIST = 20000
STALE_UPLOAD_SECONDS = 3 * 24 * 3600


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------
class FileMeta(Base):
    """Who put a file there, and when. Purely informational - the disk is the
    truth about what exists; this only adds a name to the "uploaded by" line
    and feeds the Recent view."""
    __tablename__ = "file_meta"
    id = Column(Integer, primary_key=True)
    path = Column(String, unique=True, index=True, nullable=False)
    is_dir = Column(Boolean, default=False)
    owner = Column(String, nullable=True)
    size = Column(BigInteger, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class FileStar(Base):
    __tablename__ = "file_stars"
    __table_args__ = (UniqueConstraint("user_id", "path", name="uq_file_star"),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True, nullable=False)
    path = Column(String, nullable=False)


class FileTrash(Base):
    __tablename__ = "file_trash"
    id = Column(Integer, primary_key=True)
    orig_path = Column(String, nullable=False)
    slot = Column(String, unique=True, nullable=False)     # folder under .zerko/trash
    name = Column(String, nullable=False)
    is_dir = Column(Boolean, default=False)
    size = Column(BigInteger, default=0)
    deleted_by = Column(String, nullable=True)
    deleted_at = Column(DateTime, default=datetime.utcnow, index=True)


class FileShare(Base):
    """A public link to one file or folder. Deliberately not tied to an
    account: the point is that a friend opens it without signing up."""
    __tablename__ = "file_shares"
    id = Column(Integer, primary_key=True)
    token = Column(String, unique=True, index=True, nullable=False)
    path = Column(String, nullable=False, index=True)
    is_dir = Column(Boolean, default=False)
    name = Column(String, nullable=True)
    password_hash = Column(String, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    revoked = Column(Boolean, default=False)
    view_count = Column(Integer, default=0)
    download_count = Column(Integer, default=0)
    last_accessed = Column(DateTime, nullable=True)


_TABLES = [FileMeta.__table__, FileStar.__table__, FileTrash.__table__, FileShare.__table__]


# ---------------------------------------------------------------------------
# Where the files live
# ---------------------------------------------------------------------------
_root: Optional[Path] = None
_default_parent: Optional[Path] = None
_root_lock = threading.Lock()


def configure(media_root=None):
    """Called once by main.py. The default location is a sibling of the media
    folder, so the media scanner never walks into it."""
    global _default_parent, _root
    _default_parent = Path(media_root).parent if media_root else None
    _root = None
    Base.metadata.create_all(bind=engine, tables=_TABLES)


def _candidate_roots() -> List[Path]:
    out = []
    env = os.environ.get("ZERKO_FILES_ROOT")
    if env:
        out.append(Path(env))
    else:
        if _default_parent:
            out.append(_default_parent / "Zerko Files")
        out.append(Path(__file__).resolve().parent / "Zerko Files")
    return out


def files_root() -> Path:
    global _root
    if _root is not None:
        return _root
    with _root_lock:
        if _root is not None:
            return _root
        last = None
        for cand in _candidate_roots():
            try:
                cand.mkdir(parents=True, exist_ok=True)
                for sub in ("uploads", "trash", "thumbs"):
                    (cand / INTERNAL / sub).mkdir(parents=True, exist_ok=True)
                _root = cand.resolve()
                print(f"  [files] storage folder: {_root}", flush=True)
                return _root
            except OSError as e:
                last = e
        raise HTTPException(
            status_code=503,
            detail=("The Files storage folder could not be created"
                    f"{f' ({last})' if last else ''}. Set ZERKO_FILES_ROOT to a folder "
                    "Zerko can write to, then restart."))


def _internal(name: str) -> Path:
    return files_root() / INTERNAL / name


# ---------------------------------------------------------------------------
# Paths and names - the one door
# ---------------------------------------------------------------------------
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


def clean_name(name: str, strict: bool = True) -> str:
    """A single file or folder name that is safe on every filesystem Zerko can
    sit on (Windows drives included). strict=True refuses a bad name with a
    sentence saying why; strict=False repairs it, for names that arrive from
    someone else's computer."""
    n = (name or "").strip()
    if not strict:
        n = _BAD_CHARS.sub("_", n).rstrip(" .")
        if n.split(".")[0].upper() in _RESERVED:
            n = "_" + n
        if len(n.encode("utf-8")) > 240:
            stem, dot, ext = n.rpartition(".")
            n = (stem[:150] + dot + ext[:20]) if dot else n[:150]
        return n or "unnamed"
    if not n:
        raise HTTPException(400, "Give it a name.")
    if n in (".", ".."):
        raise HTTPException(400, "That name is not allowed.")
    bad = _BAD_CHARS.search(n)
    if bad:
        ch = bad.group(0)
        shown = "a slash" if ch in "/\\" else f"'{ch}'" if ch.isprintable() else "a control character"
        raise HTTPException(400, f"Names cannot contain {shown}.")
    if n.endswith((" ", ".")):
        raise HTTPException(400, "Names cannot end with a space or a dot.")
    if n.split(".")[0].upper() in _RESERVED:
        raise HTTPException(400, f"'{n}' is a reserved name on Windows.")
    if len(n.encode("utf-8")) > 240:
        raise HTTPException(400, "That name is too long.")
    if n.lower() == INTERNAL:
        raise HTTPException(400, "That name is reserved.")
    return n


def clean_rel(rel: Optional[str]) -> str:
    """Normalise a client path to 'a/b/c' ('' is the root). Refuses traversal
    and the internal folder."""
    rel = (rel or "").replace("\\", "/")
    if "\x00" in rel:
        raise HTTPException(400, "Invalid path.")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise HTTPException(400, "Invalid path.")
    if parts and parts[0].lower() == INTERNAL:
        raise HTTPException(404, "Not found.")
    return "/".join(parts)


def _within(child: str, parent: str) -> bool:
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:
        return False


def abs_path(rel: Optional[str], *, must_exist: bool = True) -> Path:
    root = files_root()
    rel = clean_rel(rel)
    p = root.joinpath(*rel.split("/")) if rel else root
    real_root = str(root)
    # A symlink that points outside the root must not be followed.
    probe = p if p.exists() or p.is_symlink() else p.parent
    if not _within(os.path.realpath(probe), real_root):
        raise HTTPException(403, "That path is outside the Files folder.")
    if must_exist and not (p.exists()):
        raise HTTPException(404, "Not found.")
    return p


def rel_of(p: Path) -> str:
    root = files_root()
    try:
        r = p.relative_to(root)
    except ValueError:
        r = Path(os.path.relpath(p, root))
    s = str(r).replace(os.sep, "/")
    return "" if s == "." else s


def unique_name(directory: Path, name: str) -> str:
    """'report.pdf' -> 'report (1).pdf' when that name is taken."""
    if not os.path.lexists(directory / name):
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    else:
        ext = "." + ext
    for i in range(1, 10000):
        cand = f"{stem} ({i}){ext}"
        if not os.path.lexists(directory / cand):
            return cand
    return f"{stem} ({uuid.uuid4().hex[:6]}){ext}"


# ---------------------------------------------------------------------------
# What kind of thing is it
# ---------------------------------------------------------------------------
_KINDS = {
    "image": {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "tif", "tiff", "heic", "heif",
              "avif", "ico", "dng", "arw", "cr2", "cr3", "nef", "raf", "orf", "rw2", "psd"},
    "video": {"mp4", "mov", "mkv", "avi", "webm", "m4v", "wmv", "flv", "mts", "m2ts", "mxf",
              "braw", "r3d", "3gp", "mpg", "mpeg", "ts", "prores"},
    "audio": {"mp3", "wav", "flac", "aac", "m4a", "ogg", "opus", "wma", "aiff", "aif", "mid"},
    "pdf": {"pdf"},
    "document": {"doc", "docx", "odt", "rtf", "pages", "epub", "mobi", "ppt", "pptx", "odp", "key"},
    "sheet": {"xls", "xlsx", "ods", "csv", "tsv", "numbers"},
    "code": {"py", "js", "jsx", "ts", "tsx", "json", "html", "htm", "css", "scss", "c", "h", "cpp",
             "hpp", "cs", "java", "kt", "go", "rs", "rb", "php", "sh", "bat", "cmd", "ps1", "lua",
             "sql", "xml", "yml", "yaml", "toml", "ini", "cfg", "conf", "gradle", "vue", "swift"},
    "text": {"txt", "md", "log", "nfo", "srt", "vtt", "ass", "readme"},
    "archive": {"zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "zst", "cab", "lz"},
    "disk": {"iso", "img", "bin", "cue", "vhd", "vhdx", "vmdk", "vdi", "wim", "dmg"},
    "app": {"exe", "msi", "apk", "appimage", "deb", "rpm", "jar", "pkg", "msix", "appx"},
    "font": {"ttf", "otf", "woff", "woff2", "fon"},
    "project": {"drp", "drx", "prproj", "aep", "psb", "blend", "fcpxml", "xcf", "kra", "fig",
                "unitypackage", "uproject", "sln", "resolve"},
}
_EXT_KIND = {ext: kind for kind, exts in _KINDS.items() for ext in exts}
_TEXT_MIME_KINDS = {"code", "text"}


def kind_of(name: str, is_dir: bool) -> str:
    if is_dir:
        return "folder"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return _EXT_KIND.get(ext, "other")


def mime_of(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def _bearer(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return None


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Header token only - for everything that changes something."""
    tok = _bearer(request)
    if not tok:
        raise HTTPException(401, "Not signed in.")
    return get_user_from_token(tok, db)


def current_user_qs(request: Request, token: Optional[str] = None,
                    db: Session = Depends(get_db)) -> User:
    """Header or ?token= - for the read-only URLs a browser opens directly
    (downloads, previews, thumbnails)."""
    tok = _bearer(request) or token
    if not tok:
        raise HTTPException(401, "Not signed in.")
    return get_user_from_token(tok, db)


def _need(user: User, cap: str):
    if not permissions.can(user.role, cap):
        raise HTTPException(403, permissions.DENIALS.get(cap, "Your account does not have access to that."))


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------
def _entry(p: Path, st: Optional[os.stat_result] = None, *, is_dir: Optional[bool] = None) -> dict:
    if st is None:
        st = p.stat()
    if is_dir is None:
        is_dir = p.is_dir()
    name = p.name
    kind = kind_of(name, is_dir)
    return {
        "name": name,
        "path": rel_of(p),
        "is_dir": is_dir,
        "size": 0 if is_dir else st.st_size,
        "mtime": int(st.st_mtime),
        "kind": kind,
        "ext": "" if is_dir else _ext(name),
        "mime": "" if is_dir else mime_of(name),
        "thumb": (not is_dir) and kind in ("image", "video"),
    }


def _annotate(db: Session, user: User, entries: List[dict]):
    """Add starred / shared / uploaded_by using a few batched queries."""
    if not entries:
        return
    paths = [e["path"] for e in entries]
    starred, shared, owners = set(), {}, {}
    for i in range(0, len(paths), 500):
        chunk = paths[i:i + 500]
        for (p,) in db.query(FileStar.path).filter(FileStar.user_id == user.id, FileStar.path.in_(chunk)):
            starred.add(p)
        for s in db.query(FileShare).filter(FileShare.path.in_(chunk), FileShare.revoked == False):  # noqa: E712
            if s.expires_at and s.expires_at < datetime.utcnow():
                continue
            shared[s.path] = shared.get(s.path, 0) + 1
        for m in db.query(FileMeta).filter(FileMeta.path.in_(chunk)):
            owners[m.path] = m.owner
    for e in entries:
        e["starred"] = e["path"] in starred
        e["shared"] = shared.get(e["path"], 0)
        e["uploaded_by"] = owners.get(e["path"])


def _breadcrumbs(rel: str) -> List[dict]:
    crumbs, acc = [], []
    for part in [p for p in rel.split("/") if p]:
        acc.append(part)
        crumbs.append({"name": part, "path": "/".join(acc)})
    return crumbs


def _record(db: Session, rel: str, owner: Optional[str], is_dir: bool, size: int = 0):
    row = db.query(FileMeta).filter(FileMeta.path == rel).first()
    if row:
        row.owner, row.is_dir, row.size, row.created_at = owner, is_dir, size, datetime.utcnow()
    else:
        db.add(FileMeta(path=rel, owner=owner, is_dir=is_dir, size=size))


def _rebase(db: Session, old: str, new: Optional[str]):
    """A path moved or was deleted: fix up (or drop) everything that points at
    it or beneath it. new=None drops."""
    for model in (FileMeta, FileStar, FileShare):
        q = db.query(model).filter((model.path == old) | model.path.startswith(old + "/", autoescape=True))
        for row in q.all():
            if new is None:
                if model is not FileShare:
                    db.delete(row)
            else:
                row.path = new + row.path[len(old):]
                if model is FileShare and row.path == new:
                    row.name = Path(new).name


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------
_maint_at = 0.0
_maint_lock = threading.Lock()


def _maintenance():
    """Old trash and abandoned uploads. Cheap, and run at most hourly, from
    whichever request happens to arrive."""
    global _maint_at
    now = time.time()
    if now - _maint_at < 3600:
        return
    with _maint_lock:
        if now - _maint_at < 3600:
            return
        _maint_at = now
    try:
        db = SessionLocal()
        try:
            cutoff = datetime.utcnow() - timedelta(days=TRASH_DAYS)
            for row in db.query(FileTrash).filter(FileTrash.deleted_at < cutoff).all():
                shutil.rmtree(_internal("trash") / row.slot, ignore_errors=True)
                db.delete(row)
            db.commit()
        finally:
            db.close()
        up = _internal("uploads")
        for f in up.iterdir():
            try:
                if now - f.stat().st_mtime > STALE_UPLOAD_SECONDS:
                    f.unlink()
            except OSError:
                pass
    except Exception as e:                                        # pragma: no cover
        print(f"  [files] maintenance skipped: {e}", flush=True)


_usage = {"used": 0, "files": 0, "folders": 0, "at": 0.0, "busy": False}


def _compute_usage():
    root = files_root()
    used = files = folders = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            if dirpath == str(root):
                dirnames[:] = [d for d in dirnames if d.lower() != INTERNAL]
            folders += len(dirnames)
            for fn in filenames:
                try:
                    used += os.lstat(os.path.join(dirpath, fn)).st_size
                    files += 1
                except OSError:
                    pass
    finally:
        _usage.update(used=used, files=files, folders=folders, at=time.time(), busy=False)


@router.get("/usage")
def usage(user: User = Depends(current_user)):
    root = files_root()
    if not _usage["busy"] and time.time() - _usage["at"] > 120:
        _usage["busy"] = True
        threading.Thread(target=_compute_usage, daemon=True).start()
    try:
        total, _, free = shutil.disk_usage(root)
    except OSError:
        total = free = 0
    return {"used": _usage["used"], "files": _usage["files"], "folders": _usage["folders"],
            "computing": _usage["at"] == 0.0, "disk_total": total, "disk_free": free,
            "root_name": root.name}


# ---------------------------------------------------------------------------
# Browsing
# ---------------------------------------------------------------------------
@router.get("/list")
def list_dir(path: str = "", show_hidden: bool = False,
             user: User = Depends(current_user), db: Session = Depends(get_db)):
    _maintenance()
    d = abs_path(path)
    if not d.is_dir():
        raise HTTPException(400, "That is a file, not a folder.")
    root = files_root()
    entries, truncated = [], False
    with os.scandir(d) as it:
        for e in it:
            if d == root and e.name.lower() == INTERNAL:
                continue
            if e.name.startswith(".") and not show_hidden:
                continue
            try:
                st = e.stat()
                is_dir = e.is_dir()
            except OSError:
                continue          # broken link, vanished mid-listing
            entries.append(_entry(Path(e.path), st, is_dir=is_dir))
            if len(entries) >= MAX_LIST:
                truncated = True
                break
    _annotate(db, user, entries)
    rel = rel_of(d)
    return {"path": rel, "name": d.name if rel else "All files", "breadcrumbs": _breadcrumbs(rel),
            "entries": entries, "truncated": truncated}


@router.get("/tree")
def tree(path: str = "", user: User = Depends(current_user)):
    """Sub-folders only, for the folder tree. Has-children is a cheap peek."""
    d = abs_path(path)
    root = files_root()
    out = []
    with os.scandir(d) as it:
        for e in it:
            if e.name.startswith(".") or (d == root and e.name.lower() == INTERNAL):
                continue
            try:
                if not e.is_dir():
                    continue
            except OSError:
                continue
            has_children = False
            try:
                with os.scandir(e.path) as sub:
                    for i, s in enumerate(sub):
                        if i > 300:
                            has_children = True
                            break
                        if s.name.startswith("."):
                            continue
                        if s.is_dir():
                            has_children = True
                            break
            except OSError:
                pass
            out.append({"name": e.name, "path": rel_of(Path(e.path)), "has_children": has_children})
    out.sort(key=lambda x: x["name"].lower())
    return {"path": rel_of(d), "folders": out}


@router.get("/search")
def search(q: str, path: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    terms = [t for t in q.lower().split() if t]
    if not terms:
        return {"entries": [], "truncated": False}
    base = abs_path(path)
    root = files_root()
    deadline = time.time() + 8
    entries, truncated = [], False
    for dirpath, dirnames, filenames in os.walk(base):
        if Path(dirpath) == root:
            dirnames[:] = [d for d in dirnames if d.lower() != INTERNAL]
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in list(dirnames) + filenames:
            low = name.lower()
            if all(t in low for t in terms):
                p = Path(dirpath) / name
                try:
                    entries.append(_entry(p))
                except OSError:
                    continue
                if len(entries) >= 300:
                    truncated = True
                    break
        if truncated or time.time() > deadline:
            truncated = truncated or time.time() > deadline
            break
    _annotate(db, user, entries)
    return {"entries": entries, "truncated": truncated}


@router.get("/recent")
def recent(limit: int = 60, user: User = Depends(current_user), db: Session = Depends(get_db)):
    out = []
    for m in db.query(FileMeta).filter(FileMeta.is_dir == False).order_by(  # noqa: E712
            FileMeta.created_at.desc()).limit(min(limit, 200) * 2):
        try:
            p = abs_path(m.path)
            e = _entry(p)
        except (HTTPException, OSError):
            continue
        e["added_at"] = int(m.created_at.timestamp()) if m.created_at else e["mtime"]
        out.append(e)
        if len(out) >= limit:
            break
    _annotate(db, user, out)
    return {"entries": out}


@router.get("/starred")
def starred(user: User = Depends(current_user), db: Session = Depends(get_db)):
    out = []
    for s in db.query(FileStar).filter(FileStar.user_id == user.id).all():
        try:
            out.append(_entry(abs_path(s.path)))
        except (HTTPException, OSError):
            continue
    _annotate(db, user, out)
    out.sort(key=lambda e: e["name"].lower())
    return {"entries": out}


@router.get("/info")
def info(path: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = abs_path(path)
    e = _entry(p)
    _annotate(db, user, [e])
    if p.is_dir():
        deadline = time.time() + 10
        size = files = folders = 0
        partial = False
        for dirpath, dirnames, filenames in os.walk(p):
            folders += len(dirnames)
            for fn in filenames:
                try:
                    size += os.lstat(os.path.join(dirpath, fn)).st_size
                    files += 1
                except OSError:
                    pass
            if time.time() > deadline:
                partial = True
                break
        e.update(size=size, files=files, folders=folders, partial=partial)
    return e


# ---------------------------------------------------------------------------
# Serving files
# ---------------------------------------------------------------------------
def _disposition(name: str, inline: bool) -> str:
    ascii_name = re.sub(r'[^\x20-\x7e]', "_", name).replace('"', "'").replace("\\", "_")
    kind = "inline" if inline else "attachment"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


def _inline_allowed(mime: str) -> bool:
    return mime.startswith(("image/", "video/", "audio/")) or mime == "application/pdf"


def serve_file(request: Request, p: Path, *, inline: bool = False, count_cb=None) -> Response:
    """A file with proper Range support, so videos seek and big downloads
    resume. Inline display is limited to types that cannot run script."""
    st = p.stat()
    size = st.st_size
    name = p.name
    mime = mime_of(name)
    if inline and not _inline_allowed(mime):
        inline = False
    start, end, status = 0, size - 1, 200
    rng = request.headers.get("range")
    if rng and size:
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
        if m and (m.group(1) or m.group(2)):
            a, b = m.groups()
            if a == "":
                start, end = max(0, size - int(b)), size - 1
            else:
                start = int(a)
                end = int(b) if b else size - 1
            if start >= size or start > end:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            end = min(end, size - 1)
            status = 206
    length = max(0, end - start + 1)
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Disposition": _disposition(name, inline),
        "ETag": f'"{st.st_mtime_ns:x}-{size:x}"',
        "Cache-Control": "private, no-cache",
        "X-Content-Type-Options": "nosniff",
    }
    if mime == "image/svg+xml" and inline:
        headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if count_cb and start == 0:
        count_cb()

    def body():
        with open(p, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(4 * 1024 * 1024, left))
                if not chunk:
                    break
                left -= len(chunk)
                yield chunk

    return StreamingResponse(body(), status_code=status, headers=headers, media_type=mime)


@router.get("/download")
def download(request: Request, path: str, inline: bool = False,
             user: User = Depends(current_user_qs)):
    _need(user, permissions.DOWNLOAD)
    p = abs_path(path)
    if p.is_dir():
        raise HTTPException(400, "That is a folder - use the zip download.")
    return serve_file(request, p, inline=inline)


_thumb_sem = threading.Semaphore(3)


def _thumb_file(p: Path, w: int) -> Path:
    """The cached thumbnail for p, made now if need be. 415 when it cannot be."""
    st = p.stat()
    key = hashlib.sha1(f"{p}|{st.st_mtime_ns}|{st.st_size}|{w}".encode()).hexdigest()
    out = _internal("thumbs") / key[:2] / f"{key}.jpg"
    if out.exists() and out.stat().st_size > 0:
        return out
    fail = out.with_suffix(".fail")
    if fail.exists():
        raise HTTPException(415, "No preview for this file.")
    kind = kind_of(p.name, False)
    if kind not in ("image", "video"):
        raise HTTPException(415, "No preview for this file.")
    out.parent.mkdir(parents=True, exist_ok=True)
    with _thumb_sem:
        ok, why = previews.make_thumbnail(str(p), str(out), "video" if kind == "video" else "photo", w)
    if not ok:
        try:
            fail.write_text(why or "failed")
        except OSError:
            pass
        raise HTTPException(415, "No preview for this file.")
    return out


@router.get("/thumb")
def thumb(path: str, w: int = 360, user: User = Depends(current_user_qs)):
    _need(user, permissions.READ)
    p = abs_path(path)
    if p.is_dir():
        raise HTTPException(415, "No preview for this file.")
    out = _thumb_file(p, max(120, min(int(w), 1600)))
    return FileResponse(str(out), media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})


@router.get("/text")
def text_preview(path: str, user: User = Depends(current_user_qs)):
    _need(user, permissions.READ)
    p = abs_path(path)
    if p.is_dir():
        raise HTTPException(400, "That is a folder.")
    size = p.stat().st_size
    with open(p, "rb") as f:
        data = f.read(512 * 1024)
    if b"\x00" in data[:8192]:
        raise HTTPException(415, "This looks like a binary file, so there is no text to show.")
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = data.decode("utf-16", errors="replace")
    else:
        text = data.decode("utf-8-sig", errors="replace")
    return {"text": text, "truncated": size > len(data), "size": size}


# ---------------------------------------------------------------------------
# Zip (streamed: nothing is built in memory or on disk first)
# ---------------------------------------------------------------------------
class _Sink:
    def __init__(self):
        self.buf, self.pos = [], 0

    def write(self, b):
        self.buf.append(bytes(b))
        self.pos += len(b)
        return len(b)

    def tell(self):
        return self.pos

    def flush(self):
        pass

    def drain(self) -> bytes:
        out = b"".join(self.buf)
        self.buf.clear()
        return out


def _zip_time(ts: float):
    t = time.localtime(max(ts, 315532800))          # zip cannot hold pre-1980
    return (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)


def _zip_items(bases: List[Path]):
    """(absolute path, archive name, is_dir) for everything to include."""
    for base in bases:
        if base.is_dir():
            top = base.name
            yield base, top, True
            root = files_root()
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = sorted(d for d in dirnames
                                     if not os.path.islink(os.path.join(dirpath, d))
                                     and not (Path(dirpath) == root and d.lower() == INTERNAL))
                for d in dirnames:
                    dp = Path(dirpath) / d
                    yield dp, f"{top}/{os.path.relpath(dp, base)}".replace(os.sep, "/"), True
                for fn in sorted(filenames):
                    fp = Path(dirpath) / fn
                    if os.path.islink(fp):
                        continue
                    yield fp, f"{top}/{os.path.relpath(fp, base)}".replace(os.sep, "/"), False
        elif not os.path.islink(base):
            yield base, base.name, False


def zip_stream(bases: List[Path], on_start=None):
    sink = _Sink()
    zf = zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True)
    if on_start:
        on_start()
    for p, arc, is_dir in _zip_items(bases):
        try:
            st = p.stat()
        except OSError:
            continue
        if is_dir:
            zi = zipfile.ZipInfo(arc.rstrip("/") + "/", date_time=_zip_time(st.st_mtime))
            zi.external_attr = (0o40755 << 16) | 0x10
            zf.writestr(zi, b"")
            data = sink.drain()
            if data:
                yield data
            continue
        zi = zipfile.ZipInfo(arc, date_time=_zip_time(st.st_mtime))
        zi.compress_type = zipfile.ZIP_STORED
        zi.external_attr = 0o644 << 16
        try:
            with open(p, "rb") as src, zf.open(zi, "w", force_zip64=True) as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
                    data = sink.drain()
                    if data:
                        yield data
        except OSError:
            continue                # vanished or unreadable mid-zip: skip it
        data = sink.drain()
        if data:
            yield data
    zf.close()
    data = sink.drain()
    if data:
        yield data


def _zip_response(bases: List[Path], download_name: str, on_start=None) -> StreamingResponse:
    return StreamingResponse(
        zip_stream(bases, on_start), media_type="application/zip",
        headers={"Content-Disposition": _disposition(download_name, False),
                 "Cache-Control": "private, no-cache"})


@router.get("/zip")
def zip_download(request: Request, user: User = Depends(current_user_qs)):
    _need(user, permissions.DOWNLOAD)
    raw = request.query_params.getlist("path")
    if not raw:
        raise HTTPException(400, "Nothing to download.")
    bases = [abs_path(r) for r in raw]
    root = files_root()
    name = (bases[0].name if len(bases) == 1 and bases[0] != root else "Zerko files") + ".zip"
    return _zip_response(bases, name)


# ---------------------------------------------------------------------------
# Changing things
# ---------------------------------------------------------------------------
class MkdirBody(BaseModel):
    dir: str = ""
    name: str


@router.post("/mkdir")
def mkdir(body: MkdirBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _need(user, permissions.UPLOAD)
    parent = abs_path(body.dir)
    if not parent.is_dir():
        raise HTTPException(400, "That is not a folder.")
    name = clean_name(body.name)
    target = parent / name
    if os.path.lexists(target):
        raise HTTPException(409, f"'{name}' already exists here.")
    target.mkdir()
    _record(db, rel_of(target), user.username, True)
    db.commit()
    return _entry(target)


class RenameBody(BaseModel):
    path: str
    name: str


@router.post("/rename")
def rename(body: RenameBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _need(user, permissions.UPLOAD)
    src = abs_path(body.path)
    if src == files_root():
        raise HTTPException(400, "The top folder cannot be renamed.")
    name = clean_name(body.name)
    dst = src.parent / name
    if os.path.lexists(dst):
        try:
            same = os.path.samefile(src, dst)
        except OSError:
            same = False
        if not same:
            raise HTTPException(409, f"'{name}' already exists here.")
    old_rel = rel_of(src)
    os.rename(src, dst)
    _rebase(db, old_rel, rel_of(dst))
    db.commit()
    return _entry(dst)


class MoveBody(BaseModel):
    paths: List[str]
    dest: str = ""
    mode: str = "move"          # move | copy


@router.post("/move")
def move(body: MoveBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _need(user, permissions.UPLOAD)
    dest = abs_path(body.dest)
    if not dest.is_dir():
        raise HTTPException(400, "The destination is not a folder.")
    dest_rel = rel_of(dest)
    results = []
    for raw in body.paths:
        try:
            src = abs_path(raw)
            src_rel = rel_of(src)
            if src == files_root():
                raise HTTPException(400, "The top folder cannot be moved.")
            if src.is_dir() and (dest_rel == src_rel or dest_rel.startswith(src_rel + "/")):
                raise HTTPException(400, f"Cannot put '{src.name}' inside itself.")
            if src.parent == dest and not (body.mode == "copy"):
                results.append({"path": src_rel, "ok": True, "new_path": src_rel})
                continue
            name = unique_name(dest, src.name)
            target = dest / name
            if (body.mode == "copy"):
                if src.is_dir():
                    shutil.copytree(src, target, symlinks=True)
                else:
                    shutil.copy2(src, target)
                _record(db, rel_of(target), user.username, src.is_dir(),
                        0 if src.is_dir() else target.stat().st_size)
            else:
                shutil.move(str(src), str(target))
                _rebase(db, src_rel, rel_of(target))
            results.append({"path": src_rel, "ok": True, "new_path": rel_of(target)})
        except HTTPException as e:
            results.append({"path": raw, "ok": False, "error": e.detail})
        except OSError as e:
            results.append({"path": raw, "ok": False, "error": f"{e.strerror or e}"})
    db.commit()
    return {"results": results}


class PathsBody(BaseModel):
    paths: List[str]


def _tree_size(p: Path) -> int:
    if not p.is_dir():
        try:
            return p.stat().st_size
        except OSError:
            return 0
    total = 0
    for dirpath, _d, filenames in os.walk(p):
        for fn in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, fn)).st_size
            except OSError:
                pass
    return total


@router.post("/delete")
def delete(body: PathsBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Moves to the trash; nothing is destroyed here."""
    _need(user, permissions.UPLOAD)
    results = []
    for raw in body.paths:
        try:
            src = abs_path(raw)
            if src == files_root():
                raise HTTPException(400, "The top folder cannot be deleted.")
            rel = rel_of(src)
            is_dir = src.is_dir()
            size = _tree_size(src)
            slot = uuid.uuid4().hex
            slot_dir = _internal("trash") / slot
            slot_dir.mkdir(parents=True)
            shutil.move(str(src), str(slot_dir / src.name))
            row = FileTrash(orig_path=rel, slot=slot, name=src.name, is_dir=is_dir,
                            size=size, deleted_by=user.username)
            db.add(row)
            db.flush()                                    # so the id can be handed back for Undo
            _rebase(db, rel, None)
            results.append({"path": rel, "ok": True, "trash_id": row.id})
        except HTTPException as e:
            results.append({"path": raw, "ok": False, "error": e.detail})
        except OSError as e:
            results.append({"path": raw, "ok": False, "error": f"{e.strerror or e}"})
    db.commit()
    return {"results": results}


class StarBody(BaseModel):
    path: str
    starred: bool = True


@router.post("/star")
def star(body: StarBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = abs_path(body.path)
    rel = rel_of(p)
    row = db.query(FileStar).filter(FileStar.user_id == user.id, FileStar.path == rel).first()
    if body.starred and not row:
        db.add(FileStar(user_id=user.id, path=rel))
    elif not body.starred and row:
        db.delete(row)
    db.commit()
    return {"path": rel, "starred": body.starred}


# ---------------------------------------------------------------------------
# Trash
# ---------------------------------------------------------------------------
def _trash_row(r: FileTrash) -> dict:
    return {"id": r.id, "name": r.name, "orig_path": r.orig_path, "is_dir": r.is_dir,
            "size": r.size or 0, "deleted_by": r.deleted_by,
            "deleted_at": int(r.deleted_at.timestamp()) if r.deleted_at else 0,
            "kind": kind_of(r.name, r.is_dir),
            "expires_at": int((r.deleted_at + timedelta(days=TRASH_DAYS)).timestamp()) if r.deleted_at else 0}


@router.get("/trash")
def trash_list(user: User = Depends(current_user), db: Session = Depends(get_db)):
    _maintenance()
    rows = db.query(FileTrash).order_by(FileTrash.deleted_at.desc()).all()
    return {"items": [_trash_row(r) for r in rows], "days": TRASH_DAYS,
            "total_size": sum(r.size or 0 for r in rows)}


class IdsBody(BaseModel):
    ids: List[int] = []


@router.post("/trash/restore")
def trash_restore(body: IdsBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _need(user, permissions.UPLOAD)
    results = []
    for rid in body.ids:
        row = db.query(FileTrash).filter(FileTrash.id == rid).first()
        if not row:
            results.append({"id": rid, "ok": False, "error": "Not in the trash any more."})
            continue
        try:
            held = _internal("trash") / row.slot / row.name
            if not held.exists() and not os.path.islink(held):
                db.delete(row)
                results.append({"id": rid, "ok": False, "error": "The file is gone from the trash folder."})
                continue
            parent_rel = str(Path(row.orig_path).parent).replace("\\", "/")
            parent_rel = "" if parent_rel == "." else parent_rel
            parent = abs_path(parent_rel, must_exist=False)
            parent.mkdir(parents=True, exist_ok=True)
            name = unique_name(parent, row.name)
            shutil.move(str(held), str(parent / name))
            shutil.rmtree(_internal("trash") / row.slot, ignore_errors=True)
            db.delete(row)
            results.append({"id": rid, "ok": True, "path": rel_of(parent / name)})
        except (HTTPException, OSError) as e:
            results.append({"id": rid, "ok": False, "error": getattr(e, "detail", None) or str(e)})
    db.commit()
    return {"results": results}


@router.post("/trash/purge")
def trash_purge(body: IdsBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _need(user, permissions.HARD_DELETE)
    n = 0
    for rid in body.ids:
        row = db.query(FileTrash).filter(FileTrash.id == rid).first()
        if row:
            shutil.rmtree(_internal("trash") / row.slot, ignore_errors=True)
            db.delete(row)
            n += 1
    db.commit()
    return {"purged": n}


@router.post("/trash/empty")
def trash_empty(user: User = Depends(current_user), db: Session = Depends(get_db)):
    _need(user, permissions.HARD_DELETE)
    n = 0
    for row in db.query(FileTrash).all():
        shutil.rmtree(_internal("trash") / row.slot, ignore_errors=True)
        db.delete(row)
        n += 1
    db.commit()
    return {"purged": n}


# ---------------------------------------------------------------------------
# Uploading - resumable, in order, one chunk per request
#
# The id is derived from who/where/what/how-big/when-modified, so choosing the
# same file again after a dropped connection or a closed tab finds the half
# that already arrived and carries on from there.
# ---------------------------------------------------------------------------
class InitBody(BaseModel):
    dir: str = ""
    name: str
    size: int
    mtime: Optional[int] = None            # ms since epoch, the file's own modified time
    relative_path: Optional[str] = None    # 'Folder/Sub/file.ext' when a folder was dropped
    conflict: str = "rename"               # rename | replace


def _up_paths(uid: str):
    if not re.fullmatch(r"[0-9a-f]{32}", uid):
        raise HTTPException(404, "Unknown upload.")
    base = _internal("uploads")
    return base / f"{uid}.part", base / f"{uid}.json"


def _load_meta(uid: str, user: User) -> dict:
    import json
    part, meta = _up_paths(uid)
    if not meta.exists():
        raise HTTPException(404, "That upload is no longer available - start it again.")
    m = json.loads(meta.read_text(encoding="utf-8"))
    if m.get("user_id") != user.id and user.role != "admin":
        raise HTTPException(403, "That upload belongs to someone else.")
    return m


@router.post("/upload/init")
def upload_init(body: InitBody, user: User = Depends(current_user)):
    import json
    _need(user, permissions.UPLOAD)
    if body.size < 0:
        raise HTTPException(400, "Bad size.")
    base = abs_path(body.dir)
    if not base.is_dir():
        raise HTTPException(400, "The destination is not a folder.")

    # Folder structure that came with the file is recreated, repaired name by name.
    name = clean_name(body.name, strict=False)
    dest = base
    if body.relative_path:
        parts = [p for p in body.relative_path.replace("\\", "/").split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise HTTPException(400, "Invalid path.")
        if parts:
            name = clean_name(parts[-1], strict=False)
            for seg in parts[:-1]:
                dest = dest / clean_name(seg, strict=False)
                abs_path(rel_of(dest), must_exist=False)    # containment, before creating anything
                dest.mkdir(exist_ok=True)
    dest_rel = rel_of(dest)

    free = shutil.disk_usage(files_root()).free
    uid = hashlib.sha1(f"{user.id}|{dest_rel}|{name}|{body.size}|{body.mtime}".encode()).hexdigest()[:32]
    part, meta = _up_paths(uid)
    have = part.stat().st_size if part.exists() and meta.exists() else 0
    if have > body.size:
        part.unlink(missing_ok=True)
        have = 0
    if body.size - have + 64 * 1024 * 1024 > free:
        raise HTTPException(507, "Not enough free space on the drive for this file.")
    if not meta.exists():
        part.unlink(missing_ok=True)
        part.touch()
    meta.write_text(json.dumps({
        "user_id": user.id, "username": user.username, "dir": dest_rel, "name": name,
        "size": body.size, "mtime": body.mtime,
        "conflict": "replace" if body.conflict == "replace" else "rename",
    }), encoding="utf-8")
    return {"upload_id": uid, "offset": have, "chunk_size": CHUNK_SIZE, "name": name, "dir": dest_rel}


_up_locks = {}


@router.put("/upload/{uid}")
async def upload_chunk(uid: str, request: Request, offset: int,
                       user: User = Depends(current_user)):
    _need(user, permissions.UPLOAD)
    import asyncio
    m = _load_meta(uid, user)
    part, _ = _up_paths(uid)
    lock = _up_locks.setdefault(uid, asyncio.Lock())
    async with lock:
        cur = part.stat().st_size if part.exists() else 0
        if offset != cur:
            return JSONResponse(status_code=409, content={
                "detail": "Out of step - resuming from where the server is.", "expected": cur})
        room = m["size"] - cur
        written = 0
        with open(part, "ab") as f:
            async for chunk in request.stream():
                if not chunk:
                    continue
                written += len(chunk)
                if written > room:
                    raise HTTPException(413, "More data than the file was declared to hold.")
                await run_in_threadpool(f.write, chunk)
    return {"offset": cur + written, "size": m["size"]}


@router.post("/upload/{uid}/complete")
def upload_complete(uid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _need(user, permissions.UPLOAD)
    m = _load_meta(uid, user)
    part, meta = _up_paths(uid)
    got = part.stat().st_size if part.exists() else 0
    if got != m["size"]:
        raise HTTPException(409, f"Only {got} of {m['size']} bytes arrived - resume the upload.")
    dest = abs_path(m["dir"], must_exist=False)
    dest.mkdir(parents=True, exist_ok=True)
    if m.get("conflict") == "replace" and (dest / m["name"]).is_file():
        final = dest / m["name"]
        os.replace(part, final)
    else:
        final = dest / unique_name(dest, m["name"])
        os.replace(part, final)
    if m.get("mtime"):
        try:
            ts = m["mtime"] / 1000.0
            os.utime(final, (ts, ts))
        except (OSError, ValueError, OverflowError):
            pass
    meta.unlink(missing_ok=True)
    _up_locks.pop(uid, None)
    _record(db, rel_of(final), m.get("username"), False, m["size"])
    db.commit()
    return _entry(final)


@router.delete("/upload/{uid}")
def upload_cancel(uid: str, user: User = Depends(current_user)):
    _need(user, permissions.UPLOAD)
    _load_meta(uid, user)
    part, meta = _up_paths(uid)
    part.unlink(missing_ok=True)
    meta.unlink(missing_ok=True)
    _up_locks.pop(uid, None)
    return {"cancelled": True}


# ---------------------------------------------------------------------------
# Share links
# ---------------------------------------------------------------------------
class ShareBody(BaseModel):
    path: str
    password: Optional[str] = None
    expires_days: Optional[int] = None


def _share_row(s: FileShare) -> dict:
    expired = bool(s.expires_at and s.expires_at < datetime.utcnow())
    return {"id": s.id, "token": s.token, "url_path": f"/f/{s.token}", "path": s.path,
            "name": s.name, "is_dir": s.is_dir, "has_password": bool(s.password_hash),
            "expires_at": int(s.expires_at.timestamp()) if s.expires_at else None,
            "expired": expired, "revoked": bool(s.revoked), "created_by": s.created_by,
            "created_at": int(s.created_at.timestamp()) if s.created_at else 0,
            "views": s.view_count or 0, "downloads": s.download_count or 0}


@router.post("/shares")
def share_create(body: ShareBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _need(user, permissions.SHARES)
    p = abs_path(body.path)
    if p == files_root():
        raise HTTPException(400, "Share a folder or file, not the whole storage.")
    if body.expires_days is not None and not (1 <= body.expires_days <= 3650):
        raise HTTPException(400, "Expiry must be between 1 day and 10 years.")
    pw = (body.password or "").strip()
    if pw and len(pw) < 4:
        raise HTTPException(400, "Use at least 4 characters for a link password.")
    s = FileShare(token=secrets.token_urlsafe(24), path=rel_of(p), is_dir=p.is_dir(), name=p.name,
                  password_hash=get_password_hash(pw) if pw else None,
                  expires_at=(datetime.utcnow() + timedelta(days=body.expires_days)) if body.expires_days else None,
                  created_by=user.username)
    db.add(s)
    db.commit()
    db.refresh(s)
    return _share_row(s)


@router.get("/shares")
def share_list(path: Optional[str] = None, user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    q = db.query(FileShare).filter(FileShare.revoked == False)  # noqa: E712
    if path is not None:
        q = q.filter(FileShare.path == clean_rel(path))
    elif user.role != "admin":
        q = q.filter(FileShare.created_by == user.username)
    return {"shares": [_share_row(s) for s in q.order_by(FileShare.created_at.desc()).all()]}


@router.delete("/shares/{sid}")
def share_revoke(sid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _need(user, permissions.SHARES)
    s = db.query(FileShare).filter(FileShare.id == sid).first()
    if not s:
        raise HTTPException(404, "No such link.")
    if s.created_by != user.username and user.role != "admin":
        raise HTTPException(403, "Only the person who made a link, or an administrator, can turn it off.")
    s.revoked = True
    db.commit()
    return {"revoked": True}


# ---------------------------------------------------------------------------
# What a friend sees (no account)
# ---------------------------------------------------------------------------
_share_fails = {}
_SHARE_MAX_FAILS = 10
_SHARE_WINDOW = 15 * 60


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _throttled(token: str, ip: str) -> bool:
    now = time.time()
    hits = [t for t in _share_fails.get((token, ip), []) if now - t < _SHARE_WINDOW]
    _share_fails[(token, ip)] = hits
    if len(_share_fails) > 5000:
        for k in [k for k, v in _share_fails.items() if not v or now - v[-1] > _SHARE_WINDOW]:
            _share_fails.pop(k, None)
    return len(hits) >= _SHARE_MAX_FAILS


def _share_access(s: FileShare) -> str:
    return hmac.new(SECRET_KEY.encode(), f"{s.token}|{s.password_hash}".encode(), "sha256").hexdigest()[:40]


def _open_share(db: Session, request: Request, token: str, password: Optional[str]):
    """(share, state) where state is 'ok', 'locked' or 'wrong'. Raises for a
    link that does not exist, is off, or has run out."""
    s = db.query(FileShare).filter(FileShare.token == token).first()
    if not s or s.revoked:
        raise HTTPException(404, "This link is not available.")
    if s.expires_at and s.expires_at < datetime.utcnow():
        raise HTTPException(410, "This link has expired.")
    if not s.password_hash:
        return s, "ok"
    # After unlocking, the page is handed an access key so that download and
    # thumbnail URLs never carry the password itself.
    access = request.query_params.get("access")
    if access and hmac.compare_digest(access, _share_access(s)):
        return s, "ok"
    supplied = request.headers.get("x-share-password") or password
    if not supplied:
        return s, "locked"
    ip = _ip(request)
    if _throttled(token, ip):
        raise HTTPException(429, "Too many wrong passwords. Try again in a while.")
    if verify_password(supplied, s.password_hash):
        return s, "ok"
    _share_fails.setdefault((token, ip), []).append(time.time())
    return s, "wrong"


def _require_ok(state: str):
    if state == "locked":
        raise HTTPException(401, "This link needs a password.")
    if state == "wrong":
        raise HTTPException(403, "That password is not right.")


def _share_target(s: FileShare, sub: str) -> Path:
    try:
        base = abs_path(s.path)
    except HTTPException:
        raise HTTPException(404, "This has been moved or deleted, so the link no longer works.")
    sub = clean_rel(sub)
    if not sub:
        return base
    if not base.is_dir():
        raise HTTPException(404, "Not found.")
    p = base.joinpath(*sub.split("/"))
    if not _within(os.path.realpath(p), os.path.realpath(base)) or not p.exists():
        raise HTTPException(404, "Not found.")
    return p


@public.get("/{token}/info")
def pub_info(token: str, request: Request, password: Optional[str] = None, db: Session = Depends(get_db)):
    s, state = _open_share(db, request, token, password)
    if state != "ok":
        return {"locked": True, "wrong": state == "wrong"}
    p = _share_target(s, "")
    e = _entry(p)
    s.view_count = (s.view_count or 0) + 1
    s.last_accessed = datetime.utcnow()
    db.commit()
    return {"locked": False, "name": e["name"], "is_dir": e["is_dir"], "size": e["size"],
            "mtime": e["mtime"], "kind": e["kind"], "mime": e["mime"], "thumb": e["thumb"],
            "shared_by": s.created_by,
            "access": _share_access(s) if s.password_hash else None,
            "expires_at": int(s.expires_at.timestamp()) if s.expires_at else None}


@public.get("/{token}/list")
def pub_list(token: str, request: Request, sub: str = "", password: Optional[str] = None,
             db: Session = Depends(get_db)):
    s, state = _open_share(db, request, token, password)
    _require_ok(state)
    d = _share_target(s, sub)
    if not d.is_dir():
        raise HTTPException(400, "That is a file.")
    base = _share_target(s, "")
    entries = []
    with os.scandir(d) as it:
        for e in it:
            if e.name.startswith("."):
                continue
            try:
                st, is_dir = e.stat(), e.is_dir()
            except OSError:
                continue
            item = _entry(Path(e.path), st, is_dir=is_dir)
            item["path"] = str(Path(e.path).relative_to(base)).replace(os.sep, "/")
            entries.append(item)
            if len(entries) >= MAX_LIST:
                break
    rel = "" if d == base else str(d.relative_to(base)).replace(os.sep, "/")
    return {"path": rel, "name": d.name, "breadcrumbs": _breadcrumbs(rel), "entries": entries}


@public.get("/{token}/download")
def pub_download(token: str, request: Request, sub: str = "", inline: bool = False,
                 password: Optional[str] = None, db: Session = Depends(get_db)):
    s, state = _open_share(db, request, token, password)
    _require_ok(state)
    p = _share_target(s, sub)
    if p.is_dir():
        raise HTTPException(400, "That is a folder - use the zip download.")

    def count():
        try:
            s.download_count = (s.download_count or 0) + 1
            db.commit()
        except Exception:
            db.rollback()
    return serve_file(request, p, inline=inline, count_cb=count)


@public.get("/{token}/thumb")
def pub_thumb(token: str, request: Request, sub: str = "", w: int = 360,
              password: Optional[str] = None, db: Session = Depends(get_db)):
    s, state = _open_share(db, request, token, password)
    _require_ok(state)
    p = _share_target(s, sub)
    if p.is_dir():
        raise HTTPException(415, "No preview for this file.")
    out = _thumb_file(p, max(120, min(int(w), 1600)))
    return FileResponse(str(out), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@public.get("/{token}/zip")
def pub_zip(token: str, request: Request, sub: str = "", password: Optional[str] = None,
            db: Session = Depends(get_db)):
    s, state = _open_share(db, request, token, password)
    _require_ok(state)
    p = _share_target(s, sub)

    def count():
        try:
            s.download_count = (s.download_count or 0) + 1
            db.commit()
        except Exception:
            db.rollback()
    return _zip_response([p], (p.name or "files") + ".zip", count)


def install(app, media_root=None):
    """One call from main.py: tables, storage location, both routers."""
    configure(media_root)
    app.include_router(router)
    app.include_router(public)
