"""Structural demand forecaster (I2-T1, docs/model-selection.md)."""

from __future__ import annotations

import math

import pytest

from fuelops.intelligence.forecast import ForecastParams, StructuralForecaster, export_rates
from fuelops.shared import spec
from tests.factories import add_event, make_world, station, synth_history

pytestmark = pytest.mark.req("R-INT-P1", "R-INT-01")


def _mean_abs_pct(a: list[float], b: list[float]) -> float:
    return sum(abs(x - y) / y for x, y in zip(a, b, strict=True) if y > 0) / len(a)


def test_prior_only_forecast_matches_spec():
    """With no history the forecast equals the spec prior (base/96 x hour factor x region factor)."""
    w = make_world(tick=0)
    fc = StructuralForecaster().fit(w, []).forecast(w, 4)
    mu, sigma = fc[("station-mirpur", "DIESEL")]
    # tick 1 = 00:15 -> off-peak factor 0.70 for urban_high
    assert mu[0] == pytest.approx(8500 / 96 * 0.70, rel=1e-6)
    assert sigma[0] == pytest.approx(0.10 * mu[0], rel=1e-6)
    kar = fc[("station-karnaphuli", "DIESEL")][0][0]
    assert kar == pytest.approx(10500 / 96 * 0.75 * 1.08, rel=1e-6), "region factor applied"


def test_learns_scale_from_history():
    """After 3 days of history generated at 1.5x the spec, forecasts move to the data (within 6 %)."""
    w = make_world(tick=288)
    hist = synth_history(w, 288, scale=1.5, noise=0.05)
    model = StructuralForecaster().fit(w, hist)
    fc = model.forecast(w, 96)
    prior = StructuralForecaster().fit(w, []).forecast(w, 96)
    for key in [("station-mirpur", "PETROL"), ("station-tongi", "DIESEL"), ("station-coxsbazar", "OCTANE")]:
        truth = [p * 1.5 for p in prior[key][0]]
        assert _mean_abs_pct(fc[key][0], truth) < 0.06, key
    assert model.series[("station-mirpur", "PETROL")].source == "learned"


def test_multiplier_is_normalised_out_of_history():
    """History recorded during a x1.8 spike does not inflate the learned base level."""
    w = make_world(tick=192)
    hist = synth_history(w, 192, noise=0.03, multiplier_fn=lambda sid, t: 1.8 if 100 <= t < 150 else 1.0)
    fc = StructuralForecaster().fit(w, hist).forecast(w, 96)
    prior = StructuralForecaster().fit(w, []).forecast(w, 96)
    key = ("station-mirpur", "DIESEL")
    assert _mean_abs_pct(fc[key][0], prior[key][0]) < 0.05


def test_live_multiplier_scales_forecast():
    """The current station demand_multiplier multiplies the forecast from the next tick."""
    w = make_world(tick=0)
    base = StructuralForecaster().fit(w, []).forecast(w, 8)[("station-tongi", "DIESEL")][0]
    station(w, "station-tongi").demand_multiplier = 1.8
    spiked = StructuralForecaster().fit(w, []).forecast(w, 8)[("station-tongi", "DIESEL")][0]
    assert all(s == pytest.approx(b * 1.8) for s, b in zip(spiked, base, strict=True))


@pytest.mark.req("R-INT-D4", "R-ENV-01")
def test_scheduled_spike_applied_only_inside_window_and_region():
    """A SCHEDULED demand_spike on region-dhaka raises forecasts only in its window and only for Dhaka stations."""
    w = make_world(tick=0)
    base = StructuralForecaster().fit(w, []).forecast(w, 30)
    add_event(w, "demand_spike", start=8, end=20, region_ids=["region-dhaka"], multiplier=1.8)
    fc = StructuralForecaster().fit(w, []).forecast(w, 30)
    mir_b, mir = base[("station-mirpur", "PETROL")][0], fc[("station-mirpur", "PETROL")][0]
    for h in range(1, 31):
        expected = 1.8 if 8 <= h < 20 else 1.0
        assert mir[h - 1] == pytest.approx(mir_b[h - 1] * expected), h
    assert fc[("station-karnaphuli", "PETROL")][0] == base[("station-karnaphuli", "PETROL")][0]


