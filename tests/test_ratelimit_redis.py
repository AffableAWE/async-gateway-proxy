"""The Redis rate limiter.

Every test runs against fakeredis (an in-process fake that runs the real
Lua script), so it works on any laptop. When REDIS_URL is set, as in CI,
each test runs a second time against a real Redis server.
"""

import asyncio
import logging
import os
import uuid

import fakeredis
import pytest
from redis.asyncio import Redis

from app.ratelimit import Limit, MemoryRateLimiter
from app.ratelimit.redis_backend import RedisRateLimiter

REDIS_URL = os.environ.get("REDIS_URL")

BACKENDS = ["fake"] + (["real"] if REDIS_URL else [])


def make_redis(kind: str) -> Redis:
    if kind == "real":
        return Redis.from_url(REDIS_URL)
    return fakeredis.FakeAsyncRedis()


@pytest.fixture(params=BACKENDS)
def backend(request):
    return request.param


@pytest.fixture
def key() -> str:
    # A fresh key per test, so tests never share a bucket in a real Redis.
    return f"test:{uuid.uuid4()}"


def run(coro):
    return asyncio.run(coro)


# --- the algorithm, in Redis ---------------------------------------------


def test_burst_then_reject(backend, key):
    async def scenario():
        limiter = RedisRateLimiter(make_redis(backend))
        decisions = [await limiter.hit(key, Limit(3, 1)) for _ in range(4)]
        await limiter.close()
        return decisions

    decisions = run(scenario())
    assert [d.allowed for d in decisions] == [True, True, True, False]
    assert [d.remaining for d in decisions] == [2, 1, 0, 0]


def test_fractional_values_survive_the_round_trip(backend, key):
    # Redis turns Lua numbers into integers. Without returning strings,
    # retry_after 0.25 would come back as 0.
    async def scenario():
        limiter = RedisRateLimiter(make_redis(backend))
        await limiter.hit(key, Limit(1, 4))  # 4 tokens/s
        rejected = await limiter.hit(key, Limit(1, 4))
        await limiter.close()
        return rejected

    rejected = run(scenario())
    assert not rejected.allowed
    assert 0.2 < rejected.retry_after <= 0.25


def test_refills_with_redis_time(backend, key):
    async def scenario():
        limiter = RedisRateLimiter(make_redis(backend))
        limit = Limit(2, 20)  # one token every 50 ms
        first = [(await limiter.hit(key, limit)).allowed for _ in range(3)]
        await asyncio.sleep(0.15)
        after = [(await limiter.hit(key, limit)).allowed for _ in range(3)]
        await limiter.close()
        return first, after

    first, after = run(scenario())
    assert first == [True, True, False]
    assert after == [True, True, False]


def test_key_expires_once_the_bucket_would_be_full(backend, key):
    async def scenario():
        client = make_redis(backend)
        limiter = RedisRateLimiter(client)
        await limiter.hit(key, Limit(10, 5))  # full again after 2 s
        ttl = await client.pttl("gw:rl:" + key)
        await limiter.close()
        return ttl

    ttl_ms = run(scenario())
    assert 2000 < ttl_ms <= 3000  # 2 s to refill + 1 s margin


# --- why Redis: shared, atomic -------------------------------------------


def test_two_gateway_replicas_share_one_bucket(backend, key):
    """The point of the Redis backend. With in-memory buckets each replica
    would allow the full 4; through Redis the 4 are shared."""

    async def scenario():
        if backend == "real":
            replica_a = RedisRateLimiter(make_redis("real"))
            replica_b = RedisRateLimiter(make_redis("real"))
        else:
            server = fakeredis.FakeServer()  # two clients, one fake server
            replica_a = RedisRateLimiter(fakeredis.FakeAsyncRedis(server=server))
            replica_b = RedisRateLimiter(fakeredis.FakeAsyncRedis(server=server))

        limit = Limit(4, 0.001)
        results = []
        for limiter in (replica_a, replica_b, replica_a, replica_b, replica_a, replica_b):
            results.append((await limiter.hit(key, limit)).allowed)
        await replica_a.close()
        await replica_b.close()
        return results

    assert run(scenario()) == [True, True, True, True, False, False]


def test_in_memory_replicas_do_not_share(key):
    """The same scenario with the memory backend: each replica allows 4."""

    async def scenario():
        replica_a, replica_b = MemoryRateLimiter(), MemoryRateLimiter()
        limit = Limit(4, 0.001)
        return [(await limiter.hit(key, limit)).allowed for limiter in (replica_a, replica_b) * 4]

    assert all(run(scenario()))  # 8 allowed when the limit was 4


def test_concurrent_hits_never_over_admit(backend, key):
    """50 requests at once against a bucket of 10. If read-refill-take-write
    were not atomic, two hits could both see the last token."""

    async def scenario():
        limiter = RedisRateLimiter(make_redis(backend))
        decisions = await asyncio.gather(*(limiter.hit(key, Limit(10, 0.001)) for _ in range(50)))
        await limiter.close()
        return decisions

    decisions = run(scenario())
    assert sum(d.allowed for d in decisions) == 10


# --- when Redis is down --------------------------------------------------

DEAD_REDIS = "redis://127.0.0.1:1"  # nothing listens on port 1


def test_fail_open_lets_requests_through(caplog):
    caplog.set_level(logging.INFO)

    async def scenario():
        limiter = RedisRateLimiter.from_url(DEAD_REDIS, timeout_seconds=0.2, fail_open=True)
        decisions = [await limiter.hit("k", Limit(1, 1)) for _ in range(3)]
        await limiter.close()
        return decisions

    decisions = run(scenario())
    assert all(d.allowed for d in decisions)
    assert all(d.remaining is None for d in decisions)  # unknown, so no header
    # Three failures, one log line: no log flood while Redis is down.
    errors = [r for r in caplog.records if r.getMessage() == "rate_limit_backend_error"]
    assert len(errors) == 1


def test_fail_closed_rejects():
    async def scenario():
        limiter = RedisRateLimiter.from_url(DEAD_REDIS, timeout_seconds=0.2, fail_open=False)
        decision = await limiter.hit("k", Limit(1, 1))
        await limiter.close()
        return decision

    decision = run(scenario())
    assert not decision.allowed
    assert decision.retry_after == 1.0
