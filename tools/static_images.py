"""Рисует постоянные картинки бота — один раз, бот потом шлёт готовые файлы:
  * assets/menu/*.jpg — /menu: главная и по одной на каждый раздел (из SECTIONS в handlers/menu.py);
  * assets/lobby/*.jpg — карточки лобби Бункера, Мафии и Нефоров;
  * assets/mafia/*.jpg — фазы Мафии: night_N, morning_kill_N / morning_safe_N, day_N, vote_N (N = 1..10).

Запускать вручную, когда что-то из этого меняется:  python tools/static_images.py
Нужен цветной шрифт эмодзи (Noto Color Emoji: пакет fonts-noto-color-emoji). На сервере он не нужен —
бот отправляет уже готовые картинки.
"""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import math  # noqa: E402
import random  # noqa: E402

from PIL import Image, ImageDraw, ImageFilter, ImageFont  # noqa: E402

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
    cols, th, gap, gy = 3, 210, 28, 236
    rows = math.ceil(len(ORDER) / cols)
    h = max(H, gy + rows * th + (rows - 1) * gap + 56)  # разделов больше шести — картинка выше
    img = _noise(Image.new("RGB", (W, h), BG), 7)
    d = ImageDraw.Draw(img)
    _strip(d, YELLOW, RED)
    d.text((64, 52), "ПИЗДЮК", font=_bold(92), fill=TEXT)
    d.text((70, 160), "МЕНЮ БОТА · ВЫБЕРИ РАЗДЕЛ", font=_mono(28), fill=MUTED)
    face = emoji("😈", 104)
    img.paste(face, (W - 64 - face.width, 44), face)
    d = ImageDraw.Draw(img)
    gx = 64
    tw = (W - 2 * gx - gap * (cols - 1)) // cols
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
    img = _noise(Image.new("RGB", (W, H), BG), sum(map(ord, key)))  # стабильный сид: без лишних диффов
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


# ---------- карточки лобби ----------

LOBBY = ROOT / "assets" / "lobby"
LW, LH = 1280, 640


def _chips(img: Image.Image, steps: list[str], color, y: int) -> None:
    """Шаги партии плашками в ряд: «1 ВСТУПИ», «2 …»."""
    d = ImageDraw.Draw(img)
    font = _mono(26)
    x = 64
    for i, step in enumerate(steps, 1):
        label = f"{i}  {step}"
        w = int(d.textlength(label, font=font)) + 44
        d.rounded_rectangle([x, y, x + w, y + 58], radius=14, fill=PANEL, outline=color, width=2)
        d.text((x + 22, y + 29), label, font=font, fill=TEXT, anchor="lm")
        x += w + 18


def lobby_bunker() -> Image.Image:
    from games.bunker.render import ACCENT, _canvas

    img = _canvas(LW, LH, 42)
    d = ImageDraw.Draw(img)
    cx, cy, r = 1280 - 64 - 170, 250, 170
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=PANEL, outline=ACCENT, width=8)
    d.text((cx, cy + 8), "☢", font=_regular(230), fill=ACCENT, anchor="mm")
    d.text((64, 84), "СБОР ВЫЖИВШИХ · ЛОББИ", font=_mono(30), fill=ACCENT)
    d.text((58, 120), "БУНКЕР", font=_bold(132), fill=TEXT)
    for k, line in enumerate(_wrap(d, "Мест меньше, чем людей. Решайте, кого оставить за дверью.",
                                   _regular(34), 720, 2)):
        d.text((64, 290 + k * 46), line, font=_regular(34), fill=MUTED)
    _chips(img, ["ВСТУПИ", "КАРТОЧКА В ЛС", "ВЫЖИВИ"], ACCENT, 470)
    return img


