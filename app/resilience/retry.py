"""When to retry an upstream call, how long to wait, and how many retries to allow.

Three questions, three pieces:

    is_retryable()   is this failure safe and worth retrying?
    backoff_delay()  how long to wait before the next attempt?
    RetryBudget      are we already retrying too much?
"""

import random
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

# RFC 9110 section 9.2.2: sending these twice has the same effect as once.
# POST and PATCH are not idempotent: a retried POST /payments could charge
# a card twice.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

# Upstream answers that mean "not right now", not "your request is wrong".
# 500 is left out: it usually means a bug that will fail the same way again.
RETRYABLE_STATUSES = frozenset({502, 503, 504})


class Failure(Enum):
    CONNECT = "connect"  # never reached the upstream: request not sent
    TIMEOUT = "timeout"  # sent, but no answer in time
    TRANSPORT = "transport"  # sent, then the connection broke
    STATUS = "status"  # upstream answered 502/503/504


def is_retryable(method: str, failure: Failure) -> bool:
    """Is it safe, and worth it, to send this request again?

    CONNECT: the request never left the gateway, so even a POST is safe.
    TIMEOUT: never retried. The upstream may still be working on it, and a
        slow upstream is usually an overloaded one; retrying adds load and
        multiplies the client's wait by the number of attempts.
    TRANSPORT / STATUS: the upstream may have acted on the request, so
        only idempotent methods are retried.
    """
    if failure is Failure.CONNECT:
        return True
    if failure is Failure.TIMEOUT:
        return False
    return method.upper() in IDEMPOTENT_METHODS


def backoff_delay(
    retry_number: int,
    base_seconds: float,
    max_seconds: float,
    rng: Callable[[float, float], float] = random.uniform,
) -> float:
    """Exponential backoff with full jitter.

    retry_number 1, 2, 3... -> a random wait in [0, base], [0, 2*base],
    [0, 4*base]..., never more than max_seconds.

    Why random? Picture 1,000 clients that all failed at the same moment. With
    a fixed delay they all retry at the same moment too, and knock the
    upstream over again (a "thundering herd"). Random waits spread them out.
    "Full jitter" (random from 0 to the cap) spreads them the most.
    """
    cap = min(max_seconds, base_seconds * 2 ** (retry_number - 1))
    return rng(0, cap)


class RetryBudget:
    """Caps retries at a fraction of real traffic, per upstream.

    Retries are extra load, sent exactly when an upstream is struggling. If
    every request retries 2 times, a struggling upstream suddenly gets 3x
    the traffic and may never recover (a "retry storm").

    Over the last `window_seconds`, retries may be at most
        ratio * requests  +  min_per_second * window_seconds
    The second part is a small floor, so a quiet upstream with little
    traffic can still get a few retries.
    """

    def __init__(
        self,
        ratio: float = 0.2,
        min_per_second: float = 1.0,
        window_seconds: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ratio = ratio
        self.min_per_second = min_per_second
        self.window_seconds = window_seconds
        self._clock = clock
        self._requests: deque[float] = deque()
        self._retries: deque[float] = deque()

    def record_request(self) -> None:
        """Call once per incoming request (not per attempt)."""
        now = self._clock()
        self._prune(now)
        self._requests.append(now)

    def try_spend(self) -> bool:
        """Take one retry from the budget. False means: don't retry."""
        now = self._clock()
        self._prune(now)
        allowed = self.ratio * len(self._requests) + self.min_per_second * self.window_seconds
        if len(self._retries) >= allowed:
            return False
        self._retries.append(now)
        return True

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        for timestamps in (self._requests, self._retries):
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int  # total tries, including the first
    base_delay_seconds: float
    max_delay_seconds: float
