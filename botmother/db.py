from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    first_seen_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    token TEXT NOT NULL UNIQUE,
    bot_username TEXT,
    status TEXT NOT NULL,
    pid INTEGER,
    workdir TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    last_started_at INTEGER,
    last_stopped_at INTEGER,
    deleted_at INTEGER,
    FOREIGN KEY(owner_user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    prompt TEXT NOT NULL,
    code TEXT NOT NULL,
    validation_status TEXT NOT NULL,
    validation_error TEXT,
    created_at INTEGER NOT NULL,
    FOREIGN KEY(bot_id) REFERENCES bots(id)
);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    stream TEXT NOT NULL,
    line TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    FOREIGN KEY(bot_id) REFERENCES bots(id)
);

CREATE TABLE IF NOT EXISTS bot_env_vars (
    bot_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(bot_id, name),
    FOREIGN KEY(bot_id) REFERENCES bots(id)
);

CREATE INDEX IF NOT EXISTS idx_bots_owner ON bots(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_bots_status ON bots(status);
CREATE INDEX IF NOT EXISTS idx_logs_bot_id ON logs(bot_id, id);
"""


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.session() as conn:
            conn.executescript(SCHEMA)
            self._ensure_bot_username_column(conn)

    def _ensure_bot_username_column(self, conn: sqlite3.Connection) -> None:
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(bots)").fetchall()
        }
        if "bot_username" not in columns:
            conn.execute("ALTER TABLE bots ADD COLUMN bot_username TEXT")

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def upsert_user(
        self,
        user_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> None:
        now = int(time.time())
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, username, first_name, last_name, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name,
                    last_name=excluded.last_name,
                    last_seen_at=excluded.last_seen_at
                """,
                (user_id, username, first_name, last_name, now, now),
            )

    def create_bot(
        self,
        owner_user_id: int,
        chat_id: int,
        name: str,
        prompt: str,
        token: str,
        workdir: Path,
    ) -> int:
        now = int(time.time())
        with self.session() as conn:
            cur = conn.execute(
                """
                INSERT INTO bots
                    (owner_user_id, chat_id, name, prompt, token, status, workdir, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'generating', ?, ?, ?)
                """,
                (owner_user_id, chat_id, name, prompt, token, str(workdir), now, now),
            )
            return int(cur.lastrowid)

    def add_revision(
        self,
        bot_id: int,
        prompt: str,
        code: str,
        validation_status: str,
        validation_error: str | None,
    ) -> int:
        now = int(time.time())
        with self.session() as conn:
            cur = conn.execute(
                """
                INSERT INTO revisions (bot_id, prompt, code, validation_status, validation_error, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (bot_id, prompt, code, validation_status, validation_error, now),
            )
            return int(cur.lastrowid)

    def get_bot(self, bot_id: int, include_deleted: bool = False) -> sqlite3.Row | None:
        where = "b.id = ?" if include_deleted else "b.id = ? AND b.deleted_at IS NULL"
        with self.session() as conn:
            return conn.execute(
                f"""
                SELECT b.*, u.username AS owner_username, u.first_name AS owner_first_name, u.last_name AS owner_last_name
                FROM bots b
                LEFT JOIN users u ON u.user_id = b.owner_user_id
                WHERE {where}
                """,
                (bot_id,),
            ).fetchone()

    def get_bot_by_token(self, token: str) -> sqlite3.Row | None:
        with self.session() as conn:
            return conn.execute(
                """
                SELECT b.*, u.username AS owner_username, u.first_name AS owner_first_name, u.last_name AS owner_last_name
                FROM bots b
                LEFT JOIN users u ON u.user_id = b.owner_user_id
                WHERE b.token = ? AND b.deleted_at IS NULL
                ORDER BY b.id DESC
                LIMIT 1
                """,
                (token,),
            ).fetchone()

    def release_deleted_token(self, token: str) -> int:
        now = int(time.time())
        with self.session() as conn:
            cur = conn.execute(
                """
                UPDATE bots
                SET token = '__deleted__:' || id || ':' || token,
                    updated_at = ?
                WHERE token = ?
                  AND deleted_at IS NOT NULL
                """,
                (now, token),
            )
            return int(cur.rowcount)

    def list_bots(
        self, owner_user_id: int | None = None, include_deleted: bool = False
    ) -> list[sqlite3.Row]:
        clauses = []
        params: list[Any] = []
        if owner_user_id is not None:
            clauses.append("b.owner_user_id = ?")
            params.append(owner_user_id)
        if not include_deleted:
            clauses.append("b.deleted_at IS NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.session() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT b.*, u.username AS owner_username, u.first_name AS owner_first_name, u.last_name AS owner_last_name
                    FROM bots b
                    LEFT JOIN users u ON u.user_id = b.owner_user_id
                    {where}
                    ORDER BY b.id DESC
                    """,
                    params,
                ).fetchall()
            )

    def latest_revision(self, bot_id: int) -> sqlite3.Row | None:
        with self.session() as conn:
            return conn.execute(
                "SELECT * FROM revisions WHERE bot_id = ? ORDER BY id DESC LIMIT 1",
                (bot_id,),
            ).fetchone()

    def set_bot_env_vars(self, bot_id: int, env_vars: dict[str, str]) -> None:
        now = int(time.time())
        with self.session() as conn:
            for name, value in env_vars.items():
                conn.execute(
                    """
                    INSERT INTO bot_env_vars (bot_id, name, value, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(bot_id, name) DO UPDATE SET
                        value=excluded.value,
                        updated_at=excluded.updated_at
                    """,
                    (bot_id, name, value, now, now),
                )

    def get_bot_env_vars(self, bot_id: int) -> dict[str, str]:
        with self.session() as conn:
            rows = conn.execute(
                "SELECT name, value FROM bot_env_vars WHERE bot_id = ? ORDER BY name",
                (bot_id,),
            ).fetchall()
            return {str(row["name"]): str(row["value"]) for row in rows}

    def running_bots(self) -> list[sqlite3.Row]:
        with self.session() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM bots WHERE status = 'running' AND deleted_at IS NULL ORDER BY id"
                ).fetchall()
            )

    def update_bot_status(
        self, bot_id: int, status: str, pid: int | None = None
    ) -> None:
        now = int(time.time())
        with self.session() as conn:
            conn.execute(
                "UPDATE bots SET status = ?, pid = ?, updated_at = ? WHERE id = ?",
                (status, pid, now, bot_id),
            )

    def mark_started(self, bot_id: int, pid: int) -> None:
        now = int(time.time())
        with self.session() as conn:
            conn.execute(
                """
                UPDATE bots
                SET status = 'running', pid = ?, updated_at = ?, last_started_at = ?
                WHERE id = ?
                """,
                (pid, now, now, bot_id),
            )

    def mark_stopped(self, bot_id: int, status: str = "stopped") -> None:
        now = int(time.time())
        with self.session() as conn:
            conn.execute(
                """
                UPDATE bots
                SET status = ?, pid = NULL, updated_at = ?, last_stopped_at = ?
                WHERE id = ?
                """,
                (status, now, now, bot_id),
            )

    def update_bot_prompt(self, bot_id: int, prompt: str) -> None:
        now = int(time.time())
        with self.session() as conn:
            conn.execute(
                "UPDATE bots SET prompt = ?, updated_at = ? WHERE id = ?",
                (prompt, now, bot_id),
            )

    def update_bot_workdir(self, bot_id: int, workdir: Path) -> None:
        now = int(time.time())
        with self.session() as conn:
            conn.execute(
                "UPDATE bots SET workdir = ?, updated_at = ? WHERE id = ?",
                (str(workdir), now, bot_id),
            )

    def update_bot_username(self, bot_id: int, bot_username: str | None) -> None:
        now = int(time.time())
        with self.session() as conn:
            conn.execute(
                "UPDATE bots SET bot_username = ?, updated_at = ? WHERE id = ?",
                (bot_username, now, bot_id),
            )

    def soft_delete_bot(self, bot_id: int) -> None:
        now = int(time.time())
        with self.session() as conn:
            conn.execute(
                """
                UPDATE bots
                SET status = 'deleted',
                    pid = NULL,
                    deleted_at = ?,
                    updated_at = ?,
                    token = CASE
                        WHEN token LIKE '__deleted__:%' THEN token
                        ELSE '__deleted__:' || id || ':' || token
                    END
                WHERE id = ?
                """,
                (now, now, bot_id),
            )

    def add_log(
        self, bot_id: int, stream: str, line: str, keep_rows: int = 2000
    ) -> None:
        now = int(time.time())
        with self.session() as conn:
            conn.execute(
                "INSERT INTO logs (bot_id, stream, line, created_at) VALUES (?, ?, ?, ?)",
                (bot_id, stream, line[-4000:], now),
            )
            conn.execute(
                """
                DELETE FROM logs
                WHERE bot_id = ?
                  AND id NOT IN (
                    SELECT id FROM logs WHERE bot_id = ? ORDER BY id DESC LIMIT ?
                  )
                """,
                (bot_id, bot_id, keep_rows),
            )

    def get_logs(self, bot_id: int, limit: int = 30) -> list[sqlite3.Row]:
        with self.session() as conn:
            rows = conn.execute(
                """
                SELECT * FROM logs
                WHERE bot_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (bot_id, limit),
            ).fetchall()
            return list(reversed(rows))

    def set_many_stopped(self, bot_ids: Iterable[int]) -> None:
        ids = list(bot_ids)
        if not ids:
            return
        now = int(time.time())
        placeholders = ",".join("?" for _ in ids)
        with self.session() as conn:
            conn.execute(
                f"""
                UPDATE bots
                SET status = 'stopped', pid = NULL, updated_at = ?, last_stopped_at = ?
                WHERE id IN ({placeholders})
                """,
                [now, now, *ids],
            )
