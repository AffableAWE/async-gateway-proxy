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
