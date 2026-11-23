# Architecture & Debugging Decisions

## 1. Root-Cause Diagnosis

- **Event-loop / CPU behavior:**
  `scrub_and_fingerprint()` (120,000 chained SHA-256 rounds, ~40–60 ms) was called
  synchronously inside an `async def` handler, so it ran on the event-loop thread.
  While a request was hashing, nothing else could run: not `/health`, not other
  requests, not response writes. 60 concurrent requests queued up ~60 × 50 ms ≈ 3 s
  of serial CPU on the loop. Threads wouldn't fix it either: `hashlib` only releases
  the GIL for inputs larger than ~2 KB, and every round here hashes a 32-byte digest.
  On top of that, the handler `await`ed the downstream (≥250 ms by itself, more with
  a 503) before responding, so ingest latency always included downstream latency. A
  slow downstream made senders time out and retry, which created the duplicates.

- **Concurrency & race conditions:**
  There was no idempotency at all. `X-Idempotency-Key` was checked for presence and
  then ignored, `events` had no unique constraint, and each request did
  `INSERT` → `forward` → `UPDATE` independently. Two concurrent retries therefore
  produced two rows and two downstream deliveries. A check-then-insert in application
  code would still race under concurrency; the guarantee has to come from an atomic
  DB constraint. `mark_delivered` also updated by `event_id`, which isn't unique, so
  it could flip another row's status.

- **Resource & connection management:**
  - **SQLite:** a new connection (and aiosqlite thread) was opened per query, three
    per request, in the default rollback-journal mode, so readers and writers block
    each other and concurrent writers fail with `database is locked`.
  - **HTTP:** a new `httpx.AsyncClient` (new connection pool, new TCP handshake) was
    created per event, with no timeout tuning and no bound on outbound concurrency.
  - **Errors:** downstream errors were unhandled. A transient 503 raised
    `HTTPStatusError`, so the webhook got a 500, the row stayed `pending` forever, and
    nothing ever retried it. That 500 also triggered sender retries, and with them
    more duplicates.
  - **Lifecycle:** `@app.on_event("startup")` is deprecated in favour of `lifespan`.

## 2. Concurrency & Offloading Strategy

The request path and the delivery path are now fully decoupled
(transactional-outbox style, with SQLite as the outbox):

```
POST /webhooks/ingest  (event loop, a few ms)
  ├─ validate (pydantic, content capped at 64 KB)
  ├─ redact PII (regex, microseconds) ← before the first write, so raw PII never hits disk
  ├─ INSERT … ON CONFLICT(idempotency_key) DO NOTHING RETURNING id  (one hop to DB thread)
  └─ 202 Accepted (new) | 200 duplicate (same payload) | 409 (key reused, different payload)

DeliveryService (same process, background)
  ├─ asyncio.Queue[row_id] + 16 worker tasks   → bounded outbound concurrency
  ├─ claim row (conditional UPDATE + 60 s lease)
  ├─ fingerprint in ProcessPoolExecutor        → CPU off the loop *and* off the GIL
  ├─ POST downstream via one shared httpx.AsyncClient (timeouts, pooled keep-alive)
  └─ delivered | retrying (exp. backoff + jitter, honours Retry-After) | failed (dead-letter)
  + a sweeper every 2 s re-enqueues due retries / orphaned rows from a crash
```

Why these primitives:

| Need | Chosen | Rejected alternatives |
|---|---|---|
| CPU-bound hashing | `ProcessPoolExecutor` (forkserver/spawn, pre-warmed at startup, `cpu_count//2` workers, `os.nice(10)` in children) | `asyncio.to_thread`: the GIL is held for the whole loop, so the event loop still starves. Inline: what caused the bug. |
| Delivery after ack | Own worker pool on an `asyncio.Queue`, backed by DB state | FastAPI `BackgroundTasks`: unbounded (one task per request), no retry/backoff, state lost on crash, and it runs inside the request's lifecycle. Celery/Redis: forbidden by the constraints and unnecessary at this scale. |
| SQLite I/O | One long-lived `sqlite3` connection on a dedicated single-thread executor, one hop per logical operation | `aiosqlite` per call (the original): connection churn. A single aiosqlite connection worked but cost 4–5 thread hops per insert, which showed up in p95. Blocking `sqlite3` on the loop: works, but couples loop latency to disk stalls. |

Two choices to note:
- **Fingerprint after the ack.** Even in a process pool, 30 unique events × ~50 ms
  over 4 workers is ~400 ms, which would blow the 250 ms p95 if done before
  responding. The fingerprint is only needed for downstream delivery, so it moves to
  the worker. Only the cheap regex redaction stays on the request path, and it has to:
  doing it there means unredacted content is never persisted.
- **Pool sizing and `nice`.** The pool is half the cores and its processes run at
  lower priority. Hashing is no longer latency-critical, so it gives CPU priority to
  the event-loop process.

## 3. Idempotency & SQLite Concurrency

- **The guarantee comes from the database, not from timing.** `idempotency_key` is
  `TEXT NOT NULL UNIQUE`, and the insert is
  `INSERT … ON CONFLICT(idempotency_key) DO NOTHING RETURNING id`. SQLite allows a
  single writer at a time, so of two identical requests arriving in the same
  millisecond exactly one statement inserts; the other inserts nothing and reads the
  existing row. This holds across threads, connections and processes. It's tested
  with 50 concurrent in-process inserts and with two independent connections racing
  (`tests/test_idempotency_and_delivery.py`).
