"""Redis rate limiter: buckets live in Redis, shared by every gateway process.

With 3 gateway replicas and in-memory buckets, a client limited to 10
requests could send 30 (10 to each). Here all replicas read and write the
same bucket in Redis, so the limit holds across the whole fleet.

The whole check runs as one Lua script inside Redis. Doing it from Python
as GET, compute, SET would race: two replicas could both read "1 token
left" and both let a request through. Redis runs a script start to finish
without interleaving any other command, so read-refill-take-write is atomic.

The script takes the time from Redis (the TIME command), not from the
gateway. Replicas on different machines have slightly different clocks; one
clock for everyone means a fast clock can't hand out extra tokens.
Requires Redis 5 or newer.
"""

import logging
import math
import time

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.ratelimit.base import Decision, Limit

logger = logging.getLogger("gateway.ratelimit")

# Same steps as take_token() in base.py. Each bucket is a hash with two
# fields: tokens (float) and ts (seconds, from Redis TIME).
#
# Floats are returned as strings: Redis turns a Lua number reply into an
# integer, which would round 2.7 tokens down to 2 and break retry_after.
TOKEN_BUCKET_LUA = """
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local ttl_ms = tonumber(ARGV[3])

local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000

local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil then
  tokens = capacity
  ts = now
end

tokens = math.min(capacity, tokens + math.max(0, now - ts) * rate)

local allowed = 0
local retry_after = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  retry_after = (1 - tokens) / rate
end

redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', KEYS[1], ttl_ms)
return {allowed, tostring(tokens), tostring(retry_after)}
"""

# When Redis is unreachable, log at most this often instead of on every request.
_ERROR_LOG_INTERVAL_S = 10.0


class RedisRateLimiter:
    def __init__(self, client: Redis, fail_open: bool = True, key_prefix: str = "gw:rl:") -> None:
        self._redis = client
        self._fail_open = fail_open
        self._prefix = key_prefix
        # register_script sends the script once and then calls it by its
        # SHA1 (EVALSHA), so each request sends a short hash, not the code.
        # If Redis restarts and forgets it, redis-py re-sends it.
        self._script = client.register_script(TOKEN_BUCKET_LUA)
        self._last_error_log = -math.inf

    @classmethod
    def from_url(cls, url: str, timeout_seconds: float, fail_open: bool) -> "RedisRateLimiter":
        # Short timeouts: this runs on every request. If Redis hangs, a
        # request should give up on the limiter quickly, not hang with it.
        client = Redis.from_url(
            url, socket_timeout=timeout_seconds, socket_connect_timeout=timeout_seconds
        )
        return cls(client, fail_open=fail_open)

    async def hit(self, key: str, limit: Limit) -> Decision:
        # The key expires once the bucket would be full again. A full
        # bucket and a missing one mean the same thing, so idle clients
        # cost no memory in Redis.
        ttl_ms = math.ceil(limit.capacity / limit.refill_per_second * 1000) + 1000
        try:
            allowed, tokens, retry_after = await self._script(
                keys=[self._prefix + key],
                args=[limit.capacity, limit.refill_per_second, ttl_ms],
            )
        except RedisError as exc:
            return self._on_error(exc, limit)

        return Decision(
            allowed=bool(allowed),
            limit=limit.capacity,
            remaining=math.floor(float(tokens)),
            retry_after=float(retry_after),
        )

    def _on_error(self, exc: RedisError, limit: Limit) -> Decision:
        now = time.monotonic()
        if now - self._last_error_log >= _ERROR_LOG_INTERVAL_S:
            self._last_error_log = now
            logger.error(
                "rate_limit_backend_error",
                extra={
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "fail_open": self._fail_open,
                },
            )
        if self._fail_open:
            # Let the request through. remaining=None: we don't know it, so
            # no X-RateLimit-Remaining header is sent.
            return Decision(True, limit.capacity, None, 0.0)
        return Decision(False, limit.capacity, 0, 1.0)

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except RedisError:
            return False

    async def close(self) -> None:
        await self._redis.aclose()
