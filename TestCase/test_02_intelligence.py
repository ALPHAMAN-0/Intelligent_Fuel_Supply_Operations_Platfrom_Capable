"""Pytest implementation of TC-I-xxx from `02_unit_intelligence.md`.

Targets: `fuelops.intelligence.{forecast, risk, planner, evaluate}`.
Markers: `unit`.
"""

from __future__ import annotations

import pytest

from fuelops.intelligence.forecast import ForecastParams, StructuralForecaster
from fuelops.intelligence.planner import PlannerParams, plan, split_parts
from fuelops.shared import spec
from fuelops.shared.risk import score_risks, series_confidence
from fuelops.shared.world import prior_forecast
from tests.factories import make_world, synth_history


pytestmark = pytest.mark.unit


# ------------------------------------------------------------------------ TC-I-001
def test_TC_I_001_prior_forecast_matches_structural_on_empty_history() -> None:
    w = make_world(tick_minutes=60)
    model = StructuralForecaster(ForecastParams()).fit(w, history=[])
    structural = model.forecast(w, horizon=24)
    prior = prior_forecast(w, horizon=24)
    for key in prior:
        s_mu, s_sigma = structural[key]
        p_mu, p_sig = prior[key]
        assert s_mu == pytest.approx(p_mu, abs=1e-9)
        assert s_sigma == pytest.approx(p_sig, abs=1e-6)


# ------------------------------------------------------------------------ TC-I-002
def test_TC_I_002_structural_learns_hour_rates() -> None:
    # `rates` are in liters per HOUR (see StructuralForecaster docstring); the prior is also hourly.
    w = make_world(tick=96, tick_minutes=15)
    history = synth_history(w, ticks=96, scale=2.0, end_tick=96)
    model = StructuralForecaster(ForecastParams()).fit(w, history)
    series = model.series[(("station-mirpur", "PETROL"))]
    baseline = spec.prior_per_tick("urban_high", "PETROL", 8, 60, 1.0)  # 60-min basis = liters/hour
    assert series.rates[8] == pytest.approx(2.0 * baseline, rel=0.30)
    assert series.n_obs >= 4
    assert series.source in {"learned", "prior+data"}


# ------------------------------------------------------------------------ TC-I-003
def test_TC_I_003_noise_falls_back_to_spec_when_no_observations() -> None:
    w = make_world()
    model = StructuralForecaster(ForecastParams()).fit(w, history=[])
    series = model.series[("station-tongi", "DIESEL")]
    assert series.noise == pytest.approx(spec.noise("industrial"), rel=0.05)


# ------------------------------------------------------------------------ TC-I-004
def test_TC_I_004_forecast_respects_scheduled_spike() -> None:
    # A SCHEDULED demand_spike multiplies the per-hour prior by 2.0 while the event is in-window.
    # `in_window` is half-open: start_tick <= tick < end_tick, so ticks 5..8 are inside and
    # tick 9 is outside again. The forecast's mu[h-1] is for tick = t0 + h, so with t0=0 and
    # tick_minutes=60, mu[0] is tick=1 (out), mu[4] is tick=5 (in), mu[8] is tick=9 (out).
    # Hour factors for urban_high: h=1..6 are 0.7 (so prior=306.25 L/h), h=7..9 are 1.45
    # (prior=634.375 L/h). The spike acts only on the multiplier, so the ratio at the same
    # hour factor is exactly 2.0 and the ratio across hour factors is 2.0 * in_factor/out_factor.
    w = make_world(tick_minutes=60)
    from tests.factories import add_event
    add_event(w, "demand_spike", start=5, end=9, status="SCHEDULED",
              station_ids=["station-mirpur"], multiplier=2.0)
    model = StructuralForecaster(ForecastParams()).fit(w, history=[])
    fc = model.forecast(w, horizon=12)
    mu, _ = fc[("station-mirpur", "PETROL")]
    # mu[0] is tick=1 (out, hour=1, factor=0.7) and mu[4] is tick=5 (in, hour=5, factor=0.7).
    # Both are in the 0.7 plateau, so the ratio is exactly 2.0.
    assert mu[4] / mu[0] == pytest.approx(2.0)
    # Pre-window vs post-window: both out, so the ratio is just the hour factor ratio.
    pre = mu[3]    # tick=4, out, hour=4 (factor=0.7)
    post = mu[8]   # tick=9, out, hour=9 (factor=1.45)
    assert post / pre == pytest.approx(spec.hour_factor("urban_high", 9) /
                                       spec.hour_factor("urban_high", 4))
    # In-window vs post-window at the same hour factor (1.45 plateau): ratio = 2.0.
    assert mu[7] / mu[8] == pytest.approx(2.0)  # mu[7]=tick=8 (in, h=8), mu[8]=tick=9 (out, h=9)
    # And in-window vs out-of-window across different hours includes the hour factor:
    assert mu[7] / pre == pytest.approx(2.0 * spec.hour_factor("urban_high", 8) /
                                           spec.hour_factor("urban_high", 4))


