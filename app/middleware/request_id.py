"""Gives every request an ID and writes one access-log line when it finishes.

This is a plain ASGI middleware: a callable that wraps the app and sees the
raw `scope`, `receive` and `send`. It runs before routing, so every request
gets an ID, including /health and paths that end up as 404.

Why not Starlette's BaseHTTPMiddleware (the `@app.middleware("http")` style)?
It runs the app in a separate task and wraps the response in its own
streaming object. That is extra overhead on every request, and it gets in
the way once responses are streamed in Phase 6. Plain ASGI is a few more
lines and has neither problem.
"""

import logging
import re
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.request_context import request_id_var

logger = logging.getLogger("gateway.access")

HEADER = "x-request-id"

# What we accept from a client: letters, digits and . _ : - up to 128 chars.
# That covers UUIDs and the IDs other proxies (NGINX, Envoy, AWS ALB)
# generate. Anything else is replaced, so a caller cannot push huge values,
# spaces or control characters into our logs and every upstream's logs.
_VALID_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def incoming_request_id(scope: Scope) -> str | None:
    """The client's X-Request-ID if present and well formed, else None."""
    for name, value in scope["headers"]:
        # ASGI gives header names as lowercase bytes.
        if name == b"x-request-id":
            candidate = value.decode("latin-1")
            return candidate if _VALID_ID.fullmatch(candidate) else None
    return None


class RequestIDMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # lifespan and websocket events pass through
            await self.app(scope, receive, send)
            return

        request_id = incoming_request_id(scope) or str(uuid.uuid4())
        # set() returns a token; reset(token) restores the previous value.
        # Everything awaited below runs in this task and sees this ID.
        token = request_id_var.set(request_id)
        start = time.perf_counter()
        status_code = 500  # what we log if the app crashes before responding

        async def send_with_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                # Setting replaces any X-Request-ID an upstream sent back, so
                # the client always gets the ID the gateway logged.
                MutableHeaders(scope=message)[HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            peer = scope.get("client")
            fields = {
                "method": scope["method"],
                "path": scope["path"],
                "status_code": status_code,
                "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                "client_ip": peer[0] if peer else None,
            }
            # Set by the auth middleware, which runs inside this one and
            # shares the same scope dict.
            api_client = scope.get("state", {}).get("api_client")
            if api_client is not None:
                fields["client"] = api_client.client
            logger.info("request_completed", extra=fields)
            request_id_var.reset(token)
