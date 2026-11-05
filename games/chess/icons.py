"""Фигуры-значки для кнопок доски: свой набор эмодзи (custom emoji) из 12 картинок 100×100.

Шрифт кнопок в Telegram мелкий и не меняется — фигуры-символы ♜ выходили крошечными (владелец: «фигуры
маленькие»). Значок кнопки (Bot API `icon_custom_emoji_id`) — картинка размером с эмодзи: белая фигура с тёмной
обводкой, чёрная со светлой — видно в любой теме. Правило Telegram: значки в кнопках — только в сообщениях,
которые бот отправил сам (/chess в группе), и только если у владельца бота Telegram Premium; в партиях через
«@бот» — только у ботов с ником с Fragment. Поэтому шахматы пробуют значки лишь в /chess, а если Telegram их не
принял — играют обычными символами.

Набор бот создаёт сам один раз (владелец — первый ADMIN_ID): «chess1_by_<ник бота>», id значков — в настройке
chess_icons. Поменял картинки — подними SET (иначе останется старый набор).
"""
from __future__ import annotations

import io
import json
import logging
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, InputSticker

from database import Database

logger = logging.getLogger("chess")

SET = "chess1"
ORDER = "KQRBNPkqrbnp"            # белые, потом чёрные
SETTING = "chess_icons"
GLYPH = {"K": "♚", "Q": "♛", "R": "♜", "B": "♝", "N": "♞", "P": "♟"}  # залитые — силуэт фигуры
FONT = Path(__file__).resolve().parents[2] / "assets" / "fonts" / "DejaVuSans.ttf"
SIZE = 100


def draw(symbol: str) -> bytes:
    """PNG 100×100 с прозрачным фоном: белая фигура с тёмной обводкой или чёрная со светлой."""
    from PIL import Image, ImageDraw, ImageFont

    big = SIZE * 4  # рисуем крупно и уменьшаем — края гладкие
    white = symbol.isupper()
    glyph = GLYPH[symbol.upper()]
    font = ImageFont.truetype(str(FONT), int(big * 0.9))
    stroke = int(big * 0.035)
    im = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    # общий масштаб для всех фигур: пешка ниже короля, как на доске; по высоте — по королю
    king = d.textbbox((0, 0), GLYPH["K"], font=font, stroke_width=stroke)
    box = d.textbbox((0, 0), glyph, font=font, stroke_width=stroke)
    x = (big - (box[2] - box[0])) / 2 - box[0]
    y = big - (big - (king[3] - king[1])) / 2 - king[3]  # все стоят на одной линии снизу
    d.text((x, y), glyph, font=font, fill=(255, 255, 255, 255) if white else (28, 28, 28, 255),
           stroke_width=stroke, stroke_fill=(25, 25, 25, 255) if white else (235, 235, 235, 255))
    out = io.BytesIO()
    im.resize((SIZE, SIZE), Image.LANCZOS).save(out, "PNG", optimize=True)
    return out.getvalue()


async def load(db: Database) -> dict[str, str] | None:
    try:
        icons = json.loads(await db.get_setting(SETTING, "") or "null")
    except ValueError:
        return None
    return icons if isinstance(icons, dict) and set(icons) == set(ORDER) else None


async def ensure(bot: Bot, db: Database, owner_id: int) -> dict[str, str] | None:
    """id значков; набора нет — создать (раз в жизни, ~5 сек). Не вышло — None (играем символами)."""
    icons = await load(db)
    if icons:
        return icons
    me = await bot.me()
    name = f"{SET}_by_{me.username}"
    try:
        try:
            st = await bot.get_sticker_set(name)
        except TelegramAPIError:  # набора ещё нет
            await bot.create_new_sticker_set(
                user_id=owner_id, name=name, title="Шахматы", sticker_type="custom_emoji", needs_repainting=False,
                stickers=[InputSticker(sticker=BufferedInputFile(draw(s), f"{s}.png"), format="static",
                                       emoji_list=["♟️"]) for s in ORDER])
            st = await bot.get_sticker_set(name)
        ids = [s.custom_emoji_id for s in st.stickers]
        if len(ids) != len(ORDER) or not all(ids):
            logger.warning("chess: набор значков %s неполный (%d)", name, len(ids))
            return None
    except TelegramAPIError as e:
        logger.warning("chess: набор значков не создался: %s", e)
        return None
    icons = dict(zip(ORDER, ids, strict=True))
    await db.set_setting(SETTING, json.dumps(icons))
    logger.info("chess: значки фигур готовы (%s)", name)
    return icons