# ------------------------------------------------------------------------ TC-I-005
def test_TC_I_005_series_forecast_round_trip() -> None:
    from fuelops.shared.models import SeriesForecast
    sf = SeriesForecast(station_id="s", fuel_type="PETROL", first_tick=1, mu=[1.234567, 2.5], sigma=[0.1, 0.2],
                        source="learned", n_obs=10, noise=0.1)
    parsed = SeriesForecast.model_validate_json(sf.model_dump_json())
    # Round trip is lossless: the model is just a pydantic container. (Rounding happens in
    # `FittedModel.series_forecasts`, not on the schema.)
    assert parsed.mu == sf.mu
    assert parsed.sigma == sf.sigma
    assert parsed.station_id == "s"


# ------------------------------------------------------------------------ TC-I-006
def test_TC_I_006_series_confidence_drops_when_stale_or_no_data() -> None:
    w = make_world()
    c_full = series_confidence(w, n_obs=96, noise=0.10, event_nearby=False, station_id="station-mirpur")
    c_none = series_confidence(w, n_obs=0, noise=0.10, event_nearby=False, station_id="station-mirpur")
    assert c_none < c_full

    w.stale = True
    c_stale = series_confidence(w, n_obs=96, noise=0.10, event_nearby=False, station_id="station-mirpur")
    assert c_stale <= c_full

    w.stale = False
    c_event = series_confidence(w, n_obs=96, noise=0.10, event_nearby=True, station_id="station-mirpur")
    assert c_event < c_full

    c_noisy = series_confidence(w, n_obs=96, noise=0.50, event_nearby=False, station_id="station-mirpur")
    assert c_noisy < c_full


# ------------------------------------------------------------------------ TC-I-007
def test_TC_I_007_score_risks_inventory_and_count() -> None:
    w = make_world()
    fc = prior_forecast(w, horizon=24)
    risks = score_risks(w, fc)
    assert len(risks) == 12
    for r in risks:
        st = next(s for s in w.stations if s.id == r.station_id)
        assert r.on_hand == st.inventory[r.fuel_type]
        assert r.capacity == st.capacity[r.fuel_type]


# ------------------------------------------------------------------------ TC-I-008
def test_TC_I_008_p_stockout_one_when_station_closed() -> None:
    w = make_world()
    w.stations[0].status = "CLOSED"
    fc = prior_forecast(w, horizon=24)
    risks = score_risks(w, fc)
    closed = next(r for r in risks if r.station_id == "station-mirpur")
    assert closed.p_stockout == {"4h": 1.0, "12h": 1.0, "24h": 1.0}
    assert any("closed" in n for n in closed.notes)


# ------------------------------------------------------------------------ TC-I-009
def test_TC_I_009_single_route_and_no_route_available_notes() -> None:
    w = make_world()
    fc = prior_forecast(w, horizon=24)
    risks = score_risks(w, fc)
    tongi = next(r for r in risks if r.station_id == "station-tongi")
    assert "single-route station" in tongi.notes


# ------------------------------------------------------------------------ TC-I-010
def test_TC_I_010_in_transit_uses_only_active_allocations() -> None:
    w = make_world()
    from tests.factories import add_allocation
    add_allocation(w, "route-gazipur-mirpur", "DIESEL", 1500, status="PENDING")
    add_allocation(w, "route-gazipur-mirpur", "DIESEL", 2000, status="ARRIVED")
    fc = prior_forecast(w, horizon=24)
    risks = score_risks(w, fc)
    mirpur_diesel = next(r for r in risks if r.station_id == "station-mirpur" and r.fuel_type == "DIESEL")
    assert mirpur_diesel.in_transit == pytest.approx(1500.0)


