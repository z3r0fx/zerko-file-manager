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
