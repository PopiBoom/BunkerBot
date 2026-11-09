"""«Ночной эфир» для канала «Memento Mori» (идея и пример — с владельцем, 07.11): голосовое на минуту «как по радио».

Текст: первая строка — код (время словами, погода в Волгограде с open-meteo), тело — ИИ (ai.night_monologue,
Opus, тема из TOPICS, без шуток и без имён — в эфир не идёт ничего из чата), концовка — код («До рассвета — …»,
рассвет с open-meteo, без сети — по формуле). Голос — ai.speak с NIGHT_SPEAK (спокойный ведущий), по абзацу;
звук — radio.py (полоса приёмника, эфир, ветер или дождь по погоде, позывной) → OGG/Opus.
Публикует не сам: черновик — голосовым в личку админу с кнопками «В канал / Переписать / Выкинуть».
/night — новый черновик (≈$0.02). Первый эфир бот прислал владельцу сам после обновления (night_first_sent).
/horror — рубрика «Письма Волгограда» на «радио Волгограда» (ai.horror_story): ведущий здоровается и объявляет
рубрику → письмо слушателя (законченная страшная история от первого лица: завязка, кульминация, развязка, финальный
укол; весь выпуск 4-6 мин) → ведущий: «вот такая вот история, друзья», совет слушателю, прощание. Озвучка — ai.narrate_all ОДНИМ голосом на весь выпуск (ElevenLabs v3 →
Яндекс → gpt-audio), тремя кусками: вступление, письмо целиком, концовка; сведение — radio.studio (заставка владельца assets/radio/intro.mp3, под
письмом — мрачный фон и напряжение, треск эфира только в паузах, после последней фразы — обрыв в тишину).
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import math
import os
import random
import re
import time
from datetime import date, datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
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
HORROR_COUNT_KEY = "horror_no2"  # у рубрики страшных историй — свой счёт выпусков (08.11 владелец: «обнули
#                                   выпуски» — новый ключ, следующий выпуск — №1; старый horror_no не трогаем)
FIRST_KEY = "night_first_sent"
HORROR_FIRST_KEY = "horror_first_sent"
HORROR_KEY = "horror_titles"
MOTIFS_KEY = "horror_motifs"     # ключевые детали прошлых выпусков — не повторять (владелец: «ключи и т.д.»)
LORE_KEY = "horror_lore"         # сквозные персонажи рубрики (идея №7): [{"name", "what", "no", "where", "seen"}] —
#                                   пополняется только при публикации, чтобы «выпуск №N» в эфире был настоящим
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


def _caption(horror: bool, n: int, topic: str, now: datetime, weather: str, bot_username: str = "",
             fmt: str = "письмо", author: str | None = None) -> str:
    """Подпись к голосовому (и в канале): рубрика, номер выпуска, тема письма, автор (если историю прислал подписчик:
    имя — если разрешил «именем», иначе «попросил не называть имя»; 09.11 владелец: «указывать имя того, кто
    отправил»), время и погода; у рубрики — как прислать свою историю (ссылка открывает бота сразу на письме)."""
    when = f"{now:%H:%M}" + (f" · {html.escape(weather)}" if weather else "")
    if horror:
        from handlers.letters import deep_link

        subject = (topic or "без темы").strip()
        subject = subject[:1].upper() + subject[1:]
        link = deep_link(bot_username)
        icon, name = next(((f[2], f[1]) for f in ai.HORROR_FORMATS if f[0] == fmt), ("✉️", "письмо"))
        return (f"🩸 <b>{ai.HORROR_RUBRIC.upper()}</b> · выпуск №{n}\n"
                f"{icon} <i>«{html.escape(subject)}»</i>" + ("" if fmt == "письмо" else f" · {name}") + "\n"
                + ("" if author is None else f"👤 Автор: {html.escape(author[:30])}\n" if author else
                   "👤 Автор попросил не называть имя\n") + "\n"
                f"📻 Радио Волгограда · {when}"
                + (f"\n\n📨 Свою историю можно оставить в этом боте: <a href=\"{link}\">@{html.escape(bot_username)}</a>"
                   if link else ""))
    return f"🎙 <b>НОЧНОЙ ЭФИР</b> · №{n}\n\n🕯 {when}"


async def _noop(_text: str) -> None:
    return None


async def tg_retry(fn, *args, **kwargs):
    """Запрос к Telegram с повтором при «Flood control exceeded… Retry in N seconds»: подождать и ещё раз (до трёх
    раз). 09.11: «Выпуск не собрался: Flood control exceeded on method 'SendVoice'… Retry in 8 seconds» — собранный и
    озвученный выпуск пропадал из-за 8 секунд ожидания."""
    for attempt in range(4):
        try:
            return await fn(*args, **kwargs)
        except TelegramRetryAfter as e:
            if attempt == 3 or e.retry_after > 120:
                raise
            logger.info("telegram: флуд-контроль, жду %s с и повторяю", e.retry_after)
            await asyncio.sleep(e.retry_after + 1)


CAPTION_MAX = 1024  # лимит подписи Telegram: видимые знаки после разметки, в UTF-16


def _vis_len(text: str) -> int:
    """Длина подписи так, как её считает Telegram: без тегов, «&lt;» — один знак, эмодзи — два."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)).encode("utf-16-le")) // 2


