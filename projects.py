"""Projects: one folder per shoot, with a folder per stage inside it.

A property job goes through the same steps every time - the brackets come in,
they get fused, the fused ones get developed, the good ones get picked, and
the picks get exported at listing sizes. Left to plain folders, the output of
each step lands next to its input and within an hour nobody can tell which
HDRs are the current ones.

So a project is a folder that looks like this:

    20m Greenpoint/
        01 Originals/     what came off the card
        02 HDR/           what the merge made
        03 Edited/        what the editor exported
        04 Selects/       the ones worth sending
        05 Exports/       listing-sized copies

The numbering is there so the folder sorts in pipeline order in Explorer as
well as here. Every tool that produces a file asks this module where its
output belongs: if the source sits anywhere inside a project, the result goes
to that project's stage folder, and the next step therefore sees exactly what
the last one produced. If it is not in a project, nothing changes - output
lands beside the source as it always did.

Nothing here moves or deletes a file on its own.
"""

import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, Integer, String, func
from sqlalchemy.orm import Session

from auth import get_current_user
from database import Base, IndexedFolder, User, Video, engine, get_db

router = APIRouter(prefix="/api/projects", tags=["projects"])

_media_root: Optional[Path] = None
_resolve = lambda p: p
_ensure_folder_row = None          # main.py lends us its indexer

# key, folder name, what it is for
STAGES = [
    ("originals", "01 Originals", "Straight off the card"),
    ("hdr", "02 HDR", "Fused brackets"),
    ("edited", "03 Edited", "Developed and exported"),
    ("selects", "04 Selects", "The ones worth sending"),
    ("exports", "05 Exports", "Listing-sized copies"),
]
STAGE_KEYS = [s[0] for s in STAGES]
STAGE_DIRS = {k: d for k, d, _ in STAGES}


class Project(Base):
    __tablename__ = "projects"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    path = Column(String, nullable=False, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(String, nullable=True)
    # Stages this shoot does not use, comma separated. A job that arrives
    # already merged has no use for an HDR step, and a stage nobody uses is
    # one more place to look for a photo that was never there.
    skipped = Column(String, nullable=True)


# --------------------------------------------------------------------------
# where things go
# --------------------------------------------------------------------------

def project_for_path(db: Session, path: str) -> Optional[Project]:
    """The project a file or folder belongs to, if any.

    Matches on the path prefix, longest first, so a project nested inside
    another one still wins.
    """
    if not path:
        return None
    try:
        p = str(Path(path).resolve())
    except Exception:
        p = str(path)
    best = None
    for proj in db.query(Project).all():
        root = str(Path(proj.path))
        if p == root or p.startswith(root + os.sep) or p.startswith(root + "/"):
            if best is None or len(proj.path) > len(best.path):
                best = proj
    return best


def skipped_of(proj: Project) -> set:
    return {k for k in (proj.skipped or "").split(",") if k in STAGE_DIRS}


def stage_dir(db: Session, source_path: str, stage: str) -> Optional[Path]:
    """Where output belongs for something produced from `source_path`.

    If the project skips the stage that asked - a shoot that came in already
    merged skipping HDR, say - the output moves along to the next stage the
    project does use, so the pipeline closes up rather than leaving a folder
    nobody looks in. None means "not in a project": the caller keeps its old
    behaviour of writing beside the source.
    """
    if stage not in STAGE_DIRS:
        return None
    proj = project_for_path(db, source_path)
    if not proj:
        return None

    skip = skipped_of(proj)
    if stage in skip:
        order = STAGE_KEYS[STAGE_KEYS.index(stage) + 1:]
        stage = next((k for k in order if k not in skip), None)
        if stage is None:
            return None

    out = Path(proj.path) / STAGE_DIRS[stage]
    out.mkdir(parents=True, exist_ok=True)
    return out


def register_dir(db: Session, directory: Path):
    """Index a directory we just created, so it shows up in the library."""
    if _ensure_folder_row is None:
        return None
    try:
        return _ensure_folder_row(db, Path(directory))
    except Exception:
        return None


# --------------------------------------------------------------------------
# api
# --------------------------------------------------------------------------

def _safe_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="A project name is required")
    if any(ch in name for ch in ("/", "\\", "..")) or name in (".", ".."):
        raise HTTPException(status_code=400, detail="Name cannot contain slashes or ..")
    return re.sub(r'[<>:"|?*]', "_", name)[:120]


