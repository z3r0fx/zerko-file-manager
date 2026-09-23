"""The editor's Remove tool: paint over a car, a bin, a sign, a person - it is
replaced with what would have been behind it, made up from the surroundings.

It uses LaMa (Large Mask inpainting, Apache licence), 92 MB, downloaded the
first time it is needed into models/ and run on the CPU with onnxruntime -
nothing leaves the machine. Until it is there, OpenCV's quick fill stands in
(fine for small things, soft on big ones), and the tool says so.

What a removal produces is a patch: the filled pixels with a soft-edged
alpha, in source space (the photo as it came from the camera), saved as a
PNG beside the brush masks. The preview and the export both lay the same
patch over the photo before anything else happens, so they agree exactly.
"""

from __future__ import annotations

import threading
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

import numpy as np

MODEL_DIR = Path(__file__).resolve().parent / "models"
MODEL = ("inpainting_lama_2025jan.onnx", 92591623,
         "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/inpainting_lama/inpainting_lama_2025jan.onnx")
SIZE = 512

_state = {"status": "idle", "done": 0, "total": MODEL[1], "error": ""}
_lock = threading.Lock()
_session = None


def model_path() -> Path:
    return MODEL_DIR / MODEL[0]


def ready() -> bool:
    p = model_path()
    return p.is_file() and p.stat().st_size == MODEL[1]


def status() -> dict:
    if ready():
        return {"status": "ready", "done": MODEL[1], "total": MODEL[1], "error": ""}
    return dict(_state)


def _download():
    try:
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        tmp = model_path().with_suffix(".part")
        with urllib.request.urlopen(MODEL[2], timeout=60) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                _state["done"] += len(chunk)
        if tmp.stat().st_size != MODEL[1]:
            tmp.unlink(missing_ok=True)
            raise RuntimeError("the download came out the wrong size")
        tmp.replace(model_path())
        _state["status"] = "ready"
    except Exception as e:
        _state.update(status="error", error=str(e)[:200])
        print(f"remove_ai: could not download the model: {e}", flush=True)


def ensure_download() -> None:
    """Start fetching the model in the background if it is not there yet."""
    with _lock:
        if ready() or _state["status"] == "downloading":
            return
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            _state.update(status="error", error="onnxruntime is not installed")
            return
        _state.update(status="downloading", done=0, error="")
        threading.Thread(target=_download, daemon=True, name="lama-download").start()


def _get_session():
    global _session
    if _session is None:
        import onnxruntime as ort
        opt = ort.SessionOptions()
        opt.log_severity_level = 3
        _session = ort.InferenceSession(str(model_path()), opt, providers=["CPUExecutionProvider"])
    return _session


