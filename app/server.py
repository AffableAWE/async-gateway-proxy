"""How the gateway process is started: `python -m app`.

One place for the server flags that matter for a gateway, so a Dockerfile,
a Kubernetes manifest or a person typing commands can't forget one:

    proxy_headers=False   Don't trust X-Forwarded-For from clients. By default
                          uvicorn trusts it from 127.0.0.1 and rewrites the
                          client address, so a caller could fake its IP.
    access_log=False      The gateway logs its own request_completed line.
    timeout_graceful_shutdown
                          On SIGTERM uvicorn stops accepting connections and
                          waits for requests already in flight, streams
                          included, then runs the app's shutdown. This caps
                          the wait, so a never-ending stream can't block a
                          deploy forever. Kubernetes allows 30 s by default
                          before it sends SIGKILL, so stay under that.

Settings come from environment variables, the usual way to configure a
process inside a container:

    GATEWAY_HOST                       default 127.0.0.1 (0.0.0.0 in a container)
    GATEWAY_PORT                       default 8000
    GATEWAY_GRACEFUL_TIMEOUT_SECONDS   default 25
    GATEWAY_METRICS_PORT               default 9100; 0 turns the metrics server off

Metrics are served on their own port, not as /metrics on the main one. The
main port faces the internet; the metrics port is only reachable inside the
cluster, where Prometheus scrapes it. Route names, client names and error
rates are useful to an attacker too. Envoy and NGINX split admin traffic
off the same way.
"""

import os

import uvicorn
from prometheus_client import start_http_server
from prometheus_client.registry import CollectorRegistry


def server_config(app: object = "app.main:app", **overrides: object) -> uvicorn.Config:
    settings: dict = {
        "host": os.getenv("GATEWAY_HOST", "127.0.0.1"),
        "port": int(os.getenv("GATEWAY_PORT", "8000")),
        "proxy_headers": False,
        "access_log": False,
        "timeout_graceful_shutdown": int(os.getenv("GATEWAY_GRACEFUL_TIMEOUT_SECONDS", "25")),
        "lifespan": "on",  # fail to start if the app's startup fails (bad config)
    }
    settings.update(overrides)
    return uvicorn.Config(app, **settings)


def start_metrics_server(registry: CollectorRegistry, host: str, port: int) -> int:
    """Serve the registry at http://host:port/metrics. Returns the bound port.

    prometheus_client runs it in a background thread. Scrapes only read
    counters (which have their own locks) and breaker states (via
    peek_state, which changes nothing), so the thread never steps on the
    event loop.
    """
    server, _thread = start_http_server(port, addr=host, registry=registry)
    return server.server_port


def main() -> None:
    from app.main import app  # imported here so tests can import server_config cheaply

    config = server_config(app)
    metrics_port = int(os.getenv("GATEWAY_METRICS_PORT", "9100"))
    if metrics_port:
        start_metrics_server(app.state.metrics.registry, config.host, metrics_port)
    uvicorn.Server(config).run()
