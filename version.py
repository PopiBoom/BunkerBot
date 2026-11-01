"""Номер версии бота: 0.01, 0.02, … — по одному шагу на каждый коммит в main (просьба владельца).

Считается сам: сколько коммитов после BASE (последний коммит до нумерации). Поэтому версию не надо
поднимать руками — любой новый коммит = +0.01. Работает и на сервере (полный клон), и в облачной
сессии (там история обрезана, но BASE в ней есть).

При запуске бот сравнивает версию с прошлой (настройка bot_version) и, если она новая, пишет в чат
«🔄 Бот обновлён до 0.0N» (bot.py → announce_update). Обычный перезапуск без нового кода — молча.
`python version.py` печатает версию (так её берёт deploy/update.sh для своего лога).
"""
from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

BASE = "29de1ff8aef8f768655518ffff91be2bf49eb9e7"  # 0.00 — последний коммит до нумерации версий
ROOT = Path(__file__).resolve().parent


def _git(*args: str) -> str:
    out = subprocess.run(["git", "-c", f"safe.directory={ROOT}", "-C", str(ROOT), *args],
                         capture_output=True, text=True, timeout=10, check=True)
    return out.stdout.strip()


def number() -> int | None:
    """Сколько коммитов после BASE (None — git недоступен или BASE нет в истории)."""
    try:
        return int(_git("rev-list", "--count", f"{BASE}..HEAD"))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


@lru_cache(maxsize=1)  # код в процессе не меняется: обновление = перезапуск бота
def commit() -> str:
    try:
        return _git("rev-parse", "--short=7", "HEAD")
    except (OSError, subprocess.SubprocessError):
        return "?"


@lru_cache(maxsize=1)
def current() -> str:
    """«0.07» (или короткий хэш коммита, если номер посчитать не вышло)."""
    n = number()
    return f"{n / 100:.2f}" if n is not None else commit()


if __name__ == "__main__":
    print(current())
