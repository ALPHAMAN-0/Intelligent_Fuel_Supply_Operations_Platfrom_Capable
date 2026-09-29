"""Deterministic world engine for the simulator test double (guide §4-§8)."""

from __future__ import annotations

import copy
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fuelops.shared import spec

START = datetime(2026, 1, 1, tzinfo=UTC)
FUELS = ("DIESEL", "PETROL", "OCTANE")


def _f(d: float, p: float, o: float) -> dict[str, float]:
    return {"DIESEL": float(d), "PETROL": float(p), "OCTANE": float(o)}


BASE_REGIONS = [
    {"id": "region-dhaka", "name": "Dhaka Division", "demand_factor": 1.00},
    {"id": "region-chattogram", "name": "Chattogram Division", "demand_factor": 1.08},
]
BASE_DEPOTS = [
    {"id": "depot-gazipur", "name": "Gazipur Depot", "region_id": "region-dhaka", "status": "OPEN",
     "dispatch_capacity_per_tick": 12000, "capacity": _f(90000, 70000, 45000), "inventory": _f(60000, 45000, 26000)},
    {"id": "depot-patiya", "name": "Patiya Depot", "region_id": "region-chattogram", "status": "OPEN",
     "dispatch_capacity_per_tick": 11000, "capacity": _f(85000, 65000, 40000), "inventory": _f(55000, 42000, 24000)},
]
BASE_STATIONS = [
    {"id": "station-mirpur", "name": "Mirpur Fuel Station", "region_id": "region-dhaka", "status": "OPEN",
     "demand_profile": "urban_high", "demand_multiplier": 1.0, "capacity": _f(15000, 14000, 9000),
     "inventory": _f(9000, 9000, 5000)},
    {"id": "station-tongi", "name": "Tongi Fuel Station", "region_id": "region-dhaka", "status": "OPEN",
     "demand_profile": "industrial", "demand_multiplier": 1.0, "capacity": _f(18000, 9000, 6000),
     "inventory": _f(11000, 6000, 3500)},
    {"id": "station-karnaphuli", "name": "Karnaphuli Fuel Station", "region_id": "region-chattogram",
     "status": "OPEN", "demand_profile": "highway", "demand_multiplier": 1.0, "capacity": _f(14000, 15000, 9000),
     "inventory": _f(8500, 9500, 5200)},
    {"id": "station-coxsbazar", "name": "Cox's Bazar Fuel Station", "region_id": "region-chattogram",
     "status": "OPEN", "demand_profile": "regional", "demand_multiplier": 1.0, "capacity": _f(12000, 12000, 7000),
     "inventory": _f(7500, 7500, 4200)},
]
BASE_ROUTES = [
    {"id": "route-gazipur-mirpur", "source_depot_id": "depot-gazipur", "destination_station_id": "station-mirpur",
     "transit_ticks": 2, "max_shipment": 7000},
    {"id": "route-gazipur-tongi", "source_depot_id": "depot-gazipur", "destination_station_id": "station-tongi",
     "transit_ticks": 2, "max_shipment": 6500},
    {"id": "route-patiya-karnaphuli", "source_depot_id": "depot-patiya",
     "destination_station_id": "station-karnaphuli", "transit_ticks": 2, "max_shipment": 7000},
    {"id": "route-patiya-coxsbazar", "source_depot_id": "depot-patiya", "destination_station_id": "station-coxsbazar",
     "transit_ticks": 3, "max_shipment": 6000},
    {"id": "route-gazipur-karnaphuli", "source_depot_id": "depot-gazipur",
     "destination_station_id": "station-karnaphuli", "transit_ticks": 4, "max_shipment": 5000},
    {"id": "route-patiya-mirpur", "source_depot_id": "depot-patiya", "destination_station_id": "station-mirpur",
     "transit_ticks": 4, "max_shipment": 5000},
]


def _supply_schedule() -> list[dict[str, Any]]:
    """22 arrivals: 4 initial-burst arrivals at ticks 12-20, then 18 recurring resupplies 64 ticks apart (§8.7)."""
    rows = [("depot-gazipur", "DIESEL", 18000, 12), ("depot-patiya", "DIESEL", 16000, 14),
            ("depot-gazipur", "PETROL", 16000, 16), ("depot-patiya", "PETROL", 17000, 20)]
    combos = [(d, f) for d in ("depot-gazipur", "depot-patiya") for f in FUELS]
    daily = {"depot-gazipur": {"DIESEL": 22500, "PETROL": 15000, "OCTANE": 7800},
             "depot-patiya": {"DIESEL": 19100, "PETROL": 20100, "OCTANE": 10600}}
    for i in range(18):
        d, f = combos[i % len(combos)]
        rows.append((d, f, daily[d][f] * 3, 64 * (i + 1)))
    return [{"id": f"supply-{n + 1:03d}", "depot_id": d, "fuel_type": f, "quantity": float(q), "planned_tick": t,
             "actual_tick": None, "status": "SCHEDULED"} for n, (d, f, q, t) in enumerate(rows)]


