"""Игры бота: Бункер и Мафия. В одном чате — одна игра за раз.

Пока идёт игра (включая лобби), стёб-бот молчит, а сервер не обновляется (флаг GAME_FLAG):
deploy/update.sh ждёт, пока флаг не устареет, — иначе перезапуск молча оборвал бы партию.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .bunker import router as bunker_router
from .mafia import router as mafia_router

__all__ = ["bunker_router", "mafia_router", "active", "any_running", "hush", "flag_loop", "GAME_FLAG"]

logger = logging.getLogger("games")
GAME_FLAG = Path(__file__).resolve().parents[1] / ".game_running"  # свежий файл = идёт игра (см. update.sh)
FLAG_EVERY = 10  # сек: как часто обновляем флаг (update.sh считает его устаревшим через 2 минуты)


def active(chat_id: int) -> str | None:
    """Какая игра идёт в чате (вместе с лобби): "bunker", "mafia" или None."""
    from .bunker.game import games as bunker
    from .mafia.game import games as mafia

    for name, pool in (("bunker", bunker), ("mafia", mafia)):
        g = pool.get(chat_id)
        if g and g.phase != "ended":
            return name
    return None


def any_running() -> bool:
    from .bunker.game import games as bunker
    from .mafia.game import games as mafia

    return any(g.phase != "ended" for pool in (bunker, mafia) for g in pool.values())


def hush(message) -> bool:
    """Надо ли удалить это сообщение из-за идущей игры (ночь Мафии, мёртвые, посторонние).
    Нужно для команд не-админов: middleware их отбрасывает раньше игровых обработчиков."""
    from .bunker.game import _is_outsider
    from .mafia.game import must_delete

    try:
        return bool(must_delete(message) or _is_outsider(message))
    except Exception:
        return False


def _set_flag(on: bool) -> None:
    try:
        if on:
            GAME_FLAG.touch()
        else:
            GAME_FLAG.unlink(missing_ok=True)
    except OSError as e:
        logger.info("game flag: %s", e)


async def flag_loop() -> None:
    """Держит флаг «идёт игра» свежим, пока есть хоть одна партия; без партий — удаляет."""
    _set_flag(False)  # после перезапуска игр точно нет
    while True:
        await asyncio.sleep(FLAG_EVERY)
        _set_flag(any_running())
