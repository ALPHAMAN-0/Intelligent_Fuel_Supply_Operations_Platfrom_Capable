"""Risk scoring, projections and supply ETAs (I2-T2)."""

from __future__ import annotations

import pytest

from fuelops.intelligence.forecast import StructuralForecaster
from fuelops.shared.risk import score_risks
from fuelops.shared.world import (
    committed_dispatch,
    in_transit_arrivals,
    p_stockout,
    project_series,
    route_available_at,
    station_open_at,
    supply_etas,
)
from tests.factories import add_allocation, add_event, make_world, route, station

pytestmark = pytest.mark.req("R-CAP-02", "R-INT-P2", "R-INT-P3")


def _risks(w):
    fc = StructuralForecaster().fit(w, []).forecast(w, 96)
    return {(r.station_id, r.fuel_type): r for r in score_risks(w, fc)}


def test_project_series_counts_unmet_and_first_shortfall():
    """Mean projection serves demand from stock and reports unmet liters and the first short tick."""
    inv, unmet, first = project_series(100, 1000, [40, 40, 40], {})
    assert inv == [100, 60, 20, 0]
    assert unmet == pytest.approx(20)
    assert first == 3


def test_project_series_arrivals_capped_by_capacity():
    """Arrivals cannot push inventory above tank capacity."""
    inv, unmet, first = project_series(900, 1000, [10, 10], {1: 500})
    assert inv[1] == pytest.approx(990)
    assert unmet == 0 and first is None


def test_project_series_outage_serves_nothing():
    """During an outage every liter of demand is unmet and stock is untouched."""
    inv, unmet, first = project_series(500, 1000, [50, 50], {}, open_at=[False, True])
    assert inv == [500, 500, 450]
    assert unmet == pytest.approx(50) and first == 1


def test_p_stockout_monotonic_in_horizon():
    """P(stockout) never decreases as the horizon grows."""
    mu, sigma = [100.0] * 48, [10.0] * 48
    ps = [p_stockout(2000, mu, sigma, {}, h) for h in (4, 16, 20, 24, 48)]
    assert ps == sorted(ps)
    assert ps[0] < 0.01 and ps[-1] > 0.99


def test_low_inventory_series_gets_short_time_to_stockout():
    """Mirpur petrol with 800 L runs out within a few hours and scores high P(stockout 12h)."""
    w = make_world(tick=0)
    station(w, "station-mirpur").inventory["PETROL"] = 800
    r = _risks(w)[("station-mirpur", "PETROL")]
    assert r.hours_to_stockout is not None and r.hours_to_stockout < 6
    assert r.p_stockout["12h"] > 0.9
    assert r.p_stockout["4h"] <= r.p_stockout["12h"] <= r.p_stockout["24h"]


def test_in_transit_fuel_delays_stockout():
    """An in-transit allocation arriving before the stockout pushes time-to-stockout later."""
    w = make_world(tick=0)
    station(w, "station-mirpur").inventory["PETROL"] = 800
    before = _risks(w)[("station-mirpur", "PETROL")].hours_to_stockout
    add_allocation(w, "route-gazipur-mirpur", "PETROL", 6000, expected_arrival=3)
    r = _risks(w)[("station-mirpur", "PETROL")]
    assert r.in_transit == 6000 and r.inventory_position == 6800
    assert r.hours_to_stockout is None or r.hours_to_stockout > before


@pytest.mark.req("R-RES-03")
def test_outage_station_is_certain_risk():
    """A station in OUTAGE has P(stockout) = 1 and says so in its notes."""
    w = make_world(tick=0)
    station(w, "station-coxsbazar").status = "OUTAGE"
    r = _risks(w)[("station-coxsbazar", "DIESEL")]
    assert r.p_stockout == {"4h": 1.0, "12h": 1.0, "24h": 1.0}
    assert any("outage" in n for n in r.notes)


def test_confidence_lower_when_stale_or_event_nearby():
    """Confidence drops when data is stale and when a live event touches the station."""
    w = make_world(tick=0)
    fresh = _risks(w)[("station-tongi", "DIESEL")].confidence
    w.stale = True
    assert _risks(w)[("station-tongi", "DIESEL")].confidence < fresh
    w.stale = False
    add_event(w, "demand_spike", 5, 20, station_ids=["station-tongi"])
    assert _risks(w)[("station-tongi", "DIESEL")].confidence < fresh


@pytest.mark.req("R-INT-P4")
def test_supply_eta_anticipates_scheduled_delay_and_shortfall():
    """Scheduled shipment_delay shifts the ETA; scheduled supply_shortfall reduces the expected quantity."""
    w = make_world(tick=0)
    add_event(w, "shipment_delay", 5, 10, depot_ids=["depot-gazipur"], delay_ticks=4)
    add_event(w, "supply_shortfall", 5, 10, depot_ids=["depot-patiya"], fuel_types=["PETROL"], factor=0.5)
    etas = {e.id: e for e in supply_etas(w)}
    assert etas["supply-001"].eta_tick == 16 and etas["supply-001"].delayed
    assert etas["supply-002"].expected_quantity == pytest.approx(8000) and etas["supply-002"].shortfall


def test_supply_eta_detects_delay_and_shortfall_vs_first_seen():
    """A DELAYED arrival or a quantity below its first-seen value is flagged."""
    w = make_world(tick=0)
    w.supply_arrivals[0].status = "DELAYED"
    etas = {e.id: e for e in supply_etas(w, first_seen_quantity={"supply-002": 20000})}
    assert etas["supply-001"].delayed
    assert etas["supply-002"].shortfall


def test_route_availability_windows():
    """Scheduled disruptions block their window; active disruptions end at end_tick; unknown end blocks all."""
    w = make_world(tick=0)
    r = route(w, "route-gazipur-tongi")
    add_event(w, "route_disruption", 5, 10, route_ids=[r.id])
    assert route_available_at(w, r, 4) and not route_available_at(w, r, 5) and route_available_at(w, r, 10)
    w.events.clear()
    r.status = "DISRUPTED"
    assert not route_available_at(w, r, 50)
    add_event(w, "route_disruption", 0, 6, status="ACTIVE", route_ids=[r.id])
    assert not route_available_at(w, r, 5) and route_available_at(w, r, 6)


def test_station_open_windows():
    """Scheduled outages close the station inside the window only."""
    w = make_world(tick=0)
    st = station(w, "station-mirpur")
    add_event(w, "station_outage", 3, 6, station_ids=[st.id])
    assert [station_open_at(w, st, t) for t in range(2, 8)] == [True, False, False, False, True, True]


def test_in_transit_and_committed_dispatch():
    """Active allocations count as in-transit; those created this tick count against dispatch capacity."""
    w = make_world(tick=4)
    add_allocation(w, "route-gazipur-mirpur", "DIESEL", 3000, status="PENDING")
    add_allocation(w, "route-gazipur-tongi", "DIESEL", 2000, status="IN_TRANSIT", created_tick=2)
    add_allocation(w, "route-patiya-mirpur", "DIESEL", 1000, status="ARRIVED", created_tick=1)
    arr = in_transit_arrivals(w)
    assert arr[("station-mirpur", "DIESEL")] == {7: 3000}
    assert arr[("station-tongi", "DIESEL")] == {5: 2000}
    assert committed_dispatch(w) == {"depot-gazipur": 3000}
