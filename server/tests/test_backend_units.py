"""Backend units: preflight, detectors, alert lifecycle, store, explanations, heuristic fallback."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from fuelops.backend.alerts import detectors as det
from fuelops.backend.alerts.manager import AlertManager
from fuelops.backend.decision.actuator import idempotency_key
from fuelops.backend.decision.heuristic import heuristic_cycle, heuristic_plan
from fuelops.backend.decision.preflight import preflight
from fuelops.backend.domain import Decision, PlanLineRecord, PlanRecord
from fuelops.backend.explain import templates
from fuelops.backend.explain.llm import Explainer
from fuelops.backend.state.store import Store
from fuelops.shared.config import Settings
from fuelops.shared.models import PlannerParams, PlanResult, RiskSnapshot
from fuelops.shared.risk import score_risks
from fuelops.shared.world import prior_forecast
from tests.factories import add_allocation, add_event, depot, make_world, route, station

# --------------------------------------------------------------- preflight


def _pf(w, qty, fuel="DIESEL", dispatched=0.0, route_id="route-gazipur-mirpur"):
    r = route(w, route_id)
    return preflight(depot(w, r.source_depot_id), station(w, r.destination_station_id), r, fuel, qty, dispatched)


@pytest.mark.req("R-SEC-03")
def test_preflight_passes_valid_allocation():
    """A valid shipment passes and reports the largest quantity that would fit."""
    w = make_world()
    res = _pf(w, 3000)
    assert res.ok and res.max_quantity == 6000  # station headroom 15000 - 9000


@pytest.mark.req("R-SEC-03")
@pytest.mark.parametrize("mutate,qty,code", [
    (lambda w: setattr(depot(w, "depot-gazipur"), "status", "CLOSED"), 1000, "DEPOT_CLOSED"),
    (lambda w: setattr(station(w, "station-mirpur"), "status", "OUTAGE"), 1000, "STATION_CLOSED"),
    (lambda w: setattr(route(w, "route-gazipur-mirpur"), "status", "DISRUPTED"), 1000, "ROUTE_DISRUPTED"),
    (lambda w: None, 7001, "ROUTE_CAPACITY_EXCEEDED"),
    (lambda w: depot(w, "depot-gazipur").inventory.update(DIESEL=500), 1000, "INSUFFICIENT_INVENTORY"),
    (lambda w: None, 6500, "DESTINATION_CAPACITY_EXCEEDED"),
])
def test_preflight_mirrors_simulator_validation_order(mutate, qty, code):
    """Each 409 condition from guide §5.2 is caught locally with the simulator's code."""
    w = make_world()
    mutate(w)
    res = _pf(w, qty)
    assert not res.ok and res.code == code


def test_preflight_dispatch_capacity():
    """Liters already dispatched from the depot this tick count against dispatch capacity."""
    w = make_world()
    res = _pf(w, 3000, dispatched=10000)
    assert not res.ok and res.code == "DISPATCH_CAPACITY_EXCEEDED" and res.max_quantity == 2000


def test_preflight_order_first_failure_wins():
    """With several problems at once, the earliest rule in the guide's order is reported."""
    w = make_world()
    station(w, "station-mirpur").status = "OUTAGE"
    route(w, "route-gazipur-mirpur").status = "DISRUPTED"
    assert _pf(w, 99999).code == "STATION_CLOSED"


def test_preflight_route_mismatch_and_unknown():
    """A route that does not connect depot and station, or missing entities, are rejected."""
    w = make_world()
    r = route(w, "route-gazipur-mirpur")
    assert preflight(depot(w, "depot-patiya"), station(w, "station-mirpur"), r, "DIESEL", 1, 0).code == "ROUTE_MISMATCH"
    assert preflight(None, station(w, "station-mirpur"), r, "DIESEL", 1, 0).code == "NOT_FOUND"


@pytest.mark.req("R-SEC-03")
def test_idempotency_key_is_deterministic_and_short():
    """Keys are deterministic per (run, plan, line, part) and within the 150-char limit."""
    k = idempotency_key(3, 120, 2, 1)
    assert k == "fo-3-120-2-1" == idempotency_key(3, 120, 2, 1) and len(k) <= 150
    assert idempotency_key(4, 120, 2, 1) != k


