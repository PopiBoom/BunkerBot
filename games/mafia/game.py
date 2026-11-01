"""Игра «Мафия» для группового чата. Ведущий — Пиздюк (ИИ), без ИИ — запасные тексты.

Поток партии (вся партия — одна фоновая задача `_run_game`, кнопки лишь будят её):
  /mafia → лобби (вход через ЛС бота по кнопке, режим ведущего) → старт → роли картинками в ЛС
  (мафия знает своих) → обложка с городом и составом →
  НОЧЬ: чат закрыт — все сообщения удаляются; ходы кнопками в ЛС по очереди: Доктор лечит (себя — раз
  за игру, одного — не две ночи подряд) → Комиссар проверяет «мафия или нет» → мафия голосует за жертву
  (выбор окончательный, семья видит голоса друг друга, при равенстве решает Дон).
  Шаг кончается, как только все его роли сходили (или по таймеру); в чате — «Доктор сделал выбор» и т.п. →
  УТРО: сводка ведущего, роль убитого открывается; убитому бот пишет в ЛС — может прислать одно
  сообщение «последнего слова», бот перешлёт его в чат (игра его не ждёт) →
  ДЕНЬ: обсуждение с таймером, посреди — подкол ведущего (он видит только чат, ролей не знает) →
  тайное голосование кнопками, как в Бункере («никого» тоже можно); ничья — оправдания и
  переголосование между равными, снова ничья — никого не казнят; у казнённого — последнее слово в ЛС → …
  ФИНАЛ: мафии не меньше, чем мирных, — победа мафии; мафии нет — победа города; несколько кругов
  без смертей — ничья. Роли всех, эпилог, титры с номинациями, статистика для /mafia_top.

Мёртвые и не-игроки днём молчат (их сообщения молча удаляются); последнее слово — только через бота.
Состояние партий — в памяти процесса (одна на чат; Бункер и Мафия в одном чате одновременно не идут).
"""
from __future__ import annotations

import asyncio
import html
import logging
import random
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ai
from database import Database

from . import content as C
from . import render as R

logger = logging.getLogger("mafia")
router = Router(name="mafia")

MIN_PLAYERS = 4
MAX_PLAYERS = 16
LOBBY_TIMEOUT = 15 * 60   # брошенное лобби закрывается само
DOCTOR_TIME = 30          # сек на ход Доктора ночью (раньше — как только выбрал)
SHERIFF_TIME = 30         # сек на проверку Комиссара
MAFIA_TIME = 45           # сек мафии на выбор жертвы (и Дону — на проверку)
VOTE_TIME = 60            # сек на дневное голосование (раньше — если все проголосовали)
LAST_WORD_MAX = 500       # символов в последнем слове
SPEECH_TIME = 30          # сек на оправдания при ничьей
REVOTE_TIME = 30          # сек на переголосование между равными
MAX_IDLE = 2              # столько кругов «ночь + день» подряд без смертей — ничья
MAX_DAYS = 15             # предохранитель от бесконечной партии
READ_CPS = 18             # скорость чтения: пауза после сюжетного текста ≈ длина / READ_CPS
TIME_SCALE = 1.0          # для тестов: 0.01 — все таймеры в 100 раз быстрее
REFRESH_DELAY = 1.2       # сек: склеиваем частые правки сообщения голосования в одну
MAX_CAPTION = 1000
HR = "━━━━━━━━━━━━━━━"


def day_time(alive: int) -> int:
    """Сколько секунд обсуждать днём: больше живых — дольше."""
    return min(180, 60 + 15 * alive)


# ---------- состояние ----------


@dataclass
class Player:
    user_id: int
    name: str
    username: str | None = None
    role: str = "civ"
    alive: bool = True
    dm_ok: bool = False
    is_bot: bool = False          # тестовый бот (/mafia_bots): ходит и голосует сам
    serial: int = 0
    fate: str = ""                # "" — жив; night — убит ночью; day — казнён днём
    fate_no: int = 0
    healed_self: bool = False     # Доктор уже лечил себя
    last_heal: int | None = None  # кого Доктор лечил прошлой ночью
    checks: list[tuple[str, str]] = field(default_factory=list)  # проверки Комиссара: (имя, итог)
    night_msgs: dict[int, str] = field(default_factory=dict)     # открытые сообщения с кнопками в ЛС: id -> вид


@dataclass
class Game:
    chat_id: int
    host_id: int
    db: Database | None = None
    mode: str = "classic"
    players: dict[int, Player] = field(default_factory=dict)
    phase: str = "lobby"          # lobby | starting | night | morning | day | vote | ended
    city: str = ""
    serial: int = 0
    no: int = 0                   # номер ночи (и следующего за ней дня)
    started: float = 0.0
    bot_username: str = ""
    lobby_msg_id: int | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    # ночь
    night_step: str = ""          # чей сейчас ход ночью: doctor | sheriff | mafia
    night_event: asyncio.Event | None = None
    dealt: bool = False           # роли розданы — кнопка «Моя роль» работает
    kill_votes: dict[int, int] = field(default_factory=dict)  # мафиози -> жертва
    sheriff_target: int | None = None
    heal: int | None = None
    # день
    day_msg_id: int | None = None
    advance: asyncio.Event | None = None
    skip_day: bool = False
    votes: dict[int, int] = field(default_factory=dict)       # кто -> против кого (0 — «никого»)
    vote_targets: set[int] = field(default_factory=set)
    vote_stage: str = ""          # open | speech | revote | closed
    vote_msg_id: int | None = None
    vote_ends: float = 0.0
    last_word: set[int] = field(default_factory=set)          # мёртвые, кто ещё может прислать последнее слово
    edit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # правки сообщений чата — по очереди
    # итоги
    timeline: list[str] = field(default_factory=list)
    first_victim: int | None = None
    sheriff_checks: dict[int, int] = field(default_factory=dict)
    sheriff_hits: dict[int, int] = field(default_factory=dict)
    doc_saves: dict[int, int] = field(default_factory=dict)
    votes_right: dict[int, int] = field(default_factory=dict)  # мирный голосовал против мафии
    votes_wrong: dict[int, int] = field(default_factory=dict)  # мирный голосовал против своего
    cant_delete: bool = False
    pending: set[str] = field(default_factory=set)


games: dict[int, Game] = {}        # chat_id -> Game
_tasks: set[asyncio.Task] = set()  # держим ссылки, чтобы задачи не собрал GC


# ---------- мелочи ----------


def _e(s) -> str:
    return html.escape(str(s))


def _now() -> float:
    """Игровые часы: с TIME_SCALE < 1 (тесты) идут быстрее настоящих."""
    return time.time() / TIME_SCALE


def _alive(game: Game) -> list[Player]:
    return [p for p in game.players.values() if p.alive]


def _is_mafia(p: Player) -> bool:
    return C.team(p.role) == "mafia"


def _role_alive(game: Game, role: str) -> Player | None:
    return next((p for p in _alive(game) if p.role == role), None)


def _is_active(game: Game) -> bool:
    return games.get(game.chat_id) is game and game.phase != "ended"


def _style(game: Game) -> str:
    return C.MODES.get(game.mode, C.MODES["classic"])[1]


def _mode_title(game: Game) -> str:
    return C.MODES.get(game.mode, C.MODES["classic"])[0]


def _mode_key(chat_id: int) -> str:
    return f"mafia_mode:{chat_id}"


def _mention(p: Player) -> str:
    if p.is_bot:
        return f"<b>{_e(p.name)}</b>"
    if p.username:
        return f"@{p.username}"
    return f'<a href="tg://user?id={p.user_id}">{_e(p.name)}</a>'


def _names(names: list[str], last: str = " и ") -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + last + names[-1]


_SENT = re.compile(r"(?<=[.!?…])\s+(?=[А-ЯЁA-Z«\"—–-])")


def _paras(text: str) -> str:
    """Текст ведущего абзацами: если ИИ написал сплошняком — бьём по 2 предложения."""
    text = re.sub(r"\n{3,}", "\n\n", str(text).strip())
    if "\n" in text:
        return text
    sents = [x for x in _SENT.split(text) if x.strip()]
    if len(sents) < 3 and len(text) < 220:
        return text
    return "\n\n".join(" ".join(sents[i:i + 2]) for i in range(0, len(sents), 2))


def _host(game: Game, text: str) -> str:
    return f"<blockquote>{_e(_paras(text))}</blockquote>"


def _bar(value: float, top: float, width: int = 8) -> str:
    filled = 0 if top <= 0 else round(width * value / top)
    if value > 0:
        filled = max(1, filled)
    return "▓" * filled + "░" * (width - filled)


def _fate(p: Player) -> str:
    if p.fate == "night":
        return f"убит ночью {p.fate_no}"
    if p.fate == "day":
        return f"казнён днём {p.fate_no}"
    return "выжил"