def test_active_spike_expected_to_resolve_at_end_tick():
    """An ACTIVE spike's multiplier is divided back out from its end_tick onward."""
    w = make_world(tick=10)
    station(w, "station-mirpur").demand_multiplier = 1.8
    add_event(w, "demand_spike", start=8, end=14, status="ACTIVE", station_ids=["station-mirpur"], multiplier=1.8)
    fc = StructuralForecaster().fit(w, []).forecast(w, 8)[("station-mirpur", "DIESEL")][0]
    station(w, "station-mirpur").demand_multiplier = 1.0
    w.events.clear()
    base = StructuralForecaster().fit(w, []).forecast(w, 8)[("station-mirpur", "DIESEL")][0]
    for h in range(1, 9):
        t = 10 + h
        assert fc[h - 1] == pytest.approx(base[h - 1] * (1.8 if t < 14 else 1.0)), t


def test_tick_minutes_is_read_not_hardcoded():
    """With 30-minute ticks each tick carries twice the liters of a 15-minute tick at the same hour."""
    w15 = make_world(tick=0, tick_minutes=15)
    w30 = make_world(tick=0, tick_minutes=30)
    f15 = StructuralForecaster().fit(w15, []).forecast(w15, 1)[("station-tongi", "DIESEL")][0][0]
    f30 = StructuralForecaster().fit(w30, []).forecast(w30, 1)[("station-tongi", "DIESEL")][0][0]
    assert f30 == pytest.approx(2 * f15)


def test_hour_of_day_comes_from_target_sim_time():
    """At tick 23 (05:45) the next tick is 06:00, the start of the industrial busy window (factor 1.55)."""
    w = make_world(tick=23)
    mu = StructuralForecaster().fit(w, []).forecast(w, 1)[("station-tongi", "DIESEL")][0][0]
    assert mu == pytest.approx(14000 / 96 * 1.55)


def test_noise_estimated_from_residuals():
    """The jitter estimate converges to the true relative noise of the history."""
    w = make_world(tick=480)
    hist = synth_history(w, 480, noise=0.10, seed=3)
    model = StructuralForecaster(ForecastParams(noise_strength=1)).fit(w, hist)
    k = model.series[("station-karnaphuli", "PETROL")].noise
    assert 0.08 < k < 0.12
    assert model.noise_floor() == pytest.approx(
        sum(s.noise for s in model.series.values()) / 12 * math.sqrt(2 / math.pi))


def test_learned_rates_can_seed_the_prior():
    """Exported rates loaded as a model file reproduce the learned forecast at tick 0 without history."""
    w = make_world(tick=288)
    model = StructuralForecaster().fit(w, synth_history(w, 288, scale=1.3, noise=0.02))
    params = ForecastParams(version="v2", rates=export_rates(model), prior_strength=4)
    w0 = make_world(tick=0)
    seeded = StructuralForecaster(params).fit(w0, []).forecast(w0, 4)[("station-mirpur", "DIESEL")][0]
    spec_only = StructuralForecaster().fit(w0, []).forecast(w0, 4)[("station-mirpur", "DIESEL")][0]
    assert seeded[0] == pytest.approx(spec_only[0] * 1.3, rel=0.06)


def test_unknown_profile_falls_back_to_average():
    """A surprise station with an unknown demand profile still gets a forecast."""
    assert spec.daily_demand("mystery", "DIESEL") == pytest.approx(
        sum(p["DIESEL"] for p in spec.PROFILE_DAILY.values()) / 4)
    w = make_world(tick=0)
    station(w, "station-tongi").demand_profile = "mystery"
    fc = StructuralForecaster().fit(w, []).forecast(w, 2)
    assert fc[("station-tongi", "PETROL")][0][0] > 0


def test_series_forecast_payload_shape():
    """series_forecasts returns 12 series with first_tick = t0 + 1 and equal-length mu/sigma."""
    w = make_world(tick=5)
    out = StructuralForecaster().fit(w, []).series_forecasts(w, 24)
    assert len(out) == 12
    assert all(s.first_tick == 6 and len(s.mu) == len(s.sigma) == 24 for s in out)
