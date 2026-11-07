"""Блэкджек против дилера-Пиздюка (идея и макет — с владельцем, 07.11). Без ИИ.

/bj (админ, можно в личку боту) — пост-стол в канал бати (/bj чат — в этот чат): картинка, список игроков с
балансом, кнопки «Присоединиться / Выйти / Начать». Начинает и закрывает стол только тот, кто его открыл.
Раунд: ставка BET с каждого (фишки 🪙, таблица coins: старт START, на мели — DAILY раз в день), по 2 карты, у
дилера одна закрыта; ходят по очереди кнопками «Ещё / Хватит / Удвоить» (жмёт только тот, чей ход; TURN_TIME сек,
потом «хватит» сам), дилер добирает до 17, блэкджек платит 3:2. Между раундами можно войти и выйти.
Картинка стола — render.py, правки поста — одна задача на стол не чаще EDIT_GAP (лимит Telegram). Ставки раунда
пишутся в настройку bj_pending: бот перезапустился посреди раунда — ставки вернутся (_refund_lost). Пока стол
открыт — флаг обновлений (games._flag_needed → running()); простой IDLE_CLOSE — стол закрывается сам.
Под постом с кнопками Telegram прячет строку комментариев — для стола это ок (игра в самом посте).
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           InputMediaPhoto, Message)

from database import Database

from . import render

logger = logging.getLogger("blackjack")
router = Router(name="blackjack")

MSK = timezone(timedelta(hours=3))
BET = 100
START = 1000
DAILY = 100
MAX_PLAYERS = 6
TURN_TIME = 30
IDLE_CLOSE = 30 * 60
EDIT_GAP = 1.5
DECKS = 4
PENDING_KEY = "bj_pending"
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
SUITS = "♠♥♦♣"

QUIPS = {
    "bust": ["Перебор. Жадность фраера сгубила.", "{n}, математика не твоё, да?", "Перебор. Кто ж так тянет, ёбаный стыд.",
             "{n} перебрал. Как обычно, в общем."],
    "hit_high": ["{t} и тянешь? Смело. Тупо, но смело.", "{n} верит в чудеса. Чудес не будет.",
                 "С {t} ещё карту? Ну давай, удиви."],
    "stand_low": ["{n} встал на {t}. Ссыкло.", "Хватит на {t}? Ты серьёзно, бля?", "{t} очков и «хватит». Ну-ну."],
    "double": ["{n} удваивает. Либо гений, либо пиздец.", "Удвоил. Сейчас посмотрим, кто тут лох."],
    "bj": ["Блэкджек у {n}. Повезло дураку.", "{n} с блэкджеком. Не радуйся, это случайность."],
    "dealer_bust": ["Я перебрал. Не привыкайте.", "Перебор у меня. Сегодня ваш день, неудачники.",
                    "Ладно, я перебрал. Радуйтесь, пока можете."],
    "dealer_win": ["Казино всегда в плюсе, сосунки.", "Забираю фишки. Спасибо за донат.",
                   "Дилер выиграл. Как и задумано, блядь."],
    "timeout": ["{n} уснул. Хватит за тебя.", "{n}, ау? Ладно, встаю за тебя."],
    "start": ["Раздаю. Не плачьте потом.", "Поехали. Фишки ваши - будут мои."],
}


def _quip(kind: str, **kw) -> str:
    return random.choice(QUIPS[kind]).format(**kw)


def _value(cards: list) -> int:
    total, aces = 0, 0
    for rank, _suit in cards:
        if rank == "A":
            total, aces = total + 11, aces + 1
        elif rank in ("J", "Q", "K"):
            total += 10
        else:
            total += int(rank)
    while total > 21 and aces:
        total, aces = total - 10, aces - 1
    return total


def _is_bj(cards: list) -> bool:
    return len(cards) == 2 and _value(cards) == 21


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


@dataclass
class Table:
    chat_id: int
    msg_id: int
    owner: tuple[int, str]
    players: list[list] = field(default_factory=list)   # [uid, имя] — за столом
    phase: str = "lobby"                                  # lobby | play | result | closed
    round: int = 0
    deck: list = field(default_factory=list)
    dealer: list = field(default_factory=list)
    hands: dict = field(default_factory=dict)             # uid → карты
    bets: dict = field(default_factory=dict)              # uid → ставка (с удвоением)
    done: set = field(default_factory=set)
    order: list = field(default_factory=list)             # кто играет этот раунд
    turn: int = 0
    deadline: float = 0.0
    results: dict = field(default_factory=dict)           # uid → (текст, хорошо?, изменение, баланс)
    session: dict = field(default_factory=dict)           # uid → итог за вечер
    quip: str = ""
    last_action: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    dirty: int = 0
    painter: asyncio.Task | None = None
    ticker: asyncio.Task | None = None
    last_edit: float = 0.0

    @property
    def key(self) -> str:
        return f"{self.chat_id}:{self.msg_id}"

    def name_of(self, uid: int) -> str:
        return next((n for u, n in self.players if u == uid), "?")


_tables: dict[str, Table] = {}
_bg: set[asyncio.Task] = set()


def running() -> bool:
    """Живой стол — сервер не перезапускаем (иначе раунд оборвётся). Только если за ним что-то происходит: стол, где
    давно никто ничего не жмёт (или упал таймер, который его закрывает), обновления не держит."""
    now = time.time()
    return any(t.phase != "closed" and now - t.last_action < (10 * 60 if t.phase == "play" else IDLE_CLOSE)
               for t in _tables.values())


def _name(u) -> str:
    return (u.first_name or u.username or "Игрок") if u else "Игрок"


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _bg.add(task)
    task.add_done_callback(_bg.discard)
    return task


# ---------- ставки, которые надо вернуть после перезапуска ----------


async def _pending(db: Database) -> dict:
    try:
        return json.loads(await db.get_setting(PENDING_KEY, "") or "{}")
    except ValueError:
        return {}


async def _save_pending(db: Database, t: Table, bets: dict | None) -> None:
    p = await _pending(db)
    if bets:
        p[t.key] = {str(u): b for u, b in bets.items()}
    else:
        p.pop(t.key, None)
    await db.set_setting(PENDING_KEY, json.dumps(p))


async def _refund_lost(db: Database) -> None:
    """Раунды, оборванные перезапуском: стола в памяти нет — ставки назад."""
    p = await _pending(db)
    lost = {k: v for k, v in p.items() if k not in _tables}
    if not lost:
        return
    for bets in lost.values():
        for uid, bet in bets.items():
            await db.add_coins(int(uid), int(bet))
    for k in lost:
        p.pop(k, None)
    await db.set_setting(PENDING_KEY, json.dumps(p))
    logger.info("блэкджек: вернул ставки оборванных раундов (%d столов)", len(lost))


# ---------- вид ----------


def _kb(t: Table) -> InlineKeyboardMarkup | None:
    if t.phase == "closed":
        return None
    if t.phase == "play":
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🃏 Ещё", callback_data="bj:hit"),
            InlineKeyboardButton(text="✋ Хватит", callback_data="bj:stand"),
            InlineKeyboardButton(text="💰 Удвоить", callback_data="bj:double")]])
    seat = [InlineKeyboardButton(text="➕ Присоединиться", callback_data="bj:join"),
            InlineKeyboardButton(text="🚪 Выйти", callback_data="bj:leave")]
    if t.phase == "lobby":
        return InlineKeyboardMarkup(inline_keyboard=[seat, [InlineKeyboardButton(
            text=f"▶️ Начать (только {t.owner[1]})", callback_data="bj:start")]])
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔁 Следующий раунд", callback_data="bj:start"),
        InlineKeyboardButton(text="🛑 Закрыть стол", callback_data="bj:close")], seat])


async def _view(t: Table, db: Database) -> tuple[bytes, str]:
    """Картинка и подпись (HTML) для текущего состояния стола."""
    if t.phase in ("lobby", "closed") or (t.phase == "result" and not t.results):
        lines = ["🃏 <b>Блэкджек · " + ("стол закрыт" if t.phase == "closed" else "стол открыт") + "</b>",
                 f"Ставка {BET} · до {MAX_PLAYERS} игроков", ""]
        if t.phase == "closed":
            if t.session:
                lines.append("<b>Итог за вечер:</b>")
                for uid, net in sorted(t.session.items(), key=lambda kv: -kv[1]):
                    lines.append(f"{html.escape(t.name_of(uid))}: {'+' if net > 0 else ''}{_fmt(net)} 🪙")
        else:
            lines.append(f"<b>Игроки ({len(t.players)}):</b>" if t.players else "Пока никого - жми «Присоединиться».")
            for uid, name in t.players:
                lines.append(f"{html.escape(name)} - {_fmt(await db.coins_of(uid, name, START))} 🪙")
        if t.quip:
            lines += ["", f"Пиздюк: «{html.escape(t.quip)}»"]
        lines = [x for i, x in enumerate(lines) if x or (i and lines[i - 1])]  # без пустых строк подряд
        return await asyncio.to_thread(render.render_lobby, len(t.players), BET), "\n".join(lines)
    reveal = t.phase == "result"
    dealer = t.dealer if reveal else [t.dealer[0], None]
    if reveal:
        dv = _value(t.dealer)
        dealer_total = "блэкджек" if _is_bj(t.dealer) else (f"{dv} - перебор" if dv > 21 else str(dv))
    else:
        dealer_total = f"{_value([t.dealer[0]])} + ?"
    rows = []
    for uid in t.order:
        cards = t.hands[uid]
        v = _value(cards)
        total = "BJ!" if _is_bj(cards) else str(v)
        res = None
        if reveal and uid in t.results:
            res = (t.results[uid][0], t.results[uid][1])
        elif v > 21:
            res = ("ПЕРЕБОР", False)
        rows.append({"name": t.name_of(uid), "cards": cards, "total": total, "bet": t.bets[uid], "result": res})
    turn = None if reveal else t.turn
    title = f"РАУНД {t.round} · ИТОГ" if reveal else f"РАУНД {t.round}"
    png = await asyncio.to_thread(render.render_table, title, dealer, dealer_total, rows, turn)
    if reveal:
        dv = _value(t.dealer)
        head = "у дилера перебор" if dv > 21 else ("у дилера блэкджек" if _is_bj(t.dealer) else f"у дилера {dv}")
        lines = [f"🃏 <b>Итог раунда {t.round}:</b> {head}"]
        for uid in t.order:
            if uid not in t.results:
                continue
            text, _good, delta, bal = t.results[uid]
            sign = "+" if delta > 0 else ""
            lines.append(f"{html.escape(t.name_of(uid))} {sign}{_fmt(delta)} → {_fmt(bal)} 🪙"
                         + (" (блэкджек)" if text.startswith("+") and _is_bj(t.hands[uid]) else ""))
    else:
        cur = t.order[t.turn]
        left = max(0, round(t.deadline - time.time()))
        lines = [f"🃏 <b>Раунд {t.round} · ходит {html.escape(t.name_of(cur))}</b> (осталось {left} сек)"]
    if t.quip:
        lines.append(f"Пиздюк: «{html.escape(t.quip)}»")
    return png, "\n".join(lines)


async def _paint(bot: Bot, db: Database, t: Table) -> None:
    """Одна задача на стол: перерисовать пост, не чаще EDIT_GAP, всегда последнее состояние."""
    try:
        while True:
            wait = t.last_edit + EDIT_GAP - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            ver = t.dirty
            png, caption = await _view(t, db)
            try:
                await bot.edit_message_media(
                    chat_id=t.chat_id, message_id=t.msg_id, reply_markup=_kb(t),
                    media=InputMediaPhoto(media=BufferedInputFile(png, "table.png"), caption=caption[:1024],
                                          parse_mode="HTML"))
            except TelegramRetryAfter as e:
                t.last_edit = time.time() + e.retry_after
                continue
            except TelegramBadRequest as e:
                if "not modified" not in str(e):
                    logger.warning("блэкджек: пост не обновился: %s", e)
            t.last_edit = time.time()
            if t.dirty == ver:
                return
    except Exception:  # noqa: BLE001
        logger.exception("блэкджек: перерисовка стола")
    finally:
        t.painter = None


def _refresh(bot: Bot, db: Database, t: Table) -> None:
    t.dirty += 1
    t.last_action = time.time()
    if t.painter is None:
        t.painter = _spawn(_paint(bot, db, t))


# ---------- раунд ----------


def _draw(t: Table) -> tuple[str, str]:
    if len(t.deck) < 15:
        t.deck = [(r, s) for r in RANKS for s in SUITS] * DECKS
        random.shuffle(t.deck)
    return t.deck.pop()


async def _start_round(bot: Bot, db: Database, t: Table) -> str:
    """Собрать ставки и раздать. Возвращает подсказку для кнопки (пусто — всё ок)."""
    today = datetime.now(MSK).strftime("%Y-%m-%d")
    bets, order, broke = {}, [], []
    for uid, name in t.players:  # ставки — в свои переменные: стол меняем разом, без await посередине
        bal = await db.coins_of(uid, name, START)  # (иначе перерисовка ловила полуготовый раунд)
        if bal < BET:
            bal += await db.coins_bonus(uid, today, DAILY, BET)
        if bal < BET:
            broke.append(name)
            continue
        await db.add_coins(uid, -BET)
        bets[uid] = BET
        order.append(uid)
    if not order:
        return "Все на мели - играть некому."
    t.round += 1
    t.hands, t.bets, t.results, t.order, t.done = {}, bets, {}, order, set()
    t.dealer = [_draw(t), _draw(t)]
    for uid in t.order:
        t.hands[uid] = [_draw(t), _draw(t)]
        if _is_bj(t.hands[uid]):
            t.done.add(uid)
    t.phase = "play"
    t.turn, t.deadline = 0, time.time() + TURN_TIME
    t.quip = _quip("start") + (f" На мели: {', '.join(broke)}." if broke else "")
    await _save_pending(db, t, t.bets)
    if _is_bj(t.dealer) or len(t.done) == len(t.order):  # у дилера блэкджек — сразу итог
        await _finish(db, t)
    else:
        t.turn = next(i for i, u in enumerate(t.order) if u not in t.done)
        t.deadline = time.time() + TURN_TIME
    _refresh(bot, db, t)
    return ""


def _advance(t: Table) -> bool:
    """Следующий игрок. False — ходить больше некому (пора дилеру)."""
    for i in range(t.turn + 1, len(t.order)):
        if t.order[i] not in t.done:
            t.turn, t.deadline = i, time.time() + TURN_TIME
            return True
    return False


async def _finish(db: Database, t: Table) -> None:
    """Дилер открывает карту и добирает до 17, расчёт."""
    if not all(_value(t.hands[u]) > 21 for u in t.order):  # все перебрали — дилеру тянуть незачем
        while _value(t.dealer) < 17:
            t.dealer.append(_draw(t))
    dv, dbj = _value(t.dealer), _is_bj(t.dealer)
    dealer_won_all = True
    for uid in t.order:
        cards, bet = t.hands[uid], t.bets[uid]
        v, bj = _value(cards), _is_bj(cards)
        if v > 21:
            back, text, good = 0, "ПЕРЕБОР", False
        elif bj and not dbj:
            back, text, good = bet * 5 // 2, f"+{bet * 3 // 2}", True
        elif dbj and not bj:
            back, text, good = 0, f"-{bet}", False
        elif dv > 21 or v > dv:
            back, text, good = bet * 2, f"+{bet}", True
        elif v == dv:
            back, text, good = bet, "ничья", None
        else:
            back, text, good = 0, f"-{bet}", False
        bal = await db.add_coins(uid, back)
        delta = back - bet
        t.session[uid] = t.session.get(uid, 0) + delta
        t.results[uid] = (text, good, delta, bal)
        if good:
            dealer_won_all = False
    await _save_pending(db, t, None)
    t.phase = "result"
    if dv > 21:
        t.quip = _quip("dealer_bust")
    elif dealer_won_all:
        t.quip = _quip("dealer_win")


async def _act(bot: Bot, db: Database, t: Table, uid: int, action: str) -> str:
    """Ход игрока: hit / stand / double / timeout. Возвращает текст для всплывашки."""
    cards = t.hands[uid]
    name = t.name_of(uid)
    note = ""
    if action == "double":
        if len(cards) != 2:
            return "Удвоить можно только на первых двух картах."
        bal = await db.coins_of(uid, name, START)
        if bal < t.bets[uid]:
            return "Фишек на удвоение не хватает."
        await db.add_coins(uid, -t.bets[uid])
        t.bets[uid] *= 2
        await _save_pending(db, t, t.bets)
        cards.append(_draw(t))
        t.done.add(uid)
        t.quip = _quip("bust", n=name) if _value(cards) > 21 else _quip("double", n=name)
        note = f"Удвоил: {cards[-1][0]}{cards[-1][1]}, у тебя {_value(cards)}"
    elif action == "hit":
        before = _value(cards)
        cards.append(_draw(t))
        v = _value(cards)
        note = f"{cards[-1][0]}{cards[-1][1]} - у тебя {v}"
        if v > 21:
            t.done.add(uid)
            t.quip = _quip("bust", n=name)
        elif v == 21:
            t.done.add(uid)
            t.quip = ""
        else:
            t.quip = _quip("hit_high", n=name, t=before) if before >= 14 else ""
        if v < 21:
            t.deadline = time.time() + TURN_TIME  # взял карту — снова полное время
    else:  # stand / timeout
        t.done.add(uid)
        v = _value(cards)
        t.quip = _quip("timeout", n=name) if action == "timeout" else (
            _quip("stand_low", n=name, t=v) if v <= 13 else "")
        note = f"Стоишь на {v}"
    if uid in t.done and not _advance(t):
        await _finish(db, t)
    _refresh(bot, db, t)
    return note


async def _tick(bot: Bot, db: Database, t: Table) -> None:
    """Раз в секунду: просроченный ход — «хватит» сам; давно ничего не происходит — закрыть стол."""
    try:
        while t.phase != "closed":
            await asyncio.sleep(1)
            async with t.lock:
                if t.phase == "play" and time.time() > t.deadline:
                    await _act(bot, db, t, t.order[t.turn], "timeout")
                elif t.phase in ("lobby", "result") and time.time() - t.last_action > IDLE_CLOSE:
                    t.phase, t.quip = "closed", "Все уснули. Стол закрыт."
                    _refresh(bot, db, t)
    except Exception:  # noqa: BLE001
        logger.exception("блэкджек: таймер стола")


# ---------- команды и кнопки ----------


@router.message(Command("bj"))
async def cmd_bj(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    """/bj — стол в канал бати; /bj чат — в этот чат."""
    from ephemeral import temp_reply
    from handlers.channel_posts import channel_id

    await _refund_lost(db)
    here = (command.args or "").strip().lower() in ("чат", "здесь", "тут", "here")
    target = message.chat.id if here and message.chat.type in ("group", "supergroup") else await channel_id(bot, db)
    if not target:
        if message.chat.type in ("group", "supergroup"):
            target = message.chat.id
        else:
            await message.answer("Не нашёл канал. Напиши /bj в группе - стол откроется там.")
            return
    owner = (message.from_user.id, _name(message.from_user))
    try:
        sent = await bot.send_photo(target, BufferedInputFile(render.render_lobby(0, BET), "table.png"),
                                    caption="🃏 <b>Блэкджек · стол открыт</b>", parse_mode="HTML")
    except TelegramBadRequest as e:
        await temp_reply(message, f"Не смог открыть стол: {html.escape(str(e))}")
        return
    t = Table(chat_id=target, msg_id=sent.message_id, owner=owner)
    _tables[t.key] = t
    if len(_tables) > 50:
        for k in [k for k, x in _tables.items() if x.phase == "closed"]:
            _tables.pop(k, None)
    t.ticker = _spawn(_tick(bot, db, t))
    _refresh(bot, db, t)
    logger.info("блэкджек: стол %s открыл %s", t.key, owner[1])
    if message.chat.id != target and message.chat.type == "private":
        await message.answer("🃏 Стол открыт в канале. Жди игроков и жми «Начать».")


@router.callback_query(F.data.startswith("bj:"))
async def cb_table(call: CallbackQuery, bot: Bot, db: Database) -> None:
    if not call.message:
        await call.answer()
        return
    t = _tables.get(f"{call.message.chat.id}:{call.message.message_id}")
    if not t or t.phase == "closed":
        await _refund_lost(db)
        await call.answer("Стол закрыт.", show_alert=True)
        return
    action = (call.data or "")[3:]
    uid, name = call.from_user.id, _name(call.from_user)
    async with t.lock:
        note, alert = "", False
        seated = any(u == uid for u, _n in t.players)
        if action == "join":
            if seated:
                note = "Ты уже за столом."
            elif len(t.players) >= MAX_PLAYERS:
                note, alert = f"Мест нет - максимум {MAX_PLAYERS}.", True
            else:
                bal = await db.coins_of(uid, name, START)
                if bal < BET:
                    bal += await db.coins_bonus(uid, datetime.now(MSK).strftime("%Y-%m-%d"), DAILY, BET)
                if bal < BET:
                    note, alert = "Ты на мели. Пособие - 100 🪙 раз в день, приходи завтра.", True
                else:
                    t.players.append([uid, name])
                    note = f"Ты за столом. Баланс: {_fmt(bal)} 🪙" + (" (раунд уже идёт - сыграешь в следующем)"
                                                                        if t.phase == "play" else "")
                    _refresh(bot, db, t)
        elif action == "leave":
            if not seated:
                note = "Тебя и так нет за столом."
            elif t.phase == "play" and uid in t.order:
                note, alert = "Доиграй раунд - потом выходи.", True
            else:
                t.players = [p for p in t.players if p[0] != uid]
                note = "Ты вышел из-за стола."
                _refresh(bot, db, t)
        elif action in ("start", "close"):
            if uid != t.owner[0]:
                note, alert = f"Это решает {t.owner[1]} - он открыл стол.", True
            elif t.phase == "play":
                note = "Раунд ещё идёт."
            elif action == "close":
                t.phase, t.quip = "closed", "Стол закрыт. Казино благодарит за донаты."
                note = "Стол закрыт."
                _refresh(bot, db, t)
            elif not t.players:
                note, alert = "За столом никого.", True
            else:
                note = await _start_round(bot, db, t) or "Раздаю!"
        elif action in ("hit", "stand", "double"):
            if t.phase != "play":
                note = "Сейчас не раунд."
            elif uid != t.order[t.turn]:
                note = f"Сейчас ходит {t.name_of(t.order[t.turn])}." if uid not in t.order else "Не твой ход - жди."
            else:
                note = await _act(bot, db, t, uid, action)
        else:
            note = ""
    await call.answer(note, show_alert=alert)


@router.message(Command("bj_top"))
async def cmd_bj_top(message: Message, db: Database) -> None:
    """Самые богатые по фишкам."""
    rows = await db.coins_top(10)
    if not rows:
        await message.answer("🪙 Фишек ещё ни у кого нет - открой стол: /bj")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🪙 <b>Богачи блэкджека</b>", ""]
    for i, (name, bal) in enumerate(rows):
        lines.append(f"{medals[i] if i < 3 else f'{i + 1}.'} <b>{html.escape(name or '?')}</b> - {_fmt(bal)} 🪙")
    await message.answer("\n".join(lines))
