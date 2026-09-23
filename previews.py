"""Thumbnails and browser-viewable previews.

Two jobs, one module, because they fail for the same reasons:

  * thumbnails for the grid - a small JPEG of a video frame or a photo
  * previews for the photo viewer - browsers cannot draw camera RAW (DNG, ARW,
    CR2, NEF ...), HEIC or most TIFFs, so opening one showed a broken image.
    A JPEG is made once, cached, and shown instead; the original is untouched
    and is still what "download" gives you.

Each function tries several methods and returns (ok, reason). The reason is the
last thing that went wrong, in words a person can act on - "the file looks
incomplete" is a very different problem from "ffmpeg is not installed", and a
button that silently does nothing helps with neither.

Nothing here modifies the source file. Output is written to a temporary name
and renamed, so a half-written or empty file is never mistaken for a thumbnail.
"""

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Tuple

# Formats every browser can show as they are.
BROWSER_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
# Camera RAW: needs decoding (or its embedded preview extracted).
RAW_EXT = {".dng", ".arw", ".cr2", ".cr3", ".nef", ".raf", ".orf", ".rw2"}

FFMPEG_TIMEOUT = 120


def _ok_file(path: str) -> bool:
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def _tail(text: bytes, n: int = 600) -> str:
    """The end of ffmpeg's complaint. Kept whole rather than to one line: the
    line that explains the problem is often not the last one."""
    return (text or b"").decode("utf-8", "replace").strip()[-n:]


def explain_ffmpeg_error(stderr: str) -> str:
    """Turn ffmpeg's complaint into something a person can act on."""
    low = (stderr or "").lower()
    if "moov atom not found" in low:
        return ("the file looks incomplete (its index is missing) - it was probably "
                "cut short while being copied or uploaded. Re-copy it from the original.")
    if "no such file" in low:
        return "the file is not where the catalog expects it"
    if "permission denied" in low:
        return "the file cannot be read (permission denied)"
    if "invalid data found when processing input" in low:
        return "the file is damaged or in a format ffmpeg cannot read"
    if "could not find codec parameters" in low or "no video" in low:
        return "no readable video was found in the file"
    return stderr.strip()[:200] or "ffmpeg could not read the file"


def _run_ffmpeg(args: list, out: str) -> Tuple[bool, str]:
    """Run ffmpeg into a temp file beside `out`, then move it into place."""
    out_p = Path(out)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".jpg", prefix=".part-", dir=str(out_p.parent))
    os.close(fd)
    try:
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args, tmp],
                           capture_output=True, timeout=FFMPEG_TIMEOUT)
        if r.returncode == 0 and _ok_file(tmp):
            os.replace(tmp, out)
            return True, ""
        return False, _tail(r.stderr)
    except FileNotFoundError:
        return False, "ffmpeg is not installed (sudo apt install ffmpeg)"
    except subprocess.TimeoutExpired:
        return False, "ffmpeg timed out reading the file"
    except Exception as e:                                   # pragma: no cover
        return False, str(e)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------- video thumbnails
def make_video_thumb(src: str, out: str, width: int = 640) -> Tuple[bool, str]:
    """A frame from a video, trying progressively more forgiving methods."""
    if not os.path.exists(src):
        return False, "the file is not where the catalog expects it"
    if os.path.getsize(src) == 0:
        return False, "the file is empty"

    # format=yuvj420p: JPEG needs full-range 8-bit; 10-bit ProRes/HEVC and
    # alpha clips otherwise depend on ffmpeg's automatic conversion, which
    # differs between builds.
    vf = f"scale={width}:-2,format=yuvj420p"
    strategies = [
        ["-ss", "1", "-i", src, "-frames:v", "1", "-vf", vf, "-q:v", "4"],
        # very short clips have nothing at one second
        ["-i", src, "-frames:v", "1", "-vf", vf, "-q:v", "4"],
        # odd headers: look further into the file before giving up
        ["-probesize", "100M", "-analyzeduration", "100M", "-i", src,
         "-map", "0:v:0", "-frames:v", "1", "-vf", vf, "-q:v", "4"],
    ]
    seen = ""
    for args in strategies:
        ok, why = _run_ffmpeg(args, out)
        if ok:
            return True, ""
        seen += (why or "") + "\n"

    # OpenCV reads some files ffmpeg's command line will not.
    try:
        import cv2
        cap = cv2.VideoCapture(src)
        ok, frame = cap.read()
        cap.release()
        if ok and frame is not None:
            h, w = frame.shape[:2]
            if w > width:
                frame = cv2.resize(frame, (width, max(2, int(h * width / w))))
            fd, tmp = tempfile.mkstemp(suffix=".jpg", prefix=".part-", dir=str(Path(out).parent))
            os.close(fd)
            if cv2.imwrite(tmp, frame) and _ok_file(tmp):
                os.replace(tmp, out)
                return True, ""
            if os.path.exists(tmp):
                os.remove(tmp)
    except Exception:
        pass
    return False, explain_ffmpeg_error(seen)


