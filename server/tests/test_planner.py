"""Rolling-horizon LP planner (I2-T3) and what-if evaluator (I2-T4)."""

from __future__ import annotations

from collections import defaultdict

import pytest

from fuelops.intelligence.evaluate import evaluate
from fuelops.intelligence.forecast import StructuralForecaster
from fuelops.intelligence.planner import plan, split_parts
from fuelops.shared.models import FixedLine, PlannerParams
from tests.factories import add_allocation, add_event, depot, make_world, route, station

pytestmark = pytest.mark.req("R-INT-X1", "R-CAP-03", "R-INT-01")


def _plan(w, **params):
    fc = StructuralForecaster().fit(w, []).forecast(w, 96)
    return plan(w, fc, PlannerParams(**params))


def _by_route(result):
    out = defaultdict(float)
    for line in result.lines:
        out[(line.route_id, line.fuel_type)] += line.quantity
    return out


def test_split_parts_respects_max_and_min():
    """Quantities split into parts <= max_shipment, rounded to 100 L, dropping a remainder < min shipment."""
    assert split_parts(13000, 7000, 500) == [7000.0, 6000.0]
    assert split_parts(7350, 7000, 500) == [7000.0]
    assert split_parts(420, 7000, 500) == []
    assert split_parts(6999.99, 7000, 500) == [6900.0]


def test_plan_is_optimal_and_ships_to_the_station_at_risk():
    """A nearly empty Mirpur petrol tank gets a shipment now, and projected unmet demand falls."""
    w = make_world(tick=24)
    station(w, "station-mirpur").inventory["PETROL"] = 500
    res = _plan(w)
    assert res.solver_status == "OPTIMAL"
    mir = [ln for ln in res.lines if ln.station_id == "station-mirpur" and ln.fuel_type == "PETROL"]
    assert mir, "expected a shipment to Mirpur petrol"
    assert res.projected_unmet_after < res.projected_unmet_before
    ln = mir[0]
    assert ln.risk_after.projected_unmet_liters <= ln.risk_before.projected_unmet_liters
    assert ln.reason and ln.signals and ln.alternatives
    assert all(p <= route(w, ln.route_id).max_shipment for p in ln.parts)


@pytest.mark.req("R-INT-D3")
def test_dispatch_capacity_respected():
    """With every station empty, shipments created now from each depot stay within dispatch capacity."""
    w = make_world(tick=24)
    for st in w.stations:
        for f in st.inventory:
            st.inventory[f] = 0
    res = _plan(w)
    sent = defaultdict(float)
    for ln in res.lines:
        sent[ln.depot_id] += ln.quantity
    assert sent["depot-gazipur"] <= 12000 + 1e-6
    assert sent["depot-patiya"] <= 11000 + 1e-6
    assert any("Dispatch capacity binding" in b for b in res.binding_constraints)


def test_committed_dispatch_reduces_capacity():
    """Liters already dispatched this tick count against the depot's dispatch capacity."""
    w = make_world(tick=24)
    for st in w.stations:
        for f in st.inventory:
            st.inventory[f] = 0
    res = _plan(w, committed_dispatch={"depot-gazipur": 10000})
    sent = sum(ln.quantity for ln in res.lines if ln.depot_id == "depot-gazipur")
    assert sent <= 2000 + 1e-6


def test_station_headroom_including_creation_check():
    """A full tank receives nothing: on-hand + quantity may not exceed capacity at creation."""
    w = make_world(tick=24)
    st = station(w, "station-karnaphuli")
    st.inventory.update(st.capacity)
    res = _plan(w)
    for ln in res.lines:
        if ln.station_id == "station-karnaphuli":
            assert st.inventory[ln.fuel_type] + ln.quantity <= st.capacity[ln.fuel_type]
    assert not [ln for ln in res.lines if ln.station_id == "station-karnaphuli"]


def test_in_transit_fuel_prevents_double_shipping():
    """If enough fuel is already in transit, the planner does not ship more to that series."""
    w = make_world(tick=24)
    st = station(w, "station-tongi")
    st.inventory["OCTANE"] = 1000
    add_allocation(w, "route-gazipur-tongi", "OCTANE", 4500, expected_arrival=26)
    res = _plan(w)
    assert _by_route(res)[("route-gazipur-tongi", "OCTANE")] == 0


@pytest.mark.req("R-CAP-04", "R-INT-X3")
def test_disrupted_route_is_rerouted_cross_region():
    """With route-gazipur-mirpur disrupted, Mirpur is supplied via the cross-region route from Patiya."""
    w = make_world(tick=24)
    route(w, "route-gazipur-mirpur").status = "DISRUPTED"
    station(w, "station-mirpur").inventory["DIESEL"] = 300
    res = _plan(w)
    by = _by_route(res)
    assert by[("route-gazipur-mirpur", "DIESEL")] == 0
    assert by[("route-patiya-mirpur", "DIESEL")] > 0
    assert any("route-gazipur-mirpur unavailable" in b for b in res.binding_constraints)
    line = next(ln for ln in res.lines if ln.route_id == "route-patiya-mirpur")
    assert "cross-region reroute" in line.signals


