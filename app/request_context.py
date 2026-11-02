"""Per-request values that any code can read without passing them around.

A ContextVar is like a thread-local, but for asyncio tasks. Uvicorn runs
each request in its own task, and each task gets its own copy of the
context. So hundreds of requests can share one thread and one event loop,
and each one still sees only its own request ID.

A plain module-level global would not work: while request A waits on its
upstream, request B runs and would overwrite the value A reads next.
"""

from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import GatewayConfig

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id() -> str | None:
    """The current request's ID, or None outside a request (e.g. at startup)."""
    return request_id_var.get()


def request_config(scope: dict) -> "GatewayConfig":
    """The config this request runs under.

    The outermost middleware stores a snapshot of app.state.config in the
    request's scope, and everything after it reads that snapshot. A hot
    reload can swap app.state.config at any await; without the snapshot, a
    request could pass auth under the old config and be routed under the new.
    """
    snapshot = scope.get("state", {}).get("config")
    return snapshot if snapshot is not None else scope["app"].state.config
