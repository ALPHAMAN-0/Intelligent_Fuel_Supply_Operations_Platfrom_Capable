"""End-to-end scenarios on the in-process stack (backend + intelligence + simulator test double).

These exercise the whole loop: sync -> forecast -> plan -> mode gate -> preflight -> POST -> track -> alerts.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.req("R-LOOP-01", "R-CAP-01", "R-PLAT-01")


def _service_level(stack) -> float:
    return stack.world.metrics()["service_level"]


def _drain(stack, station_id: str, level: float = 1500.0) -> None:
    """Start a station low so the planner has to act within the first few ticks."""
    st = stack.world.station(station_id)
    for fuel in st["inventory"]:
        st["inventory"][fuel] = min(st["inventory"][fuel], level)


async def test_walking_skeleton_moves_fuel_automatically(stack):
    """In Auto mode the planner ships fuel every day and service level stays >= 99% over one simulated day."""
    await stack.step(96)
    c = stack.container
    allocs = stack.world.allocations
    assert len(allocs) > 10
    assert all(a["idempotency_key"].startswith("fo-") for a in allocs)
    assert _service_level(stack) >= 0.99
    assert c.orchestrator.last_policy_used == "planner" and not c.orchestrator.fallback_active
    assert c.cache.state.instance.tick == 96


@pytest.mark.req("R-INT-01", "R-INT-X1")
async def test_planner_beats_no_action_baseline(stack_factory):
    """Over two simulated days the planner's service level is far above doing nothing."""
    planned = await stack_factory()
    await planned.step(192)
    idle = await stack_factory(decisions_enabled=False)
    await idle.step(192)
    assert _service_level(planned) > 0.99
    assert _service_level(idle) < 0.9
    assert _service_level(planned) > _service_level(idle) + 0.1


@pytest.mark.req("R-RES-01", "R-INT-X4", "R-CAP-06")
async def test_intelligence_outage_falls_back_and_recovers(stack):
    """Stopping the intelligence service switches to the heuristic, raises a system alert, and recovers."""
    await stack.step(4)
    stack.intel.down = True
    await stack.step(8)
    c = stack.container
    assert c.orchestrator.fallback_active and c.orchestrator.last_policy_used == "heuristic"
    assert c.alerts.alerts["system:intelligence"].status == "open"
    assert c.health()["status"] in ("degraded", "ok")
    assert {x["name"]: x["status"] for x in c.health()["components"]}["decision_engine"] == "degraded"
    stack.intel.down = False
    c.intel.breaker._opened_at -= 60  # skip the cool-down
    await stack.step(4)
    assert not c.orchestrator.fallback_active and c.orchestrator.last_policy_used == "planner"
    assert c.alerts.alerts["system:intelligence"].status == "resolved"
    assert _service_level(stack) >= 0.99


@pytest.mark.req("R-RES-04", "R-CAP-06")
async def test_simulator_unavailable_keeps_last_good_state(stack):
    """An 'unavailable' fault leaves the backend on cached state; it resyncs when the fault clears."""
    await stack.step(3)
    before = stack.container.cache.state
    await stack.admin.post("/admin/faults", json={"type": "unavailable", "duration_seconds": 60})
    await stack.admin.post("/admin/step")
    result = await stack.container.sync.sync_once()
    assert result is None and not stack.container.cache.last_sync_ok
    assert stack.container.cache.state is before
    health = {x["name"]: x["status"] for x in stack.container.health()["components"]}
    assert health["simulator"] == "unknown" or health["simulator_api"] in ("down", "ok", "degraded")
    await stack.admin.post("/admin/faults/clear")
    stack.container.client.read_breaker._opened_at -= 60
    await stack.container.sync.sync_once()
    assert stack.container.cache.state.instance.tick == 4 and stack.container.cache.last_sync_ok


@pytest.mark.req("R-RES-03")
async def test_stale_data_blocks_auto_approval(stack):
    """With a stale_data fault active, plans are still proposed but nothing is auto-approved."""
    _drain(stack, "station-mirpur")
    _drain(stack, "station-tongi")
    await stack.step(2)
    stack.container.orchestrator.mode = "auto"
    await stack.admin.post("/admin/faults", json={"type": "stale_data", "duration_seconds": 60})
    await stack.step(6)
    c = stack.container
    assert c.cache.state.stale
    recent = [p for p in c.orchestrator.plans.values() if p.tick > 2 and p.lines]
    assert recent and all(ln.status in ("PROPOSED", "EXPIRED") for p in recent for ln in p.lines)
    assert all(ln.needs_review_reason == "simulator data is stale" for p in recent for ln in p.lines)
    assert c.alerts.alerts["system:stale_data"].status == "open"


