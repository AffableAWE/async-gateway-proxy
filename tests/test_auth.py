import logging

import httpx
import pytest

from app.config import GatewayConfig
from app.security import hash_api_key

WEB_KEY = "gw_test_web_key"
WEB_KEY_NEW = "gw_test_web_key_rotated"
CPP_ONLY_KEY = "gw_test_cpp_only_key"


@pytest.fixture
def config() -> GatewayConfig:
    """Overrides conftest's config: two routes, auth on, three keys."""
    return GatewayConfig.model_validate(
        {
            "routes": [
                {"path_prefix": "/api", "upstream": "http://fake-upstream"},
                {"path_prefix": "/cpp", "upstream": "http://fake-cpp"},
            ],
            "auth": {
                "keys": [
                    {"client": "web", "key_sha256": hash_api_key(WEB_KEY), "routes": ["/api"]},
                    # Same client, second key: how a key is rotated without downtime.
                    {"client": "web", "key_sha256": hash_api_key(WEB_KEY_NEW), "routes": ["/api"]},
                    {
                        "client": "batch",
                        "key_sha256": hash_api_key(CPP_ONLY_KEY),
                        "routes": ["/cpp"],
                    },
                ]
            },
        }
    )


def echo(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"headers": dict(request.headers)})


def never_called(request: httpx.Request) -> httpx.Response:
    raise AssertionError("a rejected request must not reach the upstream")


def key(value: str) -> dict:
    return {"X-API-Key": value}


# --- authentication (401) --------------------------------------------------


def test_health_needs_no_key(make_client):
    assert make_client(never_called).get("/health").status_code == 200


def test_missing_key_is_401(make_client):
    response = make_client(never_called).get("/api/x")

    assert response.status_code == 401
    assert response.json() == {
        "error": "missing api key",
        "request_id": response.headers["x-request-id"],
    }
    assert response.headers["www-authenticate"] == 'ApiKey header="X-API-Key"'


@pytest.mark.parametrize("bad", ["wrong", WEB_KEY + "x", WEB_KEY.upper(), " "])
def test_wrong_key_is_401(make_client, bad):
    response = make_client(never_called).get("/api/x", headers=key(bad))
    assert response.status_code == 401


def test_unknown_path_without_key_is_401_not_404(make_client):
    # Without a key you can't even find out which routes exist.
    assert make_client(never_called).get("/nope").status_code == 401


# --- authorization (403) and routing ---------------------------------------


def test_valid_key_is_proxied(make_client):
    response = make_client(echo).get("/api/x", headers=key(WEB_KEY))
    assert response.status_code == 200


def test_rotated_key_works_too(make_client):
    response = make_client(echo).get("/api/x", headers=key(WEB_KEY_NEW))
    assert response.status_code == 200


def test_key_on_a_route_it_may_not_use_is_403(make_client):
    response = make_client(never_called).get("/api/x", headers=key(CPP_ONLY_KEY))

    assert response.status_code == 403
    assert response.json()["error"] == "api key not allowed for this route"
    assert response.json()["request_id"] == response.headers["x-request-id"]


def test_each_key_reaches_its_own_route(make_client):
    client = make_client(echo)
    assert client.get("/cpp/x", headers=key(CPP_ONLY_KEY)).status_code == 200
    assert client.get("/cpp/x", headers=key(WEB_KEY)).status_code == 403


def test_unknown_path_with_valid_key_is_404(make_client):
    response = make_client(never_called).get("/nope", headers=key(WEB_KEY))
    assert response.status_code == 404


# --- what the upstream sees ------------------------------------------------


def test_upstream_gets_client_name_but_not_the_key(make_client):
    response = make_client(echo).get("/api/x", headers=key(WEB_KEY))
    seen = response.json()["headers"]

    assert "x-api-key" not in seen
    assert seen["x-gateway-client"] == "web"


def test_client_cannot_fake_its_identity(make_client):
    response = make_client(echo).get(
        "/api/x", headers={**key(WEB_KEY), "X-Gateway-Client": "admin"}
    )
    seen = response.json()["headers"]
    assert seen["x-gateway-client"] == "web"


# --- logs ------------------------------------------------------------------


def test_rejections_are_logged_without_the_key(make_client, caplog):
    caplog.set_level(logging.INFO)
    make_client(never_called).get("/api/x", headers=key("gw_some_wrong_key"))

    [rejected] = [r for r in caplog.records if r.getMessage() == "auth_rejected"]
    assert rejected.reason == "invalid api key"
    assert "gw_some_wrong_key" not in caplog.text


def test_access_log_names_the_client(make_client, caplog):
    caplog.set_level(logging.INFO)
    make_client(echo).get("/api/x", headers=key(WEB_KEY))

    [line] = [r for r in caplog.records if r.getMessage() == "request_completed"]
    assert line.client == "web"
    assert line.status_code == 200


def test_denied_access_is_logged(make_client, caplog):
    caplog.set_level(logging.INFO)
    make_client(never_called).get("/api/x", headers=key(CPP_ONLY_KEY))

    [denied] = [r for r in caplog.records if r.getMessage() == "access_denied"]
    assert denied.client == "batch"
    assert denied.route == "/api"
