from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from botmother.db import Database

logger = logging.getLogger(__name__)

MGMT_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS mgmt_admins (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    last_name   TEXT,
    role        TEXT NOT NULL DEFAULT 'admin',
    added_by    INTEGER NOT NULL,
    added_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS mgmt_broadcasts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_by         INTEGER NOT NULL,
    target_group    TEXT NOT NULL,
    custom_ids      TEXT,
    message_text    TEXT NOT NULL,
    media_file_id   TEXT,
    media_type      TEXT,
    total_targets   INTEGER NOT NULL DEFAULT 0,
    sent_count      INTEGER NOT NULL DEFAULT 0,
    fail_count      INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      INTEGER NOT NULL,
    finished_at     INTEGER
);
"""

ROLE_ADMIN = "admin"
ROLE_B2B = "b2b"
VALID_ROLES = {ROLE_ADMIN, ROLE_B2B}

# Broadcast target group constants
TARGET_ALL_USERS = "all_users"
TARGET_ALL_CHATS = "all_chats"
TARGET_BOT_OWNERS = "bot_owners"
TARGET_CUSTOM = "custom"


class MgmtDatabase:
    """
    Wraps the shared BotMother SQLite file.
    - Adds mgmt_admins and mgmt_broadcasts tables (never touches BotMother tables).
    - Provides read-only views into BotMother data for stats/dashboard/logs.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        Database(self.path).initialize()
        with self.session() as conn:
            conn.executescript(MGMT_SCHEMA)

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

    # ─── Admin / B2B access control ───────────────────────────────────────────

    def is_admin(self, user_id: int) -> bool:
        with self.session() as conn:
            row = conn.execute(
                "SELECT 1 FROM mgmt_admins WHERE user_id = ?", (user_id,)
            ).fetchone()
            return row is not None

    def get_role(self, user_id: int) -> str | None:
        with self.session() as conn:
            row = conn.execute(
                "SELECT role FROM mgmt_admins WHERE user_id = ?", (user_id,)
            ).fetchone()
            return str(row["role"]) if row else None

    def add_admin(
        self,
        user_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
        role: str,
        added_by: int,
    ) -> None:
        now = int(time.time())
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO mgmt_admins
                    (user_id, username, first_name, last_name, role, added_by, added_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name,
                    last_name=excluded.last_name,
                    role=excluded.role,
                    added_by=excluded.added_by,
                    added_at=excluded.added_at
                """,
                (user_id, username, first_name, last_name, role, added_by, now),
            )

    def remove_admin(self, user_id: int) -> int:
        with self.session() as conn:
            cur = conn.execute(
                "DELETE FROM mgmt_admins WHERE user_id = ?", (user_id,)
            )
            return int(cur.rowcount)

    def list_admins(self) -> list[sqlite3.Row]:
        with self.session() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM mgmt_admins ORDER BY role, added_at"
                ).fetchall()
            )

    # ─── Read-only BotMother data ──────────────────────────────────────────────

    def list_all_bots(self, include_deleted: bool = False) -> list[sqlite3.Row]:
        where = "" if include_deleted else "WHERE b.deleted_at IS NULL"
        with self.session() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT b.*, u.username AS owner_username,
                           u.first_name AS owner_first_name,
                           u.last_name AS owner_last_name
                    FROM bots b
                    LEFT JOIN users u ON u.user_id = b.owner_user_id
                    {where}
                    ORDER BY b.id DESC
                    """
                ).fetchall()
            )

    def get_bot(self, bot_id: int) -> sqlite3.Row | None:
        with self.session() as conn:
            return conn.execute(
                """
                SELECT b.*, u.username AS owner_username,
                       u.first_name AS owner_first_name,
                       u.last_name AS owner_last_name
                FROM bots b
                LEFT JOIN users u ON u.user_id = b.owner_user_id
                WHERE b.id = ? AND b.deleted_at IS NULL
                """,
                (bot_id,),
            ).fetchone()

    def list_all_users(self) -> list[sqlite3.Row]:
        with self.session() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM users ORDER BY last_seen_at DESC"
                ).fetchall()
            )

    def get_logs(self, bot_id: int, limit: int = 50) -> list[sqlite3.Row]:
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

    def list_bots_for_owner(self, owner_user_id: int) -> list[sqlite3.Row]:
        with self.session() as conn:
            return list(
                conn.execute(
                    """
                    SELECT b.*, u.username AS owner_username,
                           u.first_name AS owner_first_name,
                           u.last_name AS owner_last_name
                    FROM bots b
                    LEFT JOIN users u ON u.user_id = b.owner_user_id
                    WHERE b.owner_user_id = ? AND b.deleted_at IS NULL
                    ORDER BY b.id DESC
                    """,
                    (owner_user_id,),
                ).fetchall()
            )

    # ─── Broadcast recipients ──────────────────────────────────────────────────

    def get_broadcast_targets(self, group: str) -> list[int]:
        """Return a list of chat_ids or user_ids to broadcast to."""
        with self.session() as conn:
            if group == TARGET_ALL_USERS:
                rows = conn.execute(
                    "SELECT user_id FROM users ORDER BY user_id"
                ).fetchall()
                return [int(r["user_id"]) for r in rows]
            if group == TARGET_ALL_CHATS:
                # Distinct chat_ids from bots table (covers groups too)
                rows = conn.execute(
                    "SELECT DISTINCT chat_id FROM bots WHERE deleted_at IS NULL ORDER BY chat_id"
                ).fetchall()
                return [int(r["chat_id"]) for r in rows]
            if group == TARGET_BOT_OWNERS:
                rows = conn.execute(
                    """
                    SELECT DISTINCT owner_user_id
                    FROM bots
                    WHERE deleted_at IS NULL
                    ORDER BY owner_user_id
                    """
                ).fetchall()
                return [int(r["owner_user_id"]) for r in rows]
            return []

    def get_broadcast_targets_for_owner(self, owner_user_id: int) -> list[int]:
        """Return chat_ids of all chats that ever ran a bot owned by this user."""
        with self.session() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT chat_id
                FROM bots
                WHERE owner_user_id = ? AND deleted_at IS NULL
                ORDER BY chat_id
                """,
                (owner_user_id,),
            ).fetchall()
            return [int(r["chat_id"]) for r in rows]

    # ─── Broadcast log ─────────────────────────────────────────────────────────

    def create_broadcast(
        self,
        sent_by: int,
        target_group: str,
        message_text: str,
        total_targets: int,
        custom_ids: str | None = None,
        media_file_id: str | None = None,
        media_type: str | None = None,
    ) -> int:
        now = int(time.time())
        with self.session() as conn:
            cur = conn.execute(
                """
                INSERT INTO mgmt_broadcasts
                    (sent_by, target_group, custom_ids, message_text,
                     media_file_id, media_type, total_targets, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'sending', ?)
                """,
                (
                    sent_by,
                    target_group,
                    custom_ids,
                    message_text,
                    media_file_id,
                    media_type,
                    total_targets,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def finish_broadcast(
        self, broadcast_id: int, sent_count: int, fail_count: int
    ) -> None:
        now = int(time.time())
        with self.session() as conn:
            conn.execute(
                """
                UPDATE mgmt_broadcasts
                SET sent_count = ?, fail_count = ?, status = 'done', finished_at = ?
                WHERE id = ?
                """,
                (sent_count, fail_count, now, broadcast_id),
            )

    def list_broadcasts(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.session() as conn:
            return list(
                conn.execute(
                    """
                    SELECT b.*, a.username AS sender_username,
                           a.first_name AS sender_first_name
                    FROM mgmt_broadcasts b
                    LEFT JOIN mgmt_admins a ON a.user_id = b.sent_by
                    ORDER BY b.id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            )

    # ─── Credit administration ───────────────────────────────────────────────

    def credit_db(self) -> Database:
        return Database(self.path)

    def credit_balance(self, user_id: int, initial_free: int) -> int:
        return self.credit_db().credit_balance(user_id, initial_free)

    def grant_credits(
        self,
        user_id: int,
        amount: int,
        actor_user_id: int | None,
        initial_free: int,
        note: str | None = None,
    ) -> int:
        return self.credit_db().grant_credits(
            user_id,
            amount,
            actor_user_id,
            initial_free,
            note=note,
        )

    def set_credit_balance(
        self,
        user_id: int,
        balance: int,
        actor_user_id: int | None,
        initial_free: int,
        note: str | None = None,
    ) -> int:
        return self.credit_db().set_credit_balance(
            user_id,
            balance,
            actor_user_id,
            initial_free,
            note=note,
        )

    def credit_ledger_for_user(self, user_id: int, limit: int = 20) -> list[sqlite3.Row]:
        return self.credit_db().credit_ledger_for_user(user_id, limit)

    def recent_credit_ledger(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.credit_db().recent_credit_ledger(limit)

    def credit_summary(self) -> sqlite3.Row:
        return self.credit_db().credit_summary()

    def list_credit_accounts(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.credit_db().list_credit_accounts(limit)
