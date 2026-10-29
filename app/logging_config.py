"""Structured JSON logging: one JSON object per line on stdout.

Usage anywhere in the app:
    logger.info("proxy_forward", extra={"upstream": url, "status_code": 200})

The message is the event name. Everything in `extra` becomes a top-level
field, so log tools (Loki, ELK, Datadog) can filter on it.

Inside a request, every line also gets a `request_id` field automatically.
Never pass `request_id` in `extra` yourself: logging raises KeyError when
`extra` tries to overwrite an attribute the record already has.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.request_context import get_request_id

# Attributes every LogRecord has. We skip them so only our own `extra`
# fields end up in the JSON. `color_message` is added by uvicorn.
_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "taskName", "color_message",
}  # fmt: skip


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # default=str: never crash on a value json can't encode (URL, datetime...).
        return json.dumps(payload, default=str)


def install_request_id_factory() -> None:
    """Stamp the current request ID onto every log record as it is created.

    Why a record factory and not a logging.Filter: a filter on a logger does
    not run for records coming up from child loggers, and a filter on our
    handler would miss other handlers (pytest's log capture, for one). The
    factory runs once per record, before any logger or handler sees it.
    """
    old_factory = logging.getLogRecordFactory()
    if getattr(old_factory, "adds_request_id", False):
        return  # already installed; don't wrap twice

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = old_factory(*args, **kwargs)
        request_id = get_request_id()
        if request_id is not None:  # outside a request, leave the field out
            record.request_id = request_id
        return record

    factory.adds_request_id = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)


def setup_logging(level: str = "INFO") -> None:
    """Send all logs through one JSON handler on stdout. Safe to call twice."""
    install_request_id_factory()

    root = logging.getLogger()
    root.setLevel(level)

    already_set = any(isinstance(h.formatter, JsonFormatter) for h in root.handlers)
    if not already_set:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)

    # Uvicorn attaches its own plain-text handlers. Remove them and let its
    # records flow up to the root logger, so every line is JSON.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        if name == "uvicorn.access" and not uv_logger.handlers:
            # No handlers means uvicorn was started with --no-access-log.
            # Respect that instead of switching access logs back on.
            continue
        uv_logger.handlers.clear()
        uv_logger.propagate = True

    # httpx logs every outbound request at INFO. Our proxy_forward line
    # already covers that, with more fields, so keep only its warnings.
    logging.getLogger("httpx").setLevel(logging.WARNING)
