"""Readiness: should this gateway instance get traffic right now?

Two probes, two questions, two consequences in Kubernetes:

    /health  liveness   "Is the process broken?"      fails -> pod restarted
    /ready   readiness  "Can it serve right now?"     fails -> pod taken out of
                                                       the Service, not restarted

What readiness checks, and what it deliberately doesn't:

  - Config loaded, upstream connection pool open: without these nothing works.
  - Redis, but only if rate limiting uses Redis with fail_open: false. Then
    a dead Redis means every request would get 429, so this instance
    really can't serve. With fail_open: true it still serves, so Redis
    being down is reported but doesn't fail the check.
  - Upstreams: NOT a reason to be unready. If one backend going down made
    every gateway pod unready, Kubernetes would pull the whole gateway, and
    every healthy route would go down with it. A dead upstream is the
    circuit breaker's job. The number of open breakers is reported, for
    people, without changing the answer.

The body names checks but no upstream addresses: this endpoint is public.
"""

from fastapi import FastAPI

from app.config import GatewayConfig
from app.ratelimit.redis_backend import RedisRateLimiter
from app.resilience.breaker import State


async def readiness(app: FastAPI) -> tuple[bool, dict]:
    config: GatewayConfig | None = getattr(app.state, "config", None)
    checks: dict[str, str] = {}
    ready = True

    checks["config"] = "ok" if config is not None else "missing"
    ready &= config is not None

    client = getattr(app.state, "http_client", None)
    pool_open = client is not None and not client.is_closed
    checks["upstream_pool"] = "ok" if pool_open else "closed"
    ready &= pool_open

    limiter = getattr(app.state, "rate_limiter", None)
    if config is not None and config.rate_limit is not None:
        if isinstance(limiter, RedisRateLimiter):
            redis_ok = await limiter.ping()
            fail_open = config.rate_limit.fail_open
            if redis_ok:
                checks["rate_limit_redis"] = "ok"
            else:
                checks["rate_limit_redis"] = (
                    "unreachable, failing open" if fail_open else "unreachable"
                )
                ready &= fail_open
        else:
            checks["rate_limit_memory"] = "ok"

    guards = getattr(app.state, "guards", None)
    open_circuits = 0
    if guards is not None:
        open_circuits = sum(b.peek_state() is State.OPEN for b in guards.breakers().values())

    body = {
        "status": "ready" if ready else "not_ready",
        "checks": checks,
        "open_circuits": open_circuits,
    }
    return ready, body
