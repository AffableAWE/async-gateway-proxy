"""Composition root: builds the FastAPI app and wires the modules together.

Start it with:
    uvicorn app.main:app --port 8000
"""

import logging
import os
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.config import GatewayConfig, load_config
from app.config.reload import ConfigReloader
from app.health import readiness
from app.logging_config import setup_logging
from app.metrics import GatewayMetrics
from app.middleware.auth import AuthMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.request_id import RequestIDMiddleware
from app.proxy import forward_request
from app.ratelimit import MemoryRateLimiter, RateLimiter
from app.ratelimit.redis_backend import RedisRateLimiter
from app.request_context import get_request_id, request_config
from app.resilience.guards import UpstreamGuards
from app.router import match_route

logger = logging.getLogger("gateway.main")

PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def create_app(
    config: GatewayConfig | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    rate_limiter: RateLimiter | None = None,
    guards: UpstreamGuards | None = None,
) -> FastAPI:
    """Build the gateway app.

    Production calls this with no arguments: config comes from the file in
    $GATEWAY_CONFIG and httpx talks to the real network. Tests pass a config
    object and a fake transport instead, so no file or network is needed,
    and can pass a rate limiter and upstream guards with a fake clock.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Runs once before the first request and once after the last one.
        setup_logging(os.getenv("LOG_LEVEL", "INFO"))

        config_path = os.getenv("GATEWAY_CONFIG", "config.yaml")
        app.state.config = config if config is not None else load_config(config_path)

        # One client for the whole process. It keeps a pool of keep-alive
        # connections to each upstream, so only the first request to an
        # upstream pays for the TCP handshake.
        app.state.http_client = httpx.AsyncClient(
            transport=transport,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            # A proxy passes 3xx responses back to the client; it never
            # follows them itself.
            follow_redirects=False,
        )
        # httpx adds default headers to every request (User-Agent,
        # Accept-Encoding: gzip, ...). A proxy must pass on what the client
        # sent, nothing more: if a client never asked for gzip, an injected
        # Accept-Encoding could get it a gzipped body it can't read.
        app.state.http_client.headers.clear()

        # "is not None", not "or": an empty limiter has len() == 0, so it is
        # falsy, and "or" would silently swap in a different one.
        app.state.rate_limiter = (
            rate_limiter if rate_limiter is not None else build_rate_limiter(app.state.config)
        )
        # Retry budgets and circuit breakers, one per upstream.
        app.state.guards = guards if guards is not None else UpstreamGuards()

        if isinstance(app.state.rate_limiter, RedisRateLimiter):
            # Start even if Redis is down: the limiter fails open or closed
            # per request, and Redis may come back. Just say so loudly.
            if not await app.state.rate_limiter.ping():
                logger.error("rate_limit_redis_unreachable_at_startup")

        metrics: GatewayMetrics = app.state.metrics
        metrics.config_loaded_at.set(time.time())
        metrics.track_upstreams(route.upstream for route in app.state.config.routes)

        # Hot reload, only for config that came from a file (tests inject
        # config objects directly).
        reloader = None
        if config is None:

            def apply(old: GatewayConfig, new: GatewayConfig) -> None:
                app.state.config = new  # one reference swap: atomic for readers
                app.state.guards.apply_config(old, new)
                metrics.config_loaded_at.set(time.time())
                metrics.track_upstreams(route.upstream for route in new.routes)

            reloader = ConfigReloader(
                config_path,
                app.state.config,
                apply,
                on_result=lambda result: metrics.config_reloads.labels(result).inc(),
            )
            reloader.start()
        app.state.reloader = reloader

        auth = app.state.config.auth
        logger.info(
            "gateway_startup",
            extra={
                "config_source": "injected" if config is not None else config_path,
                "routes": [r.path_prefix for r in app.state.config.routes],
                "auth": "on" if auth is not None else "off",
                "api_keys": len(auth.keys) if auth is not None else 0,
                "rate_limit": (
                    app.state.config.rate_limit.backend
                    if app.state.config.rate_limit is not None
                    else "off"
                ),
                "retries": (
                    app.state.config.retries.max_attempts
                    if app.state.config.retries is not None
                    else "off"
                ),
                "circuit_breaker": "on" if app.state.config.circuit_breaker is not None else "off",
                "hot_reload": (
                    f"every {app.state.config.reload_interval_seconds}s and on SIGHUP"
                    if reloader is not None
                    else "off"
                ),
            },
        )
        if auth is None:
            logger.warning("auth_disabled", extra={"hint": "add an auth section to config.yaml"})
        try:
            yield
        finally:
            if reloader is not None:
                await reloader.stop()
            await app.state.rate_limiter.close()
            await app.state.http_client.aclose()
            logger.info("gateway_shutdown")

    # docs/redoc/openapi are switched off: in a gateway, a request to /docs
    # should be proxied like any other path, not answered by FastAPI.
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    # Created with the app, not in the lifespan: `python -m app` starts the
    # metrics server before uvicorn runs the lifespan.
    app.state.metrics = GatewayMetrics()
    app.state.metrics.watch_breakers(
        lambda: app.state.guards.breakers() if hasattr(app.state, "guards") else {}
    )

    # Middleware order: the LAST one added runs FIRST (outermost). So a
    # request passes RequestID -> Auth -> RateLimit -> routing.
    #   RequestID outermost: even a 401 or 429 is logged with an ID.
    #   Auth before RateLimit: the limiter needs to know who the caller is.
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(AuthMiddleware)
    app.add_middleware(RequestIDMiddleware)

    @app.get("/health")
    async def health() -> dict:
        """Liveness: is this process up and is its event loop responding?

        It deliberately does not check upstreams. If it did, Kubernetes would
        restart the gateway every time a backend went down.
        """
        return {"status": "ok"}

    @app.get("/ready")
    async def ready(request: Request) -> JSONResponse:
        """Readiness: should this instance get traffic? See app/health.py."""
        is_ready, body = await readiness(request.app)
        return JSONResponse(status_code=200 if is_ready else 503, content=body)

    # Registered after /health and /ready, so those are matched first. Every other
    # path and method lands here. `{full_path:path}` matches across slashes.
    @app.api_route("/{full_path:path}", methods=PROXY_METHODS)
    async def gateway(full_path: str, request: Request) -> Response:
        config: GatewayConfig = request_config(request.scope)

        route = match_route(request.url.path, config)
        if route is not None:
            request.state.route = route.path_prefix  # the metrics label
        if route is None:
            logger.info(
                "route_not_found",
                extra={"method": request.method, "path": request.url.path},
            )
            return JSONResponse(
                status_code=404,
                content={
                    "error": "no matching route",
                    "path": request.url.path,
                    "request_id": get_request_id(),
                },
            )

        # Authorization. Auth middleware already proved who the caller is;
        # now that the route is known, check the key may use it.
        api_client = getattr(request.state, "api_client", None)
        if api_client is not None and route.path_prefix not in api_client.routes:
            request.app.state.metrics.auth_rejections.labels("route_not_allowed").inc()
            logger.warning(
                "access_denied",
                extra={
                    "client": api_client.client,
                    "route": route.path_prefix,
                    "method": request.method,
                    "path": request.url.path,
                },
            )
            return JSONResponse(
                status_code=403,
                content={
                    "error": "api key not allowed for this route",
                    "request_id": get_request_id(),
                },
            )

        return await forward_request(
            request=request,
            route=route,
            client=request.app.state.http_client,
            config=config,
            guards=request.app.state.guards,
        )

    return app


def build_rate_limiter(config: GatewayConfig) -> RateLimiter:
    settings = config.rate_limit
    if settings is not None and settings.backend == "redis":
        assert settings.redis_url is not None  # checked by the config schema
        return RedisRateLimiter.from_url(
            settings.redis_url,
            timeout_seconds=settings.redis_timeout_seconds,
            fail_open=settings.fail_open,
        )
    # Built even when rate limiting is off: it costs nothing until used,
    # and Phase 7's hot reload may turn limiting on at runtime.
    return MemoryRateLimiter()


app = create_app()
