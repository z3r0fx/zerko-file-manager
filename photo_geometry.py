"""Measuring what is crooked, instead of dragging a slider until it looks right.

Every geometry control in the editor - lens distortion, keystone, straighten,
rotate, crop - was already implemented in the render, and every one of them
was a slider you moved by eye on every single photo. That is backwards for
property work: a wall either is vertical or it is not, and a horizon either is
level or it is not. There is no taste in it, which is exactly why it should be
measured. Tone, which IS taste, was the part that had been automated.

The method is deliberately conservative. A wrong auto-level is far worse than
none, because it silently reframes the shot - so everything here refuses to
answer unless the evidence is strong, and the corrections it will make are
capped well below what the sliders allow.
"""

import math
from typing import Optional

import cv2
import numpy as np

# How far from true a line can be and still count as "meant to be vertical"
# or "meant to be horizontal". Wider than this and it is a roof pitch or a
# staircase, not a wall.
VERTICAL_WINDOW_DEG = 22.0
HORIZONTAL_WINDOW_DEG = 12.0

MAX_STRAIGHTEN_DEG = 6.0      # beyond this it is a deliberate angle
MAX_PERSP_V = 0.35


def _edges(gray: np.ndarray) -> np.ndarray:
    # Blur first: without it every roof tile and leaf becomes an edge and the
    # line detector drowns in texture.
    g = cv2.GaussianBlur(gray, (5, 5), 0)
    med = float(np.median(g))
    lo = int(max(0, 0.66 * med))
    hi = int(min(255, 1.33 * med))
    return cv2.Canny(g, lo, hi, L2gradient=True)


def _lines(gray: np.ndarray):
    h, w = gray.shape[:2]
    edges = _edges(gray)
    # Loose enough to find the mullions, sills and roof lines a building is
    # full of - the first pass only found three or four on most frames, which
    # is not enough evidence to correct anything.
    min_len = int(0.08 * min(h, w))
    segs = cv2.HoughLinesP(edges, 1, np.pi / 360, threshold=40,
                           minLineLength=max(18, min_len), maxLineGap=12)
    if segs is None or len(segs) == 0:
        return []
    # OpenCV 4 hands back (N, 1, 4); OpenCV 5 hands back (N, 4).
    segs = np.asarray(segs)
    segs = segs.reshape(-1, 4)
    out = []
    for x1, y1, x2, y2 in segs:
        dx, dy = float(x2 - x1), float(y2 - y1)
        length = math.hypot(dx, dy)
        if length < 12:
            continue
        # Angle away from straight up, signed: + leans right.
        ang_v = math.degrees(math.atan2(dx, -dy if dy < 0 else dy))
        if ang_v > 90:
            ang_v -= 180
        elif ang_v < -90:
            ang_v += 180
        ang_h = math.degrees(math.atan2(dy, dx))
        if ang_h > 90:
            ang_h -= 180
        elif ang_h < -90:
            ang_h += 180
        out.append((x1, y1, x2, y2, length, ang_v, ang_h))
    return out


def _weighted_angle(vals, weights, window):
    """The dominant angle, by total line length rather than line count.

    One long roofline is better evidence than twenty short window mullions,
    and a plain mean is dragged around by whichever family happens to be more
    numerous.
    """
    if not vals:
        return None, 0.0
    vals = np.asarray(vals, dtype=np.float32)
    weights = np.asarray(weights, dtype=np.float32)
    keep = np.abs(vals) <= window
    if keep.sum() < 3:
        return None, 0.0
    v, wgt = vals[keep], weights[keep]
    # Two passes: median, then re-weight around it to shed outliers.
    centre = float(np.median(v))
    near = np.abs(v - centre) <= max(2.0, window * 0.5)
    if near.sum() < 3:
        return None, 0.0
    v, wgt = v[near], wgt[near]
    angle = float((v * wgt).sum() / max(wgt.sum(), 1e-6))
    # Confidence: how much line length agrees, and how tightly.
    spread = float(np.average(np.abs(v - angle), weights=wgt))
    tightness = max(0.0, 1.0 - spread / max(window * 0.6, 1e-6))
    mass = min(1.0, float(wgt.sum()) / (18.0 * 45.0))
    return angle, float(tightness * mass)


