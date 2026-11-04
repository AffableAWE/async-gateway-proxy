"""Prometheus metrics, read straight from each app's own registry."""

import httpx
import pytest

from app.config import GatewayConfig
from app.metrics import GatewayMetrics
from app.ratelimit import MemoryRateLimiter
from app.resilience.breaker import CircuitBreaker, State
from app.resilience.guards import UpstreamGuards
from app.security import hash_api_key
from app.server import start_metrics_server

KEY = "gw_metrics_test_key"


def value(test_client, name, **labels) -> float:
    found = test_client.app.state.metrics.registry.get_sample_value(name, labels)
    return 0.0 if found is None else found


def ok(request):
    return httpx.Response(200)


@pytest.fixture
def config() -> GatewayConfig:
    return GatewayConfig.model_validate(
        {
            "routes": [
                {"path_prefix": "/api", "upstream": "http://up-a"},
                {"path_prefix": "/cpp", "upstream": "http://up-b"},
            ],
            "auth": {
                "keys": [{"client": "web", "key_sha256": hash_api_key(KEY), "routes": ["/api"]}]
            },
            "rate_limit": {"default": {"capacity": 2, "refill_per_second": 0.001}},
            "retries": {"max_attempts": 3, "base_delay_seconds": 0},
            "circuit_breaker": {"failure_threshold": 3, "recovery_timeout_seconds": 30},
        }
    )


@pytest.fixture
def gateway(make_client):
    async def no_sleep(_):
        pass

    def _gateway(handler):
        return make_client(
            handler,
            rate_limiter=MemoryRateLimiter(),
            guards=UpstreamGuards(sleep=no_sleep, rng=lambda lo, hi: 0),
        )

    return _gateway


AUTH = {"X-API-Key": KEY}


def test_requests_are_counted_by_route_method_and_status(gateway):
    client = gateway(ok)
    client.get("/api/a", headers=AUTH)
    client.get("/api/b/c", headers=AUTH)  # a different path, the same route label
    client.get("/health")

    assert value(client, "gateway_requests_total", route="/api", method="GET", status="200") == 2
    assert value(client, "gateway_requests_total", route="/health", method="GET", status="200") == 1


def test_raw_paths_never_become_labels(gateway):
    client = gateway(ok)
    for i in range(5):
        client.get(f"/api/users/{i}", headers=AUTH)

    text = client.app.state.metrics.render().decode()
    assert "/api/users/" not in text  # only the prefix, so 5 paths = 1 series


def test_latency_histogram(gateway):
    client = gateway(ok)
    client.get("/api/a", headers=AUTH)

    assert value(client, "gateway_request_duration_seconds_count", route="/api") == 1
    assert value(client, "gateway_request_duration_seconds_bucket", route="/api", le="+Inf") == 1


def test_in_flight_returns_to_zero(gateway):
    client = gateway(ok)
    client.get("/api/a", headers=AUTH)
    assert value(client, "gateway_requests_in_flight") == 0


def test_auth_rejections_by_reason(gateway):
    client = gateway(ok)
    client.get("/api/a")
    client.get("/api/a", headers={"X-API-Key": "wrong"})
    client.get("/cpp/a", headers=AUTH)  # valid key, route not allowed

    for reason in ("missing_key", "invalid_key", "route_not_allowed"):
        assert value(client, "gateway_auth_rejections_total", reason=reason) == 1
    # Requests stopped before routing are grouped, not labelled by path.
    assert value(client, "gateway_requests_total", route="none", method="GET", status="401") == 2


def test_rate_limited_by_client(gateway):
    client = gateway(ok)
    for _ in range(3):
        client.get("/api/a", headers=AUTH)
    assert value(client, "gateway_rate_limited_total", client="web") == 1


def test_upstream_attempts_and_retries(gateway):
    calls = []

    def flaky(request):
        calls.append(1)
        return httpx.Response(503 if len(calls) == 1 else 200)

    client = gateway(flaky)
    client.get("/api/a", headers=AUTH)

    up = "http://up-a"
    assert value(client, "gateway_upstream_attempts_total", upstream=up, outcome="status") == 1
    assert value(client, "gateway_upstream_attempts_total", upstream=up, outcome="success") == 1
    assert value(client, "gateway_upstream_retries_total", upstream=up) == 1


@pytest.mark.parametrize(
    "config",
    [
        GatewayConfig.model_validate(
            {
                "routes": [{"path_prefix": "/api", "upstream": "http://up-a"}],
                "circuit_breaker": {"failure_threshold": 2, "recovery_timeout_seconds": 30},
            }
        )
    ],
)
def test_breaker_state_and_rejections(make_client):
    def down(request):
        raise httpx.ConnectError("down", request=request)

    client = make_client(down)
    text = client.app.state.metrics.render().decode()
    assert "gateway_circuit_breaker_state{" not in text  # no breaker exists yet

    client.get("/api/a")
    assert value(client, "gateway_circuit_breaker_state", upstream="http://up-a") == 0  # closed
    client.get("/api/a")
    assert value(client, "gateway_circuit_breaker_state", upstream="http://up-a") == 2  # open
    client.get("/api/a")
    assert value(client, "gateway_circuit_open_rejections_total", upstream="http://up-a") == 1


def test_reading_breaker_state_changes_nothing():
    clock = [1000.0]
    breaker = CircuitBreaker(
        "u", failure_threshold=1, recovery_timeout_seconds=10, clock=lambda: clock[0]
    )
    breaker.record_failure()
    clock[0] += 10

    assert breaker.peek_state() is State.HALF_OPEN  # what it would be...
    assert breaker._state is State.OPEN  # ...but nothing moved


def test_metrics_server_serves_the_registry():
    metrics = GatewayMetrics()
    metrics.requests.labels("/api", "GET", "200").inc()

    port = start_metrics_server(metrics.registry, "127.0.0.1", 0)
    response = httpx.get(f"http://127.0.0.1:{port}/metrics")

    assert response.status_code == 200
    assert 'gateway_requests_total{method="GET",route="/api",status="200"} 1.0' in response.text


def test_metrics_are_not_on_the_main_port(gateway):
    # /metrics on the public port is just another path: auth asks for a key.
    assert gateway(ok).get("/metrics").status_code == 401


def test_no_created_timestamp_series(gateway):
    client = gateway(ok)
    client.get("/api/a", headers=AUTH)
    assert "_created" not in client.app.state.metrics.render().decode()


def test_known_series_exist_at_zero_before_anything_happens(gateway):
    # A series that only appears on its first .inc() appears at 1, and
    # rate()/increase() never see that first step. So alerts on these
    # (ConfigReloadFailing, for one) need them to exist at 0 from startup.
    client = gateway(ok)
    registry = client.app.state.metrics.registry
    sample = registry.get_sample_value  # None = the series doesn't exist

    for result in ("success", "failed", "rejected"):
        assert sample("gateway_config_reloads_total", {"result": result}) == 0
    for reason in ("missing_key", "invalid_key", "route_not_allowed"):
        assert sample("gateway_auth_rejections_total", {"reason": reason}) == 0
    for upstream in ("http://up-a", "http://up-b"):
        for name in (
            "gateway_upstream_retries_total",
            "gateway_retry_budget_exhausted_total",
            "gateway_circuit_open_rejections_total",
            "gateway_upstream_stream_aborts_total",
        ):
            assert sample(name, {"upstream": upstream}) == 0, (name, upstream)
