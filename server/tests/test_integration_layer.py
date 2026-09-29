"""Simulator client, circuit breaker, error envelopes and SSE parsing (I1-T3, I3-T1, I3-T2)."""

from __future__ import annotations

import json

import httpx
import pytest

from fuelops.backend.integration.breaker import BreakerState, CircuitBreaker
from fuelops.backend.integration.sim_client import SimClient
from fuelops.backend.integration.sim_errors import ErrorKind, SimError, parse_error
from fuelops.backend.integration.sse_listener import SSEListener, parse_sse_lines
from fuelops.shared.sim_models import AllocationRequest
from tests.stack import no_sleep

pytestmark = pytest.mark.req("R-RES-04", "R-SEC-03")

INSTANCE = {"id": 1, "scenario_id": "baseline", "scenario_version": "1.0", "seed": 12345,
            "sim_time": "2026-01-01T00:00:00+00:00", "tick": 0, "tick_minutes": 15, "status": "PAUSED"}
ALLOC = {"id": 1, "idempotency_key": "k", "source_depot_id": "depot-gazipur", "destination_station_id": "station-mirpur",
         "route_id": "route-gazipur-mirpur", "fuel_type": "DIESEL", "quantity": 3000, "created_tick": 5,
         "departure_tick": None, "expected_arrival_tick": None, "actual_arrival_tick": None, "status": "PENDING",
         "failure_reason": None}


def client_with(handler, **kw) -> SimClient:
    return SimClient("http://sim", transport=httpx.MockTransport(handler), sleep=no_sleep, **kw)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


# ------------------------------------------------------------------ breaker


@pytest.mark.req("R-RES-05")
def test_breaker_opens_after_threshold_and_half_opens_after_cooldown():
    """CLOSED -> OPEN after N consecutive failures; HALF_OPEN after the cooldown allows exactly one probe."""
    clock = FakeClock()
    b = CircuitBreaker("t", failure_threshold=3, reset_timeout_s=10, clock=clock)
    for _ in range(2):
        b.record_failure()
    assert b.state is BreakerState.CLOSED and b.allow()
    b.record_failure()
    assert b.state is BreakerState.OPEN and not b.allow()
    clock.t = 10.5
    assert b.state is BreakerState.HALF_OPEN
    assert b.allow() and not b.allow(), "only one probe in HALF_OPEN"
    b.record_success()
    assert b.state is BreakerState.CLOSED and b.failures == 0


def test_breaker_half_open_failure_reopens():
    """A failed probe in HALF_OPEN re-opens the breaker immediately."""
    clock = FakeClock()
    b = CircuitBreaker("t", failure_threshold=1, reset_timeout_s=5, clock=clock)
    b.record_failure()
    clock.t = 6
    assert b.allow()
    b.record_failure()
    assert b.state is BreakerState.OPEN


# ----------------------------------------------------------- error envelopes


@pytest.mark.parametrize("status,body,kind,code", [
    (409, {"detail": {"code": "ROUTE_DISRUPTED", "message": "x"}}, ErrorKind.DOMAIN, "ROUTE_DISRUPTED"),
    (404, {"detail": {"code": "NOT_FOUND"}}, ErrorKind.DOMAIN, "NOT_FOUND"),
    (503, {"error": {"code": "FAULT_INJECTED", "message": "down"}}, ErrorKind.FAULT, "FAULT_INJECTED"),
    (503, {"detail": {"code": "FAULT_INJECTED"}}, ErrorKind.FAULT, "FAULT_INJECTED"),
    (422, {"detail": [{"msg": "quantity must be > 0"}]}, ErrorKind.VALIDATION, "VALIDATION_ERROR"),
    (500, "Internal Server Error", ErrorKind.HTTP, None),
])
def test_parse_all_error_envelopes(status, body, kind, code):
    """All documented envelopes (detail / error / detail-list / stream fault) map to one typed error."""
    err = parse_error(status, body, "ep")
    assert err.kind is kind and err.code == code and err.status == status


def test_retryable_classification():
    """Faults, transport errors and 5xx are retryable; 404/409/422 are not."""
    assert parse_error(503, {"error": {"code": "FAULT_INJECTED"}}).retryable
    assert parse_error(500, "x").retryable
    assert not parse_error(409, {"detail": {"code": "ROUTE_DISRUPTED"}}).retryable
    assert not parse_error(422, {"detail": []}).retryable
    assert SimError(ErrorKind.TRANSPORT, None, None, "timeout").retryable


# ------------------------------------------------------------------- client


