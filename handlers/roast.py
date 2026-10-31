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
AUTO_MIN_MESSAGES = 20         # столько новых сообщений нужно, чтобы бот сам подколол
AUTO_COOLDOWN = 90 * 60        # не чаще раза в 1,5 часа на чат
AUTO_CHANCE = 0.5              # и то не всегда
LOOP_EVERY = 10 * 60           # как часто фоновая задача проверяет чаты
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
_BOT_WORD = re.compile(r"\bбот(ик|яра|ина)?\b", re.IGNORECASE)

_last_auto: dict[int, float] = {}
_last_roast: dict[int, float] = {}
_last_week_cmd: dict[int, float] = {}
_last_reply: dict[tuple[int, int], float] = {}


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


async def _say(bot: Bot, chat_id: int, text: str, reply_to: int | None = None) -> None:
    kwargs = {}
    if reply_to:
        kwargs["reply_parameters"] = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
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
async def on_group_message(message: Message, bot: Bot, db: Database) -> None:
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

    # Обращаются ли к боту?
    text = message.text
    reply_to_bot = bool(message.reply_to_message and message.reply_to_message.from_user
                        and message.reply_to_message.from_user.id == bot.id)
    me = await bot.me()
    mentioned = bool(me.username and f"@{me.username.lower()}" in text.lower())
    addressed = reply_to_bot or mentioned or bool(_BOT_WORD.search(text))
    if not addressed:
        return

    key = (chat_id, user.id)
    now = time.time()
    rude = bool(_RUDE.search(text))
    if now - _last_reply.get(key, 0) < (10 if rude else 30):
        return
    # Простое упоминание слова «бот» без ответа и без грубости — отвечаем не всегда
    if not rude and not reply_to_bot and not mentioned and random.random() > 0.3:
        return
    _last_reply[key] = now

    answer = ""
    if rude and _SEND_OFF.search(text):
        # Посылают — отвечаем зеркально, в лоб, а ИИ добавляет короткий подкол
        answer = random.choice(MIRRORS).format(n=_name(message))
        if ai.available():
            try:
                jab = await ai.comeback(_name(message), text, short=True)
                if jab:
                    answer = f"{answer}\n\n{jab}"
            except Exception as e:
                logger.warning("comeback failed: %s", e)
        await _say(bot, chat_id, answer, reply_to=message.message_id)
        return
    if ai.available():
        try:
            if rude:
                answer = await ai.comeback(_name(message), text)
            else:
                ctx = await db.recent_messages(chat_id, limit=12)
                answer = await ai.banter(_name(message), text, _as_dicts(ctx))
        except Exception as e:
            logger.warning("bot reply failed: %s", e)
    if not answer and rude:
        answer = random.choice(COMEBACKS)
    if answer:
        await _say(bot, chat_id, answer, reply_to=message.message_id)


# ---------- фоновая работа ----------


async def _auto_roast(bot: Bot, db: Database, chat_id: int) -> None:
    now = time.time()
    last = _last_auto.get(chat_id)
    if last is None:  # после перезапуска не набрасываемся сразу
        _last_auto[chat_id] = now
        return
    if now - last < AUTO_COOLDOWN or _bunker_active(chat_id):
        return
    if await db.count_messages_since(chat_id, int(last)) < AUTO_MIN_MESSAGES:
        return
    _last_auto[chat_id] = now
    if random.random() > AUTO_CHANCE:
        return
    rows = await db.recent_messages(chat_id, limit=40, since=int(last))
    if len(rows) < 5:
        return
    names = {r[2] for r in rows}
    dossiers = {n: d for n, d in (await db.chat_dossiers(chat_id)).items() if n in names}
    result = await ai.roast_auto(_as_dicts(rows), dossiers)
    if result["text"]:
        ids = {r[0] for r in rows}
        reply_to = result["reply_to"] if result["reply_to"] in ids else None
        await _say(bot, chat_id, result["text"], reply_to=reply_to)


async def _tick(bot: Bot, db: Database) -> None:
    if not await _enabled(db) or not ai.available():
        return
    await db.purge_old_messages(int(time.time()) - KEEP_DAYS * 86400)

    # Досье: сжимаем накопившееся
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

    for chat_id in await db.active_chats(int(time.time()) - 7 * 86400):
        # Спонтанный подкол
        try:
            await _auto_roast(bot, db, chat_id)
        except Exception as e:
            logger.warning("auto roast failed in %s: %s", chat_id, e)
        # Итоги недели — по воскресеньям после 20:00 МСК
        now_msk = datetime.now(MSK)
        if now_msk.weekday() == 6 and now_msk.hour >= 20:
            key = f"roast_week:{chat_id}"
            last = float(await db.get_setting(key, "0") or 0)
            if time.time() - last > 6 * 86400:
                await db.set_setting(key, str(time.time()))
                try:
                    await _week_digest(bot, db, chat_id)
                except Exception as e:
                    logger.warning("weekly digest failed in %s: %s", chat_id, e)


async def background(bot: Bot, db: Database) -> None:
    """Запускается из bot.py: раз в LOOP_EVERY сек обновляет досье, подкалывает, подводит итоги."""
    while True:
        try:
            await _tick(bot, db)
        except Exception:
            logger.exception("roast background tick failed")
        await asyncio.sleep(LOOP_EVERY)
