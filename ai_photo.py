"""AI in the photo editor: generated looks, skies, declutter, room types and
reference matching.

Claude reads photos and answers with editor settings, boxes or words. The
generated looks at the end of this file are painted by an image model
(ai_image.py) and kept as a layer over the edit, never as slider moves.
"""
from __future__ import annotations

import json
import random
import os
from typing import Any, Dict, List, Optional

import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import ai
import photo_edit as pe
from auth import get_current_user
from database import User, Video, get_db

router = APIRouter(prefix="/api/ai/photo", tags=["ai"])


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _video(db: Session, vid: int) -> Video:
    v = db.query(Video).filter(Video.id == vid).first()
    if not v:
        raise HTTPException(status_code=404, detail="Photo not found")
    return v


def _path(v: Video) -> str:
    p = pe._resolve(v.filepath)
    if not p or not os.path.exists(p):
        raise HTTPException(status_code=404, detail="The photo file is not on disk")
    return p


def base_rgb(vid: int, path: str, w: int = 1280) -> np.ndarray:
    """The undeveloped photo (0..1 float RGB), long edge about w."""
    import cv2
    f = pe._base_file(vid, path, w)
    bgr = cv2.imread(str(f), cv2.IMREAD_COLOR)
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0


def developed(vid: int, path: str, recipe: Optional[dict], w: int = 1280) -> np.ndarray:
    """The photo with a recipe applied - what Claude looks at. Noise reduction
    is left out: it costs seconds and changes nothing Claude judges."""
    rgb = base_rgb(vid, path, w)
    try:
        rec = pe.Recipe(**(recipe or {}))
    except Exception:
        rec = pe.Recipe()
    rec = rec.model_copy(update={"dn_luma": 0.0, "dn_colour": 0.0, "wm_opacity": 0.0})
    return np.clip(pe.apply_recipe(rgb, rec, pe._source_long(path), None), 0, 1)


def saved_recipe(db: Session, vid: int) -> dict:
    row = db.query(pe.PhotoEdit).filter(pe.PhotoEdit.video_id == vid).first()
    try:
        return json.loads(row.recipe) if row and row.recipe else {}
    except Exception:
        return {}


# --------------------------------------------------------------------------
# the settings Claude may use for a look
# --------------------------------------------------------------------------

BANDS = ["red", "orange", "yellow", "green", "aqua", "blue", "purple", "magenta"]
LOOK_NUM = (
    ["exposure", "contrast", "highlights", "shadows", "whites", "blacks", "tint", "saturation", "vibrance",
     "clarity", "dehaze", "vignette", "grade_sh_hue", "grade_sh_sat", "grade_mid_hue", "grade_mid_sat",
     "grade_hi_hue", "grade_hi_sat", "grade_balance"]
    + [f"{p}_{w}_{q}" for w in ("lift", "gamma", "gain", "offset") for p, q in [("pw", "hue"), ("pw", "sat"), ("pw", "y")]]
    + [f"{k}_{b}" for k in ("hue", "sat", "lum") for b in BANDS]
)
LOOK_LISTS = ["curve", "curve_r", "curve_g", "curve_b", "slices"]

KNOBS = """Settings (all optional; leave out anything you do not change):
- exposure (-5..5, stops), contrast, highlights, shadows (-1..1), whites, blacks (-0.5..0.5)
- temp_shift: Kelvin RELATIVE to the photo now (+ = warmer, - = cooler; +300 is a gentle warm-up, 1500 is a lot)
- tint (-1..1, + = magenta, - = green), saturation, vibrance (-1..1)
- clarity (-1..1, midtone local contrast), dehaze (-0.5..1), vignette (-1..1, - darkens the corners)
- HSL mixer per band (red, orange, yellow, green, aqua, blue, purple, magenta): hue_<band>, sat_<band>, lum_<band> (-1..1)
- split grading: grade_sh_hue/grade_mid_hue/grade_hi_hue (0..360), grade_sh_sat/grade_mid_sat/grade_hi_sat (0..1, 0.1-0.3 is typical), grade_balance (-1..1)
- primary wheels like Resolve: pw_lift_*, pw_gamma_*, pw_gain_*, pw_offset_* each with _hue (0..360), _sat (0..1, keep under 0.4), _y (-1..1 brightness of that range)
- curve, curve_r, curve_g, curve_b: tone curves as [[x,y],...] in 0..1 with [0,y0] first and [1,y1] last (e.g. a soft S: [[0,0.02],[0.25,0.22],[0.75,0.8],[1,1]])
- slices: up to 6 colour slices (Resolve ColorSlice) [{"hue":0..360,"width":half-width degrees 4..60,"soft":degrees,"sat_min":0..0.3,"hue_shift":-90..90,"sat":-1..2,"density":-1..1,"lum":-1..1,"name":"..."}] - use these to fix one colour precisely (a sage wardrobe, a lawn, a pool)
Hues: 0 red, 30 orange, 55 yellow, 90 yellow-green, 120 green, 180 cyan, 220 blue, 280 purple, 320 magenta."""

SYSTEM = ("You are a senior photo editor for a real-estate and architecture studio, grading in Zerko, a Lightroom-like editor. "
          "You look at the photo and answer only with editor settings. Real-estate looks must stay believable: true whites, "
          "straight neutral walls, no crushed blacks, no neon grass or cartoon skies - unless the brief asks for something else.")


def _num_limits() -> Dict[str, tuple]:
    out = {}
    for n, f in pe.Recipe.model_fields.items():
        lo = hi = None
        for m in f.metadata:
            if hasattr(m, "ge"):
                lo = m.ge
            if hasattr(m, "le"):
                hi = m.le
        if lo is not None and hi is not None:
            out[n] = (float(lo), float(hi))
    return out


LIMITS = _num_limits()


def _curve(v) -> Optional[List[List[float]]]:
    try:
        pts = sorted([[float(np.clip(p[0], 0, 1)), float(np.clip(p[1], 0, 1))] for p in v if len(p) == 2])
    except Exception:
        return None
    if len(pts) < 2:
        return None
    pts[0][0], pts[-1][0] = 0.0, 1.0
    return pts[:12]


def clean_patch(raw: Any, current: dict) -> dict:
    """Claude's settings made safe: known keys only, inside their ranges,
    temperature turned from a shift into this photo's own Kelvin."""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {}
    for k in LOOK_NUM:
        if k in raw:
            try:
                v = float(raw[k])
            except Exception:
                continue
            lo, hi = LIMITS.get(k, (-1, 1))
            out[k] = round(float(np.clip(v, lo, hi)), 4)
    if "temp_shift" in raw:
        try:
            base = float(current.get("temp_k", 6500) or 6500)
            out["temp_k"] = round(float(np.clip(base + float(raw["temp_shift"]), 2000, 15000)))
        except Exception:
            pass
    for k in ("curve", "curve_r", "curve_g", "curve_b"):
        if k in raw:
            c = _curve(raw[k])
            if c:
                out[k] = c
    if isinstance(raw.get("slices"), list):
        sl = []
        for s in raw["slices"][:6]:
            try:
                sl.append(pe.Slice(**{k: v for k, v in s.items() if k in pe.Slice.model_fields}).model_dump())
            except Exception:
                continue
        out["slices"] = sl
    # anything that would not load is dropped rather than failing the look
    try:
        pe.Recipe(**{**current, **out})
    except Exception:
        return {k: v for k, v in out.items() if k not in ("slices",)}
    return out


def _non_default(rec: dict) -> dict:
    d = pe.Recipe().model_dump()
    keep = set(LOOK_NUM) | {"temp_k"} | set(LOOK_LISTS)
    return {k: v for k, v in rec.items() if k in keep and k in d and v != d[k]}


# --------------------------------------------------------------------------
# looks
# --------------------------------------------------------------------------

def install(app):
    app.include_router(router)


# --------------------------------------------------------------------------
# scene: new skies, windows lit, twilight
# --------------------------------------------------------------------------
#
# Each result is a patch like the Remove tool's (an RGBA PNG laid over a box
# of the undeveloped photo), so the photo's own grade still applies on top,
# the preview and the export show the same thing, and it can be removed.

import re
import shutil
import threading
import uuid

from fastapi import File, UploadFile

WORK = 3000          # the long edge the scene tools work at
_scene_lock = threading.Lock()
SKY_EXT = (".jpg", ".jpeg", ".png", ".webp")


def sky_dir():
    d = (pe._media_root or pe.Path(".")) / "_skies"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sky_files():
    return sorted([p for p in sky_dir().iterdir() if p.is_file() and p.suffix.lower() in SKY_EXT], key=lambda p: p.name.lower())


def _work_rgb(path: str) -> np.ndarray:
    return pe._read_rgb(path, max_dim=WORK)


def _sam_ready() -> bool:
    try:
        import editor_ai
        st = editor_ai.ai_status()
        return bool(st.get("runtime")) and st.get("status") == "ready"
    except Exception:
        return False


def _u8_key(path: str, u8: np.ndarray):
    st = os.stat(path)
    return (path, st.st_mtime_ns, u8.shape)


def _sky_auto(path: str, rgb: np.ndarray) -> np.ndarray:
    import editor_ai
    h, w = rgb.shape[:2]
    if _sam_ready():
        u8 = (np.clip(rgb, 0, 1) * 255).astype(np.uint8)
        try:
            return editor_ai.sam_sky(u8, _u8_key(path, u8), (h, w))
        except Exception as e:
            print(f"ai_photo: SAM sky failed, using the fallback: {e}", flush=True)
    return editor_ai.select_sky(rgb)


SKY_CHECK = (
    "The first picture is a photo. The second is the same photo with the area selected as SKY tinted red. "
    "Is the selection right - all of the sky, and nothing that is not sky (roofs, walls, trees, hills, sea)? "
    "If it is not, trace the skyline: the line where the sky meets everything else, as points from the left edge (x=0) "
    "to the right edge (x=1), in 0..1 of the picture (y down), close enough together to follow roofs and treetops "
    "(20 to 60 points). If there is no sky at all, say so. "
    'Answer as {"ok":true} or {"ok":false,"skyline":[[x,y],...]} or {"ok":false,"no_sky":true}'
)


