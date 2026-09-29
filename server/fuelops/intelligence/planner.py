"""Rolling-horizon LP planner (DEC-07, architecture.md §5.3). Solver: PuLP + CBC (ADR-013).

Plans shipments for the next H ticks, returns only the shipments to create now (t = 0), and is re-run every tick.
The objective is projected unmet liters, i.e. the judged service level. Shipment lag = 1 + transit_ticks
(created at t, departs t + 1, arrives t + 1 + transit).
"""

from __future__ import annotations

import math
import time
from collections import defaultdict

import pulp

from fuelops.shared import spec
from fuelops.shared.models import Alternative, PlannerParams, PlanLine, PlanResult, RiskSnapshot, WorldState
from fuelops.shared.world import (
    WorldIndex,
    committed_dispatch,
    event_hits,
    event_hits_station,
    hours_to_ticks,
    in_transit_arrivals,
    p_stockout,
    project_series,
    relative_arrivals,
    route_available_at,
    route_usable,
    station_open_at,
    supply_etas,
    ticks_to_hours,
)

Forecasts = dict[tuple[str, str], tuple[list[float], list[float]]]
UNIT = 100.0  # shipments are rounded down to 100 L


def split_parts(quantity: float, max_shipment: float, min_shipment: float) -> list[float]:
    """Split a quantity into allocations of at most max_shipment; drop a remainder below min_shipment."""
    parts: list[float] = []
    remaining = math.floor(quantity / UNIT + 1e-9) * UNIT
    while remaining >= max_shipment:
        parts.append(float(max_shipment))
        remaining -= max_shipment
    if remaining >= max(min_shipment, UNIT):
        parts.append(float(remaining))
    return parts


