"""Цитатник — «золотой фонд» чата.

  * ответь /quote на сообщение — оно попадёт в фонд;
  * /quote — случайная цитата картинкой, /quote @ник — цитата этого человека;
  * раз в день (вечером, вместе с клоуном дня) бот сам добавляет 1-2 лучшие фразы дня;
  * старые цитаты человека бот иногда припоминает в прожарках и ответах (см. roast.py).
"""
from __future__ import annotations

import asyncio
import html
import io
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message
from PIL import Image, ImageDraw

import ai
from database import Database

logger = logging.getLogger("quotes")
router = Router(name="quotes")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))

MSK = timezone(timedelta(hours=3))
QUOTE_COOLDOWN = 10        # сек между случайными цитатами в чате
MAX_QUOTE_LEN = 600
DAILY_PICK = 2             # сколько фраз дня бот сам кладёт в фонд
_last_quote: dict[int, float] = {}
_NON_BMP = re.compile(r"[\U00010000-\U0010FFFF☀-➿️]")  # эмодзи — в шрифте их нет


def _bunker_active(chat_id: int) -> bool:
    from handlers.roast import _bunker_active as active
    return active(chat_id)


# ---------- картинка ----------


def render_quote(num: int, text: str, name: str, date: str) -> bytes:
    """Карточка цитаты в стиле карточек Бункера: тёмный фон, сигнальная лента, «ЗОЛОТОЙ ФОНД»."""
    from games.bunker.render import (ACCENT, BG, LINE, MUTED, PAD, TEXT, W, _bold, _hazard_band, _mono,
                                     _noise, _regular, _wrap)

    text = _NON_BMP.sub("", text).strip() or "…"
    name = _NON_BMP.sub("", name).strip() or "Аноним"
    n = len(text)
    size = 60 if n < 50 else 50 if n < 130 else 42 if n < 260 else 36
    font = _bold(size) if n < 50 else _regular(size)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    lines = _wrap(probe, text, font, W - 2 * PAD - 40, 14)
    line_h = int(size * 1.35)
    top = 210
    height = top + len(lines) * line_h + 60 + 120 + 90

    img = _noise(Image.new("RGB", (W, height), BG), num)
    _hazard_band(img, 0, 34)
    _hazard_band(img, height - 34, 34)
    d = ImageDraw.Draw(img)
    d.text((PAD, 74), "ЗОЛОТОЙ ФОНД ЧАТА", font=_mono(30), fill=ACCENT)
    d.text((W - PAD, 74), f"№ {num:04d}", font=_mono(30), fill=MUTED, anchor="ra")
    d.line([(PAD, 130), (W - PAD, 130)], fill=LINE, width=3)
    d.text((PAD - 10, 112), "«", font=_bold(170), fill=(58, 62, 70))  # большая кавычка фоном
    y = top
    for line in lines:
        d.text((PAD + 24, y), line, font=font, fill=TEXT)
        y += line_h
    y += 50
    d.text((W - PAD, y), f"— {name}", font=_bold(40), fill=ACCENT, anchor="ra")
    d.text((W - PAD, y + 58), date, font=_regular(26), fill=MUTED, anchor="ra")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)  # из-за зерна фона PNG весит полмегабайта
    return buf.getvalue()


# ---------- /quote ----------


