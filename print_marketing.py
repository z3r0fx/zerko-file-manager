"""Print marketing for a property: a flyer, a four-page brochure and a window card, as PDFs.

Made from the property's own edited photos (with their edits, like the listing pack), the facts on its
About the property form, the newest listing text, the agent's card and the studio's name, plus a QR code
to the listing page. Drawn with Pillow at print resolution and saved as a PDF, so there is nothing to
install and what you see is what prints. A4, with the page edge safe for home and shop printers."""
from __future__ import annotations

import io
import os
import re
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import get_current_user
from database import User, Video, get_db

router = APIRouter(tags=["print"])

DPI = 200
A4 = (210, 297)                                     # mm
KINDS = ("flyer", "brochure", "window")


def mm(v: float) -> int:
    return int(round(v / 25.4 * DPI))


# --------------------------------------------------------------------------
# fonts: whatever good sans the computer has, the built-in one as a last resort
# --------------------------------------------------------------------------

_FONT_DIRS = ["/usr/share/fonts/truetype/ubuntu", "/mnt/c/Windows/Fonts", "C:/Windows/Fonts", "/usr/share/fonts/truetype/dejavu",
              "/usr/share/fonts/truetype", "/System/Library/Fonts/Supplemental"]
_FONT_NAMES = {
    "bold": ["Ubuntu-B.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf"],
    "medium": ["Ubuntu-M.ttf", "seguisb.ttf", "DejaVuSans.ttf", "Arial.ttf", "arial.ttf"],
    "regular": ["Ubuntu-R.ttf", "segoeui.ttf", "DejaVuSans.ttf", "Arial.ttf", "arial.ttf"],
    "light": ["Ubuntu-L.ttf", "segoeuil.ttf", "DejaVuSans.ttf", "Arial.ttf", "arial.ttf"],
}
_font_cache: dict = {}


def font(weight: str, size_pt: float) -> ImageFont.FreeTypeFont:
    px = max(6, int(round(size_pt / 72 * DPI)))
    key = (weight, px)
    if key in _font_cache:
        return _font_cache[key]
    f = None
    for name in _FONT_NAMES[weight]:
        for d in _FONT_DIRS:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                try:
                    f = ImageFont.truetype(p, px)
                except OSError:
                    continue
                # Ubuntu's files are one variable font behind several names: pick the weight asked for
                try:
                    names = [n.decode() if isinstance(n, bytes) else n for n in f.get_variation_names()]
                    want = {"bold": "Bold", "medium": "Medium", "regular": "Regular", "light": "Light"}[weight]
                    if want in names:
                        f.set_variation_by_name(want)
                except Exception:
                    pass
                break
        if f:
            break
    if f is None:
        f = ImageFont.load_default(px)
    _font_cache[key] = f
    return f


def hex_rgb(h: str, default=(31, 41, 55)) -> tuple:
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", (h or "").strip())
    if not m:
        return default
    v = m.group(1)
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))


# --------------------------------------------------------------------------
# drawing helpers
# --------------------------------------------------------------------------

def cover(img: Image.Image, w: int, h: int) -> Image.Image:
    """Fill w x h, cropping the middle (a touch above centre: rooms keep their ceilings, lawns lose some grass)."""
    iw, ih = img.size
    s = max(w / iw, h / ih)
    nw, nh = max(w, int(iw * s + 0.5)), max(h, int(ih * s + 0.5))
    im = img.resize((nw, nh), Image.LANCZOS)
    x = (nw - w) // 2
    y = int((nh - h) * 0.45)
    return im.crop((x, y, x + w, y + h))


def rounded(im: Image.Image, r: int) -> Image.Image:
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, im.size[0] - 1, im.size[1] - 1), r, fill=255)
    out = Image.new("RGBA", im.size)
    out.paste(im, (0, 0), mask)
    return out


