"""Selections the editor can make for you: the sky, the subject, an object in
a box. Each returns a greyscale mask (float32 0..1) the size of the image it
was given, in the same space - which for the editor is the source photo.

No model downloads and nothing to install: these are classical computer
vision (colour, texture, connectivity, GrabCut) tuned for property photos,
where the sky is at the top, the subject is a building or a room, and the
thing you want to select is usually a clear object.
"""

import numpy as np


def _cv2():
    import cv2
    return cv2


def _refine(mask: np.ndarray, guide: np.ndarray, radius: int = 6) -> np.ndarray:
    """Soften a hard selection so its edge follows the picture: a guided
    filter (edge-aware), then a gentle clamp so solid areas stay solid."""
    cv2 = _cv2()
    g = cv2.cvtColor((guide * 255).astype(np.uint8), cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
    p = mask.astype(np.float32)
    r = max(1, radius)
    eps = 1e-3
    box = lambda x: cv2.boxFilter(x, -1, (2 * r + 1, 2 * r + 1))
    mean_i, mean_p = box(g), box(p)
    cov = box(g * p) - mean_i * mean_p
    var = box(g * g) - mean_i * mean_i
    a = cov / (var + eps)
    b = mean_p - a * mean_i
    q = box(a) * g + box(b)
    return np.clip((q - 0.08) / 0.84, 0.0, 1.0).astype(np.float32)


def select_sky(rgb: np.ndarray) -> np.ndarray:
    """The sky: bright, blue or white, smooth, and connected to the top edge.

    Works on clear blue, hazy and overcast skies. What it will not do is take
    a white wall for sky - a wall is not joined to the top of the frame by
    other sky, and it has texture the sky does not.
    """
    cv2 = _cv2()
    h, w = rgb.shape[:2]
    k = 900.0 / max(h, w)
    small = cv2.resize(rgb, (max(1, int(w * k)), max(1, int(h * k))), interpolation=cv2.INTER_AREA) if k < 1 else rgb.copy()
    sh, sw = small.shape[:2]
    u8 = (np.clip(small, 0, 1) * 255).astype(np.uint8)
    hsv = cv2.cvtColor(u8, cv2.COLOR_RGB2HSV).astype(np.float32)
    hue = hsv[..., 0] * 2.0
    sat = hsv[..., 1] / 255.0
    val = hsv[..., 2] / 255.0
    gray = cv2.cvtColor(u8, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0

    # texture: skies are smooth
    lap = np.abs(cv2.Laplacian(cv2.GaussianBlur(gray, (0, 0), 1.2), cv2.CV_32F, ksize=3))
    texture = cv2.GaussianBlur(lap, (0, 0), 3.0)
    smooth = texture < 0.035

    # a clear sky is any blue from noon to dusk - a twilight sky is dark
    blue = (hue > 170) & (hue < 265) & (sat > 0.08) & (val > 0.12)
    # an overcast sky is near-white and brighter than a ceiling usually is
    white = (sat < 0.16) & (val > 0.78)
    # Seed only from blue or bright white; a sunset glow joins in the growing
    # step below. Seeding from warm tones took a wooden table top for sky.
    cand = smooth & (blue | white)
    # the sky is above the midline almost always - take nothing from the bottom fifth
    cand[int(sh * 0.8):, :] = False

    n, labels, stats, _ = cv2.connectedComponentsWithStats(cand.astype(np.uint8), connectivity=8)
    keep = np.zeros_like(cand)
    top = set(np.unique(labels[: max(2, sh // 40), :])) - {0}
    for lab in top:
        if stats[lab, cv2.CC_STAT_AREA] > sh * sw * 0.01:
            keep |= labels == lab
    if not keep.any():
        return np.zeros((h, w), np.float32)

    # take the sky's own colour and grow into neighbours that match it
    # (clouds, gradients) - but only through smooth, connected ground
    lab_img = cv2.cvtColor(u8, cv2.COLOR_RGB2LAB).astype(np.float32)
    mean = lab_img[keep].mean(axis=0)
    std = lab_img[keep].std(axis=0) + 6.0
    dist = np.sqrt((((lab_img - mean) / std) ** 2).sum(-1))
    grow = (dist < 3.2) & (texture < 0.06) & (val > 0.1)
    grow[int(sh * 0.85):, :] = False
    n, labels, stats, _ = cv2.connectedComponentsWithStats((grow | keep).astype(np.uint8), connectivity=8)
    top = set(np.unique(labels[: max(2, sh // 40), :])) - {0}
    sky = np.isin(labels, list(top)) if top else keep

    # fill holes the size of a bird or a lamp-post top, keep the tree gaps
    sky = cv2.morphologyEx(sky.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8)).astype(np.float32)
    soft = _refine(sky, small, radius=max(3, int(sw / 220)))
    return cv2.resize(soft, (w, h), interpolation=cv2.INTER_LINEAR)


def _grabcut(rgb: np.ndarray, rect, avoid: np.ndarray = None) -> np.ndarray:
    """`avoid`: a 0..1 map of what is certainly not the object (the sky)."""
    cv2 = _cv2()
    h, w = rgb.shape[:2]
    k = 480.0 / max(h, w)
    small = cv2.resize(rgb, (max(1, int(w * k)), max(1, int(h * k))), interpolation=cv2.INTER_AREA) if k < 1 else rgb.copy()
    sh, sw = small.shape[:2]
    bgr = cv2.cvtColor((np.clip(small, 0, 1) * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)
    x0, y0, x1, y1 = rect
    r = (int(x0 * sw), int(y0 * sh), max(4, int((x1 - x0) * sw)), max(4, int((y1 - y0) * sh)))
    mask = np.full((sh, sw), cv2.GC_BGD, np.uint8)
    mask[r[1]:r[1] + r[3], r[0]:r[0] + r[2]] = cv2.GC_PR_FGD
    if avoid is not None:
        a = cv2.resize(avoid.astype(np.float32), (sw, sh), interpolation=cv2.INTER_AREA)
        mask[a > 0.5] = cv2.GC_BGD
    if not (mask == cv2.GC_PR_FGD).any():
        return np.zeros((h, w), np.float32)
    bg = np.zeros((1, 65), np.float64)
    fg = np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(bgr, mask, None, bg, fg, 4, cv2.GC_INIT_WITH_MASK)
    except cv2.error:
        return np.zeros((h, w), np.float32)
    sel = ((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD)).astype(np.float32)
    # keep the biggest piece and whatever touches it
    n, labels, stats, _ = cv2.connectedComponentsWithStats((sel > 0).astype(np.uint8), connectivity=8)
    if n > 2:
        big = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        keepers = [i for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] > stats[big, cv2.CC_STAT_AREA] * 0.08]
        sel = np.isin(labels, keepers).astype(np.float32)
    soft = _refine(sel, small, radius=max(2, int(sw / 260)))
    return cv2.resize(soft, (w, h), interpolation=cv2.INTER_LINEAR)


def select_subject(rgb: np.ndarray) -> np.ndarray:
    """The main subject: what stands apart from the frame's edges. For a
    property exterior that is the house; for a room, the furniture and the
    features in the middle of it."""
    return _grabcut(rgb, (0.06, 0.06, 0.94, 0.97), avoid=select_sky(rgb))


def select_box(rgb: np.ndarray, box) -> np.ndarray:
    """The object inside a box the user dragged (x0, y0, x1, y1 in 0..1)."""
    x0, y0, x1, y1 = [float(v) for v in box]
    x0, x1 = sorted((max(0.0, x0), min(1.0, x1)))
    y0, y1 = sorted((max(0.0, y0), min(1.0, y1)))
    if x1 - x0 < 0.01 or y1 - y0 < 0.01:
        return np.zeros(rgb.shape[:2], np.float32)
    return _grabcut(rgb, (x0, y0, x1, y1))


# --------------------------------------------------------------------------
# Segment Anything (SAM, ViT-B, quantised ONNX): clicks and boxes
# --------------------------------------------------------------------------
#
# Runs on the server's CPU with onnxruntime - nothing leaves the machine. The
# two model files (108 MB) are fetched once, the first time a selection is
# asked for, into models/ next to the app. The photo's embedding (the slow
# part, a few seconds) is kept for the last few photos, so every click after
# the first answers at once.

import os
import threading
import time
from pathlib import Path

MODEL_DIR = Path(__file__).resolve().parent / "models"
_BASE = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/"
MODELS = {
    "sam-encoder": ("vit_b-encoder-quant.onnx", 99827815),
    "sam-decoder": ("vit_b-decoder-quant.onnx", 8742591),
}
_state = {"status": "idle", "done": 0, "total": sum(s for _, s in MODELS.values()), "error": ""}
_state_lock = threading.Lock()
_sessions: dict = {}
_emb_cache: "dict[tuple, tuple]" = {}
_run_lock = threading.Lock()


def _have_files() -> bool:
    return all((MODEL_DIR / f).is_file() and (MODEL_DIR / f).stat().st_size == size for f, size in MODELS.values())


def ai_status() -> dict:
    try:
        import onnxruntime  # noqa: F401
        rt = True
    except Exception:
        rt = False
    with _state_lock:
        st = dict(_state)
    if _have_files():
        st["status"] = "ready"
    st["runtime"] = rt
    return st


def _download():
    import urllib.request
    try:
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        done = 0
        for f, size in MODELS.values():
            dest = MODEL_DIR / f
            if dest.is_file() and dest.stat().st_size == size:
                done += size
                with _state_lock:
                    _state["done"] = done
                continue
            tmp = dest.with_suffix(".part")
            with urllib.request.urlopen(_BASE + f, timeout=60) as r, open(tmp, "wb") as out:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    with _state_lock:
                        _state["done"] = done
            if tmp.stat().st_size != size:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(f"{f} came down incomplete - try again")
            tmp.replace(dest)
        with _state_lock:
            _state.update(status="ready", error="")
    except Exception as e:
        with _state_lock:
            _state.update(status="error", error=str(e))


def ensure_models() -> dict:
    """Start the one-time download if needed. Returns the status."""
    st = ai_status()
    if st["status"] in ("ready", "downloading"):
        return st
    with _state_lock:
        _state.update(status="downloading", done=0, error="")
    threading.Thread(target=_download, daemon=True).start()
    return ai_status()


def _session(key: str):
    if key not in _sessions:
        import onnxruntime as ort
        opt = ort.SessionOptions()
        opt.intra_op_num_threads = max(1, (os.cpu_count() or 4) // 2)
        _sessions[key] = ort.InferenceSession(str(MODEL_DIR / MODELS[key][0]), opt, providers=["CPUExecutionProvider"])
    return _sessions[key]


def _embed(u8: np.ndarray, key):
    """The SAM embedding of an 8-bit RGB image, cached by `key`."""
    hit = _emb_cache.get(key)
    if hit is not None:
        return hit
    cv2 = _cv2()
    h, w = u8.shape[:2]
    s = 1024.0 / max(h, w)
    nh, nw = int(h * s + 0.5), int(w * s + 0.5)
    x = cv2.resize(u8, (nw, nh), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR).astype(np.float32)
    x = (x - np.float32([123.675, 116.28, 103.53])) / np.float32([58.395, 57.12, 57.375])
    pad = np.zeros((1024, 1024, 3), np.float32)
    pad[:nh, :nw] = x
    e = _session("sam-encoder").run(None, {"x": pad.transpose(2, 0, 1)[None]})[0]
    out = (e, s, nh, nw, h, w)
    if len(_emb_cache) >= 4:
        _emb_cache.pop(next(iter(_emb_cache)))
    _emb_cache[key] = out
    return out


def _decode(emb, points, labels) -> np.ndarray:
    """Mask logits at the embedded image's size. points in its pixels;
    labels 1 = in, 0 = out, 2/3 = box corners."""
    e, s, nh, nw, h, w = emb
    cv2 = _cv2()
    P = np.array([[[p[0] * s, p[1] * s] for p in points] + [[0.0, 0.0]]], np.float32)
    L = np.array([list(labels) + [-1]], np.float32)
    _, _, low = _session("sam-decoder").run(None, {
        "image_embeddings": e, "point_coords": P, "point_labels": L,
        "mask_input": np.zeros((1, 1, 256, 256), np.float32),
        "has_mask_input": np.zeros(1, np.float32),
        "orig_im_size": np.array([h, w], np.float32)})
    # The low-res mask covers the padded 1024 square: scale it, cut the padding
    # off, then bring it to the image's size. (The model's own full-size output
    # comes back stretched in this export.)
    lr = cv2.resize(low[0, 0], (1024, 1024), interpolation=cv2.INTER_LINEAR)[:nh, :nw]
    return cv2.resize(lr, (w, h), interpolation=cv2.INTER_LINEAR)


def _soft(logits: np.ndarray, guide_u8: np.ndarray) -> np.ndarray:
    """Logits to a 0..1 mask whose edge follows the picture."""
    hard = (logits > 0).astype(np.float32)
    return _refine(hard, guide_u8.astype(np.float32) / 255.0, radius=max(2, int(max(hard.shape) / 400)))


def sam_click(u8: np.ndarray, key, points, labels, out_hw) -> np.ndarray:
    """Points in 0..1 of the image. Returns a mask at out_hw."""
    cv2 = _cv2()
    with _run_lock:
        emb = _embed(u8, key)
        h, w = emb[4], emb[5]
        logits = _decode(emb, [(x * w, y * h) for x, y in points], labels)
    oh, ow = out_hw
    guide = cv2.resize(u8, (ow, oh), interpolation=cv2.INTER_AREA)
    return _soft(cv2.resize(logits, (ow, oh), interpolation=cv2.INTER_LINEAR), guide)


def sam_box(u8: np.ndarray, key, box, out_hw) -> np.ndarray:
    """box: x0, y0, x1, y1 in 0..1. A small box is looked at close up, at the
    photo's own resolution, so a car in a street scene is not a smudge."""
    cv2 = _cv2()
    H, W = u8.shape[:2]
    x0, y0, x1, y1 = box[0] * W, box[1] * H, box[2] * W, box[3] * H
    bw, bh = x1 - x0, y1 - y0
    oh, ow = out_hw
    out = np.zeros((oh, ow), np.float32)
    with _run_lock:
        if bw * bh < 0.3 * W * H:
            m = max(bw, bh) * 0.8 + 16
            cx0, cy0 = int(max(0, x0 - m)), int(max(0, y0 - m))
            cx1, cy1 = int(min(W, x1 + m)), int(min(H, y1 + m))
            crop = np.ascontiguousarray(u8[cy0:cy1, cx0:cx1])
            emb = _embed(crop, (key, "crop", cx0, cy0, cx1, cy1))
            logits = _decode(emb, [(x0 - cx0, y0 - cy0), (x1 - cx0, y1 - cy0)], [2, 3])
            # into the output's coordinates
            ox0, oy0 = int(round(cx0 * ow / W)), int(round(cy0 * oh / H))
            ox1, oy1 = max(ox0 + 1, int(round(cx1 * ow / W))), max(oy0 + 1, int(round(cy1 * oh / H)))
            part = cv2.resize(logits, (ox1 - ox0, oy1 - oy0), interpolation=cv2.INTER_LINEAR)
            guide = cv2.resize(crop, (ox1 - ox0, oy1 - oy0), interpolation=cv2.INTER_AREA)
            out[oy0:oy1, ox0:ox1] = _soft(part, guide)
            return out
        emb = _embed(u8, key)
        logits = _decode(emb, [(x0, y0), (x1, y1)], [2, 3])
    guide = cv2.resize(u8, (ow, oh), interpolation=cv2.INTER_AREA)
    return _soft(cv2.resize(logits, (ow, oh), interpolation=cv2.INTER_LINEAR), guide)


def sam_sky(u8: np.ndarray, key, out_hw) -> np.ndarray:
    """The sky: seeded from sky-coloured pixels along the very top of the
    frame only (so the sea below the horizon is never a seed), with the
    bottom of the frame marked as not-sky. SAM then finds the true edge -
    horizon, mountain line, roofs and trees."""
    cv2 = _cv2()
    H, W = u8.shape[:2]
    rough = select_sky(u8.astype(np.float32) / 255.0)
    top = rough.copy()
    top[max(2, int(H * 0.06)):] = 0
    ys, xs = np.where(top > 0.9)
    if len(xs) == 0:
        return np.zeros(out_hw, np.float32)
    rng = np.random.default_rng(0)
    idx = rng.choice(len(xs), min(5, len(xs)), replace=False)
    pos = [(float(xs[i]), float(ys[i])) for i in idx]
    neg = [(W * f, H * 0.95) for f in (0.25, 0.5, 0.75)]
    with _run_lock:
        emb = _embed(u8, key)
        logits = _decode(emb, pos + neg, [1] * len(pos) + [0] * len(neg))
    oh, ow = out_hw
    guide = cv2.resize(u8, (ow, oh), interpolation=cv2.INTER_AREA)
    m = _soft(cv2.resize(logits, (ow, oh), interpolation=cv2.INTER_LINEAR), guide)
    # sky is joined to the top of the frame: drop islands SAM found elsewhere
    n, labels, stats, _ = cv2.connectedComponentsWithStats((m > 0.5).astype(np.uint8), connectivity=8)
    keep = set(np.unique(labels[: max(2, oh // 40), :])) - {0}
    if keep:
        m = m * np.isin(labels, list(keep)).astype(np.float32) + m * (m <= 0.5)
    return m


# --------------------------------------------------------------------------
# windows (for the window pull)
# --------------------------------------------------------------------------

def window_boxes(rgb: np.ndarray):
    """Blown-out windows in an interior: large, bright, near-white or pale-blue
    areas, fairly rectangular, set in a darker room. Returns [(x0, y0, x1, y1)]
    in 0..1 and a rough 0..1 mask at rgb's size.

    Lamps and reflections are too small; a white wall is not bright enough
    next to a real window; the sky of an exterior is not "in a darker room"
    (and is left to the sky tools)."""
    cv2 = _cv2()
    h, w = rgb.shape[:2]
    k = 900 / max(h, w)
    small = cv2.resize(rgb, (max(1, int(w * k)), max(1, int(h * k))), interpolation=cv2.INTER_AREA) if k < 1 else rgb
    sh, sw = small.shape[:2]
    L = small @ np.float32([0.2126, 0.7152, 0.0722])
    mx, mn = small.max(-1), small.min(-1)
    sat = (mx - mn) / np.maximum(mx, 1e-4)
    med = float(np.median(L))
    thr = max(0.82, min(0.95, med + 0.42))
    bright = (L >= thr) & ((sat < 0.35) | ((small[..., 2] > small[..., 0]) & (sat < 0.6)))
    bright = cv2.morphologyEx(bright.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(bright, 8)
    boxes, keep = [], np.zeros((sh, sw), np.uint8)
    area_all = sh * sw
    for i in range(1, n):
        x, y, bw, bh, a = stats[i]
        if a < area_all * 0.003 or a > area_all * 0.45:
            continue
        if a / float(bw * bh) < 0.35:            # windows are roughly rectangular
            continue
        # a darker room around it
        m = max(4, int(max(bw, bh) * 0.15))
        rx0, ry0, rx1, ry1 = max(0, x - m), max(0, y - m), min(sw, x + bw + m), min(sh, y + bh + m)
        ring = np.ones((ry1 - ry0, rx1 - rx0), bool)
        ring[y - ry0:y - ry0 + bh, x - rx0:x - rx0 + bw] = False
        if ring.any() and float(np.median(L[ry0:ry1, rx0:rx1][ring])) > thr - 0.18:
            continue
        keep[labels == i] = 1
        boxes.append((x / sw, y / sh, (x + bw) / sw, (y + bh) / sh))
    soft = _refine(keep.astype(np.float32), small, radius=max(2, int(max(sh, sw) / 300)))
    return boxes, cv2.resize(soft, (w, h), interpolation=cv2.INTER_LINEAR)


def find_windows(rgb: np.ndarray, out_hw, u8=None, key=None) -> tuple:
    """(mask at out_hw, number of windows). With Segment Anything on hand each
    window's outline is traced by it, so the frames and bars stay crisp."""
    cv2 = _cv2()
    boxes, rough = window_boxes(rgb)
    oh, ow = out_hw
    if not boxes:
        return np.zeros((oh, ow), np.float32), 0
    out = cv2.resize(rough, (ow, oh), interpolation=cv2.INTER_LINEAR)
    if u8 is not None and key is not None and ai_status().get("status") == "ready":
        try:
            acc = np.zeros((oh, ow), np.float32)
            for b in boxes:
                pad = 0.01
                acc = np.maximum(acc, sam_box(u8, key, [max(0, b[0] - pad), max(0, b[1] - pad), min(1, b[2] + pad), min(1, b[3] + pad)], out_hw))
            # SAM traces the window; keep only what is also bright (not the curtains it may take along)
            out = np.minimum(acc, np.clip(out * 1.6, 0, 1))
        except Exception as e:
            print(f"editor_ai: SAM windows failed, using the rough outline: {e}", flush=True)
    return out.astype(np.float32), len(boxes)