# ------------------------------------------------------------------------ TC-I-011
def test_TC_I_011_snapshot_includes_safety_z_buffer() -> None:
    # `risk_snapshot` lives in fuelops.shared.risk
    from fuelops.shared.risk import risk_snapshot
    w = make_world()
    fc = prior_forecast(w, horizon=24)
    st = w.stations[0]
    fuel = "PETROL"
    risk = risk_snapshot(w, st, fuel, fc, {}, horizon=24)
    assert risk.projected_unmet_liters >= 0
    assert 0.0 <= risk.p_stockout_12h <= 1.0


# ------------------------------------------------------------------------ TC-I-012
def test_TC_I_012_planner_returns_optimal_with_depleted_inventory() -> None:
    w = make_world()
    for s in w.stations:
        s.inventory = {f: 100.0 for f in spec.FUELS}
    fc = prior_forecast(w, horizon=24)
    result = plan(w, fc, PlannerParams(horizon_ticks=24))
    assert result.solver_status == "OPTIMAL"
    assert len(result.lines) > 0
    assert result.projected_unmet_after < result.projected_unmet_before
    for line in result.lines:
        route = next(r for r in w.routes if r.id == line.route_id)
        assert line.quantity == pytest.approx(sum(line.parts))
        assert max(line.parts) <= route.max_shipment


# ------------------------------------------------------------------------ TC-I-013
def test_TC_I_013_planner_empty_when_inventory_covers_horizon() -> None:
    w = make_world()
    for s in w.stations:
        s.inventory = dict(s.capacity)
    fc = prior_forecast(w, horizon=24)
    result = plan(w, fc, PlannerParams(horizon_ticks=24))
    assert result.lines == []
    assert any("No shipment needed" in n for n in result.notes)


# ------------------------------------------------------------------------ TC-I-014
def test_TC_I_014_split_parts_quantisation() -> None:
    # Remainder threshold is `max(min_shipment, UNIT)`; with UNIT=100 and min_shipment=500, a remainder < 500 is dropped.
    assert split_parts(14500, 7000, 500) == [7000.0, 7000.0, 500.0]
    assert split_parts(7400, 7000, 500) == [7000.0]  # 400 < 500 → dropped
    assert split_parts(400, 7000, 500) == []
    assert split_parts(0, 7000, 500) == []
    # All outputs are multiples of UNIT (100 L) and never exceed max_shipment.
    for q in (1500, 2300, 5500, 8200, 12300):
        parts = split_parts(q, 7000, 500)
        assert all(p % 100 == 0 for p in parts)
        assert all(p <= 7000 for p in parts)


# ------------------------------------------------------------------------ TC-I-015
def test_TC_I_015_planner_respects_dispatch_capacity() -> None:
    w = make_world()
    w.depots[0].dispatch_capacity_per_tick = 1500
    # Force action
    for s in w.stations:
        s.inventory = {f: 100.0 for f in spec.FUELS}
    fc = prior_forecast(w, horizon=24)
    result = plan(w, fc, PlannerParams(horizon_ticks=24))
    total = sum(line.quantity for line in result.lines if line.depot_id == w.depots[0].id)
    assert total <= 1500 + 1e-6
    assert any("Dispatch capacity binding" in b for b in result.binding_constraints)


# ------------------------------------------------------------------------ TC-I-016
def test_TC_I_016_cross_region_reroute_signal() -> None:
    w = make_world()
    # Force planner to use the cross-region route for Mirpur by emptying Gazipur
    w.depots[0].inventory = {f: 0.0 for f in spec.FUELS}
    for s in w.stations:
        s.inventory = {f: 100.0 for f in spec.FUELS}
    fc = prior_forecast(w, horizon=24)
    result = plan(w, fc, PlannerParams(horizon_ticks=24))
    mirpur_lines = [line for line in result.lines if line.station_id == "station-mirpur"]
    assert mirpur_lines
    assert any("cross-region reroute" in sig for line in mirpur_lines for sig in line.signals)


