"""Find true duplicates by content, not by name.

Same-named files across shoots are usually different clips (every camera
restarts at C0001), and differently-named files are often the same take copied
twice. Only the bytes settle it.

Hashing 587 GB end to end would take hours and hammer the drive, so this uses
a cheap signature first: size, then the first and last 4 MB plus the middle.
Two different videos effectively never share all of that - and anything that
does can be confirmed with a full hash before you delete a thing.

    python dedupe.py --scan          hash what still needs hashing
    python dedupe.py --report        show duplicate groups
    python dedupe.py --verify        full-hash the candidate groups
"""

import hashlib
import os
import sys
import time
from datetime import datetime

from database import init_db, SessionLocal, Video

CHUNK = 4 * 1024 * 1024
DB_ROOT = os.environ.get("DB_ROOT")
REAL_ROOT = os.environ.get("MEDIA_ROOT")


def real_path(stored):
    if not stored:
        return stored
    if DB_ROOT and REAL_ROOT and stored.startswith(DB_ROOT):
        return REAL_ROOT.rstrip("/") + stored[len(DB_ROOT):]
    return stored


def quick_signature(path, size):
    """size + head + middle + tail. Fast, and enough to group candidates."""
    h = hashlib.sha256()
    h.update(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(CHUNK))
        if size > CHUNK * 3:
            f.seek(size // 2)
            h.update(f.read(CHUNK))
            f.seek(max(0, size - CHUNK))
            h.update(f.read(CHUNK))
    return "q:" + h.hexdigest()


def full_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
    return "f:" + h.hexdigest()


def scan(limit=None, deadline=None):
    db = SessionLocal()
    q = db.query(Video).filter(Video.file_hash.is_(None), Video.is_active.isnot(False))
    todo = q.limit(limit).all() if limit else q.all()
    done = missing = 0
    for v in todo:
        if deadline and time.time() > deadline:
            break
        p = real_path(v.filepath)
        try:
            if not p or not os.path.exists(p):
                v.file_hash = "missing"
                missing += 1
            else:
                size = os.path.getsize(p)
                v.file_hash = quick_signature(p, size)
                if not v.file_size:
                    v.file_size = size
                done += 1
            v.hashed_at = datetime.utcnow()
        except Exception as e:
            print(f"    hash failed: {v.filename}: {e}")
            v.file_hash = "error"
        if (done + missing) % 50 == 0:
            db.commit()
    db.commit()
    left = db.query(Video).filter(Video.file_hash.is_(None),
                                  Video.is_active.isnot(False)).count()
    print(f"  hashed {done}, missing {missing}, remaining {left}")
    db.close()
    return left


def groups(db):
    """Duplicate groups, newest-first inside each group."""
    rows = db.query(Video).filter(
        Video.file_hash.isnot(None),
        Video.file_hash.notin_(["missing", "error"]),
        Video.is_active.isnot(False),
    ).all()
    by_hash = {}
    for v in rows:
        by_hash.setdefault(v.file_hash, []).append(v)
    return {h: vs for h, vs in by_hash.items() if len(vs) > 1}


def report():
    db = SessionLocal()
    g = groups(db)
    total_waste = 0
    print(f"\n  {len(g)} duplicate group(s)\n")
    for h, vs in sorted(g.items(), key=lambda kv: -(kv[1][0].file_size or 0)):
        size = vs[0].file_size or 0
        waste = size * (len(vs) - 1)
        total_waste += waste
        print(f"  {len(vs)}x  {size / 1e9:.2f} GB each  (reclaim {waste / 1e9:.2f} GB)")
        for v in vs:
            print(f"        [{v.id}] {v.filepath}")
    print(f"\n  Total reclaimable: {total_waste / 1e9:.2f} GB\n")
    db.close()
    return total_waste


def verify():
    """Confirm candidate groups with a full hash before anyone deletes."""
    db = SessionLocal()
    g = groups(db)
    confirmed = 0
    for h, vs in g.items():
        if h.startswith("f:"):
            continue
        for v in vs:
            p = real_path(v.filepath)
            if p and os.path.exists(p):
                try:
                    v.file_hash = full_hash(p)
                    confirmed += 1
                except Exception as e:
                    print(f"    verify failed: {v.filename}: {e}")
        db.commit()
    print(f"  fully hashed {confirmed} files in candidate groups")
    db.close()


if __name__ == "__main__":
    init_db()
    if "--report" in sys.argv:
        report()
    elif "--verify" in sys.argv:
        verify()
        report()
    else:
        lim = None
        for a in sys.argv:
            if a.startswith("--limit="):
                lim = int(a.split("=", 1)[1])
        secs = 150
        for a in sys.argv:
            if a.startswith("--seconds="):
                secs = int(a.split("=", 1)[1])
        scan(lim, deadline=time.time() + secs)
