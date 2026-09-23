"""Fill in technical metadata and auto-tag the library.

Read-only as far as your media is concerned: this touches the database only.
No file is moved, renamed or deleted.

    python enrich.py              metadata + tags
    python enrich.py --tags-only  skip ffprobe, just tagging
    python enrich.py --dry-run    show what it would do, change nothing
"""
import os
import re
import sys
import time

from database import init_db, SessionLocal, Video, Tag, IndexedFolder
from video_processor import get_technical_metadata

# The catalog stores absolute paths from wherever the server runs. When this
# script is run somewhere the drive is mounted elsewhere, set DB_ROOT to the
# prefix stored in the database and MEDIA_ROOT to where it actually is; paths
# written back to the catalog are never rewritten, only the reads are remapped.
DB_ROOT = os.environ.get("DB_ROOT")
REAL_ROOT = os.environ.get("MEDIA_ROOT")


def real_path(stored):
    """Where this process can actually read the file."""
    if not stored:
        return stored
    if DB_ROOT and REAL_ROOT and stored.startswith(DB_ROOT):
        return REAL_ROOT.rstrip("/") + stored[len(DB_ROOT):]
    return stored

# --- tag rules -------------------------------------------------------------
# Within SOURCE and STAGE the FIRST match wins, so the lists are ordered by
# specificity. Filename evidence beats folder evidence: a DJI_ file sitting in
# a "Graded" folder is still drone footage.

SOURCE_RULES = [
    # (tag, folder patterns, filename patterns)
    ("meta glasses",  [r"meta\s*(glasses|pov)"],                 [r"^meta"]),
    ("drone",         [r"\bdrone\b"],                            [r"^DJI[_-]", r"^FC\d", r"\bdrone\b"]),
    ("gopro",         [r"\bgopro\b"],                            [r"^G[HXOP]\d{6}"]),
    ("cinema camera", [r"r3d|\bred\b|sony\s*raw|pro\s*res|proress|\bzr\b|h\.?265"],
                      [r"^C\d{3,}", r"^A\d{3}C\d", r"^NRW[_-]", r"^R\d+K[_-]", r"^H\d+[_-]"]),
    ("phone",         [r"\biphone\b|\bphone\b"],                [r"^IMG[_-]\d", r"^MVI[_-]"]),
]

# Ordered most-finished to least. A clip is at exactly one stage.
STAGE_RULES = [
    ("rendered", [r"\brender(ed|s)?\b|\brednered\b"],           [r"\brender"]),
    ("draft",    [r"\bdrafts?\b"],                              [r"\bdraft\b"]),
    ("graded",   [r"\bgraded?\b|\bgrade\b"],                    [r"^graded", r"[_ ]graded"]),
    ("proxy",    [r"\bprox(y|ies)\b"],                          [r"^proxy[_ ]"]),
    ("edited",   [r"\bedited\b|\bedit\b"],                      [r"\bedit(ed)?\b"]),
    ("raw",      [r"\braw\b"],                                  [r"\braw\b"]),
]

# Extra, non-exclusive labels
EXTRA_RULES = [
    ("hook", [r"\bhooks?\b"], [r"^hooks?\d"]),
    ("vlog", [r"\bvlog\b"],   []),
]

# Top-level folders that describe a CATEGORY rather than a shoot. These get no
# shoot tag - the source/stage tags already say what they are.
CATEGORY_FOLDERS = {
    "drone", "drone clips edited", "hooks", "vlog clips", "meta glasses",
    "new folder", "upload 2", "proress 02", "proress graded",
    "n raw graded 01", "zr graded",
}


def match_any(patterns, text):
    return any(re.search(p, text, re.I) for p in patterns)


def get_or_create(db, cache, name):
    key = name.lower()
    if key in cache:
        return cache[key]
    tag = next((t for t in db.query(Tag).all() if (t.name or "").lower() == key), None)
    if not tag:
        tag = Tag(name=name)
        db.add(tag)
        db.commit()
        db.refresh(tag)
    cache[key] = tag
    return tag


def shoot_name(relative_path):
    """Top-level folder = the shoot, unless it's really a category."""
    if not relative_path:
        return None
    top = relative_path.split("/")[0].strip()
    if top.lower() in CATEGORY_FOLDERS:
        return None
    if re.fullmatch(r"untitled|temp|misc", top, re.I):
        return None
    return top


