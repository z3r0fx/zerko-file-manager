"""Deliveries: a portal that belongs to a property, and a record of what the
person on the other end actually did with it.

Two things were missing. A portal knew nothing about the job it belonged to,
so "what went out for 12 Ocean View Drive" had no answer. And a portal counted
views but nothing else, so "did the agent ever download the photos" had no
answer either - which is the question that gets asked on the phone.
"""

import os
import queue
import secrets
import shutil
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import (Column, DateTime, ForeignKey, Integer, String, text)
from sqlalchemy.orm import Session

import permissions
import shoots as shoots_mod
from auth import get_current_user
from database import (Base, IndexedFolder, Share, User, Video, engine, get_db)

router = APIRouter(tags=["deliveries"])

# How long a delivery link lives unless told otherwise. Long enough that an
# agent does not come back to a dead link mid-listing, short enough that the
# whole archive is not sitting on the open internet a year later.
DEFAULT_EXPIRY_DAYS = 30

VIEWED = "viewed"
DOWNLOADED = "downloaded"       # one file
DOWNLOADED_ZIP = "downloaded_zip"
UPLOADED = "uploaded"
CONFIRMED = "confirmed"
TERMS = "terms"                 # agreed to the studio's terms (Manage > Business)
PAID_CLAIM = "paid_claim"       # said on the portal that they have paid

EVENT_LABELS = {
    VIEWED: "Opened the link",
    DOWNLOADED: "Downloaded a file",
    DOWNLOADED_ZIP: "Downloaded everything as a zip",
    UPLOADED: "Sent a file in",
    CONFIRMED: "Said they were done",
    TERMS: "Agreed to the terms",
    PAID_CLAIM: "Said they have paid",
}


class ShareEvent(Base):
    """One thing a viewer did on a portal.

    Deliberately holds no IP address. Knowing that someone opened the link and
    took three files is what the job needs; where they were standing is not,
    and storing it would make every delivery link a small privacy liability.
    """
    __tablename__ = "share_events"
    id = Column(Integer, primary_key=True)
    share_id = Column(Integer, ForeignKey("shares.id"), nullable=False, index=True)
    kind = Column(String, nullable=False)
    video_id = Column(Integer, nullable=True)
    detail = Column(String, nullable=True)
    viewer_name = Column(String, nullable=True)
    # "phone" or "computer". Enough to know how it was looked at, and nothing
    # that identifies the person or the device.
    device = Column(String, nullable=True)
    at = Column(DateTime, default=datetime.utcnow, index=True)


# --------------------------------------------------------------------------
# recording
# --------------------------------------------------------------------------

def _device(request: Optional[Request]) -> Optional[str]:
    if request is None:
        return None
    ua = (request.headers.get("user-agent") or "").lower()
    if not ua:
        return None
    phone = any(w in ua for w in ("iphone", "android", "ipad", "mobile"))
    return "phone" if phone else "computer"


def log(db: Session, share: Share, kind: str, video_id: Optional[int] = None,
        detail: Optional[str] = None, viewer_name: Optional[str] = None,
        request: Optional[Request] = None):
    """Record an event. Never raises: a portal that works is worth more than a
    log line, so a failure here must not break the viewer's page."""
    try:
        db.add(ShareEvent(share_id=share.id, kind=kind, video_id=video_id,
                          detail=(detail or None),
                          viewer_name=(viewer_name or None),
                          device=_device(request)))
        db.commit()
    except Exception as e:                                  # pragma: no cover
        try:
            db.rollback()
        except Exception:
            pass
        print(f"delivery: could not record {kind}: {e}", flush=True)


def _may(user: User):
    if not permissions.can(user.role, permissions.SHARES):
        raise HTTPException(status_code=403,
                            detail="Your account cannot manage portals.")


# --------------------------------------------------------------------------
# creating a delivery from a shoot
# --------------------------------------------------------------------------

class NewDelivery(BaseModel):
    folder_path: Optional[str] = None      # defaults to the shoot's folders
    title: Optional[str] = Field(None, max_length=200)
    message: Optional[str] = Field(None, max_length=1000)
    expires_days: int = Field(DEFAULT_EXPIRY_DAYS, ge=1, le=365)
    allow_download: bool = True
    allow_zip: bool = True
    watermark_previews: bool = True
    watermark_name: Optional[str] = Field(None, max_length=255)
    mark_delivered: bool = False          # move the property to "posted"


