"""Шахматы на двоих прямо в чате — как шашки у @goplaybot (просьба владельца).

Как играть: в любом чате (канал, группа, личка) набрать «@ник_бота» и выбрать «♟ Шахматы» (инлайн-режим —
его один раз включают в @BotFather: /setinline), или /chess в группе. В сообщении кнопка «Играть»: кто
нажмёт — соперник (не создатель), цвета случайные. Доска — 64 кнопки, белые снизу: нажал свою фигуру —
бот отметил, куда можно (зелёные • — ход, красные — взятие), нажал клетку — ход. Правила — python-chess: шах, мат, пат,
рокировка, взятие на проходе, превращение (в ферзя), ничья по повторению / 50 ходам / нехватке фигур.
Доску перерисовывает `_painter` (лимит правок Telegram — см. EDIT_GAP).
Сдаться — двойным нажатием 🏳, ничья — 🤝 одного и согласие второго. После партии — «🔁 Реванш».

Партия хранится в базе (настройка chess:<id>: список ходов, игроки, выбранная фигура) — перезапуски бота при
обновлениях её не обрывают. id — inline_message_id (инлайн) или «чат:сообщение» (/chess). Без ИИ: бесплатно.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import random
import time
from collections import deque

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InlineQuery,
                           InlineQueryResultArticle, InlineQueryResultCachedGif, InlineQueryResultCachedMpeg4Gif,
                           InputTextMessageContent, Message,
                           ReplyParameters)

from config import Config
from database import Database

from . import icons as icon_set

logger = logging.getLogger("chess")
router = Router(name="chess")

KEY = "chess:"            # настройки с состоянием партий
RESIGN_CONFIRM = 10       # сек: второе нажатие 🏳 в это время — сдался
VS15 = "︎"           # текстовый вид символа: иначе ♟ на айфоне рисуется эмодзи
# Белым — залитые значки, чёрным — контурные: текст кнопок в Telegram белый, и залитый ♚ на кнопке выглядит
# белой фигурой, а контурный ♔ — тёмной (по шрифтовым названиям было наоборот — владелец: «цвета перепутал»).
PIECES = {"K": "♚", "Q": "♛", "R": "♜", "B": "♝", "N": "♞", "P": "♟",
          "k": "♔", "q": "♕", "r": "♖", "b": "♗", "n": "♘", "p": "♙"}
EMPTY = "⠀"   # пустая клетка (точки на тёмных клетках рябили и мешали видеть фигуры — убраны по просьбе владельца)
# Подпись кнопки со значком фигуры: Telegram ставит значок и подпись рядом и центрует вместе — с «⠀» значок уезжал
# влево («не ровно по центру» — владелец). Сначала пробуем подпись нулевой ширины; не примет — «⠀».
ICON_TEXTS = ("\u200b", "⠀")
_icon_text = [ICON_TEXTS[0]]   # какая подпись сработала (пробуем с неё)
MOVE_DOT = "•"
# Ширина кнопок = ширина сообщения, а она — по самой длинной строке текста. Строка из «пустых» знаков
# растягивает сообщение на весь экран: клетки выходят почти квадратными (было узко — просьба владельца).
WIDE = "⠀" * 30  # 40 на айфоне — в притык к краю; в группе (сообщение уже, с аватаркой) переносилось бы
# Цвет кнопок (Bot API style): выбранная фигура — синяя, куда можно — зелёные, взятие и король под шахом —
# красные. Старые приложения цвет не показывают — для них остаются «•» на ходах.
SEL, GO, HIT = "primary", "success", "danger"
START_COUNT = {"P": 8, "N": 2, "B": 2, "R": 2, "Q": 1}

# Telegram ограничивает частоту правок одного сообщения: при каждом нажатии (выбор фигуры + ход) правка шла
# сразу, лимит кончался, правки отбивались «Too Many Requests» — доска стояла до минуты («ходы через минуту»).
# Теперь: доску перерисовывает одна задача на партию — не чаще EDIT_GAP, всегда ПОСЛЕДНЕЕ состояние (лишние
# промежуточные правки пропадают), при лимите ждёт, сколько сказал Telegram, и рисует снова. Выбор фигуры
# подсвечивается правкой, только пока правок за минуту меньше SELECT_BUDGET, иначе — подсказкой у кнопки.
EDIT_GAP = 1.0
SELECT_BUDGET = 12
ICONS_ON = "chess_icons_on"   # «1» — у владельца Premium (видно по его /chess): фигуры значками

_locks: dict[str, asyncio.Lock] = {}
_dirty: dict[str, int] = {}                 # номер последнего изменения партии
_painters: dict[str, asyncio.Task] = {}
_edits: dict[str, deque] = {}               # время правок за последнюю минуту
_flood_until: dict[str, float] = {}
_no_icons: set[str] = set()                 # партии, где Telegram значки не принял — играют символами
# /chess_post: пост в канал, партия — в комментариях (это уже группа: там значки можно, в самом канале — нет)
COMMENT_WAIT = 20   # сек ждём, пока пост канала появится в группе комментариев
# Повтор партии гифкой (replay.py, просьба владельца — только гифка, без картинки): в /chess и /chess_post бот сам
# присылает её под доской; в «@бот» бота в том чате нет — кнопка «🎞 Повтор» подставляет «@бот replay <id>», и гифка
# (загруженная тихо в личку владельцу ради file_id и сразу удалённая) отправляется одним нажатием.
DONE = "chess_done:"   # настройка: доигранная партия (ходы, игроки, итог, file_id гифки) — реванш её не стирает
REPLAY = "replay "
_bg: set[asyncio.Task] = set()
_gif_tasks: dict[str, asyncio.Task] = {}   # инлайн-партия → гифка, которая готовится с конца партии
_forwards: dict[tuple[int, int], int] = {}            # (канал, пост) → id копии поста в группе
_forward_waits: dict[tuple[int, int], asyncio.Future] = {}


def _chess():
    import chess  # python-chess (requirements.txt); без пакета шахматы просто не откроются

    return chess


def _sym(piece) -> str:
    return PIECES[piece.symbol()] + VS15


def _name(user) -> str:
    return (user.first_name or user.username or "Игрок") if user else "Игрок"


def _gid(call: CallbackQuery) -> str:
    if call.inline_message_id:
        return call.inline_message_id
    return f"{call.message.chat.id}:{call.message.message_id}" if call.message else ""


def _lock(gid: str) -> asyncio.Lock:
    if len(_locks) > 500:
        _locks.clear()
    return _locks.setdefault(gid, asyncio.Lock())


async def _load(db: Database, gid: str) -> dict | None:
    raw = await db.get_setting(KEY + gid, "")
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


async def _save(db: Database, gid: str, st: dict | None) -> None:
    st = st or {}
    st["ts"] = time.time()
    await db.set_setting(KEY + gid, json.dumps(st, ensure_ascii=False))


def _board(st: dict):
    chess = _chess()
    b = chess.Board()
    for uci in st.get("moves", []):
        b.push_uci(uci)
    return b


# ---------- вид ----------


def lobby_text(name: str) -> str:
    return (f"♟ <b>Шахматы</b>\n\n{html.escape(name)} ищет соперника.\nКто нажмёт «Играть» — играет с ним, "
            "цвета — жребием.")


def lobby_kb(uid: int, name: str) -> InlineKeyboardMarkup:
    data = f"cj:{uid}:{name}"
    while len(data.encode()) > 64:  # лимит Telegram на данные кнопки
        name = name[:-1]
        data = f"cj:{uid}:{name}"
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="♟ Играть", callback_data=data)]])


def _captured(board) -> tuple[str, str]:
    """Что съели белые и что съели чёрные (по недостающим фигурам соперника)."""
    chess = _chess()
    out = []
    for color in (chess.BLACK, chess.WHITE):  # белые съели чёрные фигуры и наоборот
        lost = []
        for letter, n in START_COUNT.items():
            kind = chess.Piece.from_symbol(letter).piece_type
            have = len(board.pieces(kind, color))
            sym = letter if color == chess.WHITE else letter.lower()
            lost += [PIECES[sym] + VS15] * max(0, n - have)
        out.append("".join(lost))
    return out[0], out[1]


def render(st: dict, icons: dict[str, str] | None = None,
           icon_text: str = ICON_TEXTS[-1]) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и доска; icons — id значков фигур (icons.py), без них — символы ♜."""
    chess = _chess()
    b = _board(st)
    wid, wname = st["w"]
    bname = st["b"][1]
    lines = ["♟ <b>Шахматы</b>", WIDE, f"⚪ {html.escape(wname)}  ·  ⚫ {html.escape(bname)}"]
    if st.get("over"):
        lines += ["", f"<b>{html.escape(st['over'])}</b>"]
    else:
        side = "⚪" if b.turn == chess.WHITE else "⚫"
        who = wname if b.turn == chess.WHITE else bname
        lines += ["", f"Ходит: {side} <b>{html.escape(who)}</b>" + ("  —  <b>ШАХ!</b>" if b.is_check() else "")]
    last = st.get("last")
    if last:
        lines.append(f"Последний ход: {last}")
    if st.get("draw") and not st.get("over"):
        offer = wname if st["draw"] == wid else bname
        lines.append(f"🤝 {html.escape(offer)} предлагает ничью")
    by_white, by_black = _captured(b)
    if by_white or by_black:
        lines.append(f"Съели: ⚪ {by_white or '-'}  ·  ⚫ {by_black or '-'}")

    sel = st.get("sel")
    targets, captures = set(), set()
    if sel is not None and not st.get("over"):
        for m in b.legal_moves:
            if m.from_square == sel:
                (captures if b.is_capture(m) else targets).add(m.to_square)
    last_sq = set(st.get("last_sq") or [])
    checked = b.king(b.turn) if b.is_check() else None
    rows = []
    for rank in range(7, -1, -1):
        row = []
        for file in range(8):
            sq = chess.square(file, rank)
            piece = b.piece_at(sq)
            style = icon = None
            if piece and icons and sq not in targets:
                icon = icons.get(piece.symbol())
            if sq == sel:
                text, style = _sym(piece), SEL
            elif sq in captures:
                text, style = (_sym(piece) if piece else MOVE_DOT), HIT  # на проходе — клетка пустая
            elif sq in targets:
                text, style = MOVE_DOT, GO
            elif piece:
                text = _sym(piece)
                style = HIT if sq == checked else None
            else:
                text = "◦" if sq in last_sq else EMPTY
            extra = {k: v for k, v in (("style", style), ("icon_custom_emoji_id", icon)) if v}  # пустые не слать
            row.append(InlineKeyboardButton(text=icon_text if icon else text,
                                            callback_data="c:x" if st.get("over") else f"c:{sq}", **extra))
        rows.append(row)
    if st.get("over"):
        row = [InlineKeyboardButton(text="🔁 Реванш", callback_data="cre")]
        if st.get("inline") and st.get("gid") and st.get("moves"):
            row.append(InlineKeyboardButton(text="🎞 Повтор", switch_inline_query_current_chat=REPLAY + st["gid"]))
        rows.append(row)
    else:
        rows.append([InlineKeyboardButton(text="🏳 Сдаться", callback_data="cr"),
                     InlineKeyboardButton(text="🤝 Ничья", callback_data="cd")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _plain(kb: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Та же доска без цвета кнопок — на случай, если Telegram цвет не принял."""
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=b.text, callback_data=b.callback_data, switch_inline_query_current_chat=b.switch_inline_query_current_chat)
        for b in row] for row in kb.inline_keyboard])


def _view(st: dict, icons: dict[str, str] | None = None,
          icon_text: str = ICON_TEXTS[-1]) -> tuple[str, InlineKeyboardMarkup]:
    if st.get("lobby"):  # реванш: снова «Играть»
        uid, name = st["lobby"]
        return lobby_text(name), lobby_kb(uid, name)
    return render(st, icons, icon_text)


def _recent_edits(gid: str) -> int:
    q = _edits.setdefault(gid, deque())
    while q and time.time() - q[0] > 60:
        q.popleft()
    return len(q)


def _flood_left(gid: str) -> int:
    return max(0, round(_flood_until.get(gid, 0) - time.time()))


async def _edit_once(bot: Bot, target: tuple, text: str, kb: InlineKeyboardMarkup):
    inline_id, chat_id, msg_id = target
    if inline_id:
        return await bot.edit_message_text(text=text, inline_message_id=inline_id, reply_markup=kb, parse_mode="HTML")
    return await bot.edit_message_text(text=text, chat_id=chat_id, message_id=msg_id, reply_markup=kb,
                                       parse_mode="HTML")


def _has_icons(kb) -> bool:
    return any(getattr(b, "icon_custom_emoji_id", None) for row in (kb.inline_keyboard if kb else []) for b in row)


async def _edit(bot: Bot, gid: str, target: tuple, st: dict, icons: dict[str, str] | None) -> None:
    """Правка доски: со значками фигур → без них (символами) → без цвета кнопок. TelegramRetryAfter — наверх."""
    tries = []
    if icons and gid not in _no_icons:
        texts = [_icon_text[0]] + [t for t in ICON_TEXTS if t != _icon_text[0]]
        tries += [("icons", t, _view(st, icons, t)) for t in texts]
    text, kb = _view(st)
    tries += [("text", "", (text, kb)), ("plain", "", (text, _plain(kb)))]
    for n, (kind, label, (text, kb)) in enumerate(tries):
        if kind == "icons" and gid in _no_icons:  # Telegram молча выкинул значки — другие подписи не помогут
            continue
        try:
            res = await _edit_once(bot, target, text, kb)
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
            logger.warning("chess edit failed (%s %r): %s", kind, label, e)
            if kind == "icons" and tries[n + 1][0] != "icons":  # ни одна подпись не прошла
                _no_icons.add(gid)
            continue
        # Telegram может молча выкинуть значки (нет Premium у владельца) — тогда на кнопках пусто: перерисовать
        if kind == "icons" and not isinstance(res, bool) and not _has_icons(getattr(res, "reply_markup", None)):
            logger.warning("chess %s: Telegram убрал значки фигур из кнопок — играем символами", gid)
            _no_icons.add(gid)
            continue
        if kind == "icons":
            _icon_text[0] = label
        return


async def _painter(bot: Bot, db: Database, gid: str, target: tuple) -> None:
    try:
        while True:
            wait = max(_flood_until.get(gid, 0) - time.time(),
                       (_edits[gid][-1] + EDIT_GAP - time.time()) if _edits.get(gid) else 0)
            if wait > 0:
                await asyncio.sleep(wait)
            ver = _dirty.get(gid, 0)
            st = await _load(db, gid)
            if not st:
                return
            icons = await icon_set.load(db) if st.get("icons") else None
            try:
                await _edit(bot, gid, target, st, icons)
            except TelegramRetryAfter as e:
                _flood_until[gid] = time.time() + e.retry_after + 0.5
                logger.info("chess %s: лимит правок Telegram, жду %s с", gid, e.retry_after)
                continue
            _recent_edits(gid)
            _edits[gid].append(time.time())
            if _dirty.get(gid, 0) == ver:  # пока рисовали, ничего не поменялось
                return
    except Exception:  # noqa: BLE001 — доска не обновилась, партия в базе цела; следующее нажатие перерисует
        logger.exception("chess painter %s", gid)
    finally:
        _painters.pop(gid, None)


async def close_all(bot: Bot, db: Database, text: str = "Партия остановлена") -> int:
    """/stop_all: все идущие партии — итог «остановлена» на доске (кнопки ходов убираются, как после мата). Сколько."""
    n = 0
    for key, _raw in await db.settings_with_prefix(KEY):
        gid = key[len(KEY):]
        async with _lock(gid):
            st = await _load(db, gid)
            if not st or st.get("over") or not st.get("w"):
                continue
            st["over"] = text
            await _save(db, gid, st)
        chat, _, msg = gid.partition(":")
        target = (None, int(chat), int(msg)) if msg else (gid, None, None)
        _dirty[gid] = _dirty.get(gid, 0) + 1
        if gid not in _painters:
            _painters[gid] = asyncio.create_task(_painter(bot, db, gid, target))
        n += 1
    return n


def _refresh(bot: Bot, db: Database, call: CallbackQuery) -> None:
    """Партия изменилась — перерисовать доску (не сразу, если правок было много: см. EDIT_GAP)."""
    gid = _gid(call)
    if not gid:
        return
    _dirty[gid] = _dirty.get(gid, 0) + 1
    if gid in _painters:
        return  # задача уже рисует — возьмёт последнее состояние
    if len(_dirty) > 500:
        for d in (_dirty, _edits, _flood_until):
            d.clear()
        _dirty[gid] = 1
    target = (call.inline_message_id, call.message.chat.id if call.message else None,
              call.message.message_id if call.message else None)
    _painters[gid] = asyncio.create_task(_painter(bot, db, gid, target))


def _slow_note(gid: str) -> str:
    left = _flood_left(gid)
    return f" Telegram притормозил доску - обновится через {left} сек." if left else ""


# ---------- создать партию ----------


@router.inline_query()
async def inline_games(query: InlineQuery, bot: Bot, db: Database, config: Config) -> None:
    """«@бот» в любом чате — карточка «♟ Шахматы» (инлайн-режим включают в @BotFather: /setinline);
    «@бот replay <id>» (кнопка «🎞 Повтор» после партии) — гифка с повтором партии."""
    if (query.query or "").startswith(REPLAY):
        await _inline_replay(query, bot, db, config)
        return
    name = _name(query.from_user)
    res = InlineQueryResultArticle(
        id="chess", title="♟ Шахматы", description="Партия на двоих: кто нажмёт «Играть» — твой соперник",
        input_message_content=InputTextMessageContent(message_text=lobby_text(name), parse_mode="HTML"),
        reply_markup=lobby_kb(query.from_user.id, name))
    await query.answer([res], cache_time=1, is_personal=True)


async def _owner_icons(message: Message, bot: Bot, db: Database, config: Config) -> None:
    """Команду написал владелец бота — запомнить, есть ли у него Premium (видно только по его сообщению), и
    при Premium приготовить набор значков фигур."""
    owner = config.admin_ids[0] if config.admin_ids else 0
    if message.from_user and message.from_user.id == owner:
        premium = bool(message.from_user.is_premium)
        await db.set_setting(ICONS_ON, "1" if premium else "0")
        if premium:
            await icon_set.ensure(bot, db, owner)


@router.message(Command("chess"))
async def cmd_chess(message: Message, bot: Bot, db: Database, config: Config) -> None:
    """/chess — то же в этом чате (в остальных — через «@бот»). Здесь фигуры — значками, если у владельца Premium."""
    name = _name(message.from_user)
    await bot.send_message(message.chat.id, lobby_text(name), reply_markup=lobby_kb(message.from_user.id, name))
    await _owner_icons(message, bot, db, config)


def note_forward(message: Message) -> None:
    """Middleware в bot.py: пост канала пришёл в группу комментариев — запомнить, где он (для /chess_post)."""
    fo = message.forward_origin
    chat = getattr(fo, "chat", None)
    post = getattr(fo, "message_id", None)
    if not (message.is_automatic_forward and chat and post):
        return
    key = (chat.id, post)
    if len(_forwards) > 200:
        _forwards.clear()
    _forwards[key] = message.message_id
    fut = _forward_waits.pop(key, None)
    if fut and not fut.done():
        fut.set_result(message.message_id)


async def _comment_root(channel: int, post: int) -> int | None:
    key = (channel, post)
    if key in _forwards:  # пришёл раньше, чем мы начали ждать
        return _forwards[key]
    fut = _forward_waits[key] = asyncio.get_running_loop().create_future()
    try:
        return await asyncio.wait_for(fut, COMMENT_WAIT)
    except asyncio.TimeoutError:
        return _forwards.get(key)
    finally:
        _forward_waits.pop(key, None)


@router.message(Command("chess_post"))
async def cmd_chess_post(message: Message, bot: Bot, db: Database, config: Config) -> None:
    """/chess_post — пост «играем в шахматы» в канал бати, сама партия — в комментариях под ним.
    В канале значки в кнопках Telegram не разрешает (только в личке и группах), а комментарии — это группа."""
    from ephemeral import temp_reply
    from handlers.channel_posts import channel_id, mark_own

    channel = await channel_id(bot, db)
    try:
        info = await bot.get_chat(channel) if channel else None
    except TelegramBadRequest:
        info = None
    group = getattr(info, "linked_chat_id", None)
    if not group:
        await temp_reply(message, "Не нашёл канал с комментариями: бот должен быть админом канала, "
                                  "а у канала - группа для комментариев.")
        return
    name = _name(message.from_user)
    await _owner_icons(message, bot, db, config)
    try:
        post = await bot.send_message(channel, f"♟ <b>Шахматы</b>\n\n{html.escape(name)} ищет соперника. "
                                               "Партия - в комментариях: жми там «Играть» 👇", parse_mode="HTML")
    except TelegramBadRequest as e:
        await temp_reply(message, f"Не смог написать в канал: {html.escape(str(e))}")
        return
    await mark_own(db, post.message_id)  # стёб-бот не комментирует свой же пост
    root = await _comment_root(channel, post.message_id)
    if not root:
        await temp_reply(message, "Пост в канале есть, но в комментариях он не появился - партию не открыть. "
                                  "Проверь, что бот есть в группе комментариев.")
        return
    # без кнопок под самим постом: у поста с кнопками Telegram прячет строку комментариев (владелец: «кнопка без
    # комментариев» — была «♟ К партии»)
    await bot.send_message(group, lobby_text(name), reply_markup=lobby_kb(message.from_user.id, name),
                           reply_parameters=ReplyParameters(message_id=root, allow_sending_without_reply=True))
    logger.info("шахматы: пост %s в канале %s, партия в комментариях", post.message_id, channel)


@router.callback_query(F.data.startswith("cj:"))
async def cb_join(call: CallbackQuery, bot: Bot, db: Database) -> None:
    gid = _gid(call)
    try:
        _, cid, cname = (call.data or "").split(":", 2)
        cid = int(cid)
    except ValueError:
        await call.answer()
        return
    async with _lock(gid):
        st = await _load(db, gid)
        if st and not st.get("over"):
            await call.answer("Партия уже идёт.", show_alert=True)
            return
        if call.from_user.id == cid:
            await call.answer("Ждём соперника: «Играть» должен нажать кто-то другой.", show_alert=True)
            return
        players = [[cid, cname], [call.from_user.id, _name(call.from_user)]]
        random.shuffle(players)
        st = {"w": players[0], "b": players[1], "moves": [], "sel": None, "start": time.time(),
              "inline": bool(call.inline_message_id), "gid": gid,
              # значки — только в сообщениях бота (/chess), не в «@бот»: так велит Telegram (см. icons.py)
              "icons": bool(call.message and not call.inline_message_id
                            and await db.get_setting(ICONS_ON, "0") == "1" and await icon_set.load(db))}
        await _save(db, gid, st)
    await call.answer("Поехали! " + ("Ты белыми, твой ход." if st["w"][0] == call.from_user.id else "Ты чёрными."))
    _refresh(bot, db, call)
    logger.info("шахматы %s: %s (белые) против %s", gid, st["w"][1], st["b"][1])


# ---------- повтор партии гифкой ----------


async def _gif(st: dict) -> BufferedInputFile:
    """Повтор партии: видео MP4 (Telegram показывает как гифку). Гифку Telegram сам перегонял в видео и при этом
    терял ходы (владелец: «пропускает некоторые ходы»), поэтому собираем видео сами; не вышло — гифка."""
    from . import replay

    minutes = round((time.time() - st["start"]) / 60) if st.get("start") else None
    args = (st["moves"], st["w"][1], st["b"][1], st["over"], minutes)
    try:
        return BufferedInputFile(await asyncio.to_thread(replay.replay_video, *args), "chess.mp4")
    except Exception as e:  # noqa: BLE001 — нет кодека и т.п.: гифка лучше, чем ничего
        logger.warning("chess: видео повтора не собралось (%s) — шлю гифку", e)
        return BufferedInputFile(await asyncio.to_thread(replay.replay_gif, *args), "chess.gif")


async def _finished(bot: Bot, db: Database, call: CallbackQuery, gid: str, st: dict) -> None:
    """Партия кончилась: запомнить её (для «🎞 Повтор») и в сообщениях бота — прислать гифку под доской. В «@бот»
    гифку готовим сразу в фоне (со Stockfish разбор идёт ~10 сек — инлайн-запрос столько ждать не может)."""
    if not st.get("moves"):
        return
    done = {k: st.get(k) for k in ("w", "b", "moves", "over", "start")}
    await db.set_setting(DONE + gid, json.dumps(done, ensure_ascii=False))
    if call.inline_message_id or not call.message:  # в чужом чате бот писать не может — там кнопка «🎞 Повтор»
        if len(_gif_tasks) > 200:
            for k in [k for k, t in _gif_tasks.items() if t.done()]:
                _gif_tasks.pop(k, None)
        task = asyncio.create_task(_cache_gif(bot, db, gid, done))
        _gif_tasks[gid] = task
        _bg.add(task)
        task.add_done_callback(_bg.discard)
        return
    chat_id, msg_id = call.message.chat.id, call.message.message_id

    async def send() -> None:
        try:
            await bot.send_animation(chat_id, await _gif(done),
                                     caption=f"🎞 Повтор партии: {html.escape(st['w'][1])} - {html.escape(st['b'][1])}",
                                     reply_parameters=ReplyParameters(message_id=msg_id,
                                                                      allow_sending_without_reply=True))
        except Exception:  # noqa: BLE001 — повтор не главное, партия уже записана
            logger.exception("chess: гифка повтора не ушла")

    task = asyncio.create_task(send())
    _bg.add(task)
    task.add_done_callback(_bg.discard)


def _owner_id() -> int:
    raw = os.getenv("BOT_FATHER_ID", "") or os.getenv("ADMIN_IDS", "").replace(";", ",").split(",")[0]
    try:
        return int(raw.strip())
    except ValueError:
        return 0


async def _cache_gif(bot: Bot, db: Database, gid: str, done: dict) -> str | None:
    """Гифка в Telegram ради file_id: тихо в личку владельцу и сразу удалить; file_id — в запись партии."""
    owner = _owner_id()
    if not owner:
        return None
    try:
        media = await _gif(done)
        msg = await bot.send_animation(owner, media, disable_notification=True)
        fid = msg.animation.file_id if msg.animation else msg.document.file_id
        try:
            await bot.delete_message(owner, msg.message_id)
        except TelegramBadRequest:
            pass
    except Exception:  # noqa: BLE001
        logger.exception("chess: гифка для инлайн-повтора не загрузилась")
        return None
    raw = await db.get_setting(DONE + gid, "")
    try:
        cur = json.loads(raw) if raw else {}
    except ValueError:
        cur = {}
    if cur.get("moves") == done["moves"]:  # пока рисовали, там не доиграли новую партию (реванш)
        cur["gif"] = fid
        cur["gif_kind"] = "mp4" if media.filename.endswith(".mp4") else "gif"
        await db.set_setting(DONE + gid, json.dumps(cur, ensure_ascii=False))
    return fid


async def _inline_replay(query: InlineQuery, bot: Bot, db: Database, config: Config) -> None:
    gid = query.query[len(REPLAY):].strip()
    raw = await db.get_setting(DONE + gid, "") if gid else ""
    try:
        done = json.loads(raw) if raw else None
    except ValueError:
        done = None
    if not done:
        await query.answer([], cache_time=1, is_personal=True)
        return
    fid = done.get("gif")
    if not fid:
        task = _gif_tasks.get(gid)
        try:
            if task and not task.done():  # ещё рисуется с конца партии — подождём немного
                fid = await asyncio.wait_for(asyncio.shield(task), 8)
            elif task:
                fid = task.result()
        except Exception:  # noqa: BLE001
            fid = None
    if not fid:
        fid = await _cache_gif(bot, db, gid, done)
    if not fid:
        await query.answer([], cache_time=1, is_personal=True)
        return
    caption = f"🎞 Повтор партии: {html.escape(done['w'][1])} - {html.escape(done['b'][1])}"
    raw = await db.get_setting(DONE + gid, "")
    try:
        kind = (json.loads(raw) if raw else {}).get("gif_kind", "gif")
    except ValueError:
        kind = "gif"
    if kind == "mp4":  # повтор видео — свой тип результата (у старых партий осталась гифка)
        res = InlineQueryResultCachedMpeg4Gif(id="replay", mpeg4_file_id=fid, title="🎞 Повтор партии", caption=caption)
    else:
        res = InlineQueryResultCachedGif(id="replay", gif_file_id=fid, title="🎞 Повтор партии", caption=caption)
    await query.answer([res], cache_time=1, is_personal=True)


# ---------- ходы ----------


def _result(b) -> str:
    if b.is_checkmate():
        return "Мат! Победили " + ("чёрные" if b.turn else "белые")
    if b.is_stalemate():
        return "Пат - ничья"
    if b.is_insufficient_material():
        return "Ничья: не хватает фигур для мата"
    if b.can_claim_threefold_repetition():
        return "Ничья: позиция повторилась трижды"
    if b.can_claim_fifty_moves():
        return "Ничья: 50 ходов без взятий и ходов пешкой"
    return ""


async def _player_turn(call: CallbackQuery, st: dict | None, b) -> bool:
    """Нажал ли игрок, чей сейчас ход (иначе — подсказка в алерте)."""
    if not st:
        await call.answer("Партия не найдена - начните новую.", show_alert=True)
        return False
    uid = call.from_user.id
    if uid not in (st["w"][0], st["b"][0]):
        await call.answer("Это не твоя партия - смотри молча.", show_alert=True)
        return False
    if st.get("over"):
        await call.answer("Партия окончена.")
        return False
    turn = st["w"] if b.turn else st["b"]
    if uid != turn[0]:
        await call.answer("Сейчас ходит соперник.")
        return False
    return True


@router.callback_query(F.data.startswith("c:"))
async def cb_square(call: CallbackQuery, bot: Bot, db: Database) -> None:
    gid = _gid(call)
    raw = (call.data or "")[2:]
    if not raw.isdigit():
        await call.answer("Партия окончена.")
        return
    sq = int(raw)
    chess = _chess()
    async with _lock(gid):
        st = await _load(db, gid)
        b = _board(st) if st else None
        if not await _player_turn(call, st, b):
            return
        piece = b.piece_at(sq)
        movable = piece is not None and piece.color == b.turn and any(m.from_square == sq for m in b.legal_moves)
        sel = st.get("sel")
        note = ""
        if sel is None or sq == sel or movable:
            if sq == sel:
                st["sel"] = None
            elif movable:
                st["sel"] = sq
            else:
                await call.answer("Нажми свою фигуру, которой можно сходить.")
                return
            # подсветку ходов рисуем, только пока лимит правок не близко — иначе подсказка у кнопки
            if _flood_left(gid) or _recent_edits(gid) >= SELECT_BUDGET:
                await _save(db, gid, st)
                await call.answer(f"Выбран {_sym(piece)} {chess.square_name(sq)} - жми, куда ходить."
                                  if st["sel"] is not None else "Отменил выбор.")
                return
        else:
            moves = [m for m in b.legal_moves if m.from_square == sel and m.to_square == sq]
            if not moves:
                await call.answer("Так ходить нельзя.")
                return
            move = next((m for m in moves if m.promotion in (None, chess.QUEEN)), moves[0])  # пешка — в ферзя
            mover = b.piece_at(sel)
            capture = b.is_capture(move)
            san = b.san(move)
            b.push(move)
            st["moves"].append(move.uci())
            st["sel"] = None
            st["draw"] = None
            st["last_sq"] = [move.from_square, move.to_square]
            st["last"] = (("⚪ " if mover.color else "⚫ ") + _sym(mover) + " "
                          + chess.square_name(move.from_square) + ("×" if capture else "-")
                          + chess.square_name(move.to_square) + ("+" if "+" in san else "#" if "#" in san else ""))
            over = _result(b)
            if over:
                winner = st["w"] if "белые" in over else st["b"] if "чёрные" in over else None
                st["over"] = over + (f" ({winner[1]})" if winner else "")
                note = st["over"]
                logger.info("шахматы %s: %s", gid, st["over"])
        await _save(db, gid, st)
    await call.answer(note or _slow_note(gid).strip())
    _refresh(bot, db, call)
    if st.get("over"):
        await _finished(bot, db, call, gid, st)


@router.callback_query(F.data == "cr")
async def cb_resign(call: CallbackQuery, bot: Bot, db: Database) -> None:
    gid = _gid(call)
    async with _lock(gid):
        st = await _load(db, gid)
        uid = call.from_user.id
        if not st or st.get("over") or uid not in (st["w"][0], st["b"][0]):
            await call.answer("Сдаться может только игрок этой партии.")
            return
        ask = st.get("resign") or [0, 0]
        if ask[0] != uid or time.time() - ask[1] > RESIGN_CONFIRM:
            st["resign"] = [uid, time.time()]
            await _save(db, gid, st)
            await call.answer("Точно сдаёшься? Нажми 🏳 ещё раз.", show_alert=True)
            return
        me, other = (st["w"], st["b"]) if uid == st["w"][0] else (st["b"], st["w"])
        st["over"] = f"{me[1]} сдался. Победил {other[1]}"
        await _save(db, gid, st)
    await call.answer("Сдался.")
    _refresh(bot, db, call)
    await _finished(bot, db, call, gid, st)


@router.callback_query(F.data == "cd")
async def cb_draw(call: CallbackQuery, bot: Bot, db: Database) -> None:
    gid = _gid(call)
    async with _lock(gid):
        st = await _load(db, gid)
        uid = call.from_user.id
        if not st or st.get("over") or uid not in (st["w"][0], st["b"][0]):
            await call.answer("Ничью предлагают только игроки этой партии.")
            return
        if st.get("draw") == uid:
            await call.answer("Ждём ответа соперника.")
            return
        if st.get("draw"):  # соперник предлагал — согласился
            st["over"] = "Ничья по согласию"
            note = "Ничья."
        else:
            st["draw"] = uid
            note = "Предложил ничью - если соперник нажмёт 🤝, партия кончится вничью."
        await _save(db, gid, st)
    await call.answer(note)
    _refresh(bot, db, call)
    if st.get("over"):
        await _finished(bot, db, call, gid, st)


@router.callback_query(F.data == "cre")
async def cb_rematch(call: CallbackQuery, bot: Bot, db: Database) -> None:
    """После партии: игрок жмёт «Реванш» — снова лобби, второй жмёт «Играть»."""
    gid = _gid(call)
    async with _lock(gid):
        st = await _load(db, gid)
        if not st or not st.get("over"):
            await call.answer()
            return
        if call.from_user.id not in (st["w"][0], st["b"][0]):
            await call.answer("Реванш - для игроков этой партии. Начни свою: @бот → «Шахматы».", show_alert=True)
            return
        # не идущая партия — «Играть» сработает
        await _save(db, gid, {"over": "лобби", "lobby": [call.from_user.id, _name(call.from_user)]})
    await call.answer("Ждём соперника.")
    _refresh(bot, db, call)
