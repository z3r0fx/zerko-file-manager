"""The HDR merge engine: brackets -> one clean photo, the way a raw developer does it.

Why this replaced plain exposure fusion (Mertens) for the HDR panel:

- Fusion works on the finished 8-bit frames and picks "well exposed" pixels.
  Around a downlight that means it took the dark frame's grey version of the
  light, so lamps and their reflections came out as grey discs with a ring.
  Here the frames are merged into one linear radiance image first, so the
  brightest thing stays the brightest thing, and only then tone-mapped.
- Brackets are often hand-held and the long frames (1/13 s at ISO 5000) are
  shaken. Fusion leaned on them for every dark area, so a chair in the
  foreground came out soft and doubled. Here every frame is lined up to a
  sharp reference with sub-pixel accuracy (perspective, not whole pixels),
  and each pixel of a frame only counts where it agrees with the reference -
  a blurred or moved frame is dropped exactly where it is blurred or moved.
- Everything runs in float and is dithered at the end, so smooth walls and
  the glow of a light never band.

The tone mapping splits the (log) brightness into a large-scale base, found
with an edge-aware (guided) filter so there are no halos, and the detail on
top of it. Only the base is compressed - window to room - and the detail is
kept whole, so texture and focus survive.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np


def _cv2():
    import cv2
    return cv2


# --------------------------------------------------------------------------
# exposure from the file itself
# --------------------------------------------------------------------------

_TAGS = {0x829A: "t", 0x829D: "f", 0x8827: "iso", 0x9204: "bias", 0x9003: "taken", 0x9291: "subsec"}


def exif_exposure(path: str) -> dict:
    """Shutter, aperture, ISO and bias straight from the TIFF/EXIF tags.

    Reads the TIFF structure itself, so it works for RAW files Pillow cannot
    open (NEF, DNG, ARW, CR2 are TIFF inside) as well as for JPEG and TIFF.
    Auto-ISO brackets change ISO between frames, which is why ISO matters.
    """
    out: dict = {}
    try:
        with open(path, "rb") as fh:
            d = fh.read(1 << 20)
    except OSError:
        return out
    start = 0
    if d[:2] == b"\xff\xd8":                      # JPEG: the TIFF block in APP1
        i = d.find(b"Exif\x00\x00")
        if i < 0:
            return out
        start = i + 6
    t = d[start:]
    if t[:2] not in (b"II", b"MM"):
        return out
    bo = "<" if t[:2] == b"II" else ">"

    def u16(o):
        return struct.unpack(bo + "H", t[o:o + 2])[0]

    def u32(o):
        return struct.unpack(bo + "I", t[o:o + 4])[0]

    seen = set()

    def walk(off, depth=0):
        if depth > 6 or off in seen or off <= 0 or off + 2 > len(t):
            return
        seen.add(off)
        n = u16(off)
        for k in range(min(n, 400)):
            e = off + 2 + 12 * k
            if e + 12 > len(t):
                return
            tag, typ, cnt = u16(e), u16(e + 2), u32(e + 4)
            vo = e + 8
            if tag in (0x8769, 0x014A):          # EXIF IFD, sub-IFDs
                try:
                    walk(u32(vo), depth + 1)
                except struct.error:
                    pass
            name = _TAGS.get(tag)
            if not name or name in out:
                continue
            try:
                if typ in (5, 10):
                    p = u32(vo)
                    a, b = struct.unpack(bo + ("II" if typ == 5 else "ii"), t[p:p + 8])
                    if b:
                        out[name] = a / b
                elif typ == 2:
                    p = u32(vo) if cnt > 4 else vo
                    out[name] = t[p:p + cnt].split(b"\0")[0].decode("ascii", "ignore").strip()
                elif typ == 3:
                    out[name] = float(u16(vo))
                elif typ == 4:
                    out[name] = float(u32(vo))
            except struct.error:
                pass
        try:
            walk(u32(off + 2 + 12 * n), depth + 1)
        except struct.error:
            pass

    try:
        walk(u32(4))
    except struct.error:
        pass
    return out


def exposures(paths: Sequence[str], frames: Sequence[np.ndarray], linear: bool) -> Tuple[np.ndarray, np.ndarray]:
    """(brightness, light) per frame, both relative.

    brightness = shutter x ISO / f-number^2: how bright the frame came out,
    what the pixel values are divided by. light = shutter / f-number^2: how
    much light the sensor actually gathered, which is what sets the noise -
    raising the ISO makes a frame brighter, not cleaner.
    Missing EXIF: worked out from the frames themselves.
    """
    info = [exif_exposure(p) for p in paths]
    ok = all(i.get("t") for i in info)
    if ok:
        f0 = next((i["f"] for i in info if i.get("f")), 1.0)
        iso0 = next((i["iso"] for i in info if i.get("iso")), 100.0)
        light = np.array([i["t"] / (i.get("f") or f0) ** 2 for i in info], np.float64)
        bright = light * np.array([(i.get("iso") or iso0) for i in info], np.float64)
        # a camera that writes the same shutter for every frame of a bracket
        # (some phones) is not telling the truth: fall back to measuring
        if len(set(np.round(np.log2(bright), 2))) > 1:
            return bright / bright.max(), light / light.max()
    # measured: median ratio of mid-tone pixels, in (roughly) linear light
    lum = []
    for f in frames:
        x = f.astype(np.float32) / (65535.0 if f.dtype == np.uint16 else 255.0)
        if not linear:
            x = x ** 2.2
        lum.append(x.mean(axis=2)[::8, ::8])
    order = np.argsort([float(np.median(v)) for v in lum])
    rel = np.ones(len(frames))
    for a, b in zip(order[:-1], order[1:]):
        ok_px = (lum[a] > 0.02) & (lum[a] < 0.8) & (lum[b] > 0.02) & (lum[b] < 0.8)
        r = float(np.median(lum[b][ok_px] / lum[a][ok_px])) if ok_px.sum() > 200 else 2.0
        rel[b] = rel[a] * max(1.05, r)
    rel = rel / rel.max()
    return rel, rel


# --------------------------------------------------------------------------
# reading the frames
# --------------------------------------------------------------------------

@dataclass
class Frame:
    img: np.ndarray          # BGR, uint16 linear (RAW) or uint8 display (JPEG, camera JPEG)
    linear: bool
    how: str                 # "full" or "preview"


def load(path: str, max_dim: int, fast: bool = False) -> Frame:
    """A frame for merging. RAW that LibRaw reads: 16-bit linear, no curve,
    the camera's white balance. Anything else (JPEG, TIFF, or the full-size
    JPEG inside a RAW LibRaw cannot decode): the 8-bit picture as it is; the
    camera's curve is measured and taken out later."""
    import previews
    cv2 = _cv2()
    from pathlib import Path
    ext = Path(path).suffix.lower()
    img, linear, how = None, False, "full"
    if ext in previews.RAW_EXT:
        try:
            import rawpy
            with rawpy.imread(path) as raw:
                rgb = raw.postprocess(use_camera_wb=True, no_auto_bright=True, gamma=(1, 1),
                                      output_bps=16, half_size=fast, user_flip=None)
            img, linear = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), True
        except Exception:
            img = None
        if img is None:
            rgb, full = previews.camera_jpeg(path)
            if rgb is not None and (full or fast):
                img = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    if img is None:
        import hdr
        img, how = hdr._load(path, int(max_dim * 1.05), fast)
        linear = False
    h, w = img.shape[:2]
    # Only shrink when it is really bigger: resampling a 6048 frame to 6000
    # softens every pixel for nothing.
    if max(h, w) > max_dim * 1.05:
        s = max_dim / float(max(h, w))
        img = cv2.resize(img, (int(round(w * s)), int(round(h * s))), interpolation=cv2.INTER_AREA)
    return Frame(img=np.ascontiguousarray(img), linear=linear, how=how)


