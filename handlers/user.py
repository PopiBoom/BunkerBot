"""Пользовательские обработчики: /start и проверка подписки."""
from __future__ import annotations

import html

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

import texts
from config import Config
from database import Database
from keyboards import CHECK_CALLBACK, subscription_keyboard
from subscription import missing_subscriptions

router = Router(name="user")
# Шлюз подписки работает только в личке — в группах /start не трогаем.
router.message.filter(F.chat.type == "private")


def _personalize(text: str, name: str) -> str:
    """Подставляет имя вместо {name}. Безопасно, даже если в тексте есть другие {}.

    Имя экранируем: тексты уходят в HTML-разметке, и имя вроде «<3 Маша» ломало отправку.
    """
    return text.replace("{name}", html.escape(name))


async def _send_reward(message: Message, db: Database, user_id: int, name: str) -> None:
    reward = await db.get_setting(texts.KEY_REWARD, texts.DEFAULT_REWARD)
    await db.mark_reward(user_id)
    await message.answer(_personalize(reward, name), disable_web_page_preview=True)


async def _show_gate(message: Message, bot: Bot, db: Database, config: Config, name: str) -> None:
    welcome = await db.get_setting(texts.KEY_WELCOME, texts.DEFAULT_WELCOME)
    kb = await subscription_keyboard(bot, config.required_chats)
    await message.answer(_personalize(welcome, name), reply_markup=kb)


@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot, db: Database, config: Config) -> None:
    user = message.from_user
    if user is None:
        return
    await db.add_user(user.id, user.username, user.first_name)

    name = user.first_name or "друг"
    missing = await missing_subscriptions(bot, config.required_chats, user.id)
    if not missing:
        await message.answer(texts.ALREADY_SUBSCRIBED_HINT)
        await _send_reward(message, db, user.id, name)
        return

    await _show_gate(message, bot, db, config, name)


@router.callback_query(F.data == CHECK_CALLBACK)
async def check_subscription(call: CallbackQuery, bot: Bot, db: Database, config: Config) -> None:
    user = call.from_user
    try:
        missing = await missing_subscriptions(bot, config.required_chats, user.id)
    except Exception:
        await call.answer(texts.CHECK_ERROR, show_alert=True)
        return

    if missing:
        await call.answer(texts.NOT_SUBSCRIBED, show_alert=True)
        return

    await call.answer("Готово! ✅")
    if call.message is not None:
        await _send_reward(call.message, db, user.id, user.first_name or "друг")
