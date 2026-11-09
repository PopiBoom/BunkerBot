"""Письма подписчиков для «Писем Волгограда» и комментарии под выпусками (владелец 08.11); тем же путём — сплетни
для «Сплетен Волгограда» (09.11, kind="gossip": ссылка ?start=gossip, выпуск — /spletni).

Письма: в личке у бота кнопка «✉️ Отправить письмо» (/start и ссылка t.me/<бот>?start=letter из подписи выпуска) →
«напиши коротко, что случилось…» → одно сообщение текстом → «как подписать: именем / анонимно» → письмо уходит
админам на утверждение («✅ Взять в эфир / 🗑 Отклонить»). Взятые — в /horror: админ выбирает, из какого письма
делать выпуск (или «придумать самому»). Вышел выпуск — автору ссылка. Хранится в настройке INBOX_KEY (последние
MAX_KEEP). Не чаще DAY_LIMIT писем в сутки от человека.

Комментарии: опубликованный выпуск (POST_KEY) приходит копией в группу комментариев — её id ловим (THREAD_KEY),
сообщения в этой ветке копим (COMMENTS_KEY), и в следующем выпуске ведущий зачитывает 1-2 из них. Ловит observe()
из middleware в bot.py.
"""
from __future__ import annotations

import html
import json
import logging
import time

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import Config
from database import Database

logger = logging.getLogger("letters")
router = Router(name="letters")

INBOX_KEY = "horror_inbox"
POST_KEY = "horror_post"          # {"channel", "msg", "n"} — последний выпуск в канале
THREAD_KEY = "horror_thread"      # {"chat", "root"} — его копия в группе комментариев
COMMENTS_KEY = "horror_comments"  # [{"name", "text"}] — комментарии к нему
MAX_KEEP = 60
DAY_LIMIT = 3
MIN_LEN, MAX_LEN = 30, 2500
WAIT_TTL = 30 * 60
MAX_COMMENTS = 40

PROMPT = ("✉️ <b>Письмо в «Письма Волгограда»</b>\n\n"
          "Напиши коротко, что с тобой случилось: где в Волгограде (район, улица, торгушка, остановка), когда и что "
          "было странного или страшного. Одним сообщением, текстом.\n\n"
          "Возможно, следующий выпуск будет именно про твой случай.")

GOSSIP_PROMPT = ("🗣 <b>Сплетня для «Сплетен Волгограда»</b>\n\n"
                 "Расскажи, что слышал: кто (без фамилий - имена мы всё равно поменяем), где в Волгограде, что "
                 "случилось и что про это говорят. Одним сообщением, текстом.\n\n"
                 "Самые сочные выйдут в эфир - с твоим именем или анонимно, как скажешь.")
# Тексты по виду: письмо в «Письма Волгограда» (horror) или сплетня в «Сплетни Волгограда» (gossip)
KINDS = {
    "horror": {"prompt": PROMPT, "button": "✉️ Отправить письмо", "what": "Письмо", "rubric": "«Писем Волгограда»",
               "cmd": "/horror", "sign": "Как подписать письмо в эфире?",
               "sent": "📨 Письмо ушло в редакцию «Писем Волгограда». Если его возьмут в эфир - бот напишет и пришлёт "
                       "ссылку на выпуск.",
               "taken": "🎙 Твоё письмо приняли в редакцию «Писем Волгограда». Когда выйдет выпуск - пришлю ссылку.",
               "used": "🎙 Твоё письмо вышло в эфир «Писем Волгограда»", "thanks": "Спасибо, что не побоялся рассказать.",
               "short": "Маловато - расскажи чуть подробнее: где это было и что случилось."},
    "gossip": {"prompt": GOSSIP_PROMPT, "button": "🗣 Слить сплетню", "what": "Сплетня", "rubric": "«Сплетен Волгограда»",
               "cmd": "/spletni", "sign": "Как подписать сплетню в эфире?",
               "sent": "🤫 Сплетня ушла в редакцию «Сплетен Волгограда». Если её возьмут в эфир - бот напишет и пришлёт "
                       "ссылку на выпуск.",
               "taken": "🎙 Твою сплетню взяли в «Сплетни Волгограда». Когда выйдет выпуск - пришлю ссылку.",
               "used": "🎙 Твоя сплетня вышла в эфир «Сплетен Волгограда»", "thanks": "Спасибо, что поделился 🤫",
               "short": "Маловато - расскажи подробнее: кто, где и что случилось."},
}

_waiting: dict[int, tuple[float, str]] = {}  # кто сейчас пишет: uid -> (когда нажал, вид)
_pending: dict[int, dict] = {}        # письмо ждёт выбора подписи: uid -> {"text", "ts", "kind"}
_cache: dict = {"loaded": False, "post": None, "thread": None}


def _kind(x: dict | str | None) -> str:
    k = x.get("kind", "horror") if isinstance(x, dict) else (x or "horror")
    return k if k in KINDS else "horror"


def letter_button(kind: str = "horror") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=KINDS[_kind(kind)]["button"],
                                callback_data="lt:new" if _kind(kind) == "horror" else f"lt:new:{kind}")