# --------------------------------------------------------------------------
# the camera's curve (8-bit frames)
# --------------------------------------------------------------------------

def response_curve(frames: Sequence[np.ndarray], bright: np.ndarray) -> np.ndarray:
    """256x3 table: 8-bit value -> linear light, per channel, measured from the
    bracket itself (Debevec). A camera JPEG is not sRGB - Nikon's curve puts
    twice as much light into the top 30 levels - so assuming sRGB made every
    bright wall and lamp land at the wrong brightness in the merge."""
    cv2 = _cv2()
    h, w = frames[0].shape[:2]
    k = min(1.0, 900.0 / max(h, w))
    small = [cv2.resize(f, (max(8, int(w * k)), max(8, int(h * k))), interpolation=cv2.INTER_AREA) for f in frames]
    try:
        crf = cv2.createCalibrateDebevec(samples=300, lambda_=20).process(small, bright.astype(np.float32))
        c = crf[:, 0, :].astype(np.float64)
    except Exception:
        c = None
    srgb = ((np.arange(256) / 255.0 + 0.055) / 1.055) ** 2.4
    srgb[:11] = np.arange(11) / 255.0 / 12.92
    if c is None or not np.all(np.isfinite(c)):
        return np.repeat(srgb[:, None], 3, axis=1).astype(np.float32)
    out = np.zeros((256, 3))
    for ch in range(3):
        v = c[:, ch]
        v = np.convolve(np.pad(v, 3, mode="edge"), np.ones(7) / 7, mode="valid")   # smooth
        v = np.maximum.accumulate(np.maximum(v, 1e-6))                             # never down
        v = v / v[250]
        # the ends of a Debevec curve are guesses (few samples): hold the
        # bottom to the straight line through the first known levels, and
        # let the top keep rising gently
        v[:4] = np.linspace(0, v[4], 5)[:4]
        out[:, ch] = v
    # one grey curve with each channel's own scale - per-channel curves from
    # a few hundred samples tint the highlights
    g = out.mean(axis=1, keepdims=True)
    return (g / g[250]).astype(np.float32).repeat(3, axis=1)


