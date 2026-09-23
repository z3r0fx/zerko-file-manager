"""Rendering DNGs the way the camera meant them to look.

Phones (Apple ProRAW, and others) store a deliberately dark, linear image and
three instructions for how to brighten it: BaselineExposure (a whole-image
push, +3.5 EV is normal for an iPhone in daylight), ProfileGainTableMap (a
local tone map - how much to lift each part of the frame depending on how
bright it is) and ProfileToneCurve (the final contrast curve). LibRaw applies
none of them, so without this such a photo opens 3 stops too dark and flat.

Only DNGs that carry a gain map or a real baseline push go this way; ordinary
camera RAWs (and drone DNGs with no such tags) render exactly as before.
"""

from __future__ import annotations

import struct
from typing import Optional

import numpy as np

_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}
BASELINE_EXPOSURE, TONE_CURVE, GAIN_TABLE_MAP = 50730, 50940, 52525
MAKE = 271

# Drones and cameras that write DNG. Many of them (DJI / Hasselblad drones in
# particular) also store a BaselineExposure of +0.8..2 EV, but their photos were
# always developed with plain LibRaw here, so they must stay that way or every
# drone edit made before would shift - and frames of one bracket would come out
# differently bright. Only a gain table map (which only phones write) or a push
# from a maker not on this list takes the phone route.
CAMERA_MAKES = ("dji", "hasselblad", "autel", "parrot", "skydio", "yuneec",
                "leica", "pentax", "ricoh", "sigma", "gopro", "insta360",
                "arashi", "sony", "canon", "nikon", "fujifilm", "panasonic",
                "olympus", "om digital")


def dng_tags(path: str, codes=(BASELINE_EXPOSURE, TONE_CURVE, GAIN_TABLE_MAP, MAKE)) -> dict:
    """{code: (type, count, bytes, byte order)} - the first of each wanted tag
    found in any IFD (main, sub-IFDs, EXIF). {} for anything not a TIFF/DNG."""
    out: dict = {}
    try:
        with open(path, "rb") as f:
            data = f.read(96 * 1024 * 1024)
    except OSError:
        return out
    if len(data) < 8 or data[:2] not in (b"II", b"MM"):
        return out
    e = "<" if data[:2] == b"II" else ">"
    u16 = lambda o: struct.unpack_from(e + "H", data, o)[0]  # noqa: E731
    u32 = lambda o: struct.unpack_from(e + "I", data, o)[0]  # noqa: E731
    seen, todo = set(), [u32(4)]
    while todo and len(seen) < 64:
        off = todo.pop()
        if off in seen or off <= 0 or off + 2 > len(data):
            continue
        seen.add(off)
        n = u16(off)
        for i in range(n):
            ent = off + 2 + i * 12
            if ent + 12 > len(data):
                break
            tag, typ, cnt = u16(ent), u16(ent + 2), u32(ent + 4)
            size = _SIZES.get(typ, 1) * cnt
            voff = ent + 8 if size <= 4 else u32(ent + 8)
            if tag in (330, 34665) and typ in (4, 13):
                for k in range(cnt):
                    todo.append(u32(voff + 4 * k) if size > 4 else u32(ent + 8))
            elif tag in codes and tag not in out and voff + size <= len(data):
                out[tag] = (typ, cnt, data[voff:voff + size], e)
        nxt = off + 2 + n * 12
        if nxt + 4 <= len(data) and u32(nxt):
            todo.append(u32(nxt))
    return out


def _numbers(t) -> list:
    typ, cnt, raw, e = t
    fmt = {3: "H", 4: "I", 5: "II", 8: "h", 9: "i", 10: "ii", 11: "f", 12: "d"}.get(typ)
    if not fmt:
        return []
    vals = struct.unpack(e + fmt * cnt, raw)
    if typ in (5, 10):
        return [vals[i] / vals[i + 1] if vals[i + 1] else 0.0 for i in range(0, len(vals), 2)]
    return list(vals)


class Look:
    """What a DNG asks for: exposure push, tone curve, gain table map."""

    def __init__(self, tags: dict):
        be = _numbers(tags[BASELINE_EXPOSURE]) if BASELINE_EXPOSURE in tags else []
        self.baseline = float(be[0]) if be else 0.0
        tc = _numbers(tags[TONE_CURVE]) if TONE_CURVE in tags else []
        self.curve = np.float32(tc).reshape(-1, 2) if len(tc) >= 4 and len(tc) % 2 == 0 else None
        mk = tags.get(MAKE)
        self.make = mk[2].split(b"\0")[0].decode("latin-1").strip().lower() if mk else ""
        self.map = None
        if GAIN_TABLE_MAP in tags:
            try:
                b = tags[GAIN_TABLE_MAP][2]
                V, H = struct.unpack_from(">II", b, 0)          # always big-endian
                sv, sh, ov, oh = struct.unpack_from(">4d", b, 8)
                N, = struct.unpack_from(">I", b, 40)
                w = np.float32(struct.unpack_from(">5f", b, 44))
                need = 64 + V * H * N * 4
                if 1 <= V <= 4096 and 1 <= H <= 4096 and 1 <= N <= 4096 and len(b) >= need:
                    tab = np.frombuffer(b[64:need], ">f4").astype(np.float32)
                    self.map = dict(V=V, H=H, sv=sv, sh=sh, ov=ov, oh=oh, N=N, w=w, tab=tab)
            except struct.error:
                self.map = None

    @property
    def wanted(self) -> bool:
        if self.map is not None:
            return True
        camera = any(self.make.startswith(m) for m in CAMERA_MAKES)
        return self.baseline >= 0.75 and not camera


