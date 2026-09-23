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

import permissions
import numpy as np
from fastapi import UploadFile, File, APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, Float, Integer, String
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
# Watermarks need their own router: on `router` they sit below
# /{video_id}, which matches first and rejects "watermarks" as a bad id.
wm_router = APIRouter(prefix="/api/photo-edit/watermarks", tags=["photo-edit"])
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
    # Named versions of the edit: [{"name", "at", "recipe"}], newest last.
    snapshots = Column(String, nullable=True)


class MaskPart(BaseModel):
    """A second shape on a mask - added to it, cut out of it, or overlapped
    with it: a radial on the house minus a painted tree, a gradient on the sky
    only where the sky selection is."""
    kind: str = Field("radial", pattern="^(radial|linear|brush)$")
    mode: str = Field("subtract", pattern="^(add|subtract|intersect)$")
    ref: str = Field("", max_length=60, pattern=r"^(|\d+/[a-z0-9]{8,32})$")
    cx: float = Field(0.5, ge=-1, le=2)
    cy: float = Field(0.5, ge=-1, le=2)
    rx: float = Field(0.2, ge=0.005, le=3)
    ry: float = Field(0.2, ge=0.005, le=3)
    angle: float = Field(0, ge=-180, le=180)
    feather: float = Field(0.5, ge=0, le=1)
    invert: bool = False


class Mask(BaseModel):
    """One local adjustment.

    Two shapes cover almost all of property work: a radial for a dark corner
    of a room or a face, and a linear for a blown window or a washed-out sky.
    Geometry is in fractions of the framed image, so a mask drawn on the
    preview lands in the same place on a 45-megapixel export.

    The adjustments are the subset that actually gets used locally. Anything
    that only makes sense globally - lens correction, the tone curve, the
    watermark - deliberately has no per-mask equivalent.
    """
    kind: str = "radial"                              # radial | linear | brush
    # brush: a painted (or AI-selected) greyscale bitmap in SOURCE space - the
    # photo as it comes off the card, before crop and geometry - so cropping
    # or straightening later never moves it off what it was painted on.
    # "<video_id>/<id>", a PNG under MEDIA_ROOT/.edit-masks.
    ref: str = Field("", max_length=60, pattern=r"^(|\d+/[a-z0-9]{8,32})$")
    # radial: centre, radii and rotation. linear: a line through cx,cy at
    # `angle`, with the gradient running across it.
    cx: float = Field(0.5, ge=-1, le=2)
    cy: float = Field(0.5, ge=-1, le=2)
    rx: float = Field(0.3, ge=0.005, le=3)
    ry: float = Field(0.3, ge=0.005, le=3)
    angle: float = Field(0, ge=-180, le=180)          # degrees
    feather: float = Field(0.5, ge=0, le=1)           # 0 hard edge, 1 all falloff
    invert: bool = False
    amount: float = Field(1.0, ge=0, le=1)            # the whole mask's strength
    enabled: bool = True
    name: str = ""
    part2: Optional[MaskPart] = None

    exposure: float = Field(0, ge=-4, le=4)
    contrast: float = Field(0, ge=-1, le=1)
    highlights: float = Field(0, ge=-1, le=1)
    shadows: float = Field(0, ge=-1, le=1)
    whites: float = Field(0, ge=-0.5, le=0.5)
    blacks: float = Field(0, ge=-0.5, le=0.5)
    saturation: float = Field(0, ge=-1, le=1)
    # Warmth as an offset in kelvin, not an absolute: a mask should shift the
    # patch relative to whatever the global white balance settled on.
    temp_shift: float = Field(0, ge=-4000, le=4000)
    tint_shift: float = Field(0, ge=-1, le=1)
    clarity: float = Field(0, ge=-1, le=1)

    # Range refinement, on any mask: keep only the tones and/or the colour
    # asked for inside it - a brush over a window that only takes the bright
    # glass, a linear over the lawn that only takes the green.
    range_lo: float = Field(0, ge=0, le=1)            # luminance from
    range_hi: float = Field(1, ge=0, le=1)            # luminance to
    range_soft: float = Field(0.1, ge=0.005, le=0.5)  # how gently the ends fade
    hue_on: bool = False
    hue: float = Field(0, ge=0, le=360)               # degrees
    hue_width: float = Field(30, ge=5, le=180)        # +- degrees kept fully


def range_weight(s: np.ndarray, m: "Mask") -> Optional[np.ndarray]:
    """How much of each pixel the mask's tone and colour ranges keep, HxW, or
    None when neither is in use. Mirrors rangeW() in the COMBINE shader."""
    lum_on = m.range_lo > 0.0 or m.range_hi < 1.0
    if not lum_on and not m.hue_on:
        return None
    w = np.ones(s.shape[:2], dtype=np.float32)
    if lum_on:
        y = (s @ LUMA).astype(np.float32)
        soft = float(m.range_soft)
        if m.range_lo > 0.0:
            w *= _smoothstep(float(m.range_lo) - soft, float(m.range_lo), y)
        if m.range_hi < 1.0:
            w *= 1.0 - _smoothstep(float(m.range_hi), float(m.range_hi) + soft, y)
    if m.hue_on:
        hue, chroma = _hue_chroma(s)
        d = np.abs(((hue - float(m.hue) + 540.0) % 360.0) - 180.0)
        wid = float(m.hue_width)
        wh = 1.0 - _smoothstep(wid, wid + 25.0, d)
        w *= wh * np.clip(chroma * 4.0, 0.0, 1.0)
    return w.astype(np.float32)


def _hue_chroma(s: np.ndarray):
    """Hue in degrees and chroma, the same formula the shaders use."""
    mx = s.max(axis=-1)
    mn = s.min(axis=-1)
    chroma = (mx - mn).astype(np.float32)
    rr, gg, bb = s[..., 0], s[..., 1], s[..., 2]
    safe = np.where(chroma < 1e-5, 1.0, chroma)
    hue = np.where(mx == rr, ((gg - bb) / safe) % 6.0,
          np.where(mx == gg, (bb - rr) / safe + 2.0,
                             (rr - gg) / safe + 4.0)) * 60.0
    hue = np.where(chroma < 1e-5, 0.0, hue).astype(np.float32)
    return hue, chroma


_BANDS = ("red", "orange", "yellow", "green", "aqua", "blue", "purple", "magenta")
_BAND_CENTRES = (0.0, 30.0, 60.0, 120.0, 180.0, 240.0, 280.0, 320.0)


def _band_weights(hue: np.ndarray):
    for centre in _BAND_CENTRES:
        d = np.abs(((hue - centre + 540.0) % 360.0) - 180.0)
        w = np.clip(1.0 - d / 45.0, 0.0, 1.0)
        yield (w * w * (3.0 - 2.0 * w)).astype(np.float32)


def _from_hue(hue: np.ndarray, mx: np.ndarray, mn: np.ndarray) -> np.ndarray:
    """RGB with this hue and the given max and min channel (HSV rebuild)."""
    h6 = (hue % 360.0) / 60.0
    c = mx - mn
    x = c * (1.0 - np.abs(h6 % 2.0 - 1.0))
    z = np.zeros_like(c)
    i = np.floor(h6).astype(np.int32) % 6
    r = np.select([i == 0, i == 1, i == 2, i == 3, i == 4, i == 5], [c, x, z, z, x, c])
    g = np.select([i == 0, i == 1, i == 2, i == 3, i == 4, i == 5], [x, c, c, x, z, z])
    b = np.select([i == 0, i == 1, i == 2, i == 3, i == 4, i == 5], [z, z, x, c, c, x])
    return (np.stack([r, g, b], -1) + mn[..., None]).astype(np.float32)


def apply_hsl(s: np.ndarray, r: "Recipe") -> np.ndarray:
    """Hue and luminance per colour family. Mirrors the DEVELOP shader."""
    hs = [float(getattr(r, f"hue_{b}", 0.0) or 0.0) for b in _BANDS]
    ls = [float(getattr(r, f"lum_{b}", 0.0) or 0.0) for b in _BANDS]
    if not any(hs) and not any(ls):
        return s
    hue, chroma = _hue_chroma(s)
    strength = np.clip(chroma * 4.0, 0.0, 1.0)
    dh = np.zeros_like(hue)
    dl = np.zeros_like(hue)
    for w, a, l in zip(_band_weights(hue), hs, ls):
        if a:
            dh += np.float32(a) * w
        if l:
            dl += np.float32(l) * w
    live = chroma >= 1e-5
    out = s
    if any(hs):
        mx = s.max(axis=-1)
        mn = s.min(axis=-1)
        moved = _from_hue(hue + dh * 30.0 * strength, mx, mn)
        out = np.where(live[..., None], moved, s)
    if any(ls):
        out = out * (1.0 + dl * 0.6 * strength)[..., None]
    return np.clip(out, 0.0, 1.0).astype(np.float32)


def _tint(hue_deg: float) -> np.ndarray:
    """A pure hue with its luminance taken out: which way to push a tone."""
    h = np.array([hue_deg], np.float32)
    c = _from_hue(h, np.array([1.0], np.float32), np.array([0.0], np.float32))[0]
    return (c - float(c @ LUMA)).astype(np.float32)


def apply_grade(s: np.ndarray, r: "Recipe") -> np.ndarray:
    """Colour grading wheels: shadows, midtones, highlights. Mirrors DEVELOP."""
    if not (r.grade_sh_sat or r.grade_mid_sat or r.grade_hi_sat):
        return s
    y = (s @ LUMA).astype(np.float32)
    bal = float(r.grade_balance)
    ws = 1.0 - _smoothstep(0.0, 0.55 + 0.25 * bal, y)
    wh = _smoothstep(0.45 + 0.25 * bal, 1.0, y)
    wm = np.clip(1.0 - ws - wh, 0.0, 1.0)
    push = (ws[..., None] * (float(r.grade_sh_sat) * _tint(r.grade_sh_hue))
            + wm[..., None] * (float(r.grade_mid_sat) * _tint(r.grade_mid_hue))
            + wh[..., None] * (float(r.grade_hi_sat) * _tint(r.grade_hi_hue)))
    return np.clip(s + push * 0.25, 0.0, 1.0).astype(np.float32)


def mask_dir(video_id) -> Path:
    d = (_media_root or Path(".")) / ".edit-masks" / str(int(video_id))
    d.mkdir(parents=True, exist_ok=True)
    return d


_bitmap_cache: Dict[str, np.ndarray] = {}


def load_bitmap(ref: str) -> Optional[np.ndarray]:
    """A brush mask's bitmap as float32 0..1 at its stored size, or None."""
    if not ref or not re.fullmatch(r"\d+/[a-z0-9]{8,32}", ref):
        return None
    if ref in _bitmap_cache:
        return _bitmap_cache[ref]
    vid, name = ref.split("/")
    p = (_media_root or Path(".")) / ".edit-masks" / vid / f"{name}.png"
    if not p.is_file():
        return None
    import cv2
    img = cv2.imread(str(p), cv2.IMREAD_UNCHANGED)
    if img is None:
        return None
    if img.ndim == 3:
        img = img[..., 0]
    out = (img.astype(np.float32) / 255.0)
    _bitmap_cache[ref] = out
    while len(_bitmap_cache) > 24:
        _bitmap_cache.pop(next(iter(_bitmap_cache)))
    return out