@router.message(Command("quote"))
async def cmd_quote(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    chat_id = message.chat.id
    if _bunker_active(chat_id):
        await message.reply("Идёт Бункер — цитаты после партии 📜")
        return
    rt = message.reply_to_message
    if rt is not None:
        await _save_quote(message, rt, bot, db)
        return

    now = time.time()
    if now - _last_quote.get(chat_id, 0) < QUOTE_COOLDOWN:
        return
    _last_quote[chat_id] = now
    user_id = None
    arg = (command.args or "").strip()
    if arg.startswith("@"):
        found = await db.find_user(chat_id, arg.split()[0])
        if not found:
            await message.reply("Не знаю такого. Пусть сначала что-нибудь напишет в чат.")
            return
        user_id = found[0]
    row = await db.random_quote(chat_id, user_id)
    if not row:
        await message.reply("📜 Золотой фонд пока пуст" + (" у этого человека" if user_id else "") + ".\n\n"
                            "Ответь /quote на смешное сообщение — и оно попадёт в фонд. "
                            "А ещё я сам каждый вечер добавляю туда лучшие фразы дня.")
        return
    num, _uid, name, text, ts = row
    date = datetime.fromtimestamp(ts, MSK).strftime("%d.%m.%Y")
    try:
        png = await asyncio.to_thread(render_quote, num, text, name or "Аноним", date)
        await message.answer_photo(BufferedInputFile(png, filename=f"quote-{num}.jpg"),
                                   caption=f"📜 Цитата №{num} · <b>{html.escape(name or 'Аноним')}</b>, {date}")
    except Exception as e:  # картинка не вышла — шлём текстом
        logger.warning("quote render failed: %s", e)
        await message.answer(f"📜 <b>Цитата №{num}</b>\n\n<blockquote>{html.escape(text)}</blockquote>\n"
                             f"— <b>{html.escape(name or 'Аноним')}</b>, {date}")


async def _save_quote(message: Message, rt: Message, bot: Bot, db: Database) -> None:
    text = (rt.text or rt.caption or "").strip()
    if not text or text.startswith("/"):
        await message.reply("В цитатник — только сообщения с текстом.")
        return
    if rt.forward_origin is not None:
        await message.reply("Это пересланное — чужие слова. В фонд — только сказанное здесь.")
        return
    if len(text) > MAX_QUOTE_LEN:
        await message.reply("Слишком длинно для цитаты — это уже мемуары.")
        return
    author = rt.from_user
    if rt.sender_chat is not None and rt.sender_chat.type == "channel":
        user_id, name = -1, rt.sender_chat.title or "канал"
    elif author is None:
        return
    elif author.id == bot.id:
        user_id, name = 0, "Пиздюк"
    elif author.is_bot:
        await message.reply("Других ботов не цитирую.")
        return
    else:
        if await db.is_optout(message.chat.id, author.id):
            await message.reply("Этот человек попросил его не трогать — в фонд не кладу.")
            return
        user_id, name = author.id, author.first_name or author.username or "Аноним"
    num = await db.add_quote(message.chat.id, rt.message_id, user_id, name, text,
                             int(rt.date.timestamp()), message.from_user.id if message.from_user else None)
    if num is None:
        await message.reply("Это уже лежит в золотом фонде 📜")
        return
    short = text if len(text) <= 120 else text[:119].rstrip() + "…"
    await message.reply(f"📜 В золотой фонд, №{num}:\n<blockquote>{html.escape(short)}</blockquote>\n"
                        f"— <b>{html.escape(name)}</b>")


# ---------- фразы дня (вызывается из фоновой работы стёб-бота) ----------


async def daily_pick(db: Database, chat_id: int) -> int:
    """Бот сам кладёт в фонд 1-2 лучшие фразы за сегодня. Возвращает, сколько добавил."""
    day_start = int(datetime.now(MSK).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    rows = await db.recent_messages(chat_id, limit=800, since=day_start)
    cands = [r for r in rows if r[1] > 0 and not str(r[3]).startswith("[") and 12 <= len(str(r[3])) <= 300
             and not str(r[3]).startswith("/")]
    if len(cands) < 5:
        return 0
    cands = cands[-300:]
    ids = await ai.pick_quotes([{"id": r[0], "name": r[2], "text": r[3]} for r in cands])
    by_id = {r[0]: r for r in cands}
    added = 0
    for mid in ids[:DAILY_PICK]:
        r = by_id.get(mid)
        if r and await db.add_quote(chat_id, r[0], r[1], r[2] or "Аноним", r[3], r[4], None):
            added += 1
            logger.info("в золотой фонд: %s — %s", r[2], r[3][:80])
    return added
