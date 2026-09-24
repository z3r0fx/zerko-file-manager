"""The shoot pipeline: from the photos of a property to the listing, in the
steps the studio ticks, in the order it sets.

Each property can have its own steps (a twilight job, a job that came in
merged already, one that needs a look on the front only) and otherwise uses
the studio's default. A run works through the steps one after another:

  weak frames -> merge brackets -> lens -> straighten -> preset -> clutter to
  remove -> rooms -> a look -> stop so I can check -> the listing pack

Every step calls the same code as the button that does it by hand, so a
pipeline edit is exactly the edit you would have made. Nothing is deleted
and no file is overwritten: edits are recipes, merges and exports are new
files. A "stop so I can check" step (and a look painted as a quick preview)
pauses the run on the review page until you carry on. Cars and people are
never tidied away by the pipeline: the clutter step only suggests, and never
suggests those.
"""
from __future__ import annotations

import copy
import json
import math
import threading
import time
import traceback
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.orm import Session

from auth import get_current_user
from database import Base, SessionLocal, User, Video, engine, get_db

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])

# --------------------------------------------------------------------------
# the steps
# --------------------------------------------------------------------------

# id, name, what it does, on by default, its options with their defaults
CATALOG: List[dict] = [
    {"id": "hdr", "name": "Merge brackets", "on": True,
     "about": "Bracketed sets are merged into one photo each (with the real view from the darkest frame). Single photos go on as they are.",
     "opts": {"mode": "hdr", "method": "natural", "windows": 0.6}},
    {"id": "cull", "name": "Leave out weak frames", "on": True,
     "about": "Blurry, badly exposed and near-duplicate photos are flagged and kept out of the rest. Nothing is deleted.",
     "opts": {"blur": True, "exposure": True, "duplicates": True, "reject": False}},
    {"id": "lens", "name": "Lens correction", "on": True,
     "about": "The lens's barrel distortion, fringes and corner shading taken out (the maker's profile, or measured).",
     "opts": {}},
    {"id": "straighten", "name": "Straighten", "on": True,
     "about": "Walls upright and the frame filled; a photo without walls is just levelled.",
     "opts": {"mode": "auto"}},
    {"id": "style", "name": "My style", "on": False,
     "about": "Each photo gets the light and colour you gave the most similar photo you have edited yourself (a bathroom like your bathrooms). Photos like none of yours are left as they are.",
     "opts": {}},
    {"id": "preset", "name": "Apply a preset", "on": False,
     "about": "One of your presets on every photo (their straightening and lens correction are kept).",
     "opts": {"preset_id": None}},
    {"id": "windows", "name": "Real window view", "on": False,
     "about": "Where the folder has a darker frame of the same shot, the view through blown-out windows is taken from it.",
     "opts": {}},
    {"id": "declutter", "name": "Find clutter", "on": False,
     "about": "Claude lists loose things a tidy shot would not have (bins, cables, bottles). You tick what goes on the review page. Never cars or people.",
     "opts": {}},
    {"id": "fixes", "name": "Property fixes", "on": False,
     "about": "Greener grass, a clean pool, screens black, a fire in the fireplace and mixed light, on the photos that have them. Each can be taken off in the editor. Never cars or people.",
     "opts": {"grass": True, "pool": True, "screen": True, "fire": False, "light": False}},
    {"id": "rooms", "name": "Sort into rooms", "on": True,
     "about": "Each photo put in its room, for naming the files (Bedroom1, Pool1...). Needs AI to be set up.",
     "opts": {}},
    {"id": "look", "name": "Paint a look", "on": False,
     "about": "One of your looks painted onto the photos you choose. As a quick preview first, you pick which to paint full size.",
     "opts": {"look_id": None, "which": "exterior", "preview": True, "room_looks": {}}},
    {"id": "pause", "name": "Stop so I can check", "on": False, "multi": True,
     "about": "The run waits here. Check the photos (the colour temperature, the look), change what you like, then carry on.",
     "opts": {}},
    {"id": "export", "name": "Listing pack", "on": True,
     "about": "The photos named by room at the portal size (and a social crop, a reel and the text if ticked), as one ZIP.",
     "opts": {"social": True, "reel": False, "text": True, "portal_width": 2048, "portal_kb": 1000}},
]
BY_ID = {s["id"]: s for s in CATALOG}
EXTERIOR = ("Exterior", "Aerial", "Garden", "Pool", "Patio", "View", "Entrance")


def default_steps() -> List[dict]:
    return [{"id": s["id"], "on": s["on"], "opts": copy.deepcopy(s["opts"])} for s in CATALOG]


def _clean(steps: List[dict]) -> List[dict]:
    """Known steps only, each with every option (new options get their defaults)."""
    out = []
    for st in steps or []:
        c = BY_ID.get(str(st.get("id")))
        if not c:
            continue
        opts = copy.deepcopy(c["opts"])
        for k, v in (st.get("opts") or {}).items():
            if k in opts:
                opts[k] = v
        out.append({"id": c["id"], "on": bool(st.get("on", c["on"])), "opts": opts,
                    **({"key": str(st.get("key") or uuid.uuid4().hex[:6])} if c.get("multi") else {})})
    # a step the studio never saw (added in an update) joins where the default order has it
    have = {s["id"] for s in out}
    for n, c in enumerate(CATALOG):
        if c["id"] not in have:
            out.insert(min(n, len(out)), {"id": c["id"], "on": False, "opts": copy.deepcopy(c["opts"])})
    return out