async def test_get_instance_parses_and_validates():
    """A valid /v1/instance payload is parsed into the typed model."""
    c = client_with(lambda r: httpx.Response(200, json=INSTANCE))
    inst = await c.get_instance()
    assert inst.tick == 0 and inst.tick_minutes == 15 and not c.stale


@pytest.mark.req("R-RES-02", "R-SEC-02")
async def test_invalid_payload_is_rejected_and_reported():
    """A payload missing required fields raises INVALID_RESPONSE and triggers the invalid-response hook."""
    seen = []

    async def on_invalid(endpoint, message):
        seen.append(endpoint)

    c = client_with(lambda r: httpx.Response(200, json={"id": 1}), on_invalid=on_invalid)
    with pytest.raises(SimError) as exc:
        await c.get_instance()
    assert exc.value.kind is ErrorKind.INVALID_RESPONSE
    assert seen == ["instance"]


async def test_non_json_body_is_invalid_response():
    """A 200 with a non-JSON body is rejected as invalid."""
    c = client_with(lambda r: httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(SimError) as exc:
        await c.get_instance()
    assert exc.value.kind is ErrorKind.INVALID_RESPONSE


async def test_retries_transient_503_then_succeeds():
    """Injected 503s are retried with backoff; the call succeeds once the fault clears."""
    calls = {"n": 0}

    def handler(r):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={"error": {"code": "FAULT_INJECTED", "message": "x"}})
        return httpx.Response(200, json=INSTANCE)

    c = client_with(handler, retries=3)
    assert (await c.get_instance()).tick == 0
    assert calls["n"] == 3


async def test_domain_errors_are_not_retried():
    """A 409 is returned immediately (business rule), not retried."""
    calls = {"n": 0}

    def handler(r):
        calls["n"] += 1
        return httpx.Response(409, json={"detail": {"code": "ROUTE_DISRUPTED", "message": "x"}})

    c = client_with(handler)
    req = AllocationRequest(idempotency_key="k", source_depot_id="d", destination_station_id="s", route_id="r",
                            fuel_type="DIESEL", quantity=10)
    with pytest.raises(SimError) as exc:
        await c.create_allocation(req)
    assert exc.value.code == "ROUTE_DISRUPTED" and calls["n"] == 1


async def test_transport_error_retried_then_raised():
    """Connection errors are retried and surface as TRANSPORT errors when they persist."""
    def handler(r):
        raise httpx.ConnectError("refused", request=r)

    c = client_with(handler, retries=2)
    with pytest.raises(SimError) as exc:
        await c.get_depots()
    assert exc.value.kind is ErrorKind.TRANSPORT


@pytest.mark.req("R-RES-05")
async def test_breaker_opens_on_sustained_faults_and_blocks_calls():
    """After enough failed reads the read breaker opens and further reads fail fast without a request."""
    calls = {"n": 0}

    def handler(r):
        calls["n"] += 1
        return httpx.Response(503, json={"error": {"code": "FAULT_INJECTED", "message": "x"}})

    c = client_with(handler, retries=1, breaker_failure_threshold=3)
    for _ in range(3):
        with pytest.raises(SimError):
            await c.get_stations()
    before = calls["n"]
    with pytest.raises(SimError) as exc:
        await c.get_stations()
    assert exc.value.kind is ErrorKind.BREAKER_OPEN and calls["n"] == before


async def test_health_bypasses_breaker():
    """/v1/health is not guarded by the breaker (it bypasses faults in the simulator)."""
    def handler(r):
        if r.url.path == "/v1/health":
            return httpx.Response(200, json={"status": "ok", "database": "ok",
                                             "simulation": {"status": "PAUSED", "tick": 0}})
        return httpx.Response(503, json={"error": {"code": "FAULT_INJECTED", "message": "x"}})

    c = client_with(handler, retries=1, breaker_failure_threshold=1)
    with pytest.raises(SimError):
        await c.get_stations()
    assert (await c.get_health()).status == "ok"


@pytest.mark.req("R-RES-03")
async def test_stale_header_tracked_per_endpoint():
    """X-Simulator-Stale: true marks the client stale until a fresh response arrives."""
    state = {"stale": True}

    def handler(r):
        headers = {"X-Simulator-Stale": "true"} if state["stale"] else {}
        return httpx.Response(200, json=INSTANCE, headers=headers)

    c = client_with(handler)
    await c.get_instance()
    assert c.stale
    state["stale"] = False
    await c.get_instance()
    assert not c.stale