@pytest.mark.req("R-CAP-04", "R-ENV-01")
def test_prepositions_single_route_station_before_scheduled_disruption():
    """Tongi's only route will be disrupted from t+2: the planner ships now so the outage window is covered."""
    w = make_world(tick=24)
    st = station(w, "station-tongi")
    st.inventory["DIESEL"] = 4000
    add_event(w, "route_disruption", 26, 60, route_ids=["route-gazipur-tongi"])
    res = _plan(w)
    shipped = _by_route(res)[("route-gazipur-tongi", "DIESEL")]
    assert shipped > 0
    assert shipped + st.inventory["DIESEL"] <= st.capacity["DIESEL"]
    line = next(ln for ln in res.lines if ln.route_id == "route-gazipur-tongi" and ln.fuel_type == "DIESEL")
    assert any("route_disruption" in s for s in line.signals)
    assert "single-route station" in line.signals


def test_route_blocked_if_disruption_starts_at_departure():
    """A shipment created now departs next tick; if the route is disrupted then, it is not planned (FAILED risk)."""
    w = make_world(tick=24)
    station(w, "station-tongi").inventory["DIESEL"] = 0
    add_event(w, "route_disruption", 25, 60, route_ids=["route-gazipur-tongi"])
    res = _plan(w)
    assert _by_route(res)[("route-gazipur-tongi", "DIESEL")] == 0


def test_outage_station_gets_no_shipments():
    """Allocations to a station in OUTAGE would be rejected (STATION_CLOSED), so none are planned."""
    w = make_world(tick=24)
    st = station(w, "station-coxsbazar")
    st.status = "OUTAGE"
    for f in st.inventory:
        st.inventory[f] = 0
    res = _plan(w)
    assert not [ln for ln in res.lines if ln.station_id == "station-coxsbazar"]


@pytest.mark.req("R-INT-X3")
def test_empty_depot_ships_nothing_and_reports_rationing():
    """With no depot stock and no supply due, nothing is shipped and the shortfall is reported."""
    w = make_world(tick=24)
    for d in w.depots:
        for f in d.inventory:
            d.inventory[f] = 0
    w.supply_arrivals.clear()
    station(w, "station-mirpur").inventory["PETROL"] = 0
    res = _plan(w)
    assert res.lines == []
    assert any("station-mirpur PETROL" in r for r in res.rationing)


def test_depot_inventory_respected():
    """Shipments of a fuel never exceed what the depot holds."""
    w = make_world(tick=24)
    depot(w, "depot-gazipur").inventory["OCTANE"] = 1500
    for sid in ("station-mirpur", "station-tongi"):
        station(w, sid).inventory["OCTANE"] = 0
    res = _plan(w)
    sent = sum(ln.quantity for ln in res.lines if ln.depot_id == "depot-gazipur" and ln.fuel_type == "OCTANE")
    assert sent <= 1500


def test_upcoming_supply_arrival_is_used():
    """Fuel scheduled to reach the depot within the horizon lets the planner cover later demand."""
    w = make_world(tick=24)
    depot(w, "depot-gazipur").inventory["DIESEL"] = 0
    station(w, "station-tongi").inventory["DIESEL"] = 0
    res_without = _plan(w)
    w.supply_arrivals[0].planned_tick = 26
    res_with = _plan(w)
    assert res_with.projected_unmet_after <= res_without.projected_unmet_after


def test_plan_lines_have_consistent_arrival_ticks():
    """Arrival tick = now + 1 + transit_ticks for every line."""
    w = make_world(tick=40)
    station(w, "station-coxsbazar").inventory["PETROL"] = 200
    res = _plan(w)
    assert res.lines
    for ln in res.lines:
        assert ln.arrival_tick == 40 + 1 + route(w, ln.route_id).transit_ticks


@pytest.mark.req("R-APP-10", "R-DS-01")
def test_evaluate_what_if_before_after():
    """A fixed shipment lowers projected unmet demand for its series in the what-if evaluator."""
    w = make_world(tick=24)
    station(w, "station-mirpur").inventory["PETROL"] = 300
    fc = StructuralForecaster().fit(w, []).forecast(w, 24)
    res = evaluate(w, fc, [FixedLine(route_id="route-gazipur-mirpur", fuel_type="PETROL", quantity=6000)], 24)
    before = {(p.station_id, p.fuel_type): p for p in res.before}
    after = {(p.station_id, p.fuel_type): p for p in res.after}
    key = ("station-mirpur", "PETROL")
    assert after[key].unmet_total < before[key].unmet_total
    assert res.unmet_after < res.unmet_before
    assert len(after[key].inventory) == 25


def test_evaluate_rejects_unknown_route():
    """Unknown routes in a what-if request are rejected."""
    w = make_world(tick=0)
    fc = StructuralForecaster().fit(w, []).forecast(w, 8)
    with pytest.raises(ValueError):
        evaluate(w, fc, [FixedLine(route_id="route-nowhere", fuel_type="DIESEL", quantity=100)], 8)


def test_solve_time_is_small():
    """A full 24-tick plan solves quickly enough for per-tick re-planning."""
    w = make_world(tick=24)
    res = _plan(w)
    assert res.solve_ms < 5000
