"""Рисование карточки игрока («пропуск в бункер») через Pillow.

Шаблон целиком в коде: тёмный фон, сигнальная лента, поля с иконками,
карта действия, тайная цель и (по желанию) штамп ДОПУЩЕН / ОТКАЗАНО.
"""
from __future__ import annotations

import io
import random
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .content import ACTIONS, ATTR_ICON, ATTRS

FONTS = Path(__file__).resolve().parents[2] / "assets" / "fonts"

W = 1000
PAD = 56
BG = (18, 20, 24)
PANEL = (28, 31, 37)
LINE = (52, 56, 64)
TEXT = (236, 236, 232)
MUTED = (140, 146, 156)
ACCENT = (247, 196, 37)      # сигнальный жёлтый
RED = (226, 54, 54)
GREEN = (52, 199, 89)


@lru_cache(maxsize=None)
def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / name), size)


def _regular(size: int):
    return _font("DejaVuSans.ttf", size)


def _bold(size: int):
    return _font("DejaVuSans-Bold.ttf", size)


def _mono(size: int):
    return _font("DejaVuSansMono-Bold.ttf", size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int, max_lines: int) -> list[str]:
    words = str(text).split()
    lines: list[str] = []
    cur = ""
    for w in words:
        test = f"{cur} {w}".strip()
        if draw.textlength(test, font=font) <= width:
            cur = test
            continue
        if cur:
            lines.append(cur)
        cur = w
        while draw.textlength(cur, font=font) > width:  # очень длинное слово
            cut = len(cur)
            while cut > 1 and draw.textlength(cur[:cut], font=font) > width:
                cut -= 1
            lines.append(cur[:cut])
            cur = cur[cut:]
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines or ["—"]