def _cut_vis(text: str, room: int) -> str:
    """Обычный текст (не HTML) не длиннее room знаков Telegram: по слову и «…»."""
    if _vis_len(html.escape(text)) <= room:
        return text
    cut = text[:max(0, room - 1)]
    while cut and _vis_len(cut) > room - 1:
        cut = cut[:-1]
    return (cut.rsplit(" ", 1)[0] if " " in cut else cut).rstrip(",.;:-— ") + "…"


def _draft_caption(caption: str, voice: str, text: str) -> str:
    """Подпись черновика: подпись выпуска, служебная строка о голосе, начало текста — всё вместе в CAPTION_MAX.
    09.11: служебная строка выросла (манера, концовка, понятность, сквозное), и 680 знаков текста сверху дали
    «message caption is too long» — собранный выпуск пропадал. Теперь текст режется по остатку места."""
    room = CAPTION_MAX - 8 - _vis_len(caption) - len("\n\nЧерновик. Текст:\n")
    voice_room = min(300, max(0, room - min(250, _vis_len(text))))  # текст важнее: под него до 250 знаков
    who = f"<i>Голос: {html.escape(_cut_vis(voice, voice_room - 7))}</i>\n" if voice and voice_room > 40 else ""
    quote = _cut_vis(text, min(680, room - _vis_len(who)))
    return (f"{caption}\n\n{who}<i>Черновик. Текст:</i>\n"
            + (f"<blockquote expandable>{html.escape(quote)}</blockquote>" if quote.strip("…") else ""))


def _short(text: str, n: int) -> str:
    """Не длиннее n знаков и не посреди слова."""
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0].rstrip(",;:-— ")


def _lore_pick(entries: list, n_next: int, used: list[str]) -> dict | None:
    """Сквозной персонаж для нового выпуска (идея владельца №7): изредка (`ai.LORE_CHANCE`), не два выпуска подряд,
    из выпусков не новее позапрошлого и не чаще `ai.LORE_MAX` раз на одного. None — в этот раз без него."""
    if not entries or random.random() >= ai.LORE_CHANCE or any(f"| {ai.LORE_MARK} |" in a for a in used[-1:]):
        return None
    pool = [e for e in entries if isinstance(e, dict) and e.get("name") and int(e.get("no") or 0) <= n_next - 2
            and int(e.get("seen") or 1) < ai.LORE_MAX]
    if not pool:
        return None
    return dict(random.choices(pool, weights=[1 / int(e.get("seen") or 1) for e in pool])[0])