def measure_geometry(rgb01: np.ndarray) -> dict:
    """{straighten, persp_v, confidence, ...} - or empty when unsure.

    rgb01 is the float image the rest of the editor works in.
    """
    img = np.clip(rgb01, 0, 1)
    gray = (img @ np.float32([0.2126, 0.7152, 0.0722]) * 255).astype(np.uint8)
    h, w = gray.shape[:2]
    segs = _lines(gray)
    if len(segs) < 6:
        return {"confidence": 0.0, "lines": len(segs)}

    lengths = [s[4] for s in segs]
    v_ang = [s[5] for s in segs]
    h_ang = [s[6] for s in segs]

    out = {"lines": len(segs)}

    # --- roll: level the horizon / the horizontals -----------------------
    ang_h, conf_h = _weighted_angle(h_ang, lengths, HORIZONTAL_WINDOW_DEG)
    ang_v, conf_v = _weighted_angle(v_ang, lengths, VERTICAL_WINDOW_DEG)

    # The two families measure the same roll in mirrored conventions: a frame
    # rolled left tilts the horizontals one way and the verticals the other by
    # the same amount. Put them in one convention before comparing, or every
    # strong case looks like a disagreement and gets thrown away.
    ang_v_roll = None if ang_v is None else -ang_v

    # Horizontals and verticals should agree about the roll. When both are
    # present and they disagree, something is wrong - so trust neither.
    roll = None
    if ang_h is not None and ang_v_roll is not None:
        if abs(ang_h - ang_v_roll) < 3.5:
            roll = (ang_h * conf_h + ang_v_roll * conf_v) / max(conf_h + conf_v, 1e-6)
            # Two independent families agreeing is the strongest evidence there
            # is, so the confidence is allowed to exceed either one alone.
            conf = min(1.0, max(conf_h, conf_v) * 1.15)
        else:
            roll, conf = ((ang_h, conf_h * 0.6) if conf_h >= conf_v
                          else (ang_v_roll, conf_v * 0.6))
    elif ang_h is not None:
        roll, conf = ang_h, conf_h
    elif ang_v_roll is not None:
        roll, conf = ang_v_roll, conf_v
    else:
        conf = 0.0

    if roll is not None and conf > 0.25 and abs(roll) <= MAX_STRAIGHTEN_DEG:
        # The render rotates by +straighten, so correcting means the opposite.
        out["straighten"] = round(float(-roll), 2)
    out["confidence"] = round(float(conf), 3)

    # --- keystone: make the verticals parallel ---------------------------
    # A camera tilted up makes vertical lines converge at the top. Compare the
    # lean of the verticals on the left of frame with those on the right: in a
    # tilted shot they lean towards each other, and the size of that
    # difference is the amount of keystone.
    left, right = [], []
    for x1, y1, x2, y2, length, av, ah in segs:
        if abs(av) > VERTICAL_WINDOW_DEG:
            continue
        cx = (x1 + x2) / 2.0
        if cx < w * 0.4:
            left.append((av, length))
        elif cx > w * 0.6:
            right.append((av, length))

    if len(left) >= 3 and len(right) >= 3:
        la, lc = _weighted_angle([a for a, _ in left], [l for _, l in left],
                                 VERTICAL_WINDOW_DEG)
        ra, rc = _weighted_angle([a for a, _ in right], [l for _, l in right],
                                 VERTICAL_WINDOW_DEG)
        if la is not None and ra is not None:
            converge = float(ra - la)      # + = tops lean together
            k_conf = min(lc, rc)
            if k_conf > 0.25 and abs(converge) > 1.2:
                amount = max(-MAX_PERSP_V, min(MAX_PERSP_V, converge / 26.0))
                out["persp_v"] = round(float(amount), 3)
                out["persp_confidence"] = round(float(k_conf), 3)
    return out


def cover_scale(straighten: float = 0.0, persp_v: float = 0.0) -> float:
    """How far to zoom so the corrections do not leave empty corners.

    Rotating or keystoning pulls the image away from the frame edge; without
    this the result has transparent wedges in the corners.
    """
    s = 1.0
    if straighten:
        t = math.radians(abs(float(straighten)))
        s = max(s, math.cos(t) + math.sin(t))
    if persp_v:
        s = max(s, 1.0 + abs(float(persp_v)) * 0.45)
    return round(min(1.6, s), 4)


