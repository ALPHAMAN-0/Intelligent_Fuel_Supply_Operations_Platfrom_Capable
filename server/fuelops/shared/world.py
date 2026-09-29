"""Pure helpers over a WorldState: time, event windows, availability, in-transit fuel, supply ETAs and projections.

Shared by the intelligence service (planner, risk) and the backend (heuristic fallback, detectors) so both reason about
the world the same way.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta

from fuelops.shared import spec
from fuelops.shared.models import SupplyEta, WorldState
from fuelops.shared.sim_models import (
    ALLOCATION_ACTIVE,
    DEPOT_SHIPPABLE,
    ROUTE_AVAILABLE,
    STATION_OPEN,
    Depot,
    Route,
    SimEvent,
    Station,
)

LIVE_EVENT = {"SCHEDULED", "ACTIVE"}


# --------------------------------------------------------------------------- time


def sim_time_at(state: WorldState, tick: int) -> datetime:
    inst = state.instance
    return inst.sim_time + timedelta(minutes=(tick - inst.tick) * inst.tick_minutes)


def hour_at(state: WorldState, tick: int) -> int:
    return sim_time_at(state, tick).hour


def ticks_per_hour(state: WorldState) -> float:
    return 60.0 / state.instance.tick_minutes


def ticks_to_hours(state: WorldState, ticks: float) -> float:
    return ticks * state.instance.tick_minutes / 60.0


def hours_to_ticks(state: WorldState, hours: float) -> int:
    return max(1, round(hours * ticks_per_hour(state)))


# ------------------------------------------------------------------------ indexes


class WorldIndex:
    """Lookup tables for one WorldState."""

    def __init__(self, state: WorldState):
        self.state = state
        self.depots: dict[str, Depot] = {d.id: d for d in state.depots}
        self.stations: dict[str, Station] = {s.id: s for s in state.stations}
        self.routes: dict[str, Route] = {r.id: r for r in state.routes}
        self.region_factor: dict[str, float] = {r.id: r.demand_factor for r in state.regions}
        self.routes_to: dict[str, list[Route]] = defaultdict(list)
        self.routes_from: dict[str, list[Route]] = defaultdict(list)
        for r in state.routes:
            self.routes_to[r.destination_station_id].append(r)
            self.routes_from[r.source_depot_id].append(r)

    def is_cross_region(self, route: Route) -> bool:
        depot = self.depots.get(route.source_depot_id)
        station = self.stations.get(route.destination_station_id)
        return bool(depot and station and depot.region_id != station.region_id)


# ------------------------------------------------------------------ event windows


def _ids(params: dict, key: str) -> list[str]:
    value = params.get(key) or []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def event_hits_station(event: SimEvent, station: Station) -> bool:
    """Filters are lists; an empty list means 'all entities of that type' (guide §7.8)."""
    station_ids = _ids(event.parameters, "station_ids")
    region_ids = _ids(event.parameters, "region_ids")
    if not station_ids and not region_ids:
        return True
    return station.id in station_ids or station.region_id in region_ids


def event_hits(event: SimEvent, key: str, entity_id: str) -> bool:
    ids = _ids(event.parameters, key)
    return not ids or entity_id in ids


def event_hits_fuel(event: SimEvent, fuel: str) -> bool:
    fuels = _ids(event.parameters, "fuel_types")
    return not fuels or fuel in fuels


def in_window(event: SimEvent, tick: int) -> bool:
    return event.start_tick <= tick < event.end_tick


def demand_multiplier_at(state: WorldState, station: Station, tick: int) -> float:
    """Station demand multiplier expected at `tick`, applying live demand_spike events to the current value."""
    m = station.demand_multiplier
    for ev in state.events:
        if ev.type != "demand_spike" or ev.status not in LIVE_EVENT or not event_hits_station(ev, station):
            continue
        factor = float(ev.parameters.get("multiplier", 1.5) or 1.5)
        if factor <= 0:
            continue
        if ev.status == "ACTIVE" and tick >= ev.end_tick:
            m = m / factor
        elif ev.status == "SCHEDULED" and in_window(ev, tick):
            m = m * factor
    return max(m, 0.01)


def route_available_at(state: WorldState, route: Route, tick: int) -> bool:
    events = [e for e in state.events if e.type == "route_disruption" and e.status in LIVE_EVENT
              and event_hits(e, "route_ids", route.id)]
    if route.status != ROUTE_AVAILABLE:
        ends = [e.end_tick for e in events if e.status == "ACTIVE"]
        # Disrupted with no known end: assume unavailable for the whole horizon.
        return bool(ends) and tick >= max(ends)
    return not any(e.status == "SCHEDULED" and in_window(e, tick) for e in events)


def station_open_at(state: WorldState, station: Station, tick: int) -> bool:
    events = [e for e in state.events if e.type == "station_outage" and e.status in LIVE_EVENT
              and event_hits(e, "station_ids", station.id)]
    if station.status != STATION_OPEN:
        ends = [e.end_tick for e in events if e.status == "ACTIVE"]
        return bool(ends) and tick >= max(ends)
    return not any(e.status == "SCHEDULED" and in_window(e, tick) for e in events)


def depot_shippable_at(state: WorldState, depot: Depot, tick: int) -> bool:
    # depot_constraint only sets CONSTRAINED, which is still shippable (guide §7.8).
    return depot.status in DEPOT_SHIPPABLE


def route_usable(state: WorldState, idx: WorldIndex, route: Route, create_tick: int) -> bool:
    """A shipment created at `create_tick` departs at create_tick + 1; the route must be up at both ticks."""
    depot = idx.depots.get(route.source_depot_id)
    station = idx.stations.get(route.destination_station_id)
    if depot is None or station is None:
        return False
    return (
        route_available_at(state, route, create_tick)
        and route_available_at(state, route, create_tick + 1)
        and station_open_at(state, station, create_tick)
        and depot_shippable_at(state, depot, create_tick)
    )


# --------------------------------------------------------------- in-transit fuel


def arrival_tick(state: WorldState, route: Route | None, created_tick: int, expected: int | None = None) -> int:
    if expected is not None:
        return expected
    transit = route.transit_ticks if route else 2
    return created_tick + 1 + transit


def in_transit_arrivals(state: WorldState) -> dict[tuple[str, str], dict[int, float]]:
    """(station, fuel) -> {arrival_tick: liters} for PENDING / IN_TRANSIT allocations."""
    routes = {r.id: r for r in state.routes}
    out: dict[tuple[str, str], dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for a in state.allocations:
        if a.status not in ALLOCATION_ACTIVE:
            continue
        t = arrival_tick(state, routes.get(a.route_id), a.created_tick, a.expected_arrival_tick)
        out[(a.destination_station_id, a.fuel_type)][t] += a.quantity
    return out


def committed_dispatch(state: WorldState) -> dict[str, float]:
    """Liters already dispatched from each depot on the current tick (counts against dispatch capacity)."""
    tick = state.instance.tick
    out: dict[str, float] = defaultdict(float)
    for a in state.allocations:
        if a.status in ALLOCATION_ACTIVE and a.created_tick == tick:
            out[a.source_depot_id] += a.quantity
    return dict(out)


def supply_etas(state: WorldState, first_seen_quantity: dict[str, float] | None = None) -> list[SupplyEta]:
    """Expected supply arrivals, anticipating scheduled shipment_delay / supply_shortfall events."""
    first_seen_quantity = first_seen_quantity or {}
    out: list[SupplyEta] = []
    for sa in state.supply_arrivals:
        if sa.status == "ARRIVED":
            continue
        eta, qty = sa.planned_tick, sa.quantity
        for ev in state.events:
            if ev.status != "SCHEDULED" or not event_hits(ev, "depot_ids", sa.depot_id) or not event_hits_fuel(ev, sa.fuel_type):
                continue
            if ev.start_tick > sa.planned_tick:
                continue
            if ev.type == "shipment_delay":
                eta += int(ev.parameters.get("delay_ticks", 2) or 2)
            elif ev.type == "supply_shortfall":
                qty *= float(ev.parameters.get("factor", 0.5) or 0.5)
        original = first_seen_quantity.get(sa.id, sa.quantity)
        out.append(SupplyEta(
            id=sa.id, depot_id=sa.depot_id, fuel_type=sa.fuel_type, quantity=sa.quantity, expected_quantity=qty,
            planned_tick=sa.planned_tick, eta_tick=eta, status=sa.status,
            delayed=sa.status == "DELAYED" or eta > sa.planned_tick,
            shortfall=qty < original - 1e-6,
        ))
    return out


# ------------------------------------------------------------------ spec prior


def prior_mu(state: WorldState, idx: WorldIndex, station: Station, fuel: str, tick: int) -> float:
    base = spec.prior_per_tick(
        station.demand_profile, fuel, hour_at(state, tick), state.instance.tick_minutes,
        idx.region_factor.get(station.region_id, 1.0),
    )
    return base * demand_multiplier_at(state, station, tick)


def prior_forecast(state: WorldState, horizon: int) -> dict[tuple[str, str], tuple[list[float], list[float]]]:
    """Spec prior x demand_multiplier: the always-available fallback forecast (no history needed)."""
    idx = WorldIndex(state)
    t0 = state.instance.tick
    out = {}
    for st in state.stations:
        k = spec.noise(st.demand_profile)
        for fuel in spec.FUELS:
            mu = [prior_mu(state, idx, st, fuel, t0 + h) for h in range(1, horizon + 1)]
            out[(st.id, fuel)] = (mu, [k * m for m in mu])
    return out


# ------------------------------------------------------------------ projection


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def project_series(
    on_hand: float,
    capacity: float,
    mu: list[float],
    arrivals: dict[int, float],
    open_at: list[bool] | None = None,
) -> tuple[list[float], float, int | None]:
    """Mean projection. `mu[k-1]` is demand at relative tick k; `arrivals[k]` liters arriving at relative tick k.

    Returns (inventory[0..H], total unmet, first relative tick with unmet demand or None).
    """
    inv = [on_hand]
    unmet_total, first_short = 0.0, None
    level = on_hand
    for k in range(1, len(mu) + 1):
        level = min(capacity, level + arrivals.get(k, 0.0))
        demand = mu[k - 1]
        if open_at is not None and not open_at[k - 1]:
            unmet = demand  # outage: nothing is served
        else:
            served = min(level, demand)
            level -= served
            unmet = demand - served
        if unmet > 1e-6:
            unmet_total += unmet
            if first_short is None:
                first_short = k
        inv.append(level)
    return inv, unmet_total, first_short


def p_stockout(on_hand: float, mu: list[float], sigma: list[float], arrivals: dict[int, float], within: int) -> float:
    """P(cumulative demand exceeds on-hand + cumulative arrivals at some tick <= within), normal approximation."""
    best = 0.0
    cum_mu = cum_var = cum_arr = 0.0
    for k in range(1, min(within, len(mu)) + 1):
        cum_mu += mu[k - 1]
        cum_var += sigma[k - 1] ** 2
        cum_arr += arrivals.get(k, 0.0)
        supply = on_hand + cum_arr
        sd = math.sqrt(cum_var)
        if sd <= 1e-9:
            p = 1.0 if cum_mu > supply else 0.0
        else:
            p = 1.0 - normal_cdf((supply - cum_mu) / sd)
        best = max(best, p)
    return min(max(best, 0.0), 1.0)


def relative_arrivals(abs_arrivals: dict[int, float], t0: int) -> dict[int, float]:
    return {t - t0: q for t, q in abs_arrivals.items() if t > t0}


def total(values: Iterable[float]) -> float:
    return float(sum(values))
