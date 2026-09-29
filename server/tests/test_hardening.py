"""Code-review hardening: approval/cycle races, what-if concurrency limit, chaos status gating, security headers,
failed-auth accounting and chaos audit."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from prometheus_client import REGISTRY

from fuelops.backend.decision.orchestrator import REVIEWABLE, DecisionError
from fuelops.backend.integration.breaker import BreakerState, CircuitBreaker
from fuelops.backend.integration.sim_client import SimClient
from fuelops.backend.integration.sim_errors import SimError
from fuelops.backend.main import create_app
from tests.stack import TOKEN

AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _drain(stack, station_id: str, level: float = 1500.0) -> None:
    st = stack.world.station(station_id)
    for fuel in st["inventory"]:
        st["inventory"][fuel] = min(st["inventory"][fuel], level)


async def _manual_plan_with_lines(stack_factory, minimum: int = 2):
    s = await stack_factory(operating_mode="manual")
    for sid in ("station-mirpur", "station-tongi", "station-karnaphuli"):
        _drain(s, sid)
    await s.step(3)
    orch = s.container.orchestrator
    plan = next(p for p in reversed(orch.plans.values())
                if sum(ln.status in REVIEWABLE for ln in p.lines) >= minimum)
    return s, orch, plan


def _client(stack) -> httpx.AsyncClient:
    app = create_app(stack.container.settings, stack.container, background=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend")


# ------------------------------------------------------------------ approval races


async def test_approve_skips_lines_that_expire_while_earlier_lines_are_submitted(stack_factory, monkeypatch):
    s, orch, plan = await _manual_plan_with_lines(stack_factory)
    first, *rest = [ln for ln in plan.lines if ln.status in REVIEWABLE]
    real_submit = s.container.actuator.submit_line

    async def submit_then_expire_the_rest(p, line):
        result = await real_submit(p, line)
        for other in rest:  # what _expire() / a newer plan does while this submission is in flight
            other.status = "EXPIRED"
        return result

    monkeypatch.setattr(s.container.actuator, "submit_line", submit_then_expire_the_rest)
    await orch.approve(plan.id, None, "operator", None)

    assert first.submissions
    assert all(ln.status == "EXPIRED" and not ln.submissions for ln in rest)
    skipped = {d.line_no for d in orch.decisions if d.plan_id == plan.id and d.action == "skip"}
    assert skipped == {ln.line_no for ln in rest}


async def test_no_new_plan_is_created_while_an_approval_is_being_submitted(stack_factory, monkeypatch):
    s, orch, plan = await _manual_plan_with_lines(stack_factory, minimum=1)
    real_submit = s.container.actuator.submit_line
    concurrent_cycles = []

    async def submit_while_a_tick_arrives(p, line):
        concurrent_cycles.append(await orch.run_cycle(s.container.cache.state, trigger="tick"))
        return await real_submit(p, line)

    monkeypatch.setattr(s.container.actuator, "submit_line", submit_while_a_tick_arrives)
    await orch.approve(plan.id, None, "operator", None)

    assert concurrent_cycles and all(result is None for result in concurrent_cycles)


async def test_modify_refuses_a_line_that_expired_during_the_what_if(stack_factory, monkeypatch):
    s, orch, plan = await _manual_plan_with_lines(stack_factory, minimum=1)
    line = next(ln for ln in plan.lines if ln.status in REVIEWABLE)
    real_evaluate = orch.evaluate

    async def evaluate_then_expire(lines):
        result = await real_evaluate(lines)
        line.status = "EXPIRED"
        return result

    monkeypatch.setattr(orch, "evaluate", evaluate_then_expire)
    with pytest.raises(DecisionError) as err:
        await orch.modify(plan.id, line.line_no, 1000.0, None, "operator", None)

    assert err.value.status == 409
    assert line.status == "EXPIRED"


# ------------------------------------------------------------------ API hardening


async def test_evaluate_is_rejected_with_429_when_all_what_if_slots_are_busy(stack_factory):
    s = await stack_factory(evaluate_max_concurrency=1)
    route = s.container.cache.state.routes[0]
    body = {"lines": [{"route_id": route.id, "fuel_type": "DIESEL", "quantity": 1000}]}
    async with _client(s) as client:
        assert (await client.post("/api/evaluate", json=body)).status_code == 200
        async with s.container.evaluate_slots:
            busy = await client.post("/api/evaluate", json=body)
    assert busy.status_code == 429
    assert busy.headers["retry-after"] == "1"


async def test_chaos_status_needs_the_operator_token(stack):
    async with _client(stack) as client:
        anonymous = await client.get("/api/chaos/status")
        operator = await client.get("/api/chaos/status", headers=AUTH)
    assert anonymous.status_code == 401
    assert operator.status_code == 200
    assert {"database_down", "faults", "events"} <= set(operator.json())


async def test_chaos_status_is_forbidden_when_chaos_is_disabled(stack_factory):
    s = await stack_factory(chaos_enabled=False)
    async with _client(s) as client:
        r = await client.get("/api/chaos/status", headers=AUTH)
    assert r.status_code == 403


async def test_chaos_database_toggle_is_audited(stack):
    async with _client(stack) as client:
        await client.post("/api/chaos/app/database", json={"down": True}, headers=AUTH)
        await client.post("/api/chaos/app/database", json={"down": False}, headers=AUTH)
        audit = (await client.get("/api/audit", headers=AUTH)).json()
    toggles = [e["detail"]["down"] for e in audit if e["action"] == "chaos.database"]
    assert toggles == [False, True]  # newest first


@pytest.mark.parametrize("path", ["/api/health/live", "/login.html"])
async def test_security_headers_on_api_and_static_responses(stack, path):
    async with _client(stack) as client:
        r = await client.get(path)
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize("headers,reason", [
    ({}, "missing"),
    ({"Authorization": "Bearer wrong"}, "invalid"),
])
async def test_rejected_operator_requests_are_counted(stack, headers, reason):
    def rejected() -> float:
        return REGISTRY.get_sample_value("fuelops_auth_rejected_total", {"reason": reason}) or 0.0

    before = rejected()
    async with _client(stack) as client:
        r = await client.get("/api/auth/verify", headers=headers)
    assert r.status_code == 401
    assert rejected() == before + 1


# ------------------------------------------------------------------ circuit breaker recovery (found by the live drill)

INSTANCE = {"id": 1, "scenario_id": "baseline", "scenario_version": "1.0", "seed": 12345,
            "sim_time": "2026-01-01T00:15:00", "tick": 1, "tick_minutes": 15, "status": "PAUSED"}
FAULT = {"error": {"code": "FAULT_INJECTED", "message": "injected unavailable"}}


async def _no_sleep(_s: float) -> None:
    return None


async def test_client_recovers_after_a_failed_half_open_probe_once_the_simulator_is_back():
    """Live drill: a 503 fault ended while the read breaker was HALF_OPEN and the backend never synced again."""
    sim = {"down": True}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json=FAULT) if sim["down"] else httpx.Response(200, json=INSTANCE)

    client = SimClient("http://sim", transport=httpx.MockTransport(handler), sleep=_no_sleep, retries=3,
                       breaker_failure_threshold=1, breaker_reset_timeout_s=0.0)
    with pytest.raises(SimError):
        await client.get_instance()          # retries exhausted -> OPEN
    with pytest.raises(SimError):
        await client.get_instance()          # HALF_OPEN probe fails while the fault is still active
    sim["down"] = False

    instance = await client.get_instance()   # the simulator is back: this must reach it

    assert instance.tick == 1
    assert client.read_breaker.state is BreakerState.CLOSED
    await client.aclose()


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_a_probe_that_never_reports_back_does_not_wedge_the_breaker():
    clock = _Clock()
    breaker = CircuitBreaker("t", failure_threshold=1, reset_timeout_s=10, clock=clock)
    breaker.record_failure()
    clock.t = 11
    assert breaker.allow()                   # the one HALF_OPEN probe...
    assert not breaker.allow()               # ...is exclusive while it is fresh
    clock.t = 22                             # ...but a probe lost for a whole cooldown is abandoned
    assert breaker.allow()


async def test_concurrent_approvals_of_the_same_plan_submit_each_line_once(stack_factory):
    s, orch, plan = await _manual_plan_with_lines(stack_factory, minimum=1)
    results = await asyncio.gather(orch.approve(plan.id, None, "a", None), orch.approve(plan.id, None, "b", None),
                                   return_exceptions=True)
    assert sum(isinstance(r, DecisionError) for r in results) == 1
    assert all(len(ln.submissions) == len(ln.parts or [ln.quantity]) for ln in plan.lines if ln.submissions)
