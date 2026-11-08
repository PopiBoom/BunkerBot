"""/sud — суд чата (идея и макет приговора — с владельцем, 07.11).

Админ: /sud ответом на сообщение человека (это сообщение — повод, статья 1) или /sud @ник. Дальше:
1) ИИ-прокурор (ai.court_charges) видит переписку ВСЕХ вокруг повода и пишет «обстоятельства дела» (о чём
   говорили, что он написал, кто как отреагировал) и 1-3 статьи — улики ДОСЛОВНО из его сообщений за неделю (ИИ
   даёт только id, текст подставляет код); один разговор — одна статья (статьи по сообщениям в пределах
   SAME_TALK сек код склеивает); нет ни одной настоящей улики — «оправдан за отсутствием состава». Владелец 08.11:
   «неразборчиво, однообразно, нет конкретики» — прокурор видел только реплики подсудимого, статьи были
   «цитата - та же цитата», Ст. 1 и Ст. 2 — про одно и то же, и в каждом деле «в особо крупном».
2) Защита: адвоката ждём LAWYER_WAIT (кнопка «🛡 Стать адвокатом» — первый нажавший, не подсудимый); выбрали —
   у него SPEECH_TIME на речь (все его сообщения в это время); последнее слово — ответ подсудимого на сообщение
   суда (иначе все его сообщения за это время). Оба высказались — ещё GRACE сек и голосование; за 30 сек —
   напоминание. Ловит observe() из middleware в bot.py. (Было: 2 мин на всё, и ЛЮБОЕ сообщение подсудимого без
   адвоката сразу запускало голосование — владелец: «так мало времени на выбор адвоката и на защиту».)
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
import re
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
LAWYER_WAIT = 180      # сек ждём, пока кто-то нажмёт «Стать адвокатом»
SPEECH_TIME = 180      # сек адвокату на речь с момента, как он вызвался
GRACE = 30             # сек после того, как высказались оба (вдруг допишут), — и голосование
VOTE_TIME = 120        # сек на голосование
PUNISH = 24 * 3600     # сек: прозвище и 🤡 на каждое сообщение
EVIDENCE_DAYS = 7      # улики — из сообщений за столько дней
CASE_KEY = "court_no:"
RECENT_KEY = "court_recent:"   # прошлые дела чата (статьи и речь) — чтобы прокурор не повторялся
SAME_TALK = 20 * 60    # сообщения подсудимого ближе этого — один разговор, одна статья
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
    rt = message.reply_to_message
    to_court = bool(rt and rt.message_id == c.get("msg_id"))
    if c["lawyer"] and uid == c["lawyer"][0]:
        c["defense"] = (c["defense"] + " / " + text if c["defense"] else text)[:400]  # речь бывает в несколько сообщений
    elif uid == c["defendant"][0] and not c.get("last_word_final"):
        if to_court:  # ответ на сообщение суда — это и есть последнее слово
            c["last_word"], c["last_word_final"] = text[:300], True
        else:
            c["last_word"] = (c["last_word"] + " / " + text if c["last_word"] else text)[:300]
    if c["lawyer"] and c["defense"] and c["last_word"]:  # оба высказались — ещё чуть-чуть, и голосование
        c["deadline"] = min(c["deadline"], time.time() + GRACE)


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
        main = None
        if rt and rt.from_user and rt.from_user.id == uid and (rt.text or rt.caption):
            # повод для суда — сообщение, ответом на которое подали иск: статья 1 за него
            main = {"id": rt.message_id, "text": rt.text or rt.caption}
            if rt.message_id not in {r[0] for r in rows}:
                rows.append((rt.message_id, uid, name, main["text"], int(rt.date.timestamp())))
                rows.sort(key=lambda r: r[4])
        context = await _context(db, chat_id, uid, rows, main and int(rt.date.timestamp()))
        try:
            recent = list(json.loads(await db.get_setting(RECENT_KEY + str(chat_id), "[]") or "[]"))
        except (ValueError, TypeError):
            recent = []
        res = await ai.court_charges(name, await db.get_dossier(chat_id, uid),
                                     [{"id": r[0], "name": r[2], "text": r[3]} for r in rows], promises, main=main,
                                     context=context, recent=recent)
    except Exception as e:
        logger.warning("court charges failed: %s", e)
        c["phase"] = "done"
        await temp_reply(message, "Прокурор не явился (ИИ не ответил) - попробуй ещё раз.")
        return
    groups = _group_charges(res["charges"], {r[0]: r for r in rows})
    charges = [(f"Ст. {i} · {g['title']}", _evidence(g["rows"])) for i, g in enumerate(groups, 1)]
    notes = [g["note"] for g in groups]
    if not charges:
        c["phase"] = "done"
        await bot.send_message(chat_id, f"⚖️ Суд над <b>{html.escape(name)}</b>: улик не нашлось - оправдан за "
                                        "отсутствием состава. Пока что.")
        return
    c.update(charges=charges, nickname=res["nickname"] or "Подсудимый", speech=res["speech"], fabula=res["fabula"])
    c["case"] = int(await db.get_setting(CASE_KEY + str(chat_id), "0") or 0) + 1
    await db.set_setting(CASE_KEY + str(chat_id), str(c["case"]))
    recent.append(" | ".join([t for t, _q in charges] + ([c["speech"]] if c["speech"] else [])))
    await db.set_setting(RECENT_KEY + str(chat_id), json.dumps(recent[-5:], ensure_ascii=False))
    lines = [f"⚖️ <b>СУД ЧАТА · Дело № {c['case']}</b>",
             f"Чат против <b>{html.escape(name)}</b> · иск подал {html.escape(c['accuser'][1])}", ""]
    if c["fabula"]:
        day = datetime.fromtimestamp(groups[0]["rows"][0][4], MSK).strftime("%d.%m")
        lines += [f"<b>ОБСТОЯТЕЛЬСТВА · {day}</b>", html.escape(c["fabula"]), ""]
    lines.append("<b>ОБВИНЕНИЕ</b>")
    for (title, quote), note in zip(charges, notes, strict=True):
        lines += [f"<b>{html.escape(title)}</b>", html.escape(quote)] + ([f"<i>{html.escape(note)}</i>"] if note else [])
    if c["speech"]:
        lines += ["", f"🗣 <i>{html.escape(c['speech'])}</i>"]
    lines += ["", f"🛡 Кто адвокат? Первый нажавший защищает, на речь - {SPEECH_TIME // 60} мин.",
              f"🎤 {html.escape(name)}, последнее слово - ответом на это сообщение.",
              f"⏳ Адвоката ждём {LAWYER_WAIT // 60} мин, потом голосование присяжных."]
    sent = await bot.send_message(chat_id, "\n".join(lines), reply_markup=_court_kb())
    c["msg_id"] = sent.message_id
    c["phase"] = "defense"
    c["deadline"] = time.time() + LAWYER_WAIT
    task = asyncio.create_task(_run(bot, db, chat_id, c))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _context(db: Database, chat_id: int, uid: int, rows: list[tuple], main_ts: int | None) -> list[dict]:
    """Переписка всех вокруг повода (±10-12 сообщений); судят по @нику — по 3 до и 2 после каждого его сообщения
    (последние 20 непустых), не больше 120 строк."""
    if main_ts:
        near = await db.messages_near(chat_id, main_ts, 12, 10)
    else:
        near, seen = [], set()
        for r in [r for r in rows if len(str(r[3])) >= 8][-20:]:
            for m in await db.messages_near(chat_id, r[4], 3, 2, span=1800):
                if m[0] not in seen:
                    seen.add(m[0])
                    near.append(m)
        near = sorted(near, key=lambda m: (m[4], m[0]))[-120:]
    return [{"id": m[0], "name": m[2] or "?", "text": m[3]} for m in near]


def _overlap(a: str, b: str) -> float:
    """Какая доля слов a есть в b (пометка, которая просто повторяет цитату, — не нужна)."""
    wa = {w for w in re.findall(r"\w+", ai._norm(a)) if len(w) > 2}
    wb = {w for w in re.findall(r"\w+", ai._norm(b)) if len(w) > 2}
    return len(wa & wb) / len(wa) if wa else 1.0


def _group_charges(raw: list[dict], by_id: dict[int, tuple]) -> list[dict]:
    """Статьи ИИ → улики из ЕГО сообщений (чужие id выкидываем); статьи про один разговор (ближе SAME_TALK)
    склеиваем в одну — раньше «Лера отпиздила всех» и «лан шучу» через минуту шли двумя статьями. Не больше 3."""
    groups: list[dict] = []
    for ch in raw:
        rs = sorted({by_id[i] for i in ch["msg_ids"] if i in by_id}, key=lambda r: r[4])
        if not rs:
            continue
        same = next((g for g in groups if any(abs(r[4] - q[4]) <= SAME_TALK for r in rs for q in g["rows"])), None)
        rows = sorted({*same["rows"], *rs}, key=lambda r: r[4]) if same else rs
        note = ch["note"]
        if note and (_overlap(note, " ".join(str(r[3]) for r in rows)) >= 0.5
                     or _overlap(note, same["title"] if same else ch["title"]) >= 0.6):
            note = ""   # пометка повторяет цитату или название — «цитата - та же цитата»
        if same:
            same["rows"], same["note"] = rows, same["note"] or note
            continue
        if groups and len(" ".join(str(r[3]) for r in rs).split()) < 3:
            continue    # рецидив из одного-двух слов («Сука поспала») — без разговора непонятен, не улика
        groups.append({"title": ch["title"], "rows": rs, "note": note})
    return groups[:3]


def _evidence(rs: list[tuple]) -> str:
    """Улики статьи: «первое» → «второе» (07.11, 23:41) — не больше двух цитат по 110 знаков."""
    quotes = " → ".join(f"«{str(r[3])[:110]}»" for r in rs[:2])
    return quotes + datetime.fromtimestamp(rs[0][4], MSK).strftime(" (%d.%m, %H:%M)")


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
    c["deadline"] = max(c["deadline"], time.time() + SPEECH_TIME)  # на речь — полное время, сколько бы ни прошло
    await call.answer(f"Ты адвокат. Пиши речь в защиту - у тебя {SPEECH_TIME // 60} мин.", show_alert=True)
    try:
        await bot.send_message(call.message.chat.id,
                               f"🛡 Адвокат: <b>{html.escape(c['lawyer'][1])}</b>. Суд слушает защиту - "
                               f"{SPEECH_TIME // 60} мин.")
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
        reminded = False
        while (left := c["deadline"] - time.time()) > 0:  # срок двигают кнопка адвоката и речи (observe)
            if left <= 30 and not reminded and left > 5:
                reminded = True
                try:
                    await bot.send_message(chat_id, "⏳ Через 30 сек - голосование присяжных.")
                except Exception as e:
                    logger.debug("court reminder: %s", e)
            await asyncio.sleep(min(1.0, left))
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
                                            guilty, f"виновен {g}, оправдать {a}", c["accuser"][1],
                                            c.get("fabula", ""))
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