# --------------------------------------------------------------------------
# lining up
# --------------------------------------------------------------------------

def _enc(lin: np.ndarray) -> np.ndarray:
    return np.power(np.clip(lin, 0, 1), 1 / 2.2).astype(np.float32)


def align_to(ref_lin_y: np.ndarray, lin_y: np.ndarray, gain: float, fine: int = 1600) -> Optional[np.ndarray]:
    """3x3 homography that maps ref pixel coords to this frame's, or None.

    Compared on brightness matched to the reference. First matched features
    (fast, and it copes with a big hand-held jump), then refined on every
    pixel that is neither black nor clipped in both (ECC) for sub-pixel
    accuracy - half a pixel off is what makes a merge look soft."""
    cv2 = _cv2()
    h, w = ref_lin_y.shape
    a0 = _enc(ref_lin_y)
    b0 = _enc(lin_y * gain)
    ok0 = ((ref_lin_y > 0.004) & (ref_lin_y < 0.85) & (lin_y * gain > 0.004)
           & (lin_y < 0.85)).astype(np.uint8)

    def scaled(size):
        k = min(1.0, size / float(max(h, w)))
        sw, sh = max(16, int(round(w * k))), max(16, int(round(h * k)))
        return (k, cv2.resize(a0, (sw, sh), interpolation=cv2.INTER_AREA),
                cv2.resize(b0, (sw, sh), interpolation=cv2.INTER_AREA),
                cv2.resize(ok0, (sw, sh), interpolation=cv2.INTER_NEAREST))

    # 1. features
    H = None
    k, a, b, m = scaled(1500)
    try:
        orb = cv2.ORB_create(4000)
        ka, da = orb.detectAndCompute((a * 255).astype(np.uint8), m)
        kb, db = orb.detectAndCompute((b * 255).astype(np.uint8), None)
        if da is not None and db is not None and len(ka) > 30 and len(kb) > 30:
            mt = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(da, db)
            if len(mt) >= 30:
                pa = np.float32([ka[x.queryIdx].pt for x in mt])
                pb = np.float32([kb[x.trainIdx].pt for x in mt])
                Hs, inl = cv2.findHomography(pa, pb, cv2.RANSAC, 1.5)
                if Hs is not None and inl is not None and int(inl.sum()) >= 25:
                    S = np.diag([k, k, 1.0])
                    H = (np.linalg.inv(S) @ Hs @ S).astype(np.float32)
    except cv2.error:
        H = None
    # 2. every pixel, sub-pixel (skipped for the quick preview: features are close enough there)
    if fine <= 0 and H is not None:
        return H
    k, a, b, m = scaled(fine or 700)
    if m.sum() < 500:
        return H
    S = np.diag([k, k, 1.0]).astype(np.float32)
    H0 = H if H is not None else np.eye(3, dtype=np.float32)
    Hs = (S @ H0 @ np.linalg.inv(S)).astype(np.float32)
    Hs /= Hs[2, 2]
    try:
        cc, Hs = cv2.findTransformECC(a, b, Hs, cv2.MOTION_HOMOGRAPHY,
                                      (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT,
                                       12 if H is not None else 40, 1e-5), m, 3)
    except cv2.error:
        return H
    if cc < 0.5:
        return H
    return (np.linalg.inv(S) @ Hs @ S).astype(np.float32)


