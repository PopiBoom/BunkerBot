"""Картинки «Нефоров в Волгограде» (Pillow, на лету): карта города с точками и фишками, итоги партии.

Эмодзи в шрифте нет — всё рисуется фигурами и текстом (DejaVu), имена чистятся от эмодзи.
"""
from __future__ import annotations

import io
import re

from PIL import Image, ImageDraw

from dashes import plain_dashes

from ..bunker.render import LINE, MUTED, PANEL, TEXT, _bold, _mono, _noise, _regular, _wrap
from . import content as C

BG = (16, 14, 22)
ACID = (170, 240, 60)        # кислотный — цвет игры
GOLD, SILVER, BRONZE = (232, 186, 64), (196, 200, 210), (205, 127, 72)
_EMOJI = re.compile("[\U0001F000-\U0001FFFF☀-➿️‍⬀-⯿]+")

SIDE = 6                      # клеток на сторону (20 по периметру)
CELL = 180
BOARD_W = SIDE * CELL
SPECIAL = {"start": ("СТАРТ", "+стипуха", ACID), "rumor": ("?", "слух", (90, 200, 255)),
           "pzd": ("П", "Пиздюк", (226, 54, 70)), "pps": ("ППС", "штраф", (120, 140, 255))}


def _plain(text) -> str:
    return re.sub(r"[ \t]{2,}", " ", _EMOJI.sub("", plain_dashes(str(text)))).strip() or "?"


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def cell_xy(i: int) -> tuple[int, int]:
    """Левый верхний угол клетки i: по часовой стрелке от левого верхнего угла поля."""
    if i < SIDE:
        gx, gy = i, 0
    elif i < 2 * SIDE - 1:
        gx, gy = SIDE - 1, i - SIDE + 1
    elif i < 3 * SIDE - 2:
        gx, gy = SIDE - 1 - (i - 2 * SIDE + 2), SIDE - 1
    else:
        gx, gy = 0, SIDE - 1 - (i - 3 * SIDE + 3)
    return gx * CELL, gy * CELL


def _dot(d: ImageDraw.ImageDraw, cx: int, cy: int, r: int, color, letter: str = "") -> None:
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color, outline=(10, 10, 14), width=3)
    if letter:
        d.text((cx, cy + 1), letter[:1].upper(), font=_bold(int(r * 1.1)), fill=(14, 14, 18), anchor="mm")


