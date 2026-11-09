"""Вечернее расписание: чтобы фишки не валились в чат разом (09.11, владелец: «не спамить по вечерам»).

Вечер занят: итоги недели (вс 20:00), новости чата (21:00), клоун дня (не раньше 21:40 и через CLOWN_AFTER_NEWS
после новостей). Всё «в случайное время дня» (черновик голосового в канал, коллекционная карточка) в окно
BUSY не попадает.
"""
from __future__ import annotations

import random

BUSY = (19 * 60 + 30, 22 * 60 + 30)   # минуты МСК: 19:30-22:30 — только итоги недели, новости и клоун
NEWS_AT = 21 * 60                     # новости чата
CLOWN_AT = 21 * 60 + 40               # клоун дня — не раньше
CLOWN_AFTER_NEWS = 35 * 60            # и не раньше, чем через 35 мин после новостей (сек)


def free_minutes(lo: int, hi: int) -> list[int]:
    """Минуты дня [lo, hi) вне вечернего окна BUSY."""
    return [m for m in range(max(0, lo), min(24 * 60, hi)) if not BUSY[0] <= m < BUSY[1]]


def pick_minute(lo: int, hi: int) -> int | None:
    """Случайная минута дня из [lo, hi) вне BUSY; None — свободных не осталось."""
    free = free_minutes(lo, hi)
    return random.choice(free) if free else None
