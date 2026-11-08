"""Сериал про чат (идея и макет обложки — с владельцем, 07.11).

Каждый день в HOUR МСК (если в чате за день было хотя бы MIN_TODAY сообщений) и по /serial у админа — новая серия:
обложка (cards.render_serial_cover), текст серии и опрос «Что дальше?». Следующая серия начинается с того, что
выбрал чат (опрос закрывается перед ней), и вплетает события дня. Герои — самые активные за день + батя, характеры —
из досье. ИИ — Opus, effort medium (ai.serial_episode, ≈$0.03 за серию). Только в чат, не в канал: канал публичный,
там имён нельзя. Вкл/выкл — /admin («🎬 Сериал»). Состояние — настройка serial:<chat> (сезон, номер, прошлые серии,
опрос).
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

import ai
from database import Database
from ephemeral import temp_reply

logger = logging.getLogger("serial")
router = Router(name="serial")

MSK = timezone(timedelta(hours=3))
ON_KEY = "serial_on"
STATE_KEY = "serial:"
HOUR = 20
MIN_TODAY = 30
CAST_MAX = 5          # кандидаты в герои; в серию ИИ берёт 2-4 (все сразу — была каша)


def _father_name() -> str:
    return ai.FATHER_NAME


async def _state(db: Database, chat_id: int) -> dict:
    try:
        st = json.loads(await db.get_setting(STATE_KEY + str(chat_id), "") or "{}")
    except ValueError:
        st = {}
    st.setdefault("season", 1)
    st.setdefault("n", 0)
    st.setdefault("episodes", [])
    return st


async def _poll_choice(bot: Bot, chat_id: int, st: dict) -> str:
    """Закрыть прошлый опрос «Что дальше?» и вернуть победивший вариант (или пусто)."""
    poll = st.get("poll")
    if not poll:
        return ""
    st["poll"] = None
    try:
        res = await bot.stop_poll(chat_id, poll)
    except Exception as e:
        logger.info("serial poll stop %s: %s", chat_id, e)
        return ""
    opts = [(o.voter_count, o.text) for o in res.options]
    if not opts or max(v for v, _t in opts) == 0:
        return ""
    return max(opts, key=lambda x: x[0])[1]


async def episode(bot: Bot, db: Database, chat_id: int) -> bool:
    """Снять и выпустить новую серию. False — некого снимать (мало народу за день)."""
    import cards
    from handlers.roast import _send_card, db_add_own

    now = datetime.now(MSK)
    day_start = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    rows = await db.recent_messages(chat_id, limit=150, since=day_start)
    if len(rows) < 10:  # день пустой — берём последние сутки
        rows = await db.recent_messages(chat_id, limit=150, since=int(time.time()) - 86400)
    people = [r for r in rows if r[1] > 0]
    counts: dict[str, int] = {}
    for r in people:
        counts[r[2] or "Аноним"] = counts.get(r[2] or "Аноним", 0) + 1
    cast = [n for n, _c in sorted(counts.items(), key=lambda kv: -kv[1])][:CAST_MAX]
    father = _father_name()
    if father not in cast and any((r[1] or "") == father for r in await db.last_seen(chat_id)):
        cast = cast[:CAST_MAX - 1] + [father]  # батя — в каждом сезоне, даже если сегодня молчал
    if len(cast) < 2:
        return False
    st = await _state(db, chat_id)
    vote = await _poll_choice(bot, chat_id, st)
    dossiers = await db.chat_dossiers(chat_id)
    n = st["n"] + 1
    msgs = [{"id": r[0], "name": r[2] or "Аноним", "text": r[3]} for r in people[-60:]]  # сырьё, не сценарий
    ep = await ai.serial_episode(st["season"], n, st["episodes"], vote, msgs, dossiers, cast)
    if not ep["text"] or not ep["title"]:
        return False
    png = await asyncio.to_thread(cards.render_serial_cover, st["season"], n, ep["title"], ep["logline"],
                                  ep["cast"] or cast)
    await _send_card(bot, chat_id, png, f"🎬 <b>Серия {n} · «{html.escape(ep['title'])}»</b>",
                     f"[сериал про чат, серия {n} «{ep['title']}»] {ep['summary']}")
    sent = await bot.send_message(chat_id, html.escape(ep["text"])[:4000])
    try:
        await db_add_own(chat_id, sent.message_id, f"[серия {n}] {ep['text']}")
    except Exception as e:
        logger.debug("serial memory: %s", e)
    poll_id = None
    if len(ep["options"]) >= 2:
        try:
            p = await bot.send_poll(chat_id, "🎬 Что будет в следующей серии?", options=ep["options"],
                                    is_anonymous=False)
            poll_id = p.message_id
        except Exception as e:
            logger.info("serial poll failed: %s", e)
    st["n"] = n
    st["episodes"] = (st["episodes"] + [{"n": n, "title": ep["title"], "summary": ep["summary"]}])[-8:]
    st["poll"] = poll_id
    st["day"] = now.strftime("%Y-%m-%d")
    await db.set_setting(STATE_KEY + str(chat_id), json.dumps(st, ensure_ascii=False))
    logger.info("сериал %s: серия %s «%s» - %s", chat_id, n, ep["title"], ep.get("premise", ""))
    return True


@router.message(Command("serial"))
async def cmd_serial(message: Message, bot: Bot, db: Database) -> None:
    """/serial — новая серия прямо сейчас (обычно выходит сама в 20:00)."""
    from handlers.roast import _bunker_active

    if message.chat.type not in ("group", "supergroup") or _bunker_active(message.chat.id):
        return
    if not ai.available():
        await temp_reply(message, "ИИ сейчас недоступен - сценарист в запое.")
        return
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        ok = await episode(bot, db, message.chat.id)
    except Exception as e:
        logger.warning("serial failed: %s", e)
        ok = False
    if not ok:
        await temp_reply(message, "Серия не снялась: мало героев или сценарист не справился - попробуй позже.")


AUTO = False  # 08.11 владелец: «отключай сериал в чате» — сам больше не выходит (вместо него новости в 21:00);
#               /serial вручную у админа остался


async def tick(bot: Bot, db: Database) -> None:
    """Из фона: раз в день в HOUR МСК — серия в каждом живом чате (выключено: AUTO = False)."""
    if not AUTO or (await db.get_setting(ON_KEY, "1")) != "1" or not ai.available():
        return
    from handlers.roast import _bunker_active

    now = datetime.now(MSK)
    if now.hour < HOUR:
        return
    today = now.strftime("%Y-%m-%d")
    day_start = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    for chat_id in await db.active_chats(int(time.time()) - 86400):
        if chat_id > 0 or _bunker_active(chat_id):
            continue
        key = f"serial_day:{chat_id}"
        if await db.get_setting(key, "") == today:
            continue
        await db.set_setting(key, today)
        if await db.count_messages_since(chat_id, day_start) < MIN_TODAY:
            continue
        try:
            await episode(bot, db, chat_id)
        except Exception as e:
            logger.warning("serial episode failed in %s: %s", chat_id, e)
