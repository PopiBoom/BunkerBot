"""«Ночной эфир» для канала «Memento Mori» (идея и пример — с владельцем, 07.11): голосовое на минуту «как по радио».

Текст: первая строка — код (время словами, погода в Волгограде с open-meteo), тело — ИИ (ai.night_monologue,
Opus, тема из TOPICS, без шуток и без имён — в эфир не идёт ничего из чата), концовка — код («До рассвета — …»,
рассвет с open-meteo, без сети — по формуле). Голос — ai.speak с NIGHT_SPEAK (спокойный ведущий), по абзацу;
звук — radio.py (полоса приёмника, эфир, ветер или дождь по погоде, позывной) → OGG/Opus.
Публикует не сам: черновик — голосовым в личку админу с кнопками «В канал / Переписать / Выкинуть».
/night — новый черновик (≈$0.02). Первый эфир бот прислал владельцу сам после обновления (night_first_sent).
/horror — «страшная история» (просьба владельца: «очень жуткое про Волгоград, можно вымышленное»; потом: «сделай
ещё страшнее… как ведущий на радио, который объявляет историю, присланную на почту»): ведущий — «на почту эфира
пришло письмо, тема: «…», отправлено в три семнадцать ночи, читаю без изменений», дальше письмо слушателя
(ai.horror_story: каждый раз новый приём из HORROR_ENGINES, эскалация в три ступени, удар по самому близкому, без
клише и бытовой завязки; война/мемориалы/теракты — нельзя), приписка ведущего (что случилось, когда редакция
ответила автору), в самых жутких местах — сбой эфира (radio glitches), под голосом гул и далёкий стук, 3-5 мин. Голос — ai.narrate: ElevenLabs по ключу
(/setkey eleven …; на сервере закрыт по региону), Яндекс SpeechKit (/setkey yandex …), без них — gpt-audio
(mini звучал «по-ИИшному»).
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import math
import os
import random
import time
from datetime import date, datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import ai
from config import Config
from database import Database

logger = logging.getLogger("night")
router = Router(name="night")

MSK = timezone(timedelta(hours=3))
LAT, LON = 48.708, 44.513                     # Волгоград
DRAFT_KEY = "night_draft"
COUNT_KEY = "night_no"
HORROR_COUNT_KEY = "horror_no"   # у рубрики страшных историй — свой счёт выпусков
FIRST_KEY = "night_first_sent"
HORROR_FIRST_KEY = "horror_first_sent"
HORROR_KEY = "horror_titles"
TOPICS_KEY = "night_topics"
TOPICS = [
    "обычные дни, которые не запоминаются", "люди за соседними окнами", "дорога домой ночью",
    "последний раз, о котором не знаешь, что он последний", "то, что мы откладываем на потом", "тишина",
    "старые фотографии в телефоне", "голоса, которые помнишь", "город, который не спит", "первый холод",
    "то, что оставляем на завтра", "письма, которые не отправили", "свет в чужих окнах",
    "песня, которая напоминает о ком-то", "люди, которые были рядом в какой-то год", "что мы любили в детстве",
    "привычки как след, который мы оставляем", "ожидание", "река, которая течёт, пока мы спим",
    "звёзды — свет, который шёл к нам тысячи лет", "остановка, на которой ты стоял сотни раз",
    "разговоры, которые не случились", "утро, которое обязательно будет",
]
_lock = asyncio.Lock()

# ---------- числа словами (для голоса: «два часа четырнадцать минут») ----------

_U_M = ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять", "десять",
        "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать", "семнадцать",
        "восемнадцать", "девятнадцать"]
_TENS = {2: "двадцать", 3: "тридцать", 4: "сорок", 5: "пятьдесят"}


def _num(n: int, fem: bool = False) -> str:
    """0–59 словами; fem — «одна/две» (минуты)."""
    n = abs(n)
    tens, units = (_TENS[n // 10], n % 10) if n >= 20 else ("", n)
    if tens and not units:
        return tens
    word = _U_M[units]
    if fem and units in (1, 2):
        word = {1: "одна", 2: "две"}[units]
    return f"{tens} {word}".strip()


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19:
        return many
    return one if n % 10 == 1 else few if n % 10 in (2, 3, 4) else many


def _clock(dt: datetime) -> str:
    h, m = dt.hour, dt.minute
    head = f"{_num(h)} {_plural(h, 'час', 'часа', 'часов')}"
    return (head + " ровно") if m == 0 else f"{head} {_num(m, fem=True)} {_plural(m, 'минута', 'минуты', 'минут')}"


def _span(sec: float) -> str:
    mins = max(1, round(sec / 60))
    h, m = divmod(mins, 60)
    parts = []
    if h:
        parts.append(f"{_num(h)} {_plural(h, 'час', 'часа', 'часов')}")
    if m:
        parts.append(f"{_num(m, fem=True)} {_plural(m, 'минута', 'минуты', 'минут')}")
    return " ".join(parts)


# ---------- погода и солнце ----------


async def _weather() -> dict | None:
    """Сейчас в Волгограде и восход/закат на 2 дня (open-meteo, без ключа)."""
    import aiohttp

    url = ("https://api.open-meteo.com/v1/forecast?latitude={}&longitude={}&current=temperature_2m,wind_speed_10m,"
           "wind_direction_10m,precipitation,weather_code&daily=sunrise,sunset&timezone=Europe%2FMoscow"
           "&wind_speed_unit=ms&forecast_days=2").format(LAT, LON)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10), trust_env=True) as s:
            async with s.get(url) as r:
                data = await r.json(content_type=None)
        return data if data.get("current") else None
    except Exception as e:
        logger.info("night: погода не загрузилась: %s", e)
        return None


def _sun(day: date, rise: bool) -> datetime:
    """Восход/закат по формуле (если погода не загрузилась), МСК, точность ±2 мин."""
    jd = day.toordinal() + 1721424.5  # полночь этой даты (с полуднем ceil уводил на сутки вперёд)
    n = math.ceil(jd - 2451545.0 + 0.0008)
    j_star = n - LON / 360
    m = (357.5291 + 0.98560028 * j_star) % 360
    mr = math.radians(m)
    c = 1.9148 * math.sin(mr) + 0.02 * math.sin(2 * mr) + 0.0003 * math.sin(3 * mr)
    lam = math.radians((m + c + 180 + 102.9372) % 360)
    j_transit = 2451545.0 + j_star + 0.0053 * math.sin(mr) - 0.0069 * math.sin(2 * lam)
    dec = math.asin(math.sin(lam) * math.sin(math.radians(23.4397)))
    phi = math.radians(LAT)
    cos_w = (math.sin(math.radians(-0.833)) - math.sin(phi) * math.sin(dec)) / (math.cos(phi) * math.cos(dec))
    w = math.degrees(math.acos(max(-1, min(1, cos_w))))
    j = j_transit - w / 360 if rise else j_transit + w / 360
    return datetime.fromtimestamp((j - 2440587.5) * 86400, tz=timezone.utc).astimezone(MSK)


_WEATHER = [((0,), "ясно"), ((1, 2, 3), "облачно"), ((45, 48), "туман"), ((51, 53, 55, 56, 57), "морось"),
            ((61, 63, 65, 66, 67, 80, 81, 82), "дождь"), ((71, 73, 75, 77, 85, 86), "снег"), ((95, 96, 99), "гроза")]
_DIRS = ["северный", "северо-восточный", "восточный", "юго-восточный", "южный", "юго-западный", "западный",
         "северо-западный"]


def _conditions(w: dict | None, now: datetime) -> dict:
    """Строка для голоса, подпись, для ИИ — и что слышно за окном (дождь или ветер), сколько до рассвета/заката."""
    out = {"line": "", "caption": "", "facts": "", "rain": False}
    sunrises, sunsets = [], []
    if w:
        cur = w["current"]
        t = round(cur.get("temperature_2m", 0))
        wind = round(cur.get("wind_speed_10m", 0))
        wdir = cur.get("wind_direction_10m", 0) or 0
        code = int(cur.get("weather_code", 0) or 0)
        cond = next((name for codes, name in _WEATHER if code in codes), "")
        out["rain"] = cond in ("дождь", "морось", "гроза") or (cur.get("precipitation") or 0) > 0
        temp_w = "ноль" if t == 0 else ("плюс " if t > 0 else "минус ") + _num(t)
        wind_w = ("ветер с Волги" if 30 <= wdir <= 150 else f"ветер {_DIRS[int((wdir + 22.5) // 45) % 8]}")
        wind_w += f" - {_num(wind)} {_plural(wind, 'метр', 'метра', 'метров')} в секунду" if wind else ""
        out["line"] = f"В Волгограде {temp_w}{', ' + cond if cond else ''}, {wind_w}."
        out["caption"] = f"{t:+d}°{', ' + cond if cond else ''}"
        out["facts"] = f"{t:+d}°, {cond or 'без осадков'}, ветер {wind} м/с"
        for s in w.get("daily", {}).get("sunrise", []):
            sunrises.append(datetime.fromisoformat(s).replace(tzinfo=MSK))
        for s in w.get("daily", {}).get("sunset", []):
            sunsets.append(datetime.fromisoformat(s).replace(tzinfo=MSK))
    today = now.date()
    sunrises = sunrises or [_sun(today, True), _sun(today + timedelta(days=1), True)]
    sunsets = sunsets or [_sun(today, False), _sun(today + timedelta(days=1), False)]
    rise = next((s for s in sunrises if s > now), None)
    sset = next((s for s in sunsets if s > now), None)
    day_now = rise and sset and sset < rise  # закат раньше рассвета — сейчас день
    if day_now:
        out["end"] = f"До заката - {_span((sset - now).total_seconds())}."
    elif rise:
        out["end"] = f"До рассвета - {_span((rise - now).total_seconds())}."
    else:
        out["end"] = ""
    month = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь",
             "декабрь"][now.month - 1]
    out["facts"] = f"{month}, {now:%H:%M}, " + (out["facts"] or "погода неизвестна") + (
        ", день" if day_now else ", ночь")
    return out


# ---------- эфир ----------


def _caption(horror: bool, n: int, topic: str, now: datetime, weather: str) -> str:
    """Подпись к голосовому (и в канале): рубрика, номер выпуска, тема письма, время и погода."""
    when = f"{now:%H:%M}" + (f" · {html.escape(weather)}" if weather else "")
    if horror:
        subject = (topic or "без темы").strip()
        subject = subject[:1].upper() + subject[1:]
        return (f"🩸 <b>{ai.HORROR_RUBRIC.upper()}</b> · выпуск №{n}\n"
                f"✉️ <i>«{html.escape(subject)}»</i>\n\n"
                f"📻 Радио Волгограда · {when}")
    return f"🎙 <b>НОЧНОЙ ЭФИР</b> · №{n}\n\n🕯 {when}"


async def _noop(_text: str) -> None:
    return None


async def make_episode(db: Database, horror: bool = False, progress=_noop) -> dict:
    """{"ogg": bytes | None, "paragraphs": [...], "caption": str, "n": int}. Без голоса — без звука (текстом).
    progress(текст) — что сейчас делаем (правит сообщение «Пишу…», чтобы было видно, что бот жив)."""
    import radio

    await progress("🔎 Ищу городские легенды и пишу письмо…" if horror else "✍️ Пишу текст эфира…")
    now = datetime.now(MSK)
    cond = _conditions(await _weather(), now)
    clock = f"{_clock(now).capitalize()}."
    glitches: set[int] = set()
    if horror:
        try:
            used = json.loads(await db.get_setting(HORROR_KEY, "[]") or "[]")
        except ValueError:
            used = []
        day = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"][now.weekday()]
        facts = (f"сейчас {_clock(now)}, {day}; " + (cond["line"] or "погода неизвестна")
                 + ("; на улице светло" if cond["end"].startswith("До заката") else "; на улице темно"))
        story = await ai.horror_story(used[-10:], facts)
        if not story["paragraphs"]:
            raise RuntimeError("ИИ не написал историю")
        topic = story["subject"] or story["title"] or "без темы"
        # ведущий «радио Волгограда» (владелец: «как будто разговариваешь со зрителем»); ИИ промолчал — запасной
        intro = story["intro"] or (f"Хей, всем привет, ребята! Это радио Волгограда. {cond['line']} И ваша любимая "
                                   f"рубрика - «{ai.HORROR_RUBRIC}». Нам прислали кучу писем, и вот одно из них.")
        outro = story["outro"] or ("Вот такая вот история, друзья. Надеюсь, с автором всё в порядке. А вы - "
                                   "проверьте, закрыта ли у вас дверь. Всем до новых встреч, с вами было радио "
                                   "Волгограда!")
        intro, outro = " ".join(intro.split()), " ".join(outro.split())
        paragraphs = [ai.strip_tags(p) for p in [intro] + story["paragraphs"] + [outro]]  # текст черновика — без пометок
        # Озвучка крупными кусками (владелец: «однотонно и скучно» — каждый абзац отдельно звучал как диктор с
        # нуля): вступление, письмо в две части (между ними — сбой эфира), концовка.
        letter = story["paragraphs"]
        cut = (story["glitches"][0] + 1) if story["glitches"] else max(1, len(letter) // 2)
        cut = min(max(1, cut), max(1, len(letter) - 1))
        parts = [letter[:cut], letter[cut:]] if len(letter) > 1 else [letter]
        chunks = [intro] + ["\n\n".join(p) for p in parts] + [outro]
        styles = ["host"] + ["letter"] * len(parts) + ["host"]
        glitches = {1} if len(parts) > 1 else set()  # сбой — после первой части письма
        await db.set_setting(HORROR_KEY, json.dumps((used + [story["premise"][:160]])[-30:], ensure_ascii=False))
    else:
        try:
            used = json.loads(await db.get_setting(TOPICS_KEY, "[]") or "[]")
        except ValueError:
            used = []
        topic = random.choice([t for t in TOPICS if t not in used[-12:]] or TOPICS)
        body = await ai.night_monologue(topic, cond["facts"], used[-4:])
        if not body:
            raise RuntimeError("ИИ не написал эфир")
        paragraphs = [f"{clock} {cond['line']}".strip()] + body + [f"Это был ночной эфир. {cond['end']}".strip()]
        chunks, styles = paragraphs, ["night"] * len(paragraphs)
        await db.set_setting(TOPICS_KEY, json.dumps((used + [topic])[-30:], ensure_ascii=False))
    n = int(await db.get_setting(HORROR_COUNT_KEY if horror else COUNT_KEY, "0") or 0) + 1
    caption = _caption(horror, n, topic, now, cond["caption"])
    voice = []
    for i, (p, style) in enumerate(zip(chunks, styles, strict=True), 1):
        await progress(f"🎙 Озвучиваю: {i} из {len(chunks)}" + (f" · {ai.last_voice[0]}" if ai.last_voice[0] else ""))
        pcm = await ai.narrate(p, horror=horror, host=style == "host")
        if not pcm:
            return {"ogg": None, "paragraphs": paragraphs, "caption": caption, "n": n, "topic": topic}
        voice.append(pcm)
    await progress("🎚 Свожу эфир…")
    if horror:  # страшная рубрика — как настоящий FM-эфир из студии
        mix = await asyncio.to_thread(radio.studio, voice, styles, ai.SPEAK_RATE, int(time.time()), glitches)
    else:       # ночной эфир — «сквозь приёмник», как было
        mix = await asyncio.to_thread(radio.broadcast, voice, ai.SPEAK_RATE, cond["rain"], int(time.time()), False,
                                      glitches)
    ogg = await asyncio.to_thread(radio.to_ogg, mix)
    logger.info("ночной эфир №%s%s: «%s», %.0f сек, голос %s", n, " (страшная история)" if horror else "", topic,
                len(mix) / radio.SR, ai.last_voice[0] or "?")
    return {"ogg": ogg, "paragraphs": paragraphs, "caption": caption, "n": n, "topic": topic}


def _kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ В канал", callback_data="nr:pub"),
        InlineKeyboardButton(text="🔄 Переписать", callback_data="nr:redo"),
        InlineKeyboardButton(text="🗑 Выкинуть", callback_data="nr:del")]])


EPISODE_TIMEOUT = 12 * 60  # сек: дольше эфир не собираем — пишем, что не вышло (а не висим молча)
PENDING_KEY = "night_pending"   # эфир собирается: {"chat", "horror", "ts", "tries"} — если бот перезапустился
#                                 посреди сборки (обновление сервера), resume_tick соберёт заново


async def send_draft(bot: Bot, db: Database, chat_id: int, horror: bool = False, progress=_noop,
                     tries: int = 0) -> bool:
    """Собрать эфир и прислать черновик в личку на одобрение."""
    async with _lock:
        await db.set_setting(PENDING_KEY, json.dumps({"chat": chat_id, "horror": horror, "ts": time.time(),
                                                      "tries": tries}))
        try:
            return await asyncio.wait_for(_send_draft(bot, db, chat_id, horror, progress), EPISODE_TIMEOUT)
        except asyncio.TimeoutError:
            await bot.send_message(chat_id, f"⌛ Эфир не собрался за {EPISODE_TIMEOUT // 60} мин - озвучка тормозит. "
                                            "Попробуй ещё раз позже.")
            return False
        finally:
            await db.set_setting(PENDING_KEY, "")


async def _send_draft(bot: Bot, db: Database, chat_id: int, horror: bool, progress) -> bool:
    try:
        await bot.send_chat_action(chat_id, "record_voice")
    except Exception:
        pass
    ep = await make_episode(db, horror=horror, progress=progress)
    text = "\n\n".join(ep["paragraphs"])
    if not ep["ogg"]:
        await bot.send_message(chat_id, f"{ep['caption']}\n\n{html.escape(text[:3200])}\n\n<i>Озвучить не вышло: нет "
                                        "ключа (/setkey yandex или OpenRouter) или сбой озвучки. Это текст.</i>")
        return False
    msg = await bot.send_voice(chat_id, BufferedInputFile(ep["ogg"], "night.ogg"), reply_markup=_kb(),
                               caption=f"{ep['caption']}\n\n<i>Черновик. Текст:</i>\n"
                                       f"<blockquote expandable>{html.escape(text[:780])}</blockquote>")
    await db.set_setting(DRAFT_KEY, json.dumps({"file_id": msg.voice.file_id, "caption": ep["caption"],
                                                "n": ep["n"], "msg": msg.message_id, "chat": chat_id,
                                                "horror": horror}, ensure_ascii=False))
    return True


@router.message(Command("horror"))
async def cmd_horror(message: Message, bot: Bot, db: Database) -> None:
    """/horror — страшная история про Волгоград черновиком в личку."""
    await cmd_night(message, bot, db, horror=True)


@router.message(Command("night"))
async def cmd_night(message: Message, bot: Bot, db: Database, horror: bool = False) -> None:
    """/night — новый ночной эфир черновиком в личку."""
    if not ai.available():
        await message.answer("ИИ недоступен - эфир не написать.")
        return
    uid = message.from_user.id
    if _lock.locked():
        await bot.send_message(uid, "⏳ Уже собираю эфир - пришлю, как будет готов.")
        return
    first = "🕯 Пишу страшную историю - минуты три…" if horror else "🎙 Собираю ночной эфир - минута-две…"
    try:
        status = await bot.send_message(uid, first)
    except Exception:
        await message.answer("Напиши боту /start в личку - черновик эфира приходит туда.")
        return
    try:
        await send_draft(bot, db, uid, horror=horror, progress=_progress(bot, status, first))
        try:
            await status.delete()  # готово — строка «Пишу…» больше не нужна
        except Exception:
            pass
    except Exception as e:
        logger.exception("night draft failed")
        await bot.send_message(uid, f"Эфир не собрался: {html.escape(str(e))[:200]}")


@router.callback_query(F.data.startswith("nr:"))
async def cb_night(call: CallbackQuery, bot: Bot, db: Database, config: Config) -> None:
    if not config.is_admin(call.from_user.id):
        await call.answer("Это для админа.", show_alert=True)
        return
    try:
        draft = json.loads(await db.get_setting(DRAFT_KEY, "") or "{}")
    except ValueError:
        draft = {}
    action = (call.data or "")[3:]
    if not draft or draft.get("msg") != (call.message.message_id if call.message else None):
        await call.answer("Этот черновик уже неактуален.", show_alert=True)
        return

    async def mark(text: str) -> None:
        try:
            await bot.edit_message_caption(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                           caption=f"{draft['caption']}\n\n{text}", reply_markup=None)
        except TelegramBadRequest:
            pass

    if action == "pub":
        from handlers.channel_posts import channel_id, mark_own

        channel = await channel_id(bot, db)
        if not channel:
            await call.answer("Не нашёл канал.", show_alert=True)
            return
        sent = await bot.send_voice(channel, draft["file_id"], caption=draft["caption"])
        await mark_own(db, sent.message_id)
        await db.set_setting(HORROR_COUNT_KEY if draft.get("horror") else COUNT_KEY, str(draft["n"]))
        await db.set_setting(DRAFT_KEY, "")
        await call.answer("Опубликовано в канале.")
        await mark("✅ Опубликовано в канале.")
    elif action == "del":
        await db.set_setting(DRAFT_KEY, "")
        await call.answer("Выкинул.")
        await mark("🗑 Выкинуто.")
    elif action == "redo":
        await db.set_setting(DRAFT_KEY, "")
        await call.answer("Переписываю - минуты три." if draft.get("horror") else "Переписываю - минута-две.")
        await mark("🔄 Переписан - новый ниже.")
        try:
            await send_draft(bot, db, call.message.chat.id, horror=bool(draft.get("horror")))
        except Exception as e:
            logger.exception("night redo failed")
            await bot.send_message(call.message.chat.id, f"Эфир не собрался: {html.escape(str(e))[:200]}")


def _progress(bot: Bot, status: Message, first: str):
    """Правит сообщение «Пишу…»: какой шаг сейчас и сколько прошло — видно, что бот жив."""
    started = time.time()

    async def show(step: str) -> None:
        try:
            await bot.edit_message_text(f"{first}\n\n{step} · {int(time.time() - started)} с", chat_id=status.chat.id,
                                        message_id=status.message_id)
        except Exception:
            pass

    return show


async def resume_tick(bot: Bot, db: Database) -> None:
    """Из фона: бот перезапустился посреди сборки эфира (обновление сервера) — собрать заново, один раз."""
    if _lock.locked():
        return
    try:
        job = json.loads(await db.get_setting(PENDING_KEY, "") or "{}")
    except ValueError:
        job = {}
    if not job:
        return
    await db.set_setting(PENDING_KEY, "")
    if job.get("tries", 0) >= 1 or time.time() - float(job.get("ts", 0)) > 3600:
        return  # уже пробовали заново (или давно) — не крутим по кругу
    chat, horror = int(job["chat"]), bool(job.get("horror"))
    first = "🔁 Бот перезапускался посреди сборки эфира - собираю заново…"
    try:
        status = await bot.send_message(chat, first)
        await send_draft(bot, db, chat, horror=horror, progress=_progress(bot, status, first), tries=1)
        try:
            await status.delete()
        except Exception:
            pass
    except Exception:
        logger.exception("night: пересборка после перезапуска не вышла")


def busy() -> bool:
    """Собирается эфир — флаг для deploy/update.sh (не перезапускать бота посреди сборки)."""
    return _lock.locked()


async def first_tick(bot: Bot, db: Database) -> None:
    """Из фона: первый эфир — владельцу в личку сам, один раз после обновления (просьба: «закинь в личку»); первая
    страшная история — так же."""
    if (await db.get_setting(FIRST_KEY, "")) == "1":
        if (await db.get_setting(HORROR_FIRST_KEY, "")) != "1":
            await _first_horror(bot, db)
        return
    raw = os.getenv("BOT_FATHER_ID", "") or os.getenv("ADMIN_IDS", "").replace(";", ",").split(",")[0]
    try:
        owner = int(raw.strip())
    except ValueError:
        return
    await db.set_setting(FIRST_KEY, "1")  # один раз, даже если не выйдет (не долбить каждые 30 сек)
    try:
        await bot.send_message(owner, "🎙 Первый ночной эфир - черновик, в канал уйдёт только по кнопке.")
        await send_draft(bot, db, owner)
    except Exception:
        logger.exception("night: первый эфир не ушёл")


async def _first_horror(bot: Bot, db: Database) -> None:
    raw = os.getenv("BOT_FATHER_ID", "") or os.getenv("ADMIN_IDS", "").replace(";", ",").split(",")[0]
    try:
        owner = int(raw.strip())
    except ValueError:
        return
    await db.set_setting(HORROR_FIRST_KEY, "1")
    try:
        await bot.send_message(owner, "🕯 Первая страшная история для ночного эфира - черновик, в канал только по кнопке."
                               + ("" if ai.yandex_key() or ai.eleven_key() else "\nГолос пока запасной (gpt-audio). "
                                  "Живой голос - ключ Яндекс SpeechKit: /setkey yandex <ключ>."))
        await send_draft(bot, db, owner, horror=True)
    except Exception:
        logger.exception("night: первая страшная история не ушла")
