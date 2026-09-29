"""Simulator error envelopes (guide §9) parsed into one exception type.

    {"detail": {"code": "...", "message": "..."}}   domain errors (404/409) and the stream_disconnect 503
    {"error":  {"code": "FAULT_INJECTED", ...}}     injected faults (503)
    {"detail": [ ... ]}                             FastAPI / pydantic validation (422)
"""

from __future__ import annotations

from enum import Enum
from typing import Any

# Domain codes (guide §5.2, §5.5).
IDEMPOTENCY_KEY_MISMATCH = "IDEMPOTENCY_KEY_MISMATCH"
NOT_FOUND = "NOT_FOUND"
ROUTE_MISMATCH = "ROUTE_MISMATCH"
DEPOT_CLOSED = "DEPOT_CLOSED"
STATION_CLOSED = "STATION_CLOSED"
ROUTE_DISRUPTED = "ROUTE_DISRUPTED"
ROUTE_CAPACITY_EXCEEDED = "ROUTE_CAPACITY_EXCEEDED"
INSUFFICIENT_INVENTORY = "INSUFFICIENT_INVENTORY"
DISPATCH_CAPACITY_EXCEEDED = "DISPATCH_CAPACITY_EXCEEDED"
DESTINATION_CAPACITY_EXCEEDED = "DESTINATION_CAPACITY_EXCEEDED"
ALLOCATION_NOT_FOUND = "ALLOCATION_NOT_FOUND"
CANNOT_CANCEL = "CANNOT_CANCEL"
FAULT_INJECTED = "FAULT_INJECTED"


class ErrorKind(str, Enum):
    DOMAIN = "domain"  # 404 / 409 business rule
    FAULT = "fault"  # injected 503
    VALIDATION = "validation"  # 422
    TRANSPORT = "transport"  # timeout / connection error
    INVALID_RESPONSE = "invalid_response"  # payload failed our schema validation
    BREAKER_OPEN = "breaker_open"
    HTTP = "http"  # any other status


class SimError(Exception):
    def __init__(self, kind: ErrorKind, status: int | None, code: str | None, message: str, endpoint: str = ""):
        super().__init__(f"{endpoint} {status} {code}: {message}".strip())
        self.kind = kind
        self.status = status
        self.code = code
        self.message = message
        self.endpoint = endpoint

    @property
    def retryable(self) -> bool:
        return self.kind in (ErrorKind.FAULT, ErrorKind.TRANSPORT) or (self.status is not None and self.status >= 500)


def parse_error(status: int, body: Any, endpoint: str = "") -> SimError:
    code, message = None, ""
    if isinstance(body, dict):
        if isinstance(body.get("error"), dict):
            code = body["error"].get("code")
            message = body["error"].get("message", "")
        elif isinstance(body.get("detail"), dict):
            code = body["detail"].get("code")
            message = body["detail"].get("message", "")
        elif isinstance(body.get("detail"), list):
            code = "VALIDATION_ERROR"
            message = "; ".join(str(e.get("msg", e)) if isinstance(e, dict) else str(e) for e in body["detail"])
        elif isinstance(body.get("detail"), str):
            message = body["detail"]
    elif isinstance(body, str):
        message = body[:300]
    if code == FAULT_INJECTED:
        kind = ErrorKind.FAULT
    elif status == 422:
        kind = ErrorKind.VALIDATION
    elif status in (404, 409):
        kind = ErrorKind.DOMAIN
    else:
        kind = ErrorKind.HTTP
    return SimError(kind, status, code, message or f"HTTP {status}", endpoint)
