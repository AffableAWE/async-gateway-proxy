"""Streaming bodies, driven through raw ASGI.

TestClient collects the whole response before returning it, so it can't
show whether the gateway streams. Here the test plays the server: it hands
the app ASGI messages and records every message the app sends back.
"""

import asyncio
import hashlib
import logging

import httpx
import pytest

from app.config import GatewayConfig
from app.main import create_app


class UpstreamBody(httpx.AsyncByteStream):
    """An upstream response body that records how far it has been read."""

    def __init__(self, chunks: list[bytes], fail_after: int | None = None) -> None:
        self.chunks = chunks
        self.fail_after = fail_after
        self.yielded = 0
        self.closed = False

    async def __aiter__(self):
        for i, chunk in enumerate(self.chunks):
            if self.fail_after is not None and i == self.fail_after:
                raise httpx.ReadError("upstream went away")
            self.yielded += 1
            yield chunk
            await asyncio.sleep(0)  # let the gateway pass it on before the next

    async def aclose(self) -> None:
        self.closed = True


def make_config(**extra) -> GatewayConfig:
    return GatewayConfig.model_validate(
        {"routes": [{"path_prefix": "/api", "upstream": "http://up"}], **extra}
    )


async def call(
    app,
    method="GET",
    path="/api/x",
    headers=(),
    body_chunks=(b"",),
    disconnect_after=None,
):
    """Run one request through the app. Returns (messages sent, error raised).

    disconnect_after=N: the client hangs up once N body chunks have arrived.
    """
    sent: list[dict] = []
    hung_up = asyncio.Event()
    queue = [
        {"type": "http.request", "body": chunk, "more_body": i < len(body_chunks) - 1}
        for i, chunk in enumerate(body_chunks)
    ]

    async def receive():
        if queue:
            return queue.pop(0)
        if disconnect_after is None:
            await asyncio.Event().wait()  # the client never hangs up
        await hung_up.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        body_messages = [m for m in sent if m["type"] == "http.response.body" and m["body"]]
        if disconnect_after is not None and len(body_messages) >= disconnect_after:
            hung_up.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"gateway")] + [(k.encode(), v.encode()) for k, v in headers],
        "client": ("127.0.0.1", 5000),
        "server": ("gateway", 80),
    }
    error = None
    async with app.router.lifespan_context(app):
        try:
            await app(scope, receive, send)
        except Exception as exc:
            error = exc
    return sent, error


def run(coro):
    return asyncio.run(coro)


def body_of(messages) -> bytes:
    return b"".join(m["body"] for m in messages if m["type"] == "http.response.body")


def response_headers(messages) -> dict[str, str]:
    start = next(m for m in messages if m["type"] == "http.response.start")
    return {k.decode(): v.decode() for k, v in start["headers"]}


# --- responses -------------------------------------------------------------


def test_response_is_passed_on_chunk_by_chunk():
    upstream_body = UpstreamBody([b"a" * 1000, b"b" * 1000, b"c" * 1000])

    def handler(request):
        return httpx.Response(200, stream=upstream_body)

    app = create_app(config=make_config(), transport=httpx.MockTransport(handler))
    messages, error = run(call(app))
    assert error is None
    chunks = [m["body"] for m in messages if m["type"] == "http.response.body" and m["body"]]
    # Three separate body messages, not one buffered blob.
    assert chunks == [b"a" * 1000, b"b" * 1000, b"c" * 1000]
    assert body_of(messages) == b"a" * 1000 + b"b" * 1000 + b"c" * 1000
    assert upstream_body.closed


def test_first_chunk_reaches_the_client_before_the_upstream_finishes():
    upstream_body = UpstreamBody([b"1", b"2", b"3", b"4"])
    read_so_far_at_first_send = []

    def handler(request):
        return httpx.Response(200, stream=upstream_body)

    app = create_app(config=make_config(), transport=httpx.MockTransport(handler))

    async def scenario():
        sent = []

        async def receive():
            if not sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.Event().wait()

        async def send(message):
            if message["type"] == "http.response.body" and message["body"]:
                if not read_so_far_at_first_send:
                    read_so_far_at_first_send.append(upstream_body.yielded)
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/x",
            "raw_path": b"/api/x",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"gateway")],
            "client": ("127.0.0.1", 5000),
            "server": ("gateway", 80),
        }
        async with app.router.lifespan_context(app):
            await app(scope, receive, send)

    run(scenario())
    # When the client got chunk 1, the upstream had produced only chunk 1.
    assert read_so_far_at_first_send == [1]


def test_upstream_content_length_is_kept():
    def handler(request):
        return httpx.Response(
            200, headers={"content-length": "6"}, stream=UpstreamBody([b"abc", b"def"])
        )

    app = create_app(config=make_config(), transport=httpx.MockTransport(handler))
    messages, _ = run(call(app))
    assert response_headers(messages)["content-length"] == "6"


def test_no_content_length_when_the_upstream_sent_none():
    def handler(request):
        return httpx.Response(200, stream=UpstreamBody([b"abc", b"def"]))

    app = create_app(config=make_config(), transport=httpx.MockTransport(handler))
    messages, _ = run(call(app))
    # The server then frames it as chunked; the gateway never guesses a length.
    assert "content-length" not in response_headers(messages)


