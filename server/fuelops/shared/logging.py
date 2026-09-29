"""Structured JSON logging to stdout (stdlib only).

    log = get_logger("sync")
    log.info("sync.completed", tick=12, duration_ms=40)
    -> {"ts": "...", "level": "info", "service": "backend", "logger": "sync", "event": "sync.completed", "tick": 12, ...}
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("log_context", default={})
_SERVICE = {"name": "fuelops"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "service": _SERVICE["name"],
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update(_context.get())
        payload.update(getattr(record, "fields", {}) or {})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", service: str = "fuelops") -> None:
    _SERVICE["name"] = service
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def bind(**fields: Any) -> None:
    _context.set({**_context.get(), **fields})


class StructLogger:
    def __init__(self, name: str):
        self._log = logging.getLogger(name)

    def _emit(self, level: int, event: str, exc_info: bool = False, **fields: Any) -> None:
        if self._log.isEnabledFor(level):
            self._log.log(level, event, extra={"fields": fields}, exc_info=exc_info)

    def debug(self, event: str, **fields: Any) -> None:
        self._emit(logging.DEBUG, event, **fields)

    def info(self, event: str, **fields: Any) -> None:
        self._emit(logging.INFO, event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._emit(logging.WARNING, event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self._emit(logging.ERROR, event, **fields)

    def exception(self, event: str, **fields: Any) -> None:
        self._emit(logging.ERROR, event, exc_info=True, **fields)


def get_logger(name: str = "fuelops") -> StructLogger:
    return StructLogger(name)
