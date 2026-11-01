"""Retries, retry budget and circuit breaker, through real requests to the gateway.

The upstream is a scripted fake that fails the way each test needs. Time is
a fake clock and sleeping is recorded, not done, so nothing here waits.
"""

import logging

import httpx
import pytest

from app.config import GatewayConfig
from app.resilience.guards import UpstreamGuards


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Upstream:
    """Answers with a scripted list of outcomes, one per call; the last repeats.

    An outcome is a status code, or an exception class to raise.
    """

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        outcome = self.outcomes[min(len(self.calls), len(self.outcomes)) - 1]
        if isinstance(outcome, type):
            raise outcome("scripted failure", request=request)
        return httpx.Response(outcome, json={"status": outcome, "call": len(self.calls)})


def make_config(retries=None, breaker=None) -> GatewayConfig:
    return GatewayConfig.model_validate(
        {
            "routes": [
                {"path_prefix": "/api", "upstream": "http://fake-upstream"},
                {"path_prefix": "/other", "upstream": "http://other-upstream"},
            ],
            "retries": retries,
            "circuit_breaker": breaker,
        }
    )


RETRIES = {"max_attempts": 3, "base_delay_seconds": 0.1, "max_delay_seconds": 1.0}
BREAKER = {"failure_threshold": 3, "recovery_timeout_seconds": 10}


@pytest.fixture
def config() -> GatewayConfig:
    return make_config(retries=RETRIES)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def guards(clock, sleeps) -> UpstreamGuards:
    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    # rng picks the top of each range, so backoff delays are predictable.
    return UpstreamGuards(clock=clock, sleep=record_sleep, rng=lambda lo, hi: hi)


@pytest.fixture
def gateway(make_client, guards):
    def _gateway(upstream):
        return make_client(upstream, guards=guards)

    return _gateway


# --- retries -------------------------------------------------------------


def test_get_is_retried_until_it_succeeds(gateway, sleeps):
    upstream = Upstream(503, 503, 200)
    response = gateway(upstream).get("/api/x")

    assert response.status_code == 200
    assert response.json()["call"] == 3
    assert len(upstream.calls) == 3
    assert sleeps == pytest.approx([0.1, 0.2])  # backoff doubles


def test_gives_up_after_max_attempts_and_returns_the_last_answer(gateway):
    upstream = Upstream(503)
    response = gateway(upstream).get("/api/x")

    assert len(upstream.calls) == 3
    assert response.status_code == 503
    assert response.json() == {"status": 503, "call": 3}  # the upstream's own body


def test_post_is_not_retried_after_the_upstream_answered(gateway):
    # The upstream may have acted on it: a retried POST could charge twice.
    upstream = Upstream(503, 200)
    response = gateway(upstream).post("/api/pay", json={"amount": 10})

    assert response.status_code == 503
    assert len(upstream.calls) == 1


def test_post_is_retried_when_the_connection_never_opened(gateway):
    # ConnectError: the request was never sent, so retrying is safe.
    upstream = Upstream(httpx.ConnectError, 200)
    response = gateway(upstream).post("/api/pay", json={"amount": 10})

    assert response.status_code == 200
    assert len(upstream.calls) == 2


def test_connect_timeout_counts_as_never_sent(gateway):
    # ConnectTimeout is a TimeoutException too, but it happened before the
    # request went out, so it is retried like ConnectError, even for POST.
    upstream = Upstream(httpx.ConnectTimeout, 200)
    response = gateway(upstream).post("/api/pay", json={"amount": 10})

    assert response.status_code == 200
    assert len(upstream.calls) == 2


def test_put_body_is_sent_again_on_retry(gateway):
    upstream = Upstream(502, 200)
    gateway(upstream).put("/api/item", content=b"same body")

    assert [call.content for call in upstream.calls] == [b"same body", b"same body"]


def test_timeouts_are_not_retried(gateway):
    upstream = Upstream(httpx.ReadTimeout, 200)
    response = gateway(upstream).get("/api/x")

    assert response.status_code == 504
    assert len(upstream.calls) == 1


def test_500_is_not_retried(gateway):
    upstream = Upstream(500, 200)
    assert gateway(upstream).get("/api/x").status_code == 500
    assert len(upstream.calls) == 1


def test_connect_errors_that_never_recover_give_502(gateway):
    upstream = Upstream(httpx.ConnectError)
    response = gateway(upstream).get("/api/x")

    assert response.status_code == 502
    assert len(upstream.calls) == 3


def test_retries_are_logged(gateway, caplog):
    caplog.set_level(logging.INFO)
    gateway(Upstream(503, 200)).get("/api/x")

    [retry] = [r for r in caplog.records if r.getMessage() == "upstream_retry"]
    assert (retry.attempt, retry.reason, retry.status_code) == (1, "status", 503)
    [forward] = [r for r in caplog.records if r.getMessage() == "proxy_forward"]
    assert forward.attempts == 2
    assert forward.request_id == retry.request_id


