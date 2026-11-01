"""Содержимое «Нефоров в Волгограде»: поле (точки города), тусовки игроков, слухи, звания для /nefor_top.

Своя игра (не копия чужой): тусовки нефоров делят точки Волгограда. Партия короткая — ROUNDS кругов,
побеждает самый большой капитал (деньги + точки с «крышами»); банкрот выбывает.
"""
from __future__ import annotations

import random

from ..bunker.content import MODE_ALIASES, MODE_ORDER, MODE_VOICE, MODES

__all__ = ["MODE_ALIASES", "MODE_ORDER", "MODE_VOICE", "MODES"]

START_CASH = 1200
LAP_BONUS = 150        # прошёл «Конечную» — стипуха
ROUNDS = 8             # кругов за партию
RENT_RATE = 0.5        # «за вход» = цена × RENT_RATE
ROOF_COST = 0.6        # «крыша» стоит цена × ROOF_COST
ROOF_MULT = (1, 3, 6)  # множитель «за вход» без крыши / с одной / с двумя
DISTRICT_MULT = 2      # весь район у одной тусовки — вход ×2 (без крыш)
MAX_ROOFS = 2
SELL_RATE = 0.5        # в долгах точка уходит городу за половину стоимости
PPS_FINE = 80

# Районы поля: ключ -> (название, цвет, цена точки)
DISTRICTS: dict[str, tuple[str, tuple[int, int, int], int]] = {
    "edge": ("Окраина", (140, 110, 80), 100),
    "sleep": ("Спальник", (90, 160, 210), 140),
    "move": ("Движ", (235, 130, 40), 180),
    "mall": ("ТЦ", (200, 70, 160), 220),
    "center": ("Центр", (220, 60, 60), 260),
    "elite": ("Элита", (232, 186, 64), 300),
}

# Клетки по кругу. Точка: ("spot", название, район); особые: ("start"|"rumor"|"pzd"|"pps", название)
BOARD: list[tuple] = [
    ("start", "Конечная"),
    ("spot", "Спартановка", "edge"),
    ("spot", "Тракторный", "edge"),
    ("spot", "Лысая гора", "edge"),
    ("spot", "Красноармейский", "sleep"),
    ("rumor", "Слух"),
    ("spot", "Торгушка", "sleep"),
    ("spot", "Ткачёвский рынок", "move"),
    ("spot", "Фишбокс", "move"),
    ("spot", "Иксфит", "move"),
    ("pps", "ППС"),
    ("spot", "Мармелад", "mall"),
    ("spot", "Акварель", "mall"),
    ("pzd", "Пиздюк"),
    ("spot", "КомсоМОЛЛ", "mall"),
    ("spot", "ЦР", "center"),
    ("rumor", "Слух"),
    ("spot", "Набережная", "center"),
    ("spot", "Европа Сити Молл", "elite"),
    ("spot", "Волгоград Арена", "elite"),
]
SIZE = len(BOARD)


def is_spot(cell: int) -> bool:
    return BOARD[cell][0] == "spot"


def cell_name(cell: int) -> str:
    return BOARD[cell][1]


def district(cell: int) -> str:
    return BOARD[cell][2] if is_spot(cell) else ""


def price(cell: int) -> int:
    return DISTRICTS[district(cell)][2] if is_spot(cell) else 0


def roof_cost(cell: int) -> int:
    return round(price(cell) * ROOF_COST / 10) * 10


def district_cells(key: str) -> list[int]:
    return [i for i in range(SIZE) if district(i) == key]


# Тусовки игроков: (название, цвет на карте)
TRIBES: list[tuple[str, tuple[int, int, int]]] = [
    ("Панки", (226, 54, 70)),
    ("Готы", (150, 100, 230)),
    ("Эмо", (236, 90, 160)),
    ("Скейтеры", (245, 150, 40)),
    ("Рэперы", (240, 205, 50)),
    ("Металлисты", (120, 150, 190)),
    ("Анимешники", (40, 200, 220)),
    ("Ролевики", (80, 190, 90)),
]

# Слухи: (вид, сумма, запасной текст). gain/lose — себе; collect — каждый платит тебе; treat — ты всем;
# step — сдвиг на клетки (без денег); tax — за каждую свою точку.
RUMORS: list[tuple[str, int, str]] = [
    ("gain", 100, "{name} продал на Авито старые кеды как «винтаж»."),
    ("gain", 150, "Тусовке {tribe} задонатили за квартирник на Тракторном."),
    ("gain", 120, "{name} нашёл заначку в кармане косухи, про которую сам забыл."),
    ("lose", 80, "{name} спустил деньги на шаурму у вокзала. Опять."),
    ("lose", 120, "{name} порвал косуху о забор — новая обошлась недёшево."),
    ("lose", 100, "{name} проспорил, что успеет на последнюю маршрутку."),
    ("collect", 30, "У {name} днюха — тусовки скидываются."),
    ("treat", 25, "{name} проставился всем — сам предложил, никто не тянул за язык."),
    ("step", 3, "{name} сел не в ту маршрутку и уехал дальше, чем хотел."),
    ("step", -2, "{name} забыл телефон и вернулся назад."),
    ("tax", 20, "На точки {tribe} пришла проверка пожарных."),
]

PZD_TEXTS = [
    "Пиздюк посмотрел на {name} и вздохнул: «С такими ходами только на Спартановке и жить».",
    "Пиздюк заметил, что {name} опять считает деньги на пальцах, и взял за урок.",
    "«{name}, ты не нефор, ты бухгалтер в бандане», — сказал Пиздюк и выписал штраф.",
    "Пиздюк сегодня добрый: {name} получает на пиво. Не привыкай.",
]

PPS_TEXTS = [
    "Патруль ППС: «Документы есть? А если найдём?» {name} откупился.",
    "ППС не понравилась причёска {name}. Сошлись на штрафе.",
]

BOT_NAMES = ["Валера", "Зинаида", "Геннадий", "Снежана", "Аркадий", "Люська", "Толик", "Ираида"]

EPILOGUE = [
    "Волгоград пережил ещё один передел. {winner} теперь решают, где тусоваться, а остальные — "
    "где занять на маршрутку.",
    "Город так и не понял, что произошло, но на всех заборах теперь логотип: {winner}. Остальным — "
    "сочувствие и шаурма.",
]


def rumor() -> tuple[str, int, str]:
    return random.choice(RUMORS)


def fmt(text: str, name: str, tribe: str) -> str:
    return text.replace("{name}", name).replace("{tribe}", tribe)


# ---------- звания для /nefor_top ----------

TITLES: list[tuple[str, str, object, str]] = [
    ("👑", "Хозяин Волгограда", lambda s: s["wins"] >= 3, "3+ победы"),
    ("🤝", "Барыга с Торгушки", lambda s: s["trades"] >= 5, "5+ сделок с другими"),
    ("🏚", "Бомж с Конечной", lambda s: s["bankrupt"] >= 2, "2+ раза банкрот"),
    ("💰", "Олигарх", lambda s: s["best"] >= 4000, "капитал 4000₽+ за партию"),
    ("🧓", "Старый нефор", lambda s: s["games"] >= 15, "15+ партий"),
    ("🐣", "Позёр", lambda s: s["games"] < 3, "меньше 3 партий"),
]


def titles(stats: dict) -> list[tuple[str, str, str]]:
    out = []
    for emo, name, cond, why in TITLES:
        try:
            if cond(stats):
                out.append((emo, name, why))
        except (KeyError, TypeError):
            continue
    return out
