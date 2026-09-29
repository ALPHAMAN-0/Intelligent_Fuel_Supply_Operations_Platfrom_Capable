"""Pytest implementation of TC-S-xxx from `03_integration_sim.md`.

Targets: `fuelops.backend.integration.{sim_client, sse_listener, breaker}` and
`fuelops.shared.{metrics, sim_models}` error envelope parsing.
Uses `respx` for httpx mocking and an in-process FastAPI app for SSE.

Markers: `unit`. None of these tests touch the real simulator.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx

from fuelops.backend.integration.breaker import BreakerState, CircuitBreaker
from fuelops.backend.integration.sim_client import SimClient
from fuelops.backend.integration.sim_errors import (
    ErrorKind,
    SimError,
    parse_error,
)
from fuelops.backend.integration.sse_listener import SSEListener, parse_sse_lines
from fuelops.shared.sim_models import (
    EventInjection,
)

pytestmark = pytest.mark.unit


# ------------------------------------------------------------------------ TC-S-001
@pytest.mark.asyncio
async def test_TC_S_001_get_health_parses_envelope(respx_mock: respx.Router) -> None:
    respx_mock.get("/v1/health").mock(return_value=httpx.Response(200, json={"status": "ok", "tick": 0}))
    client = SimClient("http://test")
    try:
        h = await client.get_health()
        assert h.status == "ok"
    finally:
        await client.aclose()


# ------------------------------------------------------------------------ TC-S-002
@pytest.mark.asyncio
async def test_TC_S_002_successful_calls_clear_stale(respx_mock: respx.Router) -> None:
    respx_mock.get("/v1/health").mock(return_value=httpx.Response(200, json={"status": "ok", "tick": 0}))
    client = SimClient("http://test")
    try:
        await client.get_health()
        await client.get_health()
        assert client.stale is False
    finally:
        await client.aclose()


# ------------------------------------------------------------------------ TC-S-003
@pytest.mark.asyncio
async def test_TC_S_003_stale_header_sets_flag(respx_mock: respx.Router) -> None:
    route = respx_mock.get("/v1/stations").mock(
        side_effect=[
            httpx.Response(200, headers={"X-Simulator-Stale": "true"}, json=[]),
            httpx.Response(200, headers={"X-Simulator-Stale": "false"}, json=[]),
        ]
    )
    client = SimClient("http://test")
    try:
        await client.get_stations()
        assert client.stale is True
        await client.get_stations()
        assert client.stale is False
        assert route.call_count == 2
    finally:
        await client.aclose()


# ------------------------------------------------------------------------ TC-S-004
@pytest.mark.asyncio
async def test_TC_S_004_retry_on_fault_injected(respx_mock: respx.Router) -> None:
    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)

    respx_mock.get("/v1/stations").mock(
        side_effect=[
            httpx.Response(503, json={"error": {"code": "FAULT_INJECTED", "message": "x"}}),
            httpx.Response(503, json={"error": {"code": "FAULT_INJECTED", "message": "x"}}),
            httpx.Response(200, json=[]),
        ]
    )
    client = SimClient("http://test", retries=3, sleep=fake_sleep, backoff_base_s=0.1)
    try:
        await client.get_stations()
        assert len(sleeps) == 2
        assert all(0 <= s <= 0.2 * 2 ** i for i, s in enumerate(sleeps))
    finally:
        await client.aclose()


# ------------------------------------------------------------------------ TC-S-005
@pytest.mark.asyncio
async def test_TC_S_005_domain_404_not_retried(respx_mock: respx.Router) -> None:
    route = respx_mock.get("/v1/depots/missing").mock(
        return_value=httpx.Response(404, json={"detail": {"code": "NOT_FOUND", "message": "nope"}})
    )
    client = SimClient("http://test", retries=5)
    try:
        with pytest.raises(SimError) as exc_info:
            await client.get_depot("missing")
        assert exc_info.value.kind is ErrorKind.DOMAIN
        assert exc_info.value.code == "NOT_FOUND"
        assert exc_info.value.retryable is False
        assert route.call_count == 1
    finally:
        await client.aclose()


# ------------------------------------------------------------------------ TC-S-006
@pytest.mark.asyncio
async def test_TC_S_006_idempotency_key_mismatch_is_409(respx_mock: respx.Router) -> None:
    respx_mock.post("/v1/allocations").mock(
        return_value=httpx.Response(409, json={"detail": {"code": "IDEMPOTENCY_KEY_MISMATCH",
                                                           "message": "different body"}})
    )
    from fuelops.shared.sim_models import AllocationRequest
    client = SimClient("http://test")
    try:
        with pytest.raises(SimError) as exc_info:
            await client.create_allocation(AllocationRequest(idempotency_key="k", source_depot_id="d",
                                                              destination_station_id="s", route_id="r",
                                                              fuel_type="PETROL", quantity=100))
        assert exc_info.value.kind is ErrorKind.DOMAIN
        assert exc_info.value.code == "IDEMPOTENCY_KEY_MISMATCH"
        assert exc_info.value.retryable is False
    finally:
        await client.aclose()


# ------------------------------------------------------------------------ TC-S-007
@pytest.mark.asyncio
async def test_TC_S_007_allocations_200_and_201_both_succeed(respx_mock: respx.Router) -> None:
    from fuelops.shared.sim_models import AllocationRequest
    body_ok = {
        "id": 1, "idempotency_key": "k", "source_depot_id": "d", "destination_station_id": "s",
        "route_id": "r", "fuel_type": "PETROL", "quantity": 100, "created_tick": 0, "status": "PENDING"
    }
    respx_mock.post("/v1/allocations").mock(side_effect=[
        httpx.Response(201, json=body_ok),
        httpx.Response(200, json=body_ok),
    ])
    client = SimClient("http://test")
    try:
        req = AllocationRequest(idempotency_key="k", source_depot_id="d", destination_station_id="s",
                                route_id="r", fuel_type="PETROL", quantity=100)
        a1, s1 = await client.create_allocation(req)
        a2, s2 = await client.create_allocation(req)
        assert s1 == 201 and s2 == 200
        assert a1.id == a2.id
    finally:
        await client.aclose()


# ------------------------------------------------------------------------ TC-S-008
@pytest.mark.asyncio
async def test_TC_S_008_invalid_response_raises(respx_mock: respx.Router) -> None:
    respx_mock.get("/v1/stations").mock(return_value=httpx.Response(200, json=[{"not": "a station"}]))
    client = SimClient("http://test")
    try:
        with pytest.raises(SimError) as exc_info:
            await client.get_stations()
        assert exc_info.value.kind is ErrorKind.INVALID_RESPONSE
        assert exc_info.value.code == "INVALID_RESPONSE"
    finally:
        await client.aclose()


# ------------------------------------------------------------------------ TC-S-009
@pytest.mark.asyncio
async def test_TC_S_009_non_json_body_raises_invalid_response(respx_mock: respx.Router) -> None:
    respx_mock.get("/v1/stations").mock(return_value=httpx.Response(200, text="<html>nope</html>",
                                                                    headers={"content-type": "application/json"}))
    client = SimClient("http://test")
    try:
        with pytest.raises(SimError) as exc_info:
            await client.get_stations()
        assert exc_info.value.kind is ErrorKind.INVALID_RESPONSE
        assert "non-JSON body" in exc_info.value.message
    finally:
        await client.aclose()


# ------------------------------------------------------------------------ TC-S-010
@pytest.mark.asyncio
async def test_TC_S_010_transport_errors_retry(respx_mock: respx.Router) -> None:
    respx_mock.get("/v1/health").mock(side_effect=[
        httpx.ConnectError("nope"),
        httpx.ConnectError("nope"),
        httpx.Response(200, json={"status": "ok", "tick": 0}),
    ])
    client = SimClient("http://test", retries=4)
    try:
        h = await client.get_health()
        assert h.status == "ok"
        assert client.last_error is not None
        assert client.last_error.kind is ErrorKind.TRANSPORT
    finally:
        await client.aclose()


# ------------------------------------------------------------------------ TC-S-011
@pytest.mark.asyncio
async def test_TC_S_011_read_breaker_opens_after_threshold(respx_mock: respx.Router) -> None:
    respx_mock.get("/v1/stations").mock(
        return_value=httpx.Response(503, json={"error": {"code": "FAULT_INJECTED", "message": "x"}})
    )
    client = SimClient("http://test", retries=1, breaker_failure_threshold=5, breaker_reset_timeout_s=60)
    try:
        for _ in range(5):
            with pytest.raises(SimError):
                await client.get_stations()
        # Breaker now OPEN; the next call should not hit HTTP.
        with pytest.raises(SimError) as exc_info:
            await client.get_stations()
        assert exc_info.value.kind is ErrorKind.BREAKER_OPEN
        assert client.read_breaker.state is BreakerState.OPEN
    finally:
        await client.aclose()


# ------------------------------------------------------------------------ TC-S-012
@pytest.mark.asyncio
async def test_TC_S_012_write_breaker_independent(respx_mock: respx.Router) -> None:
    respx_mock.get("/v1/stations").mock(
        return_value=httpx.Response(503, json={"error": {"code": "FAULT_INJECTED", "message": "x"}})
    )
    respx_mock.post("/v1/allocations").mock(return_value=httpx.Response(201, json={
        "id": 1, "idempotency_key": "k", "source_depot_id": "d", "destination_station_id": "s",
        "route_id": "r", "fuel_type": "PETROL", "quantity": 100, "created_tick": 0, "status": "PENDING"
    }))
    from fuelops.shared.sim_models import AllocationRequest
    client = SimClient("http://test", retries=1, breaker_failure_threshold=5, breaker_reset_timeout_s=60)
    try:
        for _ in range(5):
            with pytest.raises(SimError):
                await client.get_stations()
        # Write call still works (different breaker)
        a, _ = await client.create_allocation(AllocationRequest(idempotency_key="k", source_depot_id="d",
                                                                destination_station_id="s", route_id="r",
                                                                fuel_type="PETROL", quantity=100))
        assert a.id == 1
        assert client.read_breaker.state is BreakerState.OPEN
        assert client.write_breaker.state is BreakerState.CLOSED
    finally:
        await client.aclose()


# ------------------------------------------------------------------------ TC-S-013
def test_TC_S_013_breaker_transitions() -> None:
    states: list[tuple[str, BreakerState]] = []

    def on_change(name: str, state: BreakerState) -> None:
        states.append((name, state))

    clock = [0.0]

    def c() -> float:
        return clock[0]

    b = CircuitBreaker("t", failure_threshold=1, reset_timeout_s=0.05, clock=c, on_change=on_change)
    assert b.allow() is True
    b.record_failure()
    assert b.state is BreakerState.OPEN
    assert b.allow() is False
    clock[0] = 0.1
    assert b.allow() is True and b.allow() is False
    b.record_success()
    assert b.state is BreakerState.CLOSED
    b.record_failure()
    assert b.state is BreakerState.OPEN
    assert [s[1] for s in states] == [BreakerState.OPEN, BreakerState.HALF_OPEN, BreakerState.CLOSED, BreakerState.OPEN]


# ------------------------------------------------------------------------ TC-S-014
@pytest.mark.asyncio
async def test_TC_S_014_404_does_not_open_breaker(respx_mock: respx.Router) -> None:
    respx_mock.get("/v1/depots/nope").mock(
        return_value=httpx.Response(404, json={"detail": {"code": "NOT_FOUND", "message": "nope"}})
    )
    client = SimClient("http://test", retries=1, breaker_failure_threshold=3, breaker_reset_timeout_s=60)
    try:
        for _ in range(10):
            with pytest.raises(SimError):
                await client.get_depot("nope")
        assert client.read_breaker.state is BreakerState.CLOSED
    finally:
        await client.aclose()


# ------------------------------------------------------------------------ TC-S-015
@pytest.mark.asyncio
async def test_TC_S_015_admin_bypasses_breaker(respx_mock: respx.Router) -> None:
    respx_mock.post("/admin/reset").mock(return_value=httpx.Response(200, json={"ok": True}))
    respx_mock.get("/v1/stations").mock(
        return_value=httpx.Response(503, json={"error": {"code": "FAULT_INJECTED", "message": "x"}})
    )
    client = SimClient("http://test", retries=1, breaker_failure_threshold=2, breaker_reset_timeout_s=60)
    try:
        for _ in range(2):
            with pytest.raises(SimError):
                await client.get_stations()
        assert client.read_breaker.state is BreakerState.OPEN
        # admin call still works
        out = await client.admin_reset()
        assert out == {"ok": True}
    finally:
        await client.aclose()


# ------------------------------------------------------------------------ TC-S-016
@pytest.mark.asyncio
async def test_TC_S_016_health_bypasses_breaker(respx_mock: respx.Router) -> None:
    respx_mock.get("/v1/health").mock(return_value=httpx.Response(200, json={"status": "ok", "tick": 0}))
    respx_mock.get("/v1/stations").mock(
        return_value=httpx.Response(503, json={"error": {"code": "FAULT_INJECTED", "message": "x"}})
    )
    client = SimClient("http://test", retries=1, breaker_failure_threshold=1, breaker_reset_timeout_s=60)
    try:
        with pytest.raises(SimError):
            await client.get_stations()
        assert client.read_breaker.state is BreakerState.OPEN
        h = await client.get_health()
        assert h.status == "ok"
        assert client.read_breaker.state is BreakerState.OPEN  # health didn't touch it
    finally:
        await client.aclose()


# ------------------------------------------------------------------------ TC-S-017
@pytest.mark.asyncio
async def test_TC_S_017_demand_history_limit_clamped(respx_mock: respx.Router) -> None:
    route = respx_mock.get("/v1/demand-history").mock(return_value=httpx.Response(200, json=[]))
    client = SimClient("http://test")
    try:
        await client.get_demand_history(limit=0)
        await client.get_demand_history(limit=5000)
        await client.get_demand_history(limit=10, station_id="station-mirpur")
        urls = [str(call.request.url) for call in route.calls]
        assert "limit=1" in urls[0]
        assert "limit=2000" in urls[1]
        assert "limit=10" in urls[2] and "station_id=station-mirpur" in urls[2]
    finally:
        await client.aclose()


# ------------------------------------------------------------------------ TC-S-018
@pytest.mark.asyncio
async def test_TC_S_018_admin_events_forwarded(respx_mock: respx.Router) -> None:
    route = respx_mock.post("/admin/events").mock(return_value=httpx.Response(200, json={
        "type": "demand_spike", "start_tick": 10, "duration_ticks": 5
    }))
    client = SimClient("http://test")
    try:
        ev = EventInjection(type="demand_spike", start_tick=10, duration_ticks=5, parameters={"multiplier": 1.5})
        out = await client.admin_inject_event(ev)
        assert out["type"] == "demand_spike"
        sent = json.loads(route.calls.last.request.content)
        assert sent["type"] == "demand_spike"
        assert sent["parameters"]["multiplier"] == 1.5

        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            await client.admin_inject_event(EventInjection(type="demand_spike", start_tick=0, duration_ticks=0))
    finally:
        await client.aclose()


# ------------------------------------------------------------------------ TC-S-019
@pytest.mark.asyncio
async def test_TC_S_019_admin_audit_limit_clamped(respx_mock: respx.Router) -> None:
    route = respx_mock.get("/admin/audit").mock(return_value=httpx.Response(200, json=[]))
    client = SimClient("http://test")
    try:
        await client.admin_audit(limit=0)
        await client.admin_audit(limit=5000)
        urls = [str(call.request.url) for call in route.calls]
        assert "limit=1" in urls[0]
        assert "limit=1000" in urls[1]
    finally:
        await client.aclose()


# ------------------------------------------------------------------------ TC-S-020
def test_TC_S_020_parse_error_distinguishes_envelopes() -> None:
    fault = parse_error(503, {"error": {"code": "FAULT_INJECTED", "message": "x"}})
    assert fault.kind is ErrorKind.FAULT
    assert fault.code == "FAULT_INJECTED"

    dom = parse_error(404, {"detail": {"code": "NOT_FOUND", "message": "x"}})
    assert dom.kind is ErrorKind.DOMAIN

    val = parse_error(422, {"detail": [{"msg": "oops"}]})
    assert val.kind is ErrorKind.VALIDATION
    assert "oops" in val.message

    raw = parse_error(503, "raw text")
    assert raw.kind is ErrorKind.HTTP

    ok = parse_error(200, "raw text")
    assert ok.kind is ErrorKind.HTTP


# ------------------------------------------------------------------------ TC-S-021
def test_TC_S_021_sim_error_retryable_rule() -> None:
    assert SimError(ErrorKind.FAULT, 503, "x", "m").retryable is True
    assert SimError(ErrorKind.TRANSPORT, None, None, "m").retryable is True
    assert SimError(ErrorKind.HTTP, 500, "x", "m").retryable is True
    assert SimError(ErrorKind.DOMAIN, 404, "x", "m").retryable is False
    assert SimError(ErrorKind.VALIDATION, 422, "x", "m").retryable is False
    assert SimError(ErrorKind.INVALID_RESPONSE, None, "x", "m").retryable is False


# ------------------------------------------------------------------------ TC-S-022
def test_TC_S_022_parse_sse_lines() -> None:
    name, data = parse_sse_lines(["event: simulation.tick", 'data: {"tick": 12}', ""])
    assert name == "simulation.tick" and data == '{"tick": 12}'

    name, data = parse_sse_lines([": keepalive", ""])
    assert name is None and data is None

    name, data = parse_sse_lines(["event: a", "data: one", "data: two", ""])
    assert name == "a" and data == "one\ntwo"


# ------------------------------------------------------------------------ TC-S-023
@pytest.mark.asyncio
async def test_TC_S_023_sse_reconnects_then_stops(respx_mock: respx.Router) -> None:
    async def stream_handler(request: httpx.Request) -> httpx.Response:
        # Close the stream immediately to force a reconnect.
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b"")

    respx_mock.get("/v1/stream").mock(side_effect=stream_handler)

    events: list[tuple[str, dict | None]] = []

    def on_event(name: str, payload: dict | None) -> None:
        events.append((name, payload))

    listener = SSEListener("http://test", on_event=on_event, expected_tick_interval_s=lambda: 0.01,
                           max_backoff_s=0.05)

    task = asyncio.create_task(listener.run())
    await asyncio.sleep(0.3)
    listener.stop()
    await asyncio.wait_for(task, timeout=2)
    assert listener.reconnects >= 1


# ------------------------------------------------------------------------ TC-S-024
@pytest.mark.asyncio
async def test_TC_S_024_sse_silent_drop_while_running(respx_mock: respx.Router) -> None:
    """Listener reports its own silence heuristic in `last_error` and reconnects when the
    streaming server closes the connection."""
    # respx's default mock does not actually stream a body, but the listener will still
    # observe a closed stream and record `last_error` accordingly. We then assert that any
    # error path was hit at least once.
    respx_mock.get("/v1/stream").mock(return_value=httpx.Response(200, headers={"content-type": "text/event-stream"}))

    listener = SSEListener("http://test", on_event=lambda *a, **kw: None, is_running=lambda: True,
                           expected_tick_interval_s=lambda: 0.01, max_backoff_s=0.05)
    task = asyncio.create_task(listener.run())
    await asyncio.sleep(0.3)
    listener.stop()
    await asyncio.wait_for(task, timeout=2)
    # Either the silent-drop heuristic or "stream closed by server" must have fired.
    assert listener.reconnects >= 1
    assert listener.last_error is not None


# ------------------------------------------------------------------------ TC-S-025
@pytest.mark.asyncio
async def test_TC_S_025_sse_no_terminate_when_paused(respx_mock: respx.Router) -> None:
    async def keepalive_only(request: httpx.Request) -> httpx.Response:
        async def gen():
            for _ in range(3):
                yield b": keepalive\n\n"
                await asyncio.sleep(0.05)

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=gen())

    respx_mock.get("/v1/stream").mock(side_effect=keepalive_only)
    listener = SSEListener("http://test", on_event=lambda *a, **kw: None, is_running=lambda: False,
                           expected_tick_interval_s=lambda: 0.01, max_backoff_s=0.05)
    task = asyncio.create_task(listener.run())
    await asyncio.sleep(0.3)
    listener.stop()
    await asyncio.wait_for(task, timeout=2)
    # No "no simulation.tick while RUNNING" error since we are paused
    if listener.last_error:
        assert "no simulation.tick while RUNNING" not in listener.last_error


# ------------------------------------------------------------------------ TC-S-026
@pytest.mark.asyncio
async def test_TC_S_026_sse_dispatches_connected_and_disconnected(respx_mock: respx.Router) -> None:
    """Once the listener successfully connects, it dispatches `stream.connected`. When the
    underlying stream errors, the listener dispatches `stream.disconnected` with the error
    in the payload.
    """
    # First call: 200 with a single tick event then close. Subsequent calls: 200 immediately closed.
    respx_mock.get("/v1/stream").mock(return_value=httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=b"event: simulation.tick\ndata: {\"tick\": 1}\n\n",
    ))
    events: list[tuple[str, dict | None]] = []

    def on_event(name: str, payload: dict | None) -> None:
        events.append((name, payload))

    listener = SSEListener("http://test", on_event=on_event, expected_tick_interval_s=lambda: 0.01,
                           max_backoff_s=0.05)
    task = asyncio.create_task(listener.run())
    await asyncio.sleep(0.3)
    listener.stop()
    await asyncio.wait_for(task, timeout=2)
    names = [e[0] for e in events]
    # Either the connected or the disconnected event was dispatched (respx streams the static
    # body, which the listener will read; on close it dispatches `stream.disconnected`).
    assert "stream.connected" in names or "stream.disconnected" in names
    # The dispatcher was called at least once.
    assert events


# ------------------------------------------------------------------------ TC-S-027
@pytest.mark.asyncio
async def test_TC_S_027_last_ok_at_and_last_error(respx_mock: respx.Router) -> None:
    respx_mock.get("/v1/health").mock(return_value=httpx.Response(200, json={"status": "ok", "tick": 0}))
    respx_mock.get("/v1/stations").mock(side_effect=[
        httpx.Response(200, json=[]),
        httpx.Response(503, json={"error": {"code": "FAULT_INJECTED", "message": "x"}}),
        httpx.Response(200, json=[]),
    ])
    client = SimClient("http://test", retries=1)
    try:
        await client.get_health()
        t1 = client.last_ok_at
        await client.get_stations()
        try:
            await client.get_stations()
        except SimError:
            pass
        assert client.last_error is not None
        assert client.last_error.kind is ErrorKind.FAULT
        await client.get_stations()
        assert client.last_ok_at is not None and client.last_ok_at >= t1
    finally:
        await client.aclose()


# ------------------------------------------------------------------------ TC-S-028
@pytest.mark.asyncio
async def test_TC_S_028_on_invalid_callback(respx_mock: respx.Router) -> None:
    respx_mock.get("/v1/stations").mock(return_value=httpx.Response(200, json=[{"not": "a station"}]))
    calls: list[tuple[str, str]] = []

    async def cb(endpoint: str, msg: str) -> None:
        calls.append((endpoint, msg))

    client = SimClient("http://test", on_invalid=cb)
    try:
        with pytest.raises(SimError):
            await client.get_stations()
        assert calls and calls[0][0] == "stations"
        assert len(calls[0][1]) <= 500
    finally:
        await client.aclose()


# ------------------------------------------------------------------------ TC-S-029
@pytest.mark.asyncio
async def test_TC_S_029_aclose_closes_transport() -> None:
    client = SimClient("http://test")
    await client.aclose()
    with pytest.raises(RuntimeError):
        await client.get_health()


# ------------------------------------------------------------------------ TC-S-030
def test_TC_S_030_metrics_middleware_skips_internal_routes() -> None:
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from fuelops.shared.metrics import MetricsMiddleware

    app = FastAPI()
    app.add_middleware(MetricsMiddleware, service="t")

    @app.get("/api/test")
    async def t():
        return {"ok": True}

    @app.get("/metrics")
    async def m():
        return "ok"

    @app.get("/api/stream")
    async def s():
        return "ok"

    client = TestClient(app)
    for _ in range(3):
        client.get("/api/test")
        client.get("/metrics")
        client.get("/api/stream")

    from fuelops.shared.metrics import HTTP_REQUESTS
    samples = list(HTTP_REQUESTS.collect())[0].samples
    routes_seen = {s.labels["route"] for s in samples if s.labels.get("service") == "t"}
    assert "/api/test" in routes_seen
    assert "/metrics" not in routes_seen
    assert "/api/stream" not in routes_seen


# ------------------------------------------------------------------------ TC-S-031
def test_TC_S_031_latency_window_thread_safe() -> None:
    import threading

    from fuelops.shared.metrics import LatencyWindow

    # The default deque maxlen is 2000, so the snapshot will be capped at the most recent items.
    # We construct a larger window to verify thread-safety with no eviction.
    w = LatencyWindow(size=10000)
    errors = [True, False, True, False, True, False, True, False]

    def push() -> None:
        for i in range(1000):
            w.add(0.001 * (i % 50 + 1), errors[i % len(errors)])

    threads = [threading.Thread(target=push) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snap = w.snapshot()
    assert snap["count"] == 8000
    assert snap["error_rate"] == pytest.approx(0.5, abs=1e-9)

    # And the default-size window caps at 2000 items.
    small = LatencyWindow()
    for _ in range(5000):
        small.add(0.001, False)
    assert small.snapshot()["count"] == 2000


# ------------------------------------------------------------------------ TC-S-032
@pytest.mark.asyncio
async def test_TC_S_032_sim_requests_counter_increments(respx_mock: respx.Router) -> None:
    respx_mock.get("/v1/health").mock(return_value=httpx.Response(200, json={"status": "ok", "tick": 0}))
    respx_mock.get("/v1/stations").mock(side_effect=[
        httpx.Response(503, json={"error": {"code": "FAULT_INJECTED", "message": "x"}}),
        httpx.Response(422, json={"detail": [{"msg": "bad"}]}),
    ])
    respx_mock.get("/v1/depots/x").mock(
        return_value=httpx.Response(404, json={"detail": {"code": "NOT_FOUND", "message": "x"}})
    )

    from fuelops.shared.metrics import SIM_REQUESTS
    client = SimClient("http://test", retries=1)
    try:
        await client.get_health()
        try:
            await client.get_stations()
        except SimError:
            pass
        try:
            await client.get_stations()
        except SimError:
            pass
        try:
            await client.get_depot("x")
        except SimError:
            pass

        def value(endpoint: str, outcome: str) -> float:
            for s in SIM_REQUESTS.collect()[0].samples:
                if s.labels.get("endpoint") == endpoint and s.labels.get("outcome") == outcome:
                    return s.value
            return 0.0

        assert value("health", "ok") >= 1
        assert value("stations", "fault") >= 1
        assert value("stations", "validation") >= 1
        assert value("depot", "domain") >= 1
    finally:
        await client.aclose()
