"""Иконки персонажей для карточек: кто ты по «Данным» — человек, камень, инопланетянин, кактус…

Рисуем сами примитивами Pillow (без ИИ и без картинок из интернета) на холсте 400×400 и
уменьшаем — так края сглаживаются. Вид определяется по тексту «Данных» (bio).
"""
from __future__ import annotations

import re
from functools import lru_cache

from PIL import Image, ImageDraw

S = 400  # холст для рисования, потом уменьшаем

# (вид, регулярка по «Данным») — порядок важен: сначала более точные
KINDS: list[tuple[str, str]] = [
    ("clone", r"клон"),
    ("time", r"путешеств\w* во времени|машин\w* времени|из будущего|из прошлого|часы"),
    ("robot", r"нейросет|киборг|робот|андроид|тостер|компьютер|\bии\b|искусствен|чат-?бот|утюг|пылесос"),
    ("alien", r"инопланет|пришел|рептилоид|марсиан|нло|гуманоид"),
    ("rock", r"камень|булыжник|кирпич|скала|валун|галька"),
    ("cactus", r"кактус|фикус|растени|цвето?к|дерев|пальм|гриб|кабачок|картош|морков"),
    ("ghost", r"призрак|привидени|душа|дух\b|полтергейст"),
    ("bat", r"вампир|летуч\w* мыш"),
    ("skull", r"зомби|мертвец|скелет|мумия|покойн|некромант"),
    ("dino", r"динозавр|ящер|дракон|тираннозавр|птеродактил|рептили|чудовищ|монстр|несси|годзилл"),
    ("snowman", r"снегов"),
    ("gnome", r"гном|эльф|лепрекон|домовой"),
    ("doll", r"матр[её]шк|кукл|неваляшк"),
    ("mannequin", r"манекен|статуя|памятник|чучело"),
    ("jar", r"банк\w* |огур|консерв|пельмен|шаурм|пицц|бутерброд|котлет|еда"),
    ("fridge", r"холодильник|микроволнов|шкаф|стиральн"),
    ("mold", r"плесен|бактери|вирус|микроб|амёб|грибок"),
    ("animal", r"квадробер|кот\b|кошк|собак|пёс|пес\b|хомяк|енот|попуга|крыс|голуб|бобр|капибар|ёж|еж\b|медвед|лиса|волк"),
    ("elder", r"бабушк|дедушк|бабул|дед\b|старик|старушк|пенсионер"),
    ("person", r"мужчин|женщин|парень|девушк|мальчик|девочк|подросток|человек|студент|мужик|тётя|дядя|школьни"),
]


def kind_of(bio: str) -> str:
    text = (bio or "").lower()
    for kind, pattern in KINDS:
        if re.search(pattern, text):
            return kind
    return "unknown"


# ---------- рисование ----------

WHITE = (236, 236, 232, 255)
DARK = (18, 20, 24, 255)
ACCENT = (247, 196, 37, 255)
GREEN = (92, 190, 92, 255)
DGREEN = (52, 130, 60, 255)
GRAY = (150, 154, 160, 255)
RED = (214, 64, 54, 255)
SKIN = (240, 200, 160, 255)


def _person(d: ImageDraw.ImageDraw, cx: int = 200, color=WHITE, scale: float = 1.0) -> None:
    r = 52 * scale
    d.ellipse([cx - r, 120 - r * 1.0 + (1 - scale) * 60, cx + r, 120 + r + (1 - scale) * 60], fill=color)
    bw, top = 105 * scale, 205 + (1 - scale) * 50
    d.rounded_rectangle([cx - bw, top, cx + bw, 330], radius=int(70 * scale), fill=color)


