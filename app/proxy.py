"""Forwarding: the part of the gateway that acts as an HTTP client.

Takes an incoming request plus the route chosen by router.py, sends it to
the upstream with the shared httpx client, and turns the upstream's answer
into a response for the original caller.

Bodies are streamed in both directions, so memory stays flat however big
they are. The one exception: request bodies up to max_buffered_request_bytes
are read into memory first, because only a body we still hold can be sent
again on a retry.
"""

import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

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

# Response side: only hop-by-hop. The body is passed on byte for byte, so
# the upstream's own Content-Length stays correct and is kept; without it
# the client would get a chunked response and couldn't show progress.
RESPONSE_STRIP = HOP_BY_HOP


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


class UpstreamStreamError(Exception):
    """The upstream broke off in the middle of a response body.

    By then the status line and headers are already with the client, so a
    clean 502 is impossible. Raising makes the server cut the connection,
    and the client sees a truncated response instead of a short one that
    looks complete.
    """


@dataclass
class _Body:
    """The request body: in memory (can be resent) or a one-shot stream."""

    content: bytes | AsyncIterator[bytes]
    replayable: bool
    size: list[int] = field(default_factory=lambda: [0])  # updated as a stream is read

    @property
    def bytes_sent(self) -> int:
        return len(self.content) if isinstance(self.content, bytes) else self.size[0]


async def _read_body(request: Request, limit: int) -> _Body:
    """Buffer the body if it is at most `limit` bytes, else stream it.

    Reads chunk by chunk and stops buffering as soon as the limit is passed,
    so a huge body never sits in memory whole, even if the client didn't
    send a Content-Length.
    """
    stream = request.stream()
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        return _counting(_Body(stream, replayable=False))

    chunks: list[bytes] = []
    buffered = 0
    async for chunk in stream:
        chunks.append(chunk)
        buffered += len(chunk)
        if buffered > limit:

            async def rest() -> AsyncIterator[bytes]:
                for c in chunks:
                    yield c
                async for c in stream:
                    yield c

            return _counting(_Body(rest(), replayable=False))
    return _Body(b"".join(chunks), replayable=True)


def _counting(body: _Body) -> _Body:
    source = body.content
    assert not isinstance(source, bytes)

    async def counted() -> AsyncIterator[bytes]:
        async for chunk in source:
            body.size[0] += len(chunk)
            yield chunk

    body.content = counted()
    return body