def local_flow(ref_y: np.ndarray, y: np.ndarray, gain: float, size: int = 2000):
    """Per-pixel shift that lines this (already homography-aligned) frame up
    with the reference, as full-size remap maps - or None.

    A hand-held bracket is not only turned but moved: the chair and the floor
    behind it shift by different amounts (parallax), which no single
    transform of the whole frame can fix. Their edges then disagree, frames
    get left out pixel by pixel there, and edges came out ragged. Dense
    optical flow (DIS) on the brightness-matched frames follows that."""
    cv2 = _cv2()
    h, w = ref_y.shape
    k = min(1.0, size / float(max(h, w)))
    sw, sh = max(16, int(round(w * k))), max(16, int(round(h * k)))
    a = (np.clip(_enc(cv2.resize(ref_y, (sw, sh), interpolation=cv2.INTER_AREA)), 0, 1) * 255).astype(np.uint8)
    b = (np.clip(_enc(cv2.resize(y * gain, (sw, sh), interpolation=cv2.INTER_AREA)), 0, 1) * 255).astype(np.uint8)
    try:
        dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
        flow = dis.calc(a, b, None)
    except cv2.error:
        return None
    # where either frame is blown or black there is nothing to follow: no shift
    ok = ((a > 6) & (a < 245) & (b > 6) & (b < 245)).astype(np.float32)
    okb = cv2.GaussianBlur(ok, (0, 0), 3)
    flow = flow * np.minimum(1.0, okb * 1.5)[..., None]
    flow = cv2.GaussianBlur(flow, (0, 0), 1.5)
    lim = 0.02 * max(sw, sh)
    flow = np.clip(flow, -lim, lim)
    if float(np.abs(flow).max()) < 0.15:
        return None
    flow = cv2.resize(flow, (w, h), interpolation=cv2.INTER_LINEAR) / np.float32(k)
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    return gx + flow[..., 0], gy + flow[..., 1]


def warp(img: np.ndarray, H: np.ndarray) -> np.ndarray:
    cv2 = _cv2()
    h, w = img.shape[:2]
    return cv2.warpPerspective(img, H, (w, h), flags=cv2.INTER_CUBIC | cv2.WARP_INVERSE_MAP,
                               borderMode=cv2.BORDER_REPLICATE)


# --------------------------------------------------------------------------
# merging
# --------------------------------------------------------------------------

def _lum(bgr: np.ndarray) -> np.ndarray:
    return (bgr @ np.float32([0.0722, 0.7152, 0.2126])).astype(np.float32)


def _hat(v: np.ndarray, top: float = 0.93) -> np.ndarray:
    """How much to trust a value of a frame (0..1 of its own range): little
    near black (noise, the camera's curve is guesswork there), nothing near
    clipping. An 8-bit camera JPEG stops being trustworthy sooner: its top
    levels are the camera's highlight roll-off, where the channels clip at
    different points (that put a pink ring round every downlight)."""
    lo = np.clip((v - 0.002) / 0.05, 0, 1)
    hi = np.clip((top - v) / 0.14, 0, 1)
    return (lo * lo * (3 - 2 * lo)) * (hi * hi * (3 - 2 * hi))


def _sharpness(y: np.ndarray, mask: np.ndarray) -> float:
    cv2 = _cv2()
    g = _enc(y)
    lap = cv2.Laplacian(cv2.GaussianBlur(g, (0, 0), 0.7), cv2.CV_32F)
    return float(np.sqrt((lap[mask] ** 2).mean())) if mask.sum() > 1000 else 0.0


