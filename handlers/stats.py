"""/stats — статистика чата без ИИ: болтуны, матершинники, нытики, совы, молчуны.

/stats — весь чат за 30 дней (столько бот хранит переписку);
ответом на сообщение или /stats @ник — личная статистика.
"""
from __future__ import annotations

import html
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from database import Database

router = Router(name="stats")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))

MSK = timezone(timedelta(hours=3))
PERIOD_DAYS = 30
STATS_COOLDOWN = 30
HR = "━━━━━━━━━━━━━━━"
MEDALS = ["🥇", "🥈", "🥉"]
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
_last_stats: dict[int, float] = {}

# Мат — по корням, с границей слова, чтобы «хлеба», «небо», «употребляю» не считались
MAT = re.compile(
    r"\b(?:за|по|от|у|вы|на|до|при|раз|съ|о|об|про|из)?(?:ху[йеёияю]\w*|пизд\w*|[её]б(?:а|у|л|н|и|ё|ш|о|ы)\w*)"
    r"|\bбля\w*|\bсук(?:а|и|у|ой|ам|ами)\b|\bмуд(?:ак|ил|о|ач)\w*|\bпид(?:о|а)?р\w*|\bзалуп\w*|\bхерн\w*"
    r"|\bгандон\w*|\bгондон\w*|\bшлюх\w*",
    re.IGNORECASE,
)
WHINE = re.compile(
    r"\b(?:устал\w*|заебал\w*|задолбал\w*|надоел\w*|бесит|бесят|грустн\w*|тоскл\w*|скучн\w*|депрес\w*|"
    r"ненавижу|невезуч\w*|капец|не\s+хочу|не\s+могу|нет\s+сил|сил\s+нет|всё\s+плохо|все\s+плохо|"
    r"почему\s+я|за\s+что|жизнь\s+боль|хочу\s+умереть|тяжело|плохо\s+мне|мне\s+плохо)\b",
    re.IGNORECASE,
)


def _bunker_active(chat_id: int) -> bool:
    from handlers.roast import _bunker_active as active
    return active(chat_id)


def _n(x: int) -> str:
    return f"{x:,}".replace(",", " ")


