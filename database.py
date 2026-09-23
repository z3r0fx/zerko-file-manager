from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, Table, Boolean, Float, text, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship, backref
from datetime import datetime
import os
import logging

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./mediamanager.db")

# check_same_thread=False: requests now run in FastAPI's worker threads.
# timeout=30: wait for the write lock instead of raising "database is locked".
# A real pool, because many requests can now be in flight at the same time.
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
    pool_size=20,
    max_overflow=40,
    pool_pre_ping=True,
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record):
    """WAL lets readers and the writer work at the same time.

    Without it every listing blocks behind any write, which is a large part of
    why the UI felt frozen whenever somebody was uploading.
    """
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA cache_size=-64000")
        cursor.close()
    except Exception as e:
        logging.warning(f"Could not apply SQLite pragmas: {e}")


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Association table for video tags
video_tags = Table('video_tags', Base.metadata,
    Column('video_id', Integer, ForeignKey('videos.id')),
    Column('tag_id', Integer, ForeignKey('tags.id'))
)

class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    role = Column(String, default="user")  # admin or user
    session_token = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)

class IndexedFolder(Base):
    __tablename__ = "indexed_folders"
    
    id = Column(Integer, primary_key=True, index=True)
    path = Column(String, unique=True)
    name = Column(String)
    order = Column(Integer, default=0)
    added_at = Column(DateTime, default=datetime.utcnow)
    last_scanned = Column(DateTime, nullable=True)
    # Set when a person made this folder here in Zerko, rather than it being
    # found on disk by the indexer. An empty folder you just made should still
    # appear in the sidebar; an empty folder that happens to exist on the drive
    # should not.
    created_in_app = Column(DateTime, nullable=True)
    # Nested folders: mirrors the directory tree on disk.
    parent_id = Column(Integer, ForeignKey("indexed_folders.id"), nullable=True)
    relative_path = Column(String, nullable=True)   # path relative to the media root
    children = relationship("IndexedFolder",
                            backref=backref("parent", remote_side=[id]),
                            cascade="all")
    videos = relationship("Video", back_populates="folder", cascade="all, delete-orphan")

