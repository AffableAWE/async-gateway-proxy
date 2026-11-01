"""Rate limiting through real HTTP requests to the gateway."""

import asyncio
import logging
import os
import uuid

import fakeredis
import httpx
import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis

from app.config import GatewayConfig
from app.main import build_rate_limiter, create_app
from app.ratelimit import Limit, MemoryRateLimiter
from app.ratelimit.redis_backend import RedisRateLimiter
from app.security import hash_api_key

WEB_KEY = "gw_test_web"
WEB_KEY_2 = "gw_test_web_rotated"
VIP_KEY = "gw_test_vip"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


def key(value: str) -> dict:
    return {"X-API-Key": value}


@pytest.fixture
def config() -> GatewayConfig:
    """Auth on, default bucket of 3 at 1/s; the vip client gets 10."""
    return GatewayConfig.model_validate(
        {
            "routes": [{"path_prefix": "/api", "upstream": "http://fake-upstream"}],
            "auth": {
                "keys": [
                    {"client": "web", "key_sha256": hash_api_key(WEB_KEY), "routes": ["/api"]},
                    {"client": "web", "key_sha256": hash_api_key(WEB_KEY_2), "routes": ["/api"]},
                    {
                        "client": "vip",
                        "key_sha256": hash_api_key(VIP_KEY),
                        "routes": ["/api"],
                        "rate_limit": {"capacity": 10, "refill_per_second": 5},
                    },
                ]
            },
            "rate_limit": {"default": {"capacity": 3, "refill_per_second": 1}},
        }
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(make_client, clock):
    return make_client(ok, rate_limiter=MemoryRateLimiter(clock=clock))


def statuses(client, n, headers=None, path="/api/x"):
    return [client.get(path, headers=headers).status_code for _ in range(n)]


# --- the basics ----------------------------------------------------------


def test_requests_within_the_limit_get_limit_headers(client):
    responses = [client.get("/api/x", headers=key(WEB_KEY)) for _ in range(3)]

    assert [r.status_code for r in responses] == [200, 200, 200]
    assert [r.headers["x-ratelimit-remaining"] for r in responses] == ["2", "1", "0"]
    assert all(r.headers["x-ratelimit-limit"] == "3" for r in responses)


def test_over_the_limit_is_429_with_retry_after(client):
    statuses(client, 3, key(WEB_KEY))
    response = client.get("/api/x", headers=key(WEB_KEY))

    assert response.status_code == 429
    assert response.headers["retry-after"] == "1"
    assert response.headers["x-ratelimit-remaining"] == "0"
    assert response.json() == {
        "error": "rate limit exceeded",
        "request_id": response.headers["x-request-id"],
    }


def test_a_429_never_reaches_the_upstream(make_client, clock):
    calls = []

    def counting(request):
        calls.append(1)
        return httpx.Response(200)

    client = make_client(counting, rate_limiter=MemoryRateLimiter(clock=clock))
    statuses(client, 10, key(WEB_KEY))
    assert len(calls) == 3


def test_bucket_refills_after_waiting(client, clock):
    assert statuses(client, 4, key(WEB_KEY)) == [200, 200, 200, 429]
    clock.now += 2  # 1 token/s -> 2 tokens back
    assert statuses(client, 3, key(WEB_KEY)) == [200, 200, 429]


# --- who shares a bucket -------------------------------------------------


def test_clients_have_separate_buckets(client):
    assert statuses(client, 4, key(WEB_KEY)) == [200, 200, 200, 429]
    assert statuses(client, 1, key(VIP_KEY)) == [200]


def test_two_keys_of_one_client_share_a_bucket(client):
    # Otherwise a key rotation would double the client's limit.
    assert statuses(client, 2, key(WEB_KEY)) == [200, 200]
    assert statuses(client, 2, key(WEB_KEY_2)) == [200, 429]


def test_per_client_override(client):
    assert statuses(client, 11, key(VIP_KEY)) == [200] * 10 + [429]


# --- what is exempt ------------------------------------------------------


def test_health_is_never_limited(client):
    assert statuses(client, 20, path="/health") == [200] * 20


def test_rejected_auth_does_not_use_tokens(client):
    statuses(client, 10, key("wrong"))  # ten 401s
    assert statuses(client, 3, key(WEB_KEY)) == [200, 200, 200]


def test_with_auth_off_buckets_are_per_client_ip(clock):
    config = GatewayConfig.model_validate(
        {
            "routes": [{"path_prefix": "/api", "upstream": "http://fake-upstream"}],
            "rate_limit": {"default": {"capacity": 2, "refill_per_second": 1}},
        }
    )
    limiter = MemoryRateLimiter(clock=clock)

    def streamed_ok(request):  # like a real network response: an unread stream
        return httpx.Response(200, stream=httpx.ByteStream(b"ok"))

    transport = httpx.MockTransport(streamed_ok)
    app = create_app(config=config, transport=transport, rate_limiter=limiter)
    with TestClient(app) as client:  # every TestClient request comes from "testclient"
        assert statuses(client, 3) == [200, 200, 429]
        # The bucket that ran dry is keyed by IP.
        assert asyncio.run(limiter.hit("ip:testclient", Limit(2, 1))).allowed is False


# --- logs ------------------------------------------------------------------


def test_429_is_logged_with_the_client(client, caplog):
    caplog.set_level(logging.INFO)
    statuses(client, 4, key(WEB_KEY))

    [limited] = [r for r in caplog.records if r.getMessage() == "rate_limited"]
    assert limited.bucket == "client:web"
    assert limited.retry_after_s == 1

    access = [r for r in caplog.records if r.getMessage() == "request_completed"]
    assert access[-1].status_code == 429
    assert access[-1].client == "web"


# --- choosing and running the Redis backend -----------------------------


def config_with(rate_limit: dict) -> GatewayConfig:
    return GatewayConfig.model_validate(
        {
            "routes": [{"path_prefix": "/api", "upstream": "http://fake-upstream"}],
            "rate_limit": rate_limit,
        }
    )


def test_config_picks_the_backend():
    default = {"capacity": 5, "refill_per_second": 1}
    assert isinstance(build_rate_limiter(config_with({"default": default})), MemoryRateLimiter)

    redis_limiter = build_rate_limiter(
        config_with({"default": default, "backend": "redis", "redis_url": "redis://x:6379/0"})
    )
    assert isinstance(redis_limiter, RedisRateLimiter)
    asyncio.run(redis_limiter.close())


def streamed_ok(request):  # like a real network response: an unread stream
    return httpx.Response(200, stream=httpx.ByteStream(b"ok"))


REDIS_URL = os.environ.get("REDIS_URL")
DEAD_REDIS = "redis://127.0.0.1:1"  # nothing listens on port 1


@pytest.mark.parametrize("backend", ["fake"] + (["real"] if REDIS_URL else []))
def test_gateway_with_redis_backend_end_to_end(backend):
    config = config_with({"default": {"capacity": 2, "refill_per_second": 0.001}})
    redis = fakeredis.FakeAsyncRedis() if backend == "fake" else Redis.from_url(REDIS_URL)
    # A fresh prefix: a real Redis may still hold buckets from an earlier run.
    limiter = RedisRateLimiter(redis, key_prefix=f"test:{uuid.uuid4()}:")
    app = create_app(
        config=config, transport=httpx.MockTransport(streamed_ok), rate_limiter=limiter
    )

    with TestClient(app) as client:
        statuses = [client.get("/api/x").status_code for _ in range(3)]
    assert statuses == [200, 200, 429]


def test_gateway_fails_open_when_redis_is_down(caplog):
    caplog.set_level(logging.INFO)
    config = config_with(
        {
            "default": {"capacity": 1, "refill_per_second": 1},
            "backend": "redis",
            "redis_url": DEAD_REDIS,
            "redis_timeout_seconds": 0.2,
        }
    )
    app = create_app(config=config, transport=httpx.MockTransport(streamed_ok))

    with TestClient(app) as client:
        responses = [client.get("/api/x") for _ in range(3)]

    assert [r.status_code for r in responses] == [200, 200, 200]
    assert "x-ratelimit-remaining" not in responses[0].headers
    assert any(r.getMessage() == "rate_limit_redis_unreachable_at_startup" for r in caplog.records)
