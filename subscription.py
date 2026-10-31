"""Проверка подписки на требуемые каналы/чаты и получение ссылок."""
from __future__ import annotations

from aiogram import Bot
from aiogram.enums import ChatMemberStatus

# Статусы, при которых считаем, что человек состоит в чате
SUBSCRIBED_STATUSES = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.CREATOR,
}


async def is_member(bot: Bot, chat: str | int, user_id: int) -> bool:
    """True, если пользователь состоит в данном чате/канале."""
    member = await bot.get_chat_member(chat, user_id)
    if member.status in SUBSCRIBED_STATUSES:
        return True
    # RESTRICTED — состоит, но с ограничениями
    if member.status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(member, "is_member", False))
    return False


async def missing_subscriptions(
    bot: Bot, required_chats: list[str], user_id: int
) -> list[str]:
    """Возвращает список чатов, на которые пользователь НЕ подписан."""
    missing: list[str] = []
    for chat in required_chats:
        try:
            if not await is_member(bot, chat, user_id):
                missing.append(chat)
        except Exception:
            # Если проверить не удалось (бот не админ и т.п.) — считаем не подписанным,
            # чтобы не выдать контент по ошибке.
            missing.append(chat)
    return missing


async def chat_link(bot: Bot, chat: str | int) -> tuple[str, str]:
    """Возвращает (заголовок, ссылка-приглашение) для кнопки.

    Для @username ссылка строится как https://t.me/<username>.
    Для приватных чатов берём invite_link через get_chat (нужны права админа).
    """
    title = str(chat)
    url = ""
    try:
        info = await bot.get_chat(chat)
        title = info.title or title
        if info.username:
            url = f"https://t.me/{info.username}"
        elif info.invite_link:
            url = info.invite_link
        else:
            # Попробуем создать (экспортировать) ссылку-приглашение
            try:
                url = await bot.export_chat_invite_link(chat)
            except Exception:
                url = ""
    except Exception:
        if isinstance(chat, str) and chat.startswith("@"):
            url = f"https://t.me/{chat[1:]}"
    if not url and isinstance(chat, str) and chat.startswith("@"):
        url = f"https://t.me/{chat[1:]}"
    return title, url