def lobby_mafia() -> Image.Image:
    from games.mafia.render import RED as M_RED
    from games.mafia.render import TEXT as M_TEXT
    from games.mafia.render import _canvas as m_canvas
    from games.mafia.render import icon

    img = m_canvas(LW, LH, 17, M_RED)
    d = ImageDraw.Draw(img)
    cx, cy, r = 1280 - 64 - 170, 280, 170
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=PANEL, outline=M_RED, width=8)
    hat = icon("mafia", 230, M_TEXT)
    img.paste(hat, (cx - hat.width // 2, cy - hat.height // 2), hat)
    d = ImageDraw.Draw(img)
    d.text((64, 84), "СБОР ГОРОДА · ЛОББИ", font=_mono(30), fill=M_RED)
    d.text((58, 120), "МАФИЯ", font=_bold(132), fill=TEXT)
    for k, line in enumerate(_wrap(d, "Днём все соседи. Ночью кто-то из вас точит нож.", _regular(34), 720, 2)):
        d.text((64, 290 + k * 46), line, font=_regular(34), fill=MUTED)
    _chips(img, ["ВСТУПИ", "РОЛЬ В ЛС", "ВЫЧИСЛИ МАФИЮ"], M_RED, 470)
    return img


def lobby_nefor() -> Image.Image:
    from games.nefor.render import ACID, BG as N_BG

    img = _noise(Image.new("RGB", (LW, LH), N_BG), 23)
    d = ImageDraw.Draw(img)
    for x in range(0, LW, 40):  # кислотная «изолента» сверху и снизу
        col = ACID if (x // 40) % 2 == 0 else (24, 24, 28)
        d.rectangle([x, 0, x + 40, 14], fill=col)
        d.rectangle([x, LH - 14, x + 40, LH], fill=col)
    cx, cy, r = LW - 64 - 170, 280, 170
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=PANEL, outline=ACID, width=8)
    _paste_center(img, emoji("🎸", 190), cx, cy)
    d = ImageDraw.Draw(img)
    d.text((64, 84), "ВОЛГОГРАД · ЛОББИ", font=_mono(30), fill=ACID)
    d.text((58, 120), "НЕФОРЫ", font=_bold(132), fill=TEXT)
    for k, line in enumerate(_wrap(d, "Тусовки делят город: ЦР, Мармелад, Ткачёвский рынок.",
                                   _regular(34), 720, 2)):
        d.text((64, 290 + k * 46), line, font=_regular(34), fill=MUTED)
    _chips(img, ["ВСТУПИ", "ЗАЙМИ ТОЧКИ", "ЗАБЕРИ ГОРОД"], ACID, 470)
    return img


# ---------- фазы Мафии: ночь / утро (убили или все живы) / день / голосование, с номером ----------

PHASES = ROOT / "assets" / "mafia"
PHASE_MAX = 10   # дальше бот шлёт фазу текстом
P_RED = (206, 38, 52)
P_TEXT = (240, 238, 232)

def _gradient(top, bottom) -> Image.Image:
    img = Image.new("RGB", (W, H), top)
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / (H - 1)
        d.line([(0, y), (W, y)], fill=tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3)))
    return img


