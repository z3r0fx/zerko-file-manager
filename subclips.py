"""Sub-clips and searching the library by what was said.

Sub-clips: mark an in and an out on a clip, keep the range, and when wanted
render it out with ffmpeg. The range is the record; the rendered file is a
by-product that can be made again. A render lands in a "Clips" folder beside
the source and joins the library as an ordinary video, so it can be tagged,
shared on a portal or dragged into Resolve like anything else.

Two render modes, because they answer different questions:
  fast  - stream copy. Seconds even for 4K, but the cut snaps to the keyframe
          before the in point, so it can start up to a GOP early.
  exact - re-encode. Frame-accurate, slower, H.264 at high quality.

Spoken search: the transcripts already exist as timed segments. This matches
every word of the query (in any order, punctuation and case ignored) within a
segment and its neighbours, or an exact phrase in quotes, and returns the
moments - with enough about each clip to show it without a second request.
"""
import os
import queue
import re
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import (Column, DateTime, Float, ForeignKey, Integer, String,
                        or_)
from sqlalchemy.orm import Session

from auth import get_current_user, get_user_from_token
from database import (Base, IndexedFolder, SessionLocal, TranscriptionSegment,
                      User, Video, engine, get_db)
import permissions

router = APIRouter(tags=["subclips"])

_media_root: Optional[Path] = None
_resolve = lambda p: p          # noqa: E731
_ensure_folder_row = None

CLIPS_DIR_NAME = "Clips"


class SubClip(Base):
    __tablename__ = "subclips"

    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(Integer, ForeignKey("videos.id"), index=True, nullable=False)
    name = Column(String, nullable=True)
    start = Column(Float, nullable=False)      # seconds
    end = Column(Float, nullable=False)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    # rendering
    status = Column(String, default="marked")  # marked, queued, processing, done, failed
    mode = Column(String, nullable=True)       # fast / exact
    output_video_id = Column(Integer, nullable=True)   # the library row it became
    error = Column(String, nullable=True)
    rendered_at = Column(DateTime, nullable=True)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _tc(sec: float) -> str:
    sec = max(0.0, float(sec or 0))
    m, s = divmod(sec, 60)
    h, m = divmod(int(m), 60)
    return (f"{h}:{m:02d}:{s:05.2f}" if h else f"{m}:{s:05.2f}")


def _file_tc(sec: float) -> str:
    sec = int(max(0, sec or 0))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}{m:02d}{s:02d}" if h else f"{m:02d}{s:02d}"