@router.post("/api/shoots/{shoot_id}/deliver")
def deliver(shoot_id: int, body: NewDelivery, db: Session = Depends(get_db),
            current_user: User = Depends(get_current_user)):
    """Make a client-facing link for a shoot, in one step.

    The address becomes the title, the shoot's folder becomes the contents,
    and (when asked) the shoot moves to 'posted'.
    """
    _may(current_user)
    shoot = db.query(shoots_mod.Shoot).filter(
        shoots_mod.Shoot.id == shoot_id).first()
    if not shoot:
        raise HTTPException(status_code=404, detail="No such shoot")

    paths = [f.path for f in db.query(shoots_mod.ShootFolder)
             .filter(shoots_mod.ShootFolder.shoot_id == shoot.id)
             .order_by(shoots_mod.ShootFolder.path).all()]
    if body.folder_path:
        if body.folder_path not in paths:
            raise HTTPException(status_code=400,
                                detail="That folder is not on this shoot.")
        chosen = body.folder_path
    elif len(paths) == 1:
        chosen = paths[0]
    elif not paths:
        raise HTTPException(
            status_code=400,
            detail="This shoot has no folders yet, so there is nothing to send.")
    else:
        raise HTTPException(
            status_code=400,
            detail="This shoot has several folders - say which one to send.")

    folder = db.query(IndexedFolder).filter(IndexedFolder.path == chosen).first()
    if not folder:
        raise HTTPException(
            status_code=400,
            detail="That folder is not indexed yet. Rescan the library first.")

    share = Share(
        token=secrets.token_urlsafe(24),
        title=body.title or shoot.address,
        message=body.message or None,
        folder_id=folder.id,
        include_subfolders=True,
        kind="send",
        allow_download=body.allow_download,
        allow_zip=body.allow_zip,
        allow_selects=True,
        watermark_previews=body.watermark_previews,
        watermark_name=(os.path.basename(body.watermark_name)
                        if body.watermark_name and watermark_exists(os.path.basename(body.watermark_name))
                        else None),
        shoot_id=shoot.id,
        expires_at=datetime.utcnow() + timedelta(days=body.expires_days),
        created_by=current_user.username,
    )
    db.add(share)
    if body.mark_delivered and shoot.status in ("shot", "editing"):
        import shoots as _shoots
        _shoots._set_status(shoot, "posted")
    db.commit()
    db.refresh(share)
    return _delivery_dict(db, share, shoot)


def _delivery_dict(db: Session, share: Share, shoot=None) -> dict:
    counts = {}
    rows = db.query(ShareEvent.kind, ShareEvent.id) \
             .filter(ShareEvent.share_id == share.id).all()
    for kind, _ in rows:
        counts[kind] = counts.get(kind, 0) + 1
    last = db.query(ShareEvent).filter(ShareEvent.share_id == share.id) \
             .order_by(ShareEvent.at.desc()).first()
    return {
        "id": share.id,
        "token": share.token,
        "title": share.title,
        "url": f"/s/{share.token}",
        "shoot_id": share.shoot_id,
        "shoot_address": shoot.address if shoot else None,
        "watermark_previews": bool(share.watermark_previews),
        "watermark_name": share.watermark_name,
        "allow_download": bool(share.allow_download),
        "expires_at": share.expires_at.isoformat() if share.expires_at else None,
        "revoked": bool(share.revoked),
        "created_at": share.created_at.isoformat() if share.created_at else None,
        "confirmed_at": share.confirmed_at.isoformat() if share.confirmed_at else None,
        "confirmed_by": share.confirmed_by,
        "activity": counts,
        "last_activity_at": last.at.isoformat() if last else None,
    }