def _clock(sec: float) -> str:
    sec = max(0, int(round(sec)))
    return f"{sec // 60}:{sec % 60:02d}" if sec >= 60 else f"{sec} сек"


def _read(text: str) -> float:
    """Сколько дать на чтение сюжетного текста."""
    return min(25.0, 3 + len(text) / READ_CPS)


def _end_game(game: Game) -> None:
    game.phase = "ended"
    if games.get(game.chat_id) is game:
        games.pop(game.chat_id, None)
    for ev in (game.stop, game.night_event, game.advance):
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
            logger.exception("mafia task crashed in chat %s", game.chat_id)
            if fatal and _is_active(game):
                _end_game(game)
                try:
                    await _send(bot, game.chat_id, "💥 Ведущего самого замочили… Игра прервана.\n/mafia — начать заново.")
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


async def _wait(ev: asyncio.Event | None, sec: float) -> bool:
    """Ждём sec игровых секунд или события. True — разбудили."""
    if sec <= 0:
        return bool(ev and ev.is_set())
    if ev is None:
        await asyncio.sleep(sec * TIME_SCALE)
        return False
    try:
        await asyncio.wait_for(ev.wait(), timeout=sec * TIME_SCALE)
        return True
    except asyncio.TimeoutError:
        return False


async def _pause(game: Game, sec: float) -> None:
    """Пауза, которая обрывается, если партию остановили."""
    await _wait(game.stop, sec)


# ---------- отправка с учётом флуд-лимита ----------


async def _retry(call, *args, **kwargs):
    attempts = 4
    for attempt in range(attempts):
        try:
            return await call(*args, **kwargs)
        except TelegramRetryAfter as e:
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(e.retry_after + 0.5)
    raise RuntimeError("unreachable")


async def _send(bot: Bot, chat_id: int, text: str, **kwargs) -> Message:
    return await _retry(bot.send_message, chat_id, text, **kwargs)


async def _say(bot: Bot, game: Game, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> Message:
    """Сюжетное сообщение в чат и пауза, чтобы все дочитали."""
    msg = await _send(bot, game.chat_id, text, reply_markup=reply_markup)
    await _pause(game, _read(text))
    return msg


async def _edit(bot: Bot, chat_id: int, message_id: int | None, text: str,
                reply_markup: InlineKeyboardMarkup | None = None, retry: bool = True) -> bool:
    """retry=False — для необязательных правок (таймер, счётчик голосов): при флуд-лимите просто пропускаем."""
    if message_id is None:
        return False
    try:
        if retry:
            await _retry(bot.edit_message_text, text, chat_id=chat_id, message_id=message_id,
                         reply_markup=reply_markup)
        else:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup)
        return True
    except TelegramBadRequest as e:
        return "not modified" in str(e)
    except Exception as e:
        logger.debug("edit failed: %s", e)
        return False


async def _chat_edit(bot: Bot, game: Game, message_id: int | None, text, reply_markup=None,
                     ok=None, retry: bool = True) -> None:
    """Правка сообщения в чате под замком партии: правки идут строго по очереди, и устаревшая
    (например, голосование с кнопками) не может лечь поверх закрытого. ok() проверяется уже под замком."""
    async with game.edit_lock:
        if ok is not None and not ok():
            return
        await _edit(bot, game.chat_id, message_id, text() if callable(text) else text,
                    reply_markup() if callable(reply_markup) else reply_markup, retry=retry)


