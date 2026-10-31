"""Клавиатуры (inline)."""
from __future__ import annotations

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from subscription import chat_link

CHECK_CALLBACK = "check_sub"


async def subscription_keyboard(bot: Bot, required_chats: list[str]) -> InlineKeyboardMarkup:
    """Кнопки-ссылки на все требуемые чаты + кнопка проверки."""
    builder = InlineKeyboardBuilder()
    for chat in required_chats:
        title, url = await chat_link(bot, chat)
        if url:
            builder.row(InlineKeyboardButton(text=f"➕ {title}", url=url))
    builder.row(
        InlineKeyboardButton(text="✅ Проверить подписку", callback_data=CHECK_CALLBACK)
    )
    return builder.as_markup()


def admin_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats"))
    builder.row(InlineKeyboardButton(text="📢 Рассылка", callback_data="admin:broadcast"))
    builder.row(
        InlineKeyboardButton(text="✏️ Текст приветствия", callback_data="admin:edit_welcome"),
        InlineKeyboardButton(text="🎁 Текст награды", callback_data="admin:edit_reward"),
    )
    return builder.as_markup()


def cancel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✖️ Отмена", callback_data="admin:cancel"))
    return builder.as_markup()


def confirm_broadcast_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Отправить всем", callback_data="admin:broadcast_go"),
        InlineKeyboardButton(text="✖️ Отмена", callback_data="admin:cancel"),
    )
    return builder.as_markup()
