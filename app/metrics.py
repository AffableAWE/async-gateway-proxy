"""Prometheus metrics: numbers about the gateway that a dashboard can graph.

Logs answer "what happened to this request?". Metrics answer "how is the
gateway doing?": requests per second, p99 latency, how often upstreams
fail, how many breakers are open. Prometheus scrapes them every few seconds
from a plain-text page and keeps the history.

Three metric types, each for a different kind of number:
    Counter    only goes up (requests served). Rates come from its slope.
    Gauge      goes up and down (requests in flight right now).
    Histogram  counts values into buckets (latency), so Prometheus can work
               out percentiles like p95 across all requests.

Label values are kept to small, known sets: the route prefix (not the raw
path), the upstream URL, the client name. A raw path label would create a
new time series for every /users/123, /users/124... and eventually take
Prometheus down. That's "high cardinality", the classic metrics mistake.

Each app gets its own registry instead of prometheus_client's global one,
so tests can build many apps without "metric already registered" errors.
"""

from collections.abc import Callable, Iterator

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from app.resilience.breaker import CircuitBreaker, State

# Seconds. Fine-grained at the low end, where a gateway's own overhead lives.
LATENCY_BUCKETS = (0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)

BREAKER_STATE_VALUE = {State.CLOSED: 0, State.HALF_OPEN: 1, State.OPEN: 2}


class _BreakerCollector(Collector):
    """Reports each breaker's state at scrape time.

    Read when Prometheus asks, rather than updated on every state change,
    so the breaker code doesn't need to know metrics exist.
    """

    def __init__(self, breakers: Callable[[], dict[str, CircuitBreaker]]) -> None:
        self._breakers = breakers

    def collect(self) -> Iterator[GaugeMetricFamily]:
        family = GaugeMetricFamily(
            "gateway_circuit_breaker_state",
            "Circuit breaker state per upstream: 0 closed, 1 half-open, 2 open.",
            labels=["upstream"],
        )
        for upstream, breaker in sorted(self._breakers().items()):
            family.add_metric([upstream], BREAKER_STATE_VALUE[breaker.peek_state()])
        yield family


class GatewayMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        r = self.registry

        # --- every request --------------------------------------------------
        self.requests = Counter(
            "gateway_requests",
            "Requests handled, by route prefix, method and response status.",
            ["route", "method", "status"],
            registry=r,
        )
        self.request_duration = Histogram(
            "gateway_request_duration_seconds",
            "Time from request received to response finished, by route prefix.",
            ["route"],
            buckets=LATENCY_BUCKETS,
            registry=r,
        )
        self.in_flight = Gauge(
            "gateway_requests_in_flight", "Requests being handled right now.", registry=r
        )

        # --- upstreams ------------------------------------------------------
        self.upstream_attempts = Counter(
            "gateway_upstream_attempts",
            "Calls to upstreams, retries included, by outcome: "
            "success, connect, timeout, transport, status.",
            ["upstream", "outcome"],
            registry=r,
        )
        self.retries = Counter(
            "gateway_upstream_retries", "Retries sent, by upstream.", ["upstream"], registry=r
        )
        self.retry_budget_exhausted = Counter(
            "gateway_retry_budget_exhausted",
            "Retries skipped because the upstream's retry budget was used up.",
            ["upstream"],
            registry=r,
        )
        self.circuit_open_rejections = Counter(
            "gateway_circuit_open_rejections",
            "Requests answered 503 at once because the upstream's breaker was open.",
            ["upstream"],
            registry=r,
        )
        self.stream_aborts = Counter(
            "gateway_upstream_stream_aborts",
            "Responses cut off because the upstream broke off mid-body.",
            ["upstream"],
            registry=r,
        )

        # --- policy ---------------------------------------------------------
        self.auth_rejections = Counter(
            "gateway_auth_rejections",
            "Requests refused by auth: missing_key, invalid_key, route_not_allowed.",
            ["reason"],
            registry=r,
        )
        self.rate_limited = Counter(
            "gateway_rate_limited",
            "Requests refused with 429, by client name (anonymous when auth is off).",
            ["client"],
            registry=r,
        )

    def watch_breakers(self, breakers: Callable[[], dict[str, CircuitBreaker]]) -> None:
        self.registry.register(_BreakerCollector(breakers))

    def render(self) -> bytes:
        """The text page Prometheus scrapes."""
        return generate_latest(self.registry)
