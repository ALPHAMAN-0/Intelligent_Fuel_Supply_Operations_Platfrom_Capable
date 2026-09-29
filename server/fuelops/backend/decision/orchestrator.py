"""Decision orchestrator (architecture.md §4.5): one plan per cycle, human-in-the-loop modes, automatic fallback.

    sync -> forecast accuracy + detectors -> intelligence /cycle (or heuristic fallback) -> plan -> mode gate
         -> actuator (preflight + POST) -> explanation (async, never blocking)
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from fuelops.backend.alerts.manager import AlertManager
from fuelops.backend.decision.actuator import Actuator
from fuelops.backend.decision.heuristic import heuristic_cycle
from fuelops.backend.decision.intelligence_client import IntelligenceClient, IntelligenceUnavailable
from fuelops.backend.domain import Decision, Mode, PlanLineRecord, PlanRecord
from fuelops.backend.explain.llm import Explainer
from fuelops.backend.explain.templates import explain_plan
from fuelops.backend.state.cache import WorldCache
from fuelops.backend.state.store import Store
from fuelops.shared.config import Settings
from fuelops.shared.logging import get_logger
from fuelops.shared.metrics import (
    DECISION_CYCLES,
    DECISION_DURATION,
    FALLBACK_ACTIVATIONS,
    FORECAST_NOISE_FLOOR,
    PLAN_CONFIDENCE,
    STATIONS_AT_RISK,
)
from fuelops.shared.models import (
    CycleRequest,
    CycleResponse,
    DemandPoint,
    EvaluateRequest,
    EvaluateResponse,
    FixedLine,
    PlannerParams,
    WorldState,
)
from fuelops.shared.risk import evaluate as local_evaluate
from fuelops.shared.world import prior_forecast

log = get_logger("decision")
Publisher = Callable[[str, dict[str, Any]], Awaitable[None] | None]
REVIEWABLE = {"PROPOSED", "MODIFIED"}


class DecisionError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Orchestrator:
    def __init__(self, settings: Settings, cache: WorldCache, intel: IntelligenceClient, actuator: Actuator,
                 alerts: AlertManager, store: Store, explainer: Explainer, publish: Publisher | None = None):
        self.settings = settings
        self.cache = cache
        self.intel = intel
        self.actuator = actuator
        self.alerts = alerts
        self.store = store
        self.explainer = explainer
        self.publish = publish
        self.mode: Mode = settings.operating_mode
        self.policy: str = settings.planner_policy
        self.enabled = settings.decisions_enabled
        self.plans: OrderedDict[int, PlanRecord] = OrderedDict()
        self.decisions: deque[Decision] = deque(maxlen=2000)
        self._next_plan_id = 1
        self._next_decision_id = 1
        self._lock = asyncio.Lock()
        self._bg: set[asyncio.Task] = set()
        self.last_cycle_tick: int | None = None
        self.last_cycle_ms: float | None = None
        self.last_policy_used: str | None = None
        self.fallback_active = False
        self.fallback_reason: str | None = None
        self.cycles = 0

    # ------------------------------------------------------------ restore

    def restore(self, plans: list[PlanRecord], decisions: list[Decision]) -> None:
        for p in sorted(plans, key=lambda p: p.id):
            self.plans[p.id] = p
        for d in sorted(decisions, key=lambda d: d.id):
            self.decisions.append(d)
        self._next_plan_id = max([p.id for p in plans], default=0) + 1
        self._next_decision_id = max([d.id for d in decisions], default=0) + 1

    # ------------------------------------------------------------ sync hook

    async def on_state(self, state: WorldState, new_points: list[DemandPoint]) -> None:
        z_scores = self.cache.tracker.observe(new_points)
        for plan in self.actuator.refresh(list(self.plans.values())[-50:]):
            await self.store.upsert_plan(plan)
            await self._emit("plan.updated", plan.model_dump(mode="json"))
        await self._expire(state)
        tick = state.instance.tick
        due = (self.enabled and tick != self.last_cycle_tick
               and tick % self.settings.decision_every_n_ticks == 0)
        if due:
            await self.run_cycle(state, trigger="tick")
        risks = self.cache.cycle.risks if self.cache.cycle is not None and self.cache.cycle_tick == tick else None
        prev = self.cache.previous if self.cache.previous is not state else None
        await self.alerts.evaluate(state, prev, risks, z_scores, self.cache.first_seen_supply, new_points)
        await self._system_alerts(state)
        await self._emit("state", {"tick": tick, "status": state.instance.status, "stale": state.stale,
                                   "degraded": state.degraded})

    async def _system_alerts(self, state: WorldState) -> None:
        if state.stale:
            await self.alerts.raise_system("stale_data", "warning",
                                           "Simulator reports stale data: automatic approval blocked")
        else:
            await self.alerts.clear_system("stale_data")
        if state.degraded:
            await self.alerts.raise_system("simulator_api", "warning",
                                           f"Simulator API degraded ({', '.join(state.degraded)}): using last good "
                                           "values", {"degraded": state.degraded})
        else:
            await self.alerts.clear_system("simulator_api")
        if not self.store.ok:
            await self.alerts.raise_system("database", "warning",
                                           f"Database unavailable ({self.store.last_error}): running from memory")
        else:
            await self.alerts.clear_system("database")

    # ---------------------------------------------------------------- cycle

    def params(self, state: WorldState) -> PlannerParams:
        return PlannerParams(
            horizon_ticks=self.settings.plan_horizon_ticks,
            safety_z=self.settings.plan_safety_z,
            min_shipment_l=self.settings.min_shipment_l,
            committed_dispatch=self.actuator.dispatched_now(),
        )

    async def run_cycle(self, state: WorldState, trigger: str = "tick") -> PlanRecord | None:
        if self._lock.locked():
            return None  # single-flight: a slow cycle never piles up
        async with self._lock:
            start = time.perf_counter()
            tick = state.instance.tick
            self.last_cycle_tick = tick
            params = self.params(state)
            resp: CycleResponse | None = None
            fallback_reason: str | None = None
            if self.policy == "planner":
                try:
                    resp = await self.intel.cycle(CycleRequest(state=state, stats=self.cache.stats(), params=params))
                except IntelligenceUnavailable as exc:
                    fallback_reason = f"intelligence {exc.reason}: {exc}"[:200]
                    FALLBACK_ACTIVATIONS.labels(exc.reason).inc()
            else:
                fallback_reason = "policy set to heuristic by operator"
                FALLBACK_ACTIVATIONS.labels("policy").inc()
            if resp is None:
                resp = heuristic_cycle(state, params, self.cache.first_seen_quantity())
            await self._fallback_state(fallback_reason)

            self.cache.cycle, self.cache.cycle_tick = resp, tick
            self.cache.cycle_source = resp.plan.policy
            self.cache.tracker.record(resp.forecasts)
            FORECAST_NOISE_FLOOR.set(resp.model.noise_floor)
            STATIONS_AT_RISK.set(sum(1 for r in resp.risks if r.p_stockout.get("4h", 0) >= 0.5))

            plan = await self._new_plan(state, resp, fallback_reason)
            await self._gate_and_submit(state, plan)
            plan.refresh_status()
            self.last_cycle_ms = (time.perf_counter() - start) * 1000
            self.last_policy_used = resp.plan.policy
            self.cycles += 1
            DECISION_CYCLES.labels(resp.plan.policy, "ok").inc()
            DECISION_DURATION.observe(self.last_cycle_ms / 1000)
            if plan.lines:
                PLAN_CONFIDENCE.set(min(ln.confidence for ln in plan.lines))
            await self.store.upsert_plan(plan)
            await self._emit("plan.created", plan.model_dump(mode="json"))
            log.info("decision.cycle", tick=tick, trigger=trigger, policy=resp.plan.policy, lines=len(plan.lines),
                     plan=plan.id, status=plan.status, ms=round(self.last_cycle_ms, 1))
            self._maybe_explain(plan)
            return plan

    async def _fallback_state(self, reason: str | None) -> None:
        was = self.fallback_active
        self.fallback_active = reason is not None
        self.fallback_reason = reason
        if reason and not was:
            log.warning("fallback.activated", reason=reason)
            await self.alerts.raise_system("intelligence", "warning",
                                           f"Heuristic fallback active: {reason}", {"reason": reason})
        elif not reason and was:
            log.info("recovery.completed", component="intelligence")
            await self.alerts.clear_system("intelligence", "planner back in use")

    async def _new_plan(self, state: WorldState, resp: CycleResponse, fallback_reason: str | None) -> PlanRecord:
        # A new plan supersedes every plan still waiting for review.
        for old in self.plans.values():
            changed = False
            for ln in old.lines:
                if ln.status in REVIEWABLE:
                    ln.status = "EXPIRED"
                    changed = True
            if changed:
                old.refresh_status()
                await self._decide(old, None, "system", "expire", "superseded by a newer plan")
                await self.store.upsert_plan(old)
        plan = PlanRecord(
            id=self._next_plan_id, run_id=state.run_id, tick=state.instance.tick, policy=resp.plan.policy,
            mode=self.mode, expires_tick=state.instance.tick + self.settings.plan_ttl_ticks,
            stale_inputs=state.stale, fallback_reason=fallback_reason, result=resp.plan,
            lines=[PlanLineRecord(**ln.model_dump()) for ln in resp.plan.lines],
        )
        self._next_plan_id += 1
        plan.explanation, plan.explanation_source = explain_plan(plan), "template"
        self.plans[plan.id] = plan
        while len(self.plans) > 300:
            self.plans.popitem(last=False)
        return plan

    def review_reason(self, state: WorldState, line: PlanLineRecord) -> str | None:
        """Why a line needs a human, or None if it may be auto-approved in the current mode."""
        if state.stale:
            return "simulator data is stale"
        if self.mode == "manual":
            return "manual mode: every line needs operator approval"
        if self.mode == "auto":
            return None
        if line.confidence < self.settings.auto_approve_min_confidence:
            return f"confidence {line.confidence:.0%} below {self.settings.auto_approve_min_confidence:.0%}"
        if line.quantity > self.settings.auto_approve_max_liters:
            return f"{line.quantity:.0f} L exceeds auto-approval limit {self.settings.auto_approve_max_liters:.0f} L"
        for alert in self.alerts.alerts.values():
            if alert.status != "resolved" and alert.severity == "critical" and alert.kind != "shortage" \
                    and alert.entity_id in (line.station_id, line.route_id, line.depot_id):
                return f"open critical alert: {alert.title}"
        return None

    async def _gate_and_submit(self, state: WorldState, plan: PlanRecord) -> None:
        for line in plan.lines:
            reason = self.review_reason(state, line)
            if reason is not None:
                line.needs_review_reason = reason
                continue
            line.status, line.approved_by = "APPROVED", f"auto:{self.mode}"
            await self._decide(plan, line.line_no, f"auto:{self.mode}", "auto_approve", "passed the mode gate")
            await self.actuator.submit_line(plan, line)
            await self._decide(plan, line.line_no, "system", "submit", line.decision_reason,
                               {"status": line.status, "keys": [s.idempotency_key for s in line.submissions]})

    async def _expire(self, state: WorldState) -> None:
        for plan in list(self.plans.values())[-20:]:
            if plan.run_id == state.run_id and state.instance.tick >= plan.expires_tick:
                changed = False
                for ln in plan.lines:
                    if ln.status in REVIEWABLE:
                        ln.status = "EXPIRED"
                        changed = True
                if changed:
                    plan.refresh_status()
                    await self._decide(plan, None, "system", "expire", f"TTL reached at tick {state.instance.tick}")
                    await self.store.upsert_plan(plan)
                    await self._emit("plan.updated", plan.model_dump(mode="json"))

    # ------------------------------------------------------ operator actions

    def get_plan(self, plan_id: int) -> PlanRecord:
        plan = self.plans.get(plan_id)
        if plan is None:
            raise DecisionError(404, f"plan {plan_id} not found")
        return plan

    def _lines(self, plan: PlanRecord, line_nos: list[int] | None) -> list[PlanLineRecord]:
        if not line_nos:
            return [ln for ln in plan.lines if ln.status in REVIEWABLE]
        by_no = {ln.line_no: ln for ln in plan.lines}
        missing = [n for n in line_nos if n not in by_no]
        if missing:
            raise DecisionError(404, f"plan {plan.id} has no line(s) {missing}")
        return [by_no[n] for n in line_nos]

    async def approve(self, plan_id: int, line_nos: list[int] | None, actor: str, reason: str | None) -> PlanRecord:
        plan = self.get_plan(plan_id)
        state = self.cache.state
        if state is None or plan.run_id != state.run_id:
            raise DecisionError(409, "plan belongs to a previous simulator run")
        lines = self._lines(plan, line_nos)
        not_reviewable = [ln.line_no for ln in lines if ln.status not in REVIEWABLE]
        if not_reviewable:
            raise DecisionError(409, f"line(s) {not_reviewable} are not awaiting approval")
        if not lines:
            raise DecisionError(409, "nothing to approve")
        for line in lines:
            line.status, line.approved_by = "APPROVED", actor
            await self._decide(plan, line.line_no, actor, "approve", reason)
            await self.actuator.submit_line(plan, line)
            await self._decide(plan, line.line_no, "system", "submit", line.decision_reason,
                               {"status": line.status, "keys": [s.idempotency_key for s in line.submissions]})
        plan.refresh_status()
        await self.store.upsert_plan(plan)
        await self._emit("plan.updated", plan.model_dump(mode="json"))
        return plan

    async def reject(self, plan_id: int, line_nos: list[int] | None, actor: str, reason: str | None) -> PlanRecord:
        plan = self.get_plan(plan_id)
        lines = self._lines(plan, line_nos)
        bad = [ln.line_no for ln in lines if ln.status not in REVIEWABLE]
        if bad:
            raise DecisionError(409, f"line(s) {bad} are not awaiting approval")
        for line in lines:
            line.status, line.decision_reason = "REJECTED", reason
            await self._decide(plan, line.line_no, actor, "reject", reason)
        plan.refresh_status()
        await self.store.upsert_plan(plan)
        await self._emit("plan.updated", plan.model_dump(mode="json"))
        return plan

    async def modify(self, plan_id: int, line_no: int, quantity: float, route_id: str | None, actor: str,
                     reason: str | None) -> tuple[PlanRecord, EvaluateResponse]:
        plan = self.get_plan(plan_id)
        line = self._lines(plan, [line_no])[0]
        if line.status not in REVIEWABLE:
            raise DecisionError(409, f"line {line_no} is not awaiting approval")
        state = self.cache.state
        if state is None:
            raise DecisionError(503, "no world state yet")
        routes = {r.id: r for r in state.routes}
        new_route = routes.get(route_id or line.route_id)
        if new_route is None or new_route.destination_station_id != line.station_id:
            raise DecisionError(422, "route must exist and deliver to the same station")
        if quantity <= 0 or quantity > 5 * new_route.max_shipment:
            raise DecisionError(422, "quantity must be > 0 and at most 5 x max_shipment")
        evaluation = await self.evaluate([FixedLine(route_id=new_route.id, fuel_type=line.fuel_type,
                                                    quantity=quantity)])
        before = {"route_id": line.route_id, "quantity": line.quantity}
        parts, remaining = [], quantity
        while remaining > new_route.max_shipment:
            parts.append(float(new_route.max_shipment))
            remaining -= new_route.max_shipment
        parts.append(float(remaining))
        line.route_id, line.depot_id = new_route.id, new_route.source_depot_id
        line.quantity, line.parts = float(quantity), parts
        line.arrival_tick = state.instance.tick + 1 + new_route.transit_ticks
        line.status, line.decision_reason = "MODIFIED", reason
        after = {(p.station_id, p.fuel_type): p for p in evaluation.after}.get((line.station_id, line.fuel_type))
        if after is not None:
            line.risk_after.projected_unmet_liters = after.unmet_total
            line.risk_after.end_inventory_liters = after.inventory[-1] if after.inventory else 0.0
        await self._decide(plan, line_no, actor, "modify", reason,
                           {"before": before, "after": {"route_id": new_route.id, "quantity": quantity}})
        await self.store.upsert_plan(plan)
        await self._emit("plan.updated", plan.model_dump(mode="json"))
        return plan, evaluation

    async def evaluate(self, lines: list[FixedLine]) -> EvaluateResponse:
        state = self.cache.state
        if state is None:
            raise DecisionError(503, "no world state yet")
        params = self.params(state)
        req = EvaluateRequest(state=state, stats=self.cache.stats(), lines=lines, params=params)
        try:
            if self.policy == "planner":
                return await self.intel.evaluate(req)
        except IntelligenceUnavailable:
            pass
        try:
            return local_evaluate(state, prior_forecast(state, params.horizon_ticks), lines, params.horizon_ticks)
        except ValueError as exc:
            raise DecisionError(422, str(exc)) from exc

    async def cancel_allocation(self, allocation_id: int, actor: str) -> dict:
        allocation = await self.actuator.cancel(allocation_id)
        for plan in reversed(self.plans.values()):
            for line in plan.lines:
                for s in line.submissions:
                    if s.allocation_id == allocation_id:
                        s.status = allocation.status
                        await self._decide(plan, line.line_no, actor, "cancel", f"allocation {allocation_id}")
                        self.actuator.refresh([plan])
                        await self.store.upsert_plan(plan)
                        return allocation.model_dump(mode="json")
        return allocation.model_dump(mode="json")

    async def set_mode(self, mode: Mode, actor: str) -> None:
        self.mode = mode
        await self.store.audit(actor, "set_mode", {"mode": mode})
        log.info("decision.mode_changed", mode=mode, actor=actor)

    async def set_policy(self, policy: str, actor: str) -> None:
        self.policy = policy
        await self.store.audit(actor, "set_policy", {"policy": policy})
        log.info("decision.policy_changed", policy=policy, actor=actor)

    # ----------------------------------------------------------- helpers

    async def _decide(self, plan: PlanRecord, line_no: int | None, actor: str, action: str, reason: str | None,
                      detail: dict | None = None) -> Decision:
        d = Decision(id=self._next_decision_id, run_id=plan.run_id, plan_id=plan.id, line_no=line_no, actor=actor,
                     action=action, reason=reason, tick=self.cache.tick if self.cache.tick is not None else plan.tick,
                     detail=detail or {})
        self._next_decision_id += 1
        self.decisions.append(d)
        await self.store.add_decision(d)
        log.info(f"decision.{action}", plan=plan.id, line=line_no, actor=actor, reason=reason)
        return d

    def _maybe_explain(self, plan: PlanRecord) -> None:
        if not self.explainer.configured or self.explainer.mode == "off" or not plan.lines:
            return
        if self.explainer.mode == "review" and not any(ln.status in REVIEWABLE for ln in plan.lines):
            return
        plan.explanation_source = "pending"
        task = asyncio.create_task(self._explain(plan))
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _explain(self, plan: PlanRecord) -> None:
        text, source = await self.explainer.explain_plan(plan)
        plan.explanation, plan.explanation_source = text, source
        await self.store.upsert_plan(plan)
        await self._emit("plan.updated", plan.model_dump(mode="json"))

    async def explain_now(self, plan_id: int) -> PlanRecord:
        plan = self.get_plan(plan_id)
        text, source = await self.explainer.explain_plan(plan)
        plan.explanation, plan.explanation_source = text, source
        await self.store.upsert_plan(plan)
        return plan

    async def _emit(self, event: str, data: dict[str, Any]) -> None:
        if self.publish is None:
            return
        res = self.publish(event, data)
        if hasattr(res, "__await__"):
            await res

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled, "mode": self.mode, "policy": self.policy,
            "last_policy_used": self.last_policy_used, "fallback_active": self.fallback_active,
            "fallback_reason": self.fallback_reason, "last_cycle_tick": self.last_cycle_tick,
            "last_cycle_ms": round(self.last_cycle_ms, 1) if self.last_cycle_ms is not None else None,
            "cycles": self.cycles, "plans": len(self.plans), "decisions": len(self.decisions),
        }
