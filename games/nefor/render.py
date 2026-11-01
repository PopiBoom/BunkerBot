"""Картинки «Нефоров в Волгограде» (Pillow, на лету): карта города с точками и фишками, итоги партии.

Цветные значки (точки, тусовки, особые клетки) — готовые PNG из assets/nefor/icons: их один раз рисует
tools/static_images.py из эмодзи (шрифта эмодзи на сервере нет). Нет файла — клетка просто без значка.
"""
from __future__ import annotations

import io
import re
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from dashes import plain_dashes

from ..bunker.render import _bold, _mono, _noise, _regular, _wrap
from . import content as C

ICONS = Path(__file__).resolve().parents[2] / "assets" / "nefor" / "icons"
BG_TOP, BG_BOTTOM = (34, 18, 52), (8, 7, 14)
PANEL = (30, 28, 42)
PANEL_HI = (44, 41, 60)
LINE = (62, 58, 82)
TEXT = (244, 242, 236)
MUTED = (156, 152, 172)
ACID = (170, 240, 60)        # кислотный — цвет игры
PINK = (255, 70, 160)
RED = (236, 64, 80)
GOLD, SILVER, BRONZE = (240, 194, 70), (206, 210, 222), (214, 136, 80)
SPECIAL = {"start": ("СТАРТ", ACID), "rumor": ("СЛУХ", (90, 200, 255)), "pzd": ("ПИЗДЮК", RED),
           "pps": ("ППС", (110, 140, 255)), "jail": ("СИЗО", (255, 150, 40))}
_EMOJI = re.compile("[\U0001F000-\U0001FFFF☀-➿️‍⬀-⯿]+")

SIDE = 8                      # клеток на сторону (28 по периметру)
CELL = 160
M = 24                        # поля вокруг поля
BOARD_W = SIDE * CELL + 2 * M


def _plain(text) -> str:
    return re.sub(r"[ \t]{2,}", " ", _EMOJI.sub("", plain_dashes(str(text)))).strip() or "?"


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


@lru_cache(maxsize=None)
def _icon_src(emoji: str) -> Image.Image | None:
    path = ICONS / f"{C.icon_key(emoji)}.png"
    try:
        return Image.open(path).convert("RGBA") if path.exists() else None
    except OSError:
        return None


@lru_cache(maxsize=256)
def _icon(emoji: str, size: int) -> Image.Image | None:
    src = _icon_src(emoji)
    if src is None:
        return None
    k = size / max(src.size)
    return src.resize((max(1, int(src.width * k)), max(1, int(src.height * k))), Image.LANCZOS)


def _paste_icon(img: Image.Image, emoji: str, cx: int, cy: int, size: int) -> bool:
    ic = _icon(emoji, size)
    if ic is None:
        return False
    img.alpha_composite(ic, (int(cx - ic.width / 2), int(cy - ic.height / 2)))
    return True


def _background(w: int, h: int, seed: int) -> Image.Image:
    img = Image.new("RGBA", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        d.line([(0, y), (w, y)], fill=tuple(int(BG_TOP[i] * (1 - t) + BG_BOTTOM[i] * t) for i in range(3)))
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    g.ellipse([w * .1, h * .15, w * .7, h * .7], fill=(*PINK, 50))
    g.ellipse([w * .4, h * .35, w * .95, h * .9], fill=(*ACID, 34))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(min(w, h) // 8)))
    return _noise(img.convert("RGB"), seed).convert("RGBA")


def _glow_text(img: Image.Image, xy, text: str, font, fill, glow, radius: int = 14, anchor: str = "la") -> None:
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text(xy, text, font=font, fill=(*glow, 200), anchor=anchor)
    img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(radius)))
    ImageDraw.Draw(img).text(xy, text, font=font, fill=fill, anchor=anchor)


def _shadow_box(img: Image.Image, box, radius: int, blur: int = 10, alpha: int = 150) -> None:
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    x0, y0, x1, y1 = box
    ImageDraw.Draw(sh).rounded_rectangle([x0 + 3, y0 + 6, x1 + 3, y1 + 6], radius=radius, fill=(0, 0, 0, alpha))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(blur)))


def _tint(img: Image.Image, box, radius: int, color, alpha: int) -> None:
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(box, radius=radius, fill=(*color, alpha))
    img.alpha_composite(layer)


