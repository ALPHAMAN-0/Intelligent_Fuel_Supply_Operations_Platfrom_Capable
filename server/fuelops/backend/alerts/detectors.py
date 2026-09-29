"""Detectors (architecture.md §4.4). Pure functions: world state in, alert candidates out.

They run in the backend so alerts keep flowing when the intelligence service is down.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from fuelops.shared.models import DemandPoint, RiskScore, WorldState
from fuelops.shared.sim_models import DEPOT_SHIPPABLE, ROUTE_AVAILABLE, STATION_OPEN
from fuelops.shared.world import LIVE_EVENT, WorldIndex, event_hits, event_hits_fuel, event_hits_station, supply_etas

DISRUPTIVE = {"route_disruption", "station_outage"}


@dataclass
class Candidate:
    key: str
    kind: str
    severity: str
    title: str
    entity_type: str | None = None
    entity_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    incident_key: str | None = None
    incident_title: str | None = None


def _targets(params: dict) -> str:
    parts = []
    for key in ("station_ids", "region_ids", "route_ids", "depot_ids", "fuel_types"):
        if params.get(key):
            parts.append(", ".join(map(str, params[key])))
    return "; ".join(parts) or "all entities"


def detect_events(state: WorldState) -> list[Candidate]:
    out = []
    for ev in state.events:
        if ev.status not in LIVE_EVENT:
            continue
        active = ev.status == "ACTIVE"
        severity = "critical" if active and ev.type in DISRUPTIVE else "warning"
        verb = "Active" if active else "Scheduled"
        title = f"{verb} {ev.type.replace('_', ' ')} on {_targets(ev.parameters)} (ticks {ev.start_tick}-{ev.end_tick})"
        out.append(Candidate(key=f"event:{ev.id}", kind="event", severity=severity, title=title, entity_type="event",
                             entity_id=str(ev.id), detail={"type": ev.type, "status": ev.status,
                                                           "parameters": ev.parameters, "start_tick": ev.start_tick,
                                                           "end_tick": ev.end_tick},
                             incident_key=f"event:{ev.id}", incident_title=title))
    return out


def _event_incident(state: WorldState, types: set[str], key: str, entity_id: str) -> str | None:
    for ev in sorted(state.events, key=lambda e: -e.id):
        if ev.type in types and ev.status in LIVE_EVENT | {"RESOLVED"} and event_hits(ev, key, entity_id):
            return f"event:{ev.id}"
    return None


def detect_network(state: WorldState) -> list[Candidate]:
    idx = WorldIndex(state)
    out = []
    for r in state.routes:
        if r.status == ROUTE_AVAILABLE:
            continue
        alternatives = [o.id for o in idx.routes_to.get(r.destination_station_id, [])
                        if o.id != r.id and o.status == ROUTE_AVAILABLE]
        severity = "warning" if alternatives else "critical"
        title = (f"Route {r.id} {r.status.lower()}"
                 + (f"; reroute via {', '.join(alternatives)}" if alternatives
                    else f"; no alternative route to {r.destination_station_id}"))
        inc = _event_incident(state, {"route_disruption"}, "route_ids", r.id) or f"route:{r.id}"
        out.append(Candidate(key=f"route:{r.id}", kind="network", severity=severity, title=title, entity_type="route",
                             entity_id=r.id, detail={"status": r.status, "alternatives": alternatives},
                             incident_key=inc, incident_title=title))
    for st in state.stations:
        if st.status != STATION_OPEN:
            title = f"Station {st.id} {st.status.lower()}: no fuel served"
            inc = _event_incident(state, {"station_outage"}, "station_ids", st.id) or f"station:{st.id}"
            out.append(Candidate(key=f"station:{st.id}", kind="network", severity="critical", title=title,
                                 entity_type="station", entity_id=st.id, detail={"status": st.status},
                                 incident_key=inc, incident_title=title))
    for d in state.depots:
        if d.status != "OPEN":
            severity = "warning" if d.status in DEPOT_SHIPPABLE else "critical"
            title = f"Depot {d.id} {d.status.lower()}" + ("" if d.status in DEPOT_SHIPPABLE else ": cannot ship")
            inc = _event_incident(state, {"depot_constraint"}, "depot_ids", d.id) or f"depot:{d.id}"
            out.append(Candidate(key=f"depot:{d.id}", kind="network", severity=severity, title=title,
                                 entity_type="depot", entity_id=d.id, detail={"status": d.status},
                                 incident_key=inc, incident_title=title))
    return out


def detect_supply(state: WorldState, first_seen: dict[str, tuple[int, float]]) -> list[Candidate]:
    out = []
    quantities = {k: v[1] for k, v in first_seen.items()}
    for eta in supply_etas(state, quantities):
        planned0 = first_seen.get(eta.id, (eta.planned_tick, eta.quantity))[0]
        delay = max(eta.eta_tick, eta.planned_tick) - planned0
        problems = []
        if eta.status == "DELAYED" or delay > 0:
            problems.append(f"delayed {delay} ticks (ETA tick {eta.eta_tick})")
        if eta.shortfall:
            problems.append(f"quantity {eta.expected_quantity:.0f} L vs {quantities.get(eta.id, eta.quantity):.0f} L "
                            "first announced")
        if not problems:
            continue
        inc = None
        for ev in sorted(state.events, key=lambda e: -e.id):
            if ev.type in ("shipment_delay", "supply_shortfall") and event_hits(ev, "depot_ids", eta.depot_id) \
                    and event_hits_fuel(ev, eta.fuel_type):
                inc = f"event:{ev.id}"
                break
        title = f"Supply {eta.id} to {eta.depot_id} ({eta.fuel_type}) " + " and ".join(problems)
        out.append(Candidate(key=f"supply:{eta.id}", kind="supply", severity="warning", title=title,
                             entity_type="depot", entity_id=eta.depot_id,
                             detail={"eta_tick": eta.eta_tick, "planned_tick": planned0, "delay_ticks": delay,
                                     "expected_quantity": eta.expected_quantity},
                             incident_key=inc or f"supply:{eta.depot_id}",
                             incident_title=f"Supply disruption at {eta.depot_id}"))
    return out


def detect_shortages(state: WorldState, risks: list[RiskScore]) -> list[Candidate]:
    idx = WorldIndex(state)
    out = []
    for r in risks:
        tts = r.hours_to_stockout
        p4, p12 = r.p_stockout.get("4h", 0.0), r.p_stockout.get("12h", 0.0)
        if r.station_status != STATION_OPEN:
            continue  # covered by the network detector
        if p4 >= 0.5 or (tts is not None and tts <= 2):
            severity = "critical"
        elif p12 >= 0.5 or (tts is not None and tts <= 6):
            severity = "warning"
        else:
            continue
        st = idx.stations.get(r.station_id)
        inc = None
        if st is not None:
            for ev in sorted(state.events, key=lambda e: -e.id):
                if ev.status in LIVE_EVENT and ev.type in ("demand_spike", "station_outage") and event_hits_station(ev, st):
                    inc = f"event:{ev.id}"
                    break
        when = f"stockout in {tts:.1f} h" if tts is not None else "stockout risk"
        title = f"Shortage risk {r.station_id} {r.fuel_type}: {when}, P(12h) {p12:.0%}"
        out.append(Candidate(key=f"shortage:{r.station_id}:{r.fuel_type}", kind="shortage", severity=severity,
                             title=title, entity_type="station", entity_id=r.station_id,
                             detail={"fuel": r.fuel_type, "hours_to_stockout": tts, "p_stockout": r.p_stockout,
                                     "on_hand": r.on_hand, "in_transit": r.in_transit, "notes": r.notes},
                             incident_key=inc or f"shortage:{r.station_id}",
                             incident_title=f"Shortage risk at {r.station_id}"))
    return out


@dataclass
class AnomalyState:
    """Keeps consecutive-breach counters for the residual detector between syncs."""

    streak: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    active_until: dict[tuple[str, str], int] = field(default_factory=dict)
    inventory_until: dict[tuple[str, str], tuple[int, dict]] = field(default_factory=dict)
    last_z: dict[tuple[str, str], float] = field(default_factory=dict)


def detect_demand_anomalies(state: WorldState, z_scores: dict[tuple[str, str], float], memory: AnomalyState,
                            z_threshold: float = 3.0, consecutive: int = 2, hold_ticks: int = 4) -> list[Candidate]:
    tick = state.instance.tick
    idx = WorldIndex(state)
    for key, z in z_scores.items():
        memory.last_z[key] = z
        memory.streak[key] = memory.streak[key] + 1 if abs(z) > z_threshold else 0
        if memory.streak[key] >= consecutive:
            memory.active_until[key] = tick + hold_ticks
    active = {k: until for k, until in memory.active_until.items() if until >= tick}
    memory.active_until = active
    out = []
    by_region: dict[str, set[str]] = defaultdict(set)
    for (sid, fuel) in active:
        st = idx.stations.get(sid)
        z = memory.last_z.get((sid, fuel), 0.0)
        region = st.region_id if st else "unknown"
        by_region[region].add(sid)
        out.append(Candidate(key=f"demand:{sid}:{fuel}", kind="demand_anomaly", severity="warning",
                             title=f"Anomalous demand at {sid} {fuel}: residual z {z:+.1f} vs forecast",
                             entity_type="station", entity_id=sid, detail={"fuel": fuel, "z": round(z, 2)},
                             incident_key=f"region:{region}", incident_title=f"Demand anomaly in {region}"))
    for region, stations in by_region.items():
        if len(stations) >= 2:
            out.append(Candidate(key=f"region:{region}", kind="demand_anomaly", severity="critical",
                                 title=f"Emerging regional disruption in {region}: {len(stations)} stations "
                                       "with unexplained demand",
                                 entity_type="region", entity_id=region, detail={"stations": sorted(stations)},
                                 incident_key=f"region:{region}", incident_title=f"Regional disruption in {region}"))
    return out


def detect_inventory_anomalies(prev: WorldState | None, state: WorldState, new_points: list[DemandPoint],
                               memory: AnomalyState, hold_ticks: int = 4) -> list[Candidate]:
    """Station stock change between consecutive ticks should equal arrivals - served."""
    tick = state.instance.tick
    if prev is not None and state.instance.tick - prev.instance.tick == 1 and prev.run_id == state.run_id:
        served: dict[tuple[str, str], float] = defaultdict(float)
        seen: set[tuple[str, str]] = set()
        for p in new_points:
            if p.tick == tick:
                served[(p.station_id, p.fuel_type)] += p.served if p.served is not None else p.demand
                seen.add((p.station_id, p.fuel_type))
        arrived: dict[tuple[str, str], float] = defaultdict(float)
        for a in state.allocations:
            if a.status == "ARRIVED" and a.actual_arrival_tick == tick:
                arrived[(a.destination_station_id, a.fuel_type)] += a.quantity
        prev_st = {s.id: s for s in prev.stations}
        for st in state.stations:
            before = prev_st.get(st.id)
            if before is None:
                continue
            for fuel, now in st.inventory.items():
                if (st.id, fuel) not in seen:
                    continue
                expected = before.inventory.get(fuel, 0.0) + arrived[(st.id, fuel)] - served[(st.id, fuel)]
                expected = min(expected, st.capacity.get(fuel, expected))
                tolerance = max(300.0, 0.05 * st.capacity.get(fuel, 0.0))
                if abs(now - expected) > tolerance:
                    memory.inventory_until[(st.id, fuel)] = (tick + hold_ticks, {
                        "observed": round(now, 1), "expected": round(expected, 1), "tick": tick})
    memory.inventory_until = {k: v for k, v in memory.inventory_until.items() if v[0] >= tick}
    return [Candidate(key=f"inventory:{sid}:{fuel}", kind="inventory_anomaly", severity="warning",
                      title=f"Unexplained stock change at {sid} {fuel}: {d['observed']:.0f} L vs expected "
                            f"{d['expected']:.0f} L", entity_type="station", entity_id=sid,
                      detail={"fuel": fuel, **d}, incident_key=f"inventory:{sid}",
                      incident_title=f"Inventory anomaly at {sid}")
            for (sid, fuel), (_until, d) in memory.inventory_until.items()]
