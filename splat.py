"""3D fly-throughs: a drone orbit becomes a 3D scene you can fly through.

The pipeline, all on this computer's own graphics card:
  1. frames from the video (ffmpeg),
  2. where the drone was for each frame (COLMAP, the Windows CUDA build),
  3. the scene as Gaussian splats (Brush, Windows build, uses the card through
     DirectX/Vulkan - nothing to compile),
  4. the web page flies a camera through it and records the flight as a
     video, which comes back here and is saved next to the drone clip.

The tools live on the Windows side (C:\\Users\\<you>\\zerko-3d, installed by
tools/imagegen/install_3d.sh); Zerko runs in WSL and calls them there, so the
work folders are on the Windows disk too (fast for them, and no \\\\wsl paths).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from auth import get_current_user
from database import SessionLocal, User, Video

router = APIRouter(prefix="/api/splats", tags=["3d"])
HERE = Path(__file__).resolve().parent
INSTALLER = HERE / "tools" / "imagegen" / "install_3d.sh"
INSTALL_LOG = HERE / "tools" / "imagegen" / "install_3d.log"
_resolve = None
_lock = threading.Lock()
_running: dict = {}
_gpu = threading.Lock()


def _win_home() -> Optional[Path]:
    try:
        w = subprocess.run(["cmd.exe", "/c", "echo %USERPROFILE%"], capture_output=True, text=True, timeout=15).stdout.strip()
        if w:
            return Path(subprocess.run(["wslpath", "-u", w], capture_output=True, text=True, timeout=10).stdout.strip())
    except Exception:
        pass
    return None


_tools_cache: dict = {}


def tools() -> dict:
    """{"root", "colmap", "brush"} once installed, else {}."""
    if _tools_cache.get("t") and time.time() - _tools_cache.get("at", 0) < 60:
        return _tools_cache["t"]
    t = {}
    root = Path(os.environ.get("ZK_3D_HOME") or "")
    if not str(root) or str(root) == ".":
        h = _win_home()
        root = (h / "zerko-3d") if h else Path.home() / "zerko-3d"
    try:
        t = json.loads((root / "zerko.json").read_text())
        if not (Path(t.get("colmap", "")).is_file() and Path(t.get("brush", "")).is_file()):
            t = {}
    except Exception:
        t = {}
    _tools_cache.update(t=t, at=time.time())
    return t


def work_dir() -> Path:
    t = tools()
    if not t:
        raise HTTPException(status_code=400, detail="The 3D tools are not installed yet: Manage > AI > 3D fly-throughs > Install.")
    d = Path(t["root"]) / "work"
    d.mkdir(parents=True, exist_ok=True)
    return d


def win(p: Path) -> str:
    return subprocess.run(["wslpath", "-w", str(p)], capture_output=True, text=True, timeout=10).stdout.strip()


# the live Zerko and the test copy share the tools and the work folder: each only
# sees (and carries on) the scenes it made
_ME = __import__("hashlib").sha1(os.environ.get("DATABASE_URL", "sqlite:///./mediamanager.db").encode()).hexdigest()[:10]


def _mine(m: dict) -> bool:
    return m.get("server") == _ME or ("server" not in m and bool(os.environ.get("ZK_TEST_COPY")))


def _meta(sid: str) -> Path:
    return work_dir() / sid / "meta.json"


def _load(sid: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{12}", sid):
        raise HTTPException(status_code=400, detail="Bad id")
    try:
        m = json.loads(_meta(sid).read_text())
    except Exception:
        raise HTTPException(status_code=404, detail="That 3D scene is gone")
    if not _mine(m):
        raise HTTPException(status_code=404, detail="That 3D scene is gone")
    return m


def _save(m: dict):
    p = _meta(m["id"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, indent=1))
    os.replace(tmp, p)


def _run(args: List[str], log: Path, timeout: float = 7200) -> int:
    with open(log, "a") as f:
        f.write(f"\n$ {' '.join(args)}\n")
        f.flush()
        p = subprocess.run(args, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=timeout)
    return p.returncode


def _brush_running(sid: str) -> bool:
    """A Brush still training this scene (Zerko restarted while it ran: Windows
    programs keep going when the server stops)."""
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                            "@(Get-CimInstance Win32_Process -Filter \"Name='brush_app.exe'\" | "
                            f"Where-Object {{ $_.CommandLine -like '*{sid}*' }}).Count"],
                           capture_output=True, text=True, timeout=30)
        return (r.stdout or "").strip().splitlines()[-1:] not in ([], ["0"])
    except Exception:
        return False


def _card_busy(sid: str) -> bool:
    """COLMAP or Brush at work on another scene - perhaps from the other Zerko (the
    live one and the test copy share the card, but not each other's lock)."""
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                            "@(Get-CimInstance Win32_Process -Filter \"Name='brush_app.exe' or Name='colmap.exe'\" | "
                            f"Where-Object {{ $_.CommandLine -notlike '*{sid}*' }}).Count"],
                           capture_output=True, text=True, timeout=30)
        return (r.stdout or "").strip().splitlines()[-1:] not in ([], ["0"])
    except Exception:
        return False


def _wait_for_card(m: dict):
    if not _card_busy(m["id"]):
        return
    stage, progress = m.get("stage"), m.get("progress", 0)
    print(f"splat: {m['id']}: another scene is using the graphics card, waiting", flush=True)
    _stage(m, "Waiting for another scene to finish", progress)
    while _card_busy(m["id"]):
        time.sleep(20)
    _stage(m, stage, progress)


def _help(exe: str, *sub: str) -> str:
    try:
        r = subprocess.run([exe, *sub, "-h"] if sub else [exe, "--help"], capture_output=True, text=True, timeout=60)
        return (r.stdout or "") + (r.stderr or "")
    except Exception:
        return ""


# --------------------------------------------------------------------------
# the pipeline
# --------------------------------------------------------------------------

def _stage(m: dict, stage: str, progress: float):
    m["stage"], m["progress"] = stage, round(progress, 3)
    _save(m)


def _done(m: dict, step: str):
    m.setdefault("done", [])
    if step not in m["done"]:
        m["done"].append(step)
    _save(m)


def _source(video_id: int) -> str:
    db = SessionLocal()
    try:
        v = db.query(Video).filter(Video.id == video_id).first()
        if not v:
            raise RuntimeError("The drone video is no longer in the library")
        src = _resolve(v.filepath) if _resolve else v.filepath
    finally:
        db.close()
    if not src or not os.path.exists(src):
        raise RuntimeError("The video file is not on the drive")
    return src


RAW_360 = (".insp", ".insv", ".360")      # the camera's own unstitched files (two fisheye pictures side by side)


