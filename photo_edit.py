"""The develop engine behind the photo editor.

The browser does the interactive part: it draws a downscaled preview on the
GPU and re-renders it as you drag a slider. This module is the other half -
it serves that preview, remembers what you dialled in, and renders the full
resolution file when you export.

The maths here and the shader in the browser are deliberately the same
sequence, in the same order, in linear light:

    geometry              (lens, keystone, straighten, rotate, crop)
    sRGB -> linear
      white balance (temperature in kelvin, tint)
      per-channel gain
      exposure
      shadows / highlights, on a luminance mask
      contrast around an 0.18 pivot
    linear -> sRGB
      per-channel lift
      whites / blacks
      saturation, vibrance
      noise reduction       (5x5, edge-aware on luma, flat on colour)
      clarity, dehaze       (one big blur, sized to the image)
      tone curves           (master, then per channel)
      sharpening            (export size; the preview shows it approximately)

Anything that changes that order has to change in both places, or what you
export stops matching what you saw. There is a parity test for exactly that.

Nothing is destructive: the recipe is stored per photo, the original is only
ever read, and exports are written as new files.
"""

import json
import math
import re
import shutil
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
from fastapi import UploadFile, File, APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.orm import Session

import previews
from auth import get_current_user, get_user_from_token
from database import Base, IndexedFolder, SessionLocal, User, Video, engine, get_db
from media_type_utils import get_media_type

router = APIRouter(prefix="/api/photo-edit", tags=["photo-edit"])
# A prefix of its own: /api/photo-edit/{video_id} would swallow anything named
# here and then fail to read "presets" as a photo id.
presets_router = APIRouter(prefix="/api/photo-presets", tags=["photo-edit"])
match_router = APIRouter(prefix="/api/photo-match", tags=["photo-edit"])
# Likewise: a batch job id is not a photo id, so it gets its own prefix rather
# than sitting under /api/photo-edit/ where "/{video_id}" would swallow it.
batch_router = APIRouter(prefix="/api/photo-edit-batch", tags=["photo-edit"])

_media_root: Optional[Path] = None
_resolve = lambda p: p
_preview_lock = threading.Lock()

PREVIEW_W = 2000            # what the editor draws on
MAX_EXPORT = 20000


class PhotoEdit(Base):
    __tablename__ = "photo_edits"
    video_id = Column(Integer, primary_key=True)
    recipe = Column(String, nullable=False, default="{}")
    updated_at = Column(DateTime, default=datetime.utcnow)
    # 1 picked, -1 rejected, 0 or null undecided. Stars live on the photo
    # itself (Video.rating) because the library already filters on those;
    # a pick is a working decision that belongs with the editing.
    pick = Column(Integer, nullable=True)


class Recipe(BaseModel):
    exposure: float = Field(0, ge=-5, le=5)        # stops
    contrast: float = Field(0, ge=-1, le=1)
    highlights: float = Field(0, ge=-1, le=1)
    shadows: float = Field(0, ge=-1, le=1)
    whites: float = Field(0, ge=-0.5, le=0.5)
    blacks: float = Field(0, ge=-0.5, le=0.5)
    # White balance as a colour temperature, the way a camera states it.
    # 6500K is neutral: higher warms the photo, lower cools it, which is the
    # direction every other editor moves in.
    temp_k: float = Field(6500, ge=2000, le=15000)
    tint: float = Field(0, ge=-1, le=1)            # - green, + magenta
    saturation: float = Field(0, ge=-1, le=1)
    vibrance: float = Field(0, ge=-1, le=1)
    # Per-hue saturation - the colour mixer. Eight bands, 45 degrees apart,
    # with a soft falloff so a colour sitting between two bands is shared
    # between them rather than jumping.
    sat_red: float = Field(0, ge=-1, le=1)
    sat_orange: float = Field(0, ge=-1, le=1)
    sat_yellow: float = Field(0, ge=-1, le=1)
    sat_green: float = Field(0, ge=-1, le=1)
    sat_aqua: float = Field(0, ge=-1, le=1)
    sat_blue: float = Field(0, ge=-1, le=1)
    sat_purple: float = Field(0, ge=-1, le=1)
    sat_magenta: float = Field(0, ge=-1, le=1)
    # Watermark. The PNG itself lives in MEDIA_ROOT/_watermarks; these say
    # which one, how strong, how big and where. Opacity 0 means none, so the
    # feature costs nothing until it is switched on.
    wm_index: int = Field(0, ge=0, le=20)        # 0 = the first one on disk
    wm_opacity: float = Field(0, ge=0, le=1)
    wm_scale: float = Field(0.22, ge=0.03, le=1.0)   # width, as a fraction
    wm_position: int = Field(8, ge=0, le=8)          # 0..8, reading order
    wm_margin: float = Field(0.03, ge=0, le=0.25)
    sharpen: float = Field(0, ge=0, le=2)
    # Per-channel trims. Gain multiplies in linear light (a colour cast in the
    # midtones and up), lift adds in the shadows (where a cast is most visible
    # on white walls).
    r_gain: float = Field(1, ge=0.5, le=2)
    g_gain: float = Field(1, ge=0.5, le=2)
    b_gain: float = Field(1, ge=0.5, le=2)
    r_lift: float = Field(0, ge=-0.2, le=0.2)
    g_lift: float = Field(0, ge=-0.2, le=0.2)
    b_lift: float = Field(0, ge=-0.2, le=0.2)
    # Local contrast. Clarity works on the midtones; dehaze pulls the blacks
    # down and puts contrast and colour back into a flat, hazy frame.
    # Purple and green fringing on high-contrast edges - roof lines against a
    # bright sky, window frames. The most visible artefact in drone work.
    defringe: float = Field(0, ge=0, le=1)
    clarity: float = Field(0, ge=-1, le=1)
    dehaze: float = Field(0, ge=-0.5, le=1)
    # Tone curves as control points in 0..1, master first then per channel.
    # Two points (the corners) means "do nothing".
    curve: List[List[float]] = Field(default_factory=lambda: [[0.0, 0.0], [1.0, 1.0]])
    curve_r: List[List[float]] = Field(default_factory=lambda: [[0.0, 0.0], [1.0, 1.0]])
    curve_g: List[List[float]] = Field(default_factory=lambda: [[0.0, 0.0], [1.0, 1.0]])
    curve_b: List[List[float]] = Field(default_factory=lambda: [[0.0, 0.0], [1.0, 1.0]])
    # Noise. Luma smooths grain while keeping edges; colour kills the red and
    # green speckle that high ISO leaves in shadows.
    dn_luma: float = Field(0, ge=0, le=1)
    dn_colour: float = Field(0, ge=0, le=1)
    # Geometry, applied before anything else: lens distortion, then keystone,
    # then straightening, then the crop.
    distortion: float = Field(0, ge=-0.5, le=0.5)
    persp_v: float = Field(0, ge=-1, le=1)
    persp_h: float = Field(0, ge=-1, le=1)
    straighten: float = Field(0, ge=-45, le=45)      # degrees
    geo_scale: float = Field(1, ge=1, le=3)          # zoom in to hide the edges
    rotate: int = Field(0, ge=0, le=270)             # 0 / 90 / 180 / 270
    flip_h: bool = False
    flip_v: bool = False
    crop: List[float] = Field(default_factory=lambda: [0.0, 0.0, 1.0, 1.0])


# --------------------------------------------------------------------------
# the maths (mirrored by the shader in PhotoEditor.jsx)
# --------------------------------------------------------------------------

LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
PIVOT = np.float32(0.18)


def _srgb_to_linear(c: np.ndarray) -> np.ndarray:
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4).astype(np.float32)


def _linear_to_srgb(c: np.ndarray) -> np.ndarray:
    c = np.clip(c, 0, None)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * (c ** (1 / 2.4)) - 0.055).astype(np.float32)


def _smoothstep(a: float, b: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - a) / (b - a), 0.0, 1.0).astype(np.float32)
    return (t * t * (3.0 - 2.0 * t)).astype(np.float32)


def kelvin_rgb(k: float):
    """The colour a black body glows at k kelvin, normalised to 0..1.

    Tanner Helland's approximation - close enough over 2000-15000K, and short
    enough to write identically in Python and in GLSL, which is what keeps the
    editor and the export agreeing with each other.
    """
    import math
    t = max(1000.0, min(40000.0, float(k))) / 100.0
    if t <= 66.0:
        r = 255.0
        g = 99.4708025861 * math.log(t) - 161.1195681661
    else:
        r = 329.698727446 * ((t - 60.0) ** -0.1332047592)
        g = 288.1221695283 * ((t - 60.0) ** -0.0755148492)
    if t >= 66.0:
        b = 255.0
    elif t <= 19.0:
        b = 0.0
    else:
        b = 138.5177312231 * math.log(t - 10.0) - 305.0447927307
    clamp = lambda v: max(0.0, min(255.0, v)) / 255.0
    return [clamp(r), clamp(g), clamp(b)]


NEUTRAL_K = 6500.0


def wb_gains(temp_k: float, tint: float):
    """Channel gains for a temperature and tint, at constant brightness.

    Dividing by the light's own colour is what makes a HIGHER number warm the
    photo: you are telling it the scene was lit by bluer light, so it
    compensates the other way. Same convention as every camera raw editor.
    """
    ref = kelvin_rgb(NEUTRAL_K)
    cur = kelvin_rgb(temp_k)
    g = [ref[i] / max(cur[i], 1e-4) for i in range(3)]
    m = 1.0 + 0.10 * tint
    g = [g[0] * m, g[1] * (1.0 - 0.20 * tint), g[2] * m]
    # Keep the exposure where it was: only the colour should move.
    y = 0.2126 * g[0] + 0.7152 * g[1] + 0.0722 * g[2]
    if y > 1e-6:
        g = [v / y for v in g]
    return np.array(g, dtype=np.float32)



# --------------------------------------------------------------------------
# tone curves
# --------------------------------------------------------------------------

LUT_N = 256


