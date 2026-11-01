"""Картинки с живыми данными чата (рисуются на лету, Pillow): таблица позора «клоуны дня».

Стиль — как у остальных карточек бота (тёмный фон, шрифты DejaVu). Эмодзи в шрифте нет, поэтому
значок клоуна нарисован фигурами, а эмодзи из имён вырезаются.
"""
from __future__ import annotations

import io
import re

from PIL import Image, ImageDraw

from games.bunker.render import LINE, MUTED, PAD, PANEL, TEXT, _bold, _mono, _noise, _wrap

BG = (16, 14, 20)
CLOWN_RED = (226, 54, 70)
GOLD, SILVER, BRONZE = (232, 186, 64), (196, 200, 210), (205, 127, 72)
_EMOJI = re.compile("[\U0001F000-\U0001FFFF☀-➿️‍⬀-⯿]+")


def _plain(text: str) -> str:
    return re.sub(r"\s{2,}", " ", _EMOJI.sub("", str(text or ""))).strip() or "?"


def _clown(size: int) -> Image.Image:
    """Значок клоуна: лицо, кудри, красный нос, улыбка."""
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for cx, cy, col in ((.18, .36, (255, 140, 0)), (.82, .36, (60, 160, 255)), (.25, .2, (255, 70, 120)),
                        (.75, .2, (140, 220, 80))):
        r = s * .16
        d.ellipse([s * cx - r, s * cy - r, s * cx + r, s * cy + r], fill=col)
    d.ellipse([s * .2, s * .2, s * .8, s * .86], fill=(250, 246, 240))
    for ex in (.38, .62):
        d.ellipse([s * ex - s * .05, s * .4, s * ex + s * .05, s * .5], fill=(30, 30, 40))
    d.ellipse([s * .43, s * .5, s * .57, s * .64], fill=CLOWN_RED)
    d.arc([s * .32, s * .52, s * .68, s * .78], 20, 160, fill=CLOWN_RED, width=max(3, s // 22))
    return img


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_clowns(rows: list[tuple[str, int]], total_days: int) -> bytes:
    """Таблица позора: rows — [(имя, сколько раз клоун дня)] по убыванию, total_days — сколько всего выбирали."""
    w = 1000
    row_h, gap = 96, 14
    h = 330 + len(rows) * (row_h + gap) + 90
    img = _noise(Image.new("RGB", (w, h), BG), 21)
    d = ImageDraw.Draw(img)
    # цирковая полоса сверху и снизу
    for x in range(0, w, 40):
        col = CLOWN_RED if (x // 40) % 2 == 0 else (245, 240, 235)
        d.rectangle([x, 0, x + 40, 16], fill=col)
        d.rectangle([x, h - 16, x + 40, h], fill=col)
    face = _clown(170)
    img.paste(face, (w - PAD - face.width, 48), face)
    d = ImageDraw.Draw(img)
    d.text((PAD, 62), "КЛОУН ДНЯ · ВЫБИРАЕТСЯ В 21:00", font=_mono(26), fill=CLOWN_RED)
    d.text((PAD - 4, 100), "ТАБЛИЦА", font=_bold(76), fill=TEXT)
    d.text((PAD - 4, 182), "ПОЗОРА", font=_bold(76), fill=TEXT)
    y = 300
    top = max((c for _n, c in rows), default=1) or 1
    medal = {0: GOLD, 1: SILVER, 2: BRONZE}
    for i, (name, count) in enumerate(rows):
        d.rounded_rectangle([PAD, y, w - PAD, y + row_h], radius=18, fill=PANEL, outline=LINE, width=2)
        cx, cy, r = PAD + 50, y + row_h // 2, 28
        col = medal.get(i, (70, 74, 84))
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)
        d.text((cx, cy + 1), str(i + 1), font=_bold(30), fill=(20, 20, 24) if i < 3 else TEXT, anchor="mm")
        name_x = PAD + 100
        count_txt = f"×{count}"
        right = w - PAD - 28
        d.text((right, y + 34), count_txt, font=_bold(40), fill=CLOWN_RED if i == 0 else TEXT, anchor="rm")
        name_w = right - d.textlength(count_txt, font=_bold(40)) - 30 - name_x
        d.text((name_x, y + 34), _wrap(d, _plain(name), _bold(34), name_w, 1)[0], font=_bold(34), fill=TEXT,
               anchor="lm")
        bar_w = right - name_x
        d.rounded_rectangle([name_x, y + 66, name_x + bar_w, y + 76], radius=5, fill=(44, 46, 54))
        d.rounded_rectangle([name_x, y + 66, name_x + max(12, int(bar_w * count / top)), y + 76], radius=5,
                            fill=col if i < 3 else (120, 124, 134))
        y += row_h + gap
    d.text((PAD, h - 66), f"ВСЕГО КЛОУНОВ ДНЯ: {total_days} · СПИСОК ПОПОЛНЯЕТСЯ КАЖДЫЙ ВЕЧЕР", font=_mono(22), fill=MUTED)
    return _png(img)
