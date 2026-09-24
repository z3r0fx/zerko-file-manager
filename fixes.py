"""Property fixes: the per-photo jobs editing services charge for (BoxBrownie,
PhotoUp), done here.

  greener grass   - the lawn traced, its dry and patchy colour turned to a healthy green
  clean pool      - the water traced, a green or murky pool turned clear blue
  screens         - TVs and monitors traced, filled black (or with one of your photos)
  fire            - a fire lit in the fireplace (the image model, only inside the firebox)
  mixed light     - the orange cast lamps throw on walls next to daylight, or an LED strip's purple, taken out locally

Claude finds where each thing is (one question per photo for all of them);
Segment Anything traces it. Each result is a patch over just that area (or,
for mixed light, a painted mask with cooler light) - identical in the preview
and the export, and taken off again with one click. The photo itself is never
changed. Cars and people are never touched.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import List, Optional

import numpy as np
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import ai
import ai_photo
import photo_edit as pe
from auth import get_current_user
from database import User, get_db

router = APIRouter(prefix="/api/ai/fix", tags=["ai"])
import contextvars
_PATH: contextvars.ContextVar = contextvars.ContextVar("fix_path", default="")

KINDS = ("grass", "pool", "screen", "fire")

FIND_ASK = (
    "This is a property photo for a listing. Find these, if they are in it, as tight boxes in 0..1 of the picture "
    "(x0, y0 top left, x1, y1 bottom right):\n"
    "- lawns: every area of grass lawn (not trees, hedges, plants in beds, or artificial turf on a sports court)\n"
    "- pool: the water surface of a swimming pool or spa (only the water)\n"
    "- screens: the glass of each TV, computer monitor or other display screen (only the screen, not the frame or stand)\n"
    "- fireplace: the opening of a fireplace or fire box where a fire burns (only if the photo shows one, at most one)\n"
    "NEVER include cars, vehicles or people. Answer as "
    '{"lawns":[[x0,y0,x1,y1]], "pool":[[...]], "screens":[[...]], "fireplace":[[...]]} with empty lists for what is not there.'
)


def _boxes(raw) -> List[List[float]]:
    out = []
    for b in raw if isinstance(raw, list) else []:
        try:
            x0, y0, x1, y1 = (float(t) for t in b[:4])
        except Exception:
            continue
        x0, x1 = sorted((min(1.0, max(0.0, x0)), min(1.0, max(0.0, x1))))
        y0, y1 = sorted((min(1.0, max(0.0, y0)), min(1.0, max(0.0, y1))))
        if (x1 - x0) * (y1 - y0) > 1e-4:
            out.append([x0, y0, x1, y1])
    return out[:8]


def find(vid: int, path: str) -> dict:
    """{"grass": [boxes], "pool": [...], "screen": [...], "fire": [...]} as Claude sees the photo."""
    rgb = ai_photo.base_rgb(vid, path, 1600)
    d = ai.ask_json(FIND_ASK + ai.GRID_NOTE, [ai.grid_b64(rgb)], "", max_tokens=1500, temperature=0.0)
    d = d if isinstance(d, dict) else {}
    return {"grass": _boxes(d.get("lawns")), "pool": _boxes(d.get("pool")), "screen": _boxes(d.get("screens")),
            "fire": _boxes(d.get("fireplace"))[:1]}


# --------------------------------------------------------------------------
# tracing, and the patch over the area
# --------------------------------------------------------------------------

def _trace(path: str, boxes: List[List[float]]) -> np.ndarray:
    """What is inside the boxes, traced (0..1, work size)."""
    rgb = ai_photo._work_rgb(path)
    h, w = rgb.shape[:2]
    m = np.zeros((h, w), np.float32)
    for b in boxes:
        t = ai_photo.box_mask(path, rgb, b).astype(np.float32)
        if t.max() > 1.5:
            t /= 255.0
        keep = np.zeros_like(t)
        x0, y0, x1, y1 = int(b[0] * w), int(b[1] * h), int(np.ceil(b[2] * w)), int(np.ceil(b[3] * h))
        keep[y0:y1, x0:x1] = 1
        part = t * keep
        if part.sum() < 0.05 * max(1, (x1 - x0) * (y1 - y0)):
            part = keep          # the trace missed: the box itself
        m = np.maximum(m, part)
    return m


def _full_region(path: str, m_work: np.ndarray, pad: float = 0.01):
    """The full-size pixels round the traced area, and the trace at that size (feathered)."""
    import cv2
    full = pe._full_u8(path)
    H, W = full.shape[:2]
    ys, xs = np.nonzero(m_work > 0.3)
    if not len(xs):
        return None
    h, w = m_work.shape
    g = int(max(W, H) * pad) + 4
    X0, X1 = max(0, int(xs.min() * W / w) - g), min(W, int((xs.max() + 1) * W / w) + g)
    Y0, Y1 = max(0, int(ys.min() * H / h) - g), min(H, int((ys.max() + 1) * H / h) + g)
    big = cv2.resize(m_work, (W, H), interpolation=cv2.INTER_LINEAR)[Y0:Y1, X0:X1]
    feather = max(1.5, 0.002 * max(W, H))
    big = cv2.GaussianBlur(big, (0, 0), feather)
    return full[Y0:Y1, X0:X1].astype(np.float32) / 255.0, np.clip(big, 0, 1), (X0, Y0, X1, Y1, W, H)


def _patch(vid: int, rgb: np.ndarray, alpha: np.ndarray, where, kind: str) -> dict:
    X0, Y0, X1, Y1, W, H = where
    rgba = np.dstack([np.clip(rgb, 0, 1), np.clip(alpha, 0, 1)])
    return ai_photo._save_patch(vid, rgba, X0, Y0, X1, Y1, W, H, kind)


def _recolour(rgb: np.ndarray, weight: np.ndarray, target_hue: float, hue_lo: float, hue_hi: float,
              keep: float, sat_gain: float, sat_add: float, lift: float, strength: float) -> np.ndarray:
    """Turn the colours inside `weight` toward `target_hue` (degrees), keeping some of their own variation
    so the texture of the grass or the water stays; only pixels whose hue is between hue_lo and hue_hi."""
    import cv2
    hsv = cv2.cvtColor(rgb.astype(np.float32), cv2.COLOR_RGB2HSV)      # H 0..360, S 0..1, V 0..1
    hh, ss, vv = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    inrange = ((hh >= hue_lo) & (hh <= hue_hi)).astype(np.float32)
    inrange = cv2.GaussianBlur(inrange, (0, 0), 1.2)
    lit = np.clip((vv - 0.04) / 0.08, 0, 1)                             # not the deep shadows
    w = weight * inrange * lit * strength
    sel = w > 0.2
    mean = float(np.median(hh[sel])) if sel.any() else target_hue
    new_h = (target_hue + (hh - mean) * keep) % 360
    new_s = np.clip(ss * sat_gain + sat_add, 0, 0.9)
    new_v = np.clip(vv * (1 + lift), 0, 1)
    out = cv2.cvtColor(np.dstack([new_h, new_s, new_v]).astype(np.float32), cv2.COLOR_HSV2RGB)
    return rgb * (1 - w[..., None]) + out * w[..., None], w


def fix_grass(vid: int, path: str, boxes, strength: float = 1.0) -> Optional[dict]:
    m = _trace(path, boxes)
    got = _full_region(path, m)
    if not got:
        return None
    rgb, a, where = got
    # dry yellow-brown to healthy lawn green; already green grass only gets a little richer
    out, w = _recolour(rgb, a, 98.0, 20.0, 165.0, keep=0.35, sat_gain=1.2, sat_add=0.06, lift=0.03, strength=strength)
    return _patch(vid, out, a, where, "grass")


def fix_pool(vid: int, path: str, boxes, strength: float = 1.0) -> Optional[dict]:
    m = _trace(path, boxes)
    got = _full_region(path, m)
    if not got:
        return None
    rgb, a, where = got
    # green or murky water to clear aqua blue; the reflections and ripples keep their light
    out, w = _recolour(rgb, a, 188.0, 50.0, 250.0, keep=0.3, sat_gain=1.15, sat_add=0.05, lift=0.04, strength=strength)
    return _patch(vid, out, a, where, "pool")


def _quad(mask: np.ndarray) -> Optional[np.ndarray]:
    """The screen's four corners (x, y), from its trace."""
    import cv2
    mk = (mask > 0.5).astype(np.uint8)
    cs, _ = cv2.findContours(mk, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cs:
        return None
    c = max(cs, key=cv2.contourArea)
    peri = cv2.arcLength(c, True)
    for eps in (0.02, 0.04, 0.06):
        ap = cv2.approxPolyDP(c, eps * peri, True)
        if len(ap) == 4 and cv2.isContourConvex(ap):
            q = ap.reshape(4, 2).astype(np.float32)
            break
    else:
        q = cv2.boxPoints(cv2.minAreaRect(c)).astype(np.float32)
    # order: top-left, top-right, bottom-right, bottom-left
    s, d = q.sum(1), np.diff(q, axis=1).ravel()
    return np.array([q[np.argmin(s)], q[np.argmin(d)], q[np.argmax(s)], q[np.argmax(d)]], np.float32)


CORNERS_ASK = ("This is a close crop of a property photo with a grid drawn over it: the magenta lines are at 0.1, 0.2 ... 0.9 "
               "of the width (numbers along the top) and of the height (numbers down the left). Find the TV, computer monitor or "
               "other display screen. Give the four corners of its glass (not the frame, the stand or the wall) as fractions of "
               "the crop, read off the grid as exactly as you can (to 0.01), in order: top left, top right, bottom right, bottom "
               "left. It may be seen at an angle, so the corners need not make a rectangle. "
               'Answer as {"corners":[[x,y],[x,y],[x,y],[x,y]]}, or {"corners":[]} if there is no screen.')


def _ask_corners(rgb: np.ndarray, X0: int, Y0: int, X1: int, Y1: int) -> Optional[np.ndarray]:
    """Claude reads the screen's corners off a grid drawn over this part of the photo (work pixels back)."""
    from PIL import Image, ImageDraw
    crop = Image.fromarray((np.clip(rgb[Y0:Y1, X0:X1], 0, 1) * 255).astype(np.uint8))
    crop = crop.resize((1000, max(1, int(1000 * (Y1 - Y0) / max(1, X1 - X0)))), Image.LANCZOS)
    d = ImageDraw.Draw(crop)
    for i in range(1, 10):
        x, y = int(crop.width * i / 10), int(crop.height * i / 10)
        d.line([(x, 0), (x, crop.height)], fill=(255, 0, 255), width=1)
        d.line([(0, y), (crop.width, y)], fill=(255, 0, 255), width=1)
        d.text((x + 3, 3), f"{i / 10:.1f}", fill=(255, 0, 255))
        d.text((3, y + 3), f"{i / 10:.1f}", fill=(255, 0, 255))
    try:
        r = ai.ask_json(CORNERS_ASK, [ai.jpeg_b64(np.asarray(crop), 1000, 90)], "", max_tokens=300, temperature=0.0)
        c = np.array((r or {}).get("corners") or [], np.float32).reshape(-1, 2)
    except Exception:
        return None
    if c.shape != (4, 2) or not np.all((c >= -0.05) & (c <= 1.05)):
        return None
    return (c * [X1 - X0, Y1 - Y0] + [X0, Y0]).astype(np.float32)


def _screen_corners(rgb: np.ndarray, box) -> Optional[np.ndarray]:
    """The screen's four corners: a first look round Claude's box (which is often a little off), then a
    second, closer look centred on what the first found."""
    import cv2
    h, w = rgb.shape[:2]

    def square(cx, cy, side):
        return (int(max(0, cx - side / 2)), int(max(0, cy - side / 2)), int(min(w, cx + side / 2)), int(min(h, cy + side / 2)))

    x0, y0, x1, y1 = box[0] * w, box[1] * h, box[2] * w, box[3] * h
    q = _ask_corners(rgb, *square((x0 + x1) / 2, (y0 + y1) / 2, max(x1 - x0, y1 - y0) * 3.0))
    if q is None:
        return None
    qx0, qy0 = q.min(0)
    qx1, qy1 = q.max(0)
    q2 = _ask_corners(rgb, *square((qx0 + qx1) / 2, (qy0 + qy1) / 2, max(qx1 - qx0, qy1 - qy0) * 1.5))
    if q2 is not None and 0.6 < cv2.contourArea(q2) / max(1.0, cv2.contourArea(q)) < 1.6:
        q = q2
    if cv2.contourArea(q) < 0.1 * (x1 - x0) * (y1 - y0):
        return None
    return q


def _fit_quad_lines(rgb: np.ndarray, q0: np.ndarray) -> np.ndarray:
    """The screen's outline from the straight edges round a rough quad: for each side the strongest long edge near
    it (or the rough side itself), and the four that together run along the most edge win. Claude's corners get the
    slope of an angled TV's top and bottom wrong by more than a snap can fix. Returns q0 when nothing better is found."""
    import cv2
    h, w = rgb.shape[:2]
    cx, cy = q0.mean(0)
    bw = float(q0[:, 0].max() - q0[:, 0].min())
    bh = float(q0[:, 1].max() - q0[:, 1].min())
    if bw < 12 or bh < 12:
        return q0
    X0, Y0 = int(max(0, q0[:, 0].min() - 0.3 * bw)), int(max(0, q0[:, 1].min() - 0.3 * bh))
    X1, Y1 = int(min(w, q0[:, 0].max() + 0.3 * bw)), int(min(h, q0[:, 1].max() + 0.3 * bh))
    g = cv2.cvtColor((np.clip(rgb[Y0:Y1, X0:X1], 0, 1) * 255).astype(np.uint8), cv2.COLOR_RGB2GRAY)
    g = cv2.GaussianBlur(g, (0, 0), 1.0)
    gx, gy = cv2.Sobel(g.astype(np.float32), cv2.CV_32F, 1, 0), cv2.Sobel(g.astype(np.float32), cv2.CV_32F, 0, 1)
    med = float(np.median(g))
    edges = cv2.Canny(g, max(10, 0.33 * med), max(30, 0.9 * med))
    segs = cv2.HoughLinesP(edges, 1, np.pi / 360, 20, minLineLength=int(0.25 * min(bw, bh)), maxLineGap=int(0.04 * max(bw, bh)) + 2)
    segs = [] if segs is None else [s[0].astype(np.float32) + [X0, Y0, X0, Y0] for s in segs]
    # the rough sides: 0 top (p0-p1), 1 right (p1-p2), 2 bottom (p2-p3), 3 left (p3-p0)
    sides = [(q0[i], q0[(i + 1) % 4]) for i in range(4)]
    cands = []
    for k, (a, b) in enumerate(sides):
        horiz = k in (0, 2)
        mid = (a + b) / 2
        opts = [(a.astype(np.float32), b.astype(np.float32), 0.0)]
        found = []
        for sg in segs:
            p, r_ = sg[:2], sg[2:]
            d = r_ - p
            ang = abs(np.degrees(np.arctan2(d[1], d[0]))) % 180
            if horiz and not (ang < 35 or ang > 145):
                continue
            if not horiz and not (55 < ang < 125):
                continue
            n = np.array([-d[1], d[0]]) / (np.linalg.norm(d) + 1e-6)
            off = abs(float(np.dot(mid - p, n)))           # how far the rough side's middle is from this line
            if off > (0.25 * bh if horiz else 0.25 * bw):
                continue
            found.append((p, r_, float(np.linalg.norm(d))))
        found.sort(key=lambda t: -t[2])
        opts += [(p, r_, L) for p, r_, L in found[:5]]
        cands.append(opts)

    def meet(l1, l2):
        (p1, r1), (p2, r2) = l1, l2
        d1, d2 = r1 - p1, r2 - p2
        m = np.array([[d1[0], -d2[0]], [d1[1], -d2[1]]], np.float64)
        if abs(np.linalg.det(m)) < 1e-6:
            return None
        t = np.linalg.solve(m, (p2 - p1).astype(np.float64))[0]
        return (p1 + d1 * t).astype(np.float32)

    def support(a, b):
        # how much the photo has an edge along a - b (gradient across the line, sampled)
        d = b - a
        L = float(np.linalg.norm(d))
        if L < 4:
            return 0.0
        n = np.array([-d[1], d[0]]) / L
        ts = np.linspace(0.08, 0.92, 40)
        pts = a[None, :] + d[None, :] * ts[:, None] - [X0, Y0]
        xi = np.clip(np.round(pts[:, 0]).astype(int), 0, g.shape[1] - 1)
        yi = np.clip(np.round(pts[:, 1]).astype(int), 0, g.shape[0] - 1)
        return float(np.mean(np.abs(gx[yi, xi] * n[0] + gy[yi, xi] * n[1])))

    a0 = cv2.contourArea(q0.astype(np.float32))
    best, best_s = q0, sum(support(a, b) for a, b in sides)
    import itertools
    for combo in itertools.product(*cands):
        lines = [(c[0], c[1]) for c in combo]
        pts = [meet(lines[3], lines[0]), meet(lines[0], lines[1]), meet(lines[1], lines[2]), meet(lines[2], lines[3])]
        if any(p is None for p in pts):
            continue
        q = np.array(pts, np.float32)
        if not cv2.isContourConvex(q.reshape(-1, 1, 2)) or np.abs(q - q0).max() > 0.35 * max(bw, bh):
            continue
        ar = cv2.contourArea(q)
        if not 0.6 * a0 < ar < 1.6 * a0:
            continue
        sc = sum(support(q[i], q[(i + 1) % 4]) for i in range(4))
        if sc > best_s * 1.05:
            best, best_s = q, sc
    return best


def _snap_quad(rgb: np.ndarray, q: np.ndarray) -> np.ndarray:
    """Move each side of a rough quad onto the straight edge next to it (the screen's own edge), then
    take the corners where those lines meet. Keeps the rough quad when the result does not make sense."""
    import cv2
    g = cv2.GaussianBlur(cv2.cvtColor((np.clip(rgb, 0, 1) * 255).astype(np.uint8), cv2.COLOR_RGB2GRAY).astype(np.float32), (0, 0), 1.0)
    gx, gy = cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1)
    h, w = g.shape
    lines = []
    for i in range(4):
        a, b = q[i], q[(i + 1) % 4]
        L = float(np.linalg.norm(b - a))
        if L < 8:
            return q
        t = (b - a) / L
        n = np.array([-t[1], t[0]], np.float32)
        r = max(3.0, 0.035 * L)
        ds = np.linspace(-r, r, int(2 * r) + 1)
        pts = []
        for f in np.linspace(0.12, 0.88, 32):
            p = a + (b - a) * f
            xy = p[None, :] + ds[:, None] * n[None, :]
            xi = np.clip(np.round(xy[:, 0]).astype(int), 0, w - 1)
            yi = np.clip(np.round(xy[:, 1]).astype(int), 0, h - 1)
            s_ = np.abs(gx[yi, xi] * n[0] + gy[yi, xi] * n[1])
            if s_.max() < 8:
                continue
            # the strong edge nearest the rough line (the glass, not the bezel's far side)
            ok = np.flatnonzero(s_ >= 0.5 * s_.max())
            k = ok[np.argmin(np.abs(ds[ok]))]
            pts.append(xy[k])
        if len(pts) < 8:
            return q
        vx, vy, x0, y0 = cv2.fitLine(np.array(pts, np.float32), cv2.DIST_HUBER, 0, 0.01, 0.01).ravel()
        lines.append((np.array([x0, y0]), np.array([vx, vy])))
    out = []
    for i in range(4):
        (p1, d1), (p2, d2) = lines[i - 1], lines[i]
        m = np.array([[d1[0], -d2[0]], [d1[1], -d2[1]]])
        if abs(np.linalg.det(m)) < 1e-6:
            return q
        s1 = np.linalg.solve(m, p2 - p1)[0]
        out.append(p1 + d1 * s1)
    out = np.array(out, np.float32)
    a0, a1 = cv2.contourArea(q), cv2.contourArea(out)
    if not cv2.isContourConvex(out.reshape(-1, 1, 2)) or not (0.7 * a0 < a1 < 1.4 * a0) or np.abs(out - q).max() > 0.15 * np.sqrt(a0):
        return q
    return out


