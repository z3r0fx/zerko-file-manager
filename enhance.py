"""Enhance (replaces Topaz Photo AI / Gigapixel / DxO PureRAW's clean-up): AI models
run on this computer's graphics card, through the local ComfyUI.

  enlarge   - twice the size (Real-ESRGAN x4, brought down to 2x): drone stills, tight crops
  sharpen   - the same size, slight shake or softness restored (x4, brought back down to 1x)
  denoise   - the same size, noise of a dark interior taken away (SCUNet)

The photo goes through in tiles (with an overlap, blended), so a 24-megapixel
photo fits a 12 GB card. The result is a new file next to the photo
("<name> enlarged.jpg" ...), with the same edit copied onto it; the photo
itself is never changed. The two model files (67 MB and 72 MB, from their
authors' own GitHub releases) are fetched the first time they are needed.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import get_current_user
from database import IndexedFolder, SessionLocal, User, Video, get_db

router = APIRouter(prefix="/api/enhance", tags=["enhance"])

MODELS = {
    "x4": ("RealESRGAN_x4plus.pth", "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth", 67040989),
    "denoise": ("scunet_color_real_psnr.pth", "https://github.com/cszn/KAIR/releases/download/v1.0/scunet_color_real_psnr.pth", 71982841),
}
# what each job runs: the model, its own scale, and how much to scale its result
JOBS_KIND = {"enlarge": ("x4", 4, 0.5, "enlarged"), "sharpen": ("x4", 4, 0.25, "sharpened"), "denoise": ("denoise", 1, 1.0, "denoised")}
MAX_OUT = 16000                 # the longest side a result may have

_dl_lock = threading.Lock()


def model_dir() -> Path:
    import ai_image
    d = ai_image.LOCAL_ROOT / "ComfyUI" / "models" / "upscale_models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_model(key: str, say=None) -> str:
    """The model file in ComfyUI's folder, downloaded the first time (and checked by its size)."""
    name, url, size = MODELS[key]
    p = model_dir() / name
    with _dl_lock:
        if p.is_file() and p.stat().st_size == size:
            return name
        if say:
            say(f"Fetching the {key} model ({size // 1_000_000} MB, once)")
        tmp = p.with_suffix(".part")
        with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        if tmp.stat().st_size != size:
            tmp.unlink(missing_ok=True)
            raise HTTPException(status_code=502, detail=f"The {name} download was incomplete - try again")
        tmp.replace(p)
    return name


def _run_tile(model_name: str, tile: np.ndarray, scale_after: float) -> np.ndarray:
    """One tile through ComfyUI: the model, then brought to the wanted size. uint8 RGB in and out."""
    import json
    import urllib.parse
    import ai_image
    tag = uuid.uuid4().hex[:10]
    name = ai_image._comfy_upload(tile, f"zk_enh_{tag}.png")
    wf = {
        "1": {"class_type": "LoadImage", "inputs": {"image": name}},
        "2": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": model_name}},
        "3": {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["2", 0], "image": ["1", 0]}},
        "4": {"class_type": "SaveImage", "inputs": {"images": ["3", 0], "filename_prefix": f"zk_enh_{tag}"}},
    }
    d = ai_image._json(ai_image.comfy_url() + "/prompt", {"prompt": wf, "client_id": "zerko"})
    pid = d.get("prompt_id")
    if not pid:
        raise HTTPException(status_code=502, detail=f"ComfyUI refused the job: {json.dumps(d)[:300]}")
    t0 = time.time()
    while time.time() - t0 < 300:
        time.sleep(0.4)
        h = ai_image._json(ai_image.comfy_url() + f"/history/{pid}").get(pid)
        if not h:
            continue
        st = h.get("status") or {}
        if st.get("status_str") == "error":
            msgs = [m for m in st.get("messages", []) if m and m[0] == "execution_error"]
            raise HTTPException(status_code=502, detail="ComfyUI: " + ((msgs[0][1].get("exception_message") if msgs else "") or "error")[:300])
        for out in (h.get("outputs") or {}).values():
            for im in out.get("images", []):
                q = urllib.parse.urlencode({"filename": im["filename"], "subfolder": im.get("subfolder", ""), "type": im.get("type", "output")})
                res = ai_image.decode(ai_image._http(ai_image.comfy_url() + "/view?" + q, timeout=60))
                if scale_after != 1.0:
                    import cv2
                    res = cv2.resize(res, (max(1, round(res.shape[1] * scale_after)), max(1, round(res.shape[0] * scale_after))),
                                     interpolation=cv2.INTER_AREA)
                return res
    raise HTTPException(status_code=504, detail="ComfyUI took too long on one piece")


