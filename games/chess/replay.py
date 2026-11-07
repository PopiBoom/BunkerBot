"""Повтор шахматной партии гифкой (ход за ходом; итоговую картинку владелец не захотел — только гифка). Pillow, без ИИ.

Фигуры — те же, что значки кнопок (icons.piece_image), доска — тёплое дерево, последний ход подсвечен, король под
шахом — красным. Гифка: каждый полуход — кадр, последний кадр держится дольше; длинные партии крутятся быстрее,
чтобы весь повтор шёл не дольше REPLAY_MAX сек.
"""
from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

from .icons import piece_image

FONTS = Path(__file__).resolve().parents[2] / "assets" / "fonts"
BG = (16, 14, 20)                      # как у карточек бота (cards.BG)
TEXT, MUTED, ACCENT = (240, 236, 228), (150, 144, 150), (232, 188, 84)
LIGHT, DARK = (236, 220, 190), (170, 122, 84)
LAST = (247, 214, 92, 120)             # подсветка последнего хода
CHECK = (225, 45, 45)
SQ = 80                                # клетка, px
PAD = 40
TOP, BOTTOM = 112, 116                 # шапка с игроками и подвал с ходом
W = PAD * 2 + SQ * 8
H = TOP + SQ * 8 + BOTTOM
REPLAY_MAX = 40.0                      # сек на весь повтор
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


def _board_img(board, last=None):
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
    d = ImageDraw.Draw(im)
    small = _font(True, 15)
    for i in range(8):  # координаты по краю, цветом соседней клетки (a1 — тёмная)
        d.text((i * SQ + SQ - 5, SQ * 8 - 3), "abcdefgh"[i], font=small, anchor="rd",
               fill=DARK if i % 2 else LIGHT)
        d.text((4, (7 - i) * SQ + 3), str(i + 1), font=small, anchor="lt", fill=DARK if i % 2 else LIGHT)
    return im


def _frame(board, white: str, black: str, last=None, caption: str = "", sub: str = "",
           progress: float | None = None, accent: bool = False):
    from PIL import Image, ImageDraw

    im = Image.new("RGBA", (W, H), BG + (255,))
    d = ImageDraw.Draw(im)
    _player(d, PAD, TOP // 2, white, True)
    _player(d, W - PAD, TOP // 2, black, False, align_right=True)
    d.text((W // 2, TOP // 2), "vs", font=_font(False, 22), fill=MUTED, anchor="mm")
    im.alpha_composite(_board_img(board, last), (PAD, TOP))
    y0 = TOP + SQ * 8
    if caption:
        d.text((W // 2, y0 + 44), caption, font=_fit(d, caption, True, 32, W - 2 * PAD),
               fill=ACCENT if accent else TEXT, anchor="mm")
    if sub:
        d.text((W // 2, y0 + 84), sub, font=_fit(d, sub, False, 22, W - 2 * PAD), fill=MUTED, anchor="mm")
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
    step = max(250, min(800, int(REPLAY_MAX * 1000 / max(plies, 1))))
    frames = [_frame(board, white, black, caption="Начало партии", progress=0.0)]
    durations = [1200]
    for i, uci in enumerate(moves, 1):
        move = chess.Move.from_uci(uci)
        text = _move_text(board, move)
        board.push(move)
        sub = "шах!" if board.is_check() and i < plies else ""
        frames.append(_frame(board, white, black, last=move, caption=text, sub=sub, progress=i / plies))
        durations.append(step)
    title, sub = _verdict(board, result)
    if minutes:
        sub += f" · {minutes} мин"
    frames.append(_frame(board, white, black, last=board.peek() if plies else None, caption=title, sub=sub,
                         progress=1.0, accent=True))
    durations.append(4000)
    out = io.BytesIO()
    pal = [f.convert("RGB").quantize(colors=128, dither=0) for f in frames]
    pal[0].save(out, "GIF", save_all=True, append_images=pal[1:], duration=durations, loop=0, optimize=True,
                disposal=1)
    return out.getvalue()
