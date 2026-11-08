"""Загрузка конфигурации из переменных окружения / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _split(raw: str) -> list[str]:
    return [p.strip() for p in raw.replace(",", " ").split() if p.strip()]


def _parse_ids(raw: str) -> list[int]:
    return [int(p) for p in _split(raw) if p.lstrip("-").isdigit()]


@dataclass(frozen=True)
class Config:
    token: str
    admin_ids: list[int]
    required_chats: list[str]
    db_path: str
    proxy: str | None = None

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids


def proxy_all() -> bool:
    """BOT_PROXY_ALL=1 — через BOT_PROXY идёт ВЕСЬ бот (Claude, OpenRouter, голоса, погода), а не только Telegram."""
    return os.getenv("BOT_PROXY_ALL", "").strip().lower() in ("1", "true", "yes", "да", "on")


def route_all_via(proxy: str) -> None:
    """Все HTTP-клиенты бота (SDK Claude на httpx, aiohttp с trust_env) берут прокси из переменных окружения."""
    if not proxy.startswith(("http://", "https://")):
        # socks5 понимает только Telegram (aiohttp_socks); остальным клиентам нужен HTTP-прокси
        raise RuntimeError("BOT_PROXY_ALL работает с HTTP-прокси (http://логин:пароль@адрес:порт), не с socks5://")
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        os.environ[var] = proxy
    os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "BOT_TOKEN не задан. Скопируй .env.example в .env и заполни значения."
        )

    admin_ids = _parse_ids(os.getenv("ADMIN_IDS", ""))
    if not admin_ids:
        raise RuntimeError("ADMIN_IDS не задан — укажи хотя бы один Telegram ID админа.")

    required_chats = _split(os.getenv("REQUIRED_CHATS", ""))
    if not required_chats:
        raise RuntimeError(
            "REQUIRED_CHATS не задан — укажи @username или id канала/чата для проверки."
        )

    db_path = os.getenv("DB_PATH", "bot.db").strip() or "bot.db"
    proxy = os.getenv("BOT_PROXY", "").strip() or None  # на весь бот (BOT_PROXY_ALL) — включает bot.main после проверки

    return Config(
        token=token,
        admin_ids=admin_ids,
        required_chats=required_chats,
        db_path=db_path,
        proxy=proxy,
    )