# --------------------------------------------------------------------------
# where the settings and the runs are kept
# --------------------------------------------------------------------------

class PipelineConfig(Base):
    __tablename__ = "pipeline_configs"
    key = Column(String, primary_key=True)          # "default" or "shoot:<id>"
    data = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow)
    updated_by = Column(String, nullable=True)


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"
    id = Column(String, primary_key=True)
    shoot_id = Column(Integer, index=True, nullable=False)
    status = Column(String, nullable=False)         # running | paused | done | error | cancelled | stopped
    data = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)
    user = Column(String, nullable=True)


def get_config(db: Session, shoot_id: Optional[int]) -> dict:
    own = db.query(PipelineConfig).filter(PipelineConfig.key == f"shoot:{shoot_id}").first() if shoot_id else None
    row = own or db.query(PipelineConfig).filter(PipelineConfig.key == "default").first()
    steps = default_steps()
    if row:
        try:
            steps = json.loads(row.data).get("steps") or steps
        except Exception:
            pass
    return {"steps": _clean(steps), "own": bool(own), "shoot_id": shoot_id}


_runs: Dict[str, dict] = {}        # live runs, by id (also written to the database)
_lock = threading.Lock()


def _save(run: dict, force: bool = False):
    now = time.time()
    if not force and now - run.get("_saved", 0) < 2:
        return
    run["_saved"] = now
    run["updated"] = datetime.utcnow().isoformat() + "Z"
    db = SessionLocal()
    try:
        row = db.query(PipelineRun).filter(PipelineRun.id == run["id"]).first()
        data = json.dumps({k: v for k, v in run.items() if not k.startswith("_")})
        if row:
            row.status, row.data, row.updated_at = run["status"], data, datetime.utcnow()
        else:
            db.add(PipelineRun(id=run["id"], shoot_id=run["shoot_id"], status=run["status"], data=data,
                               user=run.get("user")))
        db.commit()
    finally:
        db.close()


def _load_run(run_id: str) -> Optional[dict]:
    with _lock:
        if run_id in _runs:
            return _runs[run_id]
    db = SessionLocal()
    try:
        row = db.query(PipelineRun).filter(PipelineRun.id == run_id).first()
        if not row:
            return None
        run = json.loads(row.data)
        with _lock:
            _runs[run_id] = run
        return run
    finally:
        db.close()


def _log(run: dict, text: str):
    run.setdefault("log", []).append({"at": datetime.utcnow().isoformat() + "Z", "text": text})
    run["log"] = run["log"][-200:]


# --------------------------------------------------------------------------
# the photos of a property
# --------------------------------------------------------------------------

def shoot_photos(db: Session, shoot_id: int) -> List[Video]:
    """Every photo in the property's folders, in name order, leaving out exports
    and anything already turned down (a reject)."""
    import listing_pack
    import photo_edit as pe
    import shoots
    paths = [f.path for f in db.query(shoots.ShootFolder).filter(shoots.ShootFolder.shoot_id == shoot_id).all()]
    out, seen = [], set()
    for p in paths:
        rows = (db.query(Video).filter(Video.media_type == "photo", Video.is_active.isnot(False),
                                       (Video.filepath == p) | Video.filepath.like(p.rstrip("/\\") + "/%")
                                       | Video.filepath.like(p.rstrip("/\\") + "\\%"))
                .order_by(Video.filename.asc()).all())
        for v in rows:
            import os
            if v.id in seen or listing_pack.EXPORT_DIR.search(os.path.dirname(v.filepath or "")):
                continue
            seen.add(v.id)
            out.append(v)
    rejects = {r.video_id for r in db.query(pe.PhotoEdit.video_id).filter(
        pe.PhotoEdit.video_id.in_([v.id for v in out]), pe.PhotoEdit.pick == -1).all()} if out else set()
    return [v for v in out if v.id not in rejects]


def _recipe(db, vid) -> dict:
    import ai_photo
    return ai_photo.saved_recipe(db, vid)


def _put(db, vid, patch: dict):
    import ai_photo
    cur = ai_photo.saved_recipe(db, vid)
    cur.update(patch)
    ai_photo.save_recipe(db, vid, cur)


def auto_scale(r: dict, aspect: float, most: float = 3.0) -> float:
    """How far to zoom in so a straightened photo still fills the frame (no
    empty corners) - the editor's autoScale."""
    import photo_edit as pe
    full = pe.Recipe(**{**r, "crop": [0, 0, 1, 1]})
    t = np.linspace(0, 1, 65, dtype=np.float32)
    u = np.concatenate([t, t, np.zeros_like(t), np.ones_like(t)])
    v = np.concatenate([np.zeros_like(t), np.ones_like(t), t, t])

    def ok(s):
        full.geo_scale = s
        a, b = pe.source_uv(u, v, full, aspect)
        return bool(np.all((a >= 0) & (a <= 1) & (b >= 0) & (b <= 1)))
    if ok(1.0):
        return 1.0
    lo, hi = 1.0, most
    if not ok(hi):
        return most
    for _ in range(22):
        mid = (lo + hi) / 2
        if ok(mid):
            hi = mid
        else:
            lo = mid
    return min(most, math.ceil(hi * 1000) / 1000)


