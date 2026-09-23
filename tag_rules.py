"""Your own spoken-word tag rules, beside the built-in ones.

The built-in rules in transcript_tags.py cover the usual rooms, views and
suburbs. Every business has its own words too - an estate name, a developer,
"wine cellar", "lock-up-and-go" - and those should not need a code change.

A custom rule is a tag plus the words or phrases that mean it, typed the way
people say them ("sea view, ocean view, view of the sea"). They become
word-boundary regexes here, so nobody has to write one. Built-in rules can be
switched off (a suburb you never shoot in, a rule that is too eager) without
touching the code either.

Nothing here tags anything by itself: the next auto-tagging run uses the
active rules. A preview says how many transcripts a rule would match first.
"""

import re
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.orm import Session

from auth import get_admin_user
from database import Base, Tag, User, Video, engine, get_db

router = APIRouter(prefix="/api/tags", tags=["tag-rules"])

MAX_PHRASES = 40
MAX_PHRASE_LEN = 80


class CustomTagRule(Base):
    __tablename__ = "custom_tag_rules"
    id = Column(Integer, primary_key=True)
    tag = Column(String(80), nullable=False)
    phrases = Column(Text, nullable=False)          # one per line
    min_hits = Column(Integer, default=1)
    enabled = Column(Boolean, default=True)
    created_by = Column(String(80))
    created_at = Column(DateTime, default=datetime.utcnow)


class BuiltinRuleOff(Base):
    """A built-in rule someone switched off."""
    __tablename__ = "builtin_tag_rules_off"
    tag = Column(String(80), primary_key=True)


def _clean_phrases(raw) -> List[str]:
    if isinstance(raw, str):
        raw = re.split(r"[,\n;]", raw)
    out, seen = [], set()
    for p in raw or []:
        p = re.sub(r"\s+", " ", str(p)).strip().lower()
        if not p or p in seen:
            continue
        if len(p) > MAX_PHRASE_LEN:
            raise HTTPException(status_code=400, detail=f'"{p[:30]}…" is too long for one phrase.')
        seen.add(p)
        out.append(p)
    if not out:
        raise HTTPException(status_code=400, detail="Give at least one word or phrase.")
    if len(out) > MAX_PHRASES:
        raise HTTPException(status_code=400, detail=f"Keep it to {MAX_PHRASES} phrases per tag.")
    return out


def phrases_to_regex(phrases: List[str]) -> str:
    """"sea view" -> \\bsea\\s+view\\b, joined with |. Typed text never reaches
    the regex unescaped, so a stray bracket cannot break the tagging run."""
    parts = []
    for p in phrases:
        words = [re.escape(w) for w in p.split(" ")]
        parts.append(r"\b" + r"\s+".join(words) + r"\b")
    return "|".join(parts)


def active_rules(db: Session):
    """(tag, compiled regex, min_hits) for every rule that should run."""
    from transcript_tags import RULES
    off = {r.tag for r in db.query(BuiltinRuleOff).all()}
    out = [(name, re.compile(pat, re.I), hits) for name, pat, hits in RULES if name not in off]
    for r in db.query(CustomTagRule).filter(CustomTagRule.enabled.is_(True)).all():
        try:
            out.append((r.tag, re.compile(phrases_to_regex(r.phrases.split("\n")), re.I), max(1, r.min_hits or 1)))
        except re.error:
            continue
    return out


def _as_dict(r: CustomTagRule) -> dict:
    return {"id": r.id, "tag": r.tag, "phrases": [p for p in r.phrases.split("\n") if p],
            "min_hits": r.min_hits or 1, "enabled": bool(r.enabled),
            "created_by": r.created_by,
            "created_at": r.created_at.isoformat() if r.created_at else None}


class RuleBody(BaseModel):
    tag: str = Field(..., min_length=1, max_length=80)
    phrases: List[str]
    min_hits: int = Field(1, ge=1, le=20)
    enabled: bool = True


class RulePatch(BaseModel):
    tag: Optional[str] = Field(None, min_length=1, max_length=80)
    phrases: Optional[List[str]] = None
    min_hits: Optional[int] = Field(None, ge=1, le=20)
    enabled: Optional[bool] = None


class Preview(BaseModel):
    phrases: List[str]
    min_hits: int = Field(1, ge=1, le=20)


class Toggle(BaseModel):
    enabled: bool


class Rename(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)