@dataclass
class _Attempt:
    """The outcome of one try.

    On success `response` is open, with its body not yet read: it is
    streamed to the client later. On a 502/503/504 the (small) body is read
    here, so the attempt can be retried or returned.
    """

    response: httpx.Response | None = None
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
    body: _Body,
    timeout_seconds: float,
) -> _Attempt:
    upstream_request = client.build_request(
        method=method,
        url=url,
        headers=headers,
        content=body.content,
        # httpx timeouts apply per operation (connect, each read, each write),
        # not as one total deadline. Streaming works because a slow but
        # steady body never waits long for any single chunk.
        timeout=timeout_seconds,
    )
    try:
        # stream=True: return as soon as the headers arrive, body unread.
        upstream_response = await client.send(upstream_request, stream=True)
        status = upstream_response.status_code
        if status not in RETRYABLE_STATUSES:
            return _Attempt(
                response=upstream_response,
                status_code=status,
                headers=upstream_response.headers.multi_items(),
            )
        # A 502/503/504 may be retried, so read its body and let go of the
        # connection now. Error bodies are small.
        try:
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

    return _Attempt(
        status_code=status,
        headers=upstream_response.headers.multi_items(),
        content=content,
        failure=Failure.STATUS,
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

    body = await _read_body(request, config.max_buffered_request_bytes)
    headers = _forwarded_headers(request)
    if not body.replayable and "content-length" in request.headers:
        # httpx can't know a stream's length. Passing the client's (the
        # bytes are forwarded unchanged) keeps a plain Content-Length body
        # instead of switching to chunked, which some servers don't accept.
        headers.append(("content-length", request.headers["content-length"]))

    retry = config.retries
    # A streamed body is read once, by the first attempt, so it can't be
    # sent again: no retries for it.
    max_attempts = retry.max_attempts if retry is not None and body.replayable else 1
    budget = guards.budget_for(route.upstream, config)
    breaker = guards.breaker_for(route.upstream, config)
    if budget is not None:
        budget.record_request()

    log_fields = {"method": method, "path": request.url.path, "upstream": upstream_url}
    # upstream_ms below is time spent on the upstream, retries, waits and
    # body streaming included. The access log's duration_ms is the whole
    # request. The gap between the two is the gateway's own overhead.
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

    fields = {**log_fields, "attempts": attempts, "streamed_request": not body.replayable}
    return _final_response(result, fields, start, body, config)


def _final_response(
    result: _Attempt, fields: dict, start: float, body: _Body, config: GatewayConfig
) -> Response:
    if isinstance(result.error, httpx.TimeoutException):
        logger.warning(
            "upstream_timeout",
            extra={
                **fields,
                "upstream_ms": _ms_since(start),
                "error_type": type(result.error).__name__,
                "timeout_seconds": config.upstream_timeout_seconds,
            },
        )
        return _error(504, "upstream timeout")

    if result.error is not None:
        # Connection refused, DNS failure, connection reset...
        logger.error(
            "upstream_unreachable",
            extra={
                **fields,
                "upstream_ms": _ms_since(start),
                "error_type": type(result.error).__name__,
                "error": str(result.error),
            },
        )
        return _error(502, "upstream unavailable")

    assert result.status_code is not None and result.headers is not None
    headers = filter_headers(result.headers, RESPONSE_STRIP)

    if result.response is None:
        # A 502/503/504 that survived every retry, already read: passed on
        # as the upstream sent it, since its body may say more than ours.
        _log_forward(fields, start, result.status_code, body, len(result.content))
        response = Response(content=result.content, status_code=result.status_code)
        for name, value in headers:
            response.headers.append(name, value)
        return response

    stream = _UpstreamStream(result.response, fields, start, body)
    # Appended one by one, like above: a headers dict would merge repeated
    # headers such as Set-Cookie into one.
    for name, value in headers:
        stream.headers.append(name, value)
    return stream


class _UpstreamStream(StreamingResponse):
    """Passes the upstream body to the client chunk by chunk.

    Backpressure comes for free: each chunk is handed to the server, which
    waits while the client's socket buffer is full, so we don't ask the
    upstream for more until the client has caught up. A slow client slows
    the upstream read instead of filling the gateway's memory.
    """

    def __init__(self, upstream: httpx.Response, fields: dict, start: float, body: _Body):
        super().__init__(self._chunks(), status_code=upstream.status_code)
        self._upstream = upstream
        self._fields = fields
        self._start = start
        self._body = body
        self._sent = 0
        self._finished = False  # whole body passed on
        self._failed = False  # upstream broke off

    async def _chunks(self) -> AsyncIterator[bytes]:
        try:
            # aiter_raw gives the body exactly as the upstream sent it, still
            # gzip-compressed if it was. aiter_bytes would decompress it while
            # we forward `Content-Encoding: gzip`, and the client would then
            # try to unzip plain bytes.
            async for chunk in self._upstream.aiter_raw():
                self._sent += len(chunk)
                yield chunk
        except httpx.HTTPError as exc:
            self._failed = True
            logger.error(
                "upstream_stream_aborted",
                extra={
                    **self._fields,
                    "status_code": self._upstream.status_code,
                    "response_bytes": self._sent,
                    "error_type": type(exc).__name__,
                },
            )
            raise UpstreamStreamError(str(exc)) from exc
        self._finished = True
        _log_forward(self._fields, self._start, self._upstream.status_code, self._body, self._sent)

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Always give the upstream connection back, also when the client
            # hung up mid-body. Starlette then cancels the streaming task,
            # possibly while our generator sits at a `yield`, and that
            # generator's own cleanup would only run whenever it is
            # garbage-collected.
            await self._upstream.aclose()
            if not self._finished and not self._failed:
                logger.info(
                    "client_disconnected",
                    extra={
                        **self._fields,
                        "status_code": self._upstream.status_code,
                        "response_bytes": self._sent,
                    },
                )


def _log_forward(fields: dict, start: float, status: int, body: _Body, sent: int) -> None:
    logger.info(
        "proxy_forward",
        extra={
            **fields,
            "status_code": status,
            "upstream_ms": _ms_since(start),
            "request_bytes": body.bytes_sent,
            "response_bytes": sent,
        },
    )


def _ms_since(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)