# --------------------------------------------------------------------------
# each step
# --------------------------------------------------------------------------

class Pause(Exception):
    """The run waits for you here (a check, previews to choose from)."""
    def __init__(self, reason: str):
        self.reason = reason


def _progress(run, step, done, total, note=""):
    step["done"], step["total"] = done, total
    if note:
        step["note"] = note
    _save(run)


def step_cull(run, step, db, user, opts):
    import photo_edit as pe
    import cv2
    ids = run["photos"]
    info = {}
    for n, vid in enumerate(ids):
        _progress(run, step, n, len(ids))
        v = db.query(Video).filter(Video.id == vid).first()
        path = pe._resolve(v.filepath) if v else None
        if not path:
            continue
        try:
            rgb = pe._read_rgb(path, max_dim=1000)
        except Exception:
            continue
        g = cv2.cvtColor((np.clip(rgb, 0, 1) * 255).astype(np.uint8), cv2.COLOR_RGB2GRAY)
        small = cv2.resize(g, (9, 8), interpolation=cv2.INTER_AREA).astype(np.int16)
        # sharpness: how crisp the strongest edges are (second derivative against the first) - a
        # plain room with few edges is still sharp; a shaken or out-of-focus frame is not
        gf = g.astype(np.float32)
        gx, gy = cv2.Sobel(gf, cv2.CV_32F, 1, 0), cv2.Sobel(gf, cv2.CV_32F, 0, 1)
        gm = np.sqrt(gx * gx + gy * gy)
        top = gm > np.percentile(gm, 97)
        crisp = float(np.abs(cv2.Laplacian(gf, cv2.CV_32F))[top].mean() / (gm[top].mean() + 1e-6)) if top.any() else 0.0
        info[vid] = {
            "sharp": crisp,
            "bright": float((g > 250).mean()), "dark": float((g < 8).mean()), "mean": float(g.mean()),
            "hash": (small[:, 1:] > small[:, :-1]).flatten(), "folder": v.folder_id, "name": v.filename,
        }
    flags: Dict[int, List[str]] = {}
    if info:
        for vid, x in info.items():
            why = []
            if opts.get("blur") and x["sharp"] < 0.075:
                why.append("Blurry")
            # a white room is bright on purpose: only half the frame burnt out, or a frame that is nearly black
            if opts.get("exposure") and (x["bright"] > 0.5 or x["mean"] < 22 or x["dark"] > 0.6):
                why.append("Badly exposed")
            if why:
                flags[vid] = why
        if opts.get("duplicates"):
            vals = list(info.items())
            for i in range(len(vals)):
                for k in range(i + 1, len(vals)):
                    (a, xa), (b, xb) = vals[i], vals[k]
                    if xa["folder"] == xb["folder"] and int((xa["hash"] != xb["hash"]).sum()) <= 3:
                        worse = a if xa["sharp"] < xb["sharp"] else b
                        other = xb["name"] if worse == a else xa["name"]
                        flags.setdefault(worse, []).append(f"Same as {other}")
    run["flags"] = {str(k): v for k, v in flags.items()}
    if opts.get("reject"):
        for vid in flags:
            pe.set_pick(vid, pe.PickBody(pick=-1), db, user)
    run["photos"] = [i for i in ids if i not in flags]
    step["note"] = (f"{len(flags)} left out ({', '.join(sorted({w.split(' as ')[0] for v in flags.values() for w in v}))})"
                    if flags else "All photos kept")


def step_hdr(run, step, db, user, opts):
    import hdr
    ids = run["photos"]
    found = hdr.scan(hdr.ScanBody(video_ids=ids, mode=opts.get("mode") or "hdr"), db, user)
    groups = [[i["id"] for i in g["items"]] for g in found.get("groups", []) if len(g["items"]) >= 2]
    if not groups:
        step["note"] = "No brackets found - the photos go on as they are"
        return
    look = hdr.HdrLook(method=opts.get("method") or "natural", windows=float(opts.get("windows", 0.6)))
    body = hdr.MergeBody(groups=groups, look=look, mode=opts.get("mode") or "hdr",
                         flash=[[i["id"] for i in g["items"] if i.get("flash")] for g in found.get("groups", [])
                                if len(g["items"]) >= 2])
    start = hdr.merge(body, user)
    jid = start["job_id"]
    while True:
        time.sleep(1.5)
        with hdr._jobs_lock:
            st = dict(hdr.JOBS.get(jid) or {})
        _progress(run, step, st.get("done", 0), st.get("total", len(groups)))
        if run.get("cancel"):
            with hdr._jobs_lock:
                hdr.JOBS.get(jid, {})["cancelled"] = True
        if not st.get("running"):
            break
    made = [r["id"] for r in st.get("results", []) if r.get("id")]
    in_sets = {i for g in groups for i in g}
    run["photos"] = made + [i for i in ids if i not in in_sets]
    run.setdefault("merged_from", {}).update({str(m): g for m, g in zip(made, groups)})
    step["note"] = f"{len(made)} merged from {len(in_sets)} frames" + (
        f"; {len(st.get('errors', []))} could not be merged" if st.get("errors") else "")


