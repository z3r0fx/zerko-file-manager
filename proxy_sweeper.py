"""Proxies as a background job that looks after itself.

Before this, proxies were made for uploads and for whoever pressed "generate
missing", and nothing else. Footage copied onto the drive and picked up by a
rescan never got one; a failed encode stayed failed; a proxy deleted from disk
still counted as done. This thread sweeps every few minutes and puts that
right, a bounded batch at a time, so the queue never balloons.

Mode, from ZERKO_AUTO_PROXIES:
  new  (default) - clips added in the last NEW_DAYS days. The library-wide
                   backfill stays a deliberate button press, as it always was:
                   it re-encodes everything and takes days.
  all            - every video without a proxy
  off            - sweeper does repairs only (missing files, stranded jobs)
"""
import os
import threading
import time
from datetime import datetime, timedelta

from database import SessionLocal, Video

INTERVAL = int(os.environ.get("ZERKO_PROXY_SWEEP_SECONDS", "600"))
NEW_DAYS = int(os.environ.get("ZERKO_PROXY_NEW_DAYS", "14"))
MAX_ATTEMPTS = 3
BATCH = 50

_state = {"mode": None, "last_sweep": None, "last_result": None, "running": False}
_started = False


def mode() -> str:
    m = (os.environ.get("ZERKO_AUTO_PROXIES") or "new").strip().lower()
    return m if m in ("new", "all", "off") else "new"


def status() -> dict:
    return {"mode": mode(), "interval_seconds": INTERVAL, "new_days": NEW_DAYS,
            "max_attempts": MAX_ATTEMPTS,
            "last_sweep": _state["last_sweep"], "last_result": _state["last_result"],
            "running": _state["running"]}


def _in_flight(jm) -> set:
    return {j.get("video_id") for j in list(jm.active_jobs.values())
            if j.get("type") == "proxy" and j.get("status") in ("queued", "processing")}


def sweep(jm=None, resolve=None) -> dict:
    """One pass. Safe to call any time; returns what it did."""
    if jm is None:
        from job_manager import job_manager as jm
    resolve = resolve or (lambda p: p)
    out = {"missing_file_reset": 0, "stranded_requeued": 0,
           "failed_retried": 0, "new_queued": 0}
    db = SessionLocal()
    try:
        base = db.query(Video).filter(Video.media_type == "video",
                                      Video.is_active.isnot(False))
        busy = _in_flight(jm)
        to_queue = []

        # 1. "completed" but the file is gone (cleaned up, drive swapped)
        for v in base.filter(Video.proxy_status == "completed").all():
            p = resolve(v.proxy_path) if v.proxy_path else None
            if not p or not os.path.exists(p):
                v.proxy_status = "not_generated"
                v.proxy_path = None
                out["missing_file_reset"] += 1
        db.commit()

        # 2. the database says queued/processing but nothing is working on it
        for v in base.filter(Video.proxy_status.in_(["queued", "processing"])).all():
            if v.id not in busy:
                to_queue.append(v)
                out["stranded_requeued"] += 1

        # 3. failures get a few more tries, then are left alone
        for v in (base.filter(Video.proxy_status == "failed",
                              (Video.proxy_attempts == None) |      # noqa: E711
                              (Video.proxy_attempts < MAX_ATTEMPTS))
                  .limit(BATCH).all()):
            if v.id not in busy:
                to_queue.append(v)
                out["failed_retried"] += 1

        # 4. new footage
        m = mode()
        if m != "off":
            q = base.filter((Video.proxy_status == None) |               # noqa: E711
                            (Video.proxy_status == "not_generated"))
            if m == "new":
                q = q.filter(Video.created_at >= datetime.utcnow() - timedelta(days=NEW_DAYS))
            for v in q.order_by(Video.created_at.desc()).limit(BATCH).all():
                if v.id not in busy:
                    src = resolve(v.filepath)
                    if src and os.path.exists(src):
                        to_queue.append(v)
                        out["new_queued"] += 1
        ids = [v.id for v in to_queue]
    finally:
        db.close()

    for vid in dict.fromkeys(ids):
        try:
            jm.add_job(vid, "proxy")
        except Exception as e:
            print(f"proxy sweeper: could not queue {vid}: {e}", flush=True)
    _state["last_sweep"] = datetime.utcnow().isoformat()
    _state["last_result"] = out
    return out


def start(resolve=None):
    """Start the sweeper thread once. First pass after a short delay so the
    server is fully up and resume_pending has run."""
    global _started
    if _started:
        return
    _started = True

    def loop():
        time.sleep(60)
        while True:
            _state["running"] = True
            try:
                r = sweep(resolve=resolve)
                if any(r.values()):
                    print(f"  proxy sweep: {r}", flush=True)
            except Exception as e:
                print(f"  proxy sweep failed: {e}", flush=True)
            finally:
                _state["running"] = False
            time.sleep(max(60, INTERVAL))

    threading.Thread(target=loop, daemon=True, name="zerko-proxy-sweeper").start()