def _screen_quad(rgb: np.ndarray, box, trace: np.ndarray) -> np.ndarray:
    """The screen's corners in work pixels: the biggest four-sided outline in the box (a screen's edge against its
    frame or the wall is its strongest line), else the trace when it fills the box, else the box itself."""
    import cv2
    close = _screen_corners(rgb, box)
    if close is not None:
        return _snap_quad(rgb, _fit_quad_lines(rgb, close))
    h, w = rgb.shape[:2]
    x0, y0, x1, y1 = box[0] * w, box[1] * h, box[2] * w, box[3] * h
    bw, bh = x1 - x0, y1 - y0
    px, py = bw * 0.08, bh * 0.08
    X0, Y0, X1, Y1 = int(max(0, x0 - px)), int(max(0, y0 - py)), int(min(w, x1 + px)), int(min(h, y1 + py))
    g = cv2.cvtColor((np.clip(rgb[Y0:Y1, X0:X1], 0, 1) * 255).astype(np.uint8), cv2.COLOR_RGB2GRAY)
    g = cv2.GaussianBlur(g, (0, 0), 1.0)
    med = float(np.median(g))
    edges = cv2.Canny(g, max(10, 0.4 * med), max(30, 1.0 * med))
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8))
    best, best_score = None, 0.0
    cs, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    for c in cs:
        area = cv2.contourArea(c)
        if area < 0.35 * bw * bh or area > 1.3 * bw * bh:
            continue
        ap = cv2.approxPolyDP(c, 0.03 * cv2.arcLength(c, True), True)
        if len(ap) != 4 or not cv2.isContourConvex(ap):
            continue
        q = ap.reshape(4, 2).astype(np.float32) + [X0, Y0]
        qx0, qy0 = q.min(0)
        qx1, qy1 = q.max(0)
        ix = max(0.0, min(x1, qx1) - max(x0, qx0)) * max(0.0, min(y1, qy1) - max(y0, qy0))
        iou = ix / (bw * bh + (qx1 - qx0) * (qy1 - qy0) - ix + 1e-6)
        if iou > best_score:
            best, best_score = q, iou
    if best is None or best_score < 0.5:
        ys, xs = np.nonzero(trace[int(y0):int(y1), int(x0):int(x1)] > 0.5)
        if len(xs) > 0.6 * bw * bh:
            q = _quad((trace > 0.5).astype(np.float32))
            best = q if q is not None else None
    if best is None:
        best = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], np.float32)
    s_, d_ = best.sum(1), np.diff(best, axis=1).ravel()
    return np.array([best[np.argmin(s_)], best[np.argmin(d_)], best[np.argmax(s_)], best[np.argmax(d_)]], np.float32)