def curve_lut(points) -> np.ndarray:
    """A 256-entry lookup table through the control points.

    Monotone cubic (Fritsch-Carlson): it passes through every point without
    the overshoot a plain spline gives, so dragging one point cannot make a
    dip somewhere else. The browser builds the identical table and uploads it
    as a texture, which is what keeps the curve honest between screen and
    export.
    """
    pts = sorted([(float(x), float(y)) for x, y in (points or [])], key=lambda p: p[0])
    if len(pts) < 2:
        pts = [(0.0, 0.0), (1.0, 1.0)]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    n = len(pts)

    # slopes
    d = []
    for i in range(n - 1):
        dx = max(1e-6, xs[i + 1] - xs[i])
        d.append((ys[i + 1] - ys[i]) / dx)
    m = [d[0]] + [(d[i - 1] + d[i]) / 2.0 for i in range(1, n - 1)] + [d[n - 2]]
    for i in range(n - 1):
        if abs(d[i]) < 1e-9:
            m[i] = m[i + 1] = 0.0
        else:
            a = m[i] / d[i]
            b = m[i + 1] / d[i]
            t = a * a + b * b
            if t > 9.0:
                k = 3.0 / (t ** 0.5)
                m[i] = k * a * d[i]
                m[i + 1] = k * b * d[i]

    out = np.empty(LUT_N, dtype=np.float32)
    seg = 0
    for i in range(LUT_N):
        x = i / (LUT_N - 1.0)
        while seg < n - 2 and x > xs[seg + 1]:
            seg += 1
        if x <= xs[0]:
            v = ys[0]
        elif x >= xs[-1]:
            v = ys[-1]
        else:
            h = max(1e-6, xs[seg + 1] - xs[seg])
            t = (x - xs[seg]) / h
            t2, t3 = t * t, t * t * t
            v = ((2 * t3 - 3 * t2 + 1) * ys[seg]
                 + (t3 - 2 * t2 + t) * h * m[seg]
                 + (-2 * t3 + 3 * t2) * ys[seg + 1]
                 + (t3 - t2) * h * m[seg + 1])
        out[i] = min(1.0, max(0.0, v))
    # 8-bit, because that is what the shader samples - same numbers both sides
    return (np.round(out * 255.0) / 255.0).astype(np.float32)


def _is_identity(points) -> bool:
    pts = [(round(float(x), 4), round(float(y), 4)) for x, y in (points or [])]
    return len(pts) <= 2 and all(abs(x - y) < 1e-4 for x, y in pts)


def _apply_lut(c: np.ndarray, lut: np.ndarray) -> np.ndarray:
    """Sample the table with linear interpolation, exactly as GL_LINEAR does."""
    x = np.clip(c, 0.0, 1.0) * (LUT_N - 1)
    i0 = np.floor(x).astype(np.int32)
    i1 = np.minimum(i0 + 1, LUT_N - 1)
    f = (x - i0).astype(np.float32)
    return (lut[i0] * (1.0 - f) + lut[i1] * f).astype(np.float32)


# --------------------------------------------------------------------------
# the big blur that clarity and dehaze are built on
# --------------------------------------------------------------------------

BLUR_TAPS = 4          # each side of centre, so 9 samples per direction


def blur_plan(width: int, height: int):
    """Tap spacing and weights, sized to the image.

    Both sides work the radius out from the image's own size, so clarity looks
    the same on the 2000px preview as it does on the full-resolution export -
    which it would not if the radius were a fixed number of pixels.
    """
    import math
    longest = max(width, height)
    sigma = max(2.0, 0.006 * longest)
    step = max(1, int(round(sigma / 2.0)))
    w = [math.exp(-0.5 * ((i * step) / sigma) ** 2) for i in range(-BLUR_TAPS, BLUR_TAPS + 1)]
    total = sum(w)
    return step, [v / total for v in w]


def _big_blur(img: np.ndarray) -> np.ndarray:
    import cv2
    h, w = img.shape[:2]
    step, weights = blur_plan(w, h)
    k = np.zeros(2 * BLUR_TAPS * step + 1, dtype=np.float32)
    for i, wt in enumerate(weights):
        k[i * step] = wt
    return cv2.sepFilter2D(img, -1, k, k, borderType=cv2.BORDER_REFLECT_101)



# --------------------------------------------------------------------------
# geometry: lens, keystone, straighten, crop
# --------------------------------------------------------------------------

def has_geometry(r: "Recipe") -> bool:
    c = list(r.crop or [0, 0, 1, 1])
    cropped = (abs(c[0]) > 1e-6 or abs(c[1]) > 1e-6
               or abs(c[2] - 1) > 1e-6 or abs(c[3] - 1) > 1e-6)
    return bool(cropped or r.distortion or r.persp_v or r.persp_h
                or r.straighten or r.rotate or abs(r.geo_scale - 1) > 1e-6
                or getattr(r, "flip_h", False) or getattr(r, "flip_v", False))


def source_uv(u: np.ndarray, v: np.ndarray, r: "Recipe", aspect: float):
    """Where each output pixel comes from in the source, in 0..1 uv.

    Written the same way as the shader's geometry() so the export lands on the
    same pixels. Order matters: undo the lens first, then the keystone, then
    straighten, and read the crop last - that is the order a camera and a
    tripod put the errors in, backwards.
    """
    import math
    cx, cy, cw, ch = (list(r.crop) + [0, 0, 1, 1])[:4]
    # into the cropped window, then to centred coordinates
    p_x = cx + u * cw
    p_y = cy + v * ch
    x = (p_x - 0.5) * aspect
    y = (p_y - 0.5)

    # Mirroring is a sign flip on the way in, before anything else reads the
    # coordinates - so a flipped photo straightens and crops the way it looks.
    if getattr(r, "flip_h", False):
        x = -x
    if getattr(r, "flip_v", False):
        y = -y

    if r.rotate:
        t = math.radians(float(r.rotate))
        cs, sn = math.cos(t), math.sin(t)
        x, y = x * cs - y * sn, x * sn + y * cs

    s = max(1e-6, float(r.geo_scale))
    x = x / s
    y = y / s

    if r.straighten:
        t = math.radians(float(r.straighten))
        cs, sn = math.cos(t), math.sin(t)
        x, y = x * cs - y * sn, x * sn + y * cs

    if r.persp_v or r.persp_h:
        denom = 1.0 + float(r.persp_v) * y + float(r.persp_h) * x
        denom = np.where(np.abs(denom) < 1e-3, np.sign(denom) * 1e-3 + 1e-9, denom)
        x = x / denom
        y = y / denom

    if r.distortion:
        r2 = x * x + y * y
        f = 1.0 + float(r.distortion) * r2
        x = x * f
        y = y * f

    return (x / aspect + 0.5).astype(np.float32), (y + 0.5).astype(np.float32)


def apply_geometry(img: np.ndarray, r: "Recipe") -> np.ndarray:
    """Resample the photo through source_uv(). Outside the frame reads black."""
    import cv2
    h, w = img.shape[:2]
    cw = max(1e-3, float((list(r.crop) + [0, 0, 1, 1])[2]))
    ch = max(1e-3, float((list(r.crop) + [0, 0, 1, 1])[3]))
    out_w = max(1, int(round(w * cw)))
    out_h = max(1, int(round(h * ch)))

    u = (np.arange(out_w, dtype=np.float32) + 0.5) / out_w
    v = (np.arange(out_h, dtype=np.float32) + 0.5) / out_h
    uu, vv = np.meshgrid(u, v)
    su, sv = source_uv(uu, vv, r, aspect=w / float(h))

    map_x = (su * w - 0.5).astype(np.float32)
    map_y = (sv * h - 0.5).astype(np.float32)
    return cv2.remap(img, map_x, map_y, interpolation=cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))


# --------------------------------------------------------------------------
# noise
# --------------------------------------------------------------------------

DN_TAPS = 2          # 5x5


def _ycocg(c: np.ndarray):
    y = 0.25 * c[..., 0] + 0.5 * c[..., 1] + 0.25 * c[..., 2]
    co = c[..., 0] - c[..., 2]
    cg = c[..., 1] - 0.5 * (c[..., 0] + c[..., 2])
    return y.astype(np.float32), co.astype(np.float32), cg.astype(np.float32)


def _from_ycocg(y, co, cg):
    t = y - 0.5 * cg
    g = y + 0.5 * cg
    b = t - 0.5 * co
    r = t + 0.5 * co
    return np.stack([r, g, b], axis=-1).astype(np.float32)


def denoise(c: np.ndarray, luma: float, colour: float) -> np.ndarray:
    """Edge-aware smoothing, the same 5x5 the shader walks.

    Luma keeps an edge by weighting each neighbour on how close its brightness
    is; colour just averages, because chroma noise has no detail worth saving
    and blurring it is what actually clears the red-green speckle.
    """
    if not luma and not colour:
        return c
    y, co, cg = _ycocg(c)
    pad = DN_TAPS
    yp = np.pad(y, pad, mode="reflect")
    cop = np.pad(co, pad, mode="reflect")
    cgp = np.pad(cg, pad, mode="reflect")
    h, w = y.shape

    sr = float(0.004 + 0.046 * (1.0 - luma)) if luma else 1.0
    y_acc = np.zeros_like(y); y_wsum = np.zeros_like(y)
    co_acc = np.zeros_like(co); cg_acc = np.zeros_like(cg); c_wsum = np.zeros_like(co)

    for dy in range(-DN_TAPS, DN_TAPS + 1):
        for dx in range(-DN_TAPS, DN_TAPS + 1):
            sy = yp[pad + dy:pad + dy + h, pad + dx:pad + dx + w]
            spatial = np.float32(np.exp(-0.5 * ((dx * dx + dy * dy) / (1.6 ** 2))))
            if luma:
                wgt = spatial * np.exp(-0.5 * ((sy - y) / sr) ** 2).astype(np.float32)
                y_acc += sy * wgt
                y_wsum += wgt
            if colour:
                co_acc += cop[pad + dy:pad + dy + h, pad + dx:pad + dx + w] * spatial
                cg_acc += cgp[pad + dy:pad + dy + h, pad + dx:pad + dx + w] * spatial
                c_wsum += spatial

    if luma:
        y = y * (1.0 - luma) + (y_acc / np.maximum(y_wsum, 1e-6)) * luma
    if colour:
        co = co * (1.0 - colour) + (co_acc / np.maximum(c_wsum, 1e-6)) * colour
        cg = cg * (1.0 - colour) + (cg_acc / np.maximum(c_wsum, 1e-6)) * colour
    return np.clip(_from_ycocg(y, co, cg), 0.0, 1.0)


