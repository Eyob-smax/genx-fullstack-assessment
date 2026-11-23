import asyncio
import hashlib
import json
import logging
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.database import Database
from app.downstream import build_client
from app.processor import init_worker, redact_pii, warmup
from app.worker import DeliveryService

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
# httpx logs every request at INFO; one line per delivery attempt is noise on the hot path.
logging.getLogger("httpx").setLevel(logging.WARNING)

MAX_CONTENT_BYTES = 64 * 1024


class WebhookPayload(BaseModel):
    event_id: str = Field(min_length=1, max_length=200)
    tenant_id: str = Field(min_length=1, max_length=200)
    content: str = Field(max_length=MAX_CONTENT_BYTES)


def _pool_workers() -> int:
    # Half the cores: fingerprinting is background work (already acknowledged), so we
    # trade some throughput for headroom for the event loop and co-located processes.
    configured = os.getenv("CPU_POOL_WORKERS")
    if configured:
        return max(1, int(configured))
    return max(1, (os.cpu_count() or 2) // 2)


def _mp_context():
    # Never fork a process that already has threads (aiosqlite, executor threads):
    # forkserver/spawn give clean children on every platform.
    methods = multiprocessing.get_all_start_methods()
    return multiprocessing.get_context("forkserver" if "forkserver" in methods else "spawn")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database()
    await db.connect()

    workers = _pool_workers()
    executor = ProcessPoolExecutor(max_workers=workers, mp_context=_mp_context(), initializer=init_worker)
    # Pre-spawn worker processes so the first burst doesn't pay process start-up cost.
    loop = asyncio.get_running_loop()
    await asyncio.gather(*(loop.run_in_executor(executor, warmup) for _ in range(workers)))

    client = build_client()
    delivery = DeliveryService(db, executor, client)
    await delivery.start()

    app.state.db = db
    app.state.delivery = delivery
    try:
        yield
    finally:
        await delivery.stop()
        await client.aclose()
        executor.shutdown(wait=False, cancel_futures=True)
        await db.close()


app = FastAPI(title="Webhook Ingestion Service", lifespan=lifespan)


@app.get("/health")
async def health():
    # Deliberately trivial: no I/O, so it measures event-loop responsiveness only.
    return {"status": "ok"}


def _request_hash(payload: WebhookPayload) -> str:
    canonical = json.dumps(payload.model_dump(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@app.post("/webhooks/ingest")
async def ingest_webhook(
    request: Request,
    payload: WebhookPayload,
    x_idempotency_key: str | None = Header(default=None, max_length=255),
):
    if not x_idempotency_key:
        raise HTTPException(status_code=400, detail="Missing X-Idempotency-Key header")

    db: Database = request.app.state.db
    delivery: DeliveryService = request.app.state.delivery

    request_hash = _request_hash(payload)
    result = await db.insert_or_get(
        idempotency_key=x_idempotency_key,
        request_hash=request_hash,
        event_id=payload.event_id,
        tenant_id=payload.tenant_id,
        # PII is redacted before the first write: raw content is never persisted.
        cleaned_text=redact_pii(payload.content),
    )

    if result.created:
        delivery.enqueue(result.row_id)
        return JSONResponse(
            status_code=202,
            content={"status": "accepted", "event_id": payload.event_id, "idempotency_key": x_idempotency_key},
        )

    if result.request_hash != request_hash:
        raise HTTPException(
            status_code=409,
            detail="X-Idempotency-Key was already used with a different payload",
        )

    # Replay of an already-accepted request: acknowledge without reprocessing.
    return JSONResponse(
        status_code=200,
        content={
            "status": result.status,
            "event_id": payload.event_id,
            "idempotency_key": x_idempotency_key,
            "duplicate": True,
        },
    )
