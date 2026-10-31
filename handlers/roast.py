"""Стёб-бот: запоминает сообщения в группе, ведёт досье на участников и подкалывает их.

Что умеет:
  * сохраняет текстовые сообщения групп (сырые — 30 дней), раз в сутки или после 30 новых
    сообщений человека сжимает их в короткое досье;
  * /roast @ник (или ответом на сообщение) — прожарка по досье и свежим сообщениям;
  * /week — итоги недели с номинациями; сам публикует их по воскресеньям вечером (МСК);
  * иногда сам подкалывает кого-то, когда в чате оживлённо;
  * отвечает, если к нему обращаются, и огрызается, если его посылают;
  * /forget_me — удалить всё о себе и не попадать под стёб, /remember_me — вернуться.
Включается/выключается в /admin (настройка roast_enabled).
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, ReplyParameters

import ai
from database import Database

logger = logging.getLogger("roast")
router = Router(name="roast")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))

ENABLED_KEY = "roast_enabled"
KEEP_DAYS = 30                 # сколько хранить сырые сообщения
INTERVAL_KEY = "roast_interval"  # как часто бот может сам встревать (сек, 0 — никогда)
DEFAULT_INTERVAL = 5 * 60
# (секунды, подпись, сколько новых сообщений нужно, чтобы был повод встрять)
INTERVALS = [
    (10, "10 сек", 1), (60, "1 мин", 2), (5 * 60, "5 мин", 3), (15 * 60, "15 мин", 5),
    (30 * 60, "30 мин", 8), (60 * 60, "1 час", 12), (90 * 60, "1,5 часа", 15),
    (3 * 3600, "3 часа", 20), (6 * 3600, "6 часов", 20), (0, "выкл", 0),
]
HEAVY_EVERY = 10 * 60          # досье, чистка, итоги недели — раз в 10 минут
ROAST_COOLDOWN = 30            # сек между /roast в одном чате
WEEK_COOLDOWN = 3600           # /week не чаще раза в час
MSK = timezone(timedelta(hours=3))

COMEBACKS = [
    "Сам пошёл нахуй 🖕",
    "Только после тебя, дорогуша.",
    "Записал в досье. Глава «Проблемы с агрессией».",
    "Иди сам, я дорогу не знаю — а ты, судя по всему, там частый гость.",
    "Ого, словарный запас кончился? Бывает.",
    "Не могу, я бот. А вот тебе туда — по прописке.",
]

_RUDE = re.compile(
    r"(пош[её]л|иди|пиздуй|вали|катись|отъебись|отвали)\b.{0,12}(на\s*х|нах|в\s*жоп|в\s*пизд|нафиг)"
    r"|нахуй|\bнах\b|заткнись|завали\s*(еба|рот|хлеб)|отъебись|"
    r"\b(тупой|ебан\w*|ёбан\w*|сран\w*|долбан\w*|дебильн\w*|конченн?\w*)\s+бот"
    r"|(бот\w*|\bты)\s*[-—,]?\s*(ты\s+)?(лох|дебил|тупо\w*|говно|хуйня|пидор\w*|чмо|идиот|долбо[её]б\w*"
    r"|уеб\w*|мудак|клоун|кринж\w*)",
    re.IGNORECASE,
)
_SEND_OFF = re.compile(r"на\s*хуй|нахуй|\bнах\b|в\s*жоп|в\s*пизд", re.IGNORECASE)
MIRRORS = ["Сам пошёл нахуй, {n}.", "Сам иди нахуй, {n} 🖕", "Только после тебя, {n}. Сам пошёл нахуй.",
           "{n}, сам пошёл нахуй — ты там уже как дома."]
# Оскорбления в адрес человека (для «защиты бати»)
_INSULT = re.compile(
    r"\b(лох\w*|лошар\w*|дебил\w*|туп(ой|ая|ое|ые|ица|ицa|орыл\w*)|идиот\w*|мудак\w*|мудил\w*|чмо\w*|"
    r"урод\w*|пидор\w*|пидр\w*|сук[аи]|шлюх\w*|долбо[её]б\w*|уеб\w*|у[её]бок\w*|дур[аы]|дурак\w*|"
    r"клоун\w*|кретин\w*|дегенерат\w*|даун\w*|ублюд\w*|гнид\w*|мраз\w*|овц[аы]|скотин\w*|"
    r"чушпан\w*|нищеб\w*|придур\w*|конченн?\w*|хуесос\w*|заткнись|завали)\b"
    r"|на\s*хуй|нахуй|в\s*жоп|в\s*пизд",
    re.IGNORECASE,
)
_last_defense: dict[tuple[int, int], float] = {}
DEFEND_COOLDOWN = 20  # сек на одного обидчика
FATHER_FALLBACK = ["{n}, на батю моего рот не разевай.", "{n}, ещё раз тронешь батю — прожарю так, что мама не узнает.",
                   "{n}, ты кого тут обзываешь? Сам-то кто, напомнить?"]


def _father_id(config) -> int:
    return int(os.getenv("BOT_FATHER_ID", "0") or 0) or (config.admin_ids[0] if config and config.admin_ids else 0)


def _father_pattern(username: str | None) -> re.Pattern:
    names = [r"капуст\w*", r"kapust\w*", re.escape(ai.FATHER_NAME.lower())]
    if username:
        names.append("@?" + re.escape(username.lower()))
    return re.compile(r"(?<!\w)(" + "|".join(names) + r")(?!\w)", re.IGNORECASE)


_father_username: str | None = None


async def _maybe_defend(message: Message, bot: Bot, db: Database, config) -> bool:
    """Если кто-то обзывает батю — сразу заступаемся. True, если ответили."""
    global _father_username
    user = message.from_user
    father = _father_id(config)
    if not father or user is None:
        return False
    if user.id == father:
        _father_username = user.username or _father_username  # запомним @ник бати
        return False
    text = message.text or ""
    if not _INSULT.search(text):
        return False
    reply_to_father = bool(message.reply_to_message and message.reply_to_message.from_user
                           and message.reply_to_message.from_user.id == father)
    names_father = bool(_father_pattern(_father_username).search(text))
    if not (reply_to_father or names_father):
        return False
    key = (message.chat.id, user.id)
    if time.time() - _last_defense.get(key, 0) < DEFEND_COOLDOWN:
        return True
    _last_defense[key] = time.time()

    await asyncio.sleep(random.uniform(1.0, 2.0))
    name = _name(message)
    answer = ""
    if ai.available():
        try:
            answer = await ai.defend_father(name, text, await db.get_dossier(message.chat.id, user.id),
                                            await db.style_sample(message.chat.id))
            ctx = _as_dicts(await db.recent_messages(message.chat.id, limit=8))
            answer = await ai.polish(answer, text, ctx)
        except Exception as e:
            logger.warning("defend failed: %s", e)
    if not answer:
        answer = random.choice(FATHER_FALLBACK).format(n=name)
    await _say(bot, message.chat.id, answer, reply_to=message.message_id)
    return True


_BOT_WORD = re.compile(r"\bбот(ик|яра|ина)?\b", re.IGNORECASE)

_last_auto: dict[int, float] = {}
_last_roast: dict[int, float] = {}
_last_week_cmd: dict[int, float] = {}


async def get_interval(db: Database) -> int:
    try:
        return int(await db.get_setting(INTERVAL_KEY, str(DEFAULT_INTERVAL)))
    except ValueError:
        return DEFAULT_INTERVAL


def interval_label(seconds: int) -> str:
    return next((label for sec, label, _ in INTERVALS if sec == seconds), f"{seconds} сек")


def _min_messages(seconds: int) -> int:
    return next((n for sec, _, n in INTERVALS if sec == seconds), 10)


async def _enabled(db: Database) -> bool:
    return (await db.get_setting(ENABLED_KEY, "1")) == "1"


def _name(message: Message) -> str:
    u = message.from_user
    return (u.first_name or u.username or "Аноним") if u else "Аноним"


def _as_dicts(rows: list[tuple]) -> list[dict]:
    return [{"id": r[0], "name": r[2], "text": r[3]} for r in rows]


def _bunker_active(chat_id: int) -> bool:
    try:
        from games.bunker.game import games
        g = games.get(chat_id)
        return bool(g and g.phase != "ended")
    except Exception:
        return False


_last_bot_msg: dict[int, float] = {}       # когда бот последний раз писал в чат
_answered: dict[int, set[int]] = {}        # на какие сообщения уже ответил
_pending: dict[int, dict] = {}             # ответ, который бот «печатает» прямо сейчас
REPLY_DELAY = (2.0, 4.0)                   # «читает» пару секунд, собирая все обращения
QUIET_AFTER_SPEAKING = 20                  # сам не встревает, если только что что-то сказал


async def _say(bot: Bot, chat_id: int, text: str, reply_to: int | None = None) -> None:
    kwargs = {}
    if reply_to:
        kwargs["reply_parameters"] = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
        answered = _answered.setdefault(chat_id, set())
        answered.add(reply_to)
        if len(answered) > 500:
            answered.clear()
    _last_bot_msg[chat_id] = time.time()
    await bot.send_message(chat_id, html.escape(text.strip())[:4000], **kwargs)


# ---------- команды ----------


@router.message(Command("forget_me"))
async def cmd_forget_me(message: Message, db: Database) -> None:
    if not message.from_user:
        return
    await db.forget_user(message.chat.id, message.from_user.id)
    await message.reply("🧹 Всё о тебе удалено. Больше не запоминаю и не трогаю. Вернуться: /remember_me")


@router.message(Command("remember_me"))
async def cmd_remember_me(message: Message, db: Database) -> None:
    if not message.from_user:
        return
    await db.unforget_user(message.chat.id, message.from_user.id)
    await message.reply("😈 С возвращением. Снова слежу за каждым твоим словом.")


@router.message(Command("roast"))
async def cmd_roast(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    if not await _enabled(db):
        await message.reply("Стёб-бот сейчас выключен.")
        return
    if not ai.available():
        await message.reply("Для прожарки нужен ключ ИИ.")
        return
    chat_id = message.chat.id
    now = time.time()
    if now - _last_roast.get(chat_id, 0) < ROAST_COOLDOWN:
        await message.reply("Остынь, я ещё предыдущего дожариваю.")
        return

    target_id, target_name = None, None
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        if u.is_bot:
            await message.reply("Себя не жарю. Я и так горяч.")
            return
        target_id, target_name = u.id, u.first_name or u.username
    elif command.args and command.args.strip().startswith("@"):
        found = await db.find_user(chat_id, command.args.strip().split()[0])
        if not found:
            await message.reply("Не знаю такого. Пусть сначала что-нибудь напишет в чат.")
            return
        target_id, target_name = found
    elif message.from_user:
        target_id, target_name = message.from_user.id, _name(message)

    if await db.is_optout(chat_id, target_id):
        await message.reply("Этот человек попросил его не трогать. Уважаю (нет, но правила есть правила).")
        return
    rows = await db.recent_messages(chat_id, limit=40, user_id=target_id)
    dossier = await db.get_dossier(chat_id, target_id)
    if len(rows) < 5 and not dossier:
        await message.reply(f"Про {html.escape(target_name or 'него')} я пока мало знаю. Пусть поболтает — потом поговорим.")
        return
    _last_roast[chat_id] = now
    try:
        await bot.send_chat_action(chat_id, "typing")
        text = await ai.roast_user(target_name or "Аноним", dossier, _as_dicts(rows))
    except Exception as e:
        logger.warning("roast failed: %s", e)
        await message.reply("Что-то я сегодня не в форме. Попробуй позже.")
        return
    await _say(bot, chat_id, f"🔥 Прожарка: {target_name}\n\n{text}", reply_to=message.message_id)


async def _week_digest(bot: Bot, db: Database, chat_id: int) -> bool:
    rows = await db.recent_messages(chat_id, limit=2000, since=int(time.time()) - 7 * 86400)
    by_user: dict[str, list[str]] = {}
    for _mid, _uid, name, text, _ts in rows:
        by_user.setdefault(name or "Аноним", []).append(text)
    active = sorted(by_user.items(), key=lambda kv: -len(kv[1]))[:10]
    if len(active) < 2:
        return False
    samples = {name: msgs[-25:] for name, msgs in active}
    text = await ai.weekly_digest(await db.chat_dossiers(chat_id), samples)
    await _say(bot, chat_id, f"📅 ИТОГИ НЕДЕЛИ\n\n{text}")
    return True


@router.message(Command("week"))
async def cmd_week(message: Message, bot: Bot, db: Database) -> None:
    if not await _enabled(db) or not ai.available():
        await message.reply("Стёб-бот сейчас выключен.")
        return
    now = time.time()
    if now - _last_week_cmd.get(message.chat.id, 0) < WEEK_COOLDOWN:
        await message.reply("Итоги недели уже подводили. Не чаще раза в час.")
        return
    _last_week_cmd[message.chat.id] = now
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        ok = await _week_digest(bot, db, message.chat.id)
    except Exception as e:
        logger.warning("week digest failed: %s", e)
        ok = False
    if not ok:
        await message.reply("Маловато вы общались на этой неделе, подводить нечего.")


# ---------- все сообщения группы ----------


@router.message(F.text & ~F.text.startswith("/"))
async def on_group_message(message: Message, bot: Bot, db: Database, config=None) -> None:
    user = message.from_user
    if user is None or user.is_bot or not message.text:
        return
    chat_id = message.chat.id
    if not await _enabled(db):
        return
    optout = await db.is_optout(chat_id, user.id)
    if not optout:
        await db.add_chat_message(chat_id, message.message_id, user.id, _name(message),
                                  user.username, message.text[:1000])

    # Защита бати: если кого-то обзывают — реагируем сразу, раньше всего остального
    if await _maybe_defend(message, bot, db, config):
        return

    # Обращаются ли к боту?
    text = message.text
    reply_to_bot = bool(message.reply_to_message and message.reply_to_message.from_user
                        and message.reply_to_message.from_user.id == bot.id)
    me = await bot.me()
    mentioned = bool(me.username and f"@{me.username.lower()}" in text.lower())
    addressed = reply_to_bot or mentioned or bool(_BOT_WORD.search(text))
    if not addressed:
        return

    rude = bool(_RUDE.search(text))
    # Простое упоминание слова «бот» без ответа и без грубости — отвечаем не всегда
    if not rude and not reply_to_bot and not mentioned and random.random() > 0.3:
        return

    # Как человек: пару секунд «читаем», собираем всё, что написали боту, и отвечаем ОДИН раз
    pending = _pending.get(chat_id)
    if pending:
        pending["items"].append((message, rude))
        return
    _pending[chat_id] = {"items": [(message, rude)]}
    try:
        await asyncio.sleep(random.uniform(*REPLY_DELAY))
        try:
            await bot.send_chat_action(chat_id, "typing")
        except Exception:
            pass
        items = _pending[chat_id]["items"]
        await _reply_once(bot, db, chat_id, items, config)
    finally:
        _pending.pop(chat_id, None)


async def _reply_once(bot: Bot, db: Database, chat_id: int, items: list[tuple[Message, bool]],
                      config=None) -> None:
    last_msg, _ = items[-1]
    who = last_msg.from_user
    name = _name(last_msg)
    texts = [m.text or "" for m, _ in items if m.from_user and who and m.from_user.id == who.id]
    text = "\n".join(texts) or (last_msg.text or "")
    rude = any(r for m, r in items if m.from_user and who and m.from_user.id == who.id)
    samples = await db.style_sample(chat_id)
    # «Отец» бота — первый админ из .env (или BOT_FATHER_ID)
    is_father = bool(who and who.id == _father_id(config))
    dossier = await db.get_dossier(chat_id, who.id) if who else ""

    if rude and _SEND_OFF.search(text):
        # Посылают — отвечаем зеркально, в лоб, а ИИ добавляет короткий подкол
        answer = random.choice(MIRRORS).format(n="бать" if is_father else name)
        if ai.available():
            try:
                jab = await ai.comeback(name, text, short=True, dossier=dossier, samples=samples, is_father=is_father)
                if jab:  # редактор проверяет ответ целиком, как одно сообщение
                    full = await ai.polish(f"{answer} {jab}", text,
                                           _as_dicts(await db.recent_messages(chat_id, limit=8)), max_words=20)
                    if full:
                        answer = full
            except Exception as e:
                logger.warning("comeback failed: %s", e)
        await _say(bot, chat_id, answer, reply_to=last_msg.message_id)
        return

    answer = ""
    if ai.available():
        try:
            if rude:
                answer = await ai.comeback(name, text, dossier=dossier, samples=samples, is_father=is_father)
            else:
                ctx = await db.recent_messages(chat_id, limit=15)
                answer = await ai.banter(name, text, _as_dicts(ctx), dossier=dossier, samples=samples, is_father=is_father)
            if answer:  # редактор: связно ли, к месту ли, коротко ли
                answer = await ai.polish(answer, text, _as_dicts(await db.recent_messages(chat_id, limit=8)))
        except Exception as e:
            logger.warning("bot reply failed: %s", e)
    if not answer and rude:
        answer = random.choice(COMEBACKS)
    if answer:
        await _say(bot, chat_id, answer, reply_to=last_msg.message_id)


# ---------- фоновая работа ----------


async def _auto_roast(bot: Bot, db: Database, chat_id: int, interval: int) -> None:
    now = time.time()
    last = _last_auto.get(chat_id)
    if last is None:  # после перезапуска не набрасываемся сразу
        _last_auto[chat_id] = now
        return
    if now - last < interval or _bunker_active(chat_id) or chat_id in _pending:
        return
    if now - _last_bot_msg.get(chat_id, 0) < max(QUIET_AFTER_SPEAKING, interval):
        return
    if await db.count_messages_since(chat_id, int(last)) < _min_messages(interval):
        return
    _last_auto[chat_id] = now
    # Контекст — последние сообщения, отвечать можно только на новые (с прошлой проверки)
    rows = await db.recent_messages(chat_id, limit=30)
    answered = _answered.get(chat_id, set())
    since = max(int(last), int(_last_bot_msg.get(chat_id, 0)))
    new_ids = {r[0] for r in rows if r[4] >= since and r[0] not in answered}
    if not new_ids:
        return
    names = {r[2] for r in rows}
    dossiers = {n: d for n, d in (await db.chat_dossiers(chat_id)).items() if n in names}
    result = await ai.roast_auto(_as_dicts(rows), dossiers, new_ids, await db.style_sample(chat_id))
    if chat_id in _pending or time.time() - _last_bot_msg.get(chat_id, 0) < QUIET_AFTER_SPEAKING:
        return  # пока думали, бот уже успел ответить кому-то — молчим
    if result["text"]:
        reply_to = result["reply_to"] if result["reply_to"] in new_ids else None
        trigger = next((r[3] for r in rows if r[0] == reply_to), rows[-1][3])
        text = await ai.polish(result["text"], trigger, _as_dicts(rows))
        if chat_id in _pending or time.time() - _last_bot_msg.get(chat_id, 0) < QUIET_AFTER_SPEAKING:
            return
        if text:
            await _say(bot, chat_id, text, reply_to=reply_to)


async def _heavy(bot: Bot, db: Database) -> None:
    """Раз в 10 минут: чистка старого, досье, итоги недели."""
    await db.purge_old_messages(int(time.time()) - KEEP_DAYS * 86400)
    for chat_id, user_id, name, old in await db.users_needing_dossier():
        msgs = await db.messages_since_dossier(chat_id, user_id)
        if not msgs:
            continue
        try:
            text = await ai.update_dossier(name or "Аноним", old, msgs)
            if text:
                await db.set_dossier(chat_id, user_id, name or "Аноним", text)
        except Exception as e:
            logger.warning("dossier update failed: %s", e)

    now_msk = datetime.now(MSK)
    if now_msk.weekday() == 6 and now_msk.hour >= 20:  # итоги недели — воскресенье после 20:00 МСК
        for chat_id in await db.active_chats(int(time.time()) - 7 * 86400):
            key = f"roast_week:{chat_id}"
            last = float(await db.get_setting(key, "0") or 0)
            if time.time() - last > 6 * 86400:
                await db.set_setting(key, str(time.time()))
                try:
                    await _week_digest(bot, db, chat_id)
                except Exception as e:
                    logger.warning("weekly digest failed in %s: %s", chat_id, e)


async def background(bot: Bot, db: Database) -> None:
    """Запускается из bot.py. Подколы — с частотой из /admin, тяжёлая работа — раз в 10 минут."""
    last_heavy = 0.0
    while True:
        interval = DEFAULT_INTERVAL
        try:
            interval = await get_interval(db)
            if await _enabled(db) and ai.available():
                if time.time() - last_heavy >= HEAVY_EVERY:
                    last_heavy = time.time()
                    await _heavy(bot, db)
                if interval > 0:
                    for chat_id in await db.active_chats(int(time.time()) - 86400):
                        try:
                            await _auto_roast(bot, db, chat_id, interval)
                        except Exception as e:
                            logger.warning("auto roast failed in %s: %s", chat_id, e)
        except Exception:
            logger.exception("roast background tick failed")
        # Проверяем чаще, чем интервал, но не реже раза в минуту
        await asyncio.sleep(min(60, max(5, interval // 2)) if interval > 0 else 60)


# ---------- импорт истории из выгрузки Telegram Desktop ----------

IMPORT_LIMIT = 2000


def _export_text(raw) -> str:
    """В выгрузке text — строка или список кусочков (строки и {"type":..,"text":..})."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        return "".join(p if isinstance(p, str) else str(p.get("text", "")) for p in raw)
    return ""


