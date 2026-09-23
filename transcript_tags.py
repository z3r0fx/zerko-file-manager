"""Derive tags from what is actually said in the footage.

The library had 1,414 transcripts and zero tags, which is the wrong way round:
hand-filing 1,600 clips never happens, but the narration already names the
room, the view and the price. This turns that narration into tags, so
"every kitchen reveal I have ever shot" becomes a real query.

    python transcript_tags.py --dry-run     show what it would tag
    python transcript_tags.py               apply
    python transcript_tags.py --reset       remove transcript tags first

Rules are word-boundary regexes over the transcript. A tag needs `min_hits`
matches before it sticks, which keeps a passing mention of "the bathroom is
through there" from tagging a clip that is really about the garden.
"""

import os
import re
import sys
from collections import defaultdict

from database import init_db, SessionLocal, Video, Tag

# (tag, pattern, min_hits)
# Patterns are matched case-insensitively with word boundaries already applied.
RULES = [
    # --- rooms -------------------------------------------------------------
    ("main bedroom",  r"(main|master|primary)\s+(bed\s?room|suite)",                1),
    ("bedroom",       r"bed\s?rooms?",                                              1),
    ("bathroom",      r"bath\s?rooms?|en.?suite|shower|bath\b",                     1),
    ("kitchen",       r"kitchens?|scullery|pantry",                                 1),
    ("living room",   r"living\s+(room|area)|lounge|sitting\s+room|tv\s+room",      1),
    ("dining",        r"dining\s+(room|area)|dinner\s+table",                       1),
    ("study",         r"\bstudy\b|home\s+office|\boffice\b",                        1),
    ("balcony",       r"balcon(y|ies)|patio|terrace|veranda|stoep|deck\b",          1),
    ("garage",        r"garages?|car\s*port|parking\s+bays?",                       1),
    ("garden",        r"gardens?|\blawn\b|\byard\b",                                1),
    ("pool",          r"\bpools?\b|swimming\s+pool|jacuzzi|plunge\s+pool",          1),
    ("gym",           r"\bgyms?\b|home\s+gym|fitness",                              1),
    ("braai",         r"\bbraai|barbecue|\bbbq\b",                                  1),
    ("entrance",      r"entrance|\bfoyer\b|front\s+door|walk\s+in\s+through",       1),
    ("rooftop",       r"roof\s?top|on\s+the\s+roof",                                1),

    # --- features ----------------------------------------------------------
    ("sea view",      r"(sea|ocean|water)\s+view|view\s+of\s+the\s+(sea|ocean)|atlantic", 1),
    ("mountain view", r"mountain\s+view|table\s+mountain|lion'?s\s+head|signal\s+hill",   1),
    ("open plan",     r"open\s+plan",                                               1),
    ("fireplace",     r"fire\s?place",                                              1),
    ("walk-in closet", r"walk.?in\s+(closet|robe|wardrobe)|dressing\s+room",        1),
    ("security",      r"security|\bestate\b|access\s+control|\bgated\b|alarm",      1),
    ("solar",         r"\bsolar\b|inverter|load\s?shedding|back.?up\s+power",       1),
    ("furnished",     r"furnished|furniture\s+included",                            1),
    ("renovated",     r"renovat|newly\s+built|brand\s+new\s+(build|finish)",        1),

    # --- locations ---------------------------------------------------------
    ("atlantic seaboard", r"atlantic\s+sea\s?board",                                1),
    ("green point",   r"green\s?point",                                             1),
    ("clifton",       r"\bclifton\b",                                               1),
    ("camps bay",     r"camps\s+bay",                                               1),
    ("sea point",     r"sea\s?point",                                               1),
    ("bantry bay",    r"bantry\s+bay",                                              1),
    ("waterfront",    r"waterfront|v\s*&\s*a\b",                                    1),
    ("yzerfontein",   r"yzerfontein|ysterfontein",                                  1),

    # --- content type ------------------------------------------------------
    ("price talk",    r"\brands?\b|\bmillion\b|\bprice\b|costs?\s+you|worth\b|\bR\d",  1),
    ("intro",         r"^\s*(3,?\s*2,?\s*1|three,?\s*two,?\s*one)|welcome\s+to|this\s+is\s+what", 1),
    ("call to action", r"\b(dm|message|contact|call)\s+(me|us)|link\s+in\s+bio|get\s+in\s+touch", 1),
    ("walkthrough",   r"come\s+(with\s+me|through|inside)|let'?s\s+(go|take\s+a\s+look)|follow\s+me", 1),
    ("agent talk",    r"\bsell(ing)?\b|\bbuyer|\bmarket\b|\blisting\b|on\s+show",   2),
]

COMPILED = [(name, re.compile(pat, re.I), hits) for name, pat, hits in RULES]

# Marks tags this script owns, so --reset can remove exactly these and leave
# the folder/camera tags from enrich.py alone.
OWNED = {name for name, _, _ in RULES}


def tags_for(text, rules=None):
    """Tags for one transcript. `rules` is the active set from tag_rules
    (built-ins minus the switched-off ones, plus your own); without it, the
    built-ins as written here."""
    if not text:
        return []
    out = []
    for name, rx, min_hits in (rules if rules is not None else COMPILED):
        if len(rx.findall(text)) >= min_hits:
            out.append(name)
    return out


def main():
    dry = "--dry-run" in sys.argv
    reset = "--reset" in sys.argv

    init_db()
    db = SessionLocal()

    tag_rows = {(t.name or "").lower(): t for t in db.query(Tag).all()}

    if reset and not dry:
        removed = 0
        for name in OWNED:
            t = tag_rows.get(name.lower())
            if t:
                t.videos.clear()
                removed += 1
        db.commit()
        print(f"  cleared assignments for {removed} transcript tags")
        tag_rows = {(t.name or "").lower(): t for t in db.query(Tag).all()}

    videos = db.query(Video).filter(Video.transcription.isnot(None)).all()
    print(f"\n  Scanning {len(videos)} transcripts{'  (DRY RUN)' if dry else ''}\n")

    wanted = {}
    needed = set()
    for v in videos:
        names = tags_for(v.transcription)
        if names:
            wanted[v.id] = names
            needed.update(n.lower() for n in names)

    # Create every tag up front, then assign - committing mid-loop expires the
    # session and silently drops assignments.
    if not dry:
        for n in needed:
            if n not in tag_rows:
                t = Tag(name=n)
                db.add(t)
                tag_rows[n] = t
        db.commit()
        tag_rows = {(t.name or "").lower(): t for t in db.query(Tag).all()}

    counts = defaultdict(int)
    applied = 0
    for v in videos:
        names = wanted.get(v.id) or []
        if not names:
            continue
        current = {(t.name or "").lower() for t in v.tags}
        touched = False
        for n in names:
            counts[n] += 1
            if n.lower() in current:
                continue
            if not dry:
                v.tags.append(tag_rows[n.lower()])
            touched = True
        if touched:
            applied += 1

    if not dry:
        db.commit()

    print(f"  clips tagged: {applied} of {len(videos)} with transcripts\n")
    print("  tag                    clips")
    print("  " + "-" * 34)
    for name, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {name:<24} {n}")
    db.close()


if __name__ == "__main__":
    main()
