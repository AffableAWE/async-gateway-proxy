"""Forwarding: the part of the gateway that acts as an HTTP client.

Takes an incoming request plus the route chosen by router.py, sends it to
the upstream with the shared httpx client, and turns the upstream's answer
into a response for the original caller.
"""

import logging
import time
from dataclasses import dataclass

import httpx
from fastapi import Request, Response
from fastapi.responses import JSONResponse

from app.config import GatewayConfig, RouteConfig
from app.request_context import get_request_id
from app.resilience.breaker import State
from app.resilience.guards import UpstreamGuards
from app.resilience.retry import RETRYABLE_STATUSES, Failure, backoff_delay, is_retryable
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
    # The gateway sets these itself, so drop whatever the client sent.
    # x-gateway-client is dropped even with auth off: otherwise a caller
    # could claim to be any client the upstream trusts.
    replaced = {"x-forwarded-for", "x-request-id", "x-gateway-client"}
    api_client = getattr(request.state, "api_client", None)
    if api_client is not None:
        # The client's key is for the gateway only. Passing it on would
        # hand a live secret to every upstream and its logs.
        replaced.add("x-api-key")
    headers = [(k, v) for k, v in headers if k.lower() not in replaced]

    headers.append(("x-forwarded-for", f"{prior}, {client_ip}" if prior else client_ip))
    headers.append(("x-forwarded-proto", request.url.scheme))
    if "host" in request.headers:
        headers.append(("x-forwarded-host", request.headers["host"]))

    # Same ID the gateway logs and returns to the client. If the upstream
    # logs it too, one ID finds the request on both sides.
    request_id = get_request_id()
    if request_id is not None:
        headers.append(("x-request-id", request_id))

    # Tells the upstream who is calling, checked by the gateway. Upstreams
    # can trust it only because the gateway strips any client-sent copy.
    if api_client is not None:
        headers.append(("x-gateway-client", api_client.client))
    return headers


def _error(status_code: int, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    # The ID in the body lets a client quote it in a bug report, even if
    # whatever it uses to call us hides response headers.
    return JSONResponse(
        status_code=status_code,
        content={"error": message, "request_id": get_request_id()},
        headers=headers,
    )


@dataclass
class _Attempt:
    """The outcome of one try: an upstream answer, an error, or both."""

    status_code: int | None = None
    headers: list[tuple[str, str]] | None = None
    content: bytes = b""
    error: httpx.RequestError | None = None
    failure: Failure | None = None  # None means success


async def _send_once(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: list[tuple[str, str]],
    body: bytes,
    timeout_seconds: float,
) -> _Attempt:
    # A new request object per try. The body is already in memory (bytes),
    # so it can be sent again. Phase 6's streaming bodies will make this harder.
    upstream_request = client.build_request(
        method=method,
        url=url,
        headers=headers,
        content=body,
        # httpx timeouts apply per operation (connect, each read, each write),
        # not as one total deadline for the whole exchange.
        timeout=timeout_seconds,
    )
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
    # Order matters: ConnectTimeout and PoolTimeout are TimeoutExceptions,
    # and every one of these is a RequestError.
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
        return _Attempt(error=exc, failure=Failure.CONNECT)  # request never sent
    except httpx.TimeoutException as exc:
        return _Attempt(error=exc, failure=Failure.TIMEOUT)
    except httpx.RequestError as exc:
        return _Attempt(error=exc, failure=Failure.TRANSPORT)

    status = upstream_response.status_code
    return _Attempt(
        status_code=status,
        headers=upstream_response.headers.multi_items(),
        content=content,
        failure=Failure.STATUS if status in RETRYABLE_STATUSES else None,
    )


async def forward_request(
    request: Request,
    route: RouteConfig,
    client: httpx.AsyncClient,
    config: GatewayConfig,
    guards: UpstreamGuards,
) -> Response:
    upstream_url = build_upstream_url(request.url.path, route, request.url.query)
    method = request.method

    # MVP simplification: the whole request body is read into memory.
    # Phase 6 replaces this with streaming.
    body = await request.body()
    headers = _forwarded_headers(request)

    retry = config.retries
    max_attempts = retry.max_attempts if retry is not None else 1
    budget = guards.budget_for(route.upstream, config)
    breaker = guards.breaker_for(route.upstream, config)
    if budget is not None:
        budget.record_request()

    log_fields = {"method": method, "path": request.url.path, "upstream": upstream_url}
    # upstream_ms below is time spent on the upstream, retries and waits
    # included. The access log's duration_ms is the whole request. The gap
    # between the two is the gateway's own overhead.
    start = time.perf_counter()  # monotonic: safe for measuring durations

    result: _Attempt | None = None
    attempts = 0
    while True:
        if breaker is not None and not breaker.allow_request():
            if result is None:
                # Breaker open before the first try: fail fast, don't call.
                retry_after = breaker.retry_after_seconds()
                logger.warning(
                    "circuit_open_rejected", extra={**log_fields, "retry_after_s": retry_after}
                )
                return _error(503, "upstream circuit open", {"Retry-After": str(retry_after)})
            break  # it opened during our retries: return the last failure

        attempts += 1
        result = await _send_once(
            client, method, upstream_url, headers, body, config.upstream_timeout_seconds
        )
        if breaker is not None:
            if result.failure is None:
                breaker.record_success()
            else:
                breaker.record_failure()

        if result.failure is None or retry is None or attempts >= max_attempts:
            break
        if breaker is not None and breaker.state is State.OPEN:
            break  # this failure opened it: no point waiting to retry
        if not is_retryable(method, result.failure):
            break
        if budget is not None and not budget.try_spend():
            logger.warning("retry_budget_exhausted", extra={**log_fields, "attempts": attempts})
            break

        delay = backoff_delay(
            attempts, retry.base_delay_seconds, retry.max_delay_seconds, rng=guards.rng
        )
        logger.info(
            "upstream_retry",
            extra={
                **log_fields,
                "attempt": attempts,
                "reason": result.failure.value,
                "status_code": result.status_code,
                "delay_ms": round(delay * 1000, 2),
            },
        )
        await guards.sleep(delay)

    return _final_response(result, log_fields, attempts, start, body, config)


def _final_response(
    result: _Attempt,
    log_fields: dict,
    attempts: int,
    start: float,
    body: bytes,
    config: GatewayConfig,
) -> Response:
    fields = {**log_fields, "attempts": attempts, "upstream_ms": _ms_since(start)}

    if isinstance(result.error, httpx.TimeoutException):
        logger.warning(
            "upstream_timeout",
            extra={
                **fields,
                "error_type": type(result.error).__name__,
                "timeout_seconds": config.upstream_timeout_seconds,
            },
        )
        return _error(504, "upstream timeout")

    if result.error is not None:
        # Connection refused, DNS failure, connection reset...
        logger.error(
            "upstream_unreachable",
            extra={**fields, "error_type": type(result.error).__name__, "error": str(result.error)},
        )
        return _error(502, "upstream unavailable")

    assert result.status_code is not None and result.headers is not None
    logger.info(
        "proxy_forward",
        extra={
            **fields,
            "status_code": result.status_code,
            "request_bytes": len(body),
            "response_bytes": len(result.content),
        },
    )
    # A 502/503/504 that survived every retry is passed through as the
    # upstream sent it: its body may say more than our own error would.
    response = Response(content=result.content, status_code=result.status_code)
    for name, value in filter_headers(result.headers, RESPONSE_STRIP):
        response.headers.append(name, value)
    return response


def _ms_since(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)