def step_lens(run, step, db, user, opts):
    import photo_edit as pe
    ids, fixed = run["photos"], 0
    for n, vid in enumerate(ids):
        _progress(run, step, n, len(ids))
        try:
            l = pe.lens(vid, db, user)
        except Exception:
            continue
        patch = {}
        if l.get("distortion") is not None:
            patch["distortion"], patch["distortion2"] = l["distortion"], l.get("distortion2") or 0
        if l.get("vig_k"):
            patch["lens_vig_k"] = l["vig_k"]
        for k in ("ca_r", "ca_b"):
            if l.get(k) is not None:
                patch[k] = l[k]
        if patch:
            _put(db, vid, patch)
            fixed += 1
    step["note"] = f"{fixed} of {len(ids)} corrected"


def step_straighten(run, step, db, user, opts):
    import photo_edit as pe
    ids, done = run["photos"], 0
    for n, vid in enumerate(ids):
        _progress(run, step, n, len(ids))
        r = _recipe(db, vid)
        try:
            u = pe.upright(vid, opts.get("mode") or "auto", float(r.get("distortion") or 0), int(r.get("rotate") or 0),
                           float(r.get("distortion2") or 0), db, user)
        except HTTPException:
            continue
        except Exception as e:
            _log(run, f"Straighten skipped a photo: {e}")
            continue
        patch = {"straighten": u["straighten"], "persp_v": u["persp_v"], "persp_h": u["persp_h"]}
        try:
            w, h = _dims(db, vid)
            aspect = w / float(h)
        except Exception:
            aspect = 1.5
        patch["geo_scale"] = auto_scale({**r, **patch}, aspect)
        _put(db, vid, patch)
        done += 1
    step["note"] = f"{done} of {len(ids)} straightened"


KEEP_ON_PRESET = ("crop", "rotate", "flip_h", "flip_v", "straighten", "persp_v", "persp_h", "geo_scale", "distortion",
                  "distortion2", "ca_r", "ca_b", "lens_vig", "lens_vig_k", "masks", "spots", "removes", "gen_ref",
                  "gen_amount", "gen_light", "gen_view", "gen_look", "gen_geo")


def step_style(run, step, db, user, opts):
    import style
    done, skipped = 0, 0
    for n, vid in enumerate(run["photos"]):
        _progress(run, step, n, len(run["photos"]))
        try:
            style.apply(db, vid, user.username)
            done += 1
        except HTTPException:
            skipped += 1
        except Exception as e:
            db.rollback()
            skipped += 1
            _log(run, f"My style skipped a photo: {e}")
    step["note"] = f"{done} edited like your past work" + (f", {skipped} like none of yours" if skipped else "")


def step_preset(run, step, db, user, opts):
    import ai_photo
    import photo_edit as pe
    pid = opts.get("preset_id")
    row = db.query(pe.PhotoPreset).filter(pe.PhotoPreset.id == int(pid)).first() if pid else None
    if not row:
        step["note"] = "No preset chosen - skipped"
        return
    look = json.loads(row.recipe)
    for n, vid in enumerate(run["photos"]):
        _progress(run, step, n, len(run["photos"]))
        mine = _recipe(db, vid)
        merged = pe._for_photo(look, mine)
        for k in KEEP_ON_PRESET:                 # the straightening and lens work already done stay
            if k in mine:
                merged[k] = mine[k]
        ai_photo.save_recipe(db, vid, merged)
    step["note"] = f'"{row.name}" on {len(run["photos"])} photos'


def _dims(db, vid) -> tuple:
    """The photo's width and height as the editor's base picture has them (long side 1600)."""
    import photo_edit as pe
    v = db.query(Video).filter(Video.id == vid).first()
    rgb = pe._read_rgb(pe._resolve(v.filepath), max_dim=1600)
    return rgb.shape[1], rgb.shape[0]


def step_windows(run, step, db, user, opts):
    import photo_edit as pe
    ids, done = run["photos"], 0
    for n, vid in enumerate(ids):
        _progress(run, step, n, len(ids))
        try:
            w, h = _dims(db, vid)
            found = pe.find_windows(vid, pe.WindowsBody(w=w, h=h), db, user)
            if not found.get("count") or not found.get("candidates"):
                continue
            cur = _recipe(db, vid)
            res = pe.window_pull(vid, pe.PullBody(mask_ref=found["ref"], from_id=found["candidates"][0]["id"],
                                                  removes=cur.get("removes") or []), db, user)
            removes = [x for x in (cur.get("removes") or []) if x.get("kind") != "pull"]
            removes.append({"ref": res["ref"], "box": res["box"], "kind": "pull"})
            masks = [m for m in (cur.get("masks") or []) if m.get("name") != "Windows"]
            if len(masks) < 4:
                masks.append(pe.Mask(kind="brush", ref=found["ref"], name="Windows", highlights=-0.2).model_dump())
            _put(db, vid, {"removes": removes, "masks": masks})
            done += 1
        except HTTPException:
            continue
        except Exception as e:
            _log(run, f"Window view skipped a photo: {e}")
    step["note"] = f"Real view put in {done} of {len(ids)}" if done else "No darker frames to take a view from"


CLUTTER_ASK = (
    "This is a property photo for a listing. List loose clutter a careful photographer would have tidied away: "
    "bins, cables, bottles and items on counters and tables, toiletries, shoes, toys, washing, magnets, remotes, bags, "
    "hoses. NEVER list cars, vehicles or people (the photographer removes those by hand), furniture, fixed fittings, "
    "lights, plants that belong, art, or anything large. A short label saying where it is and a tight box in 0..1: "
    'x0, y0, x1, y1. Answer as {"items":[{"label":"...","box":[x0,y0,x1,y1]}]} - an empty list if it is tidy.')


