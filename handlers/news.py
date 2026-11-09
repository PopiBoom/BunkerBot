"""«Новости чата» — каждый день в 21:00 МСК (08.11 владелец: «отключай сериал, сделай новости в 21:00»).

Выпуск: заставка-картинка (cards.render_news: «НОВОСТИ ЧАТА · ЭФИР 21:00», главный заголовок, бегущая строка с
остальными) и текст — приветствие ведущего, 3-4 сюжета дня (каждый — одно реальное событие: кто, что, как
отреагировали, чем кончилось; не больше одной дословной цитаты), «погода в чате на завтра», прощание. ИИ —
ai.daily_news (Opus, effort medium, ≈$0.03). Только в чат (канал публичный). Выходит, если за день было от
MIN_TODAY сообщений; /news у админа — выпуск сейчас. Вкл/выкл — /admin «📰 Новости». Клоун дня ждёт,
пока выйдут новости, и ещё 35 мин (roast._heavy: news.pending, last_issue, evening.py) — не валятся в чат разом.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

import ai
from database import Database
from ephemeral import temp_reply

logger = logging.getLogger("news")
router = Router(name="news")

MSK = timezone(timedelta(hours=3))
ON_KEY = "news_on"
DAY_KEY = "news_day:"     # в какой день выпуск уже был (или пробовали)
NO_KEY = "news_no:"       # номер выпуска в чате
AT_KEY = "news_at:"       # когда вышел последний выпуск (ts) — клоун дня ждёт evening.CLOWN_AFTER_NEWS
HOUR = 21
MIN_TODAY = 30
MAX_MSGS = 220            # последних сообщений дня в промпт


def _today() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d")


async def pending(db: Database, chat_id: int) -> bool:
    """Сегодняшние новости ещё должны выйти в этом чате (клоун дня их ждёт)."""
    if (await db.get_setting(ON_KEY, "1")) != "1" or not ai.available():
        return False
    now = datetime.now(MSK)
    if now.hour < HOUR or now.hour >= HOUR + 1:  # после 22:00 не ждём — что-то пошло не так
        return False
    return (await db.get_setting(DAY_KEY + str(chat_id), "")) != _today()


async def issue(bot: Bot, db: Database, chat_id: int) -> bool:
    """Выпустить новости за сегодня. False — день пустой или ИИ не справился."""
    import cards
    from handlers.roast import _send_card, db_add_own

    now = datetime.now(MSK)
    day_start = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    rows = await db.recent_messages(chat_id, limit=MAX_MSGS, since=day_start)
    rows = [r for r in rows if r[1] != 0]  # свои реплики бота — не новости
    if len(rows) < 10:
        return False
    msgs = [{"id": r[0], "name": r[2] or "Аноним", "text": r[3]} for r in rows]
    date_text = ai.date_words(now.date())
    news = await ai.daily_news(msgs, await db.chat_dossiers(chat_id), date_text)
    if not news["stories"]:
        return False
    no = int(await db.get_setting(NO_KEY + str(chat_id), "0") or 0) + 1
    await db.set_setting(NO_KEY + str(chat_id), str(no))
    heads = [s["headline"] for s in news["stories"]]
    png = await asyncio.to_thread(cards.render_news, now.strftime("%d.%m"), no, heads[0], heads[1:])
    await _send_card(bot, chat_id, png, f"📰 <b>Новости чата · выпуск № {no}</b>",
                     f"[новости чата, выпуск {no}] " + "; ".join(heads))
    lines = []
    if news["intro"]:
        lines += [f"🎙 {html.escape(news['intro'])}", ""]
    for i, s in enumerate(news["stories"], 1):
        lines += [f"<b>{i}. {html.escape(s['headline'])}</b>", html.escape(s["text"]), ""]
    if news["weather"]:
        lines += [f"🌦 <b>Погода в чате на завтра:</b> {html.escape(news['weather'])}", ""]
    if news["outro"]:
        lines.append(f"<i>{html.escape(news['outro'])}</i>")
    sent = await bot.send_message(chat_id, "\n".join(lines).strip()[:4000])
    try:
        await db_add_own(chat_id, sent.message_id, "[новости чата] " + " ".join(
            f"{s['headline']}: {s['text']}" for s in news["stories"]))
    except Exception as e:
        logger.debug("news memory: %s", e)
    await db.set_setting(AT_KEY + str(chat_id), str(int(time.time())))
    logger.info("новости %s: выпуск %s - %s", chat_id, no, " | ".join(heads))
    return True


async def last_issue(db: Database, chat_id: int) -> float:
    """Когда в чате вышли последние новости (ts, 0 — не выходили)."""
    try:
        return float(await db.get_setting(AT_KEY + str(chat_id), "0") or 0)
    except ValueError:
        return 0.0


@router.message(Command("news"))
async def cmd_news(message: Message, bot: Bot, db: Database) -> None:
    """/news — выпуск новостей за сегодня прямо сейчас (обычно выходит сам в 21:00)."""
    from handlers.roast import _bunker_active

    if message.chat.type not in ("group", "supergroup") or _bunker_active(message.chat.id):
        return
    if not ai.available():
        await temp_reply(message, "ИИ сейчас недоступен - диктор заболел.")
        return
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        ok = await issue(bot, db, message.chat.id)
    except Exception as e:
        logger.warning("news failed: %s", e)
        ok = False
    if not ok:
        await temp_reply(message, "Новостей нет: за сегодня в чате почти ничего не было.")


async def tick(bot: Bot, db: Database) -> None:
    """Из фона (раз в 30 сек): в HOUR МСК — выпуск в каждом живом чате, раз в день."""
    if (await db.get_setting(ON_KEY, "1")) != "1" or not ai.available():
        return
    from handlers.roast import _bunker_active

    now = datetime.now(MSK)
    if now.hour < HOUR:
        return
    today = _today()
    day_start = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    for chat_id in await db.active_chats(int(time.time()) - 86400):
        if chat_id > 0 or _bunker_active(chat_id):
            continue
        key = DAY_KEY + str(chat_id)
        if await db.get_setting(key, "") == today:
            continue
        await db.set_setting(key, today)  # сразу: упало — не повторяем каждые 30 сек
        if await db.count_messages_since(chat_id, day_start) < MIN_TODAY:
            continue
        try:
            await issue(bot, db, chat_id)
        except Exception as e:
            logger.warning("news issue failed in %s: %s", chat_id, e)
