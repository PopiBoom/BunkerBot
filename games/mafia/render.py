"""Картинки «Мафии» через Pillow: карточка роли (в ЛС и при смерти — с печатью), обложка, титры.

Стиль — нуар: тёмный фон, красная (мафия) или синяя (город) окантовка, значок роли
нарисован примитивами (шляпа, корона, звезда, крест, силуэт) — без шрифтов-эмодзи.
"""
from __future__ import annotations

import io
import math
import re

from PIL import Image, ImageDraw

from ..bunker.render import LINE, MUTED, PAD, PANEL, TEXT, _bold, _mono, _noise, _regular, _stamp_layer, _wrap
from . import content as C

BG = (14, 12, 15)
RED = (206, 38, 52)
GOLD = (222, 180, 62)
BLUE = (78, 148, 232)
GREEN = (60, 196, 120)
GREY = (196, 196, 190)

ROLE_COLOR = {"don": GOLD, "mafia": RED, "sheriff": BLUE, "doctor": GREEN, "civ": GREY}
TEAM_COLOR = {"mafia": RED, "town": BLUE}
STAMPS = {"killed": ("УБИТ", RED), "executed": ("КАЗНЁН", RED), "alive": ("ВЫЖИЛ", GREEN)}


_EMOJI = re.compile("[\U0001F000-\U0001FFFF\u2600-\u27BF\uFE0F\u200D\u2B00-\u2BFF]+")


