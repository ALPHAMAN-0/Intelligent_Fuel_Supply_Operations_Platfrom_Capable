"""What-if evaluator: projects every series with and without a fixed set of shipments created now."""

from __future__ import annotations

from collections import defaultdict

from fuelops.shared import spec
from fuelops.shared.models import EvaluateResponse, FixedLine, SeriesProjection, WorldState
from fuelops.shared.world import (
    WorldIndex,
    hours_to_ticks,
    in_transit_arrivals,
    p_stockout,
    project_series,
    relative_arrivals,
    station_open_at,
)

Forecasts = dict[tuple[str, str], tuple[list[float], list[float]]]


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
