"""«Нефоры в Волгограде» — своя экономическая игра для группового чата (не копия чужой настолки).

Тусовки нефоров (панки, готы, эмо…) делят точки Волгограда. Поток партии — одна фоновая задача
`_run_game`, кнопки лишь будят её:
  /nefor → лобби (вступают кнопкой прямо в чате, без лички) → старт: каждому тусовка и START_CASH →
  ROUNDS кругов; в начале круга — карта города картинкой (подпись — что было в прошлом круге и капиталы).
  ХОД: одно сообщение, которое правится: «Бросить» (или сделка до броска) → два кубика 🎲 Telegram →
  фишка идёт по полю (прошёл «Конечную» — стипуха) → клетка:
    точка свободна — «Занять» / «Мимо»; своя — «Крыша» (до двух, вход дороже); чужая — платишь за вход
    (весь район у одной тусовки — вдвое); «Слух» — событие (ИИ пишет, игра считает деньги);
    «Пиздюк» — подкол и штраф (иногда — на пиво); «ППС» — штраф.
  СДЕЛКИ: до броска — купить точку у другого игрока или продать свою, цена кнопками; второй игрок
  принимает или отказывается (боты решают сами).
  ДОЛГИ: не хватает денег — точки уходят городу за полцены, совсем нечем — банкрот, выбывает.
  ФИНАЛ: после последнего круга (или когда остался один) — у кого больше капитал (деньги + точки + крыши).
Стёб-бот во время партии молчит (games.active), чат не закрывается и ничего не удаляется.
"""
from __future__ import annotations

