"""Hot reload: the gateway reads config from a real file that the tests edit."""

import asyncio
import logging
import os
import signal
import socket
import time

import httpx
import pytest
import uvicorn
import yaml
from fastapi.testclient import TestClient

from app.main import create_app
from app.ratelimit import Decision, Limit
from app.security import hash_api_key
from app.server import server_config

KEY = "gw_reload_key"
KEY_HASH = hash_api_key(KEY)
AUTH = {"X-API-Key": KEY}


def ok(request):
    return httpx.Response(200, stream=httpx.ByteStream(b"ok"))


def base(**extra) -> dict:
    return {
        "reload_interval_seconds": 0,  # tests call reload_now() themselves
        "routes": [{"path_prefix": "/api", "upstream": "http://up-a"}],
        **extra,
    }


def with_key(config: dict, routes=("/api",)) -> dict:
    return {
        **config,
        "auth": {"keys": [{"client": "web", "key_sha256": KEY_HASH, "routes": list(routes)}]},
    }


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("GATEWAY_CONFIG", str(path))

    def write(config: dict | str) -> None:
        path.write_text(config if isinstance(config, str) else yaml.safe_dump(config))

    return write


@pytest.fixture
def gateway(config_file):
    clients = []

    def _start(config: dict, **app_kwargs) -> TestClient:
        config_file(config)
        app = create_app(transport=httpx.MockTransport(ok), **app_kwargs)
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield _start
    for client in clients:
        client.__exit__(None, None, None)


def reload(client) -> bool:
    return client.app.state.reloader.reload_now("test")


def metric(client, result) -> float:
    value = client.app.state.metrics.registry.get_sample_value(
        "gateway_config_reloads_total", {"result": result}
    )
    return value or 0.0


# --- changes that apply ----------------------------------------------------


def test_a_new_route_works_after_reload(gateway, config_file):
    client = gateway(base())
    assert client.get("/cpp/x").status_code == 404

    config_file(
        base(routes=[*base()["routes"], {"path_prefix": "/cpp", "upstream": "http://up-b"}])
    )
    assert reload(client) is True

    assert client.get("/cpp/x").status_code == 200
    assert metric(client, "success") == 1
    # The new upstream's failure counters start at 0 too (see test_metrics).
    assert (
        client.app.state.metrics.registry.get_sample_value(
            "gateway_upstream_retries_total", {"upstream": "http://up-b"}
        )
        == 0
    )


def test_a_revoked_key_stops_working_at_once(gateway, config_file):
    client = gateway(with_key(base()))
    assert client.get("/api/x", headers=AUTH).status_code == 200

    other = hash_api_key("some-other-key")
    config_file(
        {**base(), "auth": {"keys": [{"client": "x", "key_sha256": other, "routes": ["/api"]}]}}
    )
    reload(client)

    assert client.get("/api/x", headers=AUTH).status_code == 401


def test_rate_limiting_can_be_turned_on_without_a_restart(gateway, config_file):
    client = gateway(base())
    assert [client.get("/api/x").status_code for _ in range(3)] == [200, 200, 200]

    config_file(base(rate_limit={"default": {"capacity": 1, "refill_per_second": 0.001}}))
    reload(client)

    assert [client.get("/api/x").status_code for _ in range(2)] == [200, 429]


def test_an_unchanged_file_is_not_reloaded(gateway, config_file):
    client = gateway(base())
    assert reload(client) is False
    config_file(base())  # same content written again
    assert reload(client) is False
    assert metric(client, "success") == 0


def test_polling_picks_up_a_change_by_itself(gateway, config_file):
    client = gateway(base(reload_interval_seconds=0.05))
    config_file(
        base(
            reload_interval_seconds=0.05,
            routes=[*base()["routes"], {"path_prefix": "/cpp", "upstream": "http://up-b"}],
        )
    )
    deadline = time.monotonic() + 3
    while client.get("/cpp/x").status_code != 200:
        assert time.monotonic() < deadline, "polling never reloaded the file"
        time.sleep(0.02)


# --- changes that don't --------------------------------------------------


def test_broken_yaml_keeps_the_old_config(gateway, config_file, caplog):
    caplog.set_level(logging.INFO)
    client = gateway(base())
    config_file("routes: [this is: not: valid yaml")

    assert reload(client) is False
    assert client.get("/api/x").status_code == 200  # still serving the old config

    assert reload(client) is False  # same bad file again: not reported twice
    failures = [r for r in caplog.records if r.getMessage() == "config_reload_failed"]
    assert len(failures) == 1
    assert metric(client, "failed") == 1


def test_an_invalid_config_is_rejected_with_the_reason(gateway, config_file, caplog):
    caplog.set_level(logging.INFO)
    client = gateway(base())
    config_file({"routes": [{"path_prefix": "api", "upstream": "http://up-a"}]})  # no leading /

    assert reload(client) is False
    assert client.get("/api/x").status_code == 200
    [failure] = [r for r in caplog.records if r.getMessage() == "config_reload_failed"]
    assert any("path_prefix" in line for line in failure.details)


