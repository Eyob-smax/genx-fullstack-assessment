# Webhook Ingestion Service — Backend Technical Assessment

## Context
Our webhook ingestion service is dropping requests and timing out health checks during traffic spikes, and downstream partners are reporting duplicate events whenever their webhook sender retries after a slow response.

Your goal is to diagnose the root causes under realistic concurrent load and refactor the service so it handles traffic spikes reliably and idempotently.

## Setup
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Running the Benchmark & Chaos Harness
The test suite starts both the FastAPI ingestion service and a mock flaky downstream receiver (`mock_downstream.py`), then simulates a 60-request burst containing duplicate retries while continuously probing `/health`:

```bash
pytest -v -s
```

*(Note: On the initial codebase, `pytest` is expected to fail.)*

## Acceptance Criteria
1. **Event-Loop Responsiveness:** Every `GET /health` probe sent while burst traffic is in flight must respond in **< 50ms**.
2. **Fast Ingestion Acknowledgment:** `POST /webhooks/ingest` must acknowledge valid requests (`200` or `202`) with **p95 latency < 250ms**.
3. **Strict Idempotency:** Concurrent duplicate deliveries sharing the same `X-Idempotency-Key` must never produce duplicate database rows or duplicate downstream deliveries.
4. **Resilient Downstream Delivery:** Transient `503` errors from the downstream service must be retried safely so all unique events reach `"delivered"` status.

## Constraints
- **No external infrastructure:** Keep the solution self-contained using Python and SQLite (do not add Redis, Celery, RabbitMQ, or PostgreSQL).
- Complete `DECISIONS.md` explaining your root-cause diagnosis and architectural choices.

---

## Solution overview

See **[DECISIONS.md](DECISIONS.md)** for the full diagnosis and trade-offs.

```
app/
  main.py        FastAPI app + lifespan: validate → redact PII → idempotent insert → 202
  database.py    single long-lived SQLite connection (WAL) on a dedicated writer thread,
                 UNIQUE idempotency key, atomic claim-with-lease for delivery
  worker.py      DeliveryService: asyncio.Queue + bounded workers, process-pool fingerprinting,
                 retries with exponential backoff + jitter, sweeper for crash recovery
  downstream.py  shared httpx client, retryable/non-retryable error classification
  processor.py   redact_pii (cheap, inline) / compute_fingerprint (CPU, process pool)
tests/
  test_burst_load.py                 provided chaos harness (unchanged)
  test_idempotency_and_delivery.py   races, retries, dead-letter, crash recovery, HTTP contract
```

Run everything with `pytest -v -s`. Optional tuning via env vars: `CPU_POOL_WORKERS`
(default `cpu_count // 2`), `DELIVERY_CONCURRENCY` (16), `DELIVERY_MAX_ATTEMPTS` (12),
`DELIVERY_BASE_BACKOFF` (0.25 s), `DELIVERY_MAX_BACKOFF` (30 s).

> Note: the SQLite build must support `RETURNING` (SQLite ≥ 3.35, bundled with all
> current CPython 3.11+ builds).