class _Model:
    """Builds and solves the LP."""

    def __init__(self, state: WorldState, fc: Forecasts, params: PlannerParams):
        self.state, self.fc, self.params = state, fc, params
        self.idx = WorldIndex(state)
        self.t0 = state.instance.tick
        self.H = params.horizon_ticks
        self.prob = pulp.LpProblem("fuel_plan", pulp.LpMinimize)
        self.x: dict[tuple[str, str, int], pulp.LpVariable] = {}
        self.u: dict[tuple[str, str, int], pulp.LpVariable] = {}
        self.I: dict[tuple[str, str, int], pulp.LpVariable] = {}
        self.J: dict[tuple[str, str, int], pulp.LpVariable] = {}
        self.excluded_routes: dict[str, str] = {}
        self.committed = dict(params.committed_dispatch) or committed_dispatch(state)
        self._n = 0
        self._build()

    def _var(self, prefix: str, low: float = 0.0, up: float | None = None) -> pulp.LpVariable:
        self._n += 1
        return pulp.LpVariable(f"{prefix}{self._n}", lowBound=low, upBound=up)

    def _build(self) -> None:
        prob, idx, H, t0, p = self.prob, self.idx, self.H, self.t0, self.params
        objective_terms = []

        for r in self.state.routes:
            lag = r.transit_ticks + 1
            if not route_available_at(self.state, r, t0):
                self.excluded_routes[r.id] = f"{r.id} unavailable ({r.status.lower()})"
            for j in range(H):
                if j + lag > H or not route_usable(self.state, idx, r, t0 + j):
                    continue
                for f in spec.FUELS:
                    v = self._var("x", 0.0, p.max_parts_per_route * r.max_shipment)
                    self.x[(r.id, f, j)] = v
                    cost = 1e-4 * (lag + (2 if idx.is_cross_region(r) else 0)) + 1e-6 * j
                    objective_terms.append(cost * v)

        arrivals = in_transit_arrivals(self.state)
        for st in self.state.stations:
            for f in spec.FUELS:
                if (st.id, f) not in self.fc:
                    continue
                mu, sigma = self.fc[(st.id, f)]
                cap = st.capacity.get(f, 0.0)
                on_hand = st.inventory.get(f, 0.0)
                arr = relative_arrivals(arrivals.get((st.id, f), {}), t0)
                weight = 1.0 + p.priority_weights.get(f"{st.id}:{f}", p.priority_weights.get(st.id, 0.0))
                prev: float | pulp.LpVariable = on_hand
                upper = max(cap, on_hand)
                for k in range(1, H + 1):
                    is_open = station_open_at(self.state, st, t0 + k)
                    d = (mu[k - 1] + p.safety_z * sigma[k - 1]) if (is_open and k - 1 < len(mu)) else 0.0
                    I_k = self._var("I", 0.0, upper)
                    u_k = self._var("u", 0.0, d)
                    w_k = self._var("w", 0.0, None)  # spill if already-committed arrivals overflow the tank
                    inflow = [self.x[(r.id, f, k - r.transit_ticks - 1)] for r in idx.routes_to.get(st.id, [])
                              if (r.id, f, k - r.transit_ticks - 1) in self.x]
                    prob += I_k == prev + arr.get(k, 0.0) + pulp.lpSum(inflow) - d + u_k - w_k
                    self.I[(st.id, f, k)] = I_k
                    self.u[(st.id, f, k)] = u_k
                    objective_terms += [weight * u_k, 1.0 * w_k]
                    prev = I_k
                # The simulator checks station.inventory + quantity <= capacity when an allocation is created.
                for j in range(H):
                    created = [self.x[(r.id, f, j)] for r in idx.routes_to.get(st.id, []) if (r.id, f, j) in self.x]
                    if not created:
                        continue
                    if j == 0:
                        prob += pulp.lpSum(created) <= max(0.0, cap - on_hand)
                    else:
                        prob += self.I[(st.id, f, j)] + pulp.lpSum(created) <= cap
                cover = min(p.terminal_cover_ticks, len(mu))
                if cover and p.terminal_weight > 0:
                    target = min(0.6 * cap, sum(mu[H - cover:H]) if len(mu) >= H else sum(mu[-cover:]))
                    v = self._var("v", 0.0, None)
                    prob += v >= target - self.I[(st.id, f, H)]
                    objective_terms.append(p.terminal_weight * v)

        supply: dict[tuple[str, str, int], float] = defaultdict(float)
        for e in supply_etas(self.state):
            j = max(1, e.eta_tick - t0)
            if j < H:
                supply[(e.depot_id, e.fuel_type, j)] += e.expected_quantity

        for d in self.state.depots:
            for f in spec.FUELS:
                prev_j: float | pulp.LpVariable = d.inventory.get(f, 0.0)
                for j in range(H):
                    out = [self.x[(r.id, f, j)] for r in idx.routes_from.get(d.id, []) if (r.id, f, j) in self.x]
                    J_j = self._var("J", 0.0, None)
                    prob += J_j == prev_j + supply[(d.id, f, j)] - pulp.lpSum(out)
                    self.J[(d.id, f, j)] = J_j
                    prev_j = J_j
            for j in range(H):
                out = [self.x[(r.id, f, j)] for r in idx.routes_from.get(d.id, []) for f in spec.FUELS
                       if (r.id, f, j) in self.x]
                if out:
                    limit = d.dispatch_capacity_per_tick - (self.committed.get(d.id, 0.0) if j == 0 else 0.0)
                    prob += pulp.lpSum(out) <= max(0.0, limit)

        prob += pulp.lpSum(objective_terms)

    def solve(self) -> tuple[str, float, float]:
        start = time.perf_counter()
        status = self.prob.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=20))
        elapsed = (time.perf_counter() - start) * 1000
        name = pulp.LpStatus.get(status, str(status)).upper()
        name = {"OPTIMAL": "OPTIMAL", "NOT SOLVED": "NOT_SOLVED"}.get(name, name)
        obj = pulp.value(self.prob.objective) if name == "OPTIMAL" else float("nan")
        return name, float(obj or 0.0), elapsed

    @staticmethod
    def _val(v: pulp.LpVariable) -> float:
        return float(v.varValue or 0.0)

    def now_shipments(self) -> dict[tuple[str, str], float]:
        return {(rid, f): self._val(var) for (rid, f, j), var in self.x.items() if j == 0}

    def unmet_by_series(self) -> dict[tuple[str, str], float]:
        out: dict[tuple[str, str], float] = defaultdict(float)
        for (sid, f, _k), var in self.u.items():
            out[(sid, f)] += self._val(var)
        return out