@router.get("/api/shoots/{shoot_id}/deliveries")
def shoot_deliveries(shoot_id: int, db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    """Every portal sent out for this property."""
    shoot = db.query(shoots_mod.Shoot).filter(
        shoots_mod.Shoot.id == shoot_id).first()
    if not shoot:
        raise HTTPException(status_code=404, detail="No such shoot")
    rows = db.query(Share).filter(Share.shoot_id == shoot_id) \
             .order_by(Share.created_at.desc()).all()
    return {"deliveries": [_delivery_dict(db, s, shoot) for s in rows]}


# --------------------------------------------------------------------------
# what happened on a portal
# --------------------------------------------------------------------------

@router.get("/api/shares/{share_id}/activity")
def share_activity(share_id: int, limit: int = 200,
                   db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    """The trail for one portal: who opened it, what they took, when.

    Answers the question that actually gets asked - "did they ever download
    it?" - without anybody having to remember.
    """
    _may(current_user)
    share = db.query(Share).filter(Share.id == share_id).first()
    if not share:
        raise HTTPException(status_code=404, detail="No such portal")
    # Who opened a client's link and what they took is the link owner's business.
    if current_user.role != "admin" and share.created_by != current_user.username:
        raise HTTPException(status_code=403, detail="Not your portal")

    rows = db.query(ShareEvent).filter(ShareEvent.share_id == share_id) \
             .order_by(ShareEvent.at.desc()).limit(max(1, min(1000, limit))).all()

    names = {}
    for v in db.query(Video.id, Video.filename).filter(
            Video.id.in_([r.video_id for r in rows if r.video_id] or [0])).all():
        names[v.id] = v.filename

    events = [{
        "at": r.at.isoformat() if r.at else None,
        "kind": r.kind,
        "label": EVENT_LABELS.get(r.kind, r.kind),
        "video_id": r.video_id,
        "filename": names.get(r.video_id),
        "detail": r.detail,
        "viewer_name": r.viewer_name,
        "device": r.device,
    } for r in rows]

    summary = {}
    for e in events:
        summary[e["kind"]] = summary.get(e["kind"], 0) + 1

    return {
        "share_id": share.id,
        "title": share.title,
        "summary": summary,
        "downloaded_files": sorted({e["filename"] for e in events
                                    if e["kind"] == DOWNLOADED and e["filename"]}),
        "events": events,
    }


# --------------------------------------------------------------------------
# watermarked previews
# --------------------------------------------------------------------------

# Watermarking a thumbnail costs a decode, a composite and an encode. A client
# scrolling a 200-photo gallery would pay it 200 times, twice, so the result is
# written next to the thumbnail and reused until the thumbnail changes.
_PREVIEW_DIR_NAME = "_wm_previews"


def _sized_mark(mark: Path, frame_w: int, frame_h: int):
    """The watermark scaled and faded for a frame of this size, plus where it
    goes. One function so stills, display previews and video all place the
    mark identically: 28% of the width, 55% opacity, inset 4% bottom-right."""
    from PIL import Image
    wm = Image.open(mark).convert("RGBA")
    tw = max(8, int(frame_w * 0.28))
    th = max(8, int(wm.height * (tw / wm.width)))
    wm = wm.resize((tw, th), Image.LANCZOS)
    wm.putalpha(wm.split()[3].point(lambda a: int(a * 0.55)))
    x = max(0, frame_w - tw - int(frame_w * 0.04))
    y = max(0, frame_h - th - int(frame_h * 0.04))
    return wm, x, y


def _composite(base, mark: Path):
    wm, x, y = _sized_mark(mark, base.width, base.height)
    base.paste(wm, (x, y), wm)
    return base


def watermarked_preview(thumb_path: str, thumbs_root: Optional[str] = None,
                        mark_name: Optional[str] = None) -> str:
    """The thumbnail with the portal's watermark (or the first one) over it.

    Falls back to the plain thumbnail on any failure - a preview that is not
    watermarked is a smaller problem than a gallery of broken images, and the
    download itself is gated separately.
    """
    import photo_edit
    try:
        from PIL import Image
    except Exception:
        return thumb_path

    src = Path(thumb_path)
    if not src.is_file():
        return thumb_path
    mark = _first_mark(mark_name)
    if not mark:
        return thumb_path
    root = Path(thumbs_root) if thumbs_root else src.parent
    out_dir = root / _PREVIEW_DIR_NAME
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return thumb_path
    out = out_dir / f"{src.stem}__{mark.stem}.jpg"

    try:
        if out.is_file() and out.stat().st_mtime >= max(src.stat().st_mtime,
                                                        mark.stat().st_mtime):
            return str(out)

        base = _composite(Image.open(src).convert("RGB"), mark)
        base.save(out, "JPEG", quality=86)
        return str(out)
    except Exception as e:
        print(f"delivery: could not watermark {src.name}: {e}", flush=True)
        return thumb_path


# --------------------------------------------------------------------------
# watermarked full-size previews: the portal's photo viewer and video player
# --------------------------------------------------------------------------
#
# Watermarking the thumbnail alone left the real gap open: the portal's
# lightbox loads /stream/{id}, which served the ORIGINAL photo at full
# resolution, unmarked - one long-press away from being saved. These give the
# viewer and the player their own marked copies. Unlike the thumbnail, they
# FAIL CLOSED: if a marked copy cannot be made, the caller serves the marked
# thumbnail or nothing, never the original.

DISPLAY_WIDTH = 2048          # big enough for a laptop lightbox, useless for print
VIDEO_WIDTH = 1280


def _first_mark(name: Optional[str] = None) -> Optional[Path]:
    """The watermark a portal asked for by file name, or the first one in the
    folder when it asked for none - or asked for one that has since been
    deleted, because a portal that suddenly shows clean previews is worse
    than one showing a different logo."""
    import photo_edit
    try:
        marks = photo_edit.watermark_files()
    except Exception:
        return None
    if name:
        for m in marks:
            if m.name == name:
                return m
    return marks[0] if marks else _plain_mark()


def _plain_mark() -> Optional[Path]:
    """A plain "PREVIEW" mark for a studio that has not added its own logo yet: an
    unpaid or watermarked portal must never fall back to clean pictures."""
    import tempfile
    out = Path(tempfile.gettempdir()) / "zerko-preview-mark.png"
    if out.is_file():
        return out
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGBA", (1200, 300), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        try:
            font = ImageFont.load_default(size=220)
        except TypeError:
            font = ImageFont.load_default()
        box = d.textbbox((0, 0), "PREVIEW", font=font, stroke_width=10)
        x, y = (1200 - (box[2] - box[0])) // 2 - box[0], (300 - (box[3] - box[1])) // 2 - box[1]
        d.text((x, y), "PREVIEW", font=font, fill=(255, 255, 255, 255), stroke_width=10, stroke_fill=(0, 0, 0, 200))
        img.save(out)
        return out
    except Exception as e:
        print(f"delivery: could not make the plain preview mark: {e}", flush=True)
        return None


def watermark_exists(name: Optional[str]) -> bool:
    if not name:
        return True
    import photo_edit
    try:
        return any(m.name == name for m in photo_edit.watermark_files())
    except Exception:
        return False


def _fresh(out: Path, *sources) -> bool:
    try:
        if not out.is_file() or out.stat().st_size == 0:
            return False
        t = out.stat().st_mtime
        return all(t >= Path(x).stat().st_mtime for x in sources if x)
    except OSError:
        return False


def watermarked_display(src_path: str, cache_dir: str, key: str,
                        mark_name: Optional[str] = None) -> Optional[str]:
    """A marked JPEG of a photo at display size, or None if one cannot be made.

    Goes through previews.make_image_jpeg so RAW, HEIC and TIFF work exactly as
    they do for thumbnails."""
    mark = _first_mark(mark_name)
    if not mark or not src_path or not os.path.isfile(src_path):
        return None
    out_dir = Path(cache_dir) / _PREVIEW_DIR_NAME
    out = out_dir / f"{key}__display__{mark.stem}.jpg"
    if _fresh(out, src_path, mark):
        return str(out)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        import previews
        from PIL import Image
        tmp = out_dir / f".{key}.{mark.stem}.plain.jpg"
        ok, why = previews.make_image_jpeg(src_path, str(tmp), DISPLAY_WIDTH)
        if not ok:
            print(f"delivery: no display preview for {key}: {why}", flush=True)
            return None
        img = _composite(Image.open(tmp).convert("RGB"), mark)
        part = out_dir / f".{key}.{mark.stem}.part.jpg"
        img.save(part, "JPEG", quality=85)
        os.replace(part, out)
        try:
            tmp.unlink()
        except OSError:
            pass
        return str(out)
    except Exception as e:
        print(f"delivery: could not watermark display {key}: {e}", flush=True)
        return None


# Video: burning a mark in is an ffmpeg encode, far too slow to do while a
# client waits on a request. One background worker makes them in order; the
# stream endpoint serves the marked file once it exists and a 503 until then
# (the player shows the marked poster meanwhile). A failed encode is not
# retried until the source, the mark or the server changes.
_video_q: "queue.Queue" = None
_video_pending = set()
_video_failed = {}
_video_lock = threading.Lock()


def _video_out(cache_dir: str, key: str, mark: Path) -> Path:
    return Path(cache_dir) / _PREVIEW_DIR_NAME / f"{key}__video__{mark.stem}.mp4"


def watermarked_video(src_path: str, cache_dir: str, key: str,
                      start: bool = True, mark_name: Optional[str] = None) -> Optional[str]:
    """The marked video if it is ready; otherwise queue it and return None."""
    mark = _first_mark(mark_name)
    if not mark or not src_path or not os.path.isfile(src_path):
        return None
    out = _video_out(cache_dir, key, mark)
    if _fresh(out, src_path, mark):
        return str(out)
    if start:
        _queue_video(src_path, cache_dir, key, mark)
    return None


def video_state(src_path: str, cache_dir: str, key: str,
                mark_name: Optional[str] = None) -> str:
    """ready / preparing / failed / unavailable - for the portal to explain."""
    mark = _first_mark(mark_name)
    if not mark or not src_path or not os.path.isfile(src_path):
        return "unavailable"
    if _fresh(_video_out(cache_dir, key, mark), src_path, mark):
        return "ready"
    if f"{key}|{mark.name}" in _video_failed:
        return "failed"
    return "preparing"


def _queue_video(src_path: str, cache_dir: str, key: str, mark: Path):
    global _video_q
    job = f"{key}|{mark.name}"      # the same clip under two logos is two jobs
    with _video_lock:
        if job in _video_pending or job in _video_failed:
            return
        if _video_q is None:
            _video_q = queue.Queue()
            threading.Thread(target=_video_worker, daemon=True,
                             name="zerko-wm-video").start()
        _video_pending.add(job)
        _video_q.put((src_path, cache_dir, key, mark))


def _video_worker():
    while True:
        src, cache_dir, key, mark = _video_q.get()
        job = f"{key}|{mark.name}"
        try:
            ok, why = _encode_marked_video(src, cache_dir, key, mark)
            if not ok:
                _video_failed[job] = why
                print(f"delivery: watermarked video {job} failed: {why}", flush=True)
        except Exception as e:
            _video_failed[job] = str(e)
            print(f"delivery: watermarked video {job} crashed: {e}", flush=True)
        finally:
            with _video_lock:
                _video_pending.discard(job)


def _encode_marked_video(src: str, cache_dir: str, key: str, mark: Path):
    """Scale to VIDEO_WIDTH and overlay the mark. The mark is pre-sized and
    pre-faded in Python so the ffmpeg side is a plain overlay - no
    scale2ref, which newer ffmpeg builds deprecate and older ones lack."""
    if not mark or not mark.is_file():
        return False, "no watermark"
    out = _video_out(cache_dir, key, mark)
    out.parent.mkdir(parents=True, exist_ok=True)
    w, h = _probe_size(src)
    if not w or not h:
        return False, "could not read the video's size"
    fw = min(VIDEO_WIDTH, w - (w % 2))
    fh = int(round(h * fw / w / 2.0)) * 2
    wm, x, y = _sized_mark(mark, fw, fh)
    png = out.parent / f".{key}.{mark.stem}.mark.png"
    wm.save(png)
    part = out.parent / f".{key}.{mark.stem}.part.mp4"
    cmd = [shutil.which("ffmpeg") or "ffmpeg", "-y", "-hide_banner",
           "-loglevel", "error", "-i", src, "-i", str(png),
           "-filter_complex",
           f"[0:v]scale={fw}:{fh},setsar=1[b];[b][1:v]overlay={x}:{y},format=yuv420p[v]",
           "-map", "[v]", "-map", "0:a?",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
           "-f", "mp4", str(part)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=60 * 60)
    except subprocess.TimeoutExpired:
        return False, "timed out"
    finally:
        try:
            png.unlink()
        except OSError:
            pass
    if r.returncode != 0 or not part.is_file() or part.stat().st_size == 0:
        try:
            part.unlink()
        except OSError:
            pass
        return False, (r.stderr or b"").decode("utf-8", "replace")[-400:]
    os.replace(part, out)
    return True, ""


def _probe_size(src: str):
    try:
        r = subprocess.run(
            [shutil.which("ffprobe") or "ffprobe", "-v", "error",
             "-select_streams", "v:0", "-show_entries", "stream=width,height",
             "-of", "csv=p=0:s=x", src],
            capture_output=True, timeout=60)
        w, h = r.stdout.decode().strip().splitlines()[0].split("x")[:2]
        return int(w), int(h)
    except Exception:
        return None, None


# --------------------------------------------------------------------------
# changing a portal after it has gone out
# --------------------------------------------------------------------------

class EditShare(BaseModel):
    title: Optional[str] = Field(None, max_length=200)
    message: Optional[str] = Field(None, max_length=2000)
    allow_selects: Optional[bool] = None
    watermark_previews: Optional[bool] = None
    # file name in _watermarks; "" goes back to the default (the first one)
    watermark_name: Optional[str] = Field(None, max_length=255)
    allow_download: Optional[bool] = None
    allow_zip: Optional[bool] = None
    extend_days: Optional[int] = Field(None, ge=1, le=365)
    # ask the viewer to agree to the studio's terms before anything opens
    ask_terms: Optional[bool] = None


@router.patch("/api/shares/{share_id}")
def edit_share(share_id: int, body: EditShare, db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    """The usual sequence for a delivery: previews marked and downloads off
    until the invoice is paid, then downloads on. Without this the only way
    to change a portal was to revoke it and send a new link."""
    share = db.query(Share).filter(Share.id == share_id).first()
    if not share:
        raise HTTPException(status_code=404, detail="Share not found")
    if current_user.role != "admin" and share.created_by != current_user.username:
        raise HTTPException(status_code=403, detail="Not your share")
    if body.title is not None:
        share.title = body.title.strip() or None
    if body.message is not None:
        share.message = body.message.strip() or None
    if body.allow_selects is not None and (share.kind or "send") != "receive":
        share.allow_selects = body.allow_selects
    if body.watermark_previews is not None:
        share.watermark_previews = body.watermark_previews
    if body.watermark_name is not None:
        name = os.path.basename(body.watermark_name.strip())
        if name and not watermark_exists(name):
            raise HTTPException(status_code=400, detail="There is no watermark with that name.")
        share.watermark_name = name or None
    # A receiving portal shows nothing of the library, so it never offers
    # downloads, whatever is asked.
    if body.allow_download is not None and (share.kind or "send") != "receive":
        share.allow_download = body.allow_download
    if body.allow_zip is not None:
        share.allow_zip = body.allow_zip
    if body.ask_terms is not None:
        share.ask_terms = body.ask_terms
    # A link that never expires stays that way - "extend" must not impose a date.
    if body.extend_days and share.expires_at:
        base = max(share.expires_at, datetime.utcnow())
        share.expires_at = base + timedelta(days=body.extend_days)
    db.commit()
    return {"status": "ok", "id": share.id,
            "watermark_previews": bool(share.watermark_previews),
            "watermark_name": share.watermark_name,
            "allow_download": bool(share.allow_download),
            "allow_zip": True if share.allow_zip is None else bool(share.allow_zip),
            "expires_at": share.expires_at.isoformat() if share.expires_at else None}


def install(app):
    Base.metadata.create_all(bind=engine, tables=[ShareEvent.__table__])
    # Shares made before deliveries existed need the two new columns.
    for name, ddl in (("shoot_id", "INTEGER"),
                      ("watermark_previews", "BOOLEAN DEFAULT 0"),
                      ("watermark_name", "TEXT")):
        try:
            with engine.connect() as conn:
                cols = [r[1] for r in conn.execute(text("PRAGMA table_info(shares)"))]
                if name not in cols:
                    conn.execute(text(f"ALTER TABLE shares ADD COLUMN {name} {ddl}"))
                    conn.commit()
        except Exception as e:
            print(f"delivery: could not add shares.{name}: {e}", flush=True)
    app.include_router(router)
