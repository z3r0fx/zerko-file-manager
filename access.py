"""Who sees what: groups (like Discord roles), access to folders and properties, and profiles.

Admins and editors see the whole library. Client and viewer accounts are
"restricted": they see only

- folders given to them or to one of their groups (and everything under those),
- the folders of properties given to them or their groups,
- the folders of the properties whose agent their profile is linked to
  (an agent sees their own listings without anyone having to share them).

A group can be marked "sees everything", which lifts the restriction for its
members (their role still decides what they may change).

Enforcement is deny-by-default for restricted accounts: `install()` adds a
middleware that lets through only the routes in ALLOW, and checks the file or
folder id on those that carry one. Lists (the library grid, the folder tree)
are filtered in main.py through `visible_folder_ids()`.
"""

import io
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import Body, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Column, DateTime, Integer, String, UniqueConstraint, or_
from sqlalchemy.orm import Session

from auth import get_current_user, get_user_from_token
from database import Base, IndexedFolder, SessionLocal, User, Video, engine, get_db

LEVELS = {"view": 1, "download": 2, "edit": 3}
RESTRICTED_ROLES = ("client", "viewer")
COLOURS = ("#5865f2", "#3ba55d", "#faa61a", "#ed4245", "#eb459e", "#9b59b6", "#1abc9c", "#e67e22", "#95a5a6")

_media_root: Optional[Path] = None


# --- tables ------------------------------------------------------------------

class TeamGroup(Base):
    __tablename__ = "team_groups"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    colour = Column(String, nullable=False, default=COLOURS[0])
    position = Column(Integer, nullable=False, default=0)
    # Members see the whole library, like an editor, whatever their role.
    sees_all = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class TeamGroupMember(Base):
    __tablename__ = "team_group_members"
    __table_args__ = (UniqueConstraint("group_id", "user_id"),)
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, nullable=False, index=True)
    user_id = Column(Integer, nullable=False, index=True)


class AccessGrant(Base):
    """One "this group (or this person) may see that folder / property" line."""
    __tablename__ = "access_grants"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, nullable=True, index=True)
    user_id = Column(Integer, nullable=True, index=True)
    kind = Column(String, nullable=False)          # folder | property
    target_id = Column(Integer, nullable=False, index=True)
    level = Column(String, nullable=False, default="view")
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(String, nullable=True)


class UserProfile(Base):
    __tablename__ = "user_profiles"
    user_id = Column(Integer, primary_key=True)
    display_name = Column(String, nullable=True)
    title = Column(String, nullable=True)          # "Agent", "Photographer"...
    phone = Column(String, nullable=True)
    avatar = Column(String, nullable=True)         # file name in <media root>/.team/avatars
    agent_id = Column(Integer, nullable=True)      # the Agent (shoots.py) this account is
    updated_at = Column(DateTime, default=datetime.utcnow)


# --- who sees what -------------------------------------------------------------

_cache: Dict[int, tuple] = {}
_CACHE_S = 20


def invalidate():
    _cache.clear()


def _group_ids(db: Session, user_id: int) -> List[int]:
    return [r[0] for r in db.query(TeamGroupMember.group_id).filter(TeamGroupMember.user_id == user_id).all()]


def is_restricted(db: Session, user) -> bool:
    return visible_folders(db, user) is not None


def _norm(p: str) -> str:
    return str(p or "").replace("\\", "/").rstrip("/").lower()


def _property_folders(db: Session, shoot_ids) -> List[int]:
    """The library folders linked to these properties (a property keeps its folders by path)."""
    if not shoot_ids:
        return []
    from shoots import ShootFolder
    paths = {_norm(r[0]) for r in db.query(ShootFolder.path).filter(ShootFolder.shoot_id.in_(list(shoot_ids))).all()}
    if not paths:
        return []
    return [f.id for f in db.query(IndexedFolder.id, IndexedFolder.path).all() if _norm(f.path) in paths]