async def _dm(bot: Bot, p: Player, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> Message | None:
    if p.is_bot:
        return None
    try:
        msg = await _send(bot, p.user_id, text, reply_markup=reply_markup)
        p.dm_ok = True
        return msg
    except Exception as e:
        logger.info("mafia DM to %s failed: %s", p.user_id, e)
        p.dm_ok = False
        return None


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _open_bot(game: Game, text: str = "Открыть бота") -> InlineKeyboardButton | None:
    return InlineKeyboardButton(text=text, url=f"https://t.me/{game.bot_username}") if game.bot_username else None


async def _dossier(game: Game, p: Player) -> str:
    """Досье игрока из памяти стёб-бота — для одной детали в истории ведущего (кроме «забытых»)."""
    if p.is_bot or not game.db:
        return ""
    try:
        if await game.db.is_optout(game.chat_id, p.user_id):
            return ""
        return await game.db.get_dossier(game.chat_id, p.user_id)
    except Exception:
        return ""


async def _ai(coro, what: str):
    try:
        return await coro
    except Exception as e:
        logger.warning("mafia AI %s failed: %s", what, e)
        return None


# ---------- кого заглушить: ночь, мёртвые, не-игроки ----------


def must_delete(message: Message) -> bool:
    """Сообщение в чате, где идёт Мафия, которое надо удалить: ночью — любое, днём — мёртвых и не-игроков."""
    game = games.get(message.chat.id)
    if not game or game.phase in ("lobby", "ended") or game.cant_delete:
        return False
    if message.chat.type not in ("group", "supergroup"):
        return False
    if message.is_automatic_forward or message.sender_chat:  # посты канала и анонимные админы
        return False
    u = message.from_user
    if u is None or u.is_bot:
        return False
    if game.phase == "night":
        return True
    p = game.players.get(u.id)
    return not (p and p.alive)


# ---------- лобби ----------


def _lobby_text(game: Game) -> str:
    n = len(game.players)
    lines = ["🕵️ <b>МАФИЯ</b>", HR, "",
             f"Игроков: <b>{n}</b> (нужно от {MIN_PLAYERS})",
             f"Ведущий: {_mode_title(game)}", ""]
    for p in game.players.values():
        host = " (ведущий)" if p.user_id == game.host_id else ""
        dm = " — <i>не открыл бота</i>" if not (p.dm_ok or p.is_bot) else ""
        lines.append(f"• {_e(p.name)}{host}{dm}")
    if n >= MIN_PLAYERS:
        lines += ["", "<b>Состав</b>", C.composition_list(C.deal_roles(n))]
    lines += ["", "Жми «Вступить в игру» — откроется бот, нажми Start. Роль придёт в личку."]
    return "\n".join(lines)


def _lobby_kb(game: Game) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if game.bot_username:
        b.row(InlineKeyboardButton(text="Вступить в игру",
                                   url=f"https://t.me/{game.bot_username}?start=mf_{game.chat_id}"))
    b.row(_btn(f"Ведущий: {_mode_title(game)}", "mf:mode"), _btn("Выйти", "mf:leave"))
    b.row(_btn("Начать ▶️", "mf:start"), _btn("Отмена", "mf:cancel"))
    return b.as_markup()


def _unique_name(game: Game, raw: str | None) -> str:
    base = (raw or "").strip()[:32] or "Игрок"
    taken = {p.name for p in game.players.values()}
    if base not in taken:
        return base
    n = 2
    while f"{base} {n}" in taken:
        n += 1
    return f"{base} {n}"


async def _refresh_lobby(bot: Bot, game: Game) -> None:
    await _edit(bot, game.chat_id, game.lobby_msg_id, _lobby_text(game), _lobby_kb(game))


@router.message(CommandStart(deep_link=True, magic=F.args.func(lambda a: bool(a) and a.startswith("mf_"))))
async def cmd_deeplink_join(message: Message, command: CommandObject) -> None:
    """Вход в игру по кнопке-ссылке: /start mf_<chat_id> в личке бота."""
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
        await message.answer("Игра уже началась или не найдена 🙁\nПусть ведущий соберёт новую: /mafia в чате.")
        return
    if user.id in game.players:
        game.players[user.id].dm_ok = True
        await message.answer("Ты уже в игре. Жди старта — роль придёт сюда.")
        await _refresh_lobby(message.bot, game)
        return
    if len(game.players) >= MAX_PLAYERS:
        await message.answer("Увы, мест в лобби больше нет.")
        return
    player = Player(user_id=user.id, name=_unique_name(game, user.first_name), username=user.username, dm_ok=True)
    game.players[user.id] = player
    await message.answer("Ты в игре «Мафия». Когда ведущий начнёт — сюда придёт твоя роль, а ночью — кнопки "
                         "для хода.")
    try:
        await _send(message.bot, chat_id, f"<b>{_e(player.name)}</b> в игре.")
    except Exception:
        pass
    await _refresh_lobby(message.bot, game)


@router.message(Command("mafia"))
async def cmd_mafia(message: Message, command: CommandObject, db: Database) -> None:
    user = message.from_user
    if user is None:
        return
    if message.chat.type == "private":
        await message.answer("В Мафию играют в групповом чате: напиши там /mafia.")
        return
    chat_id = message.chat.id
    from games import active

    busy = active(chat_id)
    if busy:
        await message.answer("В этом чате уже идёт " + ("Мафия. Доиграйте или /mafia_stop." if busy == "mafia"
                                                         else "Бункер. Доиграйте или /bunker_stop."))
        return
    game = Game(chat_id=chat_id, host_id=user.id, db=db)
    saved = await db.get_setting(_mode_key(chat_id), "classic")
    game.mode = saved if saved in C.MODES else "classic"
    arg = (command.args or "").strip().lower()
    if arg in C.MODE_ALIASES:
        game.mode = C.MODE_ALIASES[arg]
        await db.set_setting(_mode_key(chat_id), game.mode)
    try:
        game.bot_username = (await message.bot.me()).username or ""
    except Exception:
        game.bot_username = ""
    game.players[user.id] = Player(user_id=user.id, name=_unique_name(game, user.first_name), username=user.username)
    games[chat_id] = game
    sent = await message.answer(_lobby_text(game), reply_markup=_lobby_kb(game))
    game.lobby_msg_id = sent.message_id
    _launch(message.bot, game, _lobby_timer(message.bot, game), fatal=False)


async def _lobby_timer(bot: Bot, game: Game) -> None:
    await asyncio.sleep(LOBBY_TIMEOUT)
    if games.get(game.chat_id) is not game or game.phase != "lobby":
        return
    _end_game(game)
    await _edit(bot, game.chat_id, game.lobby_msg_id, "⌛ Лобби Мафии закрыто — игра так и не началась.\n"
                                                      "/mafia — собрать заново.")


@router.message(Command("mafia_bots"))
async def cmd_mafia_bots(message: Message, command: CommandObject) -> None:
    """Добавить в лобби тестовых ботов: /mafia_bots [сколько, по умолчанию 4]."""
    game = games.get(message.chat.id)
    if not game or game.phase != "lobby":
        await message.answer("Сначала соберите лобби: /mafia")
        return
    try:
        n = max(1, min(8, int((command.args or "4").strip())))
    except ValueError:
        n = 4
    existing = sum(1 for p in game.players.values() if p.is_bot)
    added = []
    for i in range(n):
        if len(game.players) >= MAX_PLAYERS:
            break
        uid = -(existing + i + 1)  # отрицательные id — точно не настоящие люди
        name = _unique_name(game, C.BOT_NAMES[(existing + i) % len(C.BOT_NAMES)])
        game.players[uid] = Player(user_id=uid, name=name, is_bot=True)
        added.append(name)
    await message.answer("➕ В город приехали тестовые боты: " + ", ".join(_e(a) for a in added))
    await _refresh_lobby(message.bot, game)


@router.message(Command("mafia_stop"))
async def cmd_mafia_stop(message: Message) -> None:
    game = games.get(message.chat.id)
    if not game:
        await message.answer("Сейчас Мафия не идёт.")
        return
    _end_game(game)
    await message.answer("🛑 Мафия остановлена. Чат снова открыт.")


@router.message(Command("mafia_top"))
async def cmd_mafia_top(message: Message, command: CommandObject, db: Database) -> None:
    """Рейтинг по победам со званиями; ответом на сообщение или /mafia_top @ник — профиль игрока."""
    rt = message.reply_to_message
    arg = (command.args or "").strip()
    if rt and rt.from_user and not rt.from_user.is_bot:
        await _profile(message, db, rt.from_user.id, rt.from_user.first_name or "Игрок")
        return
    if arg.startswith("@"):
        found = await db.find_user(message.chat.id, arg.split()[0])
        if not found:
            await message.answer("Не знаю такого. Пусть сначала что-нибудь напишет в чат.")
            return
        await _profile(message, db, found[0], found[1] or arg)
        return
    rows = await db.mafia_top(10)
    if not rows:
        await message.answer("🏆 Рейтинг Мафии пуст — сыграйте первую партию: /mafia")
        return
    medals = ["🥇", "🥈", "🥉"]
    blocks = [f"🕵️ <b>РЕЙТИНГ МАФИИ</b>\n{HR}"]
    for i, r in enumerate(rows):
        place = medals[i] if i < 3 else f"<b>{i + 1}.</b>"
        rate = r["wins"] / r["games"] if r["games"] else 0
        bar = "▰" * round(rate * 10) + "▱" * (10 - round(rate * 10))
        ttl = C.titles(r)
        blocks.append(
            f"{place} <b>{_e(r['name'])}</b>"
            + (f"\n🏅 {', '.join(f'{e} {n}' for e, n, _w in ttl[:3])}" if ttl else "")
            + f"\n🏆 Побед: <b>{r['wins']}</b> из {r['games']} · 🔫 за мафию: {r['mafia_wins']} · 🏙 за город: "
              f"{r['town_wins']}\n<code>{bar}</code> {round(rate * 100)}%"
        )
    blocks.append(f"{HR}\n👤 Профиль: ответь /mafia_top на сообщение игрока или /mafia_top @ник\n🔁 Сыграть: /mafia")
    await message.answer("\n\n".join(blocks))


async def _profile(message: Message, db: Database, user_id: int, name: str) -> None:
    prof = await db.mafia_profile(user_id)
    if not prof or not prof["games"]:
        await message.answer(f"🕵️ {_e(name)} ещё не играл в Мафию. /mafia — исправить.")
        return
    g = prof["games"]
    ttl = C.titles(prof)
    lines = [f"👤 <b>{_e(prof['name'] or name)}</b> · МАФИЯ", HR, "",
             f"🏆 Место в рейтинге: <b>{prof['place']}</b> · побед: <b>{prof['wins']}</b> из {g} "
             f"({round(100 * prof['wins'] / g)}%)",
             f"🔫 За мафию: {prof['mafia_games']} игр, {prof['mafia_wins']} побед · 🏙 побед за город: "
             f"{prof['town_wins']}",
             f"🧱 Дожил до конца: <b>{prof['survived']}</b> · ☠️ убит ночью: {prof['killed']} · "
             f"⚖️ казнён: {prof['executed']}",
             f"🎯 Убит первой ночью: {prof['first_victim']}",
             f"⭐ Комиссаром: {prof['sheriff_hits']} мафий из {prof['sheriff_checks']} проверок · "
             f"💉 Доктором спас: {prof['doc_saves']}",
             f"🗳 Днём (за город): против мафии {prof['votes_right']}, против своих {prof['votes_wrong']}"]
    if ttl:
        lines += ["", "🏅 <b>Звания</b>"] + [f"{e} <b>{n}</b> — {w}" for e, n, w in ttl]
    else:
        lines += ["", "🏅 Званий пока нет — играй ещё."]
    await message.answer("\n".join(lines))


def _lobby_game(call: CallbackQuery) -> Game | None:
    game = games.get(call.message.chat.id) if call.message else None
    return game if game and game.phase == "lobby" else None


@router.callback_query(F.data == "mf:mode")
async def cb_mode(call: CallbackQuery) -> None:
    game = _lobby_game(call)
    if not game:
        await call.answer("Сейчас нельзя.", show_alert=True)
        return
    if call.from_user.id != game.host_id:
        await call.answer("Режим ведущего выбирает тот, кто собрал игру.", show_alert=True)
        return
    i = C.MODE_ORDER.index(game.mode) if game.mode in C.MODE_ORDER else 0
    game.mode = C.MODE_ORDER[(i + 1) % len(C.MODE_ORDER)]
    if game.db:
        await game.db.set_setting(_mode_key(game.chat_id), game.mode)
    await call.answer(f"Ведущий: {_mode_title(game)}")
    await _refresh_lobby(call.bot, game)


@router.callback_query(F.data == "mf:leave")
async def cb_leave(call: CallbackQuery) -> None:
    game = _lobby_game(call)
    if not game:
        await call.answer("Сейчас нельзя выйти.", show_alert=True)
        return
    uid = call.from_user.id
    if uid not in game.players:
        await call.answer("Тебя и так нет в игре.")
        return
    if uid == game.host_id:
        await call.answer("Ведущий не может выйти — нажми «Отмена».", show_alert=True)
        return
    game.players.pop(uid, None)
    await call.answer("Ты вышел из игры.")
    await _refresh_lobby(call.bot, game)


@router.callback_query(F.data == "mf:cancel")
async def cb_cancel(call: CallbackQuery) -> None:
    game = _lobby_game(call)
    if not game:
        await call.answer("Игра уже идёт — /mafia_stop.", show_alert=True)
        return
    if call.from_user.id != game.host_id:
        await call.answer("Отменить может только ведущий.", show_alert=True)
        return
    _end_game(game)
    await call.answer("Игра отменена.")
    if call.message:
        await call.message.edit_text("❌ Мафия отменена.")


@router.callback_query(F.data == "mf:start")
async def cb_start(call: CallbackQuery) -> None:
    game = _lobby_game(call)
    if not game:
        await call.answer("Сейчас нельзя начать.", show_alert=True)
        return
    if call.from_user.id != game.host_id:
        await call.answer("Начать может только ведущий.", show_alert=True)
        return
    if len(game.players) < MIN_PLAYERS:
        await call.answer(f"Нужно минимум {MIN_PLAYERS} игрока.", show_alert=True)
        return
    # Роль приходит в личку — без неё играть нельзя. Проверяем, кому бот может написать.
    missing = []
    for p in game.players.values():
        if p.is_bot or p.dm_ok:
            continue
        try:
            await call.bot.send_chat_action(p.user_id, "typing")
            p.dm_ok = True
        except Exception:
            missing.append(p.name)
    if missing:
        await call.answer(f"Не могу написать в личку: {', '.join(missing)}. Пусть нажмут «Вступить в игру» "
                          "и Start в боте."[:195], show_alert=True)
        await _refresh_lobby(call.bot, game)
        return
    await call.answer("Поехали!")
    game.phase = "starting"
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    _launch(call.bot, game, _run_game(call.bot, game))


# ---------- «моя роль» и пропуск обсуждения ----------


def _role_alert(game: Game, p: Player) -> str:
    s = f"Ты — {C.title(p.role)}" + ("" if p.alive else " (ты мёртв)")
    if _is_mafia(p):
        fam = [f"{o.name} ({C.title(o.role)}{'' if o.alive else ', †'})"
               for o in game.players.values() if o is not p and _is_mafia(o)]
        if fam:
            s += "\nСемья: " + ", ".join(fam)
    if p.checks:
        s += "\nПроверки: " + "; ".join(f"{n} — {r}" for n, r in p.checks)
    return s[:195]


@router.callback_query(F.data == "mf:role")
async def cb_role(call: CallbackQuery) -> None:
    game = games.get(call.message.chat.id) if call.message else None
    p = game.players.get(call.from_user.id) if game else None
    if not game or p is None:
        await call.answer("Ты не в этой игре.", show_alert=True)
        return
    if not game.dealt:
        await call.answer("Роли ещё раздаются — секунду.", show_alert=True)
        return
    await call.answer(_role_alert(game, p), show_alert=True)


@router.callback_query(F.data == "mf:skip")
async def cb_skip(call: CallbackQuery, config=None) -> None:
    game = games.get(call.message.chat.id) if call.message else None
    if not game or game.phase != "day":
        await call.answer("Сейчас нечего пропускать.", show_alert=True)
        return
    uid = call.from_user.id
    if uid != game.host_id and not (config and config.is_admin(uid)):
        await call.answer("Перейти к голосованию может только ведущий.", show_alert=True)
        return
    await call.answer("К голосованию!")
    game.skip_day = True
    if game.advance:
        game.advance.set()


# ---------- партия ----------


def _winner(game: Game) -> str | None:
    alive = _alive(game)
    m = sum(1 for p in alive if _is_mafia(p))
    if m == 0:
        return "town"
    if m >= len(alive) - m:
        return "mafia"
    return None


async def _check_rights(bot: Bot, game: Game) -> None:
    """Без права удалять сообщения ночь не закрыть — скажем об этом сразу."""
    try:
        me = await bot.me()
        member = await bot.get_chat_member(game.chat_id, me.id)
        ok = member.status == "creator" or bool(getattr(member, "can_delete_messages", False))
    except Exception as e:
        logger.info("rights check failed: %s", e)
        return
    if not ok:
        game.cant_delete = True
        await _send(bot, game.chat_id, "⚠️ У меня нет права удалять сообщения — ночью чат не закроется, а мёртвые "
                                       "смогут болтать. Дайте право «Удаление сообщений» и начните заново, "
                                       "если хотите по-честному.")


def _role_caption(game: Game, p: Player) -> str:
    name, _emo, tm, desc = C.ROLES[p.role]
    goal = C.GOALS[tm]
    lines = [f"<b>Твоя роль: {name}</b>", "", _e(desc), "", f"Цель: {_e(goal[0].lower() + goal[1:])}"]
    if tm == "mafia":
        fam = [f"{o.name} ({C.title(o.role)})" for o in game.players.values() if o is not p and _is_mafia(o)]
        lines += ["", "Семья: " + (_e(", ".join(fam)) if fam else "ты один")]
    if p.role != "civ":
        lines += ["", "Ночью сюда придут кнопки для хода."]
    return "\n".join(lines)


async def _send_role(bot: Bot, game: Game, p: Player) -> bool:
    caption = _role_caption(game, p)
    family = ([f"{o.name} ({C.title(o.role)})" for o in game.players.values() if o is not p and _is_mafia(o)]
              if _is_mafia(p) else None)
    try:
        png = await asyncio.to_thread(R.render_role_card, p.name, p.role, game.city, p.serial, family)
        await _retry(bot.send_photo, p.user_id, BufferedInputFile(png, filename="role.png"), caption=caption)
        p.dm_ok = True
        return True
    except Exception as e:
        logger.info("role card to %s failed: %s", p.user_id, e)
    return await _dm(bot, p, caption) is not None


async def _run_game(bot: Bot, game: Game) -> None:
    ai.current_game.set(f"mafia:{game.chat_id}:{int(time.time())}")  # метка для учёта расходов на ИИ
    game.started = time.time()
    players = list(game.players.values())
    roles = C.deal_roles(len(players))
    random.shuffle(roles)
    for p, role in zip(players, roles, strict=True):
        p.role = role
        p.serial = random.randint(1, 9999)
    game.serial = random.randint(1, 9999)
    comp = C.composition(roles)
    logger.info("mafia start chat=%s roles=%s", game.chat_id, {p.name: p.role for p in players})

    game.dealt = True
    status = await _send(bot, game.chat_id, "Раздаю роли…")
    await _check_rights(bot, game)
    intro = await _ai(ai.mafia_intro([p.name for p in players], comp, _style(game)), "intro") \
        if ai.available() else None
    game.city = (intro or {}).get("city") or random.choice(C.CITIES)
    intro_text = (intro or {}).get("text") or random.choice(C.INTRO).format(city=game.city)

    no_dm = [p for p in players if not p.is_bot and not await _send_role(bot, game, p)]
    try:
        await status.delete()
    except Exception:
        pass

    mafia_n = sum(1 for r in roles if C.team(r) == "mafia")
    caption = f"🕵️ <b>{_e(game.city)}</b>\n\n<b>Состав</b>\n{C.composition_list(roles)}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[_btn("Моя роль", "mf:role")]])
    try:
        png = await asyncio.to_thread(R.render_cover, game.city, intro_text, len(players), mafia_n, game.serial)
        await _retry(bot.send_photo, game.chat_id, BufferedInputFile(png, filename="mafia.png"), caption=caption,
                     reply_markup=kb)
    except Exception as e:
        logger.warning("mafia cover failed: %s", e)
        await _send(bot, game.chat_id, f"{caption}\n\n{_host(game, intro_text)}", reply_markup=kb)
    await _pause(game, _read(intro_text))
    if no_dm and _is_active(game):
        rows = [[b] for b in (_open_bot(game),) if b] + [[_btn("Моя роль", "mf:role")]]
        await _send(bot, game.chat_id,
                    f"{', '.join(_mention(p) for p in no_dm)}: не могу написать вам в личку. Откройте бота и "
                    "нажмите Start, иначе ночью не сможете ходить.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    idle = 0
    while _is_active(game):
        dead_before = sum(1 for p in players if not p.alive)
        await _night(bot, game)
        if not _is_active(game):
            return
        await _morning(bot, game)
        if not _is_active(game):
            return
        if w := _winner(game):
            await _finish(bot, game, w)
            return
        await _day(bot, game)
        if not _is_active(game):
            return
        if w := _winner(game):
            await _finish(bot, game, w)
            return
        idle = 0 if sum(1 for p in players if not p.alive) > dead_before else idle + 1
        if idle >= MAX_IDLE or game.no >= MAX_DAYS:
            await _finish(bot, game, "draw")
            return


# ---------- ночь: по очереди Доктор → Комиссар → мафия ----------

STEP_ROLES = {"doctor": {"doctor"}, "sheriff": {"sheriff"}, "mafia": {"mafia", "don"}}
STEP_TEXT = {  # (проснулись, сходили, не сходили)
    "doctor": ("Просыпается Доктор…", "Доктор сделал выбор.", "Доктор проспал."),
    "sheriff": ("Просыпается Комиссар…", "Комиссар провёл проверку.", "Комиссар проспал."),
    "mafia": ("Просыпается мафия…", "Мафия выбрала жертву.", "Мафия так и не договорилась."),
}


def _step_time(step: str) -> int:
    return {"doctor": DOCTOR_TIME, "sheriff": SHERIFF_TIME, "mafia": MAFIA_TIME}[step]


def _night_kb(game: Game) -> InlineKeyboardMarkup:
    row = [b for b in (_open_bot(game),) if b] + [_btn("Моя роль", "mf:role")]
    return InlineKeyboardMarkup(inline_keyboard=[row])


def _targets_kb(game: Game, prefix: str, targets: list[Player]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for o in targets:
        b.button(text=o.name, callback_data=f"{prefix}:{game.chat_id}:{game.no}:{o.user_id}")
    b.adjust(2)
    return b.as_markup()


def _kill_targets(game: Game) -> list[Player]:
    return [o for o in _alive(game) if not _is_mafia(o)]


def _heal_targets(game: Game, doc: Player) -> list[Player]:
    return [o for o in _alive(game) if o.user_id != doc.last_heal and (o is not doc or not doc.healed_self)]


def _kill_text(game: Game, p: Player, over: bool = False) -> str:
    fam = [o for o in _alive(game) if _is_mafia(o) and o is not p]
    lines = [f"<b>Ночь {game.no} — кого убираем?</b>"]
    if fam:
        lines.append("Семья: " + ", ".join(_e(o.name) + (" (Дон)" if o.role == "don" else "") for o in fam))
        if _role_alive(game, "don"):
            lines.append("При равенстве голосов решает Дон.")
    if game.kill_votes:
        lines.append("")
        lines += [f"{'Ты' if v == p.user_id else _e(game.players[v].name)} → {_e(game.players[t].name)}"
                  for v, t in game.kill_votes.items()]
    if over and p.user_id not in game.kill_votes:
        lines += ["", "Время вышло — ход пропущен."]
    return "\n".join(lines)


async def _refresh_kill(bot: Bot, game: Game) -> None:
    """Голоса мафии видны всей семье. Кто уже выбрал — без кнопок: выбор окончательный."""
    if game.phase != "night" or game.night_step != "mafia":
        return
    for o in _alive(game):
        if o.is_bot or not _is_mafia(o):
            continue
        kb = None if o.user_id in game.kill_votes else _targets_kb(game, "mfk", _kill_targets(game))
        for mid, kind in list(o.night_msgs.items()):
            if kind == "kill":
                await _edit(bot, o.user_id, mid, _kill_text(game, o), kb)


async def _step_actions(bot: Bot, game: Game, p: Player, step: str) -> None:
    """Кнопки ночного хода в ЛС — только для текущего шага."""
    async def send(kind: str, text: str, prefix: str, targets: list[Player]) -> None:
        if not targets:
            return
        msg = await _dm(bot, p, text, _targets_kb(game, prefix, targets))
        if msg:
            p.night_msgs[msg.message_id] = kind

    if step == "mafia":
        await send("kill", _kill_text(game, p), "mfk", _kill_targets(game))
    elif step == "sheriff":
        await send("sheriff", f"<b>Ночь {game.no} — кого проверяем?</b>\nУзнаешь сразу: мафия или нет.",
                   "mfs", [o for o in _alive(game) if o is not p])
    elif step == "doctor":
        notes = []
        if p.healed_self:
            notes.append("себя ты уже лечил")
        if p.last_heal is not None and p.last_heal in game.players:
            notes.append(f"{game.players[p.last_heal].name} — лечил прошлой ночью")
        await send("heal", f"<b>Ночь {game.no} — кого лечим?</b>\nЕсли мафия придёт за ним, он выживет."
                   + (f"\n<i>Нельзя: {_e('; '.join(notes))}</i>" if notes else ""), "mfh", _heal_targets(game, p))


def _step_done(game: Game, step: str) -> bool:
    for p in _alive(game):
        if step == "mafia":
            if _is_mafia(p) and p.user_id not in game.kill_votes and _kill_targets(game):
                return False
        elif step == "sheriff" and p.role == "sheriff" and game.sheriff_target is None:
            return False
        elif step == "doctor" and p.role == "doctor" and game.heal is None and _heal_targets(game, p):
            return False
    return True


def _check_done(game: Game) -> None:
    if game.night_step and _step_done(game, game.night_step) and game.night_event:
        game.night_event.set()


async def _night_step(bot: Bot, game: Game, step: str) -> None:
    """Один шаг ночи: роль просыпается, ходит в ЛС, в чате — «сделал выбор»."""
    actors = [p for p in _alive(game) if p.role in STEP_ROLES[step]]
    if not actors:
        return
    game.night_step = step
    game.night_event = asyncio.Event()
    wake, done, missed = STEP_TEXT[step]
    msg = await _send(bot, game.chat_id, wake)
    for p in actors:
        if not p.is_bot:
            await _step_actions(bot, game, p, step)
    if any(p.is_bot for p in actors):
        _launch(bot, game, _bots_step(bot, game, step), fatal=False)
    if not _step_done(game, step):
        await _wait(game.night_event, _step_time(step))
    if not _is_active(game):
        return
    acted = {"doctor": game.heal is not None, "sheriff": game.sheriff_target is not None,
             "mafia": bool(game.kill_votes)}[step]
    game.night_step = ""  # кнопки этого шага больше не работают
    await _chat_edit(bot, game, msg.message_id, done if acted else missed)
    for p in actors:  # закрываем кнопки шага в ЛС
        for mid, kind in list(p.night_msgs.items()):
            await _edit(bot, p.user_id, mid,
                        _kill_text(game, p, over=True) if kind == "kill" else "Время вышло — ход пропущен.")
        p.night_msgs = {}
    await _pause(game, 1.5)


async def _night(bot: Bot, game: Game) -> None:
    game.no += 1
    game.phase = "night"
    game.kill_votes, game.sheriff_target, game.heal = {}, None, None
    game.night_step = ""
    for p in game.players.values():
        p.night_msgs = {}
    await _send(bot, game.chat_id, f"🌙 <b>НОЧЬ {game.no}</b>\n{HR}\n\nГород засыпает.",
                reply_markup=_night_kb(game))
    await _pause(game, 2)
    for step in ("doctor", "sheriff", "mafia"):
        if not _is_active(game):
            return
        await _night_step(bot, game, step)
    if _is_active(game):
        game.phase = "morning"


async def _night_call(call: CallbackQuery, step: str) -> tuple[Game, Player, Player] | None:
    """Общая проверка ночной кнопки: та ночь и тот шаг, живой игрок с нужной ролью, живая цель."""
    try:
        _, chat_s, no_s, target_s = (call.data or "").split(":")
        chat_id, no, target_id = int(chat_s), int(no_s), int(target_s)
    except ValueError:
        await call.answer()
        return None
    game = games.get(chat_id)
    if not game or game.phase != "night" or game.no != no:
        await call.answer("Эта ночь уже прошла.", show_alert=True)
        return None
    p = game.players.get(call.from_user.id)
    if p is None or not p.alive or p.role not in STEP_ROLES[step]:
        await call.answer("Это не твой ход.", show_alert=True)
        return None
    if game.night_step != step:
        await call.answer("Время на ход вышло.", show_alert=True)
        return None
    target = game.players.get(target_id)
    if target is None or not target.alive:
        await call.answer("Этого игрока уже нет.", show_alert=True)
        return None
    return game, p, target


@router.callback_query(F.data.startswith("mfk:"))
async def cb_kill(call: CallbackQuery) -> None:
    ctx = await _night_call(call, "mafia")
    if not ctx:
        return
    game, p, target = ctx
    if p.user_id in game.kill_votes:
        await call.answer("Ты уже выбрал.", show_alert=True)
        return
    if _is_mafia(target):
        await call.answer("Своих не трогаем.", show_alert=True)
        return
    game.kill_votes[p.user_id] = target.user_id
    await call.answer(f"Твой выбор: {target.name}")
    await _refresh_kill(call.bot, game)
    _check_done(game)


@router.callback_query(F.data.startswith("mfs:"))
async def cb_sheriff_check(call: CallbackQuery) -> None:
    ctx = await _night_call(call, "sheriff")
    if not ctx:
        return
    game, p, target = ctx
    if game.sheriff_target is not None:
        await call.answer("Этой ночью ты уже проверял.", show_alert=True)
        return
    if target is p:
        await call.answer("Себя проверять незачем.", show_alert=True)
        return
    game.sheriff_target = target.user_id
    found = _is_mafia(target)
    res = "🔴 мафия!" if found else "не мафия"
    p.checks.append((target.name, res))
    game.sheriff_checks[p.user_id] = game.sheriff_checks.get(p.user_id, 0) + 1
    if found:
        game.sheriff_hits[p.user_id] = game.sheriff_hits.get(p.user_id, 0) + 1
    await call.answer(f"{target.name} — {res}", show_alert=True)
    if call.message:
        p.night_msgs.pop(call.message.message_id, None)
        await _edit(call.bot, p.user_id, call.message.message_id,
                    f"Ночь {game.no}, проверка: <b>{_e(target.name)}</b> — {res}")
    _check_done(game)


@router.callback_query(F.data.startswith("mfh:"))
async def cb_heal(call: CallbackQuery) -> None:
    ctx = await _night_call(call, "doctor")
    if not ctx:
        return
    game, p, target = ctx
    if game.heal is not None:
        await call.answer("Этой ночью ты уже лечишь.", show_alert=True)
        return
    if target not in _heal_targets(game, p):
        await call.answer("Его сейчас лечить нельзя.", show_alert=True)
        return
    game.heal = target.user_id
    await call.answer(f"Лечишь: {target.name}")
    if call.message:
        p.night_msgs.pop(call.message.message_id, None)
        await _edit(call.bot, p.user_id, call.message.message_id,
                    f"Ночь {game.no}: лечишь <b>{_e(target.name)}</b>")
    _check_done(game)


async def _bots_step(bot: Bot, game: Game, step: str) -> None:
    """Тестовые боты ходят в свой шаг ночи со случайной задержкой, как живые."""
    no = game.no
    actors = [p for p in _alive(game) if p.is_bot and p.role in STEP_ROLES[step]]
    for p in random.sample(actors, len(actors)):
        await asyncio.sleep(random.uniform(1.5, 4) * TIME_SCALE)
        if (not _is_active(game) or game.phase != "night" or game.no != no or game.night_step != step
                or not p.alive):
            return
        if step == "mafia":
            targets = _kill_targets(game)
            human = [t for v, t in game.kill_votes.items() if not game.players[v].is_bot]
            if targets:
                game.kill_votes[p.user_id] = (human[0] if human and random.random() < 0.7
                                              else random.choice(targets).user_id)
                _schedule(bot, game, "kill", lambda: _refresh_kill(bot, game))
        elif step == "sheriff":
            others = [o for o in _alive(game) if o is not p]
            if others:
                t = random.choice(others)
                game.sheriff_target = t.user_id
                p.checks.append((t.name, "🔴 мафия!" if _is_mafia(t) else "не мафия"))
        elif step == "doctor":
            pool = _heal_targets(game, p)
            if pool:
                game.heal = random.choice(pool).user_id
        _check_done(game)


# ---------- утро ----------


def _resolve_night(game: Game) -> tuple[Player | None, bool]:
    """Итог ночи: (убитый или None, спас ли Доктор)."""
    doctor = _role_alive(game, "doctor")
    if doctor:
        if game.heal is not None and game.heal == doctor.user_id:
            doctor.healed_self = True
        doctor.last_heal = game.heal
    votes = {v: t for v, t in game.kill_votes.items()
             if v in game.players and game.players[v].alive and _is_mafia(game.players[v])
             and t in game.players and game.players[t].alive and not _is_mafia(game.players[t])}
    if not votes:
        return None, False
    counts = Counter(votes.values())
    top = max(counts.values())
    cands = [t for t, c in counts.items() if c == top]
    don = _role_alive(game, "don")
    if len(cands) > 1 and don and votes.get(don.user_id) in cands:
        target = votes[don.user_id]
    else:
        target = random.choice(cands)
    if doctor and game.heal == target:
        game.doc_saves[doctor.user_id] = game.doc_saves.get(doctor.user_id, 0) + 1
        return None, True
    victim = game.players[target]
    victim.alive = False
    victim.fate, victim.fate_no = "night", game.no
    if game.first_victim is None:
        game.first_victim = victim.user_id
    return victim, False


async def _send_card(bot: Bot, game: Game, p: Player, stamp: str, caption: str) -> None:
    try:
        png = await asyncio.to_thread(R.render_role_card, p.name, p.role, game.city, p.serial, None, stamp)
        await _retry(bot.send_photo, game.chat_id, BufferedInputFile(png, filename="card.png"),
                     caption=caption[:MAX_CAPTION])
    except Exception as e:
        logger.warning("mafia card failed: %s", e)
        await _send(bot, game.chat_id, caption)
    await _pause(game, 3)


async def _offer_last_word(bot: Bot, game: Game, p: Player, how: str) -> None:
    """Последнее слово — по желанию и без паузы в игре: бот пишет мёртвому в ЛС, тот присылает одно
    сообщение, бот пересылает его в чат. Тестовые боты «говорят» сами."""
    if p.is_bot:  # тестовый бот «говорит» сразу, чтобы слово не влезло в следующую ночь
        await _bot_line(bot, game, p, C.BOT_LAST_WORDS, last=True, delay=False)
        return
    game.last_word.add(p.user_id)
    msg = await _dm(bot, p, f"{how}\n\nМожешь сказать последнее слово: напиши его сюда одним сообщением — "
                            "я перешлю в чат. В самом чате мёртвые молчат.")
    if msg is None:
        game.last_word.discard(p.user_id)


def _last_word_game(message: Message) -> Game | None:
    if message.chat.type != "private" or message.from_user is None or not message.text \
            or message.text.startswith("/"):
        return None
    uid = message.from_user.id
    return next((g for g in games.values() if uid in g.last_word and g.phase != "ended"), None)


def _is_last_word(message: Message) -> bool:
    return _last_word_game(message) is not None


async def _bot_line(bot: Bot, game: Game, p: Player, lines: list[str], last: bool = False,
                    delay: bool = True) -> None:
    if delay:
        await asyncio.sleep(random.uniform(2, 5) * TIME_SCALE)
    if not _is_active(game):
        return
    line = _e(random.choice(lines))
    if last:
        await _send(bot, game.chat_id, f"<b>Последнее слово — {_e(p.name)}</b> ({_e(C.title(p.role))})\n"
                                       f"<blockquote>{line}</blockquote>")
    else:
        await _send(bot, game.chat_id, f"🗣 <b>{_e(p.name)}:</b> «{line}»")


async def _morning(bot: Bot, game: Game) -> None:
    game.phase = "morning"
    victim, saved = _resolve_night(game)
    logger.info("mafia night chat=%s no=%s kill_votes=%s heal=%s -> victim=%s saved=%s", game.chat_id, game.no,
                game.kill_votes, game.heal, victim.name if victim else None, saved)
    role = C.title(victim.role) if victim else None
    game.timeline.append(f"Ночь {game.no}: " + (f"мафия убила {victim.name} ({C.title(victim.role)})" if victim
                                                 else "Доктор спас жертву мафии" if saved else "никто не погиб"))
    story = None
    if ai.available():
        dossier = await _dossier(game, victim) if victim else ""
        story = await _ai(ai.mafia_morning(game.city, game.no, victim.name if victim else None,
                                           C.title(victim.role) if victim else None, saved, dossier, _style(game)),
                          "morning")
    story = story or C.morning_fallback(victim.name if victim else None, role, saved)
    await _say(bot, game, f"☀️ <b>УТРО {game.no}</b>\n{HR}\n\n{_host(game, story)}")
    if not victim or not _is_active(game):
        return
    await _send_card(bot, game, victim, "killed",
                     f"<b>Этой ночью убит: {_e(victim.name)}</b>\nРоль: <b>{_e(role)}</b>")
    if _winner(game) is None:  # партия на этом кончилась — последнее слово уже не к месту
        await _offer_last_word(bot, game, victim, "<b>Тебя убили этой ночью.</b>")


# ---------- день ----------


def _day_text(game: Game, left: float | None = None) -> str:
    alive = _alive(game)
    lines = [f"☀️ <b>ДЕНЬ {game.no}</b>", HR, "",
             f"<b>В живых — {len(alive)}</b>"] + [f"• {_e(p.name)}" for p in alive] + ["",
             "Обсуждаем, кто мафия."]
    if left is not None:
        lines += ["", f"До голосования: <b>{_clock(left)}</b>"]
    return "\n".join(lines)


def _day_kb(game: Game) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("Моя роль", "mf:role"), _btn("К голосованию ⏩", "mf:skip")]])


