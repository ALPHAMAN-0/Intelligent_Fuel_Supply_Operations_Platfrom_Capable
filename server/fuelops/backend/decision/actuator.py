"""The only code path that calls POST /v1/allocations or /cancel (CLAUDE.md hard rule 2).

Before each POST the depot, station and route are re-read (REST is the truth) and the simulator's validation order is
replayed locally. Quantities blocked only by capacity are reduced to what fits; everything else is recorded and left
for the next planning cycle. Idempotency keys are deterministic: fo-{run}-{plan}-{line}-{part}.
"""

from __future__ import annotations

import math
from collections import defaultdict

from fuelops.backend.decision.preflight import ADJUSTABLE, REACTIONS, preflight
from fuelops.backend.domain import PlanLineRecord, PlanRecord, Submission
from fuelops.backend.integration import sim_errors as E
from fuelops.backend.integration.sim_client import SimClient
from fuelops.backend.integration.sim_errors import SimError
from fuelops.backend.state.cache import WorldCache
from fuelops.shared.logging import get_logger
from fuelops.shared.metrics import ALLOCATIONS_SUBMITTED, PLAN_LINES
from fuelops.shared.sim_models import ALLOCATION_ACTIVE, AllocationRequest

log = get_logger("actuator")
UNIT = 100.0


def idempotency_key(run_id: int, plan_id: int, line_no: int, part: int) -> str:
    return f"fo-{run_id}-{plan_id}-{line_no}-{part}"