class Video(Base):
    __tablename__ = "videos"
    
    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String)
    filepath = Column(String, unique=True)
    file_size = Column(Integer)  # bytes
    duration = Column(Float, nullable=True)  # seconds
    thumbnail_path = Column(String, nullable=True)
    folder_id = Column(Integer, ForeignKey("indexed_folders.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    # New fields for uploads
    uploaded_by = Column(String, nullable=True)
    uploaded_at = Column(DateTime, nullable=True)
    transcription = Column(String, nullable=True)
    status = Column(String, default="raw")
    media_type = Column(String, default="video")
    
    # Metadata fields
    rating = Column(Integer, nullable=True) # 1-5
    # Lightroom-style colour label: red / yellow / green / blue / purple
    color_label = Column(String, nullable=True)
    shoot_date = Column(DateTime, nullable=True)
    
    # Technical metadata fields
    camera_make = Column(String, nullable=True)
    camera_model = Column(String, nullable=True)
    video_codec = Column(String, nullable=True)
    frame_rate = Column(String, nullable=True)
    resolution = Column(String, nullable=True)
    audio_codec = Column(String, nullable=True)

    # Proxy tracking
    proxy_status = Column(String, default="not_generated") # not_generated, queued, processing, completed, failed
    proxy_path = Column(String, nullable=True)
    # MUST be declared here, not only added by the raw ALTER TABLE migration -
    # otherwise Video.is_active raises AttributeError and every listing 500s.
    is_active = Column(Boolean, default=True)
    original_path = Column(String, nullable=True)  # where it lived before the trash
    trashed_at = Column(DateTime, nullable=True)     # when it went to the trash (auto-empty counts from here)
    proxy_error = Column(String, nullable=True)
    proxy_created_at = Column(DateTime, nullable=True)
    # How many times the encode has failed. The background sweeper retries a
    # failure a few times (a file still being copied in fails once and then
    # works) and then leaves it alone instead of burning CPU on it forever.
    proxy_attempts = Column(Integer, default=0)

    # Content hash, for finding true duplicates rather than same-named files.
    file_hash = Column(String, nullable=True, index=True)
    hashed_at = Column(DateTime, nullable=True)

    # Transcription tracking
    transcription_status = Column(String, default="not_started") # not_started, queued, processing, completed, failed
    
    folder = relationship("IndexedFolder", back_populates="videos")
    tags = relationship("Tag", secondary=video_tags, back_populates="videos")
    segments = relationship("TranscriptionSegment", back_populates="video", cascade="all, delete-orphan")

class TranscriptionSegment(Base):
    __tablename__ = "transcription_segments"
    
    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(Integer, ForeignKey("videos.id"), index=True)
    text = Column(String)
    start_time = Column(Float) # seconds
    end_time = Column(Float)   # seconds
    
    video = relationship("Video", back_populates="segments")

class Tag(Base):
    __tablename__ = "tags"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    parent_id = Column(Integer, ForeignKey("tags.id"), nullable=True)
    folder_id = Column(Integer, ForeignKey("indexed_folders.id"), nullable=True)
    
    videos = relationship("Video", secondary=video_tags, back_populates="tags")
    parent = relationship("Tag", remote_side=[id], back_populates="children")
    children = relationship("Tag", back_populates="parent")

class Note(Base):
    __tablename__ = "notes"
    
    id = Column(Integer, primary_key=True, index=True)
    media_id = Column(Integer, ForeignKey("videos.id"))
    content = Column(String)
    author = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class Share(Base):
    """A tokenised, public link to a folder or a fixed set of clips.

    Deliberately not tied to a user account: the whole point is that a client
    or agent opens it without signing up for anything. Access is the token,
    so the token is long and random, and the link can expire.
    """
    __tablename__ = "shares"

    id = Column(Integer, primary_key=True, index=True)
    token = Column(String, unique=True, index=True, nullable=False)
    title = Column(String, nullable=True)
    message = Column(String, nullable=True)          # a note shown to the viewer

    folder_id = Column(Integer, ForeignKey("indexed_folders.id"), nullable=True)
    include_subfolders = Column(Boolean, default=True)
    video_ids = Column(String, nullable=True)        # CSV, when sharing a selection

    password_hash = Column(String, nullable=True)    # optional extra gate
    expires_at = Column(DateTime, nullable=True)
    allow_download = Column(Boolean, default=False)
    allow_selects = Column(Boolean, default=True)    # client can pick favourites
    # An upload link: the same token machinery, pointed the other way. Used
    # for "send this to my phone and shoot straight into the job folder", and
    # for letting an agent send footage in without an account.
    allow_upload = Column(Boolean, default=False)
    upload_count = Column(Integer, default=0)
    # What this portal is FOR. One flag-soup portal that might send and might
    # receive is harder to reason about than two named kinds - and the viewer
    # sees a different page for each.
    #   send    - they look, pick, comment, maybe download
    #   receive - they send files in and see nothing of the library
    #   both    - the old behaviour, kept for links already out in the world
    kind = Column(String, default="send")
    # When the viewer said "I am done". Picks save as they are made, but
    # nothing ever told them so and nothing told you they had finished.
    confirmed_at = Column(DateTime, nullable=True)
    confirmed_by = Column(String, nullable=True)
    # Downloading as one archive. Off means the zip endpoint refuses and the
    # viewer is only offered files one at a time - which is also what a phone
    # wants, because a zip lands in Files rather than the camera roll.
    allow_zip = Column(Boolean, default=True)
    # Where files sent through this link are held until someone files them.
    intake_folder_id = Column(Integer, ForeignKey("indexed_folders.id"), nullable=True)

    # The property this portal is delivering. A portal without one is still
    # perfectly valid - not every link is a job - but when it has one, the
    # shoot is what the work is filed under and reported against.
    shoot_id = Column(Integer, nullable=True, index=True)
    # Previews carry the watermark; the actual download does not. The point of
    # a delivery is that someone can look at the whole set before anything is
    # paid for, without being able to use it.
    watermark_previews = Column(Boolean, default=False)
    # Which logo, by file name in _watermarks. Empty means the first one - and
    # a name whose file has since been deleted also falls back to the first,
    # so a portal never quietly starts showing clean previews.
    watermark_name = Column(String, nullable=True)

    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    revoked = Column(Boolean, default=False)
    view_count = Column(Integer, default=0)
    last_viewed_at = Column(DateTime, nullable=True)

    selects = relationship("ShareSelect", back_populates="share",
                           cascade="all, delete-orphan")


class ShareSelect(Base):
    """One clip a client picked on a share link, plus optional comment."""
    __tablename__ = "share_selects"

    id = Column(Integer, primary_key=True, index=True)
    share_id = Column(Integer, ForeignKey("shares.id"), nullable=False, index=True)
    video_id = Column(Integer, ForeignKey("videos.id"), nullable=False, index=True)
    picked = Column(Boolean, default=True)
    comment = Column(String, nullable=True)
    viewer_name = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    share = relationship("Share", back_populates="selects")


def init_db():
    Base.metadata.create_all(bind=engine)
    
    # Automated migration for SQLite columns
    with engine.connect() as conn:
        # Check for proxy columns
        res = conn.execute(text("PRAGMA table_info(videos)"))
        existing_cols = [row[1] for row in res]
        
        new_cols = {
            "proxy_status": "TEXT DEFAULT 'not_generated'",
            "proxy_path": "TEXT",
            "proxy_error": "TEXT",
            "proxy_created_at": "DATETIME",
            "transcription_status": "TEXT DEFAULT 'not_started'",
            "is_active": "BOOLEAN DEFAULT 1",
            "original_path": "TEXT",
            "proxy_attempts": "INTEGER DEFAULT 0",
            "trashed_at": "TIMESTAMP",
            "color_label": "TEXT",
        }
        
        for col, col_type in new_cols.items():
            if col not in existing_cols:
                logging.info(f"Adding column {col} to videos table")
                conn.execute(text(f"ALTER TABLE videos ADD COLUMN {col} {col_type}"))
                
        # Check for indexed_folders columns
        res = conn.execute(text("PRAGMA table_info(indexed_folders)"))
        existing_folder_cols = [row[1] for row in res]
        if "order" not in existing_folder_cols:
            logging.info("Adding column order to indexed_folders table")
            conn.execute(text("ALTER TABLE indexed_folders ADD COLUMN 'order' INTEGER DEFAULT 0"))
        for col, col_type in {"parent_id": "INTEGER", "relative_path": "TEXT",
                              "created_in_app": "TIMESTAMP"}.items():
            if col not in existing_folder_cols:
                logging.info(f"Adding column {col} to indexed_folders table")
                conn.execute(text(f"ALTER TABLE indexed_folders ADD COLUMN {col} {col_type}"))

        # Duplicate detection needs a content hash per file.
        for col, col_type in {"file_hash": "TEXT", "hashed_at": "TIMESTAMP"}.items():
            if col not in existing_cols:
                logging.info(f"Adding column {col} to videos table")
                conn.execute(text(f"ALTER TABLE videos ADD COLUMN {col} {col_type}"))

        # Share links gained an upload direction.
        res = conn.execute(text("PRAGMA table_info(shares)"))
        existing_share_cols = [row[1] for row in res]
        for col, col_type in {"allow_upload": "BOOLEAN DEFAULT 0",
                              "upload_count": "INTEGER DEFAULT 0",
                              "intake_folder_id": "INTEGER",
                              "kind": "TEXT DEFAULT 'send'",
                              "allow_zip": "BOOLEAN DEFAULT 1",
                              "confirmed_at": "DATETIME",
                              "confirmed_by": "TEXT"}.items():
            if col not in existing_share_cols:
                logging.info(f"Adding column {col} to shares table")
                conn.execute(text(f"ALTER TABLE shares ADD COLUMN {col} {col_type}"))

        # Check for tags columns
        res = conn.execute(text("PRAGMA table_info(tags)"))
        existing_tag_cols = [row[1] for row in res]
        if "folder_id" not in existing_tag_cols:
            logging.info("Adding column folder_id to tags table")
            conn.execute(text("ALTER TABLE tags ADD COLUMN folder_id INTEGER"))
        
        conn.commit()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
