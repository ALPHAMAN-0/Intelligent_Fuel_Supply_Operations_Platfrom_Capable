"""Test factories: the baseline world from the integration guide §8 and synthetic demand history."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from fuelops.shared import spec
from fuelops.shared.models import DemandPoint, WorldState
from fuelops.shared.sim_models import (
    Allocation,
    Depot,
    Instance,
    Metrics,
    Region,
    Route,
    SimEvent,
    Station,
    SupplyArrival,
)

START = datetime(2026, 1, 1, tzinfo=UTC)


def fuels(d: float, p: float, o: float) -> dict[str, float]:
    return {"DIESEL": float(d), "PETROL": float(p), "OCTANE": float(o)}


def make_world(tick: int = 0, tick_minutes: int = 15, status: str = "PAUSED") -> WorldState:
    return WorldState(
        instance=Instance(id=1, scenario_id="baseline", scenario_version="1.0", seed=12345,
                          sim_time=START + timedelta(minutes=tick * tick_minutes), tick=tick,
                          tick_minutes=tick_minutes, status=status),
        regions=[Region(id="region-dhaka", name="Dhaka Division", demand_factor=1.00),
                 Region(id="region-chattogram", name="Chattogram Division", demand_factor=1.08)],
        depots=[
            Depot(id="depot-gazipur", name="Gazipur Depot", region_id="region-dhaka", status="OPEN",
                  dispatch_capacity_per_tick=12000, capacity=fuels(90000, 70000, 45000),
                  inventory=fuels(60000, 45000, 26000)),
            Depot(id="depot-patiya", name="Patiya Depot", region_id="region-chattogram", status="OPEN",
                  dispatch_capacity_per_tick=11000, capacity=fuels(85000, 65000, 40000),
                  inventory=fuels(55000, 42000, 24000)),
        ],
        stations=[
            Station(id="station-mirpur", name="Mirpur Fuel Station", region_id="region-dhaka", status="OPEN",
                    demand_profile="urban_high", demand_multiplier=1.0, capacity=fuels(15000, 14000, 9000),
                    inventory=fuels(9000, 9000, 5000)),
            Station(id="station-tongi", name="Tongi Fuel Station", region_id="region-dhaka", status="OPEN",
                    demand_profile="industrial", demand_multiplier=1.0, capacity=fuels(18000, 9000, 6000),
                    inventory=fuels(11000, 6000, 3500)),
            Station(id="station-karnaphuli", name="Karnaphuli Fuel Station", region_id="region-chattogram",
                    status="OPEN", demand_profile="highway", demand_multiplier=1.0,
                    capacity=fuels(14000, 15000, 9000), inventory=fuels(8500, 9500, 5200)),
            Station(id="station-coxsbazar", name="Cox's Bazar Fuel Station", region_id="region-chattogram",
                    status="OPEN", demand_profile="regional", demand_multiplier=1.0,
                    capacity=fuels(12000, 12000, 7000), inventory=fuels(7500, 7500, 4200)),
        ],
        routes=[
            Route(id="route-gazipur-mirpur", source_depot_id="depot-gazipur", destination_station_id="station-mirpur",
                  transit_ticks=2, max_shipment=7000, status="AVAILABLE"),
            Route(id="route-gazipur-tongi", source_depot_id="depot-gazipur", destination_station_id="station-tongi",
                  transit_ticks=2, max_shipment=6500, status="AVAILABLE"),
            Route(id="route-patiya-karnaphuli", source_depot_id="depot-patiya",
                  destination_station_id="station-karnaphuli", transit_ticks=2, max_shipment=7000,
                  status="AVAILABLE"),
            Route(id="route-patiya-coxsbazar", source_depot_id="depot-patiya",
                  destination_station_id="station-coxsbazar", transit_ticks=3, max_shipment=6000, status="AVAILABLE"),
            Route(id="route-gazipur-karnaphuli", source_depot_id="depot-gazipur",
                  destination_station_id="station-karnaphuli", transit_ticks=4, max_shipment=5000,
                  status="AVAILABLE"),
            Route(id="route-patiya-mirpur", source_depot_id="depot-patiya", destination_station_id="station-mirpur",
                  transit_ticks=4, max_shipment=5000, status="AVAILABLE"),
        ],
        supply_arrivals=[
            SupplyArrival(id="supply-001", depot_id="depot-gazipur", fuel_type="DIESEL", quantity=18000,
                          planned_tick=12, status="SCHEDULED"),
            SupplyArrival(id="supply-002", depot_id="depot-patiya", fuel_type="PETROL", quantity=16000,
                          planned_tick=16, status="SCHEDULED"),
        ],
        events=[],
        allocations=[],
        metrics=Metrics(served_demand_liters=0, unmet_demand_liters=0, service_level=1.0, allocation_liters=0,
                        allocation_failures=0),
        synced_at=START,
    )


def station(state: WorldState, sid: str) -> Station:
    return next(s for s in state.stations if s.id == sid)


def depot(state: WorldState, did: str) -> Depot:
    return next(d for d in state.depots if d.id == did)


def route(state: WorldState, rid: str) -> Route:
    return next(r for r in state.routes if r.id == rid)


def add_event(state: WorldState, type_: str, start: int, end: int, status: str = "SCHEDULED",
              **parameters) -> SimEvent:
    ev = SimEvent(id=len(state.events) + 1, type=type_, start_tick=start, end_tick=end, status=status,
                  parameters=parameters)
    state.events.append(ev)
    return ev


def add_allocation(state: WorldState, route_id: str, fuel: str, quantity: float, status: str = "IN_TRANSIT",
                   created_tick: int | None = None, expected_arrival: int | None = None) -> Allocation:
    r = route(state, route_id)
    created = state.instance.tick if created_tick is None else created_tick
    a = Allocation(id=len(state.allocations) + 1, idempotency_key=f"test-{len(state.allocations) + 1}",
                   source_depot_id=r.source_depot_id, destination_station_id=r.destination_station_id,
                   route_id=r.id, fuel_type=fuel, quantity=quantity, created_tick=created,
                   departure_tick=created + 1, expected_arrival_tick=expected_arrival or created + 1 + r.transit_ticks,
                   status=status)
    state.allocations.append(a)
    return a


def synth_history(state: WorldState, ticks: int, *, scale: float = 1.0, noise: float = 0.10, seed: int = 7,
                  multiplier_fn=None, end_tick: int | None = None) -> list[DemandPoint]:
    """Synthetic history ending at `end_tick` (default: current tick) from the spec formula x scale x jitter."""
    rng = random.Random(seed)
    region = {r.id: r.demand_factor for r in state.regions}
    tm = state.instance.tick_minutes
    end = state.instance.tick if end_tick is None else end_tick
    out: list[DemandPoint] = []
    for t in range(end - ticks + 1, end + 1):
        when = START + timedelta(minutes=t * tm)
        for st in state.stations:
            m = multiplier_fn(st.id, t) if multiplier_fn else 1.0
            for f in spec.FUELS:
                base = spec.prior_per_tick(st.demand_profile, f, when.hour, tm, region[st.region_id]) * scale
                demand = max(0.0, base * m * (1 + rng.gauss(0, noise)))
                out.append(DemandPoint(station_id=st.id, fuel_type=f, tick=t, sim_time=when, demand=demand,
                                       served=demand, multiplier=m))
    return out
