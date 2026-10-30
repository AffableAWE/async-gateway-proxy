"""Authentication: who is calling? Checked before routing.

Authorization (may this caller use this route?) happens later, in main.py,
because it needs the route, and the route is only known after matching.

    no key / wrong key            -> 401, here
    good key, route not allowed   -> 403, in main.py
    good key, unknown path        -> 404, in main.py

Rejecting before routing means a caller without a valid key gets 401 for
every path, so it can't probe which routes exist.
"""

import logging

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import GatewayConfig
from app.request_context import get_request_id

logger = logging.getLogger("gateway.auth")

API_KEY_HEADER = b"x-api-key"

# Paths that never need a key. Kubernetes probes and Prometheus can't send
# one. /ready and /metrics join this list in Phase 7.
PUBLIC_PATHS = frozenset({"/health"})


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope["headers"]:
        if key == name:
            return value.decode("latin-1").strip() or None
    return None


class AuthMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        # Read the config per request, not once in __init__: the config is
        # loaded in the lifespan, after middleware is built, and Phase 7
        # will swap it at runtime for hot reload.
        config: GatewayConfig = scope["app"].state.config
        if config.auth is None:  # auth is off
            await self.app(scope, receive, send)
            return

        presented = _header(scope, API_KEY_HEADER)
        if presented is None:
            await self._reject(scope, receive, send, "missing api key")
            return

        api_client = config.auth.lookup(presented)
        if api_client is None:
            await self._reject(scope, receive, send, "invalid api key")
            return

        # scope["state"] is per request and becomes request.state in the
        # handler. The access log and the proxy read the caller from here.
        scope.setdefault("state", {})["api_client"] = api_client
        await self.app(scope, receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send, reason: str) -> None:
        # Never log the key itself, not even a wrong one: a wrong key is
        # often a real key pasted into the wrong place.
        logger.warning(
            "auth_rejected",
            extra={"reason": reason, "method": scope["method"], "path": scope["path"]},
        )
        response = JSONResponse(
            status_code=401,
            content={"error": reason, "request_id": get_request_id()},
            # A 401 must say how to authenticate (RFC 9110, section 11.6.1).
            headers={"WWW-Authenticate": 'ApiKey header="X-API-Key"'},
        )
        await response(scope, receive, send)
