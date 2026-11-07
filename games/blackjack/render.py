"""Картинки блэкджека (Pillow, без ИИ): лобби «стол открыт» и стол раунда — сукно, карты, фишки, итоги.

Карта — (ранг, масть), закрытая — None. Игроки — до 6, в две колонки; текущий подсвечен золотом.
"""
from __future__ import annotations

import io
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter

from games.bunker.render import _bold, _font, _mono

FELT, FELT2 = (22, 92, 58), (14, 66, 40)
GOLD = (232, 188, 84)
RED = (205, 40, 50)
WIN, LOSE = (120, 230, 120), (255, 110, 110)
W = 900
CW, CH = 96, 134


def _felt(w: int, h: int) -> Image.Image:
    im = Image.new("RGB", (w, h), FELT)
    d = ImageDraw.Draw(im)
    big = max(w, h)
    for r in range(big, 0, -8):  # мягкая виньетка к краям
        t = r / big
        c = tuple(int(FELT2[i] + (FELT[i] - FELT2[i]) * (1 - t)) for i in range(3))
        d.ellipse((w / 2 - r, h * 0.35 - r * 0.7, w / 2 + r, h * 0.35 + r * 0.7), fill=c)
    return im.convert("RGBA")


@lru_cache(maxsize=64)
def _card(rank: str | None, suit: str | None) -> Image.Image:
    im = Image.new("RGBA", (CW + 8, CH + 8), (0, 0, 0, 0))
    sh = Image.new("RGBA", im.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle((6, 6, CW + 4, CH + 4), 10, fill=(0, 0, 0, 110))
    im.alpha_composite(sh.filter(ImageFilter.GaussianBlur(3)))
    d = ImageDraw.Draw(im)
    if rank is None:  # рубашка
        d.rounded_rectangle((2, 2, CW, CH), 10, fill=(150, 30, 40))
        for i in range(-CH, CW, 12):
            d.line((i + 8, 8, i + 8 + CH, 8 + CH), fill=(175, 50, 60), width=3)
        d.rounded_rectangle((2, 2, CW, CH), 10, outline=(255, 255, 255), width=4)
        d.text((CW / 2 + 1, CH / 2 + 1), "П", font=_bold(44), fill=(255, 230, 200), anchor="mm")
        return im
    d.rounded_rectangle((2, 2, CW, CH), 10, fill=(250, 248, 242), outline=(200, 200, 200), width=2)
    col = RED if suit in "♥♦" else (25, 25, 30)
    d.text((10, 8), rank, font=_bold(26 if rank == "10" else 30), fill=col)
    d.text((13, 42), suit, font=_font("DejaVuSans.ttf", 24), fill=col)
    d.text((CW / 2 + 6, CH / 2 + 14), suit, font=_font("DejaVuSans.ttf", 54), fill=col, anchor="mm")
    return im


def _hand(im: Image.Image, x: int, y: int, cards: list, max_w: int) -> None:
    step = 46 if len(cards) < 2 else min(52, (max_w - CW) // (len(cards) - 1))  # «10» не закрывается соседней
    for i, c in enumerate(cards):
        im.alpha_composite(_card(*(c or (None, None))), (x + i * step, y))


def _chip(d: ImageDraw.ImageDraw, x: int, y: int, amount: int) -> None:
    d.ellipse((x - 22, y - 22, x + 22, y + 22), fill=(230, 60, 60), outline=(255, 255, 255), width=4)
    d.ellipse((x - 13, y - 13, x + 13, y + 13), outline=(255, 255, 255), width=2)
    d.text((x + 32, y), str(amount), font=_bold(24), fill=(255, 255, 255), anchor="lm")


def _png(im: Image.Image) -> bytes:
    out = io.BytesIO()
    im.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()


def render_lobby(players: int, bet: int) -> bytes:
    im = _felt(W, 420)
    d = ImageDraw.Draw(im)
    d.text((W / 2, 80), "БЛЭКДЖЕК", font=_bold(84), fill=(255, 255, 255), anchor="mm")
    d.text((W / 2, 150), f"СТОЛ ОТКРЫТ · СТАВКА {bet} · ДИЛЕР ПИЗДЮК", font=_mono(24), fill=GOLD, anchor="mm")
    for i, c in enumerate([("A", "♠"), ("K", "♥"), None]):
        im.alpha_composite(_card(*(c or (None, None))), (W // 2 - 120 + i * 72, 200))
    d.text((W / 2, 385), f"ЗА СТОЛОМ: {players}", font=_mono(22), fill=(220, 230, 220), anchor="mm")
    return _png(im)


def render_table(title: str, dealer: list, dealer_total: str, players: list[dict], turn: int | None = None) -> bytes:
    """players — [{"name", "cards", "total", "bet", "result": ("+100", True/False/None) | None}]."""
    rows = max(1, (len(players) + 1) // 2)
    h = 300 + 190 * rows + 20
    im = _felt(W, h)
    d = ImageDraw.Draw(im)
    d.text((W / 2, 34), title, font=_mono(24), fill=GOLD, anchor="mm")
    d.text((W / 2, 70), "ДИЛЕР · ПИЗДЮК", font=_bold(26), fill=(255, 255, 255), anchor="mm")
    _hand(im, W // 2 - 70, 92, dealer, 400)
    d.text((W / 2, 252), dealer_total, font=_bold(26), fill=(255, 240, 200), anchor="mm")
    d.line((60, 286, W - 60, 286), fill=(255, 255, 255), width=1)
    for i, p in enumerate(players):
        col, row = i % 2, i // 2
        x0, y0 = 40 + col * (W // 2), 304 + row * 190
        box = (x0, y0, x0 + W // 2 - 60, y0 + 172)
        if turn == i:
            ov = Image.new("RGBA", im.size, (0, 0, 0, 0))
            ImageDraw.Draw(ov).rounded_rectangle(box, 18, fill=(255, 255, 255, 30))
            im.alpha_composite(ov)
            d = ImageDraw.Draw(im)
            d.rounded_rectangle(box, 18, outline=GOLD, width=5)
        else:
            d.rounded_rectangle(box, 18, outline=(190, 210, 195), width=2)
        name = p["name"]
        if d.textlength(name, font=_bold(26)) > 250:
            while len(name) > 2 and d.textlength(name + "…", font=_bold(26)) > 250:
                name = name[:-1]
            name += "…"
        d.text((x0 + 18, y0 + 14), name, font=_bold(26), fill=(255, 255, 255))
        d.text((x0 + W // 2 - 80, y0 + 16), p["total"], font=_bold(26), fill=(255, 240, 200), anchor="ra")
        _hand(im, x0 + 16, y0 + 50, p["cards"], 230)
        _chip(d, x0 + 290, y0 + 86, p["bet"])
        if turn == i:
            d.text((x0 + 270, y0 + 136), "ХОДИТ", font=_bold(22), fill=GOLD)
        elif p.get("result"):
            text, good = p["result"]
            d.text((x0 + 262, y0 + 136), text, font=_bold(24),
                   fill=WIN if good else LOSE if good is False else (230, 230, 230))
    return _png(im)
