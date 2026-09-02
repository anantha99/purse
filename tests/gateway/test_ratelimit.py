"""The token-bucket limiter, in isolation with an injected clock (C2.10, PRD §13).

No sleeping, no wall-clock: every test drives a mutable ``fake_now`` by hand, so
refill, exhaustion, retry_after, per-connection isolation, and idle eviction are
all deterministic. The limiter's thread-safety is a property of the lock it
holds; these tests prove the arithmetic the lock protects.
"""

from __future__ import annotations

import uuid

import pytest

from purse.gateway.ratelimit import (
    DEFAULT_WRITE_LIMIT_PER_MINUTE,
    WRITES_BUCKET,
    Limit,
    RateLimiter,
    RateLimitExceeded,
    default_limits,
)


class FakeClock:
    """A hand-advanced monotonic clock. ``tick`` moves it forward in seconds."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def tick(self, seconds: float) -> None:
        self.t += seconds


def _limiter(capacity: int = 3, per_seconds: float = 60.0, *, clock: FakeClock) -> RateLimiter:
    return RateLimiter(
        {WRITES_BUCKET: Limit(capacity=capacity, per_seconds=per_seconds)},
        now=clock,
        # Disable eviction unless a test asks for it — otherwise the horizon is
        # far off and never interferes.
        idle_eviction_seconds=0.0,
    )


def test_default_limit_is_the_prd_60_per_minute() -> None:
    limits = default_limits()
    assert set(limits) == {WRITES_BUCKET}
    assert limits[WRITES_BUCKET] == Limit(DEFAULT_WRITE_LIMIT_PER_MINUTE, 60.0)
    assert DEFAULT_WRITE_LIMIT_PER_MINUTE == 60


def test_a_full_bucket_allows_capacity_then_blocks_the_next() -> None:
    clock = FakeClock()
    limiter = _limiter(capacity=3, clock=clock)
    conn = uuid.uuid4()

    # Three writes are free; the fourth, with no time elapsed, is refused.
    limiter.check(conn)
    limiter.check(conn)
    limiter.check(conn)
    with pytest.raises(RateLimitExceeded) as exc:
        limiter.check(conn)

    assert exc.value.bucket == WRITES_BUCKET
    assert exc.value.connection_id == conn


def test_refill_returns_exactly_one_slot_after_the_refill_interval() -> None:
    clock = FakeClock()
    limiter = _limiter(capacity=3, per_seconds=60.0, clock=clock)  # 1 token / 20 s
    conn = uuid.uuid4()

    for _ in range(3):
        limiter.check(conn)
    with pytest.raises(RateLimitExceeded):
        limiter.check(conn)

    # 20 s buys exactly one token back: one write succeeds, the next is refused.
    clock.tick(20.0)
    limiter.check(conn)
    with pytest.raises(RateLimitExceeded):
        limiter.check(conn)


def test_refill_is_capped_at_capacity_no_matter_how_long_idle() -> None:
    clock = FakeClock()
    limiter = _limiter(capacity=3, per_seconds=60.0, clock=clock)
    conn = uuid.uuid4()

    limiter.check(conn)  # create the bucket, spend one
    clock.tick(10_000.0)  # idle far longer than the window

    # The bucket refilled to capacity and no further — exactly 3, then blocked.
    limiter.check(conn)
    limiter.check(conn)
    limiter.check(conn)
    with pytest.raises(RateLimitExceeded):
        limiter.check(conn)


def test_retry_after_is_the_time_until_one_token_and_is_sane() -> None:
    clock = FakeClock()
    limiter = _limiter(capacity=3, per_seconds=60.0, clock=clock)  # 20 s / token
    conn = uuid.uuid4()

    for _ in range(3):
        limiter.check(conn)
    with pytest.raises(RateLimitExceeded) as exc:
        limiter.check(conn)

    # Empty bucket, one token costs 20 s. Never negative, never beyond the window.
    assert exc.value.retry_after == pytest.approx(20.0)
    assert 0 < exc.value.retry_after <= 60.0


def test_a_refused_check_consumes_nothing() -> None:
    clock = FakeClock()
    limiter = _limiter(capacity=1, per_seconds=60.0, clock=clock)
    conn = uuid.uuid4()

    limiter.check(conn)
    # Several refusals while empty must not push the deficit deeper: after one
    # refill interval exactly one token is available, proving nothing leaked.
    for _ in range(5):
        with pytest.raises(RateLimitExceeded):
            limiter.check(conn)
    clock.tick(60.0)
    limiter.check(conn)  # the single earned token
    with pytest.raises(RateLimitExceeded):
        limiter.check(conn)


def test_connections_are_isolated() -> None:
    clock = FakeClock()
    limiter = _limiter(capacity=2, clock=clock)
    a, b = uuid.uuid4(), uuid.uuid4()

    limiter.check(a)
    limiter.check(a)
    with pytest.raises(RateLimitExceeded):
        limiter.check(a)  # A is exhausted

    # B has its own full bucket — A's exhaustion does not touch it.
    limiter.check(b)
    limiter.check(b)
    with pytest.raises(RateLimitExceeded):
        limiter.check(b)


def test_an_unconfigured_bucket_is_never_limited() -> None:
    clock = FakeClock()
    limiter = _limiter(capacity=1, clock=clock)
    conn = uuid.uuid4()

    # The writes bucket blocks after one; a bucket with no policy never does.
    limiter.check(conn)
    with pytest.raises(RateLimitExceeded):
        limiter.check(conn)
    for _ in range(100):
        limiter.check(conn, bucket="apis:use")


def test_cost_charges_multiple_tokens_at_once() -> None:
    clock = FakeClock()
    limiter = _limiter(capacity=3, clock=clock)
    conn = uuid.uuid4()

    limiter.check(conn, cost=3)  # drains the bucket in one call
    with pytest.raises(RateLimitExceeded):
        limiter.check(conn)


def test_idle_eviction_frees_a_stale_bucket_but_keeps_an_active_one() -> None:
    """The memory bound: a bucket idle past the horizon is dropped from the map;
    one touched within the horizon survives the sweep."""
    clock = FakeClock()
    limiter = RateLimiter(
        {WRITES_BUCKET: Limit(capacity=1, per_seconds=100.0)},
        now=clock,
        idle_eviction_seconds=250.0,
    )
    active, stale = uuid.uuid4(), uuid.uuid4()

    limiter.check(active)
    limiter.check(stale)  # both buckets now exist in the map

    # Keep `active` warm across the horizon (checked every 100 s); leave `stale`
    # untouched. The last check crosses 250 s since the last sweep, so it runs
    # the sweep — at which point `active` was touched 100 s ago, `stale` 300 s.
    clock.tick(100.0)
    limiter.check(active)
    clock.tick(100.0)
    limiter.check(active)
    clock.tick(100.0)
    limiter.check(active)  # this call runs the sweep

    # Assert the memory bound directly: the stale key is gone from the map.
    keys = set(limiter._buckets)
    assert (WRITES_BUCKET, active) in keys
    assert (WRITES_BUCKET, stale) not in keys


def test_eviction_of_an_idle_bucket_is_indistinguishable_from_keeping_it() -> None:
    """An evicted bucket had refilled to full, so re-creating it changes nothing.

    Two limiters, same trace, one evicting and one not: the caller cannot tell
    them apart, because by the eviction horizon the bucket is full either way.
    """
    clock_a, clock_b = FakeClock(), FakeClock()
    evicting = RateLimiter(
        {WRITES_BUCKET: Limit(capacity=2, per_seconds=60.0)},
        now=clock_a,
        idle_eviction_seconds=300.0,
    )
    keeping = RateLimiter(
        {WRITES_BUCKET: Limit(capacity=2, per_seconds=60.0)},
        now=clock_b,
        idle_eviction_seconds=0.0,  # never evicts
    )
    conn = uuid.uuid4()

    for limiter, clock in ((evicting, clock_a), (keeping, clock_b)):
        limiter.check(conn)
        clock.tick(10_000.0)
        limiter.check(conn)
        limiter.check(conn)
        with pytest.raises(RateLimitExceeded):
            limiter.check(conn)


def test_a_non_positive_capacity_is_rejected() -> None:
    with pytest.raises(ValueError, match="capacity"):
        Limit(capacity=0, per_seconds=60.0)


def test_a_non_positive_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="per_seconds"):
        Limit(capacity=1, per_seconds=0.0)
