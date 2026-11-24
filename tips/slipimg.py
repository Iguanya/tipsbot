"""Renders slip / result cards (Pillow). Result cards are only drawn from settled data in the database."""
from datetime import datetime
from functools import lru_cache
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

import config as cfg
from .model import pick_text

W = 1080
BG, HEAD, ROW_A, ROW_B = (12, 16, 28), (22, 31, 54), (18, 24, 41), (15, 20, 35)
WHITE, GREY, GREEN, RED, AMBER, BLUE = (255, 255, 255), (140, 150, 170), (46, 204, 113), (231, 76, 60), (241, 196, 15), (88, 166, 255)
STATUS_COLOR = {"won": GREEN, "lost": RED, "void": AMBER}

_FILES = {
    True: ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf", "arialbd.ttf",
           "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"],
    False: ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans.ttf", "arial.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf", "C:/Windows/Fonts/arial.ttf"],
}


@lru_cache(maxsize=None)
def _font(size, bold=True):
    for p in _FILES[bold]:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _fit(d, text, font, maxw):
    if d.textlength(text, font=font) <= maxw:
        return text
    while text and d.textlength(text + "…", font=font) > maxw:
        text = text[:-1]
    return text + "…"


def _pill(d, x_right, y, text, color, size=30):
    f = _font(size)
    w = int(d.textlength(text, font=f)) + 36
    d.rounded_rectangle([x_right - w, y, x_right, y + size + 22], radius=14, fill=color)
    d.text((x_right - w + 18, y + 8), text, font=f, fill=(10, 14, 24))


def _title(slip):
    base = "VIP SLIP" if slip["tier"] == "vip" else "FREE TIP"
    if slip["kind"] == "recovery":
        base = "RECOVERY PICK"
    return f"{base} · REF {slip['ref']}"


def render(slip, legs, brand, result=None) -> bytes:
    head, row_h, foot = 190, 170, 270
    img = Image.new("RGB", (W, head + row_h * len(legs) + foot), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, head], fill=HEAD)
    d.text((48, 34), brand.upper(), font=_font(44), fill=WHITE)
    d.text((48, 102), _title(slip), font=_font(36), fill=BLUE)
    date = datetime.now(cfg.TZ).strftime("%a %d %b %Y")
    d.text((W - 48 - d.textlength(date, font=_font(30, False)), 44), date, font=_font(30, False), fill=GREY)
    if result:
        _pill(d, W - 48, 96, result.upper(), STATUS_COLOR.get(result, GREY), 36)

    y = head
    for i, l in enumerate(legs):
        d.rectangle([0, y, W, y + row_h], fill=ROW_A if i % 2 == 0 else ROW_B)
        ko = datetime.fromtimestamp(l["commence_at"], cfg.TZ).strftime("%a %d %b · %H:%M")
        d.text((48, y + 16), _fit(d, f"{l['sport_title']} · {ko} EAT", _font(26, False), W - 400), font=_font(26, False), fill=GREY)
        d.text((48, y + 58), _fit(d, f"{l['home']} vs {l['away']}", _font(38), W - 96 - 170), font=_font(38), fill=WHITE)
        d.text((48, y + 112), _fit(d, pick_text(l), _font(34), W - 96 - 260), font=_font(34), fill=GREEN)
        odds = f"{l['odds']:.2f}"
        d.text((W - 48 - d.textlength(odds, font=_font(48)), y + 100), odds, font=_font(48), fill=WHITE)
        if result and l["status"] in STATUS_COLOR:
            _pill(d, W - 48, y + 12, l["status"].upper(), STATUS_COLOR[l["status"]], 22)
        y += row_h

    total = (slip["result_odds"] if result == "won" and slip["result_odds"] else slip["total_odds"]) or 0
    d.rectangle([0, y, W, y + 120], fill=HEAD)
    d.text((48, y + 34), "TOTAL ODDS", font=_font(34), fill=GREY)
    t = f"{total:.2f}"
    d.text((W - 48 - d.textlength(t, font=_font(60)), y + 24), t, font=_font(60), fill=GREEN if result != "lost" else WHITE)
    first = ("Result confirmed from official scores." if result
             else "Odds move. Only take a selection at the 'min odds' shown or better.")
    note = [first, "18+ | Bet responsibly | Never stake more than you can afford to lose."]
    for k, line in enumerate(note):
        d.text((48, y + 150 + k * 42), line, font=_font(26, False), fill=GREY)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()