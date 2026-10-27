import pytest

from app.config import GatewayConfig
from app.router import build_upstream_url, match_route


@pytest.fixture
def config() -> GatewayConfig:
    return GatewayConfig.model_validate(
        {
            "routes": [
                {"path_prefix": "/users", "upstream": "http://users:8000"},
                {"path_prefix": "/users/admin", "upstream": "http://admin:8000"},
                {"path_prefix": "/service-a", "upstream": "http://a:8000"},
            ]
        }
    )


@pytest.mark.parametrize(
    ("path", "expected_upstream"),
    [
        ("/users", "http://users:8000"),
        ("/users/42", "http://users:8000"),
        ("/users/admin", "http://admin:8000"),  # longest prefix wins
        ("/users/admin/settings", "http://admin:8000"),
        ("/service-a/hello", "http://a:8000"),
    ],
)
def test_match_route(config, path, expected_upstream):
    route = match_route(path, config)
    assert route is not None
    assert route.upstream == expected_upstream


@pytest.mark.parametrize("path", ["/", "/nope", "/usersx", "/service-abc/hello"])
def test_no_match_on_partial_segment(config, path):
    # '/service-abc' must not match '/service-a'. This was the bug in the
    # old find_upstream().
    assert match_route(path, config) is None


def test_root_prefix_matches_everything():
    config = GatewayConfig.model_validate(
        {"routes": [{"path_prefix": "/", "upstream": "http://default:8000"}]}
    )
    route = match_route("/anything/at/all", config)
    assert route is not None
    assert build_upstream_url("/anything/at/all", route) == "http://default:8000/anything/at/all"


@pytest.mark.parametrize(
    ("path", "query", "expected"),
    [
        ("/users/42", "", "http://users:8000/42"),
        ("/users", "", "http://users:8000/"),
        ("/users/42", "a=1&b=2", "http://users:8000/42?a=1&b=2"),
        ("/users/search", "q=a%20b&q=c", "http://users:8000/search?q=a%20b&q=c"),
    ],
)
def test_build_upstream_url(config, path, query, expected):
    route = match_route(path, config)
    assert route is not None
    assert build_upstream_url(path, route, query) == expected
