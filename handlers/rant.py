"""Голосовое Пиздюка в канал: наезд на подписоту.

08.11 владелец: «чтобы бот голосом, который стоит с /golos, иногда в канал обидную херню про подписоту выкидывал и
говорил, чтобы реакции ставили иногда»; примеры одобрил («Давай»): раз в день в случайное время, сразу в канал, без
одобрения. Потом: «без упоминаний, какие реакции ставить — у меня не все доступны» и «иногда пусть рассказывает
историю и смешно шутит, связанное с каналом». По кругу (счёт `rant_no`): наезд → смешная история → наезд с просьбой
поставить реакцию (любую, не называя).

Время дня выбирается на первом тике дня — случайное между FROM_HOUR и TO_HOUR МСК (если бот запустился посреди дня —
не раньше чем через 10 минут). Текст — ai.channel_rant (Opus medium, ≈$0.02; тема из ai.RANT_TOPICS без повторов
последних TOPIC_GAP, история — завязка из ai.RANT_STORIES, прошлые голосовые — «не повторяй»), с пометками
интонации; голос — ai.roast_speech (тот, что в /golos, ElevenLabs v4). Без ключа ElevenLabs не выходит.
/podpisota — выкинуть сейчас (/podpisota история, /podpisota реакции). Вкл/выкл — /admin «🎙 Голосовые в канал».

Ответы на комменты (владелец 09.11 выбрал «Отвечает на комменты»): под своим голосовым в канале Пиздюк отвечает
голосом прямо в обсуждении — 1-2 самым интересным, не на всё подряд. Голосовое канала приходит в группу комментариев
автоматической пересылкой — её id ловит observe (из middleware в bot.py), комментарии в этой ветке копятся; когда
QUIET секунд тихо (или прошло WAIT_MAX), ai.rant_replies (Opus, low, ≈$0.01) выбирает лучшие и пишет ответы, бот
шлёт их голосом ответом на коммент (roast._say_voice: дальше с человеком «голосовой разговор»). Под одним голосовым —
не больше REPLY_MAX ответов, не дольше REPLY_WINDOW после выхода. Обращения к боту («Пиздюк, …», ответ на его
сообщение) сюда не идут — их ведёт обычный ответ стёб-бота, но в такой ветке тоже голосом (in_thread).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
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
NO_KEY = "rant_no"          # сколько вышло всего: по кругу наезд → история → наезд с реакциями
TEXTS_KEY = "rant_texts"    # прошлые тексты — не повторяться
TOPICS_KEY = "rant_topics"  # прошлые темы
FROM_HOUR, TO_HOUR = 12, 23
CYCLE = ("rant", "story", "cta")  # что выходит по кругу
TOPIC_GAP = 6
_lock = asyncio.Lock()

POSTS_KEY = "rant_posts"      # свои голосовые: [{"chan", "msg", "ts", "text"}] — под ними отвечает на комменты
THREADS_KEY = "rant_threads"  # их копии в обсуждении: [{"chat", "root", "ts", "text", "done": [id комментов]}]
REPLY_MAX = 2                 # ответов под одним голосовым всего
REPLY_WINDOW = 12 * 3600      # дольше после выхода голосового на комменты не отвечаем
QUIET = 150                   # столько секунд никто не пишет — пора выбирать, на что ответить
WAIT_MAX = 8 * 60             # но не дольше этого с первого коммента
_cache: dict = {"loaded": False, "posts": [], "threads": []}
_buf: dict[tuple[int, int], dict] = {}   # (чат, root) → {"items": [...], "first", "last"} — ждут выбора
_EMPTY_COMMENT = re.compile(r"^[\W\d_]*(?:\+|база|жиза|ору|лол|кек|ахах\w*|хаха\w*|согласен|согласна|лайк|топ|"
                            r"класс|огонь|красава|норм|ок|да|нет|ага|угу)?[\W\d_]*$", re.IGNORECASE)


def _today() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d")


def _load(raw: str) -> list:
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return []
    return data if isinstance(data, list) else []


async def post(bot: Bot, db: Database, kind: str | None = None) -> str:
    """Записать и выложить голосовое в канал. kind — rant / story / cta (None — по кругу CYCLE).
    "" — вышло, иначе — почему нет (для админа и лога)."""
    from handlers import channel_posts
    from handlers.roast import voice_note

    if not ai.available():
        return "ИИ сейчас недоступен"
    if not ai.eleven_key() or ai.eleven_geo_blocked():
        return "нужен ElevenLabs: /setkey eleven ключ (в личку боту)"
    chan = await channel_posts.channel_id(bot, db)
    if chan is None:
        return "не нашёл канал: привяжи его к чату (Канал → Обсуждение) или впиши BOT_CHANNEL_ID в .env"
    await _load_cache(db)
    async with _lock:
        no = int(await db.get_setting(NO_KEY, "0") or 0) + 1
        kind = kind or CYCLE[(no - 1) % len(CYCLE)]
        texts = _load(await db.get_setting(TEXTS_KEY, "[]"))
        topics = _load(await db.get_setting(TOPICS_KEY, "[]"))
        pool = ai.RANT_STORIES if kind == "story" else ai.RANT_TOPICS
        topic = random.choice([t for t in pool if t not in topics[-TOPIC_GAP:]] or pool)
        text = await ai.channel_rant(texts, topic, cta=kind == "cta", story=kind == "story")
        if not ai.strip_tags(text):
            return "ИИ ничего не придумал"
        note = await voice_note(text)
        if not note:
            return f"не озвучилось ({ai.eleven_error[0] or 'ElevenLabs молчит'})"
        ogg, secs = note
        try:
            msg = await bot.send_voice(chan, BufferedInputFile(ogg, "pizdyuk.ogg"), duration=secs)
        except Exception as e:
            return f"не могу выложить в канал (бот — админ канала с правом публикации?): {e}"
        _note_post(chan, msg.message_id, ai.strip_tags(text))  # сразу, до await: пересылка в обсуждение — следом
        await channel_posts.mark_own(db, msg.message_id)  # свой пост — стёб-бот его в комментариях не обсуждает
        await _save(db)
        await db.set_setting(NO_KEY, str(no))
        await db.set_setting(TEXTS_KEY, json.dumps((texts + [ai.strip_tags(text)])[-20:], ensure_ascii=False))
        await db.set_setting(TOPICS_KEY, json.dumps((topics + [topic])[-20:], ensure_ascii=False))
        logger.info("голосовое в канал №%d (%s: %s, %d с): %s", no, kind, topic, secs, ai.strip_tags(text)[:200])
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
    """/podpisota — голосовое в канал сейчас (по кругу); /podpisota история / реакции / наезд — что именно."""
    arg = (command.args or "").strip().lower()
    kind = ("story" if arg.startswith("ист") else "cta" if arg.startswith("реак") else
            "rant" if arg.startswith("наезд") else None)
    if _lock.locked():
        await temp_reply(message, "🎙 Уже записываю - минуту.")
        return
    await temp_reply(message, "🎙 Записываю голосовое в канал…")
    try:
        await bot.send_chat_action(message.chat.id, "record_voice")
    except Exception:
        pass
    why = await post(bot, db, kind=kind)
    await temp_reply(message, "🎙 Голосовое в канале." if not why else f"Не вышло: {why}.")


# ---------- ответы на комменты под своим голосовым ----------

async def _load_cache(db: Database) -> None:
    if _cache["loaded"]:
        return
    _cache["posts"] = [p for p in _load(await db.get_setting(POSTS_KEY, "[]")) if isinstance(p, dict)]
    _cache["threads"] = [t for t in _load(await db.get_setting(THREADS_KEY, "[]")) if isinstance(t, dict)]
    _cache["loaded"] = True


async def _save(db: Database) -> None:
    _cache["posts"] = _cache["posts"][-10:]
    _cache["threads"] = _cache["threads"][-10:]
    await db.set_setting(POSTS_KEY, json.dumps(_cache["posts"], ensure_ascii=False))
    await db.set_setting(THREADS_KEY, json.dumps(_cache["threads"], ensure_ascii=False))


def _note_post(chan: int, msg_id: int, text: str) -> None:
    _cache["posts"].append({"chan": chan, "msg": msg_id, "ts": int(time.time()), "text": text[:1500]})


def _thread_of(chat_id: int, message: Message) -> dict | None:
    """Ветка комментариев под своим голосовым, в которой это сообщение (ещё не старая), или None."""
    rt = message.reply_to_message
    for th in _cache["threads"]:
        if th.get("chat") != chat_id or time.time() - th.get("ts", 0) > REPLY_WINDOW:
            continue
        if message.message_thread_id == th.get("root") or (rt and rt.message_id == th.get("root")):
            return th
    return None


def in_thread(chat_id: int, message: Message) -> bool:
    """Сообщение — в комментариях под голосовым Пиздюка (стёб-бот отвечает там голосом)."""
    return _cache["loaded"] and _thread_of(chat_id, message) is not None


async def _addressed(message: Message, text: str, bot: Bot) -> bool:
    """Обращение к боту — на него ответит обычный путь стёб-бота (тут не дублируем)."""
    from handlers.roast import _BOT_NAME, _BOT_WORD

    rt = message.reply_to_message
    if (rt and rt.from_user and rt.from_user.id == bot.id) or _BOT_NAME.search(text) or _BOT_WORD.search(text):
        return True
    me = await bot.me()
    return bool(me.username and f"@{me.username.lower()}" in text.lower())


async def observe(message: Message, bot: Bot, db: Database, config=None) -> None:
    """Из middleware: копия своего голосового в группе комментариев и комментарии под ней."""
    if message.chat.type not in ("group", "supergroup"):
        return
    await _load_cache(db)
    if not _cache["posts"]:
        return
    chat_id = message.chat.id
    if message.is_automatic_forward:
        fo = message.forward_origin
        chan, mid = getattr(getattr(fo, "chat", None), "id", None), getattr(fo, "message_id", None)
        post = next((p for p in _cache["posts"] if p.get("chan") == chan and p.get("msg") == mid), None)
        if post and not any(t.get("chat") == chat_id and t.get("root") == message.message_id
                            for t in _cache["threads"]):
            _cache["threads"].append({"chat": chat_id, "root": message.message_id, "ts": post.get("ts", 0),
                                      "text": post.get("text", ""), "done": []})
            await _save(db)
        return
    th = _thread_of(chat_id, message)
    if th is None or len(th.get("done", [])) >= REPLY_MAX:
        return
    u = message.from_user
    text = (message.text or message.caption or "").strip()
    if (not u or u.is_bot or message.sender_chat or not text or text.startswith("/") or _EMPTY_COMMENT.match(text)
            or await _addressed(message, text, bot)):
        return
    if (await db.get_setting(ON_KEY, "1")) != "1" or await db.is_optout(chat_id, u.id):
        return
    from handlers.roast import _father_id

    father = u.id == _father_id(config)
    name = (u.first_name or u.username or "Аноним").strip()[:30]
    key = (chat_id, th["root"])
    now = time.time()
    buf = _buf.get(key)
    item = {"id": message.message_id, "name": f"{name} (это твой отец {ai.FATHER_NAME})" if father else name,
            "text": text[:400], "uid": u.id, "father": father}
    if buf:
        buf["items"].append(item)
        buf["last"] = now
        return
    _buf[key] = {"items": [item], "first": now, "last": now}
    task = asyncio.create_task(_answer_later(bot, db, key))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


_tasks: set[asyncio.Task] = set()


async def _answer_later(bot: Bot, db: Database, key: tuple[int, int]) -> None:
    """Ждём, пока в ветке стихнет (QUIET) или пройдёт WAIT_MAX, — и отвечаем на 1-2 лучших коммента."""
    try:
        while True:
            buf = _buf[key]
            now = time.time()
            if now - buf["last"] >= QUIET or now - buf["first"] >= WAIT_MAX:
                break
            await asyncio.sleep(min(15.0, QUIET))
        await _answer(bot, db, key, _buf.pop(key)["items"])
    except Exception:
        logger.exception("ответ на комменты под голосовым не вышел")
    finally:
        _buf.pop(key, None)


async def _answer(bot: Bot, db: Database, key: tuple[int, int], items: list[dict]) -> None:
    from handlers import roast

    chat_id, root = key
    th = next((t for t in _cache["threads"] if t.get("chat") == chat_id and t.get("root") == root), None)
    if th is None or not ai.available() or roast._bunker_active(chat_id) or not await roast._enabled(db):
        return
    done = th.setdefault("done", [])
    left = REPLY_MAX - len(done)
    items = [it for it in items if it["id"] not in done]
    if left <= 0 or not items:
        return
    await roast._remember_lines(db, chat_id)
    replies = await ai.rant_replies(th.get("text", ""), items, left)
    logger.info("комменты под голосовым %s: %d шт., отвечаю на %d", root, len(items), len(replies))
    by_id = {it["id"]: it for it in items}
    for i, rep in enumerate(replies):
        it = by_id[rep["id"]]
        text = await ai.polish(rep["text"], it["text"], author=it["name"], max_words=ai.RANT_REPLY_MAX_WORDS)
        if not text:
            continue
        if i:
            await asyncio.sleep(random.uniform(4.0, 9.0))  # не залпом — «послушал следующий»
        done.append(it["id"])
        await _save(db)
        roast._mark("ответ на коммент под голосовым в канале", rep["text"])
        await roast._set_voice_mood(chat_id, bool(roast._RUDE.search(it["text"])), it["father"])
        if not await roast._say_voice(bot, chat_id, text, reply_to=it["id"], to_user=it["uid"]):
            await roast._say_human(bot, chat_id, text, reply_to=it["id"])  # не озвучилось — текстом
        logger.info("ответ на коммент %s (%s): %s", it["id"], it["name"], text[:160])