def _hazard_band(img: Image.Image, y: int, h: int) -> None:
    band = Image.new("RGB", (W, h), ACCENT)
    d = ImageDraw.Draw(band)
    step = 46
    for x in range(-h, W + h, step):
        d.polygon([(x, h), (x + h, 0), (x + h + step // 2, 0), (x + step // 2, h)], fill=(20, 20, 20))
    img.paste(band, (0, y))


def _noise(img: Image.Image, seed: int) -> Image.Image:
    random.seed(seed)
    grain = Image.effect_noise(img.size, 60).convert("RGB")
    return Image.blend(img, grain, 0.035)


def _stamp(img: Image.Image, text: str, color: tuple[int, int, int]) -> Image.Image:
    font = _mono(118)
    tmp = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    tw = int(tmp.textlength(text, font=font))
    sw, sh = tw + 110, 210
    layer = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    rgba = (*color, 215)
    d.rounded_rectangle([8, 8, sw - 8, sh - 8], radius=26, outline=rgba, width=12)
    d.rounded_rectangle([30, 30, sw - 30, sh - 30], radius=16, outline=rgba, width=4)
    d.text((sw // 2, sh // 2 + 4), text, font=font, fill=rgba, anchor="mm")
    # «потёртость» штампа
    rnd = random.Random(text)
    mask = layer.getchannel("A")
    holes = Image.new("L", layer.size, 255)
    hd = ImageDraw.Draw(holes)
    for _ in range(260):
        x, y = rnd.randrange(sw), rnd.randrange(sh)
        r = rnd.randrange(2, 7)
        hd.ellipse([x - r, y - r, x + r, y + r], fill=rnd.randrange(0, 140))
    layer.putalpha(Image.composite(mask, Image.new("L", layer.size, 0), holes))
    layer = layer.rotate(17, expand=True, resample=Image.BICUBIC)
    out = img.convert("RGBA")
    x = (out.width - layer.width) // 2
    y = int(out.height * 0.40) - layer.height // 2
    out.alpha_composite(layer, (x, y))
    return out.convert("RGB")


def render_card(
    name: str,
    card: dict,
    serial: int,
    action: str | None = None,
    goal: str | None = None,
    stamp: str | None = None,          # "in" | "out" | None
    revealed: set[str] | None = None,  # None — показать всё; иначе скрытые поля помечаются
    subtitle: str = "ПРОПУСК В БУНКЕР",
) -> bytes:
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    label_f, value_f, icon_f = _bold(22), _regular(32), _regular(40)
    text_x = PAD + 92
    text_w = W - text_x - PAD

    # Раскладка: сначала считаем высоту строк
    rows = []
    for key, label, _emoji, _icon in ATTRS:
        lines = _wrap(probe, card.get(key, "—"), value_f, text_w, 2)
        rows.append((key, label, lines, 40 + 40 * len(lines) + 22))

    extra = []
    if action and action in ACTIONS:
        emoji, title, desc, _when, _t = ACTIONS[action]
        extra.append(("КАРТА ДЕЙСТВИЯ", title, desc, ACCENT))
    if goal:
        extra.append(("ТАЙНАЯ ЦЕЛЬ", "", goal, (120, 170, 255)))
    extra_layout = []
    for head, title, desc, col in extra:
        body = (f"{title}. {desc}" if title else desc)
        lines = _wrap(probe, body, _regular(28), W - 2 * PAD - 48, 3)
        extra_layout.append((head, lines, col, 70 + 36 * len(lines) + 24))

    header_h = 300
    height = header_h + sum(r[3] for r in rows) + 30
    height += sum(e[3] + 20 for e in extra_layout) + 110

    img = Image.new("RGB", (W, height), BG)
    img = _noise(img, serial)
    d = ImageDraw.Draw(img)

    _hazard_band(img, 0, 34)
    _hazard_band(img, height - 34, 34)

    # Шапка
    d.text((PAD, 74), subtitle, font=_mono(30), fill=ACCENT)
    d.text((W - PAD, 74), f"№ {serial:04d}", font=_mono(30), fill=MUTED, anchor="ra")
    name_lines = _wrap(probe, name, _bold(64), W - 2 * PAD, 1)
    d.text((PAD, 126), name_lines[0], font=_bold(64), fill=TEXT)
    d.text((PAD, 214), "ДОСЬЕ ВЫЖИВШЕГО · ХРАНИТЬ В ТАЙНЕ", font=_regular(22), fill=MUTED)
    d.line([(PAD, 262), (W - PAD, 262)], fill=LINE, width=3)

    # Характеристики
    y = header_h
    for key, label, lines, h in rows:
        hidden = revealed is not None and key not in revealed
        cx, cy = PAD + 34, y + 44
        ring = MUTED if hidden else ACCENT
        d.ellipse([cx - 34, cy - 34, cx + 34, cy + 34], fill=PANEL, outline=ring, width=3)
        d.text((cx, cy + 2), ATTR_ICON[key], font=icon_f, fill=ring, anchor="mm")
        tag = f"{label.upper()}" + ("  · НЕ ОТКРЫВАЛОСЬ" if hidden else "")
        d.text((text_x, y + 6), tag, font=label_f, fill=MUTED)
        ly = y + 40
        for line in lines:
            d.text((text_x, ly), line, font=value_f, fill=(170, 170, 170) if hidden else TEXT)
            ly += 40
        y += h
        d.line([(text_x, y - 12), (W - PAD, y - 12)], fill=LINE, width=1)

    # Карта действия / цель
    y += 20
    for head, lines, col, h in extra_layout:
        d.rounded_rectangle([PAD, y, W - PAD, y + h], radius=18, fill=PANEL, outline=col, width=3)
        d.text((PAD + 24, y + 20), head, font=_mono(26), fill=col)
        ly = y + 64
        for line in lines:
            d.text((PAD + 24, ly), line, font=_regular(28), fill=TEXT)
            ly += 36
        y += h + 20

    d.text((W // 2, height - 72), "ДЕЙСТВИТЕЛЕН ДО КОНЦА СВЕТА", font=_mono(22), fill=MUTED, anchor="mm")

    if stamp == "in":
        img = _stamp(img, "ДОПУЩЕН", GREEN)
    elif stamp == "out":
        img = _stamp(img, "ОТКАЗАНО", RED)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