# --------------------------------------------------------------------------
# Upright: solve for straighten and keystone with the renderer's own mapping
# --------------------------------------------------------------------------
#
# measure_geometry() above guesses from angles in the source. Upright does it
# the exact way: take the line segments once, push their end points through
# the same projective mapping the render uses (with candidate straighten /
# keystone values) and pick the values that leave the verticals vertical and,
# for "full", the horizontals level. Lines stay lines under that mapping, so
# a segment's angle in the result is the angle between its mapped ends.

def _to_output(a, b, straighten, persp_v, persp_h, distortion, rotate):
    """Source (centred, x in aspect units) -> output (centred), the inverse
    of photo_edit.source_uv without crop, scale and flips."""
    # undo the lens: source q = p * (1 + k|p|^2)
    if distortion:
        px, py = a.copy(), b.copy()
        for _ in range(6):
            f = 1.0 + distortion * (px * px + py * py)
            px, py = a / f, b / f
        a, b = px, py
    d = 1.0 - persp_v * b - persp_h * a
    d = np.where(np.abs(d) < 1e-3, 1e-3, d)
    xs, ys = a / d, b / d
    t = -math.radians(straighten)
    cs, sn = math.cos(t), math.sin(t)
    x, y = xs * cs - ys * sn, xs * sn + ys * cs
    if rotate:
        t = -math.radians(rotate)
        cs, sn = math.cos(t), math.sin(t)
        x, y = x * cs - y * sn, x * sn + y * cs
    return x, y


def upright(rgb01: np.ndarray, mode: str, current: dict) -> dict:
    """mode: level (roll only), vertical (roll + vertical keystone), full
    (roll + both keystones). Returns {straighten, persp_v, persp_h, lines,
    verticals, horizontals} or {"error": ...} when there is nothing to go on."""
    img = np.clip(rgb01, 0, 1)
    gray = (img @ np.float32([0.2126, 0.7152, 0.0722]) * 255).astype(np.uint8)
    h, w = gray.shape[:2]
    segs = _lines(gray)
    asp = w / float(h)
    dist = float(current.get("distortion", 0) or 0)
    rot = float(current.get("rotate", 0) or 0)
    # in the photo as it stands (lens corrected, not yet straightened), which
    # family is each line meant to be in?
    if not segs:
        return {"error": "No straight lines in this photo to line up."}
    S = np.array([s[:5] for s in segs], np.float64)
    ax, ay = (S[:, 0] / w - 0.5) * asp, S[:, 1] / h - 0.5
    bx, by = (S[:, 2] / w - 0.5) * asp, S[:, 3] / h - 0.5
    ln = S[:, 4]
    cx0, cy0 = _to_output(ax, ay, 0, 0, 0, dist, rot)
    cx1, cy1 = _to_output(bx, by, 0, 0, 0, dist, rot)
    ang = np.degrees(np.arctan2(cy1 - cy0, cx1 - cx0))
    from_h = np.abs(((ang + 90) % 180) - 90)       # 0 = horizontal
    from_v = 90 - from_h                            # 0 = vertical
    vert = from_v < 25
    horz = from_h < 15
    if vert.sum() < 2 and horz.sum() < 2:
        return {"error": "Not enough straight edges to go on - use the sliders."}
    if mode != "level" and vert.sum() < 2:
        return {"error": "No vertical lines to make upright - try Level instead."}
    wv = ln[vert] / max(1.0, ln[vert].sum()) if vert.any() else None
    wh = ln[horz] / max(1.0, ln[horz].sum()) if horz.any() else None

    def cost(th, pv, ph):
        th = np.asarray(th, np.float64)[..., None]
        pv = np.asarray(pv, np.float64)[..., None]
        ph = np.asarray(ph, np.float64)[..., None]
        total = 0.0

        def angles(sel):
            a0x, a0y, a1x, a1y = ax[sel], ay[sel], bx[sel], by[sel]
            if dist:
                a0x, a0y = _undist(a0x, a0y, dist)
                a1x, a1y = _undist(a1x, a1y, dist)
            out = []
            for (px, py) in ((a0x, a0y), (a1x, a1y)):
                d = 1.0 - pv * py - ph * px
                d = np.where(np.abs(d) < 1e-3, 1e-3, d)
                xs, ys = px / d, py / d
                t = -np.radians(th)
                x, y = xs * np.cos(t) - ys * np.sin(t), xs * np.sin(t) + ys * np.cos(t)
                if rot:
                    r_ = -math.radians(rot)
                    x, y = x * math.cos(r_) - y * math.sin(r_), x * math.sin(r_) + y * math.cos(r_)
                out.append((x, y))
            (x0, y0), (x1, y1) = out
            return np.degrees(np.arctan2(y1 - y0, x1 - x0))

        if wv is not None:
            a = angles(vert)
            dv = np.abs(((a) % 180) - 90)           # 0 = vertical
            total = total + (np.minimum(dv, 8.0) ** 2 * wv).sum(-1)
        if wh is not None and mode != "vertical":
            a = angles(horz)
            dh = np.abs(((a + 90) % 180) - 90)
            total = total + (np.minimum(dh, 6.0) ** 2 * wh).sum(-1) * (1.0 if mode == "full" else 0.7)
        elif wh is not None:
            # vertical mode: level horizontals only help with the roll, gently
            a = angles(horz)
            dh = np.abs(((a + 90) % 180) - 90)
            total = total + (np.minimum(dh, 6.0) ** 2 * wh).sum(-1) * 0.15
        # prefer the smallest correction that does the job
        return total + 0.4 * (pv[..., 0] ** 2 + ph[..., 0] ** 2) * 10 + 0.002 * th[..., 0] ** 2

    pv_now = float(current.get("persp_v", 0) or 0)
    ph_now = float(current.get("persp_h", 0) or 0)
    th_grid = np.arange(-12, 12.01, 0.5)
    pv_grid = np.arange(-0.9, 0.901, 0.05) if mode != "level" else np.array([pv_now])
    ph_grid = np.arange(-0.6, 0.601, 0.05) if mode == "full" else np.array([0.0 if mode == "vertical" else ph_now])
    T, PV, PH = np.meshgrid(th_grid, pv_grid, ph_grid, indexing="ij")
    c = cost(T.ravel(), PV.ravel(), PH.ravel())
    i = int(np.argmin(c))
    best = [T.ravel()[i], PV.ravel()[i], PH.ravel()[i]]
    # refine
    for step in (0.25, 0.1, 0.04):
        tg = best[0] + np.arange(-2, 2.01, 1) * step * 2
        pg = best[1] + np.arange(-2, 2.01, 1) * step * 0.2 if mode != "level" else np.array([best[1]])
        hg = best[2] + np.arange(-2, 2.01, 1) * step * 0.2 if mode == "full" else np.array([best[2]])
        T, PV, PH = np.meshgrid(tg, pg, hg, indexing="ij")
        c = cost(T.ravel(), PV.ravel(), PH.ravel())
        i = int(np.argmin(c))
        best = [T.ravel()[i], PV.ravel()[i], PH.ravel()[i]]
    return {
        "straighten": round(float(np.clip(best[0], -45, 45)), 2),
        "persp_v": round(float(np.clip(best[1], -1, 1)), 3),
        "persp_h": round(float(np.clip(best[2], -1, 1)), 3),
        "lines": len(segs),
        "verticals": int(vert.sum()),
        "horizontals": int(horz.sum()),
    }


