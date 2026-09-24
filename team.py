"""The team: who is online, channels and direct messages.

Presence lives in memory (the web app pings every 30 s; a tab that is hidden
or idle says "away"). Chat is in the database. Live updates go through a
signed-in stream per person (`/api/team/stream`), never through `/api/events`,
which needs no sign-in and is sent to everyone.

Who sees what:
- a channel with no groups is for the team - admins, editors and anyone in a
  "sees everything" group. Client and view-only accounts never see it;
- a channel with groups is for those groups' members, plus admins;
- a direct message is for its two people only.
A photo or video attached to a message shows only to people who can open it.
"""

import asyncio
import json
import queue
import re
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

import io
import uuid
from pathlib import Path

from fastapi import Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Session

import access
from auth import get_current_user
from database import Base, SessionLocal, User, Video, engine, get_db

ONLINE_S = 75


# --- tables ------------------------------------------------------------------

class ChatChannel(Base):
    __tablename__ = "chat_channels"
    id = Column(Integer, primary_key=True)
    kind = Column(String, nullable=False, default="channel")   # channel | dm
    name = Column(String, nullable=True)
    topic = Column(String, nullable=True)
    group_ids = Column(String, nullable=True)                  # "1,4"; empty = the team
    position = Column(Integer, nullable=False, default=0)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ChatMember(Base):
    """The two people of a direct message."""
    __tablename__ = "chat_members"
    __table_args__ = (UniqueConstraint("channel_id", "user_id"),)
    id = Column(Integer, primary_key=True)
    channel_id = Column(Integer, nullable=False, index=True)
    user_id = Column(Integer, nullable=False, index=True)


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id = Column(Integer, primary_key=True)
    channel_id = Column(Integer, nullable=False, index=True)
    user_id = Column(Integer, nullable=False)
    body = Column(Text, nullable=False, default="")
    attachments = Column(Text, nullable=True)                  # JSON [{video_id}]
    mentions = Column(String, nullable=True)                   # "3,7"
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    edited_at = Column(DateTime, nullable=True)
    deleted = Column(Boolean, nullable=False, default=False)


