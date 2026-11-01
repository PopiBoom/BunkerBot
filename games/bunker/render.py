"""Рисование карточки игрока («пропуск в бункер») через Pillow.

Шаблон целиком в коде: тёмный фон, сигнальная лента, поля с иконками,
карта действия и (по желанию) штамп ДОПУЩЕН / ОТКАЗАНО / ПОГИБ.
"""
from __future__ import annotations

import io
import math
import random
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from dashes import plain_dashes

from .content import ACTIONS, ATTR_ICON, ATTRS
from .icons import badge

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
    words = plain_dashes(str(text)).split()  # «-», а не «—» — и на картинках
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
    # seed не трогает глобальный random: раньше тут был random.seed(seed) — и после каждой картинки
    # «случайные» ходы игр (слухи, реплики ботов) шли по одному и тому же сценарию. Шум Pillow от него
    # всё равно не зависел.
    del seed
    grain = Image.effect_noise(img.size, 60).convert("RGB")
    return Image.blend(img, grain, 0.035)


def _stamp_layer(text: str, color: tuple[int, int, int]) -> Image.Image:
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
    return layer.rotate(17, expand=True, resample=Image.BICUBIC)


STAMPS = {"in": ("ДОПУЩЕН", (52, 199, 89)), "out": ("ОТКАЗАНО", (226, 54, 54)),
          "dead": ("ПОГИБ", (226, 54, 54))}


def _stamp(img: Image.Image, text: str, color: tuple[int, int, int]) -> Image.Image:
    layer = _stamp_layer(text, color)
    out = img.convert("RGBA")
    x = (out.width - layer.width) // 2
    y = int(out.height * 0.40) - layer.height // 2
    out.alpha_composite(layer, (x, y))
    return out.convert("RGB")


def render_card(*args, **kwargs) -> bytes:
    return _png(render_card_image(*args, **kwargs))


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_card_image(
    name: str,
    card: dict,
    serial: int,
    action: str | None = None,
    stamp: str | None = None,          # "in" | "out" | "dead" | None
    revealed: set[str] | None = None,  # None — показать всё; иначе скрытые поля помечаются
    subtitle: str = "ПРОПУСК В БУНКЕР",
) -> Image.Image:
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
    # значок персонажа справа в шапке: человек, камень, инопланетянин… (по «Данным»)
    bsize = 140
    mark = badge(card.get("bio", ""), bsize)
    img.paste(mark, (W - PAD - bsize, 112), mark)
    name_lines = _wrap(probe, name, _bold(64), W - 2 * PAD - bsize - 30, 1)
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

    # Карта действия
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

    if stamp in STAMPS:
        img = _stamp(img, *STAMPS[stamp])
    return img


# ---------- обложка партии ----------


def _canvas(w: int, h: int, seed: int) -> Image.Image:
    img = _noise(Image.new("RGB", (w, h), BG), seed)
    band = Image.new("RGB", (w, 34), ACCENT)
    d = ImageDraw.Draw(band)
    for x in range(-34, w + 34, 46):
        d.polygon([(x, 34), (x + 34, 0), (x + 57, 0), (x + 23, 34)], fill=(20, 20, 20))
    img.paste(band, (0, 0))
    img.paste(band, (0, h - 34))
    return img


