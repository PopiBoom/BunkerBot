"""Игра «Бункер» для группового чата. Ведущий — Claude.

Поток:
  /bunker → лобби (вход по кнопке-ссылке через бота) → старт → ведущий генерирует
  катастрофу, бункер и абсурдные карточки → карточки уходят игрокам в ЛС →
  по раундам игроки ПО ОЧЕРЕДИ (бот тегает каждого, таймер 60 сек) выбирают в ЛС,
  какую характеристику открыть → голосование кого не пускать → эпилог.

Между сообщениями — паузы, чтобы история шла постепенно.
Состояние игр хранится в памяти процесса (по одному на чат).
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import random
from dataclasses import dataclass, field

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ai

logger = logging.getLogger("bunker")
router = Router(name="bunker")

MIN_PLAYERS = 2
MAX_PLAYERS = 16
TURN_TIMEOUT = 60        # сек на ход каждого игрока
VOTE_TIMEOUT = 90        # сек на голосование, потом итог подводится автоматически
LOBBY_TIMEOUT = 15 * 60  # сек: брошенное лобби закрывается само
MAX_TEXT = 3800          # запас до лимита Telegram в 4096 символов
STEP_DELAY = float(os.getenv("BUNKER_DELAY", "5") or 5)  # пауза между сюжетными сообщениями
QUIP_DELAY = max(2.0, STEP_DELAY - 1)

HR = "━━━━━━━━━━━━━━━"

ATTRS = [
    ("profession", "Профессия", "💼"),
    ("bio", "Данные", "🧍"),
    ("health", "Здоровье", "❤️"),
    ("hobby", "Хобби", "🎯"),
    ("baggage", "Багаж", "🎒"),
    ("phobia", "Фобия", "😱"),
    ("fact", "Факт", "🔎"),
    ("ability", "Способность", "✨"),
]
ATTR_MAP = {key: (label, emoji) for key, label, emoji in ATTRS}


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


@dataclass
class Game:
    chat_id: int
    host_id: int
    players: dict[int, Player] = field(default_factory=dict)
    phase: str = "lobby"  # lobby | revealing | voting | counting | ended
    catastrophe: str = ""
    bunker: str = ""
    spots: int = 1
    round_no: int = 0
    revealed_this_round: set[int] = field(default_factory=set)
    votes: dict[int, int] = field(default_factory=dict)
    lobby_msg_id: int | None = None
    bot_username: str = ""
    current_picker: int | None = None
    pick_event: asyncio.Event | None = None


games: dict[int, Game] = {}  # chat_id -> Game
_tasks: set[asyncio.Task] = set()  # держим ссылки, чтобы задачи не собрал GC


def _alive(game: Game) -> list[Player]:
    return [p for p in game.players.values() if p.alive]


def _is_active(game: Game) -> bool:
    return games.get(game.chat_id) is game and game.phase != "ended"


def _end_game(game: Game) -> None:
    game.phase = "ended"
    if games.get(game.chat_id) is game:
        games.pop(game.chat_id, None)
    if game.pick_event:
        game.pick_event.set()  # будим цикл раунда, чтобы он сразу завершился


def _launch(bot: Bot, game: Game, coro) -> None:
    """Запускает ход игры отдельной задачей. Если она упала — сообщаем в чат."""

    async def runner() -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("bunker task crashed in chat %s", game.chat_id)
            if _is_active(game):
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


# ---------- отправка / пауза / «печатает» ----------


async def _send(bot: Bot, chat_id: int, text: str, **kwargs) -> Message:
    """send_message, который переживает флуд-лимит Telegram (RetryAfter)."""
    attempts = 4
    for attempt in range(attempts):
        try:
            return await bot.send_message(chat_id, text, **kwargs)
        except TelegramRetryAfter as e:
            if attempt == attempts - 1:
                raise
            logger.info("flood limit in %s, sleeping %ss", chat_id, e.retry_after)
            await asyncio.sleep(e.retry_after + 0.5)
    raise RuntimeError("unreachable")


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


# ---------- запасной генератор (абсурдные карточки) ----------

_PROFS = ["профессиональный дождевой червь", "дегустатор несвежих мемов", "тренер по крику",
          "бывший сантехник Кремля", "массажист осьминогов", "коуч по выживанию в IKEA",
          "ночной сторож пустоты", "продавец воздуха премиум-класса", "фельдшер для роботов",
          "дрессировщик тараканов"]
_BIOS = ["муж., 34, но выглядит на 400", "жен., 28, три почки", "муж., 51, пахнет борщом",
         "жен., 42, рост как у холодильника", "небинарный гриб, 19", "муж., 23, без костей"]
_HEALTH = ["здоров, но душа мёртвая", "аллергия на понедельники", "вместо крови — энергетик",
           "хронический оптимизм", "икает морзянкой", "идеально, пугающе идеально"]
_HOBBY = ["коллекционирует чужие сны", "разговаривает с холодильником", "лепит из пельменей",
          "бокс с тенью (тень побеждает)", "вяжет свитера для голубей", "кричит в подушку профессионально"]
_PHOBIA = ["боязнь собственных коленей", "страх перед вторником", "боязнь тишины в лифте",
           "паника от звука 'пожалуйста'", "боится своей тени (взаимно)", "никаких фобий, кроме людей"]
_BAGGAGE = ["ведро с вопросами", "один носок (счастливый)", "250 кг гречки",
            "портрет Николаса Кейджа", "аптечка без аптечки", "ядерный реактор на батарейках"]
_FACT = ["однажды переспорил навигатор", "тайно управляет погодой в подъезде",
         "ел землю и ему понравилось", "клон, но не признаётся", "выиграл суд у своей совести",
         "умеет моргать громко"]
_ABILITY = ["чинит всё взглядом", "переваривает любой металл", "засыпает по команде",
            "видит Wi-Fi", "умеет быть мебелью", "гипнотизирует одним бровём"]


def _fallback_setup(names: list[str]) -> dict:
    cards = []
    for name in names:
        cards.append({
            "name": name,
            "profession": random.choice(_PROFS),
            "bio": random.choice(_BIOS),
            "health": random.choice(_HEALTH),
            "hobby": random.choice(_HOBBY),
            "phobia": random.choice(_PHOBIA),
            "baggage": random.choice(_BAGGAGE),
            "fact": random.choice(_FACT),
            "ability": random.choice(_ABILITY),
        })
    return {
        "catastrophe": "Небо позеленело, и все кошки планеты одновременно мяукнули в тональности "
                       "ре-минор. Снаружи выжить невозможно — спасёт только бункер.",
        "bunker": "Бункер на 1 год: запас гречки, генератор на честном слове и один туалет на всех. "
                  "Места ограничены — войдут не все.",
        "cards": cards,
    }


# ---------- форматирование ----------


def _e(s: str) -> str:
    return html.escape(str(s))


def _mention(p: Player) -> str:
    if p.username:
        return f"@{p.username}"
    return f'<a href="tg://user?id={p.user_id}">{_e(p.name)}</a>'


def _full_card_text(p: Player) -> str:
    c = p.card or {}
    lines = [f"🧳 <b>Твоя секретная карточка — {_e(p.name)}</b>", HR]
    for key, label, emoji in ATTRS:
        lines.append(f"{emoji} <b>{label}:</b> {_e(c.get(key, '—'))}")
    lines.append(HR)
    lines.append("Каждый раунд выбирай, что открыть. Остальное — твой секрет 😉")
    return "\n".join(lines)


def _chunks(header: str, blocks: list[str], sep: str = "\n\n") -> list[str]:
    """Склеивает блоки в сообщения не длиннее MAX_TEXT; заголовок — в первом."""
    out: list[str] = []
    cur = header
    for block in blocks:
        candidate = f"{cur}{sep}{block}" if cur else block
        if len(candidate) > MAX_TEXT and cur:
            out.append(cur)
            cur = block
        else:
            cur = candidate
    if cur:
        out.append(cur)
    return out


def _dossier_blocks(game: Game) -> list[str]:
    blocks = []
    for p in _alive(game):
        c = p.card or {}
        lines = [f"🧳 <b>{_e(p.name)}</b>"]
        shown = [(k, l, e) for k, l, e in ATTRS if k in p.revealed]
        if shown:
            for key, label, emoji in shown:
                lines.append(f"   {emoji} {label}: {_e(c.get(key, '—'))}")
        else:
            lines.append("   <i>пока ничего не открыто</i>")
        blocks.append("\n".join(lines))
    return blocks


def _lobby_text(game: Game) -> str:
    lines = [f"🏚 <b>БУНКЕР</b>", HR, f"Сбор игроков: <b>{len(game.players)}/{MAX_PLAYERS}</b>", ""]
    for p in game.players.values():
        host = " 👑" if p.user_id == game.host_id else ""
        lines.append(f"• {_e(p.name)}{host}")
    lines.append("")
    lines.append("🎮 Жми «Вступить в игру» — откроется бот, запустится и добавит тебя.")
    lines.append("Туда же, в ЛС, придёт секретная карточка. Ведущий жмёт «Начать».")
    return "\n".join(lines)


def _join_url(game: Game) -> str:
    return f"https://t.me/{game.bot_username}?start=bk_{game.chat_id}"


def _lobby_kb(game: Game) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if game.bot_username:
        b.row(InlineKeyboardButton(text="🎮 Вступить в игру", url=_join_url(game)))
    b.row(_btn("Войти без ЛС ⚡", "bk:join"), _btn("Выйти 🚪", "bk:leave"))
    b.row(_btn("Начать игру ▶️", "bk:start"))
    b.row(_btn("Отмена ❌", "bk:cancel"))
    return b.as_markup()


def _vote_kb(game: Game) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for p in _alive(game):
        count = sum(1 for t in game.votes.values() if t == p.user_id)
        label = f"{p.name} ({count})" if count else p.name
        b.row(_btn(f"🚫 {label}", f"bk:vote:{p.user_id}"))
    b.row(_btn("Подвести итог раунда 🧮", "bk:tally"))
    return b.as_markup()


def _pick_kb(game: Game, p: Player) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key, label, emoji in ATTRS:
        if key not in p.revealed:
            b.row(_btn(f"{emoji} {label}", f"bkpk:{game.chat_id}:{key}"))
    return b.as_markup()


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


# ---------- команды ----------


@router.message(Command("bunker"))
async def cmd_bunker(message: Message) -> None:
    chat_id = message.chat.id
    user = message.from_user
    if user is None:
        return
    if message.chat.type == "private":
        await message.answer("В Бункер играют в групповом чате. Добавь бота в чат и напиши там /bunker.")
        return

    existing = games.get(chat_id)
    if existing and existing.phase != "ended":
        await message.answer("В этом чате уже идёт игра в Бункер. Доиграйте или /bunker_stop.")
        return

    game = Game(chat_id=chat_id, host_id=user.id)
    try:
        game.bot_username = (await message.bot.me()).username or ""
    except Exception:
        game.bot_username = ""
    game.players[user.id] = Player(
        user_id=user.id, name=_unique_name(game, user.first_name), username=user.username
    )
    games[chat_id] = game

    sent = await message.answer(_lobby_text(game), reply_markup=_lobby_kb(game))
    game.lobby_msg_id = sent.message_id
    _launch(message.bot, game, _lobby_timer(message.bot, game))


async def _lobby_timer(bot: Bot, game: Game) -> None:
    await asyncio.sleep(LOBBY_TIMEOUT)
    if games.get(game.chat_id) is not game or game.phase != "lobby":
        return
    _end_game(game)
    try:
        if game.lobby_msg_id:
            await bot.edit_message_text(
                "⌛ Лобби закрыто — игра так и не началась.\n/bunker — собрать заново.",
                chat_id=game.chat_id, message_id=game.lobby_msg_id,
            )
    except Exception:
        pass


@router.message(CommandStart(deep_link=True, magic=F.args.func(lambda a: bool(a) and a.startswith("bk_"))))
async def cmd_deeplink_join(message: Message, command: CommandObject) -> None:
    """Вход в игру по кнопке-ссылке: /start bk_<chat_id> в личке бота."""
    user = message.from_user
    if user is None:
        return
    payload = command.args or ""
    try:
        chat_id = int(payload[3:])
    except ValueError:
        await message.answer("Не понял, в какую игру вступить. Попроси ведущего прислать кнопку заново.")
        return

    game = games.get(chat_id)
    if not game or game.phase != "lobby":
        await message.answer(
            "Игра уже началась или не найдена 🙁\n"
            "Пусть ведущий создаст новую: /bunker в групповом чате."
        )
        return
    if user.id in game.players:
        await message.answer("Ты уже в игре! Жди старта — карточка придёт сюда, в ЛС. 🧳")
        return
    if len(game.players) >= MAX_PLAYERS:
        await message.answer("Увы, мест в лобби больше нет.")
        return

    player = Player(
        user_id=user.id, name=_unique_name(game, user.first_name), username=user.username, dm_ok=True
    )
    game.players[user.id] = player
    await message.answer(
        "✅ Ты в игре «Бункер»!\n"
        "Когда ведущий начнёт — сюда придёт твоя секретная карточка, "
        "и в свой ход ты будешь выбирать здесь, что открыть."
    )
    try:
        await _send(message.bot, chat_id, f"➕ <b>{_e(player.name)}</b> спускается в бункер!")
    except Exception:
        pass
    await _refresh_lobby(message.bot, game)


@router.message(Command("bunker_stop"))
async def cmd_bunker_stop(message: Message) -> None:
    game = games.get(message.chat.id)
    if not game:
        await message.answer("Сейчас нет активной игры.")
        return
    if message.from_user and message.from_user.id != game.host_id:
        await message.answer("Остановить игру может только ведущий (кто её создал).")
        return
    _end_game(game)
    await message.answer("🛑 Игра в Бункер остановлена.")


# ---------- лобби ----------


@router.callback_query(F.data == "bk:join")
async def cb_join(call: CallbackQuery) -> None:
    game = games.get(call.message.chat.id) if call.message else None
    if not game or game.phase != "lobby":
        await call.answer("Сейчас нельзя войти.", show_alert=True)
        return
    uid = call.from_user.id
    if uid in game.players:
        await call.answer("Ты уже в игре.")
        return
    if len(game.players) >= MAX_PLAYERS:
        await call.answer("Мест в лобби больше нет.", show_alert=True)
        return
    player = Player(
        user_id=uid, name=_unique_name(game, call.from_user.first_name), username=call.from_user.username
    )
    game.players[uid] = player
    await call.answer("Ты в игре! (без ЛС — карточка не придёт, ход откроется автоматически)")
    try:
        await _send(call.bot, game.chat_id, f"➕ <b>{_e(player.name)}</b> спускается в бункер!")
    except Exception:
        pass
    await _refresh_lobby(call.bot, game)


@router.callback_query(F.data == "bk:leave")
async def cb_leave(call: CallbackQuery) -> None:
    game = games.get(call.message.chat.id) if call.message else None
    if not game or game.phase != "lobby":
        await call.answer("Сейчас нельзя выйти.", show_alert=True)
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
    await _refresh_lobby(call.bot, game)


@router.callback_query(F.data == "bk:cancel")
async def cb_cancel(call: CallbackQuery) -> None:
    game = games.get(call.message.chat.id) if call.message else None
    if not game:
        await call.answer()
        return
    if call.from_user.id != game.host_id:
        await call.answer("Отменить может только ведущий.", show_alert=True)
        return
    _end_game(game)
    await call.answer("Игра отменена.")
    if call.message:
        await call.message.edit_text("❌ Игра в Бункер отменена.")


async def _refresh_lobby(bot: Bot, game: Game) -> None:
    if game.lobby_msg_id is None:
        return
    try:
        await bot.edit_message_text(
            chat_id=game.chat_id,
            message_id=game.lobby_msg_id,
            text=_lobby_text(game),
            reply_markup=_lobby_kb(game),
        )
    except Exception:
        pass


# ---------- старт игры ----------


@router.callback_query(F.data == "bk:start")
async def cb_start(call: CallbackQuery) -> None:
    game = games.get(call.message.chat.id) if call.message else None
    if not game or game.phase != "lobby":
        await call.answer("Сейчас нельзя начать.", show_alert=True)
        return
    if call.from_user.id != game.host_id:
        await call.answer("Начать игру может только ведущий.", show_alert=True)
        return
    if len(game.players) < MIN_PLAYERS:
        await call.answer(f"Нужно минимум {MIN_PLAYERS} игрока.", show_alert=True)
        return

    await call.answer("Поехали!")
    game.phase = "revealing"
    _launch(call.bot, game, _run_game(call.bot, game))


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
            card = next(
                (c for c in pool
                 if id(c) not in used and str(c.get("name", "")).strip() not in names),
                None,
            )
        if card is not None:
            used.add(id(card))
        base = _fallback_setup([p.name])["cards"][0]
        extra = {
            k: str(v).strip() for k, v in (card or {}).items()
            if k in ATTR_MAP and v is not None and str(v).strip()
        }
        p.card = {**base, **extra, "name": p.name}


async def _run_game(bot: Bot, game: Game) -> None:
    names = [p.name for p in game.players.values()]
    status = await _send(bot, game.chat_id, "🎙 Ведущий готовит сценарий…")

    try:
        setup = await ai.generate_setup(names) if ai.available() else _fallback_setup(names)
    except Exception as e:
        logger.warning("AI setup failed: %s", e)
        setup = _fallback_setup(names)

    game.catastrophe = str(setup.get("catastrophe", "")).strip() or "Произошла катастрофа."
    game.bunker = str(setup.get("bunker", "")).strip() or "Бункер с ограниченным числом мест."

    cards = setup.get("cards")
    players = list(game.players.values())
    _assign_cards(players, cards if isinstance(cards, list) else [])

    game.spots = max(1, len(players) // 2)

    # Карточки — в личку
    no_dm = []
    for p in players:
        try:
            await _send(bot, p.user_id, _full_card_text(p))
            p.dm_ok = True
        except Exception:
            p.dm_ok = False
            no_dm.append(p)

    try:
        await status.delete()
    except Exception:
        pass

    await _say(bot, game.chat_id, f"☢️ <b>КАТАСТРОФА</b>\n{HR}\n{_e(game.catastrophe)}")
    await _say(bot, game.chat_id, f"🏚 <b>БУНКЕР</b>\n{HR}\n{_e(game.bunker)}")

    # Вступительная речь ведущего
    if ai.available():
        try:
            intro = await ai.narrate_intro(game.catastrophe, game.bunker)
            if intro:
                await _say(bot, game.chat_id, f"🎙 <i>{_e(intro)}</i>")
        except Exception as e:
            logger.warning("AI intro failed: %s", e)

    roster = "\n".join(f"• {_e(p.name)}" for p in players)
    await _say(
        bot, game.chat_id,
        f"👥 <b>Игроки ({len(players)})</b>\n{HR}\n{roster}\n\n"
        f"🎟 Мест в бункере: <b>{game.spots}</b>\n"
        f"Карточки разосланы в ЛС. Ходим по очереди, на ход — {TURN_TIMEOUT} сек.",
    )
    if no_dm:
        who = ", ".join(_e(p.name) for p in no_dm)
        await _say(
            bot, game.chat_id,
            f"⚠️ {who}: не удалось написать вам в ЛС. Откройте бота в личке и нажмите "
            f"Start. Пока характеристики будут открываться за вас автоматически.",
        )

    await _reveal_round(bot, game)


# ---------- фаза раскрытия (по очереди, с таймером) ----------


async def _reveal_round(bot: Bot, game: Game) -> None:
    if not _is_active(game):
        return
    game.phase = "revealing"
    game.round_no += 1
    game.revealed_this_round = set()

    await _say(
        bot, game.chat_id,
        f"{HR}\n🔦 <b>Раунд {game.round_no}</b>\nОткрываем характеристики по очереди. "
        f"Каждый выбирает в ЛС у бота (на ход — {TURN_TIMEOUT} сек).",
    )

    for p in list(_alive(game)):
        if not _is_active(game):
            return
        if p.user_id in game.revealed_this_round:
            continue
        remaining = [k for k, _, _ in ATTRS if k not in p.revealed]
        if not remaining:
            game.revealed_this_round.add(p.user_id)
            continue

        game.current_picker = p.user_id
        game.pick_event = asyncio.Event()

        if p.dm_ok:
            sent = False
            try:
                msg = await _send(
                    bot, p.user_id, f"🔦 Раунд {game.round_no}. Твой ход! Что открываешь?",
                    reply_markup=_pick_kb(game, p),
                )
                p.pick_msg_id = msg.message_id
                sent = True
            except Exception:
                p.dm_ok = False
            if sent:
                await _say(
                    bot, game.chat_id,
                    f"👉 Ход {_mention(p)} — выбери характеристику в ЛС у бота. "
                    f"⏳ {TURN_TIMEOUT} сек.",
                    delay=0,
                )
                try:
                    await asyncio.wait_for(game.pick_event.wait(), timeout=TURN_TIMEOUT)
                except asyncio.TimeoutError:
                    if not _is_active(game):
                        return
                    if p.user_id not in game.revealed_this_round and p.user_id not in game.revealed_this_round:
                        rem = [k for k, _, _ in ATTRS if k not in p.revealed]
                        await _reveal_attr(bot, game, p, random.choice(rem), auto=True)
                        try:
                            if p.pick_msg_id:
                                await bot.edit_message_text(
                                    "⏰ Время вышло — характеристика открыта автоматически.",
                                    chat_id=p.user_id, message_id=p.pick_msg_id,
                                )
                        except Exception:
                            pass
                game.current_picker = None
                continue

        # Нет ЛС — открываем сами
        await _say(bot, game.chat_id, f"🤖 У {_mention(p)} нет лички с ботом — открываю за него.", delay=0)
        await _reveal_attr(bot, game, p, random.choice(remaining), auto=True)
        game.current_picker = None

    if _is_active(game):
        await _open_voting(bot, game)


async def _reveal_attr(bot: Bot, game: Game, p: Player, key: str, auto: bool) -> None:
    if key in p.revealed:
        return
    p.revealed.add(key)
    game.revealed_this_round.add(p.user_id)
    label, emoji = ATTR_MAP.get(key, (key, "•"))
    val = (p.card or {}).get(key, "—")
    tail = " <i>(авто)</i>" if auto else ""
    await _say(bot, game.chat_id, f"🔓 <b>{_e(p.name)}</b> → {emoji} <b>{label}:</b> {_e(val)}{tail}")

    # Короткая шутка ведущего
    if ai.available():
        try:
            quip = await ai.narrate_quip(p.name, label, str(val))
            if quip:
                await _say(bot, game.chat_id, f"🎙 <i>{_e(quip)}</i>", delay=QUIP_DELAY)
        except Exception as e:
            logger.warning("AI quip failed: %s", e)


@router.callback_query(F.data.startswith("bkpk:"))
async def cb_pick(call: CallbackQuery) -> None:
    parts = (call.data or "").split(":")
    if len(parts) != 3:
        await call.answer()
        return
    try:
        chat_id = int(parts[1])
    except ValueError:
        await call.answer()
        return
    key = parts[2]
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
    if key not in ATTR_MAP or key in p.revealed:
        await call.answer("Нельзя открыть это.", show_alert=True)
        return

    label, _emoji = ATTR_MAP[key]
    await call.answer(f"Открыто: {label}")
    try:
        if call.message:
            await call.message.edit_text(f"✅ Ты открыл: {label}.")
    except Exception:
        pass
    await _reveal_attr(call.bot, game, p, key, auto=False)
    if game.pick_event:
        game.pick_event.set()


# ---------- голосование ----------


async def _open_voting(bot: Bot, game: Game) -> None:
    game.phase = "voting"
    game.current_picker = None
    game.votes = {}
    parts = _chunks(f"📋 <b>Досье — раунд {game.round_no}</b>\n{HR}", _dossier_blocks(game))
    for part in parts:
        await _say(bot, game.chat_id, part, delay=0 if part is not parts[-1] else STEP_DELAY)
    if not _is_active(game):
        return
    await _say(
        bot, game.chat_id,
        f"🗳 <b>ГОЛОСОВАНИЕ</b>\n{HR}\nВ живых: {len(_alive(game))} · мест: {game.spots}\n"
        f"Выберите, кого НЕ пускать в бункер 👇 (можно переголосовать)\n"
        f"⏳ На голосование {VOTE_TIMEOUT} сек, потом итог подведётся сам.",
        delay=0, reply_markup=_vote_kb(game),
    )
    _launch(bot, game, _vote_timer(bot, game, game.round_no))


def _voting_open(game: Game, round_no: int) -> bool:
    return _is_active(game) and game.phase == "voting" and game.round_no == round_no


async def _vote_timer(bot: Bot, game: Game, round_no: int) -> None:
    warn_at = 30
    await asyncio.sleep(max(0, VOTE_TIMEOUT - warn_at))
    if not _voting_open(game, round_no):
        return
    if VOTE_TIMEOUT > warn_at:
        await _send(
            bot, game.chat_id,
            f"⏳ {warn_at} сек до конца голосования! "
            f"Проголосовали: {len(game.votes)}/{len(_alive(game))}",
        )
        await asyncio.sleep(warn_at)
    if _voting_open(game, round_no):
        await _do_tally(bot, game)


@router.callback_query(F.data.startswith("bk:vote:"))
async def cb_vote(call: CallbackQuery) -> None:
    game = games.get(call.message.chat.id) if call.message else None
    if not game or game.phase != "voting":
        await call.answer("Сейчас не время голосовать.", show_alert=True)
        return
    voter = call.from_user.id
    player = game.players.get(voter)
    if player is None or not player.alive:
        await call.answer("Голосовать могут только живые игроки.", show_alert=True)
        return
    try:
        target = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    if target not in game.players or not game.players[target].alive:
        await call.answer("Этого игрока уже нет.", show_alert=True)
        return

    game.votes[voter] = target
    await call.answer(f"Голос за: {game.players[target].name}")
    try:
        await call.message.edit_reply_markup(reply_markup=_vote_kb(game))
    except Exception:
        pass

    if len(game.votes) >= len(_alive(game)):
        _launch(call.bot, game, _do_tally(call.bot, game))


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
    await call.answer()
    _launch(call.bot, game, _do_tally(call.bot, game))


async def _do_tally(bot: Bot, game: Game) -> None:
    if not _is_active(game) or game.phase != "voting":
        return
    game.phase = "counting"

    counts: dict[int, int] = {}
    for target in game.votes.values():
        if target in game.players and game.players[target].alive:
            counts[target] = counts.get(target, 0) + 1
    if counts:
        top = max(counts.values())
        candidates = [uid for uid, c in counts.items() if c == top]
        tie_note = " (жребий среди равных)" if len(candidates) > 1 else ""
    else:
        candidates = [p.user_id for p in _alive(game)]
        tie_note = " (никто не проголосовал — решил жребий)"
    loser_id = random.choice(candidates)
    loser = game.players[loser_id]
    loser.alive = False

    await _say(
        bot, game.chat_id,
        f"{HR}\n🧮 <b>Итог раунда {game.round_no}</b>\nИз бункера не пускают "
        f"<b>{_e(loser.name)}</b>{tie_note}.",
    )

    if ai.available():
        try:
            note = await ai.narrate_elimination(game.catastrophe, loser.card or {}, game.round_no)
            if note:
                await _say(bot, game.chat_id, f"🎙 <i>{_e(note)}</i>")
        except Exception as e:
            logger.warning("AI narrate failed: %s", e)

    if not _is_active(game):
        return
    if len(_alive(game)) <= game.spots:
        await _finish(bot, game)
    else:
        await _reveal_round(bot, game)


async def _finish(bot: Bot, game: Game) -> None:
    game.phase = "ended"
    survivors = _alive(game)
    names = ", ".join(_e(p.name) for p in survivors)
    await _say(bot, game.chat_id, f"{HR}\n🚪🔒 Люк задраен.\nВ бункере остаются: <b>{names}</b>.")

    if ai.available():
        try:
            cards = [p.card or {"name": p.name} for p in survivors]
            epilogue = await ai.narrate_epilogue(game.catastrophe, game.bunker, cards)
            if epilogue:
                await _say(bot, game.chat_id, f"📖 <b>Эпилог</b>\n{HR}\n<i>{_e(epilogue)}</i>")
        except Exception as e:
            logger.warning("AI epilogue failed: %s", e)

    await _send(bot, game.chat_id, "Игра окончена! Спасибо за игру 🖤\n/bunker — сыграть ещё.")
    if games.get(game.chat_id) is game:
        games.pop(game.chat_id, None)
