"""Domain models exchanged between the backend and the intelligence service."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from fuelops.shared.sim_models import (
    Allocation,
    Depot,
    Fuel,
    Instance,
    Metrics,
    Region,
    Route,
    SimEvent,
    Station,
    SupplyArrival,
)


class WorldState(BaseModel):
    """One consistent snapshot of the simulated world, built from REST reads (the source of truth)."""

    instance: Instance
    regions: list[Region] = Field(default_factory=list)
    depots: list[Depot] = Field(default_factory=list)
    stations: list[Station] = Field(default_factory=list)
    routes: list[Route] = Field(default_factory=list)
    supply_arrivals: list[SupplyArrival] = Field(default_factory=list)
    events: list[SimEvent] = Field(default_factory=list)
    allocations: list[Allocation] = Field(default_factory=list)
    metrics: Metrics | None = None
    stale: bool = False
    degraded: list[str] = Field(default_factory=list)
    synced_at: datetime
    run_id: int = 0


class DemandPoint(BaseModel):
    station_id: str
    fuel_type: Fuel
    tick: int
    sim_time: datetime
    demand: float
    served: float | None = None
    multiplier: float = 1.0


class HourStats(BaseModel):
    """Sufficient statistics of demand rate (liters/hour, multiplier removed) for one (station, fuel, hour)."""

    station_id: str
    fuel_type: Fuel
    hour: int = Field(ge=0, le=23)
    n: int = Field(ge=0)
    sum_rate: float
    sum_rate_sq: float


class PlannerParams(BaseModel):
    horizon_ticks: int = Field(default=24, ge=4, le=96)
    risk_horizon_ticks: int = Field(default=96, ge=4, le=192)
    safety_z: float = Field(default=1.0, ge=0.0, le=3.0)
    min_shipment_l: float = Field(default=500.0, ge=0.0)
    max_parts_per_route: int = Field(default=2, ge=1, le=5)
    terminal_cover_ticks: int = Field(default=16, ge=0)
    terminal_weight: float = Field(default=0.5, ge=0.0)
    committed_dispatch: dict[str, float] = Field(default_factory=dict)
    priority_weights: dict[str, float] = Field(default_factory=dict)
    alternatives: bool = True


class SeriesForecast(BaseModel):
    station_id: str
    fuel_type: Fuel
    first_tick: int
    mu: list[float]
    sigma: list[float]
    source: str
    n_obs: int
    noise: float


class RiskScore(BaseModel):
    station_id: str
    fuel_type: Fuel
    station_status: str
    on_hand: float
    in_transit: float
    capacity: float
    inventory_position: float
    hours_to_stockout: float | None
    stockout_tick: int | None
    p_stockout: dict[str, float]
    demand_next_24h: float
    confidence: float
    notes: list[str] = Field(default_factory=list)


class SupplyEta(BaseModel):
    id: str
    depot_id: str
    fuel_type: Fuel
    quantity: float
    expected_quantity: float
    planned_tick: int
    eta_tick: int
    status: str
    delayed: bool
    shortfall: bool


class RiskSnapshot(BaseModel):
    p_stockout: float  # P(stockout within the planning horizon)
    horizon_hours: float
    hours_to_stockout: float | None
    projected_unmet_liters: float
    end_inventory_liters: float  # mean projected stock at the end of the planning horizon


class Alternative(BaseModel):
    description: str
    route_id: str | None = None
    delta_unmet_liters: float


class PlanLine(BaseModel):
    line_no: int
    route_id: str
    depot_id: str
    station_id: str
    fuel_type: Fuel
    quantity: float
    parts: list[float]
    arrival_tick: int
    reason: str
    signals: list[str] = Field(default_factory=list)
    binding: list[str] = Field(default_factory=list)
    risk_before: RiskSnapshot
    risk_after: RiskSnapshot
    alternatives: list[Alternative] = Field(default_factory=list)
    confidence: float = 0.5


class PlanResult(BaseModel):
    policy: str
    tick: int
    horizon_ticks: int
    lines: list[PlanLine] = Field(default_factory=list)
    objective: float | None = None
    projected_unmet_before: float = 0.0
    projected_unmet_after: float = 0.0
    binding_constraints: list[str] = Field(default_factory=list)
    rationing: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    solver_status: str = "UNKNOWN"
    solve_ms: float = 0.0


class ModelInfo(BaseModel):
    version: str
    kind: str
    fitted_series: int
    observations: int
    noise_floor: float


class CycleRequest(BaseModel):
    state: WorldState
    history: list[DemandPoint] = Field(default_factory=list)
    stats: list[HourStats] = Field(default_factory=list)
    params: PlannerParams = Field(default_factory=PlannerParams)


class CycleResponse(BaseModel):
    forecasts: list[SeriesForecast]
    risks: list[RiskScore]
    supply_etas: list[SupplyEta]
    plan: PlanResult
    model: ModelInfo


class FixedLine(BaseModel):
    route_id: str
    fuel_type: Fuel
    quantity: float = Field(gt=0)


class EvaluateRequest(BaseModel):
    state: WorldState
    history: list[DemandPoint] = Field(default_factory=list)
    stats: list[HourStats] = Field(default_factory=list)
    lines: list[FixedLine] = Field(default_factory=list)
    params: PlannerParams = Field(default_factory=PlannerParams)


class SeriesProjection(BaseModel):
    station_id: str
    fuel_type: Fuel
    inventory: list[float]
    unmet_total: float
    stockout_tick: int | None
    p_stockout_12h: float


class EvaluateResponse(BaseModel):
    before: list[SeriesProjection]
    after: list[SeriesProjection]
    unmet_before: float
    unmet_after: float


class ForecastRequest(BaseModel):
    state: WorldState
    history: list[DemandPoint] = Field(default_factory=list)
    stats: list[HourStats] = Field(default_factory=list)
    horizon_ticks: int = Field(default=24, ge=1, le=192)