async def test_error_rate_fault_is_absorbed_by_retries(stack):
    """A 50% error_rate fault is absorbed by retries: syncing continues and fuel keeps moving."""
    await stack.admin.post("/admin/faults", json={"type": "error_rate", "duration_seconds": 120,
                                                  "parameters": {"rate": 0.5}})
    await stack.step(48)
    assert stack.container.cache.state.instance.tick >= 44
    assert stack.container.client.read_breaker.state.value == "CLOSED"
    assert _service_level(stack) >= 0.99


@pytest.mark.req("R-DS-02", "R-RES-03")
async def test_manual_mode_waits_for_operator_then_executes(stack_factory):
    """In Manual mode lines wait for approval; approving submits them and they reach the simulator."""
    s = await stack_factory(operating_mode="manual")
    await s.step(30)
    c = s.container
    assert not s.world.allocations, "nothing may be shipped without approval"
    plan = next(p for p in reversed(c.orchestrator.plans.values()) if p.lines and p.status == "PROPOSED")
    approved = await c.orchestrator.approve(plan.id, None, "operator", "looks right")
    assert all(ln.status in ("PENDING", "PARTIAL", "BLOCKED", "SIM_REJECTED") for ln in approved.lines)
    assert any(ln.status == "PENDING" for ln in approved.lines)
    assert s.world.allocations
    actions = [d.action for d in c.orchestrator.decisions if d.plan_id == plan.id]
    assert "approve" in actions and "submit" in actions
    await s.step(6)
    assert any(ln.status in ("IN_TRANSIT", "ARRIVED") for ln in approved.lines)


async def test_plans_expire_after_ttl(stack_factory):
    """Unapproved plans expire (superseded by the next cycle or at TTL) and the expiry is audited."""
    s = await stack_factory(operating_mode="manual")
    await s.step(30)
    plans = [p for p in s.container.orchestrator.plans.values() if p.lines]
    assert all(p.status == "EXPIRED" for p in plans[:-1])
    assert any(d.action == "expire" for d in s.container.orchestrator.decisions)


@pytest.mark.req("R-RES-03")
async def test_assisted_mode_sends_low_confidence_lines_to_review(stack_factory):
    """In Assisted mode, lines below the confidence threshold wait for a human."""
    s = await stack_factory(operating_mode="assisted", auto_approve_min_confidence=0.99)
    _drain(s, "station-mirpur")
    await s.step(20)
    lines = [ln for p in s.container.orchestrator.plans.values() for ln in p.lines]
    assert lines and all(ln.approved_by is None for ln in lines)
    assert all("confidence" in (ln.needs_review_reason or "") for ln in lines)


async def test_reject_and_modify_are_audited(stack_factory):
    """Operators can reject a line and modify another (with what-if); both land in the decision history."""
    s = await stack_factory(operating_mode="manual")
    await s.step(30)
    c = s.container
    plan = next(p for p in reversed(c.orchestrator.plans.values()) if len(p.lines) >= 1 and p.status == "PROPOSED")
    line = plan.lines[0]
    updated, evaluation = await c.orchestrator.modify(plan.id, line.line_no, 1500, None, "operator", "smaller")
    assert updated.lines[0].status == "MODIFIED" and updated.lines[0].quantity == 1500
    assert evaluation.unmet_after <= evaluation.unmet_before + 1e-6
    await c.orchestrator.reject(plan.id, [line.line_no], "operator", "changed my mind")
    actions = [d.action for d in c.orchestrator.decisions if d.plan_id == plan.id]
    assert "modify" in actions and "reject" in actions


async def test_cancel_pending_allocation_refunds_depot(stack_factory):
    """Cancelling a PENDING allocation refunds the depot and updates the plan line."""
    s = await stack_factory(operating_mode="manual")
    await s.step(30)
    c = s.container
    plan = next(p for p in reversed(c.orchestrator.plans.values()) if p.lines and p.status == "PROPOSED")
    await c.orchestrator.approve(plan.id, [plan.lines[0].line_no], "operator", None)
    sub = plan.lines[0].submissions[0]
    depot = s.world.depot(plan.lines[0].depot_id)
    before = depot["inventory"][plan.lines[0].fuel_type]
    result = await c.orchestrator.cancel_allocation(sub.allocation_id, "operator")
    assert result["status"] == "CANCELLED"
    assert depot["inventory"][plan.lines[0].fuel_type] == pytest.approx(before + sub.quantity)


@pytest.mark.req("R-CAP-04", "R-ENV-01", "R-CRISIS-01")
async def test_demand_spike_is_detected_and_planned_for(stack):
    """A scheduled Dhaka demand spike raises an event alert before it starts and service level holds."""
    await stack.step(4)
    await stack.admin.post("/admin/events", json={"type": "demand_spike", "start_tick": 10, "duration_ticks": 30,
                                                  "parameters": {"region_ids": ["region-dhaka"], "multiplier": 1.8}})
    await stack.step(1)
    alerts = stack.container.alerts.alerts
    assert alerts["event:1"].severity == "warning" and "Scheduled" in alerts["event:1"].title
    await stack.step(40)
    assert _service_level(stack) >= 0.98
    assert alerts["event:1"].status == "resolved"


