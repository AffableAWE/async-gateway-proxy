# app/logging_config.py
import json
import logging
import sys
import time
from typing import Any


class JsonFormatter(logging.Formatter):
    """Renders each log record as one JSON line.

    Anything passed via `extra={...}` on a log call becomes a top-level
    field. That's how request handlers attach structured context
    (request_id, upstream, status_code, latency_ms) without string-formatting.
    """

    # Standard fields that logging always adds — we don't want them duplicated
    # in the JSON output as random keys.
    _RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Pull anything attached via `extra={...}` into the top-level payload.
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(level: str = "INFO") -> None:
    """Configure the root logger once. Called from main.py at startup."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()  # Wipe Uvicorn's default handlers so we don't double-log.
    root.addHandler(handler)
    root.setLevel(level)

    # Uvicorn has its own loggers; quiet them down to avoid noisy plain-text access logs.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
