import pytest
from pydantic import ValidationError

from app.config import ConfigError, GatewayConfig, load_config


def write(tmp_path, text: str):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_loads_valid_config(tmp_path):
    path = write(
        tmp_path,
        """
upstream_timeout_seconds: 2.5
routes:
  - path_prefix: /api/
    upstream: http://localhost:8001/
""",
    )
    config = load_config(path)
    assert config.upstream_timeout_seconds == 2.5
    # Trailing slashes are normalized away at load time.
    assert config.routes[0].path_prefix == "/api"
    assert config.routes[0].upstream == "http://localhost:8001"


def test_repo_config_yaml_is_valid():
    load_config("config.yaml")


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")


def test_empty_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, ""))


@pytest.mark.parametrize(
    "route",
    [
        {"path_prefix": "api", "upstream": "http://x:1"},  # no leading slash
        {"path_prefix": "/api", "upstream": "localhost:8001"},  # no scheme
        {"path_prefix": "/api", "upstrem": "http://x:1"},  # typo in key
    ],
)
def test_bad_route_is_rejected(route):
    with pytest.raises(ValidationError):
        GatewayConfig.model_validate({"routes": [route]})


def test_duplicate_prefix_is_rejected():
    with pytest.raises(ValidationError):
        GatewayConfig.model_validate(
            {
                "routes": [
                    {"path_prefix": "/api", "upstream": "http://a:1"},
                    {"path_prefix": "/api/", "upstream": "http://b:1"},
                ]
            }
        )


def test_routes_sorted_longest_first():
    config = GatewayConfig.model_validate(
        {
            "routes": [
                {"path_prefix": "/users", "upstream": "http://a:1"},
                {"path_prefix": "/users/admin", "upstream": "http://b:1"},
            ]
        }
    )
    assert [r.path_prefix for r in config.routes] == ["/users/admin", "/users"]


# --- auth ------------------------------------------------------------------

HASH = "a" * 64


def with_auth(keys):
    return {
        "routes": [
            {"path_prefix": "/api", "upstream": "http://a:1"},
            {"path_prefix": "/cpp", "upstream": "http://b:1"},
        ],
        "auth": {"keys": keys},
    }


def test_auth_is_off_when_the_section_is_missing():
    config = GatewayConfig.model_validate(
        {"routes": [{"path_prefix": "/a", "upstream": "http://a:1"}]}
    )
    assert config.auth is None


def test_valid_auth_section():
    config = GatewayConfig.model_validate(
        with_auth([{"client": "web", "key_sha256": HASH.upper(), "routes": ["/api/"]}])
    )
    entry = config.auth.keys[0]
    assert entry.key_sha256 == HASH  # lowercased
    assert entry.routes == ["/api"]  # trailing slash normalized


def test_lookup_finds_a_key_by_its_hash():
    from app.security import hash_api_key

    config = GatewayConfig.model_validate(
        with_auth([{"client": "web", "key_sha256": hash_api_key("secret"), "routes": ["/api"]}])
    )
    assert config.auth.lookup("secret").client == "web"
    assert config.auth.lookup("wrong") is None


@pytest.mark.parametrize(
    "key",
    [
        {"client": "web", "key_sha256": "not-a-hash", "routes": ["/api"]},
        {"client": "web", "key_sha256": HASH, "routes": []},  # no routes
        {"client": "web", "key_sha256": HASH, "routes": ["/aip"]},  # unknown route
        {"client": "has space", "key_sha256": HASH, "routes": ["/api"]},
        {"client": "web", "key_sha256": HASH, "routes": ["/api"], "key": "plain"},  # extra key
    ],
)
def test_bad_key_entry_is_rejected(key):
    with pytest.raises(ValidationError):
        GatewayConfig.model_validate(with_auth([key]))


def test_same_hash_twice_is_rejected():
    entry = {"client": "web", "key_sha256": HASH, "routes": ["/api"]}
    with pytest.raises(ValidationError):
        GatewayConfig.model_validate(with_auth([entry, {**entry, "client": "other"}]))


def test_one_client_may_have_two_keys_for_rotation():
    config = GatewayConfig.model_validate(
        with_auth(
            [
                {"client": "web", "key_sha256": "a" * 64, "routes": ["/api"]},
                {"client": "web", "key_sha256": "b" * 64, "routes": ["/api"]},
            ]
        )
    )
    assert [k.client for k in config.auth.keys] == ["web", "web"]