def deep_link(bot_username: str, kind: str = "horror") -> str:
    return (f"https://t.me/{bot_username}?start={'letter' if _kind(kind) == 'horror' else kind}"
            if bot_username else "")


async def _load(db: Database) -> list[dict]:
    try:
        data = json.loads(await db.get_setting(INBOX_KEY, "[]") or "[]")
        return data if isinstance(data, list) else []
    except ValueError:
        return []


async def _save(db: Database, items: list[dict]) -> None:
    await db.set_setting(INBOX_KEY, json.dumps(items[-MAX_KEEP:], ensure_ascii=False))


async def approved(db: Database, kind: str = "horror") -> list[dict]:
    """Взятые в эфир и ещё не вышедшие письма (или сплетни — kind="gossip"), новые сверху."""
    return [x for x in reversed(await _load(db)) if x.get("status") == "ok" and _kind(x) == kind]


async def get(db: Database, lid: int) -> dict | None:
    return next((x for x in await _load(db) if x.get("id") == lid), None)


async def mark_used(bot: Bot, db: Database, lid: int, link: str) -> None:
    """Выпуск по письму вышел: статус «used», автору — ссылка на выпуск."""
    items = await _load(db)
    for x in items:
        if x.get("id") == lid:
            x["status"], x["link"] = "used", link
            t = KINDS[_kind(x)]
            try:
                await bot.send_message(x["uid"], t["used"] + (f": {link}" if link else ".") + "\n" + t["thanks"])
            except Exception as e:
                logger.info("letters: автору %s не написать: %s", x.get("uid"), e)
    await _save(db, items)


async def ask(bot: Bot, uid: int, kind: str = "horror") -> None:
    kind = _kind(kind)
    _waiting[uid] = (time.time(), kind)
    _pending.pop(uid, None)
    await bot.send_message(uid, KINDS[kind]["prompt"], reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✖️ Отмена", callback_data="lt:cancel")]]))


@router.callback_query(F.data.startswith("lt:new"))
async def cb_new(call: CallbackQuery, bot: Bot) -> None:
    await call.answer()
    try:
        await ask(bot, call.from_user.id, (call.data or "")[7:] or "horror")
    except Exception as e:
        logger.info("letters: не написать %s: %s", call.from_user.id, e)


@router.callback_query(F.data == "lt:cancel")
async def cb_cancel(call: CallbackQuery) -> None:
    _waiting.pop(call.from_user.id, None)
    _pending.pop(call.from_user.id, None)
    await call.answer("Отменил.")
    if call.message:
        try:
            await call.message.edit_text("Ок, не отправляю. Передумаешь - жми кнопку в /start.")
        except Exception:
            pass


def _is_writing(message: Message) -> bool:
    u = message.from_user
    if message.chat.type != "private" or not u:
        return False
    ts = (_waiting.get(u.id) or (0, ""))[0]
    if not ts or time.time() - ts > WAIT_TTL:
        _waiting.pop(u.id, None)
        return False
    return not (message.text or "").startswith("/")


@router.message(_is_writing)
async def on_letter(message: Message, db: Database) -> None:
    uid = message.from_user.id
    kind = (_waiting.get(uid) or (0, "horror"))[1]
    text = (message.text or message.caption or "").strip()
    if not text:
        await message.answer("Пришли историю текстом, пожалуйста - голосовые и картинки сюда пока не принимаю.")
        return
    if len(text) < MIN_LEN:
        await message.answer(KINDS[kind]["short"])
        return
    day = [x for x in await _load(db) if x.get("uid") == uid and time.time() - x.get("ts", 0) < 86400]
    if len(day) >= DAY_LIMIT:
        _waiting.pop(uid, None)
        await message.answer("На сегодня писем хватит - редакция и так не спит. Приходи завтра.")
        return
    _waiting.pop(uid, None)
    _pending[uid] = {"text": text[:MAX_LEN], "ts": time.time(), "kind": kind}
    name = (message.from_user.first_name or "").strip()[:30] or "без имени"
    await message.answer(KINDS[kind]["sign"], reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"Именем «{name}»", callback_data="lt:sign:1"),
        InlineKeyboardButton(text="Анонимно", callback_data="lt:sign:0")]]))


