"""Картинки с живыми данными чата (рисуются на лету, Pillow): таблица позора «клоуны дня», карточка
«клоун дня», постер «Оскар позора» (итоги недели) и демотиватор.

Стиль — как у остальных карточек бота (тёмный фон, шрифты DejaVu). Эмодзи в шрифте нет, поэтому
значок клоуна нарисован фигурами, а эмодзи из имён вырезаются.
"""
from __future__ import annotations

import io
import re

from PIL import Image, ImageDraw

from games.bunker.render import LINE, MUTED, PAD, PANEL, TEXT, _bold, _font, _mono, _noise, _regular, _wrap

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


def _stripes(img: Image.Image, y: int, h: int = 16) -> None:
    d = ImageDraw.Draw(img)
    for x in range(0, img.width, 40):
        d.rectangle([x, y, x + 40, y + h], fill=CLOWN_RED if (x // 40) % 2 == 0 else (245, 240, 235))


def render_clown_day(name: str, quote: str, count: int, nickname: str, date: str) -> bytes:
    """Карточка «Клоун дня»: имя, «клоун ×N», состав преступления (дословная цитата дня), прозвище на сутки."""
    w = 1000
    img = _noise(Image.new("RGB", (w, 1600), BG), 7)
    d = ImageDraw.Draw(img)
    face = _clown(230)
    img.paste(face, (w - PAD - face.width + 10, 40), face)
    d = ImageDraw.Draw(img)
    d.text((PAD, 62), f"КЛОУН ДНЯ · {date}", font=_mono(28), fill=CLOWN_RED)
    y = 108
    for line in _wrap(d, _plain(name).upper(), _bold(78), w - 2 * PAD - 250, 2):
        d.text((PAD - 4, y), line, font=_bold(78), fill=TEXT)
        y += 88
    tag = "ПЕРВЫЙ РАЗ - С ПОЧИНОМ" if count <= 1 else f"КЛОУН ×{count}"
    d.text((PAD, y + 8), tag, font=_bold(36), fill=GOLD)
    y = max(y + 80, 300)
    if quote:
        lines = _wrap(d, f"«{_plain(quote)}»", _regular(38), w - 2 * PAD - 60, 7)
        box_h = 70 + len(lines) * 52 + 26
        d.rounded_rectangle([PAD, y, w - PAD, y + box_h], radius=20, fill=PANEL, outline=LINE, width=2)
        d.text((PAD + 30, y + 22), "СОСТАВ ПРЕСТУПЛЕНИЯ", font=_mono(24), fill=CLOWN_RED)
        ty = y + 66
        for line in lines:
            d.text((PAD + 30, ty), line, font=_regular(38), fill=TEXT)
            ty += 52
        y += box_h + 30
    if nickname:
        d.text((PAD, y), "ПРОЗВИЩЕ НА СУТКИ", font=_mono(24), fill=MUTED)
        y += 36
        for line in _wrap(d, _plain(nickname), _bold(46), w - 2 * PAD, 2):
            d.text((PAD, y), line, font=_bold(46), fill=GOLD)
            y += 56
        y += 14
    d.text((PAD, y + 10), "ТАБЛИЦА ПОЗОРА: /CLOWNS", font=_mono(22), fill=MUTED)
    h = y + 70
    img = img.crop((0, 0, w, h))
    _stripes(img, 0)
    _stripes(img, h - 16)
    return _png(img)


def _statuette(size: int) -> Image.Image:
    """Золотая статуэтка «Оскара» — фигурами: голова, тело, постамент."""
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    gold, dark = (232, 186, 64), (150, 110, 30)
    d.rectangle([s * .22, s * .86, s * .78, s * .97], fill=(40, 40, 46))
    d.rectangle([s * .3, s * .8, s * .7, s * .87], fill=dark)
    d.polygon([(s * .38, s * .3), (s * .62, s * .3), (s * .58, s * .8), (s * .42, s * .8)], fill=gold)
    d.ellipse([s * .41, s * .1, s * .59, s * .3], fill=gold)
    d.line([(s * .38, s * .36), (s * .5, s * .5), (s * .62, s * .36)], fill=dark, width=max(2, s // 40))
    return img


def render_week_poster(period: str, noms: list[tuple[str, str, str]]) -> bytes:
    """Итоги недели «Оскар позора»: noms — [(номинация, имя, дословная цитата или "")]."""
    w = 1000
    img = _noise(Image.new("RGB", (w, 3000), (14, 12, 10)), 11)
    d = ImageDraw.Draw(img)
    st = _statuette(200)
    img.paste(st, (w - PAD - st.width + 20, 30), st)
    d = ImageDraw.Draw(img)
    d.text((PAD, 60), f"ИТОГИ НЕДЕЛИ · {period}", font=_mono(26), fill=MUTED)
    d.text((PAD - 4, 98), "ОСКАР", font=_bold(84), fill=GOLD)
    d.text((PAD - 4, 186), "ПОЗОРА", font=_bold(84), fill=TEXT)
    y = 300
    for i, (title, name, quote) in enumerate(noms):
        q_lines = _wrap(d, f"«{_plain(quote)}»", _regular(30), w - 2 * PAD - 120, 3) if quote else []
        box_h = 118 + len(q_lines) * 40 + (12 if q_lines else 0)
        d.rounded_rectangle([PAD, y, w - PAD, y + box_h], radius=18, fill=(28, 26, 22), outline=(80, 66, 30), width=2)
        cx, cy, r = PAD + 46, y + 52, 26
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=GOLD)
        d.text((cx, cy + 1), str(i + 1), font=_bold(28), fill=(20, 18, 14), anchor="mm")
        x = PAD + 96
        d.text((x, y + 20), _wrap(d, _plain(title).upper(), _mono(24), w - PAD - x - 24, 1)[0],
               font=_mono(24), fill=GOLD)
        d.text((x, y + 54), _wrap(d, _plain(name), _bold(40), w - PAD - x - 24, 1)[0], font=_bold(40), fill=TEXT)
        ty = y + 110
        for line in q_lines:
            d.text((x, ty), line, font=_regular(30), fill=MUTED)
            ty += 40
        y += box_h + 18
    d.text((PAD, y + 14), "НАГРАДЫ ВРУЧЕНЫ. АПЕЛЛЯЦИИ НЕ ПРИНИМАЮТСЯ.", font=_mono(22), fill=MUTED)
    h = y + 76
    img = img.crop((0, 0, w, h))
    band = ImageDraw.Draw(img)
    band.rectangle([0, 0, w, 12], fill=GOLD)
    band.rectangle([0, h - 12, w, h], fill=GOLD)
    return _png(img)


def _serif(size: int, bold: bool = False):
    return _font("DejaVuSerif-Bold.ttf" if bold else "DejaVuSerif.ttf", size)


def render_demotivator(photo: bytes, title: str, subtitle: str) -> bytes:
    """Классический демотиватор: чёрный фон, фото в белой рамке, КРУПНЫЙ заголовок и подпись под ним."""
    src = Image.open(io.BytesIO(photo)).convert("RGB")
    src.thumbnail((900, 760))
    pw, ph = src.size
    margin, gap, border = 70, 12, 3
    w = max(pw + 2 * margin, 700)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    size = 72
    t_lines = _wrap(probe, _plain(title).upper(), _serif(size, True), w - 2 * margin, 2)
    while size > 40 and any(probe.textlength(t, font=_serif(size, True)) > w - 2 * margin for t in t_lines):
        size -= 4
        t_lines = _wrap(probe, _plain(title).upper(), _serif(size, True), w - 2 * margin, 2)
    s_lines = _wrap(probe, _plain(subtitle), _serif(34), w - 2 * margin, 3) if subtitle else []
    top = 60
    text_y = top + ph + 2 * (gap + border) + 46
    h = text_y + len(t_lines) * int(size * 1.2) + (14 + len(s_lines) * 46 if s_lines else 0) + 60
    img = Image.new("RGB", (w, h), (0, 0, 0))
    d = ImageDraw.Draw(img)
    x0 = (w - pw) // 2
    d.rectangle([x0 - gap - border, top - border, x0 + pw + gap + border - 1, top + ph + 2 * gap + border - 1],
                outline=(235, 235, 235), width=border)
    img.paste(src, (x0, top + gap))
    y = text_y
    for line in t_lines:
        d.text((w // 2, y), line, font=_serif(size, True), fill=(255, 255, 255), anchor="ma")
        y += int(size * 1.2)
    y += 14
    for line in s_lines:
        d.text((w // 2, y), line, font=_serif(34), fill=(225, 225, 225), anchor="ma")
        y += 46
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()