def _sources(ids: List[int]) -> List[dict]:
    """[{id, path, kind: video|photo, name}] for the library items a scene is made from."""
    db = SessionLocal()
    try:
        rows = {v.id: v for v in db.query(Video).filter(Video.id.in_(ids)).all()}
    finally:
        db.close()
    out = []
    for i in ids:
        v = rows.get(i)
        if not v:
            raise RuntimeError("One of the videos or photos is no longer in the library")
        src = _resolve(v.filepath) if _resolve else v.filepath
        if not src or not os.path.exists(src):
            raise RuntimeError(f"{v.filename} is not on the drive")
        if Path(src).suffix.lower() in RAW_360:
            raise RuntimeError(f"{v.filename} is the 360 camera's own file. Export it as a 360 photo or video "
                               "(2:1, equirectangular) from Insta360 Studio or the phone app first.")
        out.append({"id": v.id, "path": src, "kind": v.media_type or "video", "name": v.filename})
    return out


def _is_360(w: int, h: int) -> bool:
    return w >= 2000 and h > 0 and abs(w / h - 2.0) < 0.03


# a 360 picture is cut into ordinary pictures a 90 degree lens would have taken:
# eight round the horizon and four looking down at the floor or the ground
EQUI_VIEWS = [(yaw, 0.0) for yaw in range(0, 360, 45)] + [(yaw, -35.0) for yaw in (0, 90, 180, 270)]
_equi_maps: dict = {}


