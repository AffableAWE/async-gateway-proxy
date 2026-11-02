from fastapi.testclient import TestClient

from dummy_upstream.main import app

client = TestClient(app)


def test_items():
    assert client.get("/items/7").json() == {"item_id": 7, "name": "item-7"}


def test_echo_returns_body_and_headers():
    response = client.post("/echo?x=1", json={"a": 1}, headers={"X-Test": "yes"})
    data = response.json()
    assert data["method"] == "POST"
    assert data["query"] == "x=1"
    assert data["headers"]["x-test"] == "yes"
    assert data["body"] == '{"a":1}'


def test_slow_answers_after_the_delay():
    assert client.get("/slow?delay=0").json() == {"slept_seconds": 0}


def test_slow_rejects_out_of_range_delays():
    assert client.get("/slow?delay=31").status_code == 422
    assert client.get("/slow?delay=-1").status_code == 422


def test_flaky_fails_then_succeeds():
    codes = [client.get("/flaky?key=t1&fail=2").status_code for _ in range(4)]
    assert codes == [503, 503, 200, 200]


def test_flaky_keys_are_independent():
    client.get("/flaky?key=t2&fail=1")
    assert client.get("/flaky?key=t3&fail=1").status_code == 503


def test_status_returns_the_code():
    assert client.get("/status/503").status_code == 503
    assert client.get("/status/204").status_code == 204
    assert client.get("/status/99").status_code == 400


def test_large_sends_exactly_the_size_asked_for():
    response = client.get("/large?mb=1")
    assert response.headers["content-length"] == str(1024 * 1024)
    assert len(response.content) == 1024 * 1024


def test_stream_sends_each_chunk():
    response = client.get("/stream?chunks=3&interval=0")
    assert response.text == "chunk 1/3\nchunk 2/3\nchunk 3/3\n"
    assert "content-length" not in response.headers


def test_upload_reports_size_and_hash():
    import hashlib

    body = b"x" * 100_000
    data = client.post("/upload", content=body).json()
    assert data["bytes"] == 100_000
    assert data["sha256"] == hashlib.sha256(body).hexdigest()