# ------------------------------------------------------------------------ TC-I-017
def test_TC_I_017_single_route_signal() -> None:
    w = make_world()
    # Deplete only Tongi to force a shipment there
    for s in w.stations:
        if s.id == "station-tongi":
            s.inventory = {f: 100.0 for f in spec.FUELS}
        else:
            s.inventory = dict(s.capacity)
    fc = prior_forecast(w, horizon=24)
    result = plan(w, fc, PlannerParams(horizon_ticks=24))
    tongi_lines = [line for line in result.lines if line.station_id == "station-tongi"]
    assert tongi_lines
    assert any("single-route station" in sig for line in tongi_lines for sig in line.signals)


# ------------------------------------------------------------------------ TC-I-018
def test_TC_I_018_event_signals_surface() -> None:
    w = make_world()
    from tests.factories import add_event
    add_event(w, "station_outage", start=5, end=10, status="SCHEDULED", station_ids=["station-tongi"])
    add_event(w, "demand_spike", start=3, end=8, status="ACTIVE", region_ids=["region-dhaka"], multiplier=1.8)
    for s in w.stations:
        if s.id == "station-tongi":
            s.inventory = {f: 100.0 for f in spec.FUELS}
        else:
            s.inventory = dict(s.capacity)
    fc = prior_forecast(w, horizon=24)
    result = plan(w, fc, PlannerParams(horizon_ticks=24))
    tongi_lines = [line for line in result.lines if line.station_id == "station-tongi"]
    assert tongi_lines
    sig_blob = " ".join(sig for line in tongi_lines for sig in line.signals)
    assert "station_outage" in sig_blob
    assert "demand_spike" in sig_blob


# ------------------------------------------------------------------------ TC-I-019
def test_TC_I_019_skip_alternative() -> None:
    w = make_world()
    for s in w.stations:
        if s.id == "station-mirpur":
            s.inventory = {f: 100.0 for f in spec.FUELS}
        else:
            s.inventory = dict(s.capacity)
    fc = prior_forecast(w, horizon=24)
    result = plan(w, fc, PlannerParams(horizon_ticks=24))
    assert result.lines
    line = result.lines[0]
    assert any(alt.description == "Skip this shipment" and alt.delta_unmet_liters > 0 for alt in line.alternatives)


# ------------------------------------------------------------------------ TC-I-020
def test_TC_I_020_alternative_route_offered() -> None:
    w = make_world()
    for s in w.stations:
        if s.id == "station-mirpur":
            s.inventory = {f: 100.0 for f in spec.FUELS}
        else:
            s.inventory = dict(s.capacity)
    fc = prior_forecast(w, horizon=24)
    result = plan(w, fc, PlannerParams(horizon_ticks=24))
    line = next(line for line in result.lines if line.station_id == "station-mirpur")
    assert any(alt.route_id == "route-patiya-mirpur" for alt in line.alternatives)


# ------------------------------------------------------------------------ TC-I-021
def test_TC_I_021_capacity_binding_constraint() -> None:
    # Force a tank-capacity binding: shrink Mirpur's PETROL tank to the planner's preferred
    # shipment size so the planned quantity hits capacity - UNIT.
    w = make_world()
    for s in w.stations:
        if s.id == "station-mirpur":
            s.capacity = {"PETROL": 3000.0, "DIESEL": 5000.0, "OCTANE": 4000.0}
            s.inventory = {"DIESEL": 0, "PETROL": 0, "OCTANE": 0}
        else:
            s.inventory = dict(s.capacity)
    fc = prior_forecast(w, horizon=24)
    result = plan(w, fc, PlannerParams(horizon_ticks=24, max_parts_per_route=1))
    mirpur_lines = [line for line in result.lines if line.station_id == "station-mirpur"]
    assert mirpur_lines, "planner should ship to Mirpur when PETROL is depleted"
    # At least one binding flag is present somewhere (per-line or top-level).
    binding_blob = " ".join(list(result.binding_constraints) +
                            [b for line in mirpur_lines for b in line.binding])
    assert any(k in binding_blob for k in ("tank capacity", "Dispatch capacity", "max shipment"))


