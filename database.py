"""Простое хранилище на SQLite (aiosqlite)."""
from __future__ import annotations

import time

import aiosqlite


class Database:
    def __init__(self, path: str) -> None:
        self.path = path

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id    INTEGER PRIMARY KEY,
                    username   TEXT,
                    first_name TEXT,
                    joined_at  INTEGER NOT NULL,
                    got_reward INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            await db.commit()

    # --- пользователи ---

    async def add_user(self, user_id: int, username: str | None, first_name: str | None) -> bool:
        """Добавляет пользователя. Возвращает True, если это новый пользователь."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
            exists = await cur.fetchone()
            if exists:
                await db.execute(
                    "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
                    (username, first_name, user_id),
                )
                await db.commit()
                return False
            await db.execute(
                "INSERT INTO users (user_id, username, first_name, joined_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id, username, first_name, int(time.time())),
            )
            await db.commit()
            return True

    async def mark_reward(self, user_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE users SET got_reward = 1 WHERE user_id = ?", (user_id,)
            )
            await db.commit()

    async def count_users(self) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM users")
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    async def count_rewarded(self) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM users WHERE got_reward = 1")
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    async def all_user_ids(self) -> list[int]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT user_id FROM users")
            rows = await cur.fetchall()
            return [int(r[0]) for r in rows]

    # --- настройки (редактируемые тексты) ---

    async def get_setting(self, key: str, default: str) -> str:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = await cur.fetchone()
            return row[0] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            await db.commit()