def _as_dict(db: Session, c: SubClip) -> dict:
    out = None
    if c.output_video_id:
        out = db.query(Video).filter(Video.id == c.output_video_id).first()
        if out is not None and out.is_active is False:
            out = None
    return {
        "id": c.id, "video_id": c.video_id, "name": c.name,
        "start": c.start, "end": c.end, "duration": round(c.end - c.start, 3),
        "start_tc": _tc(c.start), "end_tc": _tc(c.end),
        "status": c.status if (c.status != "done" or out) else "marked",
        "mode": c.mode, "error": c.error,
        "output_video_id": out.id if out else None,
        "output_filename": out.filename if out else None,
        "created_by": c.created_by,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def _clean_range(v: Video, start: float, end: float):
    start = max(0.0, float(start))
    end = float(end)
    if v.duration:
        end = min(end, float(v.duration))
    if end - start < 0.1:
        raise HTTPException(status_code=400,
                            detail="The out point must be after the in point.")
    return round(start, 3), round(end, 3)


def _video_or_404(db: Session, video_id: int) -> Video:
    v = db.query(Video).filter(Video.id == video_id).first()
    if not v or v.is_active is False:
        raise HTTPException(status_code=404, detail="No such clip")
    if (v.media_type or "video") not in ("video", "audio"):
        raise HTTPException(status_code=400, detail="Only video and audio have a timeline.")
    return v


# --------------------------------------------------------------------------
# marking
# --------------------------------------------------------------------------

class NewSubClip(BaseModel):
    video_id: int
    start: float = Field(..., ge=0)
    end: float = Field(..., gt=0)
    name: Optional[str] = Field(None, max_length=200)


class EditSubClip(BaseModel):
    start: Optional[float] = Field(None, ge=0)
    end: Optional[float] = Field(None, gt=0)
    name: Optional[str] = Field(None, max_length=200)


@router.get("/api/subclips")
def list_subclips(video_id: Optional[int] = None, limit: int = 500,
                  db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    q = db.query(SubClip)
    if video_id is not None:
        q = q.filter(SubClip.video_id == video_id)
    rows = q.order_by(SubClip.video_id, SubClip.start).limit(max(1, min(limit, 2000))).all()
    return {"subclips": [_as_dict(db, c) for c in rows]}


@router.post("/api/subclips")
def create_subclip(body: NewSubClip, db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    v = _video_or_404(db, body.video_id)
    start, end = _clean_range(v, body.start, body.end)
    c = SubClip(video_id=v.id, start=start, end=end,
                name=(body.name or "").strip() or None,
                created_by=current_user.username)
    db.add(c)
    db.commit()
    db.refresh(c)
    return _as_dict(db, c)


@router.patch("/api/subclips/{clip_id}")
def edit_subclip(clip_id: int, body: EditSubClip, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    c = db.query(SubClip).filter(SubClip.id == clip_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="No such sub-clip")
    v = _video_or_404(db, c.video_id)
    if body.name is not None:
        c.name = body.name.strip() or None
    if body.start is not None or body.end is not None:
        start, end = _clean_range(v, body.start if body.start is not None else c.start,
                                  body.end if body.end is not None else c.end)
        if (start, end) != (c.start, c.end):
            c.start, c.end = start, end
            # The rendered file no longer matches the range. It stays in the
            # library (someone may be using it) but is no longer this clip's.
            c.output_video_id = None
            c.status = "marked"
    db.commit()
    return _as_dict(db, c)


@router.delete("/api/subclips/{clip_id}")
def delete_subclip(clip_id: int, db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    """Forgets the range. A rendered file is a library item in its own right
    and is left alone - trash it like any other clip if it is not wanted."""
    c = db.query(SubClip).filter(SubClip.id == clip_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="No such sub-clip")
    if c.status in ("queued", "processing"):
        raise HTTPException(status_code=409, detail="It is being rendered - try again in a moment.")
    db.delete(c)
    db.commit()
    return {"status": "ok"}


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

class ExportIn(BaseModel):
    mode: str = "fast"


_q: "queue.Queue" = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()


def _start_worker():
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
        threading.Thread(target=_worker, daemon=True, name="zerko-subclips").start()


def _worker():
    while True:
        clip_id = _q.get()
        try:
            _render(clip_id)
        except Exception as e:
            print(f"subclips: render {clip_id} crashed: {e}", flush=True)
            _set(clip_id, status="failed", error=str(e)[:1000])


def _set(clip_id: int, **fields):
    db = SessionLocal()
    try:
        c = db.query(SubClip).filter(SubClip.id == clip_id).first()
        if c:
            for k, v in fields.items():
                setattr(c, k, v)
            db.commit()
    finally:
        db.close()


def _output_path(src: Path, c: SubClip) -> Path:
    out_dir = src.parent / CLIPS_DIR_NAME
    base = re.sub(r"[^\w\-. ]+", "", (c.name or "").strip())
    base = re.sub(r"\s+", " ", base).strip()
    stem = f"{src.stem}_{_file_tc(c.start)}-{_file_tc(c.end)}" + (f"_{base}" if base else "")
    ext = src.suffix.lower() if src.suffix else ".mp4"
    if c.mode == "exact":
        ext = ".mp4"
    p = out_dir / f"{stem}{ext}"
    n = 2
    while p.exists():
        p = out_dir / f"{stem}_{n}{ext}"
        n += 1
    return p


def _ffmpeg_args(src: str, out: str, start: float, end: float, mode: str):
    ff = shutil.which("ffmpeg") or "ffmpeg"
    dur = f"{end - start:.3f}"
    if mode == "exact":
        # -ss after -i: decode from the start of the GOP and discard up to the
        # exact frame. Slower to seek, exact to the frame.
        return [ff, "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{max(0, start - 5):.3f}", "-i", src,
                "-ss", f"{min(start, 5):.3f}", "-t", dur,
                "-map", "0:v:0?", "-map", "0:a?",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "256k",
                "-map_metadata", "0", "-movflags", "+faststart", out]
    # Stream copy: only the picture and sound streams (camera timecode and
    # metadata tracks do not survive a cut and break some players), starting
    # on the keyframe at or before IN so the first frames are real pictures.
    return [ff, "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{start:.3f}", "-i", src, "-t", dur,
            "-map", "0:v:0?", "-map", "0:a?", "-c", "copy",
            "-avoid_negative_ts", "make_zero", "-map_metadata", "0", out]


def _render(clip_id: int):
    db = SessionLocal()
    try:
        c = db.query(SubClip).filter(SubClip.id == clip_id).first()
        if not c:
            return
        v = db.query(Video).filter(Video.id == c.video_id).first()
        src = _resolve(v.filepath) if v else None
        if not src or not os.path.exists(src):
            c.status, c.error = "failed", "The source file is not on the drive."
            db.commit()
            return
        c.status, c.error = "processing", None
        db.commit()
        out = _output_path(Path(src), c)
        out.parent.mkdir(parents=True, exist_ok=True)
        part = out.with_name(f".{out.stem}.part{out.suffix}")
        args = _ffmpeg_args(src, str(part), c.start, c.end, c.mode or "fast")
        # the container is chosen from the name, which ends .part.ext - say it
        args = args[:-1] + ["-f", _muxer_for(out.suffix), args[-1]]
        kw = {}
        if os.name == "posix":
            kw["preexec_fn"] = lambda: os.nice(5)
        r = subprocess.run(args, capture_output=True, timeout=6 * 3600, **kw)
        if r.returncode != 0 or not part.exists() or part.stat().st_size == 0:
            try:
                part.unlink()
            except OSError:
                pass
            c.status = "failed"
            c.error = (r.stderr or b"").decode("utf-8", "replace")[-800:] or "ffmpeg failed"
            db.commit()
            return
        os.replace(part, out)
        row = _register(db, v, out, c)
        c.output_video_id = row.id if row else None
        c.status = "done"
        c.rendered_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()


def _probe_duration(path: str) -> Optional[float]:
    try:
        r = subprocess.run([shutil.which("ffprobe") or "ffprobe", "-v", "error",
                            "-show_entries", "format=duration", "-of", "csv=p=0", path],
                           capture_output=True, text=True, timeout=60)
        return round(float(r.stdout.strip()), 3)
    except Exception:
        return None


def _muxer_for(ext: str) -> str:
    return {".mov": "mov", ".mp4": "mp4", ".m4v": "mp4", ".mkv": "matroska",
            ".mxf": "mxf", ".avi": "avi", ".mts": "mpegts", ".m2ts": "mpegts",
            ".ts": "mpegts", ".wav": "wav", ".mp3": "mp3", ".m4a": "mp4",
            ".aac": "adts", ".flac": "flac"}.get(ext.lower(), "mp4")


def _register(db: Session, src_row: Video, out: Path, c: SubClip):
    """Put the render into the library as an ordinary item."""
    try:
        folder = _ensure_folder_row(db, out.parent) if _ensure_folder_row else None
    except Exception as e:
        print(f"subclips: no folder row for {out.parent}: {e}", flush=True)
        folder = None
    row = db.query(Video).filter(Video.filepath == str(out)).first()
    if row is None:
        row = Video(filename=out.name, filepath=str(out))
        db.add(row)
    row.file_size = out.stat().st_size
    # A fast cut starts on the keyframe before the in point, so it can run
    # a little longer than the range; record what the file actually holds.
    row.duration = _probe_duration(str(out)) or round(c.end - c.start, 3)
    row.media_type = src_row.media_type or "video"
    row.folder_id = folder.id if folder else src_row.folder_id
    row.uploaded_by = c.created_by
    row.uploaded_at = datetime.utcnow()
    row.status = "raw"
    row.is_active = True
    for f in ("camera_make", "camera_model", "frame_rate", "resolution",
              "shoot_date", "rating"):
        setattr(row, f, getattr(src_row, f))
    if c.mode != "exact":
        row.video_codec = src_row.video_codec
        row.audio_codec = src_row.audio_codec
    # The words spoken in this range come with it, shifted to start at zero.
    segs = (db.query(TranscriptionSegment)
            .filter(TranscriptionSegment.video_id == src_row.id,
                    TranscriptionSegment.end_time > c.start,
                    TranscriptionSegment.start_time < c.end)
            .order_by(TranscriptionSegment.start_time).all())
    db.flush()
    if segs:
        db.query(TranscriptionSegment).filter(
            TranscriptionSegment.video_id == row.id).delete()
        for s in segs:
            db.add(TranscriptionSegment(
                video_id=row.id, text=s.text,
                start_time=round(max(0.0, s.start_time - c.start), 3),
                end_time=round(min(c.end, s.end_time) - c.start, 3)))
        row.transcription = " ".join((s.text or "").strip() for s in segs)
        row.transcription_status = "completed"
    elif src_row.transcription_status in ("no_audio", "not_applicable"):
        row.transcription_status = src_row.transcription_status
    # carry the tags across: a clip of the kitchen is still a clip of the kitchen
    try:
        for t in src_row.tags:
            if t not in row.tags:
                row.tags.append(t)
    except Exception:
        pass
    db.commit()
    db.refresh(row)
    # thumbnail, then a proxy through the normal queue
    try:
        import indexer
        name = indexer.thumb_name(str(out))
        tdir = (_media_root or out.parent) / "thumbnails"
        tdir.mkdir(parents=True, exist_ok=True)
        if indexer.make_thumbnail(str(out), str(tdir / name), row.media_type):
            row.thumbnail_path = f"/thumbnails/{name}"
            db.commit()
    except Exception as e:
        print(f"subclips: thumbnail failed for {out.name}: {e}", flush=True)
    if row.media_type == "video":
        try:
            from job_manager import job_manager
            job_manager.add_job(row.id, "proxy")
        except Exception as e:
            print(f"subclips: could not queue proxy for {out.name}: {e}", flush=True)
    return row


@router.post("/api/subclips/{clip_id}/export")
def export_subclip(clip_id: int, body: ExportIn, db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    mode = (body.mode or "fast").lower()
    if mode not in ("fast", "exact"):
        raise HTTPException(status_code=400, detail="mode is 'fast' or 'exact'")
    c = db.query(SubClip).filter(SubClip.id == clip_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="No such sub-clip")
    _video_or_404(db, c.video_id)
    if c.status in ("queued", "processing"):
        return _as_dict(db, c)
    c.status, c.mode, c.error = "queued", mode, None
    db.commit()
    _start_worker()
    _q.put(c.id)
    return _as_dict(db, c)


@router.get("/api/subclips/{clip_id}/file")
def subclip_file(clip_id: int, token: Optional[str] = None,
                 db: Session = Depends(get_db)):
    """The rendered file, for a plain <a href>. Carries its token in the query
    like the other browser-opened downloads, and checks the download right
    itself because the route rules only see the path."""
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")
    user = get_user_from_token(token, db)
    if not user or not permissions.can(user.role, permissions.DOWNLOAD):
        raise HTTPException(status_code=403, detail="Your account cannot download files.")
    c = db.query(SubClip).filter(SubClip.id == clip_id).first()
    if not c or not c.output_video_id:
        raise HTTPException(status_code=404, detail="Not rendered yet")
    v = db.query(Video).filter(Video.id == c.output_video_id).first()
    path = _resolve(v.filepath) if v else None
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="The rendered file is gone")
    return FileResponse(path, filename=v.filename, media_type="application/octet-stream")


# --------------------------------------------------------------------------
# spoken search
# --------------------------------------------------------------------------

_WORD = re.compile(r"[\w']+", re.UNICODE)


def _norm(text: str) -> str:
    return " ".join(w.lower() for w in _WORD.findall(text or ""))


def parse_query(q: str):
    """(phrases, words). Quoted parts are phrases; everything else is words."""
    phrases = [_norm(p) for p in re.findall(r'"([^"]+)"', q or "")]
    rest = re.sub(r'"[^"]*"', " ", q or "")
    words = [w for w in _norm(rest).split() if w]
    return [p for p in phrases if p], list(dict.fromkeys(words))


def spoken_search(db: Session, q: str, limit: int = 60, per_clip: int = 8,
                  video_ids=None):
    phrases, words = parse_query(q)
    terms = phrases + words
    if not terms:
        return []
    # Narrow in SQL on the rarest-looking term (the longest), then check the
    # rest in Python where punctuation and neighbours can be handled properly.
    anchor = max(terms, key=len)
    first = anchor.split()[0]
    cand = (db.query(TranscriptionSegment.video_id)
            .join(Video, Video.id == TranscriptionSegment.video_id)
            .filter(Video.is_active.isnot(False),
                    TranscriptionSegment.text.ilike(f"%{first}%")))
    if video_ids:
        cand = cand.filter(TranscriptionSegment.video_id.in_(list(video_ids)))
    vids = [r[0] for r in cand.distinct().limit(2000).all()]
    if not vids:
        return []

    results = []
    for vid in vids:
        segs = (db.query(TranscriptionSegment)
                .filter(TranscriptionSegment.video_id == vid)
                .order_by(TranscriptionSegment.start_time).all())
        normed = [_norm(s.text) for s in segs]
        hits = []
        for i, s in enumerate(segs):
            window = " ".join(normed[max(0, i - 1): i + 2])
            here = normed[i]
            padded_w = f" {window} "
            if not all(f" {t} " in padded_w for t in terms):
                continue
            # the segment itself must carry at least one term, or every
            # neighbour of a hit would be reported as a hit too
            if not any(f" {t} " in f" {here} " for t in terms):
                continue
            score = sum(1 for t in terms if f" {t} " in f" {here} ")
            hits.append({"start": s.start_time, "end": s.end_time,
                         "text": s.text, "score": score})
        if not hits:
            continue
        hits.sort(key=lambda h: (-h["score"], h["start"]))
        best = hits[:per_clip]
        best.sort(key=lambda h: h["start"])
        results.append((vid, len(hits), max(h["score"] for h in hits), best))

    results.sort(key=lambda r: (-r[2], -r[1]))
    results = results[:limit]
    rows = {v.id: v for v in db.query(Video).filter(
        Video.id.in_([r[0] for r in results])).all()}
    folders = {f.id: f for f in db.query(IndexedFolder).filter(
        IndexedFolder.id.in_([v.folder_id for v in rows.values() if v.folder_id])).all()}
    out = []
    for vid, n, score, best in results:
        v = rows.get(vid)
        if not v:
            continue
        f = folders.get(v.folder_id)
        out.append({
            "video_id": v.id, "filename": v.filename, "media_type": v.media_type,
            "duration": v.duration, "thumbnail_path": v.thumbnail_path,
            "folder_id": v.folder_id,
            "folder": (f.relative_path or f.name) if f else None,
            "match_count": n, "moments": best,
        })
    return out


@router.get("/api/search/spoken")
def spoken(q: str, limit: int = 60, db: Session = Depends(get_db),
           current_user: User = Depends(get_current_user)):
    q = (q or "").strip()
    if len(q) < 2:
        return {"query": q, "results": []}
    res = spoken_search(db, q, limit=max(1, min(limit, 200)))
    return {"query": q, "results": res,
            "moments": sum(len(r["moments"]) for r in res)}


# --------------------------------------------------------------------------

def install(app, media_root, resolve_media_path, ensure_folder_row=None):
    global _media_root, _resolve, _ensure_folder_row
    _media_root = Path(media_root) if media_root else None
    _resolve = resolve_media_path or (lambda p: p)
    _ensure_folder_row = ensure_folder_row
    Base.metadata.create_all(bind=engine, tables=[SubClip.__table__])
    # Renders that were running when the server stopped: the part file is
    # useless, the range is not. Put them back to marked.
    try:
        db = SessionLocal()
        db.query(SubClip).filter(SubClip.status.in_(["queued", "processing"])) \
          .update({SubClip.status: "marked"}, synchronize_session=False)
        db.commit()
        db.close()
    except Exception as e:
        print(f"subclips: could not reset stale renders: {e}", flush=True)
    app.include_router(router)
