import os

import httpx

DOWNSTREAM_URL = os.getenv("DOWNSTREAM_URL", "http://127.0.0.1:9001/events")

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class DeliveryError(Exception):
    def __init__(self, message: str, *, retryable: bool, retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def build_client() -> httpx.AsyncClient:
    """
    One shared client for the whole process: keeps a warm connection pool to the
    downstream instead of a new TCP handshake + pool per event, and bounds sockets.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=3.0),
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
    )


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


async def forward_event(
    client: httpx.AsyncClient,
    *,
    event_id: str,
    cleaned_text: str,
    fingerprint: str,
    idempotency_key: str,
    url: str | None = None,
) -> dict:
    """
    Forwards a processed webhook event to the downstream consumer API.

    The idempotency key is propagated so a downstream that supports it can dedupe
    the (rare) redelivery that at-least-once semantics allow after a crash.
    Raises DeliveryError classified as retryable / non-retryable.
    """
    try:
        response = await client.post(
            url or os.getenv("DOWNSTREAM_URL", DOWNSTREAM_URL),
            json={"event_id": event_id, "text": cleaned_text, "fingerprint": fingerprint},
            headers={"Idempotency-Key": idempotency_key},
        )
    except httpx.TransportError as exc:  # connect errors, timeouts, resets
        raise DeliveryError(f"transport error: {exc!r}", retryable=True) from exc

    if response.status_code in RETRYABLE_STATUS:
        raise DeliveryError(
            f"downstream returned {response.status_code}",
            retryable=True,
            retry_after=_parse_retry_after(response.headers.get("Retry-After")),
        )
    if response.is_error:
        raise DeliveryError(f"downstream returned {response.status_code}", retryable=False)

    try:
        return response.json()
    except ValueError:
        return {}
