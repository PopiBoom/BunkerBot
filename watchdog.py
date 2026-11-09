"""Сторож (09.11, владелец): отвалились Claude API, ElevenLabs или прокси — бот сам пишет владельцу в личку,
один раз на сбой, и потом — «снова работает».

Claude и ElevenLabs отмечают каждый запрос: `ok(сервис)` / `fail(сервис, причина, hard=…)` (из ai.py). Сбой —
FAILS ошибок связи/сервера подряд, а «жёсткая» ошибка (ключ не принят, кончились деньги/символы, регион
закрыт) — сразу. Ошибки по содержанию (отказ модели, плохой запрос) — не сбой сервиса, их не считаем.
Прокси проверяет `loop()` раз в PROXY_EVERY: тот же netproxy.check, что при запуске (ipinfo + Telegram через
прокси); PROXY_FAILS неудач подряд — сбой. Если прокси умер, Telegram через него тоже не ответит — тогда
сообщение уходит напрямую в Bot API (без прокси), а не вышло — ждёт в очереди и уходит, как только связь есть.
"""
from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger("watchdog")

NAMES = {"claude": "Claude API", "eleven": "ElevenLabs", "proxy": "Прокси"}
FAILS = 3              # ошибок подряд — сбой
PROXY_EVERY = 5 * 60   # проверка прокси
PROXY_FAILS = 2        # проверок подряд не прошло — сбой (≈ 10 мин)
SEND_RETRY = 60        # очередь сообщений — повтор раз в минуту

_bot = None
_token = ""
_owner = 0
_proxy = ""            # прокси, через который бот работает сейчас ("" — напрямую)
_state: dict[str, dict] = {}
_queue: list[str] = []
_tasks: set = set()


def setup(bot, token: str, owner_id: int, proxy: str = "") -> None:
    global _bot, _token, _owner, _proxy
    _bot, _token, _owner, _proxy = bot, token, owner_id, proxy or ""


def _st(svc: str) -> dict:
    return _state.setdefault(svc, {"fails": 0, "down": 0.0, "why": ""})


def is_down(svc: str) -> bool:
    return bool(_st(svc)["down"])


def _mins(secs: float) -> str:
    m = int(secs // 60)
    if m < 1:
        return "меньше минуты"
    if m < 60:
        return f"{m} мин"
    return f"{m // 60} ч {m % 60} мин" if m % 60 else f"{m // 60} ч"


def ok(svc: str) -> None:
    st = _st(svc)
    st["fails"] = 0
    if st["down"]:
        took = time.time() - st["down"]
        st["down"], st["why"] = 0.0, ""
        logger.info("сторож: %s снова работает (лежал %s)", svc, _mins(took))
        _notify(f"✅ {NAMES.get(svc, svc)} снова работает (не работал {_mins(took)}).")


def fail(svc: str, why: str, hard: bool = False) -> None:
    st = _st(svc)
    st["fails"] += 1
    st["why"] = why or st["why"]
    limit = PROXY_FAILS if svc == "proxy" else FAILS
    if st["down"] or (st["fails"] < limit and not hard):
        return
    st["down"] = time.time()
    logger.warning("сторож: %s не работает (%s, ошибок подряд: %d)", svc, why, st["fails"])
    tail = {"claude": "Пока не починится, ответы и фишки с ИИ не выходят.",
            "eleven": "Пока не починится, голос - запасной (или текстом), песни и звуки не выйдут.",
            "proxy": "Бот всё ещё ходит через этот прокси - если не оживёт, /setproxy поменять или выключить."}
    _notify(f"⚠️ <b>{NAMES.get(svc, svc)} не работает</b>\n<code>{_esc(why)[:300]}</code>\n\n{tail.get(svc, '')}\n"
            "Напишу, когда снова заработает.")


def _esc(text: str) -> str:
    import html

    return html.escape(text or "")


def _notify(text: str) -> None:
    _queue.append(text)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # не в цикле (тесты) — уйдёт из loop()
    t = loop.create_task(_flush())
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


_flush_lock = asyncio.Lock()


async def _flush() -> None:
    if not (_bot and _owner):
        return
    async with _flush_lock:
        while _queue:
            text = _queue[0]
            if not await _send(text):
                return
            _queue.pop(0)


async def _send(text: str) -> bool:
    try:
        await _bot.send_message(_owner, text)
        return True
    except Exception as e:
        logger.info("сторож: через бота не ушло (%s) - пробую напрямую", e)
    try:  # прокси умер — Telegram через него не ответит; пробуем Bot API напрямую
        import aiohttp

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), trust_env=False) as s:
            async with s.post(f"https://api.telegram.org/bot{_token}/sendMessage",
                              json={"chat_id": _owner, "text": text, "parse_mode": "HTML"}) as r:
                return r.status == 200
    except Exception as e:
        logger.info("сторож: напрямую тоже не ушло: %s", e)
        return False


async def loop() -> None:
    """Фон из bot.py: проверка прокси и досылка очереди."""
    last_proxy = time.time()
    while True:
        await asyncio.sleep(SEND_RETRY)
        try:
            if _queue:
                await _flush()
            if _proxy and time.time() - last_proxy >= PROXY_EVERY:
                last_proxy = time.time()
                import netproxy

                good, info = await netproxy.check(_proxy, _token)
                if good:
                    ok("proxy")
                else:
                    fail("proxy", f"{netproxy.mask(_proxy)}: {info}")
        except Exception as e:
            logger.warning("watchdog tick failed: %s", e)


# --- как понять, что ошибка Claude — сбой сервиса, а не плохой запрос ---

def claude_error(e: BaseException) -> None:
    """Из ai._complete: ошибка запроса к Claude → fail (связь, 5xx, перегруз, лимиты, ключ, деньги) или ничего."""
    status = getattr(e, "status_code", None)
    text = str(e)
    low = text.lower()
    name = type(e).__name__
    if name in ("APIConnectionError", "APITimeoutError") or isinstance(e, (asyncio.TimeoutError, OSError)):
        fail("claude", f"нет связи: {name}")
    elif status in (401, 403):
        fail("claude", f"ключ не принят ({status})", hard=True)
    elif "credit balance" in low or "billing" in low:
        fail("claude", "кончились деньги на балансе Claude", hard=True)
    elif status == 429 or (isinstance(status, int) and status >= 500):
        fail("claude", f"ошибка {status}: {text[:160]}")
