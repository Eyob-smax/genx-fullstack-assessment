"""
Background delivery pipeline.

    POST /webhooks/ingest ──► SQLite row (status=pending) ──► asyncio.Queue[row_id]
                                                                   │
                       N async worker tasks (bounded concurrency) ◄┘
                         1. claim row (conditional UPDATE + lease)
                         2. fingerprint in ProcessPoolExecutor (CPU-bound, off the loop)
                         3. POST downstream via shared httpx client
                         4. delivered | retrying (backoff + jitter) | failed

The database, not the in-memory queue, is the source of truth. The queue is only a
low-latency wake-up mechanism; a periodic sweeper re-enqueues anything that is due
(scheduled retries, rows left over from a crash, expired leases).
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from concurrent.futures import Executor

import httpx

from app.database import Database
from app.downstream import DeliveryError, forward_event
from app.processor import compute_fingerprint

log = logging.getLogger("webhooks.worker")


class DeliveryService:
    def __init__(
        self,
        db: Database,
        executor: Executor,
        client: httpx.AsyncClient,
        *,
        concurrency: int = int(os.getenv("DELIVERY_CONCURRENCY", "16")),
        max_attempts: int = int(os.getenv("DELIVERY_MAX_ATTEMPTS", "12")),
        base_backoff: float = float(os.getenv("DELIVERY_BASE_BACKOFF", "0.25")),
        max_backoff: float = float(os.getenv("DELIVERY_MAX_BACKOFF", "30")),
        lease_seconds: float = 60.0,
        sweep_interval: float = 2.0,
        downstream_url: str | None = None,
    ):
        self.db = db
        self.executor = executor
        self.client = client
        self.concurrency = concurrency
        self.max_attempts = max_attempts
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.lease_seconds = lease_seconds
        self.sweep_interval = sweep_interval
        self.downstream_url = downstream_url

        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._queued: set[int] = set()  # dedupe: a row is in the queue at most once
        self._tasks: list[asyncio.Task] = []
        self._timers: set[asyncio.TimerHandle] = set()

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        for i in range(self.concurrency):
            self._tasks.append(asyncio.create_task(self._worker(), name=f"delivery-worker-{i}"))
        self._tasks.append(asyncio.create_task(self._sweeper(), name="delivery-sweeper"))
        # Crash recovery: anything accepted but not yet delivered is picked up again.
        for row_id in await self.db.due_ids():
            self.enqueue(row_id)

    async def stop(self) -> None:
        for timer in self._timers:
            timer.cancel()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def enqueue(self, row_id: int) -> None:
        if row_id in self._queued:
            return
        self._queued.add(row_id)
        self._queue.put_nowait(row_id)

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    # -------------------------------------------------------------- workers
    async def _worker(self) -> None:
        while True:
            row_id = await self._queue.get()
            self._queued.discard(row_id)
            try:
                await self._process(row_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # never let one bad row kill a worker
                log.exception("unexpected error processing row %s", row_id)
            finally:
                self._queue.task_done()

    async def _sweeper(self) -> None:
        while True:
            await asyncio.sleep(self.sweep_interval)
            try:
                for row_id in await self.db.due_ids():
                    self.enqueue(row_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("sweeper failed")

    async def _process(self, row_id: int) -> None:
        row = await self.db.claim(row_id, self.lease_seconds)
        if row is None:
            return  # already delivered / failed / owned by another worker / not due yet

        fingerprint = row.fingerprint
        if fingerprint is None:
            loop = asyncio.get_running_loop()
            fingerprint = await loop.run_in_executor(self.executor, compute_fingerprint, row.cleaned_text)
            await self.db.set_fingerprint(row.id, fingerprint)

        try:
            await forward_event(
                self.client,
                event_id=row.event_id,
                cleaned_text=row.cleaned_text,
                fingerprint=fingerprint,
                idempotency_key=row.idempotency_key,
                url=self.downstream_url,
            )
        except DeliveryError as exc:
            attempt = row.attempts + 1
            if not exc.retryable or attempt >= self.max_attempts:
                log.warning("row %s permanently failed after %s attempts: %s", row.id, attempt, exc)
                await self.db.mark_failed(row.id, str(exc))
                return
            delay = self._backoff(attempt, exc.retry_after)
            await self.db.schedule_retry(row.id, time.time() + delay, str(exc))
            self._schedule(row.id, delay)
            return

        await self.db.mark_delivered(row.id)

    # -------------------------------------------------------------- helpers
    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        """Exponential backoff with full jitter, capped; honours Retry-After if larger."""
        exp = min(self.max_backoff, self.base_backoff * (2 ** (attempt - 1)))
        delay = random.uniform(exp / 2, exp)
        if retry_after is not None:
            delay = max(delay, min(retry_after, self.max_backoff))
        return delay

    def _schedule(self, row_id: int, delay: float) -> None:
        loop = asyncio.get_running_loop()
        handle: asyncio.TimerHandle

        def fire() -> None:
            self._timers.discard(handle)
            self.enqueue(row_id)

        handle = loop.call_later(delay, fire)
        self._timers.add(handle)
