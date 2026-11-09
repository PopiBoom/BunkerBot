"""Бета Bot API 10.4 (`https://api.telegram.org/beta`) — весь бот через неё, со страховкой.

Зачем: HTML-виджеты (шахматы в личке — games/chess/widget.py) шлёт метод `sendWidget`, а ходы из виджета приходят
боту обновлением `widget_query` — и то и другое есть только на бета-сервере (на 09.11 вышел Bot API 10.3). Слать
виджет можно и разовым запросом (так делал /widget), а вот `widget_query` бот получает только из getUpdates беты:
обычный сервер таких обновлений не знает. Поэтому на бету переходит весь бот (как бот-пример Telegram в
widget-sandbox: `TW_BOT_API=https://api.telegram.org/beta`).

Как включается: /chess_widget (или /beta on) ставит настройку `bot_api_beta` и перезапускает бота (не посреди
игр — `admin._restart_when_free`); что просили сделать — в `beta_pending`, после перезапуска `after_start` делает
это само (вызов в шахматы приходит без второй команды). /beta off — обратно на обычный Bot API.

Страховка:
- при запуске бета проверяется (getMe, 20 с): не ответила — бот работает через обычный Bot API, а тому, кто ждал
  отложенное действие, — почему не вышло;
- бета перестала отдавать обновления (getUpdates падает `FAIL_COUNT` раз подряд дольше `FAIL_SECS`) — настройка
  выключается, бот перезапускается на обычном Bot API и пишет владельцу (без беты бот глухой — тянуть нельзя);
- файлы (фото, голосовые, кружки — бот их скачивает) сначала берутся с беты, не отдала — с обычного сервера.
Чего страховка не поймает: бета отвечает, но теряет часть обновлений. Если после включения бот «не слышит» —
/beta off.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.methods.base import TelegramMethod

logger = logging.getLogger("tgbeta")

BASE = "https://api.telegram.org/beta"
MAIN = "https://api.telegram.org"
KEY = "bot_api_beta"        # «1» — весь бот через бету
PENDING = "beta_pending"    # что сделать после перезапуска на бете (JSON: kind, user, chat, name)
NOTE = "beta_note"          # что сказать владельцу после перезапуска (бета отвалилась)
FAIL_COUNT = 5              # getUpdates упал столько раз подряд…
FAIL_SECS = 120             # …и дольше стольких секунд — уходим с беты
UPDATES = ["widget_query"]  # обновления, которые бывают только на бете

_active = False             # бот сейчас работает через бету
_db = None
_fails: list[float] = []    # время подряд идущих сбоев getUpdates
_leaving = False


def active() -> bool:
    return _active


def server() -> TelegramAPIServer:
    return TelegramAPIServer(base=BASE + "/bot{token}/{method}", file=BASE + "/file/bot{token}/{path}")


def _alt_file_url(url: str) -> str:
    """Тот же файл на обычном сервере (бета могла не отдать: где она держит файлы — нигде не сказано)."""
    return MAIN + url[len(BASE):] if url.startswith(BASE + "/file/") else ""


class BetaSession(AiohttpSession):
    """Сессия aiogram на бете: файлы — с запасным адресом, getUpdates — под присмотром (см. `_poll_failed`)."""

    async def stream_content(self, url: str, headers: dict[str, Any] | None = None, timeout: int = 30,
                             chunk_size: int = 65536, raise_for_status: bool = True):
        import aiohttp

        alt = _alt_file_url(url)
        try:
            async for chunk in super().stream_content(url, headers, timeout, chunk_size, raise_for_status):
                yield chunk
            return
        except aiohttp.ClientResponseError as e:  # до первого куска: ответ не 200
            if not alt:
                raise
            logger.info("файл с беты не отдался (%s) — беру с обычного сервера", e.status)
        async for chunk in super().stream_content(alt, headers, timeout, chunk_size, raise_for_status):
            yield chunk

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None):
        polling = getattr(method, "__api_method__", "") == "getUpdates"
        try:
            result = await super().make_request(bot, method, timeout)
        except Exception as e:
            if polling:
                _poll_failed(e)
            raise
        if polling:
            _fails.clear()
        return result


def _poll_failed(e: Exception) -> None:
    from aiogram.exceptions import TelegramConflictError, TelegramRetryAfter

    global _leaving
    if isinstance(e, (TelegramConflictError, TelegramRetryAfter, asyncio.CancelledError)) or _leaving:
        return
    now = time.time()
    _fails.append(now)
    logger.warning("бета: getUpdates не прошёл (%d подряд): %s", len(_fails), str(e)[:200])
    if len(_fails) >= FAIL_COUNT and now - _fails[0] >= FAIL_SECS:
        _leaving = True
        asyncio.get_running_loop().create_task(_leave(f"{type(e).__name__}: {e}"))


async def _leave(why: str) -> None:
    """Бета перестала отдавать обновления: выключить, запомнить, что сказать владельцу, перезапуститься."""
    logger.error("бета не отдаёт обновления (%s) — перезапуск на обычном Bot API", why)
    try:
        if _db is not None:
            await _db.set_setting(KEY, "")
            await _db.set_setting(NOTE, f"⚠️ Бета Telegram перестала отвечать - вернулся на обычный Bot API, "
                                        f"шахматы-виджет пока не работают.\n<code>{_clean(why)}</code>\n\n"
                                        "/beta on - попробовать снова.")
    finally:
        os._exit(0)  # systemd поднимет через 5 сек


def _clean(text: str) -> str:
    import html

    token = os.getenv("BOT_TOKEN", "")
    text = str(text)
    if token:
        text = text.replace(token, "<токен>")
    return html.escape(text[:200])


async def pick(config, db) -> tuple[AiohttpSession | None, str]:
    """При запуске: включена бета — проверить её. (сессия на бете, "") или (None, почему не вышло)."""
    global _active, _db
    _db = db
    if await db.get_setting(KEY, "") != "1":
        return None, ""
    session = BetaSession(api=server(), proxy=config.proxy) if config.proxy else BetaSession(api=server())
    try:
        me = await asyncio.wait_for(Bot(token=config.token, session=session).get_me(), 20)
    except Exception as e:  # noqa: BLE001 — любая беда беты: работаем как раньше
        await session.close()
        why = f"{type(e).__name__}: {e}".replace(config.token, "<токен>")[:200]
        logger.warning("бета Bot API не отвечает (%s) — работаю через обычный", why)
        return None, why
    _active = True
    logger.info("бот @%s работает через бету Bot API (%s)", me.username, BASE)
    return session, ""


def allowed_updates(used: list[str]) -> list[str]:
    """Типы обновлений для getUpdates: на бете — ещё и запросы виджетов."""
    return list(dict.fromkeys(list(used) + (UPDATES if _active else [])))


async def enable(db, pending: dict | None = None) -> None:
    await db.set_setting(KEY, "1")
    if pending:
        await db.set_setting(PENDING, json.dumps(pending, ensure_ascii=False))


async def disable(db) -> None:
    await db.set_setting(KEY, "")
    await db.set_setting(PENDING, "")


async def after_start(bot: Bot, db, config, problem: str) -> None:
    """После запуска: сказать владельцу, что бета отвалилась, и выполнить отложенное действие."""
    await asyncio.sleep(3)
    owner = config.admin_ids[0] if config.admin_ids else 0
    try:
        note = await db.get_setting(NOTE, "")
        if note and owner:
            await db.set_setting(NOTE, "")
            await bot.send_message(owner, note)
        raw = await db.get_setting(PENDING, "")
        if not raw:
            return
        await db.set_setting(PENDING, "")  # сразу: упадёт — не повторять при каждом запуске
        try:
            job = json.loads(raw)
        except ValueError:
            return
        user = int(job.get("user") or owner or 0)
        if not _active:
            if user:
                await bot.send_message(user, "⚠️ Бета Telegram не отвечает - без неё виджеты не работают, остался на "
                                             f"обычном Bot API.\n<code>{_clean(problem)}</code>\n\nПопробуй позже: "
                                             "/chess_widget или /beta on.")
            return
        if job.get("kind") == "wchess":
            from games.chess import widget

            await widget.create(bot, db, user, job.get("name") or "", where=job.get("where"))
        elif job.get("kind") == "durak":
            from games.durak import game as durak

            await durak.create(bot, db, user, job.get("name") or "", where=job.get("where"))
    except Exception:  # noqa: BLE001 — бот работает и без этого
        logger.exception("бета: действие после запуска не вышло")


# ---------- запросы виджетов ----------

_widget_handlers: list[tuple[str, Any]] = []


def on_widget(prefix: str, handler) -> None:
    """Кто разбирает запросы виджетов — по началу data: «u» — дурак, «» — остальное (шахматы)."""
    _widget_handlers.append((prefix, handler))
    _widget_handlers.sort(key=lambda h: -len(h[0]))


class WidgetQueries(BaseMiddleware):
    """Обновление widget_query (Bot API 10.4) aiogram не знает: ловим его до разбора типа обновления
    (outer middleware dp.update) и отдаём игре по началу data."""

    async def __call__(self, handler, event, data: dict):
        q = (getattr(event, "model_extra", None) or {}).get("widget_query")
        if q is None:
            return await handler(event, data)
        if isinstance(q, dict) and data.get("bot") is not None and data.get("db") is not None:
            text = str(q.get("data") or "")
            for prefix, func in _widget_handlers:
                if text.startswith(prefix):
                    await func(data["bot"], data["db"], q)
                    break
        return None


# ---------- методы Bot API 10.4, которых нет в aiogram ----------


class SendWidget(TelegramMethod[Any]):
    """sendWidget: HTML-виджет сообщением (только бета). Ответ — сообщение (dict) с полем widget."""

    __returning__ = Any
    __api_method__ = "sendWidget"

    chat_id: int
    html: str
    allow_queries: bool | None = None
    chat_data: str | None = None
    public_data: str | None = None
    caption: str | None = None
    width: int | None = None
    height: int | None = None


class EditMessageWidgetData(TelegramMethod[Any]):
    """editMessageWidgetData: поменять состояние виджета (chat_data / public_data) — молча, без «изменено»."""

    __returning__ = Any
    __api_method__ = "editMessageWidgetData"

    chat_id: int
    message_id: int
    chat_data: str | None = None
    public_data: str | None = None


class AnswerWidgetQuery(TelegramMethod[Any]):
    """answerWidgetQuery: ответ на запрос виджета (только тому, кто нажал; ≤ 1024 байт текста)."""

    __returning__ = Any
    __api_method__ = "answerWidgetQuery"

    widget_query_id: str
    data: str | None = None
