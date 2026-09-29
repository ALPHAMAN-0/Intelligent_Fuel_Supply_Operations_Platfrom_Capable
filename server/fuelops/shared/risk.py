"""Risk scoring and projections: inventory position, time-to-stockout, P(stockout) at 4/12/24 h, confidence,
projected unmet demand and the what-if evaluator. Pure functions shared by the planner and the heuristic fallback.
"""

from __future__ import annotations

from collections import defaultdict

from fuelops.shared import spec
from fuelops.shared.models import (
    EvaluateResponse,
    FixedLine,
    RiskScore,
    RiskSnapshot,
    SeriesProjection,
    WorldState,
)
from fuelops.shared.sim_models import STATION_OPEN
from fuelops.shared.world import (
    LIVE_EVENT,
    WorldIndex,
    event_hits_station,
    hours_to_ticks,
    in_transit_arrivals,
    p_stockout,
    project_series,
    relative_arrivals,
    route_available_at,
    station_open_at,
    ticks_to_hours,
)

Forecasts = dict[tuple[str, str], tuple[list[float], list[float]]]
HORIZONS_H = (("4h", 4), ("12h", 12), ("24h", 24))


def series_confidence(state: WorldState, station_id: str, n_obs: int, noise: float, event_nearby: bool) -> float:
    ticks_per_day = hours_to_ticks(state, 24)
    conf = 0.95
    if n_obs < ticks_per_day:
        conf -= 0.3 * (1 - n_obs / ticks_per_day)  # still leaning on the spec prior
    if state.stale:
        conf -= 0.3
    if event_nearby:
        conf -= 0.15
    conf -= max(0.0, noise - 0.12)  # unusually noisy series
    return round(min(0.99, max(0.05, conf)), 3)


def score_risks(
    state: WorldState,
    forecasts: Forecasts,
    n_obs: dict[tuple[str, str], int] | None = None,
    noise: dict[tuple[str, str], float] | None = None,
) -> list[RiskScore]:
    idx = WorldIndex(state)
    t0 = state.instance.tick
    arrivals = in_transit_arrivals(state)
    n_obs = n_obs or {}
    noise = noise or {}
    horizon_24h = hours_to_ticks(state, 24)
    out: list[RiskScore] = []
    for st in state.stations:
        live_events = [e for e in state.events if e.status in LIVE_EVENT and e.type in ("demand_spike", "station_outage")
                       and event_hits_station(e, st)]
        routes = idx.routes_to.get(st.id, [])
        for fuel in spec.FUELS:
            if (st.id, fuel) not in forecasts:
                continue
            mu, sigma = forecasts[(st.id, fuel)]
            h = len(mu)
            on_hand = st.inventory.get(fuel, 0.0)
            cap = st.capacity.get(fuel, 0.0)
            arr_abs = arrivals.get((st.id, fuel), {})
            arr = relative_arrivals(arr_abs, t0)
            in_transit = sum(arr_abs.values())
            open_at = [station_open_at(state, st, t0 + k) for k in range(1, h + 1)]
            _, _, first_short = project_series(on_hand, cap, mu, arr, open_at)
            probs = {label: round(p_stockout(on_hand, mu, sigma, arr, hours_to_ticks(state, hrs)), 4)
                     for label, hrs in HORIZONS_H}
            notes: list[str] = []
            if st.status != STATION_OPEN:
                notes.append(f"station {st.status.lower()}: no fuel is served")
                probs = {k: 1.0 for k in probs}
            if st.demand_multiplier > 1.05:
                notes.append(f"demand multiplier x{st.demand_multiplier:.2f}")
            for e in live_events:
                notes.append(f"{e.type} {e.status.lower()} ticks {e.start_tick}-{e.end_tick}")
            usable = [r for r in routes if route_available_at(state, r, t0)]
            if len(routes) == 1:
                notes.append("single-route station")
            if routes and not usable:
                notes.append("no route available")
            if in_transit > 0:
                notes.append(f"{in_transit:.0f} L in transit")
            out.append(RiskScore(
                station_id=st.id, fuel_type=fuel, station_status=st.status, on_hand=on_hand, in_transit=in_transit,
                capacity=cap, inventory_position=on_hand + in_transit,
                hours_to_stockout=round(ticks_to_hours(state, first_short), 2) if first_short else None,
                stockout_tick=t0 + first_short if first_short else None,
                p_stockout=probs,
                demand_next_24h=round(sum(mu[:horizon_24h]), 1),
                confidence=series_confidence(state, st.id, n_obs.get((st.id, fuel), 0),
                                             noise.get((st.id, fuel), spec.noise(st.demand_profile)),
                                             bool(live_events)),
                notes=notes,
            ))
    return out