@router.callback_query(F.data.startswith("lt:sign:"))
async def cb_sign(call: CallbackQuery, bot: Bot, db: Database, config: Config) -> None:
    u = call.from_user
    p = _pending.pop(u.id, None)
    if not p:
        await call.answer("Письмо уже отправлено или устарело.", show_alert=True)
        return
    named = call.data.endswith(":1")
    items = await _load(db)
    lid = max([x.get("id", 0) for x in items] + [0]) + 1
    name = (u.first_name or "").strip()[:30]
    kind = _kind(p.get("kind"))
    t = KINDS[kind]
    entry = {"id": lid, "uid": u.id, "name": name, "username": u.username or "", "sign": name if named else "",
             "text": p["text"], "ts": int(time.time()), "status": "new", "kind": kind}
    items.append(entry)
    await _save(db, items)
    await call.answer("Отправлено!")
    if call.message:
        try:
            await call.message.edit_text(t["sent"])
        except Exception:
            pass
    who = html.escape(name or "без имени") + (f" (@{html.escape(u.username)})" if u.username else "")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Взять в эфир", callback_data=f"lt:ok:{lid}"),
        InlineKeyboardButton(text="🗑 Отклонить", callback_data=f"lt:no:{lid}")]])
    for aid in config.admin_ids:
        try:
            await bot.send_message(aid, f"{t['button'].split()[0]} <b>{t['what']} № {lid}</b> для {t['rubric']} от {who}\n"
                                        f"Подпись в эфире: {html.escape(entry['sign']) or 'анонимно'}\n\n"
                                        f"<blockquote expandable>{html.escape(entry['text'])}</blockquote>",
                                   reply_markup=kb)
        except Exception as e:
            logger.info("letters: админу %s не отправить: %s", aid, e)
    logger.info("letters: %s № %s от %s (%s знаков)", kind, lid, u.id, len(entry["text"]))


@router.callback_query(F.data.startswith("lt:ok:") | F.data.startswith("lt:no:"))
async def cb_review(call: CallbackQuery, bot: Bot, db: Database, config: Config) -> None:
    if not config.is_admin(call.from_user.id):
        await call.answer("Это для админа.", show_alert=True)
        return
    ok = call.data.startswith("lt:ok:")
    try:
        lid = int(call.data.split(":")[2])
    except (IndexError, ValueError):
        await call.answer()
        return
    items = await _load(db)
    x = next((i for i in items if i.get("id") == lid), None)
    if not x:
        await call.answer("Письмо не нашлось.", show_alert=True)
        return
    if x.get("status") not in ("new", "ok", "no"):
        await call.answer("Это письмо уже вышло в эфир.", show_alert=True)
        return
    was = x.get("status")
    x["status"] = "ok" if ok else "no"
    await _save(db, items)
    t = KINDS[_kind(x)]
    await call.answer(f"Взято - выбери в {t['cmd']}." if ok else "Отклонено.")
    if call.message:
        try:
            await call.message.edit_reply_markup(reply_markup=None)
            await call.message.reply(f"✅ Взято в эфир - в {t['cmd']} выбери, из чего делать выпуск." if ok
                                     else "🗑 Отклонено.")
        except Exception:
            pass
    if ok and was != "ok":
        try:
            await bot.send_message(x["uid"], t["taken"])
        except Exception as e:
            logger.info("letters: автору %s не написать: %s", x.get("uid"), e)


# ---------- комментарии под выпуском ----------


async def _load_cache(db: Database) -> None:
    if _cache["loaded"]:
        return
    for key, field in ((POST_KEY, "post"), (THREAD_KEY, "thread")):
        try:
            _cache[field] = json.loads(await db.get_setting(key, "") or "null")
        except ValueError:
            _cache[field] = None
    _cache["loaded"] = True


async def on_published(db: Database, channel: int, msg: int, n: int) -> None:
    """Выпуск ушёл в канал: дальше копим комментарии под ним (прошлые — уже зачитаны)."""
    _cache.update(loaded=True, post={"channel": channel, "msg": msg, "n": n}, thread=None)
    await db.set_setting(POST_KEY, json.dumps(_cache["post"]))
    await db.set_setting(THREAD_KEY, "")
    await db.set_setting(COMMENTS_KEY, "[]")


async def comments(db: Database) -> list[dict]:
    try:
        data = json.loads(await db.get_setting(COMMENTS_KEY, "[]") or "[]")
        return data if isinstance(data, list) else []
    except ValueError:
        return []


async def observe(message: Message, db: Database) -> None:
    """Из middleware: копия выпуска в группе комментариев и комментарии под ней."""
    if message.chat.type not in ("group", "supergroup"):
        return
    await _load_cache(db)
    post = _cache["post"]
    if not post:
        return
    if message.is_automatic_forward:
        fo = message.forward_origin
        chat, mid = getattr(fo, "chat", None), getattr(fo, "message_id", None)
        if chat and chat.id == post["channel"] and mid == post["msg"]:
            _cache["thread"] = {"chat": message.chat.id, "root": message.message_id}
            await db.set_setting(THREAD_KEY, json.dumps(_cache["thread"]))
        return
    th = _cache["thread"]
    if not th or message.chat.id != th["chat"]:
        return
    rt = message.reply_to_message
    if message.message_thread_id != th["root"] and not (rt and rt.message_id == th["root"]):
        return
    u = message.from_user
    text = (message.text or message.caption or "").strip()
    if not u or u.is_bot or message.sender_chat or not text or text.startswith("/"):
        return
    if await db.is_optout(message.chat.id, u.id):
        return
    items = await comments(db)
    items.append({"name": (u.first_name or "слушатель")[:30], "text": text[:300]})
    await db.set_setting(COMMENTS_KEY, json.dumps(items[-MAX_COMMENTS:], ensure_ascii=False))