def _counts(db: Session, proj: Project):
    """How many photos and clips sit in each stage."""
    out = {}
    for key, folder, _ in STAGES:
        path = str(Path(proj.path) / folder)
        row = db.query(IndexedFolder).filter(IndexedFolder.path == path).first()
        n = 0
        if row:
            n = (db.query(func.count(Video.id))
                 .filter(Video.folder_id == row.id, Video.is_active.isnot(False))
                 .scalar()) or 0
        out[key] = {"folder_id": row.id if row else None, "count": n,
                    "name": folder, "exists": os.path.isdir(path)}
    return out


def _as_dict(db: Session, proj: Project):
    return {
        "id": proj.id,
        "name": proj.name,
        "path": proj.path,
        "skipped": sorted(skipped_of(proj)),
        "created_at": proj.created_at.isoformat() if proj.created_at else None,
        "stages": _counts(db, proj),
    }


class NewProject(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    parent_folder_id: Optional[int] = None
    # Photos to pull in as the starting point (moved into 01 Originals).
    video_ids: List[int] = []
    move_files: bool = True


@router.get("")
def list_projects(db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    rows = db.query(Project).order_by(Project.created_at.desc()).all()
    return {"projects": [_as_dict(db, p) for p in rows],
            "stages": [{"key": k, "name": n, "hint": h} for k, n, h in STAGES]}


@router.post("")
def create_project(body: NewProject, db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    name = _safe_name(body.name)

    base = _media_root
    if body.parent_folder_id:
        parent = db.query(IndexedFolder).filter(
            IndexedFolder.id == body.parent_folder_id).first()
        if parent and parent.path:
            cand = Path(_resolve(parent.path))
            if cand.is_dir():
                base = cand

    root = Path(base) / name
    if db.query(Project).filter(Project.path == str(root)).first():
        raise HTTPException(status_code=409, detail="That project already exists")
    try:
        root.mkdir(parents=True, exist_ok=True)
        for _, folder, _h in STAGES:
            (root / folder).mkdir(exist_ok=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not create the folders: {e}")

    proj = Project(name=name, path=str(root), created_at=datetime.utcnow(),
                   created_by=getattr(current_user, "username", None))
    db.add(proj)
    db.commit()
    db.refresh(proj)

    register_dir(db, root)
    for _, folder, _h in STAGES:
        register_dir(db, root / folder)

    if body.video_ids:
        _bring_in(db, proj, body.video_ids, move=body.move_files)

    return _as_dict(db, proj)


class BringIn(BaseModel):
    video_ids: List[int]
    stage: str = "originals"
    move_files: bool = True


def _bring_in(db: Session, proj: Project, ids: List[int], stage: str = "originals",
              move: bool = True):
    """Put existing library files into a stage of this project."""
    target = Path(proj.path) / STAGE_DIRS.get(stage, STAGE_DIRS["originals"])
    target.mkdir(parents=True, exist_ok=True)
    folder_row = register_dir(db, target)

    done, skipped = 0, []
    for vid in list(dict.fromkeys(ids))[:5000]:
        v = db.query(Video).filter(Video.id == vid).first()
        if not v:
            continue
        src = _resolve(v.filepath)
        if not src or not os.path.exists(src):
            skipped.append(f"{v.filename}: not on disk")
            continue
        dest = target / os.path.basename(v.filename or os.path.basename(src))
        n = 2
        while dest.exists() and str(dest) != str(src):
            stem, ext = os.path.splitext(dest.name)
            dest = target / f"{stem} ({n}){ext}"
            n += 1
        if str(dest) == str(src):
            done += 1
            continue
        try:
            if move:
                shutil.move(src, dest)
            else:
                shutil.copy2(src, dest)
        except Exception as e:
            skipped.append(f"{v.filename}: {e}")
            continue
        if move:
            v.filepath = str(dest)
            v.filename = dest.name
            if folder_row:
                v.folder_id = folder_row.id
        done += 1
    db.commit()
    return {"moved": done, "skipped": skipped[:20]}


@router.post("/{project_id}/bring-in")
def bring_in(project_id: int, body: BringIn, db: Session = Depends(get_db),
             current_user: User = Depends(get_current_user)):
    proj = db.query(Project).filter(Project.id == project_id).first()
    if not proj:
        raise HTTPException(status_code=404, detail="No such project")
    if body.stage not in STAGE_DIRS:
        raise HTTPException(status_code=400, detail="Unknown stage")
    result = _bring_in(db, proj, body.video_ids, body.stage, body.move_files)
    return {**result, "project": _as_dict(db, proj)}


class StagePrefs(BaseModel):
    skipped: List[str] = []


@router.post("/{project_id}/stages")
def set_skipped(project_id: int, body: StagePrefs, db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    """Choose which stages this shoot uses.

    Nothing is deleted: a skipped stage keeps whatever is in it, it just drops
    out of the flow, and anything that would have been written there goes to
    the next stage instead.
    """
    proj = db.query(Project).filter(Project.id == project_id).first()
    if not proj:
        raise HTTPException(status_code=404, detail="No such project")
    keep = [k for k in body.skipped if k in STAGE_DIRS]
    # Originals is where everything starts; skipping it would mean no input.
    keep = [k for k in keep if k != "originals"]
    proj.skipped = ",".join(sorted(set(keep)))
    db.commit()
    db.refresh(proj)
    return _as_dict(db, proj)


@router.get("/for-folder/{folder_id}")
def project_for_folder(folder_id: int, db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    folder = db.query(IndexedFolder).filter(IndexedFolder.id == folder_id).first()
    if not folder:
        raise HTTPException(status_code=404, detail="No such folder")
    proj = project_for_path(db, _resolve(folder.path))
    return {"project": _as_dict(db, proj) if proj else None}


@router.post("/adopt/{folder_id}")
def adopt_folder(folder_id: int, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    """Turn a folder you already have into a project, in place.

    The stage folders are added alongside whatever is in there; nothing that
    is already on disk moves. Handy for a shoot that was copied in before any
    of this existed.
    """
    folder = db.query(IndexedFolder).filter(IndexedFolder.id == folder_id).first()
    if not folder:
        raise HTTPException(status_code=404, detail="No such folder")
    root = Path(_resolve(folder.path))
    if not root.is_dir():
        raise HTTPException(status_code=404, detail="That folder is not on disk")

    existing = db.query(Project).filter(Project.path == str(root)).first()
    if existing:
        return _as_dict(db, existing)

    for _, sub, _h in STAGES:
        (root / sub).mkdir(exist_ok=True)
        register_dir(db, root / sub)

    proj = Project(name=folder.name or root.name, path=str(root),
                   created_at=datetime.utcnow(),
                   created_by=getattr(current_user, "username", None))
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return _as_dict(db, proj)


@router.delete("/{project_id}")
def forget_project(project_id: int, db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    """Stop treating this folder as a project. No file is touched."""
    db.query(Project).filter(Project.id == project_id).delete()
    db.commit()
    return {"forgotten": True}


def install(app, media_root, resolve_media_path, ensure_folder_row):
    global _media_root, _resolve, _ensure_folder_row
    _media_root = Path(media_root) if media_root else None
    _resolve = resolve_media_path
    _ensure_folder_row = ensure_folder_row
    Base.metadata.create_all(bind=engine, tables=[Project.__table__])
    # A project table made before stage-skipping existed needs the column.
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(projects)"))]
            if "skipped" not in cols:
                conn.execute(text("ALTER TABLE projects ADD COLUMN skipped TEXT"))
                conn.commit()
    except Exception as e:
        print(f"projects: could not add the skipped column: {e}", flush=True)
    app.include_router(router)