# Camera make -> tag. Derived from the file itself, so it is more reliable
# than any filename convention.
CAMERA_TAG_BY_MAKE = {
    "sony": "sony",
    "dji": "drone",
    "meta": "meta glasses",
    "atomos": "atomos",
    "android": "phone",
    "apple": "phone",
    "gopro": "gopro",
    "nikon": "nikon",
    "canon": "canon",
    "blackmagic design": "davinci render",
}


def camera_tag(make):
    return CAMERA_TAG_BY_MAKE.get((make or "").strip().lower())


def classify(relative_path, filename, make=None):
    """All tags for one clip, deduplicated, first-match-wins per category."""
    rel, name = relative_path or "", filename or ""
    tags = []

    for tag, folder_pats, file_pats in SOURCE_RULES:
        if match_any(file_pats, name) or match_any(folder_pats, rel):
            tags.append(tag)
            break

    for tag, folder_pats, file_pats in STAGE_RULES:
        if match_any(file_pats, name) or match_any(folder_pats, rel):
            tags.append(tag)
            break

    for tag, folder_pats, file_pats in EXTRA_RULES:
        if match_any(file_pats, name) or match_any(folder_pats, rel):
            tags.append(tag)

    ct = camera_tag(make)
    if ct:
        tags.append(ct)

    shoot = shoot_name(rel)
    if shoot:
        tags.append(shoot)

    seen, out = set(), []
    for tg in tags:
        if tg.lower() not in seen:
            seen.add(tg.lower())
            out.append(tg)
    return out


def run_tagging(progress=None, include_transcripts=True):
    """The tagging pass, callable from the app rather than only the CLI.

    Indexing catalogues files but produces no tags at all, so without this
    a fresh library ends up with thousands of clips and an empty Tags list.
    Returns a summary dict so the UI can show what actually happened.
    """
    def say(msg):
        if progress:
            try:
                progress(msg)
            except Exception:
                pass
        print(f"  {msg}", flush=True)

    init_db()
    db = SessionLocal()
    stats = {"scanned": 0, "tagged": 0, "tags_created": 0,
             "from_transcripts": 0, "errors": 0}
    try:
        folder_rel = {f.id: f.relative_path for f in db.query(IndexedFolder).all()}
        videos = db.query(Video).all()
        stats["scanned"] = len(videos)
        say(f"scanning {len(videos)} files")

        wanted = {}
        needed = set()
        rules = None
        if include_transcripts:
            try:
                import tag_rules
                rules = tag_rules.active_rules(db)
            except Exception as e:
                say(f"custom tag rules unavailable ({e}); using the built-in ones")
        for v in videos:
            rel = folder_rel.get(v.folder_id) or ""
            names = classify(rel, v.filename or "", v.camera_make)

            if include_transcripts and v.transcription:
                try:
                    from transcript_tags import tags_for
                    t = tags_for(v.transcription, rules)
                    if t:
                        names = list(names) + t
                        stats["from_transcripts"] += 1
                except Exception:
                    pass

            seen, uniq = set(), []
            for n in names:
                if n.lower() not in seen:
                    seen.add(n.lower())
                    uniq.append(n)
            if uniq:
                wanted[v.id] = uniq
                needed.update(n.lower() for n in uniq)

        # Create every tag first, assign second, commit once. Committing
        # inside the loop expires the session and silently drops assignments.
        existing = {(t.name or "").lower(): t for t in db.query(Tag).all()}
        for n in needed:
            if n not in existing:
                t = Tag(name=n)
                db.add(t)
                existing[n] = t
                stats["tags_created"] += 1
        db.commit()
        existing = {(t.name or "").lower(): t for t in db.query(Tag).all()}
        say(f"{stats['tags_created']} new tags created")

        done = 0
        for v in videos:
            names = wanted.get(v.id)
            if not names:
                continue
            current = {(t.name or "").lower() for t in v.tags}
            touched = False
            for n in names:
                if n.lower() in current:
                    continue
                tag = existing.get(n.lower())
                if tag is not None:
                    v.tags.append(tag)
                    touched = True
            if touched:
                stats["tagged"] += 1
            done += 1
            if done % 250 == 0:
                say(f"{done}/{len(videos)}")
        db.commit()
        say(f"done - {stats['tagged']} files tagged")
    except Exception as e:
        stats["errors"] += 1
        say(f"failed: {e}")
    finally:
        db.close()
    return stats


