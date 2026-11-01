"""Circuit breaker: stop calling an upstream that keeps failing.

Like the breaker in a fuse box. After `failure_threshold` failures in a row
it "opens", and calls fail at once with 503 instead of waiting on an upstream
that is down. That helps both sides:
  - clients get a fast answer instead of piling up behind timeouts
  - the upstream gets breathing room to recover, instead of being hammered

    CLOSED --(N failures in a row)--> OPEN --(recovery timeout)--> HALF_OPEN
      ^                                ^                              |
      |                                +-------(probe fails)----------+
      +-----------------------(probe succeeds)------------------------+

HALF_OPEN lets exactly one request through as a probe. If it succeeds the
upstream is back; if it fails, the breaker opens again for another timeout.

One breaker per upstream URL, per gateway process. Replicas don't share
breaker state; each learns about a dead upstream on its own, which takes
only `failure_threshold` requests.
"""

import logging
import math
import time
from collections.abc import Callable
from enum import Enum

logger = logging.getLogger("gateway.breaker")


class State(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self._clock = clock
        self._state = State.CLOSED
        self._failures = 0  # consecutive, in CLOSED
        self._opened_at = 0.0
        self._probe_in_flight = False
        self._probe_started_at = 0.0

    @property
    def state(self) -> State:
        # OPEN turns into HALF_OPEN by itself once the timeout has passed.
        # Checked lazily when someone asks; no timers.
        if self._state is State.OPEN and self._clock() >= self._reopen_at():
            self._transition(State.HALF_OPEN)
        return self._state

    def allow_request(self) -> bool:
        state = self.state
        if state is State.CLOSED:
            return True
        if state is State.HALF_OPEN:
            now = self._clock()
            # A probe whose result never came back (its request was
            # cancelled, say) would block every later request forever. So
            # a probe older than one recovery timeout counts as lost.
            probe_lost = now - self._probe_started_at >= self.recovery_timeout_seconds
            if not self._probe_in_flight or probe_lost:
                self._probe_in_flight = True  # this request is the one probe
                self._probe_started_at = now
                return True
        return False  # OPEN, or HALF_OPEN with the probe already out

    def record_success(self) -> None:
        if self._state is State.HALF_OPEN:
            self._probe_in_flight = False
            self._transition(State.CLOSED)
        self._failures = 0

    def record_failure(self) -> None:
        if self._state is State.HALF_OPEN:
            self._probe_in_flight = False
            self._open()
            return
        self._failures += 1
        if self._state is State.CLOSED and self._failures >= self.failure_threshold:
            self._open()

    def retry_after_seconds(self) -> int:
        """Whole seconds until the breaker will let a probe through."""
        if self._state is not State.OPEN:
            return 1
        return max(1, math.ceil(self._reopen_at() - self._clock()))

    def _open(self) -> None:
        self._opened_at = self._clock()
        self._failures = 0
        self._transition(State.OPEN)

    def _reopen_at(self) -> float:
        return self._opened_at + self.recovery_timeout_seconds

    def _transition(self, new: State) -> None:
        old, self._state = self._state, new
        log = logger.warning if new is State.OPEN else logger.info
        log(
            "circuit_state_change",
            extra={"upstream": self.name, "from_state": old.value, "to_state": new.value},
        )