def _undist(a, b, k):
    px, py = a.copy(), b.copy()
    for _ in range(6):
        f = 1.0 + k * (px * px + py * py)
        px, py = a / f, b / f
    return px, py


# --------------------------------------------------------------------------
# Lens: the maker's own correction data from a DNG, or measured from lines
# --------------------------------------------------------------------------

import struct


def _dng_opcodes(path: str) -> dict:
    """WarpRectilinear and FixVignetteRadial from a DNG's opcode lists - the
    correction the camera maker wrote into the file. {} when there is none."""
    out = {}
    try:
        with open(path, "rb") as f:
            data = f.read(64 * 1024 * 1024)
    except OSError:
        return out
    if len(data) < 8 or data[:2] not in (b"II", b"MM"):
        return out
    e = "<" if data[:2] == b"II" else ">"

    def u16(o):
        return struct.unpack_from(e + "H", data, o)[0]

    def u32(o):
        return struct.unpack_from(e + "I", data, o)[0]

    seen, todo, lists = set(), [u32(4)], []
    sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}
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
            size = sizes.get(typ, 1) * cnt
            voff = ent + 8 if size <= 4 else u32(ent + 8)
            if tag in (330, 34665) and typ in (4, 13):          # SubIFDs, EXIF IFD
                for k in range(cnt):
                    todo.append(u32(voff + 4 * k) if cnt > 1 or size > 4 else u32(ent + 8))
            elif tag in (51008, 51009, 51022):                     # OpcodeList1/2/3
                lists.append(data[voff:voff + size])
        nxt = off + 2 + n * 12
        if nxt + 4 <= len(data) and u32(nxt):
            todo.append(u32(nxt))
    for blob in lists:
        try:
            count = struct.unpack_from(">I", blob, 0)[0]
            p = 4
            for _ in range(count):
                oid, _ver, _flags, size = struct.unpack_from(">IIII", blob, p)
                body = blob[p + 16:p + 16 + size]
                p += 16 + size
                if oid == 1 and "warp" not in out:                 # WarpRectilinear
                    planes = struct.unpack_from(">I", body, 0)[0]
                    kr = struct.unpack_from(">4d", body, 4)
                    cx, cy = struct.unpack_from(">2d", body, 4 + planes * 48)
                    out["warp"] = {"kr": list(kr), "center": [cx, cy]}
                elif oid == 3 and "vignette" not in out:           # FixVignetteRadial
                    k = struct.unpack_from(">5d", body, 0)
                    cx, cy = struct.unpack_from(">2d", body, 40)
                    out["vignette"] = {"k": list(k), "center": [cx, cy]}
        except Exception:
            continue
    return out