HOUR_OF_DAY = spec.hour_factor


@dataclass
class World:
    seed: int = 12345
    tick_minutes: int = 15
    scenario_id: str = "baseline"
    tick: int = 0
    status: str = "PAUSED"
    regions: list[dict] = field(default_factory=lambda: copy.deepcopy(BASE_REGIONS))
    depots: list[dict] = field(default_factory=lambda: copy.deepcopy(BASE_DEPOTS))
    stations: list[dict] = field(default_factory=lambda: copy.deepcopy(BASE_STATIONS))
    routes: list[dict] = field(default_factory=lambda: [{**r, "status": "AVAILABLE"} for r in BASE_ROUTES])
    supply: list[dict] = field(default_factory=_supply_schedule)
    events: list[dict] = field(default_factory=list)
    allocations: list[dict] = field(default_factory=list)
    demand: list[dict] = field(default_factory=list)
    audit: list[dict] = field(default_factory=list)
    served: float = 0.0
    unmet: float = 0.0
    rng: random.Random = field(default_factory=random.Random)

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)

    # ------------------------------------------------------------ helpers

    @property
    def sim_time(self) -> datetime:
        return START + timedelta(minutes=self.tick * self.tick_minutes)

    def depot(self, i: str) -> dict | None:
        return next((d for d in self.depots if d["id"] == i), None)

    def station(self, i: str) -> dict | None:
        return next((s for s in self.stations if s["id"] == i), None)

    def route(self, i: str) -> dict | None:
        return next((r for r in self.routes if r["id"] == i), None)

    def log(self, action: str, entity_type: str | None = None, entity_id: Any = None, metadata: dict | None = None,
            result: str = "OK") -> None:
        self.audit.append({"id": len(self.audit) + 1, "wall_time": datetime.now(UTC).isoformat(),
                           "sim_time": self.sim_time.isoformat(), "tick": self.tick, "action": action,
                           "entity_type": entity_type, "entity_id": None if entity_id is None else str(entity_id),
                           "result": result, "metadata_json": metadata or {}})

    def instance(self) -> dict:
        return {"id": 1, "scenario_id": self.scenario_id, "scenario_version": "1.0", "seed": self.seed,
                "sim_time": self.sim_time.isoformat(), "tick": self.tick, "tick_minutes": self.tick_minutes,
                "status": self.status}

    def metrics(self) -> dict:
        total = self.served + self.unmet
        return {"served_demand_liters": round(self.served, 3), "unmet_demand_liters": round(self.unmet, 3),
                "service_level": round(self.served / total, 6) if total else 1.0,
                "allocation_liters": round(sum(a["quantity"] for a in self.allocations
                                               if a["status"] in ("IN_TRANSIT", "ARRIVED")), 3),
                "allocation_failures": sum(1 for a in self.allocations if a["status"] == "FAILED")}

    # ------------------------------------------------------------- events

    def _targets(self, ev: dict, kind: str) -> list[dict]:
        p = ev["parameters"]
        if kind == "station":
            sids, rids = p.get("station_ids") or [], p.get("region_ids") or []
            return [s for s in self.stations if (not sids and not rids) or s["id"] in sids or s["region_id"] in rids]
        if kind == "route":
            ids = p.get("route_ids") or []
            return [r for r in self.routes if not ids or r["id"] in ids]
        ids = p.get("depot_ids") or []
        return [d for d in self.depots if not ids or d["id"] in ids]

    def _supply_targets(self, ev: dict) -> list[dict]:
        p = ev["parameters"]
        depots, fuels = p.get("depot_ids") or [], p.get("fuel_types") or []
        return [s for s in self.supply if s["status"] != "ARRIVED" and (not depots or s["depot_id"] in depots)
                and (not fuels or s["fuel_type"] in fuels)]

    def _activate(self, ev: dict) -> None:
        t, p = ev["type"], ev["parameters"]
        if t == "demand_spike":
            m = float(p.get("multiplier", 1.5))
            for s in self._targets(ev, "station"):
                s["demand_multiplier"] = round(s["demand_multiplier"] * m, 6)
        elif t == "route_disruption":
            for r in self._targets(ev, "route"):
                r["status"] = "DISRUPTED"
        elif t == "station_outage":
            for s in self._targets(ev, "station"):
                s["status"] = "OUTAGE"
        elif t == "depot_constraint":
            for d in self._targets(ev, "depot"):
                d["status"] = "CONSTRAINED"
        elif t == "shipment_delay":
            for s in self._supply_targets(ev):
                s["planned_tick"] += int(p.get("delay_ticks", 2))
                s["status"] = "DELAYED"
        elif t == "supply_shortfall":
            for s in self._supply_targets(ev):
                s["quantity"] = round(s["quantity"] * float(p.get("factor", 0.5)), 3)
        ev["status"] = "ACTIVE"
        self.log("event.started", "event", ev["id"], {"type": t})

    def _resolve(self, ev: dict) -> None:
        t, p = ev["type"], ev["parameters"]
        if t == "demand_spike":
            m = float(p.get("multiplier", 1.5))
            for s in self._targets(ev, "station"):
                s["demand_multiplier"] = max(0.01, round(s["demand_multiplier"] / m, 6))
        elif t == "route_disruption":
            for r in self._targets(ev, "route"):
                r["status"] = "AVAILABLE"
        elif t == "station_outage":
            for s in self._targets(ev, "station"):
                s["status"] = "OPEN"
        elif t == "depot_constraint":
            for d in self._targets(ev, "depot"):
                d["status"] = "OPEN"
        ev["status"] = "RESOLVED"
        self.log("event.resolved", "event", ev["id"], {"type": t})

    def add_event(self, type_: str, start_tick: int, duration_ticks: int, parameters: dict) -> dict:
        ev = {"id": len(self.events) + 1, "type": type_, "start_tick": start_tick,
              "end_tick": start_tick + duration_ticks, "status": "SCHEDULED", "parameters": parameters or {}}
        self.events.append(ev)
        self.log("event.created", "event", ev["id"], {"type": type_})
        if start_tick <= self.tick:
            self._activate(ev)
        return ev

    # --------------------------------------------------------------- tick

    def tick_once(self) -> dict[str, list]:
        changes: dict[str, list] = {"allocations": [], "depots": []}
        self.tick += 1
        for ev in self.events:
            if ev["status"] == "ACTIVE" and ev["end_tick"] <= self.tick:
                self._resolve(ev)
        for ev in self.events:
            if ev["status"] == "SCHEDULED" and ev["start_tick"] <= self.tick:
                self._activate(ev)
                if ev["end_tick"] <= self.tick:
                    self._resolve(ev)
        for s in self.supply:
            if s["status"] != "ARRIVED" and s["planned_tick"] <= self.tick:
                d = self.depot(s["depot_id"])
                d["inventory"][s["fuel_type"]] = min(d["capacity"][s["fuel_type"]],
                                                     d["inventory"][s["fuel_type"]] + s["quantity"])
                s["status"], s["actual_tick"] = "ARRIVED", self.tick
                self.log("supply.arrived", "supply", s["id"], {"quantity": s["quantity"]})
                changes["depots"].append(d["id"])
        for a in self.allocations:
            if a["status"] == "PENDING" and a["created_tick"] < self.tick:
                r = self.route(a["route_id"])
                if r["status"] != "AVAILABLE":
                    a["status"], a["failure_reason"] = "FAILED", "ROUTE_DISRUPTED"
                    d = self.depot(a["source_depot_id"])
                    d["inventory"][a["fuel_type"]] += a["quantity"]
                    changes["depots"].append(d["id"])
                else:
                    a["status"], a["departure_tick"] = "IN_TRANSIT", self.tick
                    a["expected_arrival_tick"] = self.tick + r["transit_ticks"]
                    self.log("allocation.departed", "allocation", a["id"])
                changes["allocations"].append(a)
            elif a["status"] == "IN_TRANSIT" and a["expected_arrival_tick"] <= self.tick:
                st = self.station(a["destination_station_id"])
                f = a["fuel_type"]
                st["inventory"][f] = min(st["capacity"][f], st["inventory"][f] + a["quantity"])
                a["status"], a["actual_arrival_tick"] = "ARRIVED", self.tick
                self.log("allocation.arrived", "allocation", a["id"])
                changes["allocations"].append(a)
        region = {r["id"]: r["demand_factor"] for r in self.regions}
        hour = self.sim_time.hour
        ticks_per_day = 1440 / self.tick_minutes
        for st in self.stations:
            noise = spec.noise(st["demand_profile"])
            for f in FUELS:
                base = spec.daily_demand(st["demand_profile"], f) / ticks_per_day
                mu = base * region.get(st["region_id"], 1.0) * HOUR_OF_DAY(st["demand_profile"], hour)
                demand = max(0.0, mu * st["demand_multiplier"] * (1 + self.rng.gauss(0, noise)))
                if st["status"] != "OPEN":
                    served = 0.0
                else:
                    served = min(st["inventory"][f], demand)
                    st["inventory"][f] = round(st["inventory"][f] - served, 6)
                self.served += served
                self.unmet += demand - served
                self.demand.append({"id": len(self.demand) + 1, "station_id": st["id"], "fuel_type": f,
                                    "tick": self.tick, "sim_time": self.sim_time.isoformat(),
                                    "demand_liters": round(demand, 3), "served_liters": round(served, 3),
                                    "unmet_liters": round(demand - served, 3)})
        self.log("simulation.tick", "simulation", self.tick)
        return changes

    # -------------------------------------------------------- allocations

    def create_allocation(self, body: dict) -> tuple[int, dict]:
        key = body["idempotency_key"]
        fields = ("source_depot_id", "destination_station_id", "route_id", "fuel_type", "quantity")
        existing = next((a for a in self.allocations if a["idempotency_key"] == key), None)
        if existing is not None:
            if all(existing[k] == body[k] for k in fields):
                return 201, existing
            return 409, _err("IDEMPOTENCY_KEY_MISMATCH", "idempotency key reused with a different body")
        d, s, r = self.depot(body["source_depot_id"]), self.station(body["destination_station_id"]), \
            self.route(body["route_id"])
        f, q = body["fuel_type"], float(body["quantity"])
        if d is None or s is None or r is None:
            return 404, _err("NOT_FOUND", "depot, station or route not found")
        if r["source_depot_id"] != d["id"] or r["destination_station_id"] != s["id"]:
            return 409, _err("ROUTE_MISMATCH", "route does not connect depot and station")
        if d["status"] not in ("OPEN", "CONSTRAINED"):
            return 409, _err("DEPOT_CLOSED", "depot closed")
        if s["status"] != "OPEN":
            return 409, _err("STATION_CLOSED", "station closed")
        if r["status"] != "AVAILABLE":
            return 409, _err("ROUTE_DISRUPTED", "route disrupted")
        if q > r["max_shipment"]:
            return 409, _err("ROUTE_CAPACITY_EXCEEDED", "quantity exceeds max shipment")
        if d["inventory"][f] < q:
            return 409, _err("INSUFFICIENT_INVENTORY", "insufficient depot inventory")
        dispatched = sum(a["quantity"] for a in self.allocations if a["source_depot_id"] == d["id"]
                         and a["created_tick"] == self.tick and a["status"] in ("PENDING", "IN_TRANSIT"))
        if dispatched + q > d["dispatch_capacity_per_tick"]:
            return 409, _err("DISPATCH_CAPACITY_EXCEEDED", "depot dispatch capacity exceeded for this tick")
        if s["inventory"][f] + q > s["capacity"][f]:
            return 409, _err("DESTINATION_CAPACITY_EXCEEDED", "station capacity would be exceeded")
        d["inventory"][f] = round(d["inventory"][f] - q, 6)
        a = {"id": len(self.allocations) + 1, "idempotency_key": key, "source_depot_id": d["id"],
             "destination_station_id": s["id"], "route_id": r["id"], "fuel_type": f, "quantity": q,
             "created_tick": self.tick, "departure_tick": None, "expected_arrival_tick": None,
             "actual_arrival_tick": None, "status": "PENDING", "failure_reason": None}
        self.allocations.append(a)
        self.log("allocation.created", "allocation", a["id"], {"quantity": q, "fuel_type": f})
        return 201, a

    def cancel_allocation(self, allocation_id: int) -> tuple[int, dict]:
        a = next((x for x in self.allocations if x["id"] == allocation_id), None)
        if a is None:
            return 404, _err("ALLOCATION_NOT_FOUND", "allocation not found")
        if a["status"] != "PENDING":
            return 409, _err("CANNOT_CANCEL", "only PENDING allocations can be cancelled")
        self.depot(a["source_depot_id"])["inventory"][a["fuel_type"]] += a["quantity"]
        a["status"] = "CANCELLED"
        self.log("allocation.cancelled", "allocation", a["id"])
        return 200, a


def _err(code: str, message: str) -> dict:
    return {"detail": {"code": code, "message": message}}
