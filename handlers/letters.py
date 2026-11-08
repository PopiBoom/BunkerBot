"""Письма подписчиков для «Писем Волгограда» и комментарии под выпусками (владелец 08.11).

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

_waiting: dict[int, float] = {}       # кто сейчас пишет письмо: uid -> когда нажал
_pending: dict[int, dict] = {}        # письмо ждёт выбора подписи: uid -> {"text", "ts"}
_cache: dict = {"loaded": False, "post": None, "thread": None}


def letter_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="✉️ Отправить письмо", callback_data="lt:new")


def deep_link(bot_username: str) -> str:
    return f"https://t.me/{bot_username}?start=letter" if bot_username else ""


async def _load(db: Database) -> list[dict]:
    try:
        data = json.loads(await db.get_setting(INBOX_KEY, "[]") or "[]")
        return data if isinstance(data, list) else []
    except ValueError:
        return []


async def _save(db: Database, items: list[dict]) -> None:
    await db.set_setting(INBOX_KEY, json.dumps(items[-MAX_KEEP:], ensure_ascii=False))


async def approved(db: Database) -> list[dict]:
    """Взятые в эфир и ещё не вышедшие письма, новые сверху."""
    return [x for x in reversed(await _load(db)) if x.get("status") == "ok"]


async def get(db: Database, lid: int) -> dict | None:
    return next((x for x in await _load(db) if x.get("id") == lid), None)


async def mark_used(bot: Bot, db: Database, lid: int, link: str) -> None:
    """Выпуск по письму вышел: статус «used», автору — ссылка на выпуск."""
    items = await _load(db)
    for x in items:
        if x.get("id") == lid:
            x["status"], x["link"] = "used", link
            try:
                await bot.send_message(x["uid"], "🎙 Твоё письмо вышло в эфир «Писем Волгограда»"
                                       + (f": {link}" if link else ".") + "\nСпасибо, что не побоялся рассказать.")
            except Exception as e:
                logger.info("letters: автору %s не написать: %s", x.get("uid"), e)
    await _save(db, items)


async def ask(bot: Bot, uid: int) -> None:
    _waiting[uid] = time.time()
    _pending.pop(uid, None)
    await bot.send_message(uid, PROMPT, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✖️ Отмена", callback_data="lt:cancel")]]))


@router.callback_query(F.data == "lt:new")
async def cb_new(call: CallbackQuery, bot: Bot) -> None:
    await call.answer()
    try:
        await ask(bot, call.from_user.id)
    except Exception as e:
        logger.info("letters: не написать %s: %s", call.from_user.id, e)


@router.callback_query(F.data == "lt:cancel")
async def cb_cancel(call: CallbackQuery) -> None:
    _waiting.pop(call.from_user.id, None)
    _pending.pop(call.from_user.id, None)
    await call.answer("Отменил.")
    if call.message:
        try:
            await call.message.edit_text("Ок, письмо не отправляю. Передумаешь - жми «✉️ Отправить письмо» в /start.")
        except Exception:
            pass


def _is_writing(message: Message) -> bool:
    u = message.from_user
    if message.chat.type != "private" or not u:
        return False
    ts = _waiting.get(u.id)
    if not ts or time.time() - ts > WAIT_TTL:
        _waiting.pop(u.id, None)
        return False
    return not (message.text or "").startswith("/")


@router.message(_is_writing)
async def on_letter(message: Message, db: Database) -> None:
    uid = message.from_user.id
    text = (message.text or message.caption or "").strip()
    if not text:
        await message.answer("Пришли историю текстом, пожалуйста - голосовые и картинки сюда пока не принимаю.")
        return
    if len(text) < MIN_LEN:
        await message.answer("Маловато - расскажи чуть подробнее: где это было и что случилось.")
        return
    day = [x for x in await _load(db) if x.get("uid") == uid and time.time() - x.get("ts", 0) < 86400]
    if len(day) >= DAY_LIMIT:
        _waiting.pop(uid, None)
        await message.answer("На сегодня писем хватит - редакция и так не спит. Приходи завтра.")
        return
    _waiting.pop(uid, None)
    _pending[uid] = {"text": text[:MAX_LEN], "ts": time.time()}
    name = (message.from_user.first_name or "").strip()[:30] or "без имени"
    await message.answer("Как подписать письмо в эфире?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
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
    entry = {"id": lid, "uid": u.id, "name": name, "username": u.username or "", "sign": name if named else "",
             "text": p["text"], "ts": int(time.time()), "status": "new"}
    items.append(entry)
    await _save(db, items)
    await call.answer("Отправлено!")
    if call.message:
        try:
            await call.message.edit_text("📨 Письмо ушло в редакцию «Писем Волгограда». Если его возьмут в эфир - "
                                         "бот напишет и пришлёт ссылку на выпуск.")
        except Exception:
            pass
    who = html.escape(name or "без имени") + (f" (@{html.escape(u.username)})" if u.username else "")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Взять в эфир", callback_data=f"lt:ok:{lid}"),
        InlineKeyboardButton(text="🗑 Отклонить", callback_data=f"lt:no:{lid}")]])
    for aid in config.admin_ids:
        try:
            await bot.send_message(aid, f"✉️ <b>Письмо № {lid}</b> для «Писем Волгограда» от {who}\n"
                                        f"Подпись в эфире: {html.escape(entry['sign']) or 'анонимно'}\n\n"
                                        f"<blockquote expandable>{html.escape(entry['text'])}</blockquote>",
                                   reply_markup=kb)
        except Exception as e:
            logger.info("letters: админу %s не отправить: %s", aid, e)
    logger.info("letters: письмо № %s от %s (%s знаков)", lid, u.id, len(entry["text"]))


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
    await call.answer("Взято - выбери его в /horror." if ok else "Отклонено.")
    if call.message:
        try:
            await call.message.edit_reply_markup(reply_markup=None)
            await call.message.reply("✅ Взято в эфир - в /horror выбери, из какого письма делать выпуск." if ok
                                     else "🗑 Отклонено.")
        except Exception:
            pass
    if ok and was != "ok":
        try:
            await bot.send_message(x["uid"], "🎙 Твоё письмо приняли в редакцию «Писем Волгограда». Когда выйдет "
                                             "выпуск - пришлю ссылку.")
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
