import gzip
import json

import httpx


def echo(request: httpx.Request) -> httpx.Response:
    """Fake upstream that reports back exactly what it received."""
    return httpx.Response(
        200,
        json={
            "method": request.method,
            "url": str(request.url),
            "headers": dict(request.headers),
            "body": request.content.decode(),
        },
    )


# --- routing -------------------------------------------------------------


def test_forwards_method_path_and_query(make_client):
    client = make_client(echo)
    response = client.get("/api/items/42?a=1&b=two")

    assert response.status_code == 200
    seen = response.json()
    assert seen["method"] == "GET"
    assert seen["url"] == "http://fake-upstream/items/42?a=1&b=two"


def test_forwards_json_body(make_client):
    client = make_client(echo)
    response = client.post("/api/echo", json={"hello": "world"})

    seen = response.json()
    assert seen["method"] == "POST"
    assert json.loads(seen["body"]) == {"hello": "world"}


def test_unknown_route_returns_404(make_client):
    def must_not_be_called(request):
        raise AssertionError("unmatched routes must not reach the upstream")

    client = make_client(must_not_be_called)
    response = client.get("/nope")

    assert response.status_code == 404
    assert response.json() == {
        "error": "no matching route",
        "path": "/nope",
        "request_id": response.headers["x-request-id"],
    }


def test_upstream_error_status_passes_through(make_client):
    # A 500 from the upstream is the upstream's answer, not a gateway failure.
    client = make_client(lambda req: httpx.Response(500, json={"error": "boom"}))
    response = client.get("/api/x")

    assert response.status_code == 500
    assert response.json() == {"error": "boom"}


# --- headers -------------------------------------------------------------


def test_request_headers_are_cleaned_and_annotated(make_client):
    client = make_client(echo)
    response = client.get(
        "/api/x",
        headers={
            "X-Custom": "kept",
            "Keep-Alive": "timeout=5",  # hop-by-hop
            "Connection": "X-Debug",  # names another hop-by-hop header
            "X-Debug": "drop-me",
            "X-Forwarded-For": "10.0.0.1",  # set by an earlier proxy
        },
    )

    headers = response.json()["headers"]
    assert headers["x-custom"] == "kept"
    assert "keep-alive" not in headers
    assert "x-debug" not in headers
    assert headers["host"] == "fake-upstream"  # set by httpx, not copied
    assert headers["x-forwarded-for"] == "10.0.0.1, testclient"
    assert headers["x-forwarded-proto"] == "http"


def test_gateway_adds_no_headers_of_its_own(make_client):
    client = make_client(echo)
    # The test client sends Accept-Encoding by default; remove it to act
    # like a client (curl, for example) that never asked for compression.
    del client.headers["accept-encoding"]
    response = client.get("/api/x", headers={"User-Agent": "my-client/1.0"})

    headers = response.json()["headers"]
    assert "accept-encoding" not in headers  # httpx's default was not injected
    assert headers["user-agent"] == "my-client/1.0"  # not python-httpx


def test_repeated_response_headers_are_kept(make_client):
    def two_cookies(request):
        return httpx.Response(
            200,
            headers=[("set-cookie", "a=1"), ("set-cookie", "b=2")],
            content=b"ok",
        )

    client = make_client(two_cookies)
    response = client.get("/api/x")

    assert response.headers.get_list("set-cookie") == ["a=1", "b=2"]


def test_compressed_body_is_forwarded_untouched(make_client):
    def gzipped(request):
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip", "content-type": "text/plain"},
            content=gzip.compress(b"hello"),
        )

    client = make_client(gzipped)
    response = client.get("/api/x")

    # The test client decompresses it. If the gateway had decompressed it
    # too, this would fail with a decoding error.
    assert response.headers["content-encoding"] == "gzip"
    assert response.text == "hello"


# --- failures ------------------------------------------------------------


def test_upstream_timeout_returns_504(make_client):
    def slow(request):
        raise httpx.ReadTimeout("simulated timeout", request=request)

    client = make_client(slow)
    response = client.get("/api/slow")

    assert response.status_code == 504
    assert response.json() == {
        "error": "upstream timeout",
        "request_id": response.headers["x-request-id"],
    }


def test_upstream_unreachable_returns_502(make_client):
    def refused(request):
        raise httpx.ConnectError("simulated connection refused", request=request)

    client = make_client(refused)
    response = client.get("/api/x")

    assert response.status_code == 502
    assert response.json() == {
        "error": "upstream unavailable",
        "request_id": response.headers["x-request-id"],
    }
