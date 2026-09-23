"""The listing text helper: captions for posting a property.

One property usually goes out as several posts - the drone clip, the
walkthrough, the twilight photos, a teaser - and each needs its own words,
not the same paragraph pasted five times. So every "write" here comes out
worded differently from the ones before it for that property: the opening,
the body, the way the facts are put, which features lead and the sign-off
are each picked from what has not been used yet.

It works offline from the property's facts (bedrooms, price, features...).
If an Anthropic API key is set under the caption settings, Claude writes
them instead, is shown the earlier captions so it does not repeat them, and
the built-in writer is still there if that call fails.

Nothing is made up: a fact that is not filled in is simply not mentioned.
"""

from __future__ import annotations

import json
import random
import re
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Session

import permissions
from auth import get_current_user
from database import Base, User, Video, engine, get_db

router = APIRouter(tags=["captions"])
SETTINGS_FILE = Path(__file__).resolve().parent / "captions_settings.json"

PLATFORMS = ("instagram", "facebook", "short", "listing", "whatsapp")
TONES = ("warm", "bold", "luxury", "simple")
CLIPS = ("", "tour", "drone", "photos", "twilight", "teaser", "details")


class Caption(Base):
    __tablename__ = "captions"
    id = Column(Integer, primary_key=True)
    shoot_id = Column(Integer, ForeignKey("shoots.id"), nullable=False, index=True)
    video_id = Column(Integer, nullable=True)
    platform = Column(String, nullable=False)
    tone = Column(String, nullable=True)
    clip = Column(String, nullable=True)
    text = Column(Text, nullable=False)
    parts = Column(Text, nullable=True)       # which openings/sign-offs it used (JSON)
    source = Column(String, default="writer")  # writer | ai
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(String, nullable=True)
    copied_at = Column(DateTime, nullable=True)


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

DEFAULT_SETTINGS = {"hashtags": "", "signoff": "", "ai_key": "", "ai_model": "claude-sonnet-4-5"}


def load_settings() -> dict:
    try:
        d = json.loads(SETTINGS_FILE.read_text())
        return {**DEFAULT_SETTINGS, **{k: v for k, v in d.items() if k in DEFAULT_SETTINGS}}
    except Exception:
        return dict(DEFAULT_SETTINGS)


def _public(s: dict) -> dict:
    key = s.get("ai_key") or ""
    return {"hashtags": s["hashtags"], "signoff": s["signoff"], "ai_model": s["ai_model"],
            "ai": bool(key), "ai_key_hint": ("..." + key[-4:]) if key else ""}


# --------------------------------------------------------------------------
# words
# --------------------------------------------------------------------------

def money(n: Optional[int], rent: bool) -> str:
    if not n:
        return ""
    s = f"R{n:,}".replace(",", " ")
    return s + (" per month" if rent else "")


def _num(x) -> str:
    if x is None:
        return ""
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def kind_word(f: dict) -> str:
    return {"apartment": "apartment", "townhouse": "townhouse", "plot": "plot",
            "commercial": "space", "house": "home"}.get(f.get("kind") or "", "home")


