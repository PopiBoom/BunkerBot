"""Повтор шахматной партии гифкой (ход за ходом; итоговую картинку владелец не захотел — только гифка). Pillow, без ИИ.

Фигуры — те же, что значки кнопок (icons.piece_image), доска — тёплое дерево, ход — СТРЕЛКОЙ (просьба владельца),
король под шахом — красным. Ходы с пометкой (analyse): со Stockfish (если стоит на сервере — ставит deploy/update.sh)
— как на lichess по падению шанса на победу: «??» грубая ошибка, «?» ошибка (с подсказкой, как было лучше), «!»
единственный сильный ход; без движка — грубо по материалу: «!» выиграл фигуру/вилка, «??» отдал фигуру даром. «#» —
мат. Такие кадры держатся дольше.
Темп — STEP мс на ход (владелец: «медленнее»), длинные партии сжимаются до REPLAY_MAX сек, но не быстрее STEP_MIN.
"""
from __future__ import annotations

import io
import logging
import math
import os
import shutil
from functools import lru_cache
from pathlib import Path

from .icons import piece_image

FONTS = Path(__file__).resolve().parents[2] / "assets" / "fonts"
BG = (16, 14, 20)                      # как у карточек бота (cards.BG)
TEXT, MUTED, ACCENT = (240, 236, 228), (150, 144, 150), (232, 188, 84)
LIGHT, DARK = (236, 220, 190), (170, 122, 84)
LAST = (247, 214, 92, 120)             # подсветка последнего хода
CHECK = (225, 45, 45)
ARROW = {None: (255, 170, 0, 190), "good": (60, 190, 90, 200), "bad": (225, 50, 50, 200), "mate": (60, 140, 255, 210),
         "mistake": (240, 140, 30, 200)}
BADGE = {"good": ((60, 170, 80), "!"), "bad": ((210, 45, 45), "??"), "mate": ((50, 120, 230), "#"),
         "mistake": ((225, 120, 20), "?")}
ENGINE_TIME = 0.12      # сек Stockfish на позицию (100 полуходов ≈ 12 сек, рисуется в потоке)
ENGINE_MAX_PLIES = 200  # длиннее — без движка
logger = logging.getLogger("chess")
VALUE = {1: 1, 2: 3, 3: 3, 4: 5, 5: 9, 6: 0}   # пешка, конь, слон, ладья, ферзь, король
WHAT = {1: "пешку", 2: "коня", 3: "слона", 4: "ладью", 5: "ферзя", 6: "короля"}
STEP, STEP_MIN, NOTE_STEP = 1400, 400, 2600     # мс: обычный ход, минимум для длинных партий, ход с пометкой
SQ = 80                                # клетка, px
PAD = 40
TOP, BOTTOM = 112, 116                 # шапка с игроками и подвал с ходом
W = PAD * 2 + SQ * 8
H = TOP + SQ * 8 + BOTTOM
REPLAY_MAX = 75.0                      # сек на весь повтор (длинные партии ускоряются до STEP_MIN)
NAMES = {"K": "король", "Q": "ферзь", "R": "ладья", "B": "слон", "N": "конь", "P": "пешка"}


