"""HDR from brackets.

A property shoot comes home as sets of three, five or seven frames of the same
room at different exposures. This finds those sets and fuses each one into a
single natural-looking photo, in bulk, without anybody opening Lightroom.

The fusion is Mertens exposure fusion, not tone-mapped HDR: it picks the
best-exposed, best-contrast pixels from each frame and blends them in a
pyramid. That is what gives the clean, believable look estate agents ask for -
bright windows that still show the view, no halos, no grey mush - and it needs
no camera response curve, so it works with JPEG, TIFF and RAW alike.

Nothing is overwritten. Results land in a subfolder (default "HDR") beside the
frames they came from, and are added to the library so the existing proxy
export can take them straight to portal sizes.
"""

import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import previews
from auth import get_admin_user, get_current_user
from database import IndexedFolder, SessionLocal, User, Video, get_db
from media_type_utils import get_media_type

router = APIRouter(prefix="/api/hdr", tags=["hdr"])

_media_root: Optional[Path] = None
_resolve = lambda p: p              # replaced by install()

JOBS: Dict[str, dict] = {}
_jobs_lock = threading.Lock()
_slots = threading.Semaphore(1)     # fusing is memory-hungry; one at a time

MAX_GROUP = 9
KEEP_HOURS = 12
# 6000px on the long edge is ~24MP: more than any property portal will ever
# show, and it keeps three frames of pyramid maths inside sane memory.
DEFAULT_MAX_DIM = 6000


# --------------------------------------------------------------------------
# reading photos
# --------------------------------------------------------------------------

def _cv2():
    import cv2                       # imported lazily: the app boots without it
    return cv2


def _exif_flash(path: str) -> Optional[bool]:
    """Whether the camera says the flash fired (bit 0 of the EXIF Flash tag).
    None when the file does not say."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            exif = im.getexif()
            ifd = exif.get_ifd(0x8769) if hasattr(exif, "get_ifd") else {}
            v = (ifd or {}).get(0x9209, exif.get(0x9209))
            return None if v is None else bool(int(v) & 1)
    except Exception:
        return None


def _exif_capture(path: str) -> Tuple[Optional[datetime], Optional[float], Optional[float]]:
    """(taken_at, exposure_bias, shutter). Falls back to the file's own time."""
    taken = bias = shutter = None
    try:
        from PIL import Image, ExifTags
        with Image.open(path) as im:
            exif = im.getexif()
            if exif:
                tags = {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}
                raw = tags.get("DateTimeOriginal") or tags.get("DateTime")
                if raw:
                    try:
                        taken = datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")
                    except ValueError:
                        pass
                ifd = exif.get_ifd(0x8769) if hasattr(exif, "get_ifd") else {}
                sub = {ExifTags.TAGS.get(k, k): v for k, v in (ifd or {}).items()}
                raw = sub.get("DateTimeOriginal")
                if raw and not taken:
                    try:
                        taken = datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")
                    except ValueError:
                        pass
                for key, into in (("ExposureBiasValue", "bias"), ("ExposureTime", "shutter")):
                    val = sub.get(key, tags.get(key))
                    if val is None:
                        continue
                    try:
                        num = float(val)
                    except (TypeError, ValueError):
                        continue
                    if into == "bias":
                        bias = num
                    else:
                        shutter = num
    except Exception:
        pass
    if taken is None:
        try:
            taken = datetime.fromtimestamp(os.path.getmtime(path))
        except OSError:
            taken = None
    return taken, bias, shutter


