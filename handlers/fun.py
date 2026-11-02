"""Развлечения чата: опрос «Кто из нас…» (/poll), «вопрос дня», когда в чате тихо, демотиватор (/demot)
и мемы: по шаблону (/mem) и видео-мем с плашкой «когда…» (/vmem).

  * /poll — ИИ придумывает вопрос по досье и переписке («Кто из нас первым сольётся с зала?»),
    варианты — самые активные участники за неделю, опрос открытый (видно, кто за кого).
    Через POLL_MINUTES бот закрывает его и прожаривает «победителя» в связке с вопросом.
  * Тишина в чате QUIET_HOURS днём — бот кидает провокационный вопрос по темам чата
    (иногда вместо него — опрос). Не чаще раза в сутки, только в живом чате, не во время Бункера.
Фоновую часть (закрыть опросы, вопрос дня) вызывает фоновая задача стёб-бота (roast.background).
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, ReplyParameters

import ai
import cards
import frames
import memes
from database import Database
from handlers import roast as R

from ephemeral import temp_reply

logger = logging.getLogger("fun")
router = Router(name="fun")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))

MSK = timezone(timedelta(hours=3))
POLL_MINUTES = 15         # сколько висит опрос до итогов
POLL_COOLDOWN = POLL_MINUTES * 60  # новый /poll — не раньше, чем подведены итоги прошлого
POLL_MIN, POLL_MAX = 3, 10
QUIET_HOURS = 3           # столько часов тишины — и бот подкидывает вопрос
DAY_FROM, DAY_TO = 12, 23  # МСК: только днём и вечером, ночью не будим
KICK_GAP = 20 * 3600      # «вопрос дня» не чаще раза в ~сутки
DEAD_AFTER = 3 * 86400    # чат молчит больше 3 дней — не тормошим, он мёртв
QUIET_KEY = "quiet_kick_enabled"
_last_poll: dict[int, float] = {}


async def _members(db: Database, chat_id: int) -> list[tuple[int, str]]:
    """Самые активные за неделю (не из /forget_me): [(user_id, подпись)] — подписи уникальны."""
    rows = await db.recent_messages(chat_id, limit=3000, since=int(time.time()) - 7 * 86400)
    count: dict[int, int] = {}
    names: dict[int, str] = {}
    for _mid, uid, name, _text, _ts in rows:
        if uid > 0:
            count[uid] = count.get(uid, 0) + 1
            names[uid] = name or names.get(uid) or "Аноним"
    top = [uid for uid, n in sorted(count.items(), key=lambda kv: -kv[1]) if n >= 3][:POLL_MAX]
    out, used = [], set()
    for uid in top:
        label = names[uid][:60]
        if label in used:  # два «Саши» — различаем по @нику или номеру
            uname = await db.last_username(chat_id, uid)
            label = f"{label} (@{uname})" if uname else f"{label} #{len(used) + 1}"
        used.add(label)
        out.append((uid, label))
    return out


async def _recent_questions(db: Database, chat_id: int) -> list[str]:
    try:
        return json.loads(await db.get_setting(f"fun_questions:{chat_id}", "[]"))
    except ValueError:
        return []


async def _remember_question(db: Database, chat_id: int, q: str) -> None:
    qs = (await _recent_questions(db, chat_id) + [q])[-15:]
    await db.set_setting(f"fun_questions:{chat_id}", json.dumps(qs, ensure_ascii=False))


async def launch_poll(bot: Bot, db: Database, chat_id: int, auto: bool = False) -> str | None:
    """Запускает опрос «Кто из нас…». Возвращает причину отказа или None, если опрос запущен."""
    members = await _members(db, chat_id)
    if len(members) < POLL_MIN:
        return "Маловато народу пишет — нужно хотя бы трое активных за неделю."
    await R._remember_lines(db, chat_id)
    names = [label for _uid, label in members]
    dossiers = {n: d for n, d in (await db.chat_dossiers(chat_id)).items() if n in names}
    recent = R._as_dicts(await db.recent_messages(chat_id, limit=30))
    question = await ai.who_poll_question(names, dossiers, recent, await _recent_questions(db, chat_id))
    if not question:
        return "Что-то не придумалось. Попробуй ещё раз."
    if not question.endswith("?"):
        question += "?"
    msg = await bot.send_poll(chat_id, question, options=names, is_anonymous=False)
    R._last_bot_msg[chat_id] = time.time()
    await db.add_poll(chat_id, msg.message_id, question, [[u, n] for u, n in members],
                      int(time.time()) + POLL_MINUTES * 60)
    await _remember_question(db, chat_id, question)
    logger.info("опрос в %s%s: %s", chat_id, " (сам)" if auto else "", question)
    return None


@router.message(Command("poll"))
async def cmd_poll(message: Message, bot: Bot, db: Database) -> None:
    chat_id = message.chat.id
    if not await R._enabled(db) or not ai.available():
        await temp_reply(message, "Стёб-бот сейчас выключен.")
        return
    if R._bunker_active(chat_id):
        await temp_reply(message, "Идёт игра — опросы после партии 🗳")
        return
    if time.time() - _last_poll.get(chat_id, 0) < POLL_COOLDOWN:
        await temp_reply(message, "Один опрос уже висит — дождитесь итогов.")
        return
    _last_poll[chat_id] = time.time()
    try:
        await bot.send_chat_action(chat_id, "typing")
        why = await launch_poll(bot, db, chat_id)
    except Exception as e:
        logger.warning("poll failed: %s", e)
        why = "Не вышло запустить опрос. Попробуй позже."
    if why:
        _last_poll.pop(chat_id, None)
        await temp_reply(message, why)


async def finish_polls(bot: Bot, db: Database) -> None:
    """Закрывает опросы, у которых вышло время, и прожаривает «победителя»."""
    for chat_id, msg_id, question, options in await db.due_polls(int(time.time())):
        await db.mark_poll_done(chat_id, msg_id)
        try:
            poll = await bot.stop_poll(chat_id, msg_id)
        except Exception as e:  # опрос удалили или уже закрыт руками
            logger.info("stop poll %s/%s failed: %s", chat_id, msg_id, e)
            continue
        if R._bunker_active(chat_id):
            continue
        votes = [o.voter_count for o in poll.options]
        results = sorted(((options[i][1], votes[i]) for i in range(min(len(options), len(votes)))),
                         key=lambda kv: -kv[1])
        top_uid = next((options[i][0] for i in range(len(votes)) if i < len(options)
                        and votes[i] == max(votes) and votes[i] > 0), None)
        answer = ""
        try:
            await R._remember_lines(db, chat_id)
            dossier = await db.get_dossier(chat_id, top_uid) if top_uid else ""
            draft = await ai.poll_verdict(question, results, dossier, await db.style_sample(chat_id))
            answer = await ai.polish(draft, f"Итоги опроса «{question}»: "
                                     + ", ".join(f"{n} — {v}" for n, v in results),
                                     R._as_dicts(await db.recent_messages(chat_id, limit=10)), max_words=30)
        except Exception as e:
            logger.warning("poll verdict failed: %s", e)
        lines = [f"🗳 <b>ИТОГИ: {html.escape(question)}</b>", ""]
        medals = ["🥇", "🥈", "🥉"]
        for i, (name, v) in enumerate(results[:5]):
            lines.append(f"{medals[i] if i < 3 else '•'} {html.escape(name)} — {v}")
        if answer:
            lines += ["", html.escape(ai.scrub(answer))]
        await bot.send_message(chat_id, "\n".join(lines),
                               reply_parameters=ReplyParameters(message_id=msg_id, allow_sending_without_reply=True))
        R._last_bot_msg[chat_id] = time.time()
        if answer and R._db_ref:
            await R._db_ref[0].add_chat_message(chat_id, 0, 0, R.BOT_SELF, None, f"[итоги опроса] {answer}"[:1000])


async def quiet_kick(bot: Bot, db: Database, chat_id: int) -> None:
    """В чате давно тихо (днём) — подкидываем провокационный вопрос или опрос «Кто из нас…»."""
    if (await db.get_setting(QUIET_KEY, "1")) != "1" or R._bunker_active(chat_id):
        return
    now = time.time()
    if not DAY_FROM <= datetime.now(MSK).hour < DAY_TO:
        return
    last = await db.last_activity(chat_id)
    if not last or now - last < QUIET_HOURS * 3600 or now - last > DEAD_AFTER:
        return
    key = f"quiet_kick:{chat_id}"
    if now - float(await db.get_setting(key, "0") or 0) < KICK_GAP:
        return
    await db.set_setting(key, str(now))
    if random.random() < 0.4 and await launch_poll(bot, db, chat_id, auto=True) is None:
        return
    await R._remember_lines(db, chat_id)
    recent = R._as_dicts(await db.recent_messages(chat_id, limit=40))
    dossiers = await db.chat_dossiers(chat_id)
    q = await ai.question_of_day(recent, dossiers, await db.style_sample(chat_id),
                                 await _recent_questions(db, chat_id))
    if not q:
        return
    await _remember_question(db, chat_id, q)
    R._mark("вопрос дня в тишину")
    await R._say(bot, chat_id, f"🤔 {q}")
    logger.info("вопрос дня в %s: %s", chat_id, q)


# ---------- демотиватор: /demot ответом на фото, видео, кружок, гифку или стикер ----------


async def _media_bytes(bot: Bot, rt: Message) -> bytes | None:
    """Картинка из сообщения: фото, кадр из середины видео/кружка, превью гифки, статичный стикер."""
    if rt.photo:
        sizes = sorted(rt.photo, key=lambda p: p.width)
        photo = next((p for p in reversed(sizes) if p.width <= 1280), sizes[0])
        return (await bot.download(photo)).read()
    media = rt.video or rt.video_note
    if media is not None:
        frame = await R._video_frame(bot, media)
        if frame:
            return frame
    if rt.sticker is not None and not (rt.sticker.is_animated or rt.sticker.is_video):
        return (await bot.download(rt.sticker)).read()
    thumb = getattr(rt.video or rt.video_note or rt.animation or rt.sticker, "thumbnail", None)
    if thumb is not None:
        return (await bot.download(thumb)).read()
    return None


async def _pick_media(bot: Bot, db: Database, chat_id: int) -> tuple[int, bytes] | None:
    """/demot без ответа: бот сам берёт свежее фото, видео, кружок, стикер или гифку из чата (за 3 дня)."""
    rows = await db.recent_media(chat_id, int(time.time()) - 3 * 86400, limit=40)
    random.shuffle(rows)
    for msg_id, kind, file_id in rows[:5]:  # файл могли удалить — пробуем несколько
        try:
            raw = (await bot.download(file_id)).read()
            if kind == "video":
                from frames import middle_frame
                raw = await asyncio.to_thread(middle_frame, raw)
            if raw:
                return msg_id, raw
        except Exception as e:
            logger.info("demot pick %s failed: %s", msg_id, e)
    return None


@router.message(Command("demot", "demotivator"))
async def cmd_demot(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    """Ответом на фото (видео, кружок, гифку, стикер) — демотиватор с подписью от ИИ; без ответа — бот сам
    берёт свежее медиа из чата. /demot ЗАГОЛОВОК | подпись — своими словами (без ИИ)."""
    chat_id = message.chat.id
    if R._bunker_active(chat_id):
        await temp_reply(message, "Идёт игра - демотиваторы после партии.")
        return
    rt = message.reply_to_message
    data = None
    if rt is not None and (rt.photo or rt.video or rt.video_note or rt.animation or rt.sticker):
        target, caption = rt.message_id, rt.caption or ""
        try:
            data = await _media_bytes(bot, rt)
        except Exception as e:
            logger.warning("demot download failed: %s", e)
    else:  # ничего не выбрано — сам выбирает, что демотивировать
        picked = await _pick_media(bot, db, chat_id)
        if picked is None:
            await temp_reply(message, "В чате за 3 дня нет фото или стикеров - ответь /demot на картинку.")
            return
        (target, data), caption = picked, ""
    if not data:
        await temp_reply(message, "Не смог достать картинку из этого сообщения.")
        return
    try:
        data = await asyncio.to_thread(cards.to_jpeg, data)  # WebP-стикеры, анимации, прозрачность — в JPEG
    except Exception as e:
        logger.warning("demot image open failed: %s", e)
        await temp_reply(message, "Картинка не открылась - пришли обычное фото.")
        return
    own = (command.args or "").strip()
    if own:  # своими словами: «заголовок | подпись»
        title, _, sub = own.partition("|")
        text = {"title": title.strip(), "subtitle": sub.strip()}
    else:
        if ai.available():
            await bot.send_chat_action(chat_id, "upload_photo")
            text = await ai.demotivator(data, "image/jpeg", caption)  # не вышло у ИИ — классика из запаса
        else:
            text = {"title": "", "subtitle": ""}
        if not text["title"]:
            text = dict(zip(("title", "subtitle"), random.choice(ai.DEMOT_FALLBACK), strict=True))
    if not R._db_ref:
        R._db_ref.append(db)
    try:
        await R.send_demotivator(bot, chat_id, target, data, text["title"], text["subtitle"])
    except Exception as e:
        logger.warning("demot failed: %s", e)
        await temp_reply(message, "Не вышло отправить демотиватор - попробуй ещё раз.")


# ---------- мемы: /mem — по шаблону про переписку, /vmem — видео с плашкой «когда…» ----------


def _own_meme(args: str) -> tuple[str, list[str]] | None:
    """«/mem верх | низ» или «/mem drake | строка | строка» — свой текст, без ИИ."""
    parts = [x.strip() for x in args.split("|")]
    tid = parts.pop(0).lower() if len(parts) > 1 and parts[0].lower() in memes.TEMPLATES else None
    parts = [x for x in parts if x]
    if not parts:
        return None
    top_bottom = [t for t in memes.TEMPLATES if memes.TEMPLATES[t][1] == memes._TB]
    if len(parts) == 1:  # одна фраза — на картинку с подписью сверху и снизу, поровну
        words = parts[0].split()
        parts = [" ".join(words[:len(words) // 2]), " ".join(words[len(words) // 2:])] if len(words) > 1 else ["", parts[0]]
        tid = tid or random.choice(top_bottom)
    if tid is None:
        tid = random.choice([t for t in memes.TEMPLATES if memes.lines_count(t) == len(parts)] or top_bottom)
    n = memes.lines_count(tid)
    return tid, (parts + [""] * n)[:n]


@router.message(Command("mem", "meme"))
async def cmd_meme(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    """Мем по шаблону (Drake, «две кнопки», «план Грю»…) про переписку: ответом — про это сообщение и его
    автора, без ответа — ИИ сам ищет момент в свежей переписке. /mem верх | низ — свой текст."""
    chat_id = message.chat.id
    if R._bunker_active(chat_id):
        await temp_reply(message, "Идёт игра - мемы после партии.")
        return
    if not R._db_ref:
        R._db_ref.append(db)
    rt = message.reply_to_message
    own = (command.args or "").strip()
    if own:
        made = _own_meme(own)
        if made is None:
            await temp_reply(message, "Формат: /mem верх | низ")
            return
        await R.send_meme(bot, chat_id, rt.message_id if rt else None, *made, "мем /mem (свой текст)")
        return
    if not ai.available():
        await temp_reply(message, "Для мемов от ИИ нужен ключ. Можно своим текстом: /mem верх | низ")
        return
    about, new_ids = "", None
    if rt is not None and rt.from_user and not rt.from_user.is_bot:
        u = rt.from_user
        if await db.is_optout(chat_id, u.id):
            await temp_reply(message, "Этого человека просили не трогать.")
            return
        rows = await db.recent_messages(chat_id, limit=40, user_id=u.id)
        about = u.first_name or u.username or ""
        if any(r[0] == rt.message_id for r in rows):
            new_ids = {rt.message_id}  # мем — именно про это сообщение
    else:
        rows = await db.recent_messages(chat_id, limit=60, since=int(time.time()) - 2 * 86400)
    rows = [r for r in rows if r[1] > 0 or r[2] == R.BOT_SELF][-60:]
    if sum(1 for r in rows if r[1] > 0) < 3:
        await temp_reply(message, "Мало переписки для мема - пусть сначала поболтают.")
        return
    names = {r[2] for r in rows}
    dossiers = {n: d for n, d in (await db.chat_dossiers(chat_id)).items() if n in names}
    async with R._typing_on(bot, chat_id, "upload_photo"):
        res = await ai.meme(R._as_dicts(rows), dossiers, about=about, new_ids=new_ids,
                            avoid=R.recent_templates(chat_id))
    if not res:
        await temp_reply(message, "Не придумался мем - попробуй ещё раз.")
        return
    target = rt.message_id if rt is not None else res["reply_to"]
    try:
        await R.send_meme(bot, chat_id, target, res["template"], res["lines"], "мем /mem")
    except Exception as e:
        logger.warning("meme failed: %s", e)
        await temp_reply(message, "Не вышло отправить мем - попробуй ещё раз.")


async def _pick_video(bot: Bot, db: Database, chat_id: int) -> tuple[int, bytes] | None:
    """/vmem без ответа: свежее видео, кружок или гифка из чата (за 3 дня)."""
    rows = [r for r in await db.recent_media(chat_id, int(time.time()) - 3 * 86400, limit=40) if r[1] == "video"]
    random.shuffle(rows)
    for msg_id, _kind, file_id in rows[:4]:
        try:
            data = (await bot.download(file_id)).read()
            if 0 < await asyncio.to_thread(memes.video_duration, data) <= memes.VIDEO_MAX_SEC:
                return msg_id, data
        except Exception as e:
            logger.info("vmem pick %s failed: %s", msg_id, e)
    return None


@router.message(Command("vmem", "vmeme"))
async def cmd_vmem(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    """Видео-мем: ответом на видео, кружок или гифку — сверху белая плашка с подписью «когда…» от ИИ;
    без ответа — бот сам берёт свежее видео чата. /vmem текст — своя подпись."""
    chat_id = message.chat.id
    if R._bunker_active(chat_id):
        await temp_reply(message, "Идёт игра - мемы после партии.")
        return
    if not R._db_ref:
        R._db_ref.append(db)
    if R._vmem_lock.locked():
        await temp_reply(message, "Уже монтирую другое видео - подожди минуту.")
        return
    rt = message.reply_to_message
    media = (rt.video or rt.video_note or rt.animation) if rt is not None else None
    author, caption, as_gif = "", "", None
    if media is not None:
        if (media.file_size or 0) > R.VIDEO_MAX:
            await temp_reply(message, "Видео больше 20 МБ - Telegram не даёт боту его скачать.")
            return
        if (media.duration or 0) > memes.VIDEO_MAX_SEC:
            await temp_reply(message, f"Видео длиннее {memes.VIDEO_MAX_SEC} сек - выбери покороче.")
            return
        label = "гифка" if rt.animation else "кружок" if rt.video_note else "видео"
        as_gif = bool(rt.animation)
        author = (rt.from_user.first_name or rt.from_user.username or "") if rt.from_user else ""
        caption = rt.caption or ""
        target = rt.message_id
        try:
            data = (await bot.download(media)).read()
        except Exception as e:
            logger.warning("vmem download failed: %s", e)
            await temp_reply(message, "Не смог скачать это видео.")
            return
    else:
        picked = await _pick_video(bot, db, chat_id)
        if picked is None:
            await temp_reply(message, "В чате за 3 дня нет видео - ответь /vmem на видео, кружок или гифку.")
            return
        (target, data), label = picked, "видео"
    text = (command.args or "").strip()
    if not text:
        if not ai.available():
            await temp_reply(message, "Для подписи от ИИ нужен ключ. Можно своей: /vmem когда…")
            return
        async with R._typing_on(bot, chat_id, "upload_video"):
            frame = await asyncio.to_thread(frames.middle_frame, data)
            if frame:
                context = R._as_dicts(await db.recent_messages(chat_id, limit=15))
                text = await ai.video_caption(frame, "image/jpeg", label, author, caption, context)
        if not text:
            await temp_reply(message, "Не придумалась подпись - попробуй ещё раз или напиши свою: /vmem когда…")
            return
    try:
        await R.send_video_meme(bot, chat_id, target, data, text, f"видео-мем /vmem ({label})", as_gif=as_gif)
    except Exception as e:
        logger.warning("vmem failed: %s", e)
        await temp_reply(message, "Не вышло смонтировать видео - попробуй другое.")