def bitmap_field(m: "Mask", src_hw, r: "Recipe", out_hw) -> Optional[np.ndarray]:
    """The brush bitmap carried through the photo's geometry and crop onto the
    output frame - the same mapping the pixels themselves take."""
    bm = load_bitmap(m.ref)
    if bm is None:
        return None
    import cv2
    sh, sw = int(src_hw[0]), int(src_hw[1])
    full = cv2.resize(bm, (sw, sh), interpolation=cv2.INTER_LINEAR)
    oh, ow = int(out_hw[0]), int(out_hw[1])
    u = (np.arange(ow, dtype=np.float32) + 0.5) / ow
    v = (np.arange(oh, dtype=np.float32) + 0.5) / oh
    uu, vv = np.meshgrid(u, v)
    su, sv = source_uv(uu, vv, r, aspect=sw / float(sh))
    out = cv2.remap(full, (su * sw - 0.5).astype(np.float32), (sv * sh - 0.5).astype(np.float32),
                    interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    return np.clip(out, 0.0, 1.0).astype(np.float32)


def _combine(a: np.ndarray, b: np.ndarray, mode: str) -> np.ndarray:
    if mode == "add":
        return np.maximum(a, b)
    if mode == "intersect":
        return a * b
    return a * (1.0 - b)


def _part_field(p, h: int, w: int, src_hw, r) -> Optional[np.ndarray]:
    """One shape, 0..1 with its own invert, no amount."""
    if p.kind == "brush":
        bf = bitmap_field(p, src_hw or (h, w), r, (h, w))
        if bf is None:
            return None
        return (1.0 - bf) if p.invert else bf
    from types import SimpleNamespace
    d = {k: getattr(p, k) for k in ("kind", "cx", "cy", "rx", "ry", "angle", "feather", "invert")}
    return mask_field(SimpleNamespace(**d, amount=1.0), h, w)


def mask_field(m: Mask, h: int, w: int) -> np.ndarray:
    """The mask's strength at every pixel, 0..1, as float32 HxW.

    Shapes are computed in a square-normalised space so a radial drawn as a
    circle stays a circle on a 3:2 frame instead of stretching into an egg.
    """
    ys = (np.arange(h, dtype=np.float32) + 0.5) / float(h)
    xs = (np.arange(w, dtype=np.float32) + 0.5) / float(w)
    gx, gy = np.meshgrid(xs, ys)

    aspect = float(w) / float(h) if h else 1.0
    dx = (gx - float(m.cx)) * aspect
    dy = (gy - float(m.cy))

    th = math.radians(float(m.angle))
    ct, st = math.cos(th), math.sin(th)
    u = dx * ct + dy * st
    v = -dx * st + dy * ct

    if m.kind == "linear":
        # Distance across the line, in units of ry. Positive on one side.
        t = v / max(1e-4, float(m.ry))
        # 0 well before the line, 1 well after it; feather sets how long the
        # ramp takes. A feather of 0 is still given a sliver, or the edge
        # aliases into a visible hard line.
        soft = max(0.02, float(m.feather))
        f = np.clip((t + soft) / (2.0 * soft), 0.0, 1.0)
    else:
        r = np.sqrt((u / max(1e-4, float(m.rx))) ** 2 +
                    (v / max(1e-4, float(m.ry))) ** 2)
        inner = 1.0 - float(m.feather)
        f = 1.0 - np.clip((r - inner) / max(1e-4, 1.0 - inner), 0.0, 1.0)

    # Smoothstep, so the falloff has no visible banding where it meets 0 or 1.
    f = f * f * (3.0 - 2.0 * f)
    if m.invert:
        f = 1.0 - f
    return (f * float(m.amount)).astype(np.float32)


def _apply_one_mask(s: np.ndarray, m: Mask, field: Optional[np.ndarray] = None) -> np.ndarray:
    """Develop the whole frame as the mask asks, then blend it back by the
    mask's strength. Blending the result rather than the settings is what
    makes a feathered edge look like a gradient instead of a cut-out."""
    f = field if field is not None else mask_field(m, s.shape[0], s.shape[1])
    rw = range_weight(s, m)
    if rw is not None:
        f = f * rw
    f = f[..., None]
    if not np.any(f > 0.001):
        return s

    lin = _srgb_to_linear(s)

    if m.temp_shift or m.tint_shift:
        lin = lin * wb_gains(6500.0 + float(m.temp_shift), float(m.tint_shift))
    if m.exposure:
        lin = lin * np.float32(2.0 ** m.exposure)

    if m.shadows or m.highlights:
        y = np.clip(lin @ LUMA, 0, 4).astype(np.float32)[..., None]
        if m.shadows:
            lin = lin * (1.0 + np.float32(m.shadows) *
                         (1.0 - _smoothstep(0.0, 0.25, y)).astype(np.float32))
        if m.highlights:
            lin = lin * (1.0 + np.float32(m.highlights) * _smoothstep(0.35, 1.0, y))

    if m.contrast:
        lin = PIVOT * np.power(np.clip(lin, 1e-6, None) / PIVOT,
                               np.float32(1.0 + m.contrast))

    out = _linear_to_srgb(lin)

    if m.whites:
        out = out + np.float32(m.whites) * _smoothstep(0.5, 1.0, out)
    if m.blacks:
        out = out + np.float32(m.blacks) * (1.0 - _smoothstep(0.0, 0.5, out))

    if m.saturation:
        lum = (out @ LUMA)[..., None].astype(np.float32)
        out = lum + (out - lum) * np.float32(1.0 + m.saturation)

    if m.clarity:
        # Exactly what the global clarity does, so a local one behaves the
        # way the slider above it already taught you it behaves.
        base = _big_blur(out)
        y = (out @ LUMA)[..., None].astype(np.float32)
        mid = (1.0 - np.abs(y - 0.5) * 2.0).astype(np.float32)
        out = np.clip(out + np.float32(m.clarity) * 0.8 * (out - base) * mid,
                      0.0, 1.0)

    out = np.clip(out, 0.0, 1.0)
    return (s * (1.0 - f) + out * f).astype(np.float32)


def apply_masks(s: np.ndarray, r: "Recipe", src_hw=None) -> np.ndarray:
    h, w = s.shape[:2]
    for m in (r.masks or []):
        if not (m.enabled and m.amount > 0):
            continue
        f = _part_field(m, h, w, src_hw, r)
        if f is None:
            continue
        p2 = getattr(m, "part2", None)
        if p2 is not None:
            g = _part_field(p2, h, w, src_hw, r)
            if g is not None:
                f = _combine(f, g, p2.mode)
        s = _apply_one_mask(s, m, (f * float(m.amount)).astype(np.float32))
    return s


class RemoveArea(BaseModel):
    """Something painted out with the Remove tool: a filled patch (RGBA PNG
    under .edit-masks, by ref) laid over box = (x, y, w, h), source 0..1."""
    ref: str = Field(..., pattern=r"^\d+/[a-z0-9]{8,32}$")
    box: List[float] = Field(..., min_length=4, max_length=4)
    kind: str = Field("remove", pattern="^(remove|pull)$")   # pull: a window view from a darker frame


class Spot(BaseModel):
    """One heal or clone spot, in SOURCE space (the photo before crop and
    geometry): the circle at (x, y) is replaced by the one at (sx, sy).
    Heal also matches the replacement to the colour and brightness around
    the spot, so the patch disappears into its surroundings."""
    x: float = Field(0.5, ge=0, le=1)
    y: float = Field(0.5, ge=0, le=1)
    sx: float = Field(0.5, ge=-0.5, le=1.5)
    sy: float = Field(0.5, ge=-0.5, le=1.5)
    r: float = Field(0.02, ge=0.001, le=0.3)      # radius, fraction of the width
    feather: float = Field(0.5, ge=0, le=1)
    opacity: float = Field(1, ge=0, le=1)
    mode: str = Field("heal", pattern="^(heal|clone)$")
    g: int = Field(0, ge=0)                       # spots painted in one stroke share a group


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
    # The rest of HSL: move a colour's hue (+-1 = +-30 degrees) and its
    # brightness, one family at a time - a greener lawn, a deeper sky.
    hue_red: float = Field(0, ge=-1, le=1)
    hue_orange: float = Field(0, ge=-1, le=1)
    hue_yellow: float = Field(0, ge=-1, le=1)
    hue_green: float = Field(0, ge=-1, le=1)
    hue_aqua: float = Field(0, ge=-1, le=1)
    hue_blue: float = Field(0, ge=-1, le=1)
    hue_purple: float = Field(0, ge=-1, le=1)
    hue_magenta: float = Field(0, ge=-1, le=1)
    lum_red: float = Field(0, ge=-1, le=1)
    lum_orange: float = Field(0, ge=-1, le=1)
    lum_yellow: float = Field(0, ge=-1, le=1)
    lum_green: float = Field(0, ge=-1, le=1)
    lum_aqua: float = Field(0, ge=-1, le=1)
    lum_blue: float = Field(0, ge=-1, le=1)
    lum_purple: float = Field(0, ge=-1, le=1)
    lum_magenta: float = Field(0, ge=-1, le=1)
    # Colour grading: a tint for the shadows, the midtones and the highlights.
    grade_sh_hue: float = Field(220, ge=0, le=360)
    grade_sh_sat: float = Field(0, ge=0, le=1)
    grade_mid_hue: float = Field(40, ge=0, le=360)
    grade_mid_sat: float = Field(0, ge=0, le=1)
    grade_hi_hue: float = Field(45, ge=0, le=360)
    grade_hi_sat: float = Field(0, ge=0, le=1)
    grade_balance: float = Field(0, ge=-1, le=1)
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
    # Local adjustments. Applied last, over the finished global grade, in the
    # order they were added.
    masks: List[Mask] = Field(default_factory=list)
    # Post-crop effects, on the finished frame
    vignette: float = Field(0, ge=-1, le=1)       # - darkens the edges, + lightens
    vig_mid: float = Field(0.5, ge=0, le=1)
    vig_feather: float = Field(0.5, ge=0, le=1)
    vig_round: float = Field(0, ge=0, le=1)       # 0 follows the frame's shape, 1 a circle
    grain: float = Field(0, ge=0, le=1)
    grain_size: float = Field(0.3, ge=0, le=1)
    # Lens: brighten the corners the lens darkened. lens_vig is the slider;
    # lens_vig_k the maker's own profile (from a DNG), applied together.
    lens_vig: float = Field(0, ge=0, le=1.5)
    lens_vig_k: List[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0, 0.0], min_length=5, max_length=5)
    # Heal / clone spots, applied first, on the photo as it came out of the camera.
    spots: List[Spot] = Field(default_factory=list, max_length=600)
    # Remove tool patches, laid over the photo before the spots.
    removes: List[RemoveArea] = Field(default_factory=list, max_length=200)
    # One-click fixes that are on, and exactly what each one added - so
    # switching one off takes back its own change and nothing else.
    fixes: Dict[str, Dict[str, float]] = Field(default_factory=dict)


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

DN_TAPS = 3            # 7x7: wide enough to clear phone-sensor grain at full size


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

    # How different a neighbour may be and still count as the same surface.
    # It grows with the slider: stronger means smoother. (It used to shrink
    # as the slider went up, so 100% did almost nothing.) Mirrored in the
    # preview shader (web/src/editor/shaders.ts DENOISE) - change both.
    # (At 100% it only took a third of the noise off; now it clears sensor
    # grain, while edges stronger than the range survive.)
    sr = float(0.015 + 0.22 * luma) if luma else 1.0
    sig = float(1.2 + 1.8 * max(luma, colour))
    lk = min(1.0, 1.6 * float(luma))
    y_acc = np.zeros_like(y); y_wsum = np.zeros_like(y)
    co_acc = np.zeros_like(co); cg_acc = np.zeros_like(cg); c_wsum = np.zeros_like(co)

    for dy in range(-DN_TAPS, DN_TAPS + 1):
        for dx in range(-DN_TAPS, DN_TAPS + 1):
            sy = yp[pad + dy:pad + dy + h, pad + dx:pad + dx + w]
            spatial = np.float32(np.exp(-0.5 * ((dx * dx + dy * dy) / (sig * sig))))
            if luma:
                wgt = spatial * np.exp(-0.5 * ((sy - y) / sr) ** 2).astype(np.float32)
                y_acc += sy * wgt
                y_wsum += wgt
            if colour:
                co_acc += cop[pad + dy:pad + dy + h, pad + dx:pad + dx + w] * spatial
                cg_acc += cgp[pad + dy:pad + dy + h, pad + dx:pad + dx + w] * spatial
                c_wsum += spatial

    if luma:
        y = y * (1.0 - lk) + (y_acc / np.maximum(y_wsum, 1e-6)) * lk
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


# --------------------------------------------------------------------------
# heal and clone (mirrors the SPOT shader)
# --------------------------------------------------------------------------

SPOT_RING = 24          # samples around the edge that the heal matches to
SPOT_RHO = 0.75         # the membrane is evaluated no closer to the edge than this