def _fit_text(d: ImageDraw.ImageDraw, text: str, width: int, height: int,
              sizes=(36, 33, 30, 27, 24)) -> tuple[list[str], ImageFont.FreeTypeFont, int]:
    """Подбирает размер шрифта, чтобы текст целиком влез в блок width×height."""
    for size in sizes:
        font = _regular(size)
        step = int(size * 1.38)
        lines = _wrap(d, text, font, width, 100)
        if len(lines) * step <= height:
            return lines, font, step
    font = _regular(sizes[-1])
    step = int(sizes[-1] * 1.38)
    return _wrap(d, text, font, width, max(1, height // step)), font, step


def _poster(kicker: str, icon: str, title: str, text: str, serial: int,
            stats: list[tuple[str, tuple[int, int, int]]]) -> bytes:
    """Общий шаблон обложки/бункера: шапка, значок, заголовок, текст, плашка с цифрами."""
    w, h = 1000, 1250
    img = _canvas(w, h, serial)
    d = ImageDraw.Draw(img)
    d.text((PAD, 74), kicker, font=_mono(30), fill=ACCENT)

    cx, cy, r = w // 2, 260, 110
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=PANEL, outline=ACCENT, width=6)
    d.text((cx, cy + 6), icon, font=_regular(170), fill=ACCENT, anchor="mm")

    y = 410
    for line in _wrap(d, title.upper(), _bold(64), w - 2 * PAD, 2):
        d.text((w // 2, y), line, font=_bold(64), fill=TEXT, anchor="ma")
        y += 78
    y += 16
    d.line([(PAD, y), (w - PAD, y)], fill=LINE, width=3)
    y += 34

    box_y = h - 190
    lines, font, step = _fit_text(d, text, w - 2 * PAD, box_y - 40 - y)
    for line in lines:
        d.text((PAD, y), line, font=font, fill=(214, 214, 208))
        y += step

    if stats:
        d.rounded_rectangle([PAD, box_y, w - PAD, box_y + 100], radius=18, fill=PANEL, outline=ACCENT, width=3)
        cell = (w - 2 * PAD) / len(stats)
        for i, (label, color) in enumerate(stats):
            d.text((PAD + cell * i + cell / 2, box_y + 50), label, font=_mono(36), fill=color, anchor="mm")
    return _png(img)


def render_cover(title: str, catastrophe: str, players: int, spots: int, serial: int) -> bytes:
    return _poster(f"КАТАСТРОФА · ПАРТИЯ № {serial:04d}", "☢", title, catastrophe, serial,
                   [(f"ИГРОКОВ: {players}", TEXT)])


def render_bunker(bunker: str, players: int, spots: int, serial: int) -> bytes:
    return _poster(f"УБЕЖИЩЕ · ПАРТИЯ № {serial:04d}", "⌂", "Бункер", bunker, serial + 2,
                   [(f"МЕСТ: {spots}", ACCENT), (f"ИГРОКОВ: {players}", TEXT)])


# ---------- финальные титры ----------


def render_credits(title: str, survivors: list[dict], exiled: list[dict], nominations: list[tuple[str, str, str]],
                   survived: bool, serial: int, date: str) -> bytes:
    """Итоговая картинка партии.

    survivors — [{name, card}], exiled — [{name, round, died}],
    nominations — [(НАЗВАНИЕ, имя, пояснение)].
    """
    w = 1000
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    title_lines = _wrap(probe, title.upper(), _bold(54), w - 2 * PAD, 2)
    row_h, out_h, nom_h = 132, 42, 150
    verdict = ("ВЫЖИЛИ", GREEN) if survived else ("ПОГИБЛИ", RED)
    layer = _stamp_layer(*verdict)
    layer = layer.resize((int(layer.width * 0.75), int(layer.height * 0.75)), Image.LANCZOS)
    h = (150 + 66 * len(title_lines) + 40
         + 70 + row_h * len(survivors)
         + (70 + out_h * len(exiled) if exiled else 0)
         + (70 + (nom_h + 14) * len(nominations) if nominations else 0)
         + layer.height + 60)  # снизу — место под печать, чтобы она ничего не закрывала
    img = _canvas(w, h, serial + 1)
    d = ImageDraw.Draw(img)

    d.text((PAD, 74), f"ТИТРЫ · БУНКЕР № {serial:04d}", font=_mono(30), fill=ACCENT)
    d.text((w - PAD, 74), date, font=_mono(30), fill=MUTED, anchor="ra")
    y = 136
    for line in title_lines:
        d.text((PAD, y), line, font=_bold(54), fill=TEXT)
        y += 66
    y += 16
    d.line([(PAD, y), (w - PAD, y)], fill=LINE, width=3)
    y += 34

    d.text((PAD, y), f"В БУНКЕРЕ · {len(survivors)}", font=_mono(30), fill=GREEN)
    y += 56
    text_x = PAD + 124
    for p in survivors:
        card = p.get("card") or {}
        mark = badge(card.get("bio", ""), 100, ring=GREEN)
        img.paste(mark, (PAD, y), mark)
        d = ImageDraw.Draw(img)
        d.text((text_x, y + 4), _wrap(probe, p["name"], _bold(40), w - text_x - PAD, 1)[0], font=_bold(40), fill=TEXT)
        about = " · ".join(str(card.get(k, "")).strip() for k in ("profession", "bio") if card.get(k))
        d.text((text_x, y + 58), _wrap(probe, about or "—", _regular(26), w - text_x - PAD, 1)[0],
               font=_regular(26), fill=MUTED)
        y += row_h

    if exiled:
        y += 14
        d.text((PAD, y), f"ЗА ДВЕРЬЮ · {len(exiled)}", font=_mono(30), fill=RED)
        y += 56
        for p in exiled:
            how = "не вернулся(ась) с угрозы" if p.get("died") else "изгнан(а)"
            line = f"{p['name']} — {how}" + (f", раунд {p['round']}" if p.get("round") else "")
            d.text((PAD, y), _wrap(probe, line, _regular(28), w - 2 * PAD, 1)[0], font=_regular(28), fill=MUTED)
            y += out_h

    if nominations:
        y += 14
        d.text((PAD, y), "НОМИНАЦИИ", font=_mono(30), fill=ACCENT)
        y += 56
        for label, name, note in nominations:
            d.rounded_rectangle([PAD, y, w - PAD, y + nom_h], radius=16, fill=PANEL, outline=LINE, width=2)
            d.text((PAD + 24, y + 16), label.upper(), font=_mono(26), fill=ACCENT)
            d.text((PAD + 24, y + 52), _wrap(probe, name, _bold(36), w - 2 * PAD - 48, 1)[0], font=_bold(36), fill=TEXT)
            d.text((PAD + 24, y + 104), _wrap(probe, note, _regular(26), w - 2 * PAD - 48, 1)[0], font=_regular(26),
                   fill=MUTED)
            y += nom_h + 14

    out = img.convert("RGBA")
    out.alpha_composite(layer, (w - layer.width - 20, h - layer.height - 46))
    return _png(out.convert("RGB"))


# ---------- анимации (GIF) ----------


def _gif(frames: list[Image.Image], durations: list[int]) -> bytes:
    pal = [f.convert("P", palette=Image.ADAPTIVE, colors=128) for f in frames]
    buf = io.BytesIO()
    pal[0].save(buf, format="GIF", save_all=True, append_images=pal[1:],
                duration=durations, loop=0, optimize=True, disposal=1)
    return buf.getvalue()


def animate_stamp(card: Image.Image, stamp: str) -> bytes:
    """Карточка, на которую с размаху опускается печать."""
    text, color = STAMPS[stamp]
    width = 480
    base = card.resize((width, int(card.height * width / card.width)), Image.LANCZOS).convert("RGBA")
    layer0 = _stamp_layer(text, color)
    k = width / card.width
    frames, durs = [base.convert("RGB")], [700]
    for i, scale in enumerate([2.4, 1.9, 1.5, 1.2, 1.0]):
        sw, sh = int(layer0.width * k * scale), int(layer0.height * k * scale)
        layer = layer0.resize((sw, sh), Image.LANCZOS)
        alpha = layer.getchannel("A").point(lambda a, f=(i + 1) / 5: int(a * f))
        layer.putalpha(alpha)
        f = base.copy()
        f.alpha_composite(layer, ((width - sw) // 2, int(base.height * 0.40) - sh // 2))
        frames.append(f.convert("RGB"))
        durs.append(60)
    final = frames[-1]
    for dx, dy in [(8, -6), (-6, 5), (3, -2), (0, 0)]:  # тряска от удара
        f = Image.new("RGB", final.size, BG)
        f.paste(final, (dx, dy))
        frames.append(f)
        durs.append(50)
    durs[-1] = 3000
    return _gif(frames, durs)


def animate_hatch(text: str = "ЛЮК ЗАДРАЕН") -> bytes:
    """Створки гермодвери съезжаются, крутится штурвал."""
    w = h = 480
    inside = _canvas(w, h, 7)
    d = ImageDraw.Draw(inside)
    d.ellipse([w // 2 - 160, h // 2 - 160, w // 2 + 160, h // 2 + 160], fill=(40, 34, 18))
    d.text((w // 2, h // 2), "☢", font=_regular(170), fill=ACCENT, anchor="mm")

    def door(width: int, left: bool) -> Image.Image:
        dimg = Image.new("RGB", (max(1, width), h), (70, 74, 82))
        dd = ImageDraw.Draw(dimg)
        for y in range(40, h, 60):
            for x in (20, width - 20):
                dd.ellipse([x - 5, y - 5, x + 5, y + 5], fill=(110, 114, 122))
        edge = width - 18 if left else 0
        for y in range(-18, h, 36):
            dd.polygon([(edge, y), (edge + 18, y + 18), (edge + 18, y + 36), (edge, y + 18)], fill=ACCENT)
        return dimg

    frames, durs = [inside.copy()], [500]
    steps = 10
    for i in range(1, steps + 1):
        half = int((w // 2) * (i / steps) ** 1.6)
        f = inside.copy()
        f.paste(door(half, True), (0, 0))
        right = door(half, False)
        f.paste(right, (w - half, 0))
        frames.append(f)
        durs.append(55)
    closed = frames[-1]
    for i, angle in enumerate([0, 30, 60, 90, 120]):
        f = closed.copy()
        fd = ImageDraw.Draw(f)
        cx, cy, r = w // 2, h // 2 - 20, 70
        fd.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(200, 200, 200), width=10)
        for k in range(4):
            a = math.radians(angle + k * 90)
            fd.line([(cx, cy), (cx + r * math.cos(a), cy + r * math.sin(a))], fill=(200, 200, 200), width=8)
        fd.ellipse([cx - 14, cy - 14, cx + 14, cy + 14], fill=ACCENT)
        if i == 4:
            fd.rounded_rectangle([40, h - 140, w - 40, h - 70], radius=12, fill=(20, 20, 20), outline=ACCENT, width=3)
            fd.text((w // 2, h - 105), text, font=_mono(40), fill=ACCENT, anchor="mm")
        frames.append(f)
        durs.append(120)
    durs[-1] = 3000
    return _gif(frames, durs)