def _load(path: str, max_dim: int, fast: bool = False) -> Tuple[np.ndarray, str]:
    """Any photo we can read, as 8-bit BGR, no bigger than max_dim.

    Returns the image and how it was decoded: "full" when the file was read
    properly, or "preview" when all we could get was the small JPEG the camera
    buried inside the RAW. That difference matters enormously - fusing three
    embedded previews gives you a soft, blocky photo that looks nothing like
    what the camera captured - so the caller gets told rather than guessing.
    """
    cv2 = _cv2()
    ext = Path(path).suffix.lower()
    img = None
    how = "full"

    if ext in previews.RAW_EXT:
        try:
            import rawpy
            with rawpy.imread(path) as raw:
                # Camera white balance, and NO auto brightness: every frame in
                # the bracket has to keep its own exposure or there is nothing
                # left to fuse.
                rgb = raw.postprocess(use_camera_wb=True, no_auto_bright=True,
                                      output_bps=8, half_size=fast)
            img = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        except Exception:
            img = None

    # Deliberately NOT cv2.imread for RAW: OpenCV will happily open a DNG as a
    # plain TIFF and hand back the undemosaiced sensor data, which looks like a
    # badly upscaled photo - aliased, blocky, wrong colour. Better to fall
    # through to the embedded preview and say so.
    if img is None and ext not in previews.RAW_EXT:
        img = cv2.imread(path, cv2.IMREAD_COLOR)

    if img is None:                       # last resort: whatever previews can do
        how = "preview" if ext in previews.RAW_EXT else "full"
        # System temp, never beside the photo - see photo_edit._read_rgb.
        import tempfile
        tmp = os.path.join(tempfile.gettempdir(), f"zerko-hdrtmp-{uuid.uuid4().hex}.jpg")
        ok, why = previews.make_image_jpeg(path, tmp, width=max_dim)
        if ok:
            img = cv2.imread(tmp, cv2.IMREAD_COLOR)
        try:
            os.remove(tmp)
        except OSError:
            pass
        if img is None:
            raise RuntimeError(why or f"could not read {os.path.basename(path)}")

    h, w = img.shape[:2]
    longest = max(h, w)
    # Only ever downscale. Enlarging a frame to hit a requested size invents
    # detail that was never there, which is what made "full resolution" the
    # worst-looking option rather than the best.
    if max_dim and longest > max_dim:
        scale = max_dim / float(longest)
        img = cv2.resize(img, (int(round(w * scale)), int(round(h * scale))),
                         interpolation=cv2.INTER_AREA)
    return img, how


# --------------------------------------------------------------------------
# finding the brackets
# --------------------------------------------------------------------------

class ScanBody(BaseModel):
    folder_id: Optional[int] = None
    video_ids: List[int] = []
    gap_seconds: float = Field(4.0, ge=0.5, le=120)
    fixed_size: int = Field(0, ge=0, le=60)    # 0 = work it out
    # what the sets are for: brackets (hdr), ambient + flash (flambient), or a panorama
    mode: str = Field("hdr", pattern="^(hdr|flambient|panorama)$")


def _group(items: List[dict], gap: float, fixed: int, most: int = MAX_GROUP) -> List[List[dict]]:
    """Split a time-ordered list of frames into brackets.

    Fixed size is the safe option when you always shoot the same number. On
    auto, a set ends when the next frame is far away in time, or when the
    exposure compensation starts over - which is exactly what a bracket does
    when the camera rolls round to the next set.
    """
    groups: List[List[dict]] = []
    current: List[dict] = []

    def flush():
        if len(current) >= 2:
            groups.append(list(current))
        current.clear()

    for item in items:
        if not current:
            current.append(item)
            continue
        if fixed:
            current.append(item)
            if len(current) == fixed:
                flush()
            continue

        prev = current[-1]
        apart = None
        if item["taken_at"] and prev["taken_at"]:
            apart = abs((item["taken_at"] - prev["taken_at"]).total_seconds())
        restarted = (
            item.get("bias") is not None
            and current[0].get("bias") is not None
            and len(current) > 1
            and abs(item["bias"] - current[0]["bias"]) < 1e-6
        )
        if (apart is not None and apart > gap) or restarted or len(current) >= most:
            flush()
        current.append(item)
    flush()
    return groups


def _guess_flash(groups: List[List[dict]]) -> None:
    """Which frames of each set are the flash pops: what the camera says, or
    else the frames whose light is most neutral (flash is daylight-white, the
    room's own lamps are orange), leaving at least one ambient frame."""
    cv2 = _cv2()
    for g in groups:
        said = [_exif_flash(i["path"]) for i in g]
        if any(x is True for x in said) and not all(x is True for x in said):
            for i, x in zip(g, said):
                i["flash"] = bool(x)
            continue
        casts = []
        for i in g:
            try:
                img, _ = _load(i["path"], 400)
                b, gg, r = [float(c) for c in img.reshape(-1, 3).mean(0)]
                casts.append((r - b) / max(1.0, (r + gg + b) / 3))
            except Exception:
                casts.append(0.0)
        order = sorted(range(len(g)), key=lambda k: casts[k])
        # the coolest-lit frame(s) are flash; everything clearly warmer is ambient
        coolest = casts[order[0]]
        for k, i in enumerate(g):
            i["flash"] = casts[k] <= coolest + 0.04 and k != order[-1]


