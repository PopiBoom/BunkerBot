"""Шахматы на двоих прямо в чате — как шашки у @goplaybot (просьба владельца).

Как играть: в любом чате (канал, группа, личка) набрать «@ник_бота» и выбрать «♟ Шахматы» (инлайн-режим —
его один раз включают в @BotFather: /setinline), или /chess в группе. В сообщении кнопка «Играть»: кто
нажмёт — соперник (не создатель), цвета случайные. Доска — 64 кнопки, белые снизу: нажал свою фигуру —
бот отметил, куда можно (зелёные • — ход, красные — взятие), нажал клетку — ход. Правила — python-chess: шах, мат, пат,
рокировка, взятие на проходе, превращение (в ферзя), ничья по повторению / 50 ходам / нехватке фигур.
Сдаться — двойным нажатием 🏳, ничья — 🤝 одного и согласие второго. После партии — «🔁 Реванш».

Партия хранится в базе (настройка chess:<id>: список ходов, игроки, выбранная фигура) — перезапуски бота при
обновлениях её не обрывают. id — inline_message_id (инлайн) или «чат:сообщение» (/chess). Без ИИ: бесплатно.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InlineQuery,
                           InlineQueryResultArticle, InputTextMessageContent, Message)

from database import Database

logger = logging.getLogger("chess")
router = Router(name="chess")

KEY = "chess:"            # настройки с состоянием партий
RESIGN_CONFIRM = 10       # сек: второе нажатие 🏳 в это время — сдался
VS15 = "︎"           # текстовый вид символа: иначе ♟ на айфоне рисуется эмодзи
PIECES = {"K": "♔", "Q": "♕", "R": "♖", "B": "♗", "N": "♘", "P": "♙",
          "k": "♚", "q": "♛", "r": "♜", "b": "♝", "n": "♞", "p": "♟"}
LIGHT, DARK = "⠀", "·"   # пустые клетки: светлая — пусто, тёмная — точка (как у шашек)
MOVE_DOT = "•"
# Ширина кнопок = ширина сообщения, а она — по самой длинной строке текста. Строка из «пустых» знаков
# растягивает сообщение на весь экран: клетки выходят почти квадратными (было узко — просьба владельца).
WIDE = "⠀" * 40
# Цвет кнопок (Bot API style): выбранная фигура — синяя, куда можно — зелёные, взятие и король под шахом —
# красные. Старые приложения цвет не показывают — для них остаются «•» на ходах.
SEL, GO, HIT = "primary", "success", "danger"
START_COUNT = {"P": 8, "N": 2, "B": 2, "R": 2, "Q": 1}

_locks: dict[str, asyncio.Lock] = {}


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


def render(st: dict) -> tuple[str, InlineKeyboardMarkup]:
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
            style = None
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
                text = "◦" if sq in last_sq else (DARK if (file + rank) % 2 == 0 else LIGHT)
            row.append(InlineKeyboardButton(text=text, style=style,
                                            callback_data="c:x" if st.get("over") else f"c:{sq}"))
        rows.append(row)
    if st.get("over"):
        rows.append([InlineKeyboardButton(text="🔁 Реванш", callback_data="cre")])
    else:
        rows.append([InlineKeyboardButton(text="🏳 Сдаться", callback_data="cr"),
                     InlineKeyboardButton(text="🤝 Ничья", callback_data="cd")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _plain(kb: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Та же доска без цвета кнопок — на случай, если Telegram цвет не принял."""
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=b.text, callback_data=b.callback_data)
                                                  for b in row] for row in kb.inline_keyboard])


async def _show(bot: Bot, call: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    for markup in (kb, _plain(kb)):
        try:
            if call.inline_message_id:
                await bot.edit_message_text(text=text, inline_message_id=call.inline_message_id,
                                            reply_markup=markup, parse_mode="HTML")
            elif call.message:
                await bot.edit_message_text(text=text, chat_id=call.message.chat.id,
                                            message_id=call.message.message_id, reply_markup=markup,
                                            parse_mode="HTML")
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
            logger.warning("chess edit failed: %s", e)


# ---------- создать партию ----------


@router.inline_query()
async def inline_games(query: InlineQuery) -> None:
    """«@бот» в любом чате — карточка «♟ Шахматы» (инлайн-режим включают в @BotFather: /setinline)."""
    name = _name(query.from_user)
    res = InlineQueryResultArticle(
        id="chess", title="♟ Шахматы", description="Партия на двоих: кто нажмёт «Играть» — твой соперник",
        input_message_content=InputTextMessageContent(message_text=lobby_text(name), parse_mode="HTML"),
        reply_markup=lobby_kb(query.from_user.id, name))
    await query.answer([res], cache_time=1, is_personal=True)


@router.message(Command("chess"))
async def cmd_chess(message: Message, bot: Bot) -> None:
    """/chess — то же в этом чате (в остальных — через «@бот»)."""
    name = _name(message.from_user)
    await bot.send_message(message.chat.id, lobby_text(name), reply_markup=lobby_kb(message.from_user.id, name))


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
        st = {"w": players[0], "b": players[1], "moves": [], "sel": None}
        await _save(db, gid, st)
    await call.answer("Поехали! " + ("Ты белыми, твой ход." if st["w"][0] == call.from_user.id else "Ты чёрными."))
    await _show(bot, call, *render(st))
    logger.info("шахматы %s: %s (белые) против %s", gid, st["w"][1], st["b"][1])


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
    await call.answer(note)
    await _show(bot, call, *render(st))


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
    await _show(bot, call, *render(st))


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
    await _show(bot, call, *render(st))


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
        await _save(db, gid, {"over": "лобби"})  # не идущая партия — «Играть» сработает
    name = _name(call.from_user)
    await call.answer("Ждём соперника.")
    await _show(bot, call, lobby_text(name), lobby_kb(call.from_user.id, name))
