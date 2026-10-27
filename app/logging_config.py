"""Structured JSON logging: one JSON object per line on stdout.

Usage anywhere in the app:
    logger.info("proxy_forward", extra={"upstream": url, "status_code": 200})

The message is the event name. Everything in `extra` becomes a top-level
field, so log tools (Loki, ELK, Datadog) can filter on it.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

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


def setup_logging(level: str = "INFO") -> None:
    """Send all logs through one JSON handler on stdout. Safe to call twice."""
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