async def _day_quip(bot: Bot, game: Game, since: float) -> None:
    """Подкол ведущего посреди обсуждения: только по сообщениям живых в чате за этот день."""
    if not ai.available() or not game.db:
        return
    no = game.no
    try:
        rows = await game.db.recent_messages(game.chat_id, limit=60, since=int(since))
    except Exception:
        return
    alive = {p.user_id: p for p in _alive(game)}
    msgs = [{"id": mid, "name": alive[uid].name, "text": str(text)[:300]}
            for mid, uid, _name, text, _ts in rows if uid in alive and text]
    if len(msgs) < 3:
        return
    talked = {uid for _mid, uid, *_r in rows}
    silent = [p.name for p in alive.values() if p.user_id not in talked and not p.is_bot]
    text = await _ai(ai.mafia_day_quip(game.city, no, msgs[-40:], silent, _style(game)), "day quip")
    if text and _is_active(game) and game.phase == "day" and game.no == no:
        await _send(bot, game.chat_id, _host(game, text))


async def _day(bot: Bot, game: Game) -> None:
    game.phase = "day"
    game.skip_day = False
    game.advance = asyncio.Event()
    since = time.time()
    total = day_time(len(_alive(game)))
    msg = await _send(bot, game.chat_id, _day_text(game, total), reply_markup=_day_kb(game))
    game.day_msg_id = msg.message_id
    start = _now()
    ends, quip_at = start + total, start + total * 0.55
    quipped = not ai.available()
    while _is_active(game) and not game.skip_day:
        left = ends - _now()
        if left <= 0.05:
            break
        marks = [m for m in (60, 30, 10) if m < left - 1]
        wait = left - (marks[0] if marks else 0)
        if not quipped:
            wait = min(wait, max(0.0, quip_at - _now()))
        if await _wait(game.advance, wait):
            break
        if not quipped and _now() >= quip_at - 0.05:
            quipped = True
            _launch(bot, game, _day_quip(bot, game, since), fatal=False)
        left = ends - _now()
        if left > 1 and _is_active(game):
            await _chat_edit(bot, game, game.day_msg_id, _day_text(game, left), _day_kb(game),
                             ok=lambda: game.phase == "day", retry=False)
    if not _is_active(game):
        return
    game.phase = "vote"  # кнопка «к голосованию» и таймер больше не оживут
    await _chat_edit(bot, game, game.day_msg_id, f"☀️ <b>День {game.no}</b> — обсуждение окончено.")
    await _vote(bot, game)


