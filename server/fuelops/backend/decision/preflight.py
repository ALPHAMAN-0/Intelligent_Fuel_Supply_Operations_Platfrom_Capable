"""Client-side replica of the simulator's allocation validation order (guide §5.2).

Run against freshly re-read depot/station/route data right before POST /v1/allocations, so predictable rejections
never reach the simulator.
"""

from __future__ import annotations

from dataclasses import dataclass

from fuelops.backend.integration import sim_errors as E
from fuelops.shared.sim_models import DEPOT_SHIPPABLE, ROUTE_AVAILABLE, STATION_OPEN, Depot, Route, Station


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    code: str | None = None
    message: str = ""
    max_quantity: float | None = None  # largest quantity that would pass the capacity checks


def preflight(depot: Depot | None, station: Station | None, route: Route | None, fuel: str, quantity: float,
              dispatched_this_tick: float) -> PreflightResult:
    if depot is None or station is None or route is None:
        return PreflightResult(False, E.NOT_FOUND, "depot, station or route id unknown")
    if route.source_depot_id != depot.id or route.destination_station_id != station.id:
        return PreflightResult(False, E.ROUTE_MISMATCH, f"{route.id} does not connect {depot.id} -> {station.id}")
    if depot.status not in DEPOT_SHIPPABLE:
        return PreflightResult(False, E.DEPOT_CLOSED, f"{depot.id} is {depot.status}")
    if station.status != STATION_OPEN:
        return PreflightResult(False, E.STATION_CLOSED, f"{station.id} is {station.status}")
    if route.status != ROUTE_AVAILABLE:
        return PreflightResult(False, E.ROUTE_DISRUPTED, f"{route.id} is {route.status}")
    if quantity <= 0:
        return PreflightResult(False, "INVALID_QUANTITY", "quantity must be > 0")
    headroom = min(
        route.max_shipment,
        depot.inventory.get(fuel, 0.0),
        depot.dispatch_capacity_per_tick - dispatched_this_tick,
        station.capacity.get(fuel, 0.0) - station.inventory.get(fuel, 0.0),
    )
    if quantity > route.max_shipment:
        return PreflightResult(False, E.ROUTE_CAPACITY_EXCEEDED,
                               f"{quantity:.0f} L > max shipment {route.max_shipment:.0f} L", headroom)
    if depot.inventory.get(fuel, 0.0) < quantity:
        return PreflightResult(False, E.INSUFFICIENT_INVENTORY,
                               f"{depot.id} has {depot.inventory.get(fuel, 0.0):.0f} L {fuel}", headroom)
    if dispatched_this_tick + quantity > depot.dispatch_capacity_per_tick:
        return PreflightResult(False, E.DISPATCH_CAPACITY_EXCEEDED,
                               f"{depot.id} already dispatched {dispatched_this_tick:.0f} of "
                               f"{depot.dispatch_capacity_per_tick:.0f} L this tick", headroom)
    if station.inventory.get(fuel, 0.0) + quantity > station.capacity.get(fuel, 0.0):
        return PreflightResult(False, E.DESTINATION_CAPACITY_EXCEEDED,
                               f"{station.id} {fuel} would exceed capacity", headroom)
    return PreflightResult(True, max_quantity=headroom)


ADJUSTABLE = {E.INSUFFICIENT_INVENTORY, E.DISPATCH_CAPACITY_EXCEEDED, E.DESTINATION_CAPACITY_EXCEEDED,
              E.ROUTE_CAPACITY_EXCEEDED}

# 409 reaction map (architecture.md §4.6)
REACTIONS = {
    E.IDEMPOTENCY_KEY_MISMATCH: "bug signal: never retry with the same key",
    E.ROUTE_MISMATCH: "resync topology",
    E.NOT_FOUND: "resync topology",
    E.DEPOT_CLOSED: "re-plan from another depot",
    E.STATION_CLOSED: "drop line; station outage",
    E.ROUTE_DISRUPTED: "re-plan with the route excluded",
    E.ROUTE_CAPACITY_EXCEEDED: "split into parts <= max_shipment",
    E.INSUFFICIENT_INVENTORY: "reduce quantity or wait for supply",
    E.DISPATCH_CAPACITY_EXCEEDED: "defer remainder to next tick",
    E.DESTINATION_CAPACITY_EXCEEDED: "reduce to headroom",
    E.CANNOT_CANCEL: "shipment already moving",
}