def _compute(db: Session, user) -> Optional[Dict[int, int]]:
    if (user.role or "").lower() not in RESTRICTED_ROLES:
        return None
    gids = _group_ids(db, user.id)
    if gids and db.query(TeamGroup).filter(TeamGroup.id.in_(gids), TeamGroup.sees_all == True).count():  # noqa: E712
        return None
    who = [AccessGrant.user_id == user.id] + ([AccessGrant.group_id.in_(gids)] if gids else [])
    q = db.query(AccessGrant).filter(or_(*who))
    direct: Dict[int, int] = {}

    def give(fid, lvl):
        if lvl > direct.get(fid, 0):
            direct[fid] = lvl

    by_shoot: Dict[int, int] = {}
    for g in q.all():
        lvl = LEVELS.get(g.level, 1)
        if g.kind == "folder":
            give(g.target_id, lvl)
        elif g.kind == "property":
            by_shoot[g.target_id] = max(by_shoot.get(g.target_id, 0), lvl)
    # Their own listings: properties whose agent is the one linked to this account.
    prof = db.query(UserProfile).filter(UserProfile.user_id == user.id).first()
    if prof and prof.agent_id:
        from shoots import Agent, Shoot
        a = db.query(Agent).filter(Agent.id == prof.agent_id).first()
        if a:
            for (sid,) in db.query(Shoot.id).filter(Shoot.agent.ilike(a.name)).all():
                by_shoot[sid] = max(by_shoot.get(sid, 0), LEVELS["download"])
    for sid, lvl in by_shoot.items():
        for fid in _property_folders(db, [sid]):
            give(fid, lvl)
    # Everything under a folder comes with it.
    kids: Dict[Optional[int], List[int]] = {}
    for fid, parent in db.query(IndexedFolder.id, IndexedFolder.parent_id).all():
        kids.setdefault(parent, []).append(fid)
    out: Dict[int, int] = {}
    stack = list(direct.items())
    while stack:
        fid, lvl = stack.pop()
        if out.get(fid, 0) >= lvl:
            continue
        out[fid] = lvl
        stack.extend((k, lvl) for k in kids.get(fid, []))
    return out


def visible_folders(db: Session, user) -> Optional[Dict[int, int]]:
    """None = sees everything. Otherwise {folder id: level} of what this account may open."""
    hit = _cache.get(user.id)
    if hit and time.time() - hit[0] < _CACHE_S and hit[1] == user.role:
        return hit[2]
    res = _compute(db, user)
    _cache[user.id] = (time.time(), user.role, res)
    return res


def visible_folder_ids(db: Session, user) -> Optional[set]:
    v = visible_folders(db, user)
    return None if v is None else set(v)


def folder_level(db: Session, user, folder_id: Optional[int]) -> int:
    v = visible_folders(db, user)
    if v is None:
        return LEVELS["edit"]
    return v.get(folder_id, 0) if folder_id is not None else 0


def require_folder(db: Session, user, folder_id: Optional[int], level: str = "view"):
    """For endpoints: refuse when a restricted account may not reach this folder at this level."""
    if folder_level(db, user, folder_id) < LEVELS[level]:
        raise HTTPException(status_code=403, detail="You do not have access to that folder.")


def can_see_video(db: Session, user, video_id: int, level: str = "view") -> bool:
    v = visible_folders(db, user)
    if v is None:
        return True
    row = db.query(Video.folder_id).filter(Video.id == video_id).first()
    return bool(row) and v.get(row[0], 0) >= LEVELS[level]


# --- the guard -------------------------------------------------------------------

G, W, ANY = ("GET",), ("POST", "PUT", "PATCH", "DELETE"), ("GET", "POST", "PUT", "PATCH", "DELETE")
# (methods, pattern, what the number in it is, level needed). Anything a restricted
# account asks for that is not here is refused - a new endpoint is closed to them
# until someone decides it should be open.
ALLOW = [
    (ANY, r"^/api/(login|logout|me|video-access-token)$", None, None),
    (ANY, r"^/api/me/", None, None),
    (ANY, r"^/api/(public|team|access/me|access/profile|access/profiles|access/avatar)(/|$)", None, None),
    (G, r"^/api/videos$", None, None),                                 # filtered in main.py
    (G, r"^/api/folders(/tree)?$", None, None),                        # filtered in main.py
    (G, r"^/api/folders/(\d+)$", "folder", "view"),
    (G, r"^/api/tags$", None, None),
    (G, r"^/api/videos/(\d+)(/(notes|tags|metadata))?$", "video", "view"),
    (G, r"^/api/videos/(\d+)/download-token$", "video", "download"),
    (W, r"^/api/videos/(\d+)/(rating|status|notes|tags|rename)$", "video", "edit"),
    (G, r"^/api/(video-file|video-proxy|photo-preview)/(\d+)$", "video", "view"),
    (G, r"^/api/photo-edit/(\d+)(/(base|size|developed|tile))?$", "video", "view"),
    (G, r"^/api/download-zip$", "ids", "download"),
    (G, r"^/api/upload/(destinations|status/[\w-]+)$", None, None),
    (("POST",), r"^/api/upload(/chunk)?$", None, None),                # folder checked in main.py
]
_ALLOW = [(m, re.compile(p), k, lvl) for m, p, k, lvl in ALLOW]