# ---------- голосование ----------


def _tally(game: Game) -> tuple[list[int], int, int]:
    """(лидеры, их голоса, голоса за «никого»)."""
    counts = Counter(t for v, t in game.votes.items() if v in game.players and game.players[v].alive)
    skip = counts.pop(0, 0)
    counts = Counter({t: c for t, c in counts.items() if t in game.vote_targets and game.players[t].alive})
    top = max(counts.values(), default=0)
    return ([t for t, c in counts.items() if c == top] if top else []), top, skip


def _vote_counts(game: Game) -> tuple[dict[int, int], int]:
    """(голоса против каждого из кандидатов, голоса за «никого») — только живые голосующие."""
    scores = {u: 0 for u in game.vote_targets if game.players[u].alive}
    skip = 0
    for v, t in game.votes.items():
        if v not in game.players or not game.players[v].alive:
            continue
        if t == 0:
            skip += 1
        elif t in scores:
            scores[t] += 1
    return scores, skip


def _bars(game: Game) -> str:
    scores, skip = _vote_counts(game)
    rows = sorted(scores.items(), key=lambda kv: -kv[1])
    top = max([skip] + list(scores.values()))
    names = [game.players[u].name[:14] for u, _ in rows] + ["никого"]
    width = min(14, max(len(n) for n in names))
    lines = [f"{_e(game.players[u].name[:14].ljust(width))} {_bar(n, top)} {n}" for u, n in rows]
    lines += ["", f"{'никого'.ljust(width)} {_bar(skip, top)} {skip}"]
    return "<code>" + "\n".join(lines) + "</code>"