def process(rgb: np.ndarray, kind: str, progress=None, as_float: bool = False) -> np.ndarray:
    """The whole photo (uint8 RGB), tile by tile, blended where tiles overlap. as_float: 0..255 float32
    (for denoise, which is blended with the photo and dithered before it becomes 8-bit again)."""
    import ai_image
    key, model_scale, after, _word = JOBS_KIND[kind]
    if not ai_image.comfy_start():
        raise HTTPException(status_code=502, detail="The local image model (ComfyUI) is not installed or would not start: "
                                                    "Manage > AI > Image generation > My own PC.")
    name = ensure_model(key, progress)
    f = model_scale * after                               # the final scale of the photo
    H, W = rgb.shape[:2]
    tile = 512 if model_scale > 1 else 768
    ov = 32
    oh, ow = round(H * f), round(W * f)
    acc = np.zeros((oh, ow, 3), np.float32)
    wsum = np.zeros((oh, ow, 1), np.float32)
    ys = list(range(0, max(1, H - ov), tile - ov))
    xs = list(range(0, max(1, W - ov), tile - ov))
    total, n = len(ys) * len(xs), 0
    for y in ys:
        for x in xs:
            y1, x1 = min(H, y + tile), min(W, x + tile)
            y0, x0 = max(0, y1 - tile), max(0, x1 - tile)
            out = _run_tile(name, rgb[y0:y1, x0:x1], after).astype(np.float32)
            ty0, tx0 = round(y0 * f), round(x0 * f)
            th, tw = min(out.shape[0], oh - ty0), min(out.shape[1], ow - tx0)
            out = out[:th, :tw]
            # a soft ramp at the edges, so the seams between tiles do not show
            ry = np.minimum(np.arange(th) + 1, np.arange(th)[::-1] + 1).astype(np.float32)
            rx = np.minimum(np.arange(tw) + 1, np.arange(tw)[::-1] + 1).astype(np.float32)
            ramp = np.minimum(np.minimum(ry[:, None], rx[None, :]) / max(1.0, ov * f), 1.0)[..., None]
            acc[ty0:ty0 + th, tx0:tx0 + tw] += out * ramp
            wsum[ty0:ty0 + th, tx0:tx0 + tw] += ramp
            n += 1
            if progress:
                progress(f"{n} of {total} pieces", n / total)
    out = np.clip(acc / np.maximum(wsum, 1e-6), 0, 255)
    return out.astype(np.float32) if as_float else out.astype(np.uint8)


def blend_denoise(photo: np.ndarray, clean: np.ndarray, amount: float) -> np.ndarray:
    """The photo with `amount` (0..1) of the AI's noise removal: the rest of the photo's own grain stays,
    which also keeps smooth dark walls from breaking into bands. A little dither goes in before it becomes
    8-bit again (steps between neighbouring values were what showed as bands in the darks)."""
    a = float(np.clip(amount, 0.0, 1.0))
    p = photo.astype(np.float32)
    o = p + (clean.astype(np.float32) - p) * a
    rng = np.random.default_rng(7)
    o = o + (rng.random(o.shape[:2], np.float32) - rng.random(o.shape[:2], np.float32))[..., None] * 0.75
    return np.clip(o + 0.5, 0, 255).astype(np.uint8)


def _side(vid: int) -> Path:
    """Where a denoised copy keeps the AI's full result, to be blended again at another strength."""
    import photo_edit as pe
    d = Path(pe._media_root or ".") / ".enhance"
    d.mkdir(parents=True, exist_ok=True)
    return d / str(int(vid))


# --------------------------------------------------------------------------
# jobs: one or many photos, each result saved next to its photo
# --------------------------------------------------------------------------

JOBS: Dict[str, dict] = {}
_lock = threading.Lock()
_gpu = threading.Lock()