def _skyline(img: Image.Image, body, lit, lit_share: float, glow=None) -> None:
    """Силуэт города (одинаковый на всех картинках — сид фиксированный), окна горят с долей lit_share."""
    rnd = random.Random(7)
    d = ImageDraw.Draw(img)
    x, base = -20, H
    win_rnd = random.Random(int(lit_share * 1000) + 3)
    while x < W:
        bw = rnd.randint(70, 150)
        bh = rnd.randint(120, 250)
        top = base - bh
        d.rectangle([x, top, x + bw, base], fill=body)
        if rnd.random() < 0.3:  # антенна / крыша
            d.rectangle([x + bw // 2 - 3, top - 30, x + bw // 2 + 3, top], fill=body)
        for wy in range(top + 18, base - 20, 28):
            for wx in range(x + 12, x + bw - 16, 24):
                if win_rnd.random() < lit_share:
                    d.rectangle([wx, wy, wx + 10, wy + 14], fill=lit)
        x += bw + rnd.randint(-10, 6)
    if glow:
        haze = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(haze).rectangle([0, H - 120, W, H], fill=(*glow, 40))
        img.paste(haze.filter(ImageFilter.GaussianBlur(30)), (0, 0), haze.filter(ImageFilter.GaussianBlur(30)))


def _frame(img: Image.Image, color) -> None:
    d = ImageDraw.Draw(img)
    d.rectangle([14, 14, W - 15, H - 15], outline=color, width=6)
    d.rectangle([30, 30, W - 31, H - 31], outline=tuple(c // 2 for c in color), width=2)


def _titles(img: Image.Image, kicker: str, title: str, sub: str, kcolor) -> None:
    # тень под текстом, чтобы читался на любом фоне
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shade).rectangle([40, 60, 760, 330], fill=(0, 0, 0, 110))
    shade = shade.filter(ImageFilter.GaussianBlur(40))
    img.paste(shade, (0, 0), shade)
    d = ImageDraw.Draw(img)
    d.text((64, 78), kicker, font=_mono(30), fill=kcolor)
    d.text((58, 116), title, font=_bold(128), fill=P_TEXT)
    d.text((64, 268), sub, font=_regular(36), fill=(214, 212, 206))


def phase_night(n: int) -> Image.Image:
    img = _gradient((6, 9, 26), (22, 26, 58))
    d = ImageDraw.Draw(img)
    rnd = random.Random(n)
    for _ in range(140):  # звёзды
        x, y, r = rnd.randint(0, W), rnd.randint(0, H - 260), rnd.choice([1, 1, 1, 2])
        d.ellipse([x - r, y - r, x + r, y + r], fill=(220, 225, 255))
    # луна-полумесяц
    cx, cy, r = 1060, 150, 80
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([cx - 150, cy - 150, cx + 150, cy + 150], fill=(250, 235, 170, 60))
    glow = glow.filter(ImageFilter.GaussianBlur(50))
    img.paste(glow, (0, 0), glow)
    moon = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(moon).ellipse([cx - r, cy - r, cx + r, cy + r], fill=(250, 238, 190, 255))
    cut = Image.new("L", (W, H), 0)
    ImageDraw.Draw(cut).ellipse([cx - r + 38, cy - r - 18, cx + r + 38, cy + r - 18], fill=255)
    alpha = moon.getchannel("A")
    moon.putalpha(Image.composite(Image.new("L", (W, H), 0), alpha, cut))  # вырезаем — полумесяц
    img.paste(moon, (0, 0), moon)
    _skyline(img, (4, 5, 12), (250, 210, 110), 0.05)
    _frame(img, (60, 70, 140))
    _titles(img, "МАФИЯ · ГОРОД ЗАСЫПАЕТ", f"НОЧЬ {n}", "Мафия выходит на работу.", P_RED)
    return _noise(img, n)


def phase_morning(n: int, killed: bool = True) -> Image.Image:
    """killed — ночью кого-то убили; иначе все живы (Доктор спас или мафия не договорилась)."""
    img = _gradient((30, 20, 48), (236, 120, 60)) if killed else _gradient((44, 40, 78), (250, 168, 90))
    d = ImageDraw.Draw(img)
    cx, cy, r = 980, 470, 150
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([cx - 280, cy - 280, cx + 280, cy + 280], fill=(255, 190, 90, 90))
    glow = glow.filter(ImageFilter.GaussianBlur(60))
    img.paste(glow, (0, 0), glow)
    d = ImageDraw.Draw(img)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 206, 120))
    _skyline(img, (28, 16, 30), (255, 220, 140), 0.18)
    _frame(img, (236, 140, 70))
    if killed:  # тревога: красный отсвет над городом
        alarm = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(alarm).rectangle([0, H - 260, W, H], fill=(200, 20, 40, 70))
        alarm = alarm.filter(ImageFilter.GaussianBlur(60))
        img.paste(alarm, (0, 0), alarm)
    _titles(img, "МАФИЯ · ГОРОД ПРОСЫПАЕТСЯ", f"УТРО {n}",
           "Проснулись не все." if killed else "Сегодня проснулись все.", (255, 200, 110))
    return _noise(img, 100 + n)


def phase_day(n: int) -> Image.Image:
    img = _gradient((40, 70, 110), (120, 150, 175))
    d = ImageDraw.Draw(img)
    cx, cy, r = 1080, 130, 70
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([cx - 190, cy - 190, cx + 190, cy + 190], fill=(255, 220, 120, 120))
    glow = glow.filter(ImageFilter.GaussianBlur(55))
    img.paste(glow, (0, 0), glow)
    d = ImageDraw.Draw(img)
    for k in range(12):  # лучи — чтобы солнце не путали с луной
        a = k * math.pi / 6
        d.line([(cx + math.cos(a) * (r + 14), cy + math.sin(a) * (r + 14)),
                (cx + math.cos(a) * (r + 46), cy + math.sin(a) * (r + 46))], fill=(255, 214, 110), width=8)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 214, 110))
    _skyline(img, (34, 40, 52), (120, 140, 160), 0.25)
    _frame(img, (200, 210, 225))
    _titles(img, "МАФИЯ · ОБСУЖДЕНИЕ", f"ДЕНЬ {n}", "Кто из вас мафия?", (255, 225, 140))
    return _noise(img, 200 + n)


