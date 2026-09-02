"""In-memory per-connection rate limiting (C2.10, PRD §13).

PRD §13 fixes the controls: **writes 60/min, ``use_api`` 30/min, per
connection**. This module is the writes half — the ``apis:use`` bucket lands
with C6, which is why the limiter is not hard-wired to a single limit: it holds a
map of *named* buckets, so C6 adds an ``apis:use`` entry rather than a second
limiter.

.. rubric:: The shape

A classic token bucket, one per ``(bucket, connection_id)``. Each bucket starts
full (``capacity`` tokens) and refills continuously at ``capacity / per_seconds``
tokens per second, capped at ``capacity``. A write costs one token; when the
bucket cannot pay, the call raises :class:`RateLimitExceeded` carrying the
seconds until it could. This gives a burst of ``capacity`` writes followed by
smooth refill — friendlier than a fixed window, which lets ``2 * capacity``
writes straddle a boundary and then stalls for a whole minute.

.. rubric:: Determinism and the clock

The bucket reads time from an injected ``now`` (defaulting to
:func:`time.monotonic` — monotonic, never wall-clock, so a clock adjustment
cannot grant or deny a burst). Tests pass a fake ``now`` and advance it by hand,
so nothing here depends on real sleeping.

.. rubric:: Thread-safety and memory

The ASGI server serves concurrent requests across threads (FastMCP runs each tool
in a worker thread), so all bucket state is guarded by a single
:class:`threading.Lock`. Buckets are created lazily on first use and evicted once
they have been idle longer than ``idle_eviction_seconds`` (default 10 min ≫ the
1-min window, so an evicted bucket had already refilled to full — dropping it is
identical to keeping it, minus the memory). The sweep runs at most once per idle
interval, so steady-state cost stays O(1) amortised per call.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass

__all__ = [
    "DEFAULT_IDLE_EVICTION_SECONDS",
    "DEFAULT_WRITE_LIMIT_PER_MINUTE",
    "WRITES_BUCKET",
    "Limit",
    "RateLimitExceeded",
    "RateLimiter",
    "default_limits",
]

#: The writes budget PRD §13 fixes: 60 write operations per minute per connection.
DEFAULT_WRITE_LIMIT_PER_MINUTE = 60

#: The name of the writes bucket. The limiter is keyed by ``(bucket, connection)``
#: so C6 can add an ``apis:use`` bucket (30/min, PRD §13) beside this one.
WRITES_BUCKET = "writes"

#: A bucket untouched for this long is dropped (see the module docstring). Well
#: above the 60 s window, so an evicted bucket is always one that had refilled to
#: full — eviction is a memory bound, never a policy change.
DEFAULT_IDLE_EVICTION_SECONDS = 600.0

_SECONDS_PER_MINUTE = 60.0


@dataclass(frozen=True)
class Limit:
    """One bucket's policy: ``capacity`` tokens that refill over ``per_seconds``.

    ``capacity`` is both the steady-state allowance for a full window and the
    maximum burst. ``per_seconds`` is the window that allowance refills over, so
    the continuous refill rate is ``capacity / per_seconds`` tokens per second.
    """

    capacity: int
    per_seconds: float

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ValueError("capacity must be positive")
        if self.per_seconds <= 0:
            raise ValueError("per_seconds must be positive")

    @property
    def refill_per_second(self) -> float:
        return self.capacity / self.per_seconds


def default_limits() -> dict[str, Limit]:
    """The production limits: the writes bucket at 60/min (PRD §13)."""
    return {WRITES_BUCKET: Limit(DEFAULT_WRITE_LIMIT_PER_MINUTE, _SECONDS_PER_MINUTE)}


class RateLimitExceeded(Exception):
    """A bucket could not pay for a call. ``retry_after`` is the seconds until it can.

    ``retry_after`` is a float count of seconds (the enforcement layers round it
    up for a ``Retry-After`` header); ``bucket`` and ``connection_id`` name what
    was limited, for logging and tests.
    """

    def __init__(self, *, retry_after: float, bucket: str, connection_id: uuid.UUID) -> None:
        self.retry_after = retry_after
        self.bucket = bucket
        self.connection_id = connection_id
        super().__init__(f"rate limit exceeded for {bucket!r}; retry after {retry_after:.1f}s")


@dataclass
class _Bucket:
    """Mutable token-bucket state for one ``(bucket, connection)`` key."""

    tokens: float
    updated: float


class RateLimiter:
    """A thread-safe, in-memory token-bucket limiter keyed by connection.

    Construct with a map of named :class:`Limit` policies (defaulting to
    :func:`default_limits`, i.e. the PRD §13 writes budget). Enforce with
    :meth:`check`, which the gateway calls after scope but before the write.

    A bucket name with no configured limit is *not* limited — :meth:`check`
    returns immediately — so a caller can name a future bucket (``apis:use``)
    before its policy exists without failing closed.
    """

    def __init__(
        self,
        limits: Mapping[str, Limit] | None = None,
        *,
        now: Callable[[], float] = time.monotonic,
        idle_eviction_seconds: float = DEFAULT_IDLE_EVICTION_SECONDS,
    ) -> None:
        self._limits: dict[str, Limit] = dict(limits) if limits is not None else default_limits()
        self._now = now
        self._idle = idle_eviction_seconds
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, uuid.UUID], _Bucket] = {}
        self._last_sweep = now()

    def check(
        self,
        connection_id: uuid.UUID,
        *,
        bucket: str = WRITES_BUCKET,
        cost: int = 1,
    ) -> None:
        """Charge *cost* tokens to *connection_id*'s *bucket*, or raise.

        Raises :class:`RateLimitExceeded` when the bucket cannot cover *cost*,
        without consuming anything (the call can be retried once enough has
        refilled). A bucket with no configured limit is a no-op.
        """
        limit = self._limits.get(bucket)
        if limit is None:
            return

        now = self._now()
        with self._lock:
            self._sweep_idle(now)
            key = (bucket, connection_id)
            state = self._buckets.get(key)
            if state is None:
                state = _Bucket(tokens=float(limit.capacity), updated=now)
                self._buckets[key] = state
            else:
                elapsed = now - state.updated
                if elapsed > 0:
                    state.tokens = min(
                        float(limit.capacity),
                        state.tokens + elapsed * limit.refill_per_second,
                    )
                    state.updated = now

            if state.tokens >= cost:
                state.tokens -= cost
                return

            deficit = cost - state.tokens
            retry_after = deficit / limit.refill_per_second
        raise RateLimitExceeded(retry_after=retry_after, bucket=bucket, connection_id=connection_id)

    def _sweep_idle(self, now: float) -> None:
        """Drop buckets idle past the eviction horizon. Caller holds the lock.

        Runs at most once per idle interval, so it does not add O(n) work to
        every call. An evicted bucket has been idle far longer than any window,
        so it had already refilled to full — re-creating it on next use is
        identical to having kept it.
        """
        if self._idle <= 0 or now - self._last_sweep < self._idle:
            return
        self._last_sweep = now
        cutoff = now - self._idle
        stale = [key for key, state in self._buckets.items() if state.updated < cutoff]
        for key in stale:
            del self._buckets[key]