@lru_cache(maxsize=8)
def _font(bold: bool, size: int):
    from PIL import ImageFont

    return ImageFont.truetype(str(FONTS / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")), size)


@lru_cache(maxsize=16)
def _piece(symbol: str):
    return piece_image(symbol, SQ - 6)


def _fit(d, text: str, bold: bool, size: int, width: int):
    """Шрифт помельче, если строка не влезает."""
    while size > 14 and d.textlength(text, font=_font(bold, size)) > width:
        size -= 2
    return _font(bold, size)


def _player(d, x: int, y: int, name: str, white: bool, align_right: bool = False) -> None:
    font = _fit(d, name, True, 30, W // 2 - PAD - 60)
    r = 15
    tw = d.textlength(name, font=font)
    cx = x - tw - 14 - r if align_right else x + r
    d.ellipse((cx - r, y - r, cx + r, y + r), fill=(245, 245, 245) if white else (20, 20, 20),
              outline=(200, 200, 200) if not white else None, width=2)
    tx = x - tw if align_right else x + 2 * r + 14
    d.text((tx, y), name, font=font, fill=TEXT, anchor="lm")


def _best_capture(board, square: bool = False):
    """Сколько материала сторона, чей ход, выигрывает лучшим взятием прямо сейчас (грубо: взятая фигура минус
    своя, если поле защищено). square=True — ещё и поле, где это взятие."""
    best, where = 0, None
    for m in board.legal_moves:
        if not board.is_capture(m):
            continue
        victim = board.piece_at(m.to_square)
        gain = VALUE[victim.piece_type] if victim else 1  # на проходе — пешка
        attacker = board.piece_at(m.from_square)
        board.push(m)
        defended = board.is_attacked_by(board.turn, m.to_square)
        board.pop()
        if defended:
            gain -= VALUE[attacker.piece_type]
        if gain > best:
            best, where = gain, m.to_square
    return (best, where) if square else best


def _mate_in_one(board) -> bool:
    """Есть ли у стороны, чей ход, мат одним ходом."""
    for m in board.legal_moves:
        board.push(m)
        mate = board.is_checkmate()
        board.pop()
        if mate:
            return True
    return False


def engine_path() -> str | None:
    """Stockfish: STOCKFISH_PATH, в PATH или /usr/games (туда его ставит apt; в PATH службы /usr/games нет)."""
    for c in (os.getenv("STOCKFISH_PATH"), shutil.which("stockfish"), "/usr/games/stockfish",
              "/usr/local/bin/stockfish"):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def analyse(moves: list[str]) -> list[tuple[str | None, str]]:
    """Пометка к каждому полуходу: (вид, текст). Есть Stockfish — по нему, нет или упал — по материалу."""
    rough = analyse_rough(moves)
    path = engine_path()
    if not path or not moves or len(moves) > ENGINE_MAX_PLIES:
        return rough
    try:
        return _analyse_engine(moves, path, rough)
    except Exception as e:  # движок не запустился или упал — хватит и грубых пометок
        logger.warning("stockfish: %s — пометки без движка", e)
        return rough


def _win(cp: int) -> float:
    """Шанс на победу (0–100) по оценке в сантипешках — формула lichess."""
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def _short_move(board, move) -> str:
    from .icons import GLYPH

    import chess

    p = board.piece_at(move.from_square)
    sep = "×" if board.is_capture(move) else "-"
    return f"{GLYPH[p.symbol().upper()]} {chess.square_name(move.from_square)}{sep}{chess.square_name(move.to_square)}"


def _analyse_engine(moves: list[str], path: str, rough: list) -> list[tuple[str | None, str]]:
    """Как lichess: падение шанса на победу ≥ 30 — «??», ≥ 15 — «?» (с подсказкой лучшего хода); сыгран лучший ход, а
    второй по силе хуже на ≥ 20 — «!». Мат за доской — «#». Текст «!»/«??» — из грубого разбора, если он понял, что
    случилось (вилка, отдал ферзя), иначе общий."""
    import chess
    import chess.engine

    limit = chess.engine.Limit(time=ENGINE_TIME)
    out: list[tuple[str | None, str]] = []
    with chess.engine.SimpleEngine.popen_uci(path) as eng:
        try:
            eng.configure({"Threads": 1, "Hash": 32})
        except chess.engine.EngineError:
            pass
        b = chess.Board()
        info = eng.analyse(b, limit, multipv=2)
        prev_cap = None  # поле, где соперник только что взял: взять в ответ — не «!», это просто размен
        for i, uci in enumerate(moves):
            m = chess.Move.from_uci(uci)
            side = b.turn
            recapture = prev_cap == m.to_square and b.is_capture(m)
            prev_cap = m.to_square if b.is_capture(m) else None
            best_move = info[0]["pv"][0] if info and info[0].get("pv") else None
            best = info[0]["score"].pov(side)
            second = info[1]["score"].pov(side) if len(info) > 1 else None
            best_text = _short_move(b, best_move) if best_move and best_move != m else ""
            b.push(m)
            if b.is_checkmate():
                out.append(("mate", "Мат!"))
                break
            if b.is_game_over():
                out.append(rough[i])
                break
            info = eng.analyse(b, limit, multipv=2)
            after = info[0]["score"].pov(side)
            w_best, w_after = _win(best.score(mate_score=10000)), _win(after.score(mate_score=10000))
            drop = w_best - w_after
            rough_kind, rough_text = rough[i]
            if after.is_mate() and after.mate() < 0 and not (best.is_mate() and best.mate() < 0):
                out.append(("bad", f"Зевок: мат в {abs(after.mate())}" + (f", лучше {best_text}" if best_text else "")))
            elif best.is_mate() and best.mate() > 0 and not (after.is_mate() and after.mate() > 0):
                out.append(("bad", f"Упустил мат в {best.mate()}" + (f": {best_text}" if best_text else "")))
            elif drop >= 30:
                text = rough_text if rough_kind == "bad" else "Грубая ошибка"
                out.append(("bad", text + (f", лучше {best_text}" if best_text else "")))
            elif drop >= 15:
                out.append(("mistake", "Ошибка" + (f", лучше {best_text}" if best_text else "")))
            elif m == best_move and second is not None and 10 <= w_best <= 95 and not recapture and \
                    w_best - _win(second.score(mate_score=10000)) >= 20:
                out.append(("good", rough_text if rough_kind == "good" else "Единственный сильный ход!"))
            elif rough_kind == "good" and drop < 5:
                out.append(rough[i])  # вилка или взял даром — и движок не против
            else:
                out.append((None, ""))
    out += [(None, "")] * (len(moves) - len(out))
    return out


def analyse_rough(moves: list[str]) -> list[tuple[str | None, str]]:
    """Пометка к каждому полуходу: (вид, текст) — "good" «Выиграл ферзя!», "bad" «Зевок ладьи», "mate", или (None, "").

    Без шахматного движка: по материалу на один ход вперёд — ловит явное (отдал фигуру даром, забрал даром, вилка),
    тонкое не видит."""
    import chess

    def threat(board) -> int:  # что соперник забрал бы, если бы ходил сейчас он (ход «пропустить»)
        if board.is_check():
            return 0
        board.push(chess.Move.null())
        t = _best_capture(board)
        board.pop()
        return t

    b = chess.Board()
    out: list[tuple[str | None, str]] = []
    prev_capture: tuple[int, int] | None = None   # (поле, сколько взяли) прошлым полуходом — чтобы отличать размен
    for uci in moves:
        m = chess.Move.from_uci(uci)
        mover = b.piece_at(m.from_square)
        victim = b.piece_at(m.to_square)
        took = VALUE[victim.piece_type] if victim else (1 if b.is_en_passant(m) else 0)
        before = threat(b)  # угроза, которая висела ещё до хода, — не его зевок
        recapture = prev_capture is not None and prev_capture[0] == m.to_square
        net_took = took - (prev_capture[1] if recapture else 0)  # размен: взял в ответ — не «выиграл фигуру»
        prev_capture = (m.to_square, took) if took else None
        b.push(m)
        note: tuple[str | None, str] = (None, "")
        if b.is_checkmate():
            note = ("mate", "Мат!")
        elif _mate_in_one(b):
            note = ("bad", "Зевок: мат в один ход")
        else:
            loss, where = _best_capture(b, square=True)  # что соперник заберёт следующим ходом
            # контратака — не зевок, но только если угрожает не та самая фигура, которую сейчас и съедят
            counter = 0 if where == m.to_square else threat(b)
            if took - loss <= -2 and loss >= 3 and loss > before and counter < loss:
                # что именно отдал: самая дорогая фигура, которую можно взять
                lost = max((b.piece_at(x.to_square) for x in b.legal_moves
                            if b.is_capture(x) and b.piece_at(x.to_square)),
                           key=lambda p: VALUE[p.piece_type], default=None)
                note = ("bad", f"Зевок: отдал {WHAT[lost.piece_type]}" if lost else "Зевок")
            elif net_took - loss >= 3 and victim:
                note = ("good", f"Забрал {WHAT[victim.piece_type]}!")
            elif m.promotion:
                note = ("good", "Пешка стала ферзём!")
            else:
                targets = [b.piece_at(t) for t in b.attacks(m.to_square)
                           if b.piece_at(t) and b.piece_at(t).color != mover.color]
                big = [t for t in targets if t.piece_type == chess.KING or VALUE[t.piece_type] > VALUE[mover.piece_type]]
                if len(big) >= 2 and loss < 3:
                    order = sorted(big, key=lambda t: -(100 if t.piece_type == chess.KING else VALUE[t.piece_type]))
                    names = " и ".join(WHAT[t.piece_type] for t in order[:2])
                    note = ("good", f"Вилка: бьёт {names}!")
        out.append(note)
    return out


def _arrow(im, move, kind: str | None):
    """Стрелка хода поверх доски (полупрозрачная, как на lichess)."""
    import math

    import chess
    from PIL import Image, ImageDraw

    def center(sq):
        return chess.square_file(sq) * SQ + SQ / 2, (7 - chess.square_rank(sq)) * SQ + SQ / 2

    (x1, y1), (x2, y2) = center(move.from_square), center(move.to_square)
    ang = math.atan2(y2 - y1, x2 - x1)
    head, width = SQ * 0.42, SQ * 0.17
    sx, sy = x1 + math.cos(ang) * SQ * 0.18, y1 + math.sin(ang) * SQ * 0.18   # от края фигуры
    ex, ey = x2 - math.cos(ang) * head * 0.9, y2 - math.sin(ang) * head * 0.9  # до основания наконечника
    over = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    col = ARROW.get(kind, ARROW[None])
    d.line((sx, sy, ex, ey), fill=col, width=int(width))
    tip = (x2 - math.cos(ang) * SQ * 0.12, y2 - math.sin(ang) * SQ * 0.12)
    left = (ex + math.cos(ang + math.pi / 2) * head * 0.55, ey + math.sin(ang + math.pi / 2) * head * 0.55)
    right = (ex + math.cos(ang - math.pi / 2) * head * 0.55, ey + math.sin(ang - math.pi / 2) * head * 0.55)
    d.polygon([tip, left, right], fill=col)
    return Image.alpha_composite(im, over)


def _badge(im, sq, kind: str):
    """Кружок с «!», «??» или «#» в углу клетки, куда пошли."""
    import chess
    from PIL import ImageDraw

    col, mark = BADGE[kind]
    x, y = chess.square_file(sq) * SQ + SQ - 14, (7 - chess.square_rank(sq)) * SQ + 14
    r = 17
    d = ImageDraw.Draw(im)
    d.ellipse((x - r, y - r, x + r, y + r), fill=col, outline=(255, 255, 255), width=3)
    d.text((x, y + 1), mark, font=_font(True, 20 if len(mark) == 1 else 16), fill=(255, 255, 255), anchor="mm")


def _board_img(board, last=None, kind: str | None = None):
    """Доска 640×640 с фигурами, координатами, последним ходом и шахом."""
    import chess
    from PIL import Image, ImageDraw, ImageFilter

    im = Image.new("RGBA", (SQ * 8, SQ * 8))
    d = ImageDraw.Draw(im)
    for rank in range(8):
        for file in range(8):
            x, y = file * SQ, (7 - rank) * SQ
            d.rectangle((x, y, x + SQ - 1, y + SQ - 1), fill=LIGHT if (file + rank) % 2 else DARK)
    if last:
        over = Image.new("RGBA", im.size, (0, 0, 0, 0))
        od = ImageDraw.Draw(over)
        for sq in (last.from_square, last.to_square):
            x, y = chess.square_file(sq) * SQ, (7 - chess.square_rank(sq)) * SQ
            od.rectangle((x, y, x + SQ - 1, y + SQ - 1), fill=LAST)
        im = Image.alpha_composite(im, over)
    if board.is_check():
        k = board.king(board.turn)
        glow = Image.new("RGBA", im.size, (0, 0, 0, 0))
        x, y = chess.square_file(k) * SQ, (7 - chess.square_rank(k)) * SQ
        ImageDraw.Draw(glow).ellipse((x + 6, y + 6, x + SQ - 6, y + SQ - 6), fill=CHECK + (230,))
        im = Image.alpha_composite(im, glow.filter(ImageFilter.GaussianBlur(9)))
    for sq, piece in board.piece_map().items():
        x, y = chess.square_file(sq) * SQ + 3, (7 - chess.square_rank(sq)) * SQ + 3
        im.alpha_composite(_piece(piece.symbol()), (x, y))
    if last:
        im = _arrow(im, last, kind)
        if kind in BADGE:
            _badge(im, last.to_square, kind)
    d = ImageDraw.Draw(im)
    small = _font(True, 15)
    for i in range(8):  # координаты по краю, цветом соседней клетки (a1 — тёмная)
        d.text((i * SQ + SQ - 5, SQ * 8 - 3), "abcdefgh"[i], font=small, anchor="rd",
               fill=DARK if i % 2 else LIGHT)
        d.text((4, (7 - i) * SQ + 3), str(i + 1), font=small, anchor="lt", fill=DARK if i % 2 else LIGHT)
    return im


def _frame(board, white: str, black: str, last=None, caption: str = "", sub: str = "",
           progress: float | None = None, accent: bool = False, kind: str | None = None):
    from PIL import Image, ImageDraw

    im = Image.new("RGBA", (W, H), BG + (255,))
    d = ImageDraw.Draw(im)
    _player(d, PAD, TOP // 2, white, True)
    _player(d, W - PAD, TOP // 2, black, False, align_right=True)
    d.text((W // 2, TOP // 2), "vs", font=_font(False, 22), fill=MUTED, anchor="mm")
    im.alpha_composite(_board_img(board, last, kind), (PAD, TOP))
    y0 = TOP + SQ * 8
    if caption:
        d.text((W // 2, y0 + 44), caption, font=_fit(d, caption, True, 32, W - 2 * PAD),
               fill=ACCENT if accent else TEXT, anchor="mm")
    if sub:
        col = BADGE[kind][0] if kind in BADGE and not accent else MUTED
        bold = kind in BADGE and not accent
        d.text((W // 2, y0 + 84), sub, font=_fit(d, sub, bold, 24 if bold else 22, W - 2 * PAD), fill=col,
               anchor="mm")
    if progress is not None:
        d.rectangle((0, H - 6, W, H), fill=(40, 36, 46))
        d.rectangle((0, H - 6, int(W * progress), H), fill=ACCENT)
    return im


def _move_text(board, move) -> str:
    """«12. ♞ g1-f3» — с номером хода, значком фигуры и взятием."""
    from .icons import GLYPH

    piece = board.piece_at(move.from_square)
    n = board.fullmove_number
    num = f"{n}." if board.turn else f"{n}..."
    sep = "×" if board.is_capture(move) else "-"
    import chess

    text = f"{num} {GLYPH[piece.symbol().upper()]} {chess.square_name(move.from_square)}{sep}" \
           f"{chess.square_name(move.to_square)}"
    if move.promotion:
        text += " = ферзь"
    return text


def _verdict(board, result: str) -> tuple[str, str]:
    moves = board.fullmove_number - (1 if board.turn else 0)
    word = "ход" if moves % 10 == 1 and moves % 100 != 11 else (
        "хода" if moves % 10 in (2, 3, 4) and moves % 100 not in (12, 13, 14) else "ходов")
    return result, f"{moves} {word}"


def replay_gif(moves: list[str], white: str, black: str, result: str, minutes: int | None = None) -> bytes:
    """Гифка всей партии: стартовая позиция, каждый полуход, в конце — итог."""
    import chess

    board = chess.Board()
    plies = len(moves)
    notes = analyse(moves)
    marked = sum(1 for k, _t in notes if k)
    step, note_step = STEP, NOTE_STEP
    total = marked * note_step + (plies - marked) * step
    if total > REPLAY_MAX * 1000:  # длинная партия — ускоряем всё поровну, но не быстрее читаемого
        scale = REPLAY_MAX * 1000 / total
        step, note_step = max(STEP_MIN, int(step * scale)), max(1300, int(note_step * scale))
    frames = [_frame(board, white, black, caption="Начало партии", progress=0.0)]
    durations = [1500]
    for i, uci in enumerate(moves, 1):
        move = chess.Move.from_uci(uci)
        text = _move_text(board, move)
        board.push(move)
        kind, note = notes[i - 1]
        sub = note or ("шах!" if board.is_check() and i < plies else "")
        frames.append(_frame(board, white, black, last=move, caption=text, sub=sub, progress=i / plies, kind=kind))
        durations.append(note_step if kind else step)
    title, sub = _verdict(board, result)
    if minutes:
        sub += f" · {minutes} мин"
    blunders = [sum(1 for j, (k, _t) in enumerate(notes) if k in ("bad", "mistake") and j % 2 == side)
                for side in (0, 1)]
    if any(blunders):
        sub += f" · ошибок: белые {blunders[0]}, чёрные {blunders[1]}"
    frames.append(_frame(board, white, black, last=board.peek() if plies else None, caption=title, sub=sub,
                         progress=1.0, accent=True, kind=notes[-1][0] if notes else None))
    durations.append(4000)
    out = io.BytesIO()
    pal = [f.convert("RGB").quantize(colors=128, dither=0) for f in frames]
    pal[0].save(out, "GIF", save_all=True, append_images=pal[1:], duration=durations, loop=0, optimize=True,
                disposal=1)
    return out.getvalue()
