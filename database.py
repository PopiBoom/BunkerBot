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
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS bunker_stats (
                    user_id     INTEGER PRIMARY KEY,
                    name        TEXT NOT NULL,
                    games       INTEGER NOT NULL DEFAULT 0,
                    survived    INTEGER NOT NULL DEFAULT 0,
                    first_out   INTEGER NOT NULL DEFAULT 0,
                    goals_done  INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS ai_usage (
                    ts            INTEGER NOT NULL,
                    game_key      TEXT,
                    model         TEXT NOT NULL,
                    input_tokens  INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    cost          REAL NOT NULL
                )
                """
            )
            await db.commit()

    # --- расходы на ИИ ---

    async def add_ai_usage(self, game_key: str | None, model: str, input_tokens: int,
                           output_tokens: int, cost: float) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO ai_usage (ts, game_key, model, input_tokens, output_tokens, cost) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (int(time.time()), game_key, model, input_tokens, output_tokens, cost),
            )
            await db.commit()

    async def ai_usage_report(self) -> dict:
        """Сводка расходов: последняя партия, сегодня, 30 дней, всё время, по моделям."""
        now = int(time.time())
        day = now - now % 86400
        async with aiosqlite.connect(self.path) as db:
            async def one(sql: str, args: tuple = ()) -> tuple:
                cur = await db.execute(sql, args)
                return await cur.fetchone() or (0, 0)

            report = {
                "today": await one("SELECT COALESCE(SUM(cost),0), COUNT(*) FROM ai_usage WHERE ts >= ?", (day,)),
                "month": await one("SELECT COALESCE(SUM(cost),0), COUNT(*) FROM ai_usage WHERE ts >= ?",
                                   (now - 30 * 86400,)),
                "total": await one("SELECT COALESCE(SUM(cost),0), COUNT(*) FROM ai_usage"),
                "games": (await one("SELECT COUNT(DISTINCT game_key) FROM ai_usage WHERE game_key IS NOT NULL"))[0],
            }
            cur = await db.execute(
                "SELECT game_key FROM ai_usage WHERE game_key IS NOT NULL ORDER BY ts DESC LIMIT 1"
            )
            row = await cur.fetchone()
            report["last"] = await one(
                "SELECT COALESCE(SUM(cost),0), COUNT(*) FROM ai_usage WHERE game_key = ?", (row[0],)
            ) if row else (0, 0)
            cur = await db.execute(
                "SELECT model, SUM(cost), SUM(input_tokens), SUM(output_tokens), COUNT(*) "
                "FROM ai_usage GROUP BY model ORDER BY SUM(cost) DESC"
            )
            report["models"] = list(await cur.fetchall())
            return report

    # --- статистика Бункера ---

    async def record_bunker_game(self, results: list[dict]) -> None:
        """results: [{user_id, name, survived: bool, first_out: bool, goal_done: bool}]"""
        async with aiosqlite.connect(self.path) as db:
            for r in results:
                await db.execute(
                    "INSERT INTO bunker_stats (user_id, name, games, survived, first_out, goals_done) "
                    "VALUES (?, ?, 1, ?, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name, "
                    "games = games + 1, survived = survived + excluded.survived, "
                    "first_out = first_out + excluded.first_out, "
                    "goals_done = goals_done + excluded.goals_done",
                    (r["user_id"], r["name"], int(r["survived"]), int(r["first_out"]),
                     int(r["goal_done"])),
                )
            await db.commit()

    async def bunker_top(self, limit: int = 10) -> list[tuple]:
        """(name, games, survived, first_out, goals_done), лучшие по выживаниям."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT name, games, survived, first_out, goals_done FROM bunker_stats "
                "ORDER BY survived DESC, goals_done DESC, games ASC LIMIT ?",
                (limit,),
            )
            return list(await cur.fetchall())

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
