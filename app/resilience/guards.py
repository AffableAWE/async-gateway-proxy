"""Per-process resilience state: one retry budget and one breaker per upstream.

Kept per upstream URL, not global: one upstream that is down should open
only its own breaker and spend only its own retry budget, so routes to
healthy upstreams carry on as normal.

Built once in the app's lifespan and kept on app.state. Tests build their
own with a fake clock, a fake sleep and a fixed rng.
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable

from app.config import GatewayConfig
from app.resilience.breaker import CircuitBreaker
from app.resilience.retry import RetryBudget


class UpstreamGuards:
    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.clock = clock
        self.sleep = sleep
        self.rng = rng
        self._budgets: dict[str, RetryBudget] = {}
        self._breakers: dict[str, CircuitBreaker] = {}

    def budget_for(self, upstream: str, config: GatewayConfig) -> RetryBudget | None:
        if config.retries is None:
            return None
        if upstream not in self._budgets:
            self._budgets[upstream] = RetryBudget(
                ratio=config.retries.budget_ratio,
                min_per_second=config.retries.budget_min_per_second,
                clock=self.clock,
            )
        return self._budgets[upstream]

    def breaker_for(self, upstream: str, config: GatewayConfig) -> CircuitBreaker | None:
        if config.circuit_breaker is None:
            return None
        if upstream not in self._breakers:
            self._breakers[upstream] = CircuitBreaker(
                upstream,
                failure_threshold=config.circuit_breaker.failure_threshold,
                recovery_timeout_seconds=config.circuit_breaker.recovery_timeout_seconds,
                clock=self.clock,
            )
        return self._breakers[upstream]