def main():
    dry = "--dry-run" in sys.argv
    tags_only = "--tags-only" in sys.argv
    meta_only = "--meta-only" in sys.argv

    limit = None
    for a in sys.argv:
        if a.startswith("--limit="):
            limit = int(a.split("=", 1)[1])

    init_db()
    db = SessionLocal()
    stats = {"probed": 0, "meta_filled": 0, "tagged": 0, "tags_created": 0,
             "skipped": 0, "errors": 0}

    # Resumable by design: each run picks up only what still needs doing, so a
    # pass that is interrupted (or run in short slices) simply continues.
    q = db.query(Video)
    if not tags_only:
        q = q.filter(Video.media_type == "video", Video.resolution.is_(None))
    videos = q.limit(limit).all() if limit else q.all()
    total = len(videos)
    if total == 0:
        print("  Nothing left to enrich.")
        db.close()
        return 0

    print(f"\n  Enriching {total} files{'  (DRY RUN - nothing will be saved)' if dry else ''}\n")
    started = time.time()

    # Work out every tag this batch needs and create them ALL up front.
    # The previous version called get_or_create inside the loop, and that
    # commit expired the session - invalidating video.tags mid-iteration
    # ("SAWarning: This collection has been invalidated") and silently
    # dropping tag assignments. Create first, assign second, commit once.
    folder_rel = {f.id: f.relative_path for f in db.query(IndexedFolder).all()}
    wanted_by_video = {}
    needed = set()
    for v in videos:
        rel = folder_rel.get(v.folder_id) or ""
        names = classify(rel, v.filename or "", v.camera_make)
        wanted_by_video[v.id] = names
        needed.update(n.lower() for n in names)

    tag_by_name = {(t.name or "").lower(): t for t in db.query(Tag).all()}
    if not dry and not meta_only:
        for v in videos:
            for name in wanted_by_video[v.id]:
                if name.lower() not in tag_by_name:
                    t = Tag(name=name)
                    db.add(t)
                    tag_by_name[name.lower()] = t
                    stats["tags_created"] += 1
        db.commit()
        tag_by_name = {(t.name or "").lower(): t for t in db.query(Tag).all()}
    else:
        stats["tags_created"] = len(needed - set(tag_by_name))

    for i, v in enumerate(videos, 1):
        if i % 100 == 0 or i == total:
            print(f"    {i}/{total}  ({i / total * 100:.0f}%)  "
                  f"probed={stats['probed']} tagged={stats['tagged']}", flush=True)

        # --- technical metadata ---
        if not tags_only and v.media_type == "video" and not v.resolution:
            path = real_path(v.filepath)
            if path and os.path.exists(path):
                try:
                    meta = get_technical_metadata(path)
                    stats["probed"] += 1
                    changed = False
                    for field in ("camera_make", "camera_model", "video_codec",
                                  "resolution", "frame_rate", "audio_codec"):
                        val = meta.get(field)
                        if val and val != "NonexNone" and getattr(v, field, None) != val:
                            if not dry:
                                setattr(v, field, val)
                            changed = True
                    if changed:
                        stats["meta_filled"] += 1
                except Exception as e:
                    stats["errors"] += 1
                    print(f"      probe failed: {v.filename}: {e}")
            else:
                stats["skipped"] += 1

        # --- tags ---
        if not meta_only:
            names = wanted_by_video.get(v.id) or []
            if names:
                current = {(t.name or "").lower() for t in v.tags}
                added = False
                for name in names:
                    if name.lower() in current:
                        continue
                    if not dry:
                        tag = tag_by_name.get(name.lower())
                        if tag is not None:
                            v.tags.append(tag)
                    added = True
                if added:
                    stats["tagged"] += 1

    if not dry:
        db.commit()

    print("\n  ========================================")
    print(f"   Files processed   : {total}")
    print(f"   ffprobe runs      : {stats['probed']}")
    print(f"   metadata filled   : {stats['meta_filled']}")
    print(f"   files tagged      : {stats['tagged']}")
    print(f"   new tags created  : {stats['tags_created']}")
    print(f"   missing on disk   : {stats['skipped']}")
    print(f"   errors            : {stats['errors']}")
    print(f"   took              : {(time.time() - started) / 60:.1f} min")
    print("  ========================================\n")

    remaining = db.query(Video).filter(
        Video.media_type == "video", Video.resolution.is_(None)).count()
    print(f"   still needing metadata: {remaining}")
    db.close()
    return remaining


if __name__ == "__main__":
    main()