def _equi_size(w: int) -> int:
    """A 90-degree view holds a quarter of the panorama's width (a 5.7K Insta360: 1440 px)."""
    return int(max(1024, min(1600, (w or 4096) // 4)))


def _equi_cut(eq, S: int):
    """The equirectangular picture as len(EQUI_VIEWS) square 90-degree views."""
    import cv2
    import numpy as np
    H, W = eq.shape[:2]
    key = (W, H, S)
    if key not in _equi_maps:
        f = S / 2.0
        u, v = np.meshgrid(np.arange(S, dtype=np.float32) + 0.5 - S / 2, np.arange(S, dtype=np.float32) + 0.5 - S / 2)
        d = np.stack([u / f, v / f, np.ones_like(u)], -1)            # x right, y down, z ahead
        d /= np.linalg.norm(d, axis=-1, keepdims=True)
        maps = []
        for yaw, pitch in EQUI_VIEWS:
            p, y_ = np.radians(pitch), np.radians(yaw)
            Rx = np.array([[1, 0, 0], [0, np.cos(p), -np.sin(p)], [0, np.sin(p), np.cos(p)]], np.float32)   # tilt (y down)
            Ry = np.array([[np.cos(y_), 0, np.sin(y_)], [0, 1, 0], [-np.sin(y_), 0, np.cos(y_)]], np.float32)
            r = d @ (Ry @ Rx).T
            lon = np.arctan2(r[..., 0], r[..., 2])
            lat = np.arcsin(np.clip(-r[..., 1], -1, 1))
            mx = ((lon / (2 * np.pi) + 0.5) * W).astype(np.float32)
            my = ((0.5 - lat / np.pi) * H).astype(np.float32)
            maps.append((mx, my))
        _equi_maps.clear()
        _equi_maps[key] = maps
    return [cv2.remap(eq, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)
            for mx, my in _equi_maps[key]]


def _read_photo(path: str):
    """A photo as an upright BGR array: JPEG, PNG, TIFF, HEIC and RAW through the
    library's own reader (turned the way the camera was held)."""
    import cv2
    import numpy as np
    try:
        import photo_proxy
        return cv2.cvtColor(np.asarray(photo_proxy._open_photo(path)), cv2.COLOR_RGB2BGR)
    except Exception:
        return cv2.imread(path, cv2.IMREAD_COLOR)


def _taken(s: dict):
    """(when the photo was taken, name) for sorting a photo set."""
    t = ""
    try:
        from PIL import Image
        with Image.open(s["path"]) as im:
            ex = im.getexif()
            t = str(ex.get_ifd(0x8769).get(36867) or ex.get(306) or "")
    except Exception:
        pass
    return (t or "~", s["name"])


def _sharpness(path: Path) -> float:
    """How sharp a picture is (variance of the Laplacian on a small grey copy):
    a phone moving while it films smears the frame, and smeared frames make fuzzy splats."""
    import cv2
    im = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if im is None:
        return 0.0
    k = 640.0 / max(im.shape)
    if k < 1:
        im = cv2.resize(im, (int(im.shape[1] * k), int(im.shape[0] * k)), interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(im, cv2.CV_64F).var())


def _keep_sharpest(tmp: Path, per: int) -> List[Path]:
    """Of each run of `per` frames in a row, the sharpest one."""
    cand = sorted(tmp.glob("*.jpg"))
    if per <= 1:
        return cand
    out = []
    for i in range(0, len(cand), per):
        run = cand[i:i + per]
        out.append(max(run, key=_sharpness))
    return out


LONG = 1920                  # the long side of every picture Brush learns from (its own limit too)
PICK = 3                     # frames looked at for each one kept


def _gather(m: dict, imgs: Path, log: Path) -> List[dict]:
    """The pictures COLMAP works from, in groups that share one camera:
    frames from each ordinary video, each size of ordinary photo, and the
    90-degree views cut from 360 photos and videos. Returns the groups."""
    import cv2
    import video_edit
    srcs = _sources(m.get("sources") or [m["video_id"]])
    # photos in the order they were taken (a selection in the library comes in any order)
    ph = sorted((s for s in srcs if s["kind"] != "video"), key=_taken)
    it = iter(ph)
    srcs = [s if s["kind"] == "video" else next(it) for s in srcs]
    ff = shutil.which("ffmpeg") or "ffmpeg"
    groups: List[dict] = []
    vids = [s for s in srcs if s["kind"] == "video"]
    info = {s["id"]: video_edit.probe_path(s["path"]) for s in vids}
    flat_vids = [s for s in vids if not _is_360(info[s["id"]].get("width", 0), info[s["id"]].get("height", 0))]
    total_dur = sum(float(info[s["id"]].get("duration") or 0) or 30.0 for s in flat_vids) or 1.0
    # about four frames a second of walking, between 150 and 360 (more pictures, more of the room)
    budget = int(m.get("frames") or max(150, min(360, 4 * total_dur)))
    m["frames"] = budget
    for n, s in enumerate(srcs):
        g = imgs / f"s{n:02d}"
        g.mkdir(parents=True, exist_ok=True)
        if s["kind"] == "video":
            inf = info[s["id"]]
            dur = float(inf.get("duration") or 0) or 60.0
            one = len(srcs) == 1
            a = max(0.0, (m.get("start") or 0.0) if one else 0.0)
            b = min(dur, (m.get("end") or dur) if one else dur)
            span = max(2.0, b - a)
            if _is_360(inf.get("width", 0), inf.get("height", 0)):
                tmp = imgs / f"_eq{n:02d}"
                tmp.mkdir(exist_ok=True)
                fps = min(2.0, max(0.2, 40 / span))       # about 40 moments, twelve views each
                S360 = _equi_size(inf.get("width", 0))
                if _run([ff, "-y", "-ss", f"{a:.2f}", "-to", f"{b:.2f}", "-i", s["path"], "-vf", f"fps={fps * PICK:.3f}",
                         "-q:v", "2", str(tmp / "e_%04d.jpg")], log, 3600) != 0:
                    raise RuntimeError(f"Could not take frames from {s['name']}")
                for k, e in enumerate(_keep_sharpest(tmp, PICK)):
                    eq = cv2.imread(str(e), cv2.IMREAD_COLOR)
                    if eq is None:
                        continue
                    for j, view in enumerate(_equi_cut(eq, S360)):
                        cv2.imwrite(str(g / f"f_{k:04d}_v{j:02d}.jpg"), view, [cv2.IMWRITE_JPEG_QUALITY, 93])
                shutil.rmtree(tmp, ignore_errors=True)
                groups.append({"dir": g.name, "kind": "equi", "size": S360})
            else:
                share = budget * ((float(inf.get("duration") or 0) or 30.0) / total_dur) if len(flat_vids) > 1 else budget
                fps = min(6.0, max(0.5, max(30.0, share) / span))
                tmp = imgs / f"_c{n:02d}"
                tmp.mkdir(exist_ok=True)
                fit = f"scale='if(gte(iw,ih),min({LONG},iw),-2)':'if(gte(iw,ih),-2,min({LONG},ih))'"
                if _run([ff, "-y", "-ss", f"{a:.2f}", "-to", f"{b:.2f}", "-i", s["path"], "-vf",
                         f"fps={fps * PICK:.3f},{fit}", "-q:v", "2", str(tmp / "c_%05d.jpg")], log, 3600) != 0:
                    raise RuntimeError(f"Could not take frames from {s['name']}")
                for k, f in enumerate(_keep_sharpest(tmp, PICK)):
                    shutil.move(str(f), str(g / f"f_{k + 1:04d}.jpg"))
                shutil.rmtree(tmp, ignore_errors=True)
                groups.append({"dir": g.name, "kind": "video"})
        else:
            im = _read_photo(s["path"])
            if im is None:
                raise RuntimeError(f"Could not read {s['name']}")
            h, w = im.shape[:2]
            if _is_360(w, h):
                S360 = _equi_size(w)
                for j, view in enumerate(_equi_cut(im, S360)):
                    cv2.imwrite(str(g / f"f_0000_v{j:02d}.jpg"), view, [cv2.IMWRITE_JPEG_QUALITY, 93])
                groups.append({"dir": g.name, "kind": "equi", "size": S360})
            else:
                k = min(1.0, float(LONG) / max(h, w))
                if k < 1:
                    im = cv2.resize(im, (int(w * k), int(h * k)), interpolation=cv2.INTER_AREA)
                # named by when it was taken, so the pictures keep the walk's order when they share a folder
                cv2.imwrite(str(g / f"p{n:04d}.jpg"), im, [cv2.IMWRITE_JPEG_QUALITY, 95])
                groups.append({"dir": g.name, "kind": "photo", "shape": list(im.shape[:2])})
    # photos of one size from one camera share their lens: one group each, not one per photo
    merged: List[dict] = []
    by_shape: dict = {}
    for gr in groups:
        if gr["kind"] == "photo":
            key = tuple(gr["shape"])
            if key in by_shape:
                src_dir = imgs / gr["dir"]
                dst = imgs / by_shape[key]["dir"]
                for f in src_dir.glob("*.jpg"):
                    shutil.move(str(f), str(dst / f.name))
                shutil.rmtree(src_dir, ignore_errors=True)
                continue
            by_shape[key] = gr
        merged.append(gr)
    # equirect cuts from several 360 sources share one lens too, but keeping them apart is harmless
    for gr in merged:
        gr["count"] = len(list((imgs / gr["dir"]).glob("*.jpg")))
    merged = [gr for gr in merged if gr["count"]]
    if not merged:
        raise RuntimeError("There were no pictures to work from")
    return merged


def _registered(model: Path) -> int:
    """How many pictures a COLMAP model placed (the count at the head of images.bin)."""
    try:
        with open(model / "images.bin", "rb") as f:
            return int.from_bytes(f.read(8), "little")
    except Exception:
        return 0


def _read_images_txt(p: Path):
    """[(name, R (3x3), centre (3,))] from COLMAP's images.txt."""
    import numpy as np
    out = []
    lines = [x for x in p.read_text(errors="replace").splitlines() if x and not x.startswith("#")]
    for i in range(0, len(lines), 2):
        f = lines[i].split()
        if len(f) < 10:
            continue
        w, x, y, z = map(float, f[1:5])
        t = np.array(list(map(float, f[5:8])))
        R = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                      [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                      [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
        out.append((f[9], R, -R.T @ t))
    out.sort(key=lambda r: r[0])
    return out


VIEW_VERSION = 2      # 2: an orbit must look at one point; up from a level orbit plane only


def _view(m: dict, d: Path, colmap: str, model: Path, log: Path):
    """Which way is up, what the drone was looking at and where it started, from the
    camera path itself (a splat's bounding box is thrown off by far-off floaters, and
    COLMAP's world is only upright by chance). Saved in COLMAP's own coordinates."""
    import numpy as np
    txt = d / "cams"
    txt.mkdir(exist_ok=True)
    if not (txt / "images.txt").is_file() and \
            _run([colmap, "model_converter", "--input_path", win(model), "--output_path", win(txt), "--output_type", "TXT"], log) != 0:
        return
    cams = _read_images_txt(txt / "images.txt")
    # 360 shots: the path and the looking direction come from each moment's forward view
    fwd = [c for c in cams if "_v00" in c[0]]
    if fwd and len(fwd) >= 3:
        cams = fwd
    if len(cams) < 3:
        return
    C = np.array([c[2] for c in cams])
    F = np.array([c[1].T @ np.array([0.0, 0.0, 1.0]) for c in cams])     # where each frame looks
    U = np.array([c[1].T @ np.array([0.0, -1.0, 0.0]) for c in cams])    # each frame's up (COLMAP's y is down)
    up = U.mean(0)
    up = up / (np.linalg.norm(up) or 1)
    cc = C - C.mean(0)
    _, sv, vt = np.linalg.svd(cc, full_matrices=False)
    spread = float(sv[0] / np.sqrt(len(C))) or 1.0
    # the point every frame looks towards (least squares over the viewing lines)
    A = np.zeros((3, 3))
    b = np.zeros(3)
    for c, f in zip(C, F):
        P = np.eye(3) - np.outer(f, f)
        A += P
        b += P @ c
    target = None
    try:
        p = np.linalg.solve(A, b)
        ahead = np.mean([(p - c) @ f > 0 for c, f in zip(C, F)])
        # an orbit keeps the house in the middle of the picture (a drone: under a degree off);
        # a phone turning round a room looks everywhere (60 degrees and more off any one point)
        off = np.median([np.degrees(np.arccos(np.clip((p - c) @ f / (np.linalg.norm(p - c) or 1), -1, 1)))
                         for c, f in zip(C, F)])
        if ahead > 0.7 and off < 20 and np.linalg.norm(p - C.mean(0)) < 6 * spread + 1e-6:
            target = p
    except Exception:
        pass
    # an orbit: the plane the drone flew in says which way is up better than the tilted camera -
    # but only a level plane (one that agrees with the frames' own up) is an orbit's
    if target is not None and sv[1] > 0.35 * sv[0] and sv[2] < 0.35 * sv[1] and abs(vt[2] @ up) > 0.7:
        up = vt[2] * (1 if vt[2] @ up > 0 else -1)
    inside = target is None
    if inside:
        # a walk through a room: stand where the first frame was, look where it looked
        dist = max(spread, 0.5)
        try:
            pts = []
            for ln in (txt / "points3D.txt").read_text(errors="replace").splitlines():
                if ln and not ln.startswith("#"):
                    f = ln.split()
                    pts.append((float(f[1]), float(f[2]), float(f[3])))
                    if len(pts) >= 50000:
                        break
            X = np.array(pts)
            dep = (X - C[0]) @ F[0]
            dep = dep[dep > 0]
            if len(dep):
                dist = float(np.median(dep))
        except Exception:
            pass
        start, target = C[0], C[0] + F[0] * dist
    else:
        start = C[0]
    m["view"] = {"v": VIEW_VERSION, "up": up.round(6).tolist(), "target": np.asarray(target).round(5).tolist(),
                 "start": np.asarray(start).round(5).tolist(), "kind": "inside" if inside else "orbit",
                 "path": C[:: max(1, len(C) // 60)].round(4).tolist(),
                 "look": F[:: max(1, len(C) // 60)].round(4).tolist()}
    _save(m)


def _read_ply(p: Path):
    """(header lines, structured array) of a binary little-endian splat ply."""
    import numpy as np
    with open(p, "rb") as f:
        head = []
        while True:
            ln = f.readline()
            if not ln:
                raise ValueError("not a ply")
            head.append(ln.decode("ascii", "replace").strip())
            if head[-1] == "end_header":
                break
        n = next(int(h.split()[2]) for h in head if h.startswith("element vertex"))
        types = {"float": "<f4", "double": "<f8", "uchar": "u1", "int": "<i4", "uint": "<u4"}
        dt = [(h.split()[2], types[h.split()[1]]) for h in head if h.startswith("property ")]
        a = np.fromfile(f, dtype=np.dtype(dt), count=n)
    return head, a


def _write_ply(p: Path, head: List[str], a):
    tmp = p.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        lines = [f"element vertex {len(a)}" if h.startswith("element vertex") else h for h in head]
        f.write(("\n".join(lines) + "\n").encode("ascii"))
        a.tofile(f)
    os.replace(tmp, p)


def _clean_splats(a, pts, cams, inside: bool):
    """Which splats to keep: not the all-but-invisible ones, not the big faint blobs that
    read as fog, and in a room not the haze floating outside it (splats far beyond what the
    camera saw). Returns a boolean mask."""
    import numpy as np
    xyz = np.stack([a["x"], a["y"], a["z"]], -1).astype(np.float64)
    op = 1 / (1 + np.exp(-a["opacity"].astype(np.float64)))
    sc = np.exp(np.stack([a["scale_0"], a["scale_1"], a["scale_2"]], -1).astype(np.float64)).max(-1)
    ref = pts if len(pts) > 100 else xyz
    lo, hi = np.percentile(ref, 2, 0), np.percentile(ref, 98, 0)
    if len(cams):
        lo, hi = np.minimum(lo, cams.min(0)), np.maximum(hi, cams.max(0))
    size = float(np.linalg.norm(hi - lo)) or 1.0
    keep = op > 0.02
    keep &= ~((sc > 0.05 * size) & (op < 0.35))
    keep &= np.isfinite(xyz).all(-1)
    if inside:
        pad = (hi - lo) * 0.2 + 0.02 * size
        keep &= ((xyz > lo - pad) & (xyz < hi + pad)).all(-1)
    return keep


def _finish_ply(m: dict, d: Path, src: Path):
    """Brush's scene -> scene_raw.ply as it came, scene.ply cleaned for the viewer."""
    import numpy as np
    raw = d / "scene_raw.ply"
    if src.resolve() != raw.resolve():
        shutil.copy2(src, raw)
    try:
        head, a = _read_ply(raw)
        pts = np.zeros((0, 3))
        cams = np.zeros((0, 3))
        txt = d / "cams"
        try:
            X = []
            for ln in (txt / "points3D.txt").read_text(errors="replace").splitlines():
                if ln and not ln.startswith("#"):
                    f = ln.split()
                    X.append((float(f[1]), float(f[2]), float(f[3])))
            pts = np.array(X) if X else pts
            cams = np.array([c[2] for c in _read_images_txt(txt / "images.txt")]) if (txt / "images.txt").is_file() else cams
        except Exception:
            pass
        inside = (m.get("view") or {}).get("kind") == "inside"
        keep = _clean_splats(a, pts, cams, inside)
        _write_ply(d / "scene.ply", head, a[keep])
        m["splats"] = {"made": int(len(a)), "kept": int(keep.sum())}
        print(f"splat: {m['id']}: kept {int(keep.sum())} of {len(a)} splats", flush=True)
    except Exception as e:
        print(f"splat: {m['id']}: could not clean the scene ({e}), keeping it as it came", flush=True)
        shutil.copy2(raw, d / "scene.ply")


def _pipeline(m: dict, src: Optional[str] = None):
    """Each step is skipped when an earlier run finished it, so a scene carries on
    where it was when Zerko restarted."""
    t = tools()
    d = work_dir() / m["id"]
    log = d / "log.txt"
    colmap, brush = t["colmap"], t["brush"]
    done = m.setdefault("done", [])
    m["state"], m["error"] = "running", ""
    _save(m)
    try:
        imgs = d / "images"
        sparse = d / "sparse"
        und = d / "scene"
        # an older scene that got past the mapper before steps were recorded
        if not done and any((p / "images.bin").is_file() for p in (sparse.iterdir() if sparse.is_dir() else [])):
            for s in ("frames", "features", "matches"):
                _done(m, s)

        # 1. the pictures: frames from the videos, the photos, 360 shots cut into ordinary views
        if "frames" not in done:
            _stage(m, "Getting the pictures ready", 0.02)
            shutil.rmtree(imgs, ignore_errors=True)
            imgs.mkdir(exist_ok=True)
            m["groups"] = _gather(m, imgs, log)
            m["frame_count"] = sum(g["count"] for g in m["groups"])
            _done(m, "frames")
        groups = m.get("groups") or [{"dir": ".", "kind": "video", "count": m.get("frame_count", 0)}]

        # 2. where the drone was
        try:
            import ai_image
            ai_image.comfy_free()                  # the image model gives the card back first
        except Exception:
            pass
        with _gpu:
            db = d / "db.db"
            if "features" not in done:
                _stage(m, "Finding the camera in every frame", 0.08)
                _wait_for_card(m)
                db.unlink(missing_ok=True)
                fx = _help(colmap, "feature_extractor")
                gpu_x = "--FeatureExtraction.use_gpu" if "FeatureExtraction.use_gpu" in fx else "--SiftExtraction.use_gpu"
                use_gpu = "1"
                # one camera per group: each video, each size of photo, the views cut from 360 shots
                # (those have a known 90-degree lens, so COLMAP is told it rather than guessing)
                for gr in groups:
                    base = [colmap, "feature_extractor", "--database_path", win(db), "--image_path", win(imgs),
                            "--ImageReader.single_camera", "1"]
                    if gr["dir"] != ".":
                        lst = d / f"list_{gr['dir']}.txt"
                        lst.write_text("\n".join(f"{gr['dir']}/{f.name}" for f in sorted((imgs / gr["dir"]).glob("*.jpg"))) + "\n")
                        base += ["--image_list_path", win(lst)]
                    if gr["kind"] == "equi":
                        f = gr["size"] / 2.0
                        base += ["--ImageReader.camera_model", "PINHOLE", "--ImageReader.camera_params", f"{f},{f},{f},{f}"]
                    else:
                        base += ["--ImageReader.camera_model", "OPENCV"]
                    if _run(base + [gpu_x, use_gpu], log) != 0:
                        use_gpu = "0"              # a CUDA build that does not know this card yet: the CPU still works
                        if _run(base + [gpu_x, "0"], log) != 0:
                            raise RuntimeError("COLMAP could not find features in the pictures")
                _done(m, "features")
            if "matches" not in done:
                _stage(m, "Matching the pictures", 0.2)
                _wait_for_card(m)
                n_img = sum(g.get("count", 0) for g in groups)
                one_video = len(groups) == 1 and groups[0]["kind"] == "video"
                if n_img <= 400:
                    # photos and 360 shots are in no particular order, and a walk round a room comes back
                    # on itself: every picture against every other (a phone pan: 79 of 200 frames placed
                    # matching only neighbours, 186 like this)
                    mx = _help(colmap, "exhaustive_matcher")
                    base = [colmap, "exhaustive_matcher", "--database_path", win(db)]
                else:
                    mx = _help(colmap, "sequential_matcher")
                    ov = 12 if one_video else (3 * len(EQUI_VIEWS) if any(g["kind"] == "equi" for g in groups) else 20)
                    base = [colmap, "sequential_matcher", "--database_path", win(db), "--SequentialMatching.overlap", str(ov)]
                gpu_m = "--FeatureMatching.use_gpu" if "FeatureMatching.use_gpu" in mx else "--SiftMatching.use_gpu"
                if _run(base + [gpu_m, "1"], log) != 0:
                    if _run(base + [gpu_m, "0"], log) != 0:
                        raise RuntimeError("COLMAP could not match the frames")
                _done(m, "matches")

            def models():
                return [p for p in sparse.iterdir() if p.is_dir() and (p / "images.bin").is_file()] if sparse.is_dir() else []

            def map_it():
                shutil.rmtree(sparse, ignore_errors=True)
                sparse.mkdir(exist_ok=True)
                top = _help(colmap)
                mapper = "global_mapper" if "global_mapper" in top else "mapper"
                if _run([colmap, mapper, "--database_path", win(db), "--image_path", win(imgs), "--output_path", win(sparse)], log) != 0                         and mapper != "mapper":
                    _run([colmap, "mapper", "--database_path", win(db), "--image_path", win(imgs), "--output_path", win(sparse)], log)
            if "mapper" not in done:
                _stage(m, "Working out the flight path", 0.3)
                if not models():
                    map_it()
                n_img = sum(g.get("count", 0) for g in groups) or 1
                placed = max((_registered(p) for p in models()), default=0)
                if placed < 0.7 * n_img and not m.get("rematched"):
                    # a quick pan breaks the chain of neighbouring frames and the scene falls apart in
                    # pieces: match every picture with every other and try again (a phone turning
                    # round a room: 79 of 200 frames -> 186)
                    print(f"splat: {m['id']}: {placed} of {n_img} pictures placed, matching them all", flush=True)
                    _stage(m, "Matching every picture with every other", 0.24)
                    _wait_for_card(m)
                    mx = _help(colmap, "exhaustive_matcher")
                    gpu_m = "--FeatureMatching.use_gpu" if "FeatureMatching.use_gpu" in mx else "--SiftMatching.use_gpu"
                    base = [colmap, "exhaustive_matcher", "--database_path", win(db)]
                    if _run(base + [gpu_m, "1"], log) == 0 or _run(base + [gpu_m, "0"], log) == 0:
                        m["rematched"] = True
                        _stage(m, "Working out the flight path", 0.3)
                        map_it()
                if not models():
                    raise RuntimeError("COLMAP could not work out where the camera was. Slow, steady movement with plenty of overlap "
                                       "between pictures works best: an orbit round the house, a slow walk through a room.")
                m["placed"] = max(_registered(p) for p in models())
                _done(m, "mapper")
            best = max(models(), key=_registered)

            if "undistort" not in done:
                _stage(m, "Straightening the frames", 0.38)
                shutil.rmtree(und, ignore_errors=True)
                if _run([colmap, "image_undistorter", "--image_path", win(imgs), "--input_path", win(best),
                         "--output_path", win(und), "--output_type", "COLMAP"], log) != 0:
                    raise RuntimeError("COLMAP could not straighten the frames")
                s0 = und / "sparse" / "0"
                s0.mkdir(parents=True, exist_ok=True)
                for f in (und / "sparse").glob("*.bin"):
                    shutil.move(str(f), str(s0 / f.name))
                _done(m, "undistort")
            if not m.get("view"):
                try:
                    _view(m, d, colmap, und / "sparse" / "0", log)
                except Exception as e:
                    print(f"splat: {m['id']}: could not work out the view: {e}", flush=True)

            # 3. the scene
            if "brush" not in done or not (d / "scene.ply").is_file():
                _stage(m, "Building the 3D scene", 0.42)
                out = d / "out"
                if _brush_running(m["id"]):
                    # the run from before the restart is still going: wait for it
                    print(f"splat: {m['id']}: Brush is still at work, waiting for it", flush=True)
                    while _brush_running(m["id"]):
                        time.sleep(15)
                        _stage(m, "Building the 3D scene", min(0.97, m.get("progress", 0.5) + 0.004))
                plys = sorted(out.rglob("*.ply"), key=lambda p: p.stat().st_mtime) if out.is_dir() else []
                if plys:
                    _finish_ply(m, d, plys[-1])
                    _done(m, "brush")
            if "brush" not in done or not (d / "scene.ply").is_file():
                _stage(m, "Building the 3D scene", 0.42)
                out = d / "out"
                _wait_for_card(m)
                shutil.rmtree(out, ignore_errors=True)
                out.mkdir(exist_ok=True)
                bh = _help(brush)
                steps = int(m.get("steps") or 30000)
                args = [brush, win(und)]
                # grown twice as keenly as Brush's own default and for longer: a room came out at
                # 260 000 splats (soft walls, smeared edges) where a drone orbit had 1.4 million
                for flag, val in (("--total-steps", str(steps)), ("--export-every", str(steps)), ("--export-path", win(out)),
                                  ("--export-name", "scene.ply"), ("--max-splats", str(m.get("max_splats") or 4000000)),
                                  ("--growth-grad-threshold", "0.00002"), ("--growth-select-fraction", "0.2"),
                                  ("--growth-stop-iter", str(int(steps * 0.75))), ("--max-resolution", str(LONG))):
                    if flag in bh:
                        args += [flag, val]
                t0 = time.time()
                with open(log, "a") as lf:
                    lf.write(f"\n$ {' '.join(args)}\n")
                proc = subprocess.Popen(args, stdout=open(log, "a"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
                _running[m["id"]] = proc
                est = max(300.0, steps * 0.06)          # seconds, a first guess; the page shows it moving
                while proc.poll() is None:
                    time.sleep(5)
                    _stage(m, "Building the 3D scene", min(0.97, 0.42 + 0.55 * (time.time() - t0) / est))
                _running.pop(m["id"], None)
                plys = sorted(out.rglob("*.ply"), key=lambda p: p.stat().st_mtime)
                if not plys:
                    raise RuntimeError("Brush did not write a scene (see log.txt in the scene's folder)")
                m["build_secs"] = round(time.time() - t0)
                _stage(m, "Cleaning up the scene", 0.98)
                _finish_ply(m, d, plys[-1])
                _done(m, "brush")
        try:
            _stage(m, "Compressing the scene for the viewer", 0.99)
            m["spz_size"] = _write_spz(d)
        except Exception as e:
            print(f"splat: {m['id']}: no .spz ({e}); the viewer uses the .ply", flush=True)
        m["state"], m["stage"], m["progress"] = "done", "Ready", 1.0
        m["size"] = (d / "scene.ply").stat().st_size
        # the frames can go: the scene and the camera path are what is kept
        for junk in (imgs, und / "images", d / "out", d / "db.db"):
            if junk.is_dir():
                shutil.rmtree(junk, ignore_errors=True)
            else:
                junk.unlink(missing_ok=True)
        _save(m)
    except Exception as e:
        m["state"], m["error"] = "error", str(e)[:400]
        _save(m)
        print(f"splat: {m['id']} failed: {e}", flush=True)


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

class NewBody(BaseModel):
    video_id: Optional[int] = None
    video_ids: List[int] = Field(default_factory=list, max_length=600)   # videos and photos, one scene from all of them
    start: Optional[float] = Field(None, ge=0)
    end: Optional[float] = Field(None, ge=0)
    steps: int = Field(20000, ge=2000, le=60000)
    frames: int = Field(200, ge=40, le=600)


@router.get("/status")
def status(current_user: User = Depends(get_current_user)):
    return {"installed": bool(tools()), "installing": _install["state"] == "running"}


@router.get("")
def list_(video_id: Optional[int] = None, current_user: User = Depends(get_current_user)):
    if not tools():
        return {"items": []}
    out = []
    for m in sorted(work_dir().glob("*/meta.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            x = json.loads(m.read_text())
        except Exception:
            continue
        if _mine(x) and (video_id is None or x.get("video_id") == video_id):
            out.append(x)
    out = out[:100]
    ids = {x.get("video_id") for x in out if x.get("video_id")}
    if ids:
        db = SessionLocal()
        try:
            th = dict(db.query(Video.id, Video.thumbnail_path).filter(Video.id.in_(ids)).all())
        finally:
            db.close()
        for x in out:
            if x.get("state") == "done":
                n = _spz_ready(work_dir() / x["id"])
                if n:
                    x["spz_size"] = n
            x["thumb"] = th.get(x.get("video_id"))
    return {"items": out}


@router.post("")
def new(body: NewBody, current_user: User = Depends(get_current_user)):
    """A 3D scene from a video (a drone orbit, a phone walk through a room), a set of
    photos, 360 photos or videos, or any mix of them taken in one place."""
    ids = list(dict.fromkeys(body.video_ids or ([body.video_id] if body.video_id else [])))
    if not ids:
        raise HTTPException(status_code=400, detail="Choose a video or some photos")
    try:
        srcs = _sources(ids)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if len(srcs) == 1 and srcs[0]["kind"] != "video":
        raise HTTPException(status_code=400, detail="One photo is not enough: choose a video, a 360 photo set or twenty or more photos "
                                                    "taken while walking round")
    db = SessionLocal()
    try:
        folder_id = db.query(Video.folder_id).filter(Video.id == ids[0]).scalar()
    finally:
        db.close()
    name = Path(srcs[0]["name"]).stem + (f" and {len(srcs) - 1} more" if len(srcs) > 1 else "")
    sid = uuid.uuid4().hex[:12]
    (work_dir() / sid).mkdir(parents=True)
    m = {"id": sid, "video_id": ids[0], "sources": ids, "name": name, "folder_id": folder_id, "state": "running",
         "stage": "Waiting", "progress": 0.0, "error": "", "made": datetime.utcnow().isoformat(), "user": current_user.username,
         "start": body.start, "end": body.end, "steps": body.steps, "frames": body.frames, "videos": [], "done": [], "server": _ME}
    _save(m)
    _going.add(sid)
    threading.Thread(target=_carry_on, args=(m,), daemon=True).start()
    return m


def _write_spz(d: Path) -> int:
    import spz
    return spz.ply_to_spz(d / "scene.ply", d / "scene.spz")


def _spz_ready(d: Path) -> Optional[int]:
    """The .spz's size when it is there and made from the current scene.ply."""
    z, p = d / "scene.spz", d / "scene.ply"
    try:
        if z.is_file() and z.stat().st_mtime >= p.stat().st_mtime:
            return z.stat().st_size
    except OSError:
        pass
    return None


_spz_making: set = set()
_spz_gate = threading.Lock()


def _spz_later(sid: str, d: Path):
    """A scene made before .spz existed (or rebuilt since): compressed in the background, once."""
    with _lock:
        if sid in _spz_making:
            return
        _spz_making.add(sid)

    def go():
        try:
            with _spz_gate:            # one at a time: a big scene needs a few GB while it is read
                n = _write_spz(d)
            print(f"splat: {sid}: compressed to {n / 1e6:.0f} MB", flush=True)
        except Exception as e:
            print(f"splat: {sid}: could not compress the scene: {e}", flush=True)
        finally:
            with _lock:
                _spz_making.discard(sid)
    threading.Thread(target=go, daemon=True).start()


def _with_spz(m: dict) -> dict:
    if m.get("state") != "done":
        return m
    d = work_dir() / m["id"]
    n = _spz_ready(d)
    if n:
        m["spz_size"] = n
    else:
        m.pop("spz_size", None)
        if (d / "scene.ply").is_file():
            _spz_later(m["id"], d)
    return m


@router.get("/{sid}")
def get(sid: str, current_user: User = Depends(get_current_user)):
    m = _with_spz(_load(sid))
    if m.get("state") == "done" and (not m.get("view") or m["view"].get("v") != VIEW_VERSION):
        # a scene made before the camera path was kept: work it out from the saved cameras
        try:
            d = work_dir() / sid
            _view(m, d, tools()["colmap"], d / "scene" / "sparse" / "0", d / "log.txt")
        except Exception as e:
            print(f"splat: {sid}: could not work out the view: {e}", flush=True)
    return m


class RenameBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)


@router.patch("/{sid}")
def rename(sid: str, body: RenameBody, current_user: User = Depends(get_current_user)):
    m = _load(sid)
    m["name"] = body.name.strip()
    _save(m)
    return m


@router.post("/{sid}/rebuild")
def rebuild(sid: str, current_user: User = Depends(get_current_user)):
    """Build the scene again from its videos and photos (after the way scenes are built got
    better). The scene there now stays until the new one is ready."""
    m = _load(sid)
    with _lock:
        if sid in _going or m.get("state") == "running":
            raise HTTPException(status_code=409, detail="That scene is being built right now")
        d = work_dir() / sid
        for junk in ("images", "sparse", "cams", "out", "db.db"):
            p = d / junk
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
        shutil.rmtree(d / "scene", ignore_errors=True)
        for k in ("view", "groups", "placed", "rematched", "splats", "frame_count", "build_secs", "frames"):
            m.pop(k, None)
        m["steps"] = 30000
        m.update(done=[], state="running", stage="Waiting", progress=0.0, error="")
        _save(m)
        _going.add(sid)
    threading.Thread(target=_carry_on, args=(m,), daemon=True).start()
    return m


@router.post("/{sid}/poster")
async def poster(sid: str, file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    """A view from the scene, for its card in the list."""
    import cv2
    import numpy as np
    m = _load(sid)
    data = await file.read(20 << 20)
    im = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if im is None:
        raise HTTPException(status_code=400, detail="That is not a picture")
    h, w = im.shape[:2]
    # the card is 16:9: the middle of the view, 960 wide
    tw, th = (w, int(w * 9 / 16)) if w * 9 / 16 <= h else (int(h * 16 / 9), h)
    x0, y0 = (w - tw) // 2, (h - th) // 2
    im = cv2.resize(im[y0:y0 + th, x0:x0 + tw], (960, 540), interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(work_dir() / sid / "poster.jpg"), im, [cv2.IMWRITE_JPEG_QUALITY, 86])
    m["poster"] = int(time.time())
    _save(m)
    return {"poster": m["poster"]}


@router.get("/{sid}/poster.jpg")
def poster_jpg(sid: str, request: Request, token: Optional[str] = None):
    from auth import get_user_from_token
    db = SessionLocal()
    try:
        auth = request.headers.get("Authorization") or ""
        get_user_from_token(token or auth[7:], db)
    finally:
        db.close()
    _load(sid)
    p = work_dir() / sid / "poster.jpg"
    if not p.is_file():
        raise HTTPException(status_code=404, detail="No picture yet")
    return FileResponse(str(p), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=31536000"})


@router.post("/{sid}/resume")
def resume(sid: str, current_user: User = Depends(get_current_user)):
    """Carry on from the last step that finished (after an error or a restart)."""
    m = _load(sid)
    with _lock:
        if sid in _going:
            return m
        if m.get("state") == "done":
            return m
        _going.add(sid)
    threading.Thread(target=_carry_on, args=(m,), daemon=True).start()
    return m


_going: set = set()


def _carry_on(m: dict, src: Optional[str] = None):
    try:
        _pipeline(m, src)
    finally:
        _going.discard(m["id"])


def _resume_all():
    """Scenes that were being built when Zerko stopped carry on by themselves."""
    time.sleep(20)
    try:
        if not tools():
            return
        for p in work_dir().glob("*/meta.json"):
            try:
                m = json.loads(p.read_text())
            except Exception:
                continue
            if _mine(m) and m.get("state") == "running" and m["id"] not in _going:
                m["server"] = _ME
                _going.add(m["id"])
                print(f"splat: carrying on with {m['id']} ({m.get('stage')})", flush=True)
                _carry_on(m)
    except Exception as e:
        print(f"splat: could not carry on the unfinished scenes: {e}", flush=True)


@router.delete("/{sid}")
def delete(sid: str, current_user: User = Depends(get_current_user)):
    _load(sid)
    p = _running.pop(sid, None)
    if p:
        p.kill()
    shutil.rmtree(work_dir() / sid, ignore_errors=True)
    return {"ok": True}


# --------------------------------------------------------------------------
# sharing a scene with the client: one link, and in the portals of its property
# --------------------------------------------------------------------------

def _shoot_of(m: dict) -> Optional[int]:
    """The property the scene's footage belongs to (its folder), if any."""
    if not m.get("folder_id"):
        return None
    db = SessionLocal()
    try:
        import shoots
        from database import IndexedFolder
        path = db.query(IndexedFolder.path).filter(IndexedFolder.id == m["folder_id"]).scalar()
        s = shoots.shoot_for_path(db, path) if path else None
        return s.id if s else None
    except Exception:
        return None
    finally:
        db.close()


@router.post("/{sid}/share")
def share_scene(sid: str, current_user: User = Depends(get_current_user)):
    """A link the client can open without an account: the scene to look around in, nothing to download."""
    import secrets
    m = _load(sid)
    if m.get("state") != "done":
        raise HTTPException(status_code=400, detail="The scene is not finished yet")
    if not m.get("public"):
        m["public"] = secrets.token_urlsafe(18)
    m["shoot_id"] = _shoot_of(m)
    _save(m)
    return {"url": f"/3d/{m['public']}", "shoot_id": m["shoot_id"]}


@router.delete("/{sid}/share")
def unshare_scene(sid: str, current_user: User = Depends(get_current_user)):
    m = _load(sid)
    m.pop("public", None)
    _save(m)
    return {"ok": True}


def _shared(token: str) -> tuple:
    if not tools() or not re.fullmatch(r"[A-Za-z0-9_-]{16,40}", token or ""):
        raise HTTPException(status_code=404, detail="This link is no longer available")
    for p in work_dir().glob("*/meta.json"):
        try:
            m = json.loads(p.read_text())
        except Exception:
            continue
        if m.get("public") == token and _mine(m) and m.get("state") == "done":
            return m, p.parent
    raise HTTPException(status_code=404, detail="This link is no longer available")


def shared_for_shoot(shoot_id: Optional[int]) -> List[dict]:
    """The scenes shared with the client for this property, for its portals."""
    if not shoot_id or not tools():
        return []
    out = []
    for p in sorted(work_dir().glob("*/meta.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            m = json.loads(p.read_text())
        except Exception:
            continue
        if m.get("public") and m.get("shoot_id") == shoot_id and _mine(m) and m.get("state") == "done":
            out.append({"name": m.get("name") or "3D walk-through", "url": f"/3d/{m['public']}",
                        "poster": f"/api/public/3d/{m['public']}/poster.jpg?v={m.get('poster') or 0}"})
    return out


public_router = APIRouter(prefix="/api/public/3d", tags=["3d"])


@public_router.get("/{token}")
def public_scene(token: str):
    m, d = _shared(token)
    return {"name": m.get("name") or "3D walk-through", "view": m.get("view"),
            "size": _spz_ready(d) or m.get("size"), "poster": m.get("poster")}


@public_router.get("/{token}/scene")
def public_scene_file(token: str):
    m, d = _shared(token)
    p = d / "scene.spz" if _spz_ready(d) else d / "scene.ply"
    if not p.is_file():
        raise HTTPException(status_code=404, detail="The scene is not built yet")
    return FileResponse(str(p), media_type="application/octet-stream", headers={"Cache-Control": "private, max-age=3600"})


@public_router.get("/{token}/poster.jpg")
def public_poster(token: str):
    m, d = _shared(token)
    p = d / "poster.jpg"
    if not p.is_file():
        raise HTTPException(status_code=404, detail="No picture yet")
    return FileResponse(str(p), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/{sid}/scene.spz")
def scene_spz(sid: str, request: Request, token: Optional[str] = None, download: bool = False):
    """The scene compressed (about a tenth of the .ply), for the viewer and for sharing."""
    from auth import get_user_from_token
    db = SessionLocal()
    try:
        auth = request.headers.get("Authorization") or ""
        get_user_from_token(token or auth[7:], db)
    finally:
        db.close()
    m = _load(sid)
    d = work_dir() / sid
    if not _spz_ready(d):
        raise HTTPException(status_code=404, detail="The compressed scene is not made yet")
    p = d / "scene.spz"
    if download:
        name = re.sub(r'[\\/:*?"<>|]', "_", m.get("name") or sid) + ".spz"
        return FileResponse(str(p), media_type="application/octet-stream", filename=name)
    return FileResponse(str(p), media_type="application/octet-stream", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/{sid}/scene.ply")
def scene(sid: str, request: Request, token: Optional[str] = None, download: bool = False):
    from auth import get_user_from_token
    db = SessionLocal()
    try:
        auth = request.headers.get("Authorization") or ""
        get_user_from_token(token or auth[7:], db)
    finally:
        db.close()
    _load(sid)
    m = _load(sid)
    p = work_dir() / sid / "scene.ply"
    if not p.is_file():
        raise HTTPException(status_code=404, detail="The scene is not built yet")
    if download:
        name = re.sub(r'[\\/:*?"<>|]', "_", m.get("name") or sid) + ".ply"
        return FileResponse(str(p), media_type="application/octet-stream", filename=name)
    # the file changes when a scene is built again: the page asks for it by the build's time
    return FileResponse(str(p), media_type="application/octet-stream", headers={"Cache-Control": "private, max-age=86400"})


@router.post("/{sid}/video")
async def video(sid: str, file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    """The flight the page recorded (WebM): made into an MP4 and saved next to the drone clip."""
    m = _load(sid)
    d = work_dir() / sid
    raw = d / f"flight_{uuid.uuid4().hex[:6]}.webm"
    with open(raw, "wb") as f:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    n = len(m.get("videos") or []) + 1
    mp4 = d / f"{m['name']} - fly-through {n}.mp4"
    ff = shutil.which("ffmpeg") or "ffmpeg"
    r = subprocess.run([ff, "-y", "-i", str(raw), "-c:v", "libx264", "-preset", "slow", "-crf", "17", "-pix_fmt", "yuv420p",
                        "-r", "30",     # a browser records at an uneven rate; editors want a steady 30
                        "-movflags", "+faststart", str(mp4)], capture_output=True, text=True, timeout=1800)
    raw.unlink(missing_ok=True)
    if r.returncode != 0 or not mp4.is_file():
        raise HTTPException(status_code=500, detail="The flight could not be made into an MP4")
    saved = None
    if m.get("folder_id"):
        try:
            import video_edit
            saved = video_edit._save_to_library(m["folder_id"], mp4, current_user.username)
            if saved:
                mp4.unlink(missing_ok=True)     # the library has its copy
        except Exception as e:
            print(f"splat: could not add the fly-through to the library: {e}", flush=True)
    m.setdefault("videos", []).append({"name": mp4.name, "video_id": saved})
    _save(m)
    return {"name": mp4.name, "video_id": saved}


_install = {"state": "idle"}


@router.post("/install")
def install_tools(current_user: User = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrators only")
    if _install["state"] == "running":
        return {"state": "running"}
    log = open(INSTALL_LOG, "wb")
    p = subprocess.Popen(["bash", str(INSTALLER)], stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         cwd=str(INSTALLER.parent), start_new_session=True)
    _install["state"] = "running"

    def wait():
        p.wait()
        _install["state"] = "done" if p.returncode == 0 else "failed"
        _tools_cache.clear()
    threading.Thread(target=wait, daemon=True).start()
    return {"state": "running"}


@router.get("/install/log")
def install_log(current_user: User = Depends(get_current_user)):
    try:
        tail = INSTALL_LOG.read_text(errors="replace")[-4000:]
    except Exception:
        tail = ""
    return {"state": _install["state"], "log": tail, "installed": bool(tools())}


def install(app, resolve_media_path=None):
    global _resolve
    _resolve = resolve_media_path
    app.include_router(public_router)
    app.include_router(router)
    threading.Thread(target=_resume_all, daemon=True).start()