class ChatRead(Base):
    __tablename__ = "chat_reads"
    __table_args__ = (UniqueConstraint("channel_id", "user_id"),)
    id = Column(Integer, primary_key=True)
    channel_id = Column(Integer, nullable=False, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    last_read = Column(Integer, nullable=False, default=0)


# --- presence and the live stream ------------------------------------------------

_seen: Dict[int, tuple] = {}            # user id -> (time, "active" | "away")
_subs: List[tuple] = []                 # (user id, queue)
_lock = threading.Lock()


def presence(uid: int) -> str:
    hit = _seen.get(uid)
    if not hit or time.time() - hit[0] > ONLINE_S:
        return "offline"
    return "online" if hit[1] == "active" else "away"


def _push(user_ids, event: dict):
    with _lock:
        for uid, q in _subs:
            if user_ids is None or uid in user_ids:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass


# --- who sees what ---------------------------------------------------------------

def _gids(ch) -> List[int]:
    return [int(x) for x in (ch.group_ids or "").split(",") if x.strip().isdigit()]


def can_see(db: Session, user, ch) -> bool:
    if ch.kind == "dm":
        return db.query(ChatMember).filter(ChatMember.channel_id == ch.id, ChatMember.user_id == user.id).count() > 0
    gids = _gids(ch)
    if not gids:
        return not access.is_restricted(db, user)
    if user.role == "admin":
        return True
    return bool(set(gids) & set(access._group_ids(db, user.id)))


def audience(db: Session, ch) -> set:
    """Everyone who may read this channel - who gets its live updates."""
    if ch.kind == "dm":
        return {r[0] for r in db.query(ChatMember.user_id).filter(ChatMember.channel_id == ch.id).all()}
    return {u.id for u in db.query(User).filter((User.is_active == True) | (User.is_active == None)).all()  # noqa: E712
            if can_see(db, u, ch)}


def visible_people(db: Session, user) -> Optional[set]:
    """A client sees the team and the people in their own groups, never other clients. None = everyone."""
    if not access.is_restricted(db, user):
        return None
    mine = set(access._group_ids(db, user.id))
    out = {user.id}
    for u in db.query(User).all():
        if not access.is_restricted(db, u):
            out.add(u.id)
    if mine:
        out |= {r[0] for r in db.query(access.TeamGroupMember.user_id)
                .filter(access.TeamGroupMember.group_id.in_(mine)).all()}
    return out


def _channel(db, cid, user):
    ch = db.query(ChatChannel).filter(ChatChannel.id == cid).first()
    if not ch or not can_see(db, user, ch):
        raise HTTPException(404, "No such channel")
    return ch


def _chat_dir() -> Path:
    d = (access._media_root or Path(".")) / ".team" / "chat"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _msg_out(db, m, viewer, names=None):
    atts = []
    for a in json.loads(m.attachments or "[]"):
        if a.get("file"):
            # a picture pasted or dropped into the chat: whoever reads the chat sees it
            atts.append({"file": a["file"], "name": a.get("name") or "Picture", "url": f"/api/team/file/{a['file']}"})
            continue
        vid = a.get("video_id")
        if vid and access.can_see_video(db, viewer, vid):
            v = db.query(Video).filter(Video.id == vid).first()
            if v:
                atts.append({"video_id": vid, "name": v.filename, "media_type": v.media_type,
                             "thumb": v.thumbnail_path})
                continue
        atts.append({"hidden": True})
    return {"id": m.id, "channel_id": m.channel_id, "user_id": m.user_id,
            "body": "" if m.deleted else m.body, "deleted": bool(m.deleted),
            "attachments": [] if m.deleted else atts,
            "mentions": [int(x) for x in (m.mentions or "").split(",") if x.strip().isdigit()],
            "created_at": m.created_at.isoformat() + "Z", "edited": bool(m.edited_at)}


def _ch_out(db, ch, user, last_ids=None, reads=None):
    out = {"id": ch.id, "kind": ch.kind, "name": ch.name, "topic": ch.topic, "group_ids": _gids(ch),
           "position": ch.position}
    if ch.kind == "dm":
        other = [r[0] for r in db.query(ChatMember.user_id).filter(ChatMember.channel_id == ch.id).all()
                 if r[0] != user.id]
        out["other_id"] = other[0] if other else user.id
    last = (last_ids or {}).get(ch.id, 0)
    read = (reads or {}).get(ch.id, 0)
    out["last_id"] = last
    out["unread"] = (db.query(ChatMessage).filter(ChatMessage.channel_id == ch.id, ChatMessage.id > read,
                                                  ChatMessage.user_id != user.id,
                                                  ChatMessage.deleted == False).count()  # noqa: E712
                     if last > read else 0)
    out["mentioned"] = (db.query(ChatMessage).filter(ChatMessage.channel_id == ch.id, ChatMessage.id > read,
                                                     ChatMessage.mentions.like(f"%,{user.id},%")).count() > 0
                        if out["unread"] else False)
    return out


class ChannelIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    topic: Optional[str] = Field(None, max_length=200)
    group_ids: List[int] = []


class MessageIn(BaseModel):
    body: str = Field("", max_length=8000)
    attachments: List[int] = []                 # library file ids
    files: List[str] = []                       # pictures uploaded to this chat (/upload)


class EditIn(BaseModel):
    body: str = Field(..., max_length=8000)


class PingIn(BaseModel):
    state: str = "active"


def _staff(user):
    if user.role not in ("admin", "editor", "user"):
        raise HTTPException(403, "Only the team can make channels.")


def install(app):
    Base.metadata.create_all(bind=engine, tables=[ChatChannel.__table__, ChatMember.__table__,
                                                  ChatMessage.__table__, ChatRead.__table__])
    db = SessionLocal()
    try:
        if not db.query(ChatChannel).filter(ChatChannel.kind == "channel").count():
            db.add(ChatChannel(kind="channel", name="general", topic="The whole team", created_by="zerko"))
            db.commit()
    finally:
        db.close()

    @app.post("/api/team/ping")
    def ping(body: PingIn, user: User = Depends(get_current_user)):
        state = "away" if body.state == "away" else "active"
        before = presence(user.id)
        _seen[user.id] = (time.time(), state)
        if presence(user.id) != before:
            _push(None, {"type": "presence", "user_id": user.id, "status": presence(user.id)})
        return {"status": presence(user.id)}

    @app.get("/api/team/members")
    def members(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
        people = visible_people(db, user)
        rows = [p for p in access.all_profiles(db) if people is None or p["user_id"] in people]
        for p in rows:
            p["status"] = presence(p["user_id"])
            hit = _seen.get(p["user_id"])
            p["last_seen"] = datetime.utcfromtimestamp(hit[0]).isoformat() + "Z" if hit else None
        groups = [{"id": g.id, "name": g.name, "colour": g.colour, "position": g.position}
                  for g in db.query(access.TeamGroup).order_by(access.TeamGroup.position).all()]
        return {"members": rows, "groups": groups}

    @app.get("/api/team/channels")
    def channels(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
        rows = [c for c in db.query(ChatChannel).order_by(ChatChannel.position, ChatChannel.id).all()
                if can_see(db, user, c)]
        ids = [c.id for c in rows]
        last_ids = dict(db.query(ChatMessage.channel_id, func.max(ChatMessage.id))
                        .filter(ChatMessage.channel_id.in_(ids)).group_by(ChatMessage.channel_id).all()) if ids else {}
        reads = dict(db.query(ChatRead.channel_id, ChatRead.last_read).filter(ChatRead.user_id == user.id).all())
        return [_ch_out(db, c, user, last_ids, reads) for c in rows]

    @app.post("/api/team/channels")
    def add_channel(body: ChannelIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
        _staff(user)
        name = re.sub(r"\s+", "-", body.name.strip().lower())
        ch = ChatChannel(kind="channel", name=name, topic=(body.topic or "").strip() or None,
                         group_ids=",".join(str(g) for g in body.group_ids),
                         position=db.query(ChatChannel).count(), created_by=user.username)
        db.add(ch)
        db.commit()
        _push(audience(db, ch), {"type": "channels"})
        return _ch_out(db, ch, user)

    @app.put("/api/team/channels/{cid}")
    def edit_channel(cid: int, body: ChannelIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
        _staff(user)
        ch = _channel(db, cid, user)
        if ch.kind != "channel":
            raise HTTPException(400, "A direct message has no settings")
        before = audience(db, ch)
        ch.name = re.sub(r"\s+", "-", body.name.strip().lower())
        ch.topic = (body.topic or "").strip() or None
        ch.group_ids = ",".join(str(g) for g in body.group_ids)
        db.commit()
        _push(before | audience(db, ch), {"type": "channels"})
        return _ch_out(db, ch, user)

    @app.delete("/api/team/channels/{cid}")
    def del_channel(cid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
        if user.role != "admin":
            raise HTTPException(403, "Only an administrator can delete a channel.")
        ch = _channel(db, cid, user)
        who = audience(db, ch)
        db.query(ChatMessage).filter(ChatMessage.channel_id == cid).delete()
        db.query(ChatRead).filter(ChatRead.channel_id == cid).delete()
        db.query(ChatMember).filter(ChatMember.channel_id == cid).delete()
        db.delete(ch)
        db.commit()
        _push(who, {"type": "channels"})
        return {"ok": True}

    @app.post("/api/team/dm/{uid}")
    def dm(uid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
        people = visible_people(db, user)
        if (people is not None and uid not in people) or not db.query(User).filter(User.id == uid).first():
            raise HTTPException(404, "No such person")
        pair = {user.id, uid}
        for ch in db.query(ChatChannel).filter(ChatChannel.kind == "dm").all():
            mem = {r[0] for r in db.query(ChatMember.user_id).filter(ChatMember.channel_id == ch.id).all()}
            if mem == pair:
                return _ch_out(db, ch, user)
        ch = ChatChannel(kind="dm", created_by=user.username)
        db.add(ch)
        db.flush()
        for u in pair:
            db.add(ChatMember(channel_id=ch.id, user_id=u))
        db.commit()
        _push(pair, {"type": "channels"})
        return _ch_out(db, ch, user)

    @app.get("/api/team/channels/{cid}/messages")
    def messages(cid: int, before: Optional[int] = None, limit: int = 60, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
        _channel(db, cid, user)
        q = db.query(ChatMessage).filter(ChatMessage.channel_id == cid)
        if before:
            q = q.filter(ChatMessage.id < before)
        rows = q.order_by(ChatMessage.id.desc()).limit(max(1, min(limit, 200))).all()
        return [_msg_out(db, m, user) for m in reversed(rows)]

    @app.post("/api/team/channels/{cid}/messages")
    def send(cid: int, body: MessageIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
        ch = _channel(db, cid, user)
        text = body.body.strip()
        atts = [{"video_id": v} for v in body.attachments[:20] if access.can_see_video(db, user, v)]
        atts += [{"file": f} for f in body.files[:20]
                 if re.fullmatch(rf"{cid}_[0-9a-f]{{32}}\.jpg", f) and (_chat_dir() / f).exists()]
        if not text and not atts:
            raise HTTPException(400, "Write something first")
        who = audience(db, ch)
        names = {u.username.lower(): u.id for u in db.query(User).all()}
        for p in db.query(access.UserProfile).all():
            if p.display_name:
                names.setdefault(p.display_name.lower().replace(" ", ""), p.user_id)
        mentions = sorted({names[n.lower()] for n in re.findall(r"@([\w.-]+)", text)
                           if n.lower() in names and names[n.lower()] in who})
        if re.search(r"@(everyone|here)\b", text):
            mentions = sorted(who - {user.id})
        m = ChatMessage(channel_id=cid, user_id=user.id, body=text, attachments=json.dumps(atts),
                        mentions=(",%s," % ",".join(map(str, mentions))) if mentions else None)
        db.add(m)
        db.commit()
        _read(db, cid, user.id, m.id)
        for uid in who:
            u = db.query(User).filter(User.id == uid).first()
            if u:
                _push({uid}, {"type": "message", "message": _msg_out(db, m, u), "channel_kind": ch.kind})
        return _msg_out(db, m, user)

    @app.put("/api/team/messages/{mid}")
    def edit(mid: int, body: EditIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
        m = db.query(ChatMessage).filter(ChatMessage.id == mid).first()
        if not m or m.user_id != user.id or m.deleted:
            raise HTTPException(404, "No such message")
        ch = _channel(db, m.channel_id, user)
        m.body, m.edited_at = body.body.strip(), datetime.utcnow()
        db.commit()
        for uid in audience(db, ch):
            u = db.query(User).filter(User.id == uid).first()
            if u:
                _push({uid}, {"type": "edit", "message": _msg_out(db, m, u)})
        return _msg_out(db, m, user)

    @app.delete("/api/team/messages/{mid}")
    def delete(mid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
        m = db.query(ChatMessage).filter(ChatMessage.id == mid).first()
        if not m or (m.user_id != user.id and user.role != "admin"):
            raise HTTPException(404, "No such message")
        ch = _channel(db, m.channel_id, user)
        m.deleted = True
        db.commit()
        _push(audience(db, ch), {"type": "delete", "id": m.id, "channel_id": m.channel_id})
        return {"ok": True}

    def _read(db, cid, uid, last):
        r = db.query(ChatRead).filter(ChatRead.channel_id == cid, ChatRead.user_id == uid).first()
        if not r:
            db.add(ChatRead(channel_id=cid, user_id=uid, last_read=last))
        elif last > r.last_read:
            r.last_read = last
        db.commit()

    @app.post("/api/team/channels/{cid}/upload")
    def upload(cid: int, file: UploadFile = File(...), db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
        """A picture pasted or dropped into a chat, kept for that chat (not in the library)."""
        _channel(db, cid, user)
        from PIL import Image, ImageOps
        try:
            im = ImageOps.exif_transpose(Image.open(io.BytesIO(file.file.read(40_000_000)))).convert("RGB")
        except Exception:
            raise HTTPException(400, "That is not a picture Zerko can read.")
        im.thumbnail((2400, 2400), Image.LANCZOS)
        name = f"{cid}_{uuid.uuid4().hex}.jpg"
        im.save(_chat_dir() / name, "JPEG", quality=88)
        return {"file": name, "url": f"/api/team/file/{name}"}

    @app.get("/api/team/file/{name}")
    def chat_file(name: str, request: Request, db: Session = Depends(get_db)):
        # <img> cannot send a header: the token comes as ?token=; only people in the chat may open it
        user = access._user_for(request, db)
        if user is None:
            raise HTTPException(401, "Sign in first")
        m = re.fullmatch(r"(\d+)_[0-9a-f]{32}\.jpg", name)
        if not m:
            raise HTTPException(404, "No such picture")
        _channel(db, int(m.group(1)), user)
        f = _chat_dir() / name
        if not f.exists():
            raise HTTPException(404, "No such picture")
        return FileResponse(str(f), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    @app.post("/api/team/channels/{cid}/read")
    def mark_read(cid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
        _channel(db, cid, user)
        last = db.query(func.max(ChatMessage.id)).filter(ChatMessage.channel_id == cid).scalar() or 0
        _read(db, cid, user.id, last)
        return {"ok": True}

    @app.get("/api/team/stream")
    async def stream(request: Request):
        # EventSource cannot send a header, so the token comes as ?token=.
        db = SessionLocal()
        try:
            user = access._user_for(request, db)
        finally:
            db.close()
        if user is None:
            raise HTTPException(401, "Sign in first")
        q: queue.Queue = queue.Queue(maxsize=500)
        with _lock:
            _subs.append((user.id, q))

        async def gen():
            idle = 0
            try:
                yield "event: hello\ndata: {}\n\n"
                while not await request.is_disconnected():
                    sent = 0
                    while sent < 100:
                        try:
                            ev = q.get_nowait()
                        except queue.Empty:
                            break
                        yield f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n"
                        sent += 1
                    if not sent:
                        idle += 1
                        if idle % 40 == 0:
                            yield ": keep-alive\n\n"
                        await asyncio.sleep(0.4)
                    else:
                        idle = 0
            except asyncio.CancelledError:
                pass
            finally:
                with _lock:
                    if (user.id, q) in _subs:
                        _subs.remove((user.id, q))

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
