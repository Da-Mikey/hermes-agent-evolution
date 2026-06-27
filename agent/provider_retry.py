"""Provider connection retry with exponential backoff.

A lightweight retry wrapper for API transport-layer errors
(``APIConnectionError``, ``ReadError``, ``ConnectionError``, timeouts)
that are transient by nature.  Auth, billing, and 4xx errors are NOT
retried — they will always fail the same way.

Used by the agent loop to turn terminal transport failures in cron
sessions into recoverable transient errors (#608).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Exception type names considered retryable transport failures.
# These are the OpenAI SDK / httpx / urllib3 errors that indicate a
# transient network or socket problem rather than an application-layer
# rejection (auth, billing, policy block).
_RETRYABLE_TRANSPORT_TYPE_NAMES: frozenset[str] = frozenset({
    "APIConnectionError",
    "APITimeoutError",
    "ReadError",
    "ReadTimeout",
    "ConnectError",
    "ConnectTimeout",
    "PoolTimeout",
    "RemoteProtocolError",
    "ConnectionError",
    "ConnectionResetError",
    "ConnectionAbortedError",
    "BrokenPipeError",
    "TimeoutError",
    "ServerDisconnectedError",
    # SSL/TLS transient failures (see error_classifier.py)
    "SSLError",
    "SSLZeroReturnError",
    "SSLWantReadError",
    "SSLWantWriteError",
    "SSLEOFError",
    "SSLSyscallError",
})

# Exception type names that are NEVER retried — they indicate a
# deterministic application-layer rejection.
_NON_RETRYABLE_TYPE_NAMES: frozenset[str] = frozenset({
    "AuthenticationError",
    "PermissionDeniedError",
    "BadRequestError",
})

# Message substrings that indicate a non-retryable error even when the
# exception type is generic (e.g. RuntimeError from a provider shim).
_NON_RETRYABLE_MESSAGE_PATTERNS: tuple[str, ...] = (
    "invalid api key",
    "invalid_api_key",
    "authentication failed",
    "unauthorized",
    "insufficient credits",
    "insufficient_quota",
    "insufficient balance",
    "credit balance",
    "credits exhausted",
    "billing",
    "payment required",
    "does not exist",
    "model not found",
    "invalid model",
    "unknown parameter",
    "unsupported parameter",
    "invalid_request_error",
)


def _is_retryable(exc: BaseException) -> bool:
    """Return True if *exc* is a transient transport failure worth retrying.

    Only retries errors where no HTTP response was received from the
    server (pure transport failures).  Any exception that carries an
    HTTP status code means we got a response — the outer error handler
    in chat_completion_helpers.py should handle 4xx/5xx classification.
    """
    exc_type_name = type(exc).__name__

    # Fast path: explicit retryable type names (pure transport errors)
    if exc_type_name in _RETRYABLE_TRANSPORT_TYPE_NAMES:
        return True

    # Fast path: explicit non-retryable type names (auth/billing)
    if exc_type_name in _NON_RETRYABLE_TYPE_NAMES:
        return False

    # If the exception carries an HTTP status code, we received a
    # response from the server.  Let the outer error handler classify
    # it (4xx → auth/billing/format, 5xx → server_error, etc.).
    # Only retry when NO response was received (pure transport failure).
    for attr in ("status_code", "http_status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return False
    resp = getattr(exc, "response", None)
    if resp is not None and isinstance(getattr(resp, "status_code", None), int):
        return False

    # Heuristic: check the error message for non-retryable signals
    # (auth, billing, etc. embedded in a generic exception type).
    exc_msg_lower = str(exc).lower()
    for pattern in _NON_RETRYABLE_MESSAGE_PATTERNS:
        if pattern in exc_msg_lower:
            return False

    # Fallback: treat generic transport-looking errors as retryable.
    # Look for connection/timeout keywords in the type name or message.
    for kw in ("Connection", "Timeout", "ReadError", "BrokenPipe"):
        if kw in exc_type_name or kw.lower() in exc_msg_lower:
            return True

    # Unknown errors that don't look like transport failures: do NOT
    # retry — let the outer handler classify them.
    return False


async def retry_with_backoff(
    callable_fn: Callable[[], Awaitable[T]],
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
) -> T:
    """Call *callable_fn* with exponential backoff on transport errors.

    Args:
        callable_fn: An async callable that makes the API request.
        max_retries: Maximum number of retry attempts (default 3).
        base_delay: Initial backoff delay in seconds (default 1s).
        max_delay: Maximum backoff delay cap in seconds (default 30s).

    Returns:
        The result of *callable_fn* on success.

    Raises:
        The last exception if all retries are exhausted, or immediately
        for non-retryable errors (auth, billing, 4xx).
    """
    last_exc: BaseException | None = None

    for attempt in range(1, max_retries + 1):
        try:
            return await callable_fn()
        except Exception as exc:
            last_exc = exc

            # Auth/billing/4xx: fail fast — retrying won't help.
            if not _is_retryable(exc):
                raise

            if attempt < max_retries:
                delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                logger.warning(
                    "Provider connection retry %d/%d after %s: %s",
                    attempt,
                    max_retries,
                    type(exc).__name__,
                    exc,
                )
                await asyncio.sleep(delay)
            else:
                logger.warning(
                    "Provider connection retry %d/%d exhausted after %s: %s",
                    attempt,
                    max_retries,
                    type(exc).__name__,
                    exc,
                )

    # All retries exhausted — re-raise the last error.
    assert last_exc is not None
    raise last_exc


def retry_sync_with_backoff(
    callable_fn: Callable[[], T],
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
) -> T:
    """Synchronous variant of :func:`retry_with_backoff`.

    Used by non-streaming API call paths that run in a worker thread
    without an active event loop.
    """
    last_exc: BaseException | None = None

    for attempt in range(1, max_retries + 1):
        try:
            return callable_fn()
        except Exception as exc:
            last_exc = exc

            if not _is_retryable(exc):
                raise

            if attempt < max_retries:
                delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                logger.warning(
                    "Provider connection retry %d/%d after %s: %s",
                    attempt,
                    max_retries,
                    type(exc).__name__,
                    exc,
                )
                time.sleep(delay)
            else:
                logger.warning(
                    "Provider connection retry %d/%d exhausted after %s: %s",
                    attempt,
                    max_retries,
                    type(exc).__name__,
                    exc,
                )

    assert last_exc is not None
    raise last_exc
