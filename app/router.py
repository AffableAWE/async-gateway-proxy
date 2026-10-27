"""Routing decision: which upstream should this path go to?

Pure functions, no I/O. Given a path and the config, return a route and the
upstream URL. Keeping this apart from proxy.py means routing rules can be
tested with plain strings, and the forwarding code never has to know how a
route was chosen.
"""

from app.config import GatewayConfig, RouteConfig


def _prefix_matches(path: str, prefix: str) -> bool:
    if prefix == "/":
        return True
    # Match on a path-segment boundary only: '/api' must match '/api' and
    # '/api/x', but never '/apix'.
    return path == prefix or path.startswith(prefix + "/")


def match_route(path: str, config: GatewayConfig) -> RouteConfig | None:
    """Return the most specific route for `path`, or None.

    config.routes is already sorted longest prefix first (see models.py),
    so the first match is the longest match.
    """
    for route in config.routes:
        if _prefix_matches(path, route.path_prefix):
            return route
    return None


def build_upstream_url(path: str, route: RouteConfig, query: str = "") -> str:
    """Strip the matched prefix and append the rest to the upstream base URL.

    prefix '/api', upstream 'http://svc:8000':
        '/api/items/42' -> 'http://svc:8000/items/42'
        '/api'          -> 'http://svc:8000/'
    prefix '/', upstream 'http://svc:8000':
        '/anything'     -> 'http://svc:8000/anything'
    """
    remainder = path if route.path_prefix == "/" else path[len(route.path_prefix) :]
    if not remainder.startswith("/"):
        remainder = "/" + remainder

    url = route.upstream + remainder
    # The query string is passed through as raw text. Re-parsing and
    # rebuilding it can reorder or re-encode params and break upstreams
    # that sign their query strings.
    if query:
        url += "?" + query
    return url
