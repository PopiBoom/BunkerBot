"""HTML-виджеты в сообщениях (HTMLBubbles, Telegram 13.2) — проверка, можно ли их слать уже сейчас.

09.11 владелец: «изучи HTMLBubbles, которые вышли в 13.2» и «а сделать, чтобы всё работало именно сейчас, нельзя?».
Что известно (github.com/TelegramMessenger/widget-sandbox, docs/tgw-format.md): виджет — одна HTML-страница со своими
CSS и JS (≤ 32 КБ, ≤ 4 КБ сжатого zlib), без сети; бот шлёт его методом `sendWidget` (Bot API 10.4), меняет состояние
`editMessageWidgetData` (`chat_data`/`public_data` ≤ 1 КБ), отвечает на нажатия `answerWidgetQuery`. Можно в личку и в
группы, в каналы — пока нельзя. Bot API 10.4 ещё не вышел (на 09.11 — 10.3): Telegram проверял его на беta-сервере
`https://api.telegram.org/beta` в тестовом окружении. Работает ли бета с обычным (не тестовым) ботом — в документации
не сказано, поэтому /widget просто пробует: сначала обычный Bot API, потом бету, и пишет, что ответил Telegram.
Заработало — дальше виджеты (шахматы и т.п.) можно слать уже сейчас; нет — ждать 10.4.
"""
from __future__ import annotations

import html
import logging
import zlib

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from ephemeral import temp_reply

logger = logging.getLogger("widgets")
router = Router(name="widgets")

SERVERS = (("обычный Bot API", "https://api.telegram.org"), ("бета Bot API 10.4", "https://api.telegram.org/beta"))
MAX_HTML = 32768      # байт без сжатия
MAX_ZLIB = 4096       # байт сжатого (сервер считает как PHP gzcompress($html, 9))

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


@router.message(Command("widget"))
async def cmd_widget(message: Message, bot: Bot) -> None:
    """/widget — пробный HTML-виджет сюда: проверка, шлёт ли Telegram виджеты от бота уже сейчас."""
    raw, packed = sizes(TEST_HTML)
    if raw > MAX_HTML or packed > MAX_ZLIB:
        await temp_reply(message, f"Пробный виджет великоват: {raw} Б / {packed} Б сжатого.")
        return
    label, data = await send_widget(bot, message.chat.id, TEST_HTML)
    if data.get("ok"):
        text = (f"🫧 Виджет ушёл через {label} ({raw} Б, сжатого {packed} Б). Открылся счётчик с кнопкой «Тыкни» — "
                "значит, можно делать шахматы и остальное виджетами уже сейчас. Видишь файл или пусто — обнови Telegram "
                "до 13.2.")
    else:
        why = html.escape(str(data.get("description") or "без объяснений"))
        text = (f"🫧 Telegram пока не даёт боту слать виджеты: {why} (пробовал обычный Bot API и бету 10.4). "
                "Ждём Bot API 10.4 — как выйдет, /widget покажет.")
    try:
        await bot.send_message(message.from_user.id if message.from_user else message.chat.id, text)
    except Exception:
        await temp_reply(message, text)
