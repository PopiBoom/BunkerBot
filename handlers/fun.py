"""Развлечения чата: опрос «Кто из нас…» (/poll) и «вопрос дня», когда в чате тихо.

  * /poll — ИИ придумывает вопрос по досье и переписке («Кто из нас первым сольётся с зала?»),
    варианты — самые активные участники за неделю, опрос открытый (видно, кто за кого).
    Через POLL_MINUTES бот закрывает его и прожаривает «победителя» в связке с вопросом.
  * Тишина в чате QUIET_HOURS днём — бот кидает провокационный вопрос по темам чата
    (иногда вместо него — опрос). Не чаще раза в сутки, только в живом чате, не во время Бункера.
Фоновую часть (закрыть опросы, вопрос дня) вызывает фоновая задача стёб-бота (roast.background).
"""
from __future__ import annotations

import html
import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message, ReplyParameters

import ai
from database import Database
from handlers import roast as R

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
        await message.reply("Стёб-бот сейчас выключен.")
        return
    if R._bunker_active(chat_id):
        await message.reply("Идёт игра — опросы после партии 🗳")
        return
    if time.time() - _last_poll.get(chat_id, 0) < POLL_COOLDOWN:
        await message.reply("Один опрос уже висит — дождитесь итогов.")
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
        await message.reply(why)


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
    await R._say(bot, chat_id, f"🤔 {q}")
    logger.info("вопрос дня в %s: %s", chat_id, q)
