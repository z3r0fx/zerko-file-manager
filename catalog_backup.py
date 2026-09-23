"""Catalog backups.

The media on D: is replaceable - you still have the cards, and worst case you
reshoot. The catalog is not: thousands of Whisper transcripts, every segment
timecode, ratings, tags, notes and folder structure live in one small SQLite
file sitting on the same drive as the footage it describes. One bad sector and
that is weeks of GPU time gone.

sqlite3's own backup API is used rather than copying the file, because the
server is usually running and holds it open in WAL mode - a plain file copy
can capture a torn page and produce a backup that only fails when you need it.

Backups are gzipped, named by timestamp, and rotated. Restoring is just
gunzip + rename, no tooling required, which matters when you are restoring at
the worst possible moment.
"""

import gzip
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

def _db_path() -> Path:
    """The database the app actually uses: CATALOG_DB, else the sqlite file
    named in DATABASE_URL, else mediamanager.db beside the app."""
    if os.environ.get("CATALOG_DB"):
        return Path(os.environ["CATALOG_DB"]).resolve()
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("sqlite:///"):
        return Path(url[len("sqlite:///"):]).resolve()
    return Path("mediamanager.db").resolve()


DB_PATH = _db_path()
KEEP = int(os.environ.get("CATALOG_BACKUP_KEEP", "14"))
INTERVAL_HOURS = float(os.environ.get("CATALOG_BACKUP_HOURS", "24"))


def backup_dir() -> Path:
    """Backups live beside the media, not beside the database.

    If they sat next to mediamanager.db, the accident that takes out the
    working copy takes out the backups with it.
    """
    override = os.environ.get("CATALOG_BACKUP_DIR")
    if override:
        d = Path(override)
    else:
        media_root = os.environ.get("MEDIA_ROOT") or str(Path.home() / "Videos")
        d = Path(media_root) / "_catalog-backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def make_backup(reason: str = "scheduled") -> Path | None:
    """Take one consistent snapshot. Returns the path written, or None."""
    if not DB_PATH.exists():
        print(f"  [backup] no database at {DB_PATH}")
        return None

    dest_dir = backup_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    tmp = dest_dir / f".catalog-{stamp}.tmp"
    final = dest_dir / f"catalog-{stamp}.db.gz"

    src = None
    dst = None
    try:
        # Read-only source: never let a backup mutate the live catalog.
        src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
        dst = sqlite3.connect(str(tmp))
        src.backup(dst)          # atomic, WAL-safe, no locking of writers
        dst.close(); dst = None
        src.close(); src = None

        with open(tmp, "rb") as f_in, gzip.open(final, "wb", compresslevel=6) as f_out:
            shutil.copyfileobj(f_in, f_out, 1024 * 1024)

        size = final.stat().st_size
        print(f"  [backup] {final.name} ({size / 1024:.0f} KB, {reason})")
        return final
    except Exception as e:
        print(f"  [backup] FAILED: {e}")
        if final.exists():
            final.unlink(missing_ok=True)
        return None
    finally:
        for h in (dst, src):
            try:
                if h is not None:
                    h.close()
            except Exception:
                pass
        tmp.unlink(missing_ok=True)


def verify(path: Path) -> bool:
    """Open a backup and make sure it is a real, readable catalog.

    A backup nobody has ever opened is a guess, not a backup.
    """
    tmp = path.with_suffix(".verify.db")
    try:
        with gzip.open(path, "rb") as f_in, open(tmp, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out, 1024 * 1024)
        c = sqlite3.connect(str(tmp))
        ok = c.execute("PRAGMA integrity_check").fetchone()[0]
        counts = {
            t: c.execute(f"select count(*) from {t}").fetchone()[0]
            for t in ("videos", "transcription_segments", "tags", "users")
        }
        c.close()
        good = ok == "ok" and counts["videos"] > 0
        print(f"  [backup] verify {path.name}: integrity={ok} {counts}")
        return good
    except Exception as e:
        print(f"  [backup] verify FAILED for {path.name}: {e}")
        return False
    finally:
        tmp.unlink(missing_ok=True)


def prune(keep: int = KEEP) -> int:
    """Keep the newest `keep` backups, delete the rest."""
    files = sorted(
        backup_dir().glob("catalog-*.db.gz"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for old in files[keep:]:
        try:
            old.unlink()
            removed += 1
        except Exception as e:
            print(f"  [backup] could not remove {old.name}: {e}")
    if removed:
        print(f"  [backup] pruned {removed} old backup(s), keeping {keep}")
    return removed


def run_once(reason: str = "manual", do_verify: bool = False) -> Path | None:
    path = make_backup(reason)
    if path and do_verify:
        verify(path)
    prune()
    return path


_started = False


def start_scheduler():
    """Daily backup on a daemon thread.

    Deliberately in-process rather than a Windows Scheduled Task: the catalog
    only changes while the server is running, so tying the backup to the
    server's own lifetime means there is nothing separate to install, and
    nothing that silently stops working after a reinstall.
    """
    global _started
    if _started:
        return
    _started = True

    def loop():
        # One on startup, so there is always a recent snapshot even if the
        # machine is rarely left running for a full day.
        time.sleep(20)
        try:
            run_once("startup")
        except Exception as e:
            print(f"  [backup] startup backup failed: {e}")

        while True:
            time.sleep(INTERVAL_HOURS * 3600)
            try:
                run_once("scheduled")
            except Exception as e:
                print(f"  [backup] scheduled backup failed: {e}")

    threading.Thread(target=loop, daemon=True, name="catalog-backup").start()
    print(f"  Catalog backup: every {INTERVAL_HOURS:g}h, keeping {KEEP}, in {backup_dir()}")


def latest():
    files = sorted(backup_dir().glob("catalog-*.db.gz"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


if __name__ == "__main__":
    import sys
    if "--verify-latest" in sys.argv:
        p = latest()
        if p:
            verify(p)
        else:
            print("  [backup] nothing to verify")
    else:
        run_once("manual", do_verify="--verify" in sys.argv)
