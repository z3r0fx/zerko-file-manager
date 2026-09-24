"""Search by what is in the picture (replaces Excire Search / Lightroom's AI search),
and the picture fingerprints "My style" matches edits with.

OpenAI's CLIP (ViT-B/32, the quantised ONNX export from huggingface.co/Xenova,
about 155 MB with its tokenizer, fetched the first time) turns every photo's
thumbnail and every typed search into the same kind of vector; the closest
photos are the answer. Runs on the CPU in a background thread; the vectors are
kept in the database (a photo is looked at again only when its file changes).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Column, Integer, LargeBinary, String
from sqlalchemy.orm import Session

from auth import get_current_user
from database import Base, IndexedFolder, SessionLocal, User, Video, engine, get_db

router = APIRouter(prefix="/api/search", tags=["search"])

MODEL_DIR = Path(__file__).resolve().parent / "models" / "clip"
HF = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/"
FILES = {"vision": ("onnx/vision_model_quantized.onnx", 89117001), "text": ("onnx/text_model_quantized.onnx", 64504507),
         "tokenizer": ("tokenizer.json", 2224119)}
MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32)
STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32)
_resolve = None
_media_root: Optional[Path] = None


class ClipVec(Base):
    __tablename__ = "clip_vectors"
    video_id = Column(Integer, primary_key=True)
    stamp = Column(String, nullable=False)          # the thumbnail it was made from
    vec = Column(LargeBinary, nullable=False)       # 512 float16, unit length


_dl = threading.Lock()


def _file(key: str) -> Path:
    rel, size = FILES[key]
    p = MODEL_DIR / Path(rel).name
    with _dl:
        if p.is_file() and p.stat().st_size == size:
            return p
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".part")
        with urllib.request.urlopen(HF + rel, timeout=120) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        if tmp.stat().st_size != size:
            tmp.unlink(missing_ok=True)
            raise HTTPException(status_code=502, detail=f"The search model download ({p.name}) was incomplete - try again")
        tmp.replace(p)
    return p


def ready() -> bool:
    return all((MODEL_DIR / Path(r).name).is_file() and (MODEL_DIR / Path(r).name).stat().st_size == s for r, s in FILES.values())


@lru_cache(maxsize=1)
def _sessions():
    import onnxruntime as ort
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = max(1, (os.cpu_count() or 4) // 2)
    v = ort.InferenceSession(str(_file("vision")), opts, providers=["CPUExecutionProvider"])
    t = ort.InferenceSession(str(_file("text")), opts, providers=["CPUExecutionProvider"])
    return v, t


# ---- CLIP's tokenizer (byte-level BPE), from tokenizer.json ------------------------------------

@lru_cache(maxsize=1)
def _bpe():
    d = json.loads(_file("tokenizer").read_text(encoding="utf-8"))
    vocab = d["model"]["vocab"]
    merges = d["model"]["merges"]
    ranks = {tuple(m.split(" ") if isinstance(m, str) else m): i for i, m in enumerate(merges)}
    bs = list(range(ord("!"), ord("~") + 1)) + list(range(ord("¡"), ord("¬") + 1)) + list(range(ord("®"), ord("ÿ") + 1))
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    byte_enc = dict(zip(bs, [chr(c) for c in cs]))
    return vocab, ranks, byte_enc


def _word_bpe(word: str, ranks) -> List[str]:
    parts = list(word[:-1]) + [word[-1] + "</w>"]
    while len(parts) > 1:
        pairs = [(ranks.get((parts[i], parts[i + 1]), 1e12), i) for i in range(len(parts) - 1)]
        r, i = min(pairs)
        if r == 1e12:
            break
        parts = parts[:i] + [parts[i] + parts[i + 1]] + parts[i + 2:]
    return parts


def tokenize(text: str, length: int = 77) -> np.ndarray:
    vocab, ranks, byte_enc = _bpe()
    text = re.sub(r"\s+", " ", text.lower()).strip()
    ids = [vocab["<|startoftext|>"]]
    for w in re.findall(r"'s|'t|'re|'ve|'m|'ll|'d|[a-z]+|[0-9]|[^\sa-z0-9]+", text):
        w = "".join(byte_enc[b] for b in w.encode("utf-8"))
        for piece in _word_bpe(w, ranks):
            if piece in vocab:
                ids.append(vocab[piece])
    ids = ids[:length - 1] + [vocab["<|endoftext|>"]]
    out = np.full((1, length), vocab["<|endoftext|>"], np.int64)
    out[0, :len(ids)] = ids
    return out


def text_vec(text: str) -> np.ndarray:
    _, t = _sessions()
    ids = tokenize(text)
    feeds = {}
    for i in t.get_inputs():
        if i.name == "input_ids":
            feeds[i.name] = ids
        elif i.name == "attention_mask":
            # CLIP reads the text up to its end token (causal), so the padding after it changes nothing
            feeds[i.name] = np.ones_like(ids)
    outs = t.run(None, feeds)
    names = [o.name for o in t.get_outputs()]
    v = outs[names.index("text_embeds")] if "text_embeds" in names else outs[0]
    v = np.asarray(v, np.float32).reshape(-1)[:512]
    return v / (np.linalg.norm(v) + 1e-8)


def image_vec(rgb: np.ndarray) -> np.ndarray:
    """rgb: uint8 or 0..1 float, any size."""
    import cv2
    a = rgb.astype(np.float32) / (255.0 if rgb.dtype == np.uint8 else 1.0)
    h, w = a.shape[:2]
    s = 224 / min(h, w)
    a = cv2.resize(a, (max(224, round(w * s)), max(224, round(h * s))), interpolation=cv2.INTER_CUBIC if s > 1 else cv2.INTER_AREA)
    y0, x0 = (a.shape[0] - 224) // 2, (a.shape[1] - 224) // 2
    a = (a[y0:y0 + 224, x0:x0 + 224] - MEAN) / STD
    v, _ = _sessions()
    outs = v.run(None, {v.get_inputs()[0].name: a.transpose(2, 0, 1)[None].astype(np.float32)})
    names = [o.name for o in v.get_outputs()]
    e = outs[names.index("image_embeds")] if "image_embeds" in names else outs[0]
    e = np.asarray(e, np.float32).reshape(-1)[:512]
    return e / (np.linalg.norm(e) + 1e-8)


# ---- the index -------------------------------------------------------------

def _thumb_path(v: Video) -> Optional[str]:
    if not v.thumbnail_path or not _media_root:
        return None
    p = _media_root / "thumbnails" / Path(v.thumbnail_path).name
    return str(p) if p.is_file() else None


_state = {"running": False, "done": 0, "total": 0}
_lock = threading.Lock()


def _index_run():
    import cv2
    db = SessionLocal()
    try:
        have = {r.video_id: r.stamp for r in db.query(ClipVec.video_id, ClipVec.stamp).all()}
        todo = []
        for v in db.query(Video).filter(Video.is_active.isnot(False), Video.media_type.in_(("photo", "video"))).all():
            t = _thumb_path(v)
            if not t:
                continue
            stamp = f"{Path(t).name}:{int(os.stat(t).st_mtime)}"
            if have.get(v.id) != stamp:
                todo.append((v.id, t, stamp))
        _state.update(total=len(todo), done=0)
        for n, (vid, t, stamp) in enumerate(todo):
            try:
                bgr = cv2.imread(t, cv2.IMREAD_COLOR)
                if bgr is None:
                    continue
                vec = image_vec(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)).astype(np.float16).tobytes()
                db.merge(ClipVec(video_id=vid, stamp=stamp, vec=vec))
                if n % 25 == 0:
                    db.commit()
            except Exception as e:
                db.rollback()
                print(f"clip: {vid}: {e}", flush=True)
            _state["done"] = n + 1
        db.commit()
    finally:
        db.close()
        _state["running"] = False


def index_later():
    """Look at every photo not seen yet, in the background (one at a time)."""
    with _lock:
        if _state["running"]:
            return
        _state["running"] = True
    threading.Thread(target=_index_run, daemon=True, name="clip-index").start()


def vectors(db: Session, ids: Optional[List[int]] = None) -> Dict[int, np.ndarray]:
    q = db.query(ClipVec)
    if ids is not None:
        q = q.filter(ClipVec.video_id.in_(ids))
    return {r.video_id: np.frombuffer(r.vec, np.float16).astype(np.float32) for r in q.all()}


@router.get("/content")
def content(q: str, limit: int = 60, folder_id: Optional[int] = None, db: Session = Depends(get_db),
            current_user: User = Depends(get_current_user)):
    """The photos and videos that look most like the words."""
    q = (q or "").strip()
    if len(q) < 2:
        return {"results": [], "indexing": _state}
    try:
        tv = text_vec(q if len(q.split()) > 2 else f"a photo of {q}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search by content is not ready: {e}")
    index_later()
    rows = db.query(ClipVec.video_id, ClipVec.vec)
    if folder_id:
        f = db.query(IndexedFolder).filter(IndexedFolder.id == folder_id).first()
        if f:
            p = f.path.rstrip("/\\")
            fids = [x.id for x in db.query(IndexedFolder.id).filter(
                (IndexedFolder.path == p) | IndexedFolder.path.like(p + "/%") | IndexedFolder.path.like(p + "\\%")).all()]
            rows = rows.join(Video, Video.id == ClipVec.video_id).filter(Video.folder_id.in_(fids))
    rows = rows.all()
    if not rows:
        return {"results": [], "indexing": _state}
    ids = np.array([r.video_id for r in rows])
    m = np.stack([np.frombuffer(r.vec, np.float16) for r in rows]).astype(np.float32)
    sims = m @ tv
    order = np.argsort(-sims)[:max(1, min(limit, 300))]
    # only the ones clearly closer than the rest: CLIP scores sit in a narrow band
    cut = max(float(sims[order[0]]) - 0.035, float(np.percentile(sims, 90)), 0.22)
    keep = [int(i) for i in order if sims[i] >= cut]
    vids = {v.id: v for v in db.query(Video).filter(Video.id.in_([int(ids[i]) for i in keep])).all()}
    out = []
    for i in keep:
        v = vids.get(int(ids[i]))
        if v and v.is_active is not False:
            out.append({"id": v.id, "filename": v.filename, "thumbnail_path": v.thumbnail_path, "media_type": v.media_type,
                        "folder_id": v.folder_id, "score": round(float(sims[i]), 4)})
    return {"results": out, "indexing": _state}


@router.get("/status")
def status(current_user: User = Depends(get_current_user)):
    return {"ready": ready(), **_state}


def install(app, media_root, resolve_media_path):
    global _media_root, _resolve
    _media_root = Path(media_root) if media_root else None
    _resolve = resolve_media_path
    Base.metadata.create_all(bind=engine, tables=[ClipVec.__table__])
    app.include_router(router)
    if ready():
        # new photos since the last start are looked at in the background
        threading.Timer(60, index_later).start()