def parse_export(data: dict, limit: int = IMPORT_LIMIT) -> tuple[int, list[tuple]]:
    """Возвращает (chat_id, [(msg_id, user_id, name, text, ts)]) — последние limit сообщений."""
    raw_id = int(data.get("id", 0))
    chat_type = str(data.get("type", ""))
    chat_id = int(f"-100{raw_id}") if "supergroup" in chat_type or "channel" in chat_type else -raw_id
    rows = []
    for m in data.get("messages", []):
        if m.get("type") != "message":
            continue
        from_id = str(m.get("from_id", ""))
        if not from_id.startswith("user"):
            continue
        text = _export_text(m.get("text")).strip()
        if not text or text.startswith("/"):
            continue
        try:
            ts = int(m.get("date_unixtime") or 0)
            rows.append((int(m["id"]), int(from_id[4:]), str(m.get("from") or "Аноним"), text[:1000], ts))
        except (KeyError, ValueError):
            continue
    return chat_id, rows[-limit:]


async def import_history(db: Database, data: dict, progress=None) -> tuple[int, int, int, int]:
    """Загружает последние 2000 сообщений и сразу строит досье по ним.

    Возвращает (chat_id, загружено сообщений, людей, досье построено).
    """
    chat_id, rows = parse_export(data)
    optout = await db.optout_ids(chat_id)
    rows = [r for r in rows if r[1] not in optout]
    added = await db.import_messages(chat_id, rows)

    by_user: dict[int, list[tuple]] = {}
    for r in rows:
        by_user.setdefault(r[1], []).append(r)
    built = 0
    if ai.available():
        for i, (user_id, items) in enumerate(sorted(by_user.items(), key=lambda kv: -len(kv[1]))):
            if len(items) < 5:
                continue
            name = items[-1][2]
            if progress:
                await progress(f"🧠 Составляю досье: {name} ({i + 1}/{len(by_user)})…")
            old = await db.get_dossier(chat_id, user_id)
            try:
                text = await ai.update_dossier(name, old, [r[3] for r in items[-150:]])
                if text:
                    await db.set_dossier(chat_id, user_id, name, text)
                    built += 1
            except Exception as e:
                logger.warning("import dossier failed for %s: %s", name, e)
    return chat_id, len(rows), len(by_user), built