def look_of(path: str) -> Optional[Look]:
    if not path.lower().endswith(".dng"):
        return None
    tags = dng_tags(path)
    if not tags:
        return None
    lk = Look(tags)
    return lk if lk.wanted else None


def _gain(lin: np.ndarray, m: dict, rows=None) -> np.ndarray:
    """ProfileGainTableMap: per pixel, a gain looked up by where it is in the
    frame (bilinear over the map grid) and how bright it is (linear over the
    table's N points). rows=(first, end, full height) for a band of the frame."""
    bh, w = lin.shape[:2]
    r0, _r1, h = rows if rows else (0, bh, bh)
    V, H, N = m["V"], m["H"], m["N"]
    wt = m["w"]
    v = lin @ wt[:3] + lin.min(axis=2) * wt[3] + lin.max(axis=2) * wt[4]
    idx = np.clip(v, 0.0, 1.0) * np.float32(N - 1)
    i0 = np.minimum(idx.astype(np.int32), N - 2 if N > 1 else 0)
    ti = idx - i0
    fy = np.clip(((np.arange(r0, r0 + bh, dtype=np.float32) + 0.5) / h - m["ov"]) / m["sv"], 0, V - 1) if V > 1 else np.zeros(bh, np.float32)
    fx = np.clip(((np.arange(w, dtype=np.float32) + 0.5) / w - m["oh"]) / m["sh"], 0, H - 1) if H > 1 else np.zeros(w, np.float32)
    y0 = np.minimum(fy.astype(np.int32), max(0, V - 2)); ty = (fy - y0).astype(np.float32)[:, None]
    x0 = np.minimum(fx.astype(np.int32), max(0, H - 2)); tx = (fx - x0).astype(np.float32)[None, :]
    y1 = np.minimum(y0 + 1, V - 1); x1 = np.minimum(x0 + 1, H - 1)
    tab = m["tab"]
    i1 = np.minimum(i0 + 1, N - 1)
    out = np.zeros((bh, w), np.float32)
    for yy, wy in ((y0, 1 - ty), (y1, ty)):
        for xx, wx in ((x0, 1 - tx), (x1, tx)):
            base = (yy[:, None] * H + xx[None, :]) * N
            g = np.take(tab, base + i0) * (1 - ti) + np.take(tab, base + i1) * ti
            out += g * (wy * wx)
    return out


def _srgb(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1 / 2.4) - 0.055).astype(np.float32)


_LUT_N = 16384


def _out_lut(look: Look) -> np.ndarray:
    """Tone curve then sRGB encoding, as one table over linear 0..1."""
    x = np.linspace(0.0, 1.0, _LUT_N, dtype=np.float32)
    if look.curve is not None:
        y = np.interp(x, look.curve[:, 0], look.curve[:, 1]).astype(np.float32)
    else:
        y = x                                            # the shoulder is applied before
    return _srgb(y)


def _rows(fn, n: int, *arrays):
    """fn over horizontal bands in threads (numpy lets go of the GIL)."""
    import os
    from concurrent.futures import ThreadPoolExecutor
    k = max(1, min(8, os.cpu_count() or 1))
    if k == 1 or n < 256:
        return fn(0, n)
    step = (n + k - 1) // k
    with ThreadPoolExecutor(k) as ex:
        list(ex.map(lambda a: fn(a, min(n, a + step)), range(0, n, step)))


def render(raw, look: Look, half: bool = False, max_dim: int = 0) -> np.ndarray:
    """An open rawpy image, developed as the DNG asks. float32 sRGB 0..1,
    oriented like rawpy's normal output, no longer than max_dim if given."""
    flip = raw.sizes.flip                                # read before postprocess resets it
    lin = raw.postprocess(use_camera_wb=True, no_auto_bright=True, gamma=(1, 1),
                          output_bps=16, user_flip=0, half_size=half)
    if max_dim and max(lin.shape[:2]) > max_dim * 1.2:
        # a small copy is all that is wanted: shrink first, it is 10x less work
        import cv2
        h, w = lin.shape[:2]
        s = max_dim / float(max(h, w))
        lin = cv2.resize(lin, (max(1, round(w * s)), max(1, round(h * s))), interpolation=cv2.INTER_AREA)
    h, w = lin.shape[:2]
    out = np.empty((h, w, 3), np.float32)
    push = np.float32(2.0 ** look.baseline / 65535)
    lut = _out_lut(look)
    gain_full = None

    def band(a, b):
        x = lin[a:b].astype(np.float32)
        x *= push
        if look.map is not None:
            x *= gain_full[a:b, :, None]
        if look.curve is None:
            x /= 1.0 + 0.25 * x * x                      # a soft shoulder so the push keeps the sky
        np.clip(x, 0.0, 1.0, out=x)
        x *= np.float32(_LUT_N - 1)
        out[a:b] = np.take(lut, x.astype(np.int32))

    if look.map is not None:
        gain_full = np.empty((h, w), np.float32)

        def gband(a, b):
            x = lin[a:b].astype(np.float32) * push
            gain_full[a:b] = _gain(x, look.map, rows=(a, b, h))
        _rows(gband, h)
    _rows(band, h)
    if flip == 3:
        out = out[::-1, ::-1]
    elif flip == 5:
        out = np.rot90(out, 1)
    elif flip == 6:
        out = np.rot90(out, -1)
    return np.ascontiguousarray(out)
