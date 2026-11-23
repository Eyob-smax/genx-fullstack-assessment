"""
SQLite persistence layer.

Design notes
------------
* One long-lived ``sqlite3`` connection per process, owned by a dedicated
  single-thread executor (instead of opening/closing a connection per query on the
  request path). Every logical operation (e.g. "insert-or-get + commit") is a single
  hop to that thread, so the event loop never blocks on disk I/O and writes from this
  process are naturally serialized without extra locks. (aiosqlite uses the same
  thread-per-connection model but costs one thread hop per execute/fetch/commit call,
  4-5 hops per insert, which showed up as tail latency under burst load.)
* WAL journal mode: readers never block the writer; with ``synchronous=NORMAL`` a
  commit is an append to the WAL rather than an fsync of the main DB.
* ``busy_timeout`` so that if more than one process ever shares the file
  (e.g. ``--workers 4``) writers wait for the lock instead of failing immediately.
* Idempotency is enforced by the database itself: ``idempotency_key`` is UNIQUE and
  inserts use ``ON CONFLICT DO NOTHING``. Exactly one of N concurrent identical
  requests can create the row, regardless of timing or how many processes race.
* Delivery ownership is a conditional UPDATE ("claim with lease"), so a row can only
  be in flight to the downstream from one worker at a time.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, TypeVar

T = TypeVar("T")

DB_PATH = os.getenv("WEBHOOK_DB_PATH", "webhooks.db")

# Status lifecycle:
#   pending -> delivering -> delivered
#                  |  ^
#                  v  |
#               retrying  -> failed (dead-letter: max attempts or non-retryable error)
STATUS_PENDING = "pending"
STATUS_DELIVERING = "delivering"
STATUS_RETRYING = "retrying"
STATUS_DELIVERED = "delivered"
STATUS_FAILED = "failed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key  TEXT NOT NULL UNIQUE,
    request_hash     TEXT NOT NULL,
    event_id         TEXT NOT NULL,
    tenant_id        TEXT NOT NULL,
    cleaned_text     TEXT NOT NULL,
    fingerprint      TEXT,
    status           TEXT NOT NULL DEFAULT 'pending',
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  REAL NOT NULL DEFAULT 0,
    lease_until      REAL NOT NULL DEFAULT 0,
    last_error       TEXT,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_events_due ON events (status, next_attempt_at);
"""


@dataclass(frozen=True)
class EventRow:
    id: int
    idempotency_key: str
    request_hash: str
    event_id: str
    tenant_id: str
    cleaned_text: str
    fingerprint: str | None
    status: str
    attempts: int


@dataclass(frozen=True)
class InsertResult:
    row_id: int
    created: bool
    status: str
    request_hash: str


_ROW_COLUMNS = (
    "id, idempotency_key, request_hash, event_id, tenant_id, "
    "cleaned_text, fingerprint, status, attempts"
)

_DUE_PREDICATE = """
    (status IN ('pending', 'retrying') AND next_attempt_at <= :now)
 OR (status = 'delivering' AND lease_until < :now)
"""


