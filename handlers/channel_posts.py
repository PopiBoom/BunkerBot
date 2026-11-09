"""Канал бати: его id (привязанный к чату канал), свои посты бота (их стёб-бот не комментирует) и
образцы стиля канала для /compare.

Тестовый режим «пост раз в 2 минуты» из /admin убран (09.11, владелец). Посты в канал сейчас делают
rant (голосовые), night (эфиры), chess (/chess_post), blackjack (/bj) — все через channel_id/mark_own.
"""
from __future__ import annotations

import json
import logging
import os
import time

from aiogram import Bot

from database import Database

logger = logging.getLogger("channel_posts")

CHANNEL_KEY = "father_channel_id"  # id канала (узнаём сами через linked_chat_id чата)
OWN_KEY = "own_channel_posts"      # id своих постов — их не комментируем
POSTS_KEY = "chan_post_texts"      # тексты прошлых постов бота (из старого теста) — не образец стиля


async def channel_id(bot: Bot, db: Database) -> int | None:
    """id канала бати: из BOT_CHANNEL_ID, из памяти или у Telegram — канал, привязанный к нашему чату."""
    env = os.getenv("BOT_CHANNEL_ID", "").strip()
    if env.lstrip("-").isdigit():
        return int(env)
    saved = await db.get_setting(CHANNEL_KEY, "")
    if saved.lstrip("-").isdigit():
        return int(saved)
    for chat_id in await db.active_chats(int(time.time()) - 30 * 86400):
        try:
            info = await bot.get_chat(chat_id)
        except Exception:
            continue
        if getattr(info, "linked_chat_id", None):
            await db.set_setting(CHANNEL_KEY, str(info.linked_chat_id))
            return int(info.linked_chat_id)
    return None


_own_mem: set[int] = set()  # свои посты — сразу в память: пересылка в обсуждение может прийти раньше записи в базу


async def own_posts(db: Database) -> set[int]:
    try:
        return set(json.loads(await db.get_setting(OWN_KEY, "[]"))) | _own_mem
    except ValueError:
        return set(_own_mem)


async def mark_own(db: Database, msg_id: int) -> None:
    """Пост бота в канале — стёб-бот его в комментариях не обсуждает (и /chess_post тоже)."""
    _own_mem.add(msg_id)  # до первого await — иначе пересылка успевала стать «постом бати»
    ids = sorted(await own_posts(db) | {msg_id})[-100:]
    await db.set_setting(OWN_KEY, json.dumps(ids))


async def _style_examples(db: Database) -> list[str]:
    """Посты самого бати из памяти чата — образец стиля канала (без постов самого бота)."""
    try:
        mine = {t[:60] for t in json.loads(await db.get_setting(POSTS_KEY, "[]"))}
    except ValueError:
        mine = set()
    out = []
    for chat_id in await db.active_chats(int(time.time()) - 30 * 86400):
        for _mid, uid, _name, text, _ts in await db.recent_messages(chat_id, limit=300):
            if uid == -1 and text:  # так в памяти подписаны посты канала
                t = str(text)
                if t.startswith("["):
                    t = t.split("]", 1)[-1]
                if len(t.strip()) > 15 and t.strip()[:60] not in mine:
                    out.append(t.strip()[:300])
    return out[-12:]