# --------------------------------------------------------------- detectors


@pytest.mark.req("R-ENV-01", "R-CAP-04")
def test_detect_scheduled_and_active_events():
    """Scheduled events raise warnings; active disruptive events are critical."""
    w = make_world()
    add_event(w, "demand_spike", 10, 20, region_ids=["region-dhaka"], multiplier=1.8)
    add_event(w, "route_disruption", 0, 20, status="ACTIVE", route_ids=["route-gazipur-tongi"])
    c = {x.key: x for x in det.detect_events(w)}
    assert c["event:1"].severity == "warning" and "Scheduled" in c["event:1"].title
    assert c["event:2"].severity == "critical"


@pytest.mark.req("R-APP-08")
def test_detect_network_single_route_station_is_critical():
    """A disrupted route to a single-route station (Tongi) is critical: no alternative route."""
    w = make_world()
    route(w, "route-gazipur-tongi").status = "DISRUPTED"
    route(w, "route-gazipur-mirpur").status = "DISRUPTED"
    c = {x.key: x for x in det.detect_network(w)}
    assert c["route:route-gazipur-tongi"].severity == "critical"
    assert "no alternative route" in c["route:route-gazipur-tongi"].title
    assert c["route:route-gazipur-mirpur"].severity == "warning"
    assert "route-patiya-mirpur" in c["route:route-gazipur-mirpur"].title


def test_detect_network_outage_and_depot_constraint():
    """Station outages are critical; a CONSTRAINED depot is a warning (still shippable)."""
    w = make_world()
    station(w, "station-coxsbazar").status = "OUTAGE"
    depot(w, "depot-patiya").status = "CONSTRAINED"
    c = {x.key: x for x in det.detect_network(w)}
    assert c["station:station-coxsbazar"].severity == "critical"
    assert c["depot:depot-patiya"].severity == "warning"


@pytest.mark.req("R-INT-P4")
def test_detect_supply_delay_and_shortfall_against_first_seen():
    """Supply arrivals delayed or reduced versus their first-seen values raise supply alerts."""
    w = make_world()
    w.supply_arrivals[0].planned_tick = 16
    w.supply_arrivals[0].status = "DELAYED"
    w.supply_arrivals[1].quantity = 8000
    first = {"supply-001": (12, 18000.0), "supply-002": (16, 16000.0)}
    c = {x.key: x for x in det.detect_supply(w, first)}
    assert "delayed 4 ticks" in c["supply:supply-001"].title
    assert "quantity" in c["supply:supply-002"].title


@pytest.mark.req("R-APP-05", "R-CAP-02")
def test_detect_shortage_uses_lead_time():
    """A stockout sooner than the fastest route's lead time is critical; comfortable stock raises nothing."""
    w = make_world(tick=24)
    station(w, "station-mirpur").inventory["PETROL"] = 150
    risks = score_risks(w, prior_forecast(w, 96))
    c = {x.key: x for x in det.detect_shortages(w, risks)}
    assert c["shortage:station-mirpur:PETROL"].severity == "critical"
    assert "shortage:station-tongi:DIESEL" not in c


@pytest.mark.req("R-INT-D1", "R-INT-D4")
def test_demand_anomaly_needs_consecutive_breaches_and_flags_region():
    """Two consecutive |z| > 3 breaches open an anomaly; two stations in a region raise a regional alert."""
    w = make_world(tick=10)
    mem = det.AnomalyState()
    z = {("station-mirpur", "DIESEL"): 4.0, ("station-tongi", "PETROL"): 5.0}
    assert det.detect_demand_anomalies(w, z, mem) == []
    w.instance.tick = 11
    c = {x.key: x for x in det.detect_demand_anomalies(w, z, mem)}
    assert "demand:station-mirpur:DIESEL" in c and c["region:region-dhaka"].severity == "critical"
    w.instance.tick = 20
    assert det.detect_demand_anomalies(w, {}, mem) == [], "anomalies expire after the hold period"


