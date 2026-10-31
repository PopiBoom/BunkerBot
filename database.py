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
                    first_out   INTEGER NOT NULL DEFAULT 0
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
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS promises (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id  INTEGER NOT NULL,
                    user_id  INTEGER NOT NULL,
                    name     TEXT,
                    msg_id   INTEGER,
                    text     TEXT,
                    what     TEXT NOT NULL,
                    due_ts   INTEGER NOT NULL,
                    created  INTEGER NOT NULL,
                    reminded INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS clown_stats (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    name    TEXT,
                    count   INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (chat_id, user_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS stickers (
                    chat_id   INTEGER NOT NULL,
                    unique_id TEXT NOT NULL,
                    file_id   TEXT NOT NULL,
                    emoji     TEXT,
                    set_name  TEXT,
                    uses      INTEGER NOT NULL DEFAULT 1,
                    last_ts   INTEGER NOT NULL,
                    descr     TEXT,
                    kind      TEXT NOT NULL DEFAULT 'sticker',
                    PRIMARY KEY (chat_id, unique_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS quotes (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id  INTEGER NOT NULL,
                    msg_id   INTEGER NOT NULL,
                    user_id  INTEGER NOT NULL,
                    name     TEXT,
                    text     TEXT NOT NULL,
                    ts       INTEGER NOT NULL,
                    added_by INTEGER,
                    added_ts INTEGER NOT NULL,
                    UNIQUE (chat_id, msg_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_gender (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    m       INTEGER NOT NULL DEFAULT 0,
                    f       INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (chat_id, user_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_line_score (
                    chat_id INTEGER NOT NULL,
                    msg_id  INTEGER NOT NULL,
                    text    TEXT NOT NULL,
                    score   INTEGER NOT NULL DEFAULT 0,
                    ts      INTEGER NOT NULL,
                    PRIMARY KEY (chat_id, msg_id)
                )
                """
            )
            # Миграция: статистика Бункера для званий (серии, голоса против, угроза)
            cur = await db.execute("PRAGMA table_info(bunker_stats)")
            bcols = {r[1] for r in await cur.fetchall()}
            for col in ("streak", "best_streak", "votes_against", "heroes", "deaths", "lucky"):
                if col not in bcols:
                    await db.execute(f"ALTER TABLE bunker_stats ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
            # Миграция: в старых базах у stickers нет описания и вида (стикер / гифка)
            cur = await db.execute("PRAGMA table_info(stickers)")
            cols = {r[1] for r in await cur.fetchall()}
            if "descr" not in cols:
                await db.execute("ALTER TABLE stickers ADD COLUMN descr TEXT")
            if "kind" not in cols:
                await db.execute("ALTER TABLE stickers ADD COLUMN kind TEXT NOT NULL DEFAULT 'sticker'")
            await db.commit()

    # --- стикеры чата (бот запоминает и кидает по ситуации) ---

    async def add_sticker(self, chat_id: int, unique_id: str, file_id: str, emoji: str | None,
                          set_name: str | None, kind: str = "sticker") -> str | None:
        """Запоминает стикер или гифку. Возвращает уже известное описание (или None — ещё не смотрели)."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO stickers (chat_id, unique_id, file_id, emoji, set_name, uses, last_ts, kind) "
                "VALUES (?, ?, ?, ?, ?, 1, ?, ?) ON CONFLICT(chat_id, unique_id) DO UPDATE SET "
                "file_id = excluded.file_id, uses = uses + 1, last_ts = excluded.last_ts",
                (chat_id, unique_id, file_id, emoji, set_name, int(time.time()), kind),
            )
            await db.commit()
            # описание одно на файл: если этот стикер уже описывали (хоть в другом чате) — берём его
            cur = await db.execute(
                "SELECT descr FROM stickers WHERE unique_id = ? AND descr IS NOT NULL LIMIT 1", (unique_id,)
            )
            row = await cur.fetchone()
            if row:
                await db.execute("UPDATE stickers SET descr = ? WHERE unique_id = ? AND descr IS NULL",
                                 (row[0], unique_id))
                await db.commit()
            return row[0] if row else None

    async def set_sticker_descr(self, unique_id: str, descr: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE stickers SET descr = ? WHERE unique_id = ?", (descr, unique_id))
            await db.commit()

    async def chat_stickers(self, chat_id: int, limit: int = 40) -> list[tuple]:
        """[(file_id, emoji, set_name, uses, descr, kind)] — самые ходовые стикеры и гифки чата."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT file_id, emoji, set_name, uses, descr, kind FROM stickers WHERE chat_id = ? "
                "ORDER BY uses DESC, last_ts DESC LIMIT ?", (chat_id, limit),
            )
            return list(await cur.fetchall())

    async def last_username(self, chat_id: int, user_id: int) -> str | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT username FROM chat_messages WHERE chat_id = ? AND user_id = ? AND username IS NOT NULL "
                "ORDER BY ts DESC LIMIT 1", (chat_id, user_id),
            )
            row = await cur.fetchone()
            return row[0] if row else None

    # --- пол участников (по их же сообщениям: «я пошёл» / «я пошла») ---

    async def add_gender_hits(self, chat_id: int, user_id: int, m: int, f: int, replace: bool = False) -> None:
        async with aiosqlite.connect(self.path) as db:
            if replace:
                await db.execute("INSERT OR REPLACE INTO user_gender (chat_id, user_id, m, f) VALUES (?, ?, ?, ?)",
                                 (chat_id, user_id, m, f))
            else:
                await db.execute(
                    "INSERT INTO user_gender (chat_id, user_id, m, f) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(chat_id, user_id) DO UPDATE SET m = m + excluded.m, f = f + excluded.f",
                    (chat_id, user_id, m, f))
            await db.commit()

    async def has_genders(self, chat_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT 1 FROM user_gender WHERE chat_id = ? LIMIT 1", (chat_id,))
            return await cur.fetchone() is not None

    async def gender_counts(self, chat_id: int) -> list[tuple]:
        """[(user_id, последнее имя, m, f)]"""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT g.user_id, (SELECT c.name FROM chat_messages c WHERE c.chat_id = g.chat_id AND "
                "c.user_id = g.user_id ORDER BY c.ts DESC LIMIT 1), g.m, g.f FROM user_gender g WHERE g.chat_id = ?",
                (chat_id,))
            return list(await cur.fetchall())

    async def user_texts(self, chat_id: int, limit: int = 5000) -> list[tuple]:
        """[(user_id, text)] — для разового пересчёта пола по истории."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT user_id, text FROM chat_messages WHERE chat_id = ? AND user_id > 0 ORDER BY ts DESC LIMIT ?",
                (chat_id, limit))
            return list(await cur.fetchall())

    # --- как чат принял шутки бота (реакции на его сообщения) ---

    async def score_bot_line(self, chat_id: int, msg_id: int, delta: int) -> bool:
        """Плюс/минус шутке бота. False — это не сообщение бота."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT text FROM chat_messages WHERE chat_id = ? AND msg_id = ? AND user_id = 0 LIMIT 1",
                (chat_id, msg_id))
            row = await cur.fetchone()
            if not row:
                return False
            await db.execute(
                "INSERT INTO bot_line_score (chat_id, msg_id, text, score, ts) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(chat_id, msg_id) DO UPDATE SET score = score + excluded.score",
                (chat_id, msg_id, row[0], delta, int(time.time())))
            await db.commit()
            return True

    async def scored_lines(self, chat_id: int, good: bool, limit: int = 5) -> list[str]:
        """Шутки бота, которые чату зашли (good) или не зашли — за последние 60 дней."""
        since = int(time.time()) - 60 * 86400
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT text FROM bot_line_score WHERE chat_id = ? AND ts >= ? AND "
                + ("score >= 2 ORDER BY score DESC, ts DESC" if good else "score <= -1 ORDER BY score ASC, ts DESC")
                + " LIMIT ?", (chat_id, since, limit))
            return [r[0] for r in await cur.fetchall()]

    # --- цитатник («золотой фонд») ---

    async def add_quote(self, chat_id: int, msg_id: int, user_id: int, name: str, text: str, ts: int,
                        added_by: int | None) -> int | None:
        """Номер цитаты в фонде чата или None, если это сообщение там уже есть."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "INSERT OR IGNORE INTO quotes (chat_id, msg_id, user_id, name, text, ts, added_by, added_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (chat_id, msg_id, user_id, name, text, ts, added_by, int(time.time())),
            )
            await db.commit()
            if cur.rowcount == 0:
                return None
            cur = await db.execute("SELECT COUNT(*) FROM quotes WHERE chat_id = ?", (chat_id,))
            return int((await cur.fetchone())[0])

    async def random_quote(self, chat_id: int, user_id: int | None = None,
                           older_than: int | None = None) -> tuple | None:
        """(номер в фонде, user_id, name, text, ts) — случайная цитата чата (или человека)."""
        sql = ("SELECT (SELECT COUNT(*) FROM quotes q2 WHERE q2.chat_id = q.chat_id AND q2.id <= q.id), "
               "q.user_id, q.name, q.text, q.ts FROM quotes q WHERE q.chat_id = ?")
        args: list = [chat_id]
        if user_id is not None:
            sql += " AND q.user_id = ?"
            args.append(user_id)
        if older_than is not None:
            sql += " AND q.ts < ?"
            args.append(older_than)
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(sql + " ORDER BY RANDOM() LIMIT 1", args)
            return await cur.fetchone()

    async def quote_counts(self, chat_id: int) -> dict[int, int]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT user_id, COUNT(*) FROM quotes WHERE chat_id = ? GROUP BY user_id",
                                   (chat_id,))
            return {int(r[0]): int(r[1]) for r in await cur.fetchall()}

    # --- статистика чата (без ИИ) ---

    async def stats_messages(self, chat_id: int, since: int) -> list[tuple]:
        """[(user_id, name, text, ts)] живых участников с момента since."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT user_id, name, text, ts FROM chat_messages WHERE chat_id = ? AND ts >= ? AND user_id > 0",
                (chat_id, since),
            )
            return list(await cur.fetchall())

    async def last_seen(self, chat_id: int) -> list[tuple]:
        """[(user_id, name, последнее сообщение ts)] — все, кого бот помнит в чате."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT user_id, MAX(name), MAX(ts) FROM chat_messages WHERE chat_id = ? AND user_id > 0 "
                "GROUP BY user_id", (chat_id,),
            )
            return list(await cur.fetchall())

    async def oldest_message_ts(self, chat_id: int) -> int | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT MIN(ts) FROM chat_messages WHERE chat_id = ? AND user_id > 0", (chat_id,))
            row = await cur.fetchone()
            return int(row[0]) if row and row[0] else None

    async def bunker_stats_of(self, user_id: int) -> tuple | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT games, survived, first_out FROM bunker_stats WHERE user_id = ?", (user_id,))
            return await cur.fetchone()

    # --- обещания (ловушка на обещания) ---

    async def add_promise(self, chat_id: int, user_id: int, name: str, msg_id: int, text: str,
                          what: str, due_ts: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO promises (chat_id, user_id, name, msg_id, text, what, due_ts, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (chat_id, user_id, name, msg_id, text, what, due_ts, int(time.time())),
            )
            await db.commit()

    async def due_promises(self, now: int, limit: int = 5) -> list[tuple]:
        """(id, chat_id, user_id, name, msg_id, text, what, due_ts) — срок вышел, ещё не напоминали."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT p.id, p.chat_id, p.user_id, p.name, p.msg_id, p.text, p.what, p.due_ts FROM promises p "
                "WHERE p.reminded = 0 AND p.due_ts <= ? AND NOT EXISTS (SELECT 1 FROM roast_optout o "
                "WHERE o.chat_id = p.chat_id AND o.user_id = p.user_id) ORDER BY p.due_ts LIMIT ?",
                (now, limit),
            )
            return list(await cur.fetchall())

    async def mark_promise(self, promise_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE promises SET reminded = 1 WHERE id = ?", (promise_id,))
            await db.commit()

    async def open_promises(self, chat_id: int) -> list[tuple]:
        """(name, what, due_ts) — ещё не наступившие/не напомненные обещания чата."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT name, what, due_ts FROM promises WHERE chat_id = ? AND reminded = 0 ORDER BY due_ts",
                (chat_id,),
            )
            return list(await cur.fetchall())

    async def has_similar_promise(self, chat_id: int, user_id: int, what: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT 1 FROM promises WHERE chat_id = ? AND user_id = ? AND reminded = 0 AND lower(what) = lower(?)",
                (chat_id, user_id, what),
            )
            return await cur.fetchone() is not None

    # --- «Клоун дня» ---

    async def add_clown(self, chat_id: int, user_id: int, name: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO clown_stats (chat_id, user_id, name, count) VALUES (?, ?, ?, 1) "
                "ON CONFLICT(chat_id, user_id) DO UPDATE SET name = excluded.name, count = count + 1",
                (chat_id, user_id, name),
            )
            await db.commit()

    async def clown_counts(self, chat_id: int) -> list[tuple]:
        """[(user_id, count)] — сколько раз каждый был клоуном дня."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT user_id, count FROM clown_stats WHERE chat_id = ?", (chat_id,))
            return list(await cur.fetchall())

    async def clown_top(self, chat_id: int, limit: int = 10) -> list[tuple]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT name, count FROM clown_stats WHERE chat_id = ? ORDER BY count DESC LIMIT ?",
                (chat_id, limit),
            )
            return list(await cur.fetchall())

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
                "SELECT text FROM chat_messages WHERE chat_id = ? AND user_id > 0 AND length(text) BETWEEN 3 AND 160 "
                "AND text NOT LIKE '[%' "
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
                "SELECT COUNT(*) FROM chat_messages WHERE chat_id = ? AND ts >= ? AND user_id > 0",
                (chat_id, since)
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
                WHERE m.ts > COALESCE(d.updated, 0) AND m.user_id > 0
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
            await db.execute("DELETE FROM quotes WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
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
        """results: [{user_id, name, survived, first_out, votes_against, hero, died, lucky}]

        Серия выживаний: выжил — streak + 1, нет — 0; лучшая серия запоминается.
        (В UPDATE SQLite все выражения видят старые значения строки.)
        """
        async with aiosqlite.connect(self.path) as db:
            for r in results:
                surv = int(bool(r["survived"]))
                await db.execute(
                    "INSERT INTO bunker_stats (user_id, name, games, survived, first_out, streak, best_streak, "
                    "votes_against, heroes, deaths, lucky) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name, games = games + 1, "
                    "survived = survived + excluded.survived, first_out = first_out + excluded.first_out, "
                    "streak = CASE WHEN excluded.survived = 1 THEN streak + 1 ELSE 0 END, "
                    "best_streak = MAX(best_streak, CASE WHEN excluded.survived = 1 THEN streak + 1 ELSE 0 END), "
                    "votes_against = votes_against + excluded.votes_against, heroes = heroes + excluded.heroes, "
                    "deaths = deaths + excluded.deaths, lucky = lucky + excluded.lucky",
                    (r["user_id"], r["name"], surv, int(bool(r["first_out"])), surv, surv,
                     int(r.get("votes_against", 0)), int(bool(r.get("hero"))), int(bool(r.get("died"))),
                     int(bool(r.get("lucky")))),
                )
            await db.commit()

    _BUNKER_COLS = ("user_id", "name", "games", "survived", "first_out", "streak", "best_streak",
                    "votes_against", "heroes", "deaths", "lucky")

    async def bunker_top(self, limit: int = 10) -> list[dict]:
        """Лучшие по выживаниям: [{name, games, survived, first_out, streak, best_streak, …}]."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                f"SELECT {', '.join(self._BUNKER_COLS)} FROM bunker_stats "
                "ORDER BY survived DESC, games ASC LIMIT ?",
                (limit,),
            )
            return [dict(zip(self._BUNKER_COLS, r)) for r in await cur.fetchall()]

    async def bunker_profile(self, user_id: int) -> dict | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                f"SELECT {', '.join(self._BUNKER_COLS)} FROM bunker_stats WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            if not row:
                return None
            prof = dict(zip(self._BUNKER_COLS, row))
            cur = await db.execute("SELECT COUNT(*) FROM bunker_stats WHERE survived > ?", (prof["survived"],))
            prof["place"] = int((await cur.fetchone())[0]) + 1
            return prof

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