async def _lore_commit(db: Database, draft: dict) -> None:
    """Выпуск опубликован: сквозной персонаж, который в нём мелькал, — «видели ещё раз», новый — в список."""
    try:
        entries = list(json.loads(await db.get_setting(LORE_KEY, "[]") or "[]"))
    except (ValueError, TypeError):
        entries = []
    used = str(draft.get("lore_used") or "").strip().lower()
    for e in entries:
        if used and str(e.get("name", "")).strip().lower() == used:
            e["seen"] = int(e.get("seen") or 1) + 1
    new = draft.get("lore_new") or {}
    name = str(new.get("name") or "").strip()
    if name and name.lower() != used and all(str(e.get("name", "")).strip().lower() != name.lower() for e in entries):
        entries.append({"name": name, "what": new.get("what", ""), "no": new.get("no"), "where": new.get("where", ""),
                        "seen": 1})
    await db.set_setting(LORE_KEY, json.dumps(entries[-30:], ensure_ascii=False))


async def make_episode(db: Database, horror: bool = False, progress=_noop, letter: dict | None = None,
                       bot_username: str = "") -> dict:
    """{"ogg": bytes | None, "paragraphs": [...], "caption": str, "n": int}. Без голоса — без звука (текстом).
    progress(текст) — что сейчас делаем (правит сообщение «Пишу…», чтобы было видно, что бот жив). letter — письмо
    подписчика (handlers/letters.py), из которого делаем выпуск."""
    import radio
    from handlers import letters as inbox

    music_task, sfx_tasks, story = None, [], {}

    await progress("🔎 Ищу городские легенды и пишу письмо…" if horror else "✍️ Пишу текст эфира…")
    now = datetime.now(MSK)
    cond = _conditions(await _weather(), now)
    clock = f"{_clock(now).capitalize()}."
    if horror:
        try:
            used = json.loads(await db.get_setting(HORROR_KEY, "[]") or "[]")
        except ValueError:
            used = []
        day = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"][now.weekday()]
        facts = (f"сегодня {ai.date_words(now.date())}, {day}; сейчас {_clock(now)}; "
                 + (cond["line"] or "погода неизвестна")
                 + ("; на улице светло" if cond["end"].startswith("До заката") else "; на улице темно"))
        coms = await inbox.comments(db)
        try:
            motifs = list(json.loads(await db.get_setting(MOTIFS_KEY, "[]") or "[]"))
        except (ValueError, TypeError):
            motifs = []
        n_next = int(await db.get_setting(HORROR_COUNT_KEY, "0") or 0) + 1
        try:
            lore_all = list(json.loads(await db.get_setting(LORE_KEY, "[]") or "[]"))
        except (ValueError, TypeError):
            lore_all = []
        lore = None if letter else _lore_pick(lore_all, n_next, used)
        story = await ai.horror_story(used[-10:], facts, now.date(), listener=letter, comments=coms, motifs=motifs,
                                      lore=lore)
        if not story["paragraphs"]:
            raise RuntimeError("ИИ не написал историю")
        if story.get("motifs"):
            await db.set_setting(MOTIFS_KEY, json.dumps((motifs + story["motifs"])[-40:], ensure_ascii=False))
        if ai.eleven_key() and not ai.eleven_geo_blocked():  # музыка и звуки — пока озвучивается голос
            music_task = asyncio.create_task(ai.eleven_ambient(story.get("music", "")))
            sfx_tasks = [asyncio.create_task(ai.eleven_sfx(x["sound"], x["secs"])) for x in story.get("sfx") or []]
        topic = story["subject"] or story["title"] or "без темы"
        # ведущий «радио Волгограда» (владелец: «как будто разговариваешь со зрителем»); ИИ промолчал — запасной
        intro = story["intro"] or (f"Хей, всем привет, ребята! Это радио Волгограда. {cond['line']} И ваша любимая "
                                   f"рубрика - «{ai.HORROR_RUBRIC}». Нам прислали кучу писем, и вот одно из них.")
        outro = story["outro"] or ("Вот такая вот история, друзья. Надеюсь, с автором всё в порядке. А вы - "
                                   f"проверьте, закрыта ли у вас дверь. Это были «{ai.HORROR_RUBRIC}». "
                                   f"{ai.HORROR_SIGNOFF}")
        intro, outro = " ".join(intro.split()), " ".join(outro.split())
        call = story.get("call")
        letter_paras = list(story["paragraphs"])
        fmt = story.get("format", "письмо")
        if fmt == "диктофон":  # запись говорит сам автор; приписку того, кто прислал, читает ведущий
            ps = next((i for i in range(len(letter_paras) - 1, 0, -1)
                       if ai.strip_tags(letter_paras[i]).lstrip().lower().startswith("приписка")), None)
            if ps is not None:
                note = ai.strip_tags(letter_paras.pop(ps)).strip()
                note = re.sub(r"^приписка[^:.—-]*[:.—-]\s*", "", note, flags=re.IGNORECASE)
                outro = f"Запись на этом кончается. И приписка от того, кто прислал: {note} {outro}"
        body = list(letter_paras)
        if call:  # в тексте черновика звонок — после того абзаца, где он прозвучит
            key = ai.strip_tags(call["after"]).lower()[-25:]
            at = next((i for i, p in enumerate(body) if key and key in ai.strip_tags(p).lower()), len(body) // 2)
            body = (body[:at + 1] + [f"📞 {call['host_in']}", f"- {call['text']}"]
                    + (["📵 связь оборвалась - короткие гудки"] if call.get("drop") else [])
                    + [f"📞 {call['host_back']}"] + body[at + 1:])
        paragraphs = [ai.strip_tags(p) for p in [intro] + body + [outro]]  # текст черновика — без пометок
        # Озвучка: вступление, письмо ЦЕЛИКОМ, концовка; звонок посреди письма (владелец 08.11) — тремя отдельными
        # кусками (ведущий прерывается, звонящий, ведущий возвращается), radio.studio вклеивает их в паузу письма.
        # Владелец раньше: «история прерывается посередине» — письмо двумя кусками начиналось заново другим тоном,
        # поэтому само письмо — одним куском.
        chunks = [intro, "\n\n".join(letter_paras), outro]
        styles = ["host", "letter", "host"]
        author = story.get("author") or {}
        roles = ["host", ai.author_role(author.get("gender", "m"), int(author.get("age") or 30))
                 if fmt == "диктофон" else "letter", "host"]
        if call:
            chunks += [call["host_in"], call["text"], call["host_back"]]
            roles += ["host", f"call_{call['gender']}", "host"]
        # 240 знаков: пометки (манера, концовка, обрыв, лор) и приём должны влезть — по ним код не повторяется
        await db.set_setting(HORROR_KEY, json.dumps((used + [story["premise"][:240]])[-30:], ensure_ascii=False))
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
        roles = [False] * len(paragraphs)
        await db.set_setting(TOPICS_KEY, json.dumps((used + [topic])[-30:], ensure_ascii=False))
    n = int(await db.get_setting(HORROR_COUNT_KEY if horror else COUNT_KEY, "0") or 0) + 1
    caption = _caption(horror, n, topic, now, cond["caption"], bot_username, story.get("format", "письмо"),
                       author=((letter.get("sign") or "").strip() if letter else None))
    lore_new = story.get("lore")
    lore_info = {"lore_used": story.get("lore_used"),
                 "lore_new": ({**lore_new, "no": n, "where": _short(str(story.get("place", "")), 80)}
                              if lore_new else None)}
    async def step(i: int, total: int, who: str) -> None:
        await progress(f"🎙 Озвучиваю: {i} из {total} · {who}")

    # весь выпуск — одним голосом (кусок не вышел — всё заново следующим сервисом, а не «письмо дочитал другой»)
    voice = await ai.narrate_all(chunks, roles, horror=horror, progress=step)
    who = ai.last_voice[0] + (f" (не вышло: {ai.voice_trouble[0]})" if ai.voice_trouble[0] else "")
    if not voice:
        for t in [music_task] + sfx_tasks:
            if t:
                t.cancel()
        return {"ogg": None, "paragraphs": paragraphs, "caption": caption, "n": n, "topic": topic,
                "voice": ai.voice_trouble[0]}
    await progress("🎚 Свожу эфир…")
    if horror:  # страшная рубрика — как настоящий FM-эфир из студии
        events, music = [], None
        letter_text = chunks[1]
        letter_secs = len(voice[1]) / 2 / ai.SPEAK_RATE
        for x, task in zip(story.get("sfx") or [], sfx_tasks, strict=False):
            data = await task
            snd = await asyncio.to_thread(radio.decode, data) if data else None
            at = ai.sfx_time(x["after"], letter_text, letter_secs)
            if snd is not None and at is not None:
                events.append((at, snd[:int(5 * radio.SR)]))
            else:
                logger.info("horror: звук «%s» не встал (%s)", x["sound"][:40], "нет звука" if snd is None else
                            f"фразы «{x['after'][:40]}» нет в письме")
        if music_task:
            data = await music_task
            music = await asyncio.to_thread(radio.decode, data) if data else None
        interrupt = None
        call = story.get("call")
        if call and len(voice) >= 6:
            at = ai.sfx_time(call["after"], letter_text, letter_secs)
            interrupt = (at if at is not None else letter_secs * 0.55, voice[3:6])
        extras = (f"{story.get('format', 'письмо')}, тема «{story.get('kind', '')}», звуков {len(events)} из "
                  f"{len(story.get('sfx') or [])}, музыка {'ElevenLabs' if music is not None else 'своя'}, звонок "
                  + (f"посреди письма ({interrupt[0]:.0f} с из {letter_secs:.0f}"
                     + (", обрыв и гудки)" if call.get("drop") else ", без обрыва)") if interrupt else "нет")
                  + (", метки времени" if ai.letter_alignment[0] else "")
                  + (f", манера «{story['style']}»" if story.get("style") else "")
                  + (f", концовка «{story['ending']}»" if story.get("ending") else "")
                  + (f", понятность {story['check']['clear']}/5" if (story.get("check") or {}).get("clear") else "")
                  + (f", сквозное «{story['lore_used']}»" if story.get("lore_used") else ""))
        logger.info("horror: %s", extras)
        who = f"{who} · {extras}" if who else extras
        mix = await asyncio.to_thread(radio.studio, voice[:3], styles, ai.SPEAK_RATE, int(time.time()), events, music,
                                      interrupt, story.get("format") == "диктофон",
                                      call_drop=bool(interrupt and call.get("drop")))
    else:       # ночной эфир — «сквозь приёмник», как было
        mix = await asyncio.to_thread(radio.broadcast, voice, ai.SPEAK_RATE, cond["rain"], int(time.time()))
    ogg = await asyncio.to_thread(radio.to_ogg, mix)
    secs = max(1, int(round(len(mix) / radio.SR)))
    logger.info("ночной эфир №%s%s: «%s», %.0f сек, голос %s", n, " (страшная история)" if horror else "", topic,
                len(mix) / radio.SR, who or "?")
    return {"ogg": ogg, "paragraphs": paragraphs, "caption": caption, "n": n, "topic": topic, "voice": who,
            "letter": (letter or {}).get("id"), "secs": secs, **(lore_info if horror else {})}


def _kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ В канал", callback_data="nr:pub"),
        InlineKeyboardButton(text="🔄 Переписать", callback_data="nr:redo"),
        InlineKeyboardButton(text="🗑 Выкинуть", callback_data="nr:del")]])


