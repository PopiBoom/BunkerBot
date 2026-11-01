"""Посты бота «от себя» в канал бати (привязанный к чату канал, Memento Mori).

Пока только тестовый режим: пост раз в TEST_EVERY секунд, сам выключается через TEST_FOR —
чтобы не забыть и не спалить деньги на ИИ. Включается кнопкой в /admin.

Канал публичный, поэтому в постах нет имён участников чата и пересказа переписки — только
собственные мысли бота в духе канала. Свои посты бот в чате не комментирует (их id помним).
Чтобы постить, бот должен быть админом канала с правом «Публикация сообщений».
"""
from __future__ import annotations

import html
import json
import logging
import os
import time

from aiogram import Bot

import ai
from database import Database

logger = logging.getLogger("channel_posts")

MODE_KEY = "chan_post_mode"        # "off" | "test"
UNTIL_KEY = "chan_test_until"      # когда тест сам выключится (ts)
LAST_KEY = "chan_post_last"        # когда был последний пост (ts)
ADMIN_KEY = "chan_test_admin"      # кому написать, что тест закончился
CHANNEL_KEY = "father_channel_id"  # id канала (узнаём сами через linked_chat_id чата)
OWN_KEY = "own_channel_posts"      # id своих постов — их не комментируем
POSTS_KEY = "chan_post_texts"      # прошлые тексты — чтобы не повторяться

TEST_EVERY = 120        # сек между постами в тесте
TEST_FOR = 30 * 60      # тест сам выключается через 30 минут


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


async def own_posts(db: Database) -> set[int]:
    try:
        return set(json.loads(await db.get_setting(OWN_KEY, "[]")))
    except ValueError:
        return set()


async def _remember_post(db: Database, msg_id: int, text: str) -> None:
    ids = sorted(await own_posts(db) | {msg_id})[-100:]
    await db.set_setting(OWN_KEY, json.dumps(ids))
    try:
        texts = json.loads(await db.get_setting(POSTS_KEY, "[]"))
    except ValueError:
        texts = []
    await db.set_setting(POSTS_KEY, json.dumps((texts + [text])[-20:], ensure_ascii=False))


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


async def start_test(db: Database, admin_id: int) -> None:
    await db.set_setting(MODE_KEY, "test")
    await db.set_setting(UNTIL_KEY, str(time.time() + TEST_FOR))
    await db.set_setting(LAST_KEY, "0")
    await db.set_setting(ADMIN_KEY, str(admin_id))


async def stop(db: Database) -> None:
    await db.set_setting(MODE_KEY, "off")


async def _tell_admin(bot: Bot, db: Database, text: str) -> None:
    admin = await db.get_setting(ADMIN_KEY, "")
    if admin.lstrip("-").isdigit():
        try:
            await bot.send_message(int(admin), text)
        except Exception as e:
            logger.info("can't notify admin: %s", e)


async def tick(bot: Bot, db: Database) -> None:
    """Вызывается из фоновой задачи стёб-бота раз в ≤ минуту."""
    if (await db.get_setting(MODE_KEY, "off")) != "test":
        return
    now = time.time()
    if now >= float(await db.get_setting(UNTIL_KEY, "0") or 0):
        await stop(db)
        await _tell_admin(bot, db, "📢 Тест постов в канал закончился — выключил. Снова включить: /admin → «📢 Посты в канал».")
        return
    if now - float(await db.get_setting(LAST_KEY, "0") or 0) < TEST_EVERY:
        return
    await db.set_setting(LAST_KEY, str(now))
    chan = await channel_id(bot, db)
    if chan is None:
        await stop(db)
        await _tell_admin(bot, db, "📢 Не нашёл канал: к чату не привязан канал. Привяжи канал к чату "
                                   "(Канал → Обсуждение) или впиши BOT_CHANNEL_ID в .env. Тест выключил.")
        return
    try:
        prev = json.loads(await db.get_setting(POSTS_KEY, "[]"))
    except ValueError:
        prev = []
    try:
        model = await db.get_setting("post_model", "") or None  # победитель /compare, если выбран
        text = await ai.own_channel_post(await _style_examples(db), prev, model=model)
    except Exception as e:
        logger.warning("channel post text failed: %s", e)
        return
    if not text:
        return
    try:
        msg = await bot.send_message(chan, html.escape(text))  # «<» и «&» от ИИ иначе ломали HTML-разметку
    except Exception as e:
        await stop(db)
        await _tell_admin(bot, db, "📢 Не могу писать в канал — сделай бота админом канала с правом "
                                   f"«Публикация сообщений». Тест выключил.\n\nОшибка: {e}")
        return
    await _remember_post(db, msg.message_id, text)
    logger.info("пост в канал %s: %s", chan, text[:80])