def _save(db: Session, v: Video, rgb: np.ndarray, word: str, user: str) -> int:
    """The result as a new photo in the same folder, with the photo's edit copied onto it."""
    import cv2
    import ai_photo
    import ingest
    import photo_edit as pe
    src = Path(pe._resolve(v.filepath))
    stem = src.stem
    ext = ".jpg" if src.suffix.lower() in (".jpg", ".jpeg", ".png", ".heic", ".webp") else ".tif"
    out = src.with_name(f"{stem} {word}{ext}")
    k = 2
    while out.exists():
        out = src.with_name(f"{stem} {word} {k}{ext}")
        k += 1
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    if ext == ".jpg":
        cv2.imwrite(str(out), bgr, [cv2.IMWRITE_JPEG_QUALITY, 95])
        try:                                             # keep the camera's details (date, lens, GPS)
            from PIL import Image
            with Image.open(src) as im:
                exif = im.info.get("exif")
            if exif:
                with Image.open(out) as im2:
                    im2.load()
                    im2.save(out, "JPEG", quality=95, exif=exif)
        except Exception:
            pass
    else:
        cv2.imwrite(str(out), bgr)
    folder = db.query(IndexedFolder).filter(IndexedFolder.id == v.folder_id).first()
    vid = ingest._add_to_library(db, folder, out, user) if folder else None
    if vid:
        rec = ai_photo.saved_recipe(db, v.id)
        if rec:
            ai_photo.save_recipe(db, vid, rec)
    return vid


def _run(j: dict, ids: List[int], kind: str, amount: float = 0.7):
    import photo_edit as pe
    db = SessionLocal()
    try:
        with _gpu:
            for n, vid in enumerate(ids):
                if j.get("cancel"):
                    break
                v = db.query(Video).filter(Video.id == vid).first()
                path = pe._resolve(v.filepath) if v else None
                if not path or not os.path.exists(path):
                    j["errors"].append(f"Photo {vid}: not on the drive")
                    continue
                j["current"] = v.filename
                try:
                    rgb = pe._read_rgb(path, max_dim=100000)
                    u8 = (np.clip(rgb, 0, 1) * 255 + 0.5).astype(np.uint8)
                    f = JOBS_KIND[kind][1] * JOBS_KIND[kind][2]
                    if max(u8.shape[:2]) * f > MAX_OUT:
                        raise HTTPException(status_code=400, detail=f"too big to enlarge (the result would be over {MAX_OUT} px)")

                    def say(msg, frac=None, n=n):
                        j["step"] = msg
                        if frac is not None:
                            j["done"] = n + frac
                    if kind == "denoise":
                        clean = process(u8, kind, say, as_float=True)
                        out = blend_denoise(u8, clean, amount)
                    else:
                        clean, out = None, process(u8, kind, say)
                    new = _save(db, v, out, JOBS_KIND[kind][3], j["user"])
                    if new:
                        j["made"].append(new)
                        if clean is not None:
                            import cv2
                            import json as _json
                            side = _side(new)
                            cv2.imwrite(str(side) + ".png", cv2.cvtColor(np.clip(clean + 0.5, 0, 255).astype(np.uint8), cv2.COLOR_RGB2BGR),
                                        [cv2.IMWRITE_PNG_COMPRESSION, 3])
                            (Path(str(side) + ".json")).write_text(_json.dumps({"src": v.id, "kind": kind, "amount": amount}))
                except HTTPException as e:
                    j["errors"].append(f"{v.filename}: {e.detail}")
                except Exception as e:
                    j["errors"].append(f"{v.filename}: {e}"[:300])
                j["done"] = n + 1
        j["state"] = "done"
    except Exception as e:
        j["state"], j["error"] = "error", str(e)[:300]
    finally:
        db.close()


class EnhanceBody(BaseModel):
    video_ids: List[int] = Field(..., min_length=1, max_length=500)
    kind: str = Field(..., pattern="^(enlarge|sharpen|denoise)$")
    amount: float = Field(0.7, ge=0.0, le=1.0)          # denoise: how much of the AI's clean-up


@router.post("")
def start(body: EnhanceBody, current_user: User = Depends(get_current_user)):
    if current_user.role == "viewer":
        raise HTTPException(status_code=403, detail="Your account has view-only access.")
    j = {"id": uuid.uuid4().hex[:12], "state": "running", "kind": body.kind, "done": 0, "total": len(body.video_ids),
         "step": "Starting", "current": "", "made": [], "errors": [], "error": "", "user": current_user.username}
    with _lock:
        JOBS[j["id"]] = j
    threading.Thread(target=_run, args=(j, body.video_ids, body.kind, body.amount), daemon=True).start()
    return j


