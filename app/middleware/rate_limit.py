"""Rate limiting: one token bucket per client, checked before routing.

It runs after auth, so it knows who the caller is:
    auth on   bucket per client name   ("client:mobile-app")
    auth off  bucket per client IP     ("ip:203.0.113.7")

Keying by client name, not by key, means a client's two keys (during a
rotation) share one bucket instead of doubling its limit.

Requests rejected by auth (401) never reach this middleware, so they don't
use up anyone's tokens.
"""

import logging
import math

from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import GatewayConfig
from app.middleware.auth import PUBLIC_PATHS
from app.ratelimit import Decision, Limit, RateLimiter
from app.request_context import get_request_id

logger = logging.getLogger("gateway.ratelimit")


def _bucket_key_and_limit(scope: Scope, config: GatewayConfig) -> tuple[str, Limit]:
    assert config.rate_limit is not None
    default = config.rate_limit.default

    api_client = scope.get("state", {}).get("api_client")
    if api_client is not None:
        return f"client:{api_client.client}", (api_client.rate_limit or default).to_limit()

    peer = scope.get("client")
    return f"ip:{peer[0] if peer else 'unknown'}", default.to_limit()


def _limit_headers(decision: Decision) -> dict[str, str]:
    headers = {"X-RateLimit-Limit": str(decision.limit)}
    if decision.remaining is not None:  # None when the backend couldn't say
        headers["X-RateLimit-Remaining"] = str(decision.remaining)
    return headers


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        config: GatewayConfig = scope["app"].state.config
        if config.rate_limit is None:
            await self.app(scope, receive, send)
            return

        limiter: RateLimiter = scope["app"].state.rate_limiter
        key, limit = _bucket_key_and_limit(scope, config)
        decision = await limiter.hit(key, limit)

        if not decision.allowed:
            await self._reject(scope, receive, send, key, decision)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _limit_headers(decision).items():
                    headers[name] = value  # replaces any the upstream sent
            await send(message)

        await self.app(scope, receive, send_with_headers)

    async def _reject(
        self, scope: Scope, receive: Receive, send: Send, key: str, decision: Decision
    ) -> None:
        # Retry-After takes whole seconds. Round up: rounding down would
        # tell the client to come back before a token exists.
        retry_after = max(1, math.ceil(decision.retry_after))
        api_client = scope.get("state", {}).get("api_client")
        # Client names come from config, a small known set. Bucket keys
        # with auth off are client IPs, too many to use as a label.
        client = api_client.client if api_client is not None else "anonymous"
        scope["app"].state.metrics.rate_limited.labels(client).inc()
        logger.warning(
            "rate_limited",
            extra={
                "bucket": key,
                "limit": decision.limit,
                "retry_after_s": retry_after,
                "method": scope["method"],
                "path": scope["path"],
            },
        )
        response = JSONResponse(
            status_code=429,
            content={"error": "rate limit exceeded", "request_id": get_request_id()},
            headers={**_limit_headers(decision), "Retry-After": str(retry_after)},
        )
        await response(scope, receive, send)