def step_declutter(run, step, db, user, opts):
    import ai
    import ai_photo
    if not ai.enabled():
        step["note"] = "AI is off - skipped"
        return
    found = {}
    for n, vid in enumerate(run["photos"]):
        _progress(run, step, n, len(run["photos"]))
        try:
            v = ai_photo._video(db, vid)
            rgb = ai_photo.base_rgb(vid, ai_photo._path(v), 1600)
            d = ai.ask_json(CLUTTER_ASK + ai.GRID_NOTE, [ai.grid_b64(rgb)], "", max_tokens=2000, temperature=0.0)
        except Exception as e:
            _log(run, f"Clutter check skipped a photo: {e}")
            continue
        items = []
        for it in (d.get("items", []) if isinstance(d, dict) else []):
            label = str(it.get("label") or "")[:80]
            if any(w in label.lower() for w in ("car", "vehicle", "person", "people", "man", "woman", "child")):
                continue
            try:
                x0, y0, x1, y1 = (float(t) for t in it["box"][:4])
            except Exception:
                continue
            x0, x1 = sorted((min(1.0, max(0.0, x0)), min(1.0, max(0.0, x1))))
            y0, y1 = sorted((min(1.0, max(0.0, y0)), min(1.0, max(0.0, y1))))
            box = [x0, y0, x1, y1]
            if not 1e-5 < (x1 - x0) * (y1 - y0) < 0.25:
                continue
            items.append({"label": label or "Item", "box": box})
        if items:
            found[str(vid)] = items[:30]
    run["clutter"] = found
    n = sum(len(x) for x in found.values())
    step["note"] = (f"{n} {'thing' if n == 1 else 'things'} on {len(found)} {'photo' if len(found) == 1 else 'photos'} - tick what goes on the review page"
                    if found else "Nothing to tidy")


def step_fixes(run, step, db, user, opts):
    import ai
    import fixes
    if not ai.enabled():
        step["note"] = "AI is off - skipped"
        return
    want = {k: bool(opts.get(k)) for k in ("grass", "pool", "screen", "fire", "light")}
    if want["fire"]:
        import ai_image
        want["fire"] = ai_image.enabled()
    counts: Dict[str, int] = {}
    for n, vid in enumerate(run["photos"]):
        _progress(run, step, n, len(run["photos"]))
        try:
            for k in fixes.apply_all(db, vid, want, user.username):
                counts[k] = counts.get(k, 0) + 1
        except Exception as e:
            _log(run, f"Fixes skipped a photo: {e}")
    names = {"grass": "grass", "pool": "pool", "screen": "screens", "fire": "fire", "light": "mixed light"}
    step["note"] = ", ".join(f"{names[k]} on {v}" for k, v in counts.items()) or "Nothing to fix"


def _room_key(db, shoot_id) -> str:
    import shoots
    paths = [f.path for f in db.query(shoots.ShootFolder).filter(shoots.ShootFolder.shoot_id == shoot_id).all()]
    fids = [f for f in shoots._folder_ids(db, paths) if f]
    return f"folder:{fids[0]}" if fids else f"shoot:{shoot_id}"


def step_rooms(run, step, db, user, opts):
    import ai
    import ai_photo
    if not ai.enabled():
        step["note"] = "AI is off - skipped"
        return
    key = _room_key(db, run["shoot_id"])
    j = ai_photo._job("rooms", len(run["photos"]), user.username)
    t = threading.Thread(target=ai_photo._sort_run, args=(j, ai_photo.SortBody(key=key, video_ids=run["photos"], merge=True)),
                         daemon=True)
    t.start()
    while t.is_alive():
        time.sleep(1)
        _progress(run, step, j["done"], j["total"])
    run["rooms"] = {name: ids for name, ids in j["rooms"].items()}
    step["note"] = ", ".join(f"{len(v)} {k}" for k, v in sorted(j["rooms"].items())) or "No rooms found"


def _look_targets(run, db, opts) -> Dict[str, List[int]]:
    """look id -> the photos it goes on."""
    ids = run["photos"]
    rooms = run.get("rooms") or {}
    room_of = {vid: name for name, vids in rooms.items() for vid in vids}
    which = opts.get("which") or "exterior"
    look = opts.get("look_id")
    out: Dict[str, List[int]] = {}
    if which == "rooms":
        for vid in ids:
            lid = (opts.get("room_looks") or {}).get(room_of.get(vid, ""))
            if lid:
                out.setdefault(lid, []).append(vid)
        return out
    if not look:
        return {}
    if which == "all":
        chosen = ids
    elif which == "cover":
        import shoots
        s = db.query(shoots.Shoot).filter(shoots.Shoot.id == run["shoot_id"]).first()
        cov = s.cover_video_id if s and s.cover_video_id in ids else None
        chosen = [cov] if cov else [i for i in ids if room_of.get(i) in EXTERIOR][:1] or ids[:1]
    else:
        chosen = [i for i in ids if room_of.get(i) in EXTERIOR]
    if chosen:
        out[look] = chosen
    return out