@pytest.mark.req("R-INT-D2")
def test_inventory_anomaly_detects_unexplained_change():
    """Stock falling far more than served demand between consecutive ticks is flagged."""
    from fuelops.shared.models import DemandPoint
    prev = make_world(tick=10)
    cur = make_world(tick=11)
    station(cur, "station-tongi").inventory["DIESEL"] = 11000 - 100 - 3000  # 3000 L vanished
    pts = [DemandPoint(station_id=s.id, fuel_type=f, tick=11, sim_time=cur.instance.sim_time,
                       demand=100 if (s.id, f) == ("station-tongi", "DIESEL") else 0,
                       served=100 if (s.id, f) == ("station-tongi", "DIESEL") else 0)
           for s in cur.stations for f in ("DIESEL", "PETROL", "OCTANE")]
    c = det.detect_inventory_anomalies(prev, cur, pts, det.AnomalyState())
    assert [x.key for x in c] == ["inventory:station-tongi:DIESEL"]


def test_inventory_anomaly_accounts_for_arrivals():
    """An allocation arriving this tick explains a stock increase (no alert)."""
    from fuelops.shared.models import DemandPoint
    prev = make_world(tick=10)
    cur = make_world(tick=11)
    station(cur, "station-tongi").inventory["DIESEL"] = 11000 + 5000
    a = add_allocation(cur, "route-gazipur-tongi", "DIESEL", 5000, status="ARRIVED", created_tick=8)
    a.actual_arrival_tick = 11
    pts = [DemandPoint(station_id=s.id, fuel_type=f, tick=11, sim_time=cur.instance.sim_time, demand=0, served=0)
           for s in cur.stations for f in ("DIESEL", "PETROL", "OCTANE")]
    assert det.detect_inventory_anomalies(prev, cur, pts, det.AnomalyState()) == []


# ----------------------------------------------------------- alert manager


async def test_alert_lifecycle_and_incident_correlation(tmp_path):
    """Alerts open once, correlate into an incident with the event, and resolve when the condition clears."""
    store = Store(str(tmp_path / "a.db"))
    await store.init()
    events = []
    mgr = AlertManager(store, lambda e, d: events.append(e))
    w = make_world(tick=5)
    add_event(w, "route_disruption", 0, 20, status="ACTIVE", route_ids=["route-gazipur-tongi"])
    route(w, "route-gazipur-tongi").status = "DISRUPTED"
    await mgr.evaluate(w, None, None, {}, {}, [])
    await mgr.evaluate(w, None, None, {}, {}, [])
    assert set(mgr.alerts) == {"event:1", "route:route-gazipur-tongi"}
    inc = mgr.incidents["event:1"]
    assert set(inc.alert_ids) == {"event:1", "route:route-gazipur-tongi"} and inc.severity == "critical"
    assert events.count("alert.opened") == 2, "no duplicate alerts on re-evaluation"
    w.events[0].status = "RESOLVED"
    route(w, "route-gazipur-tongi").status = "AVAILABLE"
    await mgr.evaluate(w, None, None, {}, {}, [])
    assert all(a.status == "resolved" for a in mgr.alerts.values())
    assert inc.status == "resolved" and inc.resolved_tick == 5


async def test_system_alerts_raise_ack_and_clear(tmp_path):
    """System alerts can be raised, acknowledged by an operator and cleared on recovery."""
    store = Store(str(tmp_path / "a.db"))
    mgr = AlertManager(store)
    await mgr.raise_system("intelligence", "warning", "Heuristic fallback active")
    a = mgr.alerts["system:intelligence"]
    assert (await mgr.ack(a.id, "operator")).status == "acked"
    await mgr.clear_system("intelligence")
    assert a.status == "resolved"
    assert await mgr.ack("missing", "operator") is None