def paste_photo(page: Image.Image, img: Optional[Image.Image], box: tuple, r: int = 0):
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if img is None:
        ImageDraw.Draw(page).rounded_rectangle(box, r, fill=(228, 228, 231))
        return
    c = cover(img, w, h)
    if r:
        c = rounded(c, r)
        page.paste(c, (x0, y0), c)
    else:
        page.paste(c, (x0, y0))


def wrap(draw: ImageDraw.ImageDraw, text: str, f, width: int) -> List[str]:
    lines = []
    for para in (text or "").split("\n"):
        words, line = para.split(), ""
        if not words:
            lines.append("")
            continue
        for w in words:
            t = f"{line} {w}".strip()
            if draw.textlength(t, font=f) <= width:
                line = t
            else:
                if line:
                    lines.append(line)
                line = w
        lines.append(line)
    return lines


def text_block(draw, xy, text, f, width, fill, leading=1.35, max_lines=0) -> int:
    """Wrapped text; returns the y under it. Cut with an ellipsis past max_lines."""
    x, y = xy
    lines = wrap(draw, text, f, width)
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and draw.textlength(last + "...", font=f) > width:
            last = last[:-1]
        lines[-1] = last.rstrip(" ,.;") + "..."
    step = int(f.size * leading)
    for ln in lines:
        draw.text((x, y), ln, font=f, fill=fill)
        y += step
    return y


def fit_font(draw, text, weight, start_pt, width, min_pt=10):
    pt = start_pt
    while pt > min_pt and draw.textlength(text, font=font(weight, pt)) > width:
        pt -= 1
    return font(weight, pt)


def qr_image(url: str, px: int, dark=(17, 17, 17)) -> Optional[Image.Image]:
    if not url:
        return None
    try:
        import segno
    except ImportError:
        return None
    q = segno.make(url, error="m")
    buf = io.BytesIO()
    q.save(buf, kind="png", scale=10, border=2, dark="#%02x%02x%02x" % dark, light="#ffffff")
    buf.seek(0)
    return Image.open(buf).convert("RGB").resize((px, px), Image.NEAREST)


# --------------------------------------------------------------------------
# what goes on the page
# --------------------------------------------------------------------------

class PrintBody(BaseModel):
    kind: str = "flyer"
    photos: List[int] = Field(default_factory=list, max_length=24)   # the first is the big one
    accent: str = Field("#1f2937", max_length=9)
    price: bool = True
    qr_url: Optional[str] = Field(None, max_length=500)
    headline: Optional[str] = Field(None, max_length=90)
    text: Optional[str] = Field(None, max_length=3000)


def _facts_words(f: dict) -> List[tuple]:
    out = []
    for key, one, many in (("beds", "Bedroom", "Bedrooms"), ("baths", "Bathroom", "Bathrooms"), ("parking", "Parking", "Parking")):
        v = f.get(key)
        if v:
            n = int(v) if float(v).is_integer() else v
            out.append((str(n), one if n == 1 else many))
    if f.get("floor_m2"):
        out.append((f"{int(f['floor_m2'])} m\u00b2", "Floor"))
    if f.get("erf_m2"):
        out.append((f"{int(f['erf_m2'])} m\u00b2", "Erf"))
    return out