def render_board(players: list[dict], owners: dict[int, int], roofs: dict[int, int], round_no: int,
                 rounds: int) -> bytes:
    """players — [{id, name, color, cash, capital, pos, out}]; owners — клетка -> id игрока; roofs — клетка -> 0..2."""
    color_of = {p["id"]: tuple(p["color"]) for p in players}
    img = _noise(Image.new("RGB", (BOARD_W, BOARD_W), BG), 77 + round_no)
    d = ImageDraw.Draw(img)
    for i in range(C.SIZE):
        x, y = cell_xy(i)
        box = [x + 4, y + 4, x + CELL - 5, y + CELL - 5]
        kind = C.BOARD[i][0]
        owner = owners.get(i)
        d.rounded_rectangle(box, radius=14, fill=PANEL, outline=color_of.get(owner, LINE),
                            width=7 if owner in color_of else 2)
        if kind == "spot":
            dcol = C.DISTRICTS[C.district(i)][1]
            d.rounded_rectangle([x + 12, y + 12, x + CELL - 13, y + 26], radius=6, fill=dcol)
            size = 22  # длинное слово («Красноармейский») не рвём — уменьшаем шрифт
            while size > 14 and max(d.textlength(w_, font=_bold(size)) for w_ in C.cell_name(i).split()) > CELL - 30:
                size -= 1
            for k, line in enumerate(_wrap(d, C.cell_name(i), _bold(size), CELL - 30, 2)):
                d.text((x + 15, y + 34 + k * (size + 5)), line, font=_bold(size), fill=TEXT)
            d.text((x + 15, y + CELL - 38), f"{C.price(i)}₽", font=_mono(19), fill=MUTED)
            for r in range(roofs.get(i, 0)):  # крыши — треугольники справа внизу
                rx = x + CELL - 34 - r * 30
                d.polygon([(rx, y + CELL - 20), (rx + 24, y + CELL - 20), (rx + 12, y + CELL - 40)],
                          fill=color_of.get(owner, TEXT))
        else:
            big, small, col = SPECIAL[kind]
            d.text((x + CELL // 2, y + 70), big, font=_bold(46 if len(big) > 1 else 64), fill=col, anchor="mm")
            d.text((x + CELL // 2, y + 122), small, font=_mono(19), fill=MUTED, anchor="mm")
    # фишки игроков
    here: dict[int, list[dict]] = {}
    for p in players:
        if not p.get("out"):
            here.setdefault(p["pos"], []).append(p)
    for cell, ps in here.items():
        x, y = cell_xy(cell)
        for k, p in enumerate(ps[:6]):
            cx = x + 40 + (k % 3) * 42
            cy = y + CELL - 62 - (k // 3) * 40 if C.is_spot(cell) else y + CELL - 32 - (k // 3) * 40
            _dot(d, cx, cy, 17, p["color"], _plain(p["name"]))
    # центр: название, круг, таблица
    cx0, cy0 = CELL + 30, CELL + 30
    inner = BOARD_W - 2 * CELL - 60
    d.text((cx0, cy0), "НЕФОРЫ", font=_bold(76), fill=TEXT)
    d.text((cx0 + 4, cy0 + 86), "В ВОЛГОГРАДЕ", font=_bold(40), fill=ACID)
    d.text((cx0 + 4, cy0 + 146), f"КРУГ {min(round_no, rounds)} ИЗ {rounds}", font=_mono(26), fill=MUTED)
    y = cy0 + 200
    alive = sorted(players, key=lambda p: (p.get("out", False), -p["capital"]))
    row_h = min(64, (BOARD_W - CELL - 40 - y) // max(1, len(alive)))
    for p in alive:
        _dot(d, cx0 + 20, y + row_h // 2, 16, p["color"])
        name = _wrap(d, _plain(p["name"]), _bold(28), inner - 290, 1)[0]
        d.text((cx0 + 50, y + row_h // 2), name, font=_bold(28), fill=MUTED if p.get("out") else TEXT, anchor="lm")
        right = "БАНКРОТ" if p.get("out") else f"{p['cash']}₽ · {p['capital']}₽"
        d.text((cx0 + inner, y + row_h // 2), right, font=_mono(24), fill=(226, 54, 70) if p.get("out") else TEXT,
               anchor="rm")
        y += row_h
    d.text((cx0 + 4, BOARD_W - CELL - 46), "наличные · капитал", font=_mono(20), fill=MUTED)
    return _png(img)


def render_results(rows: list[dict], serial: int, date: str) -> bytes:
    """Итоги: rows — [{name, tribe, color, capital, spots, out}] по местам."""
    w, row_h, gap = 1000, 112, 14
    h = 300 + len(rows) * (row_h + gap) + 80
    img = _noise(Image.new("RGB", (w, h), BG), 300 + serial)
    d = ImageDraw.Draw(img)
    for x in range(0, w, 40):  # кислотная «изолента» сверху
        d.rectangle([x, 0, x + 40, 14], fill=ACID if (x // 40) % 2 == 0 else (20, 20, 24))
    pad = 56
    d.text((pad, 54), f"ИТОГИ · ПАРТИЯ № {serial:04d}", font=_mono(26), fill=ACID)
    d.text((w - pad, 54), date, font=_mono(26), fill=MUTED, anchor="ra")
    d.text((pad - 4, 96), "НЕФОРЫ", font=_bold(76), fill=TEXT)
    d.text((pad, 180), "В ВОЛГОГРАДЕ", font=_bold(40), fill=ACID)
    top = max((r["capital"] for r in rows), default=1) or 1
    medal = {0: GOLD, 1: SILVER, 2: BRONZE}
    y = 260
    for i, r in enumerate(rows):
        col = tuple(r["color"])
        d.rounded_rectangle([pad, y, w - pad, y + row_h], radius=18, fill=PANEL,
                            outline=medal.get(i, LINE), width=3 if i < 3 else 2)
        cx, cy = pad + 50, y + row_h // 2
        _dot(d, cx, cy, 30, medal.get(i, (70, 74, 84)), str(i + 1))
        nx, right = pad + 100, w - pad - 28
        cap = "БАНКРОТ" if r.get("out") else f"{r['capital']}₽"
        d.text((right, y + 32), cap, font=_bold(36), fill=(226, 54, 70) if r.get("out") else TEXT, anchor="rm")
        nw = right - d.textlength(cap, font=_bold(36)) - 30 - nx
        d.text((nx, y + 30), _wrap(d, _plain(r["name"]), _bold(34), nw, 1)[0], font=_bold(34), fill=TEXT, anchor="lm")
        d.text((nx, y + 58), f"{_plain(r['tribe'])} · точек: {r['spots']}", font=_regular(22), fill=MUTED)
        bar_w = right - nx
        frac = 0 if r.get("out") else r["capital"] / top
        d.rounded_rectangle([nx, y + 92, nx + bar_w, y + 99], radius=4, fill=(44, 46, 54))
        d.rounded_rectangle([nx, y + 92, nx + max(10, int(bar_w * frac)), y + 99], radius=4, fill=col)
        y += row_h + gap
    d.text((pad, h - 60), "КАПИТАЛ = НАЛИЧНЫЕ + ТОЧКИ + КРЫШИ", font=_mono(22), fill=MUTED)
    return _png(img)
