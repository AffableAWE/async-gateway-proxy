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
