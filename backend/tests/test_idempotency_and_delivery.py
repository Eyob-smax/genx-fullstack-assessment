"""
Focused tests for the guarantees the burst harness can't isolate:
idempotency under races (in-process and cross-connection), retry/backoff,
dead-lettering, crash recovery and the HTTP contract.
"""

import asyncio
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from app.database import Database
from app.worker import DeliveryService


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "unit.db"))
    await database.connect()
    yield database
    await database.close()


def _insert_kwargs(key: str = "idem-1", text: str = "hello"):
    return dict(idempotency_key=key, request_hash="h", event_id="evt-1", tenant_id="t", cleaned_text=text)


async def _wait_for_status(db: Database, row_id: int, status: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = await db.get(row_id)
        if row and row.status == status:
            return row
        await asyncio.sleep(0.02)
    raise AssertionError(f"row {row_id} never reached {status!r}: {await db.get(row_id)}")


# --------------------------------------------------------------------------- idempotency
async def test_concurrent_identical_inserts_create_exactly_one_row(db):
    results = await asyncio.gather(*(db.insert_or_get(**_insert_kwargs()) for _ in range(50)))
    assert sum(r.created for r in results) == 1
    assert len({r.row_id for r in results}) == 1


async def test_cross_connection_race_creates_one_row(tmp_path):
    """Two independent connections ~= two processes (e.g. uvicorn --workers 2)."""
    path = str(tmp_path / "shared.db")
    a, b = Database(path), Database(path)
    await a.connect()
    await b.connect()
    try:
        results = await asyncio.gather(
            *(conn.insert_or_get(**_insert_kwargs()) for _ in range(20) for conn in (a, b))
        )
        assert sum(r.created for r in results) == 1
        count = sqlite3.connect(path).execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert count == 1
    finally:
        await a.close()
        await b.close()


async def test_claim_is_exclusive(db):
    res = await db.insert_or_get(**_insert_kwargs())
    claims = await asyncio.gather(*(db.claim(res.row_id, lease_seconds=60) for _ in range(10)))
    assert sum(c is not None for c in claims) == 1


# ------------------------------------------------------------------------------ delivery
def _service(db, handler, **kwargs) -> tuple[DeliveryService, ThreadPoolExecutor, httpx.AsyncClient]:
    executor = ThreadPoolExecutor(max_workers=2)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    svc = DeliveryService(
        db, executor, client,
        concurrency=4, base_backoff=0.01, max_backoff=0.05, sweep_interval=0.05,
        downstream_url="http://downstream.test/events", **kwargs,
    )
    return svc, executor, client


async def test_transient_503s_are_retried_until_delivered(db):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["Idempotency-Key"])
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, json={"accepted": True})

    svc, executor, client = _service(db, handler)
    res = await db.insert_or_get(**_insert_kwargs())
    await svc.start()
    try:
        row = await _wait_for_status(db, res.row_id, "delivered")
        assert row.attempts == 3
        assert row.fingerprint is not None
        assert calls == ["idem-1"] * 3  # idempotency key propagated downstream
    finally:
        await svc.stop()
        await client.aclose()
        executor.shutdown()


async def test_transport_errors_are_retried(db):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={})

    svc, executor, client = _service(db, handler)
    res = await db.insert_or_get(**_insert_kwargs())
    await svc.start()
    try:
        await _wait_for_status(db, res.row_id, "delivered")
    finally:
        await svc.stop()
        await client.aclose()
        executor.shutdown()


async def test_non_retryable_4xx_goes_to_failed(db):
    svc, executor, client = _service(db, lambda r: httpx.Response(422))
    res = await db.insert_or_get(**_insert_kwargs())
    await svc.start()
    try:
        row = await _wait_for_status(db, res.row_id, "failed")
        assert row.attempts == 1
    finally:
        await svc.stop()
        await client.aclose()
        executor.shutdown()


async def test_gives_up_after_max_attempts(db):
    svc, executor, client = _service(db, lambda r: httpx.Response(503), max_attempts=3)
    res = await db.insert_or_get(**_insert_kwargs())
    await svc.start()
    try:
        row = await _wait_for_status(db, res.row_id, "failed")
        assert row.attempts == 3
    finally:
        await svc.stop()
        await client.aclose()
        executor.shutdown()


async def test_duplicate_enqueues_deliver_once(db):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    svc, executor, client = _service(db, handler)
    res = await db.insert_or_get(**_insert_kwargs())
    await svc.start()
    try:
        for _ in range(10):
            svc.enqueue(res.row_id)
        await _wait_for_status(db, res.row_id, "delivered")
        await asyncio.sleep(0.2)  # give the sweeper a few cycles to (not) redeliver
        assert calls == 1
    finally:
        await svc.stop()
        await client.aclose()
        executor.shutdown()


async def test_crash_recovery_picks_up_expired_lease(db):
    """A row left in 'delivering' by a crashed process is redelivered once its lease expires."""
    res = await db.insert_or_get(**_insert_kwargs())
    assert await db.claim(res.row_id, lease_seconds=-1)  # simulate crash mid-delivery

    svc, executor, client = _service(db, lambda r: httpx.Response(200, json={}))
    await svc.start()
    try:
        await _wait_for_status(db, res.row_id, "delivered")
    finally:
        await svc.stop()
        await client.aclose()
        executor.shutdown()


# ---------------------------------------------------------------------------- HTTP API
def test_http_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBHOOK_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("DOWNSTREAM_URL", "http://127.0.0.1:1/unreachable")
    monkeypatch.setenv("CPU_POOL_WORKERS", "1")
    from app.main import app

    body = {"event_id": "evt-1", "tenant_id": "t", "content": "mail me at a@example.com, SSN 123-45-6789"}
    with TestClient(app) as client:
        assert client.post("/webhooks/ingest", json=body).status_code == 400

        first = client.post("/webhooks/ingest", json=body, headers={"X-Idempotency-Key": "k1"})
        assert first.status_code == 202

        replay = client.post("/webhooks/ingest", json=body, headers={"X-Idempotency-Key": "k1"})
        assert replay.status_code == 200
        assert replay.json()["duplicate"] is True

        conflict = client.post(
            "/webhooks/ingest", json={**body, "content": "different"}, headers={"X-Idempotency-Key": "k1"}
        )
        assert conflict.status_code == 409

        assert client.get("/health").json() == {"status": "ok"}

    rows = sqlite3.connect(tmp_path / "api.db").execute("SELECT cleaned_text FROM events").fetchall()
    assert len(rows) == 1
    assert "a@example.com" not in rows[0][0] and "123-45-6789" not in rows[0][0]
