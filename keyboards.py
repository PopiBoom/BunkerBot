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
    builder.row(InlineKeyboardButton(text="💰 Расходы ИИ", callback_data="admin:ai_costs"))
    builder.row(InlineKeyboardButton(text="🔥 Стёб-бот: вкл/выкл", callback_data="admin:roast_toggle"))
    builder.row(InlineKeyboardButton(text="⏱ Как часто бот встревает", callback_data="admin:roast_freq"))
    builder.row(InlineKeyboardButton(text="😈 Как часто ставит реакции", callback_data="admin:react"))
    builder.row(InlineKeyboardButton(text="🤔 Вопрос дня в тишину: вкл/выкл", callback_data="admin:quiet_kick"))
    builder.row(InlineKeyboardButton(text="📢 Посты в канал (тест): вкл/выкл", callback_data="admin:chan_posts"))
    builder.row(InlineKeyboardButton(text="📥 Загрузить историю чата", callback_data="admin:roast_import"))
    builder.row(
        InlineKeyboardButton(text="💾 Бэкап базы", callback_data="admin:backup"),
        InlineKeyboardButton(text="🗄 Автобэкап: вкл/выкл", callback_data="admin:auto_backup"),
    )
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


def roast_interval_keyboard(current: int) -> InlineKeyboardMarkup:
    """Шанс, с которым стёб-бот сам встревает на обычное сообщение: «1 из N» (0 — выкл)."""
    from handlers.roast import CHANCES

    builder = InlineKeyboardBuilder()
    for n, label in CHANCES:
        mark = "✅ " if n == current else ""
        builder.button(text=f"{mark}{label}", callback_data=f"admin:roast_freq:{n}")
    builder.adjust(3)
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:cancel"))
    return builder.as_markup()


def react_rate_keyboard(current: float) -> InlineKeyboardMarkup:
    from handlers.roast import REACT_OPTIONS

    builder = InlineKeyboardBuilder()
    for rate, label in REACT_OPTIONS:
        mark = "✅ " if abs(rate - current) < 1e-9 else ""
        builder.button(text=f"{mark}{label}", callback_data=f"admin:react:{rate}")
    builder.adjust(4)
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:cancel"))
    return builder.as_markup()
