import hashlib
import os
import re

PII_PATTERNS = [
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
    re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
]

FINGERPRINT_ITERATIONS = 120_000


def redact_pii(raw_text: str) -> str:
    """
    Redacts common PII patterns.

    Cheap (microseconds for typical payloads, payload size is capped at the API layer),
    so it runs inline on the request path. Doing it *before* the first write means raw
    PII never touches disk.
    """
    cleaned = raw_text
    for pattern in PII_PATTERNS:
        cleaned = pattern.sub("[REDACTED]", cleaned)
    return cleaned


def compute_fingerprint(cleaned_text: str, blob_iterations: int = FINGERPRINT_ITERATIONS) -> str:
    """
    Deterministic cryptographic fingerprint. CPU-bound (~40-60ms).

    Must run in a separate *process*: hashlib only releases the GIL for inputs larger
    than ~2KB, and every round here hashes a 32-byte digest, so the GIL is held for the
    whole loop. Running it in a thread would still starve the event loop.
    """
    digest = cleaned_text.encode("utf-8")
    for _ in range(blob_iterations):
        digest = hashlib.sha256(digest).digest()
    return digest.hex()


def scrub_and_fingerprint(raw_text: str, blob_iterations: int = FINGERPRINT_ITERATIONS) -> tuple[str, str]:
    """Backwards-compatible helper: redact + fingerprint in one call."""
    cleaned = redact_pii(raw_text)
    return cleaned, compute_fingerprint(cleaned, blob_iterations)


def init_worker() -> None:
    """
    Pool-process initializer: run CPU-heavy fingerprinting at a lower OS scheduling
    priority so it can never starve the event-loop process (or co-located services)
    of CPU time. Fingerprints are off the acknowledgment path, so they can wait.
    """
    try:
        os.nice(10)
    except (AttributeError, OSError):  # Windows / restricted containers
        pass


def warmup() -> int:
    """No-op used to pre-spawn pool worker processes at startup."""
    return os.getpid()
