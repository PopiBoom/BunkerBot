"""Стёб-бот: запоминает сообщения в группе, ведёт досье на участников и подкалывает их.

Что умеет:
  * сохраняет текстовые сообщения групп (сырые — 30 дней), раз в сутки или после 30 новых
    сообщений человека сжимает их в короткое досье;
  * /roast @ник (или ответом на сообщение) — прожарка по досье и свежим сообщениям;
  * /week — итоги недели с номинациями; сам публикует их по воскресеньям вечером (МСК);
  * иногда сам подкалывает кого-то, когда в чате оживлённо;
  * отвечает, если к нему обращаются, и огрызается, если его посылают;
  * /forget_me — удалить всё о себе и не попадать под стёб, /remember_me — вернуться;
  * комментирует посты канала бати (Memento Mori), запоминает стикеры и гифки чата (каждый один
    раз описывает по картинке) и кидает их к месту;
  * пока в чате идёт «Бункер» — молчит (только запоминает).
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
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, ReactionTypeEmoji, ReplyParameters

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
    r"чушпан\w*|нищеб\w*|придур\w*|конченн?\w*|хуесос\w*|заткнись|завали|"
    r"пидар\w*|педик\w*|педр\w*|петух\w*|петушар\w*|еблан\w*|ебанат\w*|гандон\w*|гондон\w*|гей|"
    r"говн\w*|хуйл\w*|хуепл[её]т\w*|соси|сос[её]т|ебал\w*|выеб\w*|ебу|отсос\w*|аутист\w*|крыс[аы]|"
    r"терпил\w*|опущ\w*|чушк\w*|додик\w*|дрищ\w*|лузер\w*|неудачник\w*|бомж\w*|быдл\w*|"
    r"gay|fag\w*|loser|idiot|stupid|dumb)\b"
    r"|на\s*хуй|нахуй|в\s*жоп|в\s*пизд|в\s*рот",
    re.IGNORECASE,
)
# «Твой батя …» — когда говорят боту, речь о его отце
_FATHER_REF = re.compile(
    r"\b(тво(й|его|ему|им|ем)|у\s+тебя)\s+(бат\w*|от[её]?ц\w*|пап\w*|создател\w*|хозя\w*)"
    r"|\b(бат\w*|от[её]?ц\w*|пап\w*|создател\w*|хозя\w*)\s+(бота|пиздюк\w*)",
    re.IGNORECASE,
)
# Части фразы: «капустка красавчик, а петя лох» — оскорбление не про батю
_CLAUSES = re.compile(r"[,.!?;\n]+|\s+(?:а|но|зато|однако|хотя)\s+", re.IGNORECASE)
_last_defense: dict[tuple[int, int], float] = {}
DEFEND_COOLDOWN = 20  # сек на одного обидчика
FATHER_FALLBACK = ["{n}, на батю моего рот не разевай.", "{n}, ещё раз тронешь батю — прожарю так, что мама не узнает.",
                   "{n}, ты кого тут обзываешь? Сам-то кто, напомнить?"]


def _father_id(config) -> int:
    return int(os.getenv("BOT_FATHER_ID", "0") or 0) or (config.admin_ids[0] if config and config.admin_ids else 0)


def _father_pattern(username: str | None) -> re.Pattern:
    names = [r"капуст\w*", r"kapust\w*", r"капуш\w*", r"кап", re.escape(ai.FATHER_NAME.lower())]
    if username:
        names.append("@?" + re.escape(username.lower()))
    return re.compile(r"(?<!\w)(" + "|".join(names) + r")(?!\w)", re.IGNORECASE)


_father_username: str | None = None  # @ник бати; "" — искали, но не нашли


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
    if _father_username is None:  # после перезапуска вспоминаем @ник бати по памяти чата
        _father_username = await db.last_username(message.chat.id, father) or ""
    reply_to_father = bool(message.reply_to_message and message.reply_to_message.from_user
                           and message.reply_to_message.from_user.id == father)
    to_bot = bool(message.reply_to_message and message.reply_to_message.from_user
                  and message.reply_to_message.from_user.id == bot.id) or bool(_BOT_NAME.search(text))
    who = _father_pattern(_father_username or None)

    def about_father(part: str) -> bool:
        return bool(who.search(part) or (to_bot and _FATHER_REF.search(part)))

    if not (reply_to_father or about_father(text)):
        return False
    # Оскорбление и батя в одной части фразы — точно про него. Если в разных («капустка
    # красавчик, а петя лох») — уточняем у ИИ, чтобы не заступаться зря.
    sure = reply_to_father or any(about_father(part) and _INSULT.search(part) for part in _CLAUSES.split(text))
    if not sure and ai.available():
        try:
            if not await ai.is_father_insult(text):
                return False
        except Exception as e:
            logger.debug("father insult check failed: %s", e)
    _spawn(_react(bot, message.chat.id, message.message_id, "🖕", force=True))
    key = (message.chat.id, user.id)
    if time.time() - _last_defense.get(key, 0) < DEFEND_COOLDOWN:
        return True
    _last_defense[key] = time.time()

    await asyncio.sleep(random.uniform(1.0, 2.0))
    await _remember_lines(db, message.chat.id)
    name = _name(message)
    answer = ""
    if ai.available():
        try:
            answer = await ai.defend_father(name, text, await db.get_dossier(message.chat.id, user.id),
                                            await db.style_sample(message.chat.id))
            ctx = _as_dicts(await db.recent_messages(message.chat.id, limit=8))
            # author — обидчик: редактор должен оставить ответ ему, а не бате
            answer = await ai.polish(answer, text, ctx, author=name)
        except Exception as e:
            logger.warning("defend failed: %s", e)
    if not answer:
        answer = random.choice(FATHER_FALLBACK).format(n=name)
    await _say(bot, message.chat.id, answer, reply_to=message.message_id)
    return True


# Имя бота в чате: на него откликается всегда
_BOT_NAME = re.compile(r"(?<!\w)пиздю(к|ч|ш)\w*", re.IGNORECASE)
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


BOT_SELF = "🤖 ты (бот)"  # так собственные реплики бота подписаны в контексте для ИИ
_db_ref: list = []         # база для записи своих реплик (заполняется при первом сообщении)


# ---------- реакции ----------

REACT_KEY = "roast_react_rate"
REACT_OPTIONS = [(0.0, "выкл"), (0.05, "редко"), (0.15, "иногда"), (0.35, "часто")]
DEFAULT_REACT = 0.15
REACT_GAP = 4  # сек: не чаще одной реакции в чате за это время (кроме 🖕)
_react_last: dict[int, float] = {}
_react_bad: dict[int, set[str]] = {}  # реакции, запрещённые в этом чате
_bg: set[asyncio.Task] = set()


async def get_react_rate(db: Database) -> float:
    try:
        return float(await db.get_setting(REACT_KEY, str(DEFAULT_REACT)))
    except ValueError:
        return DEFAULT_REACT


def react_label(rate: float) -> str:
    return next((label for r, label in REACT_OPTIONS if abs(r - rate) < 1e-9), f"{int(rate * 100)}%")


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _bg.add(task)
    task.add_done_callback(_bg.discard)


async def _react(bot: Bot, chat_id: int, msg_id: int, emoji: str, force: bool = False) -> bool:
    if not emoji or emoji in _react_bad.get(chat_id, set()):
        return False
    if not force and time.time() - _react_last.get(chat_id, 0) < REACT_GAP:
        return False
    try:
        await bot.set_message_reaction(chat_id, msg_id, reaction=[ReactionTypeEmoji(emoji=emoji)])
        _react_last[chat_id] = time.time()
        return True
    except TelegramBadRequest as e:
        if "REACTION_INVALID" in str(e) or "reaction" in str(e).lower():
            _react_bad.setdefault(chat_id, set()).add(emoji)  # в этом чате такую нельзя
        logger.debug("reaction %s failed: %s", emoji, e)
    except Exception as e:
        logger.debug("reaction failed: %s", e)
    return False


async def _maybe_react(bot: Bot, db: Database, message: Message, is_father: bool) -> None:
    """Иногда ставит реакцию по смыслу (решает Haiku), бате — ❤️/🔥."""
    rate = await get_react_rate(db)
    if rate <= 0:
        return
    chat_id = message.chat.id
    if is_father:
        if random.random() < min(1.0, rate * 2):
            await _react(bot, chat_id, message.message_id, random.choice(["❤", "🔥", "👍", "💯"]))
        return
    if random.random() >= rate or not ai.available():
        return
    if time.time() - _react_last.get(chat_id, 0) < REACT_GAP:
        return
    try:
        emoji = await ai.pick_reaction(message.text or "", _name(message))
    except Exception as e:
        logger.debug("pick reaction failed: %s", e)
        return
    if emoji:
        await _react(bot, chat_id, message.message_id, emoji)


async def _remember_lines(db: Database, chat_id: int) -> None:
    """Подсовываем ИИ последние реплики бота, чтобы он не повторял одни и те же слова."""
    rows = await db.recent_messages(chat_id, limit=10, user_id=0)
    ai.recent_bot_lines.set([r[3] for r in rows])


async def _say(bot: Bot, chat_id: int, text: str, reply_to: int | None = None) -> None:
    kwargs = {}
    if reply_to:
        kwargs["reply_parameters"] = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
        answered = _answered.setdefault(chat_id, set())
        answered.add(reply_to)
        if len(answered) > 500:
            answered.clear()
    _last_bot_msg[chat_id] = time.time()
    sent = await bot.send_message(chat_id, html.escape(text.strip())[:4000], **kwargs)
    # Запоминаем свою реплику: иначе бот не понимает, когда его спрашивают про им же сказанное
    if _db_ref:
        try:
            await _db_ref[0].add_chat_message(chat_id, getattr(sent, "message_id", 0) or 0, 0,
                                              BOT_SELF, None, text.strip()[:1000])
        except Exception as e:
            logger.debug("save own message failed: %s", e)


# ---------- посты канала бати ----------

# Названия каналов бати через запятую (BOT_CHANNEL_NAMES в .env). Автопересылка из
# привязанного к чату канала считается постом бати всегда.
CHANNEL_NAMES = [n.strip().lower() for n in os.getenv("BOT_CHANNEL_NAMES", "Memento Mori").split(",") if n.strip()]
CHANNEL_USER_ID = -1          # так посты канала подписаны в памяти чата (не человек и не бот)
ALBUM_WAIT = 2.5              # сек: ждём, пока Telegram досыплет все фото альбома
_albums: dict[tuple[int, str], dict] = {}


def _is_father_channel_title(title: str | None) -> bool:
    t = (title or "").lower()
    return bool(t) and any(n in t for n in CHANNEL_NAMES)


def _forward_source(message: Message) -> tuple[str, int | None] | None:
    """Откуда переслано сообщение: (подпись источника, id канала/чата) или None, если не пересылка."""
    fo = message.forward_origin
    if fo is None:
        return None
    if fo.type == "channel":
        return f"канала «{fo.chat.title or 'без названия'}»", fo.chat.id
    if fo.type == "chat":
        return f"чата «{fo.sender_chat.title or 'без названия'}»", fo.sender_chat.id
    if fo.type == "user":
        return f"сообщения от {fo.sender_user.first_name or 'кого-то'}", None
    return f"сообщения от {getattr(fo, 'sender_user_name', None) or 'кого-то'}", None


def _forwarded_from_father_channel(message: Message) -> bool:
    fo = message.forward_origin
    return bool(fo is not None and fo.type == "channel" and _is_father_channel_title(fo.chat.title))


async def _album_leader(message: Message) -> tuple[bool, str, int]:
    """Альбом приходит пачкой отдельных сообщений. Первое ждёт остальные и отвечает за всех.

    Возвращает (это главное сообщение альбома, подпись альбома, сколько в нём фото/видео).
    Не альбом — (True, своя подпись, 1).
    """
    if not message.media_group_id:
        return True, message.caption or "", 1
    key = (message.chat.id, message.media_group_id)
    album = _albums.get(key)
    if album is not None:  # не первое — только досыпаем подпись и считаем
        album["count"] += 1
        if message.caption and not album["caption"]:
            album["caption"] = message.caption
        return False, "", 0
    _albums[key] = {"caption": message.caption or "", "count": 1}
    await asyncio.sleep(ALBUM_WAIT)
    album = _albums.pop(key, {"caption": message.caption or "", "count": 1})
    return True, album["caption"], album["count"]


def _media_kind(message: Message) -> str:
    return ("фото" if message.photo else "видео" if message.video or message.animation else
            "голосовое" if message.voice or message.video_note else "опрос" if message.poll else
            "файл" if message.document else "")


def _is_father_channel_post(message: Message) -> bool:
    sc = message.sender_chat
    if sc is None or sc.type != "channel":
        return False
    return bool(message.is_automatic_forward) or _is_father_channel_title(sc.title)


@router.message(_is_father_channel_post)
async def on_channel_post(message: Message, bot: Bot, db: Database) -> None:
    chat_id = message.chat.id
    if not _db_ref:
        _db_ref.append(db)
    leader, caption, count = await _album_leader(message)
    if not leader:  # альбом — комментируем один раз, на первое сообщение
        return
    channel = message.sender_chat.title or "канал бати"
    text = message.text or caption or ""
    media = _media_kind(message)
    if count > 1:
        media = f"альбом из {count} фото/видео"
    # Репост из чужого канала: автор поста не батя, батя только переслал
    src = _forward_source(message)
    repost_from = None
    if src and src[1] not in (None, message.sender_chat.id) and not _forwarded_from_father_channel(message):
        repost_from = src[0]
    if text or media:
        who = f"📢 {channel} (репост бати из {repost_from})" if repost_from else f"📢 {channel} (пост бати)"
        await db.add_chat_message(chat_id, message.message_id, CHANNEL_USER_ID, who,
                                  None, (f"[{media}] " if media else "") + text[:1000])
    if not await _enabled(db) or not ai.available() or _bunker_active(chat_id):
        return
    if not text and not media:
        return
    image = None
    try:
        if message.photo:
            sizes = sorted(message.photo, key=lambda p: p.width)
            photo = next((p for p in reversed(sizes) if p.width <= 1280), sizes[0])
            image = ((await bot.download(photo)).read(), "image/jpeg")
        else:  # у видео и гифок смотрим хотя бы превью
            thumb = getattr(message.video or message.animation or message.video_note, "thumbnail", None)
            if thumb is not None:
                data = (await bot.download(thumb)).read()
                if ai.image_mime(data):
                    image = (data, ai.image_mime(data))
                    media = f"{media} (ты видишь только его превью — первый кадр)"
        await asyncio.sleep(random.uniform(3.0, 6.0))  # «прочитал» пост
        await bot.send_chat_action(chat_id, "typing")
        await _remember_lines(db, chat_id)
        answer = await ai.channel_comment(channel, text, media, image, await db.style_sample(chat_id),
                                          repost_from=repost_from)
    except Exception as e:
        logger.warning("channel comment failed: %s", e)
        return
    if answer:
        await _say(bot, chat_id, answer, reply_to=message.message_id)


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
    if _bunker_active(message.chat.id):
        await message.reply("Идёт Бункер — прожарю после партии 😈")
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
    for _mid, uid, name, text, _ts in rows:
        if uid <= 0:  # свои реплики и посты канала не номинируем
            continue
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
    if _bunker_active(message.chat.id):
        await message.reply("Идёт Бункер — итоги недели после партии.")
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
    if not _db_ref:
        _db_ref.append(db)
    if not await _enabled(db):
        return
    optout = await db.is_optout(chat_id, user.id)
    src = _forward_source(message)
    if not optout:
        note = f"[переслано из {src[0]}] " if src else ""
        await db.add_chat_message(chat_id, message.message_id, user.id, _name(message),
                                  user.username, (note + message.text)[:1000])
    if src:  # пересланный текст — чужие слова: не обещание, не грубость боту и не наезд на батю
        if not _bunker_active(chat_id):
            _spawn(_maybe_react(bot, db, message, False))
        return

    # Обещание с датой? Запомним и в срок напомним (в фоне, ответ не задерживает)
    if not optout and _PROMISE_HINT.search(message.text):
        _spawn(_check_promise(db, message))

    # Пока идёт Бункер — бот только запоминает, не встревает и не ставит реакции
    if _bunker_active(chat_id):
        return

    # Защита бати: если кого-то обзывают — реагируем сразу, раньше всего остального
    if await _maybe_defend(message, bot, db, config):
        return

    # Обращаются ли к боту?
    text = message.text
    reply_to_bot = bool(message.reply_to_message and message.reply_to_message.from_user
                        and message.reply_to_message.from_user.id == bot.id)
    me = await bot.me()
    mentioned = bool(me.username and f"@{me.username.lower()}" in text.lower())
    by_name = bool(_BOT_NAME.search(text))
    addressed = reply_to_bot or mentioned or by_name or bool(_BOT_WORD.search(text))
    if not addressed:
        _spawn(_maybe_react(bot, db, message, user.id == _father_id(config)))
        return
    if _RUDE.search(text):
        _spawn(_react(bot, chat_id, message.message_id, "🖕", force=True))

    rude = bool(_RUDE.search(text))
    # Простое упоминание слова «бот» без ответа и без грубости — отвечаем не всегда
    if not rude and not reply_to_bot and not mentioned and not by_name and random.random() > 0.3:
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


_thread: dict[tuple[int, int], list[float]] = {}  # когда бот отвечал этому человеку
THREAD_WINDOW = 180                                # перепалка — ответы за последние 3 минуты
_EMPTY = re.compile(r"^\W*(да|нет|ага|угу|и\s*ч[её]|ч[её]|ну\s*и\s*\w+|знаю|ок|окей|пофиг|похуй|ладно|"
                    r"мм+|хм+|ха(ха)*|ахах\w*|лол|кек|\+|\)+|не\s*буду|буду|пойду|делай|сам\s*такой)\W*$",
                    re.IGNORECASE)
COOL_REACTIONS = ["🗿", "🤡", "🥱", "🙈", "😴"]


def _thread_load(chat_id: int, user_id: int) -> int:
    now = time.time()
    hist = [t for t in _thread.get((chat_id, user_id), []) if now - t < THREAD_WINDOW]
    _thread[(chat_id, user_id)] = hist
    return len(hist)


async def _reply_once(bot: Bot, db: Database, chat_id: int, items: list[tuple[Message, bool]],
                      config=None) -> None:
    last_msg, _ = items[-1]
    who = last_msg.from_user
    name = _name(last_msg)
    texts = [m.text or "" for m, _ in items if m.from_user and who and m.from_user.id == who.id]
    text = "\n".join(texts) or (last_msg.text or "")
    rt = last_msg.reply_to_message
    if rt and rt.from_user and rt.from_user.id == bot.id and rt.text:
        text = f"(отвечает на твою реплику «{rt.text[:300]}»)\n{text}"
    rude = any(r for m, r in items if m.from_user and who and m.from_user.id == who.id)
    samples = await db.style_sample(chat_id)
    await _remember_lines(db, chat_id)
    # «Отец» бота — первый админ из .env (или BOT_FATHER_ID)
    is_father = bool(who and who.id == _father_id(config))

    # Как человек: в затянувшейся перепалке отвечает всё реже, на пустое «да/и че» — реакцией
    if who and not is_father:
        n = _thread_load(chat_id, who.id)
        empty = bool(_EMPTY.match(last_msg.text or ""))
        chance = 1.0 if n < 2 else {2: 0.5, 3: 0.3}.get(n, 0.15)
        if empty and n >= 1:
            chance = min(chance, 0.3)
        if random.random() > chance:
            if random.random() < STICKER_INSTEAD or not await _send_sticker(bot, db, chat_id, text, last_msg.message_id):
                await _react(bot, chat_id, last_msg.message_id,
                             "🖕" if rude else random.choice(COOL_REACTIONS), force=True)
            return
    dossier = await db.get_dossier(chat_id, who.id) if who else ""

    if rude and _SEND_OFF.search(text):
        # Посылают — отвечаем зеркально, в лоб, а ИИ добавляет короткий подкол
        answer = random.choice(MIRRORS).format(n="бать" if is_father else name)
        if ai.available():
            try:
                jab = await ai.comeback(name, text, short=True, dossier=dossier, samples=samples, is_father=is_father)
                if jab:  # редактор проверяет ответ целиком, как одно сообщение
                    full = await ai.polish(f"{answer} {jab}", text,
                                           _as_dicts(await db.recent_messages(chat_id, limit=8)), max_words=20,
                                           author=ai.FATHER_NAME if is_father else name)
                    if full:
                        answer = full
            except Exception as e:
                logger.warning("comeback failed: %s", e)
        await _say(bot, chat_id, answer, reply_to=last_msg.message_id)
        if who:
            _thread.setdefault((chat_id, who.id), []).append(time.time())
        return

    answer = ""
    if ai.available():
        try:
            if rude:
                answer = await ai.comeback(name, text, dossier=dossier, samples=samples, is_father=is_father)
            else:
                ctx = await db.recent_messages(chat_id, limit=25)
                answer = await ai.banter(name, text, _as_dicts(ctx), dossier=dossier, samples=samples, is_father=is_father)
            if answer:  # редактор: связно ли, к месту ли, коротко ли
                answer = await ai.polish(answer, text, _as_dicts(await db.recent_messages(chat_id, limit=20)),
                                         author=ai.FATHER_NAME if is_father else name)
        except Exception as e:
            logger.warning("bot reply failed: %s", e)
    if not answer and rude:
        answer = random.choice(COMEBACKS)
    if answer:
        await _say(bot, chat_id, answer, reply_to=last_msg.message_id)
        if who:
            _thread.setdefault((chat_id, who.id), []).append(time.time())
        if random.random() < STICKER_AFTER:
            await asyncio.sleep(random.uniform(1.0, 2.5))
            await _send_sticker(bot, db, chat_id, f"{text}\nОтвет бота: {answer}")


# ---------- стикеры и гифки ----------

STICKER_AFTER = 0.2     # иногда после ответа бот докидывает стикер
STICKER_INSTEAD = 0.5   # когда лень отвечать словами: доля случаев, где хватит реакции, а не стикера
STICKER_REPLY = 0.7     # на стикер в ответ боту — стикером
STICKER_RANDOM = 0.04   # на чужой стикер — иногда своим
MIN_STICKERS = 3        # пока чат не накидал хотя бы столько разных стикеров/гифок — не отвечаем ими
_sticker_last: dict[int, float] = {}
STICKER_GAP = 20        # сек между стикерами бота в одном чате


_describing: set[str] = set()  # какие файлы уже пробовали описать в этом запуске


async def _sticker_descr(bot: Bot, db: Database, unique_id: str, known: str | None, image_src,
                         kind: str, emoji: str = "") -> str:
    """Описание стикера/гифки: из базы, а если его нет — Haiku смотрит на картинку один раз."""
    if known or not ai.available() or image_src is None or unique_id in _describing:
        return known or ""
    _describing.add(unique_id)
    try:
        data = (await bot.download(image_src)).read()
        descr = await ai.describe_sticker(data, kind, emoji)
    except Exception as e:
        logger.debug("describe %s failed: %s", kind, e)
        return ""
    if descr:
        await db.set_sticker_descr(unique_id, descr)
        logger.info("%s описан: %s", kind, descr)
    return descr


async def _send_sticker(bot: Bot, db: Database, chat_id: int, situation: str,
                        reply_to: int | None = None) -> bool:
    """Кидает подходящий по смыслу стикер или гифку из тех, что кидали в этом чате. True — кинул."""
    if not ai.available() or _bunker_active(chat_id):
        return False
    if time.time() - _sticker_last.get(chat_id, 0) < STICKER_GAP:
        return False
    rows = await db.chat_stickers(chat_id)  # (file_id, emoji, set_name, uses, descr, kind)
    if len(rows) < MIN_STICKERS:
        return False
    try:
        n = await ai.pick_sticker(situation, [("гифка" if r[5] == "gif" else "стикер", r[4] or "", r[1] or "",
                                               r[2] or "") for r in rows])
    except Exception as e:
        logger.debug("pick sticker failed: %s", e)
        return False
    if not n:
        return False
    file_id, emoji, _set, _uses, descr, kind = rows[n - 1]
    kwargs = {}
    if reply_to:
        kwargs["reply_parameters"] = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
    try:
        if kind == "gif":
            await bot.send_animation(chat_id, file_id, **kwargs)
        else:
            await bot.send_sticker(chat_id, file_id, **kwargs)
    except Exception as e:
        logger.debug("send sticker failed: %s", e)
        return False
    _sticker_last[chat_id] = time.time()
    _last_bot_msg[chat_id] = time.time()
    if _db_ref:
        label = "гифка" if kind == "gif" else "стикер"
        try:
            await _db_ref[0].add_chat_message(chat_id, 0, 0, BOT_SELF, None,
                                              f"[{label}: {descr or emoji or '?'}]")
        except Exception:
            pass
    return True


async def _maybe_answer_sticker(bot: Bot, db: Database, message: Message) -> None:
    """На стикер/гифку в ответ боту — чаще всего стикером, на чужие — изредка."""
    if _bunker_active(message.chat.id):
        return
    reply_to_bot = bool(message.reply_to_message and message.reply_to_message.from_user
                        and message.reply_to_message.from_user.id == bot.id)
    if random.random() < (STICKER_REPLY if reply_to_bot else STICKER_RANDOM):
        ctx = _as_dicts(await db.recent_messages(message.chat.id, limit=6))
        situation = "\n".join(f"{m['name']}: {m['text']}" for m in ctx)
        await asyncio.sleep(random.uniform(1.0, 3.0))
        await _send_sticker(bot, db, message.chat.id, situation, reply_to=message.message_id)


@router.message(F.sticker)
async def on_group_sticker(message: Message, bot: Bot, db: Database) -> None:
    user, st = message.from_user, message.sticker
    if user is None or user.is_bot or st is None:
        return
    chat_id = message.chat.id
    if not _db_ref:
        _db_ref.append(db)
    if not await _enabled(db) or await db.is_optout(chat_id, user.id):
        return
    known = await db.add_sticker(chat_id, st.file_unique_id, st.file_id, st.emoji, st.set_name, "sticker")
    # обычный стикер — смотрим сам файл (webp); анимированный и видео — их превью
    src = st.thumbnail if (st.is_animated or st.is_video) else st
    descr = await _sticker_descr(bot, db, st.file_unique_id, known, src, "стикер", st.emoji or "")
    await db.add_chat_message(chat_id, message.message_id, user.id, _name(message), user.username,
                              f"[стикер: {descr or st.emoji or '?'}]")
    await _maybe_answer_sticker(bot, db, message)


@router.message(F.animation)
async def on_group_gif(message: Message, bot: Bot, db: Database) -> None:
    user, gif = message.from_user, message.animation
    if user is None or user.is_bot or gif is None:
        return
    chat_id = message.chat.id
    if not _db_ref:
        _db_ref.append(db)
    if not await _enabled(db) or await db.is_optout(chat_id, user.id):
        return
    known = await db.add_sticker(chat_id, gif.file_unique_id, gif.file_id, None, None, "gif")
    descr = await _sticker_descr(bot, db, gif.file_unique_id, known, gif.thumbnail, "гифка")
    caption = f" {message.caption}" if message.caption else ""
    await db.add_chat_message(chat_id, message.message_id, user.id, _name(message), user.username,
                              f"[гифка: {descr or '?'}]{caption}"[:1000])
    await _maybe_answer_sticker(bot, db, message)

# ---------- ловушка на обещания ----------

_PROMISE_HINT = re.compile(
    r"с\s+понедельника|с\s+завтра\w*|завтра|послезавтра|на\s+(этой|следующей)\s+неделе|через\s+(неделю|месяц|\d+)|"
    r"до\s+(нового\s+года|конца|понедельника|пятницы|лета|выходных)|обещаю|клянусь|зуб\s+даю|больше\s+(не|никогда)|"
    r"брошу|бросаю|начну|начинаю|сегодня\s+же|к\s+(лету|понедельнику|пятнице|выходным)|"
    r"в\s+(понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)",
    re.IGNORECASE,
)
_promise_last: dict[tuple[int, int], float] = {}


async def _check_promise(db: Database, message: Message) -> None:
    user = message.from_user
    if not user or not ai.available():
        return
    key = (message.chat.id, user.id)
    if time.time() - _promise_last.get(key, 0) < 60:  # не дёргаем ИИ на каждое сообщение подряд
        return
    _promise_last[key] = time.time()
    now = datetime.now(MSK)
    try:
        data = await ai.extract_promise(message.text or "", _name(message), now.strftime("%Y-%m-%d %H:%M, %A"))
    except Exception as e:
        logger.debug("promise extract failed: %s", e)
        return
    if not data.get("promise") or not data.get("due") or not data.get("what"):
        return
    try:
        hh, mm = (str(data.get("time") or "12:00")[:5]).split(":")
        due = datetime.strptime(str(data["due"])[:10], "%Y-%m-%d").replace(
            hour=int(hh), minute=int(mm), tzinfo=MSK)
    except (ValueError, TypeError):
        return
    due_ts = int(due.timestamp())
    if due_ts < time.time() + 600 or due_ts > time.time() + 60 * 86400:
        return
    what = str(data["what"])[:120]
    if await db.has_similar_promise(message.chat.id, user.id, what):
        return
    await db.add_promise(message.chat.id, user.id, _name(message), message.message_id,
                         (message.text or "")[:500], what, due_ts)
    logger.info("promise saved: %s — %s до %s", _name(message), what, due.isoformat())


async def _remind_promises(bot: Bot, db: Database) -> None:
    for pid, chat_id, user_id, name, msg_id, text, what, _due in await db.due_promises(int(time.time()), limit=20):
        if _bunker_active(chat_id):  # напомним после партии
            continue
        await db.mark_promise(pid)
        try:
            await _remember_lines(db, chat_id)
            answer = await ai.promise_reminder(name or "Аноним", what, text or "",
                                               await db.get_dossier(chat_id, user_id),
                                               await db.style_sample(chat_id))
        except Exception as e:
            logger.warning("promise reminder failed: %s", e)
            answer = ""
        if not answer:
            answer = f"{name}, ну что, «{what}»? Срок вышел. Рассказывай, как слился 🤡"
        await _say(bot, chat_id, answer, reply_to=msg_id)


@router.message(Command("promises"))
async def cmd_promises(message: Message, db: Database) -> None:
    rows = await db.open_promises(message.chat.id)
    if not rows:
        await message.reply("Никто ничего не обещал. Или я пока не слышал 👀")
        return
    lines = ["📝 <b>Обещания, которые я слежу</b>", "━━━━━━━━━━━━━━━", ""]
    for name, what, due_ts in rows[:20]:
        due = datetime.fromtimestamp(due_ts, MSK).strftime("%d.%m %H:%M")
        lines.append(f"• <b>{html.escape(name or '?')}</b> — {html.escape(what)} <i>(проверю {due})</i>")
    await message.reply("\n".join(lines))


# ---------- стёб по фото ----------

PHOTO_CHANCE = 0.35  # на фото без обращения к боту отвечает примерно на каждое третье


@router.message(F.photo)
async def on_group_photo(message: Message, bot: Bot, db: Database, config=None) -> None:
    user = message.from_user
    if user is None or user.is_bot or not message.photo:
        return
    chat_id = message.chat.id
    if not _db_ref:
        _db_ref.append(db)
    if not await _enabled(db) or await db.is_optout(chat_id, user.id):
        return
    leader, caption, count = await _album_leader(message)
    if not leader:  # альбом: запоминаем и отвечаем один раз, на первое фото
        return
    # Переслано из чужого канала/чата — это не слова и не фото самого человека
    src = None if _forwarded_from_father_channel(message) else _forward_source(message)
    what = f"[альбом, {count} фото]" if count > 1 else "[фото]"
    note = f"[переслано из {src[0]}] " if src else ""
    await db.add_chat_message(chat_id, message.message_id, user.id, _name(message), user.username,
                              f"{note}{what} {caption}".strip()[:1000])
    if not ai.available() or _bunker_active(chat_id):
        return
    reply_to_bot = bool(message.reply_to_message and message.reply_to_message.from_user
                        and message.reply_to_message.from_user.id == bot.id)
    addressed = reply_to_bot or bool(_BOT_NAME.search(caption) or _BOT_WORD.search(caption))
    if not addressed:
        if random.random() > PHOTO_CHANCE or time.time() - _last_bot_msg.get(chat_id, 0) < QUIET_AFTER_SPEAKING:
            return
    is_father = user.id == _father_id(config)
    # берём не самый большой размер — модели хватит, а качать быстрее
    sizes = sorted(message.photo, key=lambda p: p.width)
    photo = next((p for p in reversed(sizes) if p.width <= 1280), sizes[0])
    try:
        await bot.send_chat_action(chat_id, "typing")
        img = (await bot.download(photo)).read()
        await _remember_lines(db, chat_id)
        draft = await ai.roast_photo(img, "image/jpeg", _name(message), caption, is_father,
                                     await db.style_sample(chat_id), forwarded_from=src[0] if src else None,
                                     count=count, father_channel=_forwarded_from_father_channel(message))
        answer = await ai.polish(draft, caption or "[фото]", _as_dicts(await db.recent_messages(chat_id, limit=10)),
                                 author=ai.FATHER_NAME if is_father else _name(message), about_photo=True,
                                 image=(img, "image/jpeg"))  # редактор видит то же фото
    except Exception as e:
        logger.warning("photo roast failed: %s", e)
        return
    if answer:
        await _say(bot, chat_id, answer, reply_to=message.message_id)


# ---------- «Клоун дня» ----------

CLOWN_HOUR = 21  # МСК: в это время (и позже) бот выбирает клоуна дня


async def _clown_of_day(bot: Bot, db: Database, chat_id: int) -> bool:
    day_start = int(datetime.now(MSK).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    rows = await db.recent_messages(chat_id, limit=1500, since=day_start)
    by_user: dict[int, tuple[str, list[str]]] = {}
    for _mid, uid, name, text, _ts in rows:
        if uid <= 0:  # бот и посты канала
            continue
        by_user.setdefault(uid, (name or "Аноним", []))[1].append(text)
    active = {uid: v for uid, v in by_user.items() if len(v[1]) >= 3}
    if len(active) < 2:
        return False
    # У ИИ каждое имя должно быть уникальным: двух «Саш» различаем по @нику или номеру
    label_of: dict[int, str] = {}
    counts: dict[str, int] = {}
    for _uid, (name, _m) in active.items():
        counts[name] = counts.get(name, 0) + 1
    seen: dict[str, int] = {}
    for uid, (name, _m) in active.items():
        if counts[name] == 1:
            label_of[uid] = name
            continue
        uname = await db.last_username(chat_id, uid)
        seen[name] = seen.get(name, 0) + 1
        label = f"{name} (@{uname})" if uname else f"{name} #{seen[name]}"
        while label in label_of.values():
            label += "'"
        label_of[uid] = label
    samples = {label_of[uid]: msgs[-25:] for uid, (_n, msgs) in active.items()}
    dossiers = {label_of[uid]: await db.get_dossier(chat_id, uid) for uid in active}
    result = await ai.clown_of_day(samples, dossiers)
    winner = next((uid for uid, label in label_of.items() if label == result["name"]), None)
    if winner is None or not result["reason"]:
        return False
    await db.add_clown(chat_id, winner, active[winner][0])
    total = next((c for uid, c in await db.clown_counts(chat_id) if uid == winner), 1)
    await _say(bot, chat_id, f"🤡 КЛОУН ДНЯ — {label_of[winner]}\n\n{result['reason']}\n\n"
                             f"Клоун уже {total}-й раз. Таблица позора: /clowns")
    return True


@router.message(Command("clowns"))
async def cmd_clowns(message: Message, db: Database) -> None:
    rows = await db.clown_top(message.chat.id)
    if not rows:
        await message.reply("Клоунов ещё не выбирали. Первый — сегодня в 21:00 🤡")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🤡 <b>ТАБЛИЦА ПОЗОРА</b>", "━━━━━━━━━━━━━━━", ""]
    for i, (name, count) in enumerate(rows):
        place = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{place} <b>{html.escape(name or '?')}</b> — клоун дня ×{count}")
    await message.reply("\n".join(lines))


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
    # Отвечать можно только на живой текст: на «[фото]», «[стикер…]», «[переслано…]» здесь бот
    # не видит самого содержимого и ответил бы мимо — по прошлым сообщениям. Они — только фон.
    new_ids = {r[0] for r in rows if r[4] >= since and r[0] not in answered and r[1] > 0
               and not str(r[3]).startswith("[")}
    if not new_ids:
        return
    await _remember_lines(db, chat_id)
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
    if now_msk.hour >= CLOWN_HOUR:  # клоун дня — раз в сутки, после 21:00 МСК
        today = now_msk.strftime("%Y-%m-%d")
        for chat_id in await db.active_chats(int(time.time()) - 86400):
            key = f"roast_clown:{chat_id}"
            if await db.get_setting(key, "") == today or _bunker_active(chat_id):
                continue
            await db.set_setting(key, today)
            try:
                await _clown_of_day(bot, db, chat_id)
            except Exception as e:
                logger.warning("clown of day failed in %s: %s", chat_id, e)
    if now_msk.weekday() == 6 and now_msk.hour >= 20:  # итоги недели — воскресенье после 20:00 МСК
        for chat_id in await db.active_chats(int(time.time()) - 7 * 86400):
            key = f"roast_week:{chat_id}"
            last = float(await db.get_setting(key, "0") or 0)
            if time.time() - last > 6 * 86400 and not _bunker_active(chat_id):
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
                try:
                    await _remind_promises(bot, db)
                except Exception as e:
                    logger.warning("promise reminders failed: %s", e)
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

    Возвращает (chat_id, новых сообщений (без дублей), людей, досье построено).
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
    return chat_id, added, len(by_user), built