def phase_vote(n: int) -> Image.Image:
    img = _gradient((40, 6, 12), (12, 4, 8))
    d = ImageDraw.Draw(img)
    # урна с бюллетенем
    bx, by = 900, 250
    d.polygon([(bx, by + 60), (bx + 260, by + 60), (bx + 240, by + 260), (bx + 20, by + 260)], fill=(90, 14, 24))
    d.polygon([(bx - 10, by + 30), (bx + 270, by + 30), (bx + 260, by + 70), (bx, by + 70)], fill=(140, 24, 38))
    d.rounded_rectangle([bx + 80, by + 42, bx + 180, by + 54], radius=5, fill=(30, 4, 8))  # прорезь
    paper = Image.new("RGBA", (120, 150), (0, 0, 0, 0))
    pd = ImageDraw.Draw(paper)
    pd.rectangle([0, 0, 119, 149], fill=(240, 236, 228))
    pd.line([(25, 30), (95, 100)], fill=P_RED, width=10)
    pd.line([(95, 30), (25, 100)], fill=P_RED, width=10)
    paper = paper.rotate(-12, expand=True, resample=Image.BICUBIC)
    img.paste(paper, (bx + 62, by - 112), paper)
    d = ImageDraw.Draw(img)
    d.rectangle([bx + 60, by + 30, bx + 200, by + 46], fill=(140, 24, 38))  # бюллетень наполовину в урне
    _skyline(img, (8, 2, 4), (180, 40, 50), 0.04)
    _frame(img, P_RED)
    _titles(img, f"МАФИЯ · ДЕНЬ {n}", "ГОЛОСУЕМ", "Кого казним? Голосование тайное.", P_RED)
    return _noise(img, 300 + n)



def save(img: Image.Image, name: str, folder: Path = IMAGES) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    img.save(folder / f"{name}.jpg", format="JPEG", quality=90, optimize=True)


if __name__ == "__main__":
    save(main_image(), "main")
    for key in ORDER:
        save(section_image(key), key)
    save(lobby_bunker(), "bunker", LOBBY)
    save(lobby_mafia(), "mafia", LOBBY)
    save(lobby_nefor(), "nefor", LOBBY)
    for n in range(1, PHASE_MAX + 1):
        save(phase_night(n), f"night_{n}", PHASES)
        save(phase_morning(n, True), f"morning_kill_{n}", PHASES)
        save(phase_morning(n, False), f"morning_safe_{n}", PHASES)
        save(phase_day(n), f"day_{n}", PHASES)
        save(phase_vote(n), f"vote_{n}", PHASES)
    print("готово:", len(list(IMAGES.glob("*.jpg"))), "меню,", len(list(LOBBY.glob("*.jpg"))), "лобби,",
          len(list(PHASES.glob("*.jpg"))), "фаз Мафии")
