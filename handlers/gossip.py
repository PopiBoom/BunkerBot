"""«Сплетни Волгограда» — весёлая рубрика «радио Волгограда» (09.11, владелец: «рубрику, но со сплетнями, как бы
всё навеселе, с шутками; раскрытие темы, возможные варианты событий»).

Брифинг: сплетни про ВЫДУМАННЫХ волгоградцев, плюс сплетни подписчиков — присылают в бота по ссылке
t.me/<бот>?start=gossip (handlers/letters.py, kind="gossip": админ берёт или отклоняет); голосом как радио (ведущий —
голос рубрики, ElevenLabs с пометками интонации; весёлая подложка и «дзынь» между частями — radio.gossip); шансы
версий в процентах (ai.gossip_story, проценты сводит код к 100). Запуск — /spletni (админ): есть взятые сплетни —
сначала выбор, из какой делать выпуск; черновик в личку «✅ В канал / 🔄 Переписать / 🗑 Выкинуть»; в канал —
голосовое с подписью (картинки к выпускам владелец 09.11 убрал). Собирается под
тем же замком, что ночной эфир и «Письма» (night._lock): один эфир за раз, и пока собирается — обновление сервера
ждёт (night.busy() в games._flag_needed).
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import ai
from config import Config
from database import Database
from handlers import night

logger = logging.getLogger("gossip")
router = Router(name="gossip")

DRAFT_KEY = "gossip_draft"
COUNT_KEY = "gossip_no"
HIST_KEY = "gossip_titles"   # прошлые выпуски: «тема:… | о чём слух | где» — не повторять
NUM_ICONS = ["1️⃣", "2️⃣", "3️⃣"]
# Музыка под выпуск (идея №9) — ElevenLabs /music без голоса, весёлая и «шпионско-комедийная»; нет ключа — синтезатор
GOSSIP_MUSIC = ("playful comedic gossip talk-show background music, sneaky pizzicato strings, light brushed percussion "
                "and finger snaps, bouncy upright bass, mischievous spy-comedy vibe, upbeat, bright major key, "
                "loopable, stays in the background under talking")
_tasks: set[asyncio.Task] = set()  # сборка идёт в фоне — держим ссылку, чтобы задачу не собрал сборщик мусора


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def _caption(n: int, story: dict, now: datetime, weather: str, bot_username: str, author: str | None) -> str:
    """Подпись выпуска (и в канале): рубрика и номер, о чём слух, кто прислал, версии с шансами, время и погода,
    как слить свою сплетню."""
    from handlers.letters import deep_link

    subject = (story.get("subject") or story.get("title") or "без темы").strip()
    subject = subject[:1].upper() + subject[1:]
    when = f"{now:%H:%M}" + (f" · {html.escape(weather)}" if weather else "")
    vers = "\n".join(f"{NUM_ICONS[i]} {html.escape(v['short'])} - <b>{v['chance']}%</b>"
                     for i, v in enumerate(story.get("versions") or []) if i < len(NUM_ICONS))
    link = deep_link(bot_username, "gossip")
    return (f"🗣 <b>{ai.GOSSIP_RUBRIC.upper()}</b> · выпуск №{n}\n"
            f"🤫 <i>«{html.escape(subject[:90])}»</i>\n"
            + ("" if author is None else f"👤 Слил(а): {html.escape(author[:30])}\n" if author else
               "👤 Автор попросил не называть имя\n")
            + (f"\n{vers}\n" if vers else "")
            + f"\n📻 Радио Волгограда · {when}"
            + (f"\n\n📨 Слить свою сплетню: <a href=\"{link}\">@{html.escape(bot_username)}</a>" if link else ""))


async def make_episode(db: Database, progress=night._noop, letter: dict | None = None,
                       bot_username: str = "", own: str | None = None) -> dict:
    """{"ogg", "paragraphs", "caption", "n", "topic", "voice", "secs", "letter"}; без голоса — ogg None."""
    import radio

    await progress("✍️ Собираю сплетни и пишу выпуск…")
    now = datetime.now(night.MSK)
    cond = night._conditions(await night._weather(), now)
    try:
        used = list(json.loads(await db.get_setting(HIST_KEY, "[]") or "[]"))
    except (ValueError, TypeError):
        used = []
    day = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"][now.weekday()]
    facts = f"сегодня {ai.date_words(now.date())}, {day}; сейчас {night._clock(now)}; " + (
        cond["line"] or "погода неизвестна")
    story = await ai.gossip_story(used[-12:], facts, now.date(), listener=letter, own=own)
    if not story:
        raise RuntimeError("ИИ не написал выпуск")
    topic = story["subject"] or story["title"] or "без темы"
    await db.set_setting(HIST_KEY, json.dumps(
        (used + [f"тема:{story['theme'][:40]} | {topic} | {story.get('place', '')}"])[-30:], ensure_ascii=False))
    seq = ai.gossip_chunks(story)
    chunks, roles, parts = [t for _, t, _ in seq], [r for r, _, _ in seq], [p for _, _, p in seq]
    call = story.get("call") or {}
    names = {"host": "Ведущий", "cohost": ai.GOSSIP_COHOST, "call_f": f"📞 {call.get('name', 'Звонок')}",
             "call_m": f"📞 {call.get('name', 'Звонок')}"}
    paragraphs = [f"{names.get(r, r)}: {ai.strip_tags(t)}" for r, t, _ in seq]  # текст черновика — кто что говорит
    music_task = None
    if ai.eleven_key() and not ai.eleven_geo_blocked():  # музыка (идея №9) — пока озвучиваются голоса
        music_task = asyncio.create_task(ai.eleven_ambient(GOSSIP_MUSIC, strict=False))
    n = int(await db.get_setting(COUNT_KEY, "0") or 0) + 1
    author = ((letter.get("sign") or "").strip() if letter else None)
    caption = _caption(n, story, now, cond["caption"], bot_username, author)

    async def step(i: int, total: int, who: str) -> None:
        await progress(f"🎙 Озвучиваю: {i} из {total} · {who}")

    voice = await ai.narrate_all(chunks, roles, horror=True, progress=step)
    who = ai.last_voice[0] + (f" (не вышло: {ai.voice_trouble[0]})" if ai.voice_trouble[0] else "")
    base = {"paragraphs": paragraphs, "caption": caption, "n": n, "topic": topic,
            "letter": (letter or {}).get("id")}
    if not voice:
        if music_task:
            music_task.cancel()
        return {**base, "ogg": None, "voice": ai.voice_trouble[0]}
    await progress("🎚 Свожу эфир…")
    music = None
    if music_task:
        data = await music_task
        music = await asyncio.to_thread(radio.decode, data) if data else None
    mix = await asyncio.to_thread(radio.gossip, voice, ai.SPEAK_RATE, int(time.time()), roles, parts, music)
    who = (f"{who} · {story['theme'][:50]} · звонок {'есть' if call else 'нет'} · музыка "
           f"{'ElevenLabs' if music is not None else 'своя'}")
    ogg = await asyncio.to_thread(radio.to_ogg, mix)
    secs = max(1, int(round(len(mix) / radio.SR)))
    logger.info("сплетни №%s: «%s», %.0f сек, голос %s", n, topic, len(mix) / radio.SR, who or "?")
    return {**base, "ogg": ogg, "voice": who, "secs": secs}


def _kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ В канал", callback_data="gs:pub"),
        InlineKeyboardButton(text="🔄 Переписать", callback_data="gs:redo"),
        InlineKeyboardButton(text="🗑 Выкинуть", callback_data="gs:del")]])


async def send_draft(bot: Bot, db: Database, chat_id: int, progress=night._noop, letter_id: int | None = None,
                     own: str | None = None) -> bool:
    """Собрать выпуск и прислать черновик в личку на одобрение (под общим замком эфиров). own — своя сплетня."""
    async with night._lock:
        try:
            return await asyncio.wait_for(_send_draft(bot, db, chat_id, progress, letter_id, own),
                                          night.EPISODE_TIMEOUT)
        except asyncio.TimeoutError:
            await bot.send_message(chat_id, f"⌛ Выпуск не собрался за {night.EPISODE_TIMEOUT // 60} мин - озвучка "
                                            "тормозит. Попробуй ещё раз позже.")
            return False


async def _send_draft(bot: Bot, db: Database, chat_id: int, progress, letter_id: int | None,
                      own: str | None = None) -> bool:
    from handlers import letters

    letter = await letters.get(db, letter_id) if letter_id else None
    try:
        me = (await bot.get_me()).username or ""
    except Exception:
        me = ""
    ep = await make_episode(db, progress=progress, letter=letter, bot_username=me, own=own)
    text = "\n\n".join(ep["paragraphs"])
    if not ep["ogg"]:
        why = f" ({html.escape(ep['voice'][:300])})" if ep.get("voice") else ""
        await night.tg_retry(bot.send_message, chat_id, f"{ep['caption']}\n\n{html.escape(text[:2800])}\n\n"
                             f"<i>Озвучить не вышло{why}. Это текст.</i>", disable_web_page_preview=True)
        return False
    voice = BufferedInputFile(ep["ogg"], "gossip.ogg")
    try:
        msg = await night.tg_retry(bot.send_voice, chat_id, voice, reply_markup=_kb(),
                                   duration=ep.get("secs") or None,
                                   caption=night._draft_caption(ep["caption"], ep.get("voice") or "", text))
    except TelegramBadRequest as e:  # подпись не пустили — голосовое дороже подписи
        if "caption" not in str(e).lower():
            raise
        logger.warning("gossip: подпись черновика не принята (%s) — без текста", e)
        msg = await night.tg_retry(bot.send_voice, chat_id, voice, reply_markup=_kb(),
                                   duration=ep.get("secs") or None,
                                   caption=night._cut_vis(re.sub(r"<[^>]+>", "", ep["caption"]), 900))
    await db.set_setting(DRAFT_KEY, json.dumps({"file_id": msg.voice.file_id, "caption": ep["caption"], "n": ep["n"],
                                                "msg": msg.message_id, "chat": chat_id, "letter": ep.get("letter"),
                                                "secs": ep.get("secs"), "own": own}, ensure_ascii=False))
    return True


async def _start(bot: Bot, db: Database, uid: int, letter_id: int | None, own: str | None = None) -> bool:
    """Собрать черновик и прислать uid в личку. False — в личку не написать."""
    try:
        if not ai.available():
            await bot.send_message(uid, "ИИ недоступен - выпуск не написать.")
            return True
        if night.busy():
            await bot.send_message(uid, "⏳ Сейчас собирается другой эфир - пришлю сплетни, когда он будет готов.")
        first = ("🗣 Собираю «Сплетни Волгограда»" + (" по твоей сплетне" if own else f" по сплетне № {letter_id}"
                                                      if letter_id else "") + " - минуты две…")
        status = await bot.send_message(uid, first)
    except Exception:
        return False
    try:
        await send_draft(bot, db, uid, progress=night._progress(bot, status, first), letter_id=letter_id, own=own)
        try:
            await status.delete()
        except Exception:
            pass
    except Exception as e:
        logger.exception("gossip draft failed")
        await bot.send_message(uid, f"Выпуск не собрался: {html.escape(str(e))[:200]}")
    return True


@router.message(Command("spletni", "сплетни", ignore_case=True))
async def cmd_spletni(message: Message, bot: Bot, db: Database, command: CommandObject | None = None) -> None:
    """/spletni (или /сплетни) — выпуск «Сплетен Волгограда» черновиком в личку. «/сплетни текст» (или ответом на
    сообщение с текстом) — выпуск по своей сплетне (владелец 09.11). Иначе есть взятые сплетни подписчиков —
    сначала выбор."""
    from handlers import letters

    own = ((command.args if command else "") or "").strip()
    rt = message.reply_to_message
    if not own and rt is not None and (rt.text or rt.caption) and not (rt.from_user and rt.from_user.is_bot):
        own = (rt.text or rt.caption or "").strip()
    if own:
        _spawn(_run(bot, db, message, None, own=own))
        return
    ready = await letters.approved(db, "gossip")
    uid = message.from_user.id
    if ready:
        rows = [[InlineKeyboardButton(text=f"🗣 № {x['id']} · {x.get('sign') or 'аноним'}: {x['text'][:28]}…",
                                      callback_data=f"gp:{x['id']}")] for x in ready[:8]]
        rows.append([InlineKeyboardButton(text="🎲 Без сплетни слушателя - придумать самому", callback_data="gp:0")])
        try:
            await bot.send_message(uid, "🗣 Из какой сплетни делать выпуск?",
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        except Exception:
            await message.answer("Напиши боту /start в личку - черновик приходит туда.")
        return
    _spawn(_run(bot, db, message, None))


async def _run(bot: Bot, db: Database, message: Message | None, letter_id: int | None, uid: int | None = None,
               own: str | None = None) -> None:
    uid = uid or message.from_user.id
    if not await _start(bot, db, uid, letter_id, own) and message is not None:
        await message.answer("Напиши боту /start в личку - черновик приходит туда.")


@router.callback_query(F.data.startswith("gp:"))
async def cb_pick(call: CallbackQuery, bot: Bot, db: Database, config: Config) -> None:
    if not config.is_admin(call.from_user.id):
        await call.answer("Это для админа.", show_alert=True)
        return
    try:
        lid = int((call.data or "")[3:]) or None
    except ValueError:
        lid = None
    await call.answer()
    if call.message:
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    _spawn(_run(bot, db, None, lid, call.from_user.id))


@router.callback_query(F.data.startswith("gs:"))
async def cb_draft(call: CallbackQuery, bot: Bot, db: Database, config: Config) -> None:
    if not config.is_admin(call.from_user.id):
        await call.answer("Это для админа.", show_alert=True)
        return
    try:
        draft = json.loads(await db.get_setting(DRAFT_KEY, "") or "{}")
    except ValueError:
        draft = {}
    action = (call.data or "")[3:]
    if not draft or draft.get("msg") != (call.message.message_id if call.message else None):
        await call.answer("Этот черновик уже неактуален.", show_alert=True)
        return

    async def mark(text: str) -> None:
        try:
            await bot.edit_message_caption(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                           caption=f"{draft['caption']}\n\n{text}", reply_markup=None)
        except TelegramBadRequest:
            pass

    if action == "pub":
        from handlers import letters
        from handlers.channel_posts import channel_id, mark_own

        channel = await channel_id(bot, db)
        if not channel:
            await call.answer("Не нашёл канал.", show_alert=True)
            return
        await db.set_setting(DRAFT_KEY, "")  # сразу: второе нажатие не опубликует второй раз
        sent = await night.tg_retry(bot.send_voice, channel, draft["file_id"], caption=draft["caption"],
                                    duration=draft.get("secs") or None)
        await mark_own(db, sent.message_id)
        await db.set_setting(COUNT_KEY, str(draft["n"]))
        if draft.get("letter"):
            try:
                ch = await bot.get_chat(channel)
                link = (f"https://t.me/{ch.username}/{sent.message_id}" if ch.username else
                        f"https://t.me/c/{str(channel).removeprefix('-100')}/{sent.message_id}")
            except Exception:
                link = ""
            await letters.mark_used(bot, db, int(draft["letter"]), link)
        await call.answer("Опубликовано в канале.")
        await mark("✅ Опубликовано в канале.")
    elif action == "del":
        await db.set_setting(DRAFT_KEY, "")
        await call.answer("Выкинул.")
        await mark("🗑 Выкинуто.")
    elif action == "redo":
        await db.set_setting(DRAFT_KEY, "")
        await call.answer("Переписываю - минуты две.")
        await mark("🔄 Переписан - новый ниже.")
        _spawn(_run(bot, db, None, draft.get("letter"), call.message.chat.id, own=draft.get("own")))
