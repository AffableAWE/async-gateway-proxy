"""In-memory rate limiter: buckets live in a dict inside this process.

Fine for one gateway process. With several (uvicorn --workers 4, or three
Kubernetes replicas) each one keeps its own buckets, so a client gets
`capacity` per process instead of in total. That is what the Redis backend
fixes.

No lock is needed. The event loop runs one coroutine at a time, and `hit`
never awaits between reading a bucket and writing it back, so no other
request can run in the middle of it.
"""

import time
from collections.abc import Callable

from app.ratelimit.base import Bucket, Decision, Limit, take_token


class MemoryRateLimiter:
    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        max_buckets: int = 100_000,
    ) -> None:
        # monotonic, not time.time(): wall-clock time can jump (NTP, manual
        # changes) and would hand out or swallow tokens when it does.
        self._clock = clock
        self._max_buckets = max_buckets
        self._buckets: dict[str, Bucket] = {}
        self._limits: dict[str, Limit] = {}

    async def hit(self, key: str, limit: Limit) -> Decision:
        now = self._clock()
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self._max_buckets:
                self._evict_full_buckets(now)
            bucket = Bucket(tokens=limit.capacity, updated_at=now)  # new clients start full
            self._buckets[key] = bucket
        self._limits[key] = limit
        return take_token(bucket, limit, now)

    def _evict_full_buckets(self, now: float) -> None:
        """Forget buckets that have refilled completely.

        A full bucket is the same as no bucket (new ones start full), so
        dropping it changes nothing for that client. Without this, keying by
        client IP lets the dict grow forever.
        """
        for key in list(self._buckets):
            bucket, limit = self._buckets[key], self._limits[key]
            refilled = bucket.tokens + (now - bucket.updated_at) * limit.refill_per_second
            if refilled >= limit.capacity:
                del self._buckets[key]
                del self._limits[key]

    def __len__(self) -> int:
        return len(self._buckets)

    async def close(self) -> None:
        self._buckets.clear()
        self._limits.clear()
