"""Пользовательские обработчики: /start (без обязательной подписки — владелец 08.11; кнопка «✉️ Отправить письмо»)
и старая кнопка «Проверить подписку» у тех, кому она осталась в старых сообщениях."""
from __future__ import annotations

import html

from aiogram import Bot, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

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


START_TEXT = (
    "👋 Привет, {name}!\n\n"
    "Я Пиздюк - бот чата и канала «радио Волгограда».\n\n"
    "✉️ Случилось что-то жуткое в Волгограде? Жми «Отправить письмо» - возможно, следующий выпуск "
    "«Писем Волгограда» будет именно про твой случай."
)


async def _start_keyboard(bot: Bot, config: Config) -> InlineKeyboardMarkup:
    """Кнопка письма и ссылки на канал и чат — без обязательной подписки (владелец 08.11: «убери обязательную
    подписку на чат и канал»)."""
    from handlers.letters import letter_button
    from subscription import chat_link

    rows = [[letter_button()]]
    for chat in config.required_chats:
        try:
            title, url = await chat_link(bot, chat)
        except Exception:
            continue
        if url:
            rows.append([InlineKeyboardButton(text=f"➕ {title}", url=url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, bot: Bot, db: Database, config: Config) -> None:
    user = message.from_user
    if user is None:
        return
    await db.add_user(user.id, user.username, user.first_name)
    arg = (command.args or "").strip()
    if arg.startswith("wch_"):  # ссылка-вызов в шахматы-виджет (/chess_widget): первый открывший — соперник
        from games.chess.widget import join

        await join(bot, db, user, arg[4:])
        return
    if arg.startswith("dur_"):  # стол в дурака (/durak): сесть за стол
        from games.durak.game import join as durak_join

        await durak_join(bot, db, user, arg[4:])
        return
    if arg == "letter":  # ссылка из подписи выпуска «Писем Волгограда»
        from handlers.letters import ask

        await ask(bot, user.id)
        return
    name = user.first_name or "друг"
    await message.answer(_personalize(START_TEXT, name), reply_markup=await _start_keyboard(bot, config),
                         disable_web_page_preview=True)


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
