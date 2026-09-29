"""Tick-driven state sync (architecture.md §4.3). REST is the source of truth; SSE events only wake this loop.

Each sync re-reads every resource in parallel; a resource that fails keeps its last good value and is listed in
`WorldState.degraded`. Demand history is mirrored incrementally (`limit` sized to the ticks since the last sync).
A tick that goes backwards, or a "Simulation reset" notice, starts a new run epoch.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from fuelops.backend.integration.sim_client import SimClient
from fuelops.backend.integration.sim_errors import SimError
from fuelops.backend.state.cache import WorldCache
from fuelops.backend.state.store import Store
from fuelops.shared.logging import get_logger
from fuelops.shared.metrics import (
    ALLOCATION_FAILURES,
    SERVED_LITERS,
    SERVICE_LEVEL,
    SIM_STALE,
    SIM_TICK,
    SSE_POLLING,
    TICK_LAG,
    UNMET_LITERS,
)
from fuelops.shared.models import DemandPoint, WorldState
from fuelops.shared.sim_models import Instance

log = get_logger("sync")

Listener = Callable[[WorldState, list[DemandPoint]], Awaitable[None]]
ROWS_PER_TICK_FALLBACK = 12


class SyncService:
    def __init__(self, client: SimClient, cache: WorldCache, store: Store, poll_interval_s: float = 1.0):
        self.client = client
        self.cache = cache
        self.store = store
        self.poll_interval_s = poll_interval_s
        self.listeners: list[Listener] = []
        self._hint = asyncio.Event()
        self._force = False
        self._reset_hint = False
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self.sse_connected: Callable[[], bool] = lambda: False
        self.last_instance: Instance | None = None
        self.last_duration_ms = 0.0

    # --------------------------------------------------------------- hints

    def hint(self, force: bool = False) -> None:
        self._force = self._force or force
        self._hint.set()

    async def on_sse_event(self, name: str, payload: dict) -> None:
        if name == "simulator.notice" and "reset" in str(payload.get("message", "")).lower():
            self._reset_hint = True
            self.hint(force=True)
        elif name in ("allocation.status_changed", "inventory.updated", "stream.connected"):
            self.hint(force=True)
        elif name == "simulation.tick":
            self.hint()

    def stop(self) -> None:
        self._stop.set()
        self._hint.set()

    # ---------------------------------------------------------------- loop

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._hint.wait(), timeout=self.poll_interval_s)
            except TimeoutError:
                pass
            if self._stop.is_set():
                break
            self._hint.clear()
            SSE_POLLING.set(0 if self.sse_connected() else 1)
            try:
                await self.sync_once()
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                self.cache.last_sync_ok = False
                self.cache.last_sync_error = str(exc)
                log.exception("sync.unexpected_error", error=str(exc))

    async def sync_once(self, force: bool = False) -> WorldState | None:
        async with self._lock:
            force = force or self._force
            self._force = False
            try:
                inst = await self.client.get_instance()
            except SimError as exc:
                self._fail(f"instance: {exc.kind.value} {exc.code or ''} {exc.message}")
                return None
            self.last_instance = inst
            SIM_TICK.set(inst.tick)
            prev = self.cache.state
            if prev is not None and (inst.tick < prev.instance.tick or self._reset_hint):
                await self._new_run(inst, reason="tick regression" if inst.tick < prev.instance.tick else "reset notice")
            self._reset_hint = False
            if self.cache.state is None and self.cache.run_id == 0:
                await self._new_run(inst, reason="startup")
            if self.cache.state is not None and not force and inst.tick == self.cache.state.instance.tick \
                    and inst.status == self.cache.state.instance.status:
                TICK_LAG.set(0)
                return self.cache.state
            return await self._full_sync(inst)

    async def _new_run(self, inst: Instance, reason: str) -> None:
        run_id = await self.store.new_run(inst.seed, inst.scenario_id, inst.tick)
        run_id = run_id or (self.cache.run_id + 1)
        log.info("sync.new_run", run_id=run_id, reason=reason, tick=inst.tick)
        self.cache.reset_run(run_id)

    def _fail(self, error: str) -> None:
        self.cache.last_sync_ok = False
        self.cache.last_sync_error = error
        log.warning("sync.failed", error=error)

    async def _full_sync(self, inst: Instance) -> WorldState | None:
        start = time.perf_counter()
        prev = self.cache.state
        names = ["regions", "depots", "stations", "routes", "supply_arrivals", "events", "allocations", "metrics"]
        calls = [self.client.get_regions(), self.client.get_depots(), self.client.get_stations(),
                 self.client.get_routes(), self.client.get_supply_arrivals(), self.client.get_events(),
                 self.client.get_allocations(), self.client.get_metrics()]
        results = await asyncio.gather(*calls, return_exceptions=True)
        values: dict[str, object] = {}
        degraded: list[str] = []
        for name, res in zip(names, results, strict=True):
            if isinstance(res, BaseException):
                if not isinstance(res, SimError):
                    raise res
                degraded.append(name)
                values[name] = getattr(prev, name) if prev is not None else None
            else:
                values[name] = res
        core = ("depots", "stations", "routes")
        if any(values[n] is None for n in core):
            self._fail(f"initial sync incomplete: {', '.join(degraded)}")
            return None

        pairs = await self._sync_history(inst, degraded)
        new_points = [p for _, p in pairs]
        state = WorldState(
            instance=inst,
            regions=values["regions"] or [],
            depots=values["depots"],
            stations=values["stations"],
            routes=values["routes"],
            supply_arrivals=values["supply_arrivals"] or [],
            events=values["events"] or [],
            allocations=values["allocations"] or [],
            metrics=values["metrics"],
            stale=self.client.stale,
            degraded=degraded,
            synced_at=datetime.now(UTC),
            run_id=self.cache.run_id,
        )
        self.cache.update_state(state)
        # Multipliers are known per tick only once the state is stored; attach them to the new points now.
        for p in new_points:
            p.multiplier = self.cache.multiplier_at(p.station_id, p.tick)
        self.cache.add_history(new_points)
        self.cache.degraded = degraded
        self.cache.last_sync_ok = not degraded
        self.cache.last_sync_error = ", ".join(degraded) or None
        self._export(state)
        self.last_duration_ms = (time.perf_counter() - start) * 1000

        await self.store.insert_observations(state.run_id, pairs)
        await self.store.save_snapshot(state)
        for listener in list(self.listeners):
            try:
                await listener(state, new_points)
            except Exception as exc:  # noqa: BLE001 - one listener must not break the others
                log.exception("sync.listener_failed", listener=getattr(listener, "__qualname__", str(listener)),
                              error=str(exc))
        return state

    async def _sync_history(self, inst: Instance, degraded: list[str]) -> list[tuple[int, DemandPoint]]:
        stations = len(self.cache.state.stations) if self.cache.state else 4
        rows_per_tick = stations * 3 or ROWS_PER_TICK_FALLBACK
        if self.cache.last_history_tick < 0:
            limit = 2000
        else:
            limit = min(2000, rows_per_tick * max(1, inst.tick - self.cache.last_history_tick) + 2 * rows_per_tick)
        try:
            rows = await self.client.get_demand_history(limit=limit)
        except SimError:
            degraded.append("demand_history")
            return []
        fresh = sorted((r for r in rows if r.id > self.cache.last_history_id), key=lambda r: r.id)
        if fresh:
            self.cache.last_history_id = fresh[-1].id
        return [(r.id, DemandPoint(station_id=r.station_id, fuel_type=r.fuel_type, tick=r.tick, sim_time=r.sim_time,
                                   demand=r.demand_liters, served=r.served_liters, multiplier=1.0)) for r in fresh]

    def _export(self, state: WorldState) -> None:
        SIM_STALE.set(1 if state.stale else 0)
        TICK_LAG.set(0)
        m = state.metrics
        if m is not None:
            SERVICE_LEVEL.set(m.service_level)
            UNMET_LITERS.set(m.unmet_demand_liters)
            SERVED_LITERS.set(m.served_demand_liters)
            ALLOCATION_FAILURES.set(m.allocation_failures)
