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

COLS, ROWS = 11, 5             # поле — вытянутое кольцо: 11 × 5 клеток, 28 по периметру
CELL = 140
PAD_X = 40                    # поля слева и справа
HEAD = 196                    # шапка: название, круг, силуэт города
FOOT = 92                     # подвал: памятка по особым клеткам
IMG_W = COLS * CELL + 2 * PAD_X
IMG_H = HEAD + ROWS * CELL + FOOT + 28


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
    w, h = COLS - 1, ROWS - 1
    if i <= w:
        gx, gy = i, 0
    elif i <= w + h:
        gx, gy = w, i - w
    elif i <= 2 * w + h:
        gx, gy = w - (i - w - h), h
    else:
        gx, gy = 0, h - (i - 2 * w - h)
    return PAD_X + gx * CELL, HEAD + gy * CELL


def _skyline(img: Image.Image, base: int, seed: int) -> None:
    """Силуэт города по низу шапки: дома с окнами, мост с вантами, Волга."""
    import random as _r

    rnd = _r.Random(seed)
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    x = 0
    while x < IMG_W:
        bw, bh = rnd.randint(40, 110), rnd.randint(30, 96)
        if 0.6 * IMG_W < x < 0.84 * IMG_W:  # место для моста
            x += bw
            continue
        d.rectangle([x, base - bh, x + bw - 6, base], fill=(22, 14, 36, 235))
        for wy in range(base - bh + 10, base - 8, 16):
            for wx in range(x + 8, x + bw - 14, 14):
                if rnd.random() < 0.28:
                    d.rectangle([wx, wy, wx + 5, wy + 7], fill=(*rnd.choice([ACID, PINK, GOLD]), 190))
        x += bw
    bx0, bx1 = int(0.6 * IMG_W), int(0.84 * IMG_W)
    d.rectangle([bx0, base - 26, bx1, base - 20], fill=(30, 20, 48, 255))  # полотно моста
    for px in (bx0 + (bx1 - bx0) // 3, bx0 + 2 * (bx1 - bx0) // 3):
        d.rectangle([px - 4, base - 92, px + 4, base - 20], fill=(30, 20, 48, 255))
        for k in range(1, 6):  # ванты
            d.line([(px, base - 90), (px - k * 22, base - 24)], fill=(*PINK, 120), width=2)
            d.line([(px, base - 90), (px + k * 22, base - 24)], fill=(*PINK, 120), width=2)
    for k in range(3):  # Волга
        d.line([(bx0 - 30, base - 8 + k * 3), (bx1 + 30, base - 8 + k * 3)], fill=(70, 120, 220, 120 - k * 30),
               width=2)
    img.alpha_composite(layer)


def _cell(img: Image.Image, i: int, owner: dict | None, roofs: int, rent: int | None) -> None:
    x, y = cell_xy(i)
    box = [x + 4, y + 4, x + CELL - 5, y + CELL - 5]
    kind = C.BOARD[i][0]
    _shadow_box(img, box, 18, blur=6, alpha=120)
    d = ImageDraw.Draw(img)
    if kind != "spot":
        label, col = SPECIAL[kind]
        d.rounded_rectangle(box, radius=18, fill=PANEL, outline=col, width=3)
        _tint(img, box, 18, col, 40)
        _paste_icon(img, C.cell_icon(i), x + CELL // 2, y + 58, 66)
        ImageDraw.Draw(img).text((x + CELL // 2, y + 110), label, font=_bold(22 if len(label) <= 5 else 17),
                                 fill=col, anchor="mm")
        return
    d.rounded_rectangle(box, radius=18, fill=PANEL, outline=LINE, width=2)
    d.rounded_rectangle([box[0] + 2, box[1] + 2, box[2] - 2, box[1] + 34], radius=16, fill=PANEL_HI)
    if owner:
        _tint(img, box, 18, owner["color"], 70)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle(box, radius=18, outline=tuple(owner["color"]), width=5)
    _paste_icon(img, C.cell_icon(i), x + 34, y + 34, 42)
    d = ImageDraw.Draw(img)
    name = C.cell_name(i)
    size = 18  # длинное слово («Красноармейский») не рвём — уменьшаем шрифт
    while size > 9 and max(d.textlength(w_, font=_bold(size)) for w_ in name.split()) > CELL - 24:
        size -= 1
    for k, line in enumerate(_wrap(d, name, _bold(size), CELL - 24, 2)):
        d.text((x + 13, y + 62 + k * (size + 3)), line, font=_bold(size), fill=TEXT)
    label = f"{rent}₽" if owner and rent is not None else f"{C.price(i)}₽"  # у занятой — вход, цвет владельца
    col = tuple(owner["color"]) if owner else ACID
    tw = d.textlength(label, font=_mono(15))
    d.rounded_rectangle([x + 12, y + CELL - 36, x + 22 + tw, y + CELL - 14], radius=11, fill=(14, 12, 22))
    d.text((x + 17, y + CELL - 25), label, font=_mono(15), fill=col, anchor="lm")
    for r in range(roofs):
        _roof(d, x + CELL - 38 - r * 29, y + CELL - 40, col)


def render_board(players: list[dict], owners: dict[int, int], roofs: dict[int, int], round_no: int,
                 rounds: int, rents: dict[int, int] | None = None) -> bytes:
    """players — [{id, name, color, icon, tribe, cash, capital, pos, out, jail}]; owners — клетка -> id игрока;
    roofs — клетка -> 0..2; rents — клетка -> текущий «вход» (для занятых)."""
    rents = rents or {}
    by_id = {p["id"]: p for p in players}
    img = _background(IMG_W, IMG_H, 77 + round_no)
    d = ImageDraw.Draw(img)
    for x in range(0, IMG_W, 44):  # кислотная «изолента» сверху и снизу
        col = ACID if (x // 44) % 2 == 0 else (16, 16, 20)
        d.rectangle([x, 0, x + 44, 12], fill=col)
        d.rectangle([x, IMG_H - 12, x + 44, IMG_H], fill=col)
    # шапка
    _skyline(img, HEAD - 14, 5)
    _glow_text(img, (PAD_X, 30), "НЕФОРЫ", _bold(88), TEXT, PINK, radius=16)
    w1 = ImageDraw.Draw(img).textlength("НЕФОРЫ", font=_bold(88))
    _glow_text(img, (PAD_X + w1 + 24, 72), "В ВОЛГОГРАДЕ", _bold(44), ACID, ACID, radius=10)
    d = ImageDraw.Draw(img)
    chip = f"КРУГ {min(round_no, rounds)} / {rounds}"
    cw = d.textlength(chip, font=_mono(30))
    d.rounded_rectangle([IMG_W - PAD_X - cw - 48, 40, IMG_W - PAD_X, 96], radius=28, fill=(14, 12, 22),
                        outline=ACID, width=3)
    d.text((IMG_W - PAD_X - cw / 2 - 24, 68), chip, font=_mono(30), fill=ACID, anchor="mm")
    # клетки и фишки
    for i in range(C.SIZE):
        _cell(img, i, by_id.get(owners.get(i)), roofs.get(i, 0), rents.get(i))
    here: dict[int, list[dict]] = {}
    for p in players:
        if not p.get("out"):
            here.setdefault(p["pos"], []).append(p)
    for cell, ps in here.items():
        x, y = cell_xy(cell)
        for k, p in enumerate(ps[:6]):  # фишки — в правом верхнем углу клетки, поверх значков
            _token(img, x + CELL - 28 - (k % 2) * 34, y + 28 + (k // 2) * 34, 17, tuple(p["color"]),
                   p.get("icon", ""), _plain(p["name"]))
    # центр: игроки карточками в две колонки
    cx0, cy0 = PAD_X + CELL + 26, HEAD + CELL + 18
    cw_all, ch_all = (COLS - 2) * CELL - 52, (ROWS - 2) * CELL - 36
    order = sorted(players, key=lambda p: (p.get("out", False), -p["capital"]))
    cols = 1 if len(order) <= 3 else 2
    per_col = -(-len(order) // cols)
    gap = 18
    card_w = (cw_all - gap * (cols - 1)) // cols
    card_h = min(118, (ch_all - gap * (per_col - 1)) // max(1, per_col))
    for n, p in enumerate(order):
        col_i, row_i = n // per_col, n % per_col
        x = cx0 + col_i * (card_w + gap)
        y = cy0 + row_i * (card_h + gap) + (ch_all - per_col * card_h - (per_col - 1) * gap) // 2
        box = [x, y, x + card_w, y + card_h]
        _shadow_box(img, box, 18, blur=8, alpha=120)
        d = ImageDraw.Draw(img)
        dim = p.get("out")
        d.rounded_rectangle(box, radius=18, fill=PANEL, outline=LINE, width=2)
        d.rounded_rectangle([x, y, x + 10, y + card_h], radius=5, fill=tuple(p["color"]))
        d.text((x + 30, y + card_h // 2), str(n + 1), font=_bold(30), fill=GOLD if n == 0 else MUTED, anchor="mm")
        mid = y + card_h // 2
        _token(img, x + 84, mid, min(32, card_h // 2 - 10), tuple(p["color"]) if not dim else (80, 80, 90),
               p.get("icon", ""), _plain(p["name"]))
        d = ImageDraw.Draw(img)
        right = "БАНКРОТ" if dim else f"{p['capital']}₽"
        d.text((x + card_w - 22, mid - 12), right, font=_bold(32), fill=RED if dim else TEXT, anchor="rm")
        nw = card_w - 140 - d.textlength(right, font=_bold(32)) - 40
        d.text((x + 128, mid - 14), _wrap(d, _plain(p["name"]), _bold(28), nw, 1)[0], font=_bold(28),
               fill=MUTED if dim else TEXT, anchor="lm")
        jail = p.get("jail") and not dim
        d.text((x + 128, mid + 18), _plain(p.get("tribe", "")) + (" · В СИЗО" if jail else ""), font=_regular(20),
               fill=(255, 150, 40) if jail else MUTED, anchor="lm")
        if not dim:
            d.text((x + card_w - 22, mid + 20), f"наличными {p['cash']}₽", font=_regular(18), fill=MUTED, anchor="rm")
    # подвал: памятка по особым клеткам
    legend = [("start", "стипуха за круг"), ("rumor", "событие"), ("pps", "штраф"),
              ("jail", f"пропуск {C.JAIL_TURNS} ходов"), ("pzd", "подкол и штраф")]
    items = []
    d = ImageDraw.Draw(img)
    for kind, what in legend:
        label, col = SPECIAL[kind]
        wl = d.textlength(label, font=_bold(20)) + d.textlength(f" - {what}", font=_regular(20)) + 40
        items.append((kind, label, col, what, wl))
    total = sum(it[4] for it in items) + 26 * (len(items) - 1)
    lx, ly = (IMG_W - total) / 2, HEAD + ROWS * CELL + FOOT // 2 + 4
    for kind, label, col, what, wl in items:
        icon = next(C.cell_icon(i) for i in range(C.SIZE) if C.BOARD[i][0] == kind)
        _paste_icon(img, icon, int(lx + 14), ly, 28)
        d = ImageDraw.Draw(img)
        d.text((lx + 34, ly), label, font=_bold(20), fill=col, anchor="lm")
        d.text((lx + 34 + d.textlength(label, font=_bold(20)), ly), f" - {what}", font=_regular(20), fill=MUTED,
               anchor="lm")
        lx += wl + 26
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
