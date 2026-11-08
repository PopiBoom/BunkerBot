"""Голосовое Пиздюка в канал: наезд на подписоту.

08.11 владелец: «чтобы бот голосом, который стоит с /golos, иногда в канал обидную херню про подписоту выкидывал и
говорил, чтобы реакции ставили иногда»; примеры одобрил («Давай»): раз в день в случайное время, каждое третье — с
требованием реакций, сразу в канал, без одобрения.

Время дня выбирается на первом тике дня — случайное между FROM_HOUR и TO_HOUR МСК (если бот запустился посреди дня —
не раньше чем через 10 минут). Текст — ai.channel_rant (Opus medium, ≈$0.02; тема из ai.RANT_TOPICS без повторов
последних TOPIC_GAP, прошлые голосовые — «не повторяй»), с пометками интонации; голос — ai.roast_speech (тот, что в
/golos, ElevenLabs v4). Реакцию просит из разрешённых в канале (словами). Без ключа ElevenLabs не выходит.
/podpisota — выкинуть сейчас (/podpisota реакции — с просьбой о реакциях). Вкл/выкл — /admin «🎙 Голосовые в канал».
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message

import ai
from database import Database
from ephemeral import temp_reply

logger = logging.getLogger("rant")
router = Router(name="rant")

MSK = timezone(timedelta(hours=3))
ON_KEY = "rant_on"
DAY_KEY = "rant_day"        # день, когда голосовое уже вышло (или пробовали — упало, не повторяем весь день)
SLOT_KEY = "rant_slot"      # «ГГГГ-ММ-ДД ЧЧ:ММ» — во сколько сегодня
NO_KEY = "rant_no"          # сколько вышло всего (каждое CTA_EVERY-е — с реакциями)
TEXTS_KEY = "rant_texts"    # прошлые тексты — не повторяться
TOPICS_KEY = "rant_topics"  # прошлые темы
FROM_HOUR, TO_HOUR = 12, 23
CTA_EVERY = 3
TOPIC_GAP = 6
_lock = asyncio.Lock()

# какие реакции бот может попросить — и как их назвать вслух
REACTION_NAMES = {"🔥": "огонёк", "🤡": "клоуна", "💩": "какашку", "👻": "привидение", "🖕": "средний палец",
                  "😈": "чертёнка", "💯": "сотку", "🙈": "обезьянку", "👀": "глаза", "😭": "рыдающую рожу"}


def _today() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d")


def _load(raw: str) -> list:
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return []
    return data if isinstance(data, list) else []


async def _reactions(bot: Bot, chan: int) -> list[str]:
    """Какие реакции разрешены в канале — словами. Ограничений нет (или не узнали) — весь наш список."""
    try:
        allowed = getattr(await bot.get_chat(chan), "available_reactions", None)
    except Exception:
        allowed = None
    if allowed:
        emojis = {getattr(r, "emoji", None) for r in allowed}
        names = [n for e, n in REACTION_NAMES.items() if e in emojis]
        if names:
            return names
    return list(REACTION_NAMES.values())


async def post(bot: Bot, db: Database, cta: bool | None = None) -> str:
    """Записать и выложить голосовое в канал. "" — вышло, иначе — почему нет (для админа и лога)."""
    import radio
    from handlers import channel_posts

    if not ai.available():
        return "ИИ сейчас недоступен"
    if not ai.eleven_key() or ai.eleven_geo_blocked():
        return "нужен ElevenLabs: /setkey eleven ключ (в личку боту)"
    chan = await channel_posts.channel_id(bot, db)
    if chan is None:
        return "не нашёл канал: привяжи его к чату (Канал → Обсуждение) или впиши BOT_CHANNEL_ID в .env"
    async with _lock:
        no = int(await db.get_setting(NO_KEY, "0") or 0) + 1
        if cta is None:
            cta = no % CTA_EVERY == 0
        texts = _load(await db.get_setting(TEXTS_KEY, "[]"))
        topics = _load(await db.get_setting(TOPICS_KEY, "[]"))
        topic = random.choice([t for t in ai.RANT_TOPICS if t not in topics[-TOPIC_GAP:]] or ai.RANT_TOPICS)
        text = await ai.channel_rant(texts, topic, cta, await _reactions(bot, chan) if cta else [])
        if not ai.strip_tags(text):
            return "ИИ ничего не придумал"
        pcm = await ai.roast_speech(text)
        if not pcm:
            return f"не озвучилось ({ai.eleven_error[0] or 'ElevenLabs молчит'})"
        x = radio._resample(pcm, ai.SPEAK_RATE)
        x *= 0.9 / (float(abs(x).max()) or 1.0)
        ogg = await asyncio.to_thread(radio.to_ogg, x)
        secs = max(1, round(len(pcm) / 2 / ai.SPEAK_RATE))
        try:
            msg = await bot.send_voice(chan, BufferedInputFile(ogg, "pizdyuk.ogg"), duration=secs)
        except Exception as e:
            return f"не могу выложить в канал (бот — админ канала с правом публикации?): {e}"
        await channel_posts.mark_own(db, msg.message_id)  # свой пост — стёб-бот его в комментариях не обсуждает
        await db.set_setting(NO_KEY, str(no))
        await db.set_setting(TEXTS_KEY, json.dumps((texts + [ai.strip_tags(text)])[-20:], ensure_ascii=False))
        await db.set_setting(TOPICS_KEY, json.dumps((topics + [topic])[-20:], ensure_ascii=False))
        logger.info("голосовое в канал №%d (%s, %d с%s): %s", no, topic, secs, ", с реакциями" if cta else "",
                    ai.strip_tags(text)[:200])
        return ""


async def tick(bot: Bot, db: Database) -> None:
    """Из фона стёб-бота: раз в день, в случайное время FROM_HOUR-TO_HOUR МСК."""
    if (await db.get_setting(ON_KEY, "1")) != "1":
        return
    now = datetime.now(MSK)
    today = _today()
    if (await db.get_setting(DAY_KEY, "")) == today:
        return
    slot = await db.get_setting(SLOT_KEY, "")
    if not slot.startswith(today):
        earliest = max(FROM_HOUR * 60, now.hour * 60 + now.minute + 10)  # запустились посреди дня — не сразу
        if earliest >= TO_HOUR * 60:
            await db.set_setting(DAY_KEY, today)  # сегодня уже поздно
            return
        at = random.randint(earliest, TO_HOUR * 60 - 1)
        slot = f"{today} {at // 60:02d}:{at % 60:02d}"
        await db.set_setting(SLOT_KEY, slot)
        logger.info("голосовое в канал сегодня в %s", slot[-5:])
    if now.strftime("%Y-%m-%d %H:%M") < slot:
        return
    await db.set_setting(DAY_KEY, today)  # сразу: упало — не повторяем каждые 30 сек
    why = await post(bot, db)
    if why:
        logger.warning("голосовое в канал не вышло: %s", why)


@router.message(Command("podpisota"))
async def cmd_podpisota(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    """/podpisota — голосовое в канал сейчас; /podpisota реакции — с требованием реакций."""
    cta = True if (command.args or "").strip().lower().startswith("реак") else None
    if _lock.locked():
        await temp_reply(message, "🎙 Уже записываю - минуту.")
        return
    await temp_reply(message, "🎙 Записываю голосовое в канал…")
    try:
        await bot.send_chat_action(message.chat.id, "record_voice")
    except Exception:
        pass
    why = await post(bot, db, cta=cta)
    await temp_reply(message, "🎙 Голосовое в канале." if not why else f"Не вышло: {why}.")
