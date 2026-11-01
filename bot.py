"""Точка входа: запуск бота (long polling)."""
from __future__ import annotations

import asyncio
import logging

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeChatMember,
    Message,
)

from commands import (ADMIN_SECTION, GROUP_SECTIONS, PRIVATE_SECTIONS, PUBLIC_COMMANDS, PUBLIC_GROUP_SECTIONS,
                      PUBLIC_PRIVATE_SECTIONS, command_name, menu)
from config import load_config
from dashes import NoLongDashes
from ephemeral import TEMP_COMMANDS, TTL, TempReplies, auto_delete
from database import Database
from games import bunker_router, flag_loop, hush, mafia_router, note_channel_post
from handlers import (admin_router, compare_router, fun_router, help_router, menu_router, quotes_router,
                      roast_router, stats_router, user_router)
from handlers.admin import auto_backup_loop
from handlers.roast import background as roast_background

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("bot")


async def set_commands(bot: Bot, config, db: Database) -> None:
    """Меню «/» по разделам (commands.py). Участники видят только /start и «забыть меня»,
    админ — все команды: в личке и в каждом известном боту чате (меню «для участника чата»)."""
    def cmds(sections) -> list[BotCommand]:
        return [BotCommand(command=c, description=d) for c, d in menu(sections)]

    admin_private = cmds(PRIVATE_SECTIONS[:-1] + [ADMIN_SECTION] + PRIVATE_SECTIONS[-1:])
    admin_group = [BotCommand(command=c, description=d) for c, d in menu(GROUP_SECTIONS, separators=True)]
    try:
        await bot.set_my_commands(cmds(PUBLIC_PRIVATE_SECTIONS), scope=BotCommandScopeAllPrivateChats())
        if PUBLIC_GROUP_SECTIONS:
            await bot.set_my_commands(cmds(PUBLIC_GROUP_SECTIONS), scope=BotCommandScopeAllGroupChats())
        else:  # участникам в группах команд нет — меню «/» у них пустое
            await bot.delete_my_commands(scope=BotCommandScopeAllGroupChats())
        for admin_id in config.admin_ids:
            await bot.set_my_commands(admin_private, scope=BotCommandScopeChat(chat_id=admin_id))
    except Exception as e:  # меню — не критично, бот работает и без него
        logger.warning("Не удалось установить меню команд: %s", e)
    import time as _time
    for chat_id in await db.active_chats(int(_time.time()) - 60 * 86400):
        for admin_id in config.admin_ids:
            scope = BotCommandScopeChatMember(chat_id=chat_id, user_id=admin_id)
            try:
                await bot.set_my_commands(admin_group, scope=scope)
            except Exception as e:  # вдруг Telegram не примет строки-разделители — тогда меню без них
                logger.info("меню админа в чате %s с разделителями не поставилось: %s", chat_id, e)
                try:
                    await bot.set_my_commands(cmds(GROUP_SECTIONS), scope=scope)
                except Exception as e2:
                    logger.info("меню админа в чате %s не поставилось: %s", chat_id, e2)


class AdminOnlyCommands(BaseMiddleware):
    """Команды — только админам. Кроме PUBLIC_COMMANDS (/start): чужие команды бот молча
    пропускает, будто их не было (а во время игры, где им не место, — удаляет).
    Команды админа в группе бот удаляет из чата и выполняет — чтобы не висели строчки «/mafia@бот»."""

    async def __call__(self, handler, event: Message, data: dict):
        if event.is_automatic_forward:  # пост канала в чате — под ним комментарии, игры их не удаляют
            note_channel_post(event.chat.id, event.message_id)
        cmd = command_name(event.text or event.caption or "")
        if cmd and cmd not in PUBLIC_COMMANDS:
            cfg = data.get("config")
            user = event.from_user
            if not (cfg and user and cfg.is_admin(user.id)):
                if hush(event):  # ночью Мафии / от мёртвых — удаляем и такое, чтобы не болтали через «/»
                    try:
                        await event.delete()
                    except Exception:
                        pass
                return None
        if cmd and event.text and event.chat.type in ("group", "supergroup"):
            # Команду в чате бот сразу убирает (в группе Telegram ещё и дописывает «@имя_бота» — некрасиво).
            # Удаляем ДО выполнения: ответ бота тогда уходит обычным сообщением, без ссылки на удалённое.
            try:
                await event.delete()
            except Exception:  # нет права удалять — просто выполняем команду
                pass
            if cmd in TEMP_COMMANDS:  # служебная команда: её ответы в чате удалятся через минуту
                token = auto_delete.set(TTL)
                try:
                    return await handler(event, data)
                finally:
                    auto_delete.reset(token)
        return await handler(event, data)


async def main() -> None:
    config = load_config()

    # Прокси нужен только в особых окружениях; по умолчанию BOT_PROXY пуст.
    session = AiohttpSession(proxy=config.proxy) if config.proxy else None
    bot = Bot(
        token=config.token,
        session=session,
        # allow_sending_without_reply: команды админа в чате бот удаляет — ответ на них уходит обычным сообщением
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, allow_sending_without_reply=True),
    )
    bot.session.middleware(NoLongDashes())  # «-» вместо «—» во всём, что бот отправляет
    bot.session.middleware(TempReplies())   # служебные ответы в чате удаляются через минуту
    db = Database(config.db_path)
    await db.init()
    import ai
    ai.set_usage_sink(db.add_ai_usage)  # учёт токенов и стоимости запросов к ИИ
    ai.set_openrouter_key(await db.get_setting("openrouter_key", ""))  # ключ, присланный боту через /setkey

    dp = Dispatcher(storage=MemoryStorage())
    # Прокидываем зависимости в обработчики (aiogram подставит по имени параметра)
    dp["db"] = db
    dp["config"] = config

    # Команды — только админам (кроме /start); проверка до всех роутеров
    dp.message.outer_middleware(AdminOnlyCommands())
    # admin_router раньше user_router: состояния FSM админа имеют приоритет
    dp.include_router(admin_router)
    dp.include_router(compare_router)
    dp.include_router(bunker_router)
    dp.include_router(mafia_router)
    dp.include_router(user_router)
    dp.include_router(help_router)
    dp.include_router(menu_router)
    dp.include_router(quotes_router)
    dp.include_router(stats_router)
    dp.include_router(fun_router)
    # Стёб-бот последним: ловит все остальные сообщения групп
    dp.include_router(roast_router)

    await set_commands(bot, config, db)

    me = await bot.get_me()
    logger.info("Бот @%s запущен. Требуемые чаты: %s", me.username, config.required_chats)

    await bot.delete_webhook(drop_pending_updates=True)
    # Фоновые задачи живут всё время работы; ссылки держим, чтобы их не собрал GC
    background = [
        asyncio.create_task(roast_background(bot, db)),
        asyncio.create_task(auto_backup_loop(bot, db, config)),  # ночной бэкап базы бате в личку
        asyncio.create_task(flag_loop()),  # флаг «идёт игра»: пока он свежий, сервер не обновляется
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
