"""Голосовое Пиздюка в канал: наезд на подписоту и смешные истории.

08.11 владелец: «чтобы бот голосом, который стоит с /golos, иногда в канал обидную херню про подписоту выкидывал и
говорил, чтобы реакции ставили иногда»; примеры одобрил: раз в день в случайное время. Потом: «без упоминаний, какие
реакции ставить — у меня не все доступны» и «иногда пусть рассказывает историю и смешно шутит, связанное с каналом».
По кругу (счёт `rant_no`): наезд → смешная история → наезд с просьбой поставить реакцию (любую, не называя).

Брифинг 09.11 (решения владельца):
- сначала ЧЕРНОВИК админу в личку — «✅ В канал / 🔄 Переписать / 🗑 Выкинуть» (rn:…), в канал — только по кнопке;
- истории трёх видов по очереди (STORY_KINDS): про себя и канал (ai.RANT_STORIES), байки про батю — выдуманные, без
  настоящих фактов его жизни (ai.RANT_FATHER_STORIES), про выдуманного подписчика без имён (ai.RANT_FAN_STORIES);
- тон «разный»: настроение выпуска (ai.RANT_MOODS — злой, нытьё, подлиза, пафос, обида, заговорщик) не повторяет
  последние MOOD_GAP;
- по имени можно тех, кто сам писал в комментариях под прошлым голосовым, и только про то, что написал;
- «только учиться»: реакции (message_reaction_count — бот админ канала) и комменты под каждым голосовым → счёт; чаще
  то настроение, вид истории и тема, что заходят (`_weights`), лучшие и худшие голосовые — ИИ образцом; вслух про это
  не говорит. Частота и форматы — как были (раз в день, наезд → история → реакции).

Время дня выбирается на первом тике дня — случайное между FROM_HOUR и TO_HOUR МСК (если бот запустился посреди дня —
не раньше чем через 10 минут). Текст — ai.channel_rant (Opus medium, ≈$0.025), с пометками интонации; голос —
ai.roast_speech (тот, что в /golos, ElevenLabs v4). Без ключа ElevenLabs не выходит.
/podpisota — черновик сейчас (/podpisota история, /podpisota реакции, /podpisota наезд). Вкл/выкл — /admin
«🎙 Голосовые в канал».

Ответы на комменты (владелец 09.11 выбрал «Отвечает на комменты»): под своим голосовым в канале Пиздюк отвечает
голосом прямо в обсуждении — 1-2 самым интересным, не на всё подряд. Голосовое канала приходит в группу комментариев
автоматической пересылкой — её id ловит observe (из middleware в bot.py), комментарии в этой ветке копятся; когда
QUIET секунд тихо (или прошло WAIT_MAX), ai.rant_replies (Opus, low, ≈$0.015) выбирает лучшие и пишет ответы, бот
шлёт их голосом ответом на коммент (roast._say_voice: дальше с человеком «голосовой разговор»). Под одним голосовым —
не больше REPLY_MAX ответов, не дольше REPLY_WINDOW после выхода. Обращения к боту («Пиздюк, …», ответ на его
сообщение) сюда не идут — их ведёт обычный ответ стёб-бота, но в такой ветке тоже голосом (in_thread).
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
                           MessageReactionCountUpdated)

import ai
from config import Config
from database import Database
from ephemeral import temp_reply

logger = logging.getLogger("rant")
router = Router(name="rant")

MSK = timezone(timedelta(hours=3))
ON_KEY = "rant_on"
DAY_KEY = "rant_day"        # день, когда черновик уже был (или пробовали — упало, не повторяем весь день)
SLOT_KEY = "rant_slot"      # «ГГГГ-ММ-ДД ЧЧ:ММ» — во сколько сегодня
NO_KEY = "rant_no"          # сколько вышло всего: по кругу наезд → история → наезд с реакциями
TEXTS_KEY = "rant_texts"    # прошлые тексты — не повторяться
TOPICS_KEY = "rant_topics"  # прошлые темы
MOODS_KEY = "rant_moods"    # прошлые настроения
STORY_KEY = "rant_story"    # прошлые виды историй
DRAFT_KEY = "rant_draft"    # черновик у админа в личке
FROM_HOUR, TO_HOUR = 12, 23
CYCLE = ("rant", "story", "cta")  # что выходит по кругу
KIND_LABELS = {"rant": "наезд", "story": "история", "cta": "наезд + просьба о реакции"}
STORY_KINDS = {"self": "про себя и канал", "father": "байка про батю", "fans": "про подписоту"}
TOPIC_GAP = 6
MOOD_GAP = 2
LEARN_MIN = 4               # столько голосовых с посчитанными реакциями — тогда уже учимся
LEARN_AFTER = 6 * 3600      # реакции голосового считаются «устоявшимися» через 6 ч
LEARN_WINDOW = 3 * 86400    # комменты под голосовым считаем 3 дня
_lock = asyncio.Lock()

POSTS_KEY = "rant_posts"      # свои голосовые: [{"chan", "msg", "ts", "text", "kind", "sub", "topic", "mood",
#                               "reactions", "comments"}] — под ними отвечает на комменты и по ним учится
THREADS_KEY = "rant_threads"  # их копии в обсуждении: [{"chat", "root", "post", "ts", "text", "done": [id], "said"}]
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


def _owner() -> int:
    raw = os.getenv("BOT_FATHER_ID", "") or os.getenv("ADMIN_IDS", "").replace(";", ",").split(",")[0]
    try:
        return int(raw.strip())
    except ValueError:
        return 0


# ---------- учёба на реакциях (владелец 09.11: «только учиться» — молча, без упоминаний) ----------

def _score(p: dict) -> float:
    return float(p.get("reactions", 0)) + 2.0 * float(p.get("comments", 0))


def _scored() -> list[dict]:
    """Свои голосовые, у которых реакции уже устоялись (и записан вид/настроение)."""
    now = time.time()
    return [p for p in _cache["posts"] if p.get("kind") and now - p.get("ts", now) >= LEARN_AFTER]


def _weights(field: str, options: list[str]) -> list[float]:
    """Вес каждого варианта по тому, как заходили голосовые с ним: среднее с поправкой на мало данных, 0.4-2.5."""
    posts = _scored()
    if len(posts) < LEARN_MIN:
        return [1.0] * len(options)
    avg = sum(_score(p) for p in posts) / len(posts) or 1.0
    out = []
    for o in options:
        got = [_score(p) for p in posts if p.get(field) == o]
        mean = (sum(got) + 2 * avg) / (len(got) + 2)  # пара «средних» голосовых в запас — мало данных не перекашивает
        out.append(min(2.5, max(0.4, mean / avg)))
    return out


def _pick(field: str, options: list[str], avoid: list[str]) -> str:
    pool = [o for o in options if o not in avoid] or list(options)
    return random.choices(pool, weights=_weights(field, pool))[0]


def _examples() -> tuple[list[str], list[str]]:
    """(зашли, не зашли) — по два голосовых, если есть из чего выбирать."""
    posts = sorted(_scored(), key=_score)
    if len(posts) < LEARN_MIN or _score(posts[-1]) <= _score(posts[0]):
        return [], []
    return [p["text"] for p in posts[-2:][::-1]], [p["text"] for p in posts[:2] if _score(p) < _score(posts[-1])]


def _taste_line() -> str:
    """Строка для черновика: что заходит лучше (админу видно, в голосовое не идёт)."""
    posts = _scored()
    if len(posts) < LEARN_MIN:
        return ""
    best = []
    for field, options, labels in (("mood", list(ai.RANT_MOODS), {}), ("sub", list(STORY_KINDS), STORY_KINDS)):
        w = _weights(field, options)
        top = max(range(len(options)), key=lambda i: w[i])
        if w[top] > 1.15:
            best.append(labels.get(options[top], options[top]))
    return f"📈 Лучше заходит: {', '.join(best)}" if best else ""


def _last_comments() -> list[dict]:
    """Что писали под последним вышедшим голосовым (если оно не старше 3 дней) — одного можно назвать по имени."""
    if not _cache["posts"]:
        return []
    last = _cache["posts"][-1]
    if time.time() - last.get("ts", 0) > LEARN_WINDOW:
        return []
    th = next((t for t in _cache["threads"] if t.get("post") == last.get("msg")), None)
    return list((th or {}).get("said") or [])[-6:]


# ---------- черновик → канал ----------

def _kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ В канал", callback_data="rn:pub"),
        InlineKeyboardButton(text="🔄 Переписать", callback_data="rn:redo"),
        InlineKeyboardButton(text="🗑 Выкинуть", callback_data="rn:del"),
    ]])


async def _get_draft(db: Database) -> dict:
    try:
        d = json.loads(await db.get_setting(DRAFT_KEY, "") or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


async def draft(bot: Bot, db: Database, to: int, kind: str | None = None, sub: str | None = None,
                skip: list[str] | None = None) -> str:
    """Записать голосовое и прислать черновиком в личку `to`. kind — rant / story / cta (None — по кругу CYCLE),
    sub — вид истории (None — по очереди и по реакциям), skip — тексты/темы, которые админ только что забраковал.
    "" — прислал, иначе — почему нет (для админа и лога)."""
    from handlers.roast import voice_note

    if not ai.available():
        return "ИИ сейчас недоступен"
    if not ai.eleven_key() or ai.eleven_geo_blocked():
        return "нужен ElevenLabs: /setkey eleven ключ (в личку боту)"
    await _load_cache(db)
    async with _lock:
        no = int(await db.get_setting(NO_KEY, "0") or 0) + 1
        kind = kind or CYCLE[(no - 1) % len(CYCLE)]
        texts = _load(await db.get_setting(TEXTS_KEY, "[]")) + list(skip or [])
        topics = _load(await db.get_setting(TOPICS_KEY, "[]")) + list(skip or [])
        moods = _load(await db.get_setting(MOODS_KEY, "[]"))
        if kind == "story":
            sub = sub or _pick("sub", list(STORY_KINDS), _load(await db.get_setting(STORY_KEY, "[]"))[-1:])
            pool = {"father": ai.RANT_FATHER_STORIES, "fans": ai.RANT_FAN_STORIES}.get(sub, ai.RANT_STORIES)
        else:
            sub, pool = "", ai.RANT_TOPICS
        topic = _pick("topic", [t for t in pool if t not in topics[-TOPIC_GAP:]] or pool, [])
        mood = _pick("mood", list(ai.RANT_MOODS), moods[-MOOD_GAP:])
        liked, disliked = _examples()
        ai.voice_mood.set("")
        text = await ai.channel_rant(texts, topic, cta=kind == "cta", story=kind == "story", mood=mood,
                                     story_kind=sub or "self", comments=_last_comments(), liked=liked,
                                     disliked=disliked)
        clean = ai.strip_tags(text)
        if not clean:
            return "ИИ ничего не придумал"
        note = await voice_note(text)
        if not note:
            return f"не озвучилось ({ai.eleven_error[0] or 'ElevenLabs молчит'})"
        ogg, secs = note
        label = KIND_LABELS[kind] + (f" · {STORY_KINDS[sub]}" if sub else "")
        caption = (f"🎙 <b>Голосовое в канал</b> · №{no} · {label} · настроение: {mood}\n"
                   f"Тема: {html.escape(topic)}\n"
                   + (f"<i>{html.escape(_taste_line())}</i>\n" if _taste_line() else "")
                   + f"<blockquote expandable>{html.escape(clean[:800])}</blockquote>")
        old = await _get_draft(db)
        msg = await bot.send_voice(to, BufferedInputFile(ogg, "pizdyuk.ogg"), duration=secs, caption=caption[:1024],
                                   reply_markup=_kb())
        await db.set_setting(DRAFT_KEY, json.dumps({
            "msg": msg.message_id, "chat": to, "file_id": msg.voice.file_id if msg.voice else "", "secs": secs,
            "text": clean, "kind": kind, "sub": sub, "topic": topic, "mood": mood, "no": no,
            "caption": caption[:900]}, ensure_ascii=False))
        if old.get("msg") and old.get("chat"):  # прошлый черновик так и висел — кнопки у него больше не работают
            try:
                await bot.edit_message_reply_markup(chat_id=old["chat"], message_id=old["msg"], reply_markup=None)
            except Exception as e:
                logger.debug("old rant draft: %s", e)
        logger.info("черновик голосового №%d (%s, %s, %s, %d с): %s", no, label, mood, topic, secs, clean[:200])
        return ""


async def publish(bot: Bot, db: Database, d: dict) -> str:
    """Черновик — в канал. "" — вышло, иначе почему нет."""
    from handlers import channel_posts

    chan = await channel_posts.channel_id(bot, db)
    if chan is None:
        return "не нашёл канал: привяжи его к чату (Канал → Обсуждение) или впиши BOT_CHANNEL_ID в .env"
    await _load_cache(db)
    try:
        msg = await bot.send_voice(chan, d["file_id"], duration=d.get("secs") or None)
    except Exception as e:
        return f"не могу выложить в канал (бот — админ канала с правом публикации?): {e}"
    _note_post(chan, msg.message_id, d["text"], d)  # сразу, до await: пересылка в обсуждение — следом
    await channel_posts.mark_own(db, msg.message_id)  # свой пост — стёб-бот его в комментариях не обсуждает
    await _save(db)
    for key, value in ((TEXTS_KEY, d["text"]), (TOPICS_KEY, d["topic"]), (MOODS_KEY, d["mood"])):
        await db.set_setting(key, json.dumps((_load(await db.get_setting(key, "[]")) + [value])[-20:],
                                             ensure_ascii=False))
    if d.get("sub"):
        await db.set_setting(STORY_KEY, json.dumps((_load(await db.get_setting(STORY_KEY, "[]")) + [d["sub"]])[-10:]))
    await db.set_setting(NO_KEY, str(d["no"]))
    logger.info("голосовое в канал №%s (%s, %s): %s", d["no"], d["kind"], d["mood"], d["text"][:160])
    return ""


@router.callback_query(F.data.startswith("rn:"))
async def cb_draft(call: CallbackQuery, bot: Bot, db: Database, config: Config) -> None:
    if not config.is_admin(call.from_user.id):
        await call.answer("Это для админа.", show_alert=True)
        return
    d = await _get_draft(db)
    action = (call.data or "")[3:]
    if not d or not call.message or d.get("msg") != call.message.message_id:
        await call.answer("Этот черновик уже неактуален.", show_alert=True)
        return

    async def mark(text: str) -> None:
        try:
            await bot.edit_message_caption(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                           caption=f"{d.get('caption', '')}\n\n{text}", reply_markup=None)
        except TelegramBadRequest:
            pass

    if action == "pub":
        await db.set_setting(DRAFT_KEY, "")  # сразу — двойное нажатие не выложит дважды
        why = await publish(bot, db, d)
        if why:
            await db.set_setting(DRAFT_KEY, json.dumps(d, ensure_ascii=False))
            await call.answer(why[:190], show_alert=True)
            return
        await call.answer("В канале.")
        await mark("✅ Опубликовано в канале.")
    elif action == "del":
        await db.set_setting(DRAFT_KEY, "")
        await call.answer("Выкинул.")
        await mark("🗑 Выкинуто.")
    elif action == "redo":
        if _lock.locked():
            await call.answer("Уже записываю - минуту.", show_alert=True)
            return
        await call.answer("Переписываю - минуту.")
        await mark("🔄 Переписан - новый ниже.")
        why = await draft(bot, db, call.message.chat.id, kind=d.get("kind"), sub=d.get("sub") or None,
                          skip=[d.get("text", ""), d.get("topic", "")])
        if why:
            await bot.send_message(call.message.chat.id, f"Не вышло: {html.escape(why)}.")


@router.message_reaction_count()
async def on_reactions(event: MessageReactionCountUpdated, db: Database) -> None:
    """Реакции под своим голосовым в канале (бот — админ канала): счёт для учёбы."""
    await _load_cache(db)
    p = next((p for p in _cache["posts"] if p.get("chan") == event.chat.id and p.get("msg") == event.message_id), None)
    if p is None:
        return
    p["reactions"] = sum(r.total_count for r in event.reactions or [])
    await _save(db)
    logger.info("реакции под голосовым %s: %s", event.message_id, p["reactions"])


async def tick(bot: Bot, db: Database) -> None:
    """Из фона стёб-бота: раз в день, в случайное время FROM_HOUR-TO_HOUR МСК — черновик владельцу в личку."""
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
    owner = _owner()
    if not owner:
        return
    why = await draft(bot, db, owner)
    if why:
        logger.warning("черновик голосового в канал не вышел: %s", why)


@router.message(Command("podpisota"))
async def cmd_podpisota(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    """/podpisota — черновик голосового в канал сейчас (по кругу); /podpisota история / реакции / наезд — что именно;
    /podpisota батя / подписота / канал — какая история."""
    arg = (command.args or "").strip().lower()
    sub = ("father" if arg.startswith(("бат", "отец")) else "fans" if arg.startswith(("подпис", "фан")) else
           "self" if arg.startswith(("канал", "себ", "про себ")) else None)
    kind = ("story" if arg.startswith("ист") or sub else "cta" if arg.startswith("реак") else
            "rant" if arg.startswith("наезд") else None)
    if _lock.locked():
        await temp_reply(message, "🎙 Уже записываю - минуту.")
        return
    to = message.from_user.id if message.from_user else 0
    await temp_reply(message, "🎙 Записываю голосовое - черновик придёт в личку…")
    try:
        why = await draft(bot, db, to, kind=kind, sub=sub)
    except TelegramBadRequest as e:
        why = f"не могу написать в личку - нажми /start у бота ({e})"
    except Exception as e:
        logger.exception("rant draft failed")
        why = str(e)[:200]
    if why:
        await temp_reply(message, f"Не вышло: {why}.")
    elif message.chat.type != "private":
        await temp_reply(message, "🎙 Черновик в личке - там кнопка «В канал».")


# ---------- ответы на комменты под своим голосовым ----------

async def _load_cache(db: Database) -> None:
    if _cache["loaded"]:
        return
    _cache["posts"] = [p for p in _load(await db.get_setting(POSTS_KEY, "[]")) if isinstance(p, dict)]
    _cache["threads"] = [t for t in _load(await db.get_setting(THREADS_KEY, "[]")) if isinstance(t, dict)]
    _cache["loaded"] = True


async def _save(db: Database) -> None:
    _cache["posts"] = _cache["posts"][-40:]  # 40 — для учёбы на реакциях
    _cache["threads"] = _cache["threads"][-10:]
    await db.set_setting(POSTS_KEY, json.dumps(_cache["posts"], ensure_ascii=False))
    await db.set_setting(THREADS_KEY, json.dumps(_cache["threads"], ensure_ascii=False))


def _note_post(chan: int, msg_id: int, text: str, d: dict | None = None) -> None:
    d = d or {}
    _cache["posts"].append({"chan": chan, "msg": msg_id, "ts": int(time.time()), "text": text[:1500],
                            "kind": d.get("kind", ""), "sub": d.get("sub", ""), "topic": d.get("topic", ""),
                            "mood": d.get("mood", ""), "reactions": 0, "comments": 0})


def _thread_of(chat_id: int, message: Message, window: int = REPLY_WINDOW) -> dict | None:
    """Ветка комментариев под своим голосовым, в которой это сообщение (не старше window), или None."""
    rt = message.reply_to_message
    for th in _cache["threads"]:
        if th.get("chat") != chat_id or time.time() - th.get("ts", 0) > window:
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
            _cache["threads"].append({"chat": chat_id, "root": message.message_id, "post": mid,
                                      "ts": post.get("ts", 0), "text": post.get("text", ""), "done": []})
            await _save(db)
        return
    th = _thread_of(chat_id, message, LEARN_WINDOW)
    u = message.from_user
    if th is None or not u or u.is_bot or message.sender_chat:
        return
    text = (message.text or message.caption or "").strip()
    if text.startswith("/"):
        return
    optout = await db.is_optout(chat_id, u.id)
    name = (u.first_name or u.username or "Аноним").strip()[:30]
    post = next((p for p in _cache["posts"] if p.get("msg") == th.get("post")), None)
    if post is not None:  # учёба: сколько комментов под голосовым (любых, и голосом, и стикером)
        post["comments"] = post.get("comments", 0) + 1
    if text and not optout and not _EMPTY_COMMENT.match(text):  # одного потом можно назвать в следующем голосовом
        th["said"] = (th.get("said") or [])[-11:] + [{"name": name, "text": text[:200]}]
    await _save(db)
    if (time.time() - th.get("ts", 0) > REPLY_WINDOW or len(th.get("done", [])) >= REPLY_MAX or not text or optout
            or _EMPTY_COMMENT.match(text) or await _addressed(message, text, bot)):
        return
    if (await db.get_setting(ON_KEY, "1")) != "1":
        return
    from handlers.roast import _father_id

    father = u.id == _father_id(config)
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