- **No duplicate delivery.** Only the request that created the row enqueues it. On
  top of that, a worker must claim the row with an atomic conditional
  `UPDATE … SET status='delivering', lease_until=now+60 WHERE id=? AND (due OR lease expired) RETURNING …`
  before calling downstream. Re-enqueues from the sweeper or duplicates in the queue
  find the row already claimed or delivered and do nothing. An in-memory `_queued`
  set also stops the same id from sitting in the queue twice.
- **Payload hash.** A SHA-256 of the canonical payload is stored with the key. A
  replay with the same key and the same body returns `200 {"duplicate": true, "status": …}`;
  the same key with a different body returns `409`, so a buggy client can't silently
  overwrite or drop an event.
- **SQLite settings:**
  - `journal_mode=WAL`: readers don't block the writer.
  - `synchronous=NORMAL`: in WAL this is crash-safe. It can lose the last commits
    on *power loss*, but not on a process crash.
  - `busy_timeout=5000`: other processes' writers wait instead of erroring.
  - Autocommit mode with single-statement atomic writes, and an index on
    `(status, next_attempt_at)` for the sweeper.
- **Downstream side.** `Idempotency-Key` is forwarded to the downstream. Delivery is
  at-least-once (see §4), so a downstream that honours the header can make it
  effectively exactly-once.

## 4. Failure Modes & Production Trade-offs

- **Downstream completely unreachable for 60 seconds:**
  Ingestion is unaffected: requests are still persisted and acknowledged in
  milliseconds, and `/health` stays green. Workers get connect errors or timeouts
  (connect timeout 3 s, total 10 s), which count as retryable. Each row is moved to
  `retrying` with backoff of 0.25 s → 0.5 → 1 → 2 → 4 → 8 → 16 → 30 s (capped), with
  jitter so retries don't all fire at once when the downstream returns. 60 s is about
  8 attempts, within `DELIVERY_MAX_ATTEMPTS=12`, so every event is delivered within
  roughly one backoff interval (≤30 s) of recovery. Outbound concurrency stays at
  16, so the recovering downstream isn't flooded. A longer outage exhausts the
  attempts and rows land in `failed` with `last_error`, acting as a dead-letter
  queue that can be replayed with an `UPDATE`. In production I'd make this
  time-based (e.g. keep retrying for 24 h) and add a circuit breaker, so we stop
  sending requests at all while the downstream is known to be down.

- **Process crashes 50 ms after acknowledging a webhook:**
  Nothing is lost: the `202` is only sent after the row is committed. On restart,
  `DeliveryService.start()` (and the periodic sweeper) selects every row that is
  `pending`, a due `retrying`, or `delivering` with an expired lease, and processes
  it. A fingerprint that hadn't been computed yet is computed then. The edge case is
  a crash *after* the downstream accepted the event but *before* `delivered` was
  committed: that row is redelivered once its lease expires. This is the usual
  at-least-once trade-off, and it's why the idempotency key is forwarded downstream.
  The one durability gap is `synchronous=NORMAL`: power loss (not a process crash)
  can drop the last few committed transactions. `synchronous=FULL` closes that at the
  cost of an fsync per ack.

- **Deploying with `--workers 4` instead of 1:**
  *Correctness holds*: the UNIQUE constraint and the claim/lease are enforced inside
  SQLite, so duplicate rows and concurrent double deliveries are still prevented
  across processes. Things would break or degrade in this order:
  1. **SQLite write contention first.** Four processes compete for SQLite's single
     write lock. With `busy_timeout` they wait instead of failing, but ingest p95
     rises under burst, and every process's sweeper also polls the same file.
  2. **CPU oversubscription.** Each worker creates its own process pool
     (`4 × cpu_count/2` hashing processes) plus 16 delivery tasks (64 concurrent
     downstream calls in total). Pool size and concurrency need to be divided by the
     worker count.
  3. **Recovery overlap.** Every process sweeps every row. The claim keeps this
     correct, but it's wasted work, and a delivery that runs longer than the 60 s
     lease could be claimed again by another process. Real multi-process deployment
     needs per-worker lease renewal, or a dedicated delivery process with N stateless
     API workers in front of it.

  The real scale-out is a server database (Postgres `SELECT … FOR UPDATE SKIP LOCKED`)
  or a queue, which the constraints rule out here.

---

### Verification

- `pytest -v -s` runs the provided burst harness plus `tests/test_idempotency_and_delivery.py`
  (10 tests: in-process and cross-connection key races, exclusive claims, 503/transport
  retries, non-retryable 4xx, max-attempt dead-letter, duplicate enqueue, crash recovery
  via expired lease, HTTP contract 400/202/200/409 and PII redaction).
- With the latency asserts relaxed, the burst harness passes every functional
  assertion consistently: 30 rows, 30 unique deliveries, 0 duplicates, all
  `delivered`, no PII.
- **Latency caveat:** on my development laptop (8 cores, under desktop load) the
  `< 50 ms /health` assert fails *only on the first probe*, which is sent at the same
  instant as the 60-request burst. Every later probe is 3–7 ms, and server-side
  handler time for `/health` is under 2 ms. Running the same harness against a
  **no-op FastAPI server** gives a first probe of 81–189 ms on this machine, so that
  number is bounded by the test client's own event loop opening 61 connections, not
  by this service. Ingest p95 lands at about 150–260 ms on the same machine, again
  mostly client-side. On a quieter machine both should fall under the thresholds.
