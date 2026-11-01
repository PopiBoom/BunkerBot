"""Рисует картинки для /menu (assets/menu/*.jpg): главную и по одной на каждый раздел.

Запускать вручную, когда меняются разделы в handlers/menu.py:  python tools/menu_images.py
Нужен цветной шрифт эмодзи (Noto Color Emoji: пакет fonts-noto-color-emoji). На сервере он не нужен —
бот отправляет уже готовые картинки.
"""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from games.bunker.render import _bold, _mono, _noise, _regular, _wrap  # noqa: E402
from handlers.menu import IMAGES, ORDER, SECTIONS  # noqa: E402

EMOJI_FONT = "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"
W, H = 1280, 720
BG = (14, 13, 17)
PANEL = (27, 28, 34)
LINE = (52, 54, 62)
TEXT = (238, 236, 230)
MUTED = (145, 148, 158)
YELLOW, RED = (247, 196, 37), (206, 38, 52)


@lru_cache(maxsize=None)
def emoji(ch: str, size: int) -> Image.Image:
    f = ImageFont.truetype(EMOJI_FONT, 109)
    im = Image.new("RGBA", (160, 136), (0, 0, 0, 0))
    ImageDraw.Draw(im).text((0, 0), ch, font=f, embedded_color=True)
    im = im.crop(im.getbbox())
    scale = size / max(im.width, im.height)
    return im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))), Image.LANCZOS)


def _paste_center(img: Image.Image, ic: Image.Image, cx: int, cy: int) -> None:
    img.paste(ic, (cx - ic.width // 2, cy - ic.height // 2), ic)


def _badge(img: Image.Image, em: str, x: int, y: int, d: int, color, icon: int) -> None:
    """Значок раздела в кружке его цвета."""
    tint = Image.new("RGBA", (d, d), (0, 0, 0, 0))
    ImageDraw.Draw(tint).ellipse([0, 0, d - 1, d - 1], fill=(*color, 70))
    img.paste(tint, (x, y), tint)
    _paste_center(img, emoji(em, icon), x + d // 2, y + d // 2)


def _strip(d: ImageDraw.ImageDraw, a, b) -> None:
    for x in range(W):
        t = x / W
        d.line([(x, 0), (x, 10)], fill=tuple(int(a[i] * (1 - t) + b[i] * t) for i in range(3)))


def _fit(d: ImageDraw.ImageDraw, text: str, size: int, width: int, minimum: int = 20):
    while size > minimum and d.textlength(text, font=_bold(size)) > width:
        size -= 1
    return _bold(size), size


def main_image() -> Image.Image:
    img = _noise(Image.new("RGB", (W, H), BG), 7)
    d = ImageDraw.Draw(img)
    _strip(d, YELLOW, RED)
    d.text((64, 52), "ПИЗДЮК", font=_bold(92), fill=TEXT)
    d.text((70, 160), "МЕНЮ БОТА · ВЫБЕРИ РАЗДЕЛ", font=_mono(28), fill=MUTED)
    face = emoji("😈", 104)
    img.paste(face, (W - 64 - face.width, 44), face)
    d = ImageDraw.Draw(img)
    gx, gy, cols, gap = 64, 236, 3, 28
    tw, th = (W - 2 * gx - gap * (cols - 1)) // cols, 210
    for i, key in enumerate(ORDER):
        s = SECTIONS[key]
        x = gx + (i % cols) * (tw + gap)
        y = gy + (i // cols) * (th + gap)
        d.rounded_rectangle([x, y, x + tw, y + th], radius=22, fill=PANEL, outline=LINE, width=2)
        _badge(img, s["emoji"], x + 24, y + 22, 84, s["color"], 54)
        d = ImageDraw.Draw(img)
        font, size = _fit(d, s["title"], 34, tw - 124 - 18, 24)
        d.text((x + 124, y + 40 + (34 - size) // 2), s["title"], font=font, fill=TEXT)
        d.line([(x + 124, y + 88), (x + 172, y + 88)], fill=s["color"], width=4)
        for k, line in enumerate(_wrap(d, s["tagline"], _regular(24), tw - 48, 2)):
            d.text((x + 26, y + 128 + k * 32), line, font=_regular(24), fill=MUTED)
    return img


def section_image(key: str) -> Image.Image:
    s = SECTIONS[key]
    color = s["color"]
    img = _noise(Image.new("RGB", (W, H), BG), hash(key) % 1000)
    d = ImageDraw.Draw(img)
    _strip(d, color, tuple(c // 2 for c in color))
    _badge(img, s["emoji"], 64, 44, 140, color, 92)
    d = ImageDraw.Draw(img)
    d.text((232, 50), "ПИЗДЮК · МЕНЮ", font=_mono(26), fill=MUTED)
    font, _ = _fit(d, s["title"].upper(), 80, W - 232 - 64, 48)
    d.text((228, 82), s["title"].upper(), font=font, fill=TEXT)
    d.text((232, 178), s["tagline"], font=_regular(28), fill=MUTED)

    rows = s["rows"]
    y, gap = 232, 12
    rh = 88 if len(rows) >= 4 else 104  # трёх строк меньше — делаем их выше, чтобы картинка не пустела
    for em, name, desc, cmd, action in rows:
        d.rounded_rectangle([64, y, W - 64, y + rh], radius=18, fill=PANEL, outline=LINE, width=2)
        if action:  # действие есть кнопкой — полоска цвета раздела
            d.rounded_rectangle([64, y + 18, 70, y + rh - 18], radius=3, fill=color)
        _paste_center(img, emoji(em, 44), 122, y + rh // 2)
        d = ImageDraw.Draw(img)
        d.text((172, y + rh // 2), name, font=_bold(32), fill=TEXT, anchor="lm")
        nx = 172 + d.textlength(name, font=_bold(32)) + 22
        cmd_w = d.textlength(cmd, font=_mono(26))
        room = W - 64 - 28 - cmd_w - 24 - nx
        d.text((nx, y + rh // 2 + 2), _wrap(d, desc, _regular(24), room, 1)[0], font=_regular(24), fill=MUTED,
               anchor="lm")
        d.text((W - 64 - 28, y + rh // 2), cmd, font=_mono(26), fill=color, anchor="rm")
        y += rh + gap
    if s.get("note"):
        d.text((66, min(y + 14, H - 52)), s["note"], font=_mono(22), fill=MUTED)
    return img


def save(img: Image.Image, name: str) -> None:
    IMAGES.mkdir(parents=True, exist_ok=True)
    img.save(IMAGES / f"{name}.jpg", format="JPEG", quality=90, optimize=True)


if __name__ == "__main__":
    save(main_image(), "main")
    for key in ORDER:
        save(section_image(key), key)
    print("готово:", sorted(p.name for p in IMAGES.glob("*.jpg")))
