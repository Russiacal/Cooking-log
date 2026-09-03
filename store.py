"""
SQLite persistence for the cooking-log server.

Two logical stores in one DB file:
- OAuthTokenStore: access tokens issued to Claude's connector. Previously
  in-memory, which meant Railway restarts kicked users through the OAuth
  handshake every few days. Now persistent.
- PhotoQueueStore: pending photo URLs pushed by the iOS Shortcut, drained
  when publish_cook is called without an explicit `photos` argument.

Pattern reused from ~/my-agents/email-triage/cache.py — same threaded
sqlite3 setup, idempotent schema creation via CREATE TABLE IF NOT EXISTS.

Path controlled by SQLITE_PATH env var. On Railway, this should point
inside an attached volume (e.g. /data/cooking-log.db) so the file
survives container restarts. Locally, default is ./cooking-log.db.
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_tokens (
  token TEXT PRIMARY KEY,
  client_id TEXT NOT NULL,
  issued_at REAL NOT NULL,
  expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tokens_expires ON oauth_tokens(expires_at);

CREATE TABLE IF NOT EXISTS pending_photos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT UNIQUE NOT NULL,
  uploaded_at REAL NOT NULL,
  consumed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_photos_unconsumed
  ON pending_photos(uploaded_at) WHERE consumed_at IS NULL;
"""


def _default_db_path() -> str:
    return os.environ.get("SQLITE_PATH", "./cooking-log.db")


def _connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def _cursor(conn: sqlite3.Connection) -> Iterator[sqlite3.Cursor]:
    cur = conn.cursor()
    try:
        yield cur
    finally:
        cur.close()


class OAuthTokenStore:
    """Persistent replacement for the token half of oauth.OAuthStore."""

    def __init__(self, conn: sqlite3.Connection, default_ttl_seconds: int):
        self._conn = conn
        self._ttl = default_ttl_seconds

    def issue(self, token: str, client_id: str) -> None:
        now = time.time()
        with _cursor(self._conn) as c:
            c.execute(
                "INSERT INTO oauth_tokens (token, client_id, issued_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (token, client_id, now, now + self._ttl),
            )

    def validate(self, token: str) -> bool:
        now = time.time()
        with _cursor(self._conn) as c:
            row = c.execute(
                "SELECT expires_at FROM oauth_tokens WHERE token = ?", (token,)
            ).fetchone()
        return row is not None and row[0] >= now

    def purge_expired(self) -> int:
        """Housekeeping. Not called automatically; run periodically if you care."""
        with _cursor(self._conn) as c:
            c.execute("DELETE FROM oauth_tokens WHERE expires_at < ?", (time.time(),))
            return c.rowcount


class PhotoQueueStore:
    """Queue of photo URLs uploaded via iOS Shortcut, awaiting publish_cook."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def add(self, urls: list[str], uploaded_at: Optional[float] = None) -> int:
        """Insert URLs. Returns count newly added (dedup on url)."""
        ts = uploaded_at if uploaded_at is not None else time.time()
        added = 0
        with _cursor(self._conn) as c:
            for url in urls:
                try:
                    c.execute(
                        "INSERT INTO pending_photos (url, uploaded_at) VALUES (?, ?)",
                        (url, ts),
                    )
                    added += 1
                except sqlite3.IntegrityError:
                    # Duplicate URL — Shortcut retry or same photo shared twice.
                    pass
        return added

    def list_unconsumed(self) -> list[str]:
        with _cursor(self._conn) as c:
            rows = c.execute(
                "SELECT url FROM pending_photos WHERE consumed_at IS NULL "
                "ORDER BY uploaded_at ASC, id ASC"
            ).fetchall()
        return [r[0] for r in rows]

    def consume_all(self) -> list[str]:
        """Atomic: fetch all unconsumed URLs AND mark them consumed."""
        now = time.time()
        with _cursor(self._conn) as c:
            rows = c.execute(
                "SELECT url FROM pending_photos WHERE consumed_at IS NULL "
                "ORDER BY uploaded_at ASC, id ASC"
            ).fetchall()
            if rows:
                c.execute(
                    "UPDATE pending_photos SET consumed_at = ? WHERE consumed_at IS NULL",
                    (now,),
                )
        return [r[0] for r in rows]

    def count_unconsumed(self) -> int:
        with _cursor(self._conn) as c:
            row = c.execute(
                "SELECT COUNT(*) FROM pending_photos WHERE consumed_at IS NULL"
            ).fetchone()
        return int(row[0])


def build_stores(
    token_ttl_seconds: int,
) -> tuple[sqlite3.Connection, OAuthTokenStore, PhotoQueueStore]:
    conn = _connect(_default_db_path())
    conn.executescript(SCHEMA)
    return conn, OAuthTokenStore(conn, token_ttl_seconds), PhotoQueueStore(conn)