# --------------------------------------------------------- projections / what-if


def risk_snapshot(state: WorldState, st, fuel: str, fc: Forecasts, arr: dict[int, float], horizon: int) -> RiskSnapshot:
    """Risk of one series over the planning horizon, given relative arrivals `arr`."""
    mu, sigma = fc[(st.id, fuel)]
    t0 = state.instance.tick
    on_hand, cap = st.inventory.get(fuel, 0.0), st.capacity.get(fuel, 0.0)
    open_at = [station_open_at(state, st, t0 + k) for k in range(1, len(mu) + 1)]
    _, _, first = project_series(on_hand, cap, mu, arr, open_at)
    inv_h, unmet_h, _ = project_series(on_hand, cap, mu[:horizon], arr, open_at[:horizon])
    p = 1.0 if st.status != "OPEN" else p_stockout(on_hand, mu, sigma, arr, horizon)
    return RiskSnapshot(p_stockout=round(p, 4), horizon_hours=round(ticks_to_hours(state, horizon), 2),
                        hours_to_stockout=round(ticks_to_hours(state, first), 2) if first else None,
                        projected_unmet_liters=round(unmet_h, 1), end_inventory_liters=round(inv_h[-1], 1))


def series_unmet(state: WorldState, fc: Forecasts, extra: dict[tuple[str, str], dict[int, float]], horizon: int) -> float:
    """Total projected unmet liters over the horizon (mean demand) with extra relative arrivals."""
    t0 = state.instance.tick
    arrivals = in_transit_arrivals(state)
    total = 0.0
    for st in state.stations:
        for f in spec.FUELS:
            if (st.id, f) not in fc:
                continue
            mu = fc[(st.id, f)][0][:horizon]
            arr = dict(relative_arrivals(arrivals.get((st.id, f), {}), t0))
            for k, q in extra.get((st.id, f), {}).items():
                arr[k] = arr.get(k, 0.0) + q
            open_at = [station_open_at(state, st, t0 + k) for k in range(1, len(mu) + 1)]
            total += project_series(st.inventory.get(f, 0.0), st.capacity.get(f, 0.0), mu, arr, open_at)[1]
    return total


def _project(state: WorldState, fc: Forecasts, extra: dict[tuple[str, str], dict[int, float]], horizon: int):
    t0 = state.instance.tick
    arrivals = in_transit_arrivals(state)
    out: list[SeriesProjection] = []
    total = 0.0
    for st in state.stations:
        for f in spec.FUELS:
            if (st.id, f) not in fc:
                continue
            mu, sigma = fc[(st.id, f)]
            mu, sigma = mu[:horizon], sigma[:horizon]
            arr = dict(relative_arrivals(arrivals.get((st.id, f), {}), t0))
            for k, q in extra.get((st.id, f), {}).items():
                arr[k] = arr.get(k, 0.0) + q
            open_at = [station_open_at(state, st, t0 + k) for k in range(1, len(mu) + 1)]
            inv, unmet, first = project_series(st.inventory.get(f, 0.0), st.capacity.get(f, 0.0), mu, arr, open_at)
            p12 = 1.0 if st.status != "OPEN" else p_stockout(st.inventory.get(f, 0.0), mu, sigma, arr,
                                                             hours_to_ticks(state, 12))
            total += unmet
            out.append(SeriesProjection(station_id=st.id, fuel_type=f, inventory=[round(v, 1) for v in inv],
                                        unmet_total=round(unmet, 1), stockout_tick=t0 + first if first else None,
                                        p_stockout_12h=round(p12, 4)))
    return out, total


def evaluate(state: WorldState, fc: Forecasts, lines: list[FixedLine], horizon: int) -> EvaluateResponse:
    idx = WorldIndex(state)
    extra: dict[tuple[str, str], dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for line in lines:
        route = idx.routes.get(line.route_id)
        if route is None:
            raise ValueError(f"unknown route {line.route_id}")
        extra[(route.destination_station_id, line.fuel_type)][route.transit_ticks + 1] += line.quantity
    before, unmet_before = _project(state, fc, {}, horizon)
    after, unmet_after = _project(state, fc, extra, horizon)
    return EvaluateResponse(before=before, after=after, unmet_before=round(unmet_before, 1),
                            unmet_after=round(unmet_after, 1))