def distortion_from_warp(kr, aspect: float) -> float:
    """The editor's single lens coefficient that best matches a DNG warp.
    DNG: source radius = r * (kr0 + kr1 r^2 + kr2 r^4 + kr3 r^6), r = 1 at the
    corner. Editor: source = p * (1 + k |p|^2), |p| = R at the corner."""
    R2 = (aspect / 2.0) ** 2 + 0.25
    rs = np.linspace(0.05, 1.0, 40)
    g = kr[0] + kr[1] * rs ** 2 + kr[2] * rs ** 4 + kr[3] * rs ** 6
    g = g / kr[0] - 1.0          # the overall scale is left to "fill the frame"
    k = float((g * rs ** 2).sum() / max(1e-9, (R2 * rs ** 4).sum()))
    return round(max(-0.5, min(0.5, k)), 4)


def measure_distortion(rgb01: np.ndarray) -> Optional[float]:
    """Plumb-line: the lens coefficient that makes the photo's long edges
    straightest. None when there are too few edges to say."""
    img = np.clip(rgb01, 0, 1)
    gray = (img @ np.float32([0.2126, 0.7152, 0.0722]) * 255).astype(np.uint8)
    h, w = gray.shape[:2]
    k_ = 800.0 / max(h, w)
    if k_ < 1:
        gray = cv2.resize(gray, (int(w * k_), int(h * k_)), interpolation=cv2.INTER_AREA)
        h, w = gray.shape[:2]
    edges = _edges(gray)
    asp = w / float(h)
    xs = (np.arange(w, dtype=np.float32) + 0.5) / w
    ys = (np.arange(h, dtype=np.float32) + 0.5) / h
    gx, gy = np.meshgrid((xs - 0.5) * asp, ys - 0.5)
    r2 = gx * gx + gy * gy
    min_len = int(0.12 * min(h, w))

    # Straight edges pile up on single columns (verticals) and rows
    # (horizontals); curved ones smear across several. The peakiness of the
    # column and row profiles of the corrected edge map is the straightness.
    def score(k):
        f = 1.0 + k * r2
        mx = ((gx * f) / asp + 0.5) * w - 0.5
        my = ((gy * f) + 0.5) * h - 0.5
        e = cv2.remap(edges, mx.astype(np.float32), my.astype(np.float32), cv2.INTER_NEAREST).astype(np.float32)
        col = e.sum(axis=0)
        row = e.sum(axis=1)
        return float((col ** 2).sum() + (row ** 2).sum())

    base = score(0.0)
    if (edges > 0).sum() < 4 * min_len:
        return None
    ks = np.arange(-0.3, 0.301, 0.02)
    sc = [score(float(k)) for k in ks]
    best = float(ks[int(np.argmax(sc))])
    for step in (0.005, 0.002):
        cand = [best + d * step for d in (-2, -1, 0, 1, 2)]
        sc2 = [score(c) for c in cand]
        best = cand[int(np.argmax(sc2))]
    # only speak up when straightening really helps
    if score(best) < base * 1.05:
        return 0.0
    return round(max(-0.5, min(0.5, best)), 4)