async def test_alerts_reset_on_new_run(tmp_path):
    """A simulator reset (new run id) clears alert state."""
    mgr = AlertManager(Store(str(tmp_path / "a.db")))
    w = make_world()
    route(w, "route-gazipur-tongi").status = "DISRUPTED"
    await mgr.evaluate(w, None, None, {}, {}, [])
    assert mgr.alerts
    w2 = make_world()
    w2.run_id = 7
    await mgr.evaluate(w2, None, None, {}, {}, [])
    assert not mgr.alerts


# ------------------------------------------------------------------- store


def _plan_record(pid=1) -> PlanRecord:
    snap = RiskSnapshot(p_stockout=0.5, horizon_hours=6, hours_to_stockout=3.0, projected_unmet_liters=100,
                        end_inventory_liters=10)
    line = PlanLineRecord(line_no=1, route_id="route-gazipur-mirpur", depot_id="depot-gazipur",
                          station_id="station-mirpur", fuel_type="PETROL", quantity=3000, parts=[3000],
                          arrival_tick=8, reason="r", risk_before=snap, risk_after=snap)
    return PlanRecord(id=pid, run_id=1, tick=5, policy="planner", mode="manual", expires_tick=7,
                      result=PlanResult(policy="planner", tick=5, horizon_ticks=24), lines=[line])


@pytest.mark.req("R-RES-04", "R-DS-02")
async def test_store_roundtrip_and_restore(tmp_path):
    """Plans, decisions and alerts survive a restart via load_recent()."""
    s = Store(str(tmp_path / "s.db"))
    run = await s.new_run(12345, "baseline", 0)
    p = _plan_record()
    p.run_id = run
    await s.upsert_plan(p)
    await s.add_decision(Decision(id=1, run_id=run, plan_id=1, actor="operator", action="approve", tick=5))
    s.close()
    s2 = Store(str(tmp_path / "s.db"))
    restored = await s2.load_recent()
    assert restored["run"] == [run] and restored["plans"][0].id == 1 and restored["decisions"][0].actor == "operator"


async def test_store_skips_rows_from_old_schema(tmp_path):
    """Rows that no longer validate are skipped and counted instead of crashing startup."""
    path = str(tmp_path / "s.db")
    s = Store(path)
    run = await s.new_run(1, "baseline", 0)
    s.close()
    con = sqlite3.connect(path)
    con.execute("INSERT INTO plans VALUES (?,?,?,?,?)", (9, run, 1, '{"legacy": true}', 0))
    con.commit()
    con.close()
    restored = await Store(path).load_recent()
    assert restored["plans"] == [] and restored["skipped"] == [1]


@pytest.mark.req("R-RES-04")
async def test_store_fault_degrades_without_raising(tmp_path):
    """With the database down every call returns a default, the store reports degraded, and recovers."""
    s = Store(str(tmp_path / "s.db"))
    await s.init()
    s.force_fault(True)
    assert await s.ping() is False and not s.ok and "injected" in s.last_error
    assert await s.new_run(1, "x", 0) is None
    await s.upsert_plan(_plan_record())
    s.force_fault(False)
    assert await s.ping() is True and s.ok


# ------------------------------------------------------------ explanations


def test_template_explanation_mentions_impact_and_fallback():
    """Template text states quantities, the risk change and any fallback."""
    p = _plan_record()
    p.fallback_reason = "intelligence unreachable"
    text = templates.explain_plan(p)
    assert "3,000 L" in text and "Fallback active" in text and "station-mirpur PETROL" in text


class _FakeMessages:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.behaviour(kwargs)


def _fake_llm(behaviour):
    return SimpleNamespace(beta=SimpleNamespace(messages=_FakeMessages(behaviour)))


@pytest.mark.req("R-INT-G4", "R-INT-G5")
async def test_llm_explanation_used_when_available():
    """With a working client the LLM text is used; the request carries model, effort and the refusal fallback."""
    fake = _fake_llm(lambda kw: SimpleNamespace(stop_reason="end_turn",
                                                content=[SimpleNamespace(type="text", text="Ship fuel now.")]))
    ex = Explainer(Settings(), client=fake)
    text, source = await ex.explain_plan(_plan_record())
    call = fake.beta.messages.calls[0]
    assert (text, source) == ("Ship fuel now.", "llm")
    assert call["model"] == "claude-opus-5-5" and call["fallbacks"] == "default"
    assert call["output_config"] == {"effort": "low"} and "server-side-fallback-2026-07-01" in call["betas"]
    assert "simulated" in call["messages"][0]["content"].lower()


