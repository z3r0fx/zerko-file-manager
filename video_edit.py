"""The video editor: projects, rendering, and handing an edit to DaVinci Resolve.

A project is a JSON document (see Project below): one main track of clips
laid end to end, with a transition into the next where wanted; a music track
of pieces placed anywhere; titles on top; one grade for everything (a clip
can carry its own); and the audio settings - ducking under speech, EQ,
voice clarity and loudness for the social sites.

Nothing here writes into the library unless asked: a render goes to the
system temp folder and is handed over as a download, like cut exports.
"Save to library" puts it in a folder of your choosing instead.

Two outputs besides the video:
  * the timeline as FCPXML, which DaVinci Resolve imports (File > Import >
    Timeline) with every clip, trim, transition, music piece, volume and
    title where it was - the colour is left for Resolve;
  * a project pack: that timeline plus the footage it uses in one ZIP, for
    finishing on another machine.

The preview in the browser (web/src/video/) follows the same maths: the
timeline layout (layout()), framing (_rect()), and the grade (bake_lut(),
mirrored in web/src/video/grade.ts - change both).
"""
from __future__ import annotations

import base64
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote

import numpy as np
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.orm import Session

from auth import get_current_user, get_user_from_token
from database import Base, SessionLocal, TranscriptionSegment, User, Video, engine, get_db
import permissions

router = APIRouter(prefix="/api/video-projects", tags=["video-edit"])
tools = APIRouter(prefix="/api/video-edit", tags=["video-edit"])

_media_root: Optional[Path] = None
_resolve = lambda p: p          # noqa: E731
_ensure_folder_row = None

FF = lambda: shutil.which("ffmpeg") or "ffmpeg"       # noqa: E731
FP = lambda: shutil.which("ffprobe") or "ffprobe"     # noqa: E731


# --------------------------------------------------------------------------
# the project
# --------------------------------------------------------------------------

class VideoProject(Base):
    __tablename__ = "video_projects"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, default="Untitled edit")
    owner = Column(String, nullable=True, index=True)
    data = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class Frame(BaseModel):
    mode: str = "fill"          # fill (crop to the shape) | fit (whole picture on a blurred copy)
    x: float = 0.5              # where the crop sits, 0..1 of the spare width
    y: float = 0.5
    zoom: float = 1.0


class Grade(BaseModel):
    exposure: float = 0.0       # stops
    contrast: float = 0.0       # -1..1
    highlights: float = 0.0
    shadows: float = 0.0
    temp: float = 0.0           # -1 cool .. 1 warm
    tint: float = 0.0           # -1 green .. 1 magenta
    saturation: float = 0.0     # -1..1
    lut: str = ""               # a .cube in the LUT folder, applied first
    lut_mix: float = 1.0


class Clip(BaseModel):
    id: str
    video_id: int
    src_in: float = 0.0
    src_out: float = 1.0
    speed: float = 1.0
    volume: float = 1.0         # linear, 0..2
    mute: bool = False
    fade_in: float = 0.0        # sound
    fade_out: float = 0.0
    trans: str = "cut"          # into the next clip
    trans_dur: float = 0.5
    frame: Frame = Frame()
    grade: Optional[Grade] = None   # None = the project's


class Music(BaseModel):
    id: str
    video_id: int
    start: float = 0.0          # on the timeline
    src_in: float = 0.0
    dur: float = 10.0
    volume: float = 0.5
    fade_in: float = 0.0
    fade_out: float = 1.5


class Title(BaseModel):
    id: str
    text: str = ""
    start: float = 0.0
    dur: float = 3.0
    style: str = "title"
    x: float = 0.5
    y: float = 0.5
    size: float = 0.06
    color: str = "#ffffff"
    accent: str = "#000000"
    weight: int = 700
    align: str = "center"
    font: str = "Inter"
    fade: float = 0.3


class AudioOpts(BaseModel):
    duck: bool = True           # music drops while someone speaks
    duck_db: float = -12.0
    eq_low: float = 0.0         # dB, on the whole mix
    eq_mid: float = 0.0
    eq_high: float = 0.0
    voice: bool = False         # clarity on the clips' sound
    loudness: bool = True       # -14 LUFS, what Instagram and YouTube play at
    target: float = -14.0


class Project(BaseModel):
    aspect: str = "9:16"
    fps: float = 25.0
    fade_in: float = 0.0        # the whole edit, from and to black
    fade_out: float = 0.0
    clips: List[Clip] = []
    music: List[Music] = []
    titles: List[Title] = []
    audio: AudioOpts = AudioOpts()
    grade: Grade = Grade()


ASPECTS = {"9:16": (1080, 1920), "4:5": (1080, 1350), "1:1": (1080, 1080), "16:9": (1920, 1080)}
TRANSITIONS = {"cut", "fade", "fadeblack", "fadewhite", "wipeleft", "wiperight", "slideleft", "slideright", "zoomin"}


def clip_len(c: Clip) -> float:
    return max(0.04, (c.src_out - c.src_in) / max(0.1, c.speed))


def trans_after(p: Project, i: int) -> float:
    """How long clip i overlaps the next. Never more than half of either."""
    if i >= len(p.clips) - 1:
        return 0.0
    c = p.clips[i]
    if c.trans == "cut" or c.trans not in TRANSITIONS:
        return 0.0
    return max(0.0, min(c.trans_dur, clip_len(c) / 2, clip_len(p.clips[i + 1]) / 2))


def layout(p: Project):
    """[(start, length)] for every clip, and the edit's total length.
    Mirrored in web/src/video/model.ts layout()."""
    out, t = [], 0.0
    for i, c in enumerate(p.clips):
        L = clip_len(c)
        out.append((t, L))
        t += L - trans_after(p, i)
    total = (out[-1][0] + out[-1][1]) if out else 0.0
    return out, total


def _load(row: VideoProject) -> Project:
    try:
        return Project(**json.loads(row.data or "{}"))
    except Exception:
        return Project()


def _row_out(db: Session, row: VideoProject, full: bool = True) -> dict:
    p = _load(row)
    _, total = layout(p)
    out = {"id": row.id, "name": row.name, "owner": row.owner,
           "created_at": row.created_at.isoformat() + "Z" if row.created_at else None,
           "updated_at": row.updated_at.isoformat() + "Z" if row.updated_at else None,
           "aspect": p.aspect, "duration": round(total, 3), "clips": len(p.clips),
           "cover": p.clips[0].video_id if p.clips else None}
    if full:
        out["data"] = p.model_dump()
    return out


def _own(db: Session, pid: int, user: User) -> VideoProject:
    row = db.query(VideoProject).filter(VideoProject.id == pid).first()
    if not row:
        raise HTTPException(status_code=404, detail="That edit no longer exists")
    return row


def _fresh_id() -> str:
    return uuid.uuid4().hex[:10]


class NewProject(BaseModel):
    name: str = Field("Untitled edit", max_length=160)
    aspect: str = "9:16"
    video_ids: List[int] = []
    subclip_ids: List[int] = []


