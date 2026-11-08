"""Прокси для всего бота (владелец 08.11: «хочу, чтобы бот работал из другой страны»).

Задаётся командой /setproxy в личку боту (хранится в базе, настройка `bot_proxy` — не в репозитории) или в .env
(`BOT_PROXY`, плюс `BOT_PROXY_ALL=1` — на весь бот). При каждом запуске бот сначала проверяет прокси (страна через
ipinfo и Telegram API через этот прокси): не отвечает — работает напрямую и пишет об этом админу, чтобы бот не
пропал со связи из-за умершего прокси.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("proxy")
SETTING = "bot_proxy"
ANNOUNCE = "bot_proxy_announce"   # после /setproxy: при следующем запуске написать админу, откуда бот теперь ходит
CHECK_TIMEOUT = 20


def mask(proxy: str | None) -> str:
    """Адрес прокси без пароля — для сообщений и логов."""
    return re.sub(r"//([^:/@]+):[^@]*@", r"//\1:•••@", proxy or "") or "нет"


def valid(proxy: str) -> bool:
    return bool(re.fullmatch(r"https?://[^\s/]+:\d{2,5}/?", proxy.strip()))


async def check(proxy: str, token: str) -> tuple[bool, str]:
    """(работает ли, «страна, город (ip)» или ошибка) — через прокси открываем ipinfo и Telegram API."""
    import aiohttp

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=CHECK_TIMEOUT)) as s:
            async with s.get("https://ipinfo.io/json", proxy=proxy) as r:
                info = await r.json(content_type=None)
            async with s.get(f"https://api.telegram.org/bot{token}/getMe", proxy=proxy) as r:
                if r.status != 200:
                    return False, f"Telegram через прокси ответил {r.status}"
    except Exception as e:
        text = str(e) or type(e).__name__
        return False, text.replace(proxy, mask(proxy))[:160]
    return True, f"{info.get('country', '?')}, {info.get('city', '?')} ({info.get('ip', '?')}, {info.get('org', '?')})"