import asyncio
import html
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import (BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ai
from database import Database
from ephemeral import delete_later, temp_reply

from ..common import HR, busy_text, lobby_text
from . import content as C
from . import render as R

logger = logging.getLogger("nefor")
router = Router(name="nefor")

MIN_PLAYERS = 2
MAX_PLAYERS = 6
LOBBY_TIMEOUT = 15 * 60
TURN_TIME = 40        # сек на бросок (и сделки до него); потом бросаем за игрока
DECIDE_TIME = 25      # сек на «занять / крыша»; потом — «мимо»
TRADE_TIME = 30       # сек на каждый шаг сделки и на ответ второго игрока
DICE_WAIT = 3.5       # сек: анимация кубика Telegram
MAX_TRADES = 2        # сделок за один ход
AI_TIMEOUT = 12       # сек: дольше ИИ не ждём — берём запасной текст
TIME_SCALE = 1.0      # для тестов: 0.01 — все таймеры в 100 раз быстрее
MAX_CAPTION = 1000
PRICE_STEPS = (0.5, 1.0, 1.5, 2.0)  # цены сделки: доля от стоимости точки


@dataclass
class Player:
    user_id: int
    name: str
    username: str | None = None
    is_bot: bool = False
    tribe: str = ""
    color: tuple[int, int, int] = (200, 200, 200)
    icon: str = ""
    jail: int = 0         # сколько своих ходов ещё сидит в СИЗО
    cash: int = C.START_CASH
    pos: int = 0
    out: bool = False
    out_no: int = 0       # каким по счёту выбыл (для мест)
    trades: int = 0


@dataclass
class Game:
    chat_id: int
    host_id: int
    db: Database | None = None
    mode: str = "classic"
    players: dict[int, Player] = field(default_factory=dict)
    order: list[int] = field(default_factory=list)
    phase: str = "lobby"          # lobby | starting | play | ended
    serial: int = 0
    round_no: int = 0
    lobby_msg_id: int | None = None
    lobby_photo: bool = False
    owners: dict[int, int] = field(default_factory=dict)   # клетка -> id владельца
    roofs: dict[int, int] = field(default_factory=dict)    # клетка -> крыш (0..2)
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    edit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # ход
    turn_uid: int | None = None
    turn_msg_id: int | None = None
    stage: str = ""               # roll | t_who | t_spot | t_price | offer | rolling | decide
    ev: asyncio.Event | None = None
    deadline: float = 0.0
    note: str = ""                # строка о сделке над кнопками
    lines: list[str] = field(default_factory=list)  # что случилось за ход (после броска)
    options: dict[str, str] = field(default_factory=dict)  # кнопки решения: ключ -> подпись
    decision: str | None = None
    trade: dict = field(default_factory=dict)
    trades_this_turn: int = 0
    # круг
    round_log: list[str] = field(default_factory=list)
    round_msgs: list[int] = field(default_factory=list)  # сообщения круга — удалим в начале следующего
    board_msg_id: int | None = None
    bankrupts: int = 0


games: dict[int, Game] = {}
_tasks: set[asyncio.Task] = set()


# ---------- мелочи ----------


def _e(s) -> str:
    return html.escape(str(s))


def _now() -> float:
    return time.time() / TIME_SCALE


def _rub(n: int) -> str:
    return f"{n}₽"


def _is_active(game: Game) -> bool:
    return game.phase not in ("ended",) and not game.stop.is_set() and games.get(game.chat_id) is game


def _alive(game: Game) -> list[Player]:
    return [game.players[u] for u in game.order if not game.players[u].out]


def _style(game: Game) -> str:
    return C.MODES.get(game.mode, C.MODES["classic"])[1]


def _mode_title(game: Game) -> str:
    return C.MODES.get(game.mode, C.MODES["classic"])[0]


def _mode_key(chat_id: int) -> str:
    return f"nefor_mode:{chat_id}"


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _end_game(game: Game) -> None:
    game.phase = "ended"
    game.stop.set()
    if games.get(game.chat_id) is game:
        games.pop(game.chat_id, None)
    if game.ev:
        game.ev.set()


def _launch(bot: Bot, game: Game, coro, fatal: bool = True) -> None:
    async def runner() -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("nefor task crashed in chat %s", game.chat_id)
            if fatal and _is_active(game):
                _end_game(game)
                try:
                    await _send(bot, game.chat_id, "💥 Игру накрыли ППС… Партия прервана.\n/nefor - начать заново.")
                except Exception:
                    pass

    task = asyncio.create_task(runner())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _wait(ev: asyncio.Event | None, sec: float) -> bool:
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
    await _wait(game.stop, sec)


async def _retry(call, *args, **kwargs):
    for attempt in range(4):
        try:
            return await call(*args, **kwargs)
        except TelegramRetryAfter as e:
            if attempt == 3:
                raise
            await asyncio.sleep(e.retry_after + 0.5)
    raise RuntimeError("unreachable")


async def _send(bot: Bot, chat_id: int, text: str, **kwargs) -> Message:
    return await _retry(bot.send_message, chat_id, text, **kwargs)


async def _edit(bot: Bot, chat_id: int, message_id: int | None, text: str,
                reply_markup: InlineKeyboardMarkup | None = None) -> None:
    if message_id is None:
        return
    try:
        await _retry(bot.edit_message_text, text=text, chat_id=chat_id, message_id=message_id,
                     reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            logger.debug("nefor edit failed: %s", e)
    except Exception as e:
        logger.debug("nefor edit failed: %s", e)


async def _delete(bot: Bot, chat_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception as e:
        logger.debug("nefor delete failed: %s", e)


async def _ai(coro, what: str):
    try:
        return await asyncio.wait_for(coro, timeout=AI_TIMEOUT)
    except Exception as e:
        logger.info("nefor AI %s failed: %s", what, e or type(e).__name__)
        return None


# ---------- деньги и точки ----------


def _spots(game: Game, uid: int) -> list[int]:
    return sorted(c for c, o in game.owners.items() if o == uid)


def _value(game: Game, cell: int) -> int:
    return C.price(cell) + game.roofs.get(cell, 0) * C.roof_cost(cell)


def _capital(game: Game, p: Player) -> int:
    return 0 if p.out else p.cash + sum(_value(game, c) for c in _spots(game, p.user_id))


def _rent(game: Game, cell: int) -> int:
    base = round(C.price(cell) * C.RENT_RATE / 5) * 5
    return base * C.ROOF_MULT[game.roofs.get(cell, 0)]


def _release(game: Game, cell: int) -> None:
    game.owners.pop(cell, None)
    game.roofs.pop(cell, None)


async def _pay(game: Game, p: Player, amount: int, to: Player | None, lines: list[str], short: list[str]) -> None:
    """p платит amount (to=None — городу). Не хватает — точки уходят городу за полцены; совсем нечем — банкрот."""
    if amount <= 0:
        return
    for cell in sorted(_spots(game, p.user_id), key=lambda c: _value(game, c)):
        if p.cash >= amount:
            break
        got = round(_value(game, cell) * C.SELL_RATE / 10) * 10
        _release(game, cell)
        p.cash += got
        lines.append(f"Не хватило денег - «{_e(C.cell_name(cell))}» ушла городу за {_rub(got)}")
        short.append(f"сдал «{C.cell_name(cell)}»")
    if p.cash >= amount:
        p.cash -= amount
        if to:
            to.cash += amount
        return
    paid, p.cash = p.cash, 0
    if to:
        to.cash += paid
    p.out = True
    game.bankrupts += 1
    p.out_no = game.bankrupts
    lines.append(f"\n<b>{_e(p.name)} - банкрот</b> и выбывает.")
    short.append("банкрот")


# ---------- лобби ----------


def _lobby_text(game: Game) -> str:
    return lobby_text("🎸 НЕФОРЫ В ВОЛГОГРАДЕ", [(p.name, p.user_id == game.host_id, True)
                                                for p in game.players.values()],
                      MAX_PLAYERS, MIN_PLAYERS, _mode_title(game), "Правила",
                      [f"• {C.ROUNDS} кругов, у каждого {_rub(C.START_CASH)} на старте",
                       "• занимай точки города, ставь крыши, бери за вход",
                       f"• СИЗО - пропуск {C.JAIL_TURNS} ходов",
                       "• сделки с другими - кнопкой до броска",
                       "• побеждает самый большой капитал"])


def _lobby_kb(game: Game) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(_btn("Вступить", "nf:join"), _btn("Выйти", "nf:leave"))
    b.row(_btn(f"Ведущий: {_mode_title(game)}", "nf:mode"))
    b.row(_btn("Начать ▶️", "nf:start"), _btn("Отмена", "nf:cancel"))
    return b.as_markup()


LOBBY_IMAGE = Path(__file__).resolve().parents[2] / "assets" / "lobby" / "nefor.jpg"
_lobby_file_id: list[str] = []


async def _send_lobby(message: Message, game: Game) -> None:
    try:
        photo = _lobby_file_id[0] if _lobby_file_id else FSInputFile(LOBBY_IMAGE)
        sent = await message.answer_photo(photo, caption=_lobby_text(game), reply_markup=_lobby_kb(game))
        if sent.photo and not _lobby_file_id:
            _lobby_file_id.append(sent.photo[-1].file_id)
        game.lobby_photo = True
    except Exception as e:
        logger.info("nefor lobby photo failed, sending text: %s", e)
        sent = await message.answer(_lobby_text(game), reply_markup=_lobby_kb(game))
        game.lobby_photo = False
    game.lobby_msg_id = sent.message_id


async def _edit_lobby(bot: Bot, game: Game, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    if game.lobby_msg_id is None:
        return
    if not game.lobby_photo:
        await _edit(bot, game.chat_id, game.lobby_msg_id, text, reply_markup)
        return
    try:
        await bot.edit_message_caption(chat_id=game.chat_id, message_id=game.lobby_msg_id, caption=text,
                                       reply_markup=reply_markup)
    except Exception as e:
        if "not modified" not in str(e):
            logger.debug("nefor lobby caption edit failed: %s", e)


async def _refresh_lobby(bot: Bot, game: Game) -> None:
    await _edit_lobby(bot, game, _lobby_text(game), _lobby_kb(game))


def _unique_name(game: Game, raw: str | None) -> str:
    base = (raw or "").strip()[:32] or "Игрок"
    taken = {p.name for p in game.players.values()}
    if base not in taken:
        return base
    n = 2
    while f"{base} {n}" in taken:
        n += 1
    return f"{base} {n}"


@router.message(Command("nefor"))
async def cmd_nefor(message: Message, command: CommandObject, db: Database) -> None:
    user = message.from_user
    if user is None:
        return
    if message.chat.type == "private":
        await message.answer("В Нефоров играют в групповом чате: напиши там /nefor.")
        return
    chat_id = message.chat.id
    from games import active

    busy = active(chat_id)
    if busy:
        await temp_reply(message, busy_text(busy))
        return
    game = Game(chat_id=chat_id, host_id=user.id, db=db)
    saved = await db.get_setting(_mode_key(chat_id), "classic")
    game.mode = saved if saved in C.MODES else "classic"
    arg = (command.args or "").strip().lower()
    if arg in C.MODE_ALIASES:
        game.mode = C.MODE_ALIASES[arg]
        await db.set_setting(_mode_key(chat_id), game.mode)
    game.players[user.id] = Player(user_id=user.id, name=_unique_name(game, user.first_name), username=user.username)
    games[chat_id] = game
    await _send_lobby(message, game)
    _launch(message.bot, game, _lobby_timer(message.bot, game), fatal=False)


async def _lobby_timer(bot: Bot, game: Game) -> None:
    await asyncio.sleep(LOBBY_TIMEOUT)
    if games.get(game.chat_id) is not game or game.phase != "lobby":
        return
    _end_game(game)
    await _edit_lobby(bot, game, "⌛ Лобби Нефоров закрыто - игра так и не началась.\n/nefor - собрать заново.")
    delete_later(bot, game.chat_id, game.lobby_msg_id)


@router.message(Command("nefor_bots"))
async def cmd_nefor_bots(message: Message, command: CommandObject) -> None:
    """Тестовые боты в лобби: /nefor_bots [сколько, по умолчанию 2]."""
    game = games.get(message.chat.id)
    if not game or game.phase != "lobby":
        await message.answer("Сначала соберите лобби: /nefor")
        return
    try:
        n = max(1, min(5, int((command.args or "2").strip())))
    except ValueError:
        n = 2
    existing = sum(1 for p in game.players.values() if p.is_bot)
    added = []
    for i in range(n):
        if len(game.players) >= MAX_PLAYERS:
            break
        uid = -(existing + i + 1)
        name = _unique_name(game, C.BOT_NAMES[(existing + i) % len(C.BOT_NAMES)])
        game.players[uid] = Player(user_id=uid, name=name, is_bot=True)
        added.append(name)
    await message.answer("➕ В город приехали тестовые боты: " + (", ".join(_e(a) for a in added) or "мест нет"))
    await _refresh_lobby(message.bot, game)


@router.message(Command("nefor_stop"))
async def cmd_nefor_stop(message: Message) -> None:
    game = games.get(message.chat.id)
    if not game:
        await message.answer("Сейчас Нефоры не идут.")
        return
    in_lobby = game.phase == "lobby"
    _end_game(game)
    if in_lobby:
        await _delete(message.bot, game.chat_id, game.lobby_msg_id)
    else:
        await _edit(message.bot, game.chat_id, game.turn_msg_id, "🛑 Партия остановлена.")
    await message.answer("🛑 Нефоры остановлены.")


def _lobby_game(call: CallbackQuery) -> Game | None:
    game = games.get(call.message.chat.id) if call.message else None
    if game and game.phase == "lobby" and game.lobby_msg_id == call.message.message_id:
        return game
    return None


async def _stale_lobby(call: CallbackQuery) -> None:
    game = games.get(call.message.chat.id) if call.message else None
    if game and call.message and game.lobby_msg_id == call.message.message_id:
        await call.answer("Игра уже началась.", show_alert=True)
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


@router.callback_query(F.data == "nf:join")
async def cb_join(call: CallbackQuery) -> None:
    game = _lobby_game(call)
    if not game:
        await _stale_lobby(call)
        return
    u = call.from_user
    if u.id in game.players:
        await call.answer("Ты уже в игре.")
        return
    if len(game.players) >= MAX_PLAYERS:
        await call.answer("Мест больше нет.", show_alert=True)
        return
    game.players[u.id] = Player(user_id=u.id, name=_unique_name(game, u.first_name), username=u.username)
    await call.answer("Ты в игре.")
    await _refresh_lobby(call.bot, game)


@router.callback_query(F.data == "nf:leave")
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
        await call.answer("Ведущий не может выйти - нажми «Отмена».", show_alert=True)
        return
    game.players.pop(uid, None)
    await call.answer("Ты вышел из игры.")
    await _refresh_lobby(call.bot, game)


@router.callback_query(F.data == "nf:mode")
async def cb_mode(call: CallbackQuery) -> None:
    game = _lobby_game(call)
    if not game:
        await _stale_lobby(call)
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


@router.callback_query(F.data == "nf:cancel")
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
    await _edit_lobby(call.bot, game, "❌ Нефоры отменены.")
    delete_later(call.bot, game.chat_id, game.lobby_msg_id)


@router.callback_query(F.data == "nf:start")
async def cb_start(call: CallbackQuery) -> None:
    game = _lobby_game(call)
    if not game:
        await _stale_lobby(call)
        return
    if call.from_user.id != game.host_id:
        await call.answer("Начать может только ведущий.", show_alert=True)
        return
    if len(game.players) < MIN_PLAYERS:
        await call.answer(f"Нужно минимум {MIN_PLAYERS} игрока.", show_alert=True)
        return
    await call.answer("Поехали!")
    game.phase = "starting"
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    _launch(call.bot, game, _run_game(call.bot, game))


# ---------- партия ----------


async def _run_game(bot: Bot, game: Game) -> None:
    game.order = list(game.players)
    random.shuffle(game.order)
    tribes = random.sample(C.TRIBES, len(game.order))
    for uid, (tribe, color, icon) in zip(game.order, tribes, strict=True):
        p = game.players[uid]
        p.tribe, p.color, p.icon, p.cash, p.pos = tribe, color, icon, C.START_CASH, 0
    if game.db:
        try:
            game.serial = int(await game.db.get_setting("nefor_serial", "0") or 0) + 1
            await game.db.set_setting("nefor_serial", str(game.serial))
        except Exception:
            game.serial = random.randint(1, 999)
    game.phase = "play"
    order = "\n".join(f"{i + 1}. <b>{_e(game.players[u].name)}</b> - {game.players[u].tribe}"
                      for i, u in enumerate(game.order))
    await _send(bot, game.chat_id,
                f"🎸 <b>НЕФОРЫ В ВОЛГОГРАДЕ</b>\n{HR}\n\nТусовки делят город. У каждого {_rub(C.START_CASH)}, "
                f"впереди {C.ROUNDS} кругов.\n\n<b>Порядок ходов</b>\n{order}\n\n"
                f"Встал на свободную точку - можно занять. На чужой - плати за вход. Своя - ставь крышу, "
                f"вход станет дороже. Попал в СИЗО - пропускаешь {C.JAIL_TURNS} хода.\n\n"
                f"Побеждает самый большой капитал: деньги + точки + крыши.")
    await _pause(game, 4)
    for rnd in range(1, C.ROUNDS + 1):
        if not _is_active(game):
            return
        game.round_no = rnd
        await _send_board(bot, game)
        for uid in list(game.order):
            if not _is_active(game):
                return
            p = game.players[uid]
            if p.out:
                continue
            if p.jail:
                p.jail -= 1
                game.round_log.append(f"• {_e(p.name)}: сидит в СИЗО")
                try:
                    m = await _send(bot, game.chat_id, f"🔒 <b>{_e(p.name)}</b> в СИЗО - пропускает ход"
                                    + (f" (ещё {p.jail})." if p.jail else ". Следующий ход - уже на свободе."))
                    game.round_msgs.append(m.message_id)
                except Exception as e:
                    logger.info("nefor jail note failed: %s", e)
                await _pause(game, 2)
                continue
            await _turn(bot, game, p)
            if len(_alive(game)) <= 1:
                break
        if len(_alive(game)) <= 1:
            break
    if _is_active(game):
        await _finish(bot, game)


def _standings(game: Game) -> list[Player]:
    return sorted(game.players.values(), key=lambda p: (not p.out, _capital(game, p), p.out_no), reverse=True)


def _board_png(game: Game) -> bytes:
    return R.render_board(
        [{"id": p.user_id, "name": p.name, "color": p.color, "icon": p.icon, "tribe": p.tribe, "cash": p.cash,
          "capital": _capital(game, p), "pos": p.pos, "out": p.out, "jail": p.jail} for p in game.players.values()],
        dict(game.owners), dict(game.roofs), game.round_no, C.ROUNDS,
        {c: _rent(game, c) for c in game.owners})


def _board_caption(game: Game) -> str:
    head = f"<b>КРУГ {game.round_no} ИЗ {C.ROUNDS}</b>"
    table = "\n".join(
        f"{i + 1}. <b>{_e(p.name)}</b> ({p.tribe}) - " + ("банкрот" if p.out else
                                                       f"{_rub(_capital(game, p))}, наличными {_rub(p.cash)}")
        for i, p in enumerate(_standings(game)))
    tail = f"\n\n<b>Капитал</b>\n{table}"
    log = ""
    if game.round_log:
        log = "\n\n<b>Прошлый круг</b>\n"
        room = MAX_CAPTION - len(head) - len(tail) - len(log)
        rows = []
        for line in game.round_log:
            if sum(len(r) + 1 for r in rows) + len(line) > room:
                break
            rows.append(line)
        log += "\n".join(rows)
    return head + log + tail


async def _send_board(bot: Bot, game: Game) -> None:
    """Карта в начале круга. Сообщения прошлого круга и прошлую карту убираем — итог круга в подписи."""
    for mid in game.round_msgs:
        await _delete(bot, game.chat_id, mid)
    game.round_msgs.clear()
    await _delete(bot, game.chat_id, game.board_msg_id)
    caption = _board_caption(game)
    game.round_log.clear()
    try:
        png = await asyncio.to_thread(_board_png, game)
        msg = await _retry(bot.send_photo, game.chat_id, BufferedInputFile(png, filename="board.png"), caption=caption)
    except Exception as e:
        logger.warning("nefor board failed: %s", e)
        msg = await _send(bot, game.chat_id, caption)
    game.board_msg_id = msg.message_id
    await _pause(game, 2)


# ---------- ход ----------


def _turn_text(game: Game, p: Player) -> str:
    head = (f"<b>Ход - {_e(p.name)}</b> · {p.tribe}\n"
            f"Деньги: <b>{_rub(p.cash)}</b> · клетка: {_e(C.cell_name(p.pos))} · круг {game.round_no}/{C.ROUNDS}")
    parts = [head]
    if game.note:
        parts.append(game.note)
    if game.lines:
        parts.append("\n".join(game.lines))
    elif game.stage == "roll":
        parts.append("Бросай кубики." + (" Или предложи сделку." if game.trades_this_turn < MAX_TRADES else ""))
    return "\n\n".join(parts)


def _turn_kb(game: Game, p: Player) -> InlineKeyboardMarkup | None:
    b = InlineKeyboardBuilder()
    st = game.stage
    if st == "roll":
        row = [_btn("🎲 Бросить", "nf:roll")]
        if game.trades_this_turn < MAX_TRADES and len(_alive(game)) > 1:
            row.append(_btn("🤝 Сделка", "nf:trade"))
        b.row(*row)
    elif st == "t_who":
        for o in _alive(game):
            if o.user_id != p.user_id:
                b.row(_btn(o.name, f"nf:tw:{o.user_id}"))
        b.row(_btn("⬅️ Назад", "nf:back"))
    elif st == "t_spot":
        other = game.players.get(game.trade.get("with"))
        for c in _spots(game, other.user_id) if other else []:
            b.row(_btn(f"Купить: {C.cell_name(c)}", f"nf:ts:b:{c}"))
        for c in _spots(game, p.user_id):
            b.row(_btn(f"Продать: {C.cell_name(c)}", f"nf:ts:s:{c}"))
        b.row(_btn("⬅️ Назад", "nf:back"))
    elif st == "t_price":
        prices = _trade_prices(game, p)
        for i in range(0, len(prices), 2):
            b.row(*[_btn(_rub(v), f"nf:tp:{v}") for v in prices[i:i + 2]])
        b.row(_btn("⬅️ Назад", "nf:back"))
    elif st == "offer":
        b.row(_btn("✅ Принять", "nf:ta"), _btn("❌ Отказать", "nf:td"))
    elif st == "decide":
        b.row(*[_btn(label, f"nf:do:{key}") for key, label in game.options.items()])
    else:
        return None
    return b.as_markup()


async def _show_turn(bot: Bot, game: Game, p: Player) -> None:
    await _edit(bot, game.chat_id, game.turn_msg_id, _turn_text(game, p), _turn_kb(game, p))


async def _turn(bot: Bot, game: Game, p: Player) -> None:
    game.turn_uid = p.user_id
    game.stage, game.note, game.lines, game.trade = "roll", "", [], {}
    game.trades_this_turn = 0
    game.ev = asyncio.Event()
    game.deadline = _now() + TURN_TIME
    msg = await _send(bot, game.chat_id, _turn_text(game, p), reply_markup=_turn_kb(game, p))
    game.turn_msg_id = msg.message_id
    game.round_msgs.append(msg.message_id)
    if p.is_bot:
        await _pause(game, 1.5)
    else:
        while _is_active(game) and game.stage != "rolled":
            left = game.deadline - _now()
            if left <= 0:
                async with game.edit_lock:
                    if game.stage == "offer":  # второй игрок не ответил — сделки нет
                        game.note = f"Сделка не состоялась: {_e(_partner(game).name)} не ответил."
                        game.stage, game.trade = "roll", {}
                        game.deadline = _now() + TURN_TIME / 2
                        await _show_turn(bot, game, p)
                        continue
                    if game.stage in ("t_who", "t_spot", "t_price"):
                        game.stage, game.trade, game.note = "roll", {}, ""
                        game.deadline = _now() + TURN_TIME / 2
                        await _show_turn(bot, game, p)
                        continue
                    game.stage = "rolled"  # не бросил сам — бросаем за него
                break
            await _wait(game.ev, left)
            game.ev.clear()
    if not _is_active(game):
        return
    async with game.edit_lock:
        game.stage, game.note = "rolling", ""
        game.lines = ["🎲 Кубики катятся…"]
        await _show_turn(bot, game, p)
    dice = []
    for _ in range(2):
        try:
            d = await _retry(bot.send_dice, game.chat_id, emoji="🎲")
            dice.append((d.message_id, d.dice.value))
        except Exception as e:
            logger.info("nefor dice failed: %s", e)
            dice.append((None, random.randint(1, 6)))
    await _pause(game, DICE_WAIT)
    a, b = dice[0][1], dice[1][1]
    game.lines = [f"🎲 {a} + {b} = <b>{a + b}</b>"]
    short: list[str] = []
    await _move(bot, game, p, a + b, short)
    for mid, _v in dice:
        await _delete(bot, game.chat_id, mid)
    if not _is_active(game):
        return
    async with game.edit_lock:
        game.stage = "done"
        game.round_log.append(f"• {_e(p.name)}: {a + b} → {_e(C.cell_name(p.pos))}"
                              + (", " + ", ".join(_e(s) for s in short) if short else ""))
        await _show_turn(bot, game, p)
    await _pause(game, 2.5)


async def _move(bot: Bot, game: Game, p: Player, steps: int, short: list[str], depth: int = 0) -> None:
    old = p.pos
    p.pos = (old + steps) % C.SIZE
    if steps > 0 and old + steps >= C.SIZE:
        p.cash += C.LAP_BONUS
        game.lines.append(f"Проехал Конечную - стипуха <b>+{_rub(C.LAP_BONUS)}</b>")
    await _resolve(bot, game, p, short, depth)


async def _ask(bot: Bot, game: Game, p: Player, options: dict[str, str], bot_choice: str) -> str:
    """Кнопки решения под ходом; бот решает сразу, человек — DECIDE_TIME сек, потом «pass»."""
    if p.is_bot:
        return bot_choice
    async with game.edit_lock:
        game.stage, game.options, game.decision = "decide", options, None
        game.ev = asyncio.Event()
        await _show_turn(bot, game, p)
    deadline = _now() + DECIDE_TIME
    while _is_active(game) and game.decision is None and _now() < deadline:
        await _wait(game.ev, deadline - _now())
    async with game.edit_lock:
        game.stage, game.options = "rolling", {}
        return game.decision or "pass"


async def _resolve(bot: Bot, game: Game, p: Player, short: list[str], depth: int) -> None:
    cell = p.pos
    kind = C.BOARD[cell][0]
    name = C.cell_name(cell)
    lines = game.lines
    if kind == "spot":
        owner = game.owners.get(cell)
        cost = C.price(cell)
        if owner is None:
            lines.append(f"<b>{_e(name)}</b> - точка свободна, {_rub(cost)}.")
            if p.cash < cost:
                lines.append("Денег не хватает - идёт мимо.")
                return
            choice = await _ask(bot, game, p, {"buy": f"Занять за {_rub(cost)}", "pass": "Мимо"},
                                "buy" if p.cash - cost >= 250 else "pass")
            if choice == "buy" and p.cash >= cost and cell not in game.owners:
                p.cash -= cost
                game.owners[cell] = p.user_id
                lines.append(f"Занял точку за <b>{_rub(cost)}</b>.")
                short.append(f"занял за {_rub(cost)}")
            else:
                lines.append("Прошёл мимо.")
        elif owner == p.user_id:
            n = game.roofs.get(cell, 0)
            rc = C.roof_cost(cell)
            lines.append(f"<b>{_e(name)}</b> - своя точка" + (f", крыш: {n}." if n else "."))
            if n >= C.MAX_ROOFS or p.cash < rc:
                return
            choice = await _ask(bot, game, p, {"roof": f"Крыша за {_rub(rc)}", "pass": "Не надо"},
                                "roof" if p.cash - rc >= 300 else "pass")
            if choice == "roof" and p.cash >= rc and game.owners.get(cell) == p.user_id:
                p.cash -= rc
                game.roofs[cell] = n + 1
                lines.append(f"Поставил крышу за <b>{_rub(rc)}</b> - теперь вход {_rub(_rent(game, cell))}.")
                short.append("поставил крышу")
        else:
            o = game.players[owner]
            rent = _rent(game, cell)
            lines.append(f"<b>{_e(name)}</b> - точка {_e(o.name)} ({o.tribe}). За вход: <b>{_rub(rent)}</b>.")
            await _pay(game, p, rent, o, lines, short)
            if not p.out:
                short.append(f"заплатил {_e(o.name)} {_rub(rent)}")
    elif kind == "rumor":
        rk, amount, text = C.rumor()
        gist = C.fmt(text, p.name, p.tribe)
        flavor = None
        if ai.available():
            await _typing(bot, game)
            flavor = await _ai(ai.nefor_line("rumor", p.name, p.tribe, gist, _style(game)), "rumor")
        lines.append(f"<b>Слух.</b> {_e(flavor or gist)}")
        await _rumor_effect(bot, game, p, rk, amount, short, depth)
    elif kind == "pzd":
        good = random.random() < 0.25
        amount = 100 if good else random.choice((60, 90, 120))
        gist = C.fmt(C.PZD_TEXTS[-1] if good else random.choice(C.PZD_TEXTS[:-1]), p.name, p.tribe)
        flavor = None
        if ai.available():
            await _typing(bot, game)
            info = (f"{gist} Сейчас у него {_rub(p.cash)} и {len(_spots(game, p.user_id))} точек. "
                    + ("Пиздюк ДАЁТ ему денег на пиво." if good else "Пиздюк берёт с него штраф."))
            flavor = await _ai(ai.nefor_line("pzd", p.name, p.tribe, info, _style(game)), "pzd")
        lines.append(f"<b>Пиздюк.</b> {_e(flavor or gist)}")
        if good:
            p.cash += amount
            lines.append(f"<b>+{_rub(amount)}</b>")
            short.append(f"Пиздюк дал {_rub(amount)}")
        else:
            lines.append(f"<b>-{_rub(amount)}</b>")
            await _pay(game, p, amount, None, lines, short)
            short.append(f"Пиздюк взял {_rub(amount)}")
    elif kind == "jail":
        p.jail = C.JAIL_TURNS
        lines.append(f"<b>СИЗО.</b> {_e(C.fmt(random.choice(C.JAIL_TEXTS), p.name, p.tribe))} "
                     f"<b>Пропускает {C.JAIL_TURNS} хода.</b>")
        short.append("загремел в СИЗО")
    elif kind == "pps":
        lines.append(f"<b>ППС.</b> {_e(C.fmt(random.choice(C.PPS_TEXTS), p.name, p.tribe))} <b>-{_rub(C.PPS_FINE)}</b>")
        await _pay(game, p, C.PPS_FINE, None, lines, short)
        short.append(f"ППС -{_rub(C.PPS_FINE)}")
    else:
        lines.append("<b>Конечная.</b> Перекур.")


async def _typing(bot: Bot, game: Game) -> None:
    try:
        await bot.send_chat_action(game.chat_id, "typing")
    except Exception:
        pass


async def _rumor_effect(bot: Bot, game: Game, p: Player, kind: str, amount: int, short: list[str],
                        depth: int) -> None:
    lines = game.lines
    others = [o for o in _alive(game) if o.user_id != p.user_id]
    if kind == "gain":
        p.cash += amount
        lines.append(f"<b>+{_rub(amount)}</b>")
        short.append(f"слух +{_rub(amount)}")
    elif kind == "lose":
        lines.append(f"<b>-{_rub(amount)}</b>")
        await _pay(game, p, amount, None, lines, short)
        short.append(f"слух -{_rub(amount)}")
    elif kind == "collect":
        got = 0
        for o in others:
            before = p.cash
            await _pay(game, o, amount, p, lines, [])
            got += p.cash - before
        lines.append(f"Каждый скинулся по {_rub(amount)}: <b>+{_rub(got)}</b>")
        short.append(f"собрал {_rub(got)}")
    elif kind == "treat":
        for o in others:
            if p.out:
                break
            await _pay(game, p, amount, o, lines, short)
        lines.append(f"Всем по {_rub(amount)}: <b>-{_rub(amount * len(others))}</b>")
        short.append(f"проставился {_rub(amount * len(others))}")
    elif kind == "step":
        lines.append(f"<b>{'Вперёд' if amount > 0 else 'Назад'} на {abs(amount)} клетки.</b>")
        if depth == 0:
            await _move(bot, game, p, amount, short, depth + 1)
    elif kind == "tax":
        n = len(_spots(game, p.user_id))
        if n:
            lines.append(f"По {_rub(amount)} за каждую точку: <b>-{_rub(amount * n)}</b>")
            await _pay(game, p, amount * n, None, lines, short)
            short.append(f"проверка -{_rub(amount * n)}")
        else:
            lines.append("Точек нет - проверка ушла ни с чем.")


# ---------- кнопки хода ----------


async def _turn_call(call: CallbackQuery, stages: tuple[str, ...], who: str = "turn") -> tuple[Game, Player] | None:
    """Кнопка текущего хода: то самое сообщение, нужный этап, нажал тот, кому положено."""
    game = games.get(call.message.chat.id) if call.message else None
    if not game or game.phase != "play" or call.message.message_id != game.turn_msg_id:
        await call.answer("Этот ход уже прошёл.")
        return None
    p = game.players.get(game.turn_uid)
    if p is None or game.stage not in stages:
        await call.answer()
        return None
    expected = game.trade.get("with") if who == "partner" else p.user_id
    if call.from_user.id != expected:
        await call.answer("Сейчас не твоя кнопка." if call.from_user.id in game.players else "Ты не в этой игре.",
                          show_alert=True)
        return None
    return game, p


@router.callback_query(F.data == "nf:roll")
async def cb_roll(call: CallbackQuery) -> None:
    got = await _turn_call(call, ("roll",))
    if not got:
        return
    game, _p = got
    async with game.edit_lock:
        if game.stage != "roll":
            await call.answer()
            return
        game.stage = "rolled"
    await call.answer()
    if game.ev:
        game.ev.set()


@router.callback_query(F.data.startswith("nf:do:"))
async def cb_decide(call: CallbackQuery) -> None:
    got = await _turn_call(call, ("decide",))
    if not got:
        return
    game, _p = got
    key = (call.data or "").split(":", 2)[2]
    if key not in game.options:
        await call.answer()
        return
    game.decision = key
    await call.answer()
    if game.ev:
        game.ev.set()


@router.callback_query(F.data == "nf:trade")
async def cb_trade(call: CallbackQuery) -> None:
    got = await _turn_call(call, ("roll",))
    if not got:
        return
    game, p = got
    if game.trades_this_turn >= MAX_TRADES:
        await call.answer(f"Не больше {MAX_TRADES} сделок за ход.", show_alert=True)
        return
    await call.answer()
    async with game.edit_lock:
        game.stage, game.trade = "t_who", {}
        game.note = "<b>Сделка.</b> С кем?"
        game.deadline = _now() + TRADE_TIME
        await _show_turn(call.bot, game, p)


@router.callback_query(F.data == "nf:back")
async def cb_back(call: CallbackQuery) -> None:
    got = await _turn_call(call, ("t_who", "t_spot", "t_price"))
    if not got:
        return
    game, p = got
    await call.answer()
    async with game.edit_lock:
        game.stage, game.trade, game.note = "roll", {}, ""
        game.deadline = max(game.deadline, _now() + TURN_TIME / 2)
        await _show_turn(call.bot, game, p)


@router.callback_query(F.data.startswith("nf:tw:"))
async def cb_trade_who(call: CallbackQuery) -> None:
    got = await _turn_call(call, ("t_who",))
    if not got:
        return
    game, p = got
    try:
        uid = int((call.data or "").split(":")[2])
    except ValueError:
        await call.answer()
        return
    other = game.players.get(uid)
    if other is None or other.out or uid == p.user_id:
        await call.answer()
        return
    if not _spots(game, uid) and not _spots(game, p.user_id):
        await call.answer("Ни у тебя, ни у него нет точек - меняться нечем.", show_alert=True)
        return
    await call.answer()
    async with game.edit_lock:
        game.stage, game.trade = "t_spot", {"with": uid}
        game.note = f"<b>Сделка с {_e(other.name)}.</b> Какую точку?"
        game.deadline = _now() + TRADE_TIME
        await _show_turn(call.bot, game, p)


def _trade_prices(game: Game, p: Player) -> list[int]:
    t = game.trade
    other = game.players.get(t.get("with"))
    if not other or "cell" not in t:
        return []
    value = _value(game, t["cell"])
    payer = p if t["kind"] == "b" else other
    out = []
    for k in PRICE_STEPS:
        v = max(10, round(value * k / 10) * 10)
        if v <= payer.cash and v not in out:
            out.append(v)
    return out


@router.callback_query(F.data.startswith("nf:ts:"))
async def cb_trade_spot(call: CallbackQuery) -> None:
    got = await _turn_call(call, ("t_spot",))
    if not got:
        return
    game, p = got
    try:
        _, _, kind, raw = (call.data or "").split(":")
        cell = int(raw)
    except ValueError:
        await call.answer()
        return
    other = game.players.get(game.trade.get("with"))
    owner = game.owners.get(cell)
    if other is None or kind not in ("b", "s") or owner != (other.user_id if kind == "b" else p.user_id):
        await call.answer()
        return
    game.trade.update(kind=kind, cell=cell)
    if not _trade_prices(game, p):
        game.trade.pop("cell", None)
        await call.answer("Денег на такую сделку не хватает." if kind == "b"
                          else f"У {other.name} не хватит денег.", show_alert=True)
        return
    await call.answer()
    async with game.edit_lock:
        game.stage = "t_price"
        verb = "Купить" if kind == "b" else "Продать"
        game.note = (f"<b>Сделка с {_e(other.name)}.</b> {verb} «{_e(C.cell_name(cell))}» "
                     f"(стоит {_rub(_value(game, cell))}). За сколько?")
        game.deadline = _now() + TRADE_TIME
        await _show_turn(call.bot, game, p)


def _partner(game: Game) -> Player:
    return game.players[game.trade["with"]]


def _offer_text(game: Game, p: Player) -> str:
    t = game.trade
    what = "купить у тебя" if t["kind"] == "b" else "продать тебе"
    return (f"<b>Сделка.</b> {_e(p.name)} предлагает {_e(_partner(game).name)} {what} "
            f"«{_e(C.cell_name(t['cell']))}» за <b>{_rub(t['price'])}</b>.")


def _bot_accepts(game: Game, bot_p: Player) -> bool:
    t = game.trade
    value = _value(game, t["cell"])
    if t["kind"] == "b":  # покупают у бота
        return t["price"] >= value * 1.5
    return t["price"] <= value and bot_p.cash - t["price"] >= 200


@router.callback_query(F.data.startswith("nf:tp:"))
async def cb_trade_price(call: CallbackQuery) -> None:
    got = await _turn_call(call, ("t_price",))
    if not got:
        return
    game, p = got
    try:
        value = int((call.data or "").split(":")[2])
    except ValueError:
        await call.answer()
        return
    if value not in _trade_prices(game, p):
        await call.answer()
        return
    await call.answer()
    async with game.edit_lock:
        game.trade["price"] = value
        other = _partner(game)
        if other.is_bot:
            ok = _bot_accepts(game, other)
            game.note = _offer_text(game, p) + "\n" + (_apply_trade(game, p) if ok else f"{_e(other.name)} отказался.")
            game.stage, game.trade = "roll", {}
            game.deadline = max(game.deadline, _now() + TURN_TIME / 2)
        else:
            game.stage = "offer"
            game.note = _offer_text(game, p) + f"\n\n{_e(other.name)}, решай."
            game.deadline = _now() + TRADE_TIME
        await _show_turn(call.bot, game, p)


def _apply_trade(game: Game, p: Player) -> str:
    """Проводит сделку (под замком); возвращает строку итога."""
    t = game.trade
    other = _partner(game)
    cell, price = t["cell"], t["price"]
    seller, buyer = (other, p) if t["kind"] == "b" else (p, other)
    if game.owners.get(cell) != seller.user_id or buyer.cash < price or other.out:
        return "Сделка сорвалась: условия уже не те."
    buyer.cash -= price
    seller.cash += price
    game.owners[cell] = buyer.user_id  # крыши уходят вместе с точкой
    p.trades += 1
    other.trades += 1
    game.trades_this_turn += 1
    game.round_log.append(f"• сделка: «{_e(C.cell_name(cell))}» → {_e(buyer.name)} за {_rub(price)}")
    return f"✅ По рукам: «{_e(C.cell_name(cell))}» теперь у {_e(buyer.name)}."


@router.callback_query(F.data.in_({"nf:ta", "nf:td"}))
async def cb_trade_answer(call: CallbackQuery) -> None:
    game = games.get(call.message.chat.id) if call.message else None
    accept = call.data == "nf:ta"
    # отозвать предложение может и тот, кто его сделал
    who = "partner"
    if game and not accept and game.turn_uid == call.from_user.id:
        who = "turn"
    got = await _turn_call(call, ("offer",), who=who)
    if not got:
        return
    game, p = got
    await call.answer()
    async with game.edit_lock:
        if game.stage != "offer":
            return
        offer = _offer_text(game, p)
        if accept:
            game.note = offer + "\n" + _apply_trade(game, p)
        elif who == "turn":
            game.note = offer + "\nПредложение отозвано."
        else:
            game.note = offer + f"\n❌ {_e(_partner(game).name)} отказался."
        game.stage, game.trade = "roll", {}
        game.deadline = max(game.deadline, _now() + TURN_TIME / 2)
        await _show_turn(call.bot, game, p)


# ---------- финал ----------


async def _finish(bot: Bot, game: Game) -> None:
    game.phase = "ended"
    ranking = _standings(game)
    win = ranking[0]
    cap = _capital(game, win)
    rows = [{"name": p.name, "tribe": p.tribe, "color": p.color, "icon": p.icon, "capital": _capital(game, p),
             "spots": len(_spots(game, p.user_id)), "out": p.out} for p in ranking]
    epi = None
    if ai.available():
        await _typing(bot, game)
        epi = await _ai(ai.nefor_epilogue(
            win.name, win.tribe, [f"{r['name']} ({r['tribe']}): " + ("банкрот" if r["out"] else f"{r['capital']}₽, "
                                  f"точек {r['spots']}") for r in rows], _style(game)), "epilogue")
    epi = epi or random.choice(C.EPILOGUE).replace("{winner}", win.tribe)
    for mid in game.round_msgs:
        await _delete(bot, game.chat_id, mid)
    game.round_msgs.clear()
    await _send(bot, game.chat_id, f"🏁 <b>ПАРТИЯ ОКОНЧЕНА</b>\n{HR}\n\nПобедил <b>{_e(win.name)}</b> "
                                   f"({win.tribe}) - капитал {_rub(cap)}.\n\n{_e(epi)}")
    await asyncio.sleep(2 * TIME_SCALE)
    try:
        png = await asyncio.to_thread(R.render_results, rows, game.serial, date.today().strftime("%d.%m.%Y"))
        await _retry(bot.send_photo, game.chat_id, BufferedInputFile(png, filename="results.png"),
                     caption="/nefor - ещё раз\n\n/nefor_top - рейтинг")
    except Exception as e:
        logger.warning("nefor results failed: %s", e)
        await _send(bot, game.chat_id, "\n".join(f"{i + 1}. {_e(r['name'])} - "
                                                 + ("банкрот" if r["out"] else _rub(r["capital"]))
                                                 for i, r in enumerate(rows)))
    if game.db:
        results = [{"user_id": p.user_id, "name": p.name, "games": 1, "wins": int(p is win),
                    "best": _capital(game, p), "trades": p.trades, "bankrupt": int(p.out),
                    "spots": len(_spots(game, p.user_id))} for p in ranking if not p.is_bot]
        try:
            await game.db.record_nefor_game(results)
        except Exception as e:
            logger.warning("nefor stats save failed: %s", e)
    _end_game(game)


# ---------- рейтинг ----------


@router.message(Command("nefor_top"))
async def cmd_nefor_top(message: Message, command: CommandObject, db: Database) -> None:
    """Рейтинг по победам; ответом на сообщение или /nefor_top @ник — профиль игрока."""
    rt = message.reply_to_message
    arg = (command.args or "").strip()
    uid, name = None, ""
    if rt and rt.from_user and not rt.from_user.is_bot:
        uid, name = rt.from_user.id, rt.from_user.first_name or "Игрок"
    elif arg.startswith("@"):
        found = await db.find_user(message.chat.id, arg.split()[0])
        if not found:
            await message.answer("Не знаю такого. Пусть сначала что-нибудь напишет в чат.")
            return
        uid, name = found[0], found[1] or arg
    if uid is not None:
        prof = await db.nefor_profile(uid)
        if not prof or not prof["games"]:
            await message.answer(f"🎸 {_e(name)} ещё не играл в Нефоров. /nefor - исправить.")
            return
        ttl = C.titles(prof)
        lines = [f"👤 <b>{_e(prof['name'] or name)}</b> · НЕФОРЫ", HR, "",
                 f"Место в рейтинге: <b>{prof['place']}</b> · побед <b>{prof['wins']}</b> из {prof['games']}",
                 f"Лучший капитал: <b>{_rub(prof['best'])}</b> · сделок: {prof['trades']} · "
                 f"банкротств: {prof['bankrupt']}"]
        lines += (["", "<b>Звания</b>"] + [f"{e} <b>{n}</b> - {w}" for e, n, w in ttl]) if ttl else []
        await message.answer("\n".join(lines))
        return
    rows = await db.nefor_top(10)
    if not rows:
        await message.answer("🎸 Рейтинг Нефоров пуст - сыграйте первую партию: /nefor")
        return
    medals = ["🥇", "🥈", "🥉"]
    blocks = [f"🎸 <b>РЕЙТИНГ НЕФОРОВ</b>\n{HR}"]
    for i, r in enumerate(rows):
        place = medals[i] if i < 3 else f"<b>{i + 1}.</b>"
        ttl = C.titles(r)
        blocks.append(f"{place} <b>{_e(r['name'])}</b>"
                      + (f"\n{', '.join(f'{e} {n}' for e, n, _w in ttl[:3])}" if ttl else "")
                      + f"\nПобед: <b>{r['wins']}</b> из {r['games']} · лучший капитал {_rub(r['best'])}")
    blocks.append(f"{HR}\nПрофиль: ответь /nefor_top на сообщение игрока или /nefor_top @ник")
    await message.answer("\n\n".join(blocks))
