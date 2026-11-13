"""SQLite durability primitives.

One process, two threads (the dialling loop and the webhook server) share a
single connection guarded by a re-entrant lock.  Every state transition is a
short ``BEGIN IMMEDIATE`` transaction, so a ``kill -9`` leaves the database on
either side of a complete transaction - never in between.

Design note: we deliberately keep the *dial intent* as a committed row **before**
the HTTP POST.  That is what makes "crashed between decided and dialled" safe.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence


SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    campaign_id           TEXT NOT NULL,
    lead_id               TEXT NOT NULL,
    tenant_id             TEXT NOT NULL,
    phone                 TEXT NOT NULL,
    attempts              INTEGER NOT NULL DEFAULT 0,
    kind                  TEXT NOT NULL DEFAULT 'fresh',
    pending_at            TEXT,
    final_state           TEXT,
    terminal_disposition  TEXT,
    last_status           TEXT,
    last_disposition      TEXT,
    last_ended_at         TEXT,
    PRIMARY KEY (campaign_id, lead_id)
);

CREATE TABLE IF NOT EXISTS calls (
    request_id    TEXT PRIMARY KEY,
    call_sid      TEXT UNIQUE,
    campaign_id   TEXT NOT NULL,
    lead_id       TEXT NOT NULL,
    tenant_id     TEXT NOT NULL,
    phone         TEXT NOT NULL,
    attempt_no    INTEGER NOT NULL,
    accepted      INTEGER NOT NULL DEFAULT 0,
    state         TEXT NOT NULL,            -- intent | active | ended
    status        TEXT,
    disposition   TEXT,
    callback_at   TEXT,
    started_at    TEXT,
    ended_at      TEXT,
    created_sim   TEXT,
    created_mono  REAL,
    response_seen INTEGER NOT NULL DEFAULT 0,
    place_error   TEXT
);
CREATE INDEX IF NOT EXISTS calls_lead   ON calls(campaign_id, lead_id);
CREATE INDEX IF NOT EXISTS calls_state  ON calls(state);
CREATE INDEX IF NOT EXISTS calls_phone  ON calls(phone);

CREATE TABLE IF NOT EXISTS dnd_phones (
    phone  TEXT PRIMARY KEY,
    sim_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,  # explicit transactions
            timeout=30.0,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        # FULL is overkill for a local album, but the write volume is tiny and
        # it removes any doubt about durability across kill -9.
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")
        with self.lock:
            self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Serialise a read-modify-write transaction across both threads."""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    # -- small helpers used inside transactions -----------------------------
    @staticmethod
    def q(conn: sqlite3.Connection, sql: str, args: Sequence = ()) -> list[sqlite3.Row]:
        return list(conn.execute(sql, args))

    @staticmethod
    def q1(conn: sqlite3.Connection, sql: str, args: Sequence = ()) -> sqlite3.Row | None:
        return conn.execute(sql, args).fetchone()

    @staticmethod
    def x(conn: sqlite3.Connection, sql: str, args: Sequence = ()) -> None:
        conn.execute(sql, args)

    def close(self) -> None:
        with self.lock:
            self.conn.close()