def _token(img: Image.Image, cx: int, cy: int, r: int, color, emoji: str, letter: str) -> None:
    """Фишка игрока: кружок цвета тусовки с белым ободком и значком тусовки."""
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).ellipse([cx - r + 2, cy - r + 5, cx + r + 2, cy + r + 5], fill=(0, 0, 0, 170))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(4)))
    d = ImageDraw.Draw(img)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color, outline=(250, 250, 250), width=3)
    if not _paste_icon(img, emoji, cx, cy, int(r * 1.25)):
        d.text((cx, cy + 1), letter[:1].upper(), font=_bold(int(r * 1.1)), fill=(14, 14, 18), anchor="mm")


def _roof(d: ImageDraw.ImageDraw, x: int, y: int, color) -> None:
    """Значок «крыши»: домик."""
    d.polygon([(x, y + 12), (x + 13, y), (x + 26, y + 12)], fill=color)
    d.rectangle([x + 4, y + 12, x + 22, y + 24], fill=color)
    d.rectangle([x + 10, y + 16, x + 16, y + 24], fill=(20, 18, 28))


def cell_xy(i: int) -> tuple[int, int]:
    """Левый верхний угол клетки i: по часовой стрелке от левого верхнего угла поля."""
    s = SIDE - 1
    if i < s:
        gx, gy = i, 0
    elif i < 2 * s:
        gx, gy = s, i - s
    elif i < 3 * s:
        gx, gy = s - (i - 2 * s), s
    else:
        gx, gy = 0, s - (i - 3 * s)
    return M + gx * CELL, M + gy * CELL


