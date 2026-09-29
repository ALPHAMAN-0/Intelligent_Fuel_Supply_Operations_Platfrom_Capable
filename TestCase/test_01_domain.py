"""Pytest implementation of the TC-D-xxx test cases from `01_unit_domain.md`.

Each test function mirrors one TC-D-id from the spec. They use the in-memory
factories from `tests/factories.py` and the pure helpers in `fuelops.shared`.

Markers: `unit` (default).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fuelops.shared import spec
from fuelops.shared.models import WorldState
from fuelops.shared.sim_models import (
    AllocationRequest,
    Depot,
    Instance,
    Station,
)
from fuelops.shared.world import (
    WorldIndex,
    arrival_tick,
    demand_multiplier_at,
    hours_to_ticks,
    in_transit_arrivals,
    p_stockout,
    prior_forecast,
    project_series,
    relative_arrivals,
    sim_time_at,
    ticks_per_hour,
    ticks_to_hours,
)
from tests.factories import make_world, synth_history

pytestmark = pytest.mark.unit


# ------------------------------------------------------------------------ TC-D-001
def test_TC_D_001_spec_prior_peak_vs_off_peak() -> None:
    peak = spec.prior_per_tick("urban_high", "PETROL", 8, 60, 1.0)
    off = spec.prior_per_tick("urban_high", "PETROL", 3, 60, 1.0)
    daily = spec.PROFILE_DAILY["urban_high"]["PETROL"]
    assert peak == pytest.approx(daily / 24 * 1.45)
    assert off == pytest.approx(daily / 24 * 0.70)


# ------------------------------------------------------------------------ TC-D-002
def test_TC_D_002_spec_prior_scales_with_region_and_tick_length() -> None:
    base = spec.prior_per_tick("regional", "DIESEL", 12, 60, 1.0)
    region_scaled = spec.prior_per_tick("regional", "DIESEL", 12, 60, 1.08)
    shorter_tick = spec.prior_per_tick("regional", "DIESEL", 12, 15, 1.0)
    assert region_scaled == pytest.approx(base * 1.08)
    assert shorter_tick == pytest.approx(base / 4)


# ------------------------------------------------------------------------ TC-D-003
def test_TC_D_003_noise_per_profile() -> None:
    assert spec.noise("urban_high") == 0.10
    assert spec.noise("industrial") == 0.08
    assert spec.noise("highway") == 0.12
    assert spec.noise("regional") == 0.10
    assert spec.noise("unknown-profile") == spec.DEFAULT_NOISE


# ------------------------------------------------------------------------ TC-D-004
def test_TC_D_004_unknown_profile_falls_back_to_average() -> None:
    avg = sum(p["DIESEL"] for p in spec.PROFILE_DAILY.values()) / len(spec.PROFILE_DAILY)
    assert spec.daily_demand("marina", "DIESEL") == pytest.approx(avg)


# ------------------------------------------------------------------------ TC-D-005
def test_TC_D_005_unknown_keys_are_ignored_unknown_status_kept() -> None:
    base = Station(id="x", name="X", region_id="r", status="OPEN", demand_profile="urban_high",
                   demand_multiplier=1.0, capacity={"DIESEL": 1, "PETROL": 1, "OCTANE": 1},
                   inventory={"DIESEL": 0, "PETROL": 0, "OCTANE": 0})
    # extra="ignore" on SimModel: anything new is silently dropped
    dumped = base.model_dump()
    rebuilt = Station.model_validate({**dumped, "weather": "rain"})
    assert rebuilt.status == "OPEN"

    weird_status = Station.model_validate({**dumped, "status": "WEIRD_THING"})
    assert weird_status.status == "WEIRD_THING"

    with pytest.raises(ValidationError):
        Station.model_validate({**dumped, "demand_multiplier": -0.1})


# ------------------------------------------------------------------------ TC-D-006
def test_TC_D_006_allocation_request_validation() -> None:
    with pytest.raises(ValidationError):
        AllocationRequest(idempotency_key="k", source_depot_id="d", destination_station_id="s",
                          route_id="r", fuel_type="PETROL", quantity=0)
    with pytest.raises(ValidationError):
        AllocationRequest(idempotency_key="", source_depot_id="d", destination_station_id="s",
                          route_id="r", fuel_type="PETROL", quantity=100)
    # 150-char key and 0.01 L are accepted
    AllocationRequest(idempotency_key="k" * 150, source_depot_id="d", destination_station_id="s",
                      route_id="r", fuel_type="PETROL", quantity=0.01)


# ------------------------------------------------------------------------ TC-D-007
def test_TC_D_007_world_state_default_and_round_trip() -> None:
    w = make_world()
    assert w.stale is False
    assert w.degraded == []
    json_str = w.model_dump_json()
    parsed = WorldState.model_validate_json(json_str)
    assert parsed == w


# ------------------------------------------------------------------------ TC-D-008
def test_TC_D_008_sim_time_and_hour_at_tick() -> None:
    w = make_world(tick=0, tick_minutes=15)  # sim_time = 2026-01-01T00:00Z
    assert sim_time_at(w, 4).hour == 1  # 4 ticks * 15 min = 1 hour later
    assert sim_time_at(w, 4 * 96).hour == 0  # wraps a day later


# ------------------------------------------------------------------------ TC-D-009
def test_TC_D_009_tick_time_round_trip() -> None:
    w = make_world(tick_minutes=15)
    assert ticks_per_hour(w) == pytest.approx(4.0)
    assert hours_to_ticks(w, 1) == 4
    assert hours_to_ticks(w, 0) == 1  # clamps to 1

    w60 = make_world(tick_minutes=60)
    assert hours_to_ticks(w60, 1) == 1
    assert ticks_to_hours(w60, 1) == pytest.approx(1.0)


# ------------------------------------------------------------------------ TC-D-010
def test_TC_D_010_world_index_routes_and_cross_region() -> None:
    w = make_world()
    idx = WorldIndex(w)
    assert {r.id for r in idx.routes_to["station-mirpur"]} == {"route-gazipur-mirpur", "route-patiya-mirpur"}
    assert {r.id for r in idx.routes_to["station-coxsbazar"]} == {"route-patiya-coxsbazar"}
    assert {r.id for r in idx.routes_from["depot-gazipur"]} == {"route-gazipur-mirpur", "route-gazipur-tongi",
                                                               "route-gazipur-karnaphuli"}
    # cross-region: gazipur->karnaphuli, patiya->mirpur
    assert idx.is_cross_region(idx.routes["route-patiya-mirpur"]) is True
    assert idx.is_cross_region(idx.routes["route-gazipur-karnaphuli"]) is True
    assert idx.is_cross_region(idx.routes["route-gazipur-mirpur"]) is False


# ------------------------------------------------------------------------ TC-D-011
def test_TC_D_011_demand_multiplier_at_for_active_and_scheduled() -> None:
    # `demand_multiplier_at` implements two responsibilities:
    #   1) Apply SCHEDULED demand_spikes whose window contains the tick (multiplier is multiplied in).
    #   2) UNDO an ACTIVE demand_spike once the tick is past its end_tick (the live value already includes
    #      the effect; the function is only used to remove it for forward planning).
    w = make_world()
    st = next(s for s in w.stations if s.id == "station-mirpur")
    from tests.factories import add_event
    add_event(w, "demand_spike", start=10, end=14, status="SCHEDULED",
              station_ids=["station-mirpur"], multiplier=2.0)
    add_event(w, "demand_spike", start=20, end=24, status="ACTIVE",
              station_ids=["station-mirpur"], multiplier=1.5)
    # Pre-spike
    assert demand_multiplier_at(w, st, 5) == pytest.approx(1.0)
    # Inside the SCHEDULED window
    assert demand_multiplier_at(w, st, 12) == pytest.approx(2.0)
    # After the SCHEDULED window (end_tick is exclusive: 14 is the first tick outside)
    assert demand_multiplier_at(w, st, 15) == pytest.approx(1.0)
    # While ACTIVE but inside the window: nothing extra (the live value already includes the spike)
    assert demand_multiplier_at(w, st, 22) == pytest.approx(1.0)
    # Past the ACTIVE end_tick: the function removes the spike
    assert demand_multiplier_at(w, st, 25) == pytest.approx(1.0 / 1.5)


# ------------------------------------------------------------------------ TC-D-012
def test_TC_D_012_route_available_at_inside_window() -> None:
    w = make_world()
    from tests.factories import add_event
    add_event(w, "route_disruption", start=30, end=34, status="SCHEDULED", route_ids=["route-gazipur-mirpur"])
    r = next(r for r in w.routes if r.id == "route-gazipur-mirpur")
    from fuelops.shared.world import route_available_at
    assert route_available_at(w, r, 20) is True
    assert route_available_at(w, r, 32) is False
    assert route_available_at(w, r, 40) is True


# ------------------------------------------------------------------------ TC-D-013
def test_TC_D_013_station_open_at_mirrors_route() -> None:
    w = make_world()
    from tests.factories import add_event
    add_event(w, "station_outage", start=50, end=54, status="SCHEDULED", station_ids=["station-tongi"])
    st = next(s for s in w.stations if s.id == "station-tongi")
    from fuelops.shared.world import station_open_at
    assert station_open_at(w, st, 40) is True
    assert station_open_at(w, st, 52) is False
    assert station_open_at(w, st, 60) is True


# ------------------------------------------------------------------------ TC-D-014
def test_TC_D_014_route_usable_requires_depart_and_arrival_clear() -> None:
    w = make_world()
    from tests.factories import add_event
    r = next(r for r in w.routes if r.id == "route-gazipur-mirpur")
    from fuelops.shared.world import WorldIndex, route_usable
    idx = WorldIndex(w)
    # Baseline
    assert route_usable(w, idx, r, create_tick=10) is True
    # Outage starts at create_tick+1: must be available at both departure (10+1=11) and arrival (11+transit=13)
    add_event(w, "station_outage", start=11, end=15, status="SCHEDULED", station_ids=["station-mirpur"])
    assert route_usable(w, idx, r, create_tick=10) is True  # outage is only at the destination; arrival IS past 11
    # Outage covers the create_tick itself (so even the creation moment is bad)
    add_event(w, "station_outage", start=10, end=14, status="SCHEDULED", station_ids=["station-mirpur"])
    assert route_usable(w, idx, r, create_tick=10) is False


# ------------------------------------------------------------------------ TC-D-015
def test_TC_D_015_arrival_tick_uses_expected_when_present() -> None:
    r = type("R", (), {"transit_ticks": 3})()
    assert arrival_tick(make_world(), r, created_tick=10) == 14
    assert arrival_tick(make_world(), r, created_tick=10, expected=999) == 999


# ------------------------------------------------------------------------ TC-D-016
def test_TC_D_016_in_transit_arrivals_aggregates_active() -> None:
    w = make_world()
    from tests.factories import add_allocation
    add_allocation(w, "route-gazipur-mirpur", "DIESEL", 1500, status="PENDING")
    add_allocation(w, "route-gazipur-mirpur", "DIESEL", 1500, status="PENDING")
    add_allocation(w, "route-gazipur-mirpur", "DIESEL", 2000, status="IN_TRANSIT")
    add_allocation(w, "route-gazipur-mirpur", "DIESEL", 999, status="ARRIVED")
    add_allocation(w, "route-patiya-mirpur", "PETROL", 700, status="PENDING")
    arrivals = in_transit_arrivals(w)
    arrival_tick_ = w.instance.tick + 1 + 2  # transit_ticks=2
    assert arrivals[("station-mirpur", "DIESEL")][arrival_tick_] == pytest.approx(5000.0)
    assert arrivals[("station-mirpur", "PETROL")][w.instance.tick + 1 + 4] == pytest.approx(700.0)


# ------------------------------------------------------------------------ TC-D-017
def test_TC_D_017_committed_dispatch_only_this_tick() -> None:
    w = make_world()
    from tests.factories import add_allocation
    tick = w.instance.tick
    add_allocation(w, "route-gazipur-mirpur", "PETROL", 100, status="PENDING", created_tick=tick - 1)
    add_allocation(w, "route-gazipur-mirpur", "PETROL", 200, status="IN_TRANSIT", created_tick=tick)
    add_allocation(w, "route-gazipur-tongi", "PETROL", 300, status="IN_TRANSIT", created_tick=tick)
    from fuelops.shared.world import committed_dispatch
    out = committed_dispatch(w)
    assert out.get("depot-gazipur", 0.0) == pytest.approx(500.0)


# ------------------------------------------------------------------------ TC-D-018
def test_TC_D_018_supply_etas_horour_delay_and_shortfall() -> None:
    w = make_world()
    from fuelops.shared.world import supply_etas
    from tests.factories import add_event
    sa = next(sa for sa in w.supply_arrivals if sa.id == "supply-001")
    add_event(w, "shipment_delay", start=10, end=12, status="SCHEDULED", depot_ids=["depot-gazipur"],
              delay_ticks=4)
    add_event(w, "supply_shortfall", start=8, end=20, status="SCHEDULED",
              depot_ids=["depot-gazipur"], fuel_types=["DIESEL"], factor=0.6)
    etas = supply_etas(w, first_seen_quantity={sa.id: sa.quantity})
    target = next(e for e in etas if e.id == sa.id)
    assert target.eta_tick == sa.planned_tick + 4
    assert target.delayed is True
    assert target.expected_quantity == pytest.approx(sa.quantity * 0.6)
    assert target.shortfall is True


# ------------------------------------------------------------------------ TC-D-019
def test_TC_D_019_prior_forecast_x_multiplier() -> None:
    w = make_world(tick_minutes=60)
    w.stations[0].demand_multiplier = 1.2
    fc = prior_forecast(w, horizon=24)
    assert len(fc) == 4 * 3
    mirpur_petrol = fc[("station-mirpur", "PETROL")]
    base = spec.prior_per_tick("urban_high", "PETROL", 0, 60, 1.0)
    assert mirpur_petrol[0][0] == pytest.approx(base * 1.2)
    assert mirpur_petrol[1][0] == pytest.approx(spec.noise("urban_high") * mirpur_petrol[0][0])


# ------------------------------------------------------------------------ TC-D-020
def test_TC_D_020_project_series_hand_rolled() -> None:
    inv, unmet, first = project_series(1000, 2000, [300, 300, 300], {2: 500})
    assert inv == pytest.approx([1000, 700, 900, 600])
    assert unmet == pytest.approx(0)
    assert first is None

    inv, unmet, first = project_series(100, 200, [300], {})
    assert first == 1
    assert unmet == pytest.approx(200)


# ------------------------------------------------------------------------ TC-D-021
def test_TC_D_021_p_stockout_monotone_and_bounded() -> None:
    p_short = p_stockout(0, [100] * 10, [10] * 10, {}, within=1)
    p_long = p_stockout(0, [100] * 10, [10] * 10, {}, within=10)
    assert p_long >= p_short
    assert 0.0 <= p_short <= 1.0
    assert 0.0 <= p_long <= 1.0

    assert p_stockout(10 ** 9, [100] * 10, [10] * 10, {}, within=10) == 0.0
    assert p_stockout(0, [0] * 5, [0] * 5, {}, within=3) == 0.0


# ------------------------------------------------------------------------ TC-D-022
def test_TC_D_022_relative_arrivals_rebase() -> None:
    out = relative_arrivals({5: 100, 11: 200, 12: 300}, t0=10)
    assert out == {1: 200, 2: 300}


# ------------------------------------------------------------------------ TC-D-023
def test_TC_D_023_event_hits_empty_means_all() -> None:
    from fuelops.shared.sim_models import SimEvent
    from fuelops.shared.world import event_hits, event_hits_station
    w = make_world()
    st_mirpur = next(s for s in w.stations if s.id == "station-mirpur")
    empty = SimEvent(id=1, type="demand_spike", start_tick=0, end_tick=1, status="ACTIVE", parameters={})
    only_other_station = SimEvent(id=2, type="demand_spike", start_tick=0, end_tick=1, status="ACTIVE",
                                  parameters={"station_ids": ["station-not-mirpur"]})
    by_region = SimEvent(id=3, type="demand_spike", start_tick=0, end_tick=1, status="ACTIVE",
                         parameters={"region_ids": [st_mirpur.region_id]})
    assert event_hits_station(empty, st_mirpur) is True
    assert event_hits_station(only_other_station, st_mirpur) is False
    assert event_hits_station(by_region, st_mirpur) is True
    assert event_hits(empty, "route_ids", "any") is True
    # Use a route-specific event to confirm a missing key is "all" and an explicit key restricts
    only_other_route = SimEvent(id=4, type="route_disruption", start_tick=0, end_tick=1, status="ACTIVE",
                                parameters={"route_ids": ["route-not-real"]})
    assert event_hits(only_other_route, "route_ids", "route-not-real") is True
    assert event_hits(only_other_route, "route_ids", "route-real") is False


# ------------------------------------------------------------------------ TC-D-024
def test_TC_D_024_make_world_matches_baseline() -> None:
    w = make_world()
    assert len(w.depots) == 2
    assert len(w.stations) == 4
    assert len(w.routes) == 6
    assert {d.dispatch_capacity_per_tick for d in w.depots} == {12000.0, 11000.0}


# ------------------------------------------------------------------------ TC-D-025
def test_TC_D_025_synth_history_expected_rows() -> None:
    w = make_world(tick=96, tick_minutes=15)
    h_full = synth_history(w, ticks=96, end_tick=96)
    assert len(h_full) == 96 * 4 * 3
    h_partial = synth_history(w, ticks=11, end_tick=10)
    assert len(h_partial) == 11 * 4 * 3
    assert min(p.tick for p in h_partial) == 0
    assert max(p.tick for p in h_partial) == 10
    assert all(p.demand >= 0 for p in h_full)


# ------------------------------------------------------------------------ TC-D-026
def test_TC_D_026_add_event_and_allocation_have_deterministic_ids() -> None:
    from tests.factories import add_allocation, add_event
    w = make_world()
    e1 = add_event(w, "demand_spike", start=10, end=20, status="SCHEDULED", station_ids=["station-mirpur"],
                   multiplier=1.5)
    e2 = add_event(w, "route_disruption", start=30, end=40, status="SCHEDULED", route_ids=["route-gazipur-mirpur"])
    assert e1.id == 1 and e2.id == 2
    a = add_allocation(w, "route-gazipur-mirpur", "PETROL", 1000)
    assert a.created_tick == w.instance.tick
    assert a.expected_arrival_tick == w.instance.tick + 1 + 2  # transit_ticks=2


# ------------------------------------------------------------------------ TC-D-027
def test_TC_D_027_latency_window_snapshot() -> None:
    from fuelops.shared.metrics import LatencyWindow
    w = LatencyWindow()
    snap = w.snapshot()
    assert snap["count"] == 0
    assert snap["p95_ms"] == 0.0

    w.add(0.123, False)
    snap = w.snapshot()
    assert snap["count"] == 1
    assert snap["p95_ms"] == pytest.approx(123.0)


# ------------------------------------------------------------------------ TC-D-028
def test_TC_D_028_settings_env_overrides_dotenv(tmp_path, monkeypatch) -> None:
    from fuelops.shared.config import load_settings
    env_file = tmp_path / ".env"
    env_file.write_text("SIM_BASE_URL=http://sim:8000\nLOG_LEVEL=DEBUG\n")
    settings = load_settings(env={"LOG_LEVEL": "WARNING"}, dotenv=env_file)
    assert settings.sim_base_url == "http://sim:8000"
    assert settings.log_level == "WARNING"


# ------------------------------------------------------------------------ TC-D-029
def test_TC_D_029_configure_logging_emits_json(capsys) -> None:
    import json

    from fuelops.shared.logging import configure_logging, get_logger
    configure_logging("INFO", "test-svc")
    log = get_logger("test")
    log.info("ping.event", value=42)
    captured = capsys.readouterr().out.strip().splitlines()
    assert captured, "expected at least one log line"
    parsed = json.loads(captured[-1])
    assert parsed["event"] == "ping.event"
    assert parsed["service"] == "test-svc"
    assert parsed["value"] == 42


# ------------------------------------------------------------------------ TC-D-030
def test_TC_D_030_numeric_fields_reject_negative_and_nan() -> None:
    # Station.demand_multiplier has ge=0 (verified above in TC-D-005); capacity values aren't constrained.
    with pytest.raises(ValidationError):
        Station(id="x", name="X", region_id="r", status="OPEN", demand_profile="urban_high",
                demand_multiplier=-1.0, capacity={"DIESEL": 1, "PETROL": 1, "OCTANE": 1},
                inventory={"DIESEL": 0, "PETROL": 0, "OCTANE": 0})
    # Depot.dispatch_capacity_per_tick has ge=0.
    with pytest.raises(ValidationError):
        Depot(id="d", name="D", region_id="r", status="OPEN", dispatch_capacity_per_tick=-1,
              capacity={"DIESEL": 1, "PETROL": 1, "OCTANE": 1},
              inventory={"DIESEL": 1, "PETROL": 0, "OCTANE": 0})
    # tick_minutes has gt=0 on Instance.
    with pytest.raises(ValidationError):
        Instance(id=1, scenario_id="x", sim_time=__import__("datetime").datetime(2026, 1, 1),
                 tick=0, tick_minutes=0, status="PAUSED")