def test_fixing_the_file_recovers(gateway, config_file):
    client = gateway(base())
    config_file("not: [valid")
    reload(client)
    config_file(base(routes=[{"path_prefix": "/new", "upstream": "http://up-a"}]))

    assert reload(client) is True
    assert client.get("/new/x").status_code == 200


def test_a_deleted_file_keeps_the_old_config(gateway, tmp_path):
    client = gateway(base())
    (tmp_path / "config.yaml").unlink()

    assert reload(client) is False
    assert client.get("/api/x").status_code == 200
    assert metric(client, "failed") == 1


def test_settings_that_need_a_restart_are_rejected(gateway, config_file, caplog):
    caplog.set_level(logging.INFO)
    limit = {"default": {"capacity": 5, "refill_per_second": 1}}
    client = gateway(base(rate_limit=limit))
    config_file(base(rate_limit={**limit, "backend": "redis", "redis_url": "redis://r:6379"}))

    assert reload(client) is False
    [rejected] = [r for r in caplog.records if r.getMessage() == "config_reload_rejected"]
    assert rejected.needs_restart == ["rate_limit backend / redis settings"]
    assert client.app.state.config.rate_limit.backend == "memory"
    assert metric(client, "rejected") == 1


# --- consistency -----------------------------------------------------------


def test_a_request_keeps_the_config_it_started_with(gateway, config_file):
    """A reload lands halfway through a request: after the rate limiter, before
    routing. The request must finish under the config it started with, not
    pass the rate limiter under one and be routed under another."""
    without_api = base(
        routes=[{"path_prefix": "/other", "upstream": "http://up-a"}],
        rate_limit={"default": {"capacity": 10, "refill_per_second": 1}},
    )

    class ReloadingLimiter:
        """Swaps in a config without /api while the request is in flight."""

        def __init__(self):
            self.app = None

        async def hit(self, key: str, limit: Limit) -> Decision:
            config_file(without_api)
            self.app.state.reloader.reload_now("mid-request")
            return Decision(True, limit.capacity, limit.capacity - 1, 0)

        async def close(self):
            pass

    limiter = ReloadingLimiter()
    client = gateway(
        base(rate_limit={"default": {"capacity": 10, "refill_per_second": 1}}),
        rate_limiter=limiter,
    )
    limiter.app = client.app

    assert client.get("/api/x").status_code == 200  # routed under the old config
    assert client.get("/api/x").status_code == 404  # the next request sees the new one


def test_changed_breaker_settings_start_breakers_fresh(gateway, config_file):
    def down(request):
        raise httpx.ConnectError("down", request=request)

    breaker = {"failure_threshold": 1, "recovery_timeout_seconds": 60}
    config_file(base(circuit_breaker=breaker))
    app = create_app(transport=httpx.MockTransport(down))
    with TestClient(app) as client:
        client.get("/api/x")  # opens the breaker
        assert client.get("/api/x").json()["error"] == "upstream circuit open"

        config_file(base(circuit_breaker={**breaker, "failure_threshold": 5}))
        app.state.reloader.reload_now("test")

        # A fresh breaker with the new threshold: the upstream is tried again.
        assert client.get("/api/x").json()["error"] == "upstream unavailable"


def test_state_for_removed_upstreams_is_dropped(gateway, config_file):
    client = gateway(base(circuit_breaker={"failure_threshold": 5}))
    client.get("/api/x")
    assert "http://up-a" in client.app.state.guards.breakers()

    config_file(
        base(
            routes=[{"path_prefix": "/api", "upstream": "http://up-b"}],
            circuit_breaker={"failure_threshold": 5},
        )
    )
    reload(client)
    assert "http://up-a" not in client.app.state.guards.breakers()


# --- SIGHUP ----------------------------------------------------------------


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="no SIGHUP on this platform")
def test_sighup_reloads_at_once(config_file):
    """kill -HUP <pid>, the classic NGINX-style reload, against a real server.

    Polling is off, so only the signal can trigger the reload.
    """
    config_file(base())

    async def scenario():
        app = create_app(transport=httpx.MockTransport(ok))
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(server_config(app, log_config=None))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started:
            await asyncio.sleep(0.01)

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            before = (await client.get("/cpp/x")).status_code
            config_file(
                base(routes=[*base()["routes"], {"path_prefix": "/cpp", "upstream": "http://b"}])
            )
            # Without a handler, SIGHUP's default action terminates the
            # process: `kill -HUP` on a server that doesn't handle it shuts
            # it down. Check first, so a missing handler fails this test
            # instead of killing the whole test run.
            assert signal.getsignal(signal.SIGHUP) is not signal.SIG_DFL
            os.kill(os.getpid(), signal.SIGHUP)
            await asyncio.sleep(0.1)
            after = (await client.get("/cpp/x")).status_code

        server.should_exit = True
        await task
        return before, after

    assert asyncio.run(scenario()) == (404, 200)
