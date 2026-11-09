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


def to_jpeg(data: bytes, max_side: int = 1280) -> bytes:
    """Любая картинка (WebP-стикер, превью анимации, PNG с прозрачностью) → обычный JPEG: первый кадр,
    прозрачное — на белом. И ИИ, и демотиватор получают один понятный формат (на стикерах бывал сбой)."""
    img = Image.open(io.BytesIO(data))
    img.seek(0)
    img = img.convert("RGBA")
    flat = Image.new("RGB", img.size, (255, 255, 255))
    flat.paste(img, mask=img.split()[3])
    flat.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    flat.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def render_demotivator(photo: bytes, title: str, subtitle: str, side: int = 1000) -> bytes:
    """Классический демотиватор, КВАДРАТНЫЙ (просьба владельца): чёрный фон, фото в белой рамке, КРУПНЫЙ
    заголовок и подпись. Фото не обрезается и не вписывается с полями, а сжимается/растягивается ровно под
    рамку (непропорционально), чтобы всё вместе было квадратом."""
    src = Image.open(io.BytesIO(photo)).convert("RGB")
    margin, gap, border = 70, 12, 3
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    width = side - 2 * margin
    size = 72
    t_lines = _wrap(probe, _plain(title).upper(), _serif(size, True), width, 2)
    while size > 40 and any(probe.textlength(t, font=_serif(size, True)) > width for t in t_lines):
        size -= 4
        t_lines = _wrap(probe, _plain(title).upper(), _serif(size, True), width, 2)
    s_lines = _wrap(probe, _plain(subtitle), _serif(34), width, 3) if subtitle else []
    text_h = 40 + len(t_lines) * int(size * 1.2) + (14 + len(s_lines) * 46 if s_lines else 0)
    pad = gap + border
    box_w, box_h = width - 2 * pad, side - 2 * 40 - text_h - 2 * pad  # место под фото (поля сверху и снизу — 40)
    # фото СЖИМАЕТСЯ/растягивается ровно под рамку, без полей — пусть и непропорционально (так хочет владелец)
    pw, ph = box_w, box_h
    src = src.resize((pw, ph), Image.LANCZOS)
    top = (side - (ph + 2 * pad + text_h)) // 2  # рамка + текст — по центру квадрата
    img = Image.new("RGB", (side, side), (0, 0, 0))
    d = ImageDraw.Draw(img)
    x0 = (side - pw) // 2
    d.rectangle([x0 - pad, top, x0 + pw + pad - 1, top + ph + 2 * pad - 1], outline=(235, 235, 235), width=border)
    img.paste(src, (x0, top + pad))
    y = top + ph + 2 * pad + 40
    for line in t_lines:
        d.text((side // 2, y), line, font=_serif(size, True), fill=(255, 255, 255), anchor="ma")
        y += int(size * 1.2)
    y += 14
    for line in s_lines:
        d.text((side // 2, y), line, font=_serif(34), fill=(225, 225, 225), anchor="ma")
        y += 46
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


# ---------- коллекционные карточки участников (handlers/collect.py) ----------

RARITY = {  # цвет рамки, фон, звёзды
    "обычная": ((150, 156, 168), (40, 43, 50), "★"),
    "редкая": ((80, 150, 255), (22, 34, 60), "★★"),
    "легендарная": ((240, 190, 70), (52, 38, 14), "★★★"),
}


def _avatar(name: str, size: int, photo: bytes | None, seed: int) -> Image.Image:
    """Круглая аватарка: фото профиля, а нет его — первая буква имени на цветном градиенте."""
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    im = None
    if photo:
        try:
            src = Image.open(io.BytesIO(photo)).convert("RGB")
            side = min(src.size)
            src = src.crop(((src.width - side) // 2, (src.height - side) // 2,
                            (src.width + side) // 2, (src.height + side) // 2))
            im = src.resize((size, size), Image.LANCZOS).convert("RGBA")
        except Exception:
            im = None
    if im is None:
        import random

        rnd = random.Random(seed)
        c1 = tuple(rnd.randint(60, 200) for _ in range(3))
        c2 = tuple(min(255, v + 50) for v in c1)
        im = Image.new("RGBA", (size, size))
        d = ImageDraw.Draw(im)
        for i in range(size):
            t = i / size
            d.line((0, i, size, i), fill=tuple(int(c1[k] * (1 - t) + c2[k] * t) for k in range(3)) + (255,))
        letter = (_plain(name)[:1] or "?").upper()
        d.text((size / 2, size / 2), letter, font=_bold(int(size * 0.5)), fill=(255, 255, 255), anchor="mm")
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    return out


def collect_card(name: str, title: str, rarity: str, stats: list[tuple[str, int]], num: int, total: int,
                 quote: str = "", photo: bytes | None = None, seed: int = 0) -> Image.Image:
    """Карточка 600×880: рамка по редкости, аватарка, имя, титул, 4 характеристики 1–99, у легендарной — цитата."""
    w, h = 600, 880
    col, bg, stars = RARITY.get(rarity, RARITY["обычная"])
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=36, fill=col)
    d.rounded_rectangle((14, 14, w - 15, h - 15), radius=28, fill=bg)
    if rarity == "легендарная":  # золотые блики
        glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        for i in range(-3, 8):
            x = i * 110
            gd.polygon([(x, 0), (x + 40, 0), (x - 160, h), (x - 200, h)], fill=(255, 230, 150, 18))
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((14, 14, w - 15, h - 15), radius=28, fill=255)
        im.paste(Image.alpha_composite(im, glow), (0, 0), mask)
        d = ImageDraw.Draw(im)
    d.text((40, 44), f"№ {num:02d}/{max(total, num):02d}", font=_mono(24), fill=col)
    d.text((w - 40, 44), stars, font=_bold(30), fill=col, anchor="ra")
    ring = Image.new("RGBA", (270, 270), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse((0, 0, 269, 269), fill=col)
    im.alpha_composite(ring, ((w - 270) // 2, 96))
    im.alpha_composite(_avatar(name, 250, photo, seed), ((w - 250) // 2, 106))
    nm = _plain(name)
    size = 54
    while size > 30 and d.textlength(nm, font=_bold(size)) > w - 80:
        size -= 4
    d.text((w / 2, 402), nm, font=_bold(size), fill=TEXT, anchor="mm")
    if title:
        d.text((w / 2, 452), _wrap(d, _plain(title), _regular(28), w - 80, 1)[0], font=_regular(28), fill=col,
               anchor="mm")
    y = 500
    for label, val in stats[:4]:
        val = max(1, min(99, int(val)))
        d.text((44, y), label, font=_mono(22), fill=MUTED)
        d.text((w - 44, y), str(val), font=_bold(24), fill=TEXT, anchor="ra")
        d.rounded_rectangle((44, y + 32, w - 44, y + 44), radius=6, fill=(255, 255, 255, 28))
        d.rounded_rectangle((44, y + 32, 44 + max(12, int((w - 88) * val / 99)), y + 44), radius=6, fill=col)
        y += 64
    if quote:
        for i, line in enumerate(_wrap(d, f"«{_plain(quote)}»", _serif(24), w - 100, 2)):
            d.text((w / 2, y + 12 + i * 32), line, font=_serif(24), fill=(250, 236, 200), anchor="mm")
    d.text((w / 2, h - 44), rarity.upper(), font=_bold(24), fill=col, anchor="mm")
    return im


def render_collect_card(*args, **kwargs) -> bytes:
    card = collect_card(*args, **kwargs)
    img = Image.new("RGB", (card.width + 80, card.height + 80), BG)
    img.paste(card, (40, 40), card)
    return _png(img)


def render_album(owner: str, entries: list[dict], deck: int) -> bytes:
    """Альбом /collection: entries — [{"name","rarity","count","stats","num","photo"}] пойманные (лучшая редкость
    на человека), остальное до deck — пустые «?»."""
    w = 1000
    cw, ch, gap = 200, 293, 24
    slots = max(deck, len(entries))
    slots = min(max(slots, 4), 24)
    rows = (slots + 3) // 4
    h = 250 + rows * (ch + gap) + 40
    img = _noise(Image.new("RGB", (w, h), BG), 5)
    d = ImageDraw.Draw(img)
    d.text((PAD, 56), f"КОЛЛЕКЦИЯ · {_plain(owner).upper()}"[:40], font=_mono(28), fill=GOLD)
    d.text((PAD - 4, 96), f"{len(entries)} из {max(deck, len(entries))} карточек", font=_bold(60), fill=TEXT)
    by = {r: sum(1 for e in entries if e["rarity"] == r) for r in RARITY}
    dup = sum(max(0, e["count"] - 1) for e in entries)
    d.text((PAD, 176), f"легендарных {by['легендарная']} · редких {by['редкая']} · обычных {by['обычная']}"
                       + (f" · повторов {dup}" if dup else ""), font=_regular(28), fill=MUTED)
    x0 = (w - (4 * cw + 3 * gap)) // 2
    for i in range(slots):
        x, y = x0 + (i % 4) * (cw + gap), 250 + (i // 4) * (ch + gap)
        if i < len(entries):
            e = entries[i]
            c = collect_card(e["name"], "", e["rarity"], e["stats"], e["num"], deck, photo=e.get("photo"),
                             seed=e["num"]).resize((cw, ch), Image.LANCZOS)
            img.paste(c, (x, y), c)
            if e["count"] > 1:
                d.ellipse((x + cw - 56, y - 12, x + cw + 10, y + 54), fill=CLOWN_RED)
                d.text((x + cw - 23, y + 21), f"×{e['count']}", font=_bold(26), fill=(255, 255, 255), anchor="mm")
        else:
            d.rounded_rectangle((x, y, x + cw, y + ch), radius=14, outline=LINE, width=3, fill=(24, 24, 30))
            d.text((x + cw / 2, y + ch / 2), "?", font=_bold(90), fill=(60, 62, 70), anchor="mm")
    return _png(img)


# ---------- суд чата (handlers/court.py) ----------


def _stamp(text: str, size: int, color: tuple, angle: float) -> Image.Image:
    """Прямоугольная печать с потёртостями, повёрнутая."""
    import random

    f = _bold(size)
    tw = ImageDraw.Draw(Image.new("RGBA", (10, 10))).textlength(text, font=f)
    w, h = int(tw + size * 1.2), int(size * 1.6)
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((4, 4, w - 4, h - 4), radius=10, outline=color + (235,), width=max(4, size // 9))
    d.text((w / 2, h / 2), text, font=f, fill=color + (235,), anchor="mm")
    rnd = random.Random(3)
    px = im.load()
    for _ in range(w * h // 9):
        x, y = rnd.randrange(w), rnd.randrange(h)
        r, g, b, a = px[x, y]
        if a:
            px[x, y] = (r, g, b, int(a * rnd.uniform(0.15, 0.7)))
    return im.rotate(angle, expand=True, resample=Image.BICUBIC)


def render_verdict(case_no: int, date: str, name: str, charges: list[tuple[str, str]], guilty: bool, votes_for: int,
                   votes_against: int, lawyer: str, defense: str, nickname: str) -> bytes:
    """Приговор: дело №, подсудимый, статьи с цитатами, вердикт присяжных полоской, адвокат, наказание, печать."""
    w = 1000
    img = _noise(Image.new("RGB", (w, 2200), BG), 11)
    d = ImageDraw.Draw(img)
    d.text((PAD, 60), f"СУД ЧАТА · ДЕЛО № {case_no} · {date}", font=_mono(28), fill=CLOWN_RED)
    d.text((PAD - 4, 104), "ПРИГОВОР", font=_bold(84), fill=TEXT)
    d.text((PAD, 206), "ПОДСУДИМЫЙ", font=_mono(24), fill=MUTED)
    nm = _wrap(d, _plain(name), _bold(60), 470, 1)[0]
    d.text((PAD, 238), nm, font=_bold(60), fill=GOLD)
    y = 336
    box_top = y
    y += 70
    for title, quote in charges[:3]:
        for line in _wrap(d, _plain(title), _bold(32), w - 2 * PAD - 60, 2):
            d.text((PAD + 30, y), line, font=_bold(32), fill=TEXT)
            y += 44
        for line in _wrap(d, _plain(quote), _regular(30), w - 2 * PAD - 60, 3):
            d.text((PAD + 30, y), line, font=_regular(30), fill=(200, 200, 196))
            y += 40
        y += 22
    d.rounded_rectangle([PAD, box_top, w - PAD, y + 4], radius=20, outline=LINE, width=2)
    d.text((PAD + 30, box_top + 22), "СОСТАВ ПРЕСТУПЛЕНИЯ", font=_mono(24), fill=CLOWN_RED)
    y += 44
    d.text((PAD, y), "ВЕРДИКТ ПРИСЯЖНЫХ", font=_mono(24), fill=MUTED)
    y += 42
    bw = w - 2 * PAD
    total = votes_for + votes_against
    d.rounded_rectangle([PAD, y, PAD + bw, y + 34], radius=17, fill=(44, 46, 54))
    if total:
        d.rounded_rectangle([PAD, y, PAD + max(34, int(bw * votes_for / total)), y + 34], radius=17,
                            fill=CLOWN_RED if votes_for else (44, 46, 54))
    y += 48
    d.text((PAD, y), f"Виновен: {votes_for}", font=_bold(30), fill=CLOWN_RED)
    d.text((w - PAD, y), f"Оправдать: {votes_against}", font=_bold(30), fill=MUTED, anchor="ra")
    y += 70
    if lawyer:
        line = f"АДВОКАТ: {_plain(lawyer)}" + (f" · «{_plain(defense)}»" if defense else "")
        for part in _wrap(d, line, _regular(28), w - 2 * PAD, 2):
            d.text((PAD, y), part, font=_regular(28), fill=MUTED)
            y += 38
    else:
        d.text((PAD, y), "АДВОКАТ: не нашёлся - даже друзья не впряглись", font=_regular(28), fill=MUTED)
        y += 38
    y += 26
    if guilty:
        d.rounded_rectangle([PAD, y, w - PAD, y + 196], radius=20, fill=PANEL, outline=LINE, width=2)
        d.text((PAD + 30, y + 22), "НАКАЗАНИЕ · 24 ЧАСА", font=_mono(24), fill=GOLD)
        d.text((PAD + 30, y + 66), _wrap(d, f"Прозвище: «{_plain(nickname)}»", _bold(32), w - 2 * PAD - 60, 1)[0],
               font=_bold(32), fill=TEXT)
        face = _clown(48)
        img.paste(face, (PAD + 26, y + 108), face)
        d.text((PAD + 84, y + 116), "клоун на каждое сообщение", font=_bold(32), fill=TEXT)
        y += 230
    else:
        d.rounded_rectangle([PAD, y, w - PAD, y + 120], radius=20, fill=PANEL, outline=LINE, width=2)
        d.text((PAD + 30, y + 22), "РЕШЕНИЕ", font=_mono(24), fill=GOLD)
        d.text((PAD + 30, y + 64), "Оправдан. Пока что.", font=_bold(32), fill=TEXT)
        y += 154
    d.text((PAD, y), "СУДЬЯ: ПИЗДЮК · ОБЖАЛОВАНИЮ НЕ ПОДЛЕЖИТ", font=_mono(22), fill=MUTED)
    h = y + 70
    img = img.crop((0, 0, w, h)).convert("RGBA")
    st = _stamp("ВИНОВЕН" if guilty else "ОПРАВДАН", 74 if guilty else 64,
                CLOWN_RED if guilty else (80, 190, 120), 14)
    img.alpha_composite(st, (w - st.width - 40, 120))
    img = img.convert("RGB")
    _stripes(img, 0)
    _stripes(img, h - 16)
    return _png(img)


# ---------- протокол происшествия (handlers/protocol.py) ----------


def render_protocol(no: int, when: str, place: str, violator: str, victim: str, story: str, explanation: str,
                    witnesses: str, seized: str, article: str) -> bytes:
    """Бланк протокола: бумага, печатные поля, «машинописный» текст, подпись и синяя круглая печать."""
    import math
    import random

    from PIL import ImageFilter

    w, h = 1000, 2400
    paper = (243, 237, 222)
    img = Image.new("RGB", (w, h), paper)
    rnd = random.Random(no)
    px = img.load()
    for _ in range(w * h // 6):  # фактура бумаги
        x, y = rnd.randrange(w), rnd.randrange(h)
        v = rnd.randint(-10, 6)
        r, g, b = px[x, y]
        px[x, y] = (r + v, g + v, b + v)
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    d = ImageDraw.Draw(img)
    ink, blue, typed, label_col = (40, 38, 36), (40, 60, 150), (30, 30, 34), (90, 86, 80)
    d.text((w / 2, 70), f"ПРОТОКОЛ № {no:04d}", font=_serif(46, True), fill=ink, anchor="mm")
    d.text((w / 2, 116), "об административном правонарушении в чате", font=_serif(28), fill=ink, anchor="mm")
    d.line((70, 150, w - 70, 150), fill=ink, width=2)
    y = 178

    def field(label: str, value: str, y: int) -> int:
        if not value:
            return y
        d.text((70, y), label, font=_serif(24), fill=label_col)
        lx = 70 + d.textlength(label, font=_serif(24)) + 14
        lines = _wrap(d, _plain(value), _mono(26), w - 70 - lx, 3)
        for i, line in enumerate(lines):
            yy = y + i * 46
            d.text((lx, yy - 2), line, font=_mono(26), fill=typed)
            d.line((lx, yy + 32, w - 70, yy + 32), fill=(150, 144, 134), width=1)
        return y + 46 * len(lines) + 14

    y = field("Дата и время:", when, y)
    y = field("Место:", place, y)
    y = field("Нарушитель:", violator, y)
    y = field("Потерпевший:", victim, y)
    y += 10
    d.text((70, y), "Обстоятельства:", font=_serif(24), fill=label_col)
    y += 40
    for line in _wrap(d, _plain(story), _mono(26), w - 140, 14):
        d.text((70, y), line, font=_mono(26), fill=typed)
        y += 40
    y += 20
    y = field("Объяснение нарушителя:", f"«{_plain(explanation)}»" if explanation else "", y)
    y = field("Свидетели:", witnesses, y)
    y = field("Изъято:", seized, y)
    y = field("Квалификация:", article, y)
    y += 40
    d.text((70, y), "Протокол составил:", font=_serif(24), fill=label_col)
    d.text((70, y + 40), "ст. лейтенант Пиздюк", font=_mono(28), fill=typed)
    d.line((560, y + 70, 900, y + 70), fill=ink, width=1)
    pts = [(580 + i * 4, y + 56 - 18 * math.sin(i / 5) - (i % 7)) for i in range(70)]
    d.line(pts, fill=blue, width=3)
    d.text((730, y + 80), "(подпись)", font=_serif(18), fill=(120, 116, 110), anchor="ma")
    y += 130
    d.text((70, y), "С протоколом ознакомлен, от подписи отказался.", font=_serif(22), fill=label_col)
    h = y + 150  # место под печать: она ставится ниже статьи, на подпись (раньше налезала на «Квалификацию»)
    img = img.crop((0, 0, w, h)).convert("RGBA")
    s = 300
    st = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    sd = ImageDraw.Draw(st)
    sd.ellipse((6, 6, s - 6, s - 6), outline=blue + (200,), width=6)
    sd.ellipse((40, 40, s - 40, s - 40), outline=blue + (200,), width=3)
    ring = "ДЕЖУРНАЯ ЧАСТЬ ЧАТА · ВОЛГОГРАД · "
    for i, ch in enumerate(ring):
        a = 2 * math.pi * i / len(ring) - math.pi / 2
        cx, cy = s / 2 + 118 * math.cos(a), s / 2 + 118 * math.sin(a)
        g = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
        ImageDraw.Draw(g).text((20, 20), ch, font=_bold(22), fill=blue + (210,), anchor="mm")
        g = g.rotate(-math.degrees(a) - 90, resample=Image.BICUBIC)
        st.alpha_composite(g, (int(cx - 20), int(cy - 20)))
    sd.text((s / 2, s / 2 - 16), "ДЛЯ", font=_bold(30), fill=blue + (210,), anchor="mm")
    sd.text((s / 2, s / 2 + 22), "СРАЧЕЙ", font=_bold(34), fill=blue + (210,), anchor="mm")
    st = st.rotate(-12, resample=Image.BICUBIC)
    img.alpha_composite(st, (w - 340, h - st.height - 10))
    return _png(img.convert("RGB"))


def render_diss_cover(name: str, title: str, genre: str, photo: bytes | None, seed: int = 0, side: int = 320) -> bytes:
    """Обложка дисс-трека (значок у аудио в Telegram — до 320×320, JPEG до 200 КБ): тёмный фон с красным
    свечением, аватарка героя, «ДИСС», название, плашка «ОСТОРОЖНО: МАТ»."""
    s = 1000
    img = Image.new("RGB", (s, s), (12, 10, 14))
    d = ImageDraw.Draw(img)
    for r in range(700, 0, -10):  # красное свечение за аватаркой
        t = r / 700
        d.ellipse((s / 2 - r, 330 - r, s / 2 + r, 330 + r),
                  fill=(int(12 + 150 * (1 - t) ** 2), int(10 + 16 * (1 - t) ** 2), int(14 + 24 * (1 - t) ** 2)))
    img = _noise(img, seed or 7)
    av = _avatar(name, 360, photo, seed)
    ring = Image.new("RGBA", (380, 380), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse((0, 0, 379, 379), fill=(245, 240, 235, 255))
    img.paste(ring, (s // 2 - 190, 70), ring)
    img.paste(av, (s // 2 - 180, 80), av)
    stamp = _stamp("ДИСС", 130, CLOWN_RED, -7)
    img.paste(stamp, (s // 2 - stamp.width // 2, 400), stamp)
    d = ImageDraw.Draw(img)
    y = 400 + stamp.height + 10
    size = 72
    t = _plain(title).upper()
    while size > 40 and d.textlength(t, font=_bold(size)) > s - 2 * PAD:
        size -= 4
    d.text((s / 2, y + size / 2), _wrap(d, t, _bold(size), s - 2 * PAD, 1)[0], font=_bold(size), fill=TEXT,
           anchor="mm")
    d.text((s / 2, y + size + 46), _wrap(d, f"ПИЗДЮК · {genre.upper()} · НА {_plain(name).upper()}", _mono(30),
                                         s - 2 * PAD, 1)[0], font=_mono(30), fill=GOLD, anchor="mm")
    label = "ОСТОРОЖНО: МАТ"
    lw = int(d.textlength(label, font=_bold(28))) + 44
    x1, y1 = s - 50, s - 44
    d.rectangle((x1 - lw, y1 - 64, x1, y1), fill=(245, 240, 235))
    d.rectangle((x1 - lw + 6, y1 - 58, x1 - 6, y1 - 6), outline=(12, 10, 14), width=3)
    d.text((x1 - lw / 2, y1 - 32), label, font=_bold(28), fill=(12, 10, 14), anchor="mm")
    out = io.BytesIO()
    img.resize((side, side), Image.LANCZOS).save(out, "JPEG", quality=88)
    return out.getvalue()


def render_news(date: str, issue: int, headline: str, ticker: list[str]) -> bytes:
    """Заставка «Новостей чата»: экран эфира — плашка «НОВОСТИ ЧАТА», «ЭФИР 21:00», главный заголовок крупно,
    внизу бегущая строка с остальными сюжетами. Плашки — по ширине текста (первая версия: «21:00» вылезало)."""
    w, h = 1000, 600
    img = Image.new("RGB", (w, h), (10, 18, 40))
    d = ImageDraw.Draw(img)
    for i in range(h):  # синий студийный градиент
        t = i / h
        d.line((0, i, w, i), fill=(int(14 + 20 * (1 - t)), int(26 + 34 * (1 - t)), int(58 + 60 * (1 - t))))
    for x in range(-h, w, 46):  # лёгкие диагонали «студии»
        d.line((x, 0, x + h, h), fill=(30, 48, 92), width=2)
    img = _noise(img, issue)
    d = ImageDraw.Draw(img)
    dark, light = (14, 22, 44), (245, 240, 235)
    # шапка: [НОВОСТИ ЧАТА][● ЭФИР 21:00]
    x, y0, y1 = PAD, 50, 116
    t1 = "НОВОСТИ ЧАТА"
    w1 = int(d.textlength(t1, font=_bold(44))) + 44
    d.rectangle((x, y0, x + w1, y1), fill=CLOWN_RED)
    d.text((x + 22, (y0 + y1) / 2), t1, font=_bold(44), fill=(255, 255, 255), anchor="lm")
    x += w1
    t2 = "ЭФИР 21:00"
    w2 = int(d.textlength(t2, font=_bold(30))) + 22 + 18 + 14 + 24
    d.rectangle((x, y0, x + w2, y1), fill=light)
    d.ellipse((x + 22, 74, x + 40, 92), fill=CLOWN_RED)
    d.text((x + 54, (y0 + y1) / 2), t2, font=_bold(30), fill=dark, anchor="lm")
    d.text((PAD, 150), f"{date} · ВЫПУСК № {issue}", font=_mono(26), fill=(200, 210, 235), anchor="lm")
    # главный заголовок — по центру свободного места между шапкой и бегущей строкой
    tick_top = h - 96
    size = 64
    t = _plain(headline).upper()
    while size > 38 and len(_wrap(d, t, _bold(size), w - 2 * PAD, 4)) > 3:
        size -= 4
    lines = _wrap(d, t, _bold(size), w - 2 * PAD, 3)
    block = len(lines) * int(size * 1.18) + 30
    y = max(190, 180 + (tick_top - 180 - block) // 2)
    for line in lines:
        d.text((PAD, y), line, font=_bold(size), fill=(255, 255, 255))
        y += int(size * 1.18)
    d.rectangle((PAD, y + 14, PAD + 120, y + 22), fill=CLOWN_RED)
    # бегущая строка: [СРОЧНО] заголовок • заголовок • …
    d.rectangle((0, tick_top, w, tick_top + 62), fill=light)
    t3 = "СРОЧНО"
    w3 = int(d.textlength(t3, font=_bold(28))) + 2 * PAD
    d.rectangle((0, tick_top, w3, tick_top + 62), fill=CLOWN_RED)
    d.text((w3 / 2, tick_top + 31), t3, font=_bold(28), fill=(255, 255, 255), anchor="mm")
    run = "  •  ".join(_plain(x) for x in ticker) or "в чате всё спокойно, подозрительно спокойно"
    d.text((w3 + 20, tick_top + 31), _wrap(d, run, _bold(26), w - w3 - 40, 1)[0], font=_bold(26), fill=dark,
           anchor="lm")
    return _png(img)


# ---------- «Письма Волгограда»: обложка-улика к выпуску ----------
# Идея владельца №19 (09.11): к голосовому в канале — картинка-улика по формату выпуска: конверт со штемпелем,
# страница дневника в клетку, кассета от диктофона или бланк объяснения. Рисуем сами (Pillow), без ИИ и бесплатно.

_BONE, _BLOOD, _INK, _PENCIL = (226, 218, 200), (150, 22, 26), (28, 38, 92), (60, 60, 66)


def _paper(w: int, h: int, color: tuple, seed: int) -> Image.Image:
    """Лист бумаги: цвет, зерно, потемневшие края и пара пятен."""
    import random

    from PIL import ImageFilter

    rnd = random.Random(seed)
    img = Image.new("RGB", (w, h), color)
    img = Image.blend(img, Image.effect_noise((w, h), 40).convert("RGB"), 0.06)
    edge = Image.new("L", (w, h), 0)
    ImageDraw.Draw(edge).rectangle([18, 18, w - 18, h - 18], fill=255)
    edge = edge.filter(ImageFilter.GaussianBlur(26))
    dark = Image.new("RGB", (w, h), tuple(int(c * 0.72) for c in color))
    img = Image.composite(img, dark, edge)
    stains = Image.new("L", (w, h), 0)
    sd = ImageDraw.Draw(stains)
    for _ in range(3):
        x, y, r = rnd.randrange(w), rnd.randrange(h), rnd.randrange(30, 90)
        sd.ellipse([x - r, y - r * 0.7, x + r, y + r * 0.7], outline=70, width=3)
        sd.ellipse([x - r + 6, y - r * 0.7 + 5, x + r - 6, y + r * 0.7 - 5], fill=18)
    stains = stains.filter(ImageFilter.GaussianBlur(3))
    return Image.composite(Image.new("RGB", (w, h), tuple(int(c * 0.8) for c in color)), img, stains)


def _handf(size: int):
    """Рукописный шрифт (Caveat, кириллица, OFL): «почерк» на уликах. Раньше был засечный шрифт, наклонённый
    сдвигом, и каракули-петли вместо строк — владелец 09.11: «какие-то кривые»."""
    try:
        return _font("Caveat-SemiBold.ttf", size)
    except OSError:
        return _serif(size)


def _write(d: ImageDraw.ImageDraw, x: int, base: int, text: str, size: int, color: tuple, width: int,
           max_lines: int, step: int) -> int:
    """Строки «от руки» по линейке: base — линия строки (буквы стоят на ней), step — шаг линеек.
    Не влезает в max_lines — почерк мельче (до 3/4), потом уже «…». Возвращает линию следующей строки."""
    for sz in range(size, int(size * 0.75) - 1, -2):
        f = _handf(sz)
        if len(_wrap(d, text, f, width, 99)) <= max_lines:
            break
    for line in _wrap(d, text, f, width, max_lines):
        d.text((x, base), line, font=f, fill=color, anchor="ls")
        base += step
    return base


def _fingerprint(size: int, color: tuple, seed: int) -> Image.Image:
    """Отпечаток пальца: овал из вложенных гребней-завитков с разрывами, пропечатан неровно."""
    import random

    from PIL import ImageFilter

    rnd = random.Random(seed)
    w, h = size, int(size * 1.3)
    im = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(im)
    cx, cy = w / 2, h * 0.55
    for i in range(1, 22):
        rx, ry = i * w / 44, i * h / 46
        a = rnd.uniform(0, 360)
        while a < 360 + 300:
            seg = rnd.uniform(40, 160)
            d.arc([cx - rx, cy - ry, cx + rx, cy + ry], a, a + seg, fill=rnd.randrange(120, 220), width=2)
            a += seg + rnd.uniform(6, 28)
    oval = Image.new("L", (w, h), 0)
    ImageDraw.Draw(oval).ellipse([2, 2, w - 2, h - 2], fill=255)
    oval = oval.filter(ImageFilter.GaussianBlur(size / 14))
    alpha = Image.composite(im, Image.new("L", (w, h), 0), oval).point(lambda v: int(v * 0.55))
    out = Image.new("RGBA", (w, h), color + (255,))
    out.putalpha(alpha)
    return out.rotate(rnd.uniform(-12, 12), expand=True, resample=Image.BICUBIC)


def _postmark(size: int, date: str, color: tuple) -> Image.Image:
    """Почтовый штемпель: двойной круг, «ВОЛГОГРАД», дата; волнистые линии гашения справа (ложатся на марку)."""
    import math

    wave = int(size * 1.15)
    im = Image.new("RGBA", (size + wave, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    c = color + (190,)
    r = size // 2 - 3
    d.ellipse([3, 3, 3 + 2 * r, 3 + 2 * r], outline=c, width=4)
    d.ellipse([3 + r * 0.16, 3 + r * 0.16, 3 + r * 1.84, 3 + r * 1.84], outline=c, width=2)
    d.text((3 + r, 3 + r * 0.62), "ВОЛГОГРАД", font=_bold(int(size * 0.095)), fill=c, anchor="mm")
    d.text((3 + r, 3 + r * 1.02), date, font=_mono(int(size * 0.14)), fill=c, anchor="mm")
    d.text((3 + r, 3 + r * 1.4), "400000", font=_mono(int(size * 0.1)), fill=c, anchor="mm")
    for k in range(4):
        yy = size * (0.29 + 0.14 * k)
        d.line([(2 * r + 10 + t, yy + math.sin(t / 13) * 6) for t in range(0, wave - 8, 3)], fill=c, width=3)
    return im


def _stamp_art(w: int, h: int) -> Image.Image:
    """Марка: зубчатый край, ночной двор — дома, одно горящее окно, луна."""
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, w, h], fill=(244, 238, 222, 255))
    for x in range(0, w + 1, 14):  # зубцы
        d.ellipse([x - 5, -5, x + 5, 5], fill=(0, 0, 0, 0))
        d.ellipse([x - 5, h - 5, x + 5, h + 5], fill=(0, 0, 0, 0))
    for y in range(0, h + 1, 14):
        d.ellipse([-5, y - 5, 5, y + 5], fill=(0, 0, 0, 0))
        d.ellipse([w - 5, y - 5, w + 5, y + 5], fill=(0, 0, 0, 0))
    m = 14
    d.rectangle([m, m, w - m, h - m], fill=(32, 40, 66, 255))
    d.ellipse([w * 0.6, h * 0.16, w * 0.6 + w * 0.2, h * 0.16 + w * 0.2], fill=(236, 226, 190, 255))
    base = h - m
    for x0, x1, top in ((m, w * 0.42, h * 0.45), (w * 0.38, w * 0.7, h * 0.58), (w * 0.66, w - m, h * 0.5)):
        d.rectangle([x0, top, x1, base], fill=(14, 16, 24, 255))
    d.rectangle([w * 0.24, h * 0.56, w * 0.3, h * 0.62], fill=(236, 196, 90, 255))  # горящее окно
    d.text((w / 2, h - m - 12), "ПОЧТА", font=_bold(13), fill=(200, 200, 210, 255), anchor="mm")
    return im


def _envelope(subject: str, sender: str, date: str, seed: int) -> Image.Image:
    """Конверт: слева вверху «от кого», справа марка с гашением, ниже «куда», тема красными чернилами,
    внизу индекс. Ничто ни на что не налезает."""
    w, h = 860, 620
    img = _paper(w, h, (206, 190, 156), seed)
    d = ImageDraw.Draw(img)
    label, rule = (92, 78, 62), (140, 124, 104)
    stamp = _stamp_art(140, 170)
    img.paste(stamp, (w - 184, 34), stamp)
    pm = _postmark(150, date, (40, 40, 60))
    img.paste(pm, (w - 184 - 150 - 14, 110), pm)
    d.text((48, 84), "От кого:", font=_serif(22), fill=label, anchor="ls")
    _write(d, 150, 84, sender, 42, _INK, 480, 1, 0)
    d.line([(146, 92), (640, 92)], fill=rule, width=2)
    d.text((48, 334), "Куда:", font=_serif(22), fill=label, anchor="ls")
    for k, line in enumerate(("Радио Волгограда,", "рубрика «Письма Волгограда»")):
        base = 334 + k * 62
        _write(d, 140, base, line, 46, _INK, w - 200, 1, 0)
        d.line([(136, base + 8), (w - 48, base + 8)], fill=rule, width=2)
    d.text((48, 482), "Тема:", font=_serif(22), fill=label, anchor="ls")
    _write(d, 140, 482, f"«{subject}»", 44, _BLOOD, w - 250, 2, 50)
    for k, ch in enumerate("400066"):  # индекс: клетки внизу слева
        x = 48 + k * 34
        d.rectangle([x, h - 64, x + 24, h - 26], outline=rule, width=2)
        d.text((x + 12, h - 45), ch, font=_mono(24), fill=_INK, anchor="mm")
    fp = _fingerprint(80, (92, 30, 24), seed)
    img.paste(fp, (w - 112, h - 124), fp)
    return img


# Строки дневника — общие, без сюжета выпуска (и без рода: «проверила/проверил»)
_DIARY_LINES = ["Опять этот звук в три часа ночи.", "Соседи ничего не слышали.", "Замок проверен. Четыре раза.",
                "Свет в подъезде снова не горит.", "В глазок смотреть нельзя.", "Телефон опять был тёплый.",
                "Завтра сходить к участковому.", "Это было не во сне.", "Шаги сверху. Там никто не живёт.",
                "Утром коврик лежал по-другому.", "Записывать всё. Каждую ночь.", "Звонок в дверь. Никого."]


def _diary(subject: str, date_words: str, seed: int) -> Image.Image:
    """Страница в клетку: дата, тема крупно, несколько записей — буквы стоят на линиях клетки; одна запись
    зачёркнута, внизу красным «Никому не открывать.» с двойным подчёркиванием."""
    import random

    rnd = random.Random(seed)
    w, h, cell = 800, 720, 30
    img = _paper(w, h, (236, 232, 218), seed)
    d = ImageDraw.Draw(img)
    for x in range(0, w, cell):
        d.line([(x, 0), (x, h)], fill=(180, 198, 222), width=1)
    for y in range(0, h, cell):
        d.line([(0, y), (w, y)], fill=(180, 198, 222), width=1)
    d.line([(96, 0), (96, h)], fill=(214, 90, 90), width=3)
    x, width, step = 120, w - 170, 2 * cell
    base = _write(d, x, 2 * step - cell, date_words[:1].upper() + date_words[1:] + ".", 44, _INK, width, 1, step)
    base = _write(d, x, base, subject[:1].upper() + subject[1:] + ".", 54, _INK, width, 3, step)
    last = h - 2 * step
    lines = rnd.sample(_DIARY_LINES, 6)
    cross = rnd.randrange(2)
    f = _handf(44)
    k = 0
    while base <= last - step and k < len(lines):
        d.text((x, base), lines[k], font=f, fill=_INK, anchor="ls")
        if k == cross:  # зачёркнута: ровная линия по середине букв
            tw = d.textlength(lines[k], font=f)
            d.line([(x - 4, base - 11), (x + tw + 4, base - 11)], fill=_INK, width=3)
        base += step
        k += 1
    red = "Никому не открывать."
    tw = d.textlength(red, font=_handf(50))
    d.text((x, last), red, font=_handf(50), fill=_BLOOD, anchor="ls")
    d.line([(x, last + 7), (x + tw, last + 7)], fill=_BLOOD, width=3)
    d.line([(x + 6, last + 14), (x + tw - 4, last + 14)], fill=_BLOOD, width=3)
    fp = _fingerprint(78, (92, 30, 24), seed + 1)
    img.paste(fp, (w - 140, h - 140), fp)
    return img


def _cassette(subject: str, n: int, date: str, seed: int) -> Image.Image:
    """Кассета: наклейка «ЗАПИСЬ №N» — тема от руки на линейке, дата; окошко с катушками и плёнкой."""
    import math

    w, h = 860, 540
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=36, fill=255)
    body = Image.new("RGB", (w, h), (34, 34, 38))
    body = Image.blend(body, Image.effect_noise((w, h), 50).convert("RGB"), 0.05)
    d = ImageDraw.Draw(body)
    for cx, cy in ((26, 26), (w - 26, 26), (26, h - 26), (w - 26, h - 26), (w / 2, h - 30)):
        d.ellipse([cx - 9, cy - 9, cx + 9, cy + 9], fill=(90, 90, 96))
        d.line([(cx - 6, cy), (cx + 6, cy)], fill=(40, 40, 44), width=3)
    lw, lh = w - 120, 310
    label = _paper(lw, lh, (232, 224, 200), seed)
    ld = ImageDraw.Draw(label)
    ld.rectangle([0, 0, lw, 46], fill=(150, 22, 26))
    ld.text((24, 23), f"ЗАПИСЬ №{n}", font=_bold(28), fill=(240, 232, 214), anchor="lm")
    ld.text((lw - 24, 23), "A", font=_bold(30), fill=(240, 232, 214), anchor="rm")
    ld.text((lw - 64, 23), date, font=_mono(22), fill=(240, 232, 214), anchor="rm")
    for base in (100, 150):
        ld.line([(24, base + 8), (lw - 24, base + 8)], fill=(170, 160, 140), width=2)
    if _write(ld, 30, 100, subject[:1].upper() + subject[1:], 46, _INK, lw - 70, 2, 50) == 150:
        _write(ld, 30, 150, "не слушать одному", 40, _BLOOD, lw - 70, 1, 0)  # тема в одну строку — приписка
    win_y, wx0, wx1 = 186, 120, lw - 120
    ld.rounded_rectangle([wx0, win_y, wx1, win_y + 100], radius=50, fill=(30, 26, 24))
    ld.rectangle([wx0 + 110, win_y + 32, wx1 - 110, win_y + 68], fill=(70, 46, 30))  # плёнка в окошке
    for cx in (wx0 + 52, wx1 - 52):  # катушки
        cy = win_y + 50
        ld.ellipse([cx - 40, cy - 40, cx + 40, cy + 40], fill=(226, 222, 214))
        ld.ellipse([cx - 18, cy - 18, cx + 18, cy + 18], fill=(30, 26, 24))
        for k in range(6):
            a = k * math.pi / 3
            ld.line([(cx + 17 * math.cos(a), cy + 17 * math.sin(a)), (cx + 26 * math.cos(a), cy + 26 * math.sin(a))],
                    fill=(226, 222, 214), width=5)
    body.paste(label, (60, 40))
    d.polygon([(150, h), (196, h - 104), (w - 196, h - 104), (w - 150, h)], fill=(26, 26, 30))
    for x in (300, w - 300):
        d.ellipse([x - 14, h - 68, x + 14, h - 40], fill=(12, 12, 14))
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(body, (0, 0), mask)
    return out


# «Объяснение»: строки от руки — без рода и без сюжета; часть слов закрашена, как в выданной копии
_STATEMENT_LINES = ["Около 23:00 в дверь позвонили три раза.", "В глазок на площадке никого не было.",
                    "Звонки повторялись до утра, каждый час.", "Соседи сказали, что ничего не слышали.",
                    "Утром на двери нашли царапины изнутри.", "Подробнее пояснить ничего не могу."]


def _statement(subject: str, date: str, seed: int) -> Image.Image:
    """Бланк объяснения: шапка машинописью, текст от руки по линейкам, часть слов закрашена; печать «КОПИЯ»
    — в пустом месте у подписи."""
    import random

    rnd = random.Random(seed)
    w, h = 760, 860
    img = _paper(w, h, (238, 236, 228), seed)
    d = ImageDraw.Draw(img)
    typed = (40, 40, 44)
    d.text((w / 2, 70), "ОБЪЯСНЕНИЕ", font=_bold(40), fill=(30, 30, 34), anchor="mm")
    d.text((60, 130), "г. Волгоград", font=_mono(22), fill=typed)
    d.text((w - 60, 130), date, font=_mono(22), fill=typed, anchor="ra")
    d.text((60, 190), "Я, ______________________, по существу", font=_mono(22), fill=typed)
    d.text((60, 226), "заданных мне вопросов поясняю следующее:", font=_mono(22), fill=typed)
    step, x, width = 52, 66, w - 132
    rules = [300 + k * step for k in range(8)]
    for base in rules:
        d.line([(60, base + 8), (w - 60, base + 8)], fill=(176, 176, 182), width=1)
    base = _write(d, x, rules[0], subject[:1].upper() + subject[1:] + ".", 42, _INK, width, 2, step)
    f = _handf(40)
    keep = sorted(rnd.sample(range(len(_STATEMENT_LINES) - 1), 4))  # по порядку событий, конец — всегда
    lines = [_STATEMENT_LINES[i] for i in keep] + [_STATEMENT_LINES[-1]]
    hide = set(rnd.sample(range(4), 2))
    for k, line in enumerate(lines):
        if base > rules[-1]:
            break
        line = _wrap(d, line, f, width, 1)[0]
        d.text((x, base), line, font=f, fill=_INK, anchor="ls")
        words = line.split()
        long = [i for i, wd in enumerate(words) if i and len(wd.strip(".,")) >= 5]
        if k in hide and long:  # закрашено маркером: одно длинное слово (не первое), полосой по высоте букв
            i = rnd.choice(long)
            x0 = x + d.textlength(" ".join(words[:i]) + " ", font=f)
            x1 = x0 + d.textlength(words[i].rstrip(".,"), font=f)
            d.rectangle([x0 - 1, base - 24, x1 + 3, base + 5], fill=(22, 22, 26))
        base += step
    d.text((60, h - 150), "С моих слов записано верно, мною прочитано.", font=_mono(20), fill=typed)
    d.text((60, h - 86), "Подпись ____________", font=_mono(20), fill=typed)
    stamp = _stamp("КОПИЯ", 44, (150, 22, 26), 5)
    img.paste(stamp, (w - stamp.width - 46, h - stamp.height - 22), stamp)
    return img


def render_horror_cover(fmt: str, n: int, subject: str, date: str, date_words: str = "", sender: str = "",
                        seed: int = 0) -> bytes:
    """Обложка выпуска «Писем Волгограда» 1080×1080 (JPEG): улика по формату — «письмо» конверт со штемпелем,
    «дневник» страница в клетку, «диктофон» кассета, «показания» бланк объяснения; сверху — рубрика и номер выпуска,
    снизу — радио и дата. date — «09.11.26», date_words — «девятое ноября», sender — от кого (письмо).
    Улика лежит РОВНО (с поворотом владелец назвал обложки «кривыми»)."""
    import random

    from PIL import ImageFilter

    side = 1080
    rnd = random.Random(seed or n)
    subject = _plain(subject or "без темы").strip()
    if len(subject) > 70:  # длинная тема — по целому слову и «…» (раньше резало посреди слова)
        subject = subject[:70].rsplit(" ", 1)[0].rstrip(",.;:-") + "…"
    img = Image.new("RGB", (side, side), (20, 16, 14))
    grain = Image.effect_noise((side, side), 70).convert("RGB")
    img = Image.blend(img, grain, 0.05)
    light = Image.new("L", (side, side), 0)
    ImageDraw.Draw(light).ellipse([-200, -260, side * 0.95, side * 0.9], fill=90)
    light = light.filter(ImageFilter.GaussianBlur(160))
    img = Image.composite(Image.new("RGB", (side, side), (64, 54, 46)), img, light)
    if fmt == "дневник":
        obj = _diary(subject, date_words or date, rnd.randrange(999)).convert("RGBA")
    elif fmt == "диктофон":
        obj = _cassette(subject, n, date, rnd.randrange(999))
    elif fmt == "показания":
        obj = _statement(subject, date, rnd.randrange(999)).convert("RGBA")
    else:
        obj = _envelope(subject, sender or "без обратного адреса", date, rnd.randrange(999)).convert("RGBA")
    scale = min(900 / obj.width, 800 / obj.height, 1.0)
    if scale < 1:
        obj = obj.resize((int(obj.width * scale), int(obj.height * scale)), Image.LANCZOS)
    x, y = (side - obj.width) // 2, 150 + (800 - obj.height) // 2
    shadow = Image.new("L", (side, side), 0)
    shadow.paste(obj.getchannel("A").point(lambda a: 170 if a > 10 else 0), (x + 12, y + 18))
    shadow = shadow.filter(ImageFilter.GaussianBlur(20))
    img = Image.composite(Image.new("RGB", (side, side), (4, 3, 3)), img, shadow)
    img.paste(obj, (x, y), obj)
    d = ImageDraw.Draw(img)
    d.text((side / 2, 62), "ПИСЬМА ВОЛГОГРАДА", font=_serif(64, True), fill=_BONE, anchor="mm")
    d.text((side / 2, 118), f"ВЫПУСК №{n}", font=_bold(30), fill=(196, 40, 44), anchor="mm")
    d.text((side / 2, side - 46), f"РАДИО ВОЛГОГРАДА · {date}", font=_bold(26), fill=(150, 140, 126), anchor="mm")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()
