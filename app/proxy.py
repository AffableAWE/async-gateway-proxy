"""Forwarding: the part of the gateway that acts as an HTTP client.

Takes an incoming request plus the route chosen by router.py, sends it to
the upstream with the shared httpx client, and turns the upstream's answer
into a response for the original caller.
"""

import logging
import time

import httpx
from fastapi import Request, Response
from fastapi.responses import JSONResponse

from app.config import RouteConfig
from app.router import build_upstream_url

logger = logging.getLogger("gateway.proxy")

# RFC 7230 section 6.1: these headers describe one TCP hop, not the whole
# message. A proxy must not pass them on, in either direction.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# Request side: httpx sets Host from the upstream URL and Content-Length
# from the body it actually sends. Forwarding the client's values can only
# be wrong.
REQUEST_STRIP = HOP_BY_HOP | {"host", "content-length"}

# Response side: Starlette sets Content-Length from the body we return.
RESPONSE_STRIP = HOP_BY_HOP | {"content-length"}


def _connection_tokens(headers: list[tuple[str, str]]) -> set[str]:
    """Headers named inside `Connection: ...` are hop-by-hop too (RFC 7230 6.1).

    Example: 'Connection: close, X-Debug' means X-Debug must be dropped.
    """
    tokens: set[str] = set()
    for name, value in headers:
        if name.lower() == "connection":
            tokens.update(t.strip().lower() for t in value.split(",") if t.strip())
    return tokens


def filter_headers(headers: list[tuple[str, str]], strip: frozenset[str]) -> list[tuple[str, str]]:
    """Drop stripped headers, keep everything else including repeated names.

    We work with (name, value) pairs, not a dict: a dict would merge repeated
    headers like Set-Cookie into one and break cookies.
    """
    drop = strip | _connection_tokens(headers)
    return [(k, v) for k, v in headers if k.lower() not in drop]


def _forwarded_headers(request: Request) -> list[tuple[str, str]]:
    headers = filter_headers(list(request.headers.items()), REQUEST_STRIP)
    # request.client is the TCP peer only when uvicorn runs with
    # --no-proxy-headers. By default uvicorn trusts X-Forwarded-For from
    # 127.0.0.1 and rewrites request.client from it, which would let a
    # caller fake its own IP. The gateway is the edge, so it must not trust it.
    client_ip = request.client.host if request.client else "unknown"

    # Append to an existing X-Forwarded-For chain instead of overwriting it,
    # so the upstream can see every proxy the request passed through.
    prior = request.headers.get("x-forwarded-for")
    headers = [(k, v) for k, v in headers if k.lower() != "x-forwarded-for"]
    headers.append(("x-forwarded-for", f"{prior}, {client_ip}" if prior else client_ip))
    headers.append(("x-forwarded-proto", request.url.scheme))
    if "host" in request.headers:
        headers.append(("x-forwarded-host", request.headers["host"]))
    return headers


def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": message})


async def forward_request(
    request: Request,
    route: RouteConfig,
    client: httpx.AsyncClient,
    timeout_seconds: float,
) -> Response:
    upstream_url = build_upstream_url(request.url.path, route, request.url.query)

    # MVP simplification: the whole request body is read into memory.
    # Phase 6 replaces this with streaming.
    body = await request.body()

    upstream_request = client.build_request(
        method=request.method,
        url=upstream_url,
        headers=_forwarded_headers(request),
        content=body,
        # httpx timeouts apply per operation (connect, each read, each write),
        # not as one total deadline for the whole exchange.
        timeout=timeout_seconds,
    )

    log_fields = {"method": request.method, "path": request.url.path, "upstream": upstream_url}
    start = time.perf_counter()  # monotonic: safe for measuring durations

    try:
        upstream_response = await client.send(upstream_request, stream=True)
        try:
            # aiter_raw gives the body exactly as the upstream sent it, still
            # gzip-compressed if it was. If we used .content, httpx would
            # decompress it while we forwarded `Content-Encoding: gzip`, and
            # the client would try to unzip plain text.
            content = b"".join([chunk async for chunk in upstream_response.aiter_raw()])
        finally:
            await upstream_response.aclose()

    except httpx.TimeoutException as exc:
        # Must come before RequestError: TimeoutException is a subclass of it.
        logger.warning(
            "upstream_timeout",
            extra={
                **log_fields,
                "latency_ms": _ms_since(start),
                "error_type": type(exc).__name__,
                "timeout_seconds": timeout_seconds,
            },
        )
        return _error(504, "upstream timeout")

    except httpx.RequestError as exc:
        # Connection refused, DNS failure, connection reset...
        logger.error(
            "upstream_unreachable",
            extra={
                **log_fields,
                "latency_ms": _ms_since(start),
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        return _error(502, "upstream unavailable")

    logger.info(
        "proxy_forward",
        extra={
            **log_fields,
            "status_code": upstream_response.status_code,
            "latency_ms": _ms_since(start),
            "response_bytes": len(content),
        },
    )

    response = Response(content=content, status_code=upstream_response.status_code)
    for name, value in filter_headers(upstream_response.headers.multi_items(), RESPONSE_STRIP):
        response.headers.append(name, value)
    return response


def _ms_since(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)
