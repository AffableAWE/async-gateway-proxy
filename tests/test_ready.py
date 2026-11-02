import fakeredis
import httpx
import pytest

from app.config import GatewayConfig
from app.ratelimit.redis_backend import RedisRateLimiter
from app.security import hash_api_key

KEY = "gw_ready_test_key"


def never_called(request):
    raise AssertionError("readiness must not call upstreams")


def config_with(**extra) -> GatewayConfig:
    return GatewayConfig.model_validate(
        {"routes": [{"path_prefix": "/api", "upstream": "http://up"}], **extra}
    )


def test_ready_when_everything_is_up(make_client):
    response = make_client(never_called).get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"config": "ok", "upstream_pool": "ok"},
        "open_circuits": 0,
    }


@pytest.mark.parametrize(
    "config",
    [
        config_with(
            auth={"keys": [{"client": "w", "key_sha256": hash_api_key(KEY), "routes": ["/api"]}]},
            rate_limit={"default": {"capacity": 1, "refill_per_second": 0.001}},
        )
    ],
)
def test_ready_needs_no_key_and_is_never_rate_limited(make_client):
    client = make_client(never_called)
    assert [client.get("/ready").status_code for _ in range(5)] == [200] * 5


@pytest.mark.parametrize(
    "config",
    [config_with(circuit_breaker={"failure_threshold": 1, "recovery_timeout_seconds": 60})],
)
def test_an_open_circuit_is_reported_but_does_not_make_the_gateway_unready(make_client):
    def down(request):
        raise httpx.ConnectError("down", request=request)

    client = make_client(down)
    client.get("/api/x")  # opens the breaker

    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json()["open_circuits"] == 1
    assert "http://up" not in response.text  # no internal addresses on a public endpoint


DEAD_REDIS = "redis://127.0.0.1:1"


def redis_config(fail_open: bool) -> GatewayConfig:
    return config_with(
        rate_limit={
            "default": {"capacity": 5, "refill_per_second": 1},
            "backend": "redis",
            "redis_url": DEAD_REDIS,
            "redis_timeout_seconds": 0.2,
            "fail_open": fail_open,
        }
    )


@pytest.mark.parametrize("config", [redis_config(fail_open=False)])
def test_unready_when_redis_is_down_and_failing_closed(make_client):
    # Every request would get 429: this instance really can't serve.
    response = make_client(never_called).get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["checks"]["rate_limit_redis"] == "unreachable"


@pytest.mark.parametrize("config", [redis_config(fail_open=True)])
def test_still_ready_when_redis_is_down_but_failing_open(make_client):
    response = make_client(never_called).get("/ready")

    assert response.status_code == 200
    assert response.json()["checks"]["rate_limit_redis"] == "unreachable, failing open"


@pytest.mark.parametrize("config", [redis_config(fail_open=False)])
def test_ready_with_a_working_redis(make_client):
    limiter = RedisRateLimiter(fakeredis.FakeAsyncRedis())
    response = make_client(never_called, rate_limiter=limiter).get("/ready")

    assert response.status_code == 200
    assert response.json()["checks"]["rate_limit_redis"] == "ok"


def test_unready_once_the_pool_is_closed(make_client):
    import asyncio

    client = make_client(never_called)
    asyncio.run(client.app.state.http_client.aclose())

    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["upstream_pool"] == "closed"


def test_health_stays_ok_when_not_ready(make_client):
    # Liveness and readiness are different questions: not ready is not broken.
    import asyncio

    client = make_client(never_called)
    asyncio.run(client.app.state.http_client.aclose())

    assert client.get("/ready").status_code == 503
    assert client.get("/health").status_code == 200
