"""Что за ссылку скинули: заголовок видео/страницы — чтобы бот стебал по сути, а не по адресу.

YouTube и TikTok — через их открытый oEmbed (название и автор), остальное — по og:title /
og:description / <title> самой страницы. Защита: только http(s), никаких адресов внутренней сети
(сервер не должен ходить по ссылкам из чата к себе домой), таймаут и предел размера.
"""
from __future__ import annotations

import asyncio
import html
import ipaddress
import logging
import re
import socket
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger("links")

URL_RE = re.compile(r"https?://[^\s<>\"'«»]+", re.IGNORECASE)
TIMEOUT = aiohttp.ClientTimeout(total=6)
MAX_BYTES = 400_000
UA = "Mozilla/5.0 (compatible; BunkerBot/1.0; +https://t.me)"
_cache: dict[str, str] = {}


def first_url(text: str, entities=None) -> str | None:
    """Первая ссылка из текста (и из скрытых ссылок-слов Telegram)."""
    for e in entities or []:
        if getattr(e, "type", "") == "text_link" and getattr(e, "url", None):
            return e.url
    m = URL_RE.search(text or "")
    return m.group(0).rstrip(".,!?)") if m else None


async def _public_host(host: str) -> bool:
    """Хост указывает только на публичные адреса (не localhost, не 10.x, 192.168.x и т.п.)."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except Exception:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return bool(infos)


def _clean(s: str | None, n: int) -> str:
    s = html.unescape(re.sub(r"\s+", " ", s or "")).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _meta(page: str, prop: str) -> str | None:
    m = re.search(r'<meta[^>]+(?:property|name)=["\']' + re.escape(prop) + r'["\'][^>]*content=["\']([^"\']*)', page, re.I) \
        or re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']' + re.escape(prop), page, re.I)
    return m.group(1) if m else None


async def _get(session: aiohttp.ClientSession, url: str) -> tuple[str, str]:
    """Скачивает начало страницы; редиректы проходим сами, проверяя каждый адрес на «публичность»."""
    for _ in range(4):
        async with session.get(url, allow_redirects=False) as r:
            if r.status in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                url = str(r.url.join(aiohttp.client.URL(r.headers["Location"])))
                host = urlparse(url).hostname
                if urlparse(url).scheme not in ("http", "https") or not host or not await _public_host(host):
                    return "", ""
                continue
            if r.status != 200:
                return "", ""
            data = await r.content.read(MAX_BYTES)
            return data.decode(r.charset or "utf-8", errors="replace"), r.content_type or ""
    return "", ""


async def link_info(url: str) -> str:
    """«[ссылка: YouTube — «Название» (автор)]» или "" если не вышло."""
    if url in _cache:
        return _cache[url]
    out = ""
    try:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname or not await _public_host(u.hostname):
            return ""
        host = u.hostname.lower().removeprefix("www.").removeprefix("m.")
        async with aiohttp.ClientSession(timeout=TIMEOUT, headers={"User-Agent": UA}, trust_env=True) as s:
            if host in ("youtube.com", "youtu.be", "music.youtube.com") or host.endswith(".youtube.com"):
                async with s.get("https://www.youtube.com/oembed", params={"url": url, "format": "json"}) as r:
                    if r.status == 200:
                        j = await r.json(content_type=None)
                        out = f"YouTube — «{_clean(j.get('title'), 120)}» ({_clean(j.get('author_name'), 40)})"
            elif host.endswith("tiktok.com"):
                async with s.get("https://www.tiktok.com/oembed", params={"url": url}) as r:
                    if r.status == 200:
                        j = await r.json(content_type=None)
                        out = f"TikTok — «{_clean(j.get('title'), 140)}» ({_clean(j.get('author_name'), 40)})"
            if not out:
                page, ctype = await _get(s, url)
                if page and "html" in ctype:
                    title = _meta(page, "og:title") or (re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
                                                       or [None, None])[1]
                    desc = _meta(page, "og:description") or _meta(page, "description")
                    if title:
                        out = f"{host} — «{_clean(title, 120)}»" + (f": {_clean(desc, 160)}" if desc else "")
    except Exception as e:
        logger.debug("link info failed for %s: %s", url, e)
        out = ""
    out = f"[ссылка: {out}]" if out else ""
    if len(_cache) > 500:
        _cache.clear()
    _cache[url] = out
    return out