@router.get("/copy/{video_id}")
def copy_info(video_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """A denoised copy: which photo it came from and how strong it is (nothing when it is not one)."""
    import json as _json
    f = Path(str(_side(video_id)) + ".json")
    if not f.is_file() or not Path(str(_side(video_id)) + ".png").is_file():
        return {"kind": None}
    d = _json.loads(f.read_text())
    src = db.query(Video).filter(Video.id == d.get("src")).first()
    return {**d, "src_name": src.filename if src else None, "src_there": bool(src)}


class AmountBody(BaseModel):
    amount: float = Field(..., ge=0.0, le=1.0)


@router.post("/copy/{video_id}/amount")
def copy_amount(video_id: int, body: AmountBody, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Blend a denoised copy again at another strength, from the original and the AI's kept result - no
    new run. The copy's file is rewritten (its edit stays); 0 gives the original's own grain back."""
    import cv2
    import json as _json
    import photo_edit as pe
    if current_user.role == "viewer":
        raise HTTPException(status_code=403, detail="Your account has view-only access.")
    side = _side(video_id)
    meta = Path(str(side) + ".json")
    if not meta.is_file():
        raise HTTPException(status_code=404, detail="This photo is not a denoised copy")
    d = _json.loads(meta.read_text())
    v = db.query(Video).filter(Video.id == video_id).first()
    src = db.query(Video).filter(Video.id == d.get("src")).first()
    if not v or not src:
        raise HTTPException(status_code=404, detail="The original photo is no longer in the library")
    out_path, src_path = pe._resolve(v.filepath), pe._resolve(src.filepath)
    clean = cv2.imread(str(side) + ".png", cv2.IMREAD_COLOR)
    if clean is None or not out_path or not src_path or not os.path.exists(src_path):
        raise HTTPException(status_code=404, detail="The original or the kept result is gone")
    u8 = (np.clip(pe._read_rgb(src_path, max_dim=100000), 0, 1) * 255 + 0.5).astype(np.uint8)
    clean = cv2.cvtColor(clean, cv2.COLOR_BGR2RGB)
    if clean.shape != u8.shape:
        raise HTTPException(status_code=409, detail="The original has changed size since - run the noise removal again")
    out = blend_denoise(u8, clean, body.amount)
    bgr = cv2.cvtColor(out, cv2.COLOR_RGB2BGR)
    tmp = out_path + ".part.jpg" if out_path.lower().endswith((".jpg", ".jpeg")) else out_path + ".part.tif"
    cv2.imwrite(tmp, bgr, [cv2.IMWRITE_JPEG_QUALITY, 95] if tmp.endswith(".jpg") else [])
    os.replace(tmp, out_path)
    v.file_size = os.path.getsize(out_path)
    if v.thumbnail_path:
        try:
            import indexer
            tdir = Path(pe._media_root or Path(out_path).parent) / "thumbnails"
            indexer.make_thumbnail(out_path, str(tdir / Path(v.thumbnail_path).name), v.media_type)
        except Exception:
            pass
    db.commit()
    meta.write_text(_json.dumps({**d, "amount": body.amount}))
    return {"ok": True, "amount": body.amount}


@router.get("/{job_id}")
def job(job_id: str, current_user: User = Depends(get_current_user)):
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="No such job")
    return j


@router.post("/{job_id}/cancel")
def cancel(job_id: str, current_user: User = Depends(get_current_user)):
    j = JOBS.get(job_id)
    if j:
        j["cancel"] = True
    return {"ok": True}


# --------------------------------------------------------------------------
# fill the corners: straightened without zooming in, the empty wedges made up
# --------------------------------------------------------------------------

GEO_RESET = {"crop": [0, 0, 1, 1], "rotate": 0, "flip_h": False, "flip_v": False, "straighten": 0.0, "persp_v": 0.0,
             "persp_h": 0.0, "geo_scale": 1.0, "distortion": 0.0, "distortion2": 0.0}


def fill_corners(rgb: np.ndarray, rec: dict) -> tuple:
    """The photo with its straightening and lens correction done at full frame (no zoom), and the
    corners that falls outside the photo filled from their surroundings. uint8 in, (uint8, share filled) out."""
    import cv2
    import photo_edit as pe
    import remove_ai
    r = pe.Recipe(**{**rec, "crop": [0, 0, 1, 1], "geo_scale": 1.0})
    H, W = rgb.shape[:2]
    turned = int(r.rotate or 0) % 180 == 90
    OW, OH = (H, W) if turned else (W, H)
    yy, xx = np.mgrid[0:OH, 0:OW].astype(np.float32)
    u, v = pe.source_uv((xx + 0.5) / OW, (yy + 0.5) / OH, r, OW / OH)
    out = cv2.remap(rgb, (u * W - 0.5).astype(np.float32), (v * H - 0.5).astype(np.float32), cv2.INTER_LANCZOS4,
                    borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    hole = ((u < 0) | (u > 1) | (v < 0) | (v > 1)).astype(np.uint8)
    share = float(hole.mean())
    if share == 0:
        return out, 0.0
    hole = cv2.dilate(hole, np.ones((5, 5), np.uint8)) > 0
    # the thin parts of the gap (a few pixels from the photo) are carried on smoothly from the edge next to them;
    # the model then only has the thicker wedges, with real picture right beside them
    dist = cv2.distanceTransform(hole.astype(np.uint8), cv2.DIST_L2, 5)
    thin_px = max(6.0, 0.015 * min(OH, OW))
    thin = hole & (dist <= thin_px)
    if thin.any():
        k = min(1.0, 2400.0 / max(OH, OW))
        sw, sh = max(1, round(OW * k)), max(1, round(OH * k))
        small = cv2.resize(out, (sw, sh), interpolation=cv2.INTER_AREA)
        sm = cv2.resize(hole.astype(np.uint8), (sw, sh), interpolation=cv2.INTER_NEAREST)
        smooth = cv2.inpaint(small, sm * 255, 5, cv2.INPAINT_TELEA)
        smooth = cv2.resize(smooth, (OW, OH), interpolation=cv2.INTER_CUBIC)
        # no black is left anywhere (the model took a black neighbour for real picture); the thick parts get
        # proper texture from the model below
        out = np.where(hole[..., None], smooth, out)
    img = out.astype(np.float32) / 255.0
    # a gap that is only a thin strip keeps the smooth fill; a thick wedge is filled whole by the model,
    # edge included, so it works from the real photo round it (and never from black: that is all filled)
    n, lab = cv2.connectedComponents(hole.astype(np.uint8))
    for i in range(1, n):
        part = lab == i
        if part.sum() < 64 or float(dist[part].max()) <= thin_px * 1.5:
            continue
        try:
            patch, (x0, y0, x1, y1), _eng = remove_ai.fill(img, part, feather_px=2.0)
        except ValueError:
            continue
        a = patch[..., 3:4]
        img[y0:y1, x0:x1] = img[y0:y1, x0:x1] * (1 - a) + patch[..., :3] * a
    return (np.clip(img, 0, 1) * 255 + 0.5).astype(np.uint8), share


class CornersBody(BaseModel):
    video_id: int


@router.post("/fill-corners")
def fill_corners_route(body: CornersBody, current_user: User = Depends(get_current_user)):
    """A new photo next to this one: straightened at full frame with the corners filled, and this photo's
    light and colour on it (its crop, masks painted on it and patches belong to the old framing)."""
    if current_user.role == "viewer":
        raise HTTPException(status_code=403, detail="Your account has view-only access.")
    j = {"id": uuid.uuid4().hex[:12], "state": "running", "kind": "corners", "done": 0, "total": 1, "step": "Straightening at full frame",
         "current": "", "made": [], "errors": [], "error": "", "user": current_user.username}
    JOBS[j["id"]] = j

    def go():
        import ai_photo
        import photo_edit as pe
        db = SessionLocal()
        try:
            v = db.query(Video).filter(Video.id == body.video_id).first()
            path = pe._resolve(v.filepath) if v else None
            if not path or not os.path.exists(path):
                raise HTTPException(status_code=404, detail="The photo is not on the drive")
            rec = ai_photo.saved_recipe(db, v.id)
            if not any(float(rec.get(k) or 0) for k in ("straighten", "persp_v", "persp_h", "distortion", "distortion2")):
                raise HTTPException(status_code=400, detail="This photo is not straightened or lens-corrected, so it has no empty corners")
            rgb = pe._read_rgb(path, max_dim=100000)
            u8 = (np.clip(rgb, 0, 1) * 255 + 0.5).astype(np.uint8)
            with pe._remove_lock:
                out, share = fill_corners(u8, rec)
            j["step"] = f"Filled {share * 100:.1f}% of the frame"
            new = _save(db, v, out, "full frame", j["user"])
            if new:
                keep = {k: val for k, val in rec.items()
                        if k not in ("spots", "removes", "gen_ref", "gen_amount", "gen_light", "gen_view", "gen_look", "gen_geo")}
                keep.update(GEO_RESET)
                keep["masks"] = [m for m in rec.get("masks", []) if m.get("kind") in ("radial", "linear", "colour")]
                ai_photo.save_recipe(db, new, keep)
                j["made"].append(new)
            j["done"] = 1
            j["state"] = "done"
        except HTTPException as e:
            j["state"], j["error"] = "error", str(e.detail)
        except Exception as e:
            j["state"], j["error"] = "error", str(e)[:300]
        finally:
            db.close()
    threading.Thread(target=go, daemon=True).start()
    return j


def install(app):
    app.include_router(router)