@router.post("/scan")
def scan(body: ScanBody, db: Session = Depends(get_db),
         current_user: User = Depends(get_current_user)):
    q = db.query(Video).filter(Video.is_active.is_(True), Video.media_type == "photo")
    if body.video_ids:
        q = q.filter(Video.id.in_(body.video_ids[:2000]))
    elif body.folder_id is not None:
        q = q.filter(Video.folder_id == body.folder_id)
    else:
        raise HTTPException(status_code=400, detail="Pick a folder or some photos first")

    rows = q.all()
    if not rows:
        return {"groups": [], "ungrouped": 0, "message": "No photos here"}

    items = []
    for v in rows:
        real = _resolve(v.filepath)
        if not real or not os.path.exists(real):
            continue
        taken, bias, shutter = _exif_capture(real)
        items.append({
            "id": v.id, "filename": v.filename, "path": real,
            "thumbnail_path": v.thumbnail_path,
            "taken_at": taken, "bias": bias, "shutter": shutter,
        })
    items.sort(key=lambda i: (i["taken_at"] or datetime.min, i["filename"]))

    if body.mode == "panorama":
        # a panorama is not a bracket: no exposure restarts, many more frames
        for i in items:
            i["bias"] = None
        groups = _group(items, body.gap_seconds, body.fixed_size, most=60)
        if not groups and body.video_ids and len(items) >= 2:
            groups = [items]
    else:
        groups = _group(items, body.gap_seconds, body.fixed_size)
    grouped_ids = {i["id"] for g in groups for i in g}
    if body.mode == "flambient":
        _guess_flash(groups)

    def out(i):
        return {
            "id": i["id"], "filename": i["filename"],
            "thumbnail_path": i["thumbnail_path"],
            "taken_at": i["taken_at"].isoformat() if i["taken_at"] else None,
            "bias": i["bias"],
            "flash": i.get("flash", False),
        }

    return {
        "groups": [{"key": f"g{n}", "items": [out(i) for i in g]} for n, g in enumerate(groups)],
        "ungrouped": len([i for i in items if i["id"] not in grouped_ids]),
        "message": f"{len(groups)} bracket{'' if len(groups) == 1 else 's'} found",
    }


# --------------------------------------------------------------------------
# fusing
# --------------------------------------------------------------------------

class HdrLook(BaseModel):
    """How a bracket is turned into one photo - shared by the preview and the merge."""
    method: str = Field("natural", pattern="^(natural|hdr)$")   # exposure fusion, or tone-mapped HDR
    deghost: bool = True           # things that moved between frames (trees, people, water) taken from one frame
    windows: float = Field(0.4, ge=0, le=1)     # pull the view through bright windows from the darkest frame
    exposure: float = Field(0, ge=-2, le=2)     # EV, the whole photo
    shadows: float = Field(0.15, ge=-1, le=1)
    contrast: float = Field(0.1, ge=-1, le=1)
    saturation: float = Field(0.05, ge=-1, le=1)
    detail: float = Field(0.2, ge=0, le=1)      # local contrast
    warmth: float = Field(0, ge=-1, le=1)


class MergeBody(BaseModel):
    groups: List[List[int]]
    look: Optional[HdrLook] = None  # the HDR panel's settings; without it the classic fusion
    mode: str = Field("hdr", pattern="^(hdr|flambient|panorama)$")
    # flambient: which frames of each set are flash pops, and how much of the
    # ambient light's mood comes through (0 all flash, 1 mostly ambient)
    flash: List[List[int]] = []
    ambient_mix: float = Field(0.45, ge=0, le=1)
    align: bool = True                       # handheld: line the frames up first
    max_dim: int = Field(DEFAULT_MAX_DIM, ge=1000, le=20000)
    fmt: str = Field("jpg", pattern="^(jpg|tif)$")
    quality: int = Field(92, ge=60, le=100)
    subfolder: str = "HDR"
    add_to_library: bool = True
    # A little contrast and warmth back after fusion, which flattens slightly.
    polish: bool = True
    # Fuse RAW even when only the camera's embedded preview can be read. Off,
    # because the result is far softer than the frames deserve.
    allow_preview_raw: bool = False
    bits16: bool = False            # TIFF: 16 bits a channel, for more editing afterwards


def _polish(img: np.ndarray) -> np.ndarray:
    """A gentle S-curve and a touch of saturation - what fusion takes out."""
    cv2 = _cv2()
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l = cv2.createCLAHE(clipLimit=1.4, tileGridSize=(8, 8)).apply(l)
    img = cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV).astype(np.float32)
    hsv[..., 1] *= 1.06
    np.clip(hsv[..., 1], 0, 255, out=hsv[..., 1])
    return cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)


