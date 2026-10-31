"""Точка входа: запуск бота (long polling)."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
)

from config import load_config
from database import Database
from games import bunker_router
from handlers import admin_router, roast_router, user_router
from handlers.roast import background as roast_background

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("bot")


async def set_commands(bot: Bot, config) -> None:
    """Меню команд, которое Telegram показывает при вводе «/»."""
    private = [
        BotCommand(command="start", description="Проверить подписку и получить доступ"),
        BotCommand(command="top", description="Рейтинг выживших в Бункере"),
    ]
    group = [
        BotCommand(command="bunker", description="Начать «Бункер» (можно: /bunker злой, добрый, кринж)"),
        BotCommand(command="bunker_stop", description="Остановить игру (только ведущий)"),
        BotCommand(command="bunker_bots", description="Добавить в лобби тестовых ботов (временно)"),
        BotCommand(command="top", description="Рейтинг выживших"),
        BotCommand(command="roast", description="Прожарить (ответом на сообщение или @ник)"),
        BotCommand(command="week", description="Итоги недели чата"),
        BotCommand(command="forget_me", description="Забыть меня и не стебать"),
        BotCommand(command="remember_me", description="Снова запоминать меня"),
    ]
    admin = private + [BotCommand(command="admin", description="Панель администратора")]
    try:
        await bot.set_my_commands(private, scope=BotCommandScopeAllPrivateChats())
        await bot.set_my_commands(group, scope=BotCommandScopeAllGroupChats())
        for admin_id in config.admin_ids:
            await bot.set_my_commands(admin, scope=BotCommandScopeChat(chat_id=admin_id))
    except Exception as e:  # меню — не критично, бот работает и без него
        logger.warning("Не удалось установить меню команд: %s", e)


async def main() -> None:
    config = load_config()

    # Прокси нужен только в особых окружениях; по умолчанию BOT_PROXY пуст.
    session = AiohttpSession(proxy=config.proxy) if config.proxy else None
    bot = Bot(
        token=config.token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    db = Database(config.db_path)
    await db.init()
    import ai
    ai.set_usage_sink(db.add_ai_usage)  # учёт токенов и стоимости запросов к ИИ

    dp = Dispatcher(storage=MemoryStorage())
    # Прокидываем зависимости в обработчики (aiogram подставит по имени параметра)
    dp["db"] = db
    dp["config"] = config

    # admin_router раньше user_router: состояния FSM админа имеют приоритет
    dp.include_router(admin_router)
    dp.include_router(bunker_router)
    dp.include_router(user_router)
    # Стёб-бот последним: ловит все остальные сообщения групп
    dp.include_router(roast_router)

    await set_commands(bot, config)

    me = await bot.get_me()
    logger.info("Бот @%s запущен. Требуемые чаты: %s", me.username, config.required_chats)

    await bot.delete_webhook(drop_pending_updates=True)
    roast_task = asyncio.create_task(roast_background(bot, db))  # noqa: F841 — живёт всё время работы
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановлено.")