def _days(sec: float) -> str:
    d = int(sec // 86400)
    if d >= 1:
        return f"{d} дн."
    h = int(sec // 3600)
    return f"{h} ч." if h >= 1 else "меньше часа"


def _collect(rows: list[tuple]) -> dict:
    """Счётчики по людям из [(user_id, name, text, ts)]."""
    s = {
        "names": {}, "msgs": Counter(), "mat": Counter(), "whine": Counter(), "night": Counter(),
        "photo": Counter(), "sticker": Counter(), "chars": Counter(), "text_msgs": Counter(), "mat_msgs": Counter(),
        "hours": Counter(), "weekdays": Counter(), "user_hours": defaultdict(Counter),
    }
    for uid, name, text, ts in rows:
        text = str(text or "")
        s["names"][uid] = name or s["names"].get(uid) or "Аноним"
        s["msgs"][uid] += 1
        dt = datetime.fromtimestamp(ts, MSK)
        s["hours"][dt.hour] += 1
        s["weekdays"][dt.weekday()] += 1
        s["user_hours"][uid][dt.hour] += 1
        if dt.hour < 6:
            s["night"][uid] += 1
        if text.startswith(("[фото", "[альбом", "[кружок", "[видео")):
            s["photo"][uid] += 1
            continue
        if text.startswith("[стикер") or text.startswith("[гифка"):
            s["sticker"][uid] += 1
            continue
        if text.startswith("[переслано"):
            continue
        s["text_msgs"][uid] += 1
        s["chars"][uid] += len(text)
        mats = len(MAT.findall(text))
        s["mat"][uid] += mats
        s["mat_msgs"][uid] += 1 if mats else 0
        if WHINE.search(text):
            s["whine"][uid] += 1
    return s


def _top(counter: Counter, names: dict, unit: str = "", limit: int = 3, extra=None) -> list[str]:
    lines = []
    for i, (uid, val) in enumerate(c for c in counter.most_common() if c[1] > 0):
        if i >= limit:
            break
        tail = f" · {extra(uid, val)}" if extra else ""
        lines.append(f"{MEDALS[i] if i < 3 else f'{i + 1}.'} <b>{html.escape(names.get(uid, '?'))}</b> — "
                     f"{_n(val)}{unit}{tail}")
    return lines


async def _chat_stats(message: Message, db: Database) -> None:
    chat_id = message.chat.id
    now = time.time()
    since = int(now) - PERIOD_DAYS * 86400
    rows = await db.stats_messages(chat_id, since)
    if len(rows) < 20:
        await message.reply("📊 Маловато данных — пообщайтесь ещё, потом посчитаю.")
        return
    s = _collect(rows)
    names = s["names"]
    oldest = await db.oldest_message_ts(chat_id) or since
    days = max(1, min(PERIOD_DAYS, int((now - max(oldest, since)) // 86400) + 1))
    peak_h = s["hours"].most_common(1)[0][0]
    peak_d = s["weekdays"].most_common(1)[0][0]

    parts = [f"📊 <b>СТАТИСТИКА ЧАТА</b> · {days} дн.", HR, "",
             f"💬 Сообщений: <b>{_n(len(rows))}</b> · участников: <b>{len(s['msgs'])}</b>",
             f"⏰ Пик: <b>{peak_h:02d}:00–{(peak_h + 1) % 24:02d}:00</b> · самый болтливый день: <b>{WEEKDAYS[peak_d]}</b>"]

    def section(title: str, lines: list[str]) -> None:
        if lines:
            parts.extend(["", f"<b>{title}</b>", *lines])

    section("🗣 БОЛТУНЫ", _top(s["msgs"], names, limit=5))
    section("🤬 МАТЕРШИННИКИ", _top(
        s["mat"], names, " слов",
        extra=lambda u, v: f"мат в {round(100 * s['mat_msgs'][u] / max(1, s['text_msgs'][u]))}% сообщений"))
    section("😭 НЫТИКИ", _top(s["whine"], names, " раз"))
    section("🦉 НОЧНЫЕ СОВЫ (00–06)", _top(s["night"], names))
    writers = Counter({u: s["chars"][u] // s["text_msgs"][u] for u in s["text_msgs"] if s["text_msgs"][u] >= 10})
    section("📜 ПИСАТЕЛИ (средняя длина)", _top(writers, names, " симв.", limit=1))
    media = []
    if s["photo"]:
        uid, n = s["photo"].most_common(1)[0]
        media.append(f"📸 Фотограф: <b>{html.escape(names[uid])}</b> — {_n(n)}")
    if s["sticker"]:
        uid, n = s["sticker"].most_common(1)[0]
        media.append(f"🃏 Стикерщик: <b>{html.escape(names[uid])}</b> — {_n(n)}")
    section("🖼 МЕДИА", media)

    seen = sorted(await db.last_seen(chat_id), key=lambda r: r[2])
    silent = [f"🤫 <b>{html.escape(name or '?')}</b> — молчит {_days(now - ts)}"
              for _uid, name, ts in seen if now - ts >= 86400][:3]
    section("🤐 МОЛЧУНЫ", silent)
    parts += ["", HR, "<i>Личная статистика: ответь /stats на сообщение или /stats @ник</i>"]
    await message.answer("\n".join(parts))


async def _user_stats(message: Message, db: Database, user_id: int, name: str) -> None:
    chat_id = message.chat.id
    now = time.time()
    rows = await db.stats_messages(chat_id, int(now) - PERIOD_DAYS * 86400)
    s = _collect(rows)
    mine = s["msgs"][user_id]
    if not mine:
        await message.reply(f"Про {html.escape(name)} за {PERIOD_DAYS} дней ничего нет — молчун или новенький.")
        return
    rank = 1 + sum(1 for v in s["msgs"].values() if v > mine)
    mat_rank = 1 + sum(1 for v in s["mat"].values() if v > s["mat"][user_id])
    hour = s["user_hours"][user_id].most_common(1)[0][0]
    texts = s["text_msgs"][user_id]
    avg = s["chars"][user_id] // texts if texts else 0
    quotes = (await db.quote_counts(chat_id)).get(user_id, 0)
    clowns = next((c for uid, c in await db.clown_counts(chat_id) if uid == user_id), 0)
    bunker = await db.bunker_stats_of(user_id)
    last = next((ts for uid, _n2, ts in await db.last_seen(chat_id) if uid == user_id), None)

    parts = [f"👤 <b>{html.escape(name)}</b> · {PERIOD_DAYS} дн.", HR, "",
             f"💬 Сообщений: <b>{_n(mine)}</b> · {rank}-е место из {len(s['msgs'])}",
             f"🤬 Мата: <b>{_n(s['mat'][user_id])}</b> слов"
             + (f" · {mat_rank}-е место, в {round(100 * s['mat_msgs'][user_id] / max(1, texts))}% сообщений"
                if s["mat"][user_id] else " · святой"),
             f"😭 Нытья: <b>{_n(s['whine'][user_id])}</b> раз",
             f"🦉 Ночью (00–06): <b>{_n(s['night'][user_id])}</b>",
             f"⏰ Чаще всего пишет: <b>{hour:02d}:00–{(hour + 1) % 24:02d}:00</b>",
             f"📏 Средняя длина: <b>{avg}</b> симв.",
             f"📸 Фото: <b>{s['photo'][user_id]}</b> · 🃏 стикеров: <b>{s['sticker'][user_id]}</b>",
             "",
             f"📜 В золотом фонде: <b>{quotes}</b> · 🤡 клоун дня: <b>{clowns}</b> раз"]
    if bunker:
        games, survived, first_out = bunker
        parts.append(f"🏚 Бункер: выжил(а) <b>{survived}</b> из {games}"
                     + (f" · вылетал(а) первым(ой): {first_out}" if first_out else ""))
    if last:
        parts.append(f"🕒 Последнее сообщение: {_days(now - last)} назад")
    await message.answer("\n".join(parts))


@router.message(Command("stats"))
async def cmd_stats(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    chat_id = message.chat.id
    if _bunker_active(chat_id):
        await message.reply("Идёт игра — статистика после партии 📊")
        return
    now = time.time()
    rt = message.reply_to_message
    arg = (command.args or "").strip()
    if rt and rt.from_user and not rt.from_user.is_bot:
        u = rt.from_user
        await _user_stats(message, db, u.id, u.first_name or u.username or "Аноним")
        return
    if arg.startswith("@"):
        found = await db.find_user(chat_id, arg.split()[0])
        if not found:
            await message.reply("Не знаю такого. Пусть сначала что-нибудь напишет в чат.")
            return
        await _user_stats(message, db, found[0], found[1] or arg)
        return
    if now - _last_stats.get(chat_id, 0) < STATS_COOLDOWN:
        await message.reply("Только что считал — листни выше 👆")
        return
    _last_stats[chat_id] = now
    await _chat_stats(message, db)
