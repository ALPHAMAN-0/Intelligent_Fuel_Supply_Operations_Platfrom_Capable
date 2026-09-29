"""Heuristic fallback policy (architecture.md §5.5) — also the benchmark baseline.

Greedy order-up-to on the spec-prior forecast: rank (station, fuel) by time-to-stockout, ship from the shortest
usable route up to a target covering lead time + cover window. No learned model, no optimizer: it keeps decisions
flowing when the intelligence service is down.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict

from fuelops.shared import spec
from fuelops.shared.models import (
    CycleResponse,
    ModelInfo,
    PlannerParams,
    PlanLine,
    PlanResult,
    SeriesForecast,
    WorldState,
)
from fuelops.shared.risk import risk_snapshot, score_risks, series_unmet
from fuelops.shared.world import (
    WorldIndex,
    committed_dispatch,
    in_transit_arrivals,
    prior_forecast,
    project_series,
    relative_arrivals,
    route_usable,
    station_open_at,
    supply_etas,
    ticks_to_hours,
)

UNIT = 100.0


def heuristic_plan(state: WorldState, fc, params: PlannerParams, cover_ticks: int = 16,
                   confidence: dict[tuple[str, str], float] | None = None) -> PlanResult:
    start = time.perf_counter()
    idx = WorldIndex(state)
    t0 = state.instance.tick
    arrivals = in_transit_arrivals(state)
    committed = dict(params.committed_dispatch) or committed_dispatch(state)
    depot_left = {d.id: dict(d.inventory) for d in state.depots}
    dispatch_left = {d.id: max(0.0, d.dispatch_capacity_per_tick - committed.get(d.id, 0.0)) for d in state.depots}
    confidence = confidence or {}

    ranked = []
    for st in state.stations:
        if not station_open_at(state, st, t0):
            continue
        for f in spec.FUELS:
            if (st.id, f) not in fc:
                continue
            mu = fc[(st.id, f)][0]
            arr = relative_arrivals(arrivals.get((st.id, f), {}), t0)
            _, _, first = project_series(st.inventory.get(f, 0.0), st.capacity.get(f, 0.0), mu, arr)
            ranked.append((first if first is not None else math.inf, st, f, arr))
    ranked.sort(key=lambda x: x[0])

    lines: list[PlanLine] = []
    extra: dict[tuple[str, str], dict[int, float]] = defaultdict(lambda: defaultdict(float))
    sent_to: dict[tuple[str, str], float] = defaultdict(float)
    for _first, st, f, arr in ranked:
        routes = sorted((r for r in idx.routes_to.get(st.id, []) if route_usable(state, idx, r, t0)),
                        key=lambda r: (r.transit_ticks, idx.is_cross_region(r)))
        if not routes:
            continue
        mu = fc[(st.id, f)][0]
        lead = routes[0].transit_ticks + 1
        cap = st.capacity.get(f, 0.0)
        on_hand = st.inventory.get(f, 0.0)
        target = min(cap, sum(mu[: lead + cover_ticks]))
        need = target - on_hand - sum(arr.values())
        for r in routes:
            if need < params.min_shipment_l:
                break
            q = min(need, params.max_parts_per_route * r.max_shipment, depot_left[r.source_depot_id].get(f, 0.0),
                    dispatch_left[r.source_depot_id], cap - on_hand - sent_to[(st.id, f)])
            q = math.floor(q / UNIT) * UNIT
            if q < params.min_shipment_l:
                continue
            parts = []
            remaining = q
            while remaining >= r.max_shipment:
                parts.append(float(r.max_shipment))
                remaining -= r.max_shipment
            if remaining >= params.min_shipment_l:
                parts.append(float(remaining))
            q = sum(parts)
            if q <= 0:
                continue
            depot_left[r.source_depot_id][f] -= q
            dispatch_left[r.source_depot_id] -= q
            sent_to[(st.id, f)] += q
            need -= q
            k_arr = r.transit_ticks + 1
            extra[(st.id, f)][k_arr] += q
            before = risk_snapshot(state, st, f, fc, dict(arr), params.horizon_ticks)
            with_arr = dict(arr)
            with_arr[k_arr] = with_arr.get(k_arr, 0.0) + q
            after = risk_snapshot(state, st, f, fc, with_arr, params.horizon_ticks)
            signals = [f"P(stockout within {before.horizon_hours:.0f} h) {before.p_stockout:.0%}",
                       "heuristic order-up-to (fallback policy)"]
            if before.hours_to_stockout is not None:
                signals.insert(0, f"stockout in {before.hours_to_stockout:.1f} h without action")
            lines.append(PlanLine(
                line_no=len(lines) + 1, route_id=r.id, depot_id=r.source_depot_id, station_id=st.id, fuel_type=f,
                quantity=q, parts=parts, arrival_tick=t0 + k_arr,
                reason=(f"Top up {st.id} {f} toward {target:.0f} L (lead {lead} ticks + "
                        f"{ticks_to_hours(state, cover_ticks):.0f} h cover) via the shortest usable route"),
                signals=signals, binding=[], risk_before=before, risk_after=after, alternatives=[],
                confidence=min(confidence.get((st.id, f), 0.6), 0.6),
            ))

    result = PlanResult(policy="heuristic", tick=t0, horizon_ticks=params.horizon_ticks, lines=lines,
                        solver_status="HEURISTIC", solve_ms=round((time.perf_counter() - start) * 1000, 2))
    result.projected_unmet_before = round(series_unmet(state, fc, {}, params.horizon_ticks), 1)
    result.projected_unmet_after = round(series_unmet(state, fc, extra, params.horizon_ticks), 1)
    result.notes.append("Fallback policy: spec-prior forecast and greedy order-up-to; no optimizer")
    return result


def heuristic_cycle(state: WorldState, params: PlannerParams,
                    first_seen_quantity: dict[str, float] | None = None) -> CycleResponse:
    """Everything the UI needs for one decision cycle, computed without the intelligence service."""
    horizon = max(params.horizon_ticks, params.risk_horizon_ticks)
    fc = prior_forecast(state, horizon)
    risks = score_risks(state, fc)
    conf = {(r.station_id, r.fuel_type): r.confidence for r in risks}
    plan = heuristic_plan(state, fc, params, confidence=conf)
    forecasts = [SeriesForecast(station_id=sid, fuel_type=f, first_tick=state.instance.tick + 1,
                                mu=[round(v, 3) for v in mu], sigma=[round(v, 3) for v in sigma], source="prior",
                                n_obs=0, noise=round(sigma[0] / mu[0], 4) if mu and mu[0] else 0.0)
                 for (sid, f), (mu, sigma) in fc.items()]
    return CycleResponse(forecasts=forecasts, risks=risks, supply_etas=supply_etas(state, first_seen_quantity),
                         plan=plan, model=ModelInfo(version="spec-prior", kind="fallback", fitted_series=len(fc),
                                                    observations=0, noise_floor=0.0))
