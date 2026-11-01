"""The token bucket and the in-memory limiter, with a fake clock.

A fake clock makes refill exact and instant: no sleeps, no flaky timing.
"""

import asyncio

import pytest

from app.ratelimit import Limit, MemoryRateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def limiter(clock) -> MemoryRateLimiter:
    return MemoryRateLimiter(clock=clock)


def hit(limiter, key="k", capacity=3, rate=1.0):
    return asyncio.run(limiter.hit(key, Limit(capacity, rate)))


def test_burst_up_to_capacity_then_reject(limiter):
    results = [hit(limiter) for _ in range(4)]

    assert [r.allowed for r in results] == [True, True, True, False]
    assert [r.remaining for r in results] == [2, 1, 0, 0]
    assert all(r.limit == 3 for r in results)


def test_retry_after_is_time_until_one_token(limiter):
    for _ in range(3):
        hit(limiter, rate=2.0)  # 2 tokens/s -> one token every 0.5 s
    rejected = hit(limiter, rate=2.0)

    assert not rejected.allowed
    assert rejected.retry_after == pytest.approx(0.5)


def test_bucket_refills_over_time(limiter, clock):
    for _ in range(3):
        hit(limiter)
    assert not hit(limiter).allowed

    clock.advance(1.0)  # 1 token/s -> one token back
    assert hit(limiter).allowed
    assert not hit(limiter).allowed


def test_refill_never_exceeds_capacity(limiter, clock):
    hit(limiter)
    clock.advance(3600)  # an hour idle earns 3600 tokens, but the cap is 3
    results = [hit(limiter) for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]


def test_partial_tokens_accumulate(limiter, clock):
    for _ in range(3):
        hit(limiter)
    clock.advance(0.4)
    assert not hit(limiter).allowed  # 0.4 of a token is not enough
    clock.advance(0.6)
    assert hit(limiter).allowed  # 0.4 + 0.6 = 1 token


def test_keys_are_independent(limiter):
    for _ in range(3):
        hit(limiter, key="a")
    assert not hit(limiter, key="a").allowed
    assert hit(limiter, key="b").allowed


def test_clock_going_backwards_grants_nothing(limiter, clock):
    for _ in range(3):
        hit(limiter)
    clock.advance(-50)
    assert not hit(limiter).allowed


def test_full_buckets_are_evicted_when_the_table_is_full(clock):
    limiter = MemoryRateLimiter(clock=clock, max_buckets=3)
    for key in ("a", "b", "c"):
        hit(limiter, key=key)
    assert len(limiter) == 3

    clock.advance(10)  # a, b, c have all refilled to full
    hit(limiter, key="d")
    assert len(limiter) == 1  # a, b, c dropped; only d remains


def test_partly_used_buckets_are_not_evicted(clock):
    limiter = MemoryRateLimiter(clock=clock, max_buckets=2)
    for _ in range(3):
        hit(limiter, key="busy")  # empty, still recovering
    hit(limiter, key="other")
    hit(limiter, key="new")  # table full: only fully refilled buckets may go
    assert not hit(limiter, key="busy").allowed  # its state was kept