def render_board(players: list[dict], owners: dict[int, int], roofs: dict[int, int], round_no: int,
                 rounds: int, rents: dict[int, int] | None = None) -> bytes:
    """players — [{id, name, color, icon, tribe, cash, capital, pos, out, jail}]; owners — клетка -> id игрока;
    roofs — клетка -> 0..2; rents — клетка -> текущий «вход» (для занятых)."""
    rents = rents or {}
    by_id = {p["id"]: p for p in players}
    img = _background(BOARD_W, BOARD_W, 77 + round_no)
    for i in range(C.SIZE):
        x, y = cell_xy(i)
        box = [x + 5, y + 5, x + CELL - 6, y + CELL - 6]
        kind = C.BOARD[i][0]
        _shadow_box(img, box, 20, blur=6, alpha=120)
        d = ImageDraw.Draw(img)
        if kind == "spot":
            owner = by_id.get(owners.get(i))
            d.rounded_rectangle(box, radius=20, fill=PANEL, outline=LINE, width=2)
            d.rounded_rectangle([box[0] + 2, box[1] + 2, box[2] - 2, box[1] + 40], radius=18, fill=PANEL_HI)
            if owner:
                _tint(img, box, 20, owner["color"], 70)
                d = ImageDraw.Draw(img)
                d.rounded_rectangle(box, radius=20, outline=tuple(owner["color"]), width=6)
            _paste_icon(img, C.cell_icon(i), x + 40, y + 40, 50)
            d = ImageDraw.Draw(img)
            name = C.cell_name(i)
            size = 20  # длинное слово («Красноармейский») не рвём — уменьшаем шрифт
            while size > 10 and max(d.textlength(w_, font=_bold(size)) for w_ in name.split()) > CELL - 28:
                size -= 1
            for k, line in enumerate(_wrap(d, name, _bold(size), CELL - 28, 2)):
                d.text((x + 16, y + 72 + k * (size + 4)), line, font=_bold(size), fill=TEXT)
            label = f"{rents[i]}₽" if owner and i in rents else f"{C.price(i)}₽"  # у занятой — вход, цвет владельца
            col = tuple(owner["color"]) if owner else ACID
            tw = d.textlength(label, font=_mono(17))
            d.rounded_rectangle([x + 14, y + CELL - 42, x + 26 + tw, y + CELL - 16], radius=12, fill=(14, 12, 22))
            d.text((x + 20, y + CELL - 29), label, font=_mono(17), fill=col, anchor="lm")
            for r in range(roofs.get(i, 0)):
                _roof(d, x + CELL - 42 - r * 30, y + CELL - 43, col)
        else:
            label, col = SPECIAL[kind]
            d.rounded_rectangle(box, radius=20, fill=PANEL, outline=col, width=3)
            _tint(img, box, 20, col, 38)
            _paste_icon(img, C.cell_icon(i), x + CELL // 2, y + 64, 74)
            d = ImageDraw.Draw(img)
            font = _bold(24 if len(label) <= 5 else 19)
            d.text((x + CELL // 2, y + 124), label, font=font, fill=col, anchor="mm")
    # фишки
    here: dict[int, list[dict]] = {}
    for p in players:
        if not p.get("out"):
            here.setdefault(p["pos"], []).append(p)
    for cell, ps in here.items():
        x, y = cell_xy(cell)
        for k, p in enumerate(ps[:6]):  # фишки — в правом верхнем углу клетки, поверх значков
            cx = x + CELL - 34 - (k % 3) * 38
            cy = y + 34 + (k // 3) * 40
            _token(img, cx, cy, 20, tuple(p["color"]), p.get("icon", ""), _plain(p["name"]))

    # центр
    x0, y0 = M + CELL + 40, M + CELL + 34
    inner = BOARD_W - 2 * (M + CELL) - 80
    _glow_text(img, (x0, y0), "НЕФОРЫ", _bold(104), TEXT, PINK, radius=18)
    _glow_text(img, (x0 + 6, y0 + 118), "В ВОЛГОГРАДЕ", _bold(46), ACID, ACID, radius=12)
    d = ImageDraw.Draw(img)
    chip = f"КРУГ {min(round_no, rounds)} / {rounds}"
    cw = d.textlength(chip, font=_mono(26))
    d.rounded_rectangle([x0 + inner - cw - 40, y0 + 14, x0 + inner, y0 + 64], radius=25, outline=ACID, width=3)
    d.text((x0 + inner - cw / 2 - 20, y0 + 39), chip, font=_mono(26), fill=ACID, anchor="mm")
    y = y0 + 200
    order = sorted(players, key=lambda p: (p.get("out", False), -p["capital"]))
    row_h = min(96, (BOARD_W - M - CELL - 30 - y) // max(1, len(order)))
    for p in order:
        box = [x0, y, x0 + inner, y + row_h - 12]
        _shadow_box(img, box, 18, blur=8, alpha=110)
        d = ImageDraw.Draw(img)
        dim = p.get("out")
        d.rounded_rectangle(box, radius=18, fill=PANEL, outline=LINE, width=2)
        d.rounded_rectangle([x0, y, x0 + 10, y + row_h - 12], radius=5, fill=tuple(p["color"]))
        mid = y + (row_h - 12) // 2
        _token(img, x0 + 50, mid, 24, tuple(p["color"]) if not dim else (80, 80, 90), p.get("icon", ""),
               _plain(p["name"]))
        d = ImageDraw.Draw(img)
        right = "БАНКРОТ" if dim else f"{p['capital']}₽"
        d.text((x0 + inner - 22, mid - 12), right, font=_bold(32), fill=RED if dim else TEXT, anchor="rm")
        rw = d.textlength(right, font=_bold(32))
        nw = inner - 120 - rw - 40
        d.text((x0 + 88, mid - 14), _wrap(d, _plain(p["name"]), _bold(28), nw, 1)[0], font=_bold(28),
               fill=MUTED if dim else TEXT, anchor="lm")
        sub = _plain(p.get("tribe", "")) + (" · В СИЗО" if p.get("jail") and not dim else "")
        d.text((x0 + 88, mid + 18), sub, font=_regular(20), fill=(255, 150, 40) if p.get("jail") else MUTED,
               anchor="lm")
        if not dim:
            d.text((x0 + inner - 22, mid + 20), f"наличными {p['cash']}₽", font=_regular(19), fill=MUTED, anchor="rm")
        y += row_h
    # памятка по особым клеткам — внизу центра
    d = ImageDraw.Draw(img)
    legend = [("start", "стипуха за круг"), ("rumor", "случайное событие"), ("pps", "штраф"),
              ("jail", f"пропуск {C.JAIL_TURNS} ходов"), ("pzd", "подкол и штраф")]
    ly = BOARD_W - M - CELL - 40 - 34 * len(legend)
    if ly > y + 10:
        for kind, what in legend:
            label, col = SPECIAL[kind]
            icon = next(C.cell_icon(i) for i in range(C.SIZE) if C.BOARD[i][0] == kind)
            _paste_icon(img, icon, x0 + 16, ly + 14, 26)
            d = ImageDraw.Draw(img)
            d.text((x0 + 42, ly + 14), label, font=_bold(20), fill=col, anchor="lm")
            d.text((x0 + 42 + d.textlength(label + "  ", font=_bold(20)), ly + 14), f"- {what}", font=_regular(20),
                   fill=MUTED, anchor="lm")
            ly += 34
    return _png(img)


def render_results(rows: list[dict], serial: int, date: str) -> bytes:
    """Итоги: rows — [{name, tribe, color, icon, capital, spots, out}] по местам (первый — победитель)."""
    w, pad = 1080, 60
    win_h, row_h, gap = 230, 112, 16
    h = 270 + win_h + 30 + max(0, len(rows) - 1) * (row_h + gap) + 90
    img = _background(w, h, 300 + serial)
    d = ImageDraw.Draw(img)
    for x in range(0, w, 44):  # кислотная «изолента» сверху и снизу
        col = ACID if (x // 44) % 2 == 0 else (16, 16, 20)
        d.rectangle([x, 0, x + 44, 14], fill=col)
        d.rectangle([x, h - 14, x + 44, h], fill=col)
    d.text((pad, 50), f"ИТОГИ · ПАРТИЯ № {serial:04d}", font=_mono(26), fill=ACID)
    d.text((w - pad, 50), date, font=_mono(26), fill=MUTED, anchor="ra")
    _glow_text(img, (pad - 4, 88), "НЕФОРЫ", _bold(92), TEXT, PINK, radius=16)
    _glow_text(img, (pad, 192), "В ВОЛГОГРАДЕ", _bold(42), ACID, ACID, radius=10)
    top = max((r["capital"] for r in rows), default=1) or 1
    y = 270
    if rows:  # победитель — большая карточка
        r = rows[0]
        col = tuple(r["color"])
        box = [pad, y, w - pad, y + win_h]
        _shadow_box(img, box, 26, blur=14, alpha=170)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle(box, radius=26, fill=PANEL, outline=GOLD, width=4)
        _tint(img, box, 26, col, 40)
        _token(img, pad + 110, y + win_h // 2 + 10, 66, col, r.get("icon", ""), _plain(r["name"]))
        _paste_icon(img, "👑", pad + 110, y + 42, 64)
        d = ImageDraw.Draw(img)
        tx = pad + 210
        d.text((tx, y + 36), "ПОБЕДИТЕЛЬ", font=_mono(26), fill=GOLD)
        d.text((tx, y + 70), _wrap(d, _plain(r["name"]), _bold(54), w - pad - tx - 30, 1)[0], font=_bold(54),
               fill=TEXT)
        d.text((tx, y + 140), f"{_plain(r['tribe'])} · точек: {r['spots']}", font=_regular(26), fill=MUTED)
        d.text((w - pad - 30, y + 150), f"{r['capital']}₽", font=_bold(48), fill=GOLD, anchor="ra")
        y += win_h + 30
    medal = {1: SILVER, 2: BRONZE}
    for i, r in enumerate(rows[1:], start=1):
        col = tuple(r["color"])
        box = [pad, y, w - pad, y + row_h]
        _shadow_box(img, box, 20, blur=10, alpha=140)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle(box, radius=20, fill=PANEL, outline=medal.get(i, LINE), width=3 if i in medal else 2)
        d.text((pad + 34, y + row_h // 2), str(i + 1), font=_bold(40), fill=medal.get(i, MUTED), anchor="mm")
        _token(img, pad + 104, y + row_h // 2, 32, col if not r.get("out") else (80, 80, 90), r.get("icon", ""),
               _plain(r["name"]))
        d = ImageDraw.Draw(img)
        nx, right = pad + 156, w - pad - 28
        cap = "БАНКРОТ" if r.get("out") else f"{r['capital']}₽"
        d.text((right, y + 36), cap, font=_bold(36), fill=RED if r.get("out") else TEXT, anchor="rm")
        nw = right - d.textlength(cap, font=_bold(36)) - 30 - nx
        d.text((nx, y + 34), _wrap(d, _plain(r["name"]), _bold(34), nw, 1)[0], font=_bold(34), fill=TEXT, anchor="lm")
        d.text((nx, y + 62), f"{_plain(r['tribe'])} · точек: {r['spots']}", font=_regular(22), fill=MUTED)
        frac = 0 if r.get("out") else r["capital"] / top
        d.rounded_rectangle([nx, y + 93, right, y + 100], radius=4, fill=(48, 46, 62))
        d.rounded_rectangle([nx, y + 93, nx + max(10, int((right - nx) * frac)), y + 100], radius=4, fill=col)
        y += row_h + gap
    d.text((pad, h - 62), "КАПИТАЛ = НАЛИЧНЫЕ + ТОЧКИ + КРЫШИ", font=_mono(22), fill=MUTED)
    return _png(img)
