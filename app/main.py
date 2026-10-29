"""Composition root: builds the FastAPI app and wires the modules together.

Start it with:
    uvicorn app.main:app --port 8000
"""

import logging
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.config import GatewayConfig, load_config
from app.logging_config import setup_logging
from app.middleware.request_id import RequestIDMiddleware
from app.proxy import forward_request
from app.request_context import get_request_id
from app.router import match_route

logger = logging.getLogger("gateway.main")

PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def create_app(
    config: GatewayConfig | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the gateway app.

    Production calls this with no arguments: config comes from the file in
    $GATEWAY_CONFIG and httpx talks to the real network. Tests pass a config
    object and a fake transport instead, so no file or network is needed.
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

        logger.info(
            "gateway_startup",
            extra={
                "config_source": "injected" if config is not None else config_path,
                "routes": [r.path_prefix for r in app.state.config.routes],
            },
        )
        try:
            yield
        finally:
            await app.state.http_client.aclose()
            logger.info("gateway_shutdown")

    # docs/redoc/openapi are switched off: in a gateway, a request to /docs
    # should be proxied like any other path, not answered by FastAPI.
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    # Middleware order: the last one added runs first (outermost). The
    # request ID must be outermost so that auth and rate-limit rejections
    # added in later phases are logged with an ID too.
    app.add_middleware(RequestIDMiddleware)

    @app.get("/health")
    async def health() -> dict:
        """Liveness: is this process up and is its event loop responding?

        It deliberately does not check upstreams. If it did, Kubernetes would
        restart the gateway every time a backend went down.
        """
        return {"status": "ok"}

    # Registered after /health, so /health is matched first. Every other
    # path and method lands here. `{full_path:path}` matches across slashes.
    @app.api_route("/{full_path:path}", methods=PROXY_METHODS)
    async def gateway(full_path: str, request: Request) -> Response:
        config: GatewayConfig = request.app.state.config

        route = match_route(request.url.path, config)
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

        return await forward_request(
            request=request,
            route=route,
            client=request.app.state.http_client,
            timeout_seconds=config.upstream_timeout_seconds,
        )

    return app


app = create_app()
