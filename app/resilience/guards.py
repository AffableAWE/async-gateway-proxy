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

    def apply_config(self, old: GatewayConfig, new: GatewayConfig) -> None:
        """After a config reload: drop state that no longer matches the config.

        Budgets and breakers are built with the settings in force when they
        were created. If those settings changed, they are dropped and
        rebuilt on next use, so new thresholds apply at once; a dropped
        breaker starts again closed. State for upstreams no longer in any
        route is dropped too.
        """
        if old.retries != new.retries:
            self._budgets.clear()
        if old.circuit_breaker != new.circuit_breaker:
            self._breakers.clear()
        live = {route.upstream for route in new.routes}
        for table in (self._budgets, self._breakers):
            for upstream in [u for u in table if u not in live]:
                del table[upstream]

    def breakers(self) -> dict[str, CircuitBreaker]:
        """Every breaker created so far, by upstream (a copy, for metrics)."""
        return dict(self._breakers)

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
