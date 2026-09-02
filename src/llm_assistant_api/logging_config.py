"""Logging setup shared by the app and the uvicorn workers."""

from __future__ import annotations

import logging
import sys

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"
_configured = False


def configure_logging(level: str = "INFO") -> None:
    """Attach a single stdout handler. Safe to call more than once."""
    global _configured

    resolved = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(resolved)

    if not _configured:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(handler)
        _configured = True

    # httpx logs every request at INFO, which duplicates our access log.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