def _vote_text(game: Game, closed: bool = False) -> str:
    alive = _alive(game)
    revote = len(game.vote_targets) < len(alive)
    if closed:
        head = "Переголосование" if revote else "Голосование"
        return f"🗳 <b>{head} · день {game.no} — закрыто</b>\n\n{_bars(game)}"
    if revote:
        names = _names([f"<b>{_e(game.players[u].name)}</b>" for u in game.vote_targets], " или ")
        lines = [f"⚖️ <b>ПЕРЕГОЛОСОВАНИЕ · ДЕНЬ {game.no}</b>", HR, "", f"Ничья. Выбираем между: {names}"]
    else:
        lines = [f"🗳 <b>ГОЛОСОВАНИЕ · ДЕНЬ {game.no}</b>", HR, "", "Кого казним? Голосование тайное."]
    lines += ["", _bars(game), "", f"Проголосовали: {len(game.votes)}/{len(alive)}"]
    return "\n".join(lines)


def _vote_kb(game: Game) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for u in sorted(game.vote_targets, key=lambda x: game.players[x].name):
        if game.players[u].alive:
            b.button(text=game.players[u].name, callback_data=f"mf:v:{game.no}:{u}")
    b.button(text="Никого", callback_data=f"mf:v:{game.no}:0")
    b.adjust(2)
    b.row(_btn("Моя роль", "mf:role"))
    return b.as_markup()