def watermark_dir() -> Path:
    """Where the logos live. A plain folder rather than an upload UI, so a PNG
    can be dropped in from Explorer and used immediately."""
    d = (_media_root or Path(".")) / "_watermarks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def watermark_files() -> list:
    return sorted([p for p in watermark_dir().glob("*.png") if p.is_file()],
                  key=lambda p: p.name.lower())


_WM_CACHE = {}


def _load_watermark(path: Path):
    """RGBA as floats, cached - a logo is reused across a whole shoot and
    decoding it per photo would dominate the render."""
    key = (str(path), path.stat().st_mtime_ns)
    hit = _WM_CACHE.get(key)
    if hit is not None:
        return hit
    from PIL import Image
    img = Image.open(path).convert("RGBA")
    arr = np.asarray(img).astype(np.float32) / 255.0
    _WM_CACHE.clear()          # one logo at a time is the normal case
    _WM_CACHE[key] = arr
    return arr


def _apply_watermark(s: np.ndarray, r: "Recipe") -> np.ndarray:
    files = watermark_files()
    if not files:
        return s
    wm = _load_watermark(files[min(int(r.wm_index), len(files) - 1)])

    h, w = s.shape[:2]
    target_w = max(8, int(round(w * float(r.wm_scale))))
    scale = target_w / wm.shape[1]
    target_h = max(8, int(round(wm.shape[0] * scale)))
    if target_w >= w or target_h >= h:
        target_w = min(target_w, w); target_h = min(target_h, h)

    # Nearest-neighbour would alias a logo badly; PIL does the resampling.
    from PIL import Image
    small = np.asarray(
        Image.fromarray((np.clip(wm, 0, 1) * 255).astype(np.uint8), "RGBA")
             .resize((target_w, target_h), Image.LANCZOS)
    ).astype(np.float32) / 255.0

    m = float(r.wm_margin)
    mx, my = int(round(w * m)), int(round(h * m))
    col = int(r.wm_position) % 3
    row = int(r.wm_position) // 3
    x = (mx if col == 0 else (w - target_w) // 2 if col == 1 else w - target_w - mx)
    y = (my if row == 0 else (h - target_h) // 2 if row == 1 else h - target_h - my)
    x = max(0, min(w - target_w, x))
    y = max(0, min(h - target_h, y))

    rgb = small[..., :3]
    alpha = (small[..., 3:4] * float(r.wm_opacity)).astype(np.float32)
    patch = s[y:y + target_h, x:x + target_w, :]
    s = s.copy()
    s[y:y + target_h, x:x + target_w, :] = patch * (1.0 - alpha) + rgb * alpha
    return s


def apply_recipe(rgb01: np.ndarray, r: Recipe) -> np.ndarray:
    """rgb01: float32 HxWx3 in 0..1 sRGB. Returns the same, developed."""
    if has_geometry(r):
        rgb01 = apply_geometry(rgb01, r)
    lin = _srgb_to_linear(rgb01)

    lin = lin * wb_gains(r.temp_k, r.tint)
    if r.r_gain != 1 or r.g_gain != 1 or r.b_gain != 1:
        lin = lin * np.array([r.r_gain, r.g_gain, r.b_gain], dtype=np.float32)
    if r.exposure:
        lin = lin * np.float32(2.0 ** r.exposure)

    if r.shadows or r.highlights:
        y = np.clip(lin @ LUMA, 0, 4).astype(np.float32)[..., None]
        if r.shadows:
            mask = (1.0 - _smoothstep(0.0, 0.25, y)).astype(np.float32)
            lin = lin * (1.0 + np.float32(r.shadows) * mask)
        if r.highlights:
            mask = _smoothstep(0.35, 1.0, y)
            lin = lin * (1.0 + np.float32(r.highlights) * mask)

    if r.contrast:
        f = np.float32(1.0 + r.contrast)
        lin = PIVOT * np.power(np.clip(lin, 1e-6, None) / PIVOT, f)

    s = _linear_to_srgb(lin)

    if r.r_lift or r.g_lift or r.b_lift:
        lift = np.array([r.r_lift, r.g_lift, r.b_lift], dtype=np.float32)
        s = s + lift * (1.0 - _smoothstep(0.0, 0.6, s))

    if r.whites:
        s = s + np.float32(r.whites) * _smoothstep(0.5, 1.0, s)
    if r.blacks:
        s = s + np.float32(r.blacks) * (1.0 - _smoothstep(0.0, 0.5, s))

    if r.saturation or r.vibrance:
        lum = (s @ LUMA)[..., None].astype(np.float32)
        if r.saturation:
            s = lum + (s - lum) * np.float32(1.0 + r.saturation)
        if r.vibrance:
            sat = (s.max(axis=-1) - s.min(axis=-1))[..., None].astype(np.float32)
            s = lum + (s - lum) * (1.0 + np.float32(r.vibrance) * (1.0 - np.clip(sat, 0, 1)))

    s = np.clip(s, 0.0, 1.0)

    # Colour mixer: saturation of one hue family at a time. Property work
    # needs this constantly - grass and pool blue want lifting while skin and
    # terracotta roofs do not, and a global saturation slider cannot tell them
    # apart.
    HUE_BANDS = (("sat_red", 0.0), ("sat_orange", 30.0), ("sat_yellow", 60.0),
                 ("sat_green", 120.0), ("sat_aqua", 180.0), ("sat_blue", 240.0),
                 ("sat_purple", 280.0), ("sat_magenta", 320.0))
    band_values = [(centre, float(getattr(r, name, 0.0) or 0.0))
                   for name, centre in HUE_BANDS]
    if any(v for _, v in band_values):
        mx = s.max(axis=-1)
        mn = s.min(axis=-1)
        chroma = mx - mn
        # Hue in degrees. Grey pixels have no hue, and the chroma weight below
        # keeps them out of it anyway.
        rr, gg, bb = s[..., 0], s[..., 1], s[..., 2]
        safe = np.where(chroma < 1e-6, 1.0, chroma)
        hue = np.where(mx == rr, ((gg - bb) / safe) % 6.0,
              np.where(mx == gg, (bb - rr) / safe + 2.0,
                                 (rr - gg) / safe + 4.0)) * 60.0
        hue = np.where(chroma < 1e-6, 0.0, hue).astype(np.float32)

        # Only pixels with real colour are affected, and the effect eases in.
        strength = np.clip(chroma * 4.0, 0.0, 1.0).astype(np.float32)

        mult = np.ones_like(hue, dtype=np.float32)
        for centre, amount in band_values:
            if not amount:
                continue
            d = np.abs(((hue - centre + 180.0) % 360.0) - 180.0)   # wrap at 360
            w = np.clip(1.0 - d / 45.0, 0.0, 1.0)                  # soft falloff
            w = (w * w * (3.0 - 2.0 * w)).astype(np.float32)       # smoothstep
            mult += np.float32(amount) * w * strength

        lum_m = (s @ LUMA)[..., None].astype(np.float32)
        s = np.clip(lum_m + (s - lum_m) * mult[..., None], 0.0, 1.0)

    if r.dn_luma or r.dn_colour:
        s = denoise(s, float(r.dn_luma), float(r.dn_colour))

    # Local contrast, both built on one big blur of the developed image.
    if r.clarity or r.dehaze:
        base = _big_blur(s)
        if r.clarity:
            detail = s - base
            # Midtones only: clarity on a white wall or a black shadow just
            # makes halos.
            y = (s @ LUMA)[..., None].astype(np.float32)
            mid = (1.0 - np.abs(y - 0.5) * 2.0).astype(np.float32)
            s = np.clip(s + np.float32(r.clarity) * 0.8 * detail * mid, 0.0, 1.0)
        if r.dehaze:
            d = np.float32(r.dehaze)
            black = 0.12 * d
            s = np.clip((s - black) / max(1e-3, 1.0 - black), 0.0, 1.0)
            s = np.clip(s + 0.5 * d * (s - base), 0.0, 1.0)
            lum = (s @ LUMA)[..., None].astype(np.float32)
            s = np.clip(lum + (s - lum) * (1.0 + 0.3 * d), 0.0, 1.0)

    # Tone curves: master, then per channel. If any of the four is doing
    # something, all four run - including the flat ones. The shader cannot
    # skip individual curves, and a flat curve still rounds to 8 bits, so
    # skipping here would leave the export a hair off the screen.
    curves = (r.curve, r.curve_r, r.curve_g, r.curve_b)
    if any(not _is_identity(c) for c in curves):
        s = _apply_lut(s, curve_lut(r.curve))
        for n, pts in enumerate((r.curve_r, r.curve_g, r.curve_b)):
            s[..., n] = _apply_lut(s[..., n], curve_lut(pts))

    if r.defringe:
        # Fringing is chroma that only exists where luminance changes fast, and
        # only in the magenta/violet and green corners of the wheel. Find those
        # two conditions together and pull the colour out, leaving everything
        # that is genuinely purple or green alone.
        d = np.float32(r.defringe)
        y = (s @ LUMA).astype(np.float32)
        gx = np.abs(np.diff(y, axis=1, prepend=y[:, :1]))
        gy = np.abs(np.diff(y, axis=0, prepend=y[:1, :]))
        edge = np.clip((gx + gy) * 6.0, 0.0, 1.0)

        mx = s.max(axis=2)
        mn = s.min(axis=2)
        chroma = mx - mn
        rr, gg, bb = s[..., 0], s[..., 1], s[..., 2]
        # magenta/violet: blue and red both above green
        violet = np.clip(np.minimum(rr, bb) - gg, 0.0, None)
        # green: green above both
        green = np.clip(gg - np.maximum(rr, bb), 0.0, None)
        suspect = np.clip((violet + green) * 4.0, 0.0, 1.0)

        amount = (edge * suspect * np.clip(chroma * 3.0, 0.0, 1.0) * d)[..., None]
        lum3 = y[..., None]
        s = np.clip(s * (1.0 - amount) + lum3 * amount, 0.0, 1.0)

    if r.sharpen:
        import cv2
        blur = cv2.GaussianBlur(s, (0, 0), 1.0)
        s = np.clip(s + np.float32(r.sharpen) * (s - blur), 0.0, 1.0)

    if r.wm_opacity > 0:
        try:
            s = _apply_watermark(s, r)
        except Exception as e:
            print(f"photo_edit: watermark failed: {e}", flush=True)

    return np.clip(s, 0.0, 1.0).astype(np.float32)


# --------------------------------------------------------------------------
# metadata
# --------------------------------------------------------------------------

def carry_exif(src: str, dst: str) -> bool:
    """Copy the source photo's EXIF onto an exported JPEG.

    OpenCV writes pixels and nothing else, so an export used to arrive with no
    capture date, no camera, and no copyright line - which matters when the
    file is a deliverable rather than a working copy.

    Two tags are deliberately rewritten rather than copied: orientation,
    because any rotation is already baked into the pixels and repeating it
    would turn the photo on its side, and software, because this file was
    developed here and saying so is honest.
    """
    try:
        from PIL import Image
    except Exception:
        return False
    try:
        with Image.open(src) as im:
            exif = im.getexif()
        if not exif:
            return False
        exif[274] = 1                              # orientation: already applied
        exif[305] = "Zerko"                        # software
        with Image.open(dst) as out:
            out.save(dst, exif=exif.tobytes(), quality="keep"
                     if dst.lower().endswith((".jpg", ".jpeg")) else None)
        return True
    except Exception as e:
        print(f"  [export] could not carry the metadata across: {e}", flush=True)
        return False


# --------------------------------------------------------------------------
# reading and writing
# --------------------------------------------------------------------------

def _read_rgb(path: str, max_dim: int = 0, strict: bool = False) -> np.ndarray:
    """Any photo as float32 RGB 0..1, optionally capped on the long edge.

    strict=True refuses to fall back to the small preview buried in a RAW
    file - right for an export, where handing back a soft upscaled version of
    a 45MP frame would be worse than an honest error.
    """
    import cv2
    ext = Path(path).suffix.lower()
    img = None
    if ext in previews.RAW_EXT:
        try:
            import rawpy
            with rawpy.imread(path) as raw:
                img = raw.postprocess(use_camera_wb=True, no_auto_bright=True, output_bps=8)
        except Exception:
            img = None
        if img is None and strict:
            raise HTTPException(
                status_code=415,
                detail="No RAW decoder is installed, so this file can only be "
                       "read at preview size. Install one with: "
                       "venv/bin/pip install rawpy")
    # Never cv2.imread a RAW: OpenCV reads DNG as a plain TIFF and returns
    # undemosaiced sensor data, which looks far worse than the preview.
    if img is None and ext not in previews.RAW_EXT:
        bgr = cv2.imread(path, cv2.IMREAD_COLOR)
        if bgr is not None:
            img = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    if img is None:
        tmp = str(Path(path).with_suffix("")) + f".edittmp-{uuid.uuid4().hex[:6]}.jpg"
        ok, why = previews.make_image_jpeg(path, tmp, width=max_dim or 4000)
        if ok:
            bgr = cv2.imread(tmp, cv2.IMREAD_COLOR)
            img = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB) if bgr is not None else None
        try:
            os.remove(tmp)
        except OSError:
            pass
        if img is None:
            raise HTTPException(status_code=415, detail=why or "Could not read this photo")

    if max_dim:
        h, w = img.shape[:2]
        longest = max(h, w)
        if longest > max_dim:
            scale = max_dim / float(longest)
            img = cv2.resize(img, (int(round(w * scale)), int(round(h * scale))),
                             interpolation=cv2.INTER_AREA)
    return (img.astype(np.float32) / 255.0)


def _recipe_of(db: Session, video_id: int) -> Recipe:
    row = db.query(PhotoEdit).filter(PhotoEdit.video_id == video_id).first()
    if not row:
        return Recipe()
    try:
        return Recipe(**json.loads(row.recipe))
    except Exception:
        return Recipe()


@router.get("/{video_id}")
def get_recipe(video_id: int, db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    row = db.query(PhotoEdit).filter(PhotoEdit.video_id == video_id).first()
    return {
        "video_id": video_id,
        "recipe": json.loads(row.recipe) if row else {},
        "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
    }


@router.post("/{video_id}")
def save_recipe(video_id: int, recipe: Recipe, db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    row = db.query(PhotoEdit).filter(PhotoEdit.video_id == video_id).first()
    body = json.dumps(recipe.model_dump())
    if row:
        row.recipe = body
        row.updated_at = datetime.utcnow()
    else:
        db.add(PhotoEdit(video_id=video_id, recipe=body, updated_at=datetime.utcnow()))
    db.commit()
    return {"saved": True}


@router.delete("/{video_id}")
def clear_recipe(video_id: int, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    db.query(PhotoEdit).filter(PhotoEdit.video_id == video_id).delete()
    db.commit()
    return {"cleared": True}


@router.get("/{video_id}/base")
def base_image(request: Request, video_id: int, token: Optional[str] = None,
               w: int = PREVIEW_W, db: Session = Depends(get_db)):
    """The undeveloped photo, bounded, for the editor to draw on.

    Always a resized JPEG - unlike the viewer's preview, which hands back the
    original file when the browser can read it. A 60MB JPEG makes a fine photo
    and a terrible texture.
    """
    if token:
        get_user_from_token(token, db)
    else:
        auth = request.headers.get("Authorization")
        if not auth or not auth.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
        get_user_from_token(auth[7:], db)

    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Photo not found")
    path = _resolve(video.filepath)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="The photo file is not on disk")

    w = max(600, min(int(w), 4000))
    st = os.stat(path)
    out = (_media_root / ".edit-base" /
           f"{video_id}_{int(st.st_mtime)}_{st.st_size}_{w}.jpg")
    if not out.exists() or out.stat().st_size == 0:
        out.parent.mkdir(parents=True, exist_ok=True)
        with _preview_lock:
            if not out.exists():
                import cv2
                rgb = _read_rgb(path, max_dim=w)
                bgr = cv2.cvtColor((rgb * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)
                if not cv2.imwrite(str(out), bgr, [cv2.IMWRITE_JPEG_QUALITY, 94]):
                    raise HTTPException(status_code=500, detail="Could not make a preview")
    return FileResponse(str(out), media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})


# --------------------------------------------------------------------------
# auto tone
# --------------------------------------------------------------------------

@router.get("/{video_id}/auto")
def auto_tone(video_id: int, db: Session = Depends(get_db),
              current_user: User = Depends(get_current_user)):
    """Settings that would make this photo a reasonable starting point.

    Deliberately gentle and deliberately explicable: put the midtone where a
    correctly exposed photo puts it, pull the highlights back only if the
    frame is genuinely clipping, lift the shadows only if they are genuinely
    blocked, and neutralise a colour cast by looking at what the brightest
    non-clipped pixels are doing - they should be grey.
    """
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Photo not found")
    path = _resolve(video.filepath)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="The photo file is not on disk")
    # Only the real recipe fields go back: the editor spreads this straight
    # into its sliders and posts the whole thing to /api/photo-edit, where a
    # stray "_spread" key would be rejected by the model.
    measured = measure_photo(path)
    public = {k: v for k, v in measured.items() if not k.startswith("_")}

    # Auto was clipping at both ends: it aims the midtone and recovers
    # highlights, but nothing checked what the result did to the extremes.
    # Solve for the headroom before handing it over.
    rec = fit_headroom(_read_rgb(path, max_dim=700), Recipe(**public))
    return {"recipe": rec.model_dump()}


def measure_photo(path: str) -> dict:
    """The analysis behind Auto, on its own so presets can build on it.

    A preset written as fixed slider positions assumes an average photo. Given
    the measurements here, the same look can instead be expressed as "this much
    more contrast than this frame already has" - which is what makes it land
    the same way on a flat drone shot and a dark interior.
    """
    rgb = _read_rgb(path, max_dim=900)
    lum = (rgb @ LUMA).astype(np.float32)

    out = {}

    # exposure: aim the median at about 45% brightness
    med = float(np.median(lum))
    if med > 1e-4:
        ev = float(np.log2(0.45 / max(med, 0.02)))
        out["exposure"] = round(max(-2.0, min(2.0, ev)), 2)

    # highlights and shadows, only where there is something to recover
    blown = float((lum > 0.98).mean())
    blocked = float((lum < 0.02).mean())
    if blown > 0.005:
        out["highlights"] = round(-min(0.8, 4.0 * blown), 2)
    if blocked > 0.01:
        out["shadows"] = round(min(0.6, 3.0 * blocked), 2)

    # contrast: flat frames get some, contrasty ones are left alone
    spread = float(np.percentile(lum, 95) - np.percentile(lum, 5))
    if spread < 0.55:
        out["contrast"] = round(min(0.35, (0.55 - spread)), 2)

    # white balance: the bright end of a room shot should be neutral
    bright = rgb[(lum > np.percentile(lum, 80)) & (lum < 0.98)]
    if bright.size > 300:
        r, g, b = [float(v) for v in bright.reshape(-1, 3).mean(axis=0)]
        if g > 1e-4:
            warmth = (r - b) / max(g, 1e-4)          # + is already warm
            # temperature moves the other way to the cast it is correcting
            out["temp_k"] = int(max(3200, min(9500, NEUTRAL_K - warmth * 4200)))
            tint = ((r + b) / 2.0 - g) / max(g, 1e-4)
            out["tint"] = round(max(-0.4, min(0.4, tint * 1.5)), 3)

    spread = float(np.percentile(lum, 95) - np.percentile(lum, 5))

    # --- from correction to a starting grade -----------------------------
    # Everything above only fixes what is wrong. On a well-exposed frame that
    # means Auto changes almost nothing and the result looks flat, because a
    # photo that is technically correct is not yet a photo anyone would send
    # to a client. The rest is the baseline a shot like this always wants.

    # Contrast used to appear only on very flat frames (spread < 0.55), so a
    # normal one got none at all. Aim at a spread of about 0.68 instead, and
    # deliver most of it through the curve rather than the slider - stacking
    # both is what reads as harsh.
    want = 0.68 - spread
    if want > 0.02:
        out["contrast"] = round(min(0.14, want * 0.45), 3)

    # A gentle S, the shape a good grade has. Scaled by how much the frame
    # still needs, and never reaching 0 or 1 at the ends.
    s = max(0.0, min(1.0, want / 0.25))
    if s > 0.05:
        toe = 0.28 - 0.045 * s
        sho = 0.72 + 0.045 * s
        out["curve"] = [[0, 0.012], [0.28, round(toe, 3)],
                        [0.72, round(sho, 3)], [1, 0.978]]

    # Open the shadows. The old rule only fired when pixels were genuinely
    # blocked, so a normal frame kept its heavy corners.
    dark = float((lum < 0.18).mean())
    lift = 0.18 + 1.1 * dark
    out["shadows"] = round(min(0.55, max(float(out.get("shadows", 0.0)), lift)), 3)

    # Hold the top back a little even when nothing is technically blowing -
    # sky and white walls are the first thing to go on a property shot.
    bright = float((lum > 0.82).mean())
    hold = -(0.12 + 0.9 * bright)
    out["highlights"] = round(max(-0.85, min(float(out.get("highlights", 0.0)), hold)), 3)
    out["whites"] = -0.02
    out["blacks"] = 0.02

    # Colour, scaled by how much the frame already has: a vivid sunset does
    # not need what a grey day needs.
    mx = rgb.max(axis=2); mn = rgb.min(axis=2)
    sat_now = float(np.mean((mx - mn) / np.maximum(mx, 1e-4)))
    room = max(0.0, min(1.0, (0.42 - sat_now) / 0.42))
    out["vibrance"] = round(0.10 + 0.22 * room, 3)
    out["saturation"] = round(0.04 + 0.12 * room, 3)

    # Texture. Modest on purpose: clarity and sharpening are the two controls
    # that make an image look processed.
    out["clarity"] = 0.10
    out["sharpen"] = 0.30
    out["dn_luma"] = 0.10

    # --- geometry --------------------------------------------------------
    # Levelling and keystone were the only controls in the editor that nothing
    # measured - every one of them a slider you moved by eye on every photo.
    # A wrong auto-level is worse than none, so this only speaks when the
    # evidence is strong, and the module caps how far it will go.
    try:
        import photo_geometry
        geo = photo_geometry.measure_geometry(rgb)
        if geo.get("confidence", 0) >= 0.45 and "straighten" in geo:
            out["straighten"] = geo["straighten"]
        if geo.get("persp_confidence", 0) >= 0.45 and "persp_v" in geo:
            out["persp_v"] = geo["persp_v"]
        if out.get("straighten") or out.get("persp_v"):
            # Rotating or keystoning pulls the picture off the frame edge, so
            # zoom just enough to cover the empty corners.
            out["geo_scale"] = photo_geometry.cover_scale(
                out.get("straighten", 0.0), out.get("persp_v", 0.0))
        out["_geo_confidence"] = geo.get("confidence", 0.0)
        out["_geo_lines"] = geo.get("lines", 0)
    except Exception as e:
        print(f"photo_edit: geometry measurement failed: {e}", flush=True)

    # --- fringing ---------------------------------------------------------
    # A quick estimate of how much violet/green sits on hard edges. Enough to
    # decide whether defringe is worth switching on at all.
    y = lum
    gx = np.abs(np.diff(y, axis=1, prepend=y[:, :1]))
    gy = np.abs(np.diff(y, axis=0, prepend=y[:1, :]))
    edge = (gx + gy) > 0.10
    if edge.mean() > 0.001:
        e = rgb[edge]
        violet = np.clip(np.minimum(e[:, 0], e[:, 2]) - e[:, 1], 0, None)
        green = np.clip(e[:, 1] - np.maximum(e[:, 0], e[:, 2]), 0, None)
        fringe = float(np.mean(violet + green))
        if fringe > 0.012:
            out["defringe"] = round(min(0.8, fringe * 14.0), 3)
        out["_fringe"] = round(fringe, 4)

    out["_spread"] = round(spread, 4)
    out["_median"] = round(float(np.median(lum)), 4)
    out["_sat"] = round(sat_now, 4)
    return out


# --------------------------------------------------------------------------
# picks and stars
# --------------------------------------------------------------------------

class PickBody(BaseModel):
    pick: int = Field(0, ge=-1, le=1)


@router.post("/{video_id}/pick")
def set_pick(video_id: int, body: PickBody, db: Session = Depends(get_db),
             current_user: User = Depends(get_current_user)):
    row = db.query(PhotoEdit).filter(PhotoEdit.video_id == video_id).first()
    if not row:
        row = PhotoEdit(video_id=video_id, recipe="{}", updated_at=datetime.utcnow())
        db.add(row)
    row.pick = body.pick or None
    db.commit()
    return {"video_id": video_id, "pick": row.pick or 0}


class StarsBody(BaseModel):
    rating: int = Field(0, ge=0, le=5)


@router.post("/{video_id}/stars")
def set_stars(video_id: int, body: StarsBody, db: Session = Depends(get_db),
              current_user: User = Depends(get_current_user)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Photo not found")
    video.rating = body.rating or None
    db.commit()
    return {"video_id": video_id, "rating": video.rating or 0}


@router.get("/{video_id}/tile")
def full_res_tile(request: Request, video_id: int, token: Optional[str] = None,
                  x: float = 0.5, y: float = 0.5, zoom: float = 2.0,
                  w: int = 1400, db: Session = Depends(get_db)):
    """A crop of the photo at its real resolution, around (x, y) in 0..1.

    The editor draws from a 2000px preview, which is right for dragging
    sliders and wrong for judging sharpening or noise: at 2x you would be
    looking at enlarged preview pixels rather than your file. This hands back
    the actual pixels for the part of the frame you are looking at, so 100%
    means 100%.
    """
    if token:
        get_user_from_token(token, db, check_session=False)
    else:
        auth = request.headers.get("Authorization")
        if not auth or not auth.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
        get_user_from_token(auth[7:], db)

    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Photo not found")
    path = _resolve(video.filepath)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="The photo file is not on disk")

    import cv2
    rgb = _read_rgb(path)                        # full size, undeveloped
    h, w_full = rgb.shape[:2]
    zoom = max(1.0, min(16.0, float(zoom)))
    out_w = max(200, min(int(w), 3000))
    out_h = int(round(out_w * h / max(1, w_full)))

    # how much of the source the viewport covers at this zoom
    span_x = min(w_full, int(round(w_full / zoom)))
    span_y = min(h, int(round(h / zoom)))
    cx = int(round(max(0.0, min(1.0, x)) * w_full))
    cy = int(round(max(0.0, min(1.0, y)) * h))
    x0 = max(0, min(w_full - span_x, cx - span_x // 2))
    y0 = max(0, min(h - span_y, cy - span_y // 2))
    crop = rgb[y0:y0 + span_y, x0:x0 + span_x]

    # Only shrink: blowing the crop up would put us back where we started.
    if crop.shape[1] > out_w:
        scale = out_w / float(crop.shape[1])
        crop = cv2.resize(crop, (out_w, int(round(crop.shape[0] * scale))),
                          interpolation=cv2.INTER_AREA)

    ok, buf = cv2.imencode(".jpg", cv2.cvtColor((np.clip(crop, 0, 1) * 255).astype(np.uint8),
                                                cv2.COLOR_RGB2BGR),
                           [cv2.IMWRITE_JPEG_QUALITY, 95])
    if not ok:
        raise HTTPException(status_code=500, detail="Could not make the tile")
    from fastapi.responses import Response
    return Response(content=buf.tobytes(), media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=60",
                             "X-Tile-Region": f"{x0},{y0},{crop.shape[1]},{crop.shape[0]}"})


class ExportBody(BaseModel):
    recipe: Optional[Recipe] = None
    width: int = Field(0, ge=0, le=MAX_EXPORT)     # 0 = native
    quality: int = Field(92, ge=60, le=100)
    fmt: str = Field("jpg", pattern="^(jpg|tif)$")
    subfolder: str = "Edited"
    add_to_library: bool = True


@router.post("/{video_id}/export")
def export(video_id: int, body: ExportBody, db: Session = Depends(get_db),
           current_user: User = Depends(get_current_user)):
    import cv2
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Photo not found")
    path = _resolve(video.filepath)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="The photo file is not on disk")

    recipe = body.recipe or _recipe_of(db, video_id)
    rgb = _read_rgb(path, max_dim=body.width or 0, strict=True)
    out_rgb = apply_recipe(rgb, recipe)

    out_dir = None
    try:
        import projects
        out_dir = projects.stage_dir(db, path, "edited")
    except Exception:
        out_dir = None
    if out_dir is None:
        out_dir = Path(path).parent / (body.subfolder or "Edited")
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(video.filename).stem
    ext = ".jpg" if body.fmt == "jpg" else ".tif"
    out_path = out_dir / f"{stem}_edit{ext}"
    n = 2
    while out_path.exists():
        out_path = out_dir / f"{stem}_edit ({n}){ext}"
        n += 1

    bgr = cv2.cvtColor((np.clip(out_rgb, 0, 1) * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)
    params = [cv2.IMWRITE_JPEG_QUALITY, body.quality] if body.fmt == "jpg" else []
    if not cv2.imwrite(str(out_path), bgr, params):
        raise HTTPException(status_code=500, detail="Could not write the exported photo")
    if body.fmt == "jpg":
        carry_exif(path, str(out_path))

    if body.add_to_library:
        try:
            _index(str(out_path), video, db)
        except Exception:
            db.rollback()

    h, w = out_rgb.shape[:2]
    return {"path": str(out_path), "filename": out_path.name, "width": w, "height": h,
            "size": os.path.getsize(out_path)}


# --------------------------------------------------------------------------
# exporting a whole selection
# --------------------------------------------------------------------------

EXPORT_JOBS: Dict[str, dict] = {}
_export_lock = threading.Lock()


class BatchExport(BaseModel):
    video_ids: List[int]
    width: int = Field(0, ge=0, le=MAX_EXPORT)      # 0 = native
    quality: int = Field(92, ge=60, le=100)
    fmt: str = Field("jpg", pattern="^(jpg|tif)$")
    subfolder: str = "Edited"
    # Photos with no settings saved: skip them, or export them as they are.
    include_unedited: bool = True
    suffix: str = "_edit"


def _run_batch(job_id: str, body: BatchExport):
    # Everything in here is wrapped: a thread that dies quietly leaves the job
    # saying "running" forever, and the person watching the progress bar has
    # no way to tell that nothing is happening.
    db = None
    try:
        db = SessionLocal()
        import cv2
        for n, vid in enumerate(body.video_ids):
            with _export_lock:
                if EXPORT_JOBS[job_id].get("cancelled"):
                    break
            video = db.query(Video).filter(Video.id == vid).first()
            name = video.filename if video else f"photo {vid}"
            with _export_lock:
                EXPORT_JOBS[job_id]["current"] = name
            try:
                if not video:
                    raise RuntimeError("no longer in the library")
                path = _resolve(video.filepath)
                if not path or not os.path.exists(path):
                    raise RuntimeError("not on disk")
                row = db.query(PhotoEdit).filter(PhotoEdit.video_id == vid).first()
                if not row and not body.include_unedited:
                    with _export_lock:
                        EXPORT_JOBS[job_id]["skipped"] += 1
                    continue
                recipe = _recipe_of(db, vid)

                rgb = _read_rgb(path, max_dim=body.width or 0, strict=True)
                out_rgb = apply_recipe(rgb, recipe)

                out_dir = None
                try:
                    import projects
                    out_dir = projects.stage_dir(db, path, "edited")
                except Exception:
                    out_dir = None
                if out_dir is None:
                    out_dir = Path(path).parent / (body.subfolder or "Edited")
                out_dir.mkdir(parents=True, exist_ok=True)

                stem = Path(video.filename).stem
                ext = ".jpg" if body.fmt == "jpg" else ".tif"
                out_path = out_dir / f"{stem}{body.suffix}{ext}"
                k = 2
                while out_path.exists():
                    out_path = out_dir / f"{stem}{body.suffix} ({k}){ext}"
                    k += 1

                bgr = cv2.cvtColor((np.clip(out_rgb, 0, 1) * 255).astype(np.uint8),
                                   cv2.COLOR_RGB2BGR)
                params = [cv2.IMWRITE_JPEG_QUALITY, body.quality] if body.fmt == "jpg" else []
                if not cv2.imwrite(str(out_path), bgr, params):
                    raise RuntimeError("could not write the file")
                if body.fmt == "jpg":
                    carry_exif(path, str(out_path))
                try:
                    _index(str(out_path), video, db)
                except Exception:
                    db.rollback()
                with _export_lock:
                    EXPORT_JOBS[job_id]["results"].append(out_path.name)
            except Exception as e:
                with _export_lock:
                    EXPORT_JOBS[job_id]["errors"].append(f"{name}: {e}")
            finally:
                with _export_lock:
                    EXPORT_JOBS[job_id]["done"] = n + 1
    except Exception as e:
        with _export_lock:
            EXPORT_JOBS[job_id]["errors"].append(f"the export stopped: {e}")
    finally:
        if db is not None:
            db.close()
        with _export_lock:
            EXPORT_JOBS[job_id]["running"] = False
            EXPORT_JOBS[job_id]["current"] = None
            EXPORT_JOBS[job_id]["finished_at"] = time.time()


@batch_router.post("")
def export_batch(body: BatchExport, current_user: User = Depends(get_current_user)):
    """Render a pile of photos with their own saved settings, in the background.

    One at a time on purpose: a full-resolution develop of a 45MP frame is
    memory-hungry, and a queue that quietly eats the machine is worse than one
    that takes a minute longer.
    """
    ids = list(dict.fromkeys(body.video_ids))[:5000]
    if not ids:
        raise HTTPException(status_code=400, detail="No photos given")
    body.video_ids = ids
    job_id = uuid.uuid4().hex[:12]
    with _export_lock:
        # Forget finished jobs from more than a few hours ago.
        cutoff = time.time() - 6 * 3600
        for jid in [k for k, v in EXPORT_JOBS.items()
                    if not v["running"] and (v.get("finished_at") or 0) < cutoff]:
            EXPORT_JOBS.pop(jid, None)
        EXPORT_JOBS[job_id] = {"id": job_id, "running": True, "done": 0,
                               "total": len(ids), "current": "Starting…",
                               "results": [], "errors": [], "skipped": 0,
                               "started_at": time.time(), "finished_at": None,
                               "cancelled": False}
    threading.Thread(target=_run_batch, args=(job_id, body), daemon=True).start()
    return {"job_id": job_id, "total": len(ids)}


@batch_router.get("/{job_id}")
def export_batch_status(job_id: str, current_user: User = Depends(get_current_user)):
    with _export_lock:
        state = EXPORT_JOBS.get(job_id)
        if not state:
            raise HTTPException(status_code=404, detail="No such job")
        return dict(state)


@batch_router.post("/{job_id}/cancel")
def export_batch_cancel(job_id: str, current_user: User = Depends(get_current_user)):
    with _export_lock:
        if job_id in EXPORT_JOBS:
            EXPORT_JOBS[job_id]["cancelled"] = True
    return {"status": "cancelling"}


def _index(out_path: str, source: Video, db: Session):
    """Add an exported photo to the library, beside where it was written."""
    parent_dir = str(Path(out_path).parent)
    folder = db.query(IndexedFolder).filter(IndexedFolder.path == parent_dir).first()
    if not folder:
        rel = parent_dir
        try:
            if _media_root:
                rel = str(Path(parent_dir).relative_to(_media_root))
        except ValueError:
            pass
        parent = db.query(IndexedFolder).filter(
            IndexedFolder.path == str(Path(parent_dir).parent)).first()
        folder = IndexedFolder(path=parent_dir, name=Path(parent_dir).name, relative_path=rel,
                               parent_id=parent.id if parent else None, added_at=datetime.utcnow())
        db.add(folder)
        db.commit()
        db.refresh(folder)
    if db.query(Video.id).filter(Video.filepath == out_path).first():
        return
    thumb_rel = None
    try:
        import indexer
        tname = indexer.thumb_name(out_path)
        if _media_root:
            thumbs = Path(_media_root) / "thumbnails"
            thumbs.mkdir(parents=True, exist_ok=True)
            ok, _ = previews.make_image_jpeg(out_path, str(thumbs / tname), width=640)
            if ok:
                thumb_rel = f"/thumbnails/{tname}"
    except Exception:
        pass
    now = datetime.utcnow()
    name = os.path.basename(out_path)
    db.add(Video(filename=name, filepath=out_path, file_size=os.path.getsize(out_path),
                 duration=0.0, thumbnail_path=thumb_rel, folder_id=folder.id,
                 media_type=get_media_type(name) or "photo", status="edited",
                 created_at=now, uploaded_at=now, uploaded_by="photo-edit"))
    db.commit()


# --------------------------------------------------------------------------
# presets: a look you dialled in once, stamped across a whole shoot
# --------------------------------------------------------------------------

class PhotoPreset(Base):
    __tablename__ = "photo_presets"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    recipe = Column(String, nullable=False)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class PresetBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    recipe: Recipe


@presets_router.get("")
def list_presets(db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    rows = db.query(PhotoPreset).order_by(PhotoPreset.name.asc()).all()
    out = []
    for p in rows:
        try:
            recipe = json.loads(p.recipe)
        except Exception:
            continue
        out.append({"id": p.id, "name": p.name, "recipe": recipe,
                    "created_by": p.created_by})
    return {"presets": out}


@presets_router.post("")
def save_preset(body: PresetBody, db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    name = body.name.strip()
    row = db.query(PhotoPreset).filter(PhotoPreset.name == name).first()
    payload = json.dumps(body.recipe.model_dump())
    if row:
        row.recipe = payload
    else:
        row = PhotoPreset(name=name, recipe=payload,
                          created_by=getattr(current_user, "username", None),
                          created_at=datetime.utcnow())
        db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "name": row.name}


# Tonal moves are "how much more than this photo already has", so they add on
# top of what the measurement found. Colour and texture are the identity of a
# look, so they are taken as written.
ADDITIVE = ("exposure", "contrast", "highlights", "shadows", "whites", "blacks",
            "clarity", "dehaze")
LIMITS = {
    "exposure": (-5, 5), "contrast": (-1, 1), "highlights": (-1, 1),
    "shadows": (-1, 1), "whites": (-0.5, 0.5), "blacks": (-0.5, 0.5),
    "clarity": (-1, 1), "dehaze": (-0.5, 1),
}


def fit_headroom(rgb01, rec: "Recipe", lo: float = 0.022, hi: float = 0.972,
                 passes: int = 16) -> "Recipe":
    """Sit the frame in its band: black point just off 0, white point just
    off 100, and nothing piled up at either end.

    This works in both directions, which matters more than it sounds. Only
    trimming produced images whose brightest pixel was 0.80 - technically
    unclipped, and milky, because nothing in the frame ever reached white.
    Too much headroom reads as flat just as surely as too little reads as
    crushed.

    Which control to reach for: the pipeline clips to 0..1 partway through,
    so by the time `whites` runs anything exposure already drove past 1.0 is
    gone - pulling whites down afterwards cannot bring it back. Highlights act
    before that clip, so they recover; exposure is the blunt instrument for
    when they are spent. Going the other way there is nothing to lose, so
    whites and blacks do the work.
    """
    out = rec.model_copy(deep=True)
    for _ in range(passes):
        lum = apply_recipe(rgb01, out) @ LUMA
        p_lo = float(np.percentile(lum, 0.1))
        p_hi = float(np.percentile(lum, 99.9))
        clipped = float((lum >= 0.997).mean())
        crushed = float((lum <= 0.003).mean())

        over_hi = p_hi - hi          # > 0: clipping, pull back
        under_hi = hi - p_hi         # > 0: headroom going spare, open up
        over_lo = lo - p_lo          # > 0: too dark, lift
        under_lo = p_lo - lo         # > 0: black point floating, drop it

        done_hi = abs(p_hi - hi) <= 0.012 and clipped <= 0.0002
        done_lo = abs(p_lo - lo) <= 0.012 and crushed <= 0.0002
        if done_hi and done_lo:
            break

        # --- top end -----------------------------------------------------
        if over_hi > 0.012 or clipped > 0.0002:
            if out.highlights > -0.95:
                out.highlights = float(max(-1.0, out.highlights
                                           - max(over_hi, 0.02) * 2.5))
            else:
                step = max(0.12, min(0.6, over_hi * 3.0 + clipped * 8.0))
                out.exposure = float(max(-5.0, out.exposure - step))
            out.whites = float(max(-0.5, out.whites - max(over_hi, 0.0) * 0.8))
        elif under_hi > 0.012:
            # Room to spare: bring the white point up to where it belongs.
            if out.whites < 0.5:
                out.whites = float(min(0.5, out.whites + under_hi * 1.4))
            else:
                out.exposure = float(min(5.0, out.exposure + min(0.25, under_hi)))

        # --- bottom end --------------------------------------------------
        if over_lo > 0.012 or crushed > 0.0002:
            out.blacks = float(min(0.5, out.blacks + max(over_lo, 0.01) * 1.8))
        elif under_lo > 0.012:
            out.blacks = float(max(-0.5, out.blacks - under_lo * 1.4))
    return out


def resolve_look(look: dict, measured: dict) -> dict:
    """Put a look onto one specific photo.

    The stored preset is still a plain recipe - it has to be, because the
    editor applies it directly. Here it is read a second way: tonal values
    become offsets from what this frame actually measures, and the colour
    temperature becomes a shift away from this frame's own neutral rather
    than an absolute Kelvin number.

    That last part is what made "Golden hour" cool a photo down: an absolute
    7800K is colder than a shot already sitting at 8790K, so the warm preset
    made it bluer.
    """
    out = dict(Recipe().model_dump())
    spread = measured.get("_spread")
    base = {k: v for k, v in measured.items() if not k.startswith("_")}

    out.update(base)

    for key, value in look.items():
        if key.startswith("_") or key not in out:
            continue
        if key in ADDITIVE:
            combined = float(base.get(key, 0.0)) + float(value)
            # A frame that is already contrasty does not need the full dose.
            if key == "contrast" and spread is not None and spread > 0.7:
                combined = float(base.get(key, 0.0)) + float(value) * 0.55
            lo, hi = LIMITS[key]
            out[key] = round(max(lo, min(hi, combined)), 3)
        elif key == "temp_k":
            # Read as a shift from neutral, applied to this photo's own
            # measured white balance.
            shift = float(value) - NEUTRAL_K
            out["temp_k"] = int(max(2000, min(15000,
                             float(base.get("temp_k", NEUTRAL_K)) + shift)))
        elif key == "tint":
            out["tint"] = round(max(-1, min(1,
                          float(base.get("tint", 0.0)) + float(value))), 3)
        else:
            out[key] = value
    return out


class MatchBody(BaseModel):
    reference_id: int
    video_ids: List[int]
    match_colour: bool = True
    match_tone: bool = True


@match_router.post("")
def match_to_reference(body: MatchBody, db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    """Make a set look like one shoot, not forty separate photos.

    "Apply to selected" copies SETTINGS, which is not the same thing: the same
    recipe on a north-facing bedroom and a sunlit lounge gives two different
    pictures. This copies the OUTCOME - it measures what the reference ends up
    looking like, measures each target, and solves each one separately for the
    same brightness, contrast, white balance and colour strength.

    Agents notice an inconsistent set long before they notice any single
    photo in it.
    """
    ref = db.query(Video).filter(Video.id == body.reference_id).first()
    if not ref:
        raise HTTPException(status_code=404, detail="Reference photo not found")
    ref_path = _resolve(ref.filepath)
    if not ref_path or not os.path.exists(ref_path):
        raise HTTPException(status_code=404, detail="The reference file is not on disk")

    # What the reference actually looks like once its own edit is applied.
    ref_rgb = _read_rgb(ref_path, max_dim=700)
    ref_edit = db.query(PhotoEdit).filter(PhotoEdit.video_id == ref.id).first()
    ref_recipe = Recipe(**json.loads(ref_edit.recipe)) if ref_edit else Recipe()
    ref_out = apply_recipe(ref_rgb, ref_recipe)
    ref_lum = ref_out @ LUMA
    target = {
        "median": float(np.median(ref_lum)),
        "spread": float(np.percentile(ref_lum, 95) - np.percentile(ref_lum, 5)),
        "temp_k": float(ref_recipe.temp_k),
        "tint": float(ref_recipe.tint),
    }
    mx, mn = ref_out.max(axis=2), ref_out.min(axis=2)
    target["sat"] = float(np.mean((mx - mn) / np.maximum(mx, 1e-4)))

    ids = [int(v) for v in dict.fromkeys(body.video_ids) if int(v) != ref.id][:400]
    rows = {v.id: v for v in db.query(Video).filter(Video.id.in_(ids)).all()}
    have = {r.video_id: r for r in
            db.query(PhotoEdit).filter(PhotoEdit.video_id.in_(ids)).all()}

    done, skipped = 0, []
    now = datetime.utcnow()
    for vid in ids:
        v = rows.get(vid)
        if not v:
            skipped.append(vid); continue
        path = _resolve(v.filepath)
        if not path or not os.path.exists(path):
            skipped.append(vid); continue
        try:
            rgb = _read_rgb(path, max_dim=700)
            rec = Recipe(**{k: val for k, val in measure_photo(path).items()
                            if not k.startswith("_")})

            if body.match_colour:
                rec.temp_k = target["temp_k"]
                rec.tint = target["tint"]

            if body.match_tone:
                # Brightness: solve in stops rather than guessing.
                for _ in range(4):
                    med = float(np.median(apply_recipe(rgb, rec) @ LUMA))
                    if abs(med - target["median"]) < 0.01 or med <= 1e-4:
                        break
                    rec.exposure = float(max(-5, min(5, rec.exposure
                                       + math.log2(target["median"] / max(med, 0.02)) * 0.8)))
                out = apply_recipe(rgb, rec)
                lum = out @ LUMA
                spread = float(np.percentile(lum, 95) - np.percentile(lum, 5))
                rec.contrast = float(max(-1, min(1, rec.contrast
                                   + (target["spread"] - spread) * 0.6)))
                # Saturation twice, gently. A single strong correction
                # consistently overshot the reference by half again.
                for _ in range(2):
                    o = apply_recipe(rgb, rec)
                    mx, mn = o.max(axis=2), o.min(axis=2)
                    sat = float(np.mean((mx - mn) / np.maximum(mx, 1e-4)))
                    if abs(sat - target["sat"]) < 0.012:
                        break
                    rec.saturation = float(max(-1, min(1, rec.saturation
                                         + (target["sat"] - sat) * 0.7)))

            rec = fit_headroom(rgb, rec)

            payload = json.dumps(rec.model_dump())
            row = have.get(vid)
            if row:
                row.recipe = payload
                row.updated_at = now
            else:
                db.add(PhotoEdit(video_id=vid, recipe=payload, updated_at=now))
            done += 1
        except Exception as e:
            print(f"photo_edit: match failed for {vid}: {e}", flush=True)
            skipped.append(vid)
    db.commit()
    return {"status": "ok", "matched": done, "skipped": len(skipped),
            "reference": ref.filename, "target": {k: round(v, 4) for k, v in target.items()}}


@router.get("/watermarks")
def list_watermarks(current_user: User = Depends(get_current_user)):
    """The logos available, in the order the wm_index slider counts them."""
    files = watermark_files()
    return {"folder": str(watermark_dir()),
            "watermarks": [{"index": i, "name": f.name,
                            "size": f.stat().st_size} for i, f in enumerate(files)]}


@router.post("/watermarks")
def upload_watermark(file: UploadFile = File(...),
                     current_user: User = Depends(get_current_user)):
    """Add a logo. PNG only - the whole point is the transparency."""
    if not permissions.can(current_user.role, permissions.UPLOAD):
        raise HTTPException(status_code=403, detail="Your account cannot upload files.")
    name = os.path.basename(file.filename or "")
    if not name.lower().endswith(".png"):
        raise HTTPException(status_code=400,
                            detail="A watermark must be a PNG, so it can have a transparent background.")
    dest = watermark_dir() / re.sub(r'[<>:"|?*\\/]', "_", name)
    with open(dest, "wb") as out:
        shutil.copyfileobj(file.file, out, 1024 * 1024)
    _WM_CACHE.clear()
    files = watermark_files()
    return {"status": "ok", "name": dest.name,
            "index": next((i for i, f in enumerate(files) if f.name == dest.name), 0),
            "count": len(files)}


@router.delete("/watermarks/{name}")
def delete_watermark(name: str, current_user: User = Depends(get_current_user)):
    if not permissions.can(current_user.role, permissions.ADMIN):
        raise HTTPException(status_code=403, detail="Admin only")
    target = watermark_dir() / os.path.basename(name)
    if not target.exists():
        raise HTTPException(status_code=404, detail="No such watermark")
    target.unlink()
    _WM_CACHE.clear()
    return {"status": "ok"}


@presets_router.get("/{preset_id}/for/{video_id}")
def resolve_preset_for_photo(preset_id: int, video_id: int,
                             db: Session = Depends(get_db),
                             current_user: User = Depends(get_current_user)):
    """This preset, solved for this photo.

    The editor can apply the stored recipe as-is (absolute, the old
    behaviour) or ask for this, which measures the frame first and lands the
    look on top of it.
    """
    preset = db.query(PhotoPreset).filter(PhotoPreset.id == preset_id).first()
    if not preset:
        raise HTTPException(status_code=404, detail="Preset not found")
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Photo not found")
    path = _resolve(video.filepath)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="The photo file is not on disk")

    try:
        look = json.loads(preset.recipe)
    except Exception:
        raise HTTPException(status_code=500, detail="That preset is unreadable")

    measured = measure_photo(path)
    resolved = Recipe(**resolve_look(look, measured))
    # Guarantee the waveform rather than hoping for it.
    resolved = fit_headroom(_read_rgb(path, max_dim=700), resolved)
    return {"name": preset.name, "recipe": resolved.model_dump(),
            "measured": {k: v for k, v in measured.items() if not k.startswith("_")}}


@presets_router.delete("/{preset_id}")
def delete_preset(preset_id: int, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    db.query(PhotoPreset).filter(PhotoPreset.id == preset_id).delete()
    db.commit()
    return {"deleted": True}


class ApplyBody(BaseModel):
    video_ids: List[int]
    recipe: Recipe


@presets_router.post("/apply")
def apply_to_many(body: ApplyBody, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    """Put the same settings on a pile of photos.

    Only the recipe is copied - nothing is rendered and no file is touched,
    so this is instant even across a whole shoot. Export when you are happy.
    """
    ids = list(dict.fromkeys(body.video_ids))[:5000]
    if not ids:
        raise HTTPException(status_code=400, detail="No photos given")
    payload = json.dumps(body.recipe.model_dump())
    now = datetime.utcnow()
    have = {r.video_id: r for r in
            db.query(PhotoEdit).filter(PhotoEdit.video_id.in_(ids)).all()}
    for vid in ids:
        row = have.get(vid)
        if row:
            row.recipe = payload
            row.updated_at = now
        else:
            db.add(PhotoEdit(video_id=vid, recipe=payload, updated_at=now))
    db.commit()
    return {"applied": len(ids)}


class EditedQuery(BaseModel):
    video_ids: List[int] = []


@presets_router.post("/edited")
def which_are_edited(body: EditedQuery, db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    """Which of these photos already carry settings - for the filmstrip badge."""
    ids = list(dict.fromkeys(body.video_ids))[:5000]
    if not ids:
        return {"edited": []}
    rows = db.query(PhotoEdit.video_id, PhotoEdit.pick).filter(
        PhotoEdit.video_id.in_(ids)).all()
    stars = db.query(Video.id, Video.rating).filter(
        Video.id.in_(ids), Video.rating.isnot(None)).all()
    return {
        "edited": [r[0] for r in rows],
        "picks": {str(vid): p for vid, p in rows if p},
        "stars": {str(vid): r for vid, r in stars if r},
    }


# --------------------------------------------------------------------------
# starter presets
# --------------------------------------------------------------------------

# Looks to begin from, not looks to finish with: each one is deliberately
# restrained, because a preset that slams the photo leaves you undoing it
# rather than adjusting it. Apply, then nudge.
STARTER_PRESETS = [
    # Shaped after a look of his that reads as balanced on this footage:
    # contrast slider barely touched (0.07), the contrast coming from a gentle
    # S curve instead; a big shadow lift; highlights pulled about a quarter;
    # whites a touch negative; saturation carrying the colour rather than
    # clarity and dehaze carrying it.
    #
    # Measured rules these obey:
    #   - contrast slider stays <= 0.10. Stacking it with a curve is what made
    #     the earlier set read as harsh.
    #   - dehaze re-seats the black point from the image, so on a dark or raw
    #     frame it eats everything - 0.52 turned 77% of a drone DNG pure
    #     black. Kept at or below 0.12, always with blacks lifted.
    #   - blacks never negative; whites slightly negative.
    #   - sharpen runs after the curve and clips, so it stays modest.
    ("Natural", {
        "contrast": 0.05, "highlights": -0.26, "shadows": 0.34,
        "whites": -0.02, "blacks": 0.02,
        "saturation": 0.10, "vibrance": 0.12, "clarity": 0.10, "sharpen": 0.30,
        "curve": [[0, 0.012], [0.28, 0.26], [0.72, 0.755], [1, 0.978]],
    }),
    ("Clean & bright", {
        "exposure": 0.25, "contrast": 0.06, "highlights": -0.40, "shadows": 0.44,
        "whites": -0.02, "blacks": 0.03, "temp_k": 6700,
        "saturation": 0.16, "vibrance": 0.20, "clarity": 0.12,
        "sharpen": 0.34, "dn_luma": 0.10,
        "curve": [[0, 0.015], [0.28, 0.275], [0.72, 0.765], [1, 0.975]],
    }),
    ("Moody", {
        "exposure": -0.12, "contrast": 0.10, "highlights": -0.30, "shadows": 0.10,
        "whites": -0.04, "blacks": 0.04, "temp_k": 6150, "tint": 0.02,
        "saturation": -0.06, "vibrance": 0.18, "clarity": 0.16, "dehaze": 0.06,
        "b_lift": 0.035, "r_lift": -0.012, "sharpen": 0.28, "dn_luma": 0.10,
        "curve": [[0, 0.028], [0.28, 0.225], [0.72, 0.775], [1, 0.965]],
    }),
    ("Warm & inviting", {
        "exposure": 0.18, "contrast": 0.05, "highlights": -0.34, "shadows": 0.42,
        "whites": -0.02, "blacks": 0.03, "temp_k": 7500, "tint": 0.02,
        "saturation": 0.18, "vibrance": 0.18, "clarity": 0.10, "sharpen": 0.30,
        "r_gain": 1.03, "b_gain": 0.982, "r_lift": 0.016, "b_lift": -0.01,
        "curve": [[0, 0.02], [0.3, 0.30], [0.7, 0.745], [1, 0.975]],
    }),
    ("Architectural", {
        "contrast": 0.08, "highlights": -0.32, "shadows": 0.40,
        "whites": -0.02, "blacks": 0.055, "temp_k": 6400,
        "saturation": 0.14, "vibrance": 0.18, "clarity": 0.20, "dehaze": 0.05,
        "b_gain": 1.03, "sharpen": 0.42, "dn_luma": 0.12,
        "curve": [[0, 0.018], [0.28, 0.255], [0.72, 0.775], [1, 0.972]],
    }),
    ("Golden hour", {
        "exposure": 0.12, "contrast": 0.05, "highlights": -0.42, "shadows": 0.44,
        "whites": -0.03, "blacks": 0.03, "temp_k": 7900, "tint": 0.03,
        "saturation": 0.22, "vibrance": 0.22, "clarity": 0.08, "sharpen": 0.28,
        "r_gain": 1.045, "g_gain": 1.005, "b_gain": 0.962,
        "r_lift": 0.018, "b_lift": -0.016,
        "curve": [[0, 0.026], [0.3, 0.315], [0.7, 0.755], [1, 0.975]],
    }),
    ("Twilight glow", {
        "exposure": 0.10, "contrast": 0.07, "highlights": -0.26, "shadows": 0.40,
        "whites": -0.02, "blacks": 0.04, "temp_k": 5400, "tint": 0.04,
        "saturation": 0.20, "vibrance": 0.22, "clarity": 0.14, "dehaze": 0.05,
        "b_gain": 1.045, "r_gain": 0.992, "b_lift": 0.032, "r_lift": -0.006,
        "sharpen": 0.28, "dn_luma": 0.12,
        "curve": [[0, 0.026], [0.28, 0.265], [0.72, 0.77], [1, 0.972]],
    }),
    ("Grey day rescue", {
        "exposure": 0.10, "contrast": 0.09, "highlights": -0.28, "shadows": 0.42,
        "whites": -0.02, "blacks": 0.07, "temp_k": 7000, "tint": 0.02,
        "saturation": 0.26, "vibrance": 0.28, "clarity": 0.18, "dehaze": 0.06,
        "sharpen": 0.36, "dn_luma": 0.10,
        "curve": [[0, 0.022], [0.28, 0.25], [0.72, 0.78], [1, 0.972]],
    }),
]


# Seeding is recorded here rather than inferred from the table being empty.
SEED_MARKER = Path(__file__).resolve().parent / ".starter-presets-seeded"


def seed_presets():
    """Put the starter looks in, once.

    This used to bail whenever the table held anything at all - which meant
    that saving a single preset of your own before the starters had ever been
    seeded locked all eight of them out for good. That is exactly what
    happened: one preset saved the night before the starters existed, and the
    next start found a non-empty table and returned.

    Seeding is now recorded with a marker file, so it happens once ever and a
    starter you delete stays deleted - without an existing preset of your own
    being mistaken for "already seeded".
    """
    db = SessionLocal()
    try:
        if SEED_MARKER.exists():
            return
        have = {name for (name,) in db.query(PhotoPreset.name).all()}
        added = 0
        for name, recipe in STARTER_PRESETS:
            if name in have:
                continue
            db.add(PhotoPreset(name=name,
                               recipe=json.dumps(Recipe(**recipe).model_dump()),
                               created_by="starter", created_at=datetime.utcnow()))
            added += 1
        db.commit()
        try:
            SEED_MARKER.write_text(datetime.utcnow().isoformat(), encoding="utf-8")
        except Exception:
            # No marker means it tries again next start. Harmless - the names
            # already present are skipped - but worth knowing about.
            print("photo_edit: could not write the seed marker", flush=True)
        if added:
            print(f"photo_edit: added {added} starter presets", flush=True)
    except Exception as e:
        db.rollback()
        print(f"photo_edit: could not add the starter presets: {e}", flush=True)
    finally:
        db.close()


def install(app, media_root, resolve_media_path):
    global _media_root, _resolve
    _media_root = Path(media_root) if media_root else None
    _resolve = resolve_media_path
    Base.metadata.create_all(bind=engine,
                             tables=[PhotoEdit.__table__, PhotoPreset.__table__])
    # A photo_edits table made before picks existed needs the column.
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(photo_edits)"))]
            if "pick" not in cols:
                conn.execute(text("ALTER TABLE photo_edits ADD COLUMN pick INTEGER"))
                conn.commit()
    except Exception as e:
        print(f"photo_edit: could not add the pick column: {e}", flush=True)
    app.include_router(router)
    app.include_router(presets_router)
    app.include_router(batch_router)
    app.include_router(match_router)
    seed_presets()
