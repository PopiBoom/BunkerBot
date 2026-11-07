"""Коллекционные карточки участников (идея и макет — с владельцем, 07.11). Без ИИ.

Раз в день в случайное время (DROP_FROM–DROP_TO МСК) бот выкидывает в чат карточку случайного участника — картинкой
с кнопкой «🖐 Поймать»: кто нажал первым, тот её и забрал. Редкость — жребием (обычная 70%, редкая 25%, легендарная
5%; у легендарной — его цитата из золотого фонда). Характеристики (мат, нытьё, сова, болтовня) — из статистики чата
за 30 дней, баллы 1–99 относительно остальных; титул — по самой сильной стороне. Аватарка — фото профиля, если
открыто, иначе буква. Альбом: кнопка «📒 Моя коллекция» (присылает в личку — команды в группе только у админов) и
/collection у админа (ответом — чужой). Колода — кто писал за 30 дней хотя бы MIN_MSGS раз; /forget_me-людей нет.
Вкл/выкл — /admin («🃏 Карточки»).
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from database import Database

logger = logging.getLogger("collect")
router = Router(name="collect")

MSK = timezone(timedelta(hours=3))
ON_KEY = "cards_on"            # «1» — карточки выпадают (по умолчанию да)
DAY_KEY = "cards_day:"         # + chat: «дата|время выпадения|done»
OPEN_KEY = "cards_open:"       # + chat: карточка, которую ещё ловят (json)
DROP_FROM, DROP_TO = 12, 22    # МСК: окно, в которое карточка выпадает
PERIOD = 30 * 86400
MIN_MSGS = 15                  # меньше сообщений за 30 дней — в колоду не попадает
RARITIES = [("обычная", 70), ("редкая", 25), ("легендарная", 5)]
RANK = {"обычная": 0, "редкая": 1, "легендарная": 2}
AVATAR_TTL = 6 * 3600
STAT_NAMES = ("МАТ", "НЫТЬЁ", "СОВА", "БОЛТОВНЯ")
TITLES = {  # по самой сильной стороне: (он, она)
    "МАТ": ("Матершинник", "Матершинница"),
    "НЫТЬЁ": ("Главный нытик", "Королева нытья"),
    "СОВА": ("Ночная сова", "Ночная сова"),
    "БОЛТОВНЯ": ("Болтун", "Болтушка"),
}

_lock = asyncio.Lock()
_avatars: dict[int, tuple[float, bytes | None]] = {}


def _father_id() -> int:
    raw = os.getenv("BOT_FATHER_ID", "") or os.getenv("ADMIN_IDS", "").replace(";", ",").split(",")[0]
    try:
        return int(raw.strip())
    except ValueError:
        return 0


async def deck(db: Database, chat_id: int) -> list[dict]:
    """Колода чата: [{"uid", "name", "num", "stats": [(название, 1–99)], "title"}], № — постоянный по id."""
    from handlers.stats import _collect

    rows = await db.stats_messages(chat_id, int(time.time()) - PERIOD)
    if not rows:
        return []
    s = _collect(rows)
    optout = await db.optout_ids(chat_id)
    members = [u for u, n in s["msgs"].items() if n >= MIN_MSGS and u not in optout and u > 0]
    if not members:
        return []
    father = _father_id()
    members.sort(key=lambda u: (u != father, u))  # батя — всегда № 01
    metrics = {
        "МАТ": {u: s["mat"][u] / max(1, s["text_msgs"][u]) for u in members},
        "НЫТЬЁ": {u: s["whine"][u] / max(1, s["msgs"][u]) for u in members},
        "СОВА": {u: s["night"][u] / max(1, s["msgs"][u]) for u in members},
        "БОЛТОВНЯ": {u: s["msgs"][u] for u in members},
    }
    scores: dict[int, dict[str, int]] = {u: {} for u in members}
    for stat, vals in metrics.items():  # место среди своих → 1–99
        order = sorted(members, key=lambda u: vals[u])
        for i, u in enumerate(order):
            scores[u][stat] = 50 if len(order) == 1 else round(5 + 94 * i / (len(order) - 1))
            if vals[u] == 0:
                scores[u][stat] = min(scores[u][stat], 3)
    genders = {}
    try:
        import ai

        genders = {int(r[0]): ai.gender_of(r[2], r[3]) for r in await db.gender_counts(chat_id)}
    except Exception as e:
        logger.debug("genders: %s", e)
    out = []
    for num, u in enumerate(members, 1):
        st = [(k, scores[u][k]) for k in STAT_NAMES]
        best = max(st, key=lambda kv: kv[1])[0]
        title = "Батя чата" if u == father else TITLES[best][1 if genders.get(u) == "f" else 0]
        out.append({"uid": u, "name": s["names"].get(u) or "Аноним", "num": num, "stats": st, "title": title})
    return out


async def _photo(bot: Bot, user_id: int) -> bytes | None:
    """Фото профиля (если открыто) — с кэшем на несколько часов."""
    hit = _avatars.get(user_id)
    if hit and time.time() - hit[0] < AVATAR_TTL:
        return hit[1]
    data = None
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if photos.total_count and photos.photos:
            sizes = photos.photos[0]
            size = next((p for p in sizes if p.width >= 320), sizes[-1])
            buf = await bot.download(size.file_id)
            data = buf.read() if buf else None
    except Exception as e:
        logger.debug("avatar %s: %s", user_id, e)
    if len(_avatars) > 300:
        _avatars.clear()
    _avatars[user_id] = (time.time(), data)
    return data


def _rarity() -> str:
    return random.choices([r for r, _w in RARITIES], weights=[w for _r, w in RARITIES])[0]


def _kb(open_: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="🖐 Поймать", callback_data="cc")]] if open_ else []
    rows.append([InlineKeyboardButton(text="📒 Моя коллекция", callback_data="cl")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def drop(bot: Bot, db: Database, chat_id: int) -> bool:
    """Выкинуть карточку в чат. False — колода пустая."""
    import cards

    members = await deck(db, chat_id)
    if not members:
        return False
    card = random.choice(members)
    rarity = _rarity()
    quote = ""
    if rarity == "легендарная":
        q = await db.random_quote(chat_id, card["uid"])
        quote = (q[3] if q else "")[:120]
    png = await asyncio.to_thread(cards.render_collect_card, card["name"], card["title"], rarity, card["stats"],
                                  card["num"], len(members), quote, await _photo(bot, card["uid"]), card["num"])
    await _close_open(bot, db, chat_id, "🃏 Никто не поймал - карточка сгорела.")
    stars = {"обычная": "", "редкая": " ★★", "легендарная": " ★★★"}[rarity]
    sent = await bot.send_photo(
        chat_id, BufferedInputFile(png, "card.png"), reply_markup=_kb(True),
        caption=f"🃏 <b>Выпала карточка{stars}!</b>\n{rarity.capitalize()} · {html.escape(card['name'])}\n\n"
                "Кто первый нажмёт - того и карточка.")
    await db.set_setting(OPEN_KEY + str(chat_id), json.dumps({
        "msg": sent.message_id, "uid": card["uid"], "name": card["name"], "rarity": rarity, "ts": time.time()},
        ensure_ascii=False))
    logger.info("карточки %s: выпала %s (%s)", chat_id, card["name"], rarity)
    return True


async def _close_open(bot: Bot, db: Database, chat_id: int, caption: str) -> None:
    raw = await db.get_setting(OPEN_KEY + str(chat_id), "")
    if not raw:
        return
    await db.set_setting(OPEN_KEY + str(chat_id), "")
    try:
        msg = json.loads(raw)["msg"]
        await bot.edit_message_caption(chat_id=chat_id, message_id=msg, caption=caption, reply_markup=_kb(False))
    except Exception as e:
        logger.debug("close old card: %s", e)


@router.callback_query(F.data == "cc")
async def cb_catch(call: CallbackQuery, bot: Bot, db: Database) -> None:
    if not call.message:
        await call.answer()
        return
    chat_id = call.message.chat.id
    async with _lock:
        raw = await db.get_setting(OPEN_KEY + str(chat_id), "")
        st = json.loads(raw) if raw else None
        if not st or st.get("msg") != call.message.message_id:
            await call.answer("Эту карточку уже не поймать.", show_alert=True)
            return
        if await db.is_optout(chat_id, call.from_user.id):
            await call.answer("Тебя тут не запоминают - карточки не для тебя.", show_alert=True)
            return
        await db.set_setting(OPEN_KEY + str(chat_id), "")
        n = await db.add_card(chat_id, call.from_user.id, st["uid"], st["rarity"])
    secs = max(1, round(time.time() - st["ts"]))
    who = call.from_user.first_name or call.from_user.username or "Кто-то"
    tail = "новая в коллекции!" if n == 1 else f"уже ×{n} такая"
    await call.answer(f"Твоя! {st['rarity'].capitalize()} карточка «{st['name']}» - {tail}", show_alert=True)
    try:
        await bot.edit_message_caption(
            chat_id=chat_id, message_id=call.message.message_id, reply_markup=_kb(False),
            caption=f"🃏 {st['rarity'].capitalize()} карточка · {html.escape(st['name'])}\n\n"
                    f"🖐 Поймал(а) <b>{html.escape(who)}</b> за {secs} сек - {tail}")
    except TelegramBadRequest as e:
        logger.debug("caught caption: %s", e)


async def album(bot: Bot, db: Database, chat_id: int, owner_id: int, owner_name: str) -> bytes:
    import cards

    members = await deck(db, chat_id)
    by_uid = {m["uid"]: m for m in members}
    best: dict[int, dict] = {}
    for uid, rarity, count, first_ts in await db.cards_of(chat_id, owner_id):
        m = by_uid.get(uid)
        if not m:  # человек выпал из колоды (молчит месяц) — карточка остаётся, характеристики по нулям
            m = {"uid": uid, "name": "Пропажа", "num": 0, "stats": [(k, 1) for k in STAT_NAMES]}
        cur = best.get(uid)
        if cur is None or RANK[rarity] > RANK[cur["rarity"]]:
            best[uid] = {"name": m["name"], "rarity": rarity, "count": (cur or {}).get("count", 0) + count,
                         "stats": m["stats"], "num": m["num"], "first": first_ts, "uid": uid}
        else:
            cur["count"] += count
    entries = sorted(best.values(), key=lambda e: (e["num"] or 999))
    photos = await asyncio.gather(*(_photo(bot, e["uid"]) for e in entries))
    for e, p in zip(entries, photos, strict=True):
        e["photo"] = p
    return await asyncio.to_thread(cards.render_album, owner_name, entries, len(members))


@router.callback_query(F.data == "cl")
async def cb_album(call: CallbackQuery, bot: Bot, db: Database) -> None:
    """Свой альбом — в личку (команды в группе только у админов, а в личке можно что угодно)."""
    if not call.message:
        await call.answer()
        return
    name = call.from_user.first_name or call.from_user.username or "Ты"
    png = await album(bot, db, call.message.chat.id, call.from_user.id, name)
    try:
        await bot.send_photo(call.from_user.id, BufferedInputFile(png, "album.png"),
                             caption=f"📒 Твоя коллекция в «{html.escape(call.message.chat.title or 'чате')}»")
        await call.answer("Альбом - у тебя в личке.")
    except Exception:
        await call.answer("Сначала напиши боту /start в личку - потом жми снова.", show_alert=True)


@router.message(Command("collection"))
async def cmd_collection(message: Message, bot: Bot, db: Database) -> None:
    """/collection — свой альбом; ответом на сообщение — альбом того человека."""
    rt = message.reply_to_message
    who = rt.from_user if rt and rt.from_user and not rt.from_user.is_bot else message.from_user
    if not who:
        return
    png = await album(bot, db, message.chat.id, who.id, who.first_name or who.username or "Аноним")
    await bot.send_photo(message.chat.id, BufferedInputFile(png, "album.png"),
                         caption=f"📒 Коллекция: {html.escape(who.first_name or who.username or 'Аноним')}")


async def tick(bot: Bot, db: Database) -> None:
    """Из фона раз в 30 сек: в каждом живом чате раз в день — карточка в случайный момент окна."""
    if (await db.get_setting(ON_KEY, "1")) != "1":
        return
    from games import active

    now = datetime.now(MSK)
    today = now.strftime("%Y-%m-%d")
    for chat_id in await db.active_chats(int(time.time()) - 86400):
        if chat_id > 0:
            continue
        key = DAY_KEY + str(chat_id)
        day, planned, done = (await db.get_setting(key, "") + "||").split("|")[:3]
        if day != today:  # новый день — назначить время выпадения
            start = max(now, now.replace(hour=DROP_FROM, minute=0, second=0, microsecond=0))
            end = now.replace(hour=DROP_TO, minute=0, second=0, microsecond=0)
            if start >= end:
                await db.set_setting(key, f"{today}|0|done")  # сегодня уже поздно
                continue
            at = start.timestamp() + random.random() * (end.timestamp() - start.timestamp())
            await db.set_setting(key, f"{today}|{int(at)}|")
            continue
        if done or not planned or time.time() < float(planned) or active(chat_id):
            continue
        await db.set_setting(key, f"{today}|{planned}|done")
        try:
            await drop(bot, db, chat_id)
        except Exception as e:
            logger.warning("card drop failed in %s: %s", chat_id, e)
