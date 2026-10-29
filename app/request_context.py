"""Per-request values that any code can read without passing them around.

A ContextVar is like a thread-local, but for asyncio tasks. Uvicorn runs
each request in its own task, and each task gets its own copy of the
context. So hundreds of requests can share one thread and one event loop,
and each one still sees only its own request ID.

A plain module-level global would not work: while request A waits on its
upstream, request B runs and would overwrite the value A reads next.
"""

from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id() -> str | None:
    """The current request's ID, or None outside a request (e.g. at startup)."""
    return request_id_var.get()
