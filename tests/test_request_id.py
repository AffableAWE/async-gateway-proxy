import asyncio
import json
import logging
import uuid

import httpx
import pytest

from app.main import create_app


def echo(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"headers": dict(request.headers)})


def is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


# --- assigning the ID ----------------------------------------------------


def test_generates_an_id_when_the_client_sends_none(make_client):
    response = make_client(echo).get("/health")
    assert is_uuid(response.headers["x-request-id"])


def test_each_request_gets_a_different_id(make_client):
    client = make_client(echo)
    ids = {client.get("/health").headers["x-request-id"] for _ in range(5)}
    assert len(ids) == 5


def test_keeps_a_well_formed_client_id(make_client):
    response = make_client(echo).get("/health", headers={"X-Request-ID": "abc-123.def:4_5"})
    assert response.headers["x-request-id"] == "abc-123.def:4_5"


@pytest.mark.parametrize("bad", ["has spaces", "a;b", "x" * 129, "", "<script>"])
def test_replaces_a_malformed_client_id(make_client, bad):
    response = make_client(echo).get("/health", headers={"X-Request-ID": bad})
    returned = response.headers["x-request-id"]
    assert returned != bad
    assert is_uuid(returned)


# --- propagating it ------------------------------------------------------


def test_id_is_sent_to_the_upstream(make_client):
    response = make_client(echo).get("/api/x")
    seen_by_upstream = response.json()["headers"]["x-request-id"]
    assert seen_by_upstream == response.headers["x-request-id"]


def test_upstream_gets_one_id_even_if_client_sent_a_bad_one(make_client):
    response = make_client(echo).get("/api/x", headers={"X-Request-ID": "bad id"})
    seen_by_upstream = response.json()["headers"]["x-request-id"]
    assert seen_by_upstream == response.headers["x-request-id"]
    assert seen_by_upstream != "bad id"


def test_upstream_cannot_replace_the_id(make_client):
    def rogue(request):
        return httpx.Response(200, headers={"x-request-id": "upstream-made-this"}, content=b"ok")

    response = make_client(rogue).get("/api/x")
    assert response.headers.get_list("x-request-id") != ["upstream-made-this"]
    assert len(response.headers.get_list("x-request-id")) == 1


def test_error_bodies_carry_the_id(make_client):
    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    response = make_client(refused).get("/api/x")
    assert response.status_code == 502
    assert response.json()["request_id"] == response.headers["x-request-id"]


# --- logs ----------------------------------------------------------------


def test_every_log_line_of_a_request_has_its_id(make_client, caplog):
    caplog.set_level(logging.INFO)
    response = make_client(echo).get("/api/x")
    request_id = response.headers["x-request-id"]

    by_event = {r.getMessage(): r for r in caplog.records}
    assert by_event["proxy_forward"].request_id == request_id
    assert by_event["request_completed"].request_id == request_id


def test_access_log_line(make_client, caplog):
    caplog.set_level(logging.INFO)
    make_client(echo).get("/nope")

    [line] = [r for r in caplog.records if r.getMessage() == "request_completed"]
    assert line.method == "GET"
    assert line.path == "/nope"
    assert line.status_code == 404
    assert line.duration_ms >= 0


# --- concurrency ---------------------------------------------------------


class _OneChunk(httpx.AsyncByteStream):
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aiter__(self):
        yield self._data


def test_concurrent_requests_never_see_each_others_id(config, caplog):
    """The reason request_id is a ContextVar and not a global.

    20 requests run at once on one event loop. Each upstream call sleeps
    for a different time, so they finish in a shuffled order and interleave
    at every await. If the ID were a module-level global, requests would
    log each other's IDs here.

    The order must be shuffled, not just reversed: requests finishing in
    exact reverse order unwind like a stack, and a global would then
    restore the right value by luck.
    """
    caplog.set_level(logging.INFO)
    count = 20

    async def slow_echo(request: httpx.Request) -> httpx.Response:
        i = int(request.url.path.rsplit("/", 1)[-1])
        await asyncio.sleep(((i * 7) % count) * 0.003)  # 0, 7, 14, 1, 8, ...
        body = json.dumps({"seen": request.headers["x-request-id"]}).encode()
        return httpx.Response(200, stream=_OneChunk(body))

    app = create_app(config=config, transport=httpx.MockTransport(slow_echo))

    async def run():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
                return await asyncio.gather(
                    *(
                        client.get(f"/api/n/{i}", headers={"X-Request-ID": f"req-{i}"})
                        for i in range(count)
                    )
                )

    responses = asyncio.run(run())

    for i, response in enumerate(responses):
        assert response.headers["x-request-id"] == f"req-{i}"
        assert response.json() == {"seen": f"req-{i}"}

    forwards = [r for r in caplog.records if r.getMessage() == "proxy_forward"]
    assert len(forwards) == count
    for record in forwards:
        i = record.path.rsplit("/", 1)[-1]
        assert record.request_id == f"req-{i}"