def _snapshot(state: WorldState, st, fuel: str, fc: Forecasts, arr: dict[int, float], horizon: int) -> RiskSnapshot:
    mu, sigma = fc[(st.id, fuel)]
    t0 = state.instance.tick
    on_hand, cap = st.inventory.get(fuel, 0.0), st.capacity.get(fuel, 0.0)
    open_at = [station_open_at(state, st, t0 + k) for k in range(1, len(mu) + 1)]
    _, _, first = project_series(on_hand, cap, mu, arr, open_at)
    _, unmet_h, _ = project_series(on_hand, cap, mu[:horizon], arr, open_at[:horizon])
    p12 = 1.0 if st.status != "OPEN" else p_stockout(on_hand, mu, sigma, arr, hours_to_ticks(state, 12))
    return RiskSnapshot(p_stockout_12h=round(p12, 4),
                        hours_to_stockout=round(ticks_to_hours(state, first), 2) if first else None,
                        projected_unmet_liters=round(unmet_h, 1))


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


def _signals(state: WorldState, idx: WorldIndex, route, st, fuel: str, before: RiskSnapshot, demand_h: float,
             cover_h: float, in_transit: float) -> list[str]:
    signals = []
    if before.hours_to_stockout is not None:
        signals.append(f"stockout in {before.hours_to_stockout:.1f} h without action")
    signals.append(f"P(stockout 12h) {before.p_stockout_12h:.0%}")
    signals.append(f"forecast demand {demand_h:.0f} L over {cover_h:.0f} h")
    if st.demand_multiplier > 1.05:
        signals.append(f"demand multiplier x{st.demand_multiplier:.2f}")
    if in_transit:
        signals.append(f"{in_transit:.0f} L already in transit")
    others = idx.routes_to.get(st.id, [])
    if len(others) == 1:
        signals.append("single-route station")
    for ev in state.events:
        if ev.status not in ("SCHEDULED", "ACTIVE"):
            continue
        relevant = (
            (ev.type in ("demand_spike", "station_outage") and event_hits_station(ev, st))
            or (ev.type == "route_disruption" and any(event_hits(ev, "route_ids", o.id) for o in others))
            or (ev.type in ("shipment_delay", "supply_shortfall", "depot_constraint")
                and event_hits(ev, "depot_ids", route.source_depot_id))
        )
        if relevant:
            signals.append(f"{ev.status.lower()} {ev.type} ticks {ev.start_tick}-{ev.end_tick}")
    if idx.is_cross_region(route):
        signals.append("cross-region reroute")
    return signals


