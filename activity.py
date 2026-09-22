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


class ActivitySeen(Base):
    """When each account last opened the bell."""
    __tablename__ = "activity_seen"
    username = Column(String, primary_key=True)
    seen_at = Column(DateTime, default=datetime.utcnow)


class Event(BaseModel):
    id: str
    kind: str                      # note | pick | comment | upload | share_view
    at: datetime
    who: Optional[str] = None
    text: Optional[str] = None
    media_id: Optional[int] = None
    filename: Optional[str] = None
    folder_id: Optional[int] = None
    share_title: Optional[str] = None
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
                share_title=title,
            ))
        if sel.comment:
            out.append(Event(
                id=f"comment-{sel.id}", kind="comment", at=when, who=who,
                text=sel.comment,
                media_id=video.id, filename=video.filename, folder_id=video.folder_id,
                share_title=title,
            ))

    # Files that arrived through an upload link.
    uploads = (db.query(Video)
                 .filter(Video.uploaded_at.isnot(None))
                 .order_by(Video.uploaded_at.desc())
                 .limit(MAX_EVENTS).all())
    for video in uploads:
        out.append(Event(
            id=f"upload-{video.id}", kind="upload", at=video.uploaded_at,
            who=video.uploaded_by, media_id=video.id, filename=video.filename,
            folder_id=video.folder_id,
        ))

    # Someone opened a link. Only the most recent visit per share is kept by
    # the schema, so this is one event per link, not one per visit.
    shares = (db.query(Share)
                .filter(Share.last_viewed_at.isnot(None), Share.revoked.is_(False))
                .order_by(Share.last_viewed_at.desc())
                .limit(25).all())
    for share in shares:
        out.append(Event(
            id=f"view-{share.id}-{int(share.last_viewed_at.timestamp())}",
            kind="share_view", at=share.last_viewed_at,
            share_title=share.title or "a share link",
            text=f"{share.view_count or 1} view{'' if (share.view_count or 1) == 1 else 's'}",
        ))

    out.sort(key=lambda e: e.at, reverse=True)
    return out[:MAX_EVENTS]


@router.get("", response_model=Feed)
@router.get("/", response_model=Feed)
def feed(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    seen = _seen_at(db, current_user.username)
    events = _collect(db)
    unread = 0
    for e in events:
        # Your own notes are not news to you.
        mine = e.kind == "note" and (e.who or "") == current_user.username
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
    events = _collect(db)
    return Feed(events=events, unread=0, seen_at=_seen_at(db, current_user.username))


def install(app):
    Base.metadata.create_all(bind=engine, tables=[ActivitySeen.__table__])
    app.include_router(router)
