"""Graceful shutdown, with a real uvicorn server running inside the test.

server.should_exit = True is exactly what uvicorn's SIGTERM handler does,
so this is a deploy or a `kubectl rollout` in miniature: a response is
still streaming when the signal arrives.
"""

import asyncio
import logging
import os
import signal
import socket
import subprocess
import sys
import time

import httpx
import pytest
import uvicorn

from app.config import GatewayConfig
from app.main import create_app
from app.server import server_config


class GatedBody(httpx.AsyncByteStream):
    """Sends the first chunk, then waits for the test before sending the rest."""

    def __init__(self, gate: asyncio.Event) -> None:
        self.gate = gate

    async def __aiter__(self):
        yield b"first half,"
        await self.gate.wait()
        yield b" second half"


def free_socket() -> socket.socket:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    return sock


async def start(app, graceful_timeout: int):
    sock = free_socket()
    config = server_config(app, timeout_graceful_shutdown=graceful_timeout, log_config=None)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.01)
    return server, task, sock.getsockname()[1]


CONFIG = GatewayConfig.model_validate(
    {"routes": [{"path_prefix": "/api", "upstream": "http://up"}]}
)


def test_in_flight_stream_finishes_after_sigterm(caplog):
    caplog.set_level(logging.INFO)

    async def scenario():
        gate = asyncio.Event()
        upstream_called = asyncio.Event()

        async def upstream(request):
            upstream_called.set()
            return httpx.Response(200, stream=GatedBody(gate))

        app = create_app(config=CONFIG, transport=httpx.MockTransport(upstream))
        server, serve_task, port = await start(app, graceful_timeout=10)
        url = f"http://127.0.0.1:{port}/api/x"

        async with httpx.AsyncClient() as client:
            in_flight = asyncio.create_task(client.get(url))
            await upstream_called.wait()
            await asyncio.sleep(0.05)  # first half is on its way to the client

            server.should_exit = True  # SIGTERM
            await asyncio.sleep(0.3)

            # Draining: no new connections...
            with pytest.raises(httpx.ConnectError):
                async with httpx.AsyncClient() as fresh:
                    await fresh.get(url)
            # ...and the app's shutdown hasn't run: the stream still needs it.
            assert not app.state.http_client.is_closed

            gate.set()  # the upstream finishes its body
            response = await in_flight

        await asyncio.wait_for(serve_task, timeout=5)
        return response, app

    response, app = asyncio.run(scenario())

    assert response.status_code == 200
    assert response.text == "first half, second half"  # nothing cut off
    assert app.state.http_client.is_closed  # closed only after the drain

    events = [r.getMessage() for r in caplog.records]
    assert events.index("proxy_forward") < events.index("gateway_shutdown")


def test_drain_gives_up_after_the_graceful_timeout():
    async def scenario():
        gate = asyncio.Event()  # never set: the stream never ends

        async def upstream(request):
            return httpx.Response(200, stream=GatedBody(gate))

        app = create_app(config=CONFIG, transport=httpx.MockTransport(upstream))
        server, serve_task, port = await start(app, graceful_timeout=1)

        async with httpx.AsyncClient() as client:
            in_flight = asyncio.create_task(client.get(f"http://127.0.0.1:{port}/api/x"))
            await asyncio.sleep(0.2)
            loop = asyncio.get_running_loop()
            began = loop.time()
            server.should_exit = True
            await asyncio.wait_for(serve_task, timeout=5)
            stopped_after = loop.time() - began
            with pytest.raises(httpx.HTTPError):  # the client was cut off
                await in_flight
        return stopped_after, app

    stopped_after, app = asyncio.run(scenario())
    assert 0.9 < stopped_after < 3  # waited about the timeout, not forever
    assert app.state.http_client.is_closed


def test_server_flags(monkeypatch):
    monkeypatch.setenv("GATEWAY_PORT", "9123")
    monkeypatch.setenv("GATEWAY_GRACEFUL_TIMEOUT_SECONDS", "7")
    config = server_config()

    assert config.port == 9123
    assert config.host == "127.0.0.1"
    assert config.timeout_graceful_shutdown == 7
    assert config.proxy_headers is False
    assert config.access_log is False
    assert config.lifespan == "on"


def test_sigterm_drains_and_exits_0(tmp_path):
    # The real process, the real signal: what `docker stop` and Kubernetes
    # do. Exit code 0 matters: 143 ("killed by SIGTERM") makes every normal
    # rollout show up as an Error in `kubectl describe pod`.
    config = tmp_path / "config.yaml"
    config.write_text("routes:\n  - path_prefix: /api\n    upstream: http://127.0.0.1:9\n")
    port = free_socket().getsockname()[1]
    env = {
        **os.environ,
        "GATEWAY_CONFIG": str(config),
        "GATEWAY_PORT": str(port),
        "GATEWAY_METRICS_PORT": "0",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "app"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            assert proc.poll() is None, proc.stdout.read()
            assert time.monotonic() < deadline, "gateway didn't start"
            time.sleep(0.1)

        proc.send_signal(signal.SIGTERM)
        output, _ = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()

    assert proc.returncode == 0, output
    assert "gateway_shutdown" in output  # the drain and the app's shutdown ran
