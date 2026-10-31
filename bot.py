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

from commands import ADMIN_SECTION, GROUP_SECTIONS, PRIVATE_SECTIONS, menu
from config import load_config
from database import Database
from games import bunker_router
from handlers import (admin_router, compare_router, fun_router, help_router, quotes_router, roast_router,
                      stats_router, user_router)
from handlers.admin import auto_backup_loop
from handlers.roast import background as roast_background

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("bot")


async def set_commands(bot: Bot, config) -> None:
    """Меню команд, которое Telegram показывает при вводе «/» — по разделам (см. commands.py)."""
    def cmds(sections) -> list[BotCommand]:
        return [BotCommand(command=c, description=d) for c, d in menu(sections)]

    private = cmds(PRIVATE_SECTIONS)
    admin = cmds(PRIVATE_SECTIONS[:-1] + [ADMIN_SECTION] + PRIVATE_SECTIONS[-1:])
    group = cmds(GROUP_SECTIONS)
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
    dp.include_router(compare_router)
    dp.include_router(bunker_router)
    dp.include_router(user_router)
    dp.include_router(help_router)
    dp.include_router(quotes_router)
    dp.include_router(stats_router)
    dp.include_router(fun_router)
    # Стёб-бот последним: ловит все остальные сообщения групп
    dp.include_router(roast_router)

    await set_commands(bot, config)

    me = await bot.get_me()
    logger.info("Бот @%s запущен. Требуемые чаты: %s", me.username, config.required_chats)

    await bot.delete_webhook(drop_pending_updates=True)
    # Фоновые задачи живут всё время работы; ссылки держим, чтобы их не собрал GC
    background = [
        asyncio.create_task(roast_background(bot, db)),
        asyncio.create_task(auto_backup_loop(bot, db, config)),  # ночной бэкап базы бате в личку
    ]
    try:
        # message_reaction Telegram по умолчанию не присылает — просим все типы, что ловят обработчики
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in background:
            task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановлено.")