def test_upstream_breaking_mid_body_cuts_the_client_off(caplog):
    caplog.set_level(logging.INFO)
    upstream_body = UpstreamBody([b"part1", b"part2", b"part3"], fail_after=2)

    def handler(request):
        return httpx.Response(200, stream=upstream_body)

    app = create_app(config=make_config(), transport=httpx.MockTransport(handler))
    messages, error = run(call(app))

    assert body_of(messages) == b"part1part2"
    # The app raises instead of finishing: finishing would send the final
    # empty chunk and the truncated body would look complete to the client.
    assert type(error).__name__ == "UpstreamStreamError"
    assert not any(m.get("more_body") is False for m in messages)
    assert upstream_body.closed

    [aborted] = [r for r in caplog.records if r.getMessage() == "upstream_stream_aborted"]
    assert aborted.response_bytes == 10
    [access] = [r for r in caplog.records if r.getMessage() == "request_completed"]
    assert access.error == "UpstreamStreamError"


def test_client_hanging_up_releases_the_upstream(caplog):
    caplog.set_level(logging.INFO)
    upstream_body = UpstreamBody([b"x" * 100] * 50)

    def handler(request):
        return httpx.Response(200, stream=upstream_body)

    app = create_app(config=make_config(), transport=httpx.MockTransport(handler))
    messages, error = run(call(app, disconnect_after=2))

    assert error is None
    assert upstream_body.yielded < 50  # stopped reading early
    assert upstream_body.closed  # and gave the connection back
    assert any(r.getMessage() == "client_disconnected" for r in caplog.records)


# --- requests --------------------------------------------------------------


def upload_handler(seen: dict, statuses=(200,)):
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        body = await request.aread()
        seen.update(
            calls=len(calls),
            sha=hashlib.sha256(body).hexdigest(),
            size=len(body),
            content_length=request.headers.get("content-length"),
            transfer_encoding=request.headers.get("transfer-encoding"),
        )
        # stream=..., not content=...: a response built from bytes counts as
        # already read, and the gateway streams it with aiter_raw().
        status = statuses[min(len(calls), len(statuses)) - 1]
        return httpx.Response(status, stream=httpx.ByteStream(b"done"))

    return handler


BIG = [b"z" * 4096] * 64  # 256 KiB in 64 chunks
BIG_SHA = hashlib.sha256(b"".join(BIG)).hexdigest()


def test_large_upload_arrives_intact_with_its_content_length(caplog):
    caplog.set_level(logging.INFO)
    seen: dict = {}
    config = make_config(max_buffered_request_bytes=1024)
    app = create_app(config=config, transport=httpx.MockTransport(upload_handler(seen)))

    messages, _ = run(
        call(app, "POST", headers=[("content-length", str(4096 * 64))], body_chunks=BIG)
    )

    assert body_of(messages) == b"done"
    assert (seen["size"], seen["sha"]) == (4096 * 64, BIG_SHA)
    # Passed on as a plain Content-Length body, not switched to chunked.
    assert seen["content_length"] == str(4096 * 64)
    assert seen["transfer_encoding"] is None

    [forward] = [r for r in caplog.records if r.getMessage() == "proxy_forward"]
    assert forward.streamed_request is True
    assert forward.request_bytes == 4096 * 64


def test_chunked_upload_without_content_length_stays_chunked():
    seen: dict = {}
    config = make_config(max_buffered_request_bytes=1024)
    app = create_app(config=config, transport=httpx.MockTransport(upload_handler(seen)))

    run(call(app, "POST", body_chunks=BIG))  # no content-length: the client sent chunked

    assert seen["sha"] == BIG_SHA
    assert seen["transfer_encoding"] == "chunked"


def test_small_body_is_buffered_and_retried():
    seen: dict = {}
    config = make_config(retries={"max_attempts": 3, "base_delay_seconds": 0})
    handler = upload_handler(seen, statuses=(503, 200))
    app = create_app(config=config, transport=httpx.MockTransport(handler))

    messages, _ = run(call(app, "PUT", body_chunks=[b"small ", b"body"]))

    assert seen["calls"] == 2  # retried
    assert seen["size"] == len(b"small body")  # and the body was sent again whole
    assert body_of(messages) == b"done"


def test_streamed_body_is_not_retried():
    # Its bytes were read once, by the first attempt; they can't be resent.
    seen: dict = {}
    config = make_config(
        max_buffered_request_bytes=1024, retries={"max_attempts": 3, "base_delay_seconds": 0}
    )
    handler = upload_handler(seen, statuses=(503, 200))
    app = create_app(config=config, transport=httpx.MockTransport(handler))

    messages, _ = run(call(app, "PUT", body_chunks=BIG))

    assert seen["calls"] == 1
    start = next(m for m in messages if m["type"] == "http.response.start")
    assert start["status"] == 503


def test_body_without_content_length_is_streamed_once_it_passes_the_limit():
    seen: dict = {}
    config = make_config(max_buffered_request_bytes=10_000)
    app = create_app(config=config, transport=httpx.MockTransport(upload_handler(seen)))

    run(call(app, "POST", body_chunks=BIG))  # 256 KiB, no content-length

    # The first 10 KB were buffered, then it switched to streaming; the
    # upstream still got every byte, in order.
    assert seen["sha"] == BIG_SHA


@pytest.mark.parametrize("limit", [0, 1024 * 1024])
def test_empty_body_works_at_any_limit(limit):
    seen: dict = {}
    config = make_config(max_buffered_request_bytes=limit)
    app = create_app(config=config, transport=httpx.MockTransport(upload_handler(seen)))
    messages, error = run(call(app, "GET"))
    assert error is None and seen["size"] == 0
