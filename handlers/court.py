"""/sud — суд чата (идея и макет приговора — с владельцем, 07.11).

Админ: /sud ответом на сообщение человека или /sud @ник. Дальше:
1) ИИ-прокурор (ai.court_charges) предъявляет 2-3 статьи — улики ДОСЛОВНО из его сообщений за неделю (ИИ даёт
   только id, текст подставляет код); нет ни одной настоящей улики — «оправдан за отсутствием состава».
2) Защита DEFENSE_TIME: кнопка «🛡 Стать адвокатом» — первый нажавший (не подсудимый) защищает, его следующее
   сообщение — речь; первое сообщение подсудимого — последнее слово (ловит observe() из middleware в bot.py).
3) Голосование VOTE_TIME, тайное: «Виновен / Оправдать», подсудимый не голосует, голос можно поменять.
4) Приговор: слово судьи (ai.court_verdict) + картинка (cards.render_verdict). Ничья — оправдан; никто не
   проголосовал — решает судья (виновен). Виновен — прозвище на сутки и 🤡 на каждое его сообщение PUNISH сек.
Красные линии — в промптах; /forget_me-людей судить нельзя. Пока идёт суд — флаг обновлений (games._flag_needed).
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import ai
from database import Database
from ephemeral import temp_reply

logger = logging.getLogger("court")
router = Router(name="court")

MSK = timezone(timedelta(hours=3))
DEFENSE_TIME = 120     # сек на адвоката и последнее слово
VOTE_TIME = 120        # сек на голосование
PUNISH = 24 * 3600     # сек: прозвище и 🤡 на каждое сообщение
EVIDENCE_DAYS = 7      # улики — из сообщений за столько дней
CASE_KEY = "court_no:"
PUNISH_KEY = "court_punished"   # {"chat:uid": [до ts, прозвище]}

_courts: dict[int, dict] = {}
_tasks: set[asyncio.Task] = set()
_punished: dict[str, list] = {}
_loaded = [False]
_vote_edit: dict[int, float] = {}


def running() -> bool:
    """Идёт ли где-то суд — чтобы сервер не перезапустился посреди заседания."""
    now = time.time()  # зависшее (упало посреди) заседание дольше 15 мин обновления не держит
    return any(c["phase"] != "done" and now - c["started"] < 900 for c in _courts.values())


def _name(u) -> str:
    return (u.first_name or u.username or "Аноним") if u else "Аноним"


async def _load_punished(db: Database) -> None:
    if _loaded[0]:
        return
    _loaded[0] = True
    try:
        _punished.update(json.loads(await db.get_setting(PUNISH_KEY, "{}") or "{}"))
    except ValueError:
        pass


async def _save_punished(db: Database) -> None:
    now = time.time()
    for k in [k for k, v in _punished.items() if v[0] < now]:
        _punished.pop(k, None)
    await db.set_setting(PUNISH_KEY, json.dumps(_punished, ensure_ascii=False))


def nickname(chat_id: int, user_id: int) -> str:
    """Прозвище по приговору, пока действует (или пусто)."""
    v = _punished.get(f"{chat_id}:{user_id}")
    return v[1] if v and v[0] > time.time() else ""


async def observe(message: Message, bot: Bot, db: Database) -> None:
    """Из middleware в bot.py, на каждое сообщение группы: речь адвоката, последнее слово, 🤡 осуждённым."""
    if not message.from_user or message.chat.type not in ("group", "supergroup"):
        return
    chat_id, uid = message.chat.id, message.from_user.id
    await _load_punished(db)
    if nickname(chat_id, uid):
        from handlers.roast import _react, _spawn

        _spawn(_react(bot, chat_id, message.message_id, "🤡", force=True))
    c = _courts.get(chat_id)
    text = (message.text or message.caption or "").strip()
    if not c or c["phase"] != "defense" or not text or text.startswith("/"):
        return
    if c["lawyer"] and uid == c["lawyer"][0] and not c["defense"]:
        c["defense"] = text[:300]
    elif uid == c["defendant"][0] and not c["last_word"]:
        c["last_word"] = text[:300]
    if (c["defense"] or not c["lawyer"]) and c["last_word"] and c.get("wake"):
        c["wake"].set()  # оба высказались — к голосованию, не дожидаясь таймера


def _court_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛡 Стать адвокатом",
                                                                       callback_data="sud:law")]])


def _vote_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚖️ Виновен", callback_data="sud:g"),
                                                  InlineKeyboardButton(text="🕊 Оправдать", callback_data="sud:a")]])


@router.message(Command("sud"))
async def cmd_sud(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    from handlers.roast import _bunker_active

    chat_id = message.chat.id
    if message.chat.type not in ("group", "supergroup"):
        return
    rt = message.reply_to_message
    target = None
    if rt and rt.from_user and not rt.from_user.is_bot:
        target = (rt.from_user.id, _name(rt.from_user))
    elif (command.args or "").strip().startswith("@"):
        found = await db.find_user(chat_id, command.args.strip().split()[0])
        target = (found[0], found[1] or command.args.strip()) if found else None
    if not target:
        await temp_reply(message, "Кого судим? /sud ответом на его сообщение или /sud @ник.")
        return
    if chat_id in _courts and _courts[chat_id]["phase"] != "done" and time.time() - _courts[chat_id]["started"] < 900:
        await temp_reply(message, "Суд уже идёт - дождитесь приговора.")
        return
    if _bunker_active(chat_id):
        return
    if await db.is_optout(chat_id, target[0]):
        await temp_reply(message, "Этого человека бот не запоминает - судить не за что.")
        return
    if not ai.available():
        await temp_reply(message, "ИИ сейчас недоступен - прокурор заболел.")
        return
    uid, name = target
    rows = await db.recent_messages(chat_id, limit=60, user_id=uid, since=int(time.time()) - EVIDENCE_DAYS * 86400)
    rows = [r for r in rows if not str(r[3]).startswith("[")]
    if len(rows) < 3:
        await temp_reply(message, f"На {name} почти ничего нет за неделю - суду нечего предъявить.")
        return
    c = {"phase": "prep", "defendant": target, "accuser": (message.from_user.id, _name(message.from_user)),
         "lawyer": None, "defense": "", "last_word": "", "votes": {}, "started": time.time()}
    _courts[chat_id] = c
    try:
        await bot.send_chat_action(chat_id, "typing")
        promises = [p["what"] for p in await db.promises_due_between(chat_id, int(time.time()) - 14 * 86400,
                                                                     int(time.time())) if p["name"] == name]
        res = await ai.court_charges(name, await db.get_dossier(chat_id, uid),
                                     [{"id": r[0], "name": r[2], "text": r[3]} for r in rows], promises)
    except Exception as e:
        logger.warning("court charges failed: %s", e)
        c["phase"] = "done"
        await temp_reply(message, "Прокурор не явился (ИИ не ответил) - попробуй ещё раз.")
        return
    by_id = {r[0]: r for r in rows}
    charges = []
    for ch in res["charges"]:
        r = by_id.get(ch["msg_id"])
        if not r:
            continue  # улика не из его сообщений — такую статью не предъявляем
        date = datetime.fromtimestamp(r[4], MSK).strftime("%d.%m")
        quote = f"«{str(r[3])[:140]}» ({date})" + (f" - {ch['note']}" if ch["note"] else "")
        charges.append((ch["title"], quote))
    if not charges:
        c["phase"] = "done"
        await bot.send_message(chat_id, f"⚖️ Суд над <b>{html.escape(name)}</b>: улик не нашлось - оправдан за "
                                        "отсутствием состава. Пока что.")
        return
    c.update(charges=charges, nickname=res["nickname"] or "Подсудимый", speech=res["speech"])
    c["case"] = int(await db.get_setting(CASE_KEY + str(chat_id), "0") or 0) + 1
    await db.set_setting(CASE_KEY + str(chat_id), str(c["case"]))
    lines = [f"⚖️ <b>СУД ЧАТА · Дело № {c['case']}</b>",
             f"Чат против <b>{html.escape(name)}</b> · иск подал {html.escape(c['accuser'][1])}", "",
             "<b>ОБВИНЕНИЕ</b>"]
    for title, quote in charges:
        lines += [f"<b>{html.escape(title)}</b>", html.escape(quote)]
    if c["speech"]:
        lines += ["", f"🗣 <i>{html.escape(c['speech'])}</i>"]
    lines += ["", "🛡 Кто адвокат? Первый нажавший защищает - пиши речь следом.",
              f"🎤 {html.escape(name)}, твоё последнее слово - следующим сообщением.",
              f"⏳ Через {DEFENSE_TIME // 60} мин - голосование присяжных."]
    sent = await bot.send_message(chat_id, "\n".join(lines), reply_markup=_court_kb())
    c["msg_id"] = sent.message_id
    c["phase"] = "defense"
    c["wake"] = asyncio.Event()
    task = asyncio.create_task(_run(bot, db, chat_id, c))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


@router.callback_query(F.data == "sud:law")
async def cb_lawyer(call: CallbackQuery, bot: Bot) -> None:
    c = _courts.get(call.message.chat.id) if call.message else None
    if not c or c["phase"] != "defense":
        await call.answer("Заседание уже не принимает адвокатов.")
        return
    if call.from_user.id == c["defendant"][0]:
        await call.answer("Сам себя не защитишь - найди друга.", show_alert=True)
        return
    if c["lawyer"]:
        await call.answer(f"Адвокат уже есть: {c['lawyer'][1]}.")
        return
    c["lawyer"] = (call.from_user.id, _name(call.from_user))
    await call.answer("Ты адвокат. Пиши речь в защиту следующим сообщением.", show_alert=True)
    try:
        await bot.send_message(call.message.chat.id,
                               f"🛡 Адвокат: <b>{html.escape(c['lawyer'][1])}</b>. Суд слушает защиту.")
    except Exception as e:
        logger.debug("lawyer note: %s", e)


@router.callback_query(F.data.in_({"sud:g", "sud:a"}))
async def cb_vote(call: CallbackQuery) -> None:
    c = _courts.get(call.message.chat.id) if call.message else None
    if not c or c["phase"] != "vote" or call.message.message_id != c.get("vote_msg"):
        await call.answer("Голосование закрыто.")
        return
    if call.from_user.id == c["defendant"][0]:
        await call.answer("Подсудимый не голосует.", show_alert=True)
        return
    guilty = call.data == "sud:g"
    c["votes"][call.from_user.id] = guilty
    await call.answer("Голос принят: " + ("виновен" if guilty else "оправдать") + ". Можно передумать до конца.")
    now = time.time()
    if now - _vote_edit.get(c["vote_msg"], 0) >= 2:  # счётчик — не чаще раза в 2 сек (лимит правок)
        _vote_edit[c["vote_msg"]] = now
        try:
            await call.message.edit_text(_vote_text(c), reply_markup=_vote_kb())
        except TelegramBadRequest:
            pass


def _vote_text(c: dict) -> str:
    return (f"🗳 <b>Присяжные, ваш вердикт по делу № {c['case']}</b> ({html.escape(c['defendant'][1])})\n"
            f"Голосование тайное, подсудимый не голосует. Проголосовали: {len(c['votes'])}")


async def _run(bot: Bot, db: Database, chat_id: int, c: dict) -> None:
    import cards
    from handlers.roast import _send_card

    try:
        try:
            await asyncio.wait_for(c["wake"].wait(), DEFENSE_TIME)
        except asyncio.TimeoutError:
            pass
        c["phase"] = "vote"
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=c["msg_id"], reply_markup=None)
        except TelegramBadRequest:
            pass
        vote = await bot.send_message(chat_id, _vote_text(c), reply_markup=_vote_kb())
        c["vote_msg"] = vote.message_id
        await asyncio.sleep(VOTE_TIME)
        c["phase"] = "closing"
        g = sum(1 for v in c["votes"].values() if v)
        a = len(c["votes"]) - g
        guilty = g > a or not c["votes"]
        try:
            await bot.edit_message_text(
                f"🗳 Голосование по делу № {c['case']} закрыто: виновен {g} · оправдать {a}"
                + (" · присяжные слились, решал судья" if not c["votes"] else ""),
                chat_id=chat_id, message_id=c["vote_msg"])
        except TelegramBadRequest:
            pass
        name = c["defendant"][1]
        try:
            speech = await ai.court_verdict(name, [t for t, _q in c["charges"]], c["defense"], c["last_word"],
                                            guilty, f"виновен {g}, оправдать {a}", c["accuser"][1])
        except Exception as e:
            logger.warning("court verdict AI failed: %s", e)
            speech = ""
        date = datetime.now(MSK).strftime("%d.%m")
        png = await asyncio.to_thread(cards.render_verdict, c["case"], date, name, c["charges"], guilty, g, a,
                                      c["lawyer"][1] if c["lawyer"] else "", c["defense"][:80], c["nickname"])
        verdict = "ВИНОВЕН" if guilty else "ОПРАВДАН"
        caption = f"⚖️ <b>Приговор по делу № {c['case']}: {html.escape(name)} - {verdict}</b>"
        if speech:
            caption += f"\n\n{html.escape(speech)}"
        if guilty:
            caption += (f"\n\nНа сутки: прозвище «{html.escape(c['nickname'])}» и 🤡 на каждое сообщение.")
        await _send_card(bot, chat_id, png, caption,
                         f"[суд, дело № {c['case']}] {name} - {verdict.lower()} ({g}:{a})"
                         + (f", прозвище на сутки «{c['nickname']}»" if guilty else "") + (f". {speech}" if speech else ""))
        if guilty:
            await _load_punished(db)
            _punished[f"{chat_id}:{c['defendant'][0]}"] = [time.time() + PUNISH, c["nickname"]]
            await _save_punished(db)
        logger.info("суд %s: дело %s, %s - %s (%s:%s)", chat_id, c["case"], name, verdict, g, a)
    except Exception:
        logger.exception("court failed in %s", chat_id)
    finally:
        c["phase"] = "done"
