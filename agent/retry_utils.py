"""Retry utilities — jittered backoff for decorrelated retries.

Replaces fixed exponential backoff with jittered delays to prevent
thundering-herd retry spikes when multiple sessions hit the same
rate-limited provider concurrently.

Also provides ``_FailureCounter``, a thread-safe session-scoped
utility for tracking consecutive failures with optional cooldown,
shared by the Telegram adapter circuit breaker and the auxiliary
client fallback chain.
"""

import calendar
import email.utils
import random
import threading
import time
from typing import Optional

# Monotonic counter for jitter seed uniqueness within the same process.
# Protected by a lock to avoid race conditions in concurrent retry paths
# (e.g. multiple gateway sessions retrying simultaneously).
_jitter_counter = 0
_jitter_lock = threading.Lock()


def jittered_backoff(
    attempt: int,
    *,
    base_delay: float = 5.0,
    max_delay: float = 120.0,
    jitter_ratio: float = 0.5,
) -> float:
    """Compute a jittered exponential backoff delay.

    Args:
        attempt: 1-based retry attempt number.
        base_delay: Base delay in seconds for attempt 1.
        max_delay: Maximum delay cap in seconds.
        jitter_ratio: Fraction of computed delay to use as random jitter
            range.  0.5 means jitter is uniform in [0, 0.5 * delay].

    Returns:
        Delay in seconds: min(base * 2^(attempt-1), max_delay) + jitter.

    The jitter decorrelates concurrent retries so multiple sessions
    hitting the same provider don't all retry at the same instant.
    """
    global _jitter_counter
    with _jitter_lock:
        _jitter_counter += 1
        tick = _jitter_counter

    exponent = max(0, attempt - 1)
    if exponent >= 63 or base_delay <= 0:
        delay = max_delay
    else:
        delay = min(base_delay * (2**exponent), max_delay)

    # Seed from time + counter for decorrelation even with coarse clocks.
    seed = (time.time_ns() ^ (tick * 0x9E3779B9)) & 0xFFFFFFFF
    rng = random.Random(seed)
    jitter = rng.uniform(0, jitter_ratio * delay)

    return delay + jitter


def extract_retry_after_seconds(
    retry_after: str | None, now: float | None = None
) -> float | None:
    """Parse a Retry-After value into seconds from now.

    Supports both integer seconds (RFC 7231 §7.1.3) and HTTP-date strings
    (RFC 7231 §7.1.1.2).  Returns None for malformed/empty values.
    Caps the result at 300 seconds (5 minutes) so a far-future Retry-After
    header cannot stall the agent indefinitely.
    """
    if not retry_after or not isinstance(retry_after, str):
        return None
    retry_after = retry_after.strip()
    if not retry_after:
        return None

    # Try integer seconds first.
    try:
        return min(float(retry_after), 300.0)
    except (TypeError, ValueError):
        pass

    # Try HTTP-date (e.g. "Wed, 21 Oct 2015 07:28:00 GMT").
    try:
        parsed = email.utils.parsedate_to_datetime(retry_after)
        retry_time = calendar.timegm(parsed.utctimetuple())
        now = now if now is not None else time.time()
        return max(0.0, min(retry_time - now, 300.0))
    except Exception:
        return None


class _FailureCounter:
    """Thread-safe, session-scoped consecutive-failure counter with optional cooldown.

    Track how many times a *single operation* (send, poll, fallback entry)
    has failed consecutively. When the threshold is exceeded the operation
    is considered ``tripped`` (unless a cooldown duration is configured).

    Thread-safe: all mutable state is protected by a reentrant lock so
    multiple callers (e.g. async gateway tasks) can share one instance.

    Typical usage::

        counter = _FailureCounter(threshold=3, cooldown=30.0)
        # … on failure …
        if counter.trip():
            logger.warning("circuit breaker tripped, waiting %ss", counter.remaining_cooldown)
            return fallback_result
        # … on success …
        counter.reset()
    """

    def __init__(
        self,
        threshold: int = 3,
        cooldown: float = 0.0,
    ) -> None:
        """
        Args:
            threshold: Consecutive failures after which ``trip()`` returns True.
            cooldown: Seconds to remain in tripped state (0 = no cooldown).
        """
        if threshold < 1:
            raise ValueError(f"threshold must be >= 1, got {threshold}")
        self._threshold = threshold
        self._cooldown = cooldown
        self._count = 0
        self._tripped_at: float = 0.0
        self._lock = threading.RLock()

    # ── Public helpers ────────────────────────────────────────────────

    def reset(self) -> None:
        """Reset the failure count and clear the tripped state."""
        with self._lock:
            self._count = 0
            self._tripped_at = 0.0

    def increment(self, now: Optional[float] = None) -> int:
        """Increment failure count and return the new value."""
        with self._lock:
            self._count += 1
            return self._count

    # ── Introspection (read-only, no lock needed for simple fields) ───

    @property
    def count(self) -> int:
        """Current consecutive-failure count."""
        with self._lock:
            return self._count

    @property
    def threshold(self) -> int:
        """Configured failure threshold."""
        return self._threshold

    @property
    def is_tripped(self) -> bool:
        """True iff the counter has exceeded threshold AND is in cooldown.

        A counter with no cooldown (``cooldown=0``) never stays tripped —
        ``trip()`` returns True once, but the next call returns False so
        the caller can decide to skip/retry based on the fresh return
        value rather than querying ``is_tripped`` later.
        """
        with self._lock:
            if self._count < self._threshold:
                return False
            if self._cooldown <= 0:
                return False
            return time.time() < self._tripped_at + self._cooldown

    @property
    def remaining_cooldown(self) -> float:
        """Seconds remaining in the cooldown period (0 if not in cooldown)."""
        with self._lock:
            if self._cooldown <= 0 or self._count < self._threshold:
                return 0.0
            remaining = (self._tripped_at + self._cooldown) - time.time()
            return max(0.0, remaining)

    # ── Core action ───────────────────────────────────────────────────

    def trip(self, now: Optional[float] = None) -> bool:
        """Check whether the circuit is tripped after incrementing.

        Increments the failure count. Returns True if the counter has
        reached threshold (regardless of cooldown).  Use this as the
        immediate decision in a failure handler — do NOT call
        ``increment()`` before ``trip()``; ``trip()`` handles the
        increment itself.

        A counter with no cooldown returns True once when the threshold
        is crossed (this call), then False on every subsequent call
        (because ``remaining_cooldown`` is 0 and the skip is no longer
        meaningful — the caller has already seen the signal once).
        """
        with self._lock:
            self._count += 1
            at = now if now is not None else time.time()
            if self._count >= self._threshold:
                self._tripped_at = at
                return True
            return False

    def succeeded(self) -> None:
        """Mark a successful operation: reset count and cooldown.

        Equivalent to ``reset()``. Use this as the success handler
        to make the call site read naturally::

            try:
                result = await do_work()
                counter.succeeded()
                return result
            except Exception:
                if counter.trip():
                    logger.warning("…")
        """
        self.reset()