def merge(frames: List[Frame], bright: np.ndarray, light: np.ndarray, align: bool = True,
          deghost: bool = True, progress=None, fine: int = 1600) -> np.ndarray:
    """Frames -> linear radiance, BGR float32, lined up on the reference frame."""
    cv2 = _cv2()
    n = len(frames)
    linear = all(f.linear for f in frames)
    if linear:
        lut = None
    else:
        lut = response_curve([f.img if f.img.dtype == np.uint8 else (f.img >> 8).astype(np.uint8)
                              for f in frames], bright)

    def to_lin(f: Frame) -> np.ndarray:
        if f.img.dtype == np.uint16:
            x = f.img.astype(np.float32) / 65535.0
            return x if f.linear else np.power(x, 2.2).astype(np.float32)
        return lut[f.img, np.arange(3)[None, None, :]] if lut is not None else \
            np.power(f.img.astype(np.float32) / 255.0, 2.2)

    def val01(f: Frame) -> np.ndarray:
        """The frame's own 0..1 level (brightest channel), for trust and clipping."""
        x = np.maximum(np.maximum(f.img[..., 0], f.img[..., 1]), f.img[..., 2])
        return x.astype(np.float32) * np.float32(1.0 / (65535.0 if f.img.dtype == np.uint16 else 255.0))

    top = 0.93 if linear else 0.86

    # Reference: the sharpest of the frames that show most of the room.
    # The middle exposure is usually it, but in a hand-held bracket the long
    # frames are shaken - they must never be the one everything lines up to.
    small_k = min(1.0, 1500.0 / max(frames[0].img.shape[:2]))
    cover, sharp = [], []
    for f in frames:
        v = cv2.resize(val01(f), None, fx=small_k, fy=small_k, interpolation=cv2.INTER_AREA)
        cover.append(float(((v > 0.06) & (v < 0.92)).mean()))
    best_cover = max(cover)
    order = np.argsort(bright)
    lins_small = []
    for f, b in zip(frames, bright):
        y = _lum(cv2.resize(to_lin(f), None, fx=small_k, fy=small_k, interpolation=cv2.INTER_AREA))
        lins_small.append(y)
    # compare sharpness where every frame is usable, after matching brightness
    common = np.ones_like(lins_small[0], dtype=bool)
    for y, b in zip(lins_small, bright):
        common &= (y > 0.01) & (y < 0.8)
    if common.sum() < 2000:
        common = np.ones_like(common)
    for y, b in zip(lins_small, bright):
        sharp.append(_sharpness(np.clip(y / b * bright[order[len(order) // 2]], 0, 1), common))
    cand = [i for i in range(n) if cover[i] >= 0.75 * best_cover]
    ref = max(cand, key=lambda i: (sharp[i], -abs(np.log2(bright[i] / np.median(bright)))))
    rel_sharp = np.array([s / max(sharp[ref], 1e-9) for s in sharp])

    ref_lin = to_lin(frames[ref])
    ref_y = _lum(ref_lin)
    ref_v = val01(frames[ref])
    h, w = ref_y.shape

    acc = np.zeros((h, w, 3), np.float32)
    wsum = np.zeros((h, w), np.float32)
    # the reference goes in first, trusted on its own values
    wr = _hat(ref_v, top) * np.float32(light[ref] ** 0.5 * 1.25)
    acc += ref_lin * (wr / np.float32(bright[ref]))[..., None]
    wsum += wr
    # a blurred estimate of the scene so far, to judge the others against
    blur_s = max(0.8, w / 4000.0)
    # Each frame is lined up with the already lined-up frame nearest to it in
    # exposure, not always the reference: two stops apart they share far more
    # detail than four (the reference's blown table top is only readable in
    # the frames either side of it), so edges there line up too.
    done_y = {ref: ref_y}
    dark = int(np.argmin(bright))
    dark_E = ref_lin / np.float32(bright[ref]) if dark == ref else None

    for step, i in enumerate(sorted((k for k in range(n) if k != ref),
                                    key=lambda k: abs(np.log2(bright[k] / bright[ref])))):
        if progress:
            progress(step + 1, n)
        f = frames[i]
        lin = to_lin(f)
        v = val01(f)
        if align:
            t = min(done_y, key=lambda k: abs(np.log2(bright[k] / bright[i])))
            ty, gain = done_y[t], float(bright[t] / bright[i])
            H = align_to(ty, _lum(lin), gain, fine)
            if H is not None and not np.allclose(H, np.eye(3), atol=1e-4):
                lin = warp(lin, H)
                v = warp(v, H)
            if True:                     # the quick preview too: it is cheap at that size
                maps = local_flow(ty, _lum(lin), gain)
                if maps is not None:
                    lin = cv2.remap(lin, maps[0], maps[1], cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
                    # cubic overshoots a little below black at hard edges; a negative light
                    # made the deghost test's log NaN and the whole photo came out black
                    np.maximum(lin, 0, out=lin)
                    v = cv2.remap(v, maps[0], maps[1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
                    del maps
            done_y[i] = _lum(lin)
        wi = _hat(v, top)
        # noise: light gathered; shake: how sharp this frame is against the reference
        wi *= np.float32(light[i] ** 0.5) * np.float32(min(1.0, rel_sharp[i]) ** 1.5)
        E_i = lin / np.float32(bright[i])
        if i == dark:
            dark_E = E_i.copy()          # lined up: the fallback where nothing else is usable
        if deghost:
            est = acc / np.maximum(wsum, 1e-6)[..., None]
            have = wsum > 0.05
            ye = cv2.GaussianBlur(_lum(est), (0, 0), blur_s)
            yi = cv2.GaussianBlur(_lum(E_i), (0, 0), blur_s)
            # noise floor of each, in radiance: one 8-bit level at that exposure
            floor_i = np.float32(0.004 / bright[i])
            floor_e = np.float32(0.004 / bright[ref])
            d = np.abs(np.log2(np.maximum(yi, 0) + floor_i) - np.log2(np.maximum(ye, 0) + floor_e))
            agree = np.exp(-(d / 0.18) ** 2)
            agree = np.where(have, agree, 1.0).astype(np.float32)
            # spread the rejection a little (a moved edge is wider than its
            # outline) and soften it, so a frame fades out instead of
            # switching off pixel by pixel - that made ragged edges
            agree = cv2.erode(agree, np.ones((3, 3), np.uint8))
            agree = cv2.GaussianBlur(agree, (0, 0), 2.0)
            wi *= agree
        acc += E_i * wi[..., None]
        wsum += wi
        del lin, E_i

    # Where no frame is trusted (a lamp itself, a blown edge) the darkest
    # frame takes over - lined up like the rest (this used the frame as shot,
    # which a hand-held bracket had moved: dark lines along bright edges).
    # A faint weight everywhere rather than a hard switch, so no seams.
    if dark_E is None:
        dark_E = to_lin(frames[dark]) / np.float32(bright[dark])
    fb = np.float32(2e-3)
    acc += dark_E * fb
    wsum += fb
    E = acc / wsum[..., None]
    # into the reference frame's brightness scale
    return (E * np.float32(bright[ref])).astype(np.float32)


# --------------------------------------------------------------------------
# tone mapping
# --------------------------------------------------------------------------

def _box(x: np.ndarray, r: int) -> np.ndarray:
    cv2 = _cv2()
    return cv2.boxFilter(x, -1, (2 * r + 1, 2 * r + 1), normalize=True, borderType=cv2.BORDER_REFLECT)


def guided_self(p: np.ndarray, r: int, eps: float, sub: int = 4) -> np.ndarray:
    """Edge-aware smoothing of p guided by itself (He et al., fast version:
    the coefficients at 1/sub size, applied at full size - so edges stay put)."""
    cv2 = _cv2()
    h, w = p.shape
    sw, sh = max(8, w // sub), max(8, h // sub)
    ps = cv2.resize(p, (sw, sh), interpolation=cv2.INTER_AREA)
    rs = max(1, r // sub)
    mean = _box(ps, rs)
    var = _box(ps * ps, rs) - mean * mean
    a = var / (var + eps)
    b = mean - a * mean
    a = _box(a, rs)
    b = _box(b, rs)
    a = cv2.resize(a, (w, h), interpolation=cv2.INTER_LINEAR)
    b = cv2.resize(b, (w, h), interpolation=cv2.INTER_LINEAR)
    return (a * p + b).astype(np.float32)


def _guided(I: np.ndarray, p: np.ndarray, r: int, eps: float) -> np.ndarray:
    """Guided filter of p by guide I (both HxW float32), full size."""
    mI, mp = _box(I, r), _box(p, r)
    cov = _box(I * p, r) - mI * mp
    var = _box(I * I, r) - mI * mI
    a = cov / (var + eps)
    b = mp - a * mI
    return (_box(a, r) * I + _box(b, r)).astype(np.float32)


def tonemap(E: np.ndarray, *, exposure: float = 0.0, shadows: float = 0.15, contrast: float = 0.1,
            saturation: float = 0.05, detail: float = 0.2, warmth: float = 0.0, windows: float = 0.4,
            noise: float = 0.4, bold: bool = False) -> np.ndarray:
    """Linear radiance (BGR) -> finished photo, BGR float 0..1 (sRGB)."""
    cv2 = _cv2()
    h, w = E.shape[:2]
    Y = np.maximum(_lum(E), 1e-7)
    L = np.log2(Y)
    if noise > 0:
        # grain, in stops: the same amount reads as more in the shadows, as
        # it should. Differences smaller than sigma are smoothed, anything
        # bigger (an edge, the weave of a fabric) is left alone.
        sig = 0.05 + 0.13 * float(noise)
        r0 = max(1, int(round(w / 3000)))
        Ls = guided_self(L, r0, eps=sig * sig, sub=1)
        L = L + np.float32(min(1.0, 0.4 + float(noise))) * (Ls - L)
        Y = np.power(2.0, L).astype(np.float32)
    # the large-scale light of the room, with its edges (window frames, walls)
    r = max(4, int(w / 45))
    base = guided_self(L, r, eps=0.35 ** 2)
    base = guided_self(base, r * 2, eps=0.6 ** 2)
    det = L - base

    # how far the base spreads, from the dark corners to the bright windows
    sm = cv2.resize(base, (max(8, w // 8), max(8, h // 8)), interpolation=cv2.INTER_AREA)
    lo, mid, hi = (float(x) for x in np.percentile(sm, [1.0, 50.0, 99.5]))
    # what the finished photo can hold: about 7 stops of base from the darkest
    # corner to the window, fewer when the windows are pulled harder
    room = 7.4 - 2.4 * float(windows) - (0.8 if bold else 0.0)
    c = min(1.0, room / max(1e-3, hi - lo))
    nb = mid + (base - mid) * c
    if shadows:
        # lift the bottom of the base toward the middle, gently
        under = np.clip(mid - nb, 0, None)
        nb = nb + float(shadows) * 0.55 * under * np.exp(-under / 4.0)
    # Windows: what is far brighter than the room (the view outside) is
    # pulled down on its own, on a soft knee, so the view shows with its
    # colour instead of sitting just under white. The room below the knee is
    # not touched at all - squeezing the whole range for the windows is what
    # flattened rooms and still left the windows nearly blown.
    top = mid + (hi - mid) * c
    top0 = top                      # the white point stays where it was: the pulled-down view sits under it
    knee = mid + 2.2
    if windows > 0 and top > knee + 0.25:
        want = knee + 0.4 + (1.0 - float(windows)) * 0.8     # windows 0.4 -> the view about 3 stops over the room
        slope = min(1.0, max(0.15, (want - knee) / (top - knee)))
        over = np.clip(nb - knee, 0, None)
        soft = 0.4
        # slope 1 at the knee, `slope` far above it: no visible step
        nb = nb - over + slope * over + (1.0 - slope) * soft * (1.0 - np.exp(-over / soft))
        top = knee + slope * (top - knee) + (1.0 - slope) * soft * (1.0 - np.exp(-(top - knee) / soft))
    dk = 1.0 + 0.9 * float(detail) + (0.35 if bold else 0.0)
    out_L = nb + det * dk

    # exposure: the middle of the room at a bright, airy grey (estate look);
    # the window (top of the base) sets the white point, and what is brighter
    # still - the lamps themselves - rolls into white on a soft shoulder
    key = 0.19 * 2.0 ** float(exposure)
    Yo = np.power(2.0, out_L - mid).astype(np.float32) * np.float32(key)
    wp = max(1.0, float(2.0 ** (top0 - mid) * key) * 1.25)
    Yd = Yo * (1.0 + Yo / np.float32(wp * wp)) / (1.0 + Yo)
    Yd = np.clip(Yd * np.float32((1.0 + wp) / (wp + 1.0 / wp)), 0, None)

    # colour: the merged colour ratios on the new brightness, cleaned of the
    # speckle the dark frames bring (colour noise follows no edges, real
    # colour follows the brightness edges - the guided filter keeps those)
    ratio = np.clip(E / Y[..., None], 0, 16).astype(np.float32)
    g = _enc(np.clip(Yd, 0, 1))
    rr = max(2, int(w / 1500))
    ratio = np.dstack([_guided(g, ratio[..., k], rr, 0.02 ** 2) for k in range(3)])
    # compressing brightness makes colour read stronger in the lifted
    # shadows; very bright areas fade toward white like film
    sat = (0.95 if not bold else 1.02) + 0.6 * float(saturation)
    ratio = np.power(np.clip(ratio, 1e-4, 16), np.float32(sat))
    ry = np.maximum(_lum(ratio), 1e-6)
    ratio = ratio / ry[..., None]
    # only what is really near white loses its colour (a blue sky in a window keeps it)
    hl = np.clip((Yd - 0.92) / 0.3, 0, 1)[..., None]
    ratio = ratio * (1 - hl) + hl
    rgb = ratio * Yd[..., None]
    if warmth:
        k = float(warmth) * 0.06
        rgb = rgb * np.float32([1 - k, 1.0, 1 + k])
    rgb = np.clip(rgb, 0, None)
    # a channel over white pulls the colour toward white instead of clipping hue
    mx = np.maximum(rgb.max(axis=2, keepdims=True), 1e-6)
    rgb = np.where(mx > 1.0, 1.0 - (1.0 - rgb / mx) * np.clip(2.0 - mx, 0, 1), rgb)
    rgb = np.clip(rgb, 0, 1)
    a = 0.055
    enc = np.where(rgb <= 0.0031308, rgb * 12.92, (1 + a) * np.power(rgb, 1 / 2.4) - a)
    if contrast:
        # contrast in the finished picture: an S round the middle
        x = enc
        enc = x + float(contrast) * 0.9 * (x - 0.5) * (1 - np.abs(2 * x - 1)) ** 1.2
    enc = np.clip(enc, 0, 1).astype(np.float32)
    # colour fringes on hard edges (a drone lens's red line under a white
    # eave): the merge and its local contrast make them stand out, so they
    # are taken off here, the editor's Defringe way (BGR -> RGB and back)
    try:
        import photo_edit
        h_ = enc.shape[0]
        step = max(64, int(3_000_000 / max(1, enc.shape[1])))
        for r0 in range(0, h_, step):          # in bands: no extra gigabyte for a 24 MP merge
            a0, a1 = max(0, r0 - 8), min(h_, r0 + step + 8)
            band = np.ascontiguousarray(enc[a0:a1, :, ::-1])
            y, co, cg = photo_edit._ycocg(band)
            co, cg = photo_edit._defringe_band(y, co, cg, co, cg, 0.8, 1.0)
            fixed = np.clip(photo_edit._from_ycocg(y, co, cg), 0, 1)[..., ::-1]
            n = min(step, h_ - r0)
            enc[r0:r0 + n] = fixed[r0 - a0:r0 - a0 + n]
    except Exception as e:
        print(f"hdr: defringe skipped: {e}", flush=True)
    return np.ascontiguousarray(enc)


def to_uint8(img01: np.ndarray, seed: int = 0) -> np.ndarray:
    """8-bit with a touch of dither, so smooth walls and the glow round a
    light never break into bands."""
    rng = np.random.default_rng(seed)
    noise = rng.random(img01.shape[:2], dtype=np.float32)[..., None]
    return np.clip(np.floor(img01 * 255.0 + noise), 0, 255).astype(np.uint8)