EPISODE_TIMEOUT = 12 * 60  # сек: дольше эфир не собираем — пишем, что не вышло (а не висим молча)
PENDING_KEY = "night_pending"   # эфир собирается: {"chat", "horror", "ts", "tries"} — если бот перезапустился
#                                 посреди сборки (обновление сервера), resume_tick соберёт заново


async def send_draft(bot: Bot, db: Database, chat_id: int, horror: bool = False, progress=_noop,
                     tries: int = 0, letter_id: int | None = None) -> bool:
    """Собрать эфир и прислать черновик в личку на одобрение. letter_id — письмо подписчика, из которого выпуск."""
    async with _lock:
        await db.set_setting(PENDING_KEY, json.dumps({"chat": chat_id, "horror": horror, "ts": time.time(),
                                                      "tries": tries, "letter": letter_id}))
        try:
            return await asyncio.wait_for(_send_draft(bot, db, chat_id, horror, progress, letter_id),
                                          EPISODE_TIMEOUT)
        except asyncio.TimeoutError:
            await bot.send_message(chat_id, f"⌛ Эфир не собрался за {EPISODE_TIMEOUT // 60} мин - озвучка тормозит. "
                                            "Попробуй ещё раз позже.")
            return False
        finally:
            await db.set_setting(PENDING_KEY, "")