_tok_cache: Dict[str, tuple] = {}


def _user_for(request: Request, db: Session):
    auth = request.headers.get("authorization") or ""
    token = auth.split(" ", 1)[1].strip() if auth.lower().startswith("bearer ") else request.query_params.get("token")
    if not token:
        return None
    hit = _tok_cache.get(token)
    if hit and time.time() - hit[0] < 30:
        return hit[1]
    try:
        u = get_user_from_token(token, db)
    except Exception:
        return None
    if len(_tok_cache) > 2000:
        _tok_cache.clear()
    _tok_cache[token] = (time.time(), u)
    return u


def _deny(msg="Your account cannot open that."):
    return JSONResponse(status_code=403, content={"detail": msg})


async def _guard(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/") or path.startswith("/api/public/") or path == "/api/events":
        return await call_next(request)
    db = SessionLocal()
    try:
        user = _user_for(request, db)
        if user is None or not is_restricted(db, user):
            return await call_next(request)
        for methods, pat, kind, lvl in _ALLOW:
            if request.method not in methods:
                continue
            m = pat.match(path)
            if not m:
                continue
            if kind == "folder":
                if folder_level(db, user, int(m.group(1))) < LEVELS[lvl]:
                    return _deny("You do not have access to that folder.")
            elif kind == "video":
                vid = int(next(g for g in m.groups() if g and g.isdigit()))
                if not can_see_video(db, user, vid, lvl):
                    return _deny("You do not have access to that file.")
            elif kind == "ids":
                ids = [int(x) for x in re.findall(r"\d+", request.query_params.get("ids") or "")]
                if not ids or not all(can_see_video(db, user, i, lvl) for i in ids):
                    return _deny("You do not have access to all of those files.")
            break
        else:
            return _deny()
    finally:
        db.close()
    return await call_next(request)


# --- routes -----------------------------------------------------------------------

def _admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="That is an administrator setting.")
    return user


class GroupIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    colour: Optional[str] = Field(None, max_length=9)
    sees_all: bool = False


class GrantEntry(BaseModel):
    group_id: Optional[int] = None
    user_id: Optional[int] = None
    level: str = "view"


class GrantsIn(BaseModel):
    kind: str
    target_id: int
    entries: List[GrantEntry] = []


class ProfileIn(BaseModel):
    display_name: Optional[str] = Field(None, max_length=60)
    title: Optional[str] = Field(None, max_length=60)
    phone: Optional[str] = Field(None, max_length=30)


class ProfileAdminIn(ProfileIn):
    agent_id: Optional[int] = None


def _clean(s):
    s = (s or "").strip()
    return s or None


def _group_out(db, g):
    members = [r[0] for r in db.query(TeamGroupMember.user_id).filter(TeamGroupMember.group_id == g.id).all()]
    grants = db.query(AccessGrant).filter(AccessGrant.group_id == g.id).all()
    return {"id": g.id, "name": g.name, "colour": g.colour, "position": g.position, "sees_all": bool(g.sees_all),
            "members": members,
            "grants": [{"kind": x.kind, "target_id": x.target_id, "level": x.level} for x in grants]}


def _avatar_dir() -> Path:
    d = (_media_root or Path(".")) / ".team" / "avatars"
    d.mkdir(parents=True, exist_ok=True)
    return d


def profile_out(u: User, p: Optional[UserProfile], groups=None):
    return {"user_id": u.id, "username": u.username, "role": u.role,
            "display_name": (p.display_name if p else None) or u.username,
            "title": p.title if p else None, "phone": p.phone if p else None,
            "agent_id": p.agent_id if p else None,
            "avatar": (f"/api/access/avatar/{u.id}?v={int(p.updated_at.timestamp())}"
                       if p and p.avatar and p.updated_at else None),
            "groups": groups or []}