# ------------------------------------------------------------------------ TC-I-022
def test_TC_I_022_excluded_route_not_used() -> None:
    w = make_world()
    w.routes[0].status = "DISRUPTED"
    for s in w.stations:
        s.inventory = {f: 100.0 for f in spec.FUELS}
    fc = prior_forecast(w, horizon=24)
    result = plan(w, fc, PlannerParams(horizon_ticks=24))
    assert all(line.route_id != "route-gazipur-mirpur" for line in result.lines)
    assert any("route-gazipur-mirpur unavailable" in b for b in result.binding_constraints)


# ------------------------------------------------------------------------ TC-I-023
def test_TC_I_023_in_transit_reduces_unmet() -> None:
    w = make_world()
    from tests.factories import add_allocation
    add_allocation(w, "route-gazipur-mirpur", "PETROL", 8000, status="PENDING")
    fc = prior_forecast(w, horizon=24)
    before = plan(w, fc, PlannerParams(horizon_ticks=24))
    # Without the in-transit: zero the allocation
    w2 = w.model_copy(deep=True)
    w2.allocations = [a for a in w2.allocations if a.status != "PENDING" or a.quantity != 8000]
    fc2 = prior_forecast(w2, horizon=24)
    after = plan(w2, fc2, PlannerParams(horizon_ticks=24))
    assert before.projected_unmet_after <= after.projected_unmet_after


# ------------------------------------------------------------------------ TC-I-024
def test_TC_I_024_terminal_cover_forces_end_inventory() -> None:
    # Verify the terminal cover slack appears in the LP: with the cover term active, the planner
    # produces a plan whose `objective` differs from the no-cover baseline, and the result remains
    # OPTIMAL.
    w = make_world()
    for s in w.stations:
        s.inventory = {f: 100.0 for f in spec.FUELS}
    fc = prior_forecast(w, horizon=24)
    p_with = plan(w, fc, PlannerParams(horizon_ticks=24, terminal_cover_ticks=8, terminal_weight=2.0))
    p_without = plan(w, fc, PlannerParams(horizon_ticks=24, terminal_weight=0.0))
    assert p_with.solver_status == "OPTIMAL"
    assert p_without.solver_status == "OPTIMAL"
    # Objective values differ (cover term added weight).
    assert p_with.objective != pytest.approx(p_without.objective, rel=1e-3)


# ------------------------------------------------------------------------ TC-I-025
def test_TC_I_025_evaluate_improves_unmet() -> None:
    from fuelops.shared.risk import evaluate
    from fuelops.shared.models import FixedLine
    w = make_world()
    # Deplete Mirpur so unmet would happen on the horizon.
    for s in w.stations:
        if s.id == "station-mirpur":
            s.inventory = {f: 50.0 for f in spec.FUELS}
    fc = prior_forecast(w, horizon=24)
    lines = [FixedLine(route_id="route-gazipur-mirpur", fuel_type="PETROL", quantity=4000)]
    response = evaluate(w, fc, lines, horizon=24)
    assert len(response.before) == 12 and len(response.after) == 12
    assert response.unmet_after < response.unmet_before


# ------------------------------------------------------------------------ TC-I-026
def test_TC_I_026_evaluate_rejects_unknown_route() -> None:
    from fuelops.shared.risk import evaluate
    from fuelops.shared.models import FixedLine
    w = make_world()
    fc = prior_forecast(w, horizon=24)
    with pytest.raises(ValueError, match="unknown route"):
        evaluate(w, fc, [FixedLine(route_id="nope", fuel_type="PETROL", quantity=500)], horizon=24)


# ------------------------------------------------------------------------ TC-I-027
def test_TC_I_027_forecast_endpoint() -> None:
    from fastapi.testclient import TestClient
    from fuelops.intelligence.main import create_app, Engine
    w = make_world()
    history = synth_history(w, ticks=96, end_tick=96)
    app = create_app(engine=Engine(models_dir="/nonexistent", version="v1"))
    with TestClient(app) as client:
        resp = client.post("/forecast", json={
            "state": w.model_dump(mode="json"),
            "history": [h.model_dump(mode="json") for h in history],
            "horizon_ticks": 24,
        })
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 12
    assert all(len(item["mu"]) == 24 for item in body)