def screen_picture() -> Optional[np.ndarray]:
    """The picture a switched-on screen shows (RGB 0..1): a calm sunset over a lake, painted once and kept with Zerko
    in assets/screens (a picture of your own in the library's _screens folder is used first)."""
    import cv2
    root = Path(pe._media_root or ".") / "_screens"
    cands = sorted(p for p in root.glob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")) if root.is_dir() else []
    cands += sorted((Path(__file__).parent / "assets" / "screens").glob("*.jpg"))
    for c in cands:
        im = cv2.imread(str(c), cv2.IMREAD_COLOR)
        if im is not None:
            return cv2.cvtColor(im, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    return None


def fix_screens(vid: int, path: str, boxes, fill: Optional[np.ndarray] = None) -> List[dict]:
    """Each screen black (a soft sheen, like a switched-off screen), or showing `fill` (RGB 0..1) in perspective."""
    import cv2
    out = []
    rgb_w = ai_photo._work_rgb(path)
    hw, ww = rgb_w.shape[:2]
    full = pe._full_u8(path)
    H, W = full.shape[:2]
    _PATH.set(path)
    for b in boxes:
        trace = _trace(path, [b])
        qw = _screen_quad(rgb_w, b, trace)
        qf = qw * [W / ww, H / hw]
        g = int(max(W, H) * 0.004) + 4
        X0, Y0 = max(0, int(qf[:, 0].min()) - g), max(0, int(qf[:, 1].min()) - g)
        X1, Y1 = min(W, int(np.ceil(qf[:, 0].max())) + g), min(H, int(np.ceil(qf[:, 1].max())) + g)
        rgb = full[Y0:Y1, X0:X1].astype(np.float32) / 255.0
        where = (X0, Y0, X1, Y1, W, H)
        q = (qf - [X0, Y0]).astype(np.float32)
        # a hair inside the edge, so the bezel stays the photo's own
        c = q.mean(0)
        q = c + (q - c) * 0.985
        h, w = rgb.shape[:2]
        if fill is not None:
            src = np.array([[0, 0], [fill.shape[1] - 1, 0], [fill.shape[1] - 1, fill.shape[0] - 1], [0, fill.shape[0] - 1]], np.float32)
            M = cv2.getPerspectiveTransform(src, q)
            pic = cv2.warpPerspective(fill.astype(np.float32), M, (w, h), flags=cv2.INTER_AREA, borderMode=cv2.BORDER_REPLICATE)
        else:
            yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
            t = ((xx - q[0][0]) / max(1.0, w) + (yy - q[0][1]) / max(1.0, h)) * 0.5
            sheen = 0.012 + 0.035 * np.exp(-((t - 0.3) ** 2) / 0.02)
            pic = np.dstack([sheen * 0.95, sheen, sheen * 1.08])
        poly = np.zeros((h * 4, w * 4), np.uint8)
        cv2.fillPoly(poly, [np.round(q * 4).astype(np.int32)], 255)
        alpha = cv2.resize(poly, (w, h), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
        out.append(_patch(vid, pic, alpha, where, "screen"))
    return out


FIRE_ASK = ("Light a real, natural wood fire in the fireplace: warm orange flames and glowing logs inside the fire box only. "
            "Keep everything else exactly as it is: the same room, walls, furniture, colours, light and framing.")


def fix_fire(vid: int, path: str, box, user: str = "") -> Optional[dict]:
    """A fire in the fireplace, painted by the image model on a crop round it and kept only inside the opening."""
    import cv2
    import ai_image
    import ai_usage
    if not ai_image.enabled():
        raise ai_image.ImageOff("Image generation is off: set it up in Manage > AI > Image generation.")
    full = pe._full_u8(path)
    H, W = full.shape[:2]
    x0, y0, x1, y1 = box
    bw, bh = (x1 - x0), (y1 - y0)
    # the model needs the room round the fireplace to paint a fire that belongs
    cx0, cy0 = max(0.0, x0 - bw * 0.8), max(0.0, y0 - bh * 0.8)
    cx1, cy1 = min(1.0, x1 + bw * 0.8), min(1.0, y1 + bh * 0.8)
    X0, Y0, X1, Y1 = int(cx0 * W), int(cy0 * H), int(cx1 * W), int(cy1 * H)
    crop = full[Y0:Y1, X0:X1]
    fw, fh = ai_image.fit_size(crop.shape[1], crop.shape[0])
    small = cv2.resize(crop, (fw, fh), interpolation=cv2.INTER_AREA)
    ai_usage.context.set({"video_id": vid, "kind": "fix", "look": "Fire in the fireplace", "user": user})
    painted = ai_image.edit(small, FIRE_ASK)
    painted = cv2.resize(painted, (crop.shape[1], crop.shape[0]), interpolation=cv2.INTER_CUBIC).astype(np.float32) / 255.0
    # only the opening: its trace, a little grown, feathered
    m = _trace(path, [box])
    mm = cv2.resize(m, (W, H), interpolation=cv2.INTER_LINEAR)[Y0:Y1, X0:X1]
    mm = cv2.dilate((mm > 0.4).astype(np.float32), np.ones((5, 5), np.uint8))
    mm = cv2.GaussianBlur(mm, (0, 0), max(2.0, 0.01 * max(crop.shape[:2])))
    # the fire's glow spills a little onto the hearth: a soft halo round the opening, faint
    halo = cv2.GaussianBlur(mm, (0, 0), max(4.0, 0.06 * max(crop.shape[:2]))) * 0.35
    alpha = np.clip(np.maximum(mm, halo * (painted.mean(-1) > crop.mean(-1) / 255.0)), 0, 1)
    return _patch(vid, painted, alpha, (X0, Y0, X1, Y1, W, H), "fire")


def _best_shift(rgb: np.ndarray, w: np.ndarray, ref_a: float, ref_b: float) -> tuple:
    """The warmth (kelvin offset) and tint that bring these pixels closest to the room's own neutral light,
    found by trying them - the same colour move the mask makes (photo_edit.apply_mask: linear * wb_gains)."""
    import cv2
    lin = pe._srgb_to_linear(rgb)
    best = (0.0, 0.0, None)
    for temp in range(-3000, 3001, 250):
        for tint in np.linspace(-1.0, 1.0, 17):
            g = np.asarray(pe.wb_gains(6500.0 + temp, float(tint)), np.float32)
            out = pe._linear_to_srgb(np.clip(lin * g, 0, 1)).astype(np.float32)
            lab = cv2.cvtColor(out.reshape(-1, 1, 3), cv2.COLOR_RGB2LAB).reshape(-1, 3)
            err = float(np.sum(w * ((lab[:, 1] - ref_a) ** 2 + (lab[:, 2] - ref_b) ** 2)) / (w.sum() + 1e-6))
            if best[2] is None or err < best[2]:
                best = (float(temp), float(tint), err)
    return best


def light_casts(vid: int, path: str) -> List[dict]:
    """Coloured light thrown on walls and ceilings that is not the room's own light: the orange of lamps next to
    daylight ("Mixed light"), or the purple, pink or green of LED strips ("LED colour"). Each becomes a painted mask,
    strongest where the cast is, with the warmth and tint that measure back to the room's neutral light."""
    import cv2
    rgb = ai_photo.base_rgb(vid, path, 1280)
    lab = cv2.cvtColor(rgb.astype(np.float32), cv2.COLOR_RGB2LAB)          # L 0..100, a/b about -128..127
    L, A, B = lab[..., 0], lab[..., 1], lab[..., 2]
    chroma = np.sqrt(A * A + B * B)
    # plain pale surfaces (walls, ceilings) show the colour of the light falling on them
    plain = ((chroma < 70) & (L > 30) & (L < 97)).astype(np.float32)
    grad = cv2.Laplacian(cv2.GaussianBlur(L, (0, 0), 1.5), cv2.CV_32F)
    plain *= (np.abs(grad) < 1.5).astype(np.float32)
    if plain.mean() < 0.05:
        return []
    s = max(rgb.shape[:2]) * 0.05
    den = cv2.GaussianBlur(plain, (0, 0), s) + 1e-4
    la = cv2.GaussianBlur(A * plain, (0, 0), s) / den
    lb = cv2.GaussianBlur(B * plain, (0, 0), s) / den
    have = den > 0.08
    if have.mean() < 0.2:
        return []
    # the room's main light: what most of its plain surfaces show (its overall colour is the white balance's job)
    ref_a, ref_b = float(np.median(la[have])), float(np.median(lb[have]))
    da, db = la - ref_a, lb - ref_b
    mag = np.sqrt(da * da + db * db)
    if float(np.percentile(mag[have], 95)) < 6.0:
        return []                                        # one colour of light across the room
    ang = np.degrees(np.arctan2(db, da))                 # 90 = yellow, 0 = magenta, -90 = blue, 180 = green
    # orange-yellow is lamps next to daylight; purple, pink and green are LED strips. Blue is left alone: a cool
    # grey wall is usually its paint, and daylight is the light the room should have.
    groups = {"Mixed light": (ang > 25) & (ang < 150), "LED colour": (ang >= -85) & (ang <= 25) | (ang >= 150) | (ang <= -160)}
    out = []
    for name, sel in groups.items():
        area = have & sel & (mag > (5.0 if name == "Mixed light" else 9.0))
        if area.mean() < 0.03:
            continue
        top = float(np.percentile(mag[area], 95))
        m = np.clip(mag / max(top, 1e-3), 0, 1) * (have & sel)
        m = cv2.GaussianBlur(m.astype(np.float32), (0, 0), s * 0.5)
        pick = (plain > 0) & (m > 0.5)
        if pick.sum() < 200:
            continue
        idx = np.flatnonzero(pick.ravel())
        if len(idx) > 6000:
            idx = np.random.default_rng(1).choice(idx, 6000, replace=False)
        px = rgb.reshape(-1, 3)[idx].astype(np.float32)
        w = m.ravel()[idx].astype(np.float32)
        # the cast is in the light, not the paint: correct it only as far as it reaches back to the room's light
        before = float(np.sum(w * ((A.ravel()[idx] - ref_a) ** 2 + (B.ravel()[idx] - ref_b) ** 2)) / (w.sum() + 1e-6))
        temp, tint, err = _best_shift(px, w, ref_a, ref_b)
        if err > 0.75 * before or (abs(temp) < 150 and abs(tint) < 0.05):
            continue
        name_id = "mx" + uuid.uuid4().hex[:12]
        cv2.imwrite(str(pe.mask_dir(vid) / f"{name_id}.png"), (np.clip(m, 0, 1) * 255).astype(np.uint8))
        out.append(pe.Mask(kind="brush", ref=f"{vid}/{name_id}", name=name, feather=0.0,
                           temp_shift=temp, tint_shift=tint).model_dump())
    return out


# --------------------------------------------------------------------------
# the API (the editor's Scene panel) and one call for the pipeline
# --------------------------------------------------------------------------

@router.post("/{video_id}/find")
def find_route(video_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    v = ai_photo._video(db, video_id)
    return find(video_id, ai_photo._path(v))


class FixBody(BaseModel):
    kind: str = Field(..., pattern="^(grass|pool|screen|fire|light)$")
    boxes: List[List[float]] = Field(default_factory=list, max_length=8)
    strength: float = Field(1.0, ge=0.1, le=1.5)
    fill_video_id: Optional[int] = None          # screens: show this photo instead of black
    fill: str = Field("black", pattern="^(black|picture)$")   # screens: black, or the screen picture (assets/screens)


@router.post("/{video_id}")
def fix_route(video_id: int, body: FixBody, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """One fix on one photo. Returns the patches (or the mask) for the recipe; nothing is saved here."""
    v = ai_photo._video(db, video_id)
    path = ai_photo._path(v)
    if body.kind == "light":
        ms = light_casts(video_id, path)
        if not ms:
            return {"patches": [], "masks": [], "note": "No mixed light found: the light is the same colour across the room."}
        return {"patches": [], "masks": ms}
    boxes = body.boxes or find(video_id, path).get(body.kind, [])
    if not boxes:
        what = {"grass": "No lawn", "pool": "No pool", "screen": "No screens", "fire": "No fireplace"}[body.kind]
        return {"patches": [], "note": f"{what} found in this photo."}
    with pe._remove_lock:
        if body.kind == "grass":
            p = fix_grass(video_id, path, boxes, body.strength)
            patches = [p] if p else []
        elif body.kind == "pool":
            p = fix_pool(video_id, path, boxes, body.strength)
            patches = [p] if p else []
        elif body.kind == "screen":
            fill = screen_picture() if body.fill == "picture" else None
            if body.fill_video_id:
                fv = ai_photo._video(db, body.fill_video_id)
                fill = ai_photo.developed(body.fill_video_id, ai_photo._path(fv), ai_photo.saved_recipe(db, body.fill_video_id), 1600)
            patches = fix_screens(video_id, path, boxes, fill)
        else:
            p = fix_fire(video_id, path, boxes[0], current_user.username)
            patches = [p] if p else []
    return {"patches": patches, "boxes": boxes}


def apply_all(db: Session, vid: int, want: dict, user: str = "") -> List[str]:
    """For the pipeline: the ticked fixes on one photo, saved into its recipe. Returns what was done."""
    path = ai_photo._path(ai_photo._video(db, vid))
    cur = ai_photo.saved_recipe(db, vid)
    removes = list(cur.get("removes") or [])
    masks = list(cur.get("masks") or [])
    done = []
    kinds = [k for k in ("grass", "pool", "screen", "fire") if want.get(k)]
    found = find(vid, path) if kinds else {}
    with pe._remove_lock:
        for k in kinds:
            boxes = found.get(k) or []
            if not boxes:
                continue
            try:
                if k == "grass":
                    ps = [fix_grass(vid, path, boxes)]
                elif k == "pool":
                    ps = [fix_pool(vid, path, boxes)]
                elif k == "screen":
                    ps = fix_screens(vid, path, boxes)
                else:
                    ps = [fix_fire(vid, path, boxes[0], user)]
            except Exception as e:
                print(f"fixes: {k} on {vid} failed: {e}", flush=True)
                continue
            ps = [p for p in ps if p]
            if ps:
                removes = [r for r in removes if r.get("kind") != k] + ps
                done.append(k)
    if want.get("light"):
        masks = [x for x in masks if x.get("name") not in ("Mixed light", "LED colour")]
        for m in light_casts(vid, path):
            if len([x for x in masks if x.get("kind") == "brush"]) < 4:
                masks.append(m)
                if "light" not in done:
                    done.append("light")
    if done:
        cur.update({"removes": removes, "masks": masks})
        ai_photo.save_recipe(db, vid, cur)
    return done


class FillBody(BaseModel):
    points: List[List[float]] = Field(..., min_length=1, max_length=4000)
    r: float = Field(..., gt=0, le=0.3)
    prompt: str = Field("", max_length=300)       # empty: take away what is painted over
    removes: List[pe.RemoveArea] = Field(default_factory=list, max_length=200)


@router.post("/{video_id}/fill")
def fill_route(video_id: int, body: FillBody, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Paint an area and say what goes there (Photoshop's Generative Fill): the area is cleared from its
    surroundings first, then the image model paints what was asked into it, and only the painted area is kept."""
    import cv2
    import ai_image
    import ai_usage
    import remove_ai
    if not ai_image.enabled():
        raise ai_image.ImageOff("Image generation is off: set it up in Manage > AI > Image generation.")
    if re.search(r"(car|cars|vehicle|person|people|man|woman|child)", body.prompt, re.I):
        raise HTTPException(status_code=400, detail="Cars and people are yours to add or take away by hand, not the AI's.")
    v = ai_photo._video(db, video_id)
    path = ai_photo._path(v)
    with pe._remove_lock:
        full = pe._full_u8(path)
        H, W = full.shape[:2]
        mask = remove_ai.stroke_mask(W, H, body.points, body.r) > 0
        ys, xs = np.nonzero(mask)
        if not len(xs):
            raise HTTPException(status_code=400, detail="Paint where it should go")
        bw, bh = xs.max() - xs.min() + 1, ys.max() - ys.min() + 1
        side = int(max(bw, bh) * 2.2) + 64
        cx, cy = (xs.min() + xs.max()) / 2, (ys.min() + ys.max()) / 2
        X0, Y0 = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
        X1, Y1 = int(min(W, cx + side / 2)), int(min(H, cy + side / 2))
        win = full[Y0:Y1, X0:X1].astype(np.float32) / 255.0
        if body.removes:
            win = pe.apply_removes(win, body.removes, window=(X0, Y0, W, H))
        wm = mask[Y0:Y1, X0:X1]
        # what was there goes first, so the model paints onto a clean spot
        try:
            patch, (px0, py0, px1, py1), _eng = remove_ai.fill(win, wm, feather_px=3.0)
            a = patch[..., 3:4]
            win[py0:py1, px0:px1] = win[py0:py1, px0:px1] * (1 - a) + patch[..., :3] * a
        except ValueError:
            pass
    fw, fh = ai_image.fit_size(win.shape[1], win.shape[0])
    small = cv2.resize((np.clip(win, 0, 1) * 255).astype(np.uint8), (fw, fh), interpolation=cv2.INTER_AREA)
    words = body.prompt.strip()
    if words:
        ask = (f"In the middle of this picture, add {words}. It must look real and belong there, with the same "
               "light, shadows, perspective and colours as the rest. Keep everything else exactly as it is.")
    else:
        # the AI remove brush: the part painted over is reimagined as what would be behind it
        ask = ("Something was taken out of the middle of this picture and the gap roughly filled. Repaint that middle part "
               "so it looks untouched: carry on the floor, wall, surfaces, edges and patterns around it, in the same light, "
               "shadows, perspective and colours. Add nothing new - no objects, no people, no cars. Keep everything else "
               "exactly as it is.")
    ai_usage.context.set({"video_id": video_id, "kind": "fix", "look": f"Fill: {words[:60]}" if words else "AI remove",
                          "user": current_user.username})
    painted = ai_image.edit(small, ask)
    painted = cv2.resize(painted, (win.shape[1], win.shape[0]), interpolation=cv2.INTER_CUBIC).astype(np.float32) / 255.0
    grow = max(3, int(max(bw, bh) * 0.04))
    alpha = cv2.dilate(wm.astype(np.uint8), np.ones((grow, grow), np.uint8)).astype(np.float32)
    alpha = cv2.GaussianBlur(alpha, (0, 0), max(2.0, grow * 0.8))
    return _patch(video_id, painted, alpha, (X0, Y0, X1, Y1, W, H), "remove")


def install(app):
    app.include_router(router)