class Database:
    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("WEBHOOK_DB_PATH", DB_PATH)
        self._executor: ThreadPoolExecutor | None = None
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------- plumbing
    async def _run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        if self._executor is None or self._conn is None:
            raise RuntimeError("Database not connected")
        conn = self._conn
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn, conn)

    async def connect(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sqlite-writer")

        def _open() -> sqlite3.Connection:
            # Autocommit mode: every write function below opens its own explicit
            # transaction (or is a single atomic statement).
            conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(SCHEMA)
            return conn

        self._conn = await asyncio.get_running_loop().run_in_executor(self._executor, _open)

    async def close(self) -> None:
        if self._conn is not None:
            await self._run(lambda c: c.close())
            self._conn = None
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None

    # --------------------------------------------------------------- ingest
    async def insert_or_get(
        self,
        *,
        idempotency_key: str,
        request_hash: str,
        event_id: str,
        tenant_id: str,
        cleaned_text: str,
    ) -> InsertResult:
        """Atomically create the event, or return the existing one for this key."""

        def op(c: sqlite3.Connection) -> InsertResult:
            inserted = c.execute(
                """
                INSERT INTO events (idempotency_key, request_hash, event_id, tenant_id, cleaned_text, status)
                VALUES (?, ?, ?, ?, ?, 'pending')
                ON CONFLICT(idempotency_key) DO NOTHING
                RETURNING id
                """,
                (idempotency_key, request_hash, event_id, tenant_id, cleaned_text),
            ).fetchall()  # fully step the statement so the autocommit completes now
            if inserted:
                return InsertResult(inserted[0][0], True, STATUS_PENDING, request_hash)
            existing = c.execute(
                "SELECT id, status, request_hash FROM events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            return InsertResult(existing[0], False, existing[1], existing[2])

        return await self._run(op)

    # ------------------------------------------------------------- delivery
    async def claim(self, row_id: int, lease_seconds: float) -> EventRow | None:
        """
        Take exclusive ownership of a row for delivery (single atomic UPDATE).
        Returns None if the row is already delivered/failed, not yet due, or
        currently leased by another worker/process.
        """

        def op(c: sqlite3.Connection) -> EventRow | None:
            now = time.time()
            row = c.execute(
                f"""
                UPDATE events
                   SET status = 'delivering', lease_until = :lease, updated_at = CURRENT_TIMESTAMP
                 WHERE id = :id AND ({_DUE_PREDICATE})
                RETURNING {_ROW_COLUMNS}
                """,
                {"lease": now + lease_seconds, "id": row_id, "now": now},
            ).fetchall()  # fully step the statement so the autocommit completes now
            return EventRow(*row[0]) if row else None

        return await self._run(op)

    async def set_fingerprint(self, row_id: int, fingerprint: str) -> None:
        await self._run(
            lambda c: c.execute(
                "UPDATE events SET fingerprint = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (fingerprint, row_id),
            )
        )

    async def mark_delivered(self, row_id: int) -> None:
        await self._run(
            lambda c: c.execute(
                """
                UPDATE events
                   SET status = 'delivered', attempts = attempts + 1, lease_until = 0,
                       last_error = NULL, updated_at = CURRENT_TIMESTAMP
                 WHERE id = ?
                """,
                (row_id,),
            )
        )

    async def schedule_retry(self, row_id: int, next_attempt_at: float, error: str) -> None:
        await self._run(
            lambda c: c.execute(
                """
                UPDATE events
                   SET status = 'retrying', attempts = attempts + 1, next_attempt_at = ?,
                       lease_until = 0, last_error = ?, updated_at = CURRENT_TIMESTAMP
                 WHERE id = ?
                """,
                (next_attempt_at, error[:500], row_id),
            )
        )

    async def mark_failed(self, row_id: int, error: str) -> None:
        await self._run(
            lambda c: c.execute(
                """
                UPDATE events
                   SET status = 'failed', attempts = attempts + 1, lease_until = 0,
                       last_error = ?, updated_at = CURRENT_TIMESTAMP
                 WHERE id = ?
                """,
                (error[:500], row_id),
            )
        )

    async def due_ids(self, limit: int = 500) -> list[int]:
        """Rows needing (re)delivery: new, due retries, or expired leases (crash recovery)."""

        def op(c: sqlite3.Connection) -> list[int]:
            rows = c.execute(
                f"SELECT id FROM events WHERE {_DUE_PREDICATE} ORDER BY id LIMIT :limit",
                {"now": time.time(), "limit": limit},
            ).fetchall()
            return [r[0] for r in rows]

        return await self._run(op)

    async def get(self, row_id: int) -> EventRow | None:
        def op(c: sqlite3.Connection) -> EventRow | None:
            row = c.execute(f"SELECT {_ROW_COLUMNS} FROM events WHERE id = ?", (row_id,)).fetchone()
            return EventRow(*row) if row else None

        return await self._run(op)


async def init_db(db_path: str | None = None) -> None:
    """Backwards-compatible helper: create the schema and exit."""
    db = Database(db_path)
    await db.connect()
    await db.close()