async def _refresh_vote(bot: Bot, game: Game) -> None:
    """Обновить счётчик голосов. Под замком и с проверкой стадии — устаревшая правка с кнопками
    не ляжет поверх закрытого голосования (иначе у всех снова появлялись бы кнопки)."""
    msg_id, stage = game.vote_msg_id, game.vote_stage
    await _chat_edit(bot, game, msg_id, lambda: _vote_text(game), lambda: _vote_kb(game),
                     ok=lambda: (game.phase == "vote" and game.vote_stage == stage and stage in ("open", "revote")
                                 and game.vote_msg_id == msg_id), retry=False)


@router.callback_query(F.data.startswith("mf:v:"))
async def cb_vote(call: CallbackQuery) -> None:
    game = games.get(call.message.chat.id) if call.message else None
    try:
        _, _, no_s, target_s = (call.data or "").split(":")
        no, target = int(no_s), int(target_s)
    except ValueError:
        await call.answer()
        return
    if not game or game.phase != "vote" or game.no != no or game.vote_stage not in ("open", "revote"):
        await call.answer("Голосование закрыто.", show_alert=True)
        return
    uid = call.from_user.id
    voter = game.players.get(uid)
    if voter is None or not voter.alive:
        await call.answer("Голосуют только живые игроки.", show_alert=True)
        return
    if target:
        if target == uid:
            await call.answer("За себя нельзя.", show_alert=True)
            return
        if target not in game.players or not game.players[target].alive:
            await call.answer("Этого игрока уже нет.", show_alert=True)
            return
        if target not in game.vote_targets:
            await call.answer("Сейчас голосуем только между теми, у кого ничья.", show_alert=True)
            return
    game.votes[uid] = target
    await call.answer(f"Голос: {game.players[target].name if target else 'никого'}")
    _schedule(call.bot, game, "vote", lambda: _refresh_vote(call.bot, game))
    if len(game.votes) >= len(_alive(game)) and game.advance:
        game.advance.set()  # все проголосовали — подводим итог


def _score_votes(game: Game, votes: dict[int, int]) -> None:
    """Для номинаций и званий: мирный голосовал против мафии или против своего."""
    for v, t in votes.items():
        if t == 0 or v not in game.players or t not in game.players or _is_mafia(game.players[v]):
            continue
        bucket = game.votes_right if _is_mafia(game.players[t]) else game.votes_wrong
        bucket[v] = bucket.get(v, 0) + 1


async def _bots_vote(bot: Bot, game: Game) -> None:
    """Тестовые боты голосуют вразброс за время голосования — как живые, а не все в первые секунды."""
    stage, no = game.vote_stage, game.no
    window = (VOTE_TIME if stage == "open" else REVOTE_TIME) * 0.75

    async def one(p: Player) -> None:
        await asyncio.sleep(random.uniform(3, max(4, window)) * TIME_SCALE)
        if not _is_active(game) or game.phase != "vote" or game.vote_stage != stage or game.no != no or not p.alive:
            return
        pool = [u for u in game.vote_targets if u != p.user_id and game.players[u].alive
                and not (_is_mafia(p) and _is_mafia(game.players[u]))]
        game.votes[p.user_id] = random.choice(pool) if pool and random.random() < 0.85 else 0
        _schedule(bot, game, "vote", lambda: _refresh_vote(bot, game))
        if len(game.votes) >= len(_alive(game)) and game.advance:
            game.advance.set()

    await asyncio.gather(*(one(p) for p in _alive(game) if p.is_bot))


async def _speech(bot: Bot, game: Game, cands: list[int]) -> None:
    game.vote_stage = "speech"
    who = _names([_mention(game.players[u]) for u in cands])
    await _send(bot, game.chat_id, f"⚖️ <b>НИЧЬЯ</b>\n{HR}\n\nПоровну голосов у {who}.\n\n"
                                   f"У вас <b>{SPEECH_TIME} сек</b> на оправдания, потом переголосование только "
                                   "между вами. Снова ничья — сегодня никого не казнят.")
    for u in cands:
        if game.players[u].is_bot:
            _launch(bot, game, _bot_line(bot, game, game.players[u], C.BOT_SPEECHES), fatal=False)
    await _pause(game, SPEECH_TIME)


