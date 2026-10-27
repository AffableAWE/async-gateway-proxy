import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import GatewayConfig
from app.main import create_app

UPSTREAM = "http://fake-upstream"


@pytest.fixture
def config() -> GatewayConfig:
    return GatewayConfig.model_validate(
        {
            "upstream_timeout_seconds": 1.0,
            "routes": [{"path_prefix": "/api", "upstream": UPSTREAM}],
        }
    )


class _OneChunk(httpx.AsyncByteStream):
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aiter__(self):
        yield self._data


def _like_network(handler):
    """Make MockTransport responses behave like real network responses.

    A response built from bytes, e.g. httpx.Response(200, json=...), is
    already read into memory, so aiter_raw() refuses it. A real upstream
    response is an unread stream. This wrapper re-wraps the raw bytes as an
    unread stream so the proxy code runs exactly as it would in production.
    """

    def wrapped(request: httpx.Request) -> httpx.Response:
        response = handler(request)
        raw = b"".join(response.stream)  # bytes as sent, before any gunzip
        return httpx.Response(
            response.status_code,
            headers=response.headers.multi_items(),
            stream=_OneChunk(raw),
        )

    return wrapped


@pytest.fixture
def make_client(config):
    """Build a TestClient whose outbound httpx calls go to `handler`.

    httpx.MockTransport sits where the network would be. The gateway code
    runs unchanged; only the bottom layer is swapped.
    """
    clients = []

    def _make(handler) -> TestClient:
        transport = httpx.MockTransport(_like_network(handler))
        app = create_app(config=config, transport=transport)
        client = TestClient(app)
        client.__enter__()  # runs the lifespan startup
        clients.append(client)
        return client

    yield _make

    for client in clients:
        client.__exit__(None, None, None)  # runs the lifespan shutdown