def _lama(crop: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """crop float32 HxWx3 0..1, mask bool HxW -> filled crop, same size."""
    import cv2
    h, w = crop.shape[:2]
    k = SIZE / max(h, w)
    nw, nh = max(1, round(w * k)), max(1, round(h * k))
    small = cv2.resize(crop, (nw, nh), interpolation=cv2.INTER_AREA if k < 1 else cv2.INTER_CUBIC)
    m = cv2.resize(mask.astype(np.uint8) * 255, (nw, nh), interpolation=cv2.INTER_NEAREST) > 0
    # pad to the square the model takes, mirroring so the edge is not a wall
    pw, ph = SIZE - nw, SIZE - nh
    img = cv2.copyMakeBorder(small, 0, ph, 0, pw, cv2.BORDER_REFLECT_101)
    mm = cv2.copyMakeBorder(m.astype(np.uint8), 0, ph, 0, pw, cv2.BORDER_CONSTANT, value=0)
    out = _get_session().run(None, {
        "image": img.transpose(2, 0, 1)[None].astype(np.float32),
        "mask": (mm > 0).astype(np.float32)[None, None],
    })[0][0].transpose(1, 2, 0)[:nh, :nw] / 255.0
    out = np.clip(out, 0, 1).astype(np.float32)
    big = cv2.resize(out, (w, h), interpolation=cv2.INTER_CUBIC)
    # the model worked on a shrunk copy: put back fine grain from outside the
    # hole so the fill does not look smoother than the photo around it
    if k < 1:
        lo = cv2.GaussianBlur(crop, (0, 0), 0.6 / k)
        detail = crop - lo
        ring = cv2.dilate(mask.astype(np.uint8), np.ones((9, 9), np.uint8)) & (~mask).astype(np.uint8)
        if ring.any():
            amp = float(np.std(detail[ring > 0]))
            rng = np.random.default_rng(1234)
            noise = rng.standard_normal(big.shape[:2]).astype(np.float32)[..., None] * amp * 0.6
            big = np.clip(big + noise, 0, 1)
    return big


def _quick(crop: np.ndarray, mask: np.ndarray) -> np.ndarray:
    import cv2
    u8 = (np.clip(crop, 0, 1) * 255).astype(np.uint8)
    rad = max(3, int(round(min(crop.shape[:2]) * 0.02)))
    out = cv2.inpaint(u8, mask.astype(np.uint8) * 255, rad, cv2.INPAINT_TELEA)
    return out.astype(np.float32) / 255.0


def fill(img: np.ndarray, mask: np.ndarray, feather_px: float) -> Tuple[np.ndarray, Tuple[int, int, int, int], str]:
    """img float32 HxWx3 (the photo so far), mask bool HxW (what to remove).
    Returns (RGBA patch float32, (x0, y0, x1, y1) in img px, engine)."""
    import cv2
    H, W = mask.shape
    ys, xs = np.nonzero(mask)
    if not len(xs):
        raise ValueError("nothing painted")
    bx0, bx1, by0, by1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    bw, bh = bx1 - bx0, by1 - by0
    # enough surroundings for the model to understand the scene
    m = int(max(bw, bh) * 0.9) + 24
    cx0, cy0 = max(0, bx0 - m), max(0, by0 - m)
    cx1, cy1 = min(W, bx1 + m), min(H, by1 + m)
    crop = img[cy0:cy1, cx0:cx1]
    cm = mask[cy0:cy1, cx0:cx1]
    engine = "lama" if ready() else "quick"
    if engine == "lama":
        try:
            filled = _lama(crop, cm)
        except Exception as e:
            print(f"remove_ai: LaMa failed, using the quick fill: {e}", flush=True)
            engine = "quick"
    if engine == "quick":
        ensure_download()
        filled = _quick(crop, cm)
    # the patch: the painted area plus a soft edge
    f = max(1.0, float(feather_px))
    grow = int(np.ceil(f * 2))
    px0, py0 = max(cx0, bx0 - grow), max(cy0, by0 - grow)
    px1, py1 = min(cx1, bx1 + grow), min(cy1, by1 + grow)
    sub = mask[py0:py1, px0:px1].astype(np.float32)
    k = int(np.ceil(f)) * 2 + 1
    alpha = cv2.GaussianBlur(cv2.dilate(sub, np.ones((k, k), np.uint8)), (0, 0), f / 2)
    alpha = np.maximum(alpha, sub)            # the painted area itself is fully replaced
    rgb = filled[py0 - cy0:py1 - cy0, px0 - cx0:px1 - cx0]
    patch = np.dstack([rgb, np.clip(alpha, 0, 1)]).astype(np.float32)
    return patch, (int(px0), int(py0), int(px1), int(py1)), engine


def stroke_mask(W: int, H: int, points, r: float) -> np.ndarray:
    """A painted stroke (source 0..1 points, radius a fraction of the width)
    as a mask at W x H, a little wider than painted so no edge is left."""
    import cv2
    m = np.zeros((H, W), np.uint8)
    rad = max(1, int(round(float(r) * W * 1.12)))
    pts = [(int(round(float(u) * W)), int(round(float(v) * H))) for u, v in points]
    for i, p in enumerate(pts):
        cv2.circle(m, p, rad, 255, -1, lineType=cv2.LINE_AA)
        if i:
            cv2.line(m, pts[i - 1], p, 255, rad * 2, lineType=cv2.LINE_AA)
    return m > 96


_patch_cache: dict = {}


def load_patch(path: Path) -> Optional[np.ndarray]:
    """A saved patch as float32 RGBA 0..1, or None."""
    key = str(path)
    try:
        mt = path.stat().st_mtime_ns
    except OSError:
        return None
    hit = _patch_cache.get(key)
    if hit and hit[0] == mt:
        return hit[1]
    import cv2
    im = cv2.imread(key, cv2.IMREAD_UNCHANGED)
    if im is None or im.ndim != 3 or im.shape[2] != 4:
        return None
    rgba = cv2.cvtColor(im, cv2.COLOR_BGRA2RGBA).astype(np.float32) / 255.0
    if len(_patch_cache) > 64:
        _patch_cache.clear()
    _patch_cache[key] = (mt, rgba)
    return rgba