def _draw(kind: str, img: Image.Image, d: ImageDraw.ImageDraw) -> None:
    if kind == "person":
        _person(d)
    elif kind == "elder":
        _person(d, cx=180)
        d.line([(300, 200), (300, 340)], fill=ACCENT, width=16)
        d.arc([260, 170, 320, 230], 180, 360, fill=ACCENT, width=16)
    elif kind == "mannequin":
        _person(d, color=GRAY)
        d.line([(200, 330), (200, 360)], fill=GRAY, width=14)
        d.line([(150, 362), (250, 362)], fill=GRAY, width=14)
    elif kind == "clone":
        _person(d, cx=150, color=GRAY, scale=0.85)
        _person(d, cx=240, color=WHITE, scale=0.95)
    elif kind == "rock":
        d.polygon([(70, 290), (95, 180), (170, 110), (265, 120), (330, 185), (340, 290), (250, 320), (140, 318)],
                  fill=GRAY)
        d.polygon([(95, 180), (170, 110), (265, 120), (230, 165), (150, 170)], fill=(178, 182, 188, 255))
        d.line([(200, 170), (215, 230), (190, 280)], fill=DARK, width=8)
        d.line([(215, 230), (265, 250)], fill=DARK, width=6)
    elif kind == "alien":
        d.ellipse([105, 60, 295, 290], fill=GREEN)
        d.polygon([(135, 225), (180, 312), (220, 312), (265, 225)], fill=GREEN)
        for sx in (-1, 1):  # раскосые глаза, не касаются друг друга
            eye = Image.new("RGBA", (84, 50), (0, 0, 0, 0))
            ImageDraw.Draw(eye).ellipse([0, 0, 84, 50], fill=DARK)
            eye = eye.rotate(32 * sx, expand=True)
            img.alpha_composite(eye, (int(200 + sx * 52 - eye.width / 2), 170 - eye.height // 2))
    elif kind == "robot":
        d.line([(200, 60), (200, 105)], fill=GRAY, width=10)
        d.ellipse([186, 46, 214, 74], fill=RED)
        d.rounded_rectangle([95, 105, 305, 300], radius=28, fill=GRAY)
        d.ellipse([140, 160, 190, 210], fill=ACCENT)
        d.ellipse([210, 160, 260, 210], fill=ACCENT)
        d.rounded_rectangle([140, 240, 260, 270], radius=8, fill=DARK)
        for x in (170, 200, 230):
            d.line([(x, 240), (x, 270)], fill=GRAY, width=5)
        d.rectangle([80, 180, 95, 230], fill=GRAY)
        d.rectangle([305, 180, 320, 230], fill=GRAY)
    elif kind == "cactus":
        d.rounded_rectangle([170, 70, 230, 300], radius=30, fill=GREEN)
        d.rounded_rectangle([100, 150, 150, 230], radius=25, fill=GREEN)
        d.rectangle([125, 205, 175, 230], fill=GREEN)
        d.rounded_rectangle([250, 120, 300, 200], radius=25, fill=GREEN)
        d.rectangle([225, 175, 275, 200], fill=GREEN)
        d.polygon([(130, 290), (270, 290), (250, 360), (150, 360)], fill=(176, 104, 60, 255))
        for x, y in [(190, 110), (210, 170), (190, 230), (120, 175), (280, 150)]:
            d.line([(x - 8, y), (x + 8, y)], fill=DGREEN, width=4)
    elif kind == "ghost":
        d.pieslice([100, 60, 300, 260], 180, 360, fill=WHITE)
        d.rectangle([100, 160, 300, 300], fill=WHITE)
        for i in range(5):  # волнистый низ
            x = 100 + i * 40
            d.ellipse([x, 280, x + 40, 330], fill=WHITE if i % 2 == 0 else (0, 0, 0, 0))
        d.ellipse([150, 140, 185, 190], fill=DARK)
        d.ellipse([215, 140, 250, 190], fill=DARK)
        d.ellipse([180, 215, 220, 250], fill=DARK)
    elif kind == "bat":
        col = (150, 90, 210, 255)
        d.polygon([(200, 150), (120, 110), (40, 150), (80, 170), (60, 210), (120, 200), (150, 250), (200, 220),
                   (250, 250), (280, 200), (340, 210), (320, 170), (360, 150), (280, 110)], fill=col)
        d.ellipse([165, 130, 235, 210], fill=col)
        d.polygon([(170, 140), (180, 105), (192, 135)], fill=col)
        d.polygon([(208, 135), (220, 105), (230, 140)], fill=col)
        d.ellipse([180, 155, 195, 170], fill=ACCENT)
        d.ellipse([205, 155, 220, 170], fill=ACCENT)
        d.polygon([(188, 192), (194, 210), (200, 192)], fill=WHITE)
        d.polygon([(200, 192), (206, 210), (212, 192)], fill=WHITE)
    elif kind == "skull":
        bone = (214, 226, 196, 255)  # зеленоватый — «зомби»
        d.ellipse([105, 60, 295, 250], fill=bone)
        d.rounded_rectangle([145, 220, 255, 310], radius=20, fill=bone)
        d.ellipse([140, 135, 190, 190], fill=DARK)
        d.ellipse([210, 135, 260, 190], fill=DARK)
        d.polygon([(200, 200), (186, 230), (214, 230)], fill=DARK)
        for x in (165, 190, 215, 240):
            d.line([(x, 262), (x, 310)], fill=DARK, width=6)
    elif kind == "dino":
        d.ellipse([110, 180, 300, 290], fill=GREEN)                                  # туловище
        d.polygon([(260, 205), (300, 90), (330, 95), (295, 230)], fill=GREEN)        # шея
        d.ellipse([290, 70, 360, 115], fill=GREEN)                                   # голова
        d.polygon([(120, 230), (30, 270), (40, 285), (130, 260)], fill=GREEN)        # хвост
        for x in (140, 175, 235, 265):
            d.rectangle([x, 270, x + 24, 340], fill=GREEN)                           # ноги
        d.ellipse([330, 82, 342, 94], fill=DARK)
    elif kind == "snowman":
        d.ellipse([110, 200, 290, 370], fill=WHITE)
        d.ellipse([135, 110, 265, 230], fill=WHITE)
        d.ellipse([155, 40, 245, 130], fill=WHITE)
        d.ellipse([178, 70, 192, 84], fill=DARK)
        d.ellipse([208, 70, 222, 84], fill=DARK)
        d.polygon([(200, 88), (200, 102), (250, 96)], fill=(240, 130, 40, 255))
        for y in (150, 185, 250):
            d.ellipse([193, y, 207, y + 14], fill=DARK)
    elif kind == "gnome":
        d.polygon([(200, 30), (110, 190), (290, 190)], fill=RED)
        d.ellipse([140, 160, 260, 250], fill=SKIN)
        d.polygon([(130, 215), (270, 215), (240, 330), (200, 360), (160, 330)], fill=WHITE)
        d.ellipse([190, 205, 214, 230], fill=(220, 150, 120, 255))
        d.ellipse([165, 185, 178, 198], fill=DARK)
        d.ellipse([222, 185, 235, 198], fill=DARK)
    elif kind == "doll":
        d.ellipse([110, 160, 290, 360], fill=RED)
        d.ellipse([130, 50, 270, 200], fill=RED)
        d.ellipse([155, 80, 245, 170], fill=SKIN)
        d.ellipse([175, 108, 187, 120], fill=DARK)
        d.ellipse([213, 108, 225, 120], fill=DARK)
        d.ellipse([168, 135, 186, 150], fill=(240, 120, 120, 255))
        d.ellipse([214, 135, 232, 150], fill=(240, 120, 120, 255))
        d.ellipse([160, 230, 240, 310], fill=ACCENT)
        d.ellipse([185, 255, 215, 285], fill=RED)
    elif kind == "jar":
        d.rounded_rectangle([120, 60, 280, 100], radius=10, fill=GRAY)
        d.rounded_rectangle([105, 95, 295, 350], radius=40, fill=(150, 200, 230, 90), outline=(190, 225, 245, 255),
                            width=8)
        for x, y, a in [(130, 160, 20), (200, 140, -15), (160, 240, 10), (230, 230, -25), (180, 300, 5)]:
            pk = Image.new("RGBA", (60, 110), (0, 0, 0, 0))
            ImageDraw.Draw(pk).ellipse([0, 0, 60, 110], fill=DGREEN)
            pk = pk.rotate(a, expand=True)
            img.alpha_composite(pk, (x - 10, y - 30))
    elif kind == "fridge":
        d.rounded_rectangle([120, 40, 280, 360], radius=24, fill=WHITE)
        d.line([(120, 150), (280, 150)], fill=DARK, width=8)
        d.rounded_rectangle([245, 80, 260, 125], radius=6, fill=GRAY)
        d.rounded_rectangle([245, 180, 260, 260], radius=6, fill=GRAY)
        d.ellipse([150, 75, 170, 95], fill=ACCENT)  # магнитик
    elif kind == "time":
        d.rectangle([120, 50, 280, 75], fill=ACCENT)
        d.rectangle([120, 325, 280, 350], fill=ACCENT)
        d.polygon([(140, 75), (260, 75), (210, 200), (260, 325), (140, 325), (190, 200)],
                  outline=WHITE, fill=(80, 84, 92, 255), width=8)
        d.polygon([(165, 110), (235, 110), (200, 185)], fill=(230, 190, 110, 255))
        d.polygon([(150, 320), (250, 320), (200, 250)], fill=(230, 190, 110, 255))
    elif kind == "mold":
        for x, y, r, c in [(150, 160, 70, GREEN), (250, 150, 55, DGREEN), (210, 250, 80, GREEN),
                           (120, 270, 45, DGREEN), (290, 260, 40, GREEN)]:
            d.ellipse([x - r, y - r, x + r, y + r], fill=c)
        for x, y in [(150, 150), (215, 245), (255, 145)]:
            d.ellipse([x - 12, y - 12, x + 12, y + 12], fill=(200, 230, 150, 255))
    elif kind == "animal":  # след лапы
        d.ellipse([130, 190, 270, 330], fill=WHITE)
        for x, y in [(110, 150), (170, 95), (240, 95), (300, 150)]:
            d.ellipse([x - 32, y - 40, x + 32, y + 40], fill=WHITE)
    else:  # кто это вообще такое
        from games.bunker.render import _bold
        d.text((200, 210), "?", font=_bold(260), fill=ACCENT, anchor="mm")


@lru_cache(maxsize=64)
def _icon_cached(kind: str, size: int) -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    _draw(kind, img, ImageDraw.Draw(img))
    return img.resize((size, size), Image.LANCZOS)


def icon(kind: str, size: int) -> Image.Image:
    return _icon_cached(kind, size).copy()


def badge(bio: str, size: int, ring=(247, 196, 37), panel=(28, 31, 37)) -> Image.Image:
    """Круглый значок с иконкой персонажа — для шапки карточки и итоговых титров."""
    k = 4
    big = Image.new("RGBA", (size * k, size * k), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    w = max(3, size // 25) * k
    d.ellipse([w // 2, w // 2, size * k - w // 2, size * k - w // 2], fill=(*panel, 255), outline=(*ring, 255), width=w)
    out = big.resize((size, size), Image.LANCZOS)
    inner = int(size * 0.68)
    out.alpha_composite(icon(kind_of(bio), inner), ((size - inner) // 2, (size - inner) // 2))
    return out