def _skyline_mask(path: str, rgb: np.ndarray, pts, rough: np.ndarray) -> np.ndarray:
    """A sky mask from Claude's skyline: everything above the line, with the
    edge itself taken from the picture (Segment Anything when it is here,
    otherwise the colour-based mask in a narrow band along the line)."""
    import cv2
    import editor_ai
    h, w = rgb.shape[:2]
    pts = sorted([(float(np.clip(x, 0, 1)), float(np.clip(y, 0, 1))) for x, y in pts])
    if pts[0][0] > 0:
        pts.insert(0, (0.0, pts[0][1]))
    if pts[-1][0] < 1:
        pts.append((1.0, pts[-1][1]))
    xs = np.arange(w, dtype=np.float32) / max(1, w - 1)
    line = np.interp(xs, [p[0] for p in pts], [p[1] for p in pts]) * h       # skyline row per column
    rows = np.arange(h, dtype=np.float32)[:, None]
    poly = (rows < line[None, :]).astype(np.float32)
    if _sam_ready():
        u8 = (np.clip(rgb, 0, 1) * 255).astype(np.uint8)
        try:
            k = min(12, len(pts))
            sel = [pts[int(i)] for i in np.linspace(0, len(pts) - 1, k)]
            pos = [(x, max(0.005, y - 0.06)) for x, y in sel if y > 0.07]
            neg = [(x, min(0.995, y + 0.06)) for x, y in sel]
            if pos:
                m = editor_ai.sam_click(u8, _u8_key(path, u8), pos + neg, [1] * len(pos) + [0] * len(neg), (h, w))
                band = np.abs(rows - line[None, :]) < h * 0.05
                return np.where(band, m, poly).astype(np.float32)
        except Exception as e:
            print(f"ai_photo: SAM skyline failed: {e}", flush=True)
    band = np.clip(1 - np.abs(rows - line[None, :]) / (h * 0.03), 0, 1)
    m = poly * (1 - band) + np.minimum(rough, (rows < line[None, :] + h * 0.03)) * band
    return editor_ai._refine(np.clip(m, 0, 1).astype(np.float32), rgb, radius=max(2, w // 500))


def sky_mask(path: str, rgb: np.ndarray) -> np.ndarray:
    """The sky: found automatically, then checked by Claude (when AI is on),
    who retraces the skyline where the automatic one went wrong."""
    m = _sky_auto(path, rgb)
    if not ai.enabled():
        return m
    try:
        tint = rgb.copy()
        tint[..., 0] = np.clip(tint[..., 0] + m * 0.6, 0, 1)
        tint[..., 1:] *= (1 - m * 0.5)[..., None]
        d = ai.ask_json(SKY_CHECK, [ai.jpeg_b64(rgb, 1024), ai.jpeg_b64(tint, 1024)], "", max_tokens=2500, temperature=0.0)
    except Exception as e:
        print(f"ai_photo: sky check skipped: {e}", flush=True)
        return m
    if not isinstance(d, dict) or d.get("ok", True):
        return m
    if d.get("no_sky"):
        return np.zeros_like(m)
    pts = d.get("skyline")
    try:
        pts = [(float(p[0]), float(p[1])) for p in pts if len(p) >= 2]
    except Exception:
        return m
    return _skyline_mask(path, rgb, pts, m) if len(pts) >= 2 else m


def item_mask(path: str, rgb: np.ndarray, box) -> Optional[np.ndarray]:
    """A small thing standing on something bigger (a vase on a table): Segment Anything pointed at the middle of
    the box, told the box's corners are not it - a box prompt alone traced the table instead."""
    import editor_ai
    if not _sam_ready():
        return None
    h, w = rgb.shape[:2]
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    pts = [(cx, cy), (x0, y0), (x1, y0), (x0, y1), (x1, y1)]
    u8 = (np.clip(rgb, 0, 1) * 255).astype(np.uint8)
    try:
        return editor_ai.sam_click(u8, _u8_key(path, u8), pts, [1, 0, 0, 0, 0], (h, w))
    except Exception as e:
        print(f"ai_photo: SAM point failed: {e}", flush=True)
        return None


def box_mask(path: str, rgb: np.ndarray, box) -> np.ndarray:
    """What is inside a dragged box (x0, y0, x1, y1 in 0..1): the house."""
    import editor_ai
    h, w = rgb.shape[:2]
    if _sam_ready():
        u8 = (np.clip(rgb, 0, 1) * 255).astype(np.uint8)
        try:
            return editor_ai.sam_box(u8, _u8_key(path, u8), box, (h, w))
        except Exception as e:
            print(f"ai_photo: SAM box failed, using the fallback: {e}", flush=True)
    import cv2
    m = editor_ai.select_box(rgb, box)
    return cv2.resize(m, (w, h), interpolation=cv2.INTER_LINEAR) if m.shape != (h, w) else m


def _save_patch(vid: int, rgba: np.ndarray, x0: int, y0: int, x1: int, y1: int, W: int, H: int, kind: str) -> dict:
    import cv2
    name = "rm" + uuid.uuid4().hex[:14]
    bgra = cv2.cvtColor((np.clip(rgba, 0, 1) * 255 + 0.5).astype(np.uint8), cv2.COLOR_RGBA2BGRA)
    if not cv2.imwrite(str(pe.mask_dir(vid) / f"{name}.png"), bgra):
        raise HTTPException(status_code=500, detail="Could not save the result")
    return {"ref": f"{vid}/{name}", "box": [x0 / W, y0 / H, (x1 - x0) / W, (y1 - y0) / H], "kind": kind}


def gradient_sky(kind: str, w: int, h: int) -> np.ndarray:
    """A plain sky made to measure: clear blue, or the blue hour."""
    t = np.linspace(0, 1, h, dtype=np.float32)[:, None, None]      # 0 top, 1 horizon
    if kind == "twilight":
        top, mid, low = np.float32([0.10, 0.16, 0.42]), np.float32([0.30, 0.36, 0.70]), np.float32([0.98, 0.62, 0.36])
        c = np.where(t < 0.6, top + (mid - top) * (t / 0.6), mid + (low - mid) * (np.clip(t - 0.6, 0, None) / 0.4) ** 1.4)
    else:
        top, low = np.float32([0.22, 0.45, 0.85]), np.float32([0.72, 0.84, 0.96])
        c = top + (low - top) * t ** 1.3
    return np.broadcast_to(c, (h, w, 3)).astype(np.float32).copy()


def load_sky(name: str, w: int, h: int) -> np.ndarray:
    """The sky picture filling w x h: scaled to cover, its bottom edge (the
    horizon side) kept, centred left to right."""
    if name.startswith("gradient:"):
        return gradient_sky(name.split(":", 1)[1], w, h)
    import cv2
    p = sky_dir() / os.path.basename(name)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="That sky is not in the skies folder any more")
    bgr = cv2.imread(str(p), cv2.IMREAD_COLOR)
    if bgr is None:
        raise HTTPException(status_code=400, detail="That sky picture could not be read")
    sky = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    sh, sw = sky.shape[:2]
    k = max(w / sw, h / sh)
    nw, nh = max(w, int(round(sw * k))), max(h, int(round(sh * k)))
    sky = cv2.resize(sky, (nw, nh), interpolation=cv2.INTER_AREA if k < 1 else cv2.INTER_CUBIC)
    x = (nw - w) // 2
    return sky[nh - h:nh, x:x + w]


def replace_sky(rgb: np.ndarray, mask: np.ndarray, sky_name: str, match: float = 1.0):
    """(patch rgba over rows 0..y1, y1, how the light changed) for a new sky.

    The new sky is set to the old sky's brightness (the photo's own exposure
    and grade still apply over it), and where the old sky showed through
    leaves and around edges its light is swapped for the new one's - that
    is what stops the bright halo round trees."""
    import cv2
    h, w = rgb.shape[:2]
    m = np.clip(mask.astype(np.float32), 0, 1)
    rows = np.nonzero((m > 0.3).any(axis=1))[0]
    if not len(rows) or (m > 0.5).mean() < 0.01:
        raise HTTPException(status_code=400, detail="No sky found in this photo")
    y1 = int(min(h, rows.max() + max(8, h // 60)))
    sky = load_sky(sky_name, w, y1)
    solid = m[:y1] > 0.85
    old_mean = rgb[:y1][solid].mean(axis=0) if solid.any() else rgb[:y1].reshape(-1, 3).mean(axis=0)
    new_mean = sky[solid].mean(axis=0) if solid.any() else sky.reshape(-1, 3).mean(axis=0)
    lum = np.float32([0.2126, 0.7152, 0.0722])
    lo, ln = float(old_mean @ lum), float(new_mean @ lum)
    gain = float(np.clip(lo / max(ln, 1e-3), 0.6, 1.5)) * match + (1 - match)
    sky = np.clip(sky * gain, 0, 1)
    a = m[:y1]
    a = np.clip((a - 0.08) / 0.84, 0, 1)
    # the light near the sky edge: scaled from the old sky's to the new sky's
    near = cv2.GaussianBlur(m[:y1], (0, 0), max(2.0, w / 400))
    ratio = np.clip((sky @ lum + 1e-3) / (float(old_mean @ lum) + 1e-3), 0.25, 1.2)[..., None]
    tint = np.clip(sky / np.maximum(sky @ lum, 1e-3)[..., None], 0.5, 1.8)
    # only the old sky's light is swapped: pixels near the edge as bright as
    # that sky (leaves lit from behind, a hazy ridge), never the dark ones
    yfg = rgb[:y1] @ lum
    spill = np.clip((yfg / (lo + 1e-3) - 0.45) * 2.0, 0, 1)
    fac = np.clip(ratio * tint, 0.2, 1.0)
    # next to the sky and as bright as it: that is sky the mask missed (the
    # gaps between leaves, a soft ridge) - it takes the new sky too
    skyish = np.clip((yfg / (lo + 1e-3) - 0.6) * 4.0, 0, 1) * np.clip(near * 3.0, 0, 1)
    a = np.maximum(a, skyish)
    fg = rgb[:y1] * (1 - (near * spill)[..., None] * 0.7 * (1 - fac))
    out = sky * a[..., None] + fg * (1 - a[..., None])
    alpha = np.clip(np.maximum(a, near * spill), 0, 1)
    # how the light moved: warmth (red against blue) and brightness
    shift = {
        "warmth": float(np.log((new_mean[0] * gain + 1e-3) / (new_mean[2] * gain + 1e-3)) - np.log((old_mean[0] + 1e-3) / (old_mean[2] + 1e-3))),
        "bright": float(np.log2((ln * gain + 1e-3) / (lo + 1e-3))),
    }
    return np.dstack([out, alpha]).astype(np.float32), y1, shift


def relight_patch(shift: dict) -> dict:
    """The rest of the photo nudged toward the new sky's light - kept as a
    fix, so it switches off on its own."""
    return {"add": {
        "temp_k": round(float(np.clip(shift["warmth"] * 900, -900, 900))),
        "exposure": round(float(np.clip(shift["bright"] * 0.25, -0.35, 0.2)), 3),
    }, "set": {}}


class SkyBody(BaseModel):
    sky: str = Field(..., max_length=200)
    relight: bool = True


@router.get("/skies")
def list_skies(current_user: User = Depends(get_current_user)):
    return {"folder": str(sky_dir()), "skies": [{"name": f.name, "size": f.stat().st_size} for f in sky_files()]}


@router.get("/skies/{name}/thumb")
def sky_thumb(name: str, current_user: User = Depends(get_current_user)):
    import cv2
    from fastapi.responses import Response
    p = sky_dir() / os.path.basename(name)
    if not p.is_file() or p.suffix.lower() not in SKY_EXT:
        raise HTTPException(status_code=404, detail="No such sky")
    img = cv2.imread(str(p), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(status_code=400, detail="Unreadable")
    k = 320 / max(img.shape[:2])
    if k < 1:
        img = cv2.resize(img, (int(img.shape[1] * k), int(img.shape[0] * k)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return Response(buf.tobytes(), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=600"})


@router.post("/skies")
def upload_sky(file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    import permissions
    if not permissions.can(current_user.role, permissions.UPLOAD):
        raise HTTPException(status_code=403, detail="Your account cannot upload files.")
    name = os.path.basename(file.filename or "")
    if not name.lower().endswith(SKY_EXT):
        raise HTTPException(status_code=400, detail="A sky must be a JPEG, PNG or WebP picture.")
    dest = sky_dir() / re.sub(r'[<>:"|?*\\/]', "_", name)
    with open(dest, "wb") as out:
        shutil.copyfileobj(file.file, out, 1024 * 1024)
    return {"status": "ok", "name": dest.name}


@router.delete("/skies/{name}")
def delete_sky(name: str, current_user: User = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    p = sky_dir() / os.path.basename(name)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="No such sky")
    p.unlink()
    return {"status": "ok"}


@router.post("/{video_id}/sky")
def sky(video_id: int, body: SkyBody, db: Session = Depends(get_db),
        current_user: User = Depends(get_current_user)):
    """A new sky, lined up on the horizon, with the light round its edges
    swapped; plus the relight (as a fix) when asked for."""
    v = _video(db, video_id)
    path = _path(v)
    with _scene_lock:
        rgb = _work_rgb(path)
        m = sky_mask(path, rgb)
        rgba, y1, shift = replace_sky(rgb, m, body.sky)
    h, w = rgb.shape[:2]
    out = {"patch": _save_patch(video_id, rgba, 0, 0, w, y1, w, h, "sky")}
    if body.relight:
        out["relight"] = relight_patch(shift)
    return out


# ---- windows lit ----------------------------------------------------------

# ---- declutter -------------------------------------------------------------

CLUTTER_PROMPT = (
    "This is a property photo for a listing. List the clutter a careful photographer would have tidied away before shooting: "
    "loose items on counters and tables, cables, bins, toiletries, shoes, toys, washing, fridge magnets, remotes, bags, "
    "personal photos, hoses. NEVER list cars, vehicles or people - the photographer removes those by hand. Do NOT list furniture, fixed fittings, lights, "
    "plants that belong, art, or anything large that would leave a hole. List every object on its own with its own box - "
    "never one box round several things (three photo frames are three items), since everything inside a box is removed. "
    "Leave out anything mostly hidden behind furniture. Give each a short label that says where it is "
    "(\"bottles on the left counter\") and a tight box in 0..1 of the picture: x0, y0 (top left), x1, y1 (bottom right). "
    'Answer as {"items":[{"label":"...","box":[x0,y0,x1,y1]}]} - an empty list if the room is already tidy.'
)


@router.post("/{video_id}/clutter")
def clutter(video_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """What Claude would tidy away, as labelled boxes for you to tick."""
    v = _video(db, video_id)
    path = _path(v)
    rgb = base_rgb(video_id, path, 1600)
    d = ai.ask_json(CLUTTER_PROMPT + ai.GRID_NOTE, [ai.grid_b64(rgb)], "", max_tokens=3000, temperature=0.0)
    raw = d.get("items", []) if isinstance(d, dict) else d
    out = []
    for it in raw if isinstance(raw, list) else []:
        try:
            x0, y0, x1, y1 = [float(t) for t in it["box"][:4]]
        except Exception:
            continue
        x0, x1 = sorted((float(np.clip(x0, 0, 1)), float(np.clip(x1, 0, 1))))
        y0, y1 = sorted((float(np.clip(y0, 0, 1)), float(np.clip(y1, 0, 1))))
        if (x1 - x0) * (y1 - y0) < 1e-5 or (x1 - x0) * (y1 - y0) > 0.25:
            continue
        label = str(it.get("label") or "Item")[:80]
        # cars and people are the photographer's to remove, never the AI's (his rule)
        if re.search(r"(car|cars|vehicle|vehicles|truck|bakkie|van|person|people|man|woman|child|kid)s?", label, re.I):
            continue
        out.append({"label": label, "box": [x0, y0, x1, y1]})
    out = out[:40]
    if out:
        try:
            out = _refine_boxes(base_rgb(video_id, path, 3200), out)
        except Exception as e:
            print(f"ai_photo: clutter close-ups skipped: {e}", flush=True)
    return {"items": out}


REFINE_PROMPT = (
    "Each picture is a close-up cut from one property photo, around one thing to tidy away (named below). "
    "For each picture give a tight box round the WHOLE of that thing - every part of it, nothing of the furniture or floor "
    "round it - in 0..1 of that close-up: x0, y0 (top left), x1, y1 (bottom right). If the thing is not in its close-up, "
    'give null for it. Answer as {"boxes":[[x0,y0,x1,y1] or null, ...]} in the same order as the pictures.'
)


def _refine_boxes(rgb: np.ndarray, items: List[dict]) -> List[dict]:
    """Claude's boxes on the whole photo are often loose or a little off (a cable half outside its box), and a
    removal stays inside the item's box - so each item is looked at again in a close-up and boxed there."""
    H, W = rgb.shape[:2]
    crops, wins = [], []
    for it in items:
        x0, y0, x1, y1 = it["box"]
        # room round the first box: Claude's first box can be off by more than its own size on small things
        mx, my = max(0.9 * (x1 - x0), 0.07), max(0.9 * (y1 - y0), 0.07 * W / H)
        cx0, cy0, cx1, cy1 = max(0.0, x0 - mx), max(0.0, y0 - my), min(1.0, x1 + mx), min(1.0, y1 + my)
        crop = rgb[int(cy0 * H):max(int(cy0 * H) + 2, int(cy1 * H)), int(cx0 * W):max(int(cx0 * W) + 2, int(cx1 * W))]
        crops.append(ai.grid_b64(crop, 768))
        wins.append((cx0, cy0, cx1, cy1))
    labels = "; ".join(f"picture {i + 1}: {it['label']}" for i, it in enumerate(items))
    d = ai.ask_json(REFINE_PROMPT + " The pictures, in order - " + labels + "." + ai.GRID_NOTE,
                    crops, "", max_tokens=2000, temperature=0.0)
    boxes = d.get("boxes") if isinstance(d, dict) else None
    if not isinstance(boxes, list) or len(boxes) != len(items):
        return items
    out = []
    for it, b, (cx0, cy0, cx1, cy1) in zip(items, boxes, wins):
        try:
            bx0, by0, bx1, by1 = [float(np.clip(float(t), 0, 1)) for t in b[:4]]
        except Exception:
            out.append(it)          # not found in the close-up, or no answer: the first box stays
            continue
        bx0, bx1 = sorted((bx0, bx1))
        by0, by1 = sorted((by0, by1))
        if (bx1 - bx0) * (by1 - by0) < 1e-4:
            out.append(it)
            continue
        cw, ch = cx1 - cx0, cy1 - cy0
        nb = [cx0 + bx0 * cw, cy0 + by0 * ch, cx0 + bx1 * cw, cy0 + by1 * ch]
        # only a correction of the first box: a close-up with three frames in it can come back boxing all three
        ob = it["box"]
        ix = max(0.0, min(ob[2], nb[2]) - max(ob[0], nb[0])) * max(0.0, min(ob[3], nb[3]) - max(ob[1], nb[1]))
        a_o, a_n = (ob[2] - ob[0]) * (ob[3] - ob[1]), (nb[2] - nb[0]) * (nb[3] - nb[1])
        # a box that moved is fine (the first one is often beside the thing) as long as it stays about the
        # same size and near: its centre within about twice the thing's size of the first one's
        ocx, ocy, ncx, ncy = (ob[0] + ob[2]) / 2, (ob[1] + ob[3]) / 2, (nb[0] + nb[2]) / 2, (nb[1] + nb[3]) / 2
        near = abs(ncx - ocx) < 2.0 * max(ob[2] - ob[0], 0.03) and abs(ncy - ocy) < 2.0 * max(ob[3] - ob[1], 0.03)
        ok = (ix > 0.25 * min(a_o, a_n) or near) and 0.15 * a_o < a_n < 2.5 * a_o
        out.append({**it, "box": nb} if ok else it)
    return out


class DeclutterBody(BaseModel):
    boxes: List[List[float]] = Field(..., min_length=1, max_length=40)
    removes: List[pe.RemoveArea] = Field(default_factory=list, max_length=200)


@router.post("/{video_id}/declutter")
def declutter(video_id: int, body: DeclutterBody, db: Session = Depends(get_db),
              current_user: User = Depends(get_current_user)):
    """Each ticked item traced (Segment Anything when it is here) and filled
    from its surroundings by the Remove tool's engine - one patch per item,
    so any one can be taken back."""
    import cv2
    import remove_ai
    v = _video(db, video_id)
    path = _path(v)
    work = pe._read_rgb(path, max_dim=4000)
    wh, ww = work.shape[:2]
    patches, failed = [], 0
    with pe._remove_lock:
        full = pe._full_u8(path)
        H, W = full.shape[:2]
        done = list(body.removes)
        for b in body.boxes:
            try:
                x0, y0, x1, y1 = [float(t) for t in b[:4]]
            except Exception:
                failed += 1
                continue
            # SAM gets the box a little bigger than Claude's, so an edge of the item just outside it is traced too
            ex, ey = (x1 - x0) * 0.1, (y1 - y0) * 0.1
            m = box_mask(path, work, [max(0.0, x0 - ex), max(0.0, y0 - ey), min(1.0, x1 + ex), min(1.0, y1 + ey)]) if _sam_ready() else None
            # the item's region in full-size pixels, with room around it
            bw, bh = (x1 - x0) * W, (y1 - y0) * H
            g = max(bw, bh) * 0.3 + 6
            X0, Y0 = int(max(0, x0 * W - g)), int(max(0, y0 * H - g))
            X1, Y1 = int(min(W, x1 * W + g)), int(min(H, y1 * H + g))
            if X1 - X0 < 4 or Y1 - Y0 < 4:
                failed += 1
                continue
            if m is not None:
                part = m[int(Y0 * wh / H):max(int(Y0 * wh / H) + 1, int(Y1 * wh / H)), int(X0 * ww / W):max(int(X0 * ww / W) + 1, int(X1 * ww / W))]
                mk = cv2.resize(part, (X1 - X0, Y1 - Y0), interpolation=cv2.INTER_LINEAR) > 0.4
                # only what is inside the item's own box (grown by a quarter - Claude's box often cuts off a lid or a
                # corner, and what is left of an item the fill copies back in): when the trace follows the table or
                # the counter it stands on, the rest of that surface must not go with it
                inbox = np.zeros_like(mk)
                gx = gy = int(min(bw, bh) * 0.25) + 4       # by the short side: a long row of frames must not reach far
                inbox[max(0, int(y0 * H) - Y0 - gy):int(y1 * H) - Y0 + gy, max(0, int(x0 * W) - X0 - gx):int(x1 * W) - X0 + gx] = True
                spill = (mk & ~inbox).sum()
                mk = mk & inbox
                # a trace that spills far past the box, or fills all of it, is the surface, not the item: point at
                # the item instead, and failing that take the box with its corners rounded off
                if spill > 0.6 * max(1, mk.sum()) or mk.sum() > 0.92 * inbox.sum():
                    pm = item_mask(path, work, [x0, y0, x1, y1])
                    if pm is not None:
                        pp = pm[int(Y0 * wh / H):max(int(Y0 * wh / H) + 1, int(Y1 * wh / H)), int(X0 * ww / W):max(int(X0 * ww / W) + 1, int(X1 * ww / W))]
                        pk = cv2.resize(pp, (X1 - X0, Y1 - Y0), interpolation=cv2.INTER_LINEAR) > 0.4
                        if (pk & ~inbox).sum() <= 0.4 * max(1, (pk & inbox).sum()) and 0.5 * inbox.sum() < (pk & inbox).sum() < 0.9 * inbox.sum():
                            mk = pk & inbox
                            spill = 0
                if spill > 0.6 * max(1, mk.sum()) or mk.sum() > 0.92 * inbox.sum():
                    # the box itself with its corners rounded (an oval round it reached a quarter past a long, thin
                    # box - a row of frames took the chairs and vases in front of it)
                    rr = max(1, int(min(bw, bh) * 0.25))
                    mk8 = np.zeros(inbox.shape, np.uint8)
                    cv2.rectangle(mk8, (int(x0 * W) - X0 + rr, int(y0 * H) - Y0 + rr), (int(x1 * W) - X0 - rr, int(y1 * H) - Y0 - rr), 1, -1)
                    mk = cv2.dilate(mk8, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * rr + 1, 2 * rr + 1))) > 0
                # the trace without its dents: whatever is left of an item next to the hole (a box's lid the trace
                # missed) the fill grows back into the whole thing
                if mk.any():
                    cnt, _ = cv2.findContours(mk.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                    hull = np.zeros(mk.shape, np.uint8)
                    cv2.fillPoly(hull, [cv2.convexHull(np.vstack(cnt))], 1)
                    mk = (hull > 0) & inbox
                if mk.sum() < 0.08 * bw * bh:
                    mk = None
            else:
                mk = None
            if mk is None:
                mk = np.zeros((Y1 - Y0, X1 - X0), bool)
                mk[int(y0 * H) - Y0:int(y1 * H) - Y0, int(x0 * W) - X0:int(x1 * W) - X0] = True
            grow = max(5, int(min(bw, bh) * 0.2))
            mk = cv2.dilate(mk.astype(np.uint8), np.ones((grow, grow), np.uint8)) > 0
            # the fill needs context around the item: a window about three times its size
            c = int(max(X1 - X0, Y1 - Y0) * 1.2) + 32
            cx0, cy0, cx1, cy1 = max(0, X0 - c), max(0, Y0 - c), min(W, X1 + c), min(H, Y1 + c)
            win = full[cy0:cy1, cx0:cx1].astype(np.float32) / 255.0
            if done:
                win = pe.apply_removes(win, done, window=(cx0, cy0, W, H))
            wm = np.zeros(win.shape[:2], bool)
            wm[Y0 - cy0:Y1 - cy0, X0 - cx0:X1 - cx0] = mk
            try:
                patch, (px0, py0, px1, py1), _engine = remove_ai.fill(win, wm, feather_px=max(2.0, grow * 0.8))
            except ValueError:
                failed += 1
                continue
            ax0, ay0, ax1, ay1 = px0 + cx0, py0 + cy0, px1 + cx0, py1 + cy0
            name = "rm" + uuid.uuid4().hex[:14]
            bgra = cv2.cvtColor((np.clip(patch, 0, 1) * 255 + 0.5).astype(np.uint8), cv2.COLOR_RGBA2BGRA)
            cv2.imwrite(str(pe.mask_dir(video_id) / f"{name}.png"), bgra)
            p = {"ref": f"{video_id}/{name}", "box": [ax0 / W, ay0 / H, (ax1 - ax0) / W, (ay1 - ay0) / H], "kind": "remove"}
            patches.append(p)
            done.append(pe.RemoveArea(**p))
    return {"patches": patches, "failed": failed, "model": remove_ai.status()}


# --------------------------------------------------------------------------
# match a look, develop a set like this one, sort into rooms
# --------------------------------------------------------------------------

from datetime import datetime

ROOM_TYPES = ["Exterior", "Aerial", "Entrance", "Lounge", "Dining", "Kitchen", "Scullery", "Bedroom", "Bathroom",
              "Study", "Family room", "Patio", "Garden", "Pool", "Garage", "Laundry", "View", "Detail"]

GEO_KEYS = ("crop", "rotate", "flip_h", "flip_v", "straighten", "persp_v", "persp_h", "geo_scale", "distortion",
            "distortion2", "ca_r", "ca_b", "lens_vig", "lens_vig_k", "masks", "spots", "removes", "fixes",
            "wm_index", "wm_opacity", "wm_scale", "wm_position", "wm_margin", "dn_luma", "dn_colour", "dn_shadows",
            "defringe", "sharpen")


def _measured(path: str) -> dict:
    try:
        return {k: v for k, v in pe.measure_photo(path).items() if not k.startswith("_")}
    except Exception:
        return {}


def match_patch(ref_b64: str, vid: int, path: str, start: dict, want_room: bool = False):
    """Settings that give this photo the reference's finished look, solved
    from a sensible starting point for this photo's own light."""
    img = ai.jpeg_b64(developed(vid, path, start, 1024), 1024)
    prompt = (
        "The FIRST picture is the reference: a finished photo whose look we want. The SECOND is another photo, shown with "
        f"a neutral starting edit: {json.dumps(_non_default(start))} (temperature {start.get('temp_k', 6500)}K).\n\n{KNOBS}\n\n"
        "Give the settings that make the second photo look like it belongs with the first - the same brightness feel, "
        "white balance, contrast and colour - adapted to its own light (a dark bedroom needs more lift than a sunny lounge). "
        "Values are absolute, except temp_shift, which is relative to the second photo's temperature."
        + (f' Also say which room or view the second photo shows, one of: {", ".join(ROOM_TYPES)}.' if want_room else "")
        + ' Answer as {"settings":{...}' + (',"room":"..."' if want_room else "") + "}"
    )
    d = ai.ask_json(prompt, [ref_b64, img], SYSTEM, max_tokens=3000, temperature=0.2)
    if not isinstance(d, dict):
        raise HTTPException(status_code=502, detail="Claude's answer could not be read - try again.")
    patch = clean_patch(d.get("settings") or {}, start)
    room = str(d.get("room") or "").strip()
    return patch, (room if room in ROOM_TYPES else None)


def _ref_b64(db: Session, ref_id: int) -> str:
    ref = _video(db, ref_id)
    return ai.jpeg_b64(developed(ref_id, _path(ref), saved_recipe(db, ref_id), 1024), 1024)


class MatchBody(BaseModel):
    reference_id: int
    recipe: Dict[str, Any] = Field(default_factory=dict)


@router.post("/{video_id}/match")
def match(video_id: int, body: MatchBody, db: Session = Depends(get_db),
          current_user: User = Depends(get_current_user)):
    """This photo given the reference photo's look (the editor applies it)."""
    v = _video(db, video_id)
    path = _path(v)
    ref = _ref_b64(db, body.reference_id)
    cur = body.recipe or saved_recipe(db, video_id)
    start = {**pe.Recipe().model_dump(), **{k: cur[k] for k in GEO_KEYS if k in cur}, **_measured(path)}
    patch, _ = match_patch(ref, video_id, path, start)
    # the measured starting point counts as part of the look
    base = {k: start[k] for k in ("exposure", "contrast", "highlights", "shadows", "whites", "blacks", "temp_k", "tint") if k in start}
    return {"patch": {**base, **patch}}


def save_recipe(db: Session, vid: int, rec: dict):
    payload = json.dumps(pe.Recipe(**rec).model_dump())
    row = db.query(pe.PhotoEdit).filter(pe.PhotoEdit.video_id == vid).first()
    if row:
        row.recipe = payload
        row.updated_at = datetime.utcnow()
    else:
        db.add(pe.PhotoEdit(video_id=vid, recipe=payload, updated_at=datetime.utcnow()))
    db.commit()


_jobs: Dict[str, dict] = {}


def _job(kind: str, total: int, user: str) -> dict:
    jid = uuid.uuid4().hex[:12]
    j = {"id": jid, "kind": kind, "total": total, "done": 0, "failed": 0, "state": "running", "rooms": {}, "error": "",
         "user": user, "started": datetime.utcnow().isoformat()}
    _jobs[jid] = j
    for k in [k for k, x in _jobs.items() if x["state"] != "running"][:-20]:
        _jobs.pop(k, None)
    return j


class DevelopBody(BaseModel):
    reference_id: int
    video_ids: List[int] = Field(..., min_length=1, max_length=500)
    rooms_key: Optional[str] = Field(None, max_length=80)   # also sort them into this export-by-room plan


def _develop_run(j: dict, body: DevelopBody):
    from concurrent.futures import ThreadPoolExecutor
    from database import SessionLocal
    db = SessionLocal()
    try:
        ref = _ref_b64(db, body.reference_id)
    except Exception as e:
        j["state"], j["error"] = "error", getattr(e, "detail", str(e))
        db.close()
        return
    db.close()

    def one(vid: int):
        s = SessionLocal()
        try:
            v = s.query(Video).filter(Video.id == vid).first()
            p = pe._resolve(v.filepath) if v else None
            if not p or not os.path.exists(p):
                raise ValueError("missing")
            mine = saved_recipe(s, vid)
            start = {**pe.Recipe().model_dump(), **{k: mine[k] for k in GEO_KEYS if k in mine}, **_measured(p)}
            patch, room = match_patch(ref, vid, p, start, want_room=True)
            save_recipe(s, vid, {**start, **patch})
            if room:
                j["rooms"].setdefault(room, []).append(vid)
            j["done"] += 1
        except Exception as e:
            j["failed"] += 1
            print(f"ai_photo: develop-like failed on {vid}: {e}", flush=True)
        finally:
            s.close()

    ids = [i for i in dict.fromkeys(body.video_ids) if i != body.reference_id]
    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(one, ids))
    if body.rooms_key and j["rooms"]:
        _merge_rooms(body.rooms_key, j["rooms"], j.get("user") or "ai")
    j["state"] = "done"


def _merge_rooms(key: str, found: Dict[str, List[int]], user: str):
    """Add the photos to the export-by-room plan, in rooms named for them;
    photos already placed by hand stay where they are."""
    import rooms as rooms_mod
    from database import SessionLocal
    s = SessionLocal()
    try:
        plan = rooms_mod.get_plan(key, s, None)
        rs = [dict(r) for r in plan.get("rooms", [])]
        placed = {i for r in rs for i in r.get("ids", [])}
        for name in ROOM_TYPES:
            ids = [i for i in found.get(name, []) if i not in placed]
            if not ids:
                continue
            r = next((x for x in rs if x["name"].lower() == name.lower()), None)
            if not r:
                r = {"id": uuid.uuid4().hex[:8], "name": name, "ids": []}
                rs.append(r)
            r["ids"] = list(r.get("ids", [])) + ids
        body = rooms_mod.Plan(key=key, rooms=[rooms_mod.Room(**r) for r in rs], style=plan.get("style", "Bedroom1"))
        class _U:   # put_plan only reads the name
            username = user
        rooms_mod.put_plan(body, s, _U())
    finally:
        s.close()


@router.post("/develop-like")
def develop_like(body: DevelopBody, current_user: User = Depends(get_current_user)):
    """Develop a set like the reference: each photo solved on its own by
    Claude for the same finished look (and, with rooms_key, sorted into rooms).
    Runs in the background; poll /api/ai/photo/jobs/{id}."""
    if not ai.enabled():
        raise ai.AiOff("AI is off: an administrator adds an Anthropic API key in Manage > AI.")
    j = _job("develop", len(body.video_ids), current_user.username)
    threading.Thread(target=_develop_run, args=(j, body), daemon=True).start()
    return j


class SortBody(BaseModel):
    key: str = Field(..., max_length=80)
    video_ids: List[int] = Field(..., min_length=1, max_length=500)
    merge: bool = True      # False: only report (the export dialog places them itself)


def _sort_run(j: dict, body: SortBody):
    from database import SessionLocal
    ids = list(dict.fromkeys(body.video_ids))
    for i in range(0, len(ids), 12):
        chunk = ids[i:i + 12]
        s = SessionLocal()
        imgs, keep = [], []
        try:
            for vid in chunk:
                v = s.query(Video).filter(Video.id == vid).first()
                p = pe._resolve(v.filepath) if v else None
                if not p or not os.path.exists(p):
                    j["failed"] += 1
                    continue
                try:
                    imgs.append(ai.jpeg_b64(base_rgb(vid, p, 600), 512, 80))
                    keep.append(vid)
                except Exception:
                    j["failed"] += 1
        finally:
            s.close()
        if not keep:
            continue
        try:
            d = ai.ask_json(
                f"These are {len(keep)} photos of one property, in order. For each, say which room or view it shows, one of: "
                f'{", ".join(ROOM_TYPES)}. A photo that does not show part of a house or its grounds - a car, a boat, a '
                f'person, a product, a close-up of an object on its own - is "None": never guess a room for it. '
                f'Answer as {{"rooms":["...", ...]}} with exactly {len(keep)} entries in the same order.',
                imgs, "", max_tokens=800, temperature=0.0, tier="fast")
            names = d.get("rooms", []) if isinstance(d, dict) else d
            for vid, name in zip(keep, names if isinstance(names, list) else []):
                name = str(name).strip()
                if name in ROOM_TYPES:
                    j["rooms"].setdefault(name, []).append(vid)
                else:
                    j["failed"] += 1            # not a room (a car, say): left for you to place
            j["done"] += len(keep)
        except Exception as e:
            j["failed"] += len(keep)
            j["error"] = getattr(e, "detail", str(e))
    if j["rooms"] and body.merge:
        _merge_rooms(body.key, j["rooms"], j.get("user") or "ai")
    j["state"] = "done"


@router.post("/rooms/sort")
def sort_rooms(body: SortBody, current_user: User = Depends(get_current_user)):
    """Claude looks at each photo and puts it in the room it shows."""
    if not ai.enabled():
        raise ai.AiOff("AI is off: an administrator adds an Anthropic API key in Manage > AI.")
    j = _job("rooms", len(body.video_ids), current_user.username)
    threading.Thread(target=_sort_run, args=(j, body), daemon=True).start()
    return j


@router.get("/jobs/{job_id}")
def job(job_id: str, current_user: User = Depends(get_current_user)):
    j = _jobs.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="That job is not running any more")
    return j


# --------------------------------------------------------------------------
# generated looks: an image model repaints the edited photo (ai_image.py)
# --------------------------------------------------------------------------
#
# The editor does the correcting; a look is painted on top of that edit and
# kept as a layer (recipe.gen_*), so the sliders are never moved and the look
# can be faded or taken off. Claude, when it is on, reads each photo and
# turns the studio's look into an instruction written for that photo.

import ai_image

FRAME_KEYS = ("crop", "rotate", "flip_h", "flip_v", "straighten", "persp_v", "persp_h", "geo_scale", "distortion", "distortion2")

GEN_SYSTEM = ("You write instructions for an image-editing model that retouches real-estate and architecture photos for a "
              "photo studio. The model follows plain, concrete visual instructions. It must never move, add or remove "
              "anything in the scene unless asked; it changes light, colour, sky and window views.")


_DEFAULTS: Dict[str, Any] = {}


def frame_sig(rec: dict) -> str:
    if not _DEFAULTS:
        _DEFAULTS.update(pe.Recipe().model_dump())
    return json.dumps({k: rec.get(k, _DEFAULTS.get(k)) for k in FRAME_KEYS}, sort_keys=True)


PREVIEW_PIXELS = ai_image.GEN_PIXELS // 4      # a quick look: a quarter of the size, about a quarter of the cost


def gen_input(vid: int, path: str, recipe: dict, pixels: int = 0) -> np.ndarray:
    """The photo as edited (the look layer itself left out), at the model's size."""
    rec = {**(recipe or {}), "gen_ref": "", "gen_amount": 1.0, "gen_light": 1.0, "gen_view": 1.0}
    rgb = developed(vid, path, rec, 1600)
    u8 = (np.clip(rgb, 0, 1) * 255 + 0.5).astype(np.uint8)
    w, h = ai_image.fit_size(u8.shape[1], u8.shape[0], pixels or ai_image.GEN_PIXELS)
    return ai_image.resize(u8, w, h)


def _boxes(d) -> Optional[list]:
    out = []
    for b in (d or {}).get("outside") or [] if isinstance(d, dict) else []:
        try:
            x0, y0, x1, y1 = (min(1.0, max(0.0, float(v))) for v in b[:4])
        except Exception:
            continue
        if abs(x1 - x0) > 0.01 and abs(y1 - y0) > 0.01:
            out.append([round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)])
    return out or None


OUTSIDE_ASK = ('Also mark where the photo shows the outdoors seen from the camera: the sky, and the view through each window, '
               'glass door or open side (not the window frames around them, not walls, not reflections), as boxes '
               '[x0,y0,x1,y1] in 0..1 of the picture (x right, y down), one per window or sky area; [] if none. '
               'And say where the photo was taken: "outdoors" (the outside of a building, a street, a garden, a drone '
               'shot) or "indoors" (a room, looking out or not).')


def _scene(d) -> Optional[str]:
    x = str((d or {}).get("scene") or "").lower() if isinstance(d, dict) else ""
    return "outdoors" if x.startswith("out") else "indoors" if x.startswith("in") else None


def _whole_things(photo: np.ndarray, lost: np.ndarray) -> np.ndarray:
    """A lost shape is found where its edges were clearest (the top of an armchair); Segment Anything,
    given a point in it, finds the whole thing in the photo. Without the model, the shape as found."""
    import cv2
    import hashlib
    hard = (lost > 0.5).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(hard, 8)
    if n <= 1:
        return lost
    out = lost.copy()
    key = ("lost", hashlib.sha1(photo[::7, ::7].tobytes()).hexdigest())
    h, w = hard.shape
    for i in range(1, n):
        comp = (lab == i).astype(np.uint8)
        dist = cv2.distanceTransform(comp, cv2.DIST_L2, 5)
        y, x = np.unravel_index(int(dist.argmax()), dist.shape)
        try:
            import editor_ai
            if not editor_ai._have_files():
                continue
            sm = editor_ai.sam_click(photo, key, [(x / w, y / h)], [1], (h, w))
        except Exception as e:
            print(f"ai_photo: whole-thing mask skipped: {e}", flush=True)
            continue
        area = float((sm > 0.5).mean())
        over = float(((sm > 0.5) & (comp > 0)).sum()) / max(1, comp.sum())
        # the thing it found must hold the shape and not be the whole room (a wall, the floor)
        if 0.005 < area < 0.35 and over > 0.3:
            out = np.maximum(out, cv2.GaussianBlur(cv2.dilate((sm > 0.5).astype(np.float32), np.ones((9, 9), np.uint8)), (0, 0), 3))
    return out


def commit_keep(keep: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """A relight that keeps the room lines up with the photo, so more of the photo's own fine detail
    (carpet, fabric, wood grain) goes back over the painting - the painting is drawn smaller and
    softer than the photo. Where the fine structure really differs it still stays the painting's."""
    return np.clip(keep * 1.35 + 0.15, 0, 1) * valid


def guard_commit(g: np.ndarray, photo: np.ndarray, m: np.ndarray, rep: np.ndarray, valid: np.ndarray, made_up) -> tuple:
    """A look that keeps the model's light shows its picture everywhere it painted - except where it
    changed things: a thing it took away (the armchair in the foreground) or a light it made up (LED
    strips along the skirting). There the photo's own pixels stay, relit with the light round them."""
    import cv2
    lost = _whole_things(photo, ai_image.lost_shapes(g, photo))
    thin = None
    if made_up is not None:
        # only thin made-up lights (LED strips, a new fitting's line): a wide warm band across the
        # floor is the sun the look is for
        mu = made_up.astype(np.float32)
        k = max(3, int(0.008 * max(mu.shape)) | 1)
        thin = np.clip(mu - cv2.morphologyEx(mu, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))), 0, 1)
        # a made-up strip runs along a real edge of the photo (the skirting, a ceiling line); the edge
        # of a sun patch lies on plain floor, where the photo has none
        edges = cv2.Canny(cv2.cvtColor(photo, cv2.COLOR_RGB2GRAY), 40, 120)
        near = cv2.dilate(edges, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))).astype(np.float32) / 255
        thin = cv2.dilate(thin * near, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    sus = lost if thin is None else np.maximum(lost, cv2.GaussianBlur(thin, (0, 0), 2))
    rep = np.maximum(rep, valid.astype(np.float32) * (1 - np.clip(sus * 1.5, 0, 1)))
    # the light over a lost thing (wide, from all round it) and over a made-up strip and its glow
    # (narrow, from just beside it): not the floor or the glow the model drew there
    def fill(m, soft, sigma):
        mask = cv2.resize(soft, (m.shape[1], m.shape[0]))
        hard = (mask > 0.5).astype(np.float32)
        mf = m.astype(np.float32)
        s_ = sigma * max(m.shape[:2])
        num = cv2.GaussianBlur(mf * (1 - hard)[..., None], (0, 0), s_)
        den = cv2.GaussianBlur(1 - hard, (0, 0), s_)[..., None] + 1e-4
        return np.clip(mf * (1 - mask[..., None]) + (num / den) * mask[..., None] + 0.5, 0, 255).astype(np.uint8)
    if lost.max() > 0.05:
        m = fill(m, lost, 0.06)
    if thin is not None and thin.max() > 0.05:
        glow = cv2.GaussianBlur(cv2.dilate(thin, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))), (0, 0), 4)
        m = fill(m, np.clip(glow * 1.5, 0, 1), 0.012)
    return m, rep, sus


def relight_texture(g: np.ndarray, photo: np.ndarray, sus: np.ndarray, valid: np.ndarray) -> tuple:
    """The relight's light with the photo's own texture: the painting's broad light and colour (its
    sun patches, shadows, glow) under the photo's fine detail (the damask on a pillow, the blanket's
    weave), which the model smooths away when it relights. Where it draws its own picture on purpose
    (a view through a window, a sky) or changed a thing, the painting stays as it is.
    Returns the new painting and where the photo's texture went in (0..1)."""
    import cv2
    w = ai_image.shape_agree(g, photo) * (1 - np.clip(sus * 1.5, 0, 1)) * valid
    G, D = g.astype(np.float32) / 255, photo.astype(np.float32) / 255
    s_ = max(1.5, 0.0012 * max(g.shape[:2]))
    gl, dl = cv2.GaussianBlur(G, (0, 0), s_), cv2.GaussianBlur(D, (0, 0), s_)
    # where the painting has detail the photo never had (a view painted into a blown-out window, clouds
    # in a white sky) its own detail stays: the photo has nothing there to lay back
    L1 = np.array([0.2126, 0.7152, 0.0722], np.float32)
    eg = np.sqrt(cv2.GaussianBlur(((G - gl) @ L1) ** 2, (0, 0), 3 * s_))
    ed = np.sqrt(cv2.GaussianBlur(((D - dl) @ L1) ** 2, (0, 0), 3 * s_))
    w = w * np.clip(1 - (eg - 2.0 * ed - 0.004) / 0.008, 0, 1)
    w = cv2.GaussianBlur(w, (0, 0), 2 * s_)
    L = np.array([0.2126, 0.7152, 0.0722], np.float32)
    # the detail scaled by how much brighter or darker the relight made that spot
    r = np.clip((gl @ L + 0.02) / (dl @ L + 0.02), 0.1, 8.0)[..., None]
    t = gl + (D - dl) * r
    out = t * w[..., None] + G * (1 - w[..., None])
    return (np.clip(out, 0, 1) * 255 + 0.5).astype(np.uint8), w


def outside_boxes(photo: np.ndarray) -> tuple:
    """(the outdoors boxes, "outdoors"/"indoors"), for a look whose words are used as they are."""
    if not ai.enabled():
        return None, None
    try:
        d = ai.ask_json(OUTSIDE_ASK + ' Answer as {"outside":[[x0,y0,x1,y1],...],"scene":"outdoors|indoors"}',
                        [ai.jpeg_b64(photo, 1024)], "", max_tokens=400, temperature=0.0)
        return _boxes(d), _scene(d)
    except Exception:
        return None, None


def tailor(photo: np.ndarray, look_name: str, look_prompt: str, ask: str = "") -> tuple:
    """Claude turns the look into an instruction for this photo, and marks where
    the photo shows the outdoors. Without Claude the look's own words are used
    as they are (and nothing is marked)."""
    if not ai.enabled():
        return (look_prompt + (" " + ask if ask else "")).strip(), None, None
    q = (f'The studio\'s look "{look_name}": {look_prompt}\n\n'
         + (f'The photographer also asked: "{ask}".\n\n' if ask else "")
         + "Write the instruction for the image-editing model for THIS photo. Name what is in it (the room or view, the main "
           "surfaces and materials, the windows, the sky if any) and say exactly how each should look in this look. Adapt the "
           "look sensibly when part of it does not apply (no sky in frame, no windows, an interior at twilight). When the look "
           "turns lights on, name the lights that are really in this photo (which windows, lamps, fittings) and say only those "
           "are lit - no new ones. Keep it under "
           '110 words, plain and visual.\n\n' + OUTSIDE_ASK +
           ' Answer as {"prompt":"...","outside":[[x0,y0,x1,y1],...],"scene":"outdoors|indoors"}')
    try:
        d = ai.ask_json(q, [ai.jpeg_b64(photo, 1024)], GEN_SYSTEM, max_tokens=1000, temperature=0.4, tier="smart")
        p = str((d or {}).get("prompt") or "").strip() if isinstance(d, dict) else ""
        return p or look_prompt, _boxes(d), _scene(d)
    except ai.AiOff:
        return look_prompt, None, None


class GenBody(BaseModel):
    video_ids: List[int] = Field(..., min_length=1, max_length=500)
    look_id: Optional[str] = Field(None, max_length=20)
    look_ids: List[str] = Field(default_factory=list, max_length=12)   # several looks, one after another
    prompt: Optional[str] = Field(None, max_length=2000)   # words of its own instead of a saved look
    ask: str = Field("", max_length=600)                   # a rework: "less orange, keep the lawn green"
    recipe: Optional[Dict[str, Any]] = None                # the editor's unsaved edit (one photo)
    apply: bool = False                                    # save it onto each photo (batch runs)
    seed: Optional[int] = Field(None, ge=0, le=2 ** 31)
    variations: int = Field(1, ge=1, le=4)
    # virtual staging instead of a look: one result per style
    stage_styles: List[str] = Field(default_factory=list, max_length=8)
    stage_empty: bool = False
    stage_changes: List[str] = Field(default_factory=list, max_length=8)
    stage_words: str = Field("", max_length=600)
    stage_label: bool = True
    # a quick, small painting to judge the look by (about a quarter of the cost); "Paint it
    # full size" then paints it again at the full size with the same seed
    preview: bool = False
    pipeline: Optional[str] = Field(None, max_length=40)     # the pipeline run that asked, if any


def _words(look: dict, body: GenBody, photo: np.ndarray) -> tuple:
    """(the words for the model, the outdoors boxes, "outdoors"/"indoors")"""
    base = look.get("prompt", "")
    if look.get("tailor", True) or body.ask:
        return tailor(photo, look.get("name", ""), base, body.ask)
    return (base, *outside_boxes(photo)) if look.get("real", True) else (base, None, None)


COMMIT_MIN = 0.6          # a finishing model's relight must keep the photo's layout this well to be used (good ones: 0.9+)


def _gen_one(db, j: dict, body: GenBody, vid: int, look: dict, examples: list, rec: dict, photo: np.ndarray, words):
    import cv2
    words, outside, scene = (tuple(words) + (None, None))[:3] if isinstance(words, tuple) else (words, None, None)
    prompt = ai_image.look_prompt({**look, "prompt": words}, len(examples))
    model = "" if body.preview else ai_image.finish_model(look, ai_image.is_outdoors(scene, outside))
    commit = look.get("blend") == "commit" and not look.get("stage")
    if model and "seedream" not in model and photo.shape[0] * photo.shape[1] > ai_image.FINISH_PIXELS * 1.1:
        # Nano Banana Pro draws at 2K (4K costs double): the photo at its size
        w_, h_ = ai_image.fit_size(photo.shape[1], photo.shape[0], ai_image.FINISH_PIXELS)
        photo = ai_image.resize(photo, w_, h_)
    import ai_usage
    for n in range(body.variations):
        seed = body.seed if body.seed is not None else look.get("seed")
        seed = int(seed) + n if seed is not None else random.randint(1, 2 ** 31 - 1)
        ai_usage.context.set({"video_id": vid, "look": look.get("name", ""), "user": j.get("user"),
                              "kind": "stage" if look.get("stage") else ("preview" if body.preview else "look")})
        g, valid = ai_image.edit_aligned(photo, prompt, examples, seed, model)
        real = look.get("real", True) and not look.get("stage")
        score = ai_image.match_score(g, photo)
        ev = ai_image.wants_evening(look.get("prompt", ""))
        # (a finishing model costs more: no second painting; its result is shown as it came)
        if real and not model and (score < ai_image.MATCH_MIN or ai_image.missed_look(g, photo, ev)):
            # the model drew a different picture (moved the mountain, the houses): once more,
            # and the better of the two is kept
            ai_usage.context.set({**ai_usage.context.get(), "kind": "retry"})
            seed2 = random.randint(1, 2 ** 31 - 1)
            g2, v2 = ai_image.edit_aligned(photo, prompt, examples, seed2)
            s2 = ai_image.match_score(g2, photo)
            bad1 = score < ai_image.MATCH_MIN or ai_image.missed_look(g, photo, ev)
            bad2 = s2 < ai_image.MATCH_MIN or ai_image.missed_look(g2, photo, ev)
            if (bad1 and not bad2) or (bad1 == bad2 and s2 > score):
                g, valid, score, seed = g2, v2, s2, seed2
        if model and real:
            # a finishing model redraws the room and shifts parts of it: lined up point by point
            g, valid = ai_image.local_align(g, photo, valid)
            score = ai_image.match_score(g, photo)
        if model and real and score < COMMIT_MIN:
            # a finishing model that drew another room (wider, reframed, a second window) instead of
            # relighting this one: Seedream is tried once more as Nano Banana Pro, which keeps the
            # framing; if that misses too nothing is kept - never a painting laid over the wrong room
            if "seedream" in model:
                ai_usage.context.set({**ai_usage.context.get(), "kind": "retry"})
                m2 = ai_image.FINISH_MODELS["nbpro"]
                g2, v2 = ai_image.edit_aligned(photo, prompt, examples, seed, m2)
                g2, v2 = ai_image.local_align(g2, photo, v2)
                s2 = ai_image.match_score(g2, photo)
                if s2 > score:
                    g, valid, score, model = g2, v2, s2, m2
            if score < COMMIT_MIN:
                raise HTTPException(status_code=422, detail="The model drew a different room instead of relighting this one, so "
                                    "nothing was kept. Paint it again, or try another look.")
        loose = bool(real and score < ai_image.MATCH_MIN)
        name = "gen" + uuid.uuid4().hex[:12]
        d = pe.mask_dir(vid)
        # four pictures: the painted one (g), the edit it came from (d), the light
        # the look brings (m, log2 ratio) and, in k, where the photo's own detail
        # may go back in (red) and where the painted picture may show (green)
        if look.get("label"):
            g = ai_image.label_staged(g)
        keep = ai_image.detail_keep(g, photo) * valid
        if commit:
            keep = commit_keep(keep, valid)
        ex_: dict = {}
        m, rep_ = ai_image.light_maps(g, photo, keep, valid, real=look.get("real", True), outside=outside,
                                      outdoors=ai_image.is_outdoors(scene, outside),
                                      neutral=ai_image.wants_neutral(look.get("prompt", "")),
                                      evening=ai_image.wants_evening(look.get("prompt", "")), loose=loose,
                                      blend="commit" if commit else look.get("blend", "detail"), extra=ex_)
        if commit:
            # the model's light is the look: its picture shows wherever it did not change things
            m, rep_, sus_ = guard_commit(g, photo, m, rep_, valid, ex_.get("made_up"))
            raw_g = g
            g, tex = relight_texture(g, photo, sus_, valid)
            keep = np.maximum(keep, tex)
        m = hi_light(vid, _path(_video(db, vid)), rec, m, photo, ex_.get("sky"))
        # BGR: B where the model painted, G where its picture shows, R where the photo's detail goes back
        k8 = (np.dstack([valid, rep_, keep]) * 255 + 0.5).astype(np.uint8)
        g = ai_image.extend_edges(g, valid)          # the sky runs to the frame's edge
        for end, img in (("g", cv2.cvtColor(g, cv2.COLOR_RGB2BGR)), ("d", cv2.cvtColor(photo, cv2.COLOR_RGB2BGR)),
                         ("m", cv2.cvtColor(m, cv2.COLOR_RGB2BGR)), ("k", k8)):
            if not cv2.imwrite(str(d / f"{name}{end}.png"), img):
                raise HTTPException(status_code=500, detail="Could not save the look")
        if commit:
            # the model's own picture, kept so the texture can be worked out again later
            cv2.imwrite(str(d / f"{name}r.png"), cv2.cvtColor(ai_image.extend_edges(raw_g, valid), cv2.COLOR_RGB2BGR))
        item = {"video_id": vid, "ref": f"{vid}/{name}", "look": look.get("name", ""), "look_id": look.get("id"),
                "prompt": words, "geo": frame_sig(rec), "made": datetime.utcnow().isoformat(), "stage": look.get("stage"),
                "maps": MAPS_VERSION, "outside": outside, "scene": scene, "match": round(score, 3), "loose": loose,
                "seed": seed, "preview": bool(body.preview), "pipeline": body.pipeline, "commit": commit,
                "model": model or None}
        # a note beside the pictures, so the photo's looks are listed again next time
        (d / f"{name}.json").write_text(json.dumps(item))
        j["items"].append(item)


def _gen_run(j: dict, body: GenBody):
    from database import SessionLocal
    db = SessionLocal()
    try:
        ids = body.look_ids or ([body.look_id] if body.look_id else [])
        if body.stage_styles:
            looks = [{**ai_image.stage_look(s_, body.stage_empty, body.stage_changes, body.stage_words), "label": body.stage_label,
                      "stage": {"stage_styles": [s_], "stage_empty": body.stage_empty, "stage_changes": body.stage_changes,
                                "stage_words": body.stage_words, "stage_label": body.stage_label}}
                     for s_ in body.stage_styles]
        elif ids:
            looks = [ai_image.get_look(i) for i in ids]
        else:
            looks = [{"id": None, "name": "My words", "prompt": body.prompt or "", "tailor": True}]
        for vid in body.video_ids:
            try:
                v = _video(db, vid)
                path = _path(v)
                rec = body.recipe if (body.recipe is not None and len(body.video_ids) == 1) else saved_recipe(db, vid)
                photo = gen_input(vid, path, rec, PREVIEW_PIXELS if body.preview else 0)
                # a look with a finishing model (a quick look never uses one: it is only to judge by) gets a bigger photo
                big = None
                if not body.preview and any((l.get("model") or "default") != "default" for l in looks) and ai_image.backend() == "fal":
                    big = gen_input(vid, path, rec, ai_image.SEEDREAM_PIXELS)
                # Claude writes every look's instruction at once; the card then paints them in turn
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=min(4, len(looks))) as pool:
                    words = list(pool.map(lambda l: _words(l, body, photo), looks))
                for look, w in zip(looks, words):
                    # a look's example photos are other properties: shown them, the model copied their layout
                    # (a twilight came back as a different house, garden and camera) and only the light could be
                    # used. The look's words carry the finish; the examples stay the look's picture in the lists.
                    ex = ai_image.look_examples(look) if look.get("id") and look.get("send_examples") else []
                    fine = big is not None and (look.get("model") or "default") != "default"
                    _gen_one(db, j, body, vid, look, ex, rec, big if fine else photo, w)
                if body.apply and j["items"]:
                    it = j["items"][-1]
                    cur = saved_recipe(db, vid)
                    cur.update(gen_ref=it["ref"], gen_amount=1.0, gen_light=1.0, gen_view=1.0, gen_look=it["look"][:80],
                               gen_geo=it["geo"])
                    save_recipe(db, vid, cur)
                j["done"] += 1
            except ai.AiOff as e:
                j["failed"] += 1
                j["error"] = str(e)
                break
            except HTTPException as e:
                j["failed"] += 1
                j["error"] = str(e.detail)
                if "not running" in str(e.detail) or "missing" in str(e.detail):
                    break
            except Exception as e:
                j["failed"] += 1
                j["error"] = f"{type(e).__name__}: {e}"
                print(f"ai_photo: look failed on {vid}: {e}", flush=True)
        j["state"] = "error" if j["failed"] and not j["done"] else "done"
    finally:
        db.close()


MAPS_VERSION = 17          # how the light and where-it-paints maps are worked out; older ones are redone
_remapping: set = set()


def _remap(refs: List[str]):
    for ref in refs:
        try:
            _rebuild_maps(ref)
        except Exception as e:
            print(f"ai_photo: could not redo the maps of {ref}: {e}", flush=True)
        finally:
            _remapping.discard(ref)


class GenMapsBody(BaseModel):
    ref: str = Field(..., pattern=r"^\d+/gen[a-f0-9]{12}$")


@router.post("/gen-maps")
def gen_maps(body: GenMapsBody, current_user: User = Depends(get_current_user)):
    """Work out a painted look's light and where-it-paints maps again from its
    two pictures (after the way they are worked out got better) - no repaint."""
    _rebuild_maps(body.ref)
    return {"ok": True}


HI_LIGHT = 3200      # the light map's long edge: lined up with the photo at about this size


def hi_light(vid: int, path: str, rec: Optional[dict], m: np.ndarray, photo: np.ndarray, sky=None) -> np.ndarray:
    """The light map at up to HI_LIGHT px, following the full photo's own edges (ai_image.upsample_light).
    Stretched from the painting's size, a twilight's light ran past every edge at full size: a pale
    rim round loungers, the pool's cyan on the coping. Any trouble: the map as it was."""
    try:
        hi = developed(vid, path, {**(rec or {}), "gen_ref": "", "gen_amount": 1.0}, HI_LIGHT)
        hi8 = (np.clip(hi, 0, 1) * 255 + 0.5).astype(np.uint8)
        if abs(hi8.shape[1] / hi8.shape[0] - photo.shape[1] / photo.shape[0]) > 0.01 or max(hi8.shape[:2]) <= max(m.shape[:2]):
            return m
        return ai_image.upsample_light(m, photo, hi8, sky=sky)
    except Exception as e:
        print(f"[looks] light map left at the painting's size: {e}", flush=True)
        return m


def _same_frame(a: str, b: str) -> bool:
    """Two frame_sig strings the same framing (one may say 0 where the other says 0.0)."""
    try:
        ja, jb = json.loads(a), json.loads(b)
        return ja.keys() == jb.keys() and all(json.dumps(ja[k]) == json.dumps(jb[k]) or
                                              np.allclose(np.asarray(ja[k], float), np.asarray(jb[k], float), atol=1e-6)
                                              for k in ja)
    except Exception:
        return a == b


def _rebuild_maps(ref: str):
    import cv2
    import shutil
    vid, name = ref.split("/")
    d = pe.mask_dir(int(vid))
    g = cv2.imread(str(d / f"{name}r.png"), cv2.IMREAD_COLOR)       # a kept relight: the model's own picture
    if g is None:
        g = cv2.imread(str(d / f"{name}g.png"), cv2.IMREAD_COLOR)
    p = cv2.imread(str(d / f"{name}d.png"), cv2.IMREAD_COLOR)
    if g is None or p is None:
        raise HTTPException(status_code=404, detail="That result is gone - make it again")
    g, p = cv2.cvtColor(g, cv2.COLOR_BGR2RGB), cv2.cvtColor(p, cv2.COLOR_BGR2RGB)
    try:
        if json.loads((d / f"{name}.json").read_text()).get("model"):
            g, _ = ai_image.local_align(g, p)          # results from before it was lined up point by point
    except Exception:
        pass
    real = True
    note = {}
    look_words, blend = "", "detail"
    try:
        note = json.loads((d / f"{name}.json").read_text())
        lid = note.get("look_id")
        if lid:
            lk = ai_image.get_look(lid)
            real, look_words, blend = lk.get("real", True), lk.get("prompt", ""), lk.get("blend", "detail")
    except Exception:
        pass
    kk = cv2.imread(str(d / f"{name}k.png"), cv2.IMREAD_COLOR)
    valid = ai_image.painted_area(g, p, kk)
    keep = ai_image.detail_keep(g, p) * valid
    if note.get("commit") or blend == "commit":
        keep = commit_keep(keep, valid)
    if note and "outside" not in note and real:
        note["outside"], note["scene"] = outside_boxes(p)      # a result from before the outdoors was marked
    score = ai_image.match_score(g, p)
    loose = bool(real and not note.get("stage") and score < ai_image.MATCH_MIN)
    if note:
        note["match"], note["loose"] = round(score, 3), loose
    ex_: dict = {}
    m, rep_ = ai_image.light_maps(g, p, keep, valid, real=real, outside=note.get("outside"), loose=loose,
                                  outdoors=ai_image.is_outdoors(note.get("scene"), note.get("outside")),
                                  neutral=ai_image.wants_neutral(look_words),
                                  evening=ai_image.wants_evening(look_words),
                                  blend="commit" if (note.get("commit") or blend == "commit") else blend, extra=ex_)
    tex_g = None
    if note.get("commit") or blend == "commit":
        if not (d / f"{name}r.png").is_file():
            cv2.imwrite(str(d / f"{name}r.png"), cv2.cvtColor(g, cv2.COLOR_RGB2BGR))
        m, rep_, sus_ = guard_commit(g, p, m, rep_, valid, ex_.get("made_up"))
        tex_g, tex = relight_texture(g, p, sus_, valid)
        keep = np.maximum(keep, tex)
    # the light lined up with the full-size photo, when the photo is still framed as it was painted
    from database import SessionLocal
    db = SessionLocal()
    try:
        v = db.query(Video).filter(Video.id == int(vid)).first()
        rec = saved_recipe(db, int(vid))
        if v and (not note.get("geo") or _same_frame(frame_sig(rec), note.get("geo"))):
            m = hi_light(int(vid), _path(v), rec, m, p, ex_.get("sky"))
    finally:
        db.close()
    k8 = (np.dstack([valid, rep_, keep]) * 255 + 0.5).astype(np.uint8)
    if tex_g is not None:
        cv2.imwrite(str(d / f"{name}g.png"), cv2.cvtColor(ai_image.extend_edges(tex_g, valid), cv2.COLOR_RGB2BGR))
    elif (valid < 0.5).any():
        cv2.imwrite(str(d / f"{name}g.png"), cv2.cvtColor(ai_image.extend_edges(g, valid), cv2.COLOR_RGB2BGR))
    cv2.imwrite(str(d / f"{name}m.png"), cv2.cvtColor(m, cv2.COLOR_RGB2BGR))
    cv2.imwrite(str(d / f"{name}k.png"), k8)
    for end in "gdmk":
        pe._gen_cache.pop(f"{ref}{end}", None)
    if note:
        note["maps"] = MAPS_VERSION
        (d / f"{name}.json").write_text(json.dumps(note))
    if os.environ.get("ZK_TEST_COPY"):
        # the test copy leaves a copy where a tester on the Windows side can look at it
        out = pe.Path(__file__).resolve().parent / ".zk-test" / "gens"
        out.mkdir(parents=True, exist_ok=True)
        for end in "gdmk":
            shutil.copy2(d / f"{name}{end}.png", out / f"{vid}_{name}{end}.png")


@router.get("/{video_id}/gens")
def gens(video_id: int, current_user: User = Depends(get_current_user)):
    """The looks painted for this photo that are still on disk, newest first."""
    d = pe.mask_dir(video_id)
    out = []
    for f in sorted(d.glob("gen*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:40]:
        if not (d / f"{f.stem}g.png").is_file() or not (d / f"{f.stem}d.png").is_file():
            continue
        try:
            it = json.loads(f.read_text())
            if it.get("model") and (it.get("match") or 0) < COMMIT_MIN:
                continue          # a finishing model that drew another room (before that was caught)
            out.append(it)
        except Exception:
            continue
    # results made before the maps got better are redone in the background (no repaint)
    old = [x["ref"] for x in out if x.get("ref") and (x.get("maps") or 1) < MAPS_VERSION and x["ref"] not in _remapping]
    if old:
        _remapping.update(old)
        threading.Thread(target=_remap, args=(old,), daemon=True).start()
    return {"items": out}


@router.post("/gen")
def gen(body: GenBody, current_user: User = Depends(get_current_user)):
    """Paint a look onto one photo (the editor) or many (a batch, saved onto each)."""
    if not ai_image.enabled():
        raise ai_image.ImageOff("Image generation is off: set it up in Manage > AI > Image generation.")
    if not body.look_id and not body.look_ids and not body.stage_styles and not (body.prompt or "").strip():
        raise HTTPException(status_code=400, detail="Choose a look, or say what the look should be")
    for i in body.look_ids or ([body.look_id] if body.look_id else []):
        ai_image.get_look(i)
    j = _job("gen", len(body.video_ids), current_user.username)
    j["items"] = []
    threading.Thread(target=_gen_run, args=(j, body), daemon=True).start()
    return j


@router.get("/look-results/{look_id}")
def look_results(look_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """The latest photos painted in this look that are still on disk (for Manage > Looks: keep one as the
    look's example). Found through the paintings log, newest first."""
    import ai_usage
    look = ai_image.get_look(look_id)
    rows = (db.query(ai_usage.AiUsage.video_id).filter(ai_usage.AiUsage.look == (look.get("name") or "")[:80],
                                                       ai_usage.AiUsage.video_id.isnot(None))
            .order_by(ai_usage.AiUsage.at.desc()).limit(300).all())
    vids = list(dict.fromkeys(r.video_id for r in rows))[:40]
    out = []
    for vid in vids:
        d = pe.mask_dir(vid)
        for f in sorted(d.glob("gen*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                x = json.loads(f.read_text())
            except Exception:
                continue
            if x.get("look_id") == look_id and (d / f"{f.stem}g.png").is_file() and not x.get("preview"):
                out.append({"video_id": vid, "ref": x.get("ref"), "at": f.stat().st_mtime})
                break
        if len(out) >= 12:
            break
    return {"items": out}


@router.get("/made")
def made(limit: int = 200, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Everything painted lately, newest first, across all photos (the Looks tab's Everything you made): each
    result with its photo's name and whether it is the look on the photo now. Found through the paintings log."""
    import ai_usage
    rows = (db.query(ai_usage.AiUsage.video_id).filter(ai_usage.AiUsage.video_id.isnot(None))
            .order_by(ai_usage.AiUsage.at.desc()).limit(3000).all())
    vids = list(dict.fromkeys(r.video_id for r in rows))[:300]
    names = {v.id: v.filename for v in db.query(Video).filter(Video.id.in_(vids)).all()} if vids else {}
    on = {}
    if vids:
        for r in db.query(pe.PhotoEdit.video_id, pe.PhotoEdit.recipe).filter(pe.PhotoEdit.video_id.in_(vids)).all():
            try:
                on[r.video_id] = json.loads(r.recipe or "{}").get("gen_ref") or ""
            except ValueError:
                pass
    out = []
    for vid in vids:
        if vid not in names:
            continue
        d = pe.mask_dir(vid)
        for f in d.glob("gen*.json"):
            try:
                x = json.loads(f.read_text())
            except Exception:
                continue
            if x.get("preview") or not (d / f"{f.stem}g.png").is_file():
                continue
            if x.get("model") and (x.get("match") or 0) < COMMIT_MIN:
                continue
            x["filename"] = names[vid]
            x["on_photo"] = on.get(vid) == x.get("ref")
            x.setdefault("made", datetime.utcfromtimestamp(f.stat().st_mtime).isoformat())
            out.append(x)
    out.sort(key=lambda x: x.get("made") or "", reverse=True)
    return {"items": out[:max(1, min(limit, 500))]}


class GenExampleBody(BaseModel):
    ref: str = Field(..., pattern=r"^\d+/gen[a-f0-9]{12}$")
    look_id: str = Field(..., max_length=20)


@router.post("/gen-example")
def gen_example(body: GenExampleBody, current_user: User = Depends(get_current_user)):
    """Keep a result you like as one of the look's examples, so later photos come out like it."""
    import cv2
    vid, name = body.ref.split("/")
    bgr = cv2.imread(str(pe.mask_dir(int(vid)) / f"{name}g.png"), cv2.IMREAD_COLOR)
    if bgr is None:
        raise HTTPException(status_code=404, detail="That result is gone - make it again")
    return ai_image.add_example(body.look_id, cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