@pytest.mark.req("R-INT-G4", "R-RES-01")
@pytest.mark.parametrize("response", [
    SimpleNamespace(stop_reason="refusal", content=[]),
    SimpleNamespace(stop_reason="end_turn", content=[]),
])
async def test_llm_refusal_or_empty_falls_back_to_template(response):
    """A refusal or empty answer falls back to the deterministic template."""
    ex = Explainer(Settings(), client=_fake_llm(lambda kw: response))
    text, source = await ex.explain_plan(_plan_record())
    assert source == "template" and "Tick 5" in text


async def test_llm_not_configured_uses_template():
    """Without ANTHROPIC_API_KEY explanations are templates and the status says so."""
    ex = Explainer(Settings())
    assert not ex.configured
    text, source = await ex.explain_plan(_plan_record())
    assert source == "template" and ex.status()["last_outcome"] == "not_configured"


async def test_llm_errors_fall_back(monkeypatch):
    """A network error from the SDK is caught and the template is used."""
    import anthropic
    import httpx

    def boom(kw):
        raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))

    ex = Explainer(Settings(), client=_fake_llm(boom))
    text, source = await ex.explain_plan(_plan_record())
    assert source == "template" and ex.last_outcome == "unreachable"


# ---------------------------------------------------------------- heuristic


@pytest.mark.req("R-INT-X2", "R-RES-01")
def test_heuristic_ships_to_most_urgent_series_first():
    """The fallback policy tops up the most urgent series via the shortest usable route."""
    w = make_world(tick=24)
    station(w, "station-mirpur").inventory["PETROL"] = 200
    plan = heuristic_plan(w, prior_forecast(w, 96), PlannerParams())
    assert plan.lines and plan.lines[0].station_id == "station-mirpur" and plan.lines[0].fuel_type == "PETROL"
    assert plan.lines[0].route_id == "route-gazipur-mirpur"
    assert plan.policy == "heuristic" and plan.projected_unmet_after <= plan.projected_unmet_before


def test_heuristic_respects_dispatch_and_depot_limits():
    """The fallback never exceeds dispatch capacity or depot stock."""
    w = make_world(tick=24)
    for st in w.stations:
        for f in st.inventory:
            st.inventory[f] = 0
    depot(w, "depot-patiya").inventory["OCTANE"] = 1200
    plan = heuristic_plan(w, prior_forecast(w, 96), PlannerParams())
    by_depot = {}
    for ln in plan.lines:
        by_depot[ln.depot_id] = by_depot.get(ln.depot_id, 0) + ln.quantity
    assert by_depot.get("depot-gazipur", 0) <= 12000 and by_depot.get("depot-patiya", 0) <= 11000
    assert sum(ln.quantity for ln in plan.lines if ln.depot_id == "depot-patiya" and ln.fuel_type == "OCTANE") <= 1200


def test_heuristic_reroutes_when_direct_route_disrupted():
    """With the direct route down, the fallback uses the cross-region route."""
    w = make_world(tick=24)
    route(w, "route-gazipur-mirpur").status = "DISRUPTED"
    station(w, "station-mirpur").inventory["DIESEL"] = 0
    plan = heuristic_plan(w, prior_forecast(w, 96), PlannerParams())
    assert any(ln.route_id == "route-patiya-mirpur" for ln in plan.lines)


def test_heuristic_cycle_provides_forecasts_and_risks():
    """The fallback cycle returns forecasts, risks and ETAs so the console keeps working."""
    w = make_world(tick=0)
    resp = heuristic_cycle(w, PlannerParams())
    assert len(resp.forecasts) == 12 and len(resp.risks) == 12 and resp.model.kind == "fallback"
    assert {e.id for e in resp.supply_etas} == {"supply-001", "supply-002"}