def _bilinear(img: np.ndarray, px: np.ndarray, py: np.ndarray) -> np.ndarray:
    """Sample at pixel coordinates (texel centres at i + 0.5), clamped to the
    edge - what a GL texture with LINEAR / CLAMP_TO_EDGE does."""
    h, w = img.shape[:2]
    x = np.clip(px - 0.5, 0.0, w - 1.0)
    y = np.clip(py - 0.5, 0.0, h - 1.0)
    x0 = np.floor(x).astype(np.int32)
    y0 = np.floor(y).astype(np.int32)
    x1 = np.minimum(x0 + 1, w - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    fx = (x - x0)[..., None].astype(np.float32)
    fy = (y - y0)[..., None].astype(np.float32)
    top = img[y0, x0] * (1 - fx) + img[y0, x1] * fx
    bot = img[y1, x0] * (1 - fx) + img[y1, x1] * fx
    return (top * (1 - fy) + bot * fy).astype(np.float32)


def _ring_offsets(img, dx, dy, sx, sy, R):
    """Colour difference between the ring around the target and the ring
    around the source, at SPOT_RING angles, each a 5-tap average."""
    k = np.arange(SPOT_RING, dtype=np.float32) * np.float32(2 * np.pi / SPOT_RING)
    cx, cy = np.cos(k) * R, np.sin(k) * R
    t = max(1.0, 0.15 * R)
    taps = [(0.0, 0.0), (t, 0.0), (-t, 0.0), (0.0, t), (0.0, -t)]
    d = sum(_bilinear(img, dx + cx + a, dy + cy + b) for a, b in taps) / 5.0
    s = sum(_bilinear(img, sx + cx + a, sy + cy + b) for a, b in taps) / 5.0
    return (d - s).astype(np.float32), k


def apply_removes(img: np.ndarray, removes, window=None) -> np.ndarray:
    """Lay the Remove tool's patches over the photo, in order - the same as
    the SPOT shader's patch mode. window = (x0, y0, full_w, full_h) when img
    is only part of the photo (the remove tool working on a crop)."""
    import remove_ai
    h, w = img.shape[:2]
    ox, oy, W, H = window if window else (0, 0, w, h)
    out = img
    for rm in removes:
        ref = rm.ref if hasattr(rm, "ref") else rm.get("ref")
        box = rm.box if hasattr(rm, "box") else rm.get("box")
        if not ref or not box or not re.fullmatch(r"\d+/[a-z0-9]{8,32}", ref):
            continue
        vid, name = ref.split("/")
        patch = remove_ai.load_patch((_media_root or Path(".")) / ".edit-masks" / vid / f"{name}.png")
        if patch is None:
            continue
        bx, by, bw, bh = (float(box[0]) * W, float(box[1]) * H, float(box[2]) * W, float(box[3]) * H)
        if bw <= 0 or bh <= 0:
            continue
        x0, x1 = max(0, int(math.floor(bx)) - ox), min(w, int(math.ceil(bx + bw)) + 1 - ox)
        y0, y1 = max(0, int(math.floor(by)) - oy), min(h, int(math.ceil(by + bh)) + 1 - oy)
        if x0 >= x1 or y0 >= y1:
            continue
        gy, gx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
        qx = (gx + 0.5 + ox - bx) / np.float32(bw)
        qy = (gy + 0.5 + oy - by) / np.float32(bh)
        inside = (qx >= 0) & (qx <= 1) & (qy >= 0) & (qy <= 1)
        if not inside.any():
            continue
        ph, pw = patch.shape[:2]
        smp = _bilinear(patch, qx * pw, qy * ph)
        a = np.where(inside, smp[..., 3], 0.0)[..., None].astype(np.float32)
        if out is img:
            out = img.copy()
        region = out[y0:y1, x0:x1]
        out[y0:y1, x0:x1] = region + (smp[..., :3] - region) * a
    return out.astype(np.float32)


def apply_spots(img: np.ndarray, spots) -> np.ndarray:
    """Heal/clone, one spot after another, each reading the result so far."""
    h, w = img.shape[:2]
    out = img.copy()
    for sp in spots:
        if sp.opacity <= 0:
            continue
        R = float(sp.r) * w
        dx, dy = float(sp.x) * w, float(sp.y) * h
        sx, sy = float(sp.sx) * w, float(sp.sy) * h
        x0, x1 = max(0, int(math.floor(dx - R))), min(w, int(math.ceil(dx + R)) + 1)
        y0, y1 = max(0, int(math.floor(dy - R))), min(h, int(math.ceil(dy + R)) + 1)
        if x0 >= x1 or y0 >= y1:
            continue
        gy, gx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
        px, py = gx + 0.5, gy + 0.5
        ox, oy = px - dx, py - dy
        rho = np.sqrt(ox * ox + oy * oy) / np.float32(max(R, 1e-6))
        inside = rho < 1.0
        if not inside.any():
            continue
        f = max(0.01, float(sp.feather))
        a = (1.0 - _smoothstep(1.0 - f, 1.0, rho)) * np.float32(sp.opacity)
        a = np.where(inside, a, 0.0).astype(np.float32)
        src = _bilinear(out, px - dx + sx, py - dy + sy)
        if sp.mode == "heal":
            offs, ang = _ring_offsets(out, dx, dy, sx, sy, R)
            th = np.arctan2(oy, ox)
            rc = np.minimum(rho, SPOT_RHO)[..., None]
            cosd = np.cos(th[..., None] - ang[None, None, :])
            P = (1.0 - rc * rc) / (1.0 - 2.0 * rc * cosd + rc * rc)
            off = (P[..., None] * offs[None, None, :, :]).sum(2) / P.sum(-1)[..., None]
            src = src + off
        src = np.clip(src, 0.0, 1.0)
        region = out[y0:y1, x0:x1]
        out[y0:y1, x0:x1] = region + (src - region) * a[..., None]
    return out.astype(np.float32)


def grain_cells(size: float) -> float:
    return 1400.0 - 1150.0 * max(0.0, min(1.0, float(size)))


def _ghash(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    with np.errstate(over="ignore"):
        h = x.astype(np.uint32) * np.uint32(374761393) + y.astype(np.uint32) * np.uint32(668265263)
        h = (h ^ (h >> np.uint32(13))) * np.uint32(1274126177)
        h ^= h >> np.uint32(16)
    return (h & np.uint32(16777215)).astype(np.float32) / np.float32(16777215.0)


def _gnoise(px: np.ndarray, py: np.ndarray) -> np.ndarray:
    ix, iy = np.floor(px), np.floor(py)
    fx, fy = (px - ix).astype(np.float32), (py - iy).astype(np.float32)
    fx = fx * fx * (3 - 2 * fx)
    fy = fy * fy * (3 - 2 * fy)
    x, y = ix.astype(np.int64), iy.astype(np.int64)
    a, b = _ghash(x, y), _ghash(x + 1, y)
    c, d = _ghash(x, y + 1), _ghash(x + 1, y + 1)
    return (a + (b - a) * fx) * (1 - fy) + (c + (d - c) * fx) * fy


def apply_finish(s: np.ndarray, r: "Recipe") -> np.ndarray:
    """Post-crop vignette and film grain, placed on the output frame. Mirrors
    finish() in the FINAL shader."""
    h, w = s.shape[:2]
    asp = w / float(h)
    u = (np.arange(w, dtype=np.float32) + 0.5) / w
    v = (np.arange(h, dtype=np.float32) + 0.5) / h
    pu, pv = np.meshgrid(u, v)
    if r.vignette:
        kx = 1.0 + (asp - 1.0) * float(r.vig_round)
        qx, qy = (pu - 0.5) * 2 * kx, (pv - 0.5) * 2
        rr = np.sqrt(qx * qx + qy * qy) / math.sqrt(kx * kx + 1.0)
        f = max(0.01, float(r.vig_feather))
        wv = _smoothstep(float(r.vig_mid) - f * 0.5, float(r.vig_mid) + f * 0.5, rr)[..., None]
        if r.vignette < 0:
            s = s * (1.0 + np.float32(r.vignette) * wv)
        else:
            s = s + (1.0 - s) * (np.float32(r.vignette) * wv)
    if r.grain:
        n_ = grain_cells(r.grain_size)
        gx, gy = pu * asp * n_, pv * n_
        n = _gnoise(gx, gy) + _gnoise(gx * 2.03 + 17.0, gy * 2.03 + 17.0) * 0.5
        n = n / 1.5 - 0.5
        y = (np.clip(s, 0, 1) @ LUMA)
        wt = 1.0 - 0.6 * np.abs(2 * y - 1) ** 2
        s = s + (np.float32(r.grain) * 0.16 * n * wt)[..., None]
    return np.clip(s, 0.0, 1.0).astype(np.float32)


def apply_lens_vignette(rgb01: np.ndarray, r: "Recipe", src_hw=None) -> np.ndarray:
    """Undo the lens's corner falloff, in linear light. The gain is worked out
    on the photo as it came from the camera (r = 1 at its corner), for each
    output pixel through the same mapping the pixels took - the order the
    GEOM shader does it in, so sharp highlights agree too."""
    h, w = rgb01.shape[:2]
    sh, sw = (src_hw or (h, w))
    asp = sw / float(sh)
    u = (np.arange(w, dtype=np.float32) + 0.5) / w
    v = (np.arange(h, dtype=np.float32) + 0.5) / h
    uu, vv = np.meshgrid(u, v)
    if has_geometry(r):
        uu, vv = source_uv(uu, vv, r, aspect=asp)
    gx, gy = (uu - 0.5) * asp, vv - 0.5
    r2 = (gx * gx + gy * gy) / np.float32((asp / 2) ** 2 + 0.25)
    k = list(r.lens_vig_k)
    gain = 1.0 + (k[0] + float(r.lens_vig)) * r2 + k[1] * r2 ** 2 + k[2] * r2 ** 3 + k[3] * r2 ** 4 + k[4] * r2 ** 5
    lin = _srgb_to_linear(np.clip(rgb01, 0, 1)) * gain[..., None].astype(np.float32)
    return np.clip(_linear_to_srgb(lin), 0, 1).astype(np.float32)


def apply_recipe(rgb01: np.ndarray, r: Recipe) -> np.ndarray:
    """rgb01: float32 HxWx3 in 0..1 sRGB. Returns the same, developed."""
    src_hw = rgb01.shape[:2]
    if r.removes:
        rgb01 = apply_removes(rgb01, r.removes)
    if r.spots:
        rgb01 = apply_spots(rgb01, r.spots)
    if has_geometry(r):
        rgb01 = apply_geometry(rgb01, r)
    if r.lens_vig or any(r.lens_vig_k):
        rgb01 = apply_lens_vignette(rgb01, r, src_hw)
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

    s = apply_hsl(s, r)
    s = apply_grade(s, r)

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

    # Local adjustments, over the finished global grade and before sharpening
    # and the watermark - so a mask cannot fight the sharpener and the logo
    # still sits on top of everything.
    if r.masks:
        s = apply_masks(s, r, src_hw)

    if r.sharpen:
        import cv2
        blur = cv2.GaussianBlur(s, (0, 0), 1.0)
        s = np.clip(s + np.float32(r.sharpen) * (s - blur), 0.0, 1.0)

    if r.vignette or r.grain:
        s = apply_finish(s, r)

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
            import dng_render
            look = dng_render.look_of(path)
            with rawpy.imread(path) as raw:
                if look is not None:
                    # phone DNGs (ProRAW...): exposure push, local tone map, curve
                    long_edge = max(raw.sizes.width, raw.sizes.height)
                    half = bool(max_dim) and max_dim <= long_edge // 2
                    img = dng_render.render(raw, look, half=half, max_dim=max_dim)
                else:
                    img = raw.postprocess(use_camera_wb=True, no_auto_bright=True, output_bps=8)
        except Exception as e:
            print(f"  [edit] could not develop {Path(path).name}: {e}", flush=True)
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
        # In the system temp folder, never beside the photo: a scratch file in
        # the shoot folder got catalogued by the next scan as a photo of its own.
        import tempfile
        tmp = os.path.join(tempfile.gettempdir(), f"zerko-edittmp-{uuid.uuid4().hex}.jpg")
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
    if img.dtype == np.float32:
        return img
    return (img.astype(np.float32) / 255.0)


def _recipe_of(db: Session, video_id: int) -> Recipe:
    row = db.query(PhotoEdit).filter(PhotoEdit.video_id == video_id).first()
    if not row:
        return Recipe()
    try:
        return Recipe(**json.loads(row.recipe))
    except Exception:
        return Recipe()


class PhotoFrame(Base):
    """Where a photo sits in a frame of another shape, per shape (ratio as
    "1.7778"): 0..1 along the side that gets cut."""
    __tablename__ = "photo_frames"
    video_id = Column(Integer, primary_key=True)
    ratio = Column(String, primary_key=True)
    pos = Column(Float, nullable=False, default=0.5)


class PhotoCopy(Base):
    """A virtual copy: another edit of the same photo (a twilight version, a
    black and white...) without a second file on disk, as in Lightroom."""
    __tablename__ = "photo_copies"
    id = Column(Integer, primary_key=True)
    video_id = Column(Integer, index=True, nullable=False)
    name = Column(String, nullable=False)
    recipe = Column(String, nullable=False, default="{}")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


def _copy_row(db: Session, video_id: int, copy: int) -> "PhotoCopy":
    row = db.query(PhotoCopy).filter(PhotoCopy.id == copy, PhotoCopy.video_id == video_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="That virtual copy is gone")
    return row


@router.get("/{video_id}")
def get_recipe(video_id: int, copy: Optional[int] = None, db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    if copy:
        c = _copy_row(db, video_id, copy)
        return {"video_id": video_id, "copy": c.id, "name": c.name, "recipe": json.loads(c.recipe or "{}"),
                "updated_at": c.updated_at.isoformat() if c.updated_at else None}
    row = db.query(PhotoEdit).filter(PhotoEdit.video_id == video_id).first()
    return {
        "video_id": video_id,
        "recipe": json.loads(row.recipe) if row else {},
        "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
    }


@router.get("/{video_id}/copies")
def list_copies(video_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    rows = db.query(PhotoCopy).filter(PhotoCopy.video_id == video_id).order_by(PhotoCopy.id).all()
    return {"copies": [{"id": c.id, "name": c.name, "updated_at": c.updated_at.isoformat() if c.updated_at else None}
                       for c in rows]}


class NewCopy(BaseModel):
    name: str = Field("", max_length=80)
    recipe: Optional[dict] = None       # the settings to start from (the current edit); none = as shot


@router.post("/{video_id}/copies")
def make_copy(video_id: int, body: NewCopy, db: Session = Depends(get_db),
              current_user: User = Depends(get_current_user)):
    n = db.query(PhotoCopy).filter(PhotoCopy.video_id == video_id).count()
    rec = Recipe(**body.recipe) if body.recipe else Recipe()
    c = PhotoCopy(video_id=video_id, name=(body.name or "").strip() or f"Copy {n + 1}",
                  recipe=json.dumps(rec.model_dump()))
    db.add(c)
    db.commit()
    db.refresh(c)
    return {"id": c.id, "name": c.name}


class RenameCopy(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)


@router.patch("/{video_id}/copies/{copy_id}")
def rename_copy(video_id: int, copy_id: int, body: RenameCopy, db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    c = _copy_row(db, video_id, copy_id)
    c.name = body.name.strip()
    db.commit()
    return {"id": c.id, "name": c.name}


@router.delete("/{video_id}/copies/{copy_id}")
def delete_copy(video_id: int, copy_id: int, db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    db.query(PhotoCopy).filter(PhotoCopy.id == copy_id, PhotoCopy.video_id == video_id).delete()
    db.commit()
    return {"deleted": True}


@router.post("/{video_id}")
def save_recipe(video_id: int, recipe: Recipe, copy: Optional[int] = None, db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    body = json.dumps(recipe.model_dump())
    if copy:
        c = _copy_row(db, video_id, copy)
        c.recipe = body
        c.updated_at = datetime.utcnow()
        db.commit()
        return {"saved": True}
    row = db.query(PhotoEdit).filter(PhotoEdit.video_id == video_id).first()
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


def _prune_full_bases(folder: Path, keep: int = 40) -> None:
    """Full-resolution editor copies are big (10MB+ each): keep the most
    recently used ones and let the rest be made again when next opened."""
    try:
        big = [p for p in folder.glob("*.jpg") if int(p.stem.rsplit("_", 1)[-1]) > 4000]
        big.sort(key=lambda p: p.stat().st_atime, reverse=True)
        for p in big[keep:]:
            p.unlink(missing_ok=True)
    except Exception as e:
        print(f"photo_edit: could not tidy the full-size copies: {e}", flush=True)


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

    return FileResponse(str(_base_file(video_id, path, w)), media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})


def _base_file(video_id: int, path: str, w: int) -> Path:
    # up to 4000 for the quick preview; beyond that it is the editor's
    # full-resolution copy (capped at what a GPU texture can hold)
    w = max(600, min(int(w), 16384))
    st = os.stat(path)
    out = (_media_root / ".edit-base" /
           f"{video_id}_{int(st.st_mtime)}_{st.st_size}_r3_{w}.jpg")
    if not out.exists() or out.stat().st_size == 0:
        out.parent.mkdir(parents=True, exist_ok=True)
        with _preview_lock:
            if not out.exists():
                import cv2
                rgb = _read_rgb(path, max_dim=w)
                bgr = cv2.cvtColor((rgb * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)
                if not cv2.imwrite(str(out), bgr, [cv2.IMWRITE_JPEG_QUALITY, 94]):
                    raise HTTPException(status_code=500, detail="Could not make a preview")
                if w > 4000:
                    _prune_full_bases(out.parent)
    return out


@router.get("/{video_id}/developed")
def developed(request: Request, video_id: int, token: Optional[str] = None, w: int = 1600,
              copy: Optional[int] = None, db: Session = Depends(get_db)):
    """The photo with its edit applied, as a JPEG - for the editor's
    reference view (a finished photo beside the one being worked on)."""
    if token:
        get_user_from_token(token, db)
    else:
        auth = request.headers.get("Authorization") or ""
        if not auth.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
        get_user_from_token(auth[7:], db)
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Photo not found")
    path = _resolve(video.filepath)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="The photo file is not on disk")
    w = max(600, min(int(w), 4000))
    if copy:
        c = _copy_row(db, video_id, copy)
        rtxt = c.recipe or "{}"
    else:
        row = db.query(PhotoEdit).filter(PhotoEdit.video_id == video_id).first()
        rtxt = row.recipe if row else "{}"
    import hashlib
    key = hashlib.sha1(rtxt.encode()).hexdigest()[:12]
    base = _base_file(video_id, path, w)
    out = base.parent / f"dev_{video_id}_{copy or 0}_{key}_{base.stat().st_mtime_ns % 10**9}_{w}.jpg"
    if not out.exists() or out.stat().st_size == 0:
        import cv2
        for old in base.parent.glob(f"dev_{video_id}_{copy or 0}_*_{w}.jpg"):
            old.unlink(missing_ok=True)
        try:
            rec = Recipe(**json.loads(rtxt))
        except Exception:
            rec = Recipe()
        bgr = cv2.imread(str(base), cv2.IMREAD_COLOR)
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        res = apply_recipe(rgb, rec)
        cv2.imwrite(str(out), cv2.cvtColor((np.clip(res, 0, 1) * 255).astype(np.uint8), cv2.COLOR_RGB2BGR),
                    [cv2.IMWRITE_JPEG_QUALITY, 90])
    return FileResponse(str(out), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=600"})


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
        ev = float(np.log2(0.45 / max(med, 0.01)))
        # up to three stops: an underexposed bracket frame really is that far off
        out["exposure"] = round(max(-2.0, min(3.0, ev)), 2)

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


class SnapshotBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    recipe: Recipe


def _snaps(row) -> list:
    try:
        return json.loads(row.snapshots or "[]") if row else []
    except Exception:
        return []


@router.get("/{video_id}/snapshots")
def list_snapshots(video_id: int, db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    row = db.query(PhotoEdit).filter(PhotoEdit.video_id == video_id).first()
    return {"snapshots": _snaps(row)}


@router.post("/{video_id}/snapshots")
def add_snapshot(video_id: int, body: SnapshotBody, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    """Keep this version of the edit under a name, to come back to or compare."""
    row = db.query(PhotoEdit).filter(PhotoEdit.video_id == video_id).first()
    if not row:
        row = PhotoEdit(video_id=video_id, recipe=json.dumps(body.recipe.model_dump()), updated_at=datetime.utcnow())
        db.add(row)
    snaps = [x for x in _snaps(row) if x.get("name") != body.name.strip()]
    snaps.append({"name": body.name.strip(), "at": datetime.utcnow().isoformat() + "Z",
                  "by": getattr(current_user, "username", None), "recipe": body.recipe.model_dump()})
    row.snapshots = json.dumps(snaps[-50:])
    db.commit()
    return {"snapshots": _snaps(row)}


@router.delete("/{video_id}/snapshots/{index}")
def delete_snapshot(video_id: int, index: int, db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    row = db.query(PhotoEdit).filter(PhotoEdit.video_id == video_id).first()
    snaps = _snaps(row)
    if not row or not (0 <= index < len(snaps)):
        raise HTTPException(status_code=404, detail="No such snapshot")
    snaps.pop(index)
    row.snapshots = json.dumps(snaps)
    db.commit()
    return {"snapshots": snaps}


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


_full_cache: "Dict[tuple, np.ndarray]" = {}
_full_lock = threading.Lock()


def _full_size(path: str) -> np.ndarray:
    """The whole photo at full resolution, as uint8 RGB - remembered for the
    last two photos, because the 100% view asks for a new tile on every pan
    and decoding a 20MP RAW each time made panning take seconds per step.
    Kept as 8-bit (60MB for 20MP) rather than float (240MB)."""
    st = os.stat(path)
    key = (path, st.st_mtime_ns, st.st_size)
    with _full_lock:
        hit = _full_cache.get(key)
    if hit is None:
        hit = (np.clip(_read_rgb(path), 0, 1) * 255).astype(np.uint8)
        with _full_lock:
            _full_cache[key] = hit
            while len(_full_cache) > 2:
                _full_cache.pop(next(iter(_full_cache)))
    return hit


# --------------------------------------------------------------------------
# brush masks: bitmaps in source space
# --------------------------------------------------------------------------

_MASK_NAME = re.compile(r"^[a-z0-9]{8,32}$")


def _mask_gc(video_id: int, keep: set):
    """Old strokes pile up (each one is a new version, so undo can step back
    through them). Keep the newest 80 plus whatever the saved edit uses."""
    d = mask_dir(video_id)
    files = sorted(d.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files[80:]:
        if p.stem not in keep:
            try:
                p.unlink()
            except OSError:
                pass


def _used_refs(db: Session, video_id: int) -> set:
    row = db.query(PhotoEdit).filter(PhotoEdit.video_id == video_id).first()
    if not row:
        return set()
    try:
        rec = json.loads(row.recipe or "{}")
    except Exception:
        return set()
    used = {str(m.get("ref", "")).split("/")[-1] for m in rec.get("masks", []) if m.get("ref")}
    used |= {str((m.get("part2") or {}).get("ref", "")).split("/")[-1] for m in rec.get("masks", []) if (m.get("part2") or {}).get("ref")}
    return used


@router.put("/{video_id}/masks/{name}")
async def put_mask(video_id: int, name: str, request: Request, db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    """Store a brush mask (a greyscale PNG). Each stroke is saved under a new
    name, so the recipe's undo history can point back at earlier ones."""
    if not _MASK_NAME.match(name):
        raise HTTPException(status_code=400, detail="Bad mask name")
    if not db.query(Video).filter(Video.id == video_id).first():
        raise HTTPException(status_code=404, detail="Photo not found")
    body = await request.body()
    if len(body) > 40 * 1024 * 1024 or not body.startswith(b"\x89PNG"):
        raise HTTPException(status_code=400, detail="Send a PNG")
    import cv2
    img = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None or img.size == 0 or max(img.shape[:2]) > 8000:
        raise HTTPException(status_code=400, detail="That PNG could not be read")
    if img.ndim == 3:
        img = img[..., 2] if img.shape[2] >= 3 else img[..., 0]   # BGR(A): take red, the painted channel
    cv2.imwrite(str(mask_dir(video_id) / f"{name}.png"), img)
    _bitmap_cache.pop(f"{video_id}/{name}", None)
    _mask_gc(video_id, _used_refs(db, video_id))
    return {"ref": f"{video_id}/{name}", "w": int(img.shape[1]), "h": int(img.shape[0])}


@router.get("/{video_id}/masks/{name}")
def get_mask(video_id: int, name: str, request: Request, token: Optional[str] = None,
             db: Session = Depends(get_db)):
    if token:
        get_user_from_token(token, db)
    else:
        auth = request.headers.get("Authorization")
        if not auth or not auth.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
        get_user_from_token(auth[7:], db)
    if not _MASK_NAME.match(name):
        raise HTTPException(status_code=400, detail="Bad mask name")
    p = mask_dir(video_id) / f"{name}.png"
    if not p.is_file():
        raise HTTPException(status_code=404, detail="No such mask")
    return FileResponse(str(p), media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


# --------------------------------------------------------------------------
# the Remove tool
# --------------------------------------------------------------------------

# the last photo the remove tool worked on, as 8-bit (a 45MP float copy would
# be half a gigabyte), so painting out five things does not decode it five times
_remove_src: Dict[str, object] = {}
_remove_lock = threading.Lock()


def _full_u8(path: str) -> np.ndarray:
    """The photo at full size, 8-bit, the last two kept (hold _remove_lock)."""
    st = os.stat(path)
    key = f"{path}|{st.st_mtime_ns}|{st.st_size}"
    if key in _remove_src:
        return _remove_src[key]
    while len(_remove_src) >= 2:
        _remove_src.pop(next(iter(_remove_src)))
    rgb = _read_rgb(path, max_dim=0, strict=True)
    img = (np.clip(rgb, 0, 1) * 255 + 0.5).astype(np.uint8)
    del rgb
    _remove_src[key] = img
    return img


def _thumb_gray(v: Video) -> Optional[np.ndarray]:
    import cv2
    if not v.thumbnail_path or not _media_root:
        return None
    p = Path(_media_root) / "thumbnails" / Path(v.thumbnail_path).name
    im = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
    return None if im is None else cv2.resize(im, (128, 85), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0


def _darker_frames(db: Session, video: Video, limit: int = 8) -> list:
    """Other frames of the same shot that are darker - brackets, or a frame
    exposed for the windows: same picture (edges line up), less light."""
    import cv2
    me = _thumb_gray(video)
    if me is None or video.folder_id is None:
        return []
    def edges(g):
        gx, gy = cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1)
        e = np.sqrt(gx * gx + gy * gy)
        return (e - e.mean()) / (e.std() + 1e-6)
    em, lm = edges(me), float(me.mean()) + 1e-4
    out = []
    for o in db.query(Video).filter(Video.folder_id == video.folder_id, Video.id != video.id,
                                    Video.media_type == "photo", Video.is_active != False).limit(400).all():  # noqa: E712
        g = _thumb_gray(o)
        if g is None:
            continue
        sim = float((edges(g) * em).mean())
        ev = float(np.log2((float(g.mean()) + 1e-4) / lm) * 2.2)   # thumbnails are gamma-encoded
        if sim > 0.45 and ev < -0.4:
            out.append({"id": o.id, "filename": o.filename, "ev": round(ev, 1), "match": round(sim, 2)})
    out.sort(key=lambda x: (-x["match"], x["ev"]))
    return out[:limit]


class WindowsBody(BaseModel):
    w: int = Field(..., ge=16, le=16384)
    h: int = Field(..., ge=16, le=16384)


@router.post("/{video_id}/windows")
def find_windows(video_id: int, body: WindowsBody, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    """Find the blown-out windows: a brush mask of them, and the darker frames
    of the same shot a view could be taken from."""
    import cv2
    import editor_ai
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Photo not found")
    path = _resolve(video.filepath)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="The photo file is not on disk")
    rgb = _read_rgb(path, max_dim=2400)
    u8 = (np.clip(rgb, 0, 1) * 255).astype(np.uint8)
    st_ = os.stat(path)
    m, n = editor_ai.find_windows(rgb, (body.h, body.w), u8=u8, key=(path, st_.st_mtime_ns, u8.shape))
    if not n:
        return {"count": 0, "ref": None, "candidates": []}
    name = uuid.uuid4().hex[:16]
    cv2.imwrite(str(mask_dir(video_id) / f"{name}.png"), (np.clip(m, 0, 1) * 255).astype(np.uint8))
    return {"count": n, "ref": f"{video_id}/{name}", "candidates": _darker_frames(db, video)}


class PullBody(BaseModel):
    mask_ref: str = Field(..., pattern=r"^\d+/[a-z0-9]{8,32}$")
    from_id: int
    removes: List[RemoveArea] = Field(default_factory=list, max_length=200)


@router.post("/{video_id}/window-pull")
def window_pull(video_id: int, body: PullBody, db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    """Take the view through the windows from a darker frame of the same shot:
    lined up with this one (a hand-held bracket moves a little), laid in
    through the window mask with a soft edge. Comes back as a patch, like the
    Remove tool's."""
    import cv2
    vids = {v.id: v for v in db.query(Video).filter(Video.id.in_([video_id, body.from_id])).all()}
    if video_id not in vids or body.from_id not in vids:
        raise HTTPException(status_code=404, detail="Photo not found")
    pa, pb = _resolve(vids[video_id].filepath), _resolve(vids[body.from_id].filepath)
    if not pa or not pb or not os.path.exists(pa) or not os.path.exists(pb):
        raise HTTPException(status_code=404, detail="A photo file is not on disk")
    mask_small = load_bitmap(body.mask_ref)
    if mask_small is None:
        raise HTTPException(status_code=400, detail="Find the windows first")
    with _remove_lock:
        me = _full_u8(pa)
        other = _full_u8(pb)
    H, W = me.shape[:2]
    if other.shape[:2] != (H, W):
        other = cv2.resize(other, (W, H), interpolation=cv2.INTER_AREA)
    # line the darker frame up with this one (ECC ignores the exposure difference)
    k = 1400 / max(H, W)
    ga = cv2.cvtColor(cv2.resize(me, (int(W * k), int(H * k)), interpolation=cv2.INTER_AREA), cv2.COLOR_RGB2GRAY)
    gb = cv2.cvtColor(cv2.resize(other, (int(W * k), int(H * k)), interpolation=cv2.INTER_AREA), cv2.COLOR_RGB2GRAY)
    warp = np.eye(3, dtype=np.float32)
    try:
        _, warp = cv2.findTransformECC(ga.astype(np.float32), gb.astype(np.float32), warp, cv2.MOTION_HOMOGRAPHY,
                                       (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 80, 1e-5), None, 5)
    except cv2.error:
        warp = np.eye(3, dtype=np.float32)
    S = np.diag([1 / k, 1 / k, 1]).astype(np.float32)
    full_warp = S @ warp @ np.linalg.inv(S)
    mask = cv2.resize(mask_small, (W, H), interpolation=cv2.INTER_LINEAR)
    ys, xs = np.nonzero(mask > 0.02)
    if not len(xs):
        raise HTTPException(status_code=400, detail="No windows in the mask")
    grow = max(4, int(W * 0.004))
    x0, y0 = max(0, int(xs.min()) - grow * 3), max(0, int(ys.min()) - grow * 3)
    x1, y1 = min(W, int(xs.max()) + 1 + grow * 3), min(H, int(ys.max()) + 1 + grow * 3)
    # ECC's warp maps this photo's pixels to the darker frame's; for the patch,
    # each of its pixels (offset by its corner) is looked up through it
    corner = np.array([[1, 0, x0], [0, 1, y0], [0, 0, 1]], np.float32)
    part = cv2.warpPerspective(other, (full_warp @ corner).astype(np.float32), (x1 - x0, y1 - y0),
                               flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_REPLICATE)
    a = mask[y0:y1, x0:x1]
    a = cv2.GaussianBlur(cv2.dilate(a, np.ones((grow, grow), np.uint8)), (0, 0), grow / 2)
    a = np.clip(np.maximum(a, mask[y0:y1, x0:x1]), 0, 1)
    rgba = np.dstack([part.astype(np.float32) / 255.0, a]).astype(np.float32)
    name = "rm" + uuid.uuid4().hex[:14]
    bgra = cv2.cvtColor((np.clip(rgba, 0, 1) * 255 + 0.5).astype(np.uint8), cv2.COLOR_RGBA2BGRA)
    if not cv2.imwrite(str(mask_dir(video_id) / f"{name}.png"), bgra):
        raise HTTPException(status_code=500, detail="Could not save the view")
    return {"ref": f"{video_id}/{name}", "box": [x0 / W, y0 / H, (x1 - x0) / W, (y1 - y0) / H], "kind": "pull"}


class RemoveBody(BaseModel):
    points: List[List[float]] = Field(..., min_length=1, max_length=4000)
    r: float = Field(..., gt=0, le=0.3)           # brush radius, fraction of the width
    removes: List[RemoveArea] = Field(default_factory=list, max_length=200)   # the ones already made


@router.get("/remove/status")
def remove_status(current_user: User = Depends(get_current_user)):
    import remove_ai
    return remove_ai.status()


@router.post("/remove/prepare")
def remove_prepare(current_user: User = Depends(get_current_user)):
    """Start fetching the remove model (92 MB) the first time the tool is opened."""
    import remove_ai
    remove_ai.ensure_download()
    return remove_ai.status()


@router.post("/{video_id}/remove")
def remove_object(video_id: int, body: RemoveBody, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    """Fill what was painted from its surroundings; returns the patch's ref
    and box to add to recipe.removes."""
    import cv2
    import remove_ai
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Photo not found")
    path = _resolve(video.filepath)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="The photo file is not on disk")
    st = os.stat(path)
    with _remove_lock:
        full = _full_u8(path)
        H, W = full.shape[:2]
        mask = remove_ai.stroke_mask(W, H, body.points, body.r)
        ys, xs = np.nonzero(mask)
        if not len(xs):
            raise HTTPException(status_code=400, detail="Paint over what should go")
        # work on a window around the stroke only, with what was removed before already in it
        m = int(max(xs.max() - xs.min(), ys.max() - ys.min()) * 1.2) + 64
        x0, y0 = max(0, int(xs.min()) - m), max(0, int(ys.min()) - m)
        x1, y1 = min(W, int(xs.max()) + 1 + m), min(H, int(ys.max()) + 1 + m)
        win = full[y0:y1, x0:x1].astype(np.float32) / 255.0
        if body.removes:
            win = apply_removes(win, body.removes, window=(x0, y0, W, H))
        try:
            patch, (px0, py0, px1, py1), engine = remove_ai.fill(win, mask[y0:y1, x0:x1], feather_px=max(2.0, body.r * W * 0.35))
        except ValueError:
            raise HTTPException(status_code=400, detail="Paint over what should go")
    name = "rm" + uuid.uuid4().hex[:14]
    out = mask_dir(video_id) / f"{name}.png"
    bgra = cv2.cvtColor((np.clip(patch, 0, 1) * 255 + 0.5).astype(np.uint8), cv2.COLOR_RGBA2BGRA)
    if not cv2.imwrite(str(out), bgra):
        raise HTTPException(status_code=500, detail="Could not save the fill")
    ax0, ay0, ax1, ay1 = px0 + x0, py0 + y0, px1 + x0, py1 + y0
    return {"ref": f"{video_id}/{name}", "box": [ax0 / W, ay0 / H, (ax1 - ax0) / W, (ay1 - ay0) / H],
            "engine": engine, "model": remove_ai.status()}


class SelectBody(BaseModel):
    kind: str = Field(..., pattern="^(sky|subject|box|click)$")
    box: Optional[List[float]] = None              # x0, y0, x1, y1 in 0..1 of the source
    points: Optional[List[List[float]]] = None     # click: [[x, y], ...] in 0..1 of the source
    labels: Optional[List[int]] = None             # click: 1 = include, 0 = leave out
    w: int = Field(..., ge=16, le=8000)            # the size of the editor's bitmap
    h: int = Field(..., ge=16, le=8000)


@router.get("/ai/status")
def ai_status(current_user: User = Depends(get_current_user)):
    """Whether the selection models are here (they download once, on first use)."""
    import editor_ai
    return editor_ai.ai_status()


@router.post("/{video_id}/select")
def ai_select(video_id: int, body: SelectBody, db: Session = Depends(get_db),
              current_user: User = Depends(get_current_user)):
    """Make a mask for you - the sky, the object you click, the object in a
    box - and store it as a brush mask the editor can paint on further.

    Uses Segment Anything on this machine when its model is here; the first
    request fetches it (202 with progress - ask again), and if it cannot be
    had the older colour-and-texture methods answer instead."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Photo not found")
    path = _resolve(video.filepath)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="The photo file is not on disk")
    import cv2
    import editor_ai
    from fastapi.responses import JSONResponse
    st = editor_ai.ai_status()
    use_sam = st["runtime"] and st["status"] == "ready"
    if st["runtime"] and not use_sam and st["status"] != "error":
        st = editor_ai.ensure_models()
        if st["status"] != "ready":
            return JSONResponse(status_code=202, content={"downloading": True, **st})
    out_hw = (body.h, body.w)
    if use_sam or editor_ai.ai_status()["status"] == "ready":
        # the photo at up to 4000px, 8-bit; its embedding is cached by file
        u8 = (np.clip(_read_rgb(path, max_dim=4000), 0, 1) * 255).astype(np.uint8)
        st_ = os.stat(path)
        key = (path, st_.st_mtime_ns, u8.shape)
        try:
            if body.kind == "sky":
                m = editor_ai.sam_sky(u8, key, out_hw)
            elif body.kind == "box":
                if not body.box or len(body.box) != 4:
                    raise HTTPException(status_code=400, detail="Drag a box around the object first")
                m = editor_ai.sam_box(u8, key, body.box, out_hw)
            elif body.kind == "click":
                if not body.points:
                    raise HTTPException(status_code=400, detail="Click on the thing to select")
                labels = body.labels or [1] * len(body.points)
                m = editor_ai.sam_click(u8, key, body.points, labels, out_hw)
            else:   # the old "subject": the middle of the frame, as a click
                m = editor_ai.sam_click(u8, key, [[0.5, 0.55]], [1], out_hw)
        except HTTPException:
            raise
        except Exception as e:
            print(f"photo_edit: SAM selection failed, using the fallback: {e}", flush=True)
            m = None
    else:
        m = None
    if m is None:
        rgb = _read_rgb(path, max_dim=1600)
        if body.kind == "sky":
            m = editor_ai.select_sky(rgb)
        elif body.kind == "box" and body.box and len(body.box) == 4:
            m = editor_ai.select_box(rgb, body.box)
        elif body.kind == "click" and body.points:
            x, y = body.points[0]
            m = editor_ai.select_box(rgb, [x - 0.08, y - 0.1, x + 0.08, y + 0.1])
        else:
            m = editor_ai.select_subject(rgb)
        m = cv2.resize(m, (body.w, body.h), interpolation=cv2.INTER_LINEAR)
    name = uuid.uuid4().hex[:16]
    cv2.imwrite(str(mask_dir(video_id) / f"{name}.png"), (np.clip(m, 0, 1) * 255).astype(np.uint8))
    cover = float((m > 0.5).mean())
    return {"ref": f"{video_id}/{name}", "coverage": round(cover, 4),
            "empty": cover < 0.002}


@router.get("/{video_id}/lens")
def lens(video_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """The lens correction for this photo: the maker's profile from a DNG when
    it carries one, otherwise the distortion measured from its straight edges."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Photo not found")
    path = _resolve(video.filepath)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="The photo file is not on disk")
    import photo_geometry
    rgb = _read_rgb(path, max_dim=1600)
    asp = rgb.shape[1] / float(rgb.shape[0])
    ops = photo_geometry._dng_opcodes(path) if path.lower().endswith(".dng") else {}
    out = {"source": "none", "distortion": None, "vig_k": None,
           "camera": " ".join(x for x in (video.camera_make, video.camera_model) if x) or None}
    if ops.get("warp"):
        out["distortion"] = photo_geometry.distortion_from_warp(ops["warp"]["kr"], asp)
        out["source"] = "profile"
    if ops.get("vignette"):
        out["vig_k"] = [round(float(x), 5) for x in ops["vignette"]["k"]]
        out["source"] = "profile"
    if out["distortion"] is None:
        d = photo_geometry.measure_distortion(rgb)
        if d is not None:
            out["distortion"] = d
            if out["source"] == "none":
                out["source"] = "measured"
    return out


@router.get("/{video_id}/upright")
def upright(video_id: int, mode: str = "vertical", distortion: float = 0.0, rotate: int = 0,
            db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Straighten and keystone values that make this photo's lines true:
    level (roll only), vertical (walls upright) or full (walls and horizontals)."""
    if mode not in ("auto", "level", "vertical", "full"):
        raise HTTPException(status_code=400, detail="mode is auto, level, vertical or full")
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Photo not found")
    path = _resolve(video.filepath)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="The photo file is not on disk")
    import photo_geometry
    rgb = _read_rgb(path, max_dim=1600)
    cur = {"distortion": distortion, "rotate": rotate}
    if mode == "auto":
        # walls upright when there are walls (the property standard), else just level it
        out = photo_geometry.upright(rgb, "vertical", cur)
        out["mode"] = "vertical"
        if "error" in out:
            out = photo_geometry.upright(rgb, "level", cur)
            out["mode"] = "level"
    else:
        out = photo_geometry.upright(rgb, mode, cur)
        out["mode"] = mode
    if "error" in out:
        raise HTTPException(status_code=422, detail=out["error"])
    return out


@router.get("/{video_id}/size")
def full_size_dims(video_id: int, db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    """The original's size in pixels - so the editor knows where 100% is. It
    also decodes the file into the cache the zoomed-in tiles read from."""
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Photo not found")
    path = _resolve(video.filepath)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="The photo file is not on disk")
    h, w = _full_size(path).shape[:2]
    return {"w": int(w), "h": int(h)}


@router.get("/{video_id}/tile")
def full_res_tile(request: Request, video_id: int, token: Optional[str] = None,
                  x: float = 0.5, y: float = 0.5, zoom: float = 2.0,
                  w: int = 1400, x0: Optional[float] = None, y0: Optional[float] = None,
                  x1: Optional[float] = None, y1: Optional[float] = None,
                  db: Session = Depends(get_db)):
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
    rgb = _full_size(path)                       # full size, undeveloped, uint8
    h, w_full = rgb.shape[:2]
    zoom = max(1.0, min(16.0, float(zoom)))
    out_w = max(200, min(int(w), 3000))
    out_h = int(round(out_w * h / max(1, w_full)))

    if None not in (x0, y0, x1, y1):
        # an exact window of the source (0..1), as the zoomed editor asks for
        fx0, fx1 = sorted((max(0.0, min(1.0, x0)), max(0.0, min(1.0, x1))))
        fy0, fy1 = sorted((max(0.0, min(1.0, y0)), max(0.0, min(1.0, y1))))
        px0, py0 = int(math.floor(fx0 * w_full)), int(math.floor(fy0 * h))
        span_x = max(8, min(w_full - px0, int(math.ceil(fx1 * w_full)) - px0))
        span_y = max(8, min(h - py0, int(math.ceil(fy1 * h)) - py0))
        px0, py0 = max(0, min(px0, w_full - span_x)), max(0, min(py0, h - span_y))
        out_w = max(64, min(int(w), 4096))
    else:
        # how much of the source the viewport covers at this zoom
        span_x = min(w_full, int(round(w_full / zoom)))
        span_y = min(h, int(round(h / zoom)))
        cx = int(round(max(0.0, min(1.0, x)) * w_full))
        cy = int(round(max(0.0, min(1.0, y)) * h))
        px0 = max(0, min(w_full - span_x, cx - span_x // 2))
        py0 = max(0, min(h - span_y, cy - span_y // 2))
    crop = rgb[py0:py0 + span_y, px0:px0 + span_x].astype(np.float32) / 255.0

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
                             "Access-Control-Expose-Headers": "X-Tile-Region",
                             "X-Tile-Region": f"{px0},{py0},{span_x},{span_y},{w_full},{h}"})


class ExportBody(BaseModel):
    recipe: Optional[Recipe] = None
    width: int = Field(0, ge=0, le=MAX_EXPORT)     # 0 = native
    quality: int = Field(92, ge=60, le=100)
    fmt: str = Field("jpg", pattern="^(jpg|tif)$")
    subfolder: str = "Edited"
    add_to_library: bool = True
    out_sharpen: str = Field("none", pattern="^(none|screen|print)$")
    max_kb: int = Field(0, ge=0, le=50000)          # 0 = no limit (JPEG only)
    name: str = ""                                  # pattern; "" = "{name}_edit"


def _output_sharpen(rgb: np.ndarray, kind: str) -> np.ndarray:
    """Sharpening for where the picture is going, after it has been sized:
    a little and fine for a screen, more and wider for paper."""
    if kind not in ("screen", "print"):
        return rgb
    import cv2
    sigma, amount = (0.6, 0.35) if kind == "screen" else (1.0, 0.6)
    blur = cv2.GaussianBlur(rgb, (0, 0), sigma)
    return np.clip(rgb + amount * (rgb - blur), 0.0, 1.0).astype(np.float32)


def _export_name(pattern: str, video, n: int, db) -> str:
    stem = Path(video.filename).stem
    if not pattern:
        return f"{stem}_edit"
    prop = ""
    if "{property}" in pattern:
        try:
            import shoots
            sh = shoots.shoot_for_path(db, video.filepath)
            prop = sh.address if sh else ""
        except Exception:
            prop = ""
    date = video.shoot_date or video.created_at
    out = (pattern.replace("{name}", stem).replace("{n}", str(n).zfill(3))
           .replace("{date}", date.strftime("%Y-%m-%d") if date else "")
           .replace("{folder}", video.folder.name if getattr(video, "folder", None) else "")
           .replace("{property}", prop))
    out = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", out).strip(" .")
    return out[:180] or f"{stem}_edit"


def _write_export(db, video, path: str, recipe: "Recipe", *, width: int, quality: int, fmt: str,
                  subfolder: str, out_sharpen: str, max_kb: int, name: str, n: int,
                  add_to_library: bool, exact_name: str = "", stage: str = "edited",
                  into: str = "", replace: bool = False, out_dir: Optional[Path] = None) -> dict:
    """Develop one photo and write it: sized, sharpened for where it is going,
    under a size limit if one is set, named by the pattern."""
    import cv2
    rgb = _read_rgb(path, max_dim=width or 0, strict=True)
    out_rgb = _output_sharpen(apply_recipe(rgb, recipe), out_sharpen)
    if out_dir is not None:
        into = ""                # the caller already chose the exact folder
    else:
        try:
            import projects
            out_dir = projects.stage_dir(db, path, stage if stage in ("edited", "exports") else "edited")
        except Exception:
            out_dir = None
        if out_dir is None:
            out_dir = Path(path).parent / (subfolder or "Edited")
    if into:
        out_dir = out_dir / re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", into).strip(" .")
    out_dir.mkdir(parents=True, exist_ok=True)
    if exact_name:
        stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", exact_name).strip(" .")[:180] or Path(video.filename).stem
    else:
        stem = _export_name(name, video, n, db)
    ext = ".jpg" if fmt == "jpg" else ".tif"
    out_path = out_dir / f"{stem}{ext}"
    k = 2
    while out_path.exists() and not replace:
        out_path = out_dir / f"{stem} ({k}){ext}"
        k += 1
    bgr = cv2.cvtColor((np.clip(out_rgb, 0, 1) * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)
    used_q = quality
    if fmt == "jpg":
        ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise RuntimeError("could not encode the photo")
        if max_kb and len(buf) > max_kb * 1024:
            # the best quality that fits: a few halvings of the range
            lo, hi, best = 40, quality - 1, None
            while lo <= hi:
                mid = (lo + hi) // 2
                ok, b = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, mid])
                if ok and len(b) <= max_kb * 1024:
                    best, used_q, lo = b, mid, mid + 1
                else:
                    hi = mid - 1
            if best is None:
                # even quality 40 is too big: make it smaller until it fits
                scale = 0.9
                while best is None and scale > 0.3:
                    small = cv2.resize(bgr, (int(bgr.shape[1] * scale), int(bgr.shape[0] * scale)), interpolation=cv2.INTER_AREA)
                    ok, b = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    if ok and len(b) <= max_kb * 1024:
                        best, used_q, bgr = b, 80, small
                    scale -= 0.1
            buf = best if best is not None else buf
        out_path.write_bytes(buf.tobytes())
        carry_exif(path, str(out_path))
    else:
        if not cv2.imwrite(str(out_path), bgr):
            raise RuntimeError("could not write the file")
    if add_to_library:
        try:
            _index(str(out_path), video, db)
        except Exception:
            db.rollback()
    h, w = bgr.shape[:2]
    return {"path": str(out_path), "filename": out_path.name, "width": w, "height": h,
            "size": os.path.getsize(out_path), "quality": used_q}


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
    try:
        return _write_export(db, video, path, recipe, width=body.width, quality=body.quality, fmt=body.fmt,
                             subfolder=body.subfolder, out_sharpen=body.out_sharpen, max_kb=body.max_kb,
                             name=body.name, n=1, add_to_library=body.add_to_library)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=f"Could not export: {e}")


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
    out_sharpen: str = Field("none", pattern="^(none|screen|print)$")
    max_kb: int = Field(0, ge=0, le=50000)
    name: str = ""
    # Export by room: the exact file name for each photo ("Bedroom1"), which
    # stage folder it goes to, a folder inside that, and whether a file of the
    # same name there is replaced (a re-export of the set) rather than kept.
    names: Dict[int, str] = {}
    # also export each photo's virtual copies, named "<photo> - <copy name>"
    copies: bool = False
    stage: str = Field("edited", pattern="^(edited|exports)$")
    into: str = Field("", max_length=120)
    replace: bool = False


def _run_batch(job_id: str, body: BatchExport):
    # Everything in here is wrapped: a thread that dies quietly leaves the job
    # saying "running" forever, and the person watching the progress bar has
    # no way to tell that nothing is happening.
    db = None
    try:
        db = SessionLocal()
        import cv2
        # Export by room: one set, so one folder for all of it - the project's
        # exports step, or next to the photos' shared parent folder.
        common = None
        if body.names:
            try:
                paths = [_resolve(v.filepath) for v in db.query(Video).filter(Video.id.in_(body.video_ids)).all()]
                paths = [p for p in paths if p]
                if paths:
                    base = None
                    try:
                        import projects
                        base = projects.stage_dir(db, paths[0], body.stage)
                    except Exception:
                        base = None
                    if base is None:
                        base = Path(os.path.commonpath([str(Path(p).parent) for p in paths])) / "Exports"
                    common = base / re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", body.into or "By room").strip(" .")
            except Exception:
                common = None
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
                r = _write_export(db, video, path, recipe, width=body.width, quality=body.quality, fmt=body.fmt,
                                  subfolder=body.subfolder, out_sharpen=body.out_sharpen, max_kb=body.max_kb,
                                  name=body.name or ("{name}" + body.suffix), n=n + 1, add_to_library=True,
                                  exact_name=body.names.get(vid, ""), stage=body.stage, into=body.into,
                                  replace=body.replace, out_dir=common)
                with _export_lock:
                    EXPORT_JOBS[job_id]["results"].append(r["filename"])
                if body.copies:
                    for c in db.query(PhotoCopy).filter(PhotoCopy.video_id == vid).order_by(PhotoCopy.id).all():
                        try:
                            crec = Recipe(**json.loads(c.recipe or "{}"))
                        except Exception:
                            continue
                        stem = r["filename"].rsplit(".", 1)[0]
                        rc = _write_export(db, video, path, crec, width=body.width, quality=body.quality, fmt=body.fmt,
                                           subfolder=body.subfolder, out_sharpen=body.out_sharpen, max_kb=body.max_kb,
                                           name="", n=n + 1, add_to_library=True, exact_name=f"{stem} - {c.name}",
                                           stage=body.stage, into=body.into, replace=body.replace, out_dir=common)
                        with _export_lock:
                            EXPORT_JOBS[job_id]["results"].append(rc["filename"])
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


# --------------------------------------------------------------------------
# a listing set: named photos in one or more sizes and shapes (export by room)
# --------------------------------------------------------------------------

class SetItem(BaseModel):
    id: int
    name: str = Field(..., min_length=1, max_length=120)


class SetOutput(BaseModel):
    label: str = Field(..., min_length=1, max_length=60)     # also the folder name
    aspect: float = Field(0, ge=0, le=10)                    # width / height; 0 = the photo's own shape
    width: int = Field(0, ge=0, le=MAX_EXPORT)               # output width; 0 = as big as the photo allows
    max_kb: int = Field(0, ge=0, le=50000)
    quality: int = Field(88, ge=40, le=100)
    sharpen: str = Field("screen", pattern="^(none|screen|print)$")


class SetExport(BaseModel):
    items: List[SetItem] = Field(..., min_length=1, max_length=2000)
    outputs: List[SetOutput] = Field(..., min_length=1, max_length=8)
    # per output: where each photo sits in its frame, 0..1 along the side that is cut (0.5 = centred)
    frames: List[Dict[str, float]] = []
    into: str = Field("By room", max_length=80)
    zip: bool = True


def frame_crop(img: np.ndarray, aspect: float, pos: float) -> np.ndarray:
    """The aspect-shaped window of img, slid to pos (0 top/left .. 1 bottom/right)."""
    h, w = img.shape[:2]
    if not aspect or abs(w / h - aspect) < 1e-3:
        return img
    pos = min(1.0, max(0.0, float(pos)))
    if w / h > aspect:                      # too wide: cut the sides
        cw = max(1, int(round(h * aspect)))
        x0 = int(round((w - cw) * pos))
        return img[:, x0:x0 + cw]
    ch = max(1, int(round(w / aspect)))     # too tall: cut top and bottom
    y0 = int(round((h - ch) * pos))
    return img[y0:y0 + ch]


def _encode_capped(bgr: np.ndarray, quality: int, max_kb: int):
    import cv2
    ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, quality, cv2.IMWRITE_JPEG_OPTIMIZE, 1])
    if not ok:
        raise RuntimeError("could not encode the photo")
    if not max_kb or len(buf) <= max_kb * 1024:
        return buf, quality, bgr
    lo, hi, best, used = 40, quality - 1, None, quality
    while lo <= hi:
        mid = (lo + hi) // 2
        ok, b = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, mid, cv2.IMWRITE_JPEG_OPTIMIZE, 1])
        if ok and len(b) <= max_kb * 1024:
            best, used, lo = b, mid, mid + 1
        else:
            hi = mid - 1
    scale = 0.9
    while best is None and scale > 0.3:
        small = cv2.resize(bgr, (int(bgr.shape[1] * scale), int(bgr.shape[0] * scale)), interpolation=cv2.INTER_AREA)
        ok, b = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 75, cv2.IMWRITE_JPEG_OPTIMIZE, 1])
        if ok and len(b) <= max_kb * 1024:
            best, used, bgr = b, 75, small
        scale -= 0.1
    return (best if best is not None else buf), used, bgr


def _run_set(job_id: str, body: SetExport):
    import cv2
    import zipfile
    db = None
    written: List[tuple] = []          # (path on disk, name in the zip)
    try:
        db = SessionLocal()
        ids = [it.id for it in body.items]
        vids = {v.id: v for v in db.query(Video).filter(Video.id.in_(ids)).all()}
        paths = [p for p in (_resolve(v.filepath) for v in vids.values()) if p]
        base = None
        try:
            import projects
            base = projects.stage_dir(db, paths[0], "exports") if paths else None
        except Exception:
            base = None
        if base is None and paths:
            base = Path(os.path.commonpath([str(Path(p).parent) for p in paths])) / "Exports"
        clean = lambda t: re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", t).strip(" .") or "Export"
        root = base / clean(body.into or "By room")
        dirs = [root / clean(o.label) for o in body.outputs]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
        for n, it in enumerate(body.items):
            with _export_lock:
                if EXPORT_JOBS[job_id].get("cancelled"):
                    break
                EXPORT_JOBS[job_id]["current"] = it.name
            v = vids.get(it.id)
            try:
                if not v:
                    raise RuntimeError("no longer in the library")
                path = _resolve(v.filepath)
                if not path or not os.path.exists(path):
                    raise RuntimeError("not on disk")
                big = max((o.width for o in body.outputs), default=0)
                rgb = _read_rgb(path, max_dim=0 if not big else int(big * 2.2), strict=True)
                dev = apply_recipe(rgb, _recipe_of(db, it.id))
                del rgb
                stem = clean(it.name)[:120]
                for k, o in enumerate(body.outputs):
                    pos = (body.frames[k] if k < len(body.frames) else {}).get(str(it.id), 0.5)
                    img = frame_crop(dev, o.aspect, pos)
                    if o.width and img.shape[1] > o.width:
                        hh = max(1, int(round(o.width / (o.aspect or img.shape[1] / img.shape[0]))))
                        img = cv2.resize(img, (o.width, hh), interpolation=cv2.INTER_AREA)
                    img = _output_sharpen(img, o.sharpen)
                    bgr = cv2.cvtColor((np.clip(img, 0, 1) * 255 + 0.5).astype(np.uint8), cv2.COLOR_RGB2BGR)
                    buf, q, bgr = _encode_capped(bgr, o.quality, o.max_kb)
                    out = dirs[k] / f"{stem}.jpg"
                    out.write_bytes(buf.tobytes())
                    carry_exif(path, str(out))
                    try:
                        _index(str(out), v, db)
                    except Exception:
                        db.rollback()
                    arc = f"{clean(o.label)}/{out.name}" if len(body.outputs) > 1 else out.name
                    written.append((out, arc))
                    with _export_lock:
                        EXPORT_JOBS[job_id]["results"].append(
                            {"filename": out.name, "output": o.label, "width": bgr.shape[1], "height": bgr.shape[0],
                             "kb": max(1, len(buf) // 1024), "quality": q})
                del dev
            except Exception as e:
                with _export_lock:
                    EXPORT_JOBS[job_id]["errors"].append(f"{it.name}: {e}")
            finally:
                with _export_lock:
                    EXPORT_JOBS[job_id]["done"] = n + 1
        with _export_lock:
            EXPORT_JOBS[job_id]["folder"] = str(root)
        if body.zip and written:
            with _export_lock:
                EXPORT_JOBS[job_id]["current"] = "Packing the ZIP"
            zdir = (_media_root or Path(".")) / ".proxies_export" / job_id
            zdir.mkdir(parents=True, exist_ok=True)
            first = vids.get(body.items[0].id)
            label = re.sub(r"[^\w\- ]+", "", (first.filename.rsplit(".", 1)[0] if first else "photos"))[:40]
            try:
                import shoots
                sh = shoots.shoot_for_path(db, first.filepath) if first else None
                if sh:
                    label = re.sub(r"[^\w\- ]+", "", sh.address)[:60]
            except Exception:
                pass
            zpath = zdir / f"{label or 'Listing'} - {datetime.now():%Y-%m-%d}.zip"
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
                for pth, arc in written:
                    z.write(pth, arc)
            with _export_lock:
                EXPORT_JOBS[job_id]["zip"] = str(zpath)
                EXPORT_JOBS[job_id]["zip_name"] = zpath.name
                EXPORT_JOBS[job_id]["zip_kb"] = max(1, zpath.stat().st_size // 1024)
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


@batch_router.post("/set")
def export_set(body: SetExport, current_user: User = Depends(get_current_user)):
    """Export by room: each photo developed once, then cut to each chosen
    shape (with its framing), sized, sharpened and squeezed under the file
    size limit, named as given; one folder per size, and a ZIP of it all."""
    job_id = uuid.uuid4().hex[:12]
    with _export_lock:
        EXPORT_JOBS[job_id] = {"id": job_id, "running": True, "done": 0, "total": len(body.items),
                               "current": "Starting…", "results": [], "errors": [], "skipped": 0,
                               "started_at": time.time(), "finished_at": None, "cancelled": False,
                               "user": current_user.username}
    threading.Thread(target=_run_set, args=(job_id, body), daemon=True).start()
    return {"job_id": job_id, "total": len(body.items)}


@batch_router.get("/{job_id}/zip")
def export_set_zip(request: Request, job_id: str, token: Optional[str] = None, db: Session = Depends(get_db)):
    if token:
        user = get_user_from_token(token, db)
    else:
        auth = request.headers.get("Authorization") or ""
        if not auth.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
        user = get_user_from_token(auth[7:], db)
    with _export_lock:
        job = EXPORT_JOBS.get(job_id)
    if not job or not job.get("zip") or (job.get("user") not in (None, user.username) and user.role != "admin"):
        raise HTTPException(status_code=404, detail="That export has expired - export it again")
    if not os.path.exists(job["zip"]):
        raise HTTPException(status_code=404, detail="The ZIP is gone - export it again")
    return FileResponse(job["zip"], media_type="application/zip", filename=job["zip_name"],
                        headers={"Cache-Control": "no-store"})


class FramesBody(BaseModel):
    ratio: str = Field(..., pattern=r"^[0-9.]{1,8}$")
    frames: Dict[str, float]


@batch_router.get("/frames/{ratio}")
def get_frames(ratio: str, ids: str = "", db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Saved framing for photos at one shape (a 4:3 photo in a 16:9 box...)."""
    want = [int(x) for x in ids.split(",") if x.strip().isdigit()][:3000]
    rows = db.query(PhotoFrame).filter(PhotoFrame.ratio == ratio, PhotoFrame.video_id.in_(want)).all() if want else []
    out = {str(r.video_id): r.pos for r in rows}
    if ratio == "1.7778":
        # the classic listing export's 16:9 framing counts too
        try:
            import photo_proxy
            for r in db.query(photo_proxy.PhotoCrop).filter(photo_proxy.PhotoCrop.video_id.in_(want)).all():
                out.setdefault(str(r.video_id), r.offset_y)
        except Exception:
            pass
    return {"frames": out}


@batch_router.put("/frames")
def put_frames(body: FramesBody, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    for k, v in list(body.frames.items())[:3000]:
        if not k.isdigit():
            continue
        row = db.query(PhotoFrame).filter(PhotoFrame.video_id == int(k), PhotoFrame.ratio == body.ratio).first()
        if row:
            row.pos = min(1.0, max(0.0, float(v)))
        else:
            db.add(PhotoFrame(video_id=int(k), ratio=body.ratio, pos=min(1.0, max(0.0, float(v)))))
    db.commit()
    return {"saved": True}


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


def _ensure_folder(db: Session, folder_dir: str) -> IndexedFolder:
    """The library folder for a directory, made along with any missing
    parents - an export into Exports/By room otherwise showed up as a stray
    "By room" at the top of the tree, cut off from its property."""
    folder = db.query(IndexedFolder).filter(IndexedFolder.path == folder_dir).first()
    if folder:
        return folder
    parent = None
    up = str(Path(folder_dir).parent)
    inside = False
    try:
        inside = bool(_media_root) and Path(up) != Path(_media_root) and Path(up).is_relative_to(Path(_media_root))
    except (TypeError, ValueError):
        inside = False
    if inside and up != folder_dir:
        parent = _ensure_folder(db, up)
    else:
        parent = db.query(IndexedFolder).filter(IndexedFolder.path == up).first()
    rel = folder_dir
    try:
        if _media_root:
            rel = str(Path(folder_dir).relative_to(_media_root))
    except ValueError:
        pass
    folder = IndexedFolder(path=folder_dir, name=Path(folder_dir).name, relative_path=rel,
                           parent_id=parent.id if parent else None, added_at=datetime.utcnow())
    db.add(folder)
    db.commit()
    db.refresh(folder)
    return folder


def _index(out_path: str, source: Video, db: Session):
    """Add an exported photo to the library, beside where it was written."""
    folder = _ensure_folder(db, str(Path(out_path).parent))
    if db.query(Video.id).filter(Video.filepath == out_path).first():
        # a re-export over the same file: its thumbnail has to follow
        try:
            import indexer
            if _media_root:
                previews.make_image_jpeg(out_path, str(Path(_media_root) / "thumbnails" / indexer.thumb_name(out_path)), width=640)
        except Exception:
            pass
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
                 passes: int = 24, mid: Optional[float] = 0.42,
                 min_spread: float = 0.6) -> "Recipe":
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
    start_exp = float(out.exposure)
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

        # The body of the picture, not just its two ends. Without these a
        # dark frame kept a dark middle (one bright lamp satisfied the white
        # point) and a grey-day frame kept its grey: both ends in place, and
        # everything between them still where it started.
        med = float(np.median(lum))
        spread = float(np.percentile(lum, 98) - np.percentile(lum, 2))
        low_mid = mid is not None and med < mid - 0.05
        high_mid = mid is not None and med > mid + 0.09
        flat = spread < min_spread and out.contrast < 0.55

        done_hi = abs(p_hi - hi) <= 0.012 and clipped <= 0.0002
        done_lo = abs(p_lo - lo) <= 0.012 and crushed <= 0.0002
        if done_hi and done_lo and not low_mid and not high_mid and not flat:
            break

        # --- the middle ----------------------------------------------------
        # Exposure moves the whole frame; the top end below then holds the
        # highlights, so a lift here does not turn into clipping.
        # Exposure only while the top end has room; once the highlights are
        # at their limit the middle comes up through the shadows instead, so
        # the two goals never fight (they did: a white wall and a purple sky
        # pushed exposure to +5 and highlights to -1).
        top_room = p_hi < hi - 0.02 and clipped <= 0.0002
        if low_mid:
            if top_room and out.exposure < start_exp + 1.5:
                out.exposure = float(min(start_exp + 1.5, out.exposure + min(0.35, max(0.05, np.log2(mid / max(med, 0.01)) * 0.5))))
            elif out.shadows < 0.7:
                out.shadows = float(min(0.7, out.shadows + 0.06))
        elif high_mid and not top_room and out.exposure > start_exp - 1.0:
            out.exposure = float(max(start_exp - 1.0, out.exposure - min(0.25, np.log2(med / mid) * 0.5)))
        if flat and top_room:
            out.contrast = float(min(0.55, out.contrast + min(0.08, (min_spread - spread) * 0.5)))

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


@wm_router.get("")
def list_watermarks(current_user: User = Depends(get_current_user)):
    """The logos available, in the order the wm_index slider counts them."""
    files = watermark_files()
    return {"folder": str(watermark_dir()),
            "watermarks": [{"index": i, "name": f.name,
                            "size": f.stat().st_size} for i, f in enumerate(files)]}


@wm_router.get("/{name}/raw")
def watermark_raw(name: str, current_user: User = Depends(get_current_user)):
    """The PNG itself, so the picker can show the logo instead of a filename."""
    from fastapi.responses import FileResponse
    target = watermark_dir() / os.path.basename(name)
    if not target.is_file() or target.suffix.lower() != ".png":
        raise HTTPException(status_code=404, detail="No such watermark")
    return FileResponse(target, media_type="image/png",
                        headers={"Cache-Control": "private, max-age=300"})


@wm_router.post("")
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


@wm_router.delete("/{name}")
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
    # Guarantee the waveform rather than hoping for it - and put the middle of
    # the frame where this look wants it (moody sits lower than bright).
    resolved = fit_headroom(_read_rgb(path, max_dim=700), resolved,
                            mid=PRESET_MID.get(preset.name, 0.42))
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


def _portable_masks(masks: list) -> list:
    """Masks that can travel to another photo: shapes only - a painted mask,
    or a painted part of a shape, belongs to the photo it was painted on."""
    out = []
    for m in masks or []:
        if m.get("kind") == "brush":
            continue
        if (m.get("part2") or {}).get("kind") == "brush":
            m = {**m, "part2": None}
        out.append(m)
    return out


def _for_photo(look: dict, mine: Optional[dict]) -> dict:
    """A look copied onto another photo. Whatever was drawn on the photo it
    came from - heal spots and painted masks - belongs to that
    photo, so the target keeps its own (or gets none)."""
    out = dict(look)
    mine = mine or {}
    out["spots"] = mine.get("spots", [])
    out["removes"] = mine.get("removes", [])
    own_brushes = [m for m in mine.get("masks", []) if m.get("kind") == "brush"]
    out["masks"] = _portable_masks(look.get("masks", [])) + own_brushes
    return out


class SyncBody(BaseModel):
    video_ids: List[int]
    recipe: Recipe
    keys: List[str]            # which recipe fields to copy; everything else stays


@presets_router.post("/sync")
def sync_to_many(body: SyncBody, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    """Lightroom's Sync: copy only the chosen parts of this photo's settings
    onto others, leaving the rest of each one's own edit as it was."""
    ids = list(dict.fromkeys(body.video_ids))[:5000]
    if not ids:
        raise HTTPException(status_code=400, detail="No photos given")
    fields = set(Recipe.model_fields)
    keys = [k for k in body.keys if k in fields]
    if not keys:
        raise HTTPException(status_code=400, detail="Choose at least one thing to sync")
    src = body.recipe.model_dump()
    now = datetime.utcnow()
    have = {r.video_id: r for r in db.query(PhotoEdit).filter(PhotoEdit.video_id.in_(ids)).all()}
    for vid in ids:
        row = have.get(vid)
        try:
            mine = json.loads(row.recipe or "{}") if row else {}
        except Exception:
            mine = {}
        merged = Recipe(**{**Recipe().model_dump(), **mine}).model_dump()
        for k in keys:
            if k == "masks":
                # shapes travel; painted masks belong to the photo they were painted on
                merged["masks"] = _portable_masks(src.get("masks", [])) + \
                                  [m for m in merged.get("masks", []) if m.get("kind") == "brush"]
            else:
                merged[k] = src.get(k)
        payload = json.dumps(Recipe(**merged).model_dump())
        if row:
            row.recipe = payload
            row.updated_at = now
        else:
            db.add(PhotoEdit(video_id=vid, recipe=payload, updated_at=now))
    db.commit()
    return {"synced": len(ids), "keys": keys}


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
    look = body.recipe.model_dump()
    payload = json.dumps(_for_photo(look, None))
    now = datetime.utcnow()
    have = {r.video_id: r for r in
            db.query(PhotoEdit).filter(PhotoEdit.video_id.in_(ids)).all()}
    for vid in ids:
        row = have.get(vid)
        if row:
            try:
                mine = json.loads(row.recipe or "{}")
            except Exception:
                mine = {}
            row.recipe = json.dumps(_for_photo(look, mine))
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
    from sqlalchemy import func as _f
    copies = db.query(PhotoCopy.video_id, _f.count(PhotoCopy.id)).filter(
        PhotoCopy.video_id.in_(ids)).group_by(PhotoCopy.video_id).all()
    return {
        "edited": [r[0] for r in rows],
        "picks": {str(vid): p for vid, p in rows if p},
        "stars": {str(vid): r for vid, r in stars if r},
        "copies": {str(vid): n for vid, n in copies if n},
    }


# --------------------------------------------------------------------------
# starter presets
# --------------------------------------------------------------------------

# Looks to begin from, not looks to finish with: each one is deliberately
# restrained, because a preset that slams the photo leaves you undoing it
# rather than adjusting it. Apply, then nudge.
STARTER_PRESETS: list = []   # none: the studio makes its own looks


# Where each starter look puts the middle of the frame (median brightness),
# when it is applied "fitted" to a photo. Your own presets use 0.42.
PRESET_MID = {
    "Natural": 0.42, "Clean & bright": 0.48, "Moody": 0.34, "Warm & inviting": 0.45,
    "Architectural": 0.43, "Golden hour": 0.44, "Twilight glow": 0.40, "Grey day rescue": 0.44,
}


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


STARTER_VERSION = 3
STARTER_MARKER = Path(__file__).resolve().parent / ".starter-presets-version"


def refresh_starters():
    """The built-in looks are gone - the studio makes its own. Remove any
    still in the library (once). Presets people saved are never touched."""
    try:
        if STARTER_MARKER.exists() and int(STARTER_MARKER.read_text().strip() or 0) >= STARTER_VERSION:
            return
    except Exception:
        pass
    db = SessionLocal()
    try:
        n = db.query(PhotoPreset).filter(PhotoPreset.created_by == "starter").delete()
        db.commit()
        STARTER_MARKER.write_text(str(STARTER_VERSION), encoding="utf-8")
        if n:
            print(f"photo_edit: removed {n} built-in presets", flush=True)
    except Exception as e:
        db.rollback()
        print(f"photo_edit: could not remove the built-in presets: {e}", flush=True)
    finally:
        db.close()


def install(app, media_root, resolve_media_path):
    global _media_root, _resolve
    _media_root = Path(media_root) if media_root else None
    _resolve = resolve_media_path
    Base.metadata.create_all(bind=engine,
                             tables=[PhotoEdit.__table__, PhotoPreset.__table__, PhotoCopy.__table__, PhotoFrame.__table__])
    # A photo_edits table made before picks existed needs the column.
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(photo_edits)"))]
            if "pick" not in cols:
                conn.execute(text("ALTER TABLE photo_edits ADD COLUMN pick INTEGER"))
                conn.commit()
            if "snapshots" not in cols:
                conn.execute(text("ALTER TABLE photo_edits ADD COLUMN snapshots TEXT"))
                conn.commit()
    except Exception as e:
        print(f"photo_edit: could not add the pick column: {e}", flush=True)
    app.include_router(wm_router)   # before `router`: /{video_id} would shadow it
    app.include_router(router)
    app.include_router(presets_router)
    app.include_router(batch_router)
    app.include_router(match_router)
    seed_presets()
    refresh_starters()
