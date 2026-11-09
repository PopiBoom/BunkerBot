"""Шахматы-виджет в личке у бота (09.11, владелец: «сделай виджет шахматы, чтобы ссылка отправлялась мне отдельным
сообщением в лс и Пиздюк пересылал его в тгк, чтобы могли присоединиться к игре, и будем играть в лс у бота»;
на вопрос «кто играет» — «я против первого»).

/chess_widget (админ; в личке или в группе): вызвавшему в личку — доска-виджет (`widget.html`, HTML-виджет Telegram
13.2) и ОТДЕЛЬНЫМ сообщением ссылка `t.me/<бот>?start=wch_<id>` с кнопкой «♟ Сыграть»; это сообщение бот пересылает
в канал бати (forward_message, пост — в `channel_posts.mark_own`). Кто первым откроет ссылку (не сам вызвавший), тот
соперник, цвета жребием; ему в личку — своя доска. Каждый играет в своей личке с ботом: нажал свою фигуру — точки,
куда можно (ходы считает бот: `l` в состоянии), нажал клетку — ход (запрос виджета `m:<id>:<откуда><куда>`, пешка —
в ферзя). Правила — python-chess. 🏳 — двумя нажатиями, 🤝 — предложить / согласиться, после партии «🔁 Реванш»
(цвета меняются). Ход сделан — сопернику в личку «♞ g1-f3. Твой ход» (прошлое такое сообщение удаляется: правка
доски — молча, без уведомления). Конец — итог обоим, повтор видео (`replay.replay_video`) обоим и в канал.

Состояние доски каждого игрока — `chat_data` его сообщения (≤ 1024 байт, JSON: `_view`), его меняет только бот
(`EditMessageWidgetData`); ответ на ход — новое состояние сразу (`AnswerWidgetQuery`), чтобы доска не ждала правку.
Партия — настройка `wchess:<id>` (ходы, игроки, сообщения с досками), перезапуски её не обрывают. Ждущий вызов —
`wchess_open`: новый /chess_widget отменяет старый, если соперника так и не нашлось.

Виджеты и `widget_query` есть только на бете Bot API 10.4 — бот переходит на неё (tgbeta.py); /chess_widget на
обычном сервере включает бету, перезапускает бота и после перезапуска сам присылает вызов.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import random
import secrets
import time
from pathlib import Path

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import Config
from database import Database
from tgbeta import AnswerWidgetQuery, EditMessageWidgetData, SendWidget
from tgbeta import on_widget as tgbeta_on_widget

logger = logging.getLogger("chess.widget")
router = Router(name="chess_widget")

KEY = "wchess:"         # настройка: партия
OPEN = "wchess_open"    # id вызова, который ждёт соперника
START = "wch_"          # /start wch_<id> — ссылка-приглашение
HTML = Path(__file__).with_name("widget.html").read_text("utf-8")
CARD = Path(__file__).resolve().parents[2] / "assets" / "lobby" / "chess.jpg"  # картинка вызова (tools/static_images.py)
CARD_KEY = "wchess_card"  # её file_id после первой отправки
SQ = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+/"  # клетка a1…h8 → знак (как в widget.html)
MAX_DATA = 1024          # chat_data и ответ на запрос — не больше (байт)
NAME_LEN = 18
CAPTION = "♟ Шахматы Пиздюка · не видно доску - обнови Telegram"
GLYPH = {"k": "♚", "q": "♛", "r": "♜", "b": "♝", "n": "♞", "p": "♟"}
DRAWS = {"stalemate": "пат", "material": "не хватает фигур для мата", "repeat": "позиция повторилась трижды",
         "fifty": "50 ходов без взятий и ходов пешкой", "agreed": "по согласию"}

_locks: dict[str, asyncio.Lock] = {}
_bg: set = set()


def _chess():
    import chess

    return chess


def _lock(gid: str) -> asyncio.Lock:
    if len(_locks) > 300:
        _locks.clear()
    return _locks.setdefault(gid, asyncio.Lock())


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _bg.add(task)
    task.add_done_callback(_bg.discard)


async def _load(db: Database, gid: str) -> dict | None:
    raw = await db.get_setting(KEY + gid, "")
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


async def _save(db: Database, gid: str, st: dict) -> None:
    st["ts"] = time.time()
    await db.set_setting(KEY + gid, json.dumps(st, ensure_ascii=False))


def _owner() -> int:
    raw = os.getenv("BOT_FATHER_ID", "") or os.getenv("ADMIN_IDS", "").replace(";", ",").split(",")[0]
    try:
        return int(raw.strip())
    except ValueError:
        return 0


def _name(user) -> str:
    if user is None:
        return "Игрок"
    if user.id == _owner():
        return "Батя"
    return (user.first_name or user.username or "Игрок")[:40]


def _color(st: dict, uid: int) -> str:
    if st.get("w") and st["w"][0] == uid:
        return "w"
    if st.get("b") and st["b"][0] == uid:
        return "b"
    return ""


def _opp(st: dict, color: str) -> list | None:
    return st.get("b") if color == "w" else st.get("w") if color == "b" else None


def _board(st: dict):
    b = _chess().Board()
    for uci in st.get("moves", []):
        b.push_uci(uci)
    return b


def _legal(b) -> str:
    """Ходы стороны, чей ход: пары знаков «откуда-куда» (превращения — одной парой, пешка идёт в ферзя)."""
    return "".join(dict.fromkeys(SQ[m.from_square] + SQ[m.to_square] for m in b.legal_moves))


def _result(b) -> str:
    if b.is_checkmate():
        return "mate"
    if b.is_stalemate():
        return "stalemate"
    if b.is_insufficient_material():
        return "material"
    if b.can_claim_threefold_repetition():
        return "repeat"
    if b.can_claim_fifty_moves():
        return "fifty"
    return ""


def _players(st: dict, end: dict) -> tuple[str, str]:
    """(победитель, проигравший) по именам."""
    win = end.get("win")
    w, b = st.get("w") or [0, "?"], st.get("b") or [0, "?"]
    return (w[1], b[1]) if win == w[0] else (b[1], w[1])


def end_line(st: dict) -> str:
    """Итог для канала и повтора: «Мат! Вася победил Батю» не склоняем — «Мат! Победил Вася»."""
    end = st.get("end") or {}
    why = end.get("why", "")
    if why == "cancel":
        return "Вызов отменён"
    if why in DRAWS:
        return "Ничья: " + DRAWS[why]
    winner, loser = _players(st, end)
    if why == "mate":
        return f"Мат! Победил {winner}"
    if why == "resign":
        return f"{loser} сдался. Победил {winner}"
    return "Партия окончена"


def _end_for(st: dict, uid: int) -> str:
    end = st.get("end") or {}
    why = end.get("why", "")
    if why in ("mate", "resign"):
        winner, loser = _players(st, end)
        if end.get("win") == uid:
            return "Мат! Ты победил" if why == "mate" else f"{loser} сдался - ты победил"
        return f"Мат. Победил {winner}" if why == "mate" else "Ты сдался"
    return end_line(st)


def _view(st: dict, uid: int) -> str:
    """Состояние доски игрока uid (chat_data его сообщения) — JSON, как его читает widget.html."""
    chess = _chess()
    b = _board(st)
    me = _color(st, uid)
    opp = _opp(st, me)
    turn = "w" if b.turn else "b"
    v: dict = {"g": st["id"], "c": me, "f": b.board_fen(), "t": turn, "n": b.fullmove_number}
    if opp:
        v["o"] = opp[1][:NAME_LEN]
    if st.get("moves"):
        last = chess.Move.from_uci(st["moves"][-1])
        v["m"] = SQ[last.from_square] + SQ[last.to_square]
    if b.is_check():  # шах, а в конце — заматованный король
        v["k"] = SQ[b.king(b.turn)]
    if st.get("end"):
        v["e"] = 1
        v["s"] = _end_for(st, uid)
        why, win = st["end"].get("why"), st["end"].get("win")
        v["x"] = "c" if why == "cancel" else "d" if why in DRAWS else "w" if win == uid else "l"  # значок итога
        if st.get("next") or st["end"].get("why") == "cancel":
            v["r"] = 3  # реванш уже идёт / вызов отменён — кнопки нет
        elif st.get("rematch"):
            v["r"] = 2 if st["rematch"] == uid else 1
    elif not opp:
        v["s"] = "Ждём соперника - ссылка в канале"
    elif turn == me:
        v["s"] = "Твой ход" + (" - шах!" if b.is_check() else "")
        v["l"] = _legal(b)
    else:
        v["s"] = f"Ходит {opp[1][:NAME_LEN]}"
    if st.get("draw") and not st.get("end"):
        v["d"] = 2 if st["draw"] == uid else 1
    data = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    if len(data.encode()) > MAX_DATA:  # почти не бывает: самые длинные имена и под 200 ходов на выбор
        v["o"] = v.get("o", "")[:6]
        v["s"] = v.get("s", "")[:24]
        data = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return data


def _move_text(piece, move, capture: bool, san: str) -> str:
    chess = _chess()
    sign = "#" if san.endswith("#") else "+" if san.endswith("+") else ""
    return (f"{GLYPH[piece.symbol().lower()]}︎ {chess.square_name(move.from_square)}"
            f"{'×' if capture else '-'}{chess.square_name(move.to_square)}{sign}")


# ---------- сообщения ----------


async def _send_board(bot: Bot, st: dict, uid: int) -> None:
    """Доска-виджет игроку в личку; id сообщения — в st["msgs"]."""
    sent = await bot(SendWidget(chat_id=uid, html=HTML, allow_queries=True, chat_data=_view(st, uid),
                                caption=CAPTION))
    st.setdefault("msgs", {})[str(uid)] = int(sent["message_id"])


async def _push(bot: Bot, st: dict, uid: int) -> None:
    """Обновить доску игрока (правка состояния — молча, без «изменено» и уведомления)."""
    mid = (st.get("msgs") or {}).get(str(uid))
    if not mid:
        return
    try:
        await bot(EditMessageWidgetData(chat_id=uid, message_id=mid, chat_data=_view(st, uid)))
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            logger.info("шахматы-виджет %s: доску %s не обновил: %s", st.get("id"), uid, e)
    except Exception as e:  # noqa: BLE001 — сеть: следующий ход поправит
        logger.info("шахматы-виджет %s: доску %s не обновил: %s", st.get("id"), uid, e)


async def _unnote(bot: Bot, st: dict, uid: int) -> None:
    mid = (st.get("notes") or {}).pop(str(uid), None)
    if mid:
        try:
            await bot.delete_message(uid, mid)
        except Exception:  # noqa: BLE001 — уже удалено
            pass


async def _note(bot: Bot, st: dict, uid: int, text: str) -> None:
    """Сообщение игроку под доской — с уведомлением (правка доски молчит). Прошлое такое — удаляется."""
    await _unnote(bot, st, uid)
    try:
        msg = await bot.send_message(uid, text)
        st.setdefault("notes", {})[str(uid)] = msg.message_id
    except Exception as e:  # noqa: BLE001 — человек заблокировал бота и т.п.
        logger.info("шахматы-виджет: не смог написать %s: %s", uid, e)


def _side(st: dict, uid: int) -> str:
    return "белыми - твой ход" if _color(st, uid) == "w" else "чёрными, первым ходит соперник"


# ---------- вызов и вход ----------


async def _send_card(bot: Bot, db: Database, uid: int, text: str, kb: InlineKeyboardMarkup, card: Path = CARD,
                     key: str = CARD_KEY) -> Message:
    """Вызов картинкой (assets/lobby/chess.jpg, потом по file_id; дурак — своя); не вышло — тем же текстом."""
    cached = await db.get_setting(key, "")
    for photo in ([cached] if cached else []) + [FSInputFile(card)]:  # нет файла — отправка упадёт, дальше текст
        try:
            msg = await bot.send_photo(uid, photo, caption=text, reply_markup=kb)
            if not isinstance(photo, str) and msg.photo:
                await db.set_setting(key, msg.photo[-1].file_id)
            return msg
        except Exception as e:  # noqa: BLE001 — протухший file_id, нет файла: дальше
            logger.info("шахматы-виджет: картинка вызова не ушла (%s)", e)
            if isinstance(photo, str):
                await db.set_setting(key, "")
    return await bot.send_message(uid, text, reply_markup=kb, disable_web_page_preview=True)


async def create(bot: Bot, db: Database, uid: int, name: str = "") -> None:
    """Новый вызов: доска вызвавшему, отдельным сообщением ссылка, её пересылка в канал."""
    from handlers.channel_posts import channel_id, mark_own

    old = await db.get_setting(OPEN, "")
    if old:  # прошлый вызов без соперника — отменить
        async with _lock(old):
            st = await _load(db, old)
            if st and not st.get("w") and not st.get("end"):
                st["end"] = {"why": "cancel", "win": 0}
                await _push(bot, st, st["host"][0])
                await _save(db, old, st)
    gid = secrets.token_urlsafe(6)
    name = name or ("Батя" if uid == _owner() else "Игрок")
    st = {"id": gid, "host": [uid, name], "w": None, "b": None, "moves": [], "created": time.time(),
          "msgs": {}, "notes": {}}
    try:
        await _send_board(bot, st, uid)
    except TelegramBadRequest as e:
        await bot.send_message(uid, f"❌ Telegram не принял доску-виджет: <code>{html.escape(str(e)[:200])}</code>")
        return
    await _save(db, gid, st)  # до ссылки: кто-то может нажать её через секунду после пересылки
    await db.set_setting(OPEN, gid)
    me = await bot.me()
    link = f"https://t.me/{me.username}?start={START}{gid}"
    text = (f"♟ <b>{html.escape(name)} ищет соперника</b>\n\n"
            "Кто первым нажмёт «Сыграть» - тот и играет. Партия - в личке у Пиздюка, доска прямо в сообщении "
            "(нужен свежий Telegram).\n\n"
            f'👉 <a href="{link}">Сыграть</a>')  # ссылка и в тексте: вдруг кнопку при пересылке не покажут
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="♟ Сыграть", url=link)]])
    invite = await _send_card(bot, db, uid, text, kb)
    channel, post = await channel_id(bot, db), None
    if channel:
        try:
            post = await bot.forward_message(channel, uid, invite.message_id)
            await mark_own(db, post.message_id)  # стёб-бот не обсуждает свой же пост
            report = "✅ Переслал в канал. Как кто-то нажмёт - напишу, доска выше оживёт."
        except Exception as e:  # noqa: BLE001
            logger.warning("шахматы-виджет: в канал не переслалось: %s", e)
            report = f"⚠️ В канал не переслалось ({html.escape(str(e)[:150])}) - перешли ссылку сам."
    else:
        report = "⚠️ Канал не нашёл (бот должен быть админом канала) - перешли ссылку сам."
    async with _lock(gid):
        st = await _load(db, gid) or st
        st["invite"] = invite.message_id
        if post:
            st["post"] = [channel, post.message_id]
        if not st.get("w"):  # соперник мог успеть раньше — тогда ему уже не до отчёта
            await _note(bot, st, uid, report)
        await _save(db, gid, st)
    logger.info("шахматы-виджет %s: вызов от %s, пост %s", gid, name, st.get("post"))


async def join(bot: Bot, db: Database, user, gid: str) -> None:
    """/start wch_<id>: первый, кто открыл ссылку, — соперник."""
    uid = user.id
    async with _lock(gid):
        st = await _load(db, gid)
        if not st:
            await bot.send_message(uid, "♟ Такой партии нет - жди новый вызов в канале.")
            return
        if _color(st, uid):  # свой же игрок — прислать доску заново (потерял)
            await _send_board(bot, st, uid)
            await _save(db, gid, st)
            return
        if st.get("end"):
            await bot.send_message(uid, "♟ Эта партия уже сыграна - жди новый вызов в канале.")
            return
        if st.get("w"):
            await bot.send_message(uid, "♟ Соперник уже нашёлся, партия идёт. Жди следующий вызов в канале.")
            return
        host = st["host"]
        if uid == host[0]:
            await bot.send_message(uid, "♟ Это твой вызов - ждём соперника, ссылка в канале.")
            return
        guest = [uid, _name(user)]
        pair = [host, guest]
        random.shuffle(pair)
        st["w"], st["b"] = pair
        st["start"] = time.time()
        await bot.send_message(uid, f"♟ Партия началась! Соперник: {html.escape(host[1])}. Ты {_side(st, uid)}.\n"
                                    "Доска ниже: нажми свою фигуру, потом клетку. Не видно доску - обнови Telegram.")
        try:
            await _send_board(bot, st, uid)
        except TelegramBadRequest as e:
            st["w"] = st["b"] = None
            st.pop("start", None)
            await bot.send_message(uid, f"❌ Telegram не принял доску: <code>{html.escape(str(e)[:200])}</code>")
            await _save(db, gid, st)
            return
        await _push(bot, st, host[0])
        await _note(bot, st, host[0], f"♟ {html.escape(guest[1])} принял вызов! Ты {_side(st, host[0])}.")
        await _save(db, gid, st)
    if await db.get_setting(OPEN, "") == gid:
        await db.set_setting(OPEN, "")
    logger.info("шахматы-виджет %s: %s (белые) против %s", gid, st["w"][1], st["b"][1])


# ---------- запросы доски ----------


def _apply(st: dict | None, uid: int, kind: str, arg: str, chat, mid) -> tuple[dict, dict]:
    """Запрос доски → (ответ виджету, что сделать после: доски, сообщения, конец, реванш). Меняет st."""
    ev: dict = {"push": [], "note": {}, "unnote": [], "done": False, "rematch": False, "changed": False}
    if not st:
        return {"err": "Партия не найдена"}, ev
    me = _color(st, uid)
    if not me:
        if st["host"][0] == uid and not st.get("w"):
            return ({"s": json.loads(_view(st, uid))} if kind == "s" else {"err": "Ждём соперника"}), ev
        return {"err": "Это не твоя партия"}, ev
    if chat == uid and mid and (st.get("msgs") or {}).get(str(uid)) != mid:
        st.setdefault("msgs", {})[str(uid)] = mid  # он ходит с этой доски — её и обновлять
        ev["changed"] = True
    opp = _opp(st, me)
    oid, my_name = opp[0], (st["w"] if me == "w" else st["b"])[1]
    end = st.get("end")
    if kind == "s":
        return {"s": json.loads(_view(st, uid))}, ev
    if kind in ("m", "r", "d") and end:
        return {"err": "Партия окончена"}, ev
    if kind == "m":
        chess = _chess()
        b = _board(st)
        if ("w" if b.turn else "b") != me:
            return {"err": f"Сейчас ходит {opp[1][:NAME_LEN]}"}, ev
        if len(arg) != 2 or arg[0] not in SQ or arg[1] not in SQ:
            return {"err": "Непонятный ход"}, ev
        fr, to = SQ.index(arg[0]), SQ.index(arg[1])
        moves = [m for m in b.legal_moves if m.from_square == fr and m.to_square == to]
        if not moves:
            return {"err": "Так ходить нельзя"}, ev
        move = next((m for m in moves if m.promotion in (None, chess.QUEEN)), moves[0])
        piece, capture, san = b.piece_at(fr), b.is_capture(move), b.san(move)
        b.push(move)
        st["moves"].append(move.uci())
        st["draw"] = None
        why = _result(b)
        if why:
            st["end"] = {"why": why, "win": uid if why == "mate" else 0}
            ev["done"] = True
        else:
            ev["note"][oid] = f"♟ {html.escape(my_name)}: {_move_text(piece, move, capture, san)}. Твой ход."
        ev["unnote"].append(uid)
    elif kind == "r":
        st["end"] = {"why": "resign", "win": oid}
        ev["done"] = True
    elif kind == "d":
        if st.get("draw") == oid:
            st["end"] = {"why": "agreed", "win": 0}
            ev["done"] = True
        elif st.get("draw") == uid:
            return {"err": "Ты уже предложил - ждём ответа"}, ev
        else:
            st["draw"] = uid
            ev["note"][oid] = (f"🤝 {html.escape(my_name)} предлагает ничью - жми «🤝 Согласен» под доской "
                               "или просто ходи.")
    elif kind == "n":
        if not end or end.get("why") == "cancel":
            return {"err": "Партия ещё идёт"}, ev
        if st.get("next"):
            return {"err": "Реванш уже идёт - доска ниже"}, ev
        if st.get("rematch") == uid:
            return {"err": "Ждём ответа соперника"}, ev
        if st.get("rematch") == oid:
            st["rematch"] = None
            ev["rematch"] = True
        else:
            st["rematch"] = uid
            ev["note"][oid] = f"🔁 {html.escape(my_name)} хочет реванш - жми «🔁 Принять реванш» под доской."
    else:
        return {"err": "Непонятный запрос"}, ev
    ev["changed"] = True
    ev["push"] = [uid, oid]
    return {"s": json.loads(_view(st, uid))}, ev


async def _answer(bot: Bot, qid: str, answer: dict) -> None:
    data = json.dumps(answer, ensure_ascii=False, separators=(",", ":"))
    if len(data.encode()) > MAX_DATA:
        data = '{"ok":1}'  # доска возьмёт состояние из правки
    try:
        await bot(AnswerWidgetQuery(widget_query_id=qid, data=data))
    except Exception as e:  # noqa: BLE001 — просрочен (15 с) и т.п.
        logger.info("answerWidgetQuery %s: %s", qid, e)


async def on_query(bot: Bot, db: Database, q: dict) -> None:
    """Обновление widget_query: ход, сдаться, ничья, реванш, «покажи состояние»."""
    qid = str(q.get("id") or "")
    uid = int((q.get("from") or {}).get("id") or 0)
    chat = (q.get("chat") or {}).get("id")
    mid = q.get("message_id")
    kind, gid, arg = (str(q.get("data") or "").split(":") + ["", "", ""])[:3]
    if not gid or not uid:
        await _answer(bot, qid, {"err": "Непонятный запрос"})
        return
    async with _lock(gid):
        st = await _load(db, gid)
        try:
            answer, ev = _apply(st, uid, kind, arg, chat, mid)
        except Exception:  # noqa: BLE001
            logger.exception("шахматы-виджет %s: запрос %r", gid, q.get("data"))
            answer, ev = {"err": "Ошибка у бота - попробуй ещё"}, {"changed": False}
        if not ev.get("changed"):
            await _answer(bot, qid, answer)
            return
        await _save(db, gid, st)
        await _answer(bot, qid, answer)  # сначала ответ — доска нажавшего обновится сразу
        for who in ev["push"]:
            await _push(bot, st, who)
        for who in ev["unnote"]:
            await _unnote(bot, st, who)
        for who, text in ev["note"].items():
            await _note(bot, st, who, text)
        if ev["done"]:
            for who in (st["w"][0], st["b"][0]):
                await _unnote(bot, st, who)
                won = (st.get("end") or {}).get("win") == who
                await _note(bot, st, who, ("🏆 " if won else "🏁 ") + html.escape(_end_for(st, who)))
            logger.info("шахматы-виджет %s: %s", gid, end_line(st))
        await _save(db, gid, st)
    if ev["done"]:
        _spawn(_replay(bot, db, dict(st)))
    if ev["rematch"]:
        await _rematch(bot, db, gid)


async def _rematch(bot: Bot, db: Database, gid: str) -> None:
    """Оба нажали «Реванш»: новая партия, цвета наоборот, новые доски обоим."""
    async with _lock(gid):
        old = await _load(db, gid)
        if not old or old.get("next"):
            return
        new = secrets.token_urlsafe(6)
        st = {"id": new, "host": old["host"], "w": old["b"], "b": old["w"], "moves": [], "created": time.time(),
              "start": time.time(), "msgs": {}, "notes": {}}
        old["next"] = new
        await _save(db, gid, old)
        for p in (st["w"], st["b"]):
            await _unnote(bot, old, p[0])
            await _push(bot, old, p[0])
        await _save(db, gid, old)
    async with _lock(new):
        for p in (st["w"], st["b"]):
            try:
                await bot.send_message(p[0], f"🔁 Реванш! Ты {_side(st, p[0])}.")
                await _send_board(bot, st, p[0])
            except Exception as e:  # noqa: BLE001
                logger.warning("шахматы-виджет: доска реванша %s не ушла: %s", p[0], e)
        await _save(db, new, st)
    logger.info("шахматы-виджет %s: реванш %s", gid, new)


async def _replay(bot: Bot, db: Database, st: dict) -> None:
    """Повтор партии видео — обоим игрокам и в канал (если ходов хоть пара)."""
    from handlers.channel_posts import channel_id, mark_own

    from . import replay

    moves = st.get("moves") or []
    if len(moves) < 2:
        return
    w, b = st["w"][1], st["b"][1]
    minutes = round((time.time() - st["start"]) / 60) if st.get("start") else None
    args = (moves, w, b, end_line(st), minutes)
    try:
        media = BufferedInputFile(await asyncio.to_thread(replay.replay_video, *args), "chess.mp4")
    except Exception as e:  # noqa: BLE001 — нет кодека: гифка
        logger.warning("шахматы-виджет: видео повтора не собралось (%s) — гифка", e)
        try:
            media = BufferedInputFile(await asyncio.to_thread(replay.replay_gif, *args), "chess.gif")
        except Exception:  # noqa: BLE001
            logger.exception("шахматы-виджет: повтор не собрался")
            return
    caption = f"🎞 Повтор: {html.escape(w)} - {html.escape(b)}. {html.escape(end_line(st))}"
    targets = [st["w"][0], st["b"][0]]
    channel = await channel_id(bot, db)
    file_id = None
    for chat in targets + ([channel] if channel else []):
        try:
            text = caption if chat != channel else (f"♟ <b>Шахматы в личке у Пиздюка</b>\n{html.escape(w)} - "
                                                    f"{html.escape(b)}: {html.escape(end_line(st))}. "
                                                    f"Ходов: {(len(moves) + 1) // 2}")
            msg = await bot.send_animation(chat, file_id or media, caption=text)
            if chat == channel:
                await mark_own(db, msg.message_id)
            if not file_id:
                anim = msg.animation or msg.document
                file_id = anim.file_id if anim else None
        except Exception as e:  # noqa: BLE001 — повтор не главное
            logger.info("шахматы-виджет: повтор в %s не ушёл: %s", chat, e)


tgbeta_on_widget("", on_query)  # остальные запросы виджетов (дурак — «u…», games/durak)


@router.message(Command("chess_widget"))
async def cmd_chess_widget(message: Message, bot: Bot, db: Database, config: Config) -> None:
    """/chess_widget — вызов в шахматы: доска и ссылка в личку, ссылка — пересылкой в канал."""
    import tgbeta

    user = message.from_user
    if user is None or not config.is_admin(user.id):
        return
    if not tgbeta.active():
        from handlers.admin import _restart_when_free

        await tgbeta.enable(db, {"kind": "wchess", "user": user.id, "name": _name(user)})
        await bot.send_message(user.id, "♟ Доска-виджет работает только через бету Telegram (Bot API 10.4) - "
                                        "включаю её и перезапускаюсь. Через ~15 сек пришлю доску и ссылку для канала."
                                        "\n\nЕсли после этого бот начнёт глючить - /beta off.")
        _restart_when_free()
        return
    await create(bot, db, user.id, _name(user))