def _plain(text) -> str:
    """Без эмодзи: в шрифте их нет, на картинке вышли бы квадратики (например, «🤖 Валера»)."""
    return re.sub(r"[ \t]{2,}", " ", _EMOJI.sub("", str(text))).strip()


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _canvas(w: int, h: int, seed: int, accent: tuple[int, int, int]) -> Image.Image:
    img = _noise(Image.new("RGB", (w, h), BG), seed)
    d = ImageDraw.Draw(img)
    # двойная рамка в цвет команды
    d.rectangle([14, 14, w - 15, h - 15], outline=accent, width=6)
    d.rectangle([30, 30, w - 31, h - 31], outline=(*[c // 2 for c in accent],), width=2)
    return img


# ---------- значки ролей ----------


def icon(role: str, size: int, color: tuple[int, int, int] | None = None) -> Image.Image:
    """Значок роли на прозрачном фоне size×size."""
    color = color or ROLE_COLOR.get(role, GREY)
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    rgba = (*color, 255)
    hole = (0, 0, 0, 0)
    if role == "mafia":  # шляпа-федора
        d.ellipse([s * .06, s * .58, s * .94, s * .80], fill=rgba)                       # поля
        d.polygon([(s * .24, s * .66), (s * .30, s * .26), (s * .43, s * .22), (s * .50, s * .30),
                   (s * .57, s * .22), (s * .70, s * .26), (s * .76, s * .66)], fill=rgba)  # тулья с заломом
        d.rectangle([s * .26, s * .52, s * .74, s * .60], fill=hole)                      # лента
    elif role == "don":  # корона
        pts = [(s * .12, s * .72), (s * .12, s * .32), (s * .31, s * .52), (s * .50, s * .20),
               (s * .69, s * .52), (s * .88, s * .32), (s * .88, s * .72)]
        d.polygon(pts, fill=rgba)
        d.rectangle([s * .12, s * .76, s * .88, s * .86], fill=rgba)
        for x, y in ((.12, .30), (.50, .18), (.88, .30)):
            r = s * .06
            d.ellipse([s * x - r, s * y - r, s * x + r, s * y + r], fill=rgba)
        for x in (.32, .50, .68):
            r = s * .045
            d.ellipse([s * x - r, s * .64 - r, s * x + r, s * .64 + r], fill=hole)
    elif role == "sheriff":  # звезда-жетон
        cx, cy, R, r = s / 2, s / 2, s * .46, s * .19
        pts = []
        for i in range(10):
            a = -math.pi / 2 + i * math.pi / 5
            rad = R if i % 2 == 0 else r
            pts.append((cx + rad * math.cos(a), cy + rad * math.sin(a)))
        d.polygon(pts, fill=rgba)
        rr = s * .11
        d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=hole)
    elif role == "doctor":  # крест
        a, b = s * .38, s * .62
        d.rounded_rectangle([a, s * .12, b, s * .88], radius=int(s * .04), fill=rgba)
        d.rounded_rectangle([s * .12, a, s * .88, b], radius=int(s * .04), fill=rgba)
    else:  # мирный — силуэт
        r = s * .17
        d.ellipse([s / 2 - r, s * .14, s / 2 + r, s * .14 + 2 * r], fill=rgba)
        d.pieslice([s * .16, s * .54, s * .84, s * 1.22], 180, 360, fill=rgba)
    return img


def badge(role: str, size: int, ring: tuple[int, int, int] | None = None, dead: bool = False) -> Image.Image:
    """Круглый значок для списков: кружок, обводка, значок роли (у погибших — тусклый)."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = ROLE_COLOR.get(role, GREY)
    if dead:
        color = tuple(int(c * 0.45) for c in color)
    d.ellipse([2, 2, size - 3, size - 3], fill=(*PANEL, 255), outline=(*(ring or color), 255), width=max(3, size // 22))
    inner = icon(role, int(size * 0.62), color)
    off = (size - inner.width) // 2
    img.alpha_composite(inner, (off, off))
    return img


def _stamp(img: Image.Image, kind: str, cy: int) -> Image.Image:
    """Печать поверх значка роли (cy — его центр): название роли и имя остаются читаемыми."""
    text, color = STAMPS[kind]
    layer = _stamp_layer(text, color)
    out = img.convert("RGBA")
    out.alpha_composite(layer, ((out.width - layer.width) // 2, max(0, cy - layer.height // 2)))
    return out.convert("RGB")


def _paragraphs(d: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    """Перенос по словам с сохранением абзацев (пустая строка между ними)."""
    out: list[str] = []
    for para in [p.strip() for p in str(text).split("\n") if p.strip()]:
        if out:
            out.append("")
        out += _wrap(d, para, font, width, 100)
    return out or ["—"]


# ---------- карточка роли ----------


def render_role_card(name: str, role: str, city: str, serial: int, family: list[str] | None = None,
                     stamp: str | None = None) -> bytes:
    """Карточка роли. family — напарники (для мафии), stamp — killed / executed / alive."""
    name, city = _plain(name), _plain(city)
    family = [_plain(f) for f in family] if family else None
    w = 1000
    tm = C.team(role)
    accent = TEAM_COLOR[tm]
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    desc = _wrap(probe, C.ROLES[role][3], _regular(32), w - 2 * PAD, 5)
    panels = [("ЦЕЛЬ", C.GOALS[tm], accent)] + ([("ТВОЯ СЕМЬЯ", ", ".join(family), RED)] if family else [])
    bodies = [_wrap(probe, t, _regular(30), w - 2 * PAD - 48, 2) for _l, t, _c in panels]
    h = 698 + 44 * len(desc) + sum(22 + 64 + 42 * len(b) for b in bodies) + 140
    img = _canvas(w, h, serial, accent)
    d = ImageDraw.Draw(img)
    d.text((PAD, 66), f"МАФИЯ · КАРТА № {serial:04d}", font=_mono(30), fill=accent)
    d.text((w - PAD, 66), "ГОРОД" if tm == "town" else "СЕМЬЯ", font=_mono(30), fill=MUTED, anchor="ra")

    cx, cy, r = w // 2, 300, 160
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=PANEL, outline=ROLE_COLOR.get(role, GREY), width=8)
    ic = icon(role, 210)
    img.paste(ic, (cx - ic.width // 2, cy - ic.height // 2), ic)
    d = ImageDraw.Draw(img)

    y = 490
    d.text((w // 2, y), C.title(role).upper(), font=_bold(78), fill=ROLE_COLOR.get(role, GREY), anchor="ma")
    y += 104
    d.text((w // 2, y), _wrap(d, name, _regular(42), w - 2 * PAD, 1)[0], font=_regular(42), fill=TEXT, anchor="ma")
    y += 74
    d.line([(PAD, y), (w - PAD, y)], fill=LINE, width=3)
    y += 30
    for line in desc:
        d.text((PAD, y), line, font=_regular(32), fill=(214, 214, 208))
        y += 44

    for (label, _text, color), body in zip(panels, bodies, strict=True):
        y += 22
        ph = 64 + 42 * len(body)
        d.rounded_rectangle([PAD, y, w - PAD, y + ph], radius=18, fill=PANEL, outline=color, width=3)
        d.text((PAD + 24, y + 16), label, font=_mono(26), fill=color)
        for i, line in enumerate(body):
            d.text((PAD + 24, y + 54 + 42 * i), line, font=_regular(30), fill=TEXT)
        y += ph
    d.text((PAD, h - 92), _wrap(d, city.upper(), _mono(26), w - 2 * PAD - 220, 1)[0], font=_mono(26), fill=MUTED)
    d.text((w - PAD, h - 92), "ДЕЛО ЗАКРЫТО" if stamp else "НИКОМУ НЕ ПОКАЗЫВАЙ", font=_mono(22), fill=MUTED,
           anchor="ra")
    if stamp in STAMPS:
        img = _stamp(img, stamp, cy)
    return _png(img)


# ---------- обложка партии ----------


def render_cover(city: str, intro: str, players: int, mafia: int, serial: int) -> bytes:
    city, intro = _plain(city), _plain(intro)
    w = 1000
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    title_lines = _wrap(probe, city.upper(), _bold(62), w - 2 * PAD, 2)
    font, step = _regular(33), 46
    lines = _paragraphs(probe, intro, font, w - 2 * PAD)[:18]
    h = 430 + 76 * len(title_lines) + 48 + step * len(lines) + 60 + 100 + 90
    img = _canvas(w, h, serial, RED)
    d = ImageDraw.Draw(img)
    d.text((PAD, 66), f"МАФИЯ · ПАРТИЯ № {serial:04d}", font=_mono(30), fill=RED)

    cx, cy, r = w // 2, 270, 125
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=PANEL, outline=RED, width=6)
    ic = icon("mafia", 180, TEXT)
    img.paste(ic, (cx - ic.width // 2, cy - ic.height // 2), ic)
    d = ImageDraw.Draw(img)

    y = 430
    for line in title_lines:
        d.text((w // 2, y), line, font=_bold(62), fill=TEXT, anchor="ma")
        y += 76
    y += 14
    d.line([(PAD, y), (w - PAD, y)], fill=LINE, width=3)
    y += 34
    for line in lines:
        d.text((PAD, y), line, font=font, fill=(214, 214, 208))
        y += step
    box_y = y + 40
    d.rounded_rectangle([PAD, box_y, w - PAD, box_y + 100], radius=18, fill=PANEL, outline=RED, width=3)
    cell = (w - 2 * PAD) / 2
    for i, (label, color) in enumerate(((f"ИГРОКОВ: {players}", TEXT), (f"МАФИЯ: {mafia}", RED))):
        d.text((PAD + cell * i + cell / 2, box_y + 50), label, font=_mono(36), fill=color, anchor="mm")
    return _png(img)


# ---------- титры ----------


def render_credits(city: str, winner: str, players: list[dict], nominations: list[tuple[str, str, str]],
                   serial: int, date: str) -> bytes:
    """players — [{name, role, fate}], fate — «выжил», «убит ночью 2», «казнён днём 1»;
    winner — mafia / town / draw; nominations — [(НАЗВАНИЕ, имя, пояснение)]."""
    city = _plain(city)
    players = [{**p, "name": _plain(p["name"])} for p in players]
    nominations = [(lab, _plain(n), _plain(note)) for lab, n, note in nominations]
    w = 1000
    head, color = {"mafia": ("ПОБЕДА МАФИИ", RED), "town": ("ПОБЕДА ГОРОДА", BLUE)}.get(
        winner, ("НИЧЬЯ", GREY))
    mafia = [p for p in players if C.team(p["role"]) == "mafia"]
    town = [p for p in players if C.team(p["role"]) != "mafia"]
    row_h, nom_h = 118, 150
    stamp = _stamp_layer({"mafia": "МАФИЯ", "town": "ГОРОД"}.get(winner, "НИЧЬЯ"), color)
    stamp = stamp.resize((int(stamp.width * 0.7), int(stamp.height * 0.7)), Image.LANCZOS)
    h = (150 + 80 + 60 + 40
         + 70 + row_h * len(mafia) + 70 + row_h * len(town)
         + (70 + (nom_h + 14) * len(nominations) if nominations else 0)
         + stamp.height + 70)
    img = _canvas(w, h, serial + 1, color)
    d = ImageDraw.Draw(img)
    d.text((PAD, 66), f"ТИТРЫ · МАФИЯ № {serial:04d}", font=_mono(30), fill=color)
    d.text((w - PAD, 66), date, font=_mono(30), fill=MUTED, anchor="ra")
    y = 130
    d.text((PAD, y), head, font=_bold(64), fill=color)
    y += 86
    d.text((PAD, y), _wrap(d, city, _regular(34), w - 2 * PAD, 1)[0], font=_regular(34), fill=MUTED)
    y += 60
    d.line([(PAD, y), (w - PAD, y)], fill=LINE, width=3)
    y += 30

    def section(title_: str, col, rows: list[dict]) -> None:
        nonlocal y, d
        if not rows:
            return
        d.text((PAD, y), f"{title_} · {len(rows)}", font=_mono(30), fill=col)
        y += 56
        tx = PAD + 118
        for p in rows:
            dead = not str(p.get("fate", "")).startswith("выжил")
            mark = badge(p["role"], 96, dead=dead)
            img.paste(mark, (PAD, y), mark)
            d = ImageDraw.Draw(img)
            d.text((tx, y + 6), _wrap(d, p["name"], _bold(38), w - tx - PAD, 1)[0], font=_bold(38),
                   fill=TEXT if not dead else MUTED)
            about = f"{C.title(p['role'])} · {p.get('fate', '')}"
            d.text((tx, y + 56), _wrap(d, about, _regular(27), w - tx - PAD, 1)[0], font=_regular(27), fill=MUTED)
            y += row_h
        y += 14

    section("МАФИЯ", RED, mafia)
    section("ГОРОД", BLUE, town)

    if nominations:
        d.text((PAD, y), "НОМИНАЦИИ", font=_mono(30), fill=GOLD)
        y += 56
        for label, name, note in nominations:
            d.rounded_rectangle([PAD, y, w - PAD, y + nom_h], radius=16, fill=PANEL, outline=LINE, width=2)
            d.text((PAD + 24, y + 16), label.upper(), font=_mono(26), fill=GOLD)
            d.text((PAD + 24, y + 52), _wrap(d, name, _bold(36), w - 2 * PAD - 48, 1)[0], font=_bold(36), fill=TEXT)
            d.text((PAD + 24, y + 104), _wrap(d, note, _regular(26), w - 2 * PAD - 48, 1)[0], font=_regular(26),
                   fill=MUTED)
            y += nom_h + 14

    out = img.convert("RGBA")
    out.alpha_composite(stamp, (w - stamp.width - 30, h - stamp.height - 40))
    return _png(out.convert("RGB"))