def _deghost(frames: List[np.ndarray]) -> List[np.ndarray]:
    """Where a frame disagrees with the middle one once exposure is matched
    (a moving tree, a person, water), use the middle frame there, brightened
    or darkened to that frame's exposure - so fusion blends one moment."""
    cv2 = _cv2()
    ref_i = len(frames) // 2
    ref = frames[ref_i].astype(np.float32) / 255.0
    rl = ref.mean(axis=2)
    ok_ref = (rl > 0.05) & (rl < 0.92)
    out = []
    for i, f in enumerate(frames):
        if i == ref_i:
            out.append(f)
            continue
        cur = f.astype(np.float32) / 255.0
        cl = cur.mean(axis=2)
        both = ok_ref & (cl > 0.05) & (cl < 0.92)
        gain = float(np.median(cl[both] / np.maximum(rl[both], 1e-3))) if both.sum() > 500 else 1.0
        diff = np.abs(np.clip(rl * gain, 0, 1) - cl)
        valid = both.astype(np.float32)
        ghost = cv2.GaussianBlur(((diff > 0.12) * valid).astype(np.float32), (0, 0), max(2.0, f.shape[1] / 400))
        ghost = np.clip(ghost * 2.5, 0, 1)[..., None]
        if ghost.max() < 0.05:
            out.append(f)
            continue
        swap = np.clip(ref * gain, 0, 1)
        out.append(np.clip((cur * (1 - ghost) + swap * ghost) * 255.0, 0, 255).astype(np.uint8))
    return out