# ---------------------------------------------------------------- photos
def _save_jpeg(img, out: str, width: int) -> Tuple[bool, str]:
    from PIL import Image
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        bg.paste(rgba, mask=rgba.split()[3])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    if img.width > width:
        img = img.resize((width, max(1, round(img.height * width / img.width))),
                         Image.Resampling.LANCZOS)
    fd, tmp = tempfile.mkstemp(suffix=".jpg", prefix=".part-", dir=str(Path(out).parent))
    os.close(fd)
    try:
        img.save(tmp, "JPEG", quality=88)
        if not _ok_file(tmp):
            return False, "the image could not be written"
        os.replace(tmp, out)
        return True, ""
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _via_rawpy(src: str, out: str, width: int) -> Tuple[bool, str]:
    """Camera RAW: use the camera's own embedded JPEG preview when it is big
    enough, otherwise decode the sensor data at half size."""
    try:
        import rawpy
    except ImportError:
        return False, "rawpy is not installed"
    from PIL import Image, ImageOps
    import io
    try:
        with rawpy.imread(src) as raw:
            img = None
            try:
                thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    cand = Image.open(io.BytesIO(thumb.data))
                    cand.load()
                    if cand.width >= min(width, 1200):
                        img = ImageOps.exif_transpose(cand)
                elif thumb.format == rawpy.ThumbFormat.BITMAP:
                    img = Image.fromarray(thumb.data)
            except Exception:
                img = None
            if img is None:
                rgb = raw.postprocess(use_camera_wb=True, half_size=True, no_auto_bright=False)
                img = Image.fromarray(rgb)
        return _save_jpeg(img, out, width)
    except Exception as e:
        return False, f"could not decode this RAW file ({str(e)[:120]})"


def _via_embedded_jpeg(src: str, out: str, width: int) -> Tuple[bool, str]:
    """Camera RAW with no decoder installed: the camera's own JPEG preview.

    Every RAW format carries one or more ordinary JPEGs inside it, rendered by
    the camera with its own colour. Without rawpy this is the only way to get
    the colours right: ffmpeg and Pillow read the sensor data (or a lossless
    JPEG of it) with no demosaic or colour matrix, which is what made DJI DNG
    files come out bright green. Lossless-JPEG sensor data does not decode as
    an ordinary JPEG, so it is skipped by construction.
    """
    import io
    from PIL import Image
    try:
        with open(src, "rb") as f:
            data = f.read()
    except OSError as e:
        return False, f"could not read the file ({e})"
    best = None                          # (pixels, offset)
    start, tries = 0, 0
    view = memoryview(data)
    while tries < 40:
        i = data.find(b"\xff\xd8\xff", start)
        if i < 0:
            break
        tries += 1
        start = i + 3
        try:
            # Pillow reads from the start of whatever it is given, so each
            # candidate gets a stream that begins at its own marker.
            im = Image.open(io.BytesIO(view[i:]))
            if im.format == "JPEG" and im.width >= 320 and im.height >= 200:
                px = im.width * im.height
                if best is None or px > best[0]:
                    best = (px, i)
        except Exception:
            continue
    if best is None:
        return False, "no usable preview inside this RAW file"
    try:
        with Image.open(io.BytesIO(view[best[1]:])) as im:
            im.load()
            return _save_jpeg(im, out, width)
    except Exception as e:
        return False, f"the RAW file's preview could not be read ({str(e)[:120]})"


