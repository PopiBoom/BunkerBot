"""Игры бота: Бункер и Мафия. В одном чате — одна игра за раз.

Пока идёт игра (включая лобби), стёб-бот молчит, а сервер не обновляется (флаг GAME_FLAG):
deploy/update.sh ждёт, пока флаг не устареет, — иначе перезапуск молча оборвал бы партию.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from .bunker import router as bunker_router
from .mafia import router as mafia_router

__all__ = ["bunker_router", "mafia_router", "active", "any_running", "hush", "flag_loop", "GAME_FLAG",
           "is_comment", "note_channel_post", "load_channel_posts"]

logger = logging.getLogger("games")
GAME_FLAG = Path(__file__).resolve().parents[1] / ".game_running"  # свежий файл = идёт игра (см. update.sh)
FLAG_EVERY = 10  # сек: как часто обновляем флаг (update.sh считает его устаревшим через 2 минуты)
MAX_GAME_AGE = 3 * 3600  # партия (или зависшее лобби) дольше 3 ч — флаг за неё больше не держим: иначе
#                          одна зависшая игра навсегда блокирует обновления сервера
_first_seen: dict[int, float] = {}


# Посты канала, пересланные в чат (их id = id ветки комментариев). Пополняется middleware в bot.py,
# при старте игры — из памяти чата (посты, пришедшие до перезапуска бота).
_channel_posts: dict[int, set[int]] = {}


def note_channel_post(chat_id: int, message_id: int) -> None:
    posts = _channel_posts.setdefault(chat_id, set())
    posts.add(message_id)
    if len(posts) > 2000:
        for old in sorted(posts)[:1000]:
            posts.discard(old)


async def load_channel_posts(db, chat_id: int) -> None:
    try:
        for mid, uid, *_rest in await db.recent_messages(chat_id, limit=3000):
            if uid == -1 and mid:  # так стёб-бот хранит посты канала (roast.CHANNEL_USER_ID)
                note_channel_post(chat_id, mid)
    except Exception as e:
        logger.info("channel posts load failed: %s", e)


def is_comment(message) -> bool:
    """Комментарий к посту канала (в том числе ответ на чужой комментарий в той же ветке).
    Игры такие сообщения не удаляют — ни ночью, ни от мёртвых и посторонних."""
    rt = getattr(message, "reply_to_message", None)
    if rt is not None:
        sc = getattr(rt, "sender_chat", None)
        if getattr(rt, "is_automatic_forward", False) or (sc is not None and getattr(sc, "type", "") == "channel"):
            return True
    tid = getattr(message, "message_thread_id", None)
    return bool(tid) and tid in _channel_posts.get(message.chat.id, ())


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


def _flag_needed() -> bool:
    """Есть ли живая (не зависшая) партия, ради которой стоит отложить обновление сервера."""
    from .bunker.game import games as bunker
    from .mafia.game import games as mafia

    now, seen, need = time.time(), set(), False
    for pool in (bunker, mafia):
        for g in pool.values():
            if g.phase == "ended":
                continue
            key = id(g)
            seen.add(key)
            first = _first_seen.setdefault(key, now)
            if now - first < MAX_GAME_AGE:
                need = True
            elif now - first < MAX_GAME_AGE + FLAG_EVERY:
                logger.warning("игра в чате %s идёт дольше %d ч — обновления больше не держу",
                               g.chat_id, MAX_GAME_AGE // 3600)
    for key in set(_first_seen) - seen:
        _first_seen.pop(key, None)
    return need


async def flag_loop() -> None:
    """Держит флаг «идёт игра» свежим, пока есть живая партия; без партий (или если она зависла) — удаляет."""
    _set_flag(False)  # после перезапуска игр точно нет
    while True:
        await asyncio.sleep(FLAG_EVERY)
        _set_flag(_flag_needed())