def gather(db: Session, shoot_id: int, body: PrintBody) -> dict:
    import business
    import captions
    import listing_pack
    import photo_edit as pe
    import shoots
    s = db.query(shoots.Shoot).filter(shoots.Shoot.id == shoot_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="No such property")
    facts = shoots.facts_of(s)
    ids = list(body.photos)
    if not ids:
        paths = [f.path for f in db.query(shoots.ShootFolder).filter(shoots.ShootFolder.shoot_id == s.id).all()]
        ids = [v.id for v in listing_pack._photos(db, paths)]
        if s.cover_video_id and s.cover_video_id in ids:
            ids.remove(s.cover_video_id)
            ids.insert(0, s.cover_video_id)
    need = {"flyer": 4, "brochure": 11, "window": 3}[body.kind]
    imgs = []
    for vid in ids[:need]:
        try:
            p = pe.developed_file(db, vid, 2400 if not imgs else 1600)
            imgs.append(Image.open(p).convert("RGB"))
        except Exception as e:
            print(f"print: photo {vid} skipped: {e}", flush=True)
    if not imgs:
        raise HTTPException(status_code=400, detail="This property has no photos to print yet. Link its folder first.")
    agent = None
    if s.agent:
        a = db.query(shoots.Agent).filter(shoots.Agent.name == s.agent).first()
        agent = {"name": s.agent, "agency": (a.agency if a else None) or s.agency, "phone": (a.phone if a else None) or s.booked_phone,
                 "email": (a.email if a else None) or s.booked_email}
    text = body.text
    if text is None:
        c = (db.query(captions.Caption).filter(captions.Caption.shoot_id == s.id, captions.Caption.platform == "listing")
             .order_by(captions.Caption.copied_at.is_(None), captions.Caption.id.desc()).first())
        text = c.text if c else ""
    rent = facts.get("listing") == "rent"
    kind = captions.kind_word(facts)
    beds = facts.get("beds")
    head = body.headline or (f"{int(beds)} bedroom {kind}" if beds else kind.capitalize()) + (f" in {s.suburb}" if s.suburb else "")
    b = business.settings(db)
    return {
        "shoot": s, "facts": facts, "imgs": imgs, "agent": agent, "text": (text or "").strip(),
        "headline": head[0].upper() + head[1:], "price": captions.money(facts.get("price"), rent) if body.price else "",
        "listing": "To rent" if rent else ("For sale" if facts.get("listing") else ""),
        "features": facts.get("features") or [], "studio": business.studio_name(b), "accent": hex_rgb(body.accent),
        "qr": (body.qr_url or "").strip(),
    }


INK, SOFT, FAINT, PAPER = (17, 17, 20), (82, 82, 91), (161, 161, 170), (255, 255, 255)


def _page() -> Image.Image:
    return Image.new("RGB", (mm(A4[0]), mm(A4[1])), PAPER)


def _facts_row(d, x, y, w, facts, accent, big=False) -> int:
    items = _facts_words(facts)
    if not items:
        return y
    n = len(items)
    cw = w / n
    fv, fl = font("bold", 26 if big else 17), font("regular", 10 if big else 8)
    for i, (v, label) in enumerate(items):
        cx = int(x + cw * i)
        if i:
            d.line((cx, y + mm(1), cx, y + mm(big and 15 or 11)), fill=(228, 228, 231), width=max(1, mm(0.3)))
        d.text((cx + (mm(4) if i else 0), y), v, font=fv, fill=accent)
        d.text((cx + (mm(4) if i else 0), y + int(fv.size * 1.15)), label.upper(), font=fl, fill=SOFT)
    return y + int(fv.size * 1.15) + int(fl.size * 1.6)


