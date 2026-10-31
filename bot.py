"""Точка входа: запуск бота (long polling)."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import load_config
from database import Database
from games import bunker_router
from handlers import admin_router, user_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("bot")


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

    dp = Dispatcher(storage=MemoryStorage())
    # Прокидываем зависимости в обработчики (aiogram подставит по имени параметра)
    dp["db"] = db
    dp["config"] = config

    # admin_router раньше user_router: состояния FSM админа имеют приоритет
    dp.include_router(admin_router)
    dp.include_router(bunker_router)
    dp.include_router(user_router)

    me = await bot.get_me()
    logger.info("Бот @%s запущен. Требуемые чаты: %s", me.username, config.required_chats)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановлено.")
