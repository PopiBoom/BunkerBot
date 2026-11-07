"""/protocol — «протокол происшествия»: последний срач чата канцелярским языком на бланке с печатью.

Админ пишет /protocol (ответом на сообщение, с которого всё началось, — тогда с него; иначе последние MSGS
сообщений). ИИ (Opus, ai.protocol, ≈$0.01) — нарушитель, потерпевший, обстоятельства, свидетели, «изъято», статья;
«объяснение нарушителя» — ДОСЛОВНО его сообщение (ИИ даёт только id). Картинка — cards.render_protocol.
ОДИН эпизод (первая версия клеила цитаты из четырёх веток — владелец: «бессмыслица»), ≤ 2 цитат — см. ai.protocol.
"""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

import ai
from database import Database
from ephemeral import temp_reply

logger = logging.getLogger("protocol")
router = Router(name="protocol")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))

MSK = timezone(timedelta(hours=3))
MSGS = 40            # без ответа — столько последних сообщений
MAX_MSGS = 80        # с ответа — не больше стольких
COUNT_KEY = "protocol_no:"


@router.message(Command("protocol"))
async def cmd_protocol(message: Message, bot: Bot, db: Database) -> None:
    import cards
    from handlers.roast import _as_dicts, _bunker_active, db_add_own

    chat_id = message.chat.id
    if _bunker_active(chat_id):
        return
    if not ai.available():
        await temp_reply(message, "ИИ сейчас недоступен - протокол не составить.")
        return
    rt = message.reply_to_message
    if rt:
        rows = await db.recent_messages(chat_id, limit=MAX_MSGS, since=int(rt.date.timestamp()) - 1)
    else:
        rows = await db.recent_messages(chat_id, limit=MSGS)
    rows = [r for r in rows if r[1] != 0]  # свои реплики бота — не улики
    if len(rows) < 5:
        await temp_reply(message, "Маловато сообщений для протокола - пусть сначала посрутся.")
        return
    await bot.send_chat_action(chat_id, "upload_photo")
    try:
        p = await ai.protocol(_as_dicts(rows), start_id=rt.message_id if rt else None)
    except Exception as e:
        logger.warning("protocol AI failed: %s", e)
        await temp_reply(message, "Не вышло составить протокол - попробуй ещё раз.")
        return
    if not p["story"] or not p["violator"]:
        await temp_reply(message, "Состава нарушения не выявлено. Скучные вы.")
        return
    by_id = {r[0]: r for r in rows}
    expl = by_id.get(p["explanation_id"])
    explanation = str(expl[3])[:200] if expl and not str(expl[3]).startswith("[") else ""
    no = int(await db.get_setting(COUNT_KEY + str(chat_id), "0") or 0) + 1
    await db.set_setting(COUNT_KEY + str(chat_id), str(no))
    last_ts = rows[-1][4]
    when = datetime.fromtimestamp(last_ts, MSK).strftime("%d.%m.%Y, %H:%M")
    title = message.chat.title or "чат"
    place = f"г. Волгоград, чат «{title}»" + (f", {p['place']}" if p["place"] else "")
    png = await asyncio.to_thread(cards.render_protocol, no, when, place, p["violator"], p["victim"], p["story"],
                                  explanation, p["witnesses"], p["seized"], p["article"])
    sent = await bot.send_photo(chat_id, BufferedInputFile(png, "protocol.png"),
                                caption=f"🚔 <b>Протокол № {no:04d}</b> · {html.escape(p['article'] or 'срач')}")
    try:
        await db_add_own(chat_id, sent.message_id, f"[протокол № {no}] нарушитель {p['violator']}: {p['story']}")
    except Exception as e:
        logger.debug("protocol memory: %s", e)
