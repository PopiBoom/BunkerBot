"""HTML-виджеты в сообщениях (HTMLBubbles, Telegram 13.2) — проверка, можно ли их слать уже сейчас.

09.11 владелец: «изучи HTMLBubbles, которые вышли в 13.2» и «а сделать, чтобы всё работало именно сейчас, нельзя?».
Что известно (github.com/TelegramMessenger/widget-sandbox, docs/tgw-format.md): виджет — одна HTML-страница со своими
CSS и JS (≤ 32 КБ, ≤ 4 КБ сжатого zlib), без сети; бот шлёт его методом `sendWidget` (Bot API 10.4), меняет состояние
`editMessageWidgetData` (`chat_data`/`public_data` ≤ 1 КБ), отвечает на нажатия `answerWidgetQuery`. Можно в личку и в
группы, в каналы — пока нельзя. Bot API 10.4 ещё не вышел (на 09.11 — 10.3): Telegram проверял его на беta-сервере
`https://api.telegram.org/beta` в тестовом окружении. Работает ли бета с обычным (не тестовым) ботом — в документации
не сказано, поэтому /widget просто пробует: сначала обычный Bot API, потом бету, и пишет, что ответил Telegram.
Заработало — дальше виджеты (шахматы и т.п.) можно слать уже сейчас; нет — ждать 10.4.

Первая живая проверка 09.11 (в группе): обычный Bot API — метода нет, бета — `INPUT_MEDIA_WIDGET_NOT_ALLOWED`. Значит,
бета с обычным ботом РАБОТАЕТ (запрос дошёл до серверов Telegram), а отказ — по типу чата: какие чаты разрешены, решает
настройка сервера (tgw-format.md, «Who may send»). Поэтому /widget в группе пробует и группу, и личку того, кто вызвал, и
пишет ответ по каждому; а `probe_loop` раз в день тихо пробует личку владельца — пустили, и виджет придёт сам вместе с
сообщением «виджеты заработали» (настройка `widgets_ok`), не пустили — ничего никуда не шлёт.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import zlib
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from database import Database
from ephemeral import temp_reply

logger = logging.getLogger("widgets")
router = Router(name="widgets")

SERVERS = (("обычный Bot API", "https://api.telegram.org"), ("бета Bot API 10.4", "https://api.telegram.org/beta"))
MAX_HTML = 32768      # байт без сжатия
MAX_ZLIB = 4096       # байт сжатого (сервер считает как PHP gzcompress($html, 9))
OK_KEY = "widgets_ok"          # куда уже пустили: «private», «group» (пусто — никуда)
PROBE_KEY = "widgets_probe"    # день последней тихой проверки
PROBE_EVERY = 6 * 3600         # как часто фон смотрит, не пора ли проверить
MSK = timezone(timedelta(hours=3))

# Пробный виджет: счётчик нажатий (хранится на устройстве, у каждого сообщения свой), цвета темы Telegram, отдача
# вибрацией. Без запросов к боту — их обычный Bot API всё равно не доставит, пока нет 10.4.
TEST_HTML = """<style>
body{margin:0;padding:14px 16px 16px;font:15px system-ui;color:var(--tg-theme-text-color)}
h1{margin:0 0 4px;font-size:18px}
p{margin:0;color:var(--tg-theme-hint-color)}
#n{margin:10px 0;font-size:44px;font-weight:700;text-align:center;font-variant-numeric:tabular-nums}
#n.b{animation:b .3s}
@keyframes b{50%{transform:scale(1.25)}}
button{display:block;width:100%;padding:11px;border:0;border-radius:12px;font:600 15px system-ui;
background:var(--tg-theme-button-color);color:var(--tg-theme-button-text-color)}
</style>
<h1>Пиздюк в пузыре</h1>
<p>Видишь это — виджеты у бота работают.</p>
<div id="n">0</div>
<button id="t">Тыкни</button>
<script>
const T=window.TelegramWidget||0,e=document.getElementById('n');
let n=0;try{n=+(localStorage.getItem('n')||0)}catch(_){}
e.textContent=n;
document.getElementById('t').addEventListener('click',()=>{
n++;try{localStorage.setItem('n',n)}catch(_){}
e.textContent=n;e.className='';void e.offsetWidth;e.className='b';
try{T.HapticFeedback.impactOccurred('light')}catch(_){}});
T.ready&&T.ready();
</script>"""


def sizes(html: str) -> tuple[int, int]:
    """(байт без сжатия, байт сжатого) — как считает сервер Telegram."""
    raw = html.encode("utf-8")
    return len(raw), len(zlib.compress(raw, 9))


def _clean(text: str, token: str) -> str:
    return str(text).replace(token, "<токен>")[:300]


async def send_widget(bot: Bot, chat_id: int, html: str, **fields) -> tuple[str, dict]:
    """Шлёт виджет: сначала обычный Bot API, потом бета. ("сервер", ответ Telegram) — у удачного ответ с ok=True."""
    import aiohttp

    body = {"chat_id": chat_id, "html": html, **fields}
    last: tuple[str, dict] = ("", {})
    async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=30)) as s:
        for label, base in SERVERS:
            try:
                async with s.post(f"{base}/bot{bot.token}/sendWidget", json=body) as r:
                    data = await r.json(content_type=None)
            except Exception as e:  # в тексте ошибки бывает адрес с токеном
                data = {"ok": False, "description": _clean(f"{type(e).__name__}: {e}", bot.token)}
            if not isinstance(data, dict):
                data = {"ok": False, "description": "непонятный ответ"}
            logger.info("sendWidget через %s: ok=%s %s", label, data.get("ok"), data.get("description", ""))
            last = (label, data)
            if data.get("ok"):
                return last
    return last


def _owner() -> int:
    raw = os.getenv("BOT_FATHER_ID", "") or os.getenv("ADMIN_IDS", "").replace(";", ",").split(",")[0]
    try:
        return int(raw.strip())
    except ValueError:
        return 0


def _why(data: dict) -> str:
    return html.escape(str(data.get("description") or "без объяснений"))


async def _mark_ok(db: Database, where: str) -> None:
    have = set(filter(None, (await db.get_setting(OK_KEY, "") or "").split(",")))
    await db.set_setting(OK_KEY, ",".join(sorted(have | {where})))


@router.message(Command("widget"))
async def cmd_widget(message: Message, bot: Bot, db: Database) -> None:
    """/widget — пробный HTML-виджет: в этот чат, а из группы — ещё и в личку тому, кто вызвал. Ответ Telegram по
    каждому — ему в личку."""
    raw, packed = sizes(TEST_HTML)
    if raw > MAX_HTML or packed > MAX_ZLIB:
        await temp_reply(message, f"Пробный виджет великоват: {raw} Б / {packed} Б сжатого.")
        return
    private = message.chat.type == "private"
    targets = [("в личку" if private else "в группу", message.chat.id, "private" if private else "group")]
    if not private and message.from_user:
        targets.append(("в личку", message.from_user.id, "private"))
    lines, worked = [], []
    for where, chat_id, kind in targets:
        label, data = await send_widget(bot, chat_id, TEST_HTML)
        if data.get("ok"):
            worked.append(where)
            await _mark_ok(db, kind)
            lines.append(f"✅ {where} - ушёл ({label})")
        else:
            lines.append(f"❌ {where} - {_why(data)}")
    if worked:
        head = (f"🫧 Виджеты работают ({', '.join(worked)})! Счётчик «Тыкни» пришёл - можно делать шахматы и остальное "
                "виджетами. Видишь файл или пусто - обнови Telegram до 13.2.")
    elif all("NOT_ALLOWED" in ln for ln in lines):
        head = ("🫧 Бета Bot API 10.4 с нашим ботом работает, но Telegram пока не пускает ботов с виджетами в эти чаты "
                "(INPUT_MEDIA_WIDGET_NOT_ALLOWED - это настройка на их стороне). Раз в день проверяю сам: как пустят - "
                "пришлю виджет в личку.")
    else:
        head = "🫧 Telegram пока не даёт боту слать виджеты. Раз в день проверяю сам: как пустят - пришлю виджет в личку."
    text = head + "\n\n" + "\n".join(lines)
    try:
        await bot.send_message(message.from_user.id if message.from_user else message.chat.id, text)
    except Exception:
        await temp_reply(message, text)


async def probe_once(bot: Bot, db: Database) -> bool:
    """Тихая проверка: пробный виджет владельцу в личку. True — пустили (виджет у него, плюс сообщение)."""
    owner = _owner()
    if not owner:
        return False
    label, data = await send_widget(bot, owner, TEST_HTML)
    if not data.get("ok"):
        logger.info("виджеты пока нельзя: %s", data.get("description", ""))
        return False
    await _mark_ok(db, "private")
    try:
        await bot.send_message(owner, f"🫧 Виджеты заработали ({label})! Выше - пробный счётчик «Тыкни». Теперь можно "
                                      "делать шахматы и остальное виджетами - напиши мне в сессию.")
    except Exception as e:
        logger.info("widgets: не смог написать владельцу: %s", e)
    return True


async def probe_loop(bot: Bot, db: Database) -> None:
    """Из bot.py: раз в день (днём, 12-20 МСК), пока виджеты в личку не пустили, — `probe_once`."""
    await asyncio.sleep(120)  # дать боту запуститься
    while True:
        try:
            now = datetime.now(MSK)
            today = now.strftime("%Y-%m-%d")
            done = "private" in (await db.get_setting(OK_KEY, "") or "")
            if not done and 12 <= now.hour < 20 and (await db.get_setting(PROBE_KEY, "")) != today:
                await db.set_setting(PROBE_KEY, today)
                await probe_once(bot, db)
        except Exception as e:
            logger.warning("widgets probe: %s", e)
        await asyncio.sleep(PROBE_EVERY)


@router.message(Command("beta"))
async def cmd_beta(message: Message, bot: Bot, db: Database) -> None:
    """/beta — через какой Bot API работает бот; /beta on / off — перейти на бету или обратно. С 09.11 (вышел Bot API
    10.4) бета не нужна: виджеты есть и на обычном сервере; /beta on — запасной путь, если там что-то сломается.
    Переход — перезапуском (не посреди игр); страховка — в tgbeta.py."""
    import tgbeta
    from handlers.admin import _restart_when_free

    if message.from_user is None:
        return
    arg = (message.text or "").split(maxsplit=1)[1:]
    arg = arg[0].strip().lower() if arg else ""
    on = tgbeta.active()
    if arg in ("on", "вкл", "да"):
        if on:
            await temp_reply(message, "🧪 Уже на бете Bot API 10.4 - виджеты работают.")
            return
        await tgbeta.enable(db, force=True)
        await temp_reply(message, "🧪 Перехожу на бету Bot API и перезапускаюсь (если идёт игра - после неё). "
                                  "Сам с неё не уйду - обратно /beta off. Не ответит бета - останусь на обычном и напишу.")
        _restart_when_free()
        return
    if arg in ("off", "выкл", "нет"):
        await tgbeta.disable(db)
        if not on:
            await temp_reply(message, "🧪 Бета и так выключена - работаю через обычный Bot API.")
            return
        await temp_reply(message, "🧪 Возвращаюсь на обычный Bot API и перезапускаюсь." + (
            "" if tgbeta.prod_widgets() else " Обычный сервер виджетов пока не знает - шахматы и дурак виджетом "
                                             "заработают, когда сам включу бету (/chess_widget, /durak)."))
        _restart_when_free()
        return
    now = ("<b>бета Bot API</b> - виджеты (шахматы, дурак в личке) работают." if on else
           "<b>обычный Bot API 10.4</b> - виджеты (шахматы, дурак в личке) работают." if tgbeta.prod_widgets() else
           "<b>обычный Bot API</b> - виджетов на нём пока нет (или не смог проверить).")
    await temp_reply(message, "🧪 Сейчас: " + now + "\n\n/beta on - на бету (перезапуск)\n/beta off - обратно")