@router.get("/custom-rules")
def list_rules(db: Session = Depends(get_db), user: User = Depends(get_admin_user)):
    from transcript_tags import RULES
    off = {r.tag for r in db.query(BuiltinRuleOff).all()}
    return {
        "custom": [_as_dict(r) for r in db.query(CustomTagRule).order_by(CustomTagRule.tag).all()],
        "builtin": [{"tag": n, "matches": p, "min_hits": h, "enabled": n not in off} for n, p, h in RULES],
    }


@router.post("/custom-rules")
def add_rule(body: RuleBody, db: Session = Depends(get_db), user: User = Depends(get_admin_user)):
    tag = body.tag.strip().lower()
    r = CustomTagRule(tag=tag, phrases="\n".join(_clean_phrases(body.phrases)),
                      min_hits=body.min_hits, enabled=body.enabled, created_by=user.username)
    db.add(r)
    db.commit()
    db.refresh(r)
    return _as_dict(r)


@router.patch("/custom-rules/{rule_id}")
def edit_rule(rule_id: int, body: RulePatch, db: Session = Depends(get_db),
              user: User = Depends(get_admin_user)):
    r = db.query(CustomTagRule).filter(CustomTagRule.id == rule_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="No such rule")
    if body.tag is not None:
        r.tag = body.tag.strip().lower()
    if body.phrases is not None:
        r.phrases = "\n".join(_clean_phrases(body.phrases))
    if body.min_hits is not None:
        r.min_hits = body.min_hits
    if body.enabled is not None:
        r.enabled = body.enabled
    db.commit()
    return _as_dict(r)


@router.delete("/custom-rules/{rule_id}")
def delete_rule(rule_id: int, db: Session = Depends(get_db), user: User = Depends(get_admin_user)):
    """Forget the rule. Tags it already put on files stay until cleared."""
    db.query(CustomTagRule).filter(CustomTagRule.id == rule_id).delete()
    db.commit()
    return {"status": "ok"}


@router.post("/builtin-rules/{tag}")
def toggle_builtin(tag: str, body: Toggle, db: Session = Depends(get_db),
                   user: User = Depends(get_admin_user)):
    from transcript_tags import OWNED
    if tag not in OWNED:
        raise HTTPException(status_code=404, detail="No such built-in rule")
    row = db.query(BuiltinRuleOff).filter(BuiltinRuleOff.tag == tag).first()
    if body.enabled and row:
        db.delete(row)
    elif not body.enabled and not row:
        db.add(BuiltinRuleOff(tag=tag))
    db.commit()
    return {"tag": tag, "enabled": body.enabled}


@router.post("/rules/preview")
def preview(body: Preview, db: Session = Depends(get_db), user: User = Depends(get_admin_user)):
    """How many transcripts a rule would tag, with a few of the lines that
    matched - so a rule can be tried before it touches anything."""
    rx = re.compile(phrases_to_regex(_clean_phrases(body.phrases)), re.I)
    rows = db.query(Video.id, Video.filename, Video.transcription) \
             .filter(Video.transcription.isnot(None)).all()
    hits, examples = 0, []
    for vid, name, text in rows:
        found = list(rx.finditer(text or ""))
        if len(found) < body.min_hits:
            continue
        hits += 1
        if len(examples) < 6:
            m = found[0]
            a, b = max(0, m.start() - 50), min(len(text), m.end() + 50)
            examples.append({"id": vid, "filename": name,
                             "before": ("…" if a else "") + text[a:m.start()],
                             "match": text[m.start():m.end()],
                             "after": text[m.end():b] + ("…" if b < len(text) else "")})
    return {"matches": hits, "of": len(rows), "examples": examples}


@router.post("/{tag_id}/rename")
def rename_tag(tag_id: int, body: Rename, db: Session = Depends(get_db),
               user: User = Depends(get_admin_user)):
    """Rename a tag. If the new name is already a tag, the two become one:
    every file on this tag moves to the other and this one goes."""
    tag = db.query(Tag).filter(Tag.id == tag_id).first()
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    name = re.sub(r"\s+", " ", body.name).strip().lower()
    other = db.query(Tag).filter(Tag.name == name, Tag.id != tag.id).first()
    if other:
        have = {v.id for v in other.videos}
        moved = 0
        for v in list(tag.videos):
            if v.id not in have:
                other.videos.append(v)
                moved += 1
        tag.videos.clear()
        db.delete(tag)
        db.commit()
        return {"status": "merged", "into": other.id, "name": other.name, "moved": moved}
    tag.name = name
    db.commit()
    return {"status": "renamed", "id": tag.id, "name": tag.name}


def install(app):
    Base.metadata.create_all(bind=engine, tables=[CustomTagRule.__table__, BuiltinRuleOff.__table__])
    app.include_router(router)
