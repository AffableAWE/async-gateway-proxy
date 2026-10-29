# async-gateway-proxy

![CI](https://github.com/AffableAWE/async-gateway-proxy/actions/workflows/ci.yml/badge.svg)

An async API gateway / reverse proxy in Python. It receives HTTP requests,
picks a backend from a YAML routing table, forwards the request, and returns
the backend's response. It is built to sit in front of a custom multithreaded
C++ HTTP server.

The project is built in phases. This README describes what exists now
(up to Phase 2: core proxy plus request tracing). Later phases are listed
under [Roadmap](#roadmap).

## Architecture

![Architecture](docs/images/AsyncProxyArchitecture.png)

What happens to one request:

1. Uvicorn accepts the TCP connection, parses the HTTP request and hands it
   to FastAPI as an ASGI call.
2. The request ID middleware gives the request an ID (the client's own
   `X-Request-ID` if it is well formed, a new UUID if not). Every log line
   for this request carries that ID.
3. `/health` is answered directly. Every other path hits one catch-all route.
4. `router.py` finds the longest `path_prefix` that matches the path. No
   match means `404`.
5. `proxy.py` builds the upstream URL (prefix removed, query string kept as
   sent), cleans the headers, adds `X-Request-ID`, and sends the request
   through a single shared `httpx.AsyncClient`.
6. The upstream's status, headers and body go back to the client with the
   same `X-Request-ID`. A timeout becomes `504`; a connection failure
   becomes `502`. One `request_completed` line is logged at the end.

Everything is async. While one request waits on its upstream, the event loop
serves other requests on the same thread.

## Features

- **Config-driven routing.** Routes live in `config.yaml`, validated by
  Pydantic at startup. A bad config stops the gateway from starting and
  reports every problem at once.
- **Longest-prefix matching** on path-segment boundaries: `/api` matches
  `/api/x` but not `/apix`.
- **Full forwarding** of method, path, raw query string, headers and body.
- **Header handling per RFC 7230:** hop-by-hop headers are dropped in both
  directions, including any header named in `Connection`. `Host` and
  `Content-Length` are set fresh for the upstream. `X-Forwarded-For` is
  appended to, never overwritten. Apart from the `X-Forwarded-*` and
  `X-Request-ID` headers, the gateway adds nothing of its own.
- **Bodies pass through untouched.** A gzipped upstream response reaches the
  client still gzipped, with its `Content-Encoding` intact. Repeated headers
  such as `Set-Cookie` are kept.
- **Connection pooling.** One `httpx.AsyncClient` for the life of the
  process, so keep-alive connections to each upstream are reused.
- **Distinct failure codes.** `504` for upstream timeout, `502` for an
  unreachable upstream, upstream `5xx` passed through as is.
- **Request IDs end to end.** Each request gets an ID that appears in every
  log line, goes to the upstream as `X-Request-ID`, comes back to the client
  in the response header and in error bodies. A client's own ID is kept if
  it is 1 to 128 characters of letters, digits and `. _ : -`; anything else
  is replaced.
- **Structured JSON logs.** A `request_completed` line for every request
  (total `duration_ms`) and a `proxy_forward` line for every forwarded one
  (`upstream_ms`, request and response sizes). The gap between the two
  times is the gateway's own overhead.

## Project layout

```
app/
  main.py            app factory, lifespan, /health, catch-all route
  router.py          route matching and upstream URL building (pure functions)
  proxy.py           forwarding, header filtering, error mapping
  logging_config.py  JSON log formatter, request ID on every record
  request_context.py ContextVar holding the current request ID
  middleware/
    request_id.py    assigns the ID, sets the response header, access log
  config/
    models.py        Pydantic schema for config.yaml
    loader.py        reads and validates the YAML file
dummy_upstream/
  main.py            small FastAPI backend for local testing
tests/               pytest suite, no network needed
config.yaml          routing table
```

## Run it locally

Needs Python 3.12 or newer.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

# Terminal 1: the backend
uvicorn dummy_upstream.main:app --port 8001

# Terminal 2: the gateway
uvicorn app.main:app --port 8000 --no-proxy-headers --no-access-log
```

Why the two flags:

- `--no-proxy-headers`: by default Uvicorn trusts `X-Forwarded-For` from
  localhost and rewrites the client address from it. The gateway is the edge,
  so it must record the real TCP peer, not a value the caller can fake.
- `--no-access-log`: the gateway writes its own `request_completed` JSON
  line for every request, so Uvicorn's access log would only duplicate it.

Environment variables:

| Variable         | Default       | Purpose                     |
|------------------|---------------|-----------------------------|
| `GATEWAY_CONFIG` | `config.yaml` | Path to the routing config  |
| `LOG_LEVEL`      | `INFO`        | Log level                   |

## Configuration

```yaml
upstream_timeout_seconds: 5.0     # optional, must be > 0

routes:                           # at least one route
  - path_prefix: /api             # must start with '/'
    upstream: http://localhost:8001

  - path_prefix: /cpp             # e.g. the C++ server
    upstream: http://localhost:8080
```

Validation rules: unknown keys are rejected (so a typo like `upstrem` fails
at startup), upstreams must be full URLs with a scheme, and duplicate
prefixes are rejected. Trailing slashes are normalized.

## Try it

```bash
# Liveness
curl localhost:8000/health
# {"status":"ok"}

# Proxied GET: /api/items/42 -> http://localhost:8001/items/42
curl localhost:8000/api/items/42
# {"item_id":42,"name":"item-42"}

# Proxied POST: the echo endpoint shows what the upstream received
curl -X POST localhost:8000/api/echo \
  -H "Content-Type: application/json" \
  -d '{"hello":"world"}'

# Your own request ID is kept, sent upstream, and returned
curl -i localhost:8000/api/echo -H "X-Request-ID: demo-42"
# x-request-id: demo-42   (and "x-request-id": "demo-42" in the echoed headers)

# No matching route
curl -i localhost:8000/nope
# 404 {"error":"no matching route","path":"/nope","request_id":"f046eddc-..."}

# A real timeout: the upstream waits 6 s, the gateway gives up after 5 s
curl -i "localhost:8000/api/slow?delay=6"
# 504 {"error":"upstream timeout","request_id":"..."}

# Stop the dummy upstream, then:
curl -i localhost:8000/api/items/1
# 502 {"error":"upstream unavailable","request_id":"..."}
```

The two log lines for one proxied request share its ID:

```json
{"ts": "2026-10-29T13:32:13.341+00:00", "level": "info", "logger": "gateway.proxy", "message": "proxy_forward", "request_id": "da5f233a-8b9c-4520-bce7-019b955a2de7", "method": "GET", "path": "/api/items/7", "upstream": "http://localhost:8001/items/7", "status_code": 200, "upstream_ms": 34.38, "request_bytes": 0, "response_bytes": 29}
{"ts": "2026-10-29T13:32:13.342+00:00", "level": "info", "logger": "gateway.access", "message": "request_completed", "request_id": "da5f233a-8b9c-4520-bce7-019b955a2de7", "method": "GET", "path": "/api/items/7", "status_code": 200, "duration_ms": 35.71, "client_ip": "127.0.0.1"}
```

To find everything about one request: `grep da5f233a gateway.log`.

## Tests

```bash
pytest
ruff check . && ruff format --check .
```

The proxy tests swap httpx's network transport for `httpx.MockTransport`,
so they cover forwarding, header rules, gzip passthrough, timeouts and
connection failures without opening a socket. One test runs 20 requests
concurrently and checks that none of them logs another's request ID. CI runs lint and tests on
Python 3.12, 3.13 and 3.14 for every push.

## Limitations

- Request and response bodies are read fully into memory. Large uploads,
  downloads and server-sent events need streaming (Phase 6).
- No authentication, rate limiting, retries or circuit breaking yet.
- `/health` is liveness only. There is no readiness check against upstreams.
- Config is read once at startup. Changing it needs a restart.
- httpx timeouts apply per operation (connect, each read), not as one total
  deadline for the request.
- One upstream URL per route, so no load balancing across replicas.
- HTTP only. TLS is expected to end at an ingress in front of the gateway.
- Any client's well-formed `X-Request-ID` is trusted. A production edge
  proxy would usually trust it only from known internal callers.
- Request IDs are not full distributed tracing: there are no spans or
  parent/child timings. OpenTelemetry would add that.

## Roadmap

| Phase | Adds |
|-------|------|
| 1     | Core proxy, config, logging, tests, CI (done) |
| 2     | Request IDs propagated to upstreams and logs (done) |
| 3     | API key authentication with per-key route access |
| 4     | Token-bucket rate limiting (in-memory, then Redis) |
| 5     | Retries with backoff and jitter, retry budget, circuit breaker |
| 6     | Streaming bodies and graceful shutdown |
| 7     | Readiness probe, Prometheus metrics, config hot reload |
| 8     | Docker and docker-compose (gateway, upstream, C++ server, Redis, Prometheus) |
| 9     | Load testing and benchmarks against direct and NGINX baselines |
| 10    | Kubernetes manifests and deployment on a k3s VPS |