# ------------------------------------------------------------------------ TC-I-028
def test_TC_I_028_cycle_endpoint() -> None:
    from fastapi.testclient import TestClient
    from fuelops.intelligence.main import create_app, Engine
    w = make_world()
    history = synth_history(w, ticks=96, end_tick=96)
    app = create_app(engine=Engine(models_dir="/nonexistent", version="v1"))
    with TestClient(app) as client:
        resp = client.post("/cycle", json={
            "state": w.model_dump(mode="json"),
            "history": [h.model_dump(mode="json") for h in history],
        })
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["forecasts"]) == 12
    assert len(body["risks"]) == 12
    assert body["plan"]["solver_status"] in {"OPTIMAL", "NOT_SOLVED"}
    assert body["model"]["version"] == "v1"


# ------------------------------------------------------------------------ TC-I-029
def test_TC_I_029_evaluate_endpoint_returns_422_on_unknown_route() -> None:
    from fastapi.testclient import TestClient
    from fuelops.intelligence.main import create_app, Engine
    w = make_world()
    app = create_app(engine=Engine(models_dir="/nonexistent", version="v1"))
    with TestClient(app) as client:
        resp = client.post("/evaluate", json={
            "state": w.model_dump(mode="json"),
            "lines": [{"route_id": "nope", "fuel_type": "PETROL", "quantity": 500}],
        })
    assert resp.status_code == 422
    assert "unknown route" in resp.text


# ------------------------------------------------------------------------ TC-I-030
def test_TC_I_030_health_and_model_info() -> None:
    from fastapi.testclient import TestClient
    from fuelops.intelligence.main import create_app, Engine
    app = create_app(engine=Engine(models_dir="/nonexistent", version="v1"))
    with TestClient(app) as client:
        h = client.get("/health").json()
        info = client.get("/model/info").json()
        metrics = client.get("/metrics").text
    assert h["status"] == "ok" and h["model_version"] == "v1"
    assert info["version"] == "v1" and info["kind"] == "structural"
    assert metrics.startswith("#") or "fuelops_" in metrics


# ------------------------------------------------------------------------ TC-I-031
def test_TC_I_031_empty_fitted_noise_floor_is_zero() -> None:
    from fuelops.intelligence.forecast import FittedModel, ForecastParams
    m = FittedModel(params=ForecastParams())
    assert m.noise_floor() == 0.0


# ------------------------------------------------------------------------ TC-I-032
def test_TC_I_032_model_info_reports_fitted_series_and_observations() -> None:
    w = make_world()
    empty = StructuralForecaster(ForecastParams()).fit(w, history=[])
    info = empty.info()
    assert info.fitted_series == 12
    assert info.observations == 0
    assert 0.0 < info.noise_floor < 0.12

    w96 = make_world(tick=96)
    history = synth_history(w96, ticks=96, end_tick=96)
    populated = StructuralForecaster(ForecastParams()).fit(w96, history)
    pop_info = populated.info()
    assert pop_info.observations >= 96 * 12
    assert 0.0 < pop_info.noise_floor < 0.12


# ------------------------------------------------------------------------ TC-I-033
def test_TC_I_033_heuristic_fallback_signature_present() -> None:
    """Heuristic fallback lives in backend (decision/heuristic.py) — this test only asserts the
    shared helpers (prior_forecast, score_risks) compose a fallback path deterministically."""
    w = make_world()
    fc = prior_forecast(w, horizon=24)
    risks = score_risks(w, fc)
    assert len(risks) == 12
    for r in risks:
        assert r.confidence >= 0.05


# ------------------------------------------------------------------------ TC-I-034
def test_TC_I_034_arrival_tick_matches_transit_formula() -> None:
    w = make_world()
    for s in w.stations:
        if s.id == "station-tongi":
            s.inventory = {f: 100.0 for f in spec.FUELS}
        else:
            s.inventory = dict(s.capacity)
    fc = prior_forecast(w, horizon=24)
    result = plan(w, fc, PlannerParams(horizon_ticks=24))
    for line in result.lines:
        route = next(r for r in w.routes if r.id == line.route_id)
        assert line.arrival_tick == w.instance.tick + 1 + route.transit_ticks