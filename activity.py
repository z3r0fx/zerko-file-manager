"""What happened while you were not looking.

Notes, client picks, client comments and files arriving through an upload link
all already leave a record in the database - but each one is only visible if
you happen to open the thing it happened to. This turns them into one feed the
bell in the header can show, newest first, with a per-user "seen" mark so the
badge only counts what you have not read.

Nothing new is written when an event happens: the feed is assembled from the
rows that already exist, so there is no queue to keep in sync and no way for a
notification to outlive the thing it points at.
"""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Column, DateTime, String
from sqlalchemy.orm import Session

from auth import get_current_user
from database import (Base, IndexedFolder, Note, Share, ShareSelect, User,
                      Video, engine, get_db)

router = APIRouter(prefix="/api/activity", tags=["activity"])

# How far back the feed looks. A bell is for "since I last looked", not an
# audit log - the pages themselves hold the full history.
MAX_EVENTS = 60

# uploaded_by values that mean "the server did this", not a person.
SYSTEM_UPLOADERS = ("indexed", "hdr", "copy", "photo-edit")


class ActivitySeen(Base):
    """When each account last opened the bell."""
    __tablename__ = "activity_seen"
    username = Column(String, primary_key=True)
    seen_at = Column(DateTime, default=datetime.utcnow)


class Event(BaseModel):
    id: str
    kind: str                      # note | pick | comment | upload | share_view | download | done
    at: datetime
    who: Optional[str] = None
    text: Optional[str] = None
    media_id: Optional[int] = None
    filename: Optional[str] = None
    folder_id: Optional[int] = None
    share_title: Optional[str] = None
    share_id: Optional[int] = None
    count: int = 1                 # uploads: how many files arrived together
    unread: bool = False


class Feed(BaseModel):
    events: List[Event]
    unread: int
    seen_at: Optional[datetime] = None


def _seen_at(db: Session, username: str) -> Optional[datetime]:
    row = db.query(ActivitySeen).filter(ActivitySeen.username == username).first()
    return row.seen_at if row else None


def _collect(db: Session) -> List[Event]:
    out: List[Event] = []

    # Notes somebody left on a clip or a photo.
    notes = (db.query(Note, Video)
               .join(Video, Video.id == Note.media_id)
               .order_by(Note.created_at.desc())
               .limit(MAX_EVENTS).all())
    for note, video in notes:
        out.append(Event(
            id=f"note-{note.id}", kind="note", at=note.created_at or datetime.utcnow(),
            who=note.author, text=note.content,
            media_id=video.id, filename=video.filename, folder_id=video.folder_id,
        ))

    # Picks and comments from a share link. One row can be both: a client can
    # tick a clip and say why, which reads better as two lines than one.
    selects = (db.query(ShareSelect, Video, Share)
                 .join(Video, Video.id == ShareSelect.video_id)
                 .join(Share, Share.id == ShareSelect.share_id)
                 .order_by(ShareSelect.created_at.desc())
                 .limit(MAX_EVENTS).all())
    for sel, video, share in selects:
        when = sel.created_at or datetime.utcnow()
        title = share.title or "a share link"
        who = sel.viewer_name or "Someone"
        if sel.picked:
            out.append(Event(
                id=f"pick-{sel.id}", kind="pick", at=when, who=who,
                media_id=video.id, filename=video.filename, folder_id=video.folder_id,
                share_title=title, share_id=share.id,
            ))
        if sel.comment:
            out.append(Event(
                id=f"comment-{sel.id}", kind="comment", at=when, who=who,
                text=sel.comment,
                media_id=video.id, filename=video.filename, folder_id=video.folder_id,
                share_title=title, share_id=share.id,
            ))

    # Files people added: through an upload link ("name (link)") or by a
    # colleague uploading. Files the scanner found, HDR merges, copies and
    # photo exports are the system's own doing, not news - a rescan of the
    # library used to put every file on the drive in the bell. One upload of
    # forty files is one line, not forty.
    uploads = (db.query(Video)
                 .filter(Video.uploaded_at.isnot(None),
                         Video.uploaded_by.isnot(None),
                         Video.uploaded_by.notin_(SYSTEM_UPLOADERS))
                 .order_by(Video.uploaded_at.desc())
                 .limit(MAX_EVENTS * 10).all())
    batch: List[Video] = []

    def flush():
        if not batch:
            return
        first = batch[0]
        out.append(Event(
            id=f"upload-{first.id}", kind="upload", at=first.uploaded_at,
            who=first.uploaded_by, media_id=first.id, filename=first.filename,
            folder_id=first.folder_id, count=len(batch),
        ))
        batch.clear()

    for video in uploads:
        if batch and (video.uploaded_by != batch[-1].uploaded_by
                      or video.folder_id != batch[-1].folder_id
                      or (batch[-1].uploaded_at - video.uploaded_at).total_seconds() > 600):
            flush()
        batch.append(video)
    flush()

    # What clients did on the links: from the portal's own event log, so a
    # download or "I'm done" is news, not just the last visit.
    try:
        from delivery import ShareEvent, VIEWED, DOWNLOADED, DOWNLOADED_ZIP, CONFIRMED
        from datetime import timedelta
        since = datetime.utcnow() - timedelta(days=30)
        evs = (db.query(ShareEvent).filter(ShareEvent.at >= since,
                                           ShareEvent.kind.in_([VIEWED, DOWNLOADED, DOWNLOADED_ZIP, CONFIRMED]))
               .order_by(ShareEvent.at.desc()).limit(2000).all())
        shares = {sh.id: sh for sh in db.query(Share).filter(
            Share.id.in_({e.share_id for e in evs} or {0})).all()}
        seen_view = set()
        dl: dict = {}
        for e in evs:
            sh = shares.get(e.share_id)
            if not sh or sh.revoked:
                continue
            title = sh.title or "a share link"
            who = e.viewer_name or None
            if e.kind == VIEWED:
                key = (e.share_id, e.at.date(), who)
                if key in seen_view:
                    continue
                seen_view.add(key)
                out.append(Event(id=f"view-{e.id}", kind="share_view", at=e.at, who=who,
                                 share_title=title, share_id=sh.id,
                                 text=("on a phone" if e.device == "phone" else None)))
            elif e.kind == DOWNLOADED:
                # one visit's downloads are one line
                g = dl.get(e.share_id)
                if g and (g["at"] - e.at).total_seconds() < 1800:
                    g["n"] += 1
                    continue
                dl[e.share_id] = {"at": e.at, "n": 1}
                ev = Event(id=f"dl-{e.id}", kind="download", at=e.at, who=who,
                           share_title=title, share_id=sh.id, media_id=e.video_id, count=1)
                dl[e.share_id]["ev"] = ev
                out.append(ev)
            elif e.kind == DOWNLOADED_ZIP:
                out.append(Event(id=f"zip-{e.id}", kind="download", at=e.at, who=who,
                                 share_title=title, share_id=sh.id, text="everything as a zip"))
            elif e.kind == CONFIRMED:
                out.append(Event(id=f"done-{e.id}", kind="done", at=e.at, who=who,
                                 share_title=title, share_id=sh.id, text=e.detail))
        for g in dl.values():
            g["ev"].count = g["n"]
    except Exception as ex:
        print(f"activity: portal events: {ex}", flush=True)

    out.sort(key=lambda e: e.at, reverse=True)
    return out[:MAX_EVENTS]


def _visible(events: List[Event], user: User) -> List[Event]:
    """What this account may see. Client picks, comments, link visits and
    uploads belong to whoever runs the client links; everyone else gets the
    notes, which they can already read on the clips themselves."""
    import permissions
    if permissions.can(user.role, permissions.SHARES):
        return events
    return [e for e in events if e.kind == "note"]


@router.get("", response_model=Feed)
@router.get("/", response_model=Feed)
def feed(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    seen = _seen_at(db, current_user.username)
    events = _visible(_collect(db), current_user)
    unread = 0
    for e in events:
        # Your own notes and uploads are not news to you.
        mine = e.kind in ("note", "upload") and (e.who or "") == current_user.username
        e.unread = bool((seen is None or e.at > seen) and not mine)
        if e.unread:
            unread += 1
    return Feed(events=events, unread=unread, seen_at=seen)


@router.post("/seen", response_model=Feed)
def mark_seen(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = db.query(ActivitySeen).filter(ActivitySeen.username == current_user.username).first()
    if row:
        row.seen_at = datetime.utcnow()
    else:
        db.add(ActivitySeen(username=current_user.username, seen_at=datetime.utcnow()))
    db.commit()
    events = _visible(_collect(db), current_user)
    return Feed(events=events, unread=0, seen_at=_seen_at(db, current_user.username))


def install(app):
    Base.metadata.create_all(bind=engine, tables=[ActivitySeen.__table__])
    app.include_router(router)