async def _send_draft(bot: Bot, db: Database, chat_id: int, horror: bool, progress,
                      letter_id: int | None = None) -> bool:
    from handlers import letters

    try:
        await bot.send_chat_action(chat_id, "record_voice")
    except Exception:
        pass
    letter = await letters.get(db, letter_id) if letter_id else None
    try:
        me = (await bot.get_me()).username or ""
    except Exception:
        me = ""
    ep = await make_episode(db, horror=horror, progress=progress, letter=letter, bot_username=me)
    text = "\n\n".join(ep["paragraphs"])
    if not ep["ogg"]:
        why = f" ({html.escape(ep['voice'][:300])})" if ep.get("voice") else ""
        await tg_retry(bot.send_message, chat_id, f"{ep['caption']}\n\n{html.escape(text[:3200])}\n\n<i>Озвучить не вышло: нет "
                                        f"ключа (/setkey yandex или OpenRouter) или сбой озвучки{why}. Это текст.</i>")
        return False
    # какой голос читал (и кто до него не справился) — видно только в черновике, в канал не идёт
    voice = BufferedInputFile(ep["ogg"], "night.ogg")
    try:
        msg = await tg_retry(bot.send_voice, chat_id, voice, reply_markup=_kb(), duration=ep.get("secs") or None,
                             caption=_draft_caption(ep["caption"], ep.get("voice") or "", text))
    except TelegramBadRequest as e:  # подпись всё равно не пустили — голосовое дороже подписи, шлём без текста
        if "caption" not in str(e).lower():
            raise
        logger.warning("night: подпись черновика не принята (%s) — без текста", e)
        msg = await tg_retry(bot.send_voice, chat_id, voice, reply_markup=_kb(), duration=ep.get("secs") or None,
                             caption=_cut_vis(re.sub(r"<[^>]+>", "", ep["caption"]), 900))
    await db.set_setting(DRAFT_KEY, json.dumps({"file_id": msg.voice.file_id, "caption": ep["caption"],
                                                "n": ep["n"], "msg": msg.message_id, "chat": chat_id,
                                                "horror": horror, "letter": ep.get("letter"),
                                                "secs": ep.get("secs"),
                                                "lore_used": ep.get("lore_used"), "lore_new": ep.get("lore_new")},
                                               ensure_ascii=False))
    return True


