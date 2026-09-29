"""Pydantic models mirroring the simulator's /v1 and /admin schemas (integration guide §4-§7).

Every simulator payload is parsed through these models; a payload that does not validate is rejected by the client
and raised as an invalid-response alert. Status fields are plain strings so that an unexpected status (for example
from a surprise organizer event) is visible to the operator instead of crashing the parser.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Fuel = Literal["DIESEL", "PETROL", "OCTANE"]
FUELS: tuple[Fuel, ...] = ("DIESEL", "PETROL", "OCTANE")

# Known status values (guide §4).
DEPOT_SHIPPABLE = {"OPEN", "CONSTRAINED"}
STATION_OPEN = "OPEN"
ROUTE_AVAILABLE = "AVAILABLE"
ALLOCATION_ACTIVE = {"PENDING", "IN_TRANSIT"}
ALLOCATION_FINAL = {"ARRIVED", "FAILED", "CANCELLED"}


class SimModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class HealthSimulation(SimModel):
    status: str
    tick: int


class Health(SimModel):
    status: str
    database: str | None = None
    simulation: HealthSimulation | None = None


class Instance(SimModel):
    id: int
    scenario_id: str
    scenario_version: str | None = None
    seed: int | None = None
    sim_time: datetime
    tick: int = Field(ge=0)
    tick_minutes: int = Field(gt=0)
    status: str


class Region(SimModel):
    id: str
    name: str
    demand_factor: float = 1.0


class Depot(SimModel):
    id: str
    name: str
    region_id: str
    status: str
    dispatch_capacity_per_tick: float = Field(ge=0)
    capacity: dict[Fuel, float]
    inventory: dict[Fuel, float]


class Station(SimModel):
    id: str
    name: str
    region_id: str
    status: str
    demand_profile: str
    demand_multiplier: float = Field(ge=0)
    capacity: dict[Fuel, float]
    inventory: dict[Fuel, float]


class Route(SimModel):
    id: str
    source_depot_id: str
    destination_station_id: str
    transit_ticks: int = Field(ge=0)
    max_shipment: float = Field(gt=0)
    status: str


class SupplyArrival(SimModel):
    id: str
    depot_id: str
    fuel_type: Fuel
    quantity: float = Field(ge=0)
    planned_tick: int
    actual_tick: int | None = None
    status: str


class SimEvent(SimModel):
    id: int
    type: str
    start_tick: int
    end_tick: int
    status: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class Allocation(SimModel):
    id: int
    idempotency_key: str
    source_depot_id: str
    destination_station_id: str
    route_id: str
    fuel_type: Fuel
    quantity: float
    created_tick: int
    departure_tick: int | None = None
    expected_arrival_tick: int | None = None
    actual_arrival_tick: int | None = None
    status: str
    failure_reason: str | None = None


class DemandObservation(SimModel):
    id: int
    station_id: str
    fuel_type: Fuel
    tick: int
    sim_time: datetime
    demand_liters: float
    served_liters: float
    unmet_liters: float


class Metrics(SimModel):
    served_demand_liters: float
    unmet_demand_liters: float
    service_level: float
    allocation_liters: float
    allocation_failures: int


class AllocationRequest(SimModel):
    idempotency_key: str = Field(min_length=1, max_length=150)
    source_depot_id: str
    destination_station_id: str
    route_id: str
    fuel_type: Fuel
    quantity: float = Field(gt=0)


class StepResult(SimModel):
    tick: int
    sim_time: datetime


class AuditEntry(SimModel):
    id: int
    wall_time: datetime | None = None
    sim_time: datetime | None = None
    tick: int | None = None
    action: str
    entity_type: str | None = None
    entity_id: str | None = None
    result: str | None = None
    metadata_json: dict[str, Any] | None = None


EventType = Literal[
    "demand_spike", "route_disruption", "station_outage", "depot_constraint", "shipment_delay", "supply_shortfall"
]
FaultType = Literal["latency", "unavailable", "error_rate", "stale_data", "stream_disconnect"]


class EventInjection(SimModel):
    type: EventType
    start_tick: int = Field(ge=0)
    duration_ticks: int = Field(gt=0)
    parameters: dict[str, Any] = Field(default_factory=dict)


class FaultInjection(SimModel):
    type: FaultType
    duration_seconds: int = Field(gt=0, le=3600)
    parameters: dict[str, Any] = Field(default_factory=dict)