def fuse_look(frames: List[np.ndarray], look: "HdrLook", shutters: Optional[List[Optional[float]]] = None) -> np.ndarray:
    """Frames (BGR uint8, same size, lined up) -> the finished photo, float BGR 0..1."""
    cv2 = _cv2()
    if look.deghost and len(frames) > 2:
        frames = _deghost(frames)
    if look.method == "hdr":
        # a radiance map and a tone map: bolder, more "HDR". Exposure times
        # from EXIF, or worked out from how bright each frame is.
        order = sorted(range(len(frames)), key=lambda k: float(frames[k].mean()))
        frames = [frames[k] for k in order]
        sh = [shutters[k] if shutters and k < len(shutters) else None for k in order]
        if not all(sh):
            m = [max(1e-3, float(f.astype(np.float32).mean()) / 255.0) for f in frames]
            mid = m[len(m) // 2]
            sh = [(x / mid) ** 2.2 / 60.0 for x in m]
        times = np.array(sh, dtype=np.float32)
        hdr = cv2.createMergeDebevec().process(frames, times.copy())
        img = cv2.createTonemapReinhard(gamma=1.0, intensity=0.2, light_adapt=0.85, color_adapt=0.0).process(hdr)
        img = np.nan_to_num(img, nan=0.0, posinf=1.0, neginf=0.0)
        # Reinhard leaves it dim and gamma-light: bring the mid-tones where fusion puts them
        med = float(np.median(img)) or 0.2
        img = np.clip(img * (0.42 / max(med, 1e-3)), 0, 1) ** 0.85
    else:
        img = np.clip(cv2.createMergeMertens(1.0, 1.0, 1.0).process(frames), 0, 1)
    img = img.astype(np.float32)
    # windows: in the brightest areas, lean on the darkest frame (the view)
    if look.windows > 0:
        dark = min(frames, key=lambda f: float(f.mean())).astype(np.float32) / 255.0
        L = img.mean(axis=2)
        m = np.clip((L - 0.72) / 0.26, 0, 1)
        m = cv2.GaussianBlur(m, (0, 0), max(1.5, img.shape[1] / 700))[..., None] * float(look.windows)
        # keep the view's own contrast but lift it so a window still reads as bright
        dl = max(1e-3, float(dark.mean()))
        lift = np.clip(dark * min(3.0, 0.55 / dl), 0, 1)
        img = img * (1 - m) + np.clip(0.5 * dark + 0.5 * lift, 0, 1) * m
    return _finish(img, look)


def _finish(img: np.ndarray, look: "HdrLook") -> np.ndarray:
    """Shadows, contrast, detail, saturation, warmth - on float BGR 0..1."""
    cv2 = _cv2()
    if look.exposure:
        img = img * np.float32(2.0 ** float(look.exposure))
    L = img @ np.float32([0.0722, 0.7152, 0.2126])
    if look.shadows:
        k = float(look.shadows)
        gain = 1.0 + k * 1.2 * np.clip(1.0 - L / 0.5, 0, 1) ** 2
        img = img * gain[..., None]
    if look.contrast:
        c = float(look.contrast)
        x = np.clip(img, 0, 1)
        s_curve = x + c * 0.6 * (x - 0.5) * (1 - np.abs(2 * x - 1))
        img = s_curve
    if look.detail > 0:
        sigma = max(2.0, img.shape[1] / 120)
        blur = cv2.GaussianBlur(img, (0, 0), sigma)
        img = img + float(look.detail) * 0.9 * (img - blur)
    img = np.clip(img, 0, 1).astype(np.float32)
    if look.saturation:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        hsv[..., 1] = np.clip(hsv[..., 1] * (1 + float(look.saturation)), 0, 1)
        img = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
    if look.warmth:
        w = float(look.warmth) * 0.06
        img = img * np.float32([1 - w, 1.0, 1 + w])
    return np.clip(img, 0, 1).astype(np.float32)


def _fuse(paths: List[str], body: MergeBody) -> np.ndarray:
    cv2 = _cv2()
    loaded = [_load(p, body.max_dim) for p in paths]
    frames = [img for img, _ in loaded]

    degraded = [os.path.basename(p) for p, (_, how) in zip(paths, loaded) if how == "preview"]
    if degraded and not body.allow_preview_raw:
        raise RuntimeError(
            "no RAW decoder is installed, so only the small preview inside "
            f"{degraded[0]} could be read - fusing that would throw away most "
            "of the detail. Install one with:  venv/bin/pip install rawpy  "
            "(or tick 'fuse anyway' to accept the lower quality)")

    # Frames that differ in size (one shot portrait by mistake) cannot fuse.
    shape = frames[0].shape
    frames = [f for f in frames if f.shape == shape]
    if len(frames) < 2:
        raise RuntimeError("the frames in this set are not the same size")

    if body.align:
        try:
            cv2.createAlignMTB().process(frames, frames)
        except Exception:
            pass                            # tripod shots do not need it anyway

    if body.look is not None:
        shutters = [_exif_capture(p)[2] for p in paths]
        img = fuse_look(frames, body.look, shutters)
        if body.fmt == "tif" and body.bits16:
            return (img * 65535.0 + 0.5).astype(np.uint16)
        return (img * 255.0 + 0.5).astype(np.uint8)
    merged = cv2.createMergeMertens().process(frames)      # float32 0..1
    out = np.clip(merged * 255.0, 0, 255).astype(np.uint8)
    return _polish(out) if body.polish else out


def _align_edges(ref: np.ndarray, img: np.ndarray) -> np.ndarray:
    """Line img up with ref by their edges (which do not care how each frame
    was lit). Left as it is when the match is doubtful - a tripod shot needs
    nothing, and a wrong nudge is worse than none."""
    cv2 = _cv2()
    h, w = ref.shape[:2]
    k = min(1.0, 1200 / max(h, w))
    def edges(x):
        g = cv2.cvtColor(cv2.resize(x, (int(w * k), int(h * k)), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY).astype(np.float32)
        g = cv2.GaussianBlur(g, (0, 0), 1.2)
        e = cv2.magnitude(cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1))
        return e / (e.mean() + 1e-6)
    er, ei = edges(ref), edges(img)
    warp = np.eye(2, 3, dtype=np.float32)
    try:
        cc, warp = cv2.findTransformECC(er, ei, warp, cv2.MOTION_EUCLIDEAN,
                                        (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 60, 1e-5), None, 5)
    except cv2.error:
        return img
    if cc < 0.4 or abs(warp[0, 2]) > w * k * 0.05 or abs(warp[1, 2]) > h * k * 0.05:
        return img
    warp[:, 2] /= k
    if abs(warp[0, 2]) < 0.3 and abs(warp[1, 2]) < 0.3 and abs(warp[0, 1]) < 1e-4:
        return img
    return cv2.warpAffine(img, warp, (w, h), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_REPLICATE)


def _flambient(paths: List[str], flash_paths: List[str], body: MergeBody) -> np.ndarray:
    """Flash + ambient. The flash frames give true colour and clean light (and
    the view through the windows); the ambient frames give the room's mood
    and its own lamps. The flash frames are stacked "lighten" (each pop lit a
    different part of the room), then the ambient light is mixed into the
    luminosity only - never where the ambient frame is blown (the windows)."""
    cv2 = _cv2()
    amb_p = [p for p in paths if p not in flash_paths]
    fl_p = [p for p in paths if p in flash_paths]
    if not amb_p or not fl_p:
        raise RuntimeError("a flambient set needs at least one ambient and one flash frame - click a frame to switch it")
    frames = [_load(p, body.max_dim)[0] for p in amb_p + fl_p]
    shape = frames[0].shape
    frames = [f if f.shape == shape else cv2.resize(f, (shape[1], shape[0]), interpolation=cv2.INTER_AREA) for f in frames]
    if body.align:
        # not AlignMTB: it thresholds on brightness, and a flash frame and an
        # ambient frame are lit so differently that it lines up the light, not the room
        frames = [frames[0]] + [_align_edges(frames[0], f) for f in frames[1:]]
    amb, fl = frames[:len(amb_p)], frames[len(amb_p):]
    ambient = amb[0] if len(amb) == 1 else np.clip(cv2.createMergeMertens().process(amb) * 255, 0, 255).astype(np.uint8)
    flash = fl[0]
    for f in fl[1:]:
        flash = np.maximum(flash, f)
    la = cv2.cvtColor(ambient, cv2.COLOR_BGR2LAB).astype(np.float32)
    lf = cv2.cvtColor(flash, cv2.COLOR_BGR2LAB).astype(np.float32)
    m = float(body.ambient_mix)
    # ambient counts less where it is blown out (OpenCV's 8-bit L runs 0..255)
    La = la[..., 0]
    blown = np.clip((La - 215.0) / 35.0, 0, 1)
    blown = cv2.GaussianBlur(blown, (0, 0), max(2.0, shape[1] / 600))
    w = (m * (1.0 - blown))[..., None]
    out = lf.copy()
    out[..., :1] = lf[..., :1] * (1 - w) + la[..., :1] * w
    wc = (0.3 * m * (1.0 - blown))[..., None]
    out[..., 1:] = lf[..., 1:] * (1 - wc) + la[..., 1:] * wc
    img = cv2.cvtColor(np.clip(out, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)
    return _polish(img) if body.polish else img


def _panorama(paths: List[str], body: MergeBody) -> np.ndarray:
    """Frames stitched into one wide photo, cropped to a clean rectangle."""
    cv2 = _cv2()
    # nothing in the photos is pure black, so the stitcher's empty edge can be told apart
    frames = [np.maximum(_load(p, min(body.max_dim, 4000))[0], 1) for p in paths]
    st = cv2.Stitcher_create(cv2.Stitcher_PANORAMA)
    code, pano = st.stitch(frames)
    if code != cv2.Stitcher_OK or pano is None:
        why = {1: "the frames do not overlap enough (aim for about a third)",
               2: "the frames could not be lined up",
               3: "the camera moved too much between frames"}.get(int(code), f"error {code}")
        raise RuntimeError(f"could not stitch: {why}")
    # crop off the ragged black edge: shrink the box until it holds no gaps
    valid = (pano.max(axis=2) > 0).astype(np.uint8)
    valid = cv2.erode(valid, np.ones((3, 3), np.uint8))
    ys, xs = np.nonzero(valid)
    x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
    for _ in range(4000):
        sub = valid[y0:y1 + 1, x0:x1 + 1]
        if sub.all() or x1 - x0 < 20 or y1 - y0 < 20:
            break
        # take a line off whichever edge has the most gaps
        edges = [(sub[0] == 0).sum(), (sub[-1] == 0).sum(), (sub[:, 0] == 0).sum(), (sub[:, -1] == 0).sum()]
        k = int(np.argmax(edges))
        if k == 0: y0 += 1
        elif k == 1: y1 -= 1
        elif k == 2: x0 += 1
        else: x1 -= 1
    out = pano[y0:y1 + 1, x0:x1 + 1]
    return _polish(out) if body.polish else out


def _index(out_path: str, source: Video, db: Session):
    """Put the finished photo in the library, in a folder row of its own."""
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
        folder = IndexedFolder(path=parent_dir, name=Path(parent_dir).name,
                               relative_path=rel,
                               parent_id=parent.id if parent else None,
                               added_at=datetime.utcnow())
        db.add(folder)
        db.commit()
        db.refresh(folder)

    if db.query(Video.id).filter(Video.filepath == out_path).first():
        return
    name = os.path.basename(out_path)
    thumb_rel = None
    try:
        import indexer
        tname = indexer.thumb_name(out_path)
        thumbs = Path(_media_root) / "thumbnails" if _media_root else None
        if thumbs:
            thumbs.mkdir(parents=True, exist_ok=True)
            ok, _ = previews.make_image_jpeg(out_path, str(thumbs / tname), width=640)
            if ok:
                thumb_rel = f"/thumbnails/{tname}"
    except Exception:
        pass
    now = datetime.utcnow()
    db.add(Video(
        filename=name, filepath=out_path, file_size=os.path.getsize(out_path),
        duration=0.0, thumbnail_path=thumb_rel, folder_id=folder.id,
        media_type=get_media_type(name) or "photo", status=source.status or "raw",
        created_at=now, uploaded_at=now, uploaded_by="hdr",
    ))
    db.commit()


def _run(job_id: str, body: MergeBody):
    with _slots:
        db = SessionLocal()
        try:
            for n, ids in enumerate(body.groups):
                with _jobs_lock:
                    if JOBS[job_id].get("cancelled"):
                        break
                    JOBS[job_id]["current"] = f"Set {n + 1} of {len(body.groups)}"
                rows = db.query(Video).filter(Video.id.in_(ids)).all()
                rows.sort(key=lambda v: ids.index(v.id))
                paths, path_of = [], {}
                for v in rows:
                    real = _resolve(v.filepath)
                    if real and os.path.exists(real):
                        paths.append(real)
                        path_of[v.id] = real
                try:
                    if len(paths) < 2:
                        raise RuntimeError("fewer than two frames could be read")
                    if body.mode == "flambient":
                        fl_ids = set(body.flash[n]) if n < len(body.flash) else set()
                        fl_paths = [path_of[i] for i in fl_ids if i in path_of]
                        img = _flambient(paths, fl_paths, body)
                    elif body.mode == "panorama":
                        img = _panorama(paths, body)
                    else:
                        img = _fuse(paths, body)

                    middle = rows[len(rows) // 2]
                    # In a project, every stage has its own folder and the
                    # merge belongs in the HDR one - that is what makes the
                    # next step see only the fused frames. Outside a project,
                    # results land beside the originals as before.
                    out_dir = None
                    try:
                        import projects
                        out_dir = projects.stage_dir(db, paths[0], "hdr")
                    except Exception:
                        out_dir = None
                    if out_dir is None:
                        sub = body.subfolder or "HDR"
                        if sub == "HDR" and body.mode != "hdr":
                            sub = {"flambient": "Flambient", "panorama": "Panorama"}[body.mode]
                        out_dir = Path(paths[0]).parent / sub
                    out_dir.mkdir(parents=True, exist_ok=True)
                    stem = Path(middle.filename).stem
                    ext = ".jpg" if body.fmt == "jpg" else ".tif"
                    tag = {"flambient": "Flambient", "panorama": "Pano"}.get(body.mode, "HDR")
                    out_path = out_dir / f"{stem}_{tag}{ext}"
                    k = 2
                    while out_path.exists():
                        out_path = out_dir / f"{stem}_{tag} ({k}){ext}"
                        k += 1

                    cv2 = _cv2()
                    params = ([cv2.IMWRITE_JPEG_QUALITY, body.quality]
                              if body.fmt == "jpg" else [])
                    if not cv2.imwrite(str(out_path), img, params):
                        raise RuntimeError("could not write the finished photo")

                    if body.add_to_library:
                        try:
                            _index(str(out_path), middle, db)
                        except Exception as e:
                            db.rollback()
                            with _jobs_lock:
                                JOBS[job_id]["errors"].append(
                                    f"{out_path.name}: saved, but not added to the library ({e})")

                    with _jobs_lock:
                        made = db.query(Video.id).filter(Video.filepath == str(out_path)).first()
                        JOBS[job_id]["results"].append({
                            "path": str(out_path), "filename": out_path.name,
                            "frames": len(paths), "id": made[0] if made else None,
                            "folder_id": None,
                        })
                except Exception as e:
                    with _jobs_lock:
                        first = rows[0].filename if rows else f"set {n + 1}"
                        JOBS[job_id]["errors"].append(f"{first}: {e}")
                finally:
                    with _jobs_lock:
                        JOBS[job_id]["done"] = n + 1
        finally:
            db.close()
            with _jobs_lock:
                JOBS[job_id]["running"] = False
                JOBS[job_id]["finished_at"] = time.time()
                JOBS[job_id]["current"] = None


class PreviewBody(BaseModel):
    ids: List[int] = Field(..., min_length=2, max_length=MAX_GROUP)
    look: HdrLook = HdrLook()
    align: bool = True
    size: int = Field(1400, ge=400, le=2400)


_preview_frames: Dict[str, np.ndarray] = {}
_preview_lock = threading.Lock()


def _preview_frame(path: str, size: int) -> np.ndarray:
    st = os.stat(path)
    key = f"{path}|{st.st_mtime_ns}|{size}"
    with _preview_lock:
        if key in _preview_frames:
            return _preview_frames[key]
    img, _ = _load(path, size, fast=True)
    with _preview_lock:
        while len(_preview_frames) > 30:
            _preview_frames.pop(next(iter(_preview_frames)))
        _preview_frames[key] = img
    return img


@router.post("/preview")
def preview(body: PreviewBody, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """The set, fused with these settings, small - for the HDR panel to show
    while you move the sliders. The merge itself runs at full size."""
    from fastapi.responses import Response
    cv2 = _cv2()
    rows = {v.id: v for v in db.query(Video).filter(Video.id.in_(body.ids)).all()}
    paths = []
    for i in body.ids:
        v = rows.get(i)
        real = _resolve(v.filepath) if v else None
        if real and os.path.exists(real):
            paths.append(real)
    if len(paths) < 2:
        raise HTTPException(status_code=400, detail="A set needs at least two frames that can be read")
    frames = [_preview_frame(p, body.size) for p in paths]
    shape = frames[0].shape
    frames = [f if f.shape == shape else cv2.resize(f, (shape[1], shape[0]), interpolation=cv2.INTER_AREA) for f in frames]
    frames = [f.copy() for f in frames]
    if body.align:
        try:
            cv2.createAlignMTB().process(frames, frames)
        except Exception:
            pass
    shutters = [_exif_capture(p)[2] for p in paths]
    img = fuse_look(frames, body.look, shutters)
    ok, buf = cv2.imencode(".jpg", (img * 255 + 0.5).astype(np.uint8), [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        raise HTTPException(status_code=500, detail="Could not make the preview")
    return Response(content=buf.tobytes(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post("/merge")
def merge(body: MergeBody, current_user: User = Depends(get_current_user)):
    groups = [g for g in body.groups if len(g) >= 2]
    if not groups:
        raise HTTPException(status_code=400, detail="Nothing to fuse")
    body.groups = groups
    _purge_old()
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        JOBS[job_id] = {
            "id": job_id, "running": True, "done": 0, "total": len(groups),
            "current": "Starting…", "results": [], "errors": [],
            "started_at": time.time(), "finished_at": None, "cancelled": False,
        }
    threading.Thread(target=_run, args=(job_id, body), daemon=True).start()
    return {"job_id": job_id, "total": len(groups)}


@router.get("/jobs/{job_id}")
def job(job_id: str, current_user: User = Depends(get_current_user)):
    with _jobs_lock:
        state = JOBS.get(job_id)
        if not state:
            raise HTTPException(status_code=404, detail="No such job")
        return dict(state)


@router.post("/jobs/{job_id}/cancel")
def cancel(job_id: str, current_user: User = Depends(get_current_user)):
    with _jobs_lock:
        if job_id in JOBS:
            JOBS[job_id]["cancelled"] = True
    return {"status": "cancelling"}


def _purge_old():
    cutoff = time.time() - KEEP_HOURS * 3600
    with _jobs_lock:
        for jid in [k for k, v in JOBS.items()
                    if not v["running"] and (v.get("finished_at") or 0) < cutoff]:
            JOBS.pop(jid, None)


def install(app, media_root, resolve_media_path):
    global _media_root, _resolve
    _media_root = Path(media_root) if media_root else None
    _resolve = resolve_media_path
    app.include_router(router)


# --------------------------------------------------------------------------
# RAW support
# --------------------------------------------------------------------------

def raw_decoder_state() -> dict:
    """Is there anything here that can develop a RAW file properly?"""
    try:
        import rawpy  # noqa: F401
        return {"installed": True, "name": "rawpy"}
    except Exception:
        pass
    wheels = []
    try:
        here = Path(__file__).resolve().parent / "wheels"
        wheels = sorted(str(p) for p in here.glob("rawpy-*.whl"))
    except Exception:
        pass
    return {"installed": False, "name": None, "offline_wheel": bool(wheels)}


@router.get("/raw-support")
def raw_support(current_user: User = Depends(get_current_user)):
    return raw_decoder_state()


@router.post("/raw-support/install")
def install_raw_support(current_user: User = Depends(get_admin_user)):
    """Install the RAW decoder from here, so nobody has to open a terminal.

    Tries the wheel shipped next to the app first - that works with no
    internet at all - and falls back to PyPI. pip runs with the very
    interpreter this app is running on, so it lands in the right environment.
    """
    import subprocess
    import sys

    state = raw_decoder_state()
    if state["installed"]:
        return {"installed": True, "message": "RAW support is already installed"}

    here = Path(__file__).resolve().parent / "wheels"
    wheels = sorted(str(p) for p in here.glob("rawpy-*.whl")) if here.is_dir() else []
    attempts = ([[sys.executable, "-m", "pip", "install", "--no-index", wheels[-1]]] if wheels else [])
    attempts.append([sys.executable, "-m", "pip", "install", "rawpy"])

    log = []
    for cmd in attempts:
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            log.append((p.stdout or "")[-1500:] + (p.returncode and (p.stderr or "")[-1500:] or ""))
            if p.returncode == 0:
                break
        except Exception as e:
            log.append(str(e)[:500])

    # Check by asking for it, not by trusting pip's exit code.
    import importlib
    try:
        importlib.invalidate_caches()
        importlib.import_module("rawpy")
        ok = True
    except Exception as e:
        ok = False
        log.append(f"still cannot import rawpy: {str(e)[:300]}")

    if not ok:
        raise HTTPException(
            status_code=500,
            detail="Could not install RAW support automatically. " + (log[-1] if log else ""))
    return {"installed": True, "message": "RAW support installed - re-run the merge",
            "log": log[-1][-400:] if log else ""}