@router.message(Command("horror"))
async def cmd_horror(message: Message, bot: Bot, db: Database) -> None:
    """/horror — страшная история про Волгоград черновиком в личку. Есть взятые письма подписчиков — сначала выбор,
    из какого письма делать выпуск (или «придумать самому»)."""
    from handlers import letters

    ready = await letters.approved(db)
    if not ready:
        await cmd_night(message, bot, db, horror=True)
        return
    rows = []
    for x in ready[:8]:
        who = x.get("sign") or "аноним"
        rows.append([InlineKeyboardButton(text=f"✉️ № {x['id']} · {who}: {x['text'][:28]}…", callback_data=f"hr:{x['id']}")])
    rows.append([InlineKeyboardButton(text="🎲 Без письма - придумать самому", callback_data="hr:0")])
    try:
        await bot.send_message(message.from_user.id, "🩸 Из какого письма делать выпуск «Писем Волгограда»?",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        await message.answer("Напиши боту /start в личку - черновик эфира приходит туда.")


@router.callback_query(F.data.startswith("hr:"))
async def cb_horror_pick(call: CallbackQuery, bot: Bot, db: Database, config: Config) -> None:
    if not config.is_admin(call.from_user.id):
        await call.answer("Это для админа.", show_alert=True)
        return
    try:
        lid = int((call.data or "")[3:]) or None
    except ValueError:
        lid = None
    await call.answer()
    if call.message:
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    await _start(bot, db, call.from_user.id, True, lid)


@router.message(Command("night"))
async def cmd_night(message: Message, bot: Bot, db: Database, horror: bool = False) -> None:
    """/night — новый ночной эфир черновиком в личку."""
    if not await _start(bot, db, message.from_user.id, horror, None):
        await message.answer("Напиши боту /start в личку - черновик эфира приходит туда.")


async def _start(bot: Bot, db: Database, uid: int, horror: bool, letter_id: int | None) -> bool:
    """Собрать черновик эфира и прислать uid в личку. False — в личку не написать."""
    if not ai.available():
        try:
            await bot.send_message(uid, "ИИ недоступен - эфир не написать.")
        except Exception:
            return False
        return True
    if _lock.locked():
        try:
            await bot.send_message(uid, "⏳ Уже собираю эфир - пришлю, как будет готов.")
        except Exception:
            return False
        return True
    first = ("🕯 Пишу страшную историю" + (f" по письму № {letter_id}" if letter_id else "") + " - минуты три…"
             if horror else "🎙 Собираю ночной эфир - минута-две…")
    try:
        status = await bot.send_message(uid, first)
    except Exception:
        return False
    try:
        await send_draft(bot, db, uid, horror=horror, progress=_progress(bot, status, first), letter_id=letter_id)
        try:
            await status.delete()  # готово — строка «Пишу…» больше не нужна
        except Exception:
            pass
    except Exception as e:
        logger.exception("night draft failed")
        await bot.send_message(uid, f"Эфир не собрался: {html.escape(str(e))[:200]}")
    return True


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
        sent = await tg_retry(bot.send_voice, channel, draft["file_id"], caption=draft["caption"],
                              duration=draft.get("secs") or None)
        await mark_own(db, sent.message_id)
        await db.set_setting(HORROR_COUNT_KEY if draft.get("horror") else COUNT_KEY, str(draft["n"]))
        if draft.get("horror"):
            from handlers import letters

            await letters.on_published(db, channel, sent.message_id, draft["n"])  # копим комментарии под ним
            await _lore_commit(db, draft)  # сквозные персонажи — только из вышедших выпусков
            if draft.get("letter"):
                try:
                    ch = await bot.get_chat(channel)
                    link = (f"https://t.me/{ch.username}/{sent.message_id}" if ch.username else
                            f"https://t.me/c/{str(channel).removeprefix('-100')}/{sent.message_id}")
                except Exception:
                    link = ""
                await letters.mark_used(bot, db, int(draft["letter"]), link)
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
            await send_draft(bot, db, call.message.chat.id, horror=bool(draft.get("horror")),
                             letter_id=draft.get("letter"))
        except Exception as e:
            logger.exception("night redo failed")
            await bot.send_message(call.message.chat.id, f"Эфир не собрался: {html.escape(str(e))[:200]}")


PROGRESS_GAP = 3.0  # сек: «Пишу… / Озвучиваю N из M» правим не чаще


def _progress(bot: Bot, status: Message, first: str):
    """Правит сообщение «Пишу…»: какой шаг сейчас и сколько прошло — видно, что бот жив."""
    started, last = time.time(), [0.0]

    async def show(step: str) -> None:
        if time.time() - last[0] < PROGRESS_GAP:  # правки тоже считаются во флуд-контроле Telegram
            return
        last[0] = time.time()
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
        await send_draft(bot, db, chat, horror=horror, progress=_progress(bot, status, first), tries=1,
                         letter_id=job.get("letter"))
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
