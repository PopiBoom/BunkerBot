"""/diss — дисс-трек на участника: песня с вокалом про него по досье и его сообщениям (выбор владельца 08.11).

Админ: /diss ответом на сообщение человека или /diss @ник; жанр — словом после (/diss @ник шансон): рэп (по
умолчанию), трэп, панк, рок, шансон, попса, метал. Текст — ai.diss_lyrics (Opus, effort medium, ≈$0.03): факты
только из досье, сообщений за 2 недели и слитых обещаний, мат, красные линии. Песня — ai.eleven_music (ElevenLabs
Music по composition plan: вступление «Это Пиздюк. Дисс на …», куплет, припев, куплет, припев, концовка; ≈1.5 мин,
≈$0.25-0.5; только платный тариф ElevenLabs). ElevenLabs не пропустил текст по содержанию — одна попытка текстом
без мата. Трек — send_audio (исполнитель «Пиздюк», обложка cards.render_diss_cover, в подписи — припев).
Пока пишется (минута-две) — busy() держит флаг обновлений; одна песня за раз.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import time

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message

import ai
from database import Database
from ephemeral import temp_reply

logger = logging.getLogger("diss")
router = Router(name="diss")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))

EVIDENCE_DAYS = 14
RECENT_KEY = "diss_recent:"   # прошлые припевы на человека — чтобы не повторяться
BUSY_MAX = 10 * 60            # дольше флаг обновлений не держим (зависло)
_busy = {"since": 0.0}


def busy() -> bool:
    return bool(_busy["since"]) and time.time() - _busy["since"] < BUSY_MAX


def _name(u) -> str:
    return (u.full_name or u.username or "Аноним").strip()


def _parse(args: str) -> tuple[str, str]:
    """(@ник или "", жанр) из текста после /diss."""
    nick, genre = "", "рэп"
    for w in (args or "").split():
        if w.startswith("@") and not nick:
            nick = w
        elif ai.diss_genre(w):
            genre = ai.diss_genre(w)
    return nick, genre


async def _edit(bot: Bot, chat_id: int, msg_id: int, text: str) -> None:
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id)
    except Exception as e:
        logger.debug("diss progress: %s", e)


@router.message(Command("diss"))
async def cmd_diss(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    from handlers.collect import _father_id, _photo
    from handlers.roast import _bunker_active, db_add_own

    chat_id = message.chat.id
    if _bunker_active(chat_id):
        return
    nick, genre = _parse(command.args or "")
    rt = message.reply_to_message
    target = None
    if rt and rt.from_user and not rt.from_user.is_bot:
        target = (rt.from_user.id, _name(rt.from_user))
    elif nick:
        found = await db.find_user(chat_id, nick)
        target = (found[0], found[1] or nick) if found else None
    if not target:
        await temp_reply(message, "На кого дисс? /diss ответом на его сообщение или /diss @ник (жанр можно "
                                  "словом после: рэп, трэп, панк, рок, шансон, попса, метал).")
        return
    if busy():
        await temp_reply(message, "Уже записываю дисс - подожди, студия одна.")
        return
    if await db.is_optout(chat_id, target[0]):
        await temp_reply(message, "Этого человека бот не запоминает - петь не о чем.")
        return
    if not ai.available():
        await temp_reply(message, "ИИ сейчас недоступен - текст писать некому.")
        return
    if not ai.eleven_key():
        await temp_reply(message, "Для песни нужен ключ ElevenLabs: /setkey eleven ключ (в личку боту).")
        return
    if ai.eleven_geo_blocked():
        await temp_reply(message, "ElevenLabs сейчас не пускает сервер по региону - песню не записать.")
        return
    uid, name = target
    rows = await db.recent_messages(chat_id, limit=80, user_id=uid, since=int(time.time()) - EVIDENCE_DAYS * 86400)
    rows = [r for r in rows if not str(r[3]).startswith("[")]
    dossier = await db.get_dossier(chat_id, uid)
    if len(rows) < 5 and not dossier:
        await temp_reply(message, f"Про {name} почти ничего нет - дисс выйдет ни о чём.")
        return
    _busy["since"] = time.time()
    progress = None
    try:
        progress = await bot.send_message(chat_id, f"🎤 Пишу дисс на <b>{html.escape(name)}</b> ({genre})…")
        await _make(bot, db, chat_id, uid, name, genre, rows, dossier, progress.message_id, _father_id(),
                    _photo, db_add_own)
    except Exception:
        logger.exception("diss failed in %s", chat_id)
        await temp_reply(message, "Студия сгорела - дисс не вышел. Попробуй ещё раз.")
    finally:
        _busy["since"] = 0.0
        if progress:
            try:
                await bot.delete_message(chat_id, progress.message_id)
            except Exception as e:
                logger.debug("diss progress delete: %s", e)


async def _make(bot: Bot, db: Database, chat_id: int, uid: int, name: str, genre: str, rows: list[tuple],
                dossier: str, progress_id: int, father: int, photo_of, add_own) -> None:
    import cards

    now = int(time.time())
    promises = [p["what"] for p in await db.promises_due_between(chat_id, now - 30 * 86400, now) if p["name"] == name]
    gender = None
    for g_uid, _n, m, f in await db.gender_counts(chat_id):
        if g_uid == uid:
            gender = ai.gender_of(m, f)
    key = f"{RECENT_KEY}{chat_id}:{uid}"
    try:
        avoid = list(json.loads(await db.get_setting(key, "[]") or "[]"))
    except (ValueError, TypeError):
        avoid = []
    messages = [{"id": r[0], "name": r[2], "text": r[3]} for r in rows]
    song, lyrics = b"", None
    for clean in (False, True):
        lyrics = await ai.diss_lyrics(name, dossier, messages, promises, gender, genre, father=uid == father,
                                      avoid=avoid, clean=clean)
        logger.info("дисс на %s (%s%s): «%s», факты: %s", name, genre, ", без мата" if clean else "",
                    lyrics["title"], "; ".join(lyrics["facts"]))
        await _edit(bot, chat_id, progress_id, f"🎚 Записываю трек «{html.escape(lyrics['title'])}» на "
                                               f"<b>{html.escape(name)}</b> - минута-две…")
        await bot.send_chat_action(chat_id, "record_voice")
        song, why = await ai.eleven_music(ai.diss_plan(lyrics, genre, name))
        if not song and why == "model":
            song, why = await ai.eleven_music(ai.diss_plan(lyrics, genre, name, v1=True), model="music_v1")
        if song or why != "content":
            break
        logger.warning("дисс: ElevenLabs не пропустил текст — пробую без мата")
    if not song:
        await bot.send_message(chat_id, f"🎤 Дисс на {html.escape(name)} не записался: "
                                        f"{html.escape(ai.music_error[0] or 'ElevenLabs не ответил')}.")
        return
    await bot.send_chat_action(chat_id, "upload_voice")
    cover = await asyncio.to_thread(cards.render_diss_cover, name, lyrics["title"], ai.DISS_GENRES[genre][0],
                                    await photo_of(bot, uid), uid % 997)
    chorus = "\n".join(lyrics["chorus"])
    caption = (f"🎤 <b>Пиздюк - «{html.escape(lyrics['title'])}»</b>\nДисс на {html.escape(name)} · "
               f"{ai.DISS_GENRES[genre][0]}\n\n<i>{html.escape(chorus)}</i>")
    sent = await bot.send_audio(chat_id, BufferedInputFile(song, f"Пиздюк - {lyrics['title']}.mp3"),
                                title=lyrics["title"], performer="Пиздюк", caption=caption[:1024],
                                thumbnail=BufferedInputFile(cover, "cover.jpg"))
    try:
        await add_own(chat_id, getattr(sent, "message_id", 0) or 0,
                      f"[дисс-трек на {name} «{lyrics['title']}», {genre}] припев: {' / '.join(lyrics['chorus'])}")
    except Exception as e:
        logger.debug("diss memory: %s", e)
    avoid = (avoid + [" / ".join(lyrics["chorus"])])[-3:]
    await db.set_setting(key, json.dumps(avoid, ensure_ascii=False))