def step_look(run, step, db, user, opts):
    import ai_image
    import ai_photo
    if not ai_image.enabled():
        step["note"] = "Image generation is off - skipped"
        return
    targets = _look_targets(run, db, opts)
    if not targets:
        step["note"] = ("No look chosen" if not opts.get("look_id") and opts.get("which") != "rooms"
                        else "No photos for the look (sort into rooms first, or choose All photos)")
        return
    preview = bool(opts.get("preview"))
    total = sum(len(v) for v in targets.values())
    done = 0
    painted = run.setdefault("looks", {})
    for lid, vids in targets.items():
        j = ai_photo._job("gen", len(vids), user.username)
        j["items"] = []
        body = ai_photo.GenBody(video_ids=vids, look_id=lid, apply=not preview, preview=preview, pipeline=run["id"])
        t = threading.Thread(target=ai_photo._gen_run, args=(j, body), daemon=True)
        t.start()
        while t.is_alive():
            time.sleep(1.5)
            _progress(run, step, done + j["done"] + j["failed"], total)
        for it in j["items"]:
            painted[str(it["video_id"])] = {"ref": it["ref"], "look": it["look"], "look_id": lid, "seed": it.get("seed"),
                                            "preview": preview, "loose": it.get("loose", False)}
        done += len(vids)
        if j.get("error"):
            _log(run, f"Look: {j['error']}")
    step["note"] = f"{len(painted)} painted" + (" as quick previews" if preview else "")
    if preview and painted:
        raise Pause("previews")


def step_pause(run, step, db, user, opts):
    raise Pause("check")


def step_export(run, step, db, user, opts):
    import listing_pack
    jid = uuid.uuid4().hex[:12]
    j = {"id": jid, "state": "running", "step": "Starting", "done": 0, "total": 0, "current": "", "errors": [],
         "error": "", "user": user.username, "reel": False}
    with listing_pack._lock:
        listing_pack.JOBS[jid] = j
    body = listing_pack.PackBody(rooms=False, social=bool(opts.get("social")), reel=bool(opts.get("reel")),
                                 text=bool(opts.get("text")), portal_width=int(opts.get("portal_width") or 2048),
                                 portal_kb=int(opts.get("portal_kb") or 1000))
    t = threading.Thread(target=listing_pack._run, args=(j, run["shoot_id"], body), daemon=True)
    t.start()
    while t.is_alive():
        time.sleep(1.5)
        _progress(run, step, j.get("done", 0), j.get("total", 0), j.get("step", ""))
    if j.get("state") == "error":
        raise RuntimeError(j.get("error") or "The listing pack could not be made")
    run["export"] = {"job": jid}
    step["note"] = "Ready to download"


RUNNERS = {"cull": step_cull, "hdr": step_hdr, "lens": step_lens, "straighten": step_straighten, "preset": step_preset,
           "windows": step_windows, "declutter": step_declutter, "fixes": step_fixes, "style": step_style, "rooms": step_rooms, "look": step_look,
           "pause": step_pause, "export": step_export}


# --------------------------------------------------------------------------
# running
# --------------------------------------------------------------------------

def _worker(run_id: str):
    run = _load_run(run_id)
    if not run:
        return
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == run.get("user")).first()
        if not user:
            raise RuntimeError("The account that started this run is gone")
        run["status"] = "running"
        run.pop("pause", None)
        _save(run, True)
        while run["at"] < len(run["steps"]):
            if run.get("cancel"):
                run["status"] = "cancelled"
                _log(run, "Stopped")
                break
            step = run["steps"][run["at"]]
            if not step.get("on"):
                run["at"] += 1
                continue
            step["state"] = "running"
            step["started"] = datetime.utcnow().isoformat() + "Z"
            _save(run, True)
            try:
                RUNNERS[step["id"]](run, step, db, user, step.get("opts") or {})
                step["state"] = "done"
                _log(run, f"{BY_ID[step['id']]['name']}: {step.get('note') or 'done'}")
                run["at"] += 1
            except Pause as p:
                step["state"] = "waiting"
                run["status"] = "paused"
                run["pause"] = p.reason
                _log(run, f"{BY_ID[step['id']]['name']}: waiting for you")
                _save(run, True)
                return
            except Exception as e:
                db.rollback()
                step["state"] = "error"
                step["note"] = str(getattr(e, "detail", e))[:300]
                run["status"] = "error"
                _log(run, f"{BY_ID[step['id']]['name']} failed: {step['note']}")
                print(f"pipeline: {step['id']} failed: {traceback.format_exc()}", flush=True)
                _save(run, True)
                return
            _save(run, True)
        if run["status"] == "running":
            run["status"] = "done"
            _log(run, "Finished")
    except Exception as e:
        run["status"] = "error"
        _log(run, f"Stopped: {e}")
    finally:
        db.close()
        _save(run, True)


def _start(run_id: str):
    threading.Thread(target=_worker, args=(run_id,), daemon=True).start()


# --------------------------------------------------------------------------
# the API
# --------------------------------------------------------------------------

@router.get("/steps")
def steps(current_user: User = Depends(get_current_user)):
    return {"steps": [{k: v for k, v in s.items()} for s in CATALOG]}


