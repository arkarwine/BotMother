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
    preferred_locale TEXT,
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

CREATE TABLE IF NOT EXISTS credit_accounts (
    user_id INTEGER PRIMARY KEY,
    balance INTEGER NOT NULL DEFAULT 0,
    free_grant_issued INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS credit_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    kind TEXT NOT NULL,
    action TEXT NOT NULL,
    reservation_id INTEGER,
    bot_id INTEGER,
    note TEXT,
    actor_user_id INTEGER,
    created_at INTEGER NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS credit_reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    bot_id INTEGER,
    note TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS credit_runtime_state (
    user_id INTEGER PRIMARY KEY,
    accumulated_seconds INTEGER NOT NULL DEFAULT 0,
    last_metered_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_bots_owner ON bots(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_bots_status ON bots(status);
CREATE INDEX IF NOT EXISTS idx_logs_bot_id ON logs(bot_id, id);
CREATE INDEX IF NOT EXISTS idx_credit_ledger_user ON credit_ledger(user_id, id);
CREATE INDEX IF NOT EXISTS idx_credit_reservations_user ON credit_reservations(user_id, status);
"""


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.session() as conn:
            conn.executescript(SCHEMA)
            self._ensure_bot_username_column(conn)
            self._ensure_user_preferred_locale_column(conn)

    def _ensure_bot_username_column(self, conn: sqlite3.Connection) -> None:
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(bots)").fetchall()
        }
        if "bot_username" not in columns:
            conn.execute("ALTER TABLE bots ADD COLUMN bot_username TEXT")

    def _ensure_user_preferred_locale_column(self, conn: sqlite3.Connection) -> None:
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }
        if "preferred_locale" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN preferred_locale TEXT")

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
                INSERT INTO users (user_id, username, first_name, last_name, preferred_locale, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name,
                    last_name=excluded.last_name,
                    last_seen_at=excluded.last_seen_at
                """,
                (user_id, username, first_name, last_name, now, now),
            )

    def get_user_locale(self, user_id: int) -> str | None:
        with self.session() as conn:
            row = conn.execute(
                "SELECT preferred_locale FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                return None
            value = str(row["preferred_locale"] or "").strip()
            return value or None

    def update_user_locale(self, user_id: int, preferred_locale: str | None) -> None:
        now = int(time.time())
        with self.session() as conn:
            conn.execute(
                """
                UPDATE users
                SET preferred_locale = ?,
                    last_seen_at = ?
                WHERE user_id = ?
                """,
                (preferred_locale, now, user_id),
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

    def count_revisions(self, bot_id: int) -> int:
        with self.session() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM revisions WHERE bot_id = ?",
                (bot_id,),
            ).fetchone()
            return int(row["count"] if row is not None else 0)

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

    def _ensure_credit_account_conn(
        self, conn: sqlite3.Connection, user_id: int, initial_free: int
    ) -> sqlite3.Row:
        now = int(time.time())
        row = conn.execute(
            "SELECT * FROM credit_accounts WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is not None:
            return row

        grant = max(0, int(initial_free))
        conn.execute(
            """
            INSERT OR IGNORE INTO users
                (user_id, username, first_name, last_name, preferred_locale, first_seen_at, last_seen_at)
            VALUES (?, NULL, NULL, NULL, NULL, ?, ?)
            """,
            (user_id, now, now),
        )
        conn.execute(
            """
            INSERT INTO credit_accounts
                (user_id, balance, free_grant_issued, created_at, updated_at)
            VALUES (?, ?, 1, ?, ?)
            """,
            (user_id, grant, now, now),
        )
        conn.execute(
            """
            INSERT INTO credit_ledger
                (user_id, amount, balance_after, kind, action, note, created_at)
            VALUES (?, ?, ?, 'grant', 'initial_free', 'One-time free credits', ?)
            """,
            (user_id, grant, grant, now),
        )
        return conn.execute(
            "SELECT * FROM credit_accounts WHERE user_id = ?", (user_id,)
        ).fetchone()

    def ensure_credit_account(
        self, user_id: int, initial_free: int
    ) -> sqlite3.Row:
        with self.session() as conn:
            return self._ensure_credit_account_conn(conn, user_id, initial_free)

    def credit_balance(self, user_id: int, initial_free: int) -> int:
        with self.session() as conn:
            row = self._ensure_credit_account_conn(conn, user_id, initial_free)
            return int(row["balance"])

    def reserve_credits(
        self,
        user_id: int,
        amount: int,
        action: str,
        initial_free: int,
        bot_id: int | None = None,
        note: str | None = None,
    ) -> tuple[int | None, int]:
        amount = max(0, int(amount))
        with self.session() as conn:
            row = self._ensure_credit_account_conn(conn, user_id, initial_free)
            balance = int(row["balance"])
            if amount == 0:
                return None, balance
            if balance < amount:
                return None, balance
            now = int(time.time())
            new_balance = balance - amount
            cur = conn.execute(
                """
                INSERT INTO credit_reservations
                    (user_id, amount, action, status, bot_id, note, created_at, updated_at)
                VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)
                """,
                (user_id, amount, action, bot_id, note, now, now),
            )
            reservation_id = int(cur.lastrowid)
            conn.execute(
                "UPDATE credit_accounts SET balance = ?, updated_at = ? WHERE user_id = ?",
                (new_balance, now, user_id),
            )
            conn.execute(
                """
                INSERT INTO credit_ledger
                    (user_id, amount, balance_after, kind, action, reservation_id,
                     bot_id, note, created_at)
                VALUES (?, ?, ?, 'debit', ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    -amount,
                    new_balance,
                    action,
                    reservation_id,
                    bot_id,
                    note,
                    now,
                ),
            )
            return reservation_id, new_balance

    def settle_credit_reservation(
        self, reservation_id: int | None, bot_id: int | None = None, note: str | None = None
    ) -> bool:
        if reservation_id is None:
            return False
        now = int(time.time())
        with self.session() as conn:
            row = conn.execute(
                "SELECT * FROM credit_reservations WHERE id = ?",
                (reservation_id,),
            ).fetchone()
            if row is None or row["status"] != "pending":
                return False
            conn.execute(
                """
                UPDATE credit_reservations
                SET status = 'settled',
                    bot_id = COALESCE(?, bot_id),
                    note = COALESCE(?, note),
                    updated_at = ?
                WHERE id = ?
                """,
                (bot_id, note, now, reservation_id),
            )
            return True

    def refund_credit_reservation(
        self, reservation_id: int | None, note: str | None = None
    ) -> bool:
        if reservation_id is None:
            return False
        now = int(time.time())
        with self.session() as conn:
            row = conn.execute(
                "SELECT * FROM credit_reservations WHERE id = ?",
                (reservation_id,),
            ).fetchone()
            if row is None or row["status"] != "pending":
                return False
            account = conn.execute(
                "SELECT * FROM credit_accounts WHERE user_id = ?", (row["user_id"],)
            ).fetchone()
            if account is None:
                return False
            amount = int(row["amount"])
            new_balance = int(account["balance"]) + amount
            conn.execute(
                """
                UPDATE credit_reservations
                SET status = 'refunded', note = COALESCE(?, note), updated_at = ?
                WHERE id = ?
                """,
                (note, now, reservation_id),
            )
            conn.execute(
                "UPDATE credit_accounts SET balance = ?, updated_at = ? WHERE user_id = ?",
                (new_balance, now, row["user_id"]),
            )
            conn.execute(
                """
                INSERT INTO credit_ledger
                    (user_id, amount, balance_after, kind, action, reservation_id,
                     bot_id, note, created_at)
                VALUES (?, ?, ?, 'refund', ?, ?, ?, ?, ?)
                """,
                (
                    row["user_id"],
                    amount,
                    new_balance,
                    row["action"],
                    reservation_id,
                    row["bot_id"],
                    note,
                    now,
                ),
            )
            return True

    def grant_credits(
        self,
        user_id: int,
        amount: int,
        actor_user_id: int | None,
        initial_free: int,
        note: str | None = None,
        kind: str = "grant",
    ) -> int:
        amount = int(amount)
        with self.session() as conn:
            row = self._ensure_credit_account_conn(conn, user_id, initial_free)
            now = int(time.time())
            new_balance = int(row["balance"]) + amount
            conn.execute(
                "UPDATE credit_accounts SET balance = ?, updated_at = ? WHERE user_id = ?",
                (new_balance, now, user_id),
            )
            conn.execute(
                """
                INSERT INTO credit_ledger
                    (user_id, amount, balance_after, kind, action, note,
                     actor_user_id, created_at)
                VALUES (?, ?, ?, ?, 'admin_adjustment', ?, ?, ?)
                """,
                (user_id, amount, new_balance, kind, note, actor_user_id, now),
            )
            return new_balance

    def set_credit_balance(
        self,
        user_id: int,
        balance: int,
        actor_user_id: int | None,
        initial_free: int,
        note: str | None = None,
    ) -> int:
        balance = max(0, int(balance))
        with self.session() as conn:
            row = self._ensure_credit_account_conn(conn, user_id, initial_free)
            amount = balance - int(row["balance"])
            now = int(time.time())
            conn.execute(
                "UPDATE credit_accounts SET balance = ?, updated_at = ? WHERE user_id = ?",
                (balance, now, user_id),
            )
            conn.execute(
                """
                INSERT INTO credit_ledger
                    (user_id, amount, balance_after, kind, action, note,
                     actor_user_id, created_at)
                VALUES (?, ?, ?, 'set', 'admin_adjustment', ?, ?, ?)
                """,
                (user_id, amount, balance, note, actor_user_id, now),
            )
            return balance

    def credit_ledger_for_user(
        self, user_id: int, limit: int = 20
    ) -> list[sqlite3.Row]:
        with self.session() as conn:
            return list(
                conn.execute(
                    """
                    SELECT * FROM credit_ledger
                    WHERE user_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (user_id, limit),
                ).fetchall()
            )

    def recent_credit_ledger(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.session() as conn:
            return list(
                conn.execute(
                    """
                    SELECT l.*, u.username, u.first_name, u.last_name
                    FROM credit_ledger l
                    LEFT JOIN users u ON u.user_id = l.user_id
                    ORDER BY l.id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            )

    def credit_summary(self) -> sqlite3.Row:
        with self.session() as conn:
            return conn.execute(
                """
                SELECT
                    (SELECT COALESCE(SUM(amount), 0) FROM credit_ledger WHERE amount > 0) AS total_issued,
                    (SELECT COALESCE(SUM(-amount), 0) FROM credit_ledger WHERE amount < 0) AS total_spent,
                    (SELECT COALESCE(SUM(-amount), 0) FROM credit_ledger WHERE action = 'runtime' AND amount < 0) AS runtime_spent,
                    (SELECT COUNT(*) FROM credit_accounts WHERE balance <= 5) AS low_balance_users,
                    (SELECT COUNT(*) FROM credit_accounts) AS account_count
                """
            ).fetchone()

    def list_credit_accounts(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.session() as conn:
            return list(
                conn.execute(
                    """
                    SELECT a.*, u.username, u.first_name, u.last_name
                    FROM credit_accounts a
                    LEFT JOIN users u ON u.user_id = a.user_id
                    ORDER BY a.balance ASC, a.updated_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            )

    def accrue_runtime_credits(
        self,
        user_id: int,
        bot_count: int,
        now: int,
        seconds_per_credit: int,
        initial_free: int,
    ):
        from .credits import RuntimeChargeResult

        bot_count = max(0, int(bot_count))
        seconds_per_credit = max(1, int(seconds_per_credit))
        with self.session() as conn:
            account = self._ensure_credit_account_conn(conn, user_id, initial_free)
            state = conn.execute(
                "SELECT * FROM credit_runtime_state WHERE user_id = ?", (user_id,)
            ).fetchone()
            if state is None:
                conn.execute(
                    """
                    INSERT INTO credit_runtime_state
                        (user_id, accumulated_seconds, last_metered_at, updated_at)
                    VALUES (?, 0, ?, ?)
                    """,
                    (user_id, now, now),
                )
                return RuntimeChargeResult(user_id, 0, int(account["balance"]), False, 0)

            elapsed = max(0, now - int(state["last_metered_at"]))
            total_seconds = int(state["accumulated_seconds"]) + (elapsed * bot_count)
            due = total_seconds // seconds_per_credit
            remainder = total_seconds % seconds_per_credit
            balance = int(account["balance"])
            if due <= 0:
                conn.execute(
                    """
                    UPDATE credit_runtime_state
                    SET accumulated_seconds = ?, last_metered_at = ?, updated_at = ?
                    WHERE user_id = ?
                    """,
                    (total_seconds, now, now, user_id),
                )
                return RuntimeChargeResult(user_id, 0, balance, False, 0)

            charged = min(balance, due)
            new_balance = balance - charged
            if charged:
                conn.execute(
                    "UPDATE credit_accounts SET balance = ?, updated_at = ? WHERE user_id = ?",
                    (new_balance, now, user_id),
                )
                conn.execute(
                    """
                    INSERT INTO credit_ledger
                        (user_id, amount, balance_after, kind, action, note, created_at)
                    VALUES (?, ?, ?, 'debit', 'runtime', ?, ?)
                    """,
                    (
                        user_id,
                        -charged,
                        new_balance,
                        f"Runtime: {bot_count} running bot(s)",
                        now,
                    ),
                )

            should_stop = charged < due
            stored_seconds = seconds_per_credit if should_stop else remainder
            conn.execute(
                """
                UPDATE credit_runtime_state
                SET accumulated_seconds = ?, last_metered_at = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (stored_seconds, now, now, user_id),
            )
            return RuntimeChargeResult(user_id, charged, new_balance, should_stop, int(due))

    def reset_runtime_meter_for_users(self, user_ids: Iterable[int], now: int | None = None) -> None:
        ids = sorted({int(user_id) for user_id in user_ids})
        if not ids:
            return
        timestamp = int(time.time()) if now is None else int(now)
        placeholders = ",".join("?" for _ in ids)
        with self.session() as conn:
            conn.execute(
                f"""
                UPDATE credit_runtime_state
                SET last_metered_at = ?, updated_at = ?
                WHERE user_id IN ({placeholders})
                """,
                [timestamp, timestamp, *ids],
            )

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
