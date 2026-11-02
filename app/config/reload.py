"""Hot reload: pick up config.yaml changes without restarting the gateway.

Change a route, revoke an API key, tune a rate limit: save the file, and
within `reload_interval_seconds` (or at once, with `kill -HUP <pid>`) the
gateway uses it. No restart, no dropped connections.

The rules, in order:

1. Notice changes by hashing the file's bytes, not by its modification
   time. In Kubernetes a ConfigMap is updated by swapping a symlink, which
   mtime checks can miss; a hash can't.
2. Validate before using. Broken YAML or an invalid rule is rejected, the
   old config keeps serving, and the error is logged once (not every
   interval) until the file changes again. A typo must never take the
   gateway down.
3. Some settings can't change in a running process, like moving the rate
   limiter from memory to Redis. Changing those is rejected with a clear
   message rather than half-applied. Restart to apply them.
4. Swap with one reference assignment: `app.state.config = new`. Readers
   see the old config or the new one, never a mix. Each request already
   holds its own snapshot (see request_config), so requests in flight
   finish under the config they started with.
"""

import asyncio
import hashlib
import logging
import signal
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError

from app.config.loader import ConfigError, parse_config
from app.config.models import GatewayConfig

logger = logging.getLogger("gateway.config")


def restart_only_changes(old: GatewayConfig, new: GatewayConfig) -> list[str]:
    """Settings that differ between old and new but need a restart to change."""

    def limiter(c: GatewayConfig) -> tuple | None:
        # The memory limiter exists whether or not limiting is on, so
        # turning memory limiting on or off is fine at runtime. A Redis
        # limiter is built once, with its URL, timeout and fail mode.
        r = c.rate_limit
        if r is None or r.backend == "memory":
            return None
        return (r.backend, r.redis_url, r.redis_timeout_seconds, r.fail_open)

    if limiter(old) != limiter(new):
        return ["rate_limit backend / redis settings"]
    return []


class ConfigReloader:
    def __init__(
        self,
        path: str | Path,
        current: GatewayConfig,
        apply: Callable[[GatewayConfig, GatewayConfig], None],
        on_result: Callable[[str], None] = lambda result: None,
    ) -> None:
        """
        apply(old, new)  installs a new config (the app swaps app.state.config
                         and resets what depends on it)
        on_result(r)     called with "success", "failed" or "rejected" (metrics)
        """
        self.path = Path(path)
        self.current = current
        self._apply = apply
        self._on_result = on_result
        self._last_seen = self._fingerprint()  # what's on disk was loaded at startup
        self._task: asyncio.Task | None = None

    def _fingerprint(self) -> str | None:
        try:
            return hashlib.sha256(self.path.read_bytes()).hexdigest()
        except OSError:
            return None

    def reload_now(self, trigger: str = "manual") -> bool:
        """Check the file once. Returns True if a new config was installed.

        Synchronous on purpose: reading and validating a small file takes
        well under a millisecond, and with no await inside, nothing else
        can run halfway through a reload.
        """
        try:
            data = self.path.read_bytes()
        except OSError as exc:
            if self._last_seen is not None:
                self._last_seen = None
                self._fail(trigger, f"cannot read config file: {exc}")
            return False

        digest = hashlib.sha256(data).hexdigest()
        if digest == self._last_seen:
            return False  # unchanged
        # Remember it even if it turns out bad: report a broken file once,
        # then wait for the next edit instead of logging it every interval.
        self._last_seen = digest

        try:
            new = parse_config(data, source=str(self.path))
        except ConfigError as exc:
            self._fail(trigger, str(exc))
            return False
        except ValidationError as exc:
            errors = [
                f"{'.'.join(str(p) for p in e['loc']) or 'config'}: {e['msg']}"
                for e in exc.errors()
            ]
            self._fail(trigger, "invalid config", errors)
            return False

        needs_restart = restart_only_changes(self.current, new)
        if needs_restart:
            logger.error(
                "config_reload_rejected",
                extra={
                    "trigger": trigger,
                    "needs_restart": needs_restart,
                    "kept": "previous config",
                },
            )
            self._on_result("rejected")
            return False

        old, self.current = self.current, new
        self._apply(old, new)
        logger.info(
            "config_reloaded",
            extra={
                "trigger": trigger,
                "routes": [r.path_prefix for r in new.routes],
                "api_keys": len(new.auth.keys) if new.auth is not None else 0,
            },
        )
        self._on_result("success")
        return True

    def _fail(self, trigger: str, error: str, details: list[str] | None = None) -> None:
        logger.error(
            "config_reload_failed",
            extra={
                "trigger": trigger,
                "error": error,
                "details": details or [],
                "kept": "previous config",
            },
        )
        self._on_result("failed")

    # --- running in the background -----------------------------------------

    def start(self) -> None:
        """Start polling, and reload on SIGHUP where the platform allows it."""
        self._task = asyncio.create_task(self._poll())
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, self.reload_now, "sighup")
        except (NotImplementedError, RuntimeError, ValueError, AttributeError):
            # Windows has no SIGHUP, and signal handlers can only be set from
            # the main thread (not under a test client, for one).
            pass

    async def stop(self) -> None:
        try:
            asyncio.get_running_loop().remove_signal_handler(signal.SIGHUP)
        except (NotImplementedError, RuntimeError, ValueError, AttributeError):
            pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _poll(self) -> None:
        while True:
            # Read every loop, so a reload can change the interval too.
            interval = self.current.reload_interval_seconds
            await asyncio.sleep(interval if interval > 0 else 1.0)
            if interval > 0:
                self.reload_now("poll")
