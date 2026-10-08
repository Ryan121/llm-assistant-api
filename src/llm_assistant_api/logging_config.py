"""Logging setup shared by the app and the uvicorn workers."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any


class _JSONFormatter(logging.Formatter):
    """JSON formatter for structured logging."""

    def format(self, record: logging.LogRecord) -> str:
        log_data: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%03dZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add optional fields
        if record.exc_info:
            log_data["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            log_data["stack_info"] = self.formatStack(record.stack_info)

        # Add extra fields from record
        for key, value in record.__dict__.items():
            if key not in {
                "name",
                "msg",
                "args",
                "created",
                "filename",
                "funcName",
                "levelname",
                "levelno",
                "lineno",
                "module",
                "msecs",
                "pathname",
                "process",
                "processName",
                "relativeCreated",
                "stack_info",
                "exc_info",
                "thread",
                "threadName",
                "taskName",
            }:
                log_data[key] = value

        return json.dumps(log_data, default=str)


_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"
_JSON_FORMAT = _JSONFormatter()
_configured = False


def configure_logging(level: str = "INFO", log_format: str = "text") -> None:
    """Attach a single stdout handler. Safe to call more than once.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_format: "text" for human-readable, "json" for structured logging
    """
    global _configured

    resolved = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(resolved)

    if not _configured:
        # Remove any existing handlers
        root.handlers.clear()

        handler = logging.StreamHandler(sys.stdout)
        if log_format.lower() == "json":
            handler.setFormatter(_JSON_FORMAT)
        else:
            handler.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(handler)
        _configured = True

    # httpx logs every request at INFO, which duplicates our access log.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