class Actuator:
    def __init__(self, client: SimClient, cache: WorldCache, min_shipment_l: float = 500.0):
        self.client = client
        self.cache = cache
        self.min_shipment_l = min_shipment_l
        self._dispatched: dict[tuple[int, int, str], float] = defaultdict(float)  # (run, tick, depot) -> liters
        self.last_error: str | None = None

    def dispatched_now(self) -> dict[str, float]:
        state = self.cache.state
        if state is None:
            return {}
        out: dict[str, float] = defaultdict(float)
        for a in state.allocations:
            if a.status in ALLOCATION_ACTIVE and a.created_tick == state.instance.tick:
                out[a.source_depot_id] += a.quantity
        for (run, tick, depot), q in self._dispatched.items():
            if run == state.run_id and tick == state.instance.tick:
                out[depot] = max(out[depot], q)
        return dict(out)

    async def submit_line(self, plan: PlanRecord, line: PlanLineRecord) -> PlanLineRecord:
        """Submit every part of an approved line. Updates line.status and line.submissions in place."""
        state = self.cache.state
        tick = state.instance.tick if state else plan.tick
        try:
            depot = await self.client.get_depot(line.depot_id)
            station = await self.client.get_station(line.station_id)
            routes = {r.id: r for r in await self.client.get_routes()}
        except SimError as exc:
            line.status = "BLOCKED"
            line.decision_reason = f"could not re-read world before submitting: {exc.kind.value} {exc.message}"
            self.last_error = line.decision_reason
            PLAN_LINES.labels("BLOCKED").inc()
            return line
        route = routes.get(line.route_id)
        accepted, problems = 0, []
        for part_no, planned in enumerate(line.parts or [line.quantity]):
            dispatched = self.dispatched_now().get(line.depot_id, 0.0)
            qty = planned
            pre = preflight(depot, station, route, line.fuel_type, qty, dispatched)
            if not pre.ok and pre.code in ADJUSTABLE and pre.max_quantity is not None:
                qty = math.floor(max(0.0, pre.max_quantity) / UNIT) * UNIT
                if qty >= self.min_shipment_l:
                    problems.append(f"part {part_no}: reduced {planned:.0f} -> {qty:.0f} L ({pre.code})")
                    pre = preflight(depot, station, route, line.fuel_type, qty, dispatched)
            key = idempotency_key(plan.run_id, plan.id, line.line_no, part_no)
            sub = Submission(part=part_no, quantity=qty, idempotency_key=key)
            line.submissions.append(sub)
            if not pre.ok:
                sub.status, sub.error = "PREFLIGHT_BLOCKED", f"{pre.code}: {pre.message}"
                problems.append(f"part {part_no}: {pre.code} ({REACTIONS.get(pre.code or '', 'blocked')})")
                ALLOCATIONS_SUBMITTED.labels("preflight_blocked").inc()
                continue
            req = AllocationRequest(idempotency_key=key, source_depot_id=line.depot_id,
                                    destination_station_id=line.station_id, route_id=line.route_id,
                                    fuel_type=line.fuel_type, quantity=qty)
            try:
                alloc, status = await self.client.create_allocation(req)
            except SimError as exc:
                sub.status = "SIM_REJECTED" if exc.kind is E.ErrorKind.DOMAIN else "FAILED_TO_SUBMIT"
                sub.http_status, sub.error = exc.status, f"{exc.code or exc.kind.value}: {exc.message}"
                problems.append(f"part {part_no}: {exc.code or exc.kind.value} "
                                f"({REACTIONS.get(exc.code or '', 'retry next cycle')})")
                ALLOCATIONS_SUBMITTED.labels(exc.code or exc.kind.value).inc()
                log.warning("decision.rejected", plan=plan.id, line=line.line_no, part=part_no, key=key,
                            code=exc.code, kind=exc.kind.value)
                continue
            sub.allocation_id, sub.status, sub.http_status = alloc.id, alloc.status, status
            accepted += 1
            self._dispatched[(plan.run_id, tick, line.depot_id)] = dispatched + qty
            # The simulator deducts depot stock at creation; mirror it so later parts preflight correctly.
            depot.inventory[line.fuel_type] = depot.inventory.get(line.fuel_type, 0.0) - qty
            ALLOCATIONS_SUBMITTED.labels("accepted").inc()
            log.info("decision.submitted", plan=plan.id, line=line.line_no, part=part_no, key=key,
                     allocation_id=alloc.id, quantity=qty, http_status=status)
        total = len(line.submissions)
        if accepted == total and total:
            line.status = "PENDING"
        elif accepted:
            line.status = "PARTIAL"
        elif any(s.status == "SIM_REJECTED" for s in line.submissions):
            line.status = "SIM_REJECTED"
        else:
            line.status = "BLOCKED"
        if problems:
            line.decision_reason = "; ".join(problems)
        PLAN_LINES.labels(line.status).inc()
        return line

    def refresh(self, plans: list[PlanRecord]) -> list[PlanRecord]:
        """Map simulator allocation statuses back onto plan lines. Returns plans that changed."""
        state = self.cache.state
        if state is None:
            return []
        by_id = {a.id: a for a in state.allocations}
        changed = []
        for plan in plans:
            if plan.run_id != state.run_id:
                continue
            dirty = False
            for line in plan.lines:
                ids = [s for s in line.submissions if s.allocation_id is not None]
                if not ids:
                    continue
                for s in ids:
                    a = by_id.get(s.allocation_id)
                    if a is not None and a.status != s.status:
                        s.status = a.status
                        dirty = True
                statuses = {s.status for s in ids}
                new = line.status
                if statuses == {"ARRIVED"}:
                    new = "ARRIVED"
                elif "FAILED" in statuses:
                    new = "FAILED"
                elif statuses <= {"CANCELLED"}:
                    new = "CANCELLED"
                elif "IN_TRANSIT" in statuses or ("ARRIVED" in statuses and "PENDING" not in statuses):
                    new = "IN_TRANSIT"
                elif "PENDING" in statuses:
                    new = "PENDING"
                if len(ids) < len(line.submissions) and new in ("ARRIVED", "IN_TRANSIT", "PENDING"):
                    new = "PARTIAL" if new != "ARRIVED" else "ARRIVED"
                if new != line.status:
                    line.status = new
                    dirty = True
                    if new in ("ARRIVED", "FAILED", "CANCELLED"):
                        PLAN_LINES.labels(new).inc()
            if dirty:
                plan.refresh_status()
                changed.append(plan)
        return changed

    async def cancel(self, allocation_id: int):
        return await self.client.cancel_allocation(allocation_id)
