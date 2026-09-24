"""Generated looks: an image model repaints the edited photo in a look.

Claude reads photos; it cannot paint them. The painting is done by an image
editing model, reached one of four ways (Manage > AI > Image generation):

- "comfyui": ComfyUI with Qwen Image Edit 2511 on this computer's own
  NVIDIA card (tools/imagegen/install_comfyui.sh, "Install Image AI.bat").
  Free per image; nothing leaves the computer.
- "fal", "gemini", "openai": the person's own key for that service. Keys are
  kept in ai_settings.json next to the database (git-ignored, never packaged)
  and never sent to the browser.

The looks themselves (a name, an instruction and a few example photos that
show the result) are the studio's own, kept in ai_looks.json + ai_looks/.
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import random
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import List, Optional

import numpy as np
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import ai
from auth import get_current_user
from database import User

router = APIRouter(prefix="/api/ai/image", tags=["ai"])

HERE = Path(__file__).resolve().parent
LOCAL_ROOT = Path(os.environ.get("ZK_IMAGEGEN_HOME") or (Path.home() / "zerko-imagegen"))
INSTALLER = HERE / "tools" / "imagegen" / "install_comfyui.sh"
INSTALL_LOG = HERE / "tools" / "imagegen" / "install.log"
BACKENDS = ("off", "comfyui", "fal", "gemini", "openai")
DEFAULT_MODELS = {"fal": "fal-ai/qwen-image-edit-2511", "gemini": "gemini-3.1-flash-image", "openai": "gpt-image-2"}
NAMES = {"off": "Off", "comfyui": "My own PC (ComfyUI)", "fal": "fal.ai key", "gemini": "Google Gemini key", "openai": "OpenAI key"}
# Qwen Image Edit works at about one megapixel; the result is laid over the
# full-size photo as light and colour, the photo's own detail kept under it
GEN_PIXELS = 1024 * 1024


class ImageOff(ai.AiOff):
    """Image generation is not set up: the web app says so and links to Manage > AI."""


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

def settings() -> dict:
    d = ai._load().get("image")
    return d if isinstance(d, dict) else {}


def _save_settings(d: dict):
    all_ = ai._load()
    all_["image"] = d
    ai._save(all_)


def local_installed() -> bool:
    return (LOCAL_ROOT / "zerko.json").is_file() and (LOCAL_ROOT / "start.sh").is_file()


def backend() -> str:
    b = (settings().get("backend") or "").strip()
    if b in BACKENDS:
        return b
    return "comfyui" if local_installed() else "off"


def _key(b: str) -> str:
    return (settings().get(f"{b}_key") or "").strip()


def model_for(b: str) -> str:
    return (settings().get(f"{b}_model") or "").strip() or DEFAULT_MODELS.get(b, "")


def enabled() -> bool:
    b = backend()
    if b == "off":
        return False
    if b == "comfyui":
        return local_installed() or bool(settings().get("comfy_url"))
    return bool(_key(b))


def comfy_url() -> str:
    return (settings().get("comfy_url") or "http://127.0.0.1:8188").rstrip("/")


# --------------------------------------------------------------------------
# image helpers
# --------------------------------------------------------------------------

def fit_size(w: int, h: int, pixels: int = GEN_PIXELS) -> tuple:
    """A size near `pixels` with this aspect, multiples of 16, and one that
    Qwen's reference encoder (1 MP, rounded to 8) maps onto itself - so the
    picture it is shown and the one it paints are the same shape and nothing
    shifts."""
    a = w / float(h)
    best = None
    H0 = math.sqrt(pixels / a)
    for dh in range(-6, 7):
        H = max(16, int(round(H0 / 16.0)) * 16 + dh * 16)
        for W in (int(math.floor(H * a / 16.0)) * 16, int(math.ceil(H * a / 16.0)) * 16):
            if W < 16:
                continue
            s = math.sqrt(1024 * 1024 / float(W * H))
            if round(W * s / 8.0) * 8 != W or round(H * s / 8.0) * 8 != H:
                continue
            err = abs(W / float(H) - a) / a + abs(W * H - pixels) / float(pixels) * 0.2
            if best is None or err < best[0]:
                best = (err, W, H)
    if best is None:   # always found in practice; fall back to plain rounding
        return max(16, int(round(H0 * a / 16)) * 16), max(16, int(round(H0 / 16)) * 16)
    return best[1], best[2]


def to_png(rgb: np.ndarray) -> bytes:
    from PIL import Image
    b = io.BytesIO()
    Image.fromarray(rgb).save(b, "PNG")
    return b.getvalue()


def to_jpeg(rgb: np.ndarray, q: int = 92) -> bytes:
    from PIL import Image
    b = io.BytesIO()
    Image.fromarray(rgb).save(b, "JPEG", quality=q)
    return b.getvalue()


def decode(data: bytes) -> np.ndarray:
    from PIL import Image
    im = Image.open(io.BytesIO(data)).convert("RGB")
    return np.asarray(im).copy()


def resize(rgb: np.ndarray, w: int, h: int) -> np.ndarray:
    import cv2
    if rgb.shape[1] == w and rgb.shape[0] == h:
        return rgb
    interp = cv2.INTER_AREA if w < rgb.shape[1] else cv2.INTER_CUBIC
    return cv2.resize(rgb, (w, h), interpolation=interp)


def _match(gs: np.ndarray, ds: np.ndarray):
    """A first guess at how the painted picture was moved or zoomed, from
    features both pictures share (robust to a big zoom, unlike ECC alone)."""
    import cv2
    sift = cv2.SIFT_create(4000)
    k1, d1 = sift.detectAndCompute((ds * 255).astype(np.uint8), None)
    k2, d2 = sift.detectAndCompute((gs * 255).astype(np.uint8), None)
    if d1 is None or d2 is None or len(k1) < 12 or len(k2) < 12:
        return None
    pairs = cv2.BFMatcher().knnMatch(d1, d2, k=2)
    good = [m for m, n in (p for p in pairs if len(p) == 2) if m.distance < 0.75 * n.distance]
    if len(good) < 12:
        return None
    src = np.float32([k1[m.queryIdx].pt for m in good])     # in the photo
    dst = np.float32([k2[m.trainIdx].pt for m in good])     # in the painted picture
    M, inl = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=3.0)
    if M is None or inl is None or int(inl.sum()) < 10:
        return None
    return M.astype(np.float32)


def align(g: np.ndarray, d: np.ndarray):
    """Line the painted picture up with the one it was painted from. Editing
    models can shift, zoom or reframe the whole picture; left alone that shows
    as doubled edges and the wrong light in the wrong place. Returns the
    picture and where it has real content (0 along an edge it had to be
    shifted in from)."""
    import cv2
    if g.shape != d.shape:
        g = resize(g, d.shape[1], d.shape[0])
    ones = np.ones(d.shape[:2], np.float32)
    try:
        s_ = 1024.0 / max(d.shape[:2])
        sw, sh = int(d.shape[1] * s_), int(d.shape[0] * s_)
        gs = cv2.cvtColor(resize(g, sw, sh), cv2.COLOR_RGB2GRAY).astype(np.float32) / 255
        ds = cv2.cvtColor(resize(d, sw, sh), cv2.COLOR_RGB2GRAY).astype(np.float32) / 255
        # warp maps a photo pixel to where it is in the painted picture
        warp = _match(gs, ds)
        if warp is None:
            warp = np.eye(2, 3, dtype=np.float32)
        # then refine on edges (a look changes the light, not where things are)
        ge = cv2.Laplacian(cv2.GaussianBlur(gs, (0, 0), 1.2), cv2.CV_32F)
        de = cv2.Laplacian(cv2.GaussianBlur(ds, (0, 0), 1.2), cv2.CV_32F)
        try:
            _, w2 = cv2.findTransformECC(de, ge, warp.copy(), cv2.MOTION_AFFINE,
                                         (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 100, 1e-6), None, 5)
            warp = w2
        except cv2.error:
            pass
        sc = math.sqrt(abs(float(np.linalg.det(warp[:, :2]))))
        print(f"ai_image: lined up (zoom {sc:.3f}, shift {warp[0, 2]:.1f},{warp[1, 2]:.1f} px of {sw})", flush=True)
        if not 0.5 < sc < 2.0:
            return g, ones
        warp[:, 2] /= s_
        size = (d.shape[1], d.shape[0])
        out = cv2.warpAffine(g, warp, size, flags=cv2.INTER_CUBIC | cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_REPLICATE)
        valid = cv2.warpAffine(ones, warp, size, flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderValue=0)
        valid = cv2.erode(valid, np.ones((5, 5), np.uint8))
        return out, valid
    except Exception as e:
        print(f"ai_image: could not line up: {e}", flush=True)
        return g, ones


def _box(x: np.ndarray, r: int) -> np.ndarray:
    import cv2
    return cv2.boxFilter(x, -1, (2 * r + 1, 2 * r + 1), normalize=True, borderType=cv2.BORDER_REFLECT)


def guided(guide: np.ndarray, src: np.ndarray, r: int, eps: float) -> np.ndarray:
    """He's guided filter: smooths src but keeps the guide's edges, so light
    taken from the painted picture stops at a roofline instead of haloing."""
    mi, mp = _box(guide, r), _box(src, r)
    cov = _box(guide * src, r) - mi * mp
    var = _box(guide * guide, r) - mi * mi
    a = cov / (var + eps)
    b = mp - a * mi
    return _box(a, r) * guide + _box(b, r)


RATIO_LO, RATIO_SPAN = -4.0, 7.0      # the light map holds log2 ratios from -4 to +3 stops


def _colour_coeffs(I: np.ndarray, P: np.ndarray, r: int, eps: float):
    """He's guided filter with a colour guide: per spot, P ~ A.I + B. A colour guide tells a
    cyan pool from the grey table legs in it, which are just as bright."""
    H, W = I.shape[:2]
    C = P.shape[2]
    mi = np.dstack([_box(I[..., j], r) for j in range(3)])
    mp = np.dstack([_box(P[..., c], r) for c in range(C)])
    sig = np.empty((H, W, 3, 3), np.float32)
    for i in range(3):
        for j in range(i, 3):
            v = _box(I[..., i] * I[..., j], r) - mi[..., i] * mi[..., j]
            sig[..., i, j] = sig[..., j, i] = v
    sig += np.eye(3, dtype=np.float32) * eps
    cov = np.empty((H, W, 3, C), np.float32)
    for c in range(C):
        for j in range(3):
            cov[..., j, c] = _box(I[..., j] * P[..., c], r) - mi[..., j] * mp[..., c]
    A = np.linalg.solve(sig, cov).transpose(0, 1, 3, 2)            # H, W, C, 3
    B = mp - np.einsum("hwcj,hwj->hwc", A, mi)
    A = np.stack([np.stack([_box(A[..., c, j], r) for j in range(3)], -1) for c in range(C)], -2)
    B = np.dstack([_box(B[..., c], r) for c in range(C)])
    return A.astype(np.float32), B.astype(np.float32)


def upsample_light(m: np.ndarray, lo: np.ndarray, hi: np.ndarray, r: int = 3, eps: float = 2e-3,
                   sky: Optional[np.ndarray] = None) -> np.ndarray:
    """The light map (uint8, the painting's size) made at the full photo's size, following
    the full photo's own edges. Stretched as it was, a twilight's light ran 5-25 px past
    every edge at full size: a pale rim round the loungers, the pool's cyan on the coping and
    the table legs. lo is the photo at the map's size, hi the same edit larger."""
    import cv2
    h, w = m.shape[:2]
    Hh, Wh = hi.shape[:2]
    if lo.shape[:2] != (h, w):
        lo = cv2.resize(lo, (w, h), interpolation=cv2.INTER_AREA)
    R = m[..., :3].astype(np.float32) / 255 * RATIO_SPAN + RATIO_LO
    A, B = _colour_coeffs(lo.astype(np.float32) / 255, R, r, eps)
    # never outside the light found round each spot (no overshoot where the fit extrapolates)
    k = np.ones((2 * r + 1, 2 * r + 1), np.uint8)
    lo_lim, hi_lim = cv2.erode(R, k), cv2.dilate(R, k)
    out = np.empty((Hh, Wh, 3), np.uint8)
    A9 = A.reshape(h, w, 9)
    step = 256
    for y0 in range(0, Hh, step):
        y1 = min(Hh, y0 + step)
        # the rows of the small maps these full-size rows need, resized as one piece
        sy0, sy1 = max(0, int(y0 * h / Hh) - 2), min(h, int(math.ceil(y1 * h / Hh)) + 2)
        big = lambda x: cv2.resize(x[sy0:sy1], (Wh, int(round((sy1 - sy0) * Hh / h))), interpolation=cv2.INTER_LINEAR)
        off = int(round(sy0 * Hh / h))
        cut = lambda x: big(x)[y0 - off:y1 - off]
        a = cut(A9).reshape(-1, Wh, 3, 3)
        n = a.shape[0]
        I = hi[y0:y0 + n].astype(np.float32) / 255
        q = np.einsum("hwcj,hwj->hwc", a, I) + cut(B)[:n]
        q = np.clip(q, cut(lo_lim)[:n], cut(hi_lim)[:n])
        if sky is not None:
            # an evening sky that was blown white behind the trees (light_maps): each full-size
            # pixel that shows the white sky takes the painting's sky colour, each leaf and frond
            # the light of the trees
            sk = cut(sky)[:n]
            z, f, S, rf, T = sk[..., 0:1], sk[..., 1:2], sk[..., 2:5], sk[..., 5:8], sk[..., 8:9]
            spread = I.max(-1) - I.min(-1)
            lum = (I @ np.array([0.2126, 0.7152, 0.0722], np.float32))[..., None]
            bl = (np.clip((lum[..., 0] - 0.78) / 0.12, 0, 1) * np.clip((0.12 - spread) / 0.06, 0, 1))[..., None]
            # a leaf against the sky no brighter than the painting's leaves there
            rf = np.minimum(rf, np.log2((T + 0.012) / (lum + 0.012)) + (rf - rf.mean(-1, keepdims=True)))
            rs = np.log2((S + 0.012) / (I + 0.012))
            # one soft band round the sky (a hard edge cut through the fronds in blocks)
            q = q + (bl * rs + (1 - bl) * np.minimum(q, rf) - q) * z
        out[y0:y0 + n] = (np.clip((q - RATIO_LO) / RATIO_SPAN, 0, 1) * 255 + 0.5).astype(np.uint8)
    return out


def _lamps(lit: np.ndarray, guide: np.ndarray, lg: np.ndarray, sd: np.ndarray, sat: np.ndarray, evening: bool = True,
           warmth=None, green=None):
    """The lights the look switched on, checked against the photo.
    Each lit spot is grown to the whole window pane or lamp shade it sits in
    (the photo's own region round it, so a window lights up in one piece and
    not as a round blob). A spot with nothing in the photo for it to be - on
    a plain wall that runs on and on, in a tree, on brickwork - is a light the
    model made up: it is dropped, and so is the glow it threw.
    A light is only kept where the photo has something that can be one: glass
    (darker than what is round it, or already bright, and not a coloured car or
    a painted door) that is not simply more of the road or wall round it. By day
    (a look that does not bring the evening) no new lights at all.
    A spot only grows to the pane round it where the painting is not blue or cyan
    there (a dark rooftop pool it lit is not a window), and a compact dark pane
    that cannot be traced (dark glass on a timber house) is kept as it is when it
    is clearly darker than everything round it and not a hedge.
    warmth: the painting's red minus blue; green: the photo's green over red and blue.
    Returns (lights 0..1, made_up 0..1)."""
    import cv2
    H, W = guide.shape
    sm = cv2.bilateralFilter(guide.astype(np.float32), 5, 0.06, 3)
    img8 = (np.clip(sm, 0, 1) * 255 + 0.5).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats((lit > 0.35).astype(np.uint8), connectivity=8)
    out = np.zeros((H, W), np.float32)
    bad = np.zeros((H, W), np.float32)
    big = 0.03 * H * W
    for i in range(1, min(n, 1500)):
        x, y, w, h, a = (int(v) for v in stats[i])
        if a < 5 or a > big:
            continue                      # a speck, or a sunlit wall: left to the light map
        if not evening:
            # by day a brighter, warmer spot is the look's sun on a wall, a roof, a hillside -
            # the light we want. Only a tiny, very bright new spot is a lamp the model switched
            # on, and only that is left out
            comp0 = lab[y:y + h, x:x + w] == i
            if a <= 80 and float(lg[y:y + h, x:x + w][comp0].max()) > 0.85                     and float(guide[y:y + h, x:x + w][comp0].mean()) < 0.7:
                b0 = bad[y:y + h, x:x + w]
                b0[comp0] = 1.0
            continue
        if max(w, h) > 6 * min(w, h) and a > 150 or a < 0.25 * w * h and a > 150:
            continue                      # a long streak (a sky it painted over a hilltop), not a lamp
        comp = lab[y:y + h, x:x + w] == i
        sub = np.where(comp, lg[y:y + h, x:x + w], -1.0)
        sy, sx = np.unravel_index(int(np.argmax(sub)), sub.shape)
        pad = max(w, h) * 2 + 10
        X0, Y0, X1, Y1 = max(0, x - pad), max(0, y - pad), min(W, x + w + pad), min(H, y + h + pad)
        roi = img8[Y0:Y1, X0:X1].copy()
        fm = np.zeros((Y1 - Y0 + 2, X1 - X0 + 2), np.uint8)
        cv2.floodFill(roi, fm, (x + sx - X0, y + sy - Y0), 255, 5, 5, 4 | cv2.FLOODFILL_MASK_ONLY | (255 << 8))
        reg = fm[1:-1, 1:-1] > 0
        ra = int(reg.sum())
        edge = reg[0, :].any() or reg[-1, :].any() or reg[:, 0].any() or reg[:, -1].any()
        cw = comp.astype(np.float32)
        tex = float((sd[y:y + h, x:x + w] * cw).sum() / max(1.0, cw.sum()))
        was_lit = float((guide[y:y + h, x:x + w] * cw).sum() / max(1.0, cw.sum())) > 0.72   # a lamp already on in the photo
        full = np.zeros((Y1 - Y0, X1 - X0), bool)
        full[y - Y0:y - Y0 + h, x - X0:x - X0 + w] = comp
        # what the photo has there, against a ring round it
        spot = full | (reg if ra <= max(14 * a, 500) else full)
        ring = cv2.dilate(spot.astype(np.uint8), np.ones((9, 9), np.uint8)).astype(bool) & ~spot
        gs, gr = guide[Y0:Y1, X0:X1], np.maximum(ring.sum(), 1)
        mean_in = float(gs[full].mean())
        mean_ring = float((gs * ring).sum() / gr)
        colour = float(sat[Y0:Y1, X0:X1][full].mean())
        glass = (was_lit or mean_in < mean_ring - 0.05 or mean_in > mean_ring + 0.12) and colour < 0.3
        if not evening and was_lit:
            continue                      # by day a lamp already on (or a sun patch) is simply relit
        if not evening or not glass:
            b = bad[Y0:Y1, X0:X1]
            b[full] = 1.0                 # by day, or on a road, a car, a wall: made up
            continue
        cool = False
        if warmth is not None and ra and ra <= max(14 * a, 500):
            wr = reg & ~full
            cool = bool(wr.any()) and float(warmth[Y0:Y1, X0:X1][wr].mean()) < -0.02
        if ra and not edge and ra <= max(14 * a, 500) and not cool:
            ys, xs = np.nonzero(reg)
            box = (int(xs.max() - xs.min()) + 1) * (int(ys.max() - ys.min()) + 1)
            if ra / box > 0.5:            # a pane, a shade: one compact piece of the photo
                o = out[Y0:Y1, X0:X1]
                o[reg | full] = 1.0
                continue
        if was_lit or (tex < 0.03 and not edge and a < 600):
            o = out[Y0:Y1, X0:X1]
            o[full] = 1.0                 # glass or a fitting too small to trace: the spot as it is
        elif (mean_in < mean_ring - 0.08 and mean_in < 0.72 * mean_ring and a >= 0.45 * w * h and a < 3000
              and (green is None or float(green[Y0:Y1, X0:X1][full].mean()) < 0.03)):
            o = out[Y0:Y1, X0:X1]
            o[full] = 1.0                 # a compact dark pane (dark glass, a door's glass): the spot as it is
        else:
            b = bad[Y0:Y1, X0:X1]
            b[full] = 1.0
    ell = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    out = cv2.morphologyEx(out, cv2.MORPH_CLOSE, ell)
    # a pane lit only part of the way down (a ragged edge where the model's light
    # stopped) is lit to its corners: each piece filled out to its outline when that
    # outline is nearly the piece itself (not round an L-shaped corner of wall)
    cnt, _ = cv2.findContours((out > 0.5).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for c in cnt:
        hull = cv2.convexHull(c)
        ca, ha = cv2.contourArea(c), cv2.contourArea(hull)
        if ca >= 6 and ha <= 1.45 * ca:
            cv2.fillPoly(out, [hull], 1.0)
    bad = cv2.dilate(bad, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (31, 31)))
    return out, np.clip(bad - out, 0, 1)


def outside_mask(boxes, H: int, W: int) -> Optional[np.ndarray]:
    """Where the photo shows the outdoors (the sky, the view through windows and
    open sides), from Claude's boxes (x0, y0, x1, y1 in 0..1), a little larger."""
    if not boxes:
        return None
    m = np.zeros((H, W), np.float32)
    pad = 0.02
    for b in boxes:
        try:
            x0, y0, x1, y1 = (float(v) for v in b[:4])
        except Exception:
            continue
        X0, Y0 = int(max(0.0, min(x0, x1) - pad) * W), int(max(0.0, min(y0, y1) - pad) * H)
        X1, Y1 = int(min(1.0, max(x0, x1) + pad) * W), int(min(1.0, max(y0, y1) + pad) * H)
        if X1 > X0 and Y1 > Y0:
            m[Y0:Y1, X0:X1] = 1.0
    return m if m.any() else None


def _night_view(where: Optional[np.ndarray], G: np.ndarray, guide: np.ndarray, lum: np.ndarray) -> np.ndarray:
    """The outside that turns to dusk as a whole: the sky and the hazy view under
    it. Relighting a bright, hazy daytime view by three stops leaves a grey-blue
    fog with the city and the clouds ghosting through it, and a seam where the
    painted sky starts; the model's own dusk view (haze and clouds gone, its
    lights on) is used for all of it instead.
    Only inside what Claude marked as outdoors (the sky, the view through each
    window): there, what the painting made a dusk blue and the photo had bright
    is grown over soft edges, so it stops at a window frame, a roofline, railings
    and plants, and the walls stay the photo's own, relit."""
    import cv2
    H, W = guide.shape
    if where is None:
        return np.zeros((H, W), np.float32)
    g8 = (np.clip(G, 0, 1) * 255 + 0.5).astype(np.uint8)
    hsv = cv2.cvtColor(g8, cv2.COLOR_RGB2HSV).astype(np.float32)
    hue, sat = hsv[..., 0] * 2, hsv[..., 1] / 255
    bluish = (hue > 185) & (hue < 265) & (sat > 0.18)
    gg = cv2.cvtColor(g8, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255
    hg = gg - cv2.GaussianBlur(gg, (0, 0), 2.0)
    tg = np.sqrt(cv2.GaussianBlur(hg * hg, (0, 0), 3.0))
    darker = np.clip((-lum - 0.5) / 0.6, 0, 1)
    inside = where > 0.5
    # a smooth blue in the painting, or - where the photo's window is blown out (behind
    # blinds, say) - any sky blue the painting put there: the view through the window
    seed = inside & bluish & (guide > 0.5) & (darker > 0.2) & ((tg < 0.02) | (guide > 0.8))
    seed = cv2.morphologyEx(seed.astype(np.uint8), cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    if seed.sum() < 0.001 * H * W:
        return np.zeros((H, W), np.float32)
    gb = cv2.GaussianBlur(guide, (0, 0), 1.5)
    gx, gy = cv2.Sobel(gb, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(gb, cv2.CV_32F, 0, 1, ksize=3)
    grad = np.sqrt(gx * gx + gy * gy) / 8
    # Claude's box is a guess at the window: a blown-out pane or view that carries on past
    # its edge (a box stopping halfway down the window) is part of the outside too
    near = cv2.dilate(inside.astype(np.uint8), np.ones((int(0.12 * H) | 1, int(0.06 * W) | 1), np.uint8)) > 0
    # past the box only where the painting has a view there too (sky blue, or much darker as
    # a dusk view is) - not onto a blown ceiling it painted as ceiling (a pale block)
    viewish = bluish | (darker > 0.2)
    allowed = (((inside | (near & (guide > 0.8))) & viewish) & (guide > 0.35) & (grad < 0.2)).astype(np.uint8)
    k = np.ones((3, 3), np.uint8)
    cur = seed.copy()
    for _ in range(max(8, int(0.15 * max(H, W)))):
        nxt = cv2.dilate(cur, k) & allowed | cur
        if np.array_equal(nxt, cur):
            break
        cur = nxt
    n, lab, stats, _ = cv2.connectedComponentsWithStats(cur, connectivity=8)
    keep = np.zeros_like(cur)
    for i in range(1, n):
        if stats[i][4] >= 0.002 * H * W:
            keep[lab == i] = 1
    # clouds, boats and town lights inside the view the growth went round are part of it
    v = cv2.morphologyEx(keep, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11)))
    cnt, _ = cv2.findContours(v, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    filled = np.zeros((H, W), np.uint8)
    cv2.drawContours(filled, cnt, -1, 1, thickness=-1)
    grown = inside | (allowed > 0)
    v = np.maximum(v, filled * (guide > 0.4) * grown).astype(np.float32)
    # where a box cuts across a plain wall, fade out instead of a straight seam (the fade
    # follows where the view really went, not the box)
    soft = cv2.GaussianBlur(np.maximum(where.astype(np.float32), grown * (cur > 0)), (0, 0), 0.012 * max(H, W))
    return _snap(v, guide) * np.clip(soft * 1.6, 0, 1)


def _snap(mask: np.ndarray, guide: np.ndarray) -> np.ndarray:
    """A mask's edge moved onto the photo's own edges and kept crisp (a window
    frame, a roofline), with no halo and no soft blobs on plain walls."""
    import cv2
    s_ = np.clip(guided(guide, mask.astype(np.float32), 4, 2e-4), 0, 1)
    s_ = np.clip((s_ - 0.25) / 0.5, 0, 1)
    return cv2.GaussianBlur(s_, (0, 0), 0.7).astype(np.float32)


def _detail_ratio(G: np.ndarray, D: np.ndarray, sigma: float = 2.5) -> np.ndarray:
    """The painting's light and colour as a log2 ratio over the photo, fine enough to keep a
    porch light's pool and a lit window's edge, too coarse to carry the model's drawing of a
    texture. Worked out in linear light and returned for the display values apply_gen
    multiplies (s * 2^m), so the result is the photo's own detail under the painting's light."""
    import cv2
    lin = lambda x: np.power(np.clip(x, 0, 1), 2.2)
    num = cv2.GaussianBlur(lin(G), (0, 0), sigma)
    den = cv2.GaussianBlur(lin(D), (0, 0), sigma)
    return (np.log2((num + 0.004) / (den + 0.004)) / 2.2).astype(np.float32)


def _fill_edges(x: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Where the model's picture did not reach (it zoomed or turned a little), the light
    of the nearest place it did reach, pixel by pixel. (Copying whole rows across the
    frame made a hard-edged block of light where the photo had dark lens corners.)"""
    if valid.min() >= 0.5:
        return x
    return extend_edges(x, valid)


MATCH_MIN = 0.35      # below this the painting is a different picture (moved mountain, houses)


def local_match(g: np.ndarray, d: np.ndarray) -> np.ndarray:
    """Per spot, 0..1: the painting still has the photo's shapes there (its edges run the
    same way), even if it redrew the texture. Where it does, the painting's light can be
    trusted up close - its sun and shade on that wall - not only as a broad wash."""
    import cv2
    lg = np.log(cv2.cvtColor(g, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255 + 0.02)
    ld = np.log(cv2.cvtColor(d, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255 + 0.02)
    s_ = 0.004 * max(g.shape[:2])

    def grads(x):
        x = cv2.GaussianBlur(x, (0, 0), s_)
        return cv2.Sobel(x, cv2.CV_32F, 1, 0), cv2.Sobel(x, cv2.CV_32F, 0, 1)
    ax, ay = grads(lg)
    bx, by = grads(ld)
    w = np.sqrt(bx * bx + by * by)
    cos = (ax * bx + ay * by) / (w * np.sqrt(ax * ax + ay * ay) + 1e-6)
    S = 0.02 * max(g.shape[:2])
    agree = cv2.GaussianBlur(cos * w, (0, 0), S) / (cv2.GaussianBlur(w, (0, 0), S) + 1e-4)
    edges = cv2.GaussianBlur(w, (0, 0), S)
    # a plain area (no edges to compare) counts as matching when the rest round it does
    return np.clip((agree - 0.4) / 0.35, 0, 1).astype(np.float32) * np.clip(edges / (np.median(edges) + 1e-6), 0.3, 1)


def match_score(g: np.ndarray, d: np.ndarray) -> float:
    """How well the painting keeps the photo's layout, 0..1: whether its edges run the same
    way as the photo's, weighted by the photo's own edges. Good paintings score 0.6 to 1
    (a dark twilight scores lower, it has fewer edges); one where the model redrew the
    scene scores near 0."""
    import cv2
    lg = np.log(cv2.cvtColor(g, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255 + 0.02)
    ld = np.log(cv2.cvtColor(d, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255 + 0.02)
    s_ = 0.004 * max(g.shape[:2])

    def grads(x):
        x = cv2.GaussianBlur(x, (0, 0), s_)
        return cv2.Sobel(x, cv2.CV_32F, 1, 0), cv2.Sobel(x, cv2.CV_32F, 0, 1)
    ax, ay = grads(lg)
    bx, by = grads(ld)
    w, w2 = np.sqrt(bx * bx + by * by), np.sqrt(ax * ax + ay * ay)
    cos = (ax * bx + ay * by) / (w * w2 + 1e-6)
    return float((cos * w).sum() / (w.sum() + 1e-6))


def missed_look(g: np.ndarray, d: np.ndarray, evening: Optional[bool]) -> bool:
    """An evening look that came back as daylight (the model ignored the words)."""
    if not evening:
        return False
    g, d = g.astype(np.float32), d.astype(np.float32)
    bluer = float((g[..., 2] - g[..., 0]).mean() - (d[..., 2] - d[..., 0]).mean())   # RGB
    return float(g.mean()) > 0.72 * float(d.mean()) and bluer < 6


def painted_area(g: np.ndarray, d: np.ndarray, k: Optional[np.ndarray] = None) -> np.ndarray:
    """Where the model really painted (1) - kept in the blue channel of the k map since
    v6. For an older result: not black where the photo is not black either, and not in
    a filled-in stretch along the edge (every pixel the same as its neighbour)."""
    import cv2
    if k is not None and k.ndim == 3 and k[..., 0].max() > 0:
        return (k[..., 0] > 127).astype(np.float32)            # BGR as read: blue first
    valid = ~((g.max(axis=2) == 0) & (d.max(axis=2) > 24))
    eqx = np.all(g[:, 1:] == g[:, :-1], -1)
    eqy = np.all(g[1:] == g[:-1], -1)
    flat = np.zeros(g.shape[:2], bool)
    flat[1:-1, 1:-1] = (eqx[1:-1, 1:] & eqx[1:-1, :-1]) | (eqy[1:, 1:-1] & eqy[:-1, 1:-1])
    n, lab, st, _ = cv2.connectedComponentsWithStats(flat.astype(np.uint8), connectivity=4)
    H, W = flat.shape
    for i in range(1, n):
        x, y, w, h, a = (int(v) for v in st[i])
        if a > 150 and (x <= 1 or y <= 1 or x + w >= W - 1 or y + h >= H - 1):
            valid[lab == i] = False
    valid = cv2.erode(valid.astype(np.uint8), np.ones((3, 3), np.uint8))
    return valid.astype(np.float32)


def extend_edges(img: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """The painted picture carried out to the frame where the model did not reach (it
    zoomed or turned a little): each empty pixel takes the nearest painted one, so a sky
    runs to the edge instead of stopping in a band of the relit photo."""
    import cv2
    inv = (valid < 0.5).astype(np.uint8)
    if not inv.any() or inv.all():
        return img
    _, lab = cv2.distanceTransformWithLabels(inv, cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
    ys, xs = np.nonzero(inv == 0)                 # the labels count the painted pixels in this order
    m = inv > 0
    idx = np.clip(lab[m] - 1, 0, len(ys) - 1)
    out = img.copy()
    out[m] = img[ys[idx], xs[idx]]
    return out


def is_outdoors(scene: Optional[str], boxes) -> bool:
    """An outdoor photo (a house from the street, a drone shot): Claude says so; for a
    result from before it was asked, a sky box across the whole top says so."""
    if scene in ("outdoors", "indoors"):
        return scene == "outdoors"
    for b in boxes or []:
        try:
            x0, y0, x1, y1 = (float(v) for v in b[:4])
        except Exception:
            continue
        if min(y0, y1) < 0.04 and abs(x1 - x0) > 0.85:
            return True
    return False


def _gone(guide: np.ndarray, lg: np.ndarray, G: Optional[np.ndarray] = None, D: Optional[np.ndarray] = None) -> np.ndarray:
    """Where the photo has a clear thing - a car, a person, a sign - that the painted
    picture no longer has (it removed it, or drew a light over it), 0..1. Compared on
    the log of the brightness, so a tree the look made darker still counts as there,
    and a hazy view the model made clear is not taken for something gone."""
    import cv2
    ld, lgg = np.log(guide + 0.02), np.log(lg + 0.02)
    hd, hg = ld - cv2.GaussianBlur(ld, (0, 0), 2.0), lgg - cv2.GaussianBlur(lgg, (0, 0), 2.0)
    ed = np.sqrt(cv2.GaussianBlur(hd * hd, (0, 0), 4.0))
    eg = np.sqrt(cv2.GaussianBlur(hg * hg, (0, 0), 4.0))
    cov = cv2.GaussianBlur(hd * hg, (0, 0), 4.0) / (ed * eg + 1e-6)
    # something gone leaves a smooth patch where the photo had edges; a tree or a roof the
    # model redrew is still as busy, only differently (that is left painted)
    gone = (np.clip((ed - 0.06) / 0.08, 0, 1) * np.clip((0.5 - cov) / 0.3, 0, 1)
            * np.clip((0.55 - eg / (ed + 1e-6)) / 0.25, 0, 1))
    # a tree or a wall the evening turned into a dark silhouette lost its edges too, but it
    # is still there, only dark: its own (dark) light is right
    gone = gone * np.clip((lg - 0.06) / 0.08, 0, 1)
    # a pane the look lit is a lit window, not a thing gone: warm and bright in the painting,
    # window-sized, over grey glass in the photo (by day it only reflected the sky, so it
    # is not brighter than before); a coloured car under a glow the model drew stays the photo's
    if G is not None and D is not None:
        warm_up = ((G[..., 0] - G[..., 2]) > 0.08) & (lg > 0.3)
        sat_d = D.max(-1) - D.min(-1)
    else:
        warm_up = ((lg - guide) > 0.12) & (lg > 0.3)
        sat_d = np.zeros_like(guide)
    if G is not None and D is not None:
        # a strongly coloured thing that changed colour altogether (a blue car under a
        # glow the model drew over it); dusk only darkens a colour or cools it a little
        hs = lambda x: cv2.cvtColor((np.clip(x, 0, 1) * 255).astype(np.uint8), cv2.COLOR_RGB2HSV).astype(np.float32)
        a, b = hs(D), hs(G)
        dh = np.abs(a[..., 0] - b[..., 0]) * 2
        dh = np.minimum(dh, 360 - dh)
        flip = (a[..., 1] > 90) & (a[..., 2] > 50) & (b[..., 1] > 60) & (dh > 70)
        flip = cv2.morphologyEx(flip.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        flip = cv2.morphologyEx(flip, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)))
        flip = flip.astype(np.float32)
    else:
        flip = np.zeros_like(guide)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(warm_up.astype(np.uint8), connectivity=8)
    small = np.zeros(n, bool)
    evening = float(lg.mean()) < 0.5 * float(guide.mean())      # a twilight is about a third as bright
    if n > 1:
        sums = np.bincount(lab.ravel(), weights=sat_d.ravel(), minlength=n)
        grey = sums / np.maximum(1, stats[:, 4]) < 0.22
        small[1:] = (stats[1:, 4] < 0.006 * guide.size) & grey[1:]
    panes = cv2.dilate(small[lab].astype(np.float32), np.ones((5, 5), np.uint8))
    # only a look that brings the evening turns lights on; by day a lit pane is made up
    # and the photo's own glass goes back
    gone = gone * (1 - panes) if evening else np.maximum(gone, panes * (lg > guide + 0.05))
    gone = np.maximum(gone, flip)
    ell = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    gone = cv2.morphologyEx(gone, cv2.MORPH_OPEN, ell)          # whole things, not specks of texture
    # a thing: a car, a sign, a person - not a hillside of bushes the model drew smoother
    n2, lab2, st2, _ = cv2.connectedComponentsWithStats((gone > 0.3).astype(np.uint8), connectivity=8)
    if n2 > 1:
        small2 = np.zeros(n2, bool)
        small2[1:] = st2[1:, 4] < 0.01 * guide.size
        gone = gone * small2[lab2]
    gone = cv2.dilate(gone, ell)
    return _snap(gone, guide)


def _fill_holes(m: np.ndarray) -> np.ndarray:
    import cv2
    cnt, _ = cv2.findContours(m.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = np.zeros(m.shape, np.uint8)
    cv2.drawContours(out, cnt, -1, 1, thickness=-1)
    return out


def _sky(G: np.ndarray, guide: np.ndarray, sd: np.ndarray, lg: np.ndarray, outside, D: Optional[np.ndarray] = None) -> np.ndarray:
    """The photo's own sky, 0..1: the smooth, open area that runs down from the top
    edge (or from a sky Claude marked) and stops at the first real edge - a ridge of
    trees, a roofline, a wall. Worked out from the photo, not from Claude's box, so
    it follows the ridge instead of cutting straight across it."""
    import cv2
    H, W = guide.shape
    gg = lg
    hg = gg - cv2.GaussianBlur(gg, (0, 0), 2.0)
    sg = np.sqrt(cv2.GaussianBlur(hg * hg, (0, 0), 3.0))
    gb = cv2.GaussianBlur(guide, (0, 0), 1.2)
    gx, gy = cv2.Sobel(gb, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(gb, cv2.CV_32F, 0, 1, ksize=3)
    grad = np.sqrt(gx * gx + gy * gy) / 8
    # open sky in the photo: bright or plain, without the fine texture of leaves and roofs;
    # the painting there is plain too (its sky), so a tree it kept is not taken
    cand = (((sd < 0.018) & (guide > 0.35)) | ((guide > 0.82) & (sd < 0.035))) & (sg < 0.04) & (grad < 0.06)
    # clouds and a hazy, blown sky: bright, soft (no sharp edges of a roofline or a railing)
    # and pale or blue - a white wall is bright too, but has hard edges round it
    hsv_d = cv2.cvtColor((np.clip(D if D is not None else np.dstack([guide] * 3), 0, 1) * 255).astype(np.uint8),
                         cv2.COLOR_RGB2HSV).astype(np.float32)
    skyish = ((hsv_d[..., 1] < 70) | ((hsv_d[..., 0] * 2 > 180) & (hsv_d[..., 0] * 2 < 250)))
    soft = cv2.GaussianBlur(grad, (0, 0), 2) < 0.05
    cand = cand | ((guide > 0.55) & skyish & soft & (sd < 0.03))
    # a bright, busy cloudy sky where the painting has plain sky: sky too, as long as the
    # photo has no hard edge there (a roof, a mast)
    cand = cand | ((guide > 0.5) & skyish & (sg < 0.015) & (cv2.GaussianBlur(grad, (0, 0), 2) < 0.1))
    cand = cv2.morphologyEx(cand.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(cand, connectivity=4)
    seeds = np.zeros(n, bool)
    top = np.unique(lab[0, :])
    seeds[top] = True
    box = outside_mask(outside, H, W)
    if box is not None:
        for i in np.unique(lab[box > 0.5]):
            seeds[i] |= stats[i][1] < 0.05 * H          # a sky Claude marked that reaches the top
    seeds[0] = False
    seeds &= stats[:, 4] >= 0.01 * H * W
    sky = seeds[lab].astype(np.uint8)
    if not sky.any():
        return np.zeros((H, W), np.float32)
    # clouds, a bird, a power line inside it are part of it
    sky = cv2.morphologyEx(sky, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)))
    sky = _fill_holes(sky) & (guide > 0.2).astype(np.uint8)
    if D is not None:
        # right up to the treetops and the roofline: the strip of sky next to them is busier
        # (it was left out above) but has the sky's own colour; left as the photo, relit, it
        # showed as a pale rim round every tree
        b = sky.astype(np.float32)
        den = cv2.GaussianBlur(b, (0, 0), 4)
        mean = cv2.GaussianBlur(D * b[..., None], (0, 0), 4) / np.maximum(den, 1e-4)[..., None]
        like = (np.abs(D - mean).max(-1) < 0.07) & (den > 0.05)
        cur = sky.copy()
        for _ in range(10):
            nxt = cv2.dilate(cur, np.ones((3, 3), np.uint8)) & like.astype(np.uint8) | cur
            if np.array_equal(nxt, cur):
                break
            cur = nxt
        sky = cur
    # a pixel or two over the ridge: the photo's bright sky between the leaves, relit, showed
    # as a pale line along it
    sky = _snap(sky.astype(np.float32), guide)
    return cv2.dilate(sky, np.ones((3, 3), np.uint8))


def _haze(G: np.ndarray, D: np.ndarray, guide: np.ndarray, lg: np.ndarray, sky: np.ndarray) -> np.ndarray:
    """The far view under the sky that the photo only shows through haze (pale, flat,
    washed out) and the painting shows clearly, 0..1. Relit, haze stays a grey fog
    over the town; there the painting (with the photo's detail laid back) is used,
    fading in with distance the way haze does."""
    import cv2
    H, W = guide.shape
    if sky.max() < 0.5:
        return np.zeros((H, W), np.float32)
    ld, lgl = np.log(guide + 0.03), np.log(lg + 0.03)
    s = 0.006 * max(H, W)
    cd = np.sqrt(cv2.GaussianBlur((ld - cv2.GaussianBlur(ld, (0, 0), s)) ** 2, (0, 0), s))
    cg = np.sqrt(cv2.GaussianBlur((lgl - cv2.GaussianBlur(lgl, (0, 0), s)) ** 2, (0, 0), s))
    # haze lifts the darkest of the three colours everywhere in a patch (the dark-channel
    # rule): a near roof or tree always has something dark, a view through haze does not
    dark = cv2.erode(D.min(-1), np.ones((7, 7), np.uint8))
    dark = cv2.GaussianBlur(dark, (0, 0), 3)
    washed = np.clip((dark - 0.4) / 0.2, 0, 1)
    clearer = np.clip((cg / (cd + 0.01) - 1.1) / 0.6, 0, 1)
    hz = cv2.GaussianBlur(washed * clearer, (0, 0), 0.01 * max(H, W))
    # only the view that carries on below the sky, not a pale wall in the front
    near = cv2.dilate((sky > 0.5).astype(np.uint8), np.ones((int(0.25 * H) | 1, 3), np.uint8)).astype(np.float32)
    near = cv2.GaussianBlur(near, (0, 0), 0.03 * max(H, W))
    hz = np.clip((hz - 0.15) / 0.35, 0, 1) * near * (1 - sky)
    return hz.astype(np.float32)


NEUTRAL_WORDS = re.compile(r"neutral|no colou?r cast|white walls|white balance|cast-free", re.I)


def wants_neutral(look_words: str) -> bool:
    """A look that asks for white walls and ceilings (Clean and bright, Blue sky day): its
    whites are made white, not left the painting's cream."""
    w = look_words or ""
    return bool(NEUTRAL_WORDS.search(w)) and not re.search(r"golden|sunset|twilight|dusk|night", w, re.I)


def wants_evening(look_words: str) -> Optional[bool]:
    """Whether a look brings the evening (lights switched on, a dusk view), from its words;
    None when they do not say (then the painting's brightness decides)."""
    w = look_words or ""
    if re.search(r"twilight|dusk|blue hour|night|evening", w, re.I):
        return True
    if re.search(r"golden hour|sunset glow|sunny|daylight|day\b|bright|morning|midday", w, re.I):
        return False
    return None


def _neutral(ratio: np.ndarray, D: np.ndarray, skip: np.ndarray, valid: np.ndarray,
             G: Optional[np.ndarray] = None) -> np.ndarray:
    """White balance on what should be white. The relit room's pale, nearly grey
    surfaces (walls, ceiling, linen) are measured, and the whole light is turned so
    they come out neutral - keeping a fifth of the warmth, and every warm glow near a
    window stays warmer than the rest, since the whole room turns together. A dim
    white is lifted a little too (at most half a stop)."""
    R = np.clip(D * np.exp2(ratio), 1e-4, 1.0)
    y = R @ np.array([0.2126, 0.7152, 0.0722], np.float32)
    ch = (R.max(-1) - R.min(-1)) / (R.max(-1) + 1e-4)
    pale = (y > 0.4) & (y < 0.97) & (ch < 0.28) & (skip < 0.2) & (valid > 0)
    if pale.sum() < 0.02 * pale.size:
        return ratio
    Lw = np.array([0.2126, 0.7152, 0.0722], np.float32)
    cast = np.median(np.log2(R[pale]) - np.log2(y[pale])[:, None], axis=0)
    cast = cast - cast @ Lw
    if G is not None:
        # what the relit whites lost against the painting's is put back, and the painting's
        # own cast (a cream it left) is taken a third further towards white; a room of warm
        # stone or wood is warm in the painting too and is left warm (corrected against a
        # grey ideal, a purple wall went bluer still)
        Gc = np.clip(G, 1e-4, 1.0)
        yg = Gc @ Lw
        cg = np.median(np.log2(Gc[pale]) - np.log2(yg[pale])[:, None], axis=0)
        cg = cg - cg @ Lw
        fix = np.clip(-(cast - cg) - 0.35 * cg, -0.3, 0.3).astype(np.float32)
    else:
        fix = np.clip(-0.5 * cast, -0.2, 0.2).astype(np.float32)
    lift = float(np.clip(np.log2(0.78 / max(float(np.median(y[pale])), 1e-3)), 0.0, 0.5))
    return ratio + fix + np.float32(lift)


def light_maps(g: np.ndarray, d: np.ndarray, keep: np.ndarray, valid: np.ndarray, real: bool = True, outside=None,
               outdoors: bool = False, neutral: bool = False, evening: Optional[bool] = None, loose: bool = False,
               blend: str = "detail", extra: Optional[dict] = None):
    """What a look does to the photo, split in two:
    - the light (m): how much brighter or darker, and what colour, each spot
      became - taken from the painted picture, smoothed along the photo's own
      edges. Applied to the photo itself, so every house, car and tree stays
      the real one, relit.
    - where the model may show its own picture (repaint, 0..1): where the photo
      had no detail to lose (blown-out sky and windows), the outside view when
      it turns to dusk as a whole, and the lamps and windows it switched on -
      only the ones that are in the photo. Everywhere when the look is set to
      repaint freely.
    Returns m (uint8 RGB, log2 ratio) and repaint (float 0..1)."""
    import cv2
    G = g.astype(np.float32) / 255
    D = d.astype(np.float32) / 255
    lr = np.log2((G + 0.012) / (D + 0.012))
    # the colour of the change, measured so a channel the photo has almost none of (the blue
    # in dark wood) does not read as a huge change - that turned wood lilac
    Lw = np.array([0.2126, 0.7152, 0.0722], np.float32)
    lc = np.log2((G + 0.08) / (D + 0.08))
    # (a look that asks for clean whites may take a coloured cast - a purple LED wash - out)
    cmax = 1.4 if neutral else 0.6
    lr = (lr @ Lw)[..., None] + np.clip(lc - (lc @ Lw)[..., None], -cmax, cmax)
    guide = cv2.cvtColor(d, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255
    lg = cv2.cvtColor(g, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255
    hd = guide - cv2.GaussianBlur(guide, (0, 0), 2.0)
    sd = np.sqrt(cv2.GaussianBlur(hd * hd, (0, 0), 3.0))
    lum = lr @ np.array([0.30, 0.59, 0.11], np.float32)
    ell = lambda n: cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (n, n))
    # (the +0.25 keeps dark warm glass - a window on a brown timber house - from reading as a
    # coloured car or a painted door)
    sat = (D.max(-1) - D.min(-1)) / (D.max(-1) + 0.25)
    # a look that brings the evening (a twilight is about a third as bright): only then are
    # lights switched on
    if evening is None:
        evening = float(lg[valid > 0].mean() if valid.any() else lg.mean()) < 0.55 * float(guide.mean())
    lights = made_up = None
    if real:
        # Lights the look switched on (lit windows, wall and garden lights):
        # spots that became much brighter and warm - lamp light is warm; a
        # painted sky is not.
        warm = np.clip((G[..., 0] - G[..., 2]) / 0.08, 0, 1)
        lit = np.clip((lg - guide - 0.10) / 0.15, 0, 1) * np.clip((lg - 0.25) / 0.2, 0, 1) * warm
        lights, made_up = _lamps(lit, guide, lg, sd, sat, evening, G[..., 0] - G[..., 2],
                                 D[..., 1] - np.maximum(D[..., 0], D[..., 2]))
        # how much brighter than its surroundings each spot is, in the photo and in the painting
        ex_d = guide - cv2.GaussianBlur(guide, (0, 0), 25)
        ex_g = lg - cv2.GaussianBlur(lg, (0, 0), 25)
        # A pale shape the model drew that is not in the photo (a flake, a
        # smudge - not a warm lamp): no light is taken from it.
        spike = np.clip((ex_g - np.maximum(ex_d, 0) - 0.10) / 0.08, 0, 1) * (1 - warm) * (1 - lights)
        spike = cv2.dilate(cv2.morphologyEx(spike, cv2.MORPH_OPEN, ell(5)), ell(15))
        strips = np.zeros_like(guide)
        if evening and not outdoors:
            # thin warm lines the model drew that stand out from everything round them (LED
            # strips round a window frame, along a ceiling) and are not a light
            # in the photo: made up, their glow too
            strips = warm * np.clip((ex_g - np.maximum(ex_d, 0) - 0.08) / 0.1, 0, 1) * (1 - lights)
            strips = cv2.morphologyEx(strips, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
            made_up = np.maximum(made_up, cv2.dilate(strips, ell(5)))
        if outdoors:
            # outside, a lit spot counts as made up only on plants and trees: glass that
            # reflected the day is busy too, and a lit window there is a real one
            hsv = cv2.cvtColor(d, cv2.COLOR_RGB2HSV).astype(np.float32)
            leafy = cv2.dilate((((hsv[..., 0] * 2) > 60) & ((hsv[..., 0] * 2) < 170) & (hsv[..., 1] > 50)).astype(np.float32),
                               ell(9))
            made_up = made_up * leafy
            # and whatever the model took away or drew over (a car, a sign) is the photo's own,
            # relit with the light round it - not with the glow the model put in its place
            made_up = np.maximum(made_up, _gone(guide, lg, G, D))
        made_up = np.clip(np.maximum(made_up, spike), 0, 1)
    # Light is smooth: it changes at edges (a roofline, a window frame) but not
    # inside a textured wall. A wide edge-aware filter takes the light and
    # leaves the model's own version of every texture behind - with a narrow
    # one, a plaster wall it redrew slightly differently came out in blotches.
    fine = np.dstack([guided(guide, lr[..., c], 14, 6e-3) for c in range(3)])
    # where the painted picture no longer matches the photo (a car it moved,
    # a hillside it painted over with sky, a lamp it made up) its colours say
    # nothing about the light there: take the light from the matching parts
    # around it instead
    wgt = (np.clip((keep - 0.3) / 0.4, 0, 1) * valid).astype(np.float32)
    if real and not loose:
        # where the painting still has the photo's shapes (it only redrew the texture), its
        # light is trusted up close too: the golden sun on this wall, the shade under that
        # tree - taken only from the matching fine detail it came out flat
        wgt = np.maximum(wgt, local_match(g, d) * valid)
    # a hillside or a roof the look turned into a dark silhouette has lost its fine structure,
    # but its light is still right: dark (taken from far away it came out a pale patch with
    # a round edge over the hill)
    wgt = np.maximum(wgt, np.clip((0.12 - lg) / 0.06, 0, 1) * np.clip((guide - lg - 0.1) / 0.1, 0, 1) * valid)
    if made_up is not None:
        wgt = wgt * (1 - made_up)
    num = cv2.GaussianBlur(lr * wgt[..., None], (0, 0), 40)
    den = cv2.GaussianBlur(wgt, (0, 0), 40)[..., None]
    sel = wgt > 0.5
    overall = np.median(lr[sel], axis=0) if sel.sum() > 100 else np.zeros(3, np.float32)
    # far from anything that matched, the light of a wider area, then the picture's overall
    # light - blended, never switched (a hard switch drew a block of light over a hillside
    # the model had redrawn)
    s2 = 0.12 * max(guide.shape)
    num2 = cv2.GaussianBlur(lr * wgt[..., None], (0, 0), s2)
    den2 = cv2.GaussianBlur(wgt, (0, 0), s2)[..., None]
    e = np.float32(0.01)
    coarse = ((num + 0.3 * num2 + e * overall) / (den + 0.3 * den2 + e)).astype(np.float32)
    kb = cv2.GaussianBlur(wgt, (0, 0), 12)[..., None]
    if loose:
        # the model drew a different picture (its houses, its mountain are elsewhere): only
        # its overall light is taken, in broad strokes, and none of its picture is shown
        allw = cv2.GaussianBlur(valid, (0, 0), s2)[..., None]
        broad2 = cv2.GaussianBlur(lr * valid[..., None], (0, 0), s2) / np.maximum(allw, 1e-4)
        broad2 = np.dstack([guided(guide, broad2[..., c], 30, 2e-2) for c in range(3)])
        m = (np.clip((_fill_edges(broad2, valid) - RATIO_LO) / RATIO_SPAN, 0, 1) * 255 + 0.5).astype(np.uint8)
        return m, np.zeros_like(guide)
    # along an edge the painted picture did not cover, the light around it carries on
    ratio = coarse + (fine - coarse) * kb
    if made_up is not None:
        mu = cv2.GaussianBlur(made_up, (0, 0), 6)[..., None]
        ratio = ratio + (coarse - ratio) * mu      # no glow from a lamp that is not there
    # Sunbeams: the stripes of sun the look threw across a floor, a bed or a wall. They are
    # not in the photo, so the edge-aware light (which only changes at the photo's own edges)
    # smoothed them away. What the light missed that is brighter, several pixels wide (finer
    # than that is the model redrawing a texture) and on a plain, lit surface is added back
    # as light - on the photo's own floorboards, never as the model's drawing of them.
    if real and not evening:
        Lw3 = np.array([0.2126, 0.7152, 0.0722], np.float32)
        miss = (lr @ Lw3) - (ratio @ Lw3)
        miss = cv2.GaussianBlur(miss, (0, 0), 2.5)
        hd2 = cv2.GaussianBlur(guide, (0, 0), 1.0)
        ex, ey = cv2.Sobel(hd2, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(hd2, cv2.CV_32F, 0, 1, ksize=3)
        calm = np.clip(1 - np.sqrt(ex * ex + ey * ey) / 8 / 0.08, 0, 1)
        trust = calm * np.clip((guide - 0.12) / 0.1, 0, 1) * valid
        if made_up is not None:
            trust = trust * (1 - made_up)
        beam = np.clip((miss - 0.12) * 1.3, 0, 1.5) * cv2.GaussianBlur(trust, (0, 0), 1.5)
        beam = cv2.morphologyEx(beam, cv2.MORPH_OPEN, ell(3))
        # the beam's colour is the painting's, as it is round it
        ratio = ratio + beam[..., None]
    # The colour of light changes slowly across a room; a patch where the model drew a
    # different colour (a sun patch on warm wood it painted white) turned the wood round it
    # lilac. The colour of the light is held near the colour round it; its brightness is not.
    y_r = ratio @ np.array([0.2126, 0.7152, 0.0722], np.float32)
    chroma = ratio - y_r[..., None]
    wide = cv2.GaussianBlur(chroma, (0, 0), 0.015 * max(guide.shape))
    chroma = wide + np.clip(chroma - wide, -(0.6 if neutral else 0.2), 0.6 if neutral else 0.2)
    ratio = y_r[..., None] + chroma
    if real and not evening:
        # By day the light stays soft and your own shadows stay yours. The model draws
        # shadows of its own (palms across a wall where the photo has sun): they came out
        # as blue blotches, so a dip well below the light round it is only kept where the
        # photo itself has an edge there. And the photo's own shadows are never pushed down
        # more than about half a stop - crushed, they looked flat, not golden.
        Lw4 = np.array([0.2126, 0.7152, 0.0722], np.float32)
        y4 = ratio @ Lw4
        broad = cv2.GaussianBlur(y4, (0, 0), 0.02 * max(guide.shape))
        gb4 = cv2.GaussianBlur(guide, (0, 0), 1.5)
        ex4, ey4 = cv2.Sobel(gb4, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(gb4, cv2.CV_32F, 0, 1, ksize=3)
        edge4 = cv2.GaussianBlur(np.clip(np.sqrt(ex4 * ex4 + ey4 * ey4) / 8 / 0.12, 0, 1), (0, 0), 3)
        dev = y4 - broad
        # where the painting's sun and shade disagree with the photo's own (its sun where the
        # photo is in shade, or the other way round) the model moved a shadow: left out
        lp = np.log2(guide + 0.02)
        pl = lp - cv2.GaussianBlur(lp, (0, 0), 0.02 * max(guide.shape))
        conflict = np.where(dev > 0, np.clip((-pl - 0.15) / 0.3, 0, 1), np.clip((pl - 0.1) / 0.3, 0, 1))
        dev = dev * (1 - cv2.GaussianBlur(conflict.astype(np.float32), (0, 0), 2))
        dip = np.minimum(dev, 0)
        keep_dip = np.maximum(dip * edge4, np.maximum(dip, -0.25))
        y5 = broad + np.maximum(dev, 0) + keep_dip
        dark = np.clip((0.3 - cv2.GaussianBlur(guide, (0, 0), 2)) / 0.2, 0, 1)
        y5 = np.where(y5 < 0, y5 * (1 - dark) + np.maximum(y5, -0.5) * dark, y5)
        ratio = ratio + (y5 - y4)[..., None]
    if real and blend != "strict":
        # "detail" (the default since the owner compared them, 2026-09-24): all of the painting's
        # light and colour - the porch glow, the pools on the steps, the lit garden - on the
        # photo's own fine detail, as ComfyUI relight workflows do ("frequency detail restore").
        # The strict light above drops every glow it cannot trace to a lamp in the photo, which
        # left twilights looking like a darkened photo with a few lit windows.
        # Where the model took a thing away or redrew it in another colour (a blue car it
        # painted teal), the strict light is kept: the car stays the photo's, lit like round it.
        det = _detail_ratio(G, D)
        # and where it drew something else there, much brighter (a dark mirror turned into a glowing white
        # panel): taken as light, that lifted the dark reflection 2-3 stops into blotches and noise
        up = det @ np.array([0.2126, 0.7152, 0.0722], np.float32)
        swapped = (np.clip((up - 1.8) / 0.6, 0, 1) * np.clip((0.25 - local_match(g, d)) / 0.15, 0, 1)
                   * np.clip((cv2.GaussianBlur(lg, (0, 0), 3) - 0.7) / 0.12, 0, 1))   # a near-white panel, not a lamp's glow
        # only a panel's worth of it (a mirror, a picture, a screen): the bright core of a lamp's glow or a
        # sliver of gloss is small, and taking the room's light there left dark blots
        nsw, lsw, ssw, _ = cv2.connectedComponentsWithStats((swapped > 0.5).astype(np.uint8), connectivity=8)
        big = np.zeros(nsw, np.float32)
        big[1:] = ssw[1:, 4] > 0.006 * guide.size
        swapped = swapped * big[lsw]
        swapped = cv2.GaussianBlur(cv2.dilate(swapped, ell(9)), (0, 0), 4)
        gone = cv2.GaussianBlur(_gone(guide, lg, G, D), (0, 0), 3)[..., None]
        # where the painting shows another scene altogether (a lawn where the photo has a patio,
        # through a window with burglar bars) its light up close is a ghost of that scene: only
        # the broad light is taken there. By day only: at dusk a lit window is just where the
        # painting differs, and the broad light made it a flat glowing block
        if not evening:
            other = np.clip((0.3 - local_match(g, d)) / 0.15, 0, 1) * valid
            other = cv2.morphologyEx(other, cv2.MORPH_OPEN, ell(9))
            gone = np.maximum(gone, cv2.GaussianBlur(other, (0, 0), 4)[..., None])
        ratio = det + (ratio - det) * gone
        Lw5 = np.array([0.2126, 0.7152, 0.0722], np.float32)
        warmth = ratio[..., 0] - ratio[..., 2]
        warm_look = float(np.median(warmth[valid > 0.5] if (valid > 0.5).any() else warmth)) >= 0.12
        if not evening:
            # By day the model still switches lights on inside: a sunroom's glass and a covered
            # patio came back glowing yellow (Blue sky day, Clean and bright on DJI_0403). In a look
            # that is not warm as a whole (not a golden hour), a spot that turned much warmer and
            # brighter than the light round it is a lamp, not daylight: it takes the light round it.
            sw = 0.03 * max(guide.shape)
            y5 = ratio @ Lw5
            glow = (np.clip((y5 - cv2.GaussianBlur(y5, (0, 0), sw) - 0.25) / 0.3, 0, 1)
                    * np.clip((warmth - cv2.GaussianBlur(warmth, (0, 0), sw) - 0.12) / 0.2, 0, 1))
            if warm_look:
                # a golden hour lights whole walls warm and bright: only a glow on what is glass in
                # the photo (darker than the wall round it, as windows are by day) is a lamp
                glass = np.clip((cv2.GaussianBlur(guide, (0, 0), 6) - guide - 0.04) / 0.08, 0, 1)
                glow = glow * cv2.GaussianBlur(glass, (0, 0), 1.5)
            glow = cv2.GaussianBlur(cv2.dilate(glow, ell(7)), (0, 0), 3)
            if glow.max() > 0.05:
                wk5 = 1 - np.clip(glow * 2, 0, 1)
                around = cv2.GaussianBlur(ratio * wk5[..., None], (0, 0), sw) / np.maximum(cv2.GaussianBlur(wk5, (0, 0), sw), 1e-3)[..., None]
                ratio = ratio + (around - ratio) * glow[..., None]
        if swapped.max() > 0.05:
            # there it takes the light of the room round it (a mirror is as much brighter as the room it shows)
            s3 = 0.05 * max(guide.shape)
            wk = 1 - swapped
            around = cv2.GaussianBlur(ratio * wk[..., None], (0, 0), s3) / np.maximum(cv2.GaussianBlur(wk, (0, 0), s3), 1e-3)[..., None]
            ratio = ratio + (around - ratio) * swapped[..., None]
        where_out = np.ones_like(guide) if outdoors else outside_mask(outside, *guide.shape)
        if not evening and not neutral and where_out is not None:
            # By day, trees and fronds the sun blew out pale against a white sky (a gap in the canopy): the
            # painting drew its blue sky there, and taken as colour it turned the photo's own leaves sky-blue so
            # they seemed cut away. Where the photo is bright and colourless but has structure, only the
            # painting's brightness is taken, not its colour.
            spread = D.max(-1) - D.min(-1)
            pale = np.clip((guide - 0.70) / 0.13, 0, 1) * np.clip((0.18 - spread) / 0.08, 0, 1)
            busy = np.clip((sd - 0.008) / 0.012, 0, 1)
            leaf = cv2.GaussianBlur(pale * busy * cv2.GaussianBlur(where_out.astype(np.float32), (0, 0), 6), (0, 0), 2.5)[..., None]
            y_ = (ratio @ np.array([0.2126, 0.7152, 0.0722], np.float32))[..., None]
            # only the painted sky's blue (golden light on those leaves is the look, and stays)
            blue = np.clip((ratio[..., 2] - ratio[..., 0] - 0.05) / 0.2, 0, 1)[..., None]
            ratio = ratio + (y_ - ratio) * leaf * blue
    ratio = _fill_edges(ratio, valid)
    if neutral and real and blend == "strict":
        ratio = _neutral(ratio, D, lights if lights is not None else np.zeros_like(guide), valid, G)
    m = (np.clip((ratio - RATIO_LO) / RATIO_SPAN, 0, 1) * 255 + 0.5).astype(np.uint8)
    if not real:
        return m, valid.astype(np.float32)
    # Only where the photo has nothing to show - blown out to white and flat (a
    # burnt-out window or sky) - does the model paint in its own detail. A
    # blue sky or a plain ceiling is simply relit: that already gives the
    # look's colours there, with nothing of the model's drawing in it.
    flat = np.clip(1 - (sd - 0.006) / 0.010, 0, 1) * np.clip((guide - 0.86) / 0.08, 0, 1)
    # whole areas, not specks and holes: a window pane is painted in one piece
    flat = cv2.morphologyEx(flat, cv2.MORPH_CLOSE, ell(15))
    flat = cv2.morphologyEx(flat, cv2.MORPH_OPEN, ell(19))
    H, W = guide.shape
    box = outside_mask(outside, H, W)
    if outdoors:
        # Outside, the photo stays the photo - every house, car, sign and tree relit, never
        # redrawn. The painting shows only in the sky (traced from the photo, so it follows
        # the ridge and the rooftops), in the far view the photo only shows through haze,
        # in the windows the look lit and where the photo is blown out to nothing.
        sky = _sky(G, guide, sd, lg, outside, D)
        haze = _haze(G, D, guide, lg, sky)
        view = np.maximum(sky, haze)
        blown_sky = np.clip((guide - 0.70) / 0.15, 0, 1) * np.clip((0.16 - (D.max(-1) - D.min(-1))) / 0.08, 0, 1)
        # only a sky that was blown white, not a pale blue one with clouds (that is painted over as before)
        skyb = sky > 0.5
        bluish = float(((D[..., 2] - D[..., 0]) > 0.08)[skyb].mean()) if skyb.any() else 1.0
        if real and evening and skyb.any() and float(blown_sky[skyb].mean()) > 0.6 and bluish < 0.1:
            # An evening over a sky that was blown white behind the trees: the painting's own
            # trees there are soft and drawn elsewhere (pasted in, the photo's sharp palm fronds
            # became a blurred double), and its light smeared over the leaves left the white
            # gaps between them as pale speckles. So the photo stays the photo here too: its
            # leaves and fronds darken like the trees below, and only what shows the white sky
            # takes the painting's sky colour - through the light map, which is lined up with
            # the full-size photo's edges later (upsample_light).
            R = m.astype(np.float32) / 255 * RATIO_SPAN + RATIO_LO
            blown = np.clip((guide - 0.70) / 0.15, 0, 1) * np.clip((0.16 - (D.max(-1) - D.min(-1))) / 0.08, 0, 1)
            tex = np.sqrt(cv2.GaussianBlur((lg - cv2.GaussianBlur(lg, (0, 0), 2.0)) ** 2, (0, 0), 3.0))
            ws = sky * blown * np.clip((0.03 - tex) / 0.02, 0, 1) * valid
            s1, s2 = 8, 0.06 * max(H, W)
            S1 = cv2.GaussianBlur(G * ws[..., None], (0, 0), s1) / np.maximum(cv2.GaussianBlur(ws, (0, 0), s1), 1e-4)[..., None]
            S2 = cv2.GaussianBlur(G * ws[..., None], (0, 0), s2) / np.maximum(cv2.GaussianBlur(ws, (0, 0), s2), 1e-4)[..., None]
            c1 = np.clip(cv2.GaussianBlur(ws, (0, 0), s1) / 0.3, 0, 1)[..., None]
            S = S2 + (S1 - S2) * c1
            r_sky = np.log2((S + 0.012) / (D + 0.012))
            # the light of the trees below the sky, carried up into it
            near = cv2.dilate(sky, ell(int(0.12 * H) | 1))
            wf = valid * (1 - cv2.dilate(sky, ell(9))) * (1 - blown) * np.clip(near, 0, 1)
            sf = 0.05 * max(H, W)
            den_f = cv2.GaussianBlur(wf, (0, 0), sf)
            if den_f.max() > 1e-3:
                r_fol = cv2.GaussianBlur(R * wf[..., None], (0, 0), sf) / np.maximum(den_f, 1e-4)[..., None]
                r_fol = np.where((den_f > 1e-3)[..., None], r_fol, np.median(R[wf > 0.5], axis=0) if (wf > 0.5).any() else R)
            else:
                r_fol = R
            z = cv2.GaussianBlur(sky, (0, 0), 1.0)[..., None]
            inside = blown[..., None] * r_sky + (1 - blown[..., None]) * r_fol
            R = R + (inside - R) * z
            # white sky showing through the canopy just under it: sky colour too
            fringe = (np.clip(near, 0, 1) * (1 - sky) * blown * valid)[..., None]
            R = R + (r_sky - R) * fringe
            m = (np.clip((R - RATIO_LO) / RATIO_SPAN, 0, 1) * 255 + 0.5).astype(np.uint8)
            view = haze * (1 - sky)
            if extra is not None:
                # frond or sky is decided again on the full-size photo (a frond is thinner than
                # one pixel here): where the sky is, the sky colour and the trees' light
                # and how dark the painting drew the leaves against that sky: at dusk a sunlit
                # frond is a dark shape, not a pale one (kept at the trees' light it stayed daylit)
                wt = z[..., 0] * np.clip((cv2.GaussianBlur(lg, (0, 0), 6) - lg - 0.03) / 0.05, 0, 1) * valid
                st = 0.04 * max(H, W)
                dt = cv2.GaussianBlur(wt, (0, 0), st)
                T = np.where(dt > 1e-3, cv2.GaussianBlur(lg * wt, (0, 0), st) / np.maximum(dt, 1e-4),
                             float(np.percentile(lg[sky > 0.5], 10)) if (sky > 0.5).any() else 0.15)
                band = cv2.GaussianBlur(np.maximum(sky, np.clip(near, 0, 1)) * valid, (0, 0), 0.02 * max(H, W))
                extra["sky"] = np.dstack([np.maximum(z[..., 0], band), np.zeros_like(band), S, r_fol,
                                          np.clip(T, 0.03, 0.5)]).astype(np.float32)
        where = None
    else:
        view = _night_view(box, G, guide, lum)
        if real:
            view = view * (1 - cv2.dilate(strips, ell(5)))      # no made-up strip lights in the window
        # blown-out and flat only counts at a window (near what Claude marked as outdoors, or
        # high up in the room with nothing marked): a blown sheet, a sun patch on the floor
        # or a white pillow is relit, never drawn over by the model
        if box is not None:
            where = cv2.dilate(box, np.ones((int(0.1 * H) | 1, int(0.1 * W) | 1), np.uint8))
        else:
            where = np.zeros((H, W), np.float32)
            where[:int(0.6 * H)] = 1
    if where is not None:
        n, lab, stats, _ = cv2.connectedComponentsWithStats((flat > 0.5).astype(np.uint8), connectivity=8)
        ok = np.zeros(n, bool)
        if n > 1:
            inw = np.bincount(lab.ravel(), weights=where.ravel(), minlength=n) / np.maximum(stats[:, 4], 1)
            ok[1:] = (inw[1:] > 0.6) & (stats[1:, 4] > 0.002 * H * W)
        flat = flat * cv2.dilate(ok[lab].astype(np.uint8), ell(9)).astype(np.float32)
    # only where the painting put something there (a view, a sky, a colour): a blown ceiling
    # it left plain white too is relit, not swapped for its own white (a pale block)
    tex_g = np.sqrt(cv2.GaussianBlur((lg - cv2.GaussianBlur(lg, (0, 0), 2.0)) ** 2, (0, 0), 3.0))
    changed = (np.abs(lg - guide) > 0.08) | ((G.max(-1) - G.min(-1)) > 0.12) | (tex_g > 0.012)
    changed = cv2.dilate(cv2.morphologyEx(changed.astype(np.uint8), cv2.MORPH_OPEN, ell(5)), ell(9)).astype(np.float32)
    flat = flat * cv2.GaussianBlur(changed, (0, 0), 3)
    if real and not evening:
        # by day a blown window stays the photo's, relit: the model painted lamps on behind the
        # glass (a sunroom came back glowing yellow on Blue sky day)
        lampish = np.clip((((G[..., 0] - G[..., 2]) - (D[..., 0] - D[..., 2])) - 0.08) / 0.10, 0, 1)
        flat = flat * (1 - cv2.GaussianBlur(cv2.dilate(lampish, ell(9)), (0, 0), 3))
    flat = _snap(flat, guide)
    lights = cv2.GaussianBlur(lights, (0, 0), 0.8)
    # the sky and the view run to the frame's edge (the painting is carried out there, see
    # extend_edges); a pane or a blown patch only where the model really painted
    rep_ = np.clip(np.maximum(np.maximum(flat, lights) * valid, view), 0, 1)
    return m, rep_.astype(np.float32)


def detail_keep(g: np.ndarray, d: np.ndarray) -> np.ndarray:
    """Where the photo's own full-size detail may be laid back over the
    painted picture (1) and where not (0). Where the model changed what is
    there - a view painted into a blown-out window, a new sky, a reflection -
    the old edges no longer belong and would show as faint double lines, so
    only the painted picture is used. Found by comparing the fine structure
    of the two pictures: where it still matches, the detail is kept."""
    import cv2
    gg = cv2.cvtColor(g, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255
    dd = cv2.cvtColor(d, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255
    hg = gg - cv2.GaussianBlur(gg, (0, 0), 2.0)
    hd = dd - cv2.GaussianBlur(dd, (0, 0), 2.0)
    b = lambda x: cv2.GaussianBlur(x, (0, 0), 4.0)
    vg, vd, cov = b(hg * hg), b(hd * hd), b(hg * hd)
    corr = cov / np.sqrt(vg * vd + 1e-8)
    keep = np.clip((corr - 0.25) / 0.45, 0, 1)
    # flat in the photo: there is no detail to double up, keep it (grain, texture)
    flat = np.clip(1 - (np.sqrt(vd) - 0.004) / 0.008, 0, 1)
    keep = np.maximum(keep, flat)
    keep = cv2.erode(keep, np.ones((3, 3), np.uint8))
    return cv2.GaussianBlur(keep, (0, 0), 1.5)


# --------------------------------------------------------------------------
# the generators
# --------------------------------------------------------------------------

_gpu = threading.Lock()          # one picture at a time on the local card
_last_use = [0.0]
_freed = [True]


def _http(url: str, data: Optional[bytes] = None, headers: Optional[dict] = None, method: Optional[str] = None,
          timeout: float = 60) -> bytes:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method or ("POST" if data is not None else "GET"))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _json(url: str, body=None, headers=None, timeout: float = 60):
    h = {"content-type": "application/json", **(headers or {})}
    return json.loads(_http(url, json.dumps(body).encode() if body is not None else None, h, timeout=timeout) or b"{}")


def _multipart(fields: dict, files: list) -> tuple:
    """fields {name: str}, files [(field, filename, bytes, mime)] -> (body, content-type)"""
    bnd = "zk" + uuid.uuid4().hex
    out = io.BytesIO()
    for k, v in fields.items():
        out.write(f'--{bnd}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for field, name, data, mime in files:
        out.write(f'--{bnd}\r\nContent-Disposition: form-data; name="{field}"; filename="{name}"\r\nContent-Type: {mime}\r\n\r\n'.encode())
        out.write(data)
        out.write(b"\r\n")
    out.write(f"--{bnd}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={bnd}"


def comfy_up() -> bool:
    try:
        _http(comfy_url() + "/system_stats", timeout=3)
        return True
    except Exception:
        return False


def comfy_start(wait: float = 150) -> bool:
    """Start the local ComfyUI if it is installed and not running."""
    if comfy_up():
        return True
    start = LOCAL_ROOT / "start.sh"
    if not start.is_file():
        return False
    log = open(LOCAL_ROOT / "comfyui.log", "ab")
    subprocess.Popen(["bash", str(start)], stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                     cwd=str(LOCAL_ROOT), start_new_session=True)
    t0 = time.time()
    while time.time() - t0 < wait:
        time.sleep(2)
        if comfy_up():
            return True
    return False


def _comfy_model() -> str:
    try:
        m = json.loads((LOCAL_ROOT / "zerko.json").read_text()).get("model")
        if m:
            return m
    except Exception:
        pass
    try:
        info = _json(comfy_url() + "/object_info/UnetLoaderGGUF")
        names = info["UnetLoaderGGUF"]["input"]["required"]["unet_name"][0]
        for n in names:
            if "qwen" in n.lower() and "edit" in n.lower():
                return n
    except Exception:
        pass
    raise HTTPException(status_code=502, detail="The Qwen Image Edit model is not in ComfyUI - run Install Image AI again.")


NEEDED = [("CLIPLoader", "clip_name", "qwen_2.5_vl_7b_fp8_scaled.safetensors", "text_encoders"),
          ("VAELoader", "vae_name", "qwen_image_vae.safetensors", "vae"),
          ("LoraLoaderModelOnly", "lora_name", "Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors", "loras")]


def comfy_missing() -> List[str]:
    """Model files ComfyUI cannot see, in plain words ("x belongs in models/y")."""
    out = []
    for node, field, name, folder in NEEDED:
        try:
            have = _json(comfy_url() + f"/object_info/{node}")[node]["input"]["required"][field][0]
        except Exception:
            continue
        if name not in have:
            out.append(f"{name} belongs in ComfyUI/models/{folder}")
    return out


def _comfy_upload(rgb: np.ndarray, name: str) -> str:
    body, ct = _multipart({"overwrite": "true", "type": "input"}, [("image", name, to_png(rgb), "image/png")])
    d = json.loads(_http(comfy_url() + "/upload/image", body, {"content-type": ct}, timeout=60))
    return d.get("name") or name


# How far the model may move from the photo. At 1.0 it starts from noise and,
# on a busy scene (a drone shot of a hillside of houses), redraws and reframes
# everything; starting from the photo itself keeps every house where it is.
DENOISE = 1.0


def comfy_workflow(model: str, images: List[str], prompt: str, seed: int, steps: int = 4, denoise: float = DENOISE) -> dict:
    """Qwen Image Edit 2511 with the 4-step Lightning LoRA (ComfyUI's own
    template, in API form). images[0] is the photo; any more are examples."""
    wf = {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": model}},
        "2": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": "Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors", "strength_model": 1.0}},
        "3": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["2", 0], "shift": 3.1}},
        "4": {"class_type": "CFGNorm", "inputs": {"model": ["3", 0], "strength": 1.0}},
        "5": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_2.5_vl_7b_fp8_scaled.safetensors", "type": "qwen_image", "device": "default"}},
        "6": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_vae.safetensors"}},
    }
    pos = {"clip": ["5", 0], "prompt": prompt, "vae": ["6", 0]}
    neg = {"clip": ["5", 0], "prompt": "", "vae": ["6", 0]}
    for i, name in enumerate(images[:3]):
        nid = str(20 + i)
        wf[nid] = {"class_type": "LoadImage", "inputs": {"image": name}}
        pos[f"image{i + 1}"] = [nid, 0]
        neg[f"image{i + 1}"] = [nid, 0]
    wf["8"] = {"class_type": "TextEncodeQwenImageEditPlus", "inputs": pos}
    wf["9"] = {"class_type": "TextEncodeQwenImageEditPlus", "inputs": neg}
    wf["10"] = {"class_type": "FluxKontextMultiReferenceLatentMethod", "inputs": {"conditioning": ["8", 0], "reference_latents_method": "index_timestep_zero"}}
    wf["11"] = {"class_type": "FluxKontextMultiReferenceLatentMethod", "inputs": {"conditioning": ["9", 0], "reference_latents_method": "index_timestep_zero"}}
    wf["12"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["20", 0], "vae": ["6", 0]}}
    wf["13"] = {"class_type": "KSampler", "inputs": {"model": ["4", 0], "seed": seed, "steps": steps, "cfg": 1.0, "sampler_name": "euler",
                                                     "scheduler": "simple", "positive": ["10", 0], "negative": ["11", 0],
                                                     "latent_image": ["12", 0], "denoise": denoise}}
    wf["14"] = {"class_type": "VAEDecode", "inputs": {"samples": ["13", 0], "vae": ["6", 0]}}
    wf["15"] = {"class_type": "SaveImage", "inputs": {"images": ["14", 0], "filename_prefix": "zerko/look"}}
    return wf


def _comfy_edit(photo: np.ndarray, examples: List[np.ndarray], prompt: str, seed: int) -> np.ndarray:
    if not comfy_start():
        raise HTTPException(status_code=502, detail="ComfyUI is not running and could not be started. Check Manage > AI > Image generation.")
    model = _comfy_model()
    missing = comfy_missing()
    if missing:
        raise HTTPException(status_code=502, detail="A model file is missing or in the wrong folder: " + "; ".join(missing) + f" (under {LOCAL_ROOT}).")
    tag = uuid.uuid4().hex[:10]
    names = [_comfy_upload(photo, f"zk_{tag}_0.png")]
    for i, ex in enumerate(examples[:2]):
        names.append(_comfy_upload(ex, f"zk_{tag}_{i + 1}.png"))
    wf = comfy_workflow(model, names, prompt, seed)
    try:
        d = _json(comfy_url() + "/prompt", {"prompt": wf, "client_id": "zerko"})
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "replace")[:600]
        raise HTTPException(status_code=502, detail=f"ComfyUI refused the job: {msg}")
    pid = d.get("prompt_id")
    if not pid:
        raise HTTPException(status_code=502, detail=f"ComfyUI refused the job: {json.dumps(d)[:400]}")
    t0 = time.time()
    while time.time() - t0 < 900:
        time.sleep(1.5)
        h = _json(comfy_url() + f"/history/{pid}").get(pid)
        if not h:
            continue
        st = h.get("status") or {}
        if st.get("status_str") == "error":
            msgs = [m for m in st.get("messages", []) if m and m[0] == "execution_error"]
            err = (msgs[0][1].get("exception_message") if msgs else "") or "unknown error"
            if "out of memory" in err.lower():
                err = "the graphics card ran out of memory - close games or other GPU apps and try again"
            raise HTTPException(status_code=502, detail=f"ComfyUI: {err.strip()[:300]}")
        for out in (h.get("outputs") or {}).values():
            for im in out.get("images", []):
                q = urllib.parse.urlencode({"filename": im["filename"], "subfolder": im.get("subfolder", ""), "type": im.get("type", "output")})
                return decode(_http(comfy_url() + "/view?" + q, timeout=60))
    raise HTTPException(status_code=504, detail="ComfyUI took more than 15 minutes on one picture.")


def comfy_free():
    try:
        _json(comfy_url() + "/free", {"unload_models": True, "free_memory": True}, timeout=10)
    except Exception:
        pass


def _idle_watch():
    """Give the graphics card back after ten quiet minutes, so a game or
    Resolve started later gets all of it."""
    while True:
        time.sleep(60)
        if not _freed[0] and time.time() - _last_use[0] > 600:
            comfy_free()
            _freed[0] = True


threading.Thread(target=_idle_watch, daemon=True).start()


def _fal_edit(photo, examples, prompt, seed):
    k = _key("fal")
    imgs = ["data:image/jpeg;base64," + base64.b64encode(to_jpeg(x)).decode() for x in [photo, *examples[:2]]]
    body = {"prompt": prompt, "image_urls": imgs, "num_images": 1, "seed": seed, "output_format": "png",
            "enable_safety_checker": False, "image_size": {"width": photo.shape[1], "height": photo.shape[0]}}
    try:
        d = _json(f"https://fal.run/{model_for('fal')}", body, {"Authorization": f"Key {k}"}, timeout=300)
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"fal.ai answered {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
    url = ((d.get("images") or [{}])[0]).get("url")
    if not url:
        raise HTTPException(status_code=502, detail="fal.ai returned no picture")
    if url.startswith("data:"):
        return decode(base64.b64decode(url.split(",", 1)[1]))
    return decode(_http(url, timeout=120))


def _gemini_edit(photo, examples, prompt, seed):
    k = _key("gemini")
    parts = [{"text": prompt}]
    for x in [photo, *examples[:2]]:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(to_jpeg(x)).decode()}})
    body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": {"responseModalities": ["IMAGE"], "seed": seed}}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{urllib.parse.quote(model_for('gemini'))}:generateContent"
    try:
        d = _json(url, body, {"x-goog-api-key": k}, timeout=300)
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Gemini answered {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
    for c in d.get("candidates", []):
        for p in (c.get("content") or {}).get("parts", []):
            inl = p.get("inline_data") or p.get("inlineData")
            if inl and inl.get("data"):
                return decode(base64.b64decode(inl["data"]))
    raise HTTPException(status_code=502, detail="Gemini returned no picture (it may have declined the request)")


def _openai_edit(photo, examples, prompt, seed):
    k = _key("openai")
    h, w = photo.shape[:2]
    size = "1536x1024" if w > h * 1.2 else ("1024x1536" if h > w * 1.2 else "1024x1024")
    files = [("image[]", f"img{i}.png", to_png(x), "image/png") for i, x in enumerate([photo, *examples[:2]])]
    body, ct = _multipart({"model": model_for("openai"), "prompt": prompt, "size": size, "quality": "high", "input_fidelity": "high"}, files)
    try:
        d = json.loads(_http("https://api.openai.com/v1/images/edits", body, {"Authorization": f"Bearer {k}", "content-type": ct}, timeout=300))
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"OpenAI answered {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
    b = ((d.get("data") or [{}])[0]).get("b64_json")
    if not b:
        raise HTTPException(status_code=502, detail="OpenAI returned no picture")
    return decode(base64.b64decode(b))


def edit(photo: np.ndarray, prompt: str, examples: Optional[List[np.ndarray]] = None, seed: Optional[int] = None) -> np.ndarray:
    """Repaint `photo` (uint8 RGB, already at fit_size) as asked. Returns a
    uint8 RGB picture of the same size, lined up with the photo."""
    return align(_edit_raw(photo, prompt, examples, seed), photo)[0]


def _edit_raw(photo: np.ndarray, prompt: str, examples: Optional[List[np.ndarray]] = None, seed: Optional[int] = None) -> np.ndarray:
    b = backend()
    if not enabled():
        raise ImageOff("Image generation is off: set it up in Manage > AI > Image generation.")
    ex = [resize(x, *_ex_size(x)) for x in (examples or [])]
    seed = int(seed if seed is not None else random.randint(1, 2 ** 31))
    if b == "comfyui":
        with _gpu:
            _last_use[0] = time.time()
            _freed[0] = False
            out = _comfy_edit(photo, ex, prompt, seed)
            _last_use[0] = time.time()
    elif b == "fal":
        out = _fal_edit(photo, ex, prompt, seed)
    elif b == "gemini":
        out = _gemini_edit(photo, ex, prompt, seed)
    else:
        out = _openai_edit(photo, ex, prompt, seed)
    import ai_usage
    ai_usage.record(b, "" if b == "comfyui" else model_for(b), photo.shape[1], photo.shape[0])
    return out


def edit_aligned(photo: np.ndarray, prompt: str, examples: Optional[List[np.ndarray]] = None, seed: Optional[int] = None):
    """Like edit(), and where the lined-up picture has real content."""
    raw = _edit_raw(photo, prompt, examples, seed)
    return align(raw, photo)


def _ex_size(x: np.ndarray) -> tuple:
    # examples only show the finish: small is enough, and keeps the card's memory for the photo
    s = min(1.0, math.sqrt(512 * 512 / float(x.shape[0] * x.shape[1])))
    return max(16, int(x.shape[1] * s) // 16 * 16), max(16, int(x.shape[0] * s) // 16 * 16)


# --------------------------------------------------------------------------
# the studio's looks
# --------------------------------------------------------------------------

_LOOKS_HOME = Path(os.environ.get("ZK_LOOKS_DIR") or HERE)    # a test copy keeps its own
LOOKS_FILE = _LOOKS_HOME / "ai_looks.json"
LOOKS_DIR = _LOOKS_HOME / "ai_looks"
_looks_lock = threading.Lock()
KEEP = ("Keep the room or building, every object, the walls, windows, furniture, the camera angle, framing, zoom and "
        "composition exactly the same - do not move, add or remove cars, people, buildings, trees or anything else, and do "
        "not redraw the neighbouring houses or the hillside. Change only the light, the colour, the sky and what is seen "
        "through windows. Switch on only lights that are already in the photo - its lamps, light fittings and windows - "
        "never add a new lamp, fitting or lit window; a lit window glows across its whole pane. A real photograph taken "
        "with a professional camera, not HDR, not CGI, no text.")

# The four the owner asked for. They are an ordinary part of his list: edit,
# rename or delete them like any look he makes himself.
FIRST_LOOKS = [
    {"name": "Clean and bright", "prompt": "Professional real-estate retouch, bright and clean: pure neutral white walls and ceilings with no colour cast, even soft daylight filling the room, lifted shadows, gentle natural contrast, true-to-life colours, crisp detail, and clear bright views through the windows (blue sky, the real view) instead of blown-out white."},
    {"name": "Twilight", "prompt": "Real-estate twilight at blue hour, about twenty minutes after sunset: the whole sky a deep, clean twilight blue, darker at the top, fading to a thin warm peach glow along the horizon, no daylight left anywhere; the outside view darker and crisp, no haze, with the lights of the town and the street lamps on. Every light that is already in the photo is on: each window glows warm 2700K across its whole pane, lamps, pendants, downlights, wall and garden lights are lit and throw soft warm light onto the walls, floor, paths and lawn nearby. The ambient light is dim and cool, so the warm lights stand out; skylights and glass show the dusk sky. Balanced like a professional twilight shoot: nothing blown out, shadows dark but not black."},
    {"name": "Blue sky day", "prompt": "Bright sunny day: a clear deep blue sky with a few soft white clouds, clean sunlight, fresh green lawn and plants, clean white walls, true-to-life colours and crisp detail."},
    {"name": "Golden hour", "prompt": "Golden hour shortly before sunset: low warm sunlight raking across the property, a warm glowing sky, soft long shadows, rich but natural colours, interior lights softly on."},
]


OLD_TWILIGHT = "Real-estate twilight just after sunset: a deep blue dusk sky with a soft pink and orange glow near the horizon, every interior light on so warm 2700K light glows through all the windows, exterior wall and garden lights on, warm light spilling onto walls, paths and lawn; the ambient light darker and bluer but the property clearly lit."


def _looks_load() -> list:
    try:
        d = json.loads(LOOKS_FILE.read_text())
        if isinstance(d, list):
            # the first Twilight, never changed by the owner: the better words
            for x in d:
                if x.get("prompt") == OLD_TWILIGHT:
                    x["prompt"] = FIRST_LOOKS[1]["prompt"]
            return d
    except FileNotFoundError:
        looks = [{"id": uuid.uuid4().hex[:10], "examples": [], "tailor": True, "seed": None, **x} for x in FIRST_LOOKS]
        _looks_save(looks)
        return looks
    except Exception:
        pass
    return []


def _looks_save(looks: list):
    tmp = LOOKS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(looks, indent=1))
    os.replace(tmp, LOOKS_FILE)


def get_look(look_id: str) -> dict:
    for x in _looks_load():
        if x.get("id") == look_id:
            return x
    raise HTTPException(status_code=404, detail="That look no longer exists")


def look_examples(look: dict) -> List[np.ndarray]:
    out = []
    for n in look.get("examples", [])[:2]:
        p = LOOKS_DIR / look["id"] / n
        try:
            out.append(decode(p.read_bytes()))
        except Exception:
            continue
    return out


def look_prompt(look: dict, n_examples: int) -> str:
    p = look.get("prompt", "").strip()
    if n_examples:
        pics = "picture 2" if n_examples == 1 else "pictures 2 and 3"
        p += (f" Match the finish of the example{'s' if n_examples > 1 else ''} in {pics} - their brightness, white balance, "
              f"contrast and colour - but edit picture 1 only: nothing from the example{'s' if n_examples > 1 else ''} is copied into it.")
    return f"{p} {look.get('keep') or KEEP}"


# --------------------------------------------------------------------------
# virtual staging: the room furnished (and, if asked, refreshed) in a style
# --------------------------------------------------------------------------

STAGE_KEEP = ("Keep the room's architecture exactly as it is: the walls, windows, doors, ceiling, floor area, built-in "
              "fittings, the view outside, the camera angle, framing and zoom. Furniture must fit the room's real size and "
              "perspective, stand on the floor with natural shadows and match the light coming in. A real photograph of a "
              "professionally staged home, not CGI, no people, no text.")

STAGE_STYLES = {
    "Modern": "modern contemporary furniture: clean lines, neutral whites, greys and charcoal, a low sofa or bed, a large textured rug, simple art and a couple of plants",
    "Scandinavian": "Scandinavian style: light oak, white and soft grey, linen and wool textiles, simple pale wood furniture, green plants",
    "Coastal": "relaxed coastal style: white and sand tones with soft blue accents, rattan and linen, light natural wood",
    "Luxury": "luxury styling: rich textures, velvet and boucle upholstery, marble and brass accents, a statement light fitting",
    "Farmhouse": "modern farmhouse style: natural and whitewashed wood, cream and beige, woven textures, black metal details",
    "Industrial": "industrial loft style: leather, black steel, reclaimed wood, warm Edison lighting",
    "Minimal": "minimal styling: very few, beautiful pieces, lots of calm space, warm neutral tones",
}

STAGE_CHANGES = {
    "walls": "repaint every wall a fresh clean white",
    "floors": "replace the flooring with light natural oak boards",
    "kitchen": "give the kitchen modern handleless white cabinets with a stone counter",
    "bathroom": "give the bathroom modern large-format tiles and new fittings",
}


def stage_look(style: str, empty: bool, changes: List[str], words: str = "") -> dict:
    what = STAGE_STYLES.get(style, style)
    p = "Virtually stage this room. Decide from the photo what the room is for (lounge, bedroom, dining room, study, patio) and furnish it for that with " + what + "."
    if empty:
        p += " First take out every piece of existing furniture, clutter and personal items."
    extra = [STAGE_CHANGES[c] for c in changes if c in STAGE_CHANGES]
    if extra:
        p += " Also " + "; ".join(extra) + "."
    if words.strip():
        p += " " + words.strip()
    return {"id": None, "name": f"Staged: {style}", "prompt": p, "tailor": True, "real": False, "keep": STAGE_KEEP, "label": True}


def label_staged(g: np.ndarray) -> np.ndarray:
    """Write "Virtually staged" small in the bottom-left corner: buyers and
    the portals expect to be told."""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.fromarray(g)
    dr = ImageDraw.Draw(im, "RGBA")
    size = max(12, im.height // 42)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", size)
    except Exception:
        font = ImageFont.load_default()
    text = "Virtually staged"
    x0, y0, x1, y1 = dr.textbbox((0, 0), text, font=font)
    pad = size // 2
    bx, by = pad, im.height - (y1 - y0) - pad * 3
    dr.rectangle([bx, by, bx + (x1 - x0) + pad * 2, by + (y1 - y0) + pad * 2], fill=(0, 0, 0, 110))
    dr.text((bx + pad - x0, by + pad - y0), text, fill=(255, 255, 255, 235), font=font)
    return np.asarray(im).copy()


class LookBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    prompt: str = Field(..., min_length=3, max_length=2000)
    tailor: bool = True
    real: bool = True       # keep the photo's own detail; only light, sky and window views change
    blend: str = Field("detail", pattern=r"^(detail|strict)$")   # detail: all the painting's light on the photo's detail; strict: only light traced to the photo
    seed: Optional[int] = Field(None, ge=0, le=2 ** 31)


def _public(x: dict) -> dict:
    return {**{k: x.get(k) for k in ("id", "name", "prompt", "examples", "tailor", "seed")}, "real": x.get("real", True),
            "blend": x.get("blend", "detail")}


@router.get("/looks")
def list_looks(current_user: User = Depends(get_current_user)):
    with _looks_lock:
        return {"looks": [_public(x) for x in _looks_load()]}


@router.post("/looks")
def add_look(body: LookBody, current_user: User = Depends(get_current_user)):
    with _looks_lock:
        looks = _looks_load()
        x = {"id": uuid.uuid4().hex[:10], "examples": [], **body.model_dump()}
        looks.append(x)
        _looks_save(looks)
    return _public(x)


@router.put("/looks/{look_id}")
def edit_look(look_id: str, body: LookBody, current_user: User = Depends(get_current_user)):
    with _looks_lock:
        looks = _looks_load()
        for x in looks:
            if x.get("id") == look_id:
                x.update(body.model_dump())
                _looks_save(looks)
                return _public(x)
    raise HTTPException(status_code=404, detail="That look no longer exists")


@router.delete("/looks/{look_id}")
def delete_look(look_id: str, current_user: User = Depends(get_current_user)):
    with _looks_lock:
        looks = [x for x in _looks_load() if x.get("id") != look_id]
        _looks_save(looks)
    return {"ok": True}


class OrderBody(BaseModel):
    ids: List[str]


@router.put("/looks-order")
def order_looks(body: OrderBody, current_user: User = Depends(get_current_user)):
    with _looks_lock:
        looks = _looks_load()
        pos = {k: i for i, k in enumerate(body.ids)}
        looks.sort(key=lambda x: pos.get(x.get("id"), 999))
        _looks_save(looks)
    return {"looks": [_public(x) for x in looks]}


def add_example(look_id: str, rgb: np.ndarray) -> dict:
    """Keep a picture (uint8 RGB) as one of a look's examples (two at most:
    the newest replaces the oldest)."""
    with _looks_lock:
        looks = _looks_load()
        x = next((l for l in looks if l.get("id") == look_id), None)
        if not x:
            raise HTTPException(status_code=404, detail="That look no longer exists")
        d = LOOKS_DIR / look_id
        d.mkdir(parents=True, exist_ok=True)
        s = min(1.0, 1600.0 / max(rgb.shape[:2]))
        small = resize(rgb, int(rgb.shape[1] * s), int(rgb.shape[0] * s))
        name = uuid.uuid4().hex[:12] + ".jpg"
        (d / name).write_bytes(to_jpeg(small, 90))
        ex = (x.get("examples") or []) + [name]
        for old in ex[:-2]:
            try:
                (d / old).unlink()
            except OSError:
                pass
        x["examples"] = ex[-2:]
        _looks_save(looks)
        return _public(x)


@router.post("/looks/{look_id}/examples")
async def upload_example(look_id: str, file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    data = await file.read()
    if len(data) > 60 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="That picture is too big (60 MB at most)")
    try:
        rgb = decode(data)
    except Exception:
        raise HTTPException(status_code=400, detail="That file is not a picture Zerko can read (use JPEG, PNG or WebP)")
    return add_example(look_id, rgb)


@router.delete("/looks/{look_id}/examples/{name}")
def delete_example(look_id: str, name: str, current_user: User = Depends(get_current_user)):
    if not re.fullmatch(r"[a-f0-9]{12}\.jpg", name):
        raise HTTPException(status_code=400, detail="Bad name")
    with _looks_lock:
        looks = _looks_load()
        for x in looks:
            if x.get("id") == look_id:
                x["examples"] = [n for n in x.get("examples", []) if n != name]
                _looks_save(looks)
                try:
                    (LOOKS_DIR / look_id / name).unlink()
                except OSError:
                    pass
                return _public(x)
    raise HTTPException(status_code=404, detail="That look no longer exists")


@router.get("/looks/{look_id}/examples/{name}")
def get_example(look_id: str, name: str, request: Request, token: Optional[str] = None):
    from auth import get_user_from_token
    from database import SessionLocal
    db = SessionLocal()
    try:
        auth = request.headers.get("Authorization") or ""
        get_user_from_token(token or auth[7:], db)
    finally:
        db.close()
    if not re.fullmatch(r"[a-f0-9]{10}", look_id) or not re.fullmatch(r"[a-f0-9]{12}\.jpg", name):
        raise HTTPException(status_code=400, detail="Bad name")
    p = LOOKS_DIR / look_id / name
    if not p.is_file():
        raise HTTPException(status_code=404, detail="No such picture")
    return FileResponse(str(p), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


# --------------------------------------------------------------------------
# settings, test and the local install
# --------------------------------------------------------------------------

def _admin(u: User):
    if u.role != "admin":
        raise HTTPException(status_code=403, detail="Administrators only")


@router.get("/status")
def status(current_user: User = Depends(get_current_user)):
    import ai_usage
    b = backend()
    # what one full-size painting probably costs (the prices in Manage > AI), shown before paid actions
    return {"on": enabled(), "backend": b, "name": NAMES[b], "price": ai_usage.estimate(b, GEN_PIXELS / 1e6)}


@router.get("/settings")
def get_settings(current_user: User = Depends(get_current_user)):
    _admin(current_user)
    s = settings()
    return {
        "on": enabled(), "backend": backend(), "backends": [{"id": b, "name": NAMES[b]} for b in BACKENDS],
        "comfy_url": comfy_url(), "local": {"installed": local_installed(), "running": comfy_up(), "folder": str(LOCAL_ROOT),
                                            "installing": _install["state"] == "running"},
        "keys": {b: ai._hint(_key(b)) for b in ("fal", "gemini", "openai")},
        "models": {b: model_for(b) for b in ("fal", "gemini", "openai")},
        "default_models": DEFAULT_MODELS,
    }


class ImageSettings(BaseModel):
    backend: Optional[str] = Field(None, pattern="^(off|comfyui|fal|gemini|openai)$")
    comfy_url: Optional[str] = Field(None, max_length=200)
    fal_key: Optional[str] = Field(None, max_length=400)
    gemini_key: Optional[str] = Field(None, max_length=400)
    openai_key: Optional[str] = Field(None, max_length=400)
    fal_model: Optional[str] = Field(None, max_length=120)
    gemini_model: Optional[str] = Field(None, max_length=120)
    openai_model: Optional[str] = Field(None, max_length=120)


@router.put("/settings")
def put_settings(body: ImageSettings, current_user: User = Depends(get_current_user)):
    _admin(current_user)
    s = settings()
    for k, v in body.model_dump().items():
        if v is not None:
            s[k] = v.strip()
    if s.get("comfy_url") and not re.match(r"^https?://", s["comfy_url"]):
        raise HTTPException(status_code=400, detail="The ComfyUI address starts with http:// (usually http://127.0.0.1:8188)")
    _save_settings(s)
    return get_settings(current_user)


@router.post("/test")
def test(current_user: User = Depends(get_current_user)):
    """Paint one small picture end to end: proves the model loads and answers."""
    _admin(current_user)
    import ai_usage
    ai_usage.context.set({"kind": "test", "user": current_user.username})
    import cv2
    w, h = fit_size(4, 3, 512 * 512)
    img = np.zeros((h, w, 3), np.uint8)
    img[:] = (200, 200, 205)
    cv2.rectangle(img, (w // 4, h // 3), (3 * w // 4, h - 20), (150, 120, 100), -1)
    cv2.rectangle(img, (w // 2 - 30, h // 2), (w // 2 + 30, h // 2 + 60), (230, 230, 240), -1)
    t0 = time.time()
    out = edit(img, "Turn this simple drawing into a small house at dusk with warm light in its window. " + KEEP)
    return {"ok": True, "seconds": round(time.time() - t0, 1), "size": [int(out.shape[1]), int(out.shape[0])]}


_install = {"state": "idle", "proc": None}


@router.post("/install")
def install_local(current_user: User = Depends(get_current_user)):
    """Run the local installer in the background (NVIDIA card, about 23 GB)."""
    _admin(current_user)
    if _install["state"] == "running":
        return {"state": "running"}
    if not INSTALLER.is_file():
        raise HTTPException(status_code=404, detail="The installer is missing from tools/imagegen")
    log = open(INSTALL_LOG, "wb")
    p = subprocess.Popen(["bash", str(INSTALLER)], stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         cwd=str(INSTALLER.parent), start_new_session=True)
    _install.update(state="running", proc=p)

    def wait():
        p.wait()
        _install["state"] = "done" if p.returncode == 0 else "failed"
    threading.Thread(target=wait, daemon=True).start()
    return {"state": "running"}


@router.get("/install")
def install_state(current_user: User = Depends(get_current_user)):
    _admin(current_user)
    try:
        tail = INSTALL_LOG.read_text(errors="replace")[-4000:]
    except Exception:
        tail = ""
    st = _install["state"]
    if st == "idle" and "DONE." in tail:
        st = "done"
    return {"state": st, "log": tail, "installed": local_installed()}


@router.post("/local/start")
def local_start(current_user: User = Depends(get_current_user)):
    _admin(current_user)
    return {"running": comfy_start()}


@router.post("/local/free")
def local_free(current_user: User = Depends(get_current_user)):
    """Give the graphics card's memory back now."""
    _admin(current_user)
    comfy_free()
    _freed[0] = True
    return {"ok": True}


def install(app):
    app.include_router(router)