@router.get("")
def list_projects(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    rows = db.query(VideoProject).order_by(VideoProject.updated_at.desc()).all()
    return [_row_out(db, r, full=False) for r in rows]


@router.post("")
def create_project(body: NewProject, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    p = Project(aspect=body.aspect if body.aspect in ASPECTS else "9:16")
    fps_set = False
    wanted = []
    if body.subclip_ids:
        from subclips import SubClip
        rows = {r.id: r for r in db.query(SubClip).filter(SubClip.id.in_(body.subclip_ids)).all()}
        wanted += [(rows[i].video_id, rows[i].start, rows[i].end) for i in body.subclip_ids if i in rows]
    for vid in body.video_ids:
        wanted.append((vid, None, None))
    for vid, a, b in wanted:
        v = db.query(Video).filter(Video.id == vid).first()
        if not v or (v.media_type or "video") not in ("video", "audio"):
            continue
        info = probe_video(v)
        if v.media_type == "audio":
            p.music.append(Music(id=_fresh_id(), video_id=v.id, start=0.0, dur=float(info.get("duration") or 10)))
            continue
        if not fps_set and info.get("fps"):
            p.fps, fps_set = float(info["fps"]), True
        dur = float(info.get("duration") or v.duration or 5)
        p.clips.append(Clip(id=_fresh_id(), video_id=v.id, src_in=a or 0.0, src_out=b if b else dur))
    row = VideoProject(name=(body.name or "Untitled edit").strip()[:160] or "Untitled edit",
                       owner=current_user.username, data=p.model_dump_json(),
                       created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.add(row)
    db.commit()
    db.refresh(row)
    return _row_out(db, row)


@router.get("/{pid}")
def get_project(pid: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return _row_out(db, _own(db, pid, current_user))


class SaveProject(BaseModel):
    name: Optional[str] = Field(None, max_length=160)
    data: Optional[Project] = None


@router.put("/{pid}")
def save_project(pid: int, body: SaveProject, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = _own(db, pid, current_user)
    if body.name is not None and body.name.strip():
        row.name = body.name.strip()[:160]
    if body.data is not None:
        row.data = body.data.model_dump_json()
    row.updated_at = datetime.utcnow()
    db.commit()
    return _row_out(db, row, full=False)


@router.delete("/{pid}")
def delete_project(pid: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = _own(db, pid, current_user)
    db.delete(row)
    db.commit()
    return {"status": "ok"}


@router.post("/{pid}/duplicate")
def duplicate_project(pid: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = _own(db, pid, current_user)
    new = VideoProject(name=f"{row.name} copy"[:160], owner=current_user.username, data=row.data,
                       created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.add(new)
    db.commit()
    db.refresh(new)
    return _row_out(db, new)


# --------------------------------------------------------------------------
# what the timeline needs to draw: sizes, frames, sound, speech
# --------------------------------------------------------------------------

_PROBE: Dict[tuple, dict] = {}


def _cache_dir(name: str) -> Path:
    d = Path(tempfile.gettempdir()) / "zerko-video" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _src(v: Video) -> str:
    p = _resolve(v.filepath) if v else None
    if not p or not os.path.exists(p):
        raise HTTPException(status_code=404, detail=f"{v.filename if v else 'The clip'} is not on the drive")
    return p


def _fps(s: str) -> float:
    try:
        a, b = (s or "0/1").split("/")
        return float(a) / float(b) if float(b) else 0.0
    except Exception:
        try:
            return float(s)
        except Exception:
            return 0.0


def probe_path(path: str) -> dict:
    try:
        key = (path, os.stat(path).st_mtime_ns)
    except OSError:
        return {}
    if key in _PROBE:
        return _PROBE[key]
    info = {"duration": 0.0, "fps": 0.0, "width": 0, "height": 0, "has_audio": False, "has_video": False}
    try:
        r = subprocess.run([FP(), "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
                           capture_output=True, text=True, timeout=60)
        j = json.loads(r.stdout or "{}")
        info["duration"] = float((j.get("format") or {}).get("duration") or 0)
        ftc = ((j.get("format") or {}).get("tags") or {}).get("timecode")
        if ftc:
            info["timecode"] = ftc
        for s in j.get("streams", []):
            if s.get("codec_type") == "video" and not info["has_video"] and \
                    (s.get("disposition") or {}).get("attached_pic") != 1:
                info["has_video"] = True
                w, h = int(s.get("width") or 0), int(s.get("height") or 0)
                rot = 0
                try:
                    rot = int((s.get("tags") or {}).get("rotate") or 0)
                except Exception:
                    pass
                for sd in s.get("side_data_list") or []:
                    if "rotation" in sd:
                        try:
                            rot = int(sd["rotation"])
                        except Exception:
                            pass
                if abs(rot) % 180 == 90:
                    w, h = h, w
                info["width"], info["height"] = w, h
                info["fps"] = round(_fps(s.get("avg_frame_rate")) or _fps(s.get("r_frame_rate")), 3)
                info["pix_fmt"] = s.get("pix_fmt")
                info["codec"] = s.get("codec_name")
            if s.get("codec_type") == "audio":
                info["has_audio"] = True
            tc = (s.get("tags") or {}).get("timecode")
            if tc and not info.get("timecode"):
                info["timecode"] = tc
    except Exception as e:
        print(f"[video-edit] probe failed for {path}: {e}", flush=True)
    _PROBE[key] = info
    return info


def probe_video(v: Video) -> dict:
    try:
        return probe_path(_src(v))
    except HTTPException:
        return {}


def _video(db: Session, vid: int) -> Video:
    v = db.query(Video).filter(Video.id == vid).first()
    if not v or v.is_active is False:
        raise HTTPException(status_code=404, detail="No such clip")
    return v


@tools.get("/probe/{vid}")
def probe(vid: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    v = _video(db, vid)
    return {"id": v.id, "filename": v.filename, "media_type": v.media_type, **probe_video(v)}


def _auth_q(request: Request, token: Optional[str], db: Session) -> User:
    if token:
        u = get_user_from_token(token, db, check_session=False)
    else:
        a = request.headers.get("Authorization") or ""
        if not a.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing token")
        u = get_user_from_token(a[7:], db)
    if not u:
        raise HTTPException(status_code=401, detail="Not signed in")
    return u


@tools.get("/frame/{vid}")
def frame(vid: int, request: Request, t: float = 0.0, w: int = 160, token: Optional[str] = None,
          db: Session = Depends(get_db)):
    """One frame, small - the pictures along a clip on the timeline."""
    _auth_q(request, token, db)
    v = _video(db, vid)
    src = _resolve(v.proxy_path) if v.proxy_path and os.path.exists(_resolve(v.proxy_path) or "") else _src(v)
    w = max(32, min(640, int(w)))
    t = max(0.0, round(float(t), 1))
    out = _cache_dir("frames") / f"{vid}_{os.stat(src).st_mtime_ns % 10**9}_{t:.1f}_{w}.jpg"
    if not out.exists():
        subprocess.run([FF(), "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", src, "-frames:v", "1",
                        "-vf", f"scale={w}:-2", "-q:v", "5", str(out)], capture_output=True, timeout=60)
        if not out.exists():
            subprocess.run([FF(), "-v", "error", "-y", "-sseof", "-0.2", "-i", src, "-frames:v", "1",
                            "-vf", f"scale={w}:-2", "-q:v", "5", str(out)], capture_output=True, timeout=60)
    if not out.exists():
        raise HTTPException(status_code=404, detail="No picture there")
    return FileResponse(str(out), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@tools.get("/peaks/{vid}")
def peaks(vid: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """The sound's outline: 50 values a second, 0..255 - for waveforms."""
    v = _video(db, vid)
    src = _src(v)
    out = _cache_dir("peaks") / f"{vid}_{os.stat(src).st_mtime_ns % 10**9}.json"
    if out.exists():
        return Response(out.read_text(), media_type="application/json")
    rate = 50
    r = subprocess.run([FF(), "-v", "error", "-i", src, "-vn", "-ac", "1", "-ar", "4000", "-f", "s16le", "-"],
                       capture_output=True, timeout=600)
    a = np.frombuffer(r.stdout or b"", dtype=np.int16).astype(np.float32) / 32768.0
    step = 4000 // rate
    n = len(a) // step
    if n:
        blk = np.abs(a[: n * step]).reshape(n, step).max(axis=1)
        # a little lift for quiet material, so speech is visible next to music
        vals = np.clip(np.sqrt(blk) * 255, 0, 255).astype(np.uint8)
    else:
        vals = np.zeros(0, np.uint8)
    body = json.dumps({"rate": rate, "peaks": base64.b64encode(vals.tobytes()).decode()})
    out.write_text(body)
    return Response(body, media_type="application/json")


@tools.get("/speech/{vid}")
def speech(vid: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """When someone is talking in the clip (from its transcript) - the music ducks there."""
    segs = (db.query(TranscriptionSegment).filter(TranscriptionSegment.video_id == vid)
            .order_by(TranscriptionSegment.start_time).all())
    out = []
    for s in segs:
        if s.start_time is None or s.end_time is None or not (s.text or "").strip():
            continue
        if out and s.start_time - out[-1][1] < 0.8:
            out[-1][1] = max(out[-1][1], float(s.end_time))
        else:
            out.append([float(s.start_time), float(s.end_time)])
    return {"ranges": out, "segments": [{"start": s.start_time, "end": s.end_time, "text": s.text}
                                         for s in segs if (s.text or "").strip()]}


# --------------------------------------------------------------------------
# LUTs
# --------------------------------------------------------------------------

def lut_dir() -> Path:
    d = (_media_root or Path(".")) / "_luts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _lut_path(name: str) -> Optional[Path]:
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    p = lut_dir() / name
    return p if p.is_file() else None


_CUBES: Dict[tuple, tuple] = {}


def read_cube(path: Path):
    """(size, table[b][g][r][3], domain_min, domain_max)"""
    key = (str(path), path.stat().st_mtime_ns)
    if key in _CUBES:
        return _CUBES[key]
    size, rows, dmin, dmax = 0, [], [0.0, 0.0, 0.0], [1.0, 1.0, 1.0]
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        up = line.upper()
        if up.startswith("LUT_3D_SIZE"):
            size = int(line.split()[1])
        elif up.startswith("DOMAIN_MIN"):
            dmin = [float(x) for x in line.split()[1:4]]
        elif up.startswith("DOMAIN_MAX"):
            dmax = [float(x) for x in line.split()[1:4]]
        elif up[0].isalpha():
            continue
        else:
            parts = line.split()
            if len(parts) >= 3:
                rows.append([float(parts[0]), float(parts[1]), float(parts[2])])
    if not size or len(rows) < size ** 3:
        raise HTTPException(status_code=400, detail=f"{path.name} is not a 3D .cube LUT")
    t = np.array(rows[: size ** 3], np.float32).reshape(size, size, size, 3)  # [b][g][r]
    _CUBES[key] = (size, t, np.array(dmin, np.float32), np.array(dmax, np.float32))
    return _CUBES[key]


def apply_cube(rgb: np.ndarray, cube) -> np.ndarray:
    size, t, dmin, dmax = cube
    x = np.clip((rgb - dmin) / np.maximum(dmax - dmin, 1e-6), 0, 1) * (size - 1)
    i0 = np.floor(x).astype(np.int32)
    i0 = np.clip(i0, 0, size - 2)
    f = x - i0
    r0, g0, b0 = i0[..., 0], i0[..., 1], i0[..., 2]
    fr, fg, fb = f[..., 0:1], f[..., 1:2], f[..., 2:3]
    out = np.zeros_like(rgb)
    for db_ in (0, 1):
        for dg in (0, 1):
            for dr in (0, 1):
                w = (fr if dr else 1 - fr) * (fg if dg else 1 - fg) * (fb if db_ else 1 - fb)
                out += t[b0 + db_, g0 + dg, r0 + dr] * w
    return out


LUMA = np.array([0.2126, 0.7152, 0.0722], np.float32)


def grade_rgb(c: np.ndarray, g: Grade) -> np.ndarray:
    """The grade, on 0..1 display RGB. Mirrored in web/src/video/grade.ts."""
    if g.lut:
        p = _lut_path(g.lut)
        if p:
            try:
                looked = apply_cube(c, read_cube(p))
                c = c + (looked - c) * float(g.lut_mix)
            except HTTPException:
                pass
    c = np.clip(c, 0, 1)
    if g.exposure:
        c = np.power(c, 2.2) * (2.0 ** g.exposure)
        c = np.power(np.clip(c, 0, None), 1 / 2.2)
    if g.temp or g.tint:
        c = c * np.array([1 + 0.12 * g.temp, 1 - 0.1 * g.tint, 1 - 0.12 * g.temp], np.float32)
    if g.shadows or g.highlights:
        L = np.clip(c @ LUMA, 1e-4, None)[..., None]
        Lc = np.clip(L, 0, 1)
        n = Lc + g.shadows * 0.6 * Lc * (1 - Lc) ** 2 + g.highlights * 0.6 * Lc ** 2 * (1 - Lc)
        c = c * (n / L)
    if g.contrast:
        x = np.clip(c, 0, 1)
        c = x + g.contrast * 1.2 * x * (1 - x) * (2 * x - 1)
    if g.saturation:
        L = (c @ LUMA)[..., None]
        c = L + (c - L) * (1 + g.saturation)
    return np.clip(c, 0, 1).astype(np.float32)


def grade_is_flat(g: Grade) -> bool:
    return not (g.exposure or g.contrast or g.highlights or g.shadows or g.temp or g.tint or g.saturation
                or (g.lut and _lut_path(g.lut) and g.lut_mix > 0))


def bake_lut(g: Grade, out: Path, size: int = 33) -> Path:
    r = np.linspace(0, 1, size, dtype=np.float32)
    B, G, R = np.meshgrid(r, r, r, indexing="ij")
    grid = np.stack([R, G, B], -1).reshape(-1, 3)
    res = grade_rgb(grid, g)
    lines = ["TITLE \"Zerko grade\"", f"LUT_3D_SIZE {size}"]
    lines += [f"{a:.6f} {b:.6f} {c:.6f}" for a, b, c in res]
    out.write_text("\n".join(lines) + "\n")
    return out


@tools.get("/luts")
def list_luts(current_user: User = Depends(get_current_user)):
    return {"luts": sorted([p.name for p in lut_dir().glob("*.cube")], key=str.lower), "folder": str(lut_dir())}


@tools.get("/luts/{name}")
def get_lut(name: str, current_user: User = Depends(get_current_user)):
    p = _lut_path(name)
    if not p:
        raise HTTPException(status_code=404, detail="No such LUT")
    size, t, dmin, dmax = read_cube(p)
    return {"name": name, "size": size, "min": dmin.tolist(), "max": dmax.tolist(),
            "data": base64.b64encode(t.astype(np.float32).tobytes()).decode()}


@tools.post("/luts")
async def upload_lut(file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "", Path(file.filename or "").name).strip()
    if not name.lower().endswith(".cube"):
        raise HTTPException(status_code=400, detail="A LUT is a .cube file")
    data = await file.read()
    if len(data) > 64 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="That LUT is too big")
    p = lut_dir() / name
    p.write_bytes(data)
    try:
        read_cube(p)
    except HTTPException:
        p.unlink(missing_ok=True)
        raise
    return {"name": name}


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def _even(x: float) -> int:
    return max(2, int(round(x / 2)) * 2)


def _rect(sw: int, sh: int, W: int, H: int, fr: Frame):
    """Where the picture sits in the frame: (scaled w, h, x, y) in pixels.
    Mirrored in web/src/video/model.ts frameRect()."""
    z = max(0.2, min(4.0, fr.zoom or 1.0))
    if fr.mode == "fit":
        s = min(W / sw, H / sh) * z
    else:
        s = max(W / sw, H / sh) * z
    dw, dh = sw * s, sh * s
    if fr.mode == "fit":
        x, y = (W - dw) / 2, (H - dh) / 2
    else:
        x, y = -(dw - W) * fr.x, -(dh - H) * fr.y
    return dw, dh, x, y


def _speed_chain(speed: float) -> str:
    """atempo only takes 0.5..2 in older ffmpeg: chain it."""
    parts, s = [], speed
    while s > 2.0:
        parts.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        parts.append("atempo=0.5")
        s /= 0.5
    if abs(s - 1.0) > 1e-3:
        parts.append(f"atempo={s:.5f}")
    return ",".join(parts)


def _duck_expr(ranges, duck_db: float, ramp: float = 0.35) -> str:
    """volume expression for the music: full, dipping to duck_db over each range."""
    if not ranges:
        return "1"
    k = 10 ** (duck_db / 20.0)
    terms = [f"clip((t-{a - ramp:.3f})/{ramp:.3f},0,1)*clip(({b + ramp:.3f}-t)/{ramp:.3f},0,1)" for a, b in ranges]
    m = terms[0]
    for tt in terms[1:]:
        m = f"max({m},{tt})"
    return f"1-{1 - k:.4f}*{m}"


def speech_on_timeline(db: Session, p: Project) -> list:
    """Speech in the clips, where it lands on the timeline (muted clips excluded)."""
    lay, _ = layout(p)
    out = []
    cache = {}
    for c, (start, L) in zip(p.clips, lay):
        if c.mute or c.volume <= 0.02:
            continue
        if c.video_id not in cache:
            cache[c.video_id] = speech(c.video_id, db, None)["ranges"]
        for a, b in cache[c.video_id]:
            a2, b2 = max(a, c.src_in), min(b, c.src_out)
            if b2 > a2:
                out.append([start + (a2 - c.src_in) / c.speed, start + (b2 - c.src_in) / c.speed])
    out.sort()
    merged = []
    for a, b in out:
        if merged and a - merged[-1][1] < 0.8:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def build_render(db: Session, p: Project, W: int, H: int, crf: int, titles: Dict[str, Path],
                 work: Path, out: Path, preset: str = "medium"):
    """The whole ffmpeg command for the edit, and its length in seconds."""
    if not p.clips:
        raise HTTPException(status_code=400, detail="The edit has no clips yet")
    lay, total = layout(p)
    fps = p.fps if p.fps and p.fps > 1 else 25.0
    args = [FF(), "-y", "-hide_banner", "-v", "error", "-progress", "pipe:1", "-nostats"]
    fg: List[str] = []
    n_in = 0
    vids = {}
    for c in p.clips:
        if c.video_id not in vids:
            vids[c.video_id] = _video(db, c.video_id)

    vlabels, alabels = [], []
    for i, (c, (start, L)) in enumerate(zip(p.clips, lay)):
        v = vids[c.video_id]
        src = _src(v)
        info = probe_path(src)
        sw, sh = info.get("width") or W, info.get("height") or H
        span = c.src_out - c.src_in
        args += ["-ss", f"{max(0.0, c.src_in):.3f}", "-t", f"{span + 0.2:.3f}", "-i", src]
        k = n_in
        n_in += 1
        # picture
        chain = [f"[{k}:v]setpts=PTS-STARTPTS"]
        if abs(c.speed - 1) > 1e-3:
            chain.append(f"setpts=PTS/{c.speed:.5f}")
        chain.append(f"fps={fps:.5f}")
        # a clip that runs out early holds its last frame, so the timing never slips
        chain.append(f"tpad=stop_mode=clone:stop_duration=2,trim=duration={L:.4f}")
        g = c.grade or p.grade
        dw, dh, x, y = _rect(sw, sh, W, H, c.frame)
        sws, shs = _even(dw), _even(dh)
        if c.frame.mode == "fit":
            bw, bh, bx, by = _rect(sw, sh, W, H, Frame(mode="fill"))
            fg.append(",".join(chain) + f",split=2[f{i}a][f{i}b]")
            fg.append(f"[f{i}a]scale={_even(bw / 8)}:{_even(bh / 8)},crop={_even(W / 8)}:{_even(H / 8)},"
                      f"boxblur=10:2,scale={W}:{H},eq=brightness=-0.06[bg{i}]")
            fg.append(f"[f{i}b]scale={sws}:{shs}[fgp{i}]")
            fg.append(f"[bg{i}][fgp{i}]overlay=x={int(round(x))}:y={int(round(y))}:shortest=1,setsar=1[pic{i}]")
        else:
            cx = int(round(min(max(0, -x), sws - W)))
            cy = int(round(min(max(0, -y), shs - H)))
            if sws < W or shs < H:      # zoomed out past the edges: black around it
                fg.append(",".join(chain) + f",scale={sws}:{shs}[fgp{i}]")
                fg.append(f"color=c=black:s={W}x{H}:r={fps:.5f}:d={L + 0.5:.4f}[bk{i}]")
                fg.append(f"[bk{i}][fgp{i}]overlay=x={int(round(x))}:y={int(round(y))}:shortest=1,setsar=1[pic{i}]")
            else:
                fg.append(",".join(chain) + f",scale={sws}:{shs},crop={W}:{H}:{cx}:{cy},setsar=1[pic{i}]")
        lab = f"pic{i}"
        if not grade_is_flat(g):
            lut = bake_lut(g, work / f"grade_{i}.cube")
            lp = str(lut).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
            fg.append(f"[{lab}]scale=in_range=tv:in_color_matrix=bt709:out_range=pc,format=rgb48le,"
                      f"lut3d=file='{lp}',scale=in_range=pc:out_range=tv:out_color_matrix=bt709,format=yuv420p[gr{i}]")
            lab = f"gr{i}"
        fg.append(f"[{lab}]format=yuv420p,settb=1/{int(round(fps * 1000))}[v{i}]")
        vlabels.append(f"v{i}")
        # sound
        if info.get("has_audio") and not c.mute and c.volume > 0.001:
            ach = [f"[{k}:a]asetpts=PTS-STARTPTS", "aformat=sample_rates=48000:channel_layouts=stereo"]
            sp = _speed_chain(c.speed)
            if sp:
                ach.append(sp)
            ach.append(f"apad=whole_dur={L:.4f},atrim=duration={L:.4f}")
            if abs(c.volume - 1) > 1e-3:
                ach.append(f"volume={c.volume:.4f}")
            if c.fade_in > 0:
                ach.append(f"afade=t=in:st=0:d={min(c.fade_in, L):.3f}")
            if c.fade_out > 0:
                fo = min(c.fade_out, L)
                ach.append(f"afade=t=out:st={L - fo:.3f}:d={fo:.3f}")
            fg.append(",".join(ach) + f"[a{i}]")
        else:
            fg.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={L:.4f}[a{i}]")
        alabels.append(f"a{i}")

    # join the clips: a cut is a concat, a transition an xfade / acrossfade
    cv, ca, cur = vlabels[0], alabels[0], lay[0][1]
    for i in range(1, len(p.clips)):
        d = trans_after(p, i - 1)
        nv, na = f"jv{i}", f"ja{i}"
        if d > 0:
            fg.append(f"[{cv}][{vlabels[i]}]xfade=transition={p.clips[i - 1].trans}:duration={d:.4f}:"
                      f"offset={cur - d:.4f}[{nv}]")
            fg.append(f"[{ca}][{alabels[i]}]acrossfade=d={d:.4f}:c1=tri:c2=tri[{na}]")
        else:
            fg.append(f"[{cv}][{vlabels[i]}]concat=n=2:v=1:a=0[{nv}]")
            fg.append(f"[{ca}][{alabels[i]}]concat=n=2:v=0:a=1[{na}]")
        cv, ca = nv, na
        cur += lay[i][1] - d

    # titles: full-frame PNGs drawn by the browser, faded in and out
    for tt in p.titles:
        png = titles.get(tt.id)
        if not png or tt.dur <= 0 or tt.start >= total:
            continue
        dur = min(tt.dur, total - tt.start)
        args += ["-loop", "1", "-framerate", f"{fps:.5f}", "-t", f"{dur:.3f}", "-i", str(png)]
        k = n_in
        n_in += 1
        f = min(tt.fade, dur / 2)
        ch = f"[{k}:v]format=rgba"
        if f > 0:
            ch += f",fade=t=in:st=0:d={f:.3f}:alpha=1,fade=t=out:st={dur - f:.3f}:d={f:.3f}:alpha=1"
        ch += f",setpts=PTS-STARTPTS+{tt.start:.4f}/TB[t_{k}]"
        fg.append(ch)
        nv = f"tv{k}"
        fg.append(f"[{cv}][t_{k}]overlay=0:0:eof_action=pass:enable='between(t,{tt.start:.3f},{tt.start + dur:.3f})'[{nv}]")
        cv = nv

    # the whole edit from / to black
    vpost = []
    if p.fade_in > 0:
        vpost.append(f"fade=t=in:st=0:d={min(p.fade_in, total / 2):.3f}")
    if p.fade_out > 0:
        fo = min(p.fade_out, total / 2)
        vpost.append(f"fade=t=out:st={total - fo:.3f}:d={fo:.3f}")
    vpost.append("format=yuv420p")
    fg.append(f"[{cv}]" + ",".join(vpost) + "[vout]")

    # clips' sound: voice clarity
    if p.audio.voice:
        fg.append(f"[{ca}]highpass=f=80,equalizer=f=250:t=q:w=1:g=-2,equalizer=f=3200:t=q:w=1.2:g=3,"
                  f"acompressor=threshold=0.125:ratio=3:attack=10:release=150:makeup=1.4[cl]")
        ca = "cl"

    # music, placed, faded, summed, ducked under speech
    mus = []
    for m in p.music:
        if m.dur <= 0.05 or m.start >= total:
            continue
        mv = _video(db, m.video_id)
        msrc = _src(mv)
        dur = min(m.dur, total - m.start)
        args += ["-ss", f"{max(0.0, m.src_in):.3f}", "-t", f"{dur + 0.1:.3f}", "-i", msrc]
        k = n_in
        n_in += 1
        ch = [f"[{k}:a]asetpts=PTS-STARTPTS", "aformat=sample_rates=48000:channel_layouts=stereo",
              f"atrim=duration={dur:.4f}", f"volume={max(0.0, m.volume):.4f}"]
        if m.fade_in > 0:
            ch.append(f"afade=t=in:st=0:d={min(m.fade_in, dur):.3f}")
        if m.fade_out > 0:
            fo = min(m.fade_out, dur)
            ch.append(f"afade=t=out:st={dur - fo:.3f}:d={fo:.3f}")
        ms = int(round(m.start * 1000))
        if ms > 0:
            ch.append(f"adelay={ms}|{ms}")
        ch.append(f"apad=whole_dur={total:.4f},atrim=duration={total:.4f}")
        fg.append(",".join(ch) + f"[m{k}]")
        mus.append(f"m{k}")
    if mus:
        if len(mus) > 1:
            fg.append("".join(f"[{x}]" for x in mus) +
                      f"amix=inputs={len(mus)}:duration=longest:dropout_transition=0,volume={len(mus)}[mbus]")
        else:
            fg.append(f"[{mus[0]}]anull[mbus]")
        mb = "mbus"
        if p.audio.duck:
            ranges = speech_on_timeline(db, p)
            if ranges:
                fg.append(f"[mbus]volume='{_duck_expr(ranges, p.audio.duck_db)}':eval=frame[mduck]")
                mb = "mduck"
        fg.append(f"[{ca}][{mb}]amix=inputs=2:duration=first:dropout_transition=0,volume=2[mix]")
        ca = "mix"

    post = []
    if p.audio.eq_low:
        post.append(f"bass=g={p.audio.eq_low:.2f}:f=120")
    if p.audio.eq_mid:
        post.append(f"equalizer=f=1000:t=q:w=0.9:g={p.audio.eq_mid:.2f}")
    if p.audio.eq_high:
        post.append(f"treble=g={p.audio.eq_high:.2f}:f=8000")
    if p.audio.loudness:
        post.append(f"loudnorm=I={max(-30.0, min(-8.0, p.audio.target)):.1f}:TP=-1.5:LRA=11")
    if p.fade_in > 0:
        post.append(f"afade=t=in:st=0:d={min(p.fade_in, total / 2):.3f}")
    if p.fade_out > 0:
        fo = min(p.fade_out, total / 2)
        post.append(f"afade=t=out:st={total - fo:.3f}:d={fo:.3f}")
    post.append("aresample=48000")
    fg.append(f"[{ca}]" + ",".join(post) + "[aout]")

    script = work / "graph.txt"
    script.write_text(";\n".join(fg))
    args += ["-filter_complex_script", str(script), "-map", "[vout]", "-map", "[aout]",
             "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
             "-profile:v", "high", "-r", f"{fps:.5f}",
             "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
             "-c:a", "aac", "-b:a", "320k", "-ar", "48000",
             "-t", f"{total:.4f}", "-movflags", "+faststart", str(out)]
    return args, total


RENDERS: Dict[str, dict] = {}
_rlock = threading.Lock()
_TTL = 3 * 3600


def _sweep():
    now = time.time()
    with _rlock:
        for k in [k for k, j in RENDERS.items() if now - j["t"] > _TTL and j["status"] != "running"]:
            RENDERS.pop(k, None)
    d = _cache_dir("renders")
    for x in d.iterdir():
        try:
            if now - x.stat().st_mtime > _TTL:
                shutil.rmtree(x, ignore_errors=True) if x.is_dir() else x.unlink()
        except OSError:
            pass


class TitlePng(BaseModel):
    id: str
    png: str        # data URL or base64


class RenderIn(BaseModel):
    size: int = 1080            # the short side: 720, 1080, 2160
    quality: str = "high"       # high | small
    titles: List[TitlePng] = []
    filename: Optional[str] = Field(None, max_length=160)
    save_folder_id: Optional[int] = None   # set: keep it in the library too
    data: Optional[Project] = None         # unsaved changes, rendered as they are


def _safe_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "", s or "").strip(" .")
    return re.sub(r"\s+", " ", s)[:150] or "Edit"


def _run_render(job_id: str, args: list, total: float):
    job = RENDERS[job_id]
    kw = {}
    if os.name == "posix":
        kw["preexec_fn"] = lambda: os.nice(5)
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kw)
        with _rlock:
            job["pid"] = proc.pid
        for line in proc.stdout:
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    us = int(line.split("=")[1])
                    with _rlock:
                        job["progress"] = max(0.0, min(0.999, us / 1e6 / max(total, 0.01)))
                except ValueError:
                    pass
            if job.get("cancel"):
                proc.kill()
        err = proc.stderr.read() if proc.stderr else ""
        rc = proc.wait()
        out = Path(job["file"])
        if job.get("cancel"):
            with _rlock:
                job["status"], job["error"] = "cancelled", "Stopped"
            return
        if rc != 0 or not out.exists() or out.stat().st_size == 0:
            with _rlock:
                job["status"], job["error"] = "failed", (err or "ffmpeg failed")[-900:]
            return
        if job.get("save_folder_id"):
            try:
                job["saved_id"] = _save_to_library(job["save_folder_id"], out, job["user"])
            except Exception as e:
                job["save_error"] = str(e)[:300]
        with _rlock:
            job["status"], job["progress"] = "done", 1.0
    except Exception as e:
        with _rlock:
            job["status"], job["error"] = "failed", str(e)[:900]


def _save_to_library(folder_id: int, out: Path, user: str) -> Optional[int]:
    from database import IndexedFolder
    db = SessionLocal()
    try:
        folder = db.query(IndexedFolder).filter(IndexedFolder.id == folder_id).first()
        dest_dir = Path(_resolve(folder.path)) if folder and folder.path else None
        if not dest_dir or not dest_dir.is_dir():
            raise RuntimeError("That folder is not on the drive")
        dest = dest_dir / out.name
        n = 2
        while dest.exists():
            dest = dest_dir / f"{out.stem}_{n}{out.suffix}"
            n += 1
        shutil.copy2(out, dest)
        row = Video(filename=dest.name, filepath=str(dest), file_size=dest.stat().st_size,
                    media_type="video", folder_id=folder.id, uploaded_by=user,
                    uploaded_at=datetime.utcnow(), status="edited", is_active=True,
                    duration=probe_path(str(dest)).get("duration"))
        db.add(row)
        db.commit()
        db.refresh(row)
        try:
            import indexer
            name = indexer.thumb_name(str(dest))
            tdir = (_media_root or dest.parent) / "thumbnails"
            tdir.mkdir(parents=True, exist_ok=True)
            if indexer.make_thumbnail(str(dest), str(tdir / name), "video"):
                row.thumbnail_path = f"/thumbnails/{name}"
                db.commit()
        except Exception as e:
            print(f"[video-edit] thumbnail failed for {dest.name}: {e}", flush=True)
        try:
            from job_manager import job_manager
            job_manager.add_job(row.id, "proxy")
        except Exception:
            pass
        return row.id
    finally:
        db.close()


def _png_bytes(s: str) -> bytes:
    if s.startswith("data:"):
        s = s.split(",", 1)[1]
    return base64.b64decode(s)


def _out_size(aspect: str, short: int):
    w, h = ASPECTS.get(aspect, (1080, 1920))
    k = max(360, min(2160, int(short))) / min(w, h)
    return _even(w * k), _even(h * k)


@router.post("/{pid}/render")
def render(pid: int, body: RenderIn, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = _own(db, pid, current_user)
    p = body.data or _load(row)
    if body.data is not None:       # rendering what is on screen: keep it
        row.data = body.data.model_dump_json()
        row.updated_at = datetime.utcnow()
        db.commit()
    _sweep()
    W, H = _out_size(p.aspect, body.size)
    job_id = uuid.uuid4().hex[:16]
    work = _cache_dir("renders") / job_id
    work.mkdir(parents=True, exist_ok=True)
    titles = {}
    for t in body.titles:
        tid = re.sub(r"[^\w-]", "", t.id)[:20]
        f = work / f"title_{tid}.png"
        try:
            f.write_bytes(_png_bytes(t.png))
            titles[t.id] = f
        except Exception:
            pass
    out = work / f"{_safe_name(body.filename or row.name)}.mp4"
    crf = 18 if body.quality == "high" else 23
    args, total = build_render(db, p, W, H, crf, titles, work, out)
    with _rlock:
        RENDERS[job_id] = dict(t=time.time(), user=current_user.username, status="running", progress=0.0,
                               error=None, file=str(out), filename=out.name, total=total, w=W, h=H,
                               save_folder_id=body.save_folder_id, project=pid)
    threading.Thread(target=_run_render, args=(job_id, args, total), daemon=True,
                     name=f"zerko-render-{job_id}").start()
    return {"id": job_id, "duration": round(total, 3), "width": W, "height": H}


render_router = APIRouter(prefix="/api/video-render", tags=["video-edit"])


@render_router.get("/{job_id}")
def render_status(job_id: str, current_user: User = Depends(get_current_user)):
    with _rlock:
        j = RENDERS.get(job_id)
        if not j or (j["user"] != current_user.username and current_user.role != "admin"):
            raise HTTPException(status_code=404, detail="That render has expired - render it again")
        return {k: j.get(k) for k in ("status", "progress", "error", "filename", "total", "w", "h",
                                      "saved_id", "save_error")}


@render_router.post("/{job_id}/cancel")
def render_cancel(job_id: str, current_user: User = Depends(get_current_user)):
    with _rlock:
        j = RENDERS.get(job_id)
        if j and (j["user"] == current_user.username or current_user.role == "admin"):
            j["cancel"] = True
    return {"status": "ok"}


@render_router.get("/{job_id}/file")
def render_file(job_id: str, token: Optional[str] = None, db: Session = Depends(get_db)):
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")
    user = get_user_from_token(token, db)
    if not user or not permissions.can(user.role, permissions.DOWNLOAD):
        raise HTTPException(status_code=403, detail="Your account cannot download files.")
    with _rlock:
        j = RENDERS.get(job_id)
    if not j or j.get("status") != "done" or (j["user"] != user.username and user.role != "admin"):
        raise HTTPException(status_code=404, detail="That render has expired - render it again")
    if not os.path.exists(j["file"]):
        raise HTTPException(status_code=404, detail="The file is gone - render it again")
    mt = "application/zip" if j["file"].endswith(".zip") else "video/mp4"
    return FileResponse(j["file"], media_type=mt, filename=j["filename"], headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------
# DaVinci Resolve: the timeline as FCPXML
# --------------------------------------------------------------------------

def _rate(fps: float):
    """(frame duration as FCPXML rational numerator, denominator, timebase fps)"""
    ntsc = {23.976: (1001, 24000), 29.97: (1001, 30000), 59.94: (1001, 60000), 47.952: (1001, 48000)}
    for k, v in ntsc.items():
        if abs(fps - k) < 0.01:
            return v[0], v[1], v[1] / v[0]
    f = int(round(fps)) or 25
    return 100, f * 100, float(f)


class _T:
    """Seconds to whole frames, written as FCPXML time."""

    def __init__(self, fps: float):
        self.n, self.d, self.fps = _rate(fps)

    def frames(self, sec: float) -> int:
        return int(round(sec * self.fps))

    def __call__(self, sec: float) -> str:
        f = self.frames(sec)
        return "0s" if f == 0 else f"{f * self.n}/{self.d}s"

    def f(self, frames: int) -> str:
        return "0s" if frames == 0 else f"{frames * self.n}/{self.d}s"


def _ft(x: Fraction) -> str:
    """An exact time as FCPXML writes it."""
    x = Fraction(x)
    return "0s" if x == 0 else (f"{x.numerator}s" if x.denominator == 1 else f"{x.numerator}/{x.denominator}s")


def tc_start(info: dict) -> Fraction:
    """Where the file's own timecode starts, in seconds. Resolve finds a clip
    by path AND timecode, so the timeline has to use the camera's numbers."""
    tc = (info or {}).get("timecode") or ""
    m = re.match(r"^(\d+):(\d+):(\d+)[:;.](\d+)$", tc.strip())
    if not m:
        return Fraction(0)
    h, mi, se, fr = (int(g) for g in m.groups())
    fps = float(info.get("fps") or 25)
    n, d, _ = _rate(fps)
    base = int(round(fps))
    frames = ((h * 60 + mi) * 60 + se) * base + fr
    if ";" in tc or "." in tc or n == 1001:
        if n == 1001 and ";" in tc:          # drop-frame numbering
            drop = 2 * base // 30
            mins = h * 60 + mi
            frames -= drop * (mins - mins // 10)
    return Fraction(frames * n, d)


def win_url(path: str) -> str:
    """A server path as Windows sees it: /mnt/d/Media/x.mov -> file:///D:/Media/x.mov"""
    p = str(path).replace("\\", "/")
    m = re.match(r"^/mnt/([a-zA-Z])/(.*)$", p)
    if m:
        p = f"{m.group(1).upper()}:/{m.group(2)}"
    if re.match(r"^[A-Za-z]:/", p):
        return "file:///" + quote(p, safe="/:")
    return "file://" + quote(p, safe="/")


def _x(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _db_str(v: float) -> str:
    if v <= 0.0001:
        return "-96dB"
    return f"{20 * math.log10(v):.1f}dB"


def _hex01(c: str) -> str:
    c = (c or "#ffffff").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except Exception:
        r = g = b = 255
    return f"{r / 255:.3f} {g / 255:.3f} {b / 255:.3f} 1"


RESOLVE_TRANS = {"fade": "Cross Dissolve", "fadeblack": "Fade to Color", "fadewhite": "Fade to Color",
                 "wipeleft": "Wipe", "wiperight": "Wipe", "slideleft": "Slide", "slideright": "Slide",
                 "zoomin": "Cross Dissolve"}


def build_fcpxml(db: Session, p: Project, name: str, path_of=None) -> str:
    """The edit as an FCPXML 1.8 timeline (the version Resolve reads best). path_of(video) -> the media URL
    (a project pack points at its own copies)."""
    W, H = ASPECTS.get(p.aspect, (1080, 1920))
    fps = p.fps if p.fps and p.fps > 1 else 25.0
    T = _T(fps)
    lay, total = layout(p)
    res: List[str] = []
    res.append(f'<format id="r0" name="Zerko {W}x{H}" frameDuration="{T.f(1)}" width="{W}" height="{H}" colorSpace="1-1-1 (Rec. 709)"/>')
    assets: Dict[int, str] = {}
    tcs: Dict[int, Fraction] = {}
    formats: Dict[tuple, str] = {}
    nid = [1]

    def rid(prefix="r"):
        nid[0] += 1
        return f"{prefix}{nid[0]}"

    def asset(vid: int) -> str:
        if vid in assets:
            return assets[vid]
        v = _video(db, vid)
        src = _src(v)
        info = probe_path(src)
        has_v = bool(info.get("has_video")) and (v.media_type or "video") == "video"
        fmt = ""
        if has_v:
            key = (info.get("width"), info.get("height"), round(info.get("fps") or fps, 3))
            if key not in formats:
                fid = rid()
                fT = _T(key[2] or fps)
                res.append(f'<format id="{fid}" frameDuration="{fT.f(1)}" width="{key[0]}" height="{key[1]}"/>')
                formats[key] = fid
            fmt = f' format="{formats[key]}"'
        aid = rid()
        url = path_of(v) if path_of else win_url(src)
        dur = T(float(info.get("duration") or v.duration or 0))
        tcs[vid] = tc_start(info)
        res.append(f'<asset id="{aid}" name="{_x(v.filename)}" start="{_ft(tcs[vid])}" duration="{dur}" '
                   f'hasVideo="{1 if has_v else 0}" hasAudio="{1 if info.get("has_audio") else 0}"{fmt} '
                   f'audioSources="1" audioChannels="2" audioRate="48000" src="{_x(url)}"/>')
        assets[vid] = aid
        return aid

    title_fx = None
    trans_fx: Dict[str, str] = {}

    # frame-snap the layout so every edit lands on a frame
    fstart = [T.frames(s) for s, _ in lay]
    flen = [max(1, T.frames(L)) for _, L in lay]
    fd = [T.frames(trans_after(p, i)) for i in range(len(p.clips))]

    spine: List[str] = []
    placed: List[tuple] = []       # (timeline frame start, frame end, spine index) for connecting
    for i, c in enumerate(p.clips):
        aid = asset(c.video_id)
        v = _video(db, c.video_id)
        info = probe_path(_src(v))
        # a transition is centred on the edit: the clip before gives up half,
        # the one after starts half later - the media under it is the handle
        cut_in = fd[i - 1] // 2 if i > 0 else 0
        cut_out = fd[i] - fd[i] // 2 if i < len(p.clips) - 1 else 0
        off = fstart[i] + cut_in
        dur = flen[i] - cut_in - cut_out
        src_start = c.src_in + cut_in / T.fps * c.speed
        inner = []
        sw, sh = info.get("width") or W, info.get("height") or H
        fit = min(W / sw, H / sh)
        dw, dh, x, y = _rect(sw, sh, W, H, c.frame)
        scale = (dw / sw) / fit
        # FCPXML positions: the frame's height is 100 units, origin in the centre
        px = ((x + dw / 2) - W / 2) / H * 100
        py = -((y + dh / 2) - H / 2) / H * 100
        if abs(scale - 1) > 1e-3 or abs(px) > 1e-3 or abs(py) > 1e-3:
            inner.append(f'<adjust-transform position="{px:.3f} {py:.3f}" scale="{scale:.4f} {scale:.4f}"/>')
        if c.mute:
            inner.append('<adjust-volume amount="-96dB"/>')
        elif abs(c.volume - 1) > 1e-3:
            inner.append(f'<adjust-volume amount="{_db_str(c.volume)}"/>')
        tc0 = tcs.get(c.video_id, Fraction(0))
        tcf = "DF" if ";" in (info.get("timecode") or "") else "NDF"
        start_attr = _ft(tc0 + Fraction(T.frames(src_start) * T.n, T.d))
        if abs(c.speed - 1) > 1e-3:
            # retimed: the clip's own time runs 1/speed of the media's, and
            # start is given in the clip's time
            mdur = float(info.get("duration") or v.duration or c.src_out)
            inner.insert(0, f'<timeMap><timept time="{_ft(tc0)}" value="{_ft(tc0)}" interp="linear"/>'
                            f'<timept time="{_ft(tc0 + Fraction(T.frames(mdur / c.speed) * T.n, T.d))}" '
                            f'value="{_ft(tc0 + Fraction(T.frames(mdur) * T.n, T.d))}" interp="linear"/></timeMap>')
            start_attr = _ft(tc0 + Fraction(T.frames(src_start / c.speed) * T.n, T.d))
        spine.append([f'<asset-clip ref="{aid}" offset="{T.f(off)}" name="{_x(v.filename)}" '
                      f'start="{start_attr}" duration="{T.f(dur)}" tcFormat="{tcf}"', inner, off, c])
        placed.append((off, off + dur, len(spine) - 1, src_start, c.speed, tc0))
        if i < len(p.clips) - 1 and fd[i] > 0:
            kind = RESOLVE_TRANS.get(c.trans, "Cross Dissolve")
            if kind not in trans_fx:
                tid = rid()
                trans_fx[kind] = tid
                # Final Cut's own Cross Dissolve id: Resolve knows it
                res.append(f'<effect id="{tid}" name="{kind}" uid="FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265"/>')
            spine.append(f'<transition name="{kind}" offset="{T.f(fstart[i + 1])}" duration="{T.f(fd[i])}">'
                         f'<filter-video ref="{trans_fx[kind]}" name="{kind}"/></transition>')

    def connect(t_sec: float) -> tuple:
        """The spine clip a connected piece hangs off, and the local time for t."""
        tf = T.frames(t_sec)
        best = placed[0]
        for pl in placed:
            if pl[0] <= tf < pl[1]:
                best = pl
                break
            if pl[0] <= tf:
                best = pl
        off, _, idx, src_start, speed, tc0 = best
        local = tc0 + Fraction(T.frames(src_start / speed) + (tf - off), 1) * T.n / T.d
        return idx, local

    for m in p.music:
        if not placed:
            break
        aid = asset(m.video_id)
        v = _video(db, m.video_id)
        idx, local = connect(m.start)
        dur = min(m.dur, max(0.04, total - m.start))
        el = (f'<asset-clip ref="{aid}" lane="-1" offset="{_ft(local)}" name="{_x(v.filename)}" '
              f'start="{_ft(tcs.get(m.video_id, Fraction(0)) + Fraction(T.frames(m.src_in) * T.n, T.d))}" duration="{T(dur)}" tcFormat="NDF" audioRole="music">'
              f'<adjust-volume amount="{_db_str(m.volume)}"/>')
        el += "</asset-clip>"
        spine[idx][1].append(el)

    for n, tt in enumerate(p.titles):
        if not placed or not tt.text.strip():
            continue
        if title_fx is None:
            title_fx = rid()
            res.append(f'<effect id="{title_fx}" name="Basic Title" '
                       f'uid=".../Titles.localized/Bumper:Opener.localized/Basic Title.localized/Basic Title.moti"/>')
        idx, local = connect(tt.start)
        sid = f"ts{n + 1}"
        px = (tt.x - 0.5) * W / H * 100
        py = -(tt.y - 0.5) * 100
        size = max(10, int(round(tt.size * H)))
        spine[idx][1].append(
            f'<title ref="{title_fx}" lane="1" offset="{_ft(local)}" name="{_x(tt.text[:40])}" start="0s" duration="{T(tt.dur)}">'
            f'<param name="Position" key="9999/999166631/999166633/1/100/101" value="{px:.2f} {py:.2f}"/>'
            f'<text><text-style ref="{sid}">{_x(tt.text)}</text-style></text>'
            f'<text-style-def id="{sid}"><text-style font="{_x(tt.font or "Helvetica")}" fontSize="{size}" '
            f'fontColor="{_hex01(tt.color)}" bold="{1 if tt.weight >= 600 else 0}" alignment="{_x(tt.align)}"/>'
            f'</text-style-def></title>')

    body = []
    for s in spine:
        if isinstance(s, str):
            body.append(s)
        else:
            head, inner, _, _ = s
            body.append(head + ">" + "".join(inner) + "</asset-clip>")
    seq_dur = T(total)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n<fcpxml version="1.8">\n'
            "<resources>\n" + "\n".join(res) + "\n</resources>\n"
            f'<library><event name="Zerko"><project name="{_x(name)}">'
            f'<sequence format="r0" duration="{seq_dur}" tcStart="0s" tcFormat="NDF" audioLayout="stereo" audioRate="48k">'
            "<spine>\n" + "\n".join(body) + "\n</spine></sequence></project></event></library>\n</fcpxml>\n")


def _q_user(request: Request, token: Optional[str], db: Session) -> User:
    u = _auth_q(request, token, db)
    if not permissions.can(u.role, permissions.DOWNLOAD):
        raise HTTPException(status_code=403, detail="Your account cannot download files.")
    return u


@router.get("/{pid}/fcpxml")
def fcpxml(pid: int, request: Request, token: Optional[str] = None, db: Session = Depends(get_db)):
    u = _q_user(request, token, db)
    row = _own(db, pid, u)
    xml = build_fcpxml(db, _load(row), row.name)
    fname = f"{_safe_name(row.name)}.fcpxml"
    return Response(xml, media_type="application/xml",
                    headers={"Content-Disposition": f"attachment; filename*=utf-8''{quote(fname)}",
                             "Cache-Control": "no-store"})


class PackIn(BaseModel):
    proxies: bool = False


def _run_pack(job_id: str, files: list, xml: str, readme: str, zpath: Path):
    job = RENDERS[job_id]
    try:
        total = sum(os.path.getsize(s) for s, _ in files) or 1
        done = 0
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
            z.writestr("Timeline.fcpxml", xml)
            z.writestr("READ ME.txt", readme)
            for src, arc in files:
                if job.get("cancel"):
                    raise RuntimeError("Stopped")
                z.write(src, arc)
                done += os.path.getsize(src)
                with _rlock:
                    job["progress"] = min(0.999, done / total)
        with _rlock:
            job["status"], job["progress"] = "done", 1.0
    except Exception as e:
        with _rlock:
            job["status"], job["error"] = ("cancelled" if str(e) == "Stopped" else "failed"), str(e)[:500]


@router.post("/{pid}/pack")
def pack(pid: int, body: PackIn, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """The Resolve timeline and the footage it uses, in one ZIP."""
    row = _own(db, pid, current_user)
    p = _load(row)
    ids = []
    for c in p.clips:
        ids.append(c.video_id)
    for m in p.music:
        ids.append(m.video_id)
    files, arcs, used = [], {}, set()
    for vid in dict.fromkeys(ids):
        v = _video(db, vid)
        src = _src(v)
        if body.proxies and v.proxy_path and (v.media_type or "video") == "video":
            pp = _resolve(v.proxy_path)
            if pp and os.path.exists(pp):
                src = pp
        name = v.filename if not body.proxies else Path(v.filename).stem + Path(src).suffix
        base, ext = os.path.splitext(name)
        n = 2
        while name.lower() in used:
            name = f"{base}_{n}{ext}"
            n += 1
        used.add(name.lower())
        files.append((src, f"Media/{name}"))
        arcs[vid] = name
    _sweep()
    job_id = uuid.uuid4().hex[:16]
    work = _cache_dir("renders") / job_id
    work.mkdir(parents=True, exist_ok=True)
    # Resolve needs full paths; these point at where the pack says to unzip
    # it, and Resolve relinks by name from the Media folder either way.
    xml = build_fcpxml(db, p, row.name, path_of=lambda v: win_url(f"C:/Zerko Packs/{_safe_name(row.name)}/Media/{arcs[v.id]}"))
    readme = (f"{row.name}\n\n"
              "Opening this in DaVinci Resolve:\n"
              f"1. Unzip this into  C:\\Zerko Packs\\{_safe_name(row.name)}  (then everything links by itself),\n"
              "   or anywhere else.\n"
              "2. In Resolve: File > Import > Timeline, pick Timeline.fcpxml.\n"
              "3. If clips show offline (unzipped somewhere else): select them in the Media Pool,\n"
              "   right-click > Relink Selected Clips, and point it at the Media folder.\n\n"
              + ("The footage in Media is the smaller proxy copies - relink to the originals for the final render.\n"
                 if body.proxies else "The footage in Media is the original files.\n"))
    zpath = work / f"{_safe_name(row.name)} - Resolve pack.zip"
    with _rlock:
        RENDERS[job_id] = dict(t=time.time(), user=current_user.username, status="running", progress=0.0,
                               error=None, file=str(zpath), filename=zpath.name, total=0, w=0, h=0,
                               project=pid)
    threading.Thread(target=_run_pack, args=(job_id, files, xml, readme, zpath), daemon=True).start()
    return {"id": job_id}


def install(app, media_root, resolve_media_path, ensure_folder_row=None):
    global _media_root, _resolve, _ensure_folder_row
    _media_root = Path(media_root) if media_root else None
    _resolve = resolve_media_path or (lambda p: p)
    _ensure_folder_row = ensure_folder_row
    Base.metadata.create_all(bind=engine, tables=[VideoProject.__table__])
    app.include_router(tools)
    app.include_router(render_router)
    app.include_router(router)