def all_profiles(db: Session):
    """Everyone active, with profile and groups - what the team column draws."""
    profs = {p.user_id: p for p in db.query(UserProfile).all()}
    mem: Dict[int, List[int]] = {}
    for gid, uid in db.query(TeamGroupMember.group_id, TeamGroupMember.user_id).all():
        mem.setdefault(uid, []).append(gid)
    return [profile_out(u, profs.get(u.id), mem.get(u.id, []))
            for u in db.query(User).filter((User.is_active == True) | (User.is_active == None)).all()]  # noqa: E712


def install(app, media_root):
    global _media_root
    _media_root = Path(media_root) if media_root else None
    Base.metadata.create_all(bind=engine, tables=[TeamGroup.__table__, TeamGroupMember.__table__,
                                                  AccessGrant.__table__, UserProfile.__table__])
    app.middleware("http")(_guard)

    @app.get("/api/access/me")
    def me(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
        v = visible_folders(db, user)
        p = db.query(UserProfile).filter(UserProfile.user_id == user.id).first()
        return {"restricted": v is not None, "folders": None if v is None else len(v),
                "can_upload": v is None or any(lv >= LEVELS["edit"] for lv in v.values()),
                "profile": profile_out(user, p, _group_ids(db, user.id))}

    @app.get("/api/access/groups")
    def groups(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
        rows = db.query(TeamGroup).order_by(TeamGroup.position, TeamGroup.id).all()
        return [_group_out(db, g) for g in rows]

    @app.post("/api/access/groups")
    def add_group(body: GroupIn, db: Session = Depends(get_db), user: User = Depends(_admin)):
        n = db.query(TeamGroup).count()
        g = TeamGroup(name=body.name.strip(), colour=body.colour or COLOURS[n % len(COLOURS)],
                      sees_all=body.sees_all, position=n)
        db.add(g)
        db.commit()
        invalidate()
        return _group_out(db, g)

    @app.put("/api/access/groups/order")
    def order_groups(ids: List[int] = Body(...), db: Session = Depends(get_db), user: User = Depends(_admin)):
        for i, gid in enumerate(ids):
            db.query(TeamGroup).filter(TeamGroup.id == gid).update({"position": i})
        db.commit()
        return {"ok": True}

    @app.put("/api/access/groups/{gid}")
    def edit_group(gid: int, body: GroupIn, db: Session = Depends(get_db), user: User = Depends(_admin)):
        g = db.query(TeamGroup).filter(TeamGroup.id == gid).first()
        if not g:
            raise HTTPException(404, "No such group")
        g.name, g.sees_all = body.name.strip(), body.sees_all
        if body.colour:
            g.colour = body.colour
        db.commit()
        invalidate()
        return _group_out(db, g)

    @app.delete("/api/access/groups/{gid}")
    def del_group(gid: int, db: Session = Depends(get_db), user: User = Depends(_admin)):
        db.query(TeamGroupMember).filter(TeamGroupMember.group_id == gid).delete()
        db.query(AccessGrant).filter(AccessGrant.group_id == gid).delete()
        db.query(TeamGroup).filter(TeamGroup.id == gid).delete()
        db.commit()
        invalidate()
        return {"ok": True}

    @app.put("/api/access/groups/{gid}/members")
    def set_members(gid: int, user_ids: List[int] = Body(...), db: Session = Depends(get_db),
                    user: User = Depends(_admin)):
        if not db.query(TeamGroup).filter(TeamGroup.id == gid).first():
            raise HTTPException(404, "No such group")
        db.query(TeamGroupMember).filter(TeamGroupMember.group_id == gid).delete()
        for uid in sorted(set(user_ids)):
            db.add(TeamGroupMember(group_id=gid, user_id=uid))
        db.commit()
        invalidate()
        return {"ok": True}

    @app.get("/api/access/grants")
    def grants(kind: str, target_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
        """Who can see this folder or property (besides admins and editors)."""
        rows = db.query(AccessGrant).filter(AccessGrant.kind == kind, AccessGrant.target_id == target_id).all()
        return [{"group_id": g.group_id, "user_id": g.user_id, "level": g.level} for g in rows]

    @app.put("/api/access/grants")
    def set_grants(body: GrantsIn, db: Session = Depends(get_db), user: User = Depends(_admin)):
        if body.kind not in ("folder", "property"):
            raise HTTPException(400, "kind is folder or property")
        db.query(AccessGrant).filter(AccessGrant.kind == body.kind, AccessGrant.target_id == body.target_id).delete()
        for e in body.entries:
            if (e.group_id is None) == (e.user_id is None) or e.level not in LEVELS:
                raise HTTPException(400, "Each line is a group or a person, and view, download or edit")
            db.add(AccessGrant(group_id=e.group_id, user_id=e.user_id, kind=body.kind, target_id=body.target_id,
                               level=e.level, created_by=user.username))
        db.commit()
        invalidate()
        return {"ok": True}

    @app.get("/api/access/sees/{uid}")
    def sees(uid: int, db: Session = Depends(get_db), user: User = Depends(_admin)):
        """What this account can open - so the Groups page can say "sees 3 folders"."""
        u = db.query(User).filter(User.id == uid).first()
        if not u:
            raise HTTPException(404, "No such account")
        v = visible_folders(db, u)
        if v is None:
            return {"all": True, "folders": []}
        names = dict(db.query(IndexedFolder.id, IndexedFolder.name).filter(IndexedFolder.id.in_(list(v))).all())
        inv = {n: k for k, n in LEVELS.items()}
        return {"all": False, "folders": [{"id": f, "name": names.get(f), "level": inv[l]} for f, l in v.items()]}

    @app.get("/api/access/profiles")
    def profiles(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
        return all_profiles(db)

    def _save_profile(db, uid, body, admin=False):
        p = db.query(UserProfile).filter(UserProfile.user_id == uid).first()
        if not p:
            p = UserProfile(user_id=uid)
            db.add(p)
        p.display_name, p.title, p.phone = _clean(body.display_name), _clean(body.title), _clean(body.phone)
        if admin:
            p.agent_id = body.agent_id
        p.updated_at = datetime.utcnow()
        db.commit()
        invalidate()
        return p

    @app.put("/api/access/profile")
    def my_profile(body: ProfileIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
        return profile_out(user, _save_profile(db, user.id, body), _group_ids(db, user.id))

    @app.put("/api/access/profiles/{uid}")
    def admin_profile(uid: int, body: ProfileAdminIn, db: Session = Depends(get_db), user: User = Depends(_admin)):
        u = db.query(User).filter(User.id == uid).first()
        if not u:
            raise HTTPException(404, "No such account")
        return profile_out(u, _save_profile(db, uid, body, admin=True), _group_ids(db, uid))

    @app.post("/api/access/profile/avatar")
    def avatar_up(file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
        from PIL import Image, ImageOps
        try:
            im = ImageOps.exif_transpose(Image.open(io.BytesIO(file.file.read(12_000_000)))).convert("RGB")
        except Exception:
            raise HTTPException(400, "That is not a picture Zerko can read.")
        s = min(im.size)
        im = im.crop(((im.width - s) // 2, (im.height - s) // 2, (im.width + s) // 2, (im.height + s) // 2))
        im = im.resize((256, 256), Image.LANCZOS)
        name = f"{user.id}.jpg"
        im.save(_avatar_dir() / name, "JPEG", quality=88)
        p = db.query(UserProfile).filter(UserProfile.user_id == user.id).first()
        if not p:
            p = UserProfile(user_id=user.id)
            db.add(p)
        p.avatar, p.updated_at = name, datetime.utcnow()
        db.commit()
        return profile_out(user, p, _group_ids(db, user.id))

    @app.delete("/api/access/profile/avatar")
    def avatar_del(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
        p = db.query(UserProfile).filter(UserProfile.user_id == user.id).first()
        if p and p.avatar:
            (_avatar_dir() / p.avatar).unlink(missing_ok=True)
            p.avatar, p.updated_at = None, datetime.utcnow()
            db.commit()
        return {"ok": True}

    @app.get("/api/access/avatar/{uid}")
    def avatar(uid: int, request: Request, db: Session = Depends(get_db)):
        # <img> cannot send a header, so the token may come as ?token= - but it must come.
        if _user_for(request, db) is None:
            raise HTTPException(401, "Sign in first")
        p = db.query(UserProfile).filter(UserProfile.user_id == uid).first()
        f = _avatar_dir() / p.avatar if p and p.avatar else None
        if not f or not f.exists():
            raise HTTPException(404, "No picture")
        return FileResponse(str(f), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})