def camera_jpeg(src: str):
    """When the RAW itself cannot be decoded (a camera newer than LibRaw, like
    the Nikon ZR's High Efficiency NEF), most cameras still store a full-size
    JPEG of every shot inside the file. Returns (rgb uint8 array, full) where
    full means it is the whole sensor's size - good enough to merge HDR and to
    edit - or (None, False)."""
    import io
    import numpy as np
    from PIL import Image
    try:
        with open(src, "rb") as f:
            data = f.read()
    except OSError:
        return None, False
    view = memoryview(data)
    best = None
    start, tries = 0, 0
    while tries < 40:
        i = data.find(b"\xff\xd8\xff", start)
        if i < 0:
            break
        tries += 1
        start = i + 3
        try:
            im = Image.open(io.BytesIO(view[i:]))
            if im.format == "JPEG" and im.width >= 320:
                if best is None or im.width * im.height > best[0]:
                    best = (im.width * im.height, i)
        except Exception:
            continue
    if best is None:
        return None, False
    sensor_long, flip = 0, 0
    try:
        import rawpy
        with rawpy.imread(src) as raw:
            sensor_long = max(raw.sizes.width, raw.sizes.height)
            flip = int(raw.sizes.flip or 0)
    except Exception:
        pass
    try:
        with Image.open(io.BytesIO(view[best[1]:])) as im:
            im.load()
            rgb = np.asarray(im.convert("RGB"))
    except Exception:
        return None, False
    # the embedded JPEG is stored the way the sensor lies; turn it like the RAW would be
    if flip == 3:
        rgb = rgb[::-1, ::-1]
    elif flip == 5:
        rgb = np.rot90(rgb, 1)
    elif flip == 6:
        rgb = np.rot90(rgb, -1)
    long_edge = max(rgb.shape[:2])
    full = long_edge >= (0.9 * sensor_long if sensor_long else 3800)
    return np.ascontiguousarray(rgb), full


def raw_decode_note(src: str) -> str:
    """Why a RAW could not be developed, in words for the person looking at it."""
    try:
        import rawpy  # noqa: F401
    except Exception:
        return "No RAW decoder is installed (venv/bin/pip install rawpy)."
    return (f"{Path(src).name} is a RAW this version of LibRaw cannot decode yet "
            "(newer cameras, like the Nikon ZR's High Efficiency NEF). Set the camera to "
            "Lossless compressed RAW, or convert the files with Adobe DNG Converter.")


def _via_pillow(src: str, out: str, width: int) -> Tuple[bool, str]:
    from PIL import Image, ImageOps
    try:
        try:                                    # HEIC/HEIF, if the plugin is there
            import pillow_heif
            pillow_heif.register_heif_opener()
        except Exception:
            pass
        with Image.open(src) as img:
            img.load()
            img = ImageOps.exif_transpose(img)
            return _save_jpeg(img, out, width)
    except Exception as e:
        return False, f"Pillow could not open it ({str(e)[:120]})"


def _via_ffmpeg_image(src: str, out: str, width: int) -> Tuple[bool, str]:
    vf = f"scale='min({width},iw)':-2,format=yuvj420p"
    ok, why = _run_ffmpeg(["-i", src, "-frames:v", "1", "-vf", vf, "-q:v", "3"], out)
    return ok, explain_ffmpeg_error(why) if not ok else ""


def make_image_jpeg(src: str, out: str, width: int = 640) -> Tuple[bool, str]:
    """A JPEG of any photo format we can decode, at most `width` wide."""
    if not os.path.exists(src):
        return False, "the file is not where the catalog expects it"
    if os.path.getsize(src) == 0:
        return False, "the file is empty"
    ext = Path(src).suffix.lower()
    # For RAW, ffmpeg goes last: it reads the sensor data with no colour
    # processing, which is a green picture - better than nothing, but only just.
    order = ([_via_rawpy, _via_embedded_jpeg, _via_ffmpeg_image] if ext in RAW_EXT
             else [_via_pillow, _via_ffmpeg_image])
    reasons = []
    for fn in order:
        ok, why = fn(src, out, width)
        if ok:
            return True, ""
        if why and "not installed" not in why:
            reasons.append(why)
    if ext in RAW_EXT and not reasons:
        reasons.append("no RAW decoder is available - install one with: "
                       "pip install rawpy")
    return False, reasons[0] if reasons else "could not read this image"


def make_thumbnail(src: str, out: str, media_type: str, width: int = 640) -> Tuple[bool, str]:
    if media_type == "video":
        return make_video_thumb(src, out, width)
    return make_image_jpeg(src, out, width)
