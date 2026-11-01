"""Token bucket: the rate-limiting algorithm both backends share.

Picture a bucket that holds up to `capacity` tokens and refills at
`refill_per_second`. Each request takes one token. Empty bucket: rejected.

    capacity           how big a burst is allowed (e.g. 20 requests at once)
    refill_per_second  the long-run average rate (e.g. 10 requests/second)

Why token bucket and not a fixed window ("100 requests per minute")?
A fixed window lets a client send 100 at 0:59 and 100 more at 1:00: 200 in
two seconds. The bucket has no window edges, so a burst can never be more
than `capacity`.

Nothing ticks in the background. A bucket stores its token count and the
time it was last touched, and adds the tokens it earned since then the next
time it is used ("lazy refill"). That keeps each check O(1).
"""

import math
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Limit:
    capacity: int
    refill_per_second: float


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int  # bucket capacity, for the X-RateLimit-Limit header
    remaining: int | None  # whole tokens left after this request; None if unknown
    retry_after: float  # seconds until one token is back; 0 if allowed


@dataclass
class Bucket:
    tokens: float
    updated_at: float


def take_token(bucket: Bucket, limit: Limit, now: float) -> Decision:
    """Refill the bucket for the time that passed, then try to take one token.

    Mutates `bucket`. This is the whole algorithm; the Redis backend runs
    the same steps in a Lua script.
    """
    elapsed = max(0.0, now - bucket.updated_at)  # never negative, even if a clock steps back
    bucket.tokens = min(limit.capacity, bucket.tokens + elapsed * limit.refill_per_second)
    bucket.updated_at = now

    if bucket.tokens >= 1:
        bucket.tokens -= 1
        return Decision(True, limit.capacity, math.floor(bucket.tokens), 0.0)

    # Rejected: say how long until the bucket holds one whole token again.
    retry_after = (1 - bucket.tokens) / limit.refill_per_second
    return Decision(False, limit.capacity, 0, retry_after)


class RateLimiter(Protocol):
    """What the middleware needs from a backend (in-memory or Redis)."""

    async def hit(self, key: str, limit: Limit) -> Decision: ...

    async def close(self) -> None: ...