def plan(
    state: WorldState,
    fc: Forecasts,
    params: PlannerParams,
    confidence: dict[tuple[str, str], float] | None = None,
) -> PlanResult:
    confidence = confidence or {}
    t0 = state.instance.tick
    H = params.horizon_ticks
    model = _Model(state, fc, params)
    status, objective, solve_ms = model.solve()
    result = PlanResult(policy="planner", tick=t0, horizon_ticks=H, solver_status=status, solve_ms=round(solve_ms, 2))
    if status != "OPTIMAL":
        result.notes.append(f"LP status {status}: no plan")
        return result
    result.objective = round(objective, 3)
    idx = model.idx
    arrivals = in_transit_arrivals(state)

    extra: dict[tuple[str, str], dict[int, float]] = defaultdict(lambda: defaultdict(float))
    chosen: list[tuple] = []
    for (rid, f), q in sorted(model.now_shipments().items(), key=lambda kv: -kv[1]):
        r = idx.routes[rid]
        parts = split_parts(q, r.max_shipment, params.min_shipment_l)
        qty = sum(parts)
        if qty <= 0:
            continue
        k_arr = r.transit_ticks + 1
        extra[(r.destination_station_id, f)][k_arr] += qty
        chosen.append((r, f, qty, parts, k_arr))

    unmet_after = series_unmet(state, fc, extra, H)
    result.projected_unmet_before = round(series_unmet(state, fc, {}, H), 1)
    result.projected_unmet_after = round(unmet_after, 1)

    used: dict[str, float] = defaultdict(float)
    for r, _f, qty, _parts, _k in chosen:
        used[r.source_depot_id] += qty
    for d in state.depots:
        limit = d.dispatch_capacity_per_tick - model.committed.get(d.id, 0.0)
        if limit > 0 and used[d.id] >= limit - UNIT - 1:
            result.binding_constraints.append(
                f"Dispatch capacity binding at {d.id}: {used[d.id]:.0f} of {limit:.0f} L available this tick")
        for f in spec.FUELS:
            J0 = model.J.get((d.id, f, 0))
            if J0 is not None and model._val(J0) <= UNIT and used[d.id] > 0:
                result.binding_constraints.append(f"{d.id} {f} stock fully committed by this plan")
    for reason in model.excluded_routes.values():
        result.binding_constraints.append(f"Route {reason}; excluded from the plan")

    for (sid, f), u in sorted(model.unmet_by_series().items(), key=lambda kv: -kv[1]):
        if u > 50:
            result.rationing.append(f"{sid} {f}: {u:.0f} L shortfall at planning demand within "
                                    f"{ticks_to_hours(state, H):.0f} h even with the best plan")

    lines: list[PlanLine] = []
    for n, (r, f, qty, parts, k_arr) in enumerate(chosen, start=1):
        st = idx.stations[r.destination_station_id]
        base_arr = dict(relative_arrivals(arrivals.get((st.id, f), {}), t0))
        with_arr = dict(base_arr)
        for k, q in extra[(st.id, f)].items():
            with_arr[k] = with_arr.get(k, 0.0) + q
        before = _snapshot(state, st, f, fc, base_arr, H)
        after = _snapshot(state, st, f, fc, with_arr, H)
        mu = fc[(st.id, f)][0]
        cover_h = ticks_to_hours(state, min(H, len(mu)))
        demand_h = sum(mu[:H])
        binding = []
        if any(r.source_depot_id in b and "Dispatch" in b for b in result.binding_constraints):
            binding.append(f"dispatch capacity at {r.source_depot_id}")
        if st.inventory.get(f, 0.0) + qty >= st.capacity.get(f, 0.0) - UNIT:
            binding.append(f"{st.id} {f} tank capacity")
        if qty >= params.max_parts_per_route * r.max_shipment - UNIT:
            binding.append(f"{r.id} max shipment")
        signals = _signals(state, idx, r, st, f, before, demand_h, cover_h, sum(base_arr.values()))

        alternatives: list[Alternative] = []
        if params.alternatives:
            skip_extra = {key: dict(v) for key, v in extra.items()}
            skip_extra[(st.id, f)][k_arr] = skip_extra[(st.id, f)].get(k_arr, 0.0) - qty
            delta = series_unmet(state, fc, skip_extra, H) - unmet_after
            alternatives.append(Alternative(description="Skip this shipment", delta_unmet_liters=round(delta, 1)))
            for alt in idx.routes_to.get(st.id, []):
                if alt.id == r.id or not route_usable(state, idx, alt, t0):
                    continue
                alt_qty = min(qty, params.max_parts_per_route * alt.max_shipment,
                              idx.depots[alt.source_depot_id].inventory.get(f, 0.0))
                if alt_qty <= 0:
                    continue
                alt_extra = {key: dict(v) for key, v in skip_extra.items()}
                alt_k = alt.transit_ticks + 1
                alt_extra[(st.id, f)][alt_k] = alt_extra[(st.id, f)].get(alt_k, 0.0) + alt_qty
                delta = series_unmet(state, fc, alt_extra, H) - unmet_after
                alternatives.append(Alternative(
                    description=f"Ship {alt_qty:.0f} L via {alt.id} (arrives tick {t0 + alt_k})",
                    route_id=alt.id, delta_unmet_liters=round(delta, 1)))

        reason = (f"Ship {qty:.0f} L {f} {r.source_depot_id} -> {st.id}, arriving tick {t0 + k_arr}: "
                  f"projected unmet over {cover_h:.0f} h falls from {before.projected_unmet_liters:.0f} L "
                  f"to {after.projected_unmet_liters:.0f} L")
        lines.append(PlanLine(
            line_no=n, route_id=r.id, depot_id=r.source_depot_id, station_id=st.id, fuel_type=f, quantity=qty,
            parts=parts, arrival_tick=t0 + k_arr, reason=reason, signals=signals, binding=binding,
            risk_before=before, risk_after=after, alternatives=alternatives,
            confidence=confidence.get((st.id, f), 0.8),
        ))
    result.lines = lines
    if not lines:
        result.notes.append("No shipment needed this tick: projected stock covers planning demand")
    return result