@pytest.mark.req("R-CAP-04", "R-CRISIS-01")
async def test_route_disruption_reroutes_through_patiya(stack):
    """With route-gazipur-mirpur disrupted, Mirpur is served via route-patiya-mirpur."""
    await stack.admin.post("/admin/events", json={"type": "route_disruption", "start_tick": 1, "duration_ticks": 60,
                                                  "parameters": {"route_ids": ["route-gazipur-mirpur"]}})
    _drain(stack, "station-mirpur", 3000)
    await stack.step(60)
    routes = {a["route_id"] for a in stack.world.allocations if a["destination_station_id"] == "station-mirpur"}
    assert "route-patiya-mirpur" in routes
    assert not [a for a in stack.world.allocations if a["route_id"] == "route-gazipur-mirpur"
                and a["created_tick"] >= 1 and a["status"] != "CANCELLED"]
    assert stack.world.metrics()["allocation_failures"] == 0
    assert _service_level(stack) >= 0.98


@pytest.mark.req("R-CRISIS-01")
async def test_combined_crisis_keeps_service_and_raises_incidents(stack):
    """Demand spike + route disruption + shipment delay together: incidents open and service stays high."""
    for ev in (
        {"type": "demand_spike", "start_tick": 5, "duration_ticks": 40,
         "parameters": {"region_ids": ["region-dhaka"], "multiplier": 1.6}},
        {"type": "route_disruption", "start_tick": 5, "duration_ticks": 40,
         "parameters": {"route_ids": ["route-gazipur-mirpur"]}},
        {"type": "shipment_delay", "start_tick": 5, "duration_ticks": 1,
         "parameters": {"depot_ids": ["depot-gazipur"], "delay_ticks": 6}},
    ):
        await stack.admin.post("/admin/events", json=ev)
    await stack.step(60)
    incidents = stack.container.alerts.incidents
    assert len(incidents) >= 3
    assert _service_level(stack) >= 0.95


async def test_station_outage_gets_no_shipments_and_alerts(stack):
    """During a station outage nothing is shipped there and a critical alert is open."""
    await stack.admin.post("/admin/events", json={"type": "station_outage", "start_tick": 1, "duration_ticks": 20,
                                                  "parameters": {"station_ids": ["station-coxsbazar"]}})
    await stack.step(10)
    assert not [a for a in stack.world.allocations if a["destination_station_id"] == "station-coxsbazar"
                and 1 <= a["created_tick"] < 20]
    assert stack.container.alerts.alerts["station:station-coxsbazar"].severity == "critical"


async def test_simulator_reset_starts_new_run(stack):
    """After /admin/reset the backend detects the tick regression and opens a new run epoch."""
    await stack.step(10)
    run = stack.container.cache.run_id
    await stack.admin.post("/admin/reset")
    await stack.container.sync.sync_once()
    assert stack.container.cache.run_id != run and stack.container.cache.state.instance.tick == 0
    await stack.step(5)
    assert stack.container.cache.state.run_id == stack.container.cache.run_id


@pytest.mark.req("R-OBS-03", "R-INT-P1")
async def test_forecast_accuracy_is_tracked_near_noise_floor(stack):
    """After a day, rolling forecast MAPE is reported per horizon and sits near the jitter noise floor."""
    await stack.step(120)
    summary = stack.container.cache.tracker.summary()
    assert summary["1"]["samples"] > 100
    assert summary["1"]["mape"] < 0.2
    noise_floor = stack.container.cache.cycle.model.noise_floor
    assert 0.03 < noise_floor < 0.15


async def test_database_outage_degrades_and_recovers(stack):
    """With the database down the loop keeps running from memory; health shows it; recovery clears the alert."""
    stack.container.store.force_fault(True)
    await stack.step(4)
    c = stack.container
    assert {x["name"]: x["status"] for x in c.health()["components"]}["database"] == "down"
    assert c.alerts.alerts["system:database"].status == "open"
    assert c.cache.state.instance.tick == 4
    stack.container.store.force_fault(False)
    await c.store.ping()
    await stack.step(1)
    assert c.alerts.alerts["system:database"].status == "resolved"


@pytest.mark.req("R-INT-X4")
async def test_policy_switch_to_heuristic_is_policy_rollback(stack):
    """Operators can roll the decision policy back to the heuristic at runtime."""
    await stack.container.orchestrator.set_policy("heuristic", "operator")
    await stack.step(6)
    assert stack.container.orchestrator.last_policy_used == "heuristic"
    assert "policy set to heuristic" in (stack.container.orchestrator.fallback_reason or "")