def _agent_card(page, d, box, g, qr_px=0):
    x0, y0, x1, y1 = box
    a, acc = g["agent"], g["accent"]
    d.rectangle(box, fill=acc)
    pad = mm(7)
    tx = x0 + pad
    qr = qr_image(g["qr"], qr_px or (y1 - y0 - pad * 2), dark=INK)
    right = x1 - pad
    if qr:
        qx = x1 - pad - qr.size[0]
        qy = y0 + (y1 - y0 - qr.size[1]) // 2
        page.paste(qr, (qx, qy))
        right = qx - mm(5)
        f = font("regular", 7)
        d.text((qx + qr.size[0] // 2, qy + qr.size[1] + mm(1.2)), "Scan to see more", font=f, fill=(255, 255, 255), anchor="mt")
    y = y0 + pad
    if a:
        d.text((tx, y), "YOUR AGENT", font=font("medium", 8), fill=(255, 255, 255, 180))
        y += mm(5)
        fn = fit_font(d, a["name"], "bold", 20, right - tx)
        d.text((tx, y), a["name"], font=fn, fill=(255, 255, 255))
        y += int(fn.size * 1.25)
        if a.get("agency"):
            d.text((tx, y), a["agency"], font=font("regular", 11), fill=(229, 231, 235))
            y += mm(6)
        line = "   ".join(x for x in [a.get("phone"), a.get("email")] if x)
        if line:
            d.text((tx, y + mm(1)), line, font=fit_font(d, line, "medium", 12, right - tx), fill=(255, 255, 255))
    else:
        d.text((tx, y), "For viewings", font=font("bold", 18), fill=(255, 255, 255))
    d.text((tx, y1 - pad + mm(2)), f"Photography: {g['studio']}", font=font("light", 7), fill=(209, 213, 219), anchor="lb")


def flyer(g) -> List[Image.Image]:
    page = _page()
    d = ImageDraw.Draw(page, "RGBA")
    W, H = page.size
    M = mm(12)
    imgs, acc = g["imgs"], g["accent"]
    hero_h = mm(138)
    paste_photo(page, imgs[0], (0, 0, W, hero_h))
    # price band over the photo's foot
    if g["price"] or g["listing"]:
        band = (M, hero_h - mm(18), M + mm(95), hero_h + mm(4))
        d.rectangle(band, fill=acc)
        d.text((band[0] + mm(5), band[1] + mm(4)), g["listing"].upper() or " ", font=font("medium", 8), fill=(229, 231, 235))
        d.text((band[0] + mm(5), band[1] + mm(9)), g["price"] or g["listing"], font=fit_font(d, g["price"] or g["listing"], "bold", 24, mm(85)), fill=(255, 255, 255))
    y = hero_h + mm(12)
    fh = fit_font(d, g["headline"], "bold", 26, W - 2 * M)
    d.text((M, y), g["headline"], font=fh, fill=INK)
    y += int(fh.size * 1.2)
    addr = ", ".join(x for x in [g["shoot"].address] if x)
    d.text((M, y), addr, font=font("regular", 11), fill=SOFT)
    y += mm(10)
    y = _facts_row(d, M, y, W - 2 * M, g["facts"], acc) + mm(5)
    # three photos under the facts
    rest = imgs[1:4]
    card_h = mm(40)
    card_y = H - card_h
    if rest:
        gap = mm(3)
        th = mm(38)
        tw = (W - 2 * M - gap * (len(rest) - 1)) // len(rest)
        for i, im in enumerate(rest):
            x = M + i * (tw + gap)
            paste_photo(page, im, (x, y, x + tw, y + th), r=mm(2))
        y += th + mm(6)
    # features in two columns, or the listing text
    room = card_y - mm(6) - y
    if g["features"] and room > mm(10):
        f = font("regular", 10)
        step = int(f.size * 1.6)
        rows = max(1, min((len(g["features"]) + 1) // 2, room // step))
        colw = (W - 2 * M) // 2
        for i, feat in enumerate(g["features"][:rows * 2]):
            cx = M + (i // rows) * colw
            cy = y + (i % rows) * step
            d.ellipse((cx, cy + f.size * 0.35, cx + mm(1.6), cy + f.size * 0.35 + mm(1.6)), fill=acc)
            d.text((cx + mm(4), cy), feat[:48], font=f, fill=INK)
    elif g["text"] and room > mm(10):
        f = font("regular", 10)
        text_block(d, (M, y), g["text"], f, W - 2 * M, SOFT, max_lines=max(1, room // int(f.size * 1.4)))
    _agent_card(page, d, (0, card_y, W, H), g)
    return [page]


def window(g) -> List[Image.Image]:
    page = _page()
    d = ImageDraw.Draw(page, "RGBA")
    W, H = page.size
    M = mm(12)
    imgs, acc = g["imgs"], g["accent"]
    hero_h = mm(135)
    paste_photo(page, imgs[0], (0, 0, W, hero_h))
    if g["listing"]:
        tag = g["listing"].upper()
        f = font("bold", 14)
        tw = int(d.textlength(tag, font=f)) + mm(10)
        d.rectangle((M, M, M + tw, M + mm(11)), fill=acc)
        d.text((M + mm(5), M + mm(5.5)), tag, font=f, fill=(255, 255, 255), anchor="lm")
    y = hero_h + mm(10)
    if g["price"]:
        fp = fit_font(d, g["price"], "bold", 54, W - 2 * M)
        d.text((M, y), g["price"], font=fp, fill=acc)
        y += int(fp.size * 1.15)
    fh = fit_font(d, g["headline"], "bold", 28, W - 2 * M)
    d.text((M, y), g["headline"], font=fh, fill=INK)
    y += int(fh.size * 1.3) + mm(4)
    y = _facts_row(d, M, y, W - 2 * M, g["facts"], INK, big=True) + mm(6)
    if len(imgs) > 1 and H - mm(36) - y > mm(30):
        gap = mm(3)
        rest = imgs[1:3]
        th = min(mm(48), H - mm(36) - y - mm(6))
        tw = (W - 2 * M - gap * (len(rest) - 1)) // len(rest)
        for i, im in enumerate(rest):
            x = M + i * (tw + gap)
            paste_photo(page, im, (x, y, x + tw, y + th), r=mm(2))
    _agent_card(page, d, (0, H - mm(36), W, H), g)
    return [page]


def brochure(g) -> List[Image.Image]:
    imgs, acc = g["imgs"], g["accent"]
    pages = []
    # 1: the cover
    p = _page()
    d = ImageDraw.Draw(p, "RGBA")
    W, H = p.size
    M = mm(14)
    paste_photo(p, imgs[0], (0, 0, W, H))
    shade = Image.new("RGBA", (W, mm(110)))
    sd = ImageDraw.Draw(shade)
    for i in range(shade.size[1]):
        sd.line((0, i, W, i), fill=(0, 0, 0, int(200 * (i / shade.size[1]) ** 1.4)))
    p.paste(shade, (0, H - shade.size[1]), shade)
    y = H - mm(62)
    if g["listing"]:
        d.text((M, y), g["listing"].upper(), font=font("medium", 10), fill=(229, 231, 235))
        y += mm(7)
    fh = fit_font(d, g["headline"], "bold", 34, W - 2 * M)
    d.text((M, y), g["headline"], font=fh, fill=(255, 255, 255))
    y += int(fh.size * 1.2)
    d.text((M, y), g["shoot"].address, font=font("regular", 13), fill=(229, 231, 235))
    if g["price"]:
        fp = font("bold", 22)
        d.text((W - M, H - M), g["price"], font=fp, fill=(255, 255, 255), anchor="rb")
    d.rectangle((M, H - M - mm(1.2), M + mm(28), H - M), fill=acc)
    pages.append(p)

    # 2: the words, the facts, the features
    p = _page()
    d = ImageDraw.Draw(p, "RGBA")
    y = M
    d.text((M, y), "About the property", font=font("bold", 22), fill=INK)
    y += mm(14)
    y = _facts_row(d, M, y, W - 2 * M, g["facts"], acc) + mm(8)
    photo_min = mm(70)
    bottom = H - M - (photo_min + mm(8) if len(imgs) > 1 else 0)
    if g["text"]:
        f = font("regular", 11)
        y = text_block(d, (M, y), g["text"], f, W - 2 * M, SOFT, leading=1.5, max_lines=max(1, (bottom - y - mm(40)) // int(f.size * 1.5))) + mm(6)
    if g["features"] and bottom - y > mm(15):
        d.text((M, y), "Features", font=font("bold", 13), fill=INK)
        y += mm(9)
        f = font("regular", 10)
        step = int(f.size * 1.6)
        rows = max(1, min((len(g["features"]) + 1) // 2, (bottom - y) // step))
        colw = (W - 2 * M) // 2
        for i, feat in enumerate(g["features"][:rows * 2]):
            cx, cy = M + (i // rows) * colw, y + (i % rows) * step
            d.ellipse((cx, cy + f.size * 0.35, cx + mm(1.6), cy + f.size * 0.35 + mm(1.6)), fill=acc)
            d.text((cx + mm(4), cy), feat[:52], font=f, fill=INK)
        y += rows * step
    if len(imgs) > 1:
        paste_photo(p, imgs[1], (M, max(y + mm(8), mm(95)), W - M, H - M), r=mm(2))
    pages.append(p)

    # 3: a grid of photos (one wide, four under it)
    p = _page()
    gap = mm(3)
    grid = imgs[2:7]
    if grid:
        wide_h = mm(95)
        paste_photo(p, grid[0], (M, M, W - M, M + wide_h), r=mm(2))
        cells = grid[1:]
        cw = (W - 2 * M - gap) // 2
        ch = (H - 2 * M - wide_h - gap * 2) // 2
        for i, im in enumerate(cells):
            x = M + (i % 2) * (cw + gap)
            yy = M + wide_h + gap + (i // 2) * (ch + gap)
            paste_photo(p, im, (x, yy, x + cw, yy + ch), r=mm(2))
        pages.append(p)

    # 4: the last photos and the agent
    p = _page()
    d = ImageDraw.Draw(p, "RGBA")
    more = imgs[7:11]
    card_h = mm(46)
    if more:
        # as many as there are: one fills the space, two stack, three and four share it
        top, bot = M, H - card_h - mm(8)
        full, half = W - 2 * M, (W - 2 * M - gap) // 2
        rows = 1 if len(more) == 1 else 2
        rh = (bot - top - gap * (rows - 1)) // rows
        boxes = []
        if len(more) == 1:
            boxes = [(M, top, M + full, bot)]
        elif len(more) == 2:
            boxes = [(M, top, M + full, top + rh), (M, top + rh + gap, M + full, bot)]
        else:
            boxes = [(M, top, M + full, top + rh)] if len(more) == 3 else [(M, top, M + half, top + rh), (M + half + gap, top, M + full, top + rh)]
            boxes += [(M, top + rh + gap, M + half, bot), (M + half + gap, top + rh + gap, M + full, bot)]
        for im, bx in zip(more, boxes):
            paste_photo(p, im, bx, r=mm(2))
    else:
        d.text((M, M), g["headline"], font=font("bold", 22), fill=INK)
    _agent_card(p, d, (0, H - card_h, W, H), g)
    pages.append(p)
    return pages


def render(g, kind: str) -> bytes:
    pages = {"flyer": flyer, "brochure": brochure, "window": window}[kind](g)
    buf = io.BytesIO()
    pages[0].save(buf, "PDF", resolution=DPI, save_all=True, append_images=pages[1:], quality=90)
    return buf.getvalue()


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", " ", s or "property").strip().replace(" ", "-")[:60] or "property"


@router.post("/api/shoots/{shoot_id}/print")
def make_print(shoot_id: int, body: PrintBody, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if body.kind not in KINDS:
        raise HTTPException(status_code=400, detail="Choose a flyer, a brochure or a window card.")
    g = gather(db, shoot_id, body)
    pdf = render(g, body.kind)
    name = f"{_slug(g['shoot'].address)} {({'flyer': 'Flyer', 'brochure': 'Brochure', 'window': 'Window card'})[body.kind]}.pdf"
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/api/shoots/{shoot_id}/print/photos")
def print_photos(shoot_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """The photos a print can use: the edited set of the property's folders, the cover first."""
    import listing_pack
    import shoots
    s = db.query(shoots.Shoot).filter(shoots.Shoot.id == shoot_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="No such property")
    paths = [f.path for f in db.query(shoots.ShootFolder).filter(shoots.ShootFolder.shoot_id == s.id).all()]
    rows = listing_pack._photos(db, paths)
    if s.cover_video_id:
        rows.sort(key=lambda v: v.id != s.cover_video_id)
    return {"photos": [{"id": v.id, "name": v.filename, "thumb": v.thumbnail_path} for v in rows[:200]]}


def install(app):
    app.include_router(router)
