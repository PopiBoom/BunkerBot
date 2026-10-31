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
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_messages (
                    chat_id  INTEGER NOT NULL,
                    msg_id   INTEGER NOT NULL,
                    user_id  INTEGER NOT NULL,
                    name     TEXT,
                    username TEXT,
                    text     TEXT NOT NULL,
                    ts       INTEGER NOT NULL
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_messages ON chat_messages (chat_id, ts)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS dossiers (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    name    TEXT,
                    text    TEXT NOT NULL,
                    updated INTEGER NOT NULL,
                    PRIMARY KEY (chat_id, user_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS roast_optout (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    PRIMARY KEY (chat_id, user_id)
                )
                """
            )
            await db.commit()

    # --- память чата (стёб-бот) ---

    async def add_chat_message(self, chat_id: int, msg_id: int, user_id: int, name: str,
                               username: str | None, text: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO chat_messages (chat_id, msg_id, user_id, name, username, text, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chat_id, msg_id, user_id, name, username, text, int(time.time())),
            )
            await db.commit()

    async def import_messages(self, chat_id: int, rows: list[tuple]) -> int:
        """Импорт из выгрузки Telegram: rows = [(msg_id, user_id, name, text, ts)]. Дубли пропускаются."""
        added = 0
        async with aiosqlite.connect(self.path) as db:
            for msg_id, user_id, name, text, ts in rows:
                cur = await db.execute(
                    "SELECT 1 FROM chat_messages WHERE chat_id = ? AND msg_id = ?", (chat_id, msg_id)
                )
                if await cur.fetchone():
                    continue
                await db.execute(
                    "INSERT INTO chat_messages (chat_id, msg_id, user_id, name, username, text, ts) "
                    "VALUES (?, ?, ?, ?, NULL, ?, ?)",
                    (chat_id, msg_id, user_id, name, text, ts),
                )
                added += 1
            await db.commit()
        return added

    async def style_sample(self, chat_id: int, n: int = 25) -> list[str]:
        """Случайные живые фразы из чата — образец манеры общения для бота."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT text FROM chat_messages WHERE chat_id = ? AND length(text) BETWEEN 3 AND 160 "
                "ORDER BY RANDOM() LIMIT ?", (chat_id, n),
            )
            return [r[0] for r in await cur.fetchall()]

    async def optout_ids(self, chat_id: int) -> set[int]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT user_id FROM roast_optout WHERE chat_id = ?", (chat_id,))
            return {int(r[0]) for r in await cur.fetchall()}

    async def recent_messages(self, chat_id: int, limit: int = 40, user_id: int | None = None,
                              since: int = 0) -> list[tuple]:
        """[(msg_id, user_id, name, text, ts)] от старых к новым, без тех, кто отказался."""
        sql = ("SELECT m.msg_id, m.user_id, m.name, m.text, m.ts FROM chat_messages m "
               "WHERE m.chat_id = ? AND m.ts >= ? AND NOT EXISTS (SELECT 1 FROM roast_optout o "
               "WHERE o.chat_id = m.chat_id AND o.user_id = m.user_id)")
        args: list = [chat_id, since]
        if user_id is not None:
            sql += " AND m.user_id = ?"
            args.append(user_id)
        sql += " ORDER BY m.ts DESC LIMIT ?"
        args.append(limit)
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(sql, args)
            rows = list(await cur.fetchall())
        return rows[::-1]

    async def count_messages_since(self, chat_id: int, since: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE chat_id = ? AND ts >= ?", (chat_id, since)
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    async def active_chats(self, since: int) -> list[int]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT DISTINCT chat_id FROM chat_messages WHERE ts >= ?", (since,))
            return [int(r[0]) for r in await cur.fetchall()]

    async def find_user(self, chat_id: int, username: str) -> tuple | None:
        """(user_id, name) по @username среди тех, кто писал в чат."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT user_id, name FROM chat_messages WHERE chat_id = ? AND lower(username) = lower(?) "
                "ORDER BY ts DESC LIMIT 1", (chat_id, username.lstrip("@")),
            )
            return await cur.fetchone()

    async def users_needing_dossier(self, min_new: int = 30, stale_new: int = 5,
                                    stale_after: int = 86400, limit: int = 5) -> list[tuple]:
        """(chat_id, user_id, name, старое досье) — у кого накопилось новое для сжатия."""
        now = int(time.time())
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """
                SELECT m.chat_id, m.user_id, MAX(m.name), COALESCE(d.text, ''), COUNT(*) AS n,
                       COALESCE(d.updated, 0) AS upd
                FROM chat_messages m
                LEFT JOIN dossiers d ON d.chat_id = m.chat_id AND d.user_id = m.user_id
                WHERE m.ts > COALESCE(d.updated, 0)
                  AND NOT EXISTS (SELECT 1 FROM roast_optout o
                                  WHERE o.chat_id = m.chat_id AND o.user_id = m.user_id)
                GROUP BY m.chat_id, m.user_id
                HAVING n >= ? OR (n >= ? AND ? - upd >= ?)
                ORDER BY n DESC LIMIT ?
                """,
                (min_new, stale_new, now, stale_after, limit),
            )
            return [(r[0], r[1], r[2], r[3]) for r in await cur.fetchall()]

    async def messages_since_dossier(self, chat_id: int, user_id: int, limit: int = 120) -> list[str]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """
                SELECT m.text FROM chat_messages m
                LEFT JOIN dossiers d ON d.chat_id = m.chat_id AND d.user_id = m.user_id
                WHERE m.chat_id = ? AND m.user_id = ? AND m.ts > COALESCE(d.updated, 0)
                ORDER BY m.ts DESC LIMIT ?
                """,
                (chat_id, user_id, limit),
            )
            return [r[0] for r in await cur.fetchall()][::-1]

    async def set_dossier(self, chat_id: int, user_id: int, name: str, text: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO dossiers (chat_id, user_id, name, text, updated) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(chat_id, user_id) DO UPDATE SET name = excluded.name, "
                "text = excluded.text, updated = excluded.updated",
                (chat_id, user_id, name, text, int(time.time())),
            )
            await db.commit()

    async def get_dossier(self, chat_id: int, user_id: int) -> str:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT text FROM dossiers WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
            )
            row = await cur.fetchone()
            return row[0] if row else ""

    async def chat_dossiers(self, chat_id: int) -> dict[str, str]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT name, text FROM dossiers d WHERE chat_id = ? AND NOT EXISTS ("
                "SELECT 1 FROM roast_optout o WHERE o.chat_id = d.chat_id AND o.user_id = d.user_id)",
                (chat_id,),
            )
            return {r[0]: r[1] for r in await cur.fetchall()}

    async def is_optout(self, chat_id: int, user_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT 1 FROM roast_optout WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
            )
            return await cur.fetchone() is not None

    async def forget_user(self, chat_id: int, user_id: int) -> None:
        """Удаляет всё о человеке в этом чате и больше его не запоминает."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM chat_messages WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
            await db.execute("DELETE FROM dossiers WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
            await db.execute("INSERT OR IGNORE INTO roast_optout (chat_id, user_id) VALUES (?, ?)",
                             (chat_id, user_id))
            await db.commit()

    async def unforget_user(self, chat_id: int, user_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM roast_optout WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
            await db.commit()

    async def purge_old_messages(self, older_than: int, keep_last: int = 3000) -> None:
        """Удаляет сообщения старше older_than, но последние keep_last в каждом чате оставляет."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "DELETE FROM chat_messages WHERE ts < ? AND rowid NOT IN ("
                "SELECT c2.rowid FROM chat_messages c2 WHERE c2.chat_id = chat_messages.chat_id "
                "ORDER BY c2.ts DESC LIMIT ?)",
                (older_than, keep_last),
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