async def _vote(bot: Bot, game: Game) -> None:
    game.phase = "vote"
    game.vote_targets = {p.user_id for p in _alive(game)}
    executed, note = None, ""
    for attempt in range(2):
        game.votes = {}
        game.vote_stage = "open" if attempt == 0 else "revote"
        game.advance = asyncio.Event()
        game.vote_ends = _now() + (VOTE_TIME if attempt == 0 else REVOTE_TIME)
        msg = await _send(bot, game.chat_id, _vote_text(game), reply_markup=_vote_kb(game))
        game.vote_msg_id = msg.message_id
        if any(p.is_bot for p in _alive(game)):
            _launch(bot, game, _bots_vote(bot, game), fatal=False)
        await _wait(game.advance, game.vote_ends - _now())  # до конца времени или пока все не проголосуют
        if not _is_active(game):
            return
        async with game.edit_lock:  # закрываем после всех начатых правок — кнопки уже не вернутся
            game.vote_stage = "closed"
            await _edit(bot, game.chat_id, game.vote_msg_id, _vote_text(game, closed=True))
        if attempt == 0:
            _score_votes(game, game.votes)
        cands, top, skip = _tally(game)
        logger.info("mafia vote chat=%s day=%s attempt=%s votes=%s -> %s top=%s skip=%s", game.chat_id, game.no,
                    attempt, {game.players[v].name: (game.players[t].name if t else "-") for v, t in game.votes.items()},
                    [game.players[u].name for u in cands], top, skip)
        if top == 0:
            note = "большинство за «никого»" if skip else "никто не проголосовал"
            break
        if skip >= top:
            note = "за «никого» голосов не меньше"
            break
        if len(cands) == 1:
            executed = cands[0]
            break
        if attempt == 1:
            note = "снова ничья"
            break
        await _speech(bot, game, cands)
        if not _is_active(game):
            return
        game.vote_targets = set(cands)
    if executed is not None:
        await _execute(bot, game, game.players[executed])
    else:
        game.timeline.append(f"День {game.no}: никого не казнили ({note})")
        await _say(bot, game, f"<b>Сегодня никого не казнят</b> — {_e(note)}.")


async def _execute(bot: Bot, game: Game, p: Player) -> None:
    p.alive = False
    p.fate, p.fate_no = "day", game.no
    right = _is_mafia(p)
    role = C.title(p.role)
    game.timeline.append(f"День {game.no}: город казнил {p.name} ({C.title(p.role)})")
    story = None
    if ai.available():
        story = await _ai(ai.mafia_execution(game.city, game.no, p.name, C.title(p.role), right,
                                             await _dossier(game, p), _style(game)), "execution")
    story = story or C.executed_fallback(p.name, role, right)
    await _say(bot, game, f"⚖️ <b>ПРИГОВОР</b>\n{HR}\n\nГород казнит: <b>{_e(p.name)}</b>\n\n{_host(game, story)}")
    if not _is_active(game):
        return
    await _send_card(bot, game, p, "executed",
                     f"<b>{_e(p.name)}</b> — {_e(role)}\n"
                     + ("Город попал: это мафия." if right else "Город ошибся: казнили своего."))
    if _winner(game) is None:
        await _offer_last_word(bot, game, p, "<b>Город тебя казнил.</b>")


# ---------- финал ----------


async def _nominations(game: Game) -> list[tuple[str, str, str]]:
    noms: list[tuple[str, str, str]] = []
    pl = game.players
    if game.first_victim in pl:
        v = pl[game.first_victim]
        noms.append(("Первая жертва", v.name, "убит в первую же ночь" if v.fate_no == 1
                     else f"первым погиб от мафии — ночью {v.fate_no}"))
    sheriff = next((p for p in pl.values() if p.role == "sheriff"), None)
    if sheriff:
        hits, checks = game.sheriff_hits.get(sheriff.user_id, 0), game.sheriff_checks.get(sheriff.user_id, 0)
        if hits:
            noms.append(("Шерлок", sheriff.name, f"нашёл мафию: {hits} из {checks} проверок"))
        elif checks >= 2:
            noms.append(("Комиссар Слепошарый", sheriff.name, f"{checks} проверки — и ни одной мафии"))
    for uid, n in game.doc_saves.items():
        if uid in pl:
            noms.append(("Айболит", pl[uid].name, f"спас от мафии: {n}"))
    if game.votes_right:
        uid, n = max(game.votes_right.items(), key=lambda kv: kv[1])
        if n >= 2:
            noms.append(("Чуйка", pl[uid].name, f"{n} {C._plural(n, 'раз', 'раза', 'раз')} голосовал против мафии"))
    if game.votes_wrong:
        uid, n = max(game.votes_wrong.items(), key=lambda kv: kv[1])
        if n >= 2:
            noms.append(("Палач невиновных", pl[uid].name, f"{n} {C._plural(n, 'раз', 'раза', 'раз')} голосовал против своих"))
    if game.db and len(noms) < 5:  # кто больше всех болтал за партию (бот запоминает переписку дня)
        try:
            rows = await game.db.recent_messages(game.chat_id, limit=3000, since=int(game.started))
            counts = Counter(uid for _m, uid, *_r in rows if uid in pl)
            if counts:
                uid, n = counts.most_common(1)[0]
                if n >= 5:
                    noms.append(("Болтун", pl[uid].name, f"{n} сообщений за партию"))
        except Exception:
            pass
    return noms[:5]


async def _finish(bot: Bot, game: Game, winner: str) -> None:
    game.phase = "ended"  # чат снова открыт
    players = sorted(game.players.values(), key=lambda p: (C.ROLE_ORDER.index(p.role), p.name))
    epi_task = asyncio.create_task(_ai(ai.mafia_epilogue(
        game.city, winner, [{"name": p.name, "role": C.title(p.role), "fate": _fate(p)} for p in players],
        game.timeline, _style(game)), "epilogue")) if ai.available() else None
    head = {"mafia": "Победила <b>мафия</b>.", "town": "Победил <b>город</b>.",
            }.get(winner, "<b>Ничья</b> — город уснул от скуки.")
    try:
        await bot.send_chat_action(game.chat_id, "typing")
    except Exception:
        pass
    epi = (await epi_task if epi_task else None) or C.EPILOGUE.get(winner, C.EPILOGUE["draw"]).format(city=game.city)
    await _send(bot, game.chat_id, f"🏁 <b>ПАРТИЯ ОКОНЧЕНА</b>\n{HR}\n\n{head}\n\n{_host(game, epi)}")
    await asyncio.sleep(2 * TIME_SCALE)

    noms = await _nominations(game)
    try:  # роли всех и судьбы — на картинке
        png = await asyncio.to_thread(
            R.render_credits, game.city, winner, [{"name": p.name, "role": p.role, "fate": _fate(p)} for p in players],
            noms, game.serial, date.today().strftime("%d.%m.%Y"))
        await _retry(bot.send_photo, game.chat_id, BufferedInputFile(png, filename="credits.png"),
                     caption="/mafia — ещё раз\n\n/mafia_top — рейтинг")
    except Exception as e:
        logger.warning("mafia credits failed: %s", e)
        await _send(bot, game.chat_id, "<b>Кто был кем</b>\n\n" + "\n".join(
            f"<b>{_e(p.name)}</b> — {C.title(p.role)}, {_fate(p)}" for p in players))

    if game.db:
        results = []
        for p in players:
            if p.is_bot:
                continue
            tm, win = C.team(p.role), C.team(p.role) == winner
            results.append({
                "user_id": p.user_id, "name": p.name, "games": 1, "wins": int(win),
                "mafia_games": int(tm == "mafia"), "mafia_wins": int(win and tm == "mafia"),
                "town_wins": int(win and tm == "town"), "survived": int(p.alive),
                "first_victim": int(p.user_id == game.first_victim), "killed": int(p.fate == "night"),
                "executed": int(p.fate == "day"), "executed_town": int(p.fate == "day" and tm == "town"),
                "sheriff_checks": game.sheriff_checks.get(p.user_id, 0),
                "sheriff_hits": game.sheriff_hits.get(p.user_id, 0), "doc_saves": game.doc_saves.get(p.user_id, 0),
                "votes_right": game.votes_right.get(p.user_id, 0), "votes_wrong": game.votes_wrong.get(p.user_id, 0),
            })
        try:
            await game.db.record_mafia_game(results)
        except Exception as e:
            logger.warning("mafia stats save failed: %s", e)
    _end_game(game)


# ---------- последнее слово: мёртвый пишет боту в ЛС одно сообщение ----------


@router.message(_is_last_word)
async def on_last_word(message: Message) -> None:
    game = _last_word_game(message)
    if game is None or message.from_user is None:
        return
    uid = message.from_user.id
    game.last_word.discard(uid)  # только одно сообщение
    p = game.players[uid]
    text = (message.text or "").strip()[:LAST_WORD_MAX]
    try:
        await _send(message.bot, game.chat_id,
                    f"<b>Последнее слово — {_e(p.name)}</b> ({_e(C.title(p.role))})\n"
                    f"<blockquote>{_e(text)}</blockquote>")
        await message.answer("Передал в чат.")
    except Exception as e:
        logger.warning("last word failed: %s", e)
        await message.answer("Не смог передать в чат.")


# ---------- удаление: ночь, мёртвые, не-игроки (последним — после команд) ----------


@router.message(must_delete)
async def on_hush(message: Message) -> None:
    game = games.get(message.chat.id)
    if not game:
        return
    try:
        await message.delete()
    except Exception as e:  # нет права «удалять сообщения» — больше не пытаемся в этой партии
        game.cant_delete = True
        logger.info("mafia can't delete in %s: %s", message.chat.id, e)
        try:
            await _send(message.bot, game.chat_id, "⚠️ Не могу удалять сообщения — дайте мне право «Удаление "
                                                   "сообщений», иначе ночью чат не закрыть.")
        except Exception:
            pass
        return