def _list(items: List[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _tag(s: str) -> str:
    words = re.sub(r"[^\w\s]", " ", s or "").split()
    return "#" + "".join(w[:1].upper() + w[1:] for w in words) if words else ""


# Openings. {k} is the kind of place (home, apartment...), {where} "in Sea
# Point" or "", {beds} "3-bedroom " or "". Each has an id so a property never
# gets the same one twice in a row of posts.
HOOKS: Dict[str, List[str]] = {
    "warm": [
        "Come on in - this {beds}{k} {where}is one you will want to see.",
        "Picture your mornings here: a {beds}{k} {where}with room to breathe.",
        "Some places just feel like home the moment you walk in. This {beds}{k} {where}is one of them.",
        "Here is a {beds}{k} {where}made for real life - and for having people over.",
        "Take a slow look around this {beds}{k} {where}.",
        "Light, space and a good feeling all round: meet this {beds}{k} {where}.",
        "If you have been waiting for the right {k} {where}, this could be it.",
        "A {beds}{k} {where}that is ready for its next chapter.",
        "Walk through this {beds}{k} {where}with us.",
        "This {beds}{k} {where}has the kind of space families look for.",
        "Welcome to a {beds}{k} {where}that simply works.",
        "Easy living, good light and space to grow - this {beds}{k} {where}has it.",
    ],
    "bold": [
        "Just listed: {beds}{k} {where}.",
        "Stop scrolling - this {beds}{k} {where}needs a look.",
        "New on the market: a {beds}{k} {where}that ticks the boxes.",
        "This one will not sit around for long. {Beds}{k} {where}.",
        "Fresh listing alert: {beds}{k} {where}.",
        "Viewings are open on this {beds}{k} {where}.",
        "Big space, great spot: {beds}{k} {where}.",
        "Here it is - the {beds}{k} {where}everyone will be asking about.",
        "Out now: the full tour of this {beds}{k} {where}.",
        "Do not miss this {beds}{k} {where}.",
        "Ready when you are: {beds}{k} {where}.",
        "Just in - a {beds}{k} {where}worth moving for.",
    ],
    "luxury": [
        "An exceptional {beds}{k} {where}, finished without compromise.",
        "Refined, generous and quietly impressive: a {beds}{k} {where}.",
        "Presenting a {beds}{k} {where}of rare quality.",
        "Designed for those who notice the details - a {beds}{k} {where}.",
        "A residence that sets its own standard. {Beds}{k} {where}.",
        "Elegance at every turn in this {beds}{k} {where}.",
        "Where space meets craftsmanship: a {beds}{k} {where}.",
        "Discover a {beds}{k} {where}made for living well.",
        "Understated luxury in a {beds}{k} {where}.",
        "Considered design, beautiful light - this {beds}{k} {where}delivers both.",
        "A {beds}{k} {where}for the discerning buyer.",
        "Timeless style in a {beds}{k} {where}.",
    ],
    "simple": [
        "{Beds}{k} {where}.",
        "For sale: {beds}{k} {where}.",
        "Now showing: {beds}{k} {where}.",
        "Take a look at this {beds}{k} {where}.",
        "Available now: {beds}{k} {where}.",
        "On the market: {beds}{k} {where}.",
        "{Beds}{k} {where}- see the tour.",
        "Here is a {beds}{k} {where}.",
        "A {beds}{k} {where}worth a look.",
        "Presenting a {beds}{k} {where}.",
        "Listed today: {beds}{k} {where}.",
        "Tour this {beds}{k} {where}.",
    ],
}

# What the clip is, said a few ways - so the drone post and the walkthrough
# post of one property do not read the same.
CLIP_LINES: Dict[str, List[str]] = {
    "tour": ["Walk through every room in the full tour.", "Here is the full walkthrough, room by room.",
             "Take the tour - every room, start to finish.", "The full video tour shows how it all flows together."],
    "drone": ["From the air you see just how well it sits.", "The drone view shows the whole setting.",
              "See the position from above - the surroundings say a lot.", "A bird's-eye look at the property and the area around it."],
    "photos": ["Swipe through the photos.", "The photos tell the story - swipe through.",
               "Have a look through the full set of photos.", "Every room, photographed - swipe to see them."],
    "twilight": ["It looks even better as the lights come on.", "Dusk shows this place at its best.",
                 "Seen at twilight, when it really glows.", "Evenings here look like this."],
    "teaser": ["A quick look - the full tour is coming.", "Just a taste. Full tour on the way.",
               "Here is a first look.", "A short teaser before the full tour."],
    "details": ["It is the details that make it.", "Look closer - the finishes are worth it.",
                "Small things, done well.", "The finishes up close."],
}

FEATURE_LINES = [
    "Highlights include {list}.",
    "Think {list}.",
    "You get {list}.",
    "Standouts: {list}.",
    "Expect {list}.",
    "Among the best bits: {list}.",
    "Add {list} to that.",
    "Features include {list}.",
]

CTAS = {
    "instagram": ["Send us a message to book a viewing.", "DM for details or to view.", "Message us to arrange a viewing.",
                  "Save this post and share it with someone who needs to see it.", "Tag someone who would love this.",
                  "Link in bio for the full listing.", "Ask us anything in the comments.", "Viewings by appointment - get in touch."],
    "facebook": ["Message us to arrange a viewing.", "Get in touch to view it.", "Comment or message us for the details.",
                 "Share this with someone who is looking.", "Viewings by appointment - send us a message.",
                 "Call or message to book your viewing."],
    "short": ["Follow for more tours.", "DM to view.", "Save this one.", "Full tour on our page.", "Want to see it? Message us."],
    "listing": ["Contact the agent to arrange a viewing.", "Viewings strictly by appointment.",
                "Call the agent today to arrange your viewing.", "Get in touch to arrange a private viewing."],
    "whatsapp": ["Reply here if you would like to view it.", "Let me know if you want to see it.",
                 "Happy to set up a viewing - just reply.", "Want to have a look? Reply and I will arrange it."],
}

LISTING_BODIES = [
    "Offering {size} of living space{erf}, the layout is practical and easy to live with.",
    "The layout makes good use of {size}{erf}, with living areas that flow naturally.",
    "Set on {erf_only}, the {k} gives you room inside and out.",
    "Inside, {size} is laid out for everyday living and entertaining alike.",
]

GENERIC_TAGS = {
    "sale": ["#PropertyForSale", "#HomeForSale", "#RealEstate", "#NewListing", "#JustListed", "#PropertyTour", "#DreamHome", "#HouseHunting"],
    "rent": ["#ToLet", "#ForRent", "#RentalProperty", "#RealEstate", "#NewListing", "#PropertyTour", "#HomeToRent"],
}
CLIP_TAGS = {"tour": ["#HomeTour", "#PropertyVideo", "#Walkthrough"], "drone": ["#DroneFootage", "#AerialView", "#DronePhotography"],
             "photos": ["#RealEstatePhotography", "#PropertyPhotography", "#InteriorDesign"],
             "twilight": ["#TwilightPhotography", "#GoldenHour"], "teaser": ["#ComingSoon", "#Reels"],
             "details": ["#InteriorDetails", "#Finishes"]}


def guess_clip(v: Optional[Video]) -> str:
    """What sort of post a file makes, from its name, folder and length."""
    if not v:
        return ""
    s = f"{v.filepath or ''} {v.filename or ''}".lower()
    if re.search(r"twilight|dusk|night", s):
        return "twilight"
    if re.search(r"\bdji|drone|aerial|mavic|fpv", s):
        return "drone"
    if (v.media_type or "") in ("image", "photo"):
        return "photos"
    if re.search(r"teaser|reel|short|vertical|story", s) or (v.duration and v.duration < 25):
        return "teaser"
    if re.search(r"walk|tour|through", s):
        return "tour"
    if v.media_type == "video":
        return "tour"
    return ""


class Opts(BaseModel):
    platform: str = "instagram"
    tone: str = "warm"
    clip: str = ""
    video_id: Optional[int] = None
    price: bool = True
    agent: bool = True
    address: bool = False
    hashtags: bool = True
    emoji: bool = False


def _pick(rng: random.Random, n: int, used, prefix: str) -> int:
    """One of n, not used for this property yet - or, once they all have
    been, one of those used longest ago (never one of the last few)."""
    recent = list(used) if isinstance(used, list) else []
    seen = set(used)
    fresh = [i for i in range(n) if f"{prefix}{i}" not in seen]
    if fresh:
        return rng.choice(fresh)
    order = [v for v in recent if v.startswith(prefix)]            # newest first
    last_seen = {}
    for k, v in enumerate(order):
        last_seen.setdefault(v, k)
    ranked = sorted(range(n), key=lambda i: -last_seen.get(f"{prefix}{i}", 10**6))
    return rng.choice(ranked[: max(1, n // 2)])


def write(facts: dict, place: dict, o: Opts, used, rng: Optional[random.Random] = None,
          settings: Optional[dict] = None) -> tuple:
    """(text, parts). `used` holds the part ids of this property's earlier
    captions; the ones not used yet are picked first."""
    rng = rng or random.Random()
    settings = settings or load_settings()
    f = facts
    rent = f.get("listing") == "rent"
    k = kind_word(f)
    beds = _num(f.get("beds"))
    beds_s = f"{beds}-bedroom " if beds and k != "plot" else ""
    # a map lookup can leave a municipal ward as the suburb ("Cape Town Ward
    # 115"): nobody calls a place that in a caption
    suburb = re.sub(r"\s*\bWard\s*\d+\b", "", (place.get("suburb") or "")).strip(" ,-")
    where = f"in {suburb} " if suburb else ""
    if o.address and place.get("address") and not re.match(r"^shoot (at|in) ", place["address"], re.I):
        where = f"at {place['address']}{', ' + suburb if suburb else ''} "
    tone = o.tone if o.tone in TONES else "warm"
    parts: dict = {}

    def fill(t: str) -> str:
        t = t.replace("{Beds}", (beds_s[:1].upper() + beds_s[1:]) if beds_s else "").replace("{beds}", beds_s)
        t = t.replace("{k}", k).replace("{where}", where)
        t = re.sub(r"\s+([.,])", r"\1", t).replace(" - .", ".").strip()
        t = re.sub(r"\s{2,}", " ", t)
        return t[:1].upper() + t[1:]

    hi = _pick(rng, len(HOOKS[tone]), used, f"h{tone}")
    parts["hook"] = f"h{tone}{hi}"
    hook = fill(HOOKS[tone][hi])
    if rent and tone == "simple":
        hook = hook.replace("For sale:", "To let:")

    # the facts line, in one of a few shapes
    bits = []
    em = o.emoji
    if beds and k != "plot":
        bits.append(("🛏 " if em else "") + f"{beds} bed{'s' if beds != '1' else ''}")
    if f.get("baths"):
        b = _num(f["baths"])
        bits.append(("🛁 " if em else "") + f"{b} bath{'s' if b != '1' else ''}")
    if f.get("parking"):
        bits.append(("🚗 " if em else "") + f"{f['parking']} parking")
    if f.get("floor_m2"):
        bits.append(("📐 " if em else "") + f"{f['floor_m2']} m² inside")
    if f.get("erf_m2"):
        bits.append(f"{f['erf_m2']} m² {'plot' if k == 'plot' else 'erf'}")
    price = money(f.get("price"), rent) if o.price else ""
    shape = rng.randrange(3)
    parts["facts"] = shape
    facts_txt = ""
    if bits:
        if o.platform in ("listing",):
            facts_txt = ""
        elif shape == 0:
            facts_txt = " | ".join(bits)
        elif shape == 1:
            facts_txt = " · ".join(bits)
        else:
            facts_txt = "\n".join(("• " if not em else "") + b for b in bits)
    if price and o.platform != "listing":
        pl = rng.choice(["Rent: {p}", "{p}", "Available at {p}"] if rent else ["Asking {p}", "Priced at {p}", "{p}"])
        facts_txt = (facts_txt + ("\n" if facts_txt else "") + ("💰 " if em else "") + pl.replace("{p}", price)).strip()

    feats = list(f.get("features") or [])
    rng.shuffle(feats)
    feat_txt = ""
    if feats:
        take = feats[: rng.choice([2, 3, 3, 4])] if o.platform not in ("short", "whatsapp") else feats[:2]
        fi = _pick(rng, len(FEATURE_LINES), used, "f")
        if o.platform == "listing" and fi not in (0, 4, 7):
            fi = rng.choice((0, 4, 7))
        parts["feat"] = f"f{fi}"
        feat_txt = FEATURE_LINES[fi].replace("{list}", _list([x[:1].lower() + x[1:] if x[:2].lower() != x[:2].upper() or len(x) < 2 else x for x in take]))

    clip_txt = ""
    if o.clip in CLIP_LINES:
        ci = _pick(rng, len(CLIP_LINES[o.clip]), used, f"c{o.clip}")
        parts["clip"] = f"c{o.clip}{ci}"
        clip_txt = CLIP_LINES[o.clip][ci]

    ctas = CTAS.get(o.platform, CTAS["instagram"])
    cti = _pick(rng, len(ctas), used, f"a{o.platform}")
    parts["cta"] = f"a{o.platform}{cti}"
    cta = ctas[cti]

    agent_txt = ""
    if o.agent and place.get("agent"):
        a = place["agent"]
        ag = place.get("agency")
        ph = place.get("phone")
        who = a + (f" ({ag})" if ag else "")
        forms = [f"Agent: {who}" + (f" - {ph}" if ph else ""),
                 f"Contact {who}" + (f" on {ph}" if ph else "") + ".",
                 f"Listed by {who}." + (f" {ph}" if ph else ""),
                 f"Viewings: {who}" + (f", {ph}" if ph else "")]
        agent_txt = forms[rng.randrange(len(forms))]
        if em:
            agent_txt = "📞 " + agent_txt
    signoff = (settings.get("signoff") or "").strip()

    tags = []
    if o.hashtags and o.platform in ("instagram", "facebook", "short"):
        pool = list(GENERIC_TAGS["rent" if rent else "sale"])
        rng.shuffle(pool)
        local = []
        if suburb:
            local = [_tag(suburb), _tag(suburb + (" Rentals" if rent else " Property")),
                     _tag(suburb + " Living"), _tag(suburb + " Homes")]
            rng.shuffle(local)
        clip_tags = list(CLIP_TAGS.get(o.clip, []))
        rng.shuffle(clip_tags)
        n = {"instagram": 14, "facebook": 4, "short": 5}[o.platform]
        mine = [t if t.startswith("#") else "#" + t for t in re.split(r"[\s,]+", settings.get("hashtags") or "") if t.strip("#")]
        for t in mine + local[:2] + clip_tags[:2] + pool + local[2:]:
            if t and t.lower() not in [x.lower() for x in tags]:
                tags.append(t)
        tags = tags[:max(n, len(mine))]

    if o.platform == "listing":
        size = f"{f['floor_m2']} m²" if f.get("floor_m2") else ""
        erf = f"{f['erf_m2']} m²" if f.get("erf_m2") else ""
        body = ""
        if size or erf:
            opts = [i for i, t in enumerate(LISTING_BODIES) if (size or "{size}" not in t) and (erf or "{erf_only}" not in t)]
            if opts:
                bi = rng.choice([i for i in opts if f"b{i}" not in set(used)] or opts)
                parts["body"] = f"b{bi}"
                body = (LISTING_BODIES[bi].replace("{size}", size or "the space")
                        .replace("{erf_only}", erf).replace("{erf}", f" on a {erf} erf" if erf and size else "")
                        .replace("{k}", k))
        rooms = []
        if beds and k != "plot":
            rooms.append(f"{beds} bedroom{'s' if beds != '1' else ''}")
        if f.get("baths"):
            b = _num(f["baths"]); rooms.append(f"{b} bathroom{'s' if b != '1' else ''}")
        if f.get("parking"):
            rooms.append(f"parking for {f['parking']}")
        rooms_txt = f"There {'are' if rooms and not rooms[0].startswith('1 ') else 'is'} {_list(rooms)}." if rooms else ""
        price_txt = f"{'Rental' if rent else 'Asking price'}: {price}." if price else ""
        paras = [hook, " ".join(x for x in (body, rooms_txt) if x), feat_txt, " ".join(x for x in (price_txt, cta) if x), agent_txt, signoff]
        text = "\n\n".join(p for p in paras if p)
    elif o.platform == "whatsapp":
        line = " ".join(x for x in (hook, clip_txt) if x)
        text = "\n".join(p for p in (line, facts_txt.replace("\n", " · ") if facts_txt else "", feat_txt, cta, agent_txt, signoff) if p)
    elif o.platform == "short":
        text = "\n\n".join(p for p in (hook + (" " + clip_txt if clip_txt else ""), facts_txt.replace("\n", " · "), cta, " ".join(tags)) if p)
    else:
        first = " ".join(x for x in (hook, clip_txt) if x)
        paras = [first, feat_txt, facts_txt, cta, agent_txt, signoff, " ".join(tags) if tags else ""]
        text = "\n\n".join(p for p in paras if p)
    return text.strip(), parts


# --------------------------------------------------------------------------
# Claude (optional)
# --------------------------------------------------------------------------

def write_ai(facts: dict, place: dict, o: Opts, earlier: List[str], settings: dict) -> str:
    rent = facts.get("listing") == "rent"
    known = {
        "kind": facts.get("kind"), "listing": "to rent" if rent else ("for sale" if facts.get("listing") else None),
        "price": money(facts.get("price"), rent) if o.price else None,
        "bedrooms": facts.get("beds"), "bathrooms": facts.get("baths"), "parking": facts.get("parking"),
        "floor_m2": facts.get("floor_m2"), "erf_m2": facts.get("erf_m2"), "features": facts.get("features") or None,
        "suburb": place.get("suburb"), "address": place.get("address") if o.address else None,
        "agent": ({"name": place.get("agent"), "agency": place.get("agency"), "phone": place.get("phone")}
                  if o.agent and place.get("agent") else None),
    }
    known = {k: v for k, v in known.items() if v not in (None, "", [])}
    where = {"instagram": "an Instagram post", "facebook": "a Facebook post", "short": "a TikTok / Reels / Shorts caption (short)",
             "listing": "a property portal listing description (no hashtags, no emoji, 2-4 short paragraphs)",
             "whatsapp": "a WhatsApp message to a buyer (short, personal)"}[o.platform]
    clip = {"tour": "the video walkthrough", "drone": "the drone video", "photos": "the photo set",
            "twilight": "the twilight photos", "teaser": "a short teaser clip", "details": "close-ups of the finishes"}.get(o.clip, "")
    rules = [
        f"Write {where} for this property" + (f", to go with {clip}" if clip else "") + ".",
        f"Tone: {o.tone}.",
        "Use ONLY these facts; never invent rooms, views, finishes, prices or places: " + json.dumps(known),
        "South African English and Rand amounts as given.",
        "Emoji: " + ("a few, tastefully" if o.emoji else "none") + ".",
        ("Hashtags at the end" + (f", always including: {settings.get('hashtags')}" if settings.get("hashtags") else "")
         if o.hashtags and o.platform in ("instagram", "facebook", "short") else "No hashtags."),
    ]
    if settings.get("signoff"):
        rules.append(f"End with this sign-off line: {settings['signoff']}")
    if earlier:
        rules.append("It must read clearly differently from these earlier captions for the same property "
                     "(different opening, structure and wording):\n---\n" + "\n---\n".join(earlier[:8]))
    rules.append("Reply with the caption text only.")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps({"model": settings.get("ai_model") or DEFAULT_SETTINGS["ai_model"], "max_tokens": 900,
                         "messages": [{"role": "user", "content": "\n".join(rules)}]}).encode(),
        headers={"x-api-key": settings["ai_key"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=40) as r:
        d = json.loads(r.read())
    return "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text").strip()


# --------------------------------------------------------------------------
# api
# --------------------------------------------------------------------------

def _shoot(db: Session, shoot_id: int):
    import shoots
    s = db.query(shoots.Shoot).filter(shoots.Shoot.id == shoot_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="No such property")
    return s


def _place(db: Session, s) -> dict:
    import shoots
    out = {"address": s.address, "suburb": s.suburb, "agent": s.agent}
    if s.agent:
        a = db.query(shoots.Agent).filter(shoots.Agent.name == s.agent).first()
        if a:
            out.update(agency=a.agency, phone=a.phone)
    return out


def _dict(c: Caption) -> dict:
    return {"id": c.id, "platform": c.platform, "tone": c.tone, "clip": c.clip, "video_id": c.video_id,
            "text": c.text, "source": c.source, "created_at": c.created_at.isoformat() if c.created_at else None,
            "copied_at": c.copied_at.isoformat() if c.copied_at else None}


@router.get("/api/shoots/{shoot_id}/captions")
def list_captions(shoot_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _shoot(db, shoot_id)
    rows = (db.query(Caption).filter(Caption.shoot_id == shoot_id, Caption.copied_at.isnot(None))
            .order_by(Caption.copied_at.desc()).limit(50).all())
    return {"captions": [_dict(c) for c in rows], "settings": _public(load_settings())}


@router.get("/api/shoots/{shoot_id}/captions/guess")
def guess(shoot_id: int, video_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    v = db.query(Video).filter(Video.id == video_id).first()
    return {"clip": guess_clip(v)}


@router.post("/api/shoots/{shoot_id}/captions")
def make_caption(shoot_id: int, o: Opts, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    import shoots
    s = _shoot(db, shoot_id)
    if o.platform not in PLATFORMS:
        raise HTTPException(status_code=400, detail="Unknown place to post.")
    if o.clip not in CLIPS:
        o.clip = ""
    facts = shoots.facts_of(s)
    place = _place(db, s)
    earlier = db.query(Caption).filter(Caption.shoot_id == s.id).order_by(Caption.id.desc()).limit(60).all()
    used: list = []                     # newest first
    for c in earlier:
        try:
            used.extend(str(v) for v in json.loads(c.parts or "{}").values())
        except ValueError:
            pass
    settings = load_settings()
    source, note = "writer", None
    text = ""
    if settings.get("ai_key"):
        try:
            text = write_ai(facts, place, o, [c.text for c in earlier if c.platform == o.platform][:8], settings)
            source = "ai"
        except Exception as e:  # the built-in writer still works
            note = f"Claude could not be reached ({str(e)[:120]}), so the built-in writer wrote this one."
    parts = {}
    if not text:
        text, parts = write(facts, place, o, used, settings=settings)
    c = Caption(shoot_id=s.id, video_id=o.video_id, platform=o.platform, tone=o.tone, clip=o.clip,
                text=text, parts=json.dumps(parts), source=source, created_by=current_user.username)
    db.add(c)
    db.commit()
    # keep the pile of never-used drafts small; copied ones are the history
    stale = (db.query(Caption).filter(Caption.shoot_id == s.id, Caption.copied_at.is_(None))
             .order_by(Caption.id.desc()).offset(80).all())
    for x in stale:
        db.delete(x)
    db.commit()
    out = _dict(c)
    missing = [n for n, key in (("bedrooms", "beds"), ("price", "price"), ("features", "features")) if not facts.get(key)]
    out["missing"] = missing
    if note:
        out["note"] = note
    return out


@router.post("/api/shoots/{shoot_id}/captions/{caption_id}/copied")
def copied(shoot_id: int, caption_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    c = db.query(Caption).filter(Caption.id == caption_id, Caption.shoot_id == shoot_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="No such caption")
    c.copied_at = datetime.utcnow()
    db.commit()
    return _dict(c)


class EditText(BaseModel):
    text: str = Field(..., max_length=6000)


@router.patch("/api/shoots/{shoot_id}/captions/{caption_id}")
def edit_caption(shoot_id: int, caption_id: int, body: EditText, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    c = db.query(Caption).filter(Caption.id == caption_id, Caption.shoot_id == shoot_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="No such caption")
    c.text = body.text
    db.commit()
    return _dict(c)


@router.delete("/api/shoots/{shoot_id}/captions/{caption_id}")
def delete_caption(shoot_id: int, caption_id: int, db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    c = db.query(Caption).filter(Caption.id == caption_id, Caption.shoot_id == shoot_id).first()
    if c:
        db.delete(c)
        db.commit()
    return {"status": "ok"}


class SettingsBody(BaseModel):
    hashtags: Optional[str] = Field(None, max_length=600)
    signoff: Optional[str] = Field(None, max_length=300)
    ai_key: Optional[str] = Field(None, max_length=300)
    ai_model: Optional[str] = Field(None, max_length=80)


@router.get("/api/captions/settings")
def get_settings(current_user: User = Depends(get_current_user)):
    return _public(load_settings())


@router.put("/api/captions/settings")
def put_settings(body: SettingsBody, current_user: User = Depends(get_current_user)):
    s = load_settings()
    if body.ai_key is not None or body.ai_model is not None:
        if not permissions.can(current_user.role, permissions.ADMIN):
            raise HTTPException(status_code=403, detail="Only an administrator can set the Claude key.")
    for k in ("hashtags", "signoff", "ai_model"):
        v = getattr(body, k)
        if v is not None:
            s[k] = v.strip()
    if body.ai_key is not None:
        s["ai_key"] = body.ai_key.strip()
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))
    try:
        SETTINGS_FILE.chmod(0o600)
    except OSError:
        pass
    return _public(s)


def install(app):
    Base.metadata.create_all(bind=engine, tables=[Caption.__table__])
    app.include_router(router)
