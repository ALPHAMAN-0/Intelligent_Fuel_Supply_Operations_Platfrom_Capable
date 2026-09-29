"""Typed, resilient async client for the BUP Fuel Supply Simulator.

- timeouts on every call, retries with full-jitter backoff on 503 / transport errors (never on 404/409/422)
- circuit breakers per group (reads, writes); /v1/health and /admin/* bypass them (they bypass faults too)
- every payload validated with pydantic; invalid payloads are rejected and reported
- X-Simulator-Stale is tracked per endpoint
- POST /v1/allocations is retried with the same idempotency key; 200 and 201 both mean success
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx
from pydantic import TypeAdapter, ValidationError

from fuelops.backend.integration.breaker import STATE_VALUE, BreakerState, CircuitBreaker
from fuelops.backend.integration.sim_errors import ErrorKind, SimError, parse_error
from fuelops.shared.metrics import SIM_BREAKER_STATE, SIM_INVALID_RESPONSES, SIM_LATENCY, SIM_REQUESTS
from fuelops.shared.sim_models import (
    Allocation,
    AllocationRequest,
    AuditEntry,
    DemandObservation,
    Depot,
    EventInjection,
    FaultInjection,
    Health,
    Instance,
    Metrics,
    Region,
    Route,
    SimEvent,
    Station,
    StepResult,
    SupplyArrival,
)

T = TypeVar("T")

_ADAPTERS: dict[Any, TypeAdapter] = {}


def _adapter(tp: Any) -> TypeAdapter:
    if tp not in _ADAPTERS:
        _ADAPTERS[tp] = TypeAdapter(tp)
    return _ADAPTERS[tp]


def _breaker_metric(name: str, state: BreakerState) -> None:
    SIM_BREAKER_STATE.labels(name).set(STATE_VALUE[state])


class SimClient:
    def __init__(
        self,
        base_url: str,
        *,
        connect_timeout_s: float = 2.0,
        read_timeout_s: float = 5.0,
        retries: int = 3,
        breaker_failure_threshold: int = 5,
        breaker_reset_timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        on_invalid: Callable[[str, str], Awaitable[None] | None] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        backoff_base_s: float = 0.2,
        backoff_cap_s: float = 2.0,
    ):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(read_timeout_s, connect=connect_timeout_s),
            transport=transport,
        )
        self.retries = max(1, retries)
        self.read_breaker = CircuitBreaker("sim-read", breaker_failure_threshold, breaker_reset_timeout_s,
                                           on_change=_breaker_metric)
        self.write_breaker = CircuitBreaker("sim-write", breaker_failure_threshold, breaker_reset_timeout_s,
                                            on_change=_breaker_metric)
        _breaker_metric("sim-read", BreakerState.CLOSED)
        _breaker_metric("sim-write", BreakerState.CLOSED)
        self._on_invalid = on_invalid
        self._sleep = sleep
        self._backoff_base = backoff_base_s
        self._backoff_cap = backoff_cap_s
        self.stale_endpoints: set[str] = set()
        self.last_error: SimError | None = None
        self.last_ok_at: float | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def stale(self) -> bool:
        return bool(self.stale_endpoints)

    # ------------------------------------------------------------------ core

    async def _call(
        self,
        method: str,
        path: str,
        *,
        endpoint: str,
        params: dict | None = None,
        json: Any = None,
        group: str | None = "read",
        retries: int | None = None,
    ) -> tuple[Any, int]:
        breaker = {"read": self.read_breaker, "write": self.write_breaker}.get(group or "")
        attempts = retries if retries is not None else self.retries
        err: SimError | None = None
        for attempt in range(1, attempts + 1):
            probing = breaker is not None and breaker.state is BreakerState.HALF_OPEN
            if breaker is not None and not breaker.allow():
                SIM_REQUESTS.labels(endpoint, "breaker_open").inc()
                raise SimError(ErrorKind.BREAKER_OPEN, None, None, f"circuit breaker {breaker.name} open", endpoint)
            start = time.perf_counter()
            try:
                resp = await self._http.request(method, path, params=params, json=json)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                err = SimError(ErrorKind.TRANSPORT, None, None, f"{type(exc).__name__}: {exc}", endpoint)
            else:
                SIM_LATENCY.labels(endpoint).observe(time.perf_counter() - start)
                if resp.status_code < 400:
                    if breaker is not None:
                        breaker.record_success()
                    if resp.headers.get("x-simulator-stale", "").lower() == "true":
                        self.stale_endpoints.add(endpoint)
                    else:
                        self.stale_endpoints.discard(endpoint)
                    try:
                        body = resp.json()
                    except ValueError as exc:
                        raise await self._invalid(endpoint, f"non-JSON body: {exc}") from exc
                    SIM_REQUESTS.labels(endpoint, "ok").inc()
                    self.last_ok_at = time.time()
                    return body, resp.status_code
                try:
                    body = resp.json()
                except ValueError:
                    body = resp.text
                err = parse_error(resp.status_code, body, endpoint)
            SIM_REQUESTS.labels(endpoint, err.kind.value).inc()
            self.last_error = err
            if not err.retryable:
                if breaker is not None:
                    breaker.record_success()  # the API answered; a business-rule rejection is not an outage
                raise err
            if probing:
                # A failed HALF_OPEN probe re-opens the breaker now. Retrying would only be refused by allow() and
                # used to leave the probe "in flight" forever, so the client never recovered after an outage.
                breaker.record_failure()
                raise err
            if attempt < attempts:
                delay = random.uniform(0, min(self._backoff_cap, self._backoff_base * 2 ** (attempt - 1)))
                await self._sleep(delay)
        assert err is not None
        if breaker is not None:
            breaker.record_failure()  # one failure per call that exhausted its retries, not per attempt
        raise err

    async def _invalid(self, endpoint: str, message: str) -> SimError:
        SIM_INVALID_RESPONSES.labels(endpoint).inc()
        SIM_REQUESTS.labels(endpoint, "invalid_response").inc()
        err = SimError(ErrorKind.INVALID_RESPONSE, None, "INVALID_RESPONSE", message[:500], endpoint)
        self.last_error = err
        if self._on_invalid is not None:
            res = self._on_invalid(endpoint, message[:500])
            if asyncio.iscoroutine(res):
                await res
        return err

    async def _get(self, path: str, tp: Any, endpoint: str, params: dict | None = None, group: str | None = "read"):
        body, _ = await self._call("GET", path, endpoint=endpoint, params=params, group=group)
        try:
            return _adapter(tp).validate_python(body)
        except ValidationError as exc:
            raise await self._invalid(endpoint, str(exc)) from exc

    # ----------------------------------------------------------------- reads

    async def get_health(self) -> Health:
        return await self._get("/v1/health", Health, "health", group=None)

    async def get_instance(self) -> Instance:
        return await self._get("/v1/instance", Instance, "instance")

    async def get_regions(self) -> list[Region]:
        return await self._get("/v1/regions", list[Region], "regions")

    async def get_depots(self) -> list[Depot]:
        return await self._get("/v1/depots", list[Depot], "depots")

    async def get_depot(self, depot_id: str) -> Depot:
        return await self._get(f"/v1/depots/{depot_id}", Depot, "depot")

    async def get_stations(self) -> list[Station]:
        return await self._get("/v1/stations", list[Station], "stations")

    async def get_station(self, station_id: str) -> Station:
        return await self._get(f"/v1/stations/{station_id}", Station, "station")

    async def get_routes(self) -> list[Route]:
        return await self._get("/v1/routes", list[Route], "routes")

    async def get_supply_arrivals(self) -> list[SupplyArrival]:
        return await self._get("/v1/supply-arrivals", list[SupplyArrival], "supply_arrivals")

    async def get_events(self) -> list[SimEvent]:
        return await self._get("/v1/events", list[SimEvent], "events")

    async def get_allocations(self) -> list[Allocation]:
        return await self._get("/v1/allocations", list[Allocation], "allocations")

    async def get_demand_history(self, limit: int = 200, station_id: str | None = None) -> list[DemandObservation]:
        params: dict[str, Any] = {"limit": max(1, min(2000, int(limit)))}
        if station_id:
            params["station_id"] = station_id
        return await self._get("/v1/demand-history", list[DemandObservation], "demand_history", params)

    async def get_metrics(self) -> Metrics:
        return await self._get("/v1/metrics", Metrics, "metrics")

    # ---------------------------------------------------------------- writes

    async def create_allocation(self, req: AllocationRequest) -> tuple[Allocation, int]:
        """Safe to retry: same idempotency key + same body returns the existing allocation."""
        body, status = await self._call("POST", "/v1/allocations", endpoint="create_allocation",
                                        json=req.model_dump(), group="write")
        try:
            return Allocation.model_validate(body), status
        except ValidationError as exc:
            raise await self._invalid("create_allocation", str(exc)) from exc

    async def cancel_allocation(self, allocation_id: int) -> Allocation:
        body, _ = await self._call("POST", f"/v1/allocations/{allocation_id}/cancel", endpoint="cancel_allocation",
                                   group="write", retries=1)
        try:
            return Allocation.model_validate(body)
        except ValidationError as exc:
            raise await self._invalid("cancel_allocation", str(exc)) from exc

    # ----------------------------------------------------------------- admin
    # /admin/* bypasses fault injection. Used only by tests, scripts and the token-gated chaos proxy.

    async def admin_post(self, action: str, payload: dict | None = None) -> Any:
        body, _ = await self._call("POST", f"/admin/{action}", endpoint=f"admin_{action.replace('/', '_')}",
                                   json=payload, group=None, retries=2)
        return body

    async def admin_run(self) -> Any:
        return await self.admin_post("run")

    async def admin_pause(self) -> Any:
        return await self.admin_post("pause")

    async def admin_step(self) -> StepResult:
        return StepResult.model_validate(await self.admin_post("step"))

    async def admin_reset(self) -> Any:
        return await self.admin_post("reset")

    async def admin_inject_event(self, event: EventInjection) -> Any:
        return await self.admin_post("events", event.model_dump())

    async def admin_inject_fault(self, fault: FaultInjection) -> Any:
        return await self.admin_post("faults", fault.model_dump())

    async def admin_clear_faults(self) -> Any:
        return await self.admin_post("faults/clear")

    async def admin_audit(self, limit: int = 200) -> list[AuditEntry]:
        return await self._get("/admin/audit", list[AuditEntry], "admin_audit",
                               {"limit": max(1, min(1000, limit))}, group=None)

    async def admin_get(self, path: str) -> Any:
        body, _ = await self._call("GET", f"/admin/{path}", endpoint=f"admin_get_{path}", group=None, retries=2)
        return body