@pytest.mark.parametrize("config", [make_config()])
def test_no_retries_section_means_one_attempt(gateway, config):
    upstream = Upstream(503, 200)
    assert gateway(upstream).get("/api/x").status_code == 503
    assert len(upstream.calls) == 1


# --- retry budget --------------------------------------------------------


@pytest.mark.parametrize(
    "config", [make_config(retries={**RETRIES, "budget_ratio": 0, "budget_min_per_second": 0.2})]
)
def test_retry_budget_stops_a_retry_storm(gateway, caplog):
    # Floor: 0.2/s * 10 s = 2 retries in the window, then no more.
    caplog.set_level(logging.INFO)
    upstream = Upstream(503)
    client = gateway(upstream)
    for _ in range(4):
        client.get("/api/x")

    # Request 1 uses both retries (3 calls); requests 2-4 get none (1 call each).
    assert len(upstream.calls) == 3 + 1 + 1 + 1
    assert any(r.getMessage() == "retry_budget_exhausted" for r in caplog.records)


# --- circuit breaker -----------------------------------------------------


@pytest.mark.parametrize("config", [make_config(breaker=BREAKER)])
def test_breaker_opens_and_fails_fast(gateway):
    upstream = Upstream(httpx.ConnectError)
    client = gateway(upstream)

    assert [client.get("/api/x").status_code for _ in range(3)] == [502, 502, 502]
    response = client.get("/api/x")

    assert response.status_code == 503
    assert response.json()["error"] == "upstream circuit open"
    assert response.headers["retry-after"] == "10"
    assert len(upstream.calls) == 3  # the 4th never reached the upstream


@pytest.mark.parametrize("config", [make_config(breaker=BREAKER)])
def test_breaker_recovers_through_a_probe(gateway, clock):
    upstream = Upstream(502, 502, 502, 200)
    client = gateway(upstream)
    for _ in range(3):
        client.get("/api/x")
    assert client.get("/api/x").status_code == 503  # open

    clock.now += 10  # recovery timeout over: half-open
    assert client.get("/api/x").status_code == 200  # the probe succeeds
    assert client.get("/api/x").status_code == 200  # closed again
    assert len(upstream.calls) == 5


@pytest.mark.parametrize("config", [make_config(breaker=BREAKER)])
def test_breaker_is_per_upstream(gateway):
    def upstream(request):
        if request.url.host == "fake-upstream":
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200)

    client = gateway(upstream)
    for _ in range(3):
        client.get("/api/x")
    assert client.get("/api/x").status_code == 503
    assert client.get("/other/x").status_code == 200  # a healthy upstream is untouched


@pytest.mark.parametrize("config", [make_config(breaker=BREAKER)])
def test_upstream_4xx_and_500_do_not_trip_the_breaker(gateway):
    upstream = Upstream(404, 500, 404, 500, 404)
    client = gateway(upstream)
    assert [client.get("/api/x").status_code for _ in range(5)] == [404, 500, 404, 500, 404]


@pytest.mark.parametrize("config", [make_config(retries=RETRIES, breaker=BREAKER)])
def test_breaker_opening_mid_retry_stops_the_retries(gateway):
    # Threshold 3, max_attempts 3: the third failed attempt opens the breaker.
    upstream = Upstream(503)
    client = gateway(upstream)

    first = client.get("/api/x")
    assert first.status_code == 503 and first.json() == {"status": 503, "call": 3}
    second = client.get("/api/x")
    assert second.json()["error"] == "upstream circuit open"
    assert len(upstream.calls) == 3


@pytest.mark.parametrize(
    "config", [make_config(retries=RETRIES, breaker={**BREAKER, "failure_threshold": 2})]
)
def test_no_backoff_wait_once_the_breaker_opens(gateway, sleeps):
    # Attempt 1 fails, wait, attempt 2 fails and opens the breaker. A third
    # attempt can't happen, so the gateway must not wait for it.
    upstream = Upstream(503)
    gateway(upstream).get("/api/x")

    assert len(upstream.calls) == 2
    assert sleeps == pytest.approx([0.1])


@pytest.mark.parametrize("config", [make_config(breaker=BREAKER)])
def test_breaker_state_changes_are_logged(gateway, caplog):
    caplog.set_level(logging.INFO)
    client = gateway(Upstream(httpx.ConnectError))
    for _ in range(4):
        client.get("/api/x")

    [opened] = [r for r in caplog.records if r.getMessage() == "circuit_state_change"]
    assert (opened.upstream, opened.to_state) == ("http://fake-upstream", "open")
    assert any(r.getMessage() == "circuit_open_rejected" for r in caplog.records)
