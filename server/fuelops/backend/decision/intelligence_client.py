"""HTTP client for the intelligence service, with a timeout and a circuit breaker.

Any failure raises IntelligenceUnavailable; the orchestrator then switches to the heuristic fallback.
"""

from __future__ import annotations

import time

import httpx
from pydantic import ValidationError

from fuelops.backend.integration.breaker import CircuitBreaker
from fuelops.shared.metrics import INTELLIGENCE_UP
from fuelops.shared.models import CycleRequest, CycleResponse, EvaluateRequest, EvaluateResponse


class IntelligenceUnavailable(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class IntelligenceClient:
    def __init__(self, base_url: str, timeout_s: float = 3.0, transport: httpx.AsyncBaseTransport | None = None,
                 failure_threshold: int = 2, reset_timeout_s: float = 10.0):
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=httpx.Timeout(timeout_s, connect=1.0),
                                       transport=transport)
        self.breaker = CircuitBreaker("intelligence", failure_threshold, reset_timeout_s)
        self.up: bool | None = None
        self.last_error: str | None = None
        self.last_latency_ms: float | None = None
        self.model_version: str | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(self, path: str, body: str, reason_prefix: str) -> dict:
        if not self.breaker.allow():
            raise IntelligenceUnavailable("breaker_open", "intelligence circuit breaker open")
        start = time.perf_counter()
        try:
            resp = await self._http.post(path, content=body, headers={"Content-Type": "application/json"})
        except httpx.TimeoutException as exc:
            self._failed(f"{reason_prefix} timeout: {exc}")
            raise IntelligenceUnavailable("timeout", str(exc) or "timeout") from exc
        except httpx.TransportError as exc:
            self._failed(f"{reason_prefix} unreachable: {exc}")
            raise IntelligenceUnavailable("unreachable", str(exc) or type(exc).__name__) from exc
        self.last_latency_ms = (time.perf_counter() - start) * 1000
        if resp.status_code >= 500:
            self._failed(f"{reason_prefix} HTTP {resp.status_code}")
            raise IntelligenceUnavailable("server_error", f"HTTP {resp.status_code}")
        if resp.status_code >= 400:
            self.breaker.record_success()
            raise IntelligenceUnavailable("bad_request", f"HTTP {resp.status_code}: {resp.text[:200]}")
        self.breaker.record_success()
        self.up, self.last_error = True, None
        INTELLIGENCE_UP.set(1)
        return resp.json()

    def _failed(self, error: str) -> None:
        self.breaker.record_failure()
        self.up, self.last_error = False, error
        INTELLIGENCE_UP.set(0)

    async def cycle(self, req: CycleRequest) -> CycleResponse:
        data = await self._post("/cycle", req.model_dump_json(), "cycle")
        try:
            resp = CycleResponse.model_validate(data)
        except ValidationError as exc:
            self._failed("cycle invalid response")
            raise IntelligenceUnavailable("invalid_response", str(exc)[:300]) from exc
        self.model_version = resp.model.version
        return resp

    async def evaluate(self, req: EvaluateRequest) -> EvaluateResponse:
        data = await self._post("/evaluate", req.model_dump_json(), "evaluate")
        return EvaluateResponse.model_validate(data)

    async def health(self) -> bool:
        try:
            resp = await self._http.get("/health", timeout=2.0)
            ok = resp.status_code == 200
        except httpx.HTTPError as exc:
            ok = False
            self.last_error = f"health: {exc}"
        if ok and not self.breaker.state == "OPEN":
            self.up = True
            INTELLIGENCE_UP.set(1)
        elif not ok:
            self.up = False
            INTELLIGENCE_UP.set(0)
        return ok
