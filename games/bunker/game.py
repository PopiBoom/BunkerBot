"""Игра «Бункер» для группового чата. Ведущий — Claude.

Поток партии (вся партия — одна фоновая задача `_run_game`, кнопки лишь будят её):
  /bunker → лобби (вход только через ЛС бота, выбор режима ведущего) → старт →
  ИИ генерирует катастрофу, бункер, факты о бункере и карточки → каждому в ЛС
  приходит карточка-картинка и карта действия → закреплённое «табло» →
  раунды: новый факт о бункере, игроки по очереди открывают характеристику (таймер
  в сообщении), одна реплика ведущего на раунд → голосование (в маленьких партиях первые
  раунды — разогрев без голосования; полоски; при ничьей лидеров — их последнее слово, и если
  ничья осталась — переголосование только между ними) → изгнание с картинкой «ОТКАЗАНО» →
  финал: эпилог, вердикт «выжила ли колония», альбом выживших, рейтинг.

Состояние игр хранится в памяти процесса (по одному на чат).
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ai
from database import Database
from ephemeral import delete_later, temp_reply

from ..common import lobby_text
from . import content as C
from . import render as R
from .render import render_card

logger = logging.getLogger("bunker")
router = Router(name="bunker")

MIN_PLAYERS = 2
MAX_PLAYERS = 16
TURN_TIMEOUT = 60         # сек на ход каждого игрока
REVOTE_TIMEOUT = 30       # сек на переголосование при ничьей (закрывается раньше, если все проголосовали)
MAX_REVOTES = 2           # сколько раз переголосовываем, потом — жребий
SPEECH_AT = 45            # через сколько сек после начала голосования — последнее слово
SPEECH_LEN = 30           # сек на оправдания
LOBBY_TIMEOUT = 15 * 60   # сек: брошенное лобби закрывается само
MAX_TEXT = 3800           # запас до лимита Telegram в 4096 символов
MAX_CAPTION = 1000        # лимит подписи к фото — 1024
STEP_DELAY = float(os.getenv("BUNKER_DELAY", "20") or 20)  # пауза после сюжетных сообщений, чтобы все дочитали
REVEAL_DELAY = 4          # сек: пауза после открытия одной характеристики
HOST_DELAY = STEP_DELAY * 1.5         # пауза после реплики ведущего — её надо дочитать
SPEECH_NOTE_DELAY = REVEAL_DELAY * 1.5  # пауза после оценки речей перед итогом
THREAT_VOTE = 90          # сек на голосование «кого отправить устранять угрозу»
THREAT_MIN_PLAYERS = 4    # угроза бывает в партиях от стольких игроков
REFRESH_DELAY = 1.2       # сек: склеиваем частые правки табло/голосования в одну


def warmup_rounds(players: int) -> int:
    """Сколько первых раундов без голосования: в маленькой партии иначе выгоняют вслепую."""
    if players <= 4:
        return 2
    if players <= 8:
        return 1
    return 0

HR = "━━━━━━━━━━━━━━━"


# ---------- состояние ----------


@dataclass
class Player:
    user_id: int
    name: str
    username: str | None = None
    card: dict | None = None
    alive: bool = True
    revealed: set[str] = field(default_factory=set)
    dm_ok: bool = False
    pick_msg_id: int | None = None
    action: str | None = None
    action_used: bool = False
    serial: int = 0
    out_round: int | None = None
    died: bool = False  # погиб на угрозе, а не изгнан голосованием
    is_bot: bool = False  # тестовый бот (/bunker_bots): ходит и голосует сам


@dataclass
class Game:
    chat_id: int
    host_id: int
    db: Database | None = None
    mode: str = "classic"
    players: dict[int, Player] = field(default_factory=dict)
    phase: str = "lobby"  # lobby | revealing | voting | counting | ended
    catastrophe: str = ""
    bunker: str = ""
    bunker_secrets: list[str] = field(default_factory=list)
    bunker_known: list[str] = field(default_factory=list)
    spots: int = 1
    round_no: int = 0
    revealed_this_round: set[int] = field(default_factory=set)
    round_reveals: list[dict] = field(default_factory=list)
    # голосование
    votes: dict[int, int] = field(default_factory=dict)            # кто голосует -> против кого
    vote_targets: set[int] = field(default_factory=set)  # за кого можно голосовать (при ничьей — только равные)
    revote: int = 0                                        # номер переголосования (0 — основное)
    immune: set[int] = field(default_factory=set)        # защищены в ИДУЩЕМ голосовании
    immune_next: set[int] = field(default_factory=set)   # сыграли иммунитет — защищены в СЛЕДУЮЩЕМ
    doubled: set[int] = field(default_factory=set)
    cursed: set[int] = field(default_factory=set)
    stage: str = ""        # open | speech | after — стадии голосования
    speakers: dict[int, list[str]] = field(default_factory=dict)
    advance: asyncio.Event | None = None
    force_tally: bool = False
    # сообщения
    lobby_msg_id: int | None = None
    lobby_photo: bool = False  # лобби отправлено картинкой (правим подпись)
    board_msg_id: int | None = None
    vote_msg_id: int | None = None
    turn_msg_id: int | None = None
    bot_username: str = ""
    warmup: int = 0        # первые раунды без голосования
    outsider_warned: bool = False  # уже сказали посторонним, что во время игры пишут только игроки
    cant_delete: bool = False      # у бота нет права удалять сообщения
    current_picker: int | None = None
    pick_event: asyncio.Event | None = None
    pending: set[str] = field(default_factory=set)
    edit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # правки голосований — строго по очереди
    first_out: int | None = None
    round_votes: list[dict] = field(default_factory=list)  # по раундам: {"main": {кто: против кого}, "final": …, "out": id}
    heroes: set[int] = field(default_factory=set)          # кто справился с угрозой
    serial: int = 0
    title: str = ""
    # угроза в середине игры
    threat: dict = field(default_factory=dict)
    threat_round: int = 0
    threat_votes: dict[int, int] = field(default_factory=dict)
    threat_chances: dict[int, dict] = field(default_factory=dict)
    threat_msg_id: int | None = None
    hero: int | None = None


games: dict[int, Game] = {}  # chat_id -> Game
_tasks: set[asyncio.Task] = set()  # держим ссылки, чтобы задачи не собрал GC


def _alive(game: Game) -> list[Player]:
    return [p for p in game.players.values() if p.alive]


def _is_active(game: Game) -> bool:
    return games.get(game.chat_id) is game and game.phase != "ended"


def _style(game: Game) -> str:
    return C.MODES.get(game.mode, C.MODES["classic"])[1]


def _mode_title(game: Game) -> str:
    return C.MODES.get(game.mode, C.MODES["classic"])[0]


def _end_game(game: Game) -> None:
    game.phase = "ended"
    if games.get(game.chat_id) is game:
        games.pop(game.chat_id, None)
    for ev in (game.pick_event, game.advance):
        if ev:
            ev.set()  # будим ожидания, чтобы партия сразу завершилась


def _launch(bot: Bot, game: Game, coro, fatal: bool = True) -> None:
    """Запускает корутину отдельной задачей. Если упал ход игры — сообщаем в чат."""

    async def runner() -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("bunker task crashed in chat %s", game.chat_id)
            if fatal and _is_active(game):
                _end_game(game)
                try:
                    await _send(bot, game.chat_id,
                                "💥 Ведущий споткнулся о генератор… Игра прервана.\n"
                                "/bunker — начать заново.")
                except Exception:
                    pass

    task = asyncio.create_task(runner())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _schedule(bot: Bot, game: Game, key: str, fn) -> None:
    """Отложенное обновление: много вызовов подряд → одна правка сообщения."""
    if key in game.pending:
        return
    game.pending.add(key)

    async def run() -> None:
        await asyncio.sleep(REFRESH_DELAY)
        game.pending.discard(key)
        await fn()

    _launch(bot, game, run(), fatal=False)


def _unique_name(game: Game, raw: str | None) -> str:
    """Имя игрока, уникальное в пределах партии («Саша», «Саша 2», …)."""
    base = (raw or "").strip()[:32] or "Игрок"
    taken = {p.name for p in game.players.values()}
    if base not in taken:
        return base
    n = 2
    while f"{base} {n}" in taken:
        n += 1
    return f"{base} {n}"


# ---------- отправка с учётом флуд-лимита ----------


async def _retry(call, *args, **kwargs):
    attempts = 4
    for attempt in range(attempts):
        try:
            return await call(*args, **kwargs)
        except TelegramRetryAfter as e:
            if attempt == attempts - 1:
                raise
            logger.info("flood limit, sleeping %ss", e.retry_after)
            await asyncio.sleep(e.retry_after + 0.5)
    raise RuntimeError("unreachable")


async def _send(bot: Bot, chat_id: int, text: str, **kwargs) -> Message:
    return await _retry(bot.send_message, chat_id, text, **kwargs)


async def _say(bot: Bot, chat_id: int, text: str, delay: float = STEP_DELAY,
               reply_markup: InlineKeyboardMarkup | None = None) -> Message:
    msg = await _send(bot, chat_id, text, reply_markup=reply_markup)
    if delay > 0:
        try:
            await bot.send_chat_action(chat_id, "typing")
        except Exception:
            pass
        await asyncio.sleep(delay)
    return msg


async def _edit(bot: Bot, chat_id: int, message_id: int | None, text: str,
                reply_markup: InlineKeyboardMarkup | None = None) -> bool:
    if message_id is None:
        return False
    try:
        await _retry(bot.edit_message_text, text, chat_id=chat_id, message_id=message_id,
                     reply_markup=reply_markup)
        return True
    except TelegramBadRequest as e:
        return "not modified" in str(e)
    except Exception as e:
        logger.debug("edit failed: %s", e)
        return False


async def _send_photo(bot: Bot, chat_id: int, png: bytes, caption: str = "",
                      reply_markup: InlineKeyboardMarkup | None = None) -> Message:
    return await _retry(
        bot.send_photo, chat_id, BufferedInputFile(png, filename="card.png"),
        caption=caption[:MAX_CAPTION] or None, reply_markup=reply_markup,
    )


async def _render(p: Player, game: Game, **kwargs) -> bytes:
    return await asyncio.to_thread(render_card, p.name, p.card or {}, p.serial, **kwargs)


async def _send_exile_card(bot: Bot, game: Game, p: Player, stamp: str, caption: str) -> None:
    """Досье выбывшего: картинка с печатью, при сбое — текст."""
    try:
        png = await _render(p, game, stamp=stamp, revealed=set(p.revealed))
        await _send_photo(bot, game.chat_id, png, caption)
    except Exception as e:
        logger.warning("exile card failed: %s", e)
        await _send(bot, game.chat_id, caption[:MAX_TEXT])


# ---------- форматирование ----------


def _e(s) -> str:
    return html.escape(str(s))


def _cut(s, n: int) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _mention(p: Player) -> str:
    if p.is_bot:
        return f"<b>{_e(p.name)}</b>"
    if p.username:
        return f"@{p.username}"
    return f'<a href="tg://user?id={p.user_id}">{_e(p.name)}</a>'


def _names(names: list[str], last: str = " и ") -> str:
    """«A», «A и B», «A, B и C»."""
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + last + names[-1]


def _quote(text: str) -> str:
    return f"<blockquote expandable>{_e(text)}</blockquote>"


def _host(game: Game, text: str) -> str:
    """Реплика ведущего: заголовок в духе режима и текст абзацами в цитате."""
    voice = C.MODE_VOICE.get(game.mode, C.MODE_VOICE["classic"])
    return f"{voice}\n<blockquote>{_e(text.strip())}</blockquote>"


AI_TIMEOUT = 90       # сек: дольше реплику ведущего не ждём — партия идёт дальше без неё
SETUP_TIMEOUT = 150   # сек: сценарий партии (Opus, большой JSON); дольше — встроенный генератор


def _ai_task(coro) -> asyncio.Task:
    """Запускает запрос к ИИ заранее, пока идёт пауза; ошибки и зависания не роняют и не держат игру."""
    async def safe():
        try:
            return await asyncio.wait_for(coro, timeout=AI_TIMEOUT)
        except Exception as e:  # в том числе TimeoutError
            logger.warning("AI call failed: %s", e or type(e).__name__)
            return None
    return asyncio.create_task(safe())


def _bot_kb(game: Game, text: str = "🃏 Открыть свою карточку") -> InlineKeyboardMarkup | None:
    if not game.bot_username:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, url=f"https://t.me/{game.bot_username}")
    ]])


def _msg_link(game: Game, message_id: int | None) -> str | None:
    s = str(game.chat_id)
    if message_id is None or not s.startswith("-100"):
        return None
    return f"https://t.me/c/{s[4:]}/{message_id}"


def _bar(value: float, top: float, width: int = 8) -> str:
    filled = 0 if top <= 0 else round(width * value / top)
    if value > 0:
        filled = max(1, filled)
    return "▓" * filled + "░" * (width - filled)


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:.1f}"


def _lobby_text(game: Game) -> str:
    n = len(game.players)
    spots = max(1, n // 2)  # как при старте партии
    return lobby_text("🏚 БУНКЕР", [(p.name, p.user_id == game.host_id, p.dm_ok or p.is_bot)
                                    for p in game.players.values()],
                      MAX_PLAYERS, MIN_PLAYERS, _mode_title(game), "Расклад",
                      [f"• Мест в бункере - {spots}", f"• Останутся за дверью - {n - spots}"])


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _lobby_kb(game: Game) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if game.bot_username:
        b.row(InlineKeyboardButton(
            text="🎮 Вступить в игру",
            url=f"https://t.me/{game.bot_username}?start=bk_{game.chat_id}",
        ))
    b.row(_btn(f"🎭 Ведущий: {_mode_title(game)}", "bk:mode"), _btn("Выйти 🚪", "bk:leave"))
    b.row(_btn("Начать игру ▶️", "bk:start"), _btn("Отмена ❌", "bk:cancel"))
    return b.as_markup()

# ---------- лобби картинкой (assets/lobby — готовый файл, рисует tools/static_images.py) ----------

LOBBY_IMAGE = Path(__file__).resolve().parents[2] / "assets" / "lobby" / "bunker.jpg"
_lobby_file_id: list[str] = []  # file_id картинки в Telegram — после первой отправки не грузим заново


async def _send_lobby(message: Message, game: Game) -> None:
    """Лобби — картинка с подписью (список игроков) и кнопками; без картинки — просто текстом."""
    try:
        photo = _lobby_file_id[0] if _lobby_file_id else FSInputFile(LOBBY_IMAGE)
        sent = await message.answer_photo(photo, caption=_lobby_text(game), reply_markup=_lobby_kb(game))
        if sent.photo and not _lobby_file_id:
            _lobby_file_id.append(sent.photo[-1].file_id)
        game.lobby_photo = True
    except Exception as e:
        logger.info("lobby photo failed, sending text: %s", e)
        sent = await message.answer(_lobby_text(game), reply_markup=_lobby_kb(game))
        game.lobby_photo = False
    game.lobby_msg_id = sent.message_id


async def _edit_lobby(bot: Bot, game: Game, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Правка лобби: у картинки меняем подпись, у текста — текст."""
    if game.lobby_msg_id is None:
        return
    if not game.lobby_photo:
        await _edit(bot, game.chat_id, game.lobby_msg_id, text, reply_markup)
        return
    try:
        await bot.edit_message_caption(chat_id=game.chat_id, message_id=game.lobby_msg_id, caption=text,
                                       reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            logger.debug("lobby caption edit failed: %s", e)
    except Exception as e:
        logger.debug("lobby caption edit failed: %s", e)



def _board_player(p: Player, short: bool) -> str:
    shown = [k for k in C.ATTR_KEYS if k in p.revealed]
    if not shown:
        return f"<b>{_e(p.name)}</b>\n<i>пока ничего не открыто</i>"
    if short:
        return f"<b>{_e(p.name)}</b>  " + " ".join(C.ATTR_MAP[k][1] for k in shown)
    lines = [f"<b>{_e(p.name)}</b>"]
    for k in shown:
        lines.append(f"{C.ATTR_MAP[k][1]} {_e(_cut((p.card or {}).get(k, '—'), 48))}")
    return "\n".join(lines)


def _board_text(game: Game, short: bool = False) -> str:
    alive = _alive(game)
    out_players = sorted((p for p in game.players.values() if not p.alive), key=lambda x: x.out_round or 0)
    parts = [
        f"📌 <b>БУНКЕР · раунд {game.round_no}</b>" if game.round_no else "📌 <b>БУНКЕР</b>",
        f"🎟 Мест: <b>{game.spots}</b> · В игре: <b>{len(alive)}</b>"
        + (" · 🏁 <b>игра окончена</b>" if game.phase == "ended" else ""),
    ]
    if game.bunker_known:
        facts = "\n".join(f"• {_e(_cut(f, 90))}" for f in game.bunker_known)
        parts.append(f"🏚 <b>Что известно о бункере</b>\n{facts}")

    title = "✅ <b>ПОПАЛИ В БУНКЕР</b>" if game.phase == "ended" else "🟢 <b>В ИГРЕ</b>"
    blocks = "\n\n".join(_board_player(p, short) for p in alive)
    opened = sum(len(p.revealed) for p in alive)
    if opened > 6:  # много открытых карт — сворачиваем, чтобы закреп не был простынёй
        blocks = f"<blockquote expandable>{blocks}</blockquote>"
    parts.append(f"{title} ({len(alive)})\n\n{blocks}")

    if out_players:
        gone = "\n".join(
            f"<s>{_e(p.name)}</s> — {'☠️ погиб(ла)' if p.died else 'изгнан(а)'}, раунд {p.out_round}"
            for p in out_players)
        parts.append(f"💀 <b>ВЫБЫЛИ</b> ({len(out_players)})\n{gone}")

    text = f"\n{HR}\n".join(parts)
    if len(text) > MAX_TEXT and not short:
        return _board_text(game, short=True)
    return text[:MAX_TEXT]


async def _refresh_board(bot: Bot, game: Game) -> None:
    await _edit(bot, game.chat_id, game.board_msg_id, _board_text(game))


def _attr_line(p: Player, key: str) -> str:
    label, emoji = C.ATTR_MAP[key]
    mark = "✅" if key in p.revealed else "🔒"
    return f"{mark} {emoji} <b>{label}:</b> {_e(_cut((p.card or {}).get(key, '—'), 60))}"


def _pick_text(game: Game, p: Player) -> str:
    lines = [f"🔦 <b>Раунд {game.round_no} — твой ход!</b> ⏳ {TURN_TIMEOUT} сек", ""]
    lines += [_attr_line(p, k) for k in C.ATTR_KEYS]
    lines += ["", "✅ — уже открыто, 🔒 — пока секрет. Что открываешь?"]
    return "\n".join(lines)


def _action_btn(game: Game, p: Player) -> InlineKeyboardButton | None:
    if not p.action or p.action_used or p.action not in C.ACTIONS:
        return None
    emoji, title, *_ = C.ACTIONS[p.action]
    return _btn(f"🃏 Сыграть: {emoji} {title}", f"bkact:{game.chat_id}")


def _pick_kb(game: Game, p: Player) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key in C.ATTR_KEYS:
        if key not in p.revealed:
            label, emoji = C.ATTR_MAP[key]
            b.button(text=f"{emoji} {label}", callback_data=f"bkpk:{game.chat_id}:{key}")
    b.adjust(2)
    act = _action_btn(game, p)
    if act:
        b.row(act)
    return b.as_markup()


def _card_caption(game: Game, p: Player) -> str:
    parts = [f"🧳 <b>Твоя секретная карточка, {_e(p.name)}</b>",
             "В свой ход открываешь одну характеристику. Остальное — секрет 😉"]
    if p.action in C.ACTIONS:
        emoji, title, desc, when, _ = C.ACTIONS[p.action]
        when_txt = "только во время голосования" if when == "vote" else "в любой момент раунда"
        parts.append(f"\n🃏 <b>Карта действия: {emoji} {title}</b>\n{_e(desc)} <i>(одноразовая, {when_txt})</i>")
    return "\n".join(parts)


def _scores(game: Game) -> dict[int, float]:
    """Сколько голосов против каждого кандидата (с учётом карт действий)."""
    targets = game.vote_targets or {p.user_id for p in _alive(game)}
    scores = {p.user_id: 0.0 for p in _alive(game) if p.user_id in targets}
    for voter, target in game.votes.items():
        if target in scores and voter not in game.cursed:
            scores[target] += 2.0 if voter in game.doubled else 1.0
    return scores


def _bars(game: Game) -> str:
    scores = _scores(game)
    top = max(scores.values(), default=0)
    width = max((len(game.players[u].name) for u in scores), default=4)
    width = min(width, 14)
    lines = []
    for uid, val in sorted(scores.items(), key=lambda kv: -kv[1]):
        p = game.players[uid]
        name = _cut(p.name, 14).ljust(width)
        mark = " 🛡" if uid in game.immune else ""
        lines.append(f"{_e(name)} {_bar(val, top)} {_num(val)}{mark}")
    return "<code>" + "\n".join(lines) + "</code>"


def _vote_text(game: Game) -> str:
    alive = _alive(game)
    if game.revote:
        names = _names([f"<b>{_e(game.players[u].name)}</b>" for u in game.vote_targets], " или ")
        lines = [f"⚖️ <b>ПЕРЕГОЛОСОВАНИЕ · раунд {game.round_no}</b>", HR, "",
                 f"Ничья! Выбираем только между: {names}", ""]
    else:
        lines = [f"🗳 <b>ГОЛОСОВАНИЕ · раунд {game.round_no}</b>", HR, "",
                 f"👥 В живых: {len(alive)} · 🎟 мест: {game.spots}", ""]
    lines += [f"✋ Проголосовали: {len(game.votes)}/{len(alive)}", "", _bars(game), ""]
    effects = []
    if game.immune:
        effects.append("🛡 иммунитет (против них голосовать нельзя): "
                       + ", ".join(_e(game.players[u].name) for u in game.immune))
    if game.doubled:
        effects.append("✌️ двойной голос: " + ", ".join(_e(game.players[u].name) for u in game.doubled))
    if game.cursed:
        effects.append("🧿 голос не считается: " + ", ".join(_e(game.players[u].name) for u in game.cursed))
    if effects:
        lines += effects + [""]
    lines.append("❓ Кого <b>НЕ</b> пускать в бункер? Голосуют только игроки, голос можно менять.")
    link = _msg_link(game, game.board_msg_id)
    if link:
        lines += ["", f'📌 <a href="{link}">Табло с открытыми картами</a>']
    return "\n".join(lines)


def _vote_kb(game: Game) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for p in _alive(game):
        if p.user_id in game.immune:  # за иммунного голосовать нельзя — кнопки нет
            continue
        if not game.vote_targets or p.user_id in game.vote_targets:
            b.button(text=f"🚫 {p.name}", callback_data=f"bk:vote:{p.user_id}")
    b.adjust(2)
    b.row(_btn("Подвести итог 🧮 (ведущий)", "bk:tally"))
    return b.as_markup()


async def _refresh_vote(bot: Bot, game: Game) -> None:
    """Обновить голосование. Под замком и с проверкой стадии уже под ним: иначе отложенная правка
    (или повтор после флуд-лимита) ложилась поверх закрытого голосования и возвращала кнопки."""
    mid = game.vote_msg_id
    async with game.edit_lock:
        if game.phase == "voting" and game.stage in ("open", "speech", "revote") and game.vote_msg_id == mid:
            await _edit(bot, game.chat_id, mid, _vote_text(game), _vote_kb(game))


async def _refresh_threat(bot: Bot, game: Game) -> None:
    """То же для голосования «кого отправить на угрозу»."""
    mid = game.threat_msg_id
    async with game.edit_lock:
        if game.phase == "threat" and game.threat_msg_id == mid:
            await _edit(bot, game.chat_id, mid, _threat_text(game), _threat_kb(game))


# ---------- посторонние во время партии ----------


def _is_outsider(message: Message) -> bool:
    """Сообщение в группе от того, кто не играет в идущей партии."""
    game = games.get(message.chat.id)
    if not game or game.phase in ("lobby", "ended") or game.cant_delete:
        return False
    if message.chat.type not in ("group", "supergroup"):
        return False
    if message.sender_chat or message.is_automatic_forward:  # посты канала и анонимные админы
        return False
    from games import is_comment

    if is_comment(message):  # комментарии к постам канала во время игры не удаляем
        return False
    u = message.from_user
    return bool(u and not u.is_bot and u.id not in game.players)


@router.message(_is_outsider)
async def on_outsider(message: Message) -> None:
    game = games.get(message.chat.id)
    if not game:
        return
    try:
        await message.delete()
    except Exception as e:  # нет права «удалять сообщения» — больше не пытаемся в этой партии
        game.cant_delete = True
        logger.info("can't delete outsider message in %s: %s", message.chat.id, e)
        return
    if not game.outsider_warned:
        game.outsider_warned = True
        try:
            note = await _send(message.bot, game.chat_id,
                               "🤫 Идёт игра в Бункер — пишут только игроки. "
                               "Сообщения остальных удаляю до конца партии.")
        except Exception:
            return

        async def drop() -> None:
            await asyncio.sleep(30)
            try:
                await note.delete()
            except Exception:
                pass

        _launch(message.bot, game, drop(), fatal=False)


# ---------- команды ----------


@router.message(Command("bunker"))
async def cmd_bunker(message: Message, command: CommandObject, db: Database) -> None:
    chat_id = message.chat.id
    user = message.from_user
    if user is None:
        return
    if message.chat.type == "private":
        await message.answer("В Бункер играют в групповом чате. Добавь бота в чат и напиши там /bunker.")
        return

    existing = games.get(chat_id)
    if existing and existing.phase != "ended":
        await temp_reply(message, "В этом чате уже идёт игра в Бункер. Доиграйте или /bunker_stop.")
        return
    from games import active, load_channel_posts

    if active(chat_id) == "mafia":
        await temp_reply(message, "В этом чате идёт Мафия. Доиграйте или /mafia_stop.")
        return
    await load_channel_posts(db, chat_id)  # чтобы не удалять комментарии к постам канала

    game = Game(chat_id=chat_id, host_id=user.id, db=db)
    arg = (command.args or "").strip().lower()
    saved = await db.get_setting(_mode_key(chat_id), "classic")
    game.mode = saved if saved in C.MODES else "classic"
    if arg in C.MODE_ALIASES:
        game.mode = C.MODE_ALIASES[arg]
        await db.set_setting(_mode_key(chat_id), game.mode)
    try:
        game.bot_username = (await message.bot.me()).username or ""
    except Exception:
        game.bot_username = ""
    game.players[user.id] = Player(
        user_id=user.id, name=_unique_name(game, user.first_name), username=user.username
    )
    games[chat_id] = game

    await _send_lobby(message, game)
    _launch(message.bot, game, _lobby_timer(message.bot, game), fatal=False)


async def _lobby_timer(bot: Bot, game: Game) -> None:
    await asyncio.sleep(LOBBY_TIMEOUT)
    if games.get(game.chat_id) is not game or game.phase != "lobby":
        return
    _end_game(game)
    await _edit_lobby(bot, game, "⌛ Лобби закрыто — игра так и не началась.\n/bunker — собрать заново.")
    delete_later(bot, game.chat_id, game.lobby_msg_id)


@router.message(CommandStart(deep_link=True, magic=F.args.func(lambda a: bool(a) and a.startswith("bk_"))))
async def cmd_deeplink_join(message: Message, command: CommandObject) -> None:
    """Вход в игру по кнопке-ссылке: /start bk_<chat_id> в личке бота."""
    user = message.from_user
    if user is None:
        return
    try:
        chat_id = int((command.args or "")[3:])
    except ValueError:
        await message.answer("Не понял, в какую игру вступить. Попроси ведущего прислать кнопку заново.")
        return

    game = games.get(chat_id)
    if not game or game.phase != "lobby":
        await message.answer("Игра уже началась или не найдена 🙁\n"
                             "Пусть ведущий создаст новую: /bunker в групповом чате.")
        return
    if user.id in game.players:
        game.players[user.id].dm_ok = True  # например, ведущий подключает личку
        await message.answer("Ты уже в игре! Жди старта — карточка придёт сюда, в ЛС. 🧳")
        await _edit_lobby(message.bot, game, _lobby_text(game), _lobby_kb(game))
        return
    if len(game.players) >= MAX_PLAYERS:
        await message.answer("Увы, мест в лобби больше нет.")
        return

    player = Player(user_id=user.id, name=_unique_name(game, user.first_name),
                    username=user.username, dm_ok=True)
    game.players[user.id] = player
    await message.answer("✅ Ты в игре «Бункер»!\n"
                         "Когда ведущий начнёт — сюда придут твоя карточка и карта действия, а в свой ход ты будешь выбирать здесь, что открыть.")
    try:
        await _send(message.bot, chat_id, f"➕ <b>{_e(player.name)}</b> спускается в бункер!")
    except Exception:
        pass
    await _edit_lobby(message.bot, game, _lobby_text(game), _lobby_kb(game))


def _mode_key(chat_id: int) -> str:
    return f"bunker_mode:{chat_id}"


# ---------- тестовые боты (временно) ----------

BOT_NAMES = ["Валера", "Зинаида", "Геннадий", "Снежана", "Аркадий", "Люська"]


@router.message(Command("bunker_bots"))
async def cmd_bunker_bots(message: Message, command: CommandObject) -> None:
    """Добавить в лобби тестовых ботов: /bunker_bots [сколько, по умолчанию 3]."""
    game = games.get(message.chat.id)
    if not game or game.phase != "lobby":
        await message.answer("Сначала создайте лобби: /bunker")
        return
    if message.from_user and message.from_user.id != game.host_id:
        await message.answer("Добавлять ботов может только ведущий.")
        return
    try:
        n = max(1, min(6, int((command.args or "3").strip())))
    except ValueError:
        n = 3
    added = []
    existing = sum(1 for p in game.players.values() if p.is_bot)
    for i in range(n):
        if len(game.players) >= MAX_PLAYERS:
            break
        uid = -(existing + i + 1)  # отрицательные id — точно не настоящие люди
        name = _unique_name(game, BOT_NAMES[(existing + i) % len(BOT_NAMES)])
        game.players[uid] = Player(user_id=uid, name=name, is_bot=True)
        added.append(name)
    await message.answer("➕ В бункер спускаются тестовые боты: " + ", ".join(_e(a) for a in added))
    await _edit_lobby(message.bot, game, _lobby_text(game), _lobby_kb(game))


def _bots(game: Game) -> list[Player]:
    return [p for p in _alive(game) if p.is_bot]


async def _bots_vote(bot: Bot, game: Game, threat: bool = False) -> None:
    """Боты голосуют со случайной задержкой, как живые."""
    revote = game.revote
    for p in random.sample(_bots(game), len(_bots(game))):
        await asyncio.sleep(random.uniform(2, 6))
        if not _is_active(game) or game.phase != ("threat" if threat else "voting") or not p.alive:
            return
        if threat:
            targets = [o.user_id for o in _alive(game)]
            game.threat_votes[p.user_id] = random.choice(targets)
            _schedule(bot, game, "threat", lambda: _refresh_threat(bot, game))
            if len(game.threat_votes) >= len(_alive(game)) and game.advance:
                game.advance.set()
            continue
        if game.revote != revote or game.stage == "closed":
            return
        targets = [u for u in (game.vote_targets or {o.user_id for o in _alive(game)})
                   if u != p.user_id and u not in game.immune]
        if not targets:
            continue
        game.votes[p.user_id] = random.choice(targets)
        _schedule(bot, game, "vote", lambda: _refresh_vote(bot, game))
        if game.stage in ("open", "revote") and len(game.votes) >= len(_alive(game)) and game.advance:
            game.advance.set()


async def _bot_speech(bot: Bot, game: Game, p: Player) -> None:
    await asyncio.sleep(random.uniform(3, 10))
    if game.stage != "speech" or p.user_id not in game.speakers:
        return
    line = random.choice(C.BOT_SPEECHES)
    game.speakers[p.user_id].append(line)
    await _send(bot, game.chat_id, f"🎤 <b>{_e(p.name)}:</b> «{_e(line)}»")


@router.message(Command("bunker_stop"))
async def cmd_bunker_stop(message: Message) -> None:
    game = games.get(message.chat.id)
    if not game:
        await message.answer("Сейчас нет активной игры.")
        return
    if message.from_user and message.from_user.id != game.host_id:
        await message.answer("Остановить игру может только ведущий (кто её создал).")
        return
    in_lobby = game.phase == "lobby"
    _end_game(game)
    if in_lobby:  # остановили ещё в лобби — убираем его с кнопками, чтобы не висело
        try:
            await message.bot.delete_message(game.chat_id, game.lobby_msg_id)
        except Exception:
            pass
    if game.board_msg_id:
        try:
            await message.bot.unpin_chat_message(game.chat_id, message_id=game.board_msg_id)
        except Exception:
            pass
    await message.answer("🛑 Игра в Бункер остановлена.")


@router.message(Command("top", "bunker_top"))
async def cmd_top(message: Message, command: CommandObject, db: Database) -> None:
    """Рейтинг выживших со званиями; ответом на сообщение или /top @ник — профиль игрока."""
    rt = message.reply_to_message
    arg = (command.args or "").strip()
    if rt and rt.from_user and not rt.from_user.is_bot:
        await _bunker_profile(message, db, rt.from_user.id, rt.from_user.first_name or "Игрок")
        return
    if arg.startswith("@"):
        found = await db.find_user(message.chat.id, arg.split()[0])
        if not found:
            await message.answer("Не знаю такого. Пусть сначала что-нибудь напишет в чат.")
            return
        await _bunker_profile(message, db, found[0], found[1] or arg)
        return

    rows = await db.bunker_top(10)
    if not rows:
        await message.answer("🏆 Рейтинг пуст — сыграйте первую партию: /bunker")
        return
    medals = ["🥇", "🥈", "🥉"]
    blocks = [f"🏆 <b>РЕЙТИНГ ВЫЖИВШИХ</b>\n{HR}"]
    for i, r in enumerate(rows):
        place = medals[i] if i < 3 else f"<b>{i + 1}.</b>"
        rate = r["survived"] / r["games"] if r["games"] else 0
        filled = round(rate * 10)
        bar = "▰" * filled + "▱" * (10 - filled)
        ttl = C.titles(r)
        streak = f" · 🔥 серия {r['streak']}" if r["streak"] >= 2 else ""
        blocks.append(
            f"{place} <b>{_e(r['name'])}</b>"
            + (f"\n🏅 {', '.join(f'{e} {n}' for e, n, _w in ttl[:3])}" if ttl else "")
            + f"\n🏚 Выжил(а): <b>{r['survived']}</b> из {r['games']}{streak}"
            f"\n<code>{bar}</code> {round(rate * 100)}%"
        )
    blocks.append(f"{HR}\n👤 Профиль игрока: ответь /top на его сообщение или /top @ник\n🔁 Сыграть ещё: /bunker")
    await message.answer("\n\n".join(blocks))


async def _bunker_profile(message: Message, db: Database, user_id: int, name: str) -> None:
    prof = await db.bunker_profile(user_id)
    if not prof or not prof["games"]:
        await message.answer(f"🏚 {_e(name)} ещё не играл(а) в Бункер. /bunker — исправить.")
        return
    g = prof["games"]
    rate = round(100 * prof["survived"] / g)
    ttl = C.titles(prof)
    lines = [f"👤 <b>{_e(prof['name'] or name)}</b> · БУНКЕР", HR, "",
             f"🏆 Место в рейтинге: <b>{prof['place']}</b>",
             f"🏚 Выжил(а): <b>{prof['survived']}</b> из {g} ({rate}%)",
             f"🔥 Серия сейчас: <b>{prof['streak']}</b> · лучшая: <b>{prof['best_streak']}</b>",
             f"💥 Вылетал(а) первым(ой): <b>{prof['first_out']}</b>",
             f"🎯 Голосов против: <b>{prof['votes_against']}</b> (≈{prof['votes_against'] / g:.1f} за партию)",
             f"🦸 Справился(ась) с угрозой: <b>{prof['heroes']}</b> · ☠️ не вернулся(ась): <b>{prof['deaths']}</b>",
             f"🍀 Выжил(а) без голосов против: <b>{prof['lucky']}</b>"]
    if ttl:
        lines += ["", "🏅 <b>Звания</b>"] + [f"{e} <b>{n}</b> — {w}" for e, n, w in ttl]
    else:
        lines += ["", "🏅 Званий пока нет — играй ещё."]
    await message.answer("\n".join(lines))


# ---------- лобби ----------


def _lobby_game(call: CallbackQuery) -> Game | None:
    """Лобби, к которому относится нажатая кнопка, — только если это его сообщение (а не старого лобби)."""
    game = games.get(call.message.chat.id) if call.message else None
    if game and game.phase == "lobby" and game.lobby_msg_id == call.message.message_id:
        return game
    return None


async def _stale_lobby(call: CallbackQuery) -> None:
    """Кнопка старого лобби (отменено, остановлено, уже есть новое): говорим и убираем это сообщение."""
    game = games.get(call.message.chat.id) if call.message else None
    if game and call.message and game.lobby_msg_id == call.message.message_id:
        await call.answer("Игра уже началась.", show_alert=True)  # это лобби идущей партии — не трогаем
        return
    await call.answer("Это лобби уже закрыто.", show_alert=True)
    if call.message:
        try:
            await call.message.delete()
        except Exception:
            try:
                await call.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass


@router.callback_query(F.data == "bk:mode")
async def cb_mode(call: CallbackQuery) -> None:
    game = _lobby_game(call)
    if not game:
        await _stale_lobby(call)
        return
    if call.from_user.id != game.host_id:
        await call.answer("Режим ведущего выбирает только тот, кто создал игру.", show_alert=True)
        return
    i = C.MODE_ORDER.index(game.mode) if game.mode in C.MODE_ORDER else 0
    game.mode = C.MODE_ORDER[(i + 1) % len(C.MODE_ORDER)]
    if game.db:
        await game.db.set_setting(_mode_key(game.chat_id), game.mode)  # запомним на следующие партии
    await call.answer(f"Ведущий: {_mode_title(game)}")
    await _edit_lobby(call.bot, game, _lobby_text(game), _lobby_kb(game))


@router.callback_query(F.data == "bk:leave")
async def cb_leave(call: CallbackQuery) -> None:
    game = _lobby_game(call)
    if not game:
        await _stale_lobby(call)
        return
    uid = call.from_user.id
    if uid not in game.players:
        await call.answer("Тебя и так нет в игре.")
        return
    if uid == game.host_id:
        await call.answer("Ведущий не может выйти — используйте «Отмена».", show_alert=True)
        return
    game.players.pop(uid, None)
    await call.answer("Ты вышел из игры.")
    await _edit_lobby(call.bot, game, _lobby_text(game), _lobby_kb(game))


@router.callback_query(F.data == "bk:cancel")
async def cb_cancel(call: CallbackQuery) -> None:
    game = _lobby_game(call)
    if not game:
        await _stale_lobby(call)
        return
    if call.from_user.id != game.host_id:
        await call.answer("Отменить может только ведущий.", show_alert=True)
        return
    _end_game(game)
    await call.answer("Игра отменена.")
    if call.message:
        await _edit_lobby(call.bot, game, "❌ Игра в Бункер отменена.")
        delete_later(call.bot, game.chat_id, game.lobby_msg_id)  # мёртвое лобби — уберём из чата


@router.callback_query(F.data == "bk:start")
async def cb_start(call: CallbackQuery) -> None:
    game = _lobby_game(call)
    if not game:
        await _stale_lobby(call)
        return
    if call.from_user.id != game.host_id:
        await call.answer("Начать игру может только ведущий.", show_alert=True)
        return
    if len(game.players) < MIN_PLAYERS:
        await call.answer(f"Нужно минимум {MIN_PLAYERS} игрока.", show_alert=True)
        return

    await call.answer("Поехали!")
    game.phase = "revealing"
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    _launch(call.bot, game, _run_game(call.bot, game))


# ---------- подготовка партии ----------


def _assign_cards(players: list[Player], cards: list) -> None:
    """Раздаёт карточки от ИИ: по имени, иначе любую свободную, иначе запасную.

    Каждая карточка достаётся не больше чем одному игроку; пустые поля
    добиваются из запасного генератора.
    """
    pool = [c for c in cards if isinstance(c, dict)]
    names = {p.name for p in players}
    by_name: dict[str, dict] = {}
    for c in pool:
        by_name.setdefault(str(c.get("name", "")).strip(), c)

    used: set[int] = set()
    for p in players:
        card = by_name.get(p.name)
        if card is not None and id(card) in used:
            card = None
        if card is None:
            card = next((c for c in pool
                         if id(c) not in used and str(c.get("name", "")).strip() not in names), None)
        if card is not None:
            used.add(id(card))
        base = C.fallback_card(p.name)
        extra = {k: str(v).strip() for k, v in (card or {}).items()
                 if k in C.ATTR_MAP and v is not None and str(v).strip()}
        p.card = {**base, **extra, "name": p.name}
        p.card["bio"] = C.clean_bio(p.card.get("bio", ""))
        rel = str(p.card.get("relation", "")).strip()
        if not rel or rel.startswith("ни с кем"):  # пусто — иногда придумаем связь сами
            others = [o.name for o in players if o is not p]
            p.card["relation"] = C.make_relation(others) if random.random() < 0.5 else random.choice(C.SOLO_RELATIONS)
    # ИИ любит раздавать одних «мужчин» и «женщин» — треть партии отдаём нелюдям
    C.ensure_creatures([p.card for p in players])


async def _run_game(bot: Bot, game: Game) -> None:
    # Метка партии для учёта расходов на ИИ (наследуется всеми подзадачами партии)
    ai.current_game.set(f"{game.chat_id}:{int(time.time())}")
    players = list(game.players.values())
    names = [p.name for p in players]
    status = await _send(bot, game.chat_id, "🎙 Ведущий готовит сценарий… (до минуты)")
    # Тема катастрофы — случайная, но не как в прошлой партии этого чата
    last = await game.db.get_setting(f"bunker_last_cat:{game.chat_id}", "") if game.db else ""
    cat = C.pick_catastrophe(last)
    if game.db:
        await game.db.set_setting(f"bunker_last_cat:{game.chat_id}", cat[0])

    try:
        setup = (await asyncio.wait_for(ai.generate_setup(names, C.style_examples(), cat[0]), SETUP_TIMEOUT)
                 if ai.available() else C.fallback_setup(names, cat))
    except Exception as e:
        logger.warning("AI setup failed: %s", e)
        setup = C.fallback_setup(names, cat)

    game.catastrophe = str(setup.get("catastrophe", "")).strip() or cat[2]
    game.bunker = str(setup.get("bunker", "")).strip() or C.FALLBACK_BUNKER
    game.title = str(setup.get("title", "")).strip()[:60] or cat[1]
    threat = setup.get("threat")
    if not (isinstance(threat, dict) and threat.get("title") and threat.get("text")):
        threat = random.choice(C.FALLBACK_THREATS)
    game.threat = {"title": str(threat["title"]).strip(), "text": str(threat["text"]).strip()}
    game.serial = random.randint(1, 9999)
    secrets = [str(s).strip() for s in (setup.get("bunker_secrets") or []) if str(s).strip()]
    extra = [s for s in random.sample(C.FALLBACK_BUNKER_SECRETS, len(C.FALLBACK_BUNKER_SECRETS))
             if s not in secrets]
    game.bunker_secrets = (secrets + extra)[:MAX_PLAYERS]

    cards = setup.get("cards")
    _assign_cards(players, cards if isinstance(cards, list) else [])
    for p, action in zip(players, C.deal_actions(len(players))):
        p.action = action
        p.serial = random.randint(1, 9999)

    game.spots = max(1, len(players) // 2)
    game.warmup = warmup_rounds(len(players))
    rounds = len(players) - game.spots
    if len(players) >= THREAT_MIN_PLAYERS and rounds >= 2:
        game.threat_round = game.warmup + max(1, (rounds + 1) // 2)  # где-то в середине партии

    # Карточки-картинки — в личку
    no_dm = []
    for p in players:
        if p.is_bot:
            continue
        try:
            png = await _render(p, game, action=p.action)
            kb = InlineKeyboardBuilder()
            act = _action_btn(game, p)
            if act:
                kb.row(act)
            await _send_photo(bot, p.user_id, png, _card_caption(game, p), kb.as_markup())
            p.dm_ok = True
        except Exception as e:
            logger.info("card DM to %s failed: %s", p.user_id, e)
            p.dm_ok = False
            no_dm.append(p)

    try:
        await status.delete()
    except Exception:
        pass

    # Катастрофа и бункер — только картинками, одним альбомом
    try:
        cover = await asyncio.to_thread(R.render_cover, game.title, game.catastrophe,
                                        len(players), game.spots, game.serial)
        bunker_png = await asyncio.to_thread(R.render_bunker, game.bunker, len(players),
                                             game.spots, game.serial)
        await _retry(bot.send_media_group, game.chat_id, [
            InputMediaPhoto(media=BufferedInputFile(cover, filename="catastrophe.png")),
            InputMediaPhoto(media=BufferedInputFile(bunker_png, filename="bunker.png")),
        ])
        await asyncio.sleep(STEP_DELAY)
    except Exception as e:
        logger.warning("cover failed: %s", e)
        await _say(bot, game.chat_id,
                   f"☢️ <b>КАТАСТРОФА</b>\n{_quote(game.catastrophe)}\n\n"
                   f"🏚 <b>БУНКЕР</b>\n{_quote(game.bunker)}")


    if no_dm:
        who = ", ".join(_mention(p) for p in no_dm)
        await _say(bot, game.chat_id,
                   f"⚠️ {who}: не удалось написать вам в ЛС.\n\n"
                   f"Откройте бота и нажмите Start — пока характеристики будут открываться "
                   f"за вас автоматически.", delay=3, reply_markup=_bot_kb(game, "🤖 Открыть бота"))

    board = await _send(bot, game.chat_id, _board_text(game))
    game.board_msg_id = board.message_id
    try:
        await bot.pin_chat_message(game.chat_id, board.message_id, disable_notification=True)
    except Exception as e:
        logger.info("pin failed: %s", e)

    while _is_active(game):
        await _reveal_round(bot, game)
        if not _is_active(game):
            return
        if game.round_no == game.threat_round:
            died = await _threat(bot, game)
            if not _is_active(game):
                return
            if died:  # угроза уже забрала одного — обычного голосования в этом раунде нет
                if len(_alive(game)) <= game.spots:
                    await _finish(bot, game)
                    return
                continue
        if game.round_no <= game.warmup:
            left = game.warmup - game.round_no
            await _say(bot, game.chat_id,
                       f"☕ <b>Раунд {game.round_no} — разогрев, голосования нет.</b>\n"
                       + ("Ещё один круг карт — и начнём выгонять." if left else
                          "Со следующего раунда — голосуем, кого не пускать."), delay=REVEAL_DELAY)
            continue
        await _voting(bot, game)
        if not _is_active(game):
            return
        if len(_alive(game)) <= game.spots:
            await _finish(bot, game)
            return


# ---------- фаза раскрытия ----------


def _round_summary(game: Game) -> str:
    """Большая карточка «итоги раунда»: всё, что открыли за раунд, по игрокам."""
    by_player: dict[str, list[dict]] = {}
    for r in game.round_reveals:
        by_player.setdefault(r["name"], []).append(r)
    blocks = []
    for name, items in by_player.items():
        lines = [f"<b>{_e(name)}</b>"]
        lines += [f"{r['emoji']} {r['label']}: {_e(r['value'])}" for r in items]
        blocks.append("\n".join(lines))
    body = "\n\n".join(blocks)
    quote = "blockquote expandable" if len(game.round_reveals) > 6 else "blockquote"
    return (f"📋 <b>ИТОГИ РАУНДА {game.round_no}</b>\n{HR}\n\n"
            f"<{quote}>{body}</{quote.split()[0]}>")[:MAX_TEXT]


async def _reveal_round(bot: Bot, game: Game) -> None:
    game.phase = "revealing"
    game.round_no += 1
    game.revealed_this_round = set()
    game.round_reveals = []

    fact = ""
    if game.round_no - 1 < len(game.bunker_secrets):
        fact = game.bunker_secrets[game.round_no - 1]
        game.bunker_known.append(fact)
    text = f"{HR}\n🔦 <b>РАУНД {game.round_no}</b>\n{HR}\n\n"
    if fact:
        text += f"🏚 <b>Новое о бункере:</b>\n<blockquote>{_e(fact)}</blockquote>\n\n"
    text += "Открываем характеристики по очереди 👇"
    await _say(bot, game.chat_id, text, delay=0)
    await _refresh_board(bot, game)

    for p in list(_alive(game)):
        if not _is_active(game):
            return
        if not p.alive or p.user_id in game.revealed_this_round:
            continue
        remaining = [k for k in C.ATTR_KEYS if k not in p.revealed]
        if not remaining:
            continue
        await _turn(bot, game, p, remaining)
        game.current_picker = None
        game.turn_msg_id = None

    if not _is_active(game) or not game.round_reveals:
        return
    # Ведущий комментирует через раунд (1-й, 3-й, …) — экономим запросы к ИИ.
    # Пока все читают итоги раунда, комментарий уже пишется.
    comment = ai.available() and game.round_no % 2 == 1
    note_task = _ai_task(ai.narrate_round(game.catastrophe, game.round_no, game.round_reveals,
                                          _style(game))) if comment else None
    await _say(bot, game.chat_id, _round_summary(game), delay=0)
    note = await note_task if note_task else None
    if note and _is_active(game):
        await _say(bot, game.chat_id, _host(game, note), delay=HOST_DELAY)


def _turn_text(p: Player, left: int) -> str:
    return (f"👉 <b>Ход {_mention(p)}</b>\n\n"
            f"Выбери, какую характеристику открыть — кнопка ниже ведёт к боту.\n\n"
            f"⏳ <b>{left} сек</b>")


async def _turn(bot: Bot, game: Game, p: Player, remaining: list[str]) -> None:
    game.current_picker = p.user_id
    game.pick_event = asyncio.Event()

    if p.is_bot:
        await asyncio.sleep(random.uniform(2, 4))
        await _reveal_attr(bot, game, p, random.choice(remaining), auto=False)
        return

    if p.dm_ok:
        try:
            msg = await _send(bot, p.user_id, _pick_text(game, p), reply_markup=_pick_kb(game, p))
            p.pick_msg_id = msg.message_id
        except Exception:
            p.dm_ok = False

    if not p.dm_ok:
        await _reveal_attr(bot, game, p, random.choice(remaining), auto=True,
                           note="нет лички с ботом — открываю сам")
        return

    turn = await _send(bot, game.chat_id, _turn_text(p, TURN_TIMEOUT), reply_markup=_bot_kb(game))
    game.turn_msg_id = turn.message_id

    left = TURN_TIMEOUT
    for mark in [m for m in (30, 10) if m < TURN_TIMEOUT] + [0]:
        try:
            await asyncio.wait_for(game.pick_event.wait(), timeout=left - mark)
            return  # выбрал — cb_pick уже всё открыл
        except asyncio.TimeoutError:
            pass
        if not _is_active(game) or p.user_id in game.revealed_this_round:
            return
        left = mark
        if mark:
            await _edit(bot, game.chat_id, game.turn_msg_id, _turn_text(p, mark), _bot_kb(game))

    rem = [k for k in C.ATTR_KEYS if k not in p.revealed]
    if rem:
        await _reveal_attr(bot, game, p, random.choice(rem), auto=True, note="время вышло")
    if p.pick_msg_id:
        await _edit(bot, p.user_id, p.pick_msg_id, "⏰ Время вышло — характеристика открыта автоматически.")


def _reveal_text(p: Player, key: str, note: str = "", prefix: str = "") -> str:
    label, emoji = C.ATTR_MAP[key]
    val = (p.card or {}).get(key, "—")
    tail = f"\n<i>{_e(note)}</i>" if note else ""
    return (f"{prefix}🔓 <b>{_e(p.name)}</b> открывает карту{tail}\n\n"
            f"<blockquote>{emoji} <b>{label.upper()}</b>\n{_e(val)}</blockquote>")


async def _reveal_attr(bot: Bot, game: Game, p: Player, key: str, auto: bool,
                       note: str = "", count_turn: bool = True, prefix: str = "") -> None:
    if key in p.revealed:
        return
    p.revealed.add(key)
    label, emoji = C.ATTR_MAP[key]
    if count_turn:
        game.revealed_this_round.add(p.user_id)
    game.round_reveals.append({"name": p.name, "label": label, "emoji": emoji,
                               "value": str((p.card or {}).get(key, "—"))})
    text = _reveal_text(p, key, note=note if auto else "", prefix=prefix)
    if count_turn and game.turn_msg_id and game.current_picker == p.user_id:
        # Сообщение о ходе закрываем (без кнопки и таймера), карта — отдельным сообщением
        await _edit(bot, game.chat_id, game.turn_msg_id, f"✅ Ход {_mention(p)} сделан")
        game.turn_msg_id = None
    await _send(bot, game.chat_id, text)
    _schedule(bot, game, "board", lambda: _refresh_board(bot, game))
    await asyncio.sleep(REVEAL_DELAY)


@router.callback_query(F.data.startswith("bkpk:"))
async def cb_pick(call: CallbackQuery) -> None:
    parts = (call.data or "").split(":")
    try:
        chat_id, key = int(parts[1]), parts[2]
    except (IndexError, ValueError):
        await call.answer()
        return
    game = games.get(chat_id)
    if not game or game.phase != "revealing":
        await call.answer("Этот раунд уже прошёл.", show_alert=True)
        return
    uid = call.from_user.id
    p = game.players.get(uid)
    if p is None or not p.alive:
        await call.answer("Ты не в игре.", show_alert=True)
        return
    if game.current_picker != uid:
        await call.answer("Сейчас не твой ход — дождись своей очереди.", show_alert=True)
        return
    if uid in game.revealed_this_round:
        await call.answer("Ты уже открыл в этом раунде.")
        return
    if key not in C.ATTR_MAP or key in p.revealed:
        await call.answer("Нельзя открыть это.", show_alert=True)
        return

    label, _emoji = C.ATTR_MAP[key]
    await call.answer(f"Открыто: {label}")
    try:
        if call.message:
            await call.message.edit_text(f"✅ Ты открыл: {label}.")
    except Exception:
        pass
    await _reveal_attr(call.bot, game, p, key, auto=False)
    if game.pick_event:
        game.pick_event.set()


# ---------- карты действий ----------


def _action_allowed(game: Game, when: str) -> bool:
    if when == "vote":
        return game.phase == "voting"
    return game.phase in ("revealing", "voting")


@router.callback_query(F.data.startswith("bkact:"))
async def cb_action(call: CallbackQuery) -> None:
    try:
        chat_id = int((call.data or "").split(":")[1])
    except (IndexError, ValueError):
        await call.answer()
        return
    game = games.get(chat_id)
    p = game.players.get(call.from_user.id) if game else None
    if not game or not _is_active(game) or p is None or not p.alive:
        await call.answer("Сейчас это не сыграть.", show_alert=True)
        return
    if p.action_used or p.action not in C.ACTIONS:
        await call.answer("Карта действия уже сыграна.", show_alert=True)
        return
    emoji, title, desc, when, needs_target = C.ACTIONS[p.action]
    if not _action_allowed(game, when):
        await call.answer("Эту карту можно сыграть только во время голосования."
                          if when == "vote" else "Сейчас не время.", show_alert=True)
        return
    if not needs_target:
        await call.answer(f"{title}!")
        await _play_action(call.bot, game, p, None)
        return
    targets = [o for o in _alive(game) if o.user_id != p.user_id]
    if not targets:
        await call.answer("Не на кого сыграть.", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for o in targets:
        b.button(text=o.name, callback_data=f"bkat:{chat_id}:{o.user_id}")
    b.adjust(2)
    await call.answer()
    await _send(call.bot, p.user_id, f"🃏 {emoji} <b>{title}</b> — на кого?", reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("bkat:"))
async def cb_action_target(call: CallbackQuery) -> None:
    try:
        _, chat_s, target_s = (call.data or "").split(":")
        chat_id, target_id = int(chat_s), int(target_s)
    except ValueError:
        await call.answer()
        return
    game = games.get(chat_id)
    p = game.players.get(call.from_user.id) if game else None
    target = game.players.get(target_id) if game else None
    if not game or not _is_active(game) or p is None or not p.alive or target is None or not target.alive:
        await call.answer("Сейчас это не сыграть.", show_alert=True)
        return
    if p.action_used or p.action not in C.ACTIONS:
        await call.answer("Карта действия уже сыграна.", show_alert=True)
        return
    _emoji, title, _desc, when, _t = C.ACTIONS[p.action]
    if not _action_allowed(game, when):
        await call.answer("Сейчас не время.", show_alert=True)
        return
    if p.action == "expose" and all(k in target.revealed for k in C.ATTR_KEYS):
        await call.answer("У этого игрока уже всё открыто.", show_alert=True)
        return
    await call.answer(f"{title} → {target.name}")
    try:
        if call.message:
            await call.message.edit_text(f"🃏 Сыграно: {title} → {_e(target.name)}")
    except Exception:
        pass
    await _play_action(call.bot, game, p, target)


async def _play_action(bot: Bot, game: Game, p: Player, target: Player | None) -> None:
    p.action_used = True
    action = p.action or ""
    emoji, title, *_ = C.ACTIONS[action]
    head = f"🃏 <b>{_e(p.name)}</b> играет {emoji} <b>{title}</b>"
    if action == "immunity":
        # Иммунитет всегда на СЛЕДУЮЩЕЕ голосование: нельзя дождаться голосов против и «сжечь» их
        game.immune_next.add(p.user_id)
        when = ("в голосовании следующего раунда" if game.phase == "voting"
                else "в ближайшем голосовании")
        await _send(bot, game.chat_id, f"{head}! {when.capitalize()} против него(неё) голосовать нельзя.")
    elif action == "double":
        game.doubled.add(p.user_id)
        await _send(bot, game.chat_id, f"{head}! Его(её) голос сейчас считается за два.")
    elif action == "curse" and target:
        game.cursed.add(target.user_id)
        await _send(bot, game.chat_id, f"{head} на <b>{_e(target.name)}</b> — его(её) голос не считается!")
    elif action == "expose" and target:
        hidden = [k for k in C.ATTR_KEYS if k not in target.revealed]
        await _reveal_attr(bot, game, target, random.choice(hidden), auto=False, count_turn=False,
                           prefix=f"{head} на <b>{_e(target.name)}</b>!\n\n")
    elif action == "swap" and target:
        mine, theirs = p.card.get("baggage", "—"), target.card.get("baggage", "—")
        p.card["baggage"], target.card["baggage"] = theirs, mine
        if "baggage" in p.revealed or "baggage" in target.revealed:
            p.revealed.add("baggage")
            target.revealed.add("baggage")
            detail = (f"\n🎒 {_e(p.name)}: {_e(theirs)}\n🎒 {_e(target.name)}: {_e(mine)}")
        else:
            detail = "\nЧто у кого теперь в багаже — пока тайна 🤫"
        await _send(bot, game.chat_id, f"{head} с <b>{_e(target.name)}</b>!{detail}")
        for who, new in ((p, theirs), (target, mine)):
            try:
                await _send(bot, who.user_id, f"🔁 Обмен багажом! Теперь у тебя: <b>{_e(new)}</b>")
            except Exception:
                pass
    elif action == "reroll":
        options = [h for h in C.FALLBACK["health"] if h != p.card.get("health")]
        p.card["health"] = random.choice(options)
        p.revealed.discard("health")
        await _reveal_attr(bot, game, p, "health", auto=False, count_turn=False,
                           prefix=f"{head}! Здоровье переброшено.\n\n")
    _schedule(bot, game, "board", lambda: _refresh_board(bot, game))
    _schedule(bot, game, "vote", lambda: _refresh_vote(bot, game))


# ---------- угроза ----------


def _threat_scores(game: Game) -> dict[int, float]:
    scores = {p.user_id: 0.0 for p in _alive(game)}
    for _voter, target in game.threat_votes.items():
        if target in scores:
            scores[target] += 1.0
    return scores


def _threat_text(game: Game, closed: bool = False) -> str:
    scores = _threat_scores(game)
    lines = [f"⚠️ <b>УГРОЗА: {_e(game.threat.get('title', '').upper())}</b>", HR, "",
             f"<blockquote>{_e(game.threat.get('text', ''))}</blockquote>", ""]
    lines.append("🗳 Голосование закрыто." if closed else
                 "Кого отправим разбираться? Шанс выжить у каждого свой — ведущий держит его в тайне 🤫")
    lines.append("")
    for p in _alive(game):
        votes = f" — 🗳 {_num(scores[p.user_id])}" if scores.get(p.user_id) else ""
        lines.append(f"<b>{_e(p.name)}</b>{votes}")
    if not closed:
        lines += ["", f"⏳ {THREAT_VOTE} сек. Голосуют только игроки."]
    return "\n".join(lines)


def _threat_kb(game: Game) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for p in _alive(game):
        b.button(text=f"🔦 {p.name}", callback_data=f"bk:tv:{p.user_id}")
    b.adjust(2)
    return b.as_markup()


async def _threat_chances(game: Game) -> dict[int, dict]:
    alive = _alive(game)
    res: dict = {}
    if ai.available():
        public = [{"name": p.name,
                   "known": {C.ATTR_MAP[k][0]: (p.card or {}).get(k, "") for k in C.ATTR_KEYS}}
                  for p in alive]
        try:
            res = await asyncio.wait_for(ai.threat_chances(game.threat.get("text", ""), public), AI_TIMEOUT)
        except Exception as e:
            logger.warning("AI threat chances failed: %s", e)
    out = {}
    for p in alive:
        r = res.get(p.name)
        if not r:
            known = " ".join(str((p.card or {}).get(k, "")) for k in C.ATTR_KEYS)
            chance, why = C.fallback_chance(known)
            r = {"chance": chance, "why": why}
        out[p.user_id] = r
    return out


async def _threat(bot: Bot, game: Game) -> bool:
    """Угроза посреди игры. True — отправленный погиб."""
    game.phase = "threat"
    game.threat_votes = {}
    game.advance = asyncio.Event()
    game.threat_chances = await _threat_chances(game)
    msg = await _send(bot, game.chat_id, _threat_text(game), reply_markup=_threat_kb(game))
    game.threat_msg_id = msg.message_id
    if _bots(game):
        _launch(bot, game, _bots_vote(bot, game, threat=True), fatal=False)
    try:
        await asyncio.wait_for(game.advance.wait(), timeout=THREAT_VOTE)
    except asyncio.TimeoutError:
        pass
    if not _is_active(game):
        return False

    game.phase = "counting"
    scores = _threat_scores(game)
    top = max(scores.values(), default=0)
    if top > 0:
        candidates = [u for u, sc in scores.items() if sc == top]
        note = " (жребий среди равных)" if len(candidates) > 1 else ""
    else:
        candidates = list(scores)
        note = " (никто не проголосовал — решил жребий)"
    p = game.players[random.choice(candidates)]
    chance = int(game.threat_chances.get(p.user_id, {}).get("chance", 50))
    died = random.randint(1, 100) <= chance
    async with game.edit_lock:  # после всех начатых правок — кнопки уже не вернутся
        await _edit(bot, game.chat_id, game.threat_msg_id, _threat_text(game, closed=True))

    if died:
        known_card = dict(p.card or {})  # досье погибшего и так откроется целиком
    else:  # выжившему не раскрываем скрытые карты
        known_card = {k: v for k, v in (p.card or {}).items() if k in p.revealed or k == "name"}
    story_task = _ai_task(ai.threat_outcome(game.threat.get("text", ""), known_card, chance, died,
                                            _style(game))) if ai.available() else None
    logger.info("threat chat=%s %s chance=%s died=%s", game.chat_id, p.name, chance, died)
    await _say(bot, game.chat_id,
               f"🔦 Разбираться с угрозой идёт <b>{_e(p.name)}</b>{note}.\n\n"
               f"🎲 Бросаем кости…")
    story = (await story_task if story_task else None) or ""
    voice = C.MODE_VOICE.get(game.mode, C.MODE_VOICE["classic"])

    if died:
        p.alive = False
        p.died = True
        p.out_round = game.round_no
        if game.first_out is None:
            game.first_out = p.user_id
        caption = f"☠️ <b>{_e(p.name)} не вернулся(ась)</b>"
        if story:
            room = MAX_CAPTION - len(caption) - len(voice) - 60
            caption += f"\n\n{voice}\n<blockquote expandable>{_e(_cut(story, room))}</blockquote>"
        await _send_exile_card(bot, game, p, "dead", caption)
        await _refresh_board(bot, game)
        await asyncio.sleep(HOST_DELAY if story else STEP_DELAY)
        return True

    game.hero = p.user_id
    game.heroes.add(p.user_id)
    text = f"🦸 <b>{_e(p.name)} справился(ась) и вернулся(ась)!</b>"
    if story:
        text += f"\n\n{_host(game, story)}"
    text += "\n\n🛡 Награда: иммунитет — в ближайшем голосовании против него(неё) голосовать нельзя."
    await _say(bot, game.chat_id, text, delay=HOST_DELAY if story else STEP_DELAY)
    return False


@router.callback_query(F.data.startswith("bk:tv:"))
async def cb_threat_vote(call: CallbackQuery) -> None:
    game = games.get(call.message.chat.id) if call.message else None
    if not game or game.phase != "threat":
        await call.answer("Сейчас не время.", show_alert=True)
        return
    try:
        target = int((call.data or "").split(":")[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    if target not in game.players or not game.players[target].alive:
        await call.answer("Этого игрока уже нет.", show_alert=True)
        return
    voter = game.players.get(call.from_user.id)
    if voter is None or not voter.alive:
        await call.answer("Голосуют только игроки, которые ещё в игре.", show_alert=True)
        return
    game.threat_votes[call.from_user.id] = target
    await call.answer(f"Отправляем: {game.players[target].name}")
    _schedule(call.bot, game, "threat", lambda: _refresh_threat(call.bot, game))
    if len(game.threat_votes) >= len(_alive(game)) and game.advance:
        game.advance.set()


# ---------- голосование ----------


def _pick_loser(game: Game) -> tuple[list[int], float]:
    """Кандидаты с наибольшим числом голосов (иммунные не в счёт) и это число."""
    scores = _scores(game)
    pool = {u: sc for u, sc in scores.items() if u not in game.immune} or dict(scores)
    top = max(pool.values(), default=0)
    if top <= 0:
        return list(pool), 0
    return [u for u, sc in pool.items() if sc == top], top


async def _voting(bot: Bot, game: Game) -> None:
    game.phase = "voting"
    game.current_picker = None
    game.immune, game.doubled, game.cursed = set(), set(), set()
    # иммунитет, сыгранный заранее (до этого голосования), включается сейчас и сгорает после него
    game.immune = {u for u in game.immune_next if u in game.players and game.players[u].alive}
    game.immune_next = set()
    game.speakers = {}
    if game.hero and game.hero in game.players and game.players[game.hero].alive:
        game.immune.add(game.hero)  # награда за устранённую угрозу
    game.hero = None
    game.vote_targets = {p.user_id for p in _alive(game)}

    async def wait(sec: float) -> bool:
        """Ждём sec секунд или «толчка». True — пора подводить итог."""
        game.advance.clear()
        try:
            await asyncio.wait_for(game.advance.wait(), timeout=max(0.0, sec))
        except asyncio.TimeoutError:
            pass
        return game.force_tally or not _is_active(game)

    note = ""
    main_votes: dict[int, int] = {}
    for attempt in range(MAX_REVOTES + 1):
        game.revote = attempt
        game.votes = {}
        game.force_tally = False
        game.stage = "open" if attempt == 0 else "revote"
        game.advance = asyncio.Event()
        msg = await _say(bot, game.chat_id, _vote_text(game), delay=0, reply_markup=_vote_kb(game))
        game.vote_msg_id = msg.message_id
        if _bots(game):
            _launch(bot, game, _bots_vote(bot, game), fatal=False)

        if attempt == 0:
            # Напоминание тем, у кого есть карта «для голосования»
            for p in _alive(game):
                if p.dm_ok and not p.action_used and p.action in C.ACTIONS and C.ACTIONS[p.action][3] == "vote":
                    kb = InlineKeyboardBuilder()
                    kb.row(_action_btn(game, p))
                    try:
                        await _send(bot, p.user_id, "🗳 Голосование открыто — самое время для карты действия!",
                                    reply_markup=kb.as_markup())
                    except Exception:
                        pass
            # Голосуем SPEECH_AT сек (или пока все не проголосуют). Последнее слово — только если
            # у лидеров поровну голосов; если после речей ничья осталась — переголосование между ними.
            if not await wait(SPEECH_AT):
                tied, tied_top = _pick_loser(game)
                if tied_top > 0 and len(tied) >= 2:
                    await _start_speech(bot, game, tied)
                    if game.stage == "speech" and not await wait(SPEECH_LEN):
                        await _end_speech(bot, game)
        else:
            await wait(REVOTE_TIMEOUT)  # закрывается сразу, как только все проголосовали

        if not _is_active(game):
            return
        game.stage = "closed"  # дальше голоса не принимаются и сообщение не «оживает»
        if attempt == 0:
            main_votes = dict(game.votes)  # для номинаций и званий: основное голосование раунда
        candidates, top = _pick_loser(game)
        logger.info(
            "vote chat=%s round=%s revote=%s votes=%s scores=%s immune=%s doubled=%s cursed=%s -> %s",
            game.chat_id, game.round_no, attempt,
            {game.players[v].name: game.players[t].name for v, t in game.votes.items()
             if v in game.players and t in game.players},
            {game.players[u].name: sc for u, sc in _scores(game).items()},
            [game.players[u].name for u in game.immune], [game.players[u].name for u in game.doubled],
            [game.players[u].name for u in game.cursed], [game.players[u].name for u in candidates],
        )
        async with game.edit_lock:  # после всех начатых правок — кнопки уже не вернутся
            await _edit(bot, game.chat_id, game.vote_msg_id,
                        f"🗳 <b>Голосование · раунд {game.round_no} — закрыто</b>\n\n{_bars(game)}")
        # Если больше всех набрал игрок с иммунитетом — прямо так и скажем
        scores = _scores(game)
        shielded = [u for u in game.immune if u in scores and scores[u] > top]
        if shielded:
            who = _names([_e(game.players[u].name) for u in shielded])
            note = f" — у {who} больше голосов, но 🛡 иммунитет, поэтому выбывает следующий по голосам"
        if top <= 0:
            note = " (никто не проголосовал — решил жребий)"
            break
        if len(candidates) == 1:
            break
        if attempt == MAX_REVOTES:
            note += " (снова ничья — решил жребий)"
            break
        names = _names([f"<b>{_e(game.players[u].name)}</b>" for u in candidates])
        await _send(bot, game.chat_id, f"⚖️ <b>Ничья!</b> У {names} поровну голосов.\n\n"
                                       f"Переголосование — только между ними 👇")
        game.vote_targets = set(candidates)

    if _is_active(game):
        loser = game.players[random.choice(candidates)]
        game.round_votes.append({"round": game.round_no, "main": main_votes, "final": dict(game.votes),
                                 "out": loser.user_id})
        await _tally(bot, game, loser, note)


async def _start_speech(bot: Bot, game: Game, leaders: list[int]) -> None:
    """Последнее слово для тех, у кого поровну больше всех голосов."""
    leaders = list(leaders)
    random.shuffle(leaders)
    note = ""
    game.speakers = {u: [] for u in leaders}
    game.stage = "speech"
    for u in leaders:
        if game.players[u].is_bot:
            _launch(bot, game, _bot_speech(bot, game, game.players[u]), fatal=False)
    who = _names([_mention(game.players[u]) for u in leaders])
    await _send(bot, game.chat_id,
                f"🎤 <b>ПОСЛЕДНЕЕ СЛОВО</b>\n{HR}\n\n"
                + (f"<i>{note.strip()}</i>\n\n" if note else "")
                + f"⚖️ У {who} поровну голосов — у вас <b>{SPEECH_LEN} сек</b>.\n\n"
                f"Напишите в чат, почему вас нельзя оставлять за дверью! "
                f"Остальные могут поменять голос. Если ничья останется — переголосуем только между вами.")


async def _end_speech(bot: Bot, game: Game) -> None:
    game.stage = "after"
    speeches = {game.players[u].name: " ".join(t)[:600] for u, t in game.speakers.items() if t}
    # Ведущий оценивает речи, только если высказались оба обвиняемых
    if len(speeches) >= 2 and ai.available():
        try:
            note = await asyncio.wait_for(ai.narrate_speeches(game.catastrophe, speeches, _style(game)), AI_TIMEOUT)
            if note:
                await _say(bot, game.chat_id, _host(game, note), delay=SPEECH_NOTE_DELAY)
                return
        except Exception as e:
            logger.warning("AI speech note failed: %s", e)
    if not speeches:
        await _send(bot, game.chat_id, "🤐 Обвиняемые промолчали. Подводим итог…")


def _is_speech(message: Message) -> bool:
    game = games.get(message.chat.id)
    return bool(
        game and game.phase == "voting" and game.stage == "speech" and message.from_user
        and message.from_user.id in game.speakers and message.text
        and not message.text.startswith("/")
    )


@router.message(_is_speech)
async def on_speech(message: Message) -> None:
    game = games.get(message.chat.id)
    if game and message.from_user:
        game.speakers[message.from_user.id].append((message.text or "")[:300])


@router.callback_query(F.data.startswith("bk:vote:"))
async def cb_vote(call: CallbackQuery) -> None:
    game = games.get(call.message.chat.id) if call.message else None
    if not game or game.phase != "voting":
        await call.answer("Сейчас не время голосовать.", show_alert=True)
        return
    try:
        target = int((call.data or "").split(":")[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    if target not in game.players or not game.players[target].alive:
        await call.answer("Этого игрока уже нет.", show_alert=True)
        return

    if game.stage == "closed":
        await call.answer("Голосование уже закрыто.", show_alert=True)
        return
    if target in game.immune:
        await call.answer(f"🛡 {game.players[target].name} под иммунитетом — в этом голосовании против "
                          "него(неё) голосовать нельзя.", show_alert=True)
        return
    if game.vote_targets and target not in game.vote_targets:
        await call.answer("Сейчас голосуем только между теми, у кого ничья.", show_alert=True)
        return
    voter = call.from_user.id
    player = game.players.get(voter)
    if player is None or not player.alive:
        await call.answer("Голосуют только игроки, которые ещё в игре.", show_alert=True)
        return
    game.votes[voter] = target
    await call.answer(f"Голос за: {game.players[target].name}")
    _schedule(call.bot, game, "vote", lambda: _refresh_vote(call.bot, game))

    if game.stage in ("open", "revote") and len(game.votes) >= len(_alive(game)) and game.advance:
        game.advance.set()  # все проголосовали — к речам (или к итогу переголосования)


@router.callback_query(F.data == "bk:tally")
async def cb_tally(call: CallbackQuery) -> None:
    game = games.get(call.message.chat.id) if call.message else None
    if not game or game.phase != "voting":
        await call.answer("Сейчас нечего подводить.", show_alert=True)
        return
    if call.from_user.id != game.host_id:
        await call.answer("Подвести итог может только ведущий.", show_alert=True)
        return
    if not game.votes:
        await call.answer("Ещё никто не проголосовал.", show_alert=True)
        return
    await call.answer("Подводим итог!")
    game.force_tally = True
    if game.advance:
        game.advance.set()


async def _tally(bot: Bot, game: Game, loser: Player, note: str = "") -> None:
    if not _is_active(game) or game.phase != "voting":
        return
    game.phase = "counting"
    game.stage = ""
    bars = _bars(game)
    loser.alive = False
    loser.out_round = game.round_no
    if game.first_out is None:
        game.first_out = loser.user_id

    story_task = _ai_task(ai.narrate_elimination(game.catastrophe, loser.card or {}, game.round_no,
                                                 _style(game))) if ai.available() else None
    await _say(bot, game.chat_id,
               f"{HR}\n🧮 <b>ИТОГ РАУНДА {game.round_no}</b>\n{HR}\n\n{bars}\n\n"
               f"🚪 В бункер не пускают <b>{_e(loser.name)}</b>{note}.")

    story = (await story_task if story_task else None) or ""
    voice = C.MODE_VOICE.get(game.mode, C.MODE_VOICE["classic"])
    caption = f"📂 <b>Досье изгнанного: {_e(loser.name)}</b>"
    if story:
        room = MAX_CAPTION - len(caption) - len(voice) - 60
        caption += f"\n\n{voice}\n<blockquote expandable>{_e(_cut(story, room))}</blockquote>"
    await _send_exile_card(bot, game, loser, "out", caption)
    await _refresh_board(bot, game)
    await asyncio.sleep(HOST_DELAY if story else STEP_DELAY)


# ---------- финал ----------


def _votes_against(game: Game) -> dict[int, int]:
    """Сколько голосов против каждого собрал за партию (основные голосования раундов)."""
    out: dict[int, int] = {}
    for r in game.round_votes:
        for target in r["main"].values():
            out[target] = out.get(target, 0) + 1
    return out


def _nominations(game: Game, against: dict[int, int]) -> list[tuple[str, str, str]]:
    """Номинации для титров: Мишень, Везунчик, Снайпер, Предатель, Герой."""
    name = lambda uid: game.players[uid].name if uid in game.players else "?"  # noqa: E731
    noms: list[tuple[str, str, str]] = []
    if against:
        uid, n = max(against.items(), key=lambda kv: kv[1])
        noms.append(("Мишень", name(uid), f"{n} голос(а) против за игру"))
    survivors = [p for p in _alive(game)]
    lucky = [p for p in survivors if not against.get(p.user_id)]
    if lucky and game.round_votes:
        noms.append(("Везунчик", ", ".join(p.name for p in lucky[:2]), "ни одного голоса против"))
    hits: dict[int, int] = {}
    betray: dict[int, int] = {}
    alive_ids = {p.user_id for p in survivors}
    for r in game.round_votes:
        for voter, target in r["final"].items():
            if target == r["out"]:
                hits[voter] = hits.get(voter, 0) + 1
            if target in alive_ids:
                betray[voter] = betray.get(voter, 0) + 1
    if hits:
        uid, n = max(hits.items(), key=lambda kv: kv[1])
        noms.append(("Снайпер", name(uid), f"{n} раз(а) попал(а) в изгнанного"))
    if betray:
        uid, n = max(betray.items(), key=lambda kv: kv[1])
        noms.append(("Предатель", name(uid), f"{n} раз(а) голосовал(а) против выживших"))
    for uid in game.heroes:
        noms.append(("Герой угрозы", name(uid), "справился(ась) и вернулся(ась)"))
    return noms[:5]


async def _finish(bot: Bot, game: Game) -> None:
    game.phase = "ended"
    survivors = _alive(game)
    exiled = [p for p in game.players.values() if not p.alive]
    # Вердикт ИИ пишет, пока все смотрят на пропуска выживших
    verdict_task = _ai_task(ai.final_verdict(
        game.catastrophe, game.bunker, game.bunker_known,
        # без «Связей»: там имена выбывших, а в эпилоге должны быть только выжившие
        [{k: v for k, v in (p.card or {"name": p.name}).items() if k != "relation"} for p in survivors],
        [p.name for p in exiled], _style(game),
    )) if ai.available() else None

    names = ", ".join(_e(p.name) for p in survivors)
    hatch_text = f"🚪🔒 <b>ЛЮК ЗАДРАЕН</b>\n\nВ бункере остаются:\n<b>{names}</b>"
    try:
        gif = await asyncio.to_thread(R.animate_hatch)
        await _retry(bot.send_animation, game.chat_id, BufferedInputFile(gif, filename="hatch.gif"),
                     caption=hatch_text)
        await asyncio.sleep(2)
    except Exception as e:
        logger.warning("hatch animation failed: %s", e)
        await _say(bot, game.chat_id, f"{HR}\n{hatch_text}", delay=3)

    # Альбом выживших с печатью «ДОПУЩЕН» — до эпилога
    try:
        media = []
        for i, p in enumerate(survivors):
            png = await _render(p, game, stamp="in")
            media.append(InputMediaPhoto(
                media=BufferedInputFile(png, filename=f"{p.serial}.png"),
                caption="🏚 Пропуска выживших" if i == 0 else None,
            ))
        for i in range(0, len(media), 10):
            await _retry(bot.send_media_group, game.chat_id, media[i:i + 10])
        if media:
            await asyncio.sleep(STEP_DELAY / 3)
    except Exception as e:
        logger.warning("survivor album failed: %s", e)

    verdict = await verdict_task if verdict_task else None
    if not verdict or not verdict.get("story"):
        ok = random.random() < 0.55
        verdict = {"survived": ok, "story": (
            "Год в бункере прошёл в спорах о гречке и графике дежурств по туалету.\n\n"
            + ("Когда люк открылся, снаружи их встретил новый мир — и он был не так уж плох."
               if ok else "Когда пришло время открывать люк, открывать было уже некому.")
        )}
    result = "✅ <b>КОЛОНИЯ ВЫЖИЛА!</b>" if verdict["survived"] else "☠️ <b>КОЛОНИЯ ПОГИБЛА.</b>"
    await _say(bot, game.chat_id,
               f"📖 <b>ЭПИЛОГ</b>\n{HR}\n\n{_quote(verdict['story'])}\n\n🏁 Вердикт: {result}", delay=5)

    against = _votes_against(game)
    try:
        from datetime import date
        exiled_info = [{"name": p.name, "round": p.out_round, "died": p.died}
                       for p in sorted(exiled, key=lambda x: x.out_round or 0)]
        card = await asyncio.to_thread(
            R.render_credits, game.title, [{"name": p.name, "card": p.card} for p in survivors], exiled_info,
            _nominations(game, against), bool(verdict["survived"]), game.serial, date.today().strftime("%d.%m.%Y"))
        await _send_photo(bot, game.chat_id, card, "🎬 <b>Титры партии</b>")
        await asyncio.sleep(3)
    except Exception as e:
        logger.warning("postcard failed: %s", e)

    results = [{"user_id": p.user_id, "name": p.name, "survived": p.alive,
                "first_out": p.user_id == game.first_out, "votes_against": against.get(p.user_id, 0),
                "hero": p.user_id in game.heroes, "died": p.died,
                "lucky": p.alive and not against.get(p.user_id)}
               for p in game.players.values() if not p.is_bot]

    if game.db:
        try:
            await game.db.record_bunker_game(results)
        except Exception as e:
            logger.warning("stats save failed: %s", e)

    await _refresh_board(bot, game)
    if game.board_msg_id:
        try:
            await bot.unpin_chat_message(game.chat_id, message_id=game.board_msg_id)
        except Exception:
            pass
    await _send(bot, game.chat_id,
                "🖤 <b>Игра окончена!</b> Спасибо за игру.\n\n"
                "🔁 /bunker — сыграть ещё\n🏆 /top — рейтинг выживших")
    if games.get(game.chat_id) is game:
        games.pop(game.chat_id, None)