def _key(shoot_id: str) -> Optional[int]:
    if shoot_id == "default":
        return None
    try:
        return int(shoot_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Not a property")


@router.get("/config/{shoot_id}")
def read_config(shoot_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return get_config(db, _key(shoot_id))


class ConfigBody(BaseModel):
    steps: List[Dict[str, Any]] = Field(default_factory=list, max_length=40)


@router.put("/config/{shoot_id}")
def write_config(shoot_id: str, body: ConfigBody, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    sid = _key(shoot_id)
    key = f"shoot:{sid}" if sid else "default"
    row = db.query(PipelineConfig).filter(PipelineConfig.key == key).first()
    data = json.dumps({"steps": _clean(body.steps)})
    if row:
        row.data, row.updated_at, row.updated_by = data, datetime.utcnow(), current_user.username
    else:
        db.add(PipelineConfig(key=key, data=data, updated_by=current_user.username))
    db.commit()
    return get_config(db, sid)


@router.delete("/config/{shoot_id}")
def reset_config(shoot_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """This property goes back to the studio's default steps."""
    sid = _key(shoot_id)
    if sid:
        db.query(PipelineConfig).filter(PipelineConfig.key == f"shoot:{sid}").delete()
        db.commit()
    return get_config(db, sid)


class RunBody(BaseModel):
    shoot_id: int
    video_ids: List[int] = Field(default_factory=list, max_length=3000)   # only these (default: all of the property)


@router.post("/run")
def start_run(body: RunBody, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    import shoots
    if current_user.role == "viewer":
        raise HTTPException(status_code=403, detail="Your account has view-only access.")
    if not db.query(shoots.Shoot).filter(shoots.Shoot.id == body.shoot_id).first():
        raise HTTPException(status_code=404, detail="No such property")
    for r in _runs.values():
        if r["shoot_id"] == body.shoot_id and r["status"] == "running":
            raise HTTPException(status_code=409, detail="This property's pipeline is already running")
    photos = [v.id for v in shoot_photos(db, body.shoot_id)]
    if body.video_ids:
        want = set(body.video_ids)
        photos = [i for i in photos if i in want]
    if not photos:
        raise HTTPException(status_code=400, detail="This property has no photos yet - add its folder first")
    cfg = get_config(db, body.shoot_id)
    run = {
        "id": uuid.uuid4().hex[:12], "shoot_id": body.shoot_id, "user": current_user.username, "status": "running",
        "created": datetime.utcnow().isoformat() + "Z", "at": 0, "photos": photos, "started_with": list(photos),
        "steps": [{**s, "name": BY_ID[s["id"]]["name"], "state": "waiting" if s["on"] else "off", "note": "",
                   "done": 0, "total": 0} for s in cfg["steps"]],
        "log": [], "flags": {}, "rooms": {}, "looks": {}, "clutter": {},
    }
    with _lock:
        _runs[run["id"]] = run
    _save(run, True)
    _start(run["id"])
    return _public(run)


def _public(run: dict) -> dict:
    return {k: v for k, v in run.items() if not k.startswith("_")}


@router.get("/runs")
def list_runs(shoot_id: Optional[int] = None, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    q = db.query(PipelineRun)
    if shoot_id is not None:
        q = q.filter(PipelineRun.shoot_id == shoot_id)
    rows = q.order_by(PipelineRun.created_at.desc()).limit(20).all()
    out = []
    for r in rows:
        live = _runs.get(r.id)
        d = live or json.loads(r.data)
        out.append({"id": r.id, "shoot_id": r.shoot_id, "status": d.get("status"), "created": d.get("created"),
                    "updated": d.get("updated"), "photos": len(d.get("photos") or []), "pause": d.get("pause"),
                    "step": next((s["name"] for s in d.get("steps", []) if s.get("state") in ("running", "waiting", "error")), None)})
    return {"runs": out}


@router.get("/run/{run_id}")
def read_run(run_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    run = _load_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="No such run")
    out = _public(run)
    # the file names, for the review page (merged photos are new files, made during the run)
    ids = {int(i) for i in [*run.get("photos", []), *run.get("started_with", []), *(run.get("flags") or {}),
                            *(run.get("left_out") or {}), *(run.get("merged_from") or {})]}
    out["names"] = {str(v.id): v.filename for v in db.query(Video.id, Video.filename).filter(Video.id.in_(ids)).all()} if ids else {}
    return out


class ContinueBody(BaseModel):
    full_size: List[int] = Field(default_factory=list, max_length=3000)   # previews to paint full size
    drop_looks: List[int] = Field(default_factory=list, max_length=3000)  # previews not wanted: no look on these
    leave_out: List[int] = Field(default_factory=list, max_length=3000)   # photos taken out of the rest of the run
    put_back: List[int] = Field(default_factory=list, max_length=3000)    # photos left out earlier, back in


@router.post("/run/{run_id}/continue")
def continue_run(run_id: str, body: ContinueBody, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    run = _load_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="No such run")
    if run["status"] == "running":
        raise HTTPException(status_code=409, detail="It is running already")
    _choose(run, body.leave_out, body.put_back)
    step = run["steps"][run["at"]] if run["at"] < len(run["steps"]) else None
    if step and step["state"] == "waiting" and run.get("pause") == "previews":
        # the chosen previews become the real thing in the background, as the next step's first job
        run["full_size"] = [i for i in body.full_size if str(i) in run.get("looks", {})]
        for i in body.drop_looks:
            run.get("looks", {}).pop(str(i), None)
        step["state"] = "done"
        run["at"] += 1
        if run["full_size"]:
            run["steps"].insert(run["at"], {"id": "fullsize", "name": "Paint the chosen looks full size", "on": True,
                                             "opts": {}, "state": "waiting", "note": "", "done": 0, "total": 0})
    elif step and step["state"] in ("waiting", "error"):
        if step["state"] == "waiting":
            step["state"] = "done"
            run["at"] += 1
        else:
            step["state"] = "waiting"          # an error: try that step again
    run["status"] = "running"
    run.pop("cancel", None)
    _save(run, True)
    _start(run_id)
    return _public(run)


def _choose(run: dict, leave_out: List[int], put_back: List[int]):
    if leave_out:
        out = set(leave_out)
        run["photos"] = [i for i in run["photos"] if i not in out]
        for i in out:
            run.setdefault("left_out", {})[str(i)] = "You left it out"
    if put_back:
        known = set(run.get("started_with", [])) | {int(k) for k in (run.get("merged_from") or {})}
        for i in put_back:
            if i in known and i not in run["photos"]:
                run["photos"].append(i)
                (run.get("flags") or {}).pop(str(i), None)
                (run.get("left_out") or {}).pop(str(i), None)


class ChooseBody(BaseModel):
    leave_out: List[int] = Field(default_factory=list, max_length=3000)
    put_back: List[int] = Field(default_factory=list, max_length=3000)


@router.post("/run/{run_id}/photos")
def choose_photos(run_id: str, body: ChooseBody, current_user: User = Depends(get_current_user)):
    """Leave photos out of the rest of the run, or put them back, while it waits for you."""
    run = _load_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="No such run")
    if run["status"] == "running":
        raise HTTPException(status_code=409, detail="Wait until it stops for you")
    _choose(run, body.leave_out, body.put_back)
    _save(run, True)
    return _public(run)


def step_fullsize(run, step, db, user, opts):
    """The previews you picked, painted at full size with the same seed and put on the photos."""
    import ai_photo
    ids = run.pop("full_size", []) or []
    looks = run.get("looks", {})
    for n, vid in enumerate(ids):
        _progress(run, step, n, len(ids))
        it = looks.get(str(vid))
        if not it:
            continue
        j = ai_photo._job("gen", 1, user.username)
        j["items"] = []
        ai_photo._gen_run(j, ai_photo.GenBody(video_ids=[vid], look_id=it["look_id"], seed=it.get("seed"), apply=True,
                                              pipeline=run["id"]))
        if j["items"]:
            looks[str(vid)] = {**it, "ref": j["items"][-1]["ref"], "preview": False, "loose": j["items"][-1].get("loose")}
    step["note"] = f"{len(ids)} painted full size"


RUNNERS["fullsize"] = step_fullsize
BY_ID["fullsize"] = {"id": "fullsize", "name": "Paint the chosen looks full size"}


@router.post("/run/{run_id}/cancel")
def cancel_run(run_id: str, current_user: User = Depends(get_current_user)):
    run = _load_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="No such run")
    if run["status"] == "running":
        run["cancel"] = True
    else:
        run["status"] = "cancelled"
        _save(run, True)
    return _public(run)


class TidyBody(BaseModel):
    video_id: int
    items: List[int] = Field(..., min_length=1, max_length=40)     # which of the photo's suggestions go


@router.post("/run/{run_id}/tidy")
def tidy(run_id: str, body: TidyBody, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """The clutter you ticked on the review page, removed (traced and filled, one patch each,
    so any one can be taken back in the editor's Remove list)."""
    import ai_photo
    run = _load_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="No such run")
    found = (run.get("clutter") or {}).get(str(body.video_id)) or []
    boxes = [found[i]["box"] for i in body.items if 0 <= i < len(found)]
    if not boxes:
        raise HTTPException(status_code=400, detail="Nothing ticked")
    cur = _recipe(db, body.video_id)
    res = ai_photo.declutter(body.video_id, ai_photo.DeclutterBody(boxes=boxes, removes=cur.get("removes") or []),
                             db, current_user)
    patches = [p if isinstance(p, dict) else p.model_dump() for p in res.get("patches", [])]
    _put(db, body.video_id, {"removes": (cur.get("removes") or []) + patches})
    left = [x for i, x in enumerate(found) if i not in set(body.items)]
    if left:
        run["clutter"][str(body.video_id)] = left
    else:
        run["clutter"].pop(str(body.video_id), None)
    run.setdefault("tidied", {})[str(body.video_id)] = len(patches) + int((run.get("tidied") or {}).get(str(body.video_id), 0))
    _save(run, True)
    return {"removed": len(patches), "failed": res.get("failed", 0), "run": _public(run)}


def _resume_after_restart():
    """Runs that were going when Zerko stopped are marked stopped; Carry on starts the step again."""
    db = SessionLocal()
    try:
        for row in db.query(PipelineRun).filter(PipelineRun.status == "running").all():
            d = json.loads(row.data)
            d["status"] = "stopped"
            for s in d.get("steps", []):
                if s.get("state") == "running":
                    s["state"] = "error"
                    s["note"] = "Zerko restarted during this step - carry on to do it again"
            row.status, row.data = "stopped", json.dumps(d)
        db.commit()
    finally:
        db.close()


def install(app):
    Base.metadata.create_all(bind=engine, tables=[PipelineConfig.__table__, PipelineRun.__table__])
    try:
        _resume_after_restart()
    except Exception as e:
        print(f"pipeline: could not tidy old runs: {e}", flush=True)
    app.include_router(router)