@pytest.mark.parametrize("status", [200, 201])
async def test_idempotent_replay_accepts_200_and_201(status):
    """Both 200 and 201 on POST /v1/allocations are success (the guide is inconsistent)."""
    c = client_with(lambda r: httpx.Response(status, json=ALLOC))
    req = AllocationRequest(idempotency_key="k", source_depot_id="depot-gazipur",
                            destination_station_id="station-mirpur", route_id="route-gazipur-mirpur",
                            fuel_type="DIESEL", quantity=3000)
    alloc, code = await c.create_allocation(req)
    assert alloc.id == 1 and code == status


async def test_post_retry_reuses_same_idempotency_key():
    """A POST retried after a 503 sends the identical body (same key) every time."""
    bodies = []

    def handler(r):
        bodies.append(json.loads(r.content))
        if len(bodies) == 1:
            return httpx.Response(503, json={"error": {"code": "FAULT_INJECTED", "message": "x"}})
        return httpx.Response(201, json=ALLOC)

    c = client_with(handler)
    req = AllocationRequest(idempotency_key="fo-1-2-1-0", source_depot_id="depot-gazipur",
                            destination_station_id="station-mirpur", route_id="route-gazipur-mirpur",
                            fuel_type="DIESEL", quantity=3000)
    await c.create_allocation(req)
    assert len(bodies) == 2 and bodies[0] == bodies[1]


def test_allocation_request_validates_key_and_quantity():
    """Client-side validation rejects empty/long keys and non-positive quantities before any HTTP call."""
    base = dict(source_depot_id="d", destination_station_id="s", route_id="r", fuel_type="DIESEL")
    with pytest.raises(ValueError):
        AllocationRequest(idempotency_key="", quantity=1, **base)
    with pytest.raises(ValueError):
        AllocationRequest(idempotency_key="x" * 151, quantity=1, **base)
    with pytest.raises(ValueError):
        AllocationRequest(idempotency_key="k", quantity=0, **base)
    with pytest.raises(ValueError):
        AllocationRequest(idempotency_key="k", quantity=1, **{**base, "fuel_type": "KEROSENE"})


async def test_demand_history_limit_is_clamped():
    """limit is always sent and clamped to [1, 2000]."""
    seen = []

    def handler(r):
        seen.append(int(r.url.params["limit"]))
        return httpx.Response(200, json=[])

    c = client_with(handler)
    await c.get_demand_history(limit=99999)
    await c.get_demand_history(limit=0)
    assert seen == [2000, 1]


# ---------------------------------------------------------------------- SSE


def test_sse_parser_ignores_comments_and_joins_data():
    """Comment lines (': connected', ': keepalive') are ignored; event and data fields are parsed."""
    assert parse_sse_lines([": connected"]) == (None, None)
    name, data = parse_sse_lines(["event: simulation.tick", 'data: {"tick": 3}'])
    assert name == "simulation.tick" and json.loads(data) == {"tick": 3}


@pytest.mark.req("R-CAP-06")
async def test_sse_listener_dispatches_events_and_reconnects():
    """The listener dispatches events, reports the stream closing, and reconnects (no replay)."""
    body = (": connected\n\n"
            "event: simulation.tick\ndata: {\"tick\": 1, \"sim_time\": \"2026-01-01T00:15:00+00:00\"}\n\n"
            "event: allocation.status_changed\ndata: {\"id\": 1}\n\n")
    connects = {"n": 0}
    events = []
    listener = None

    def handler(r):
        connects["n"] += 1
        if connects["n"] >= 2:
            listener.stop()
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async def on_event(name, payload):
        events.append(name)

    listener = SSEListener("http://sim", on_event, transport=httpx.MockTransport(handler), max_backoff_s=0.01)
    await listener.run()
    assert "simulation.tick" in events and "allocation.status_changed" in events
    assert "stream.disconnected" in events and connects["n"] >= 2


async def test_sse_listener_handles_503_stream_fault():
    """A stream_disconnect fault (503) is treated as a disconnect and retried."""
    attempts = {"n": 0}
    events = []
    listener = None

    def handler(r):
        attempts["n"] += 1
        if attempts["n"] >= 2:
            listener.stop()
        return httpx.Response(503, json={"detail": {"code": "FAULT_INJECTED"}})

    listener = SSEListener("http://sim", lambda n, p: events.append(n), transport=httpx.MockTransport(handler),
                           max_backoff_s=0.01)
    await listener.run()
    assert attempts["n"] >= 2 and not listener.connected
    assert "503" in (listener.last_error or "")
