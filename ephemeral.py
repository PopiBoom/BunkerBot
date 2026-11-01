"""Служебные («рабочие») ответы бота в чате удаляются сами через 30 секунд (просьба владельца):
«нет активной игры», «никто ничего не обещал», «уже идёт игра», текстовые справки и рейтинги —
чтобы не засоряли чат. Картинки (таблица позора, карточки) остаются — удаляется только текст.

Как устроено: пока выполняется «временная» команда (TEMP_COMMANDS) или отправляется ответ через
temp_reply(), стоит метка auto_delete; middleware сессии видит её и ставит удаление каждого сообщения,
отправленного в группу текстом. Картинки и всё остальное (игры, прожарка, цитаты, опросы) не трогается.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging

from aiogram.client.session.middlewares.base import BaseRequestMiddleware

logger = logging.getLogger("ephemeral")

TTL = 30  # сек, через сколько удалить
# Команды, все ответы которых в чате — служебные (таблицы, статусы, справка)
TEMP_COMMANDS = {"bunker_stop", "mafia_stop", "bunker_bots", "mafia_bots", "clowns", "promises", "top",
                 "mafia_top", "stats", "help", "forget_me", "remember_me"}
_SENDS = {"SendMessage"}  # только текст: картинки владелец хочет оставлять

auto_delete: contextvars.ContextVar[int] = contextvars.ContextVar("auto_delete", default=0)
_tasks: set[asyncio.Task] = set()


def delete_later(bot, chat_id: int, message_id: int | None, delay: float = TTL) -> None:
    """Удалить сообщение через delay секунд (ошибки — молча: уже удалили руками и т.п.)."""
    if not message_id:
        return

    async def run() -> None:
        await asyncio.sleep(delay)
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception as e:
            logger.debug("auto delete %s/%s failed: %s", chat_id, message_id, e)

    task = asyncio.create_task(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def temp_reply(message, text: str, **kwargs):
    """message.reply, но ответ в группе удалится через минуту."""
    token = auto_delete.set(TTL)
    try:
        return await message.reply(text, **kwargs)
    finally:
        auto_delete.reset(token)


class TempReplies(BaseRequestMiddleware):
    """Сообщения, отправленные в группу под меткой auto_delete, удаляются через её срок."""

    async def __call__(self, make_request, bot, method):
        result = await make_request(bot, method)
        ttl = auto_delete.get()
        if ttl and type(method).__name__ in _SENDS:
            chat_id = getattr(method, "chat_id", None)
            if isinstance(chat_id, int) and chat_id < 0:  # только группы — в личке ничего не удаляем
                delete_later(bot, chat_id, getattr(result, "message_id", None), ttl)
        return result
