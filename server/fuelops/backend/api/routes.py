"""Operator API (architecture.md §7). Reads are served from the cache/DB, never proxied to the simulator.

Mutating endpoints require `Authorization: Bearer <OPERATOR_TOKEN>`.
"""

from __future__ import annotations

import hmac
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from fuelops.backend.container import Container
from fuelops.backend.decision.orchestrator import DecisionError
from fuelops.backend.integration.sim_errors import ErrorKind, SimError
from fuelops.shared import spec
from fuelops.shared.models import FixedLine
from fuelops.shared.sim_models import EventInjection, FaultInjection
from fuelops.shared.world import in_transit_arrivals, project_series, relative_arrivals, station_open_at, supply_etas

router = APIRouter(prefix="/api")


def container(request: Request) -> Container:
    return request.app.state.container


def require_operator(request: Request, authorization: str | None = Header(default=None)) -> str:
    token = request.app.state.container.settings.operator_token
    if token is None or not token.get_secret_value():
        raise HTTPException(503, "OPERATOR_TOKEN is not configured on the server")
    scheme, _, supplied = (authorization or "").strip().partition(" ")
    supplied = supplied.strip()
    if scheme.lower() != "bearer" or not supplied \
            or not hmac.compare_digest(supplied.encode(), token.get_secret_value().encode()):
        raise HTTPException(401, "operator token required", headers={"WWW-Authenticate": "Bearer"})
    return "operator"


def _state(c: Container):
    if c.cache.state is None:
        raise HTTPException(503, "no world state yet: waiting for the first successful simulator sync")
    return c.cache.state


def _decision(exc: DecisionError) -> HTTPException:
    return HTTPException(exc.status, exc.message)


async def _resync(c: Container) -> None:
    """After an operator action, re-read the simulator so the next GET shows the new allocations (REST is truth)."""
    try:
        await c.sync.sync_once(force=True)
    except Exception:  # noqa: BLE001 - the action already succeeded; the background loop will catch up
        c.sync.hint(force=True)


def _sim_http(exc: SimError) -> HTTPException:
    status = exc.status if exc.kind is ErrorKind.DOMAIN and exc.status else 502
    return HTTPException(status, {"code": exc.code or exc.kind.value, "message": exc.message})


# ------------------------------------------------------------------ operator sign-in


@router.get("/auth/verify")
async def verify_operator(actor: str = Depends(require_operator)) -> dict[str, Any]:
    """Side-effect-free token check for the login page: 200 valid, 401 rejected, 503 no OPERATOR_TOKEN."""
    return {"ok": True, "actor": actor}


# ------------------------------------------------------------------ health / state


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health")
async def health(c: Container = Depends(container)) -> dict[str, Any]:
    return c.health()


@router.get("/state")
async def state(c: Container = Depends(container)) -> dict[str, Any]:
    s = _state(c)
    return {
        "world": s.model_dump(mode="json"),
        "freshness": {"stale": s.stale, "degraded": s.degraded, "synced_at": s.synced_at,
                      "sync_ok": c.cache.last_sync_ok, "sse_connected": bool(c.sse and c.sse.connected)},
        "decision": c.orchestrator.status(),
        "cycle_tick": c.cache.cycle_tick,
        "cycle_source": c.cache.cycle_source,
    }


@router.get("/risks")
async def risks(c: Container = Depends(container)) -> list[dict[str, Any]]:
    if c.cache.cycle is None:
        return []
    items = [r.model_dump(mode="json") for r in c.cache.cycle.risks]
    return sorted(items, key=lambda r: -r["p_stockout"].get("12h", 0))


@router.get("/demand/regional")
async def regional_demand(ticks: int = Query(4, ge=1, le=96), c: Container = Depends(container)) -> dict[str, Any]:
    s = _state(c)
    region_of = {st.id: st.region_id for st in s.stations}
    out: dict[str, dict[str, float]] = {}
    for p in c.cache.recent_history(ticks=ticks):
        region = region_of.get(p.station_id, "unknown")
        out.setdefault(region, {f: 0.0 for f in spec.FUELS})
        out[region][p.fuel_type] += p.demand
    forecast: dict[str, dict[str, float]] = {}
    if c.cache.cycle is not None:
        for f in c.cache.cycle.forecasts:
            region = region_of.get(f.station_id, "unknown")
            forecast.setdefault(region, {x: 0.0 for x in spec.FUELS})
            forecast[region][f.fuel_type] += sum(f.mu[:ticks])
    return {"window_ticks": ticks, "observed": out, "forecast_next": forecast}


@router.get("/stations/{station_id}")
async def station_detail(station_id: str, history_ticks: int = Query(96, ge=4, le=672),
                         c: Container = Depends(container)) -> dict[str, Any]:
    s = _state(c)
    st = next((x for x in s.stations if x.id == station_id), None)
    if st is None:
        raise HTTPException(404, f"station {station_id} not found")
    t0 = s.instance.tick
    cycle = c.cache.cycle
    fc = {(f.station_id, f.fuel_type): f for f in (cycle.forecasts if cycle else [])}
    arrivals = in_transit_arrivals(s)
    plan = next((p for p in reversed(c.orchestrator.plans.values()) if p.run_id == s.run_id), None)
    fuels = {}
    for fuel in spec.FUELS:
        series = fc.get((st.id, fuel))
        risk = next((r for r in (cycle.risks if cycle else []) if r.station_id == st.id and r.fuel_type == fuel), None)
        base_arr = dict(relative_arrivals(arrivals.get((st.id, fuel), {}), t0))
        with_arr = dict(base_arr)
        if plan is not None and plan.tick == t0:
            for ln in plan.lines:
                if ln.station_id == st.id and ln.fuel_type == fuel and ln.status not in ("REJECTED", "EXPIRED"):
                    k = ln.arrival_tick - t0
                    with_arr[k] = with_arr.get(k, 0.0) + ln.quantity
        projection = {}
        if series is not None:
            open_at = [station_open_at(s, st, t0 + k) for k in range(1, len(series.mu) + 1)]
            projection["without_plan"] = project_series(st.inventory.get(fuel, 0.0), st.capacity.get(fuel, 0.0),
                                                        series.mu, base_arr, open_at)[0]
            projection["with_plan"] = project_series(st.inventory.get(fuel, 0.0), st.capacity.get(fuel, 0.0),
                                                     series.mu, with_arr, open_at)[0]
        history = [{"tick": p.tick, "demand": round(p.demand, 2), "served": p.served}
                   for p in c.cache.recent_history(st.id, history_ticks) if p.fuel_type == fuel]
        fuels[fuel] = {
            "inventory": st.inventory.get(fuel, 0.0), "capacity": st.capacity.get(fuel, 0.0),
            "risk": risk.model_dump(mode="json") if risk else None,
            "forecast": series.model_dump(mode="json") if series else None,
            "projection": {k: [round(v, 1) for v in val] for k, val in projection.items()},
            "history": history,
        }
    return {"station": st.model_dump(mode="json"), "tick": t0, "tick_minutes": s.instance.tick_minutes,
            "routes": [r.model_dump(mode="json") for r in s.routes if r.destination_station_id == st.id],
            "fuels": fuels}


@router.get("/depots/{depot_id}")
async def depot_detail(depot_id: str, c: Container = Depends(container)) -> dict[str, Any]:
    s = _state(c)
    d = next((x for x in s.depots if x.id == depot_id), None)
    if d is None:
        raise HTTPException(404, f"depot {depot_id} not found")
    etas = [e.model_dump(mode="json") for e in supply_etas(s, c.cache.first_seen_quantity()) if e.depot_id == d.id]
    return {"depot": d.model_dump(mode="json"), "supply": etas,
            "routes": [r.model_dump(mode="json") for r in s.routes if r.source_depot_id == d.id]}


@router.get("/intelligence")
async def intelligence(c: Container = Depends(container)) -> dict[str, Any]:
    cycle = c.cache.cycle
    return {
        "model": cycle.model.model_dump() if cycle else None,
        "source": c.cache.cycle_source,
        "forecast_accuracy": c.cache.tracker.summary(),
        "noise_floor": cycle.model.noise_floor if cycle else None,
        "decision": c.orchestrator.status(),
        "llm": c.explainer.status(),
    }


# ------------------------------------------------------------------- alerts


@router.get("/alerts")
async def alerts(status: Literal["active", "open", "acked", "resolved", "all"] = "active",
                 c: Container = Depends(container)) -> list[dict[str, Any]]:
    return [a.model_dump(mode="json") for a in c.alerts.list_alerts(None if status == "all" else status)]


@router.post("/alerts/{alert_id:path}/ack")
async def ack_alert(alert_id: str, c: Container = Depends(container),
                    actor: str = Depends(require_operator)) -> dict[str, Any]:
    alert = await c.alerts.ack(alert_id, actor)
    if alert is None:
        raise HTTPException(404, f"alert {alert_id} not found")
    return alert.model_dump(mode="json")


@router.get("/incidents")
async def incidents(status: Literal["open", "resolved", "all"] = "all",
                    c: Container = Depends(container)) -> list[dict[str, Any]]:
    return [i.model_dump(mode="json") for i in c.alerts.list_incidents(None if status == "all" else status)]


@router.get("/incidents/{incident_id:path}")
async def incident(incident_id: str, c: Container = Depends(container)) -> dict[str, Any]:
    inc = c.alerts.incidents.get(incident_id)
    if inc is None:
        raise HTTPException(404, f"incident {incident_id} not found")
    return {**inc.model_dump(mode="json"),
            "alerts": [c.alerts.alerts[a].model_dump(mode="json") for a in inc.alert_ids if a in c.alerts.alerts]}


# -------------------------------------------------------------------- plans


class LineSelection(BaseModel):
    lines: list[int] | None = None
    reason: str | None = Field(default=None, max_length=500)


class ModifyRequest(BaseModel):
    quantity: float = Field(gt=0)
    route_id: str | None = None
    reason: str | None = Field(default=None, max_length=500)


class EvaluateBody(BaseModel):
    lines: list[FixedLine] = Field(min_length=1, max_length=50)


@router.get("/plans")
async def plans(limit: int = Query(20, ge=1, le=300), c: Container = Depends(container)) -> list[dict[str, Any]]:
    items = list(c.orchestrator.plans.values())[-limit:]
    return [p.model_dump(mode="json") for p in reversed(items)]


@router.get("/plans/current")
async def current_plan(c: Container = Depends(container)) -> dict[str, Any] | None:
    s = c.cache.state
    for p in reversed(c.orchestrator.plans.values()):
        if s is None or p.run_id == s.run_id:
            return p.model_dump(mode="json")
    return None


@router.get("/plans/{plan_id}")
async def plan(plan_id: int, c: Container = Depends(container)) -> dict[str, Any]:
    try:
        return c.orchestrator.get_plan(plan_id).model_dump(mode="json")
    except DecisionError as exc:
        raise _decision(exc) from exc


@router.post("/plans/compute")
async def compute(c: Container = Depends(container), actor: str = Depends(require_operator)) -> dict[str, Any]:
    s = _state(c)
    p = await c.orchestrator.run_cycle(s, trigger="operator")
    if p is None:
        raise HTTPException(409, "a decision cycle is already running")
    return p.model_dump(mode="json")


@router.post("/plans/{plan_id}/approve")
async def approve(plan_id: int, body: LineSelection | None = None, c: Container = Depends(container),
                  actor: str = Depends(require_operator)) -> dict[str, Any]:
    body = body or LineSelection()
    try:
        plan = await c.orchestrator.approve(plan_id, body.lines, actor, body.reason)
    except DecisionError as exc:
        raise _decision(exc) from exc
    await _resync(c)
    return plan.model_dump(mode="json")


@router.post("/plans/{plan_id}/reject")
async def reject(plan_id: int, body: LineSelection | None = None, c: Container = Depends(container),
                 actor: str = Depends(require_operator)) -> dict[str, Any]:
    body = body or LineSelection()
    try:
        return (await c.orchestrator.reject(plan_id, body.lines, actor, body.reason)).model_dump(mode="json")
    except DecisionError as exc:
        raise _decision(exc) from exc


@router.post("/plans/{plan_id}/lines/{line_no}/modify")
async def modify(plan_id: int, line_no: int, body: ModifyRequest, c: Container = Depends(container),
                 actor: str = Depends(require_operator)) -> dict[str, Any]:
    try:
        p, evaluation = await c.orchestrator.modify(plan_id, line_no, body.quantity, body.route_id, actor, body.reason)
    except DecisionError as exc:
        raise _decision(exc) from exc
    return {"plan": p.model_dump(mode="json"), "evaluation": evaluation.model_dump(mode="json")}


@router.post("/plans/{plan_id}/explain")
async def explain(plan_id: int, c: Container = Depends(container),
                  actor: str = Depends(require_operator)) -> dict[str, Any]:
    try:
        p = await c.orchestrator.explain_now(plan_id)
    except DecisionError as exc:
        raise _decision(exc) from exc
    return {"explanation": p.explanation, "source": p.explanation_source}


@router.post("/evaluate")
async def evaluate(body: EvaluateBody, c: Container = Depends(container)) -> dict[str, Any]:
    try:
        return (await c.orchestrator.evaluate(body.lines)).model_dump(mode="json")
    except DecisionError as exc:
        raise _decision(exc) from exc


@router.get("/decisions")
async def decisions(limit: int = Query(200, ge=1, le=2000), c: Container = Depends(container)) -> list[dict]:
    items = list(c.orchestrator.decisions)[-limit:]
    return [d.model_dump(mode="json") for d in reversed(items)]


# ---------------------------------------------------------- supply / shipments


@router.get("/supply")
async def supply(c: Container = Depends(container)) -> dict[str, Any]:
    s = _state(c)
    etas = supply_etas(s, c.cache.first_seen_quantity())
    return {
        "upcoming": [e.model_dump(mode="json") for e in sorted(etas, key=lambda e: e.eta_tick)],
        "arrived": [a.model_dump(mode="json") for a in s.supply_arrivals if a.status == "ARRIVED"],
        "first_seen": {k: {"planned_tick": v[0], "quantity": v[1]} for k, v in c.cache.first_seen_supply.items()},
    }


@router.get("/allocations")
async def allocations(c: Container = Depends(container)) -> list[dict[str, Any]]:
    s = _state(c)
    owner: dict[int, dict[str, Any]] = {}
    for p in c.orchestrator.plans.values():
        for ln in p.lines:
            for sub in ln.submissions:
                if sub.allocation_id is not None:
                    owner[sub.allocation_id] = {"plan_id": p.id, "line_no": ln.line_no, "policy": p.policy,
                                                "approved_by": ln.approved_by}
    return [{**a.model_dump(mode="json"), "plan": owner.get(a.id)} for a in s.allocations]


@router.post("/allocations/{allocation_id}/cancel")
async def cancel(allocation_id: int, c: Container = Depends(container),
                 actor: str = Depends(require_operator)) -> dict[str, Any]:
    try:
        result = await c.orchestrator.cancel_allocation(allocation_id, actor)
    except SimError as exc:
        raise _sim_http(exc) from exc
    await _resync(c)
    return result


# ----------------------------------------------------------------- settings


class SettingsUpdate(BaseModel):
    mode: Literal["manual", "assisted", "auto"] | None = None
    policy: Literal["planner", "heuristic"] | None = None
    decisions_enabled: bool | None = None


@router.get("/settings")
async def get_settings_view(c: Container = Depends(container)) -> dict[str, Any]:
    s = c.settings
    return {"mode": c.orchestrator.mode, "policy": c.orchestrator.policy,
            "decisions_enabled": c.orchestrator.enabled, "plan_horizon_ticks": s.plan_horizon_ticks,
            "plan_ttl_ticks": s.plan_ttl_ticks, "auto_approve_min_confidence": s.auto_approve_min_confidence,
            "auto_approve_max_liters": s.auto_approve_max_liters, "chaos_enabled": s.chaos_enabled,
            "llm": c.explainer.status(), "operator_token_configured": bool(s.operator_token)}


@router.put("/settings")
async def put_settings(body: SettingsUpdate, c: Container = Depends(container),
                       actor: str = Depends(require_operator)) -> dict[str, Any]:
    if body.mode is not None:
        await c.orchestrator.set_mode(body.mode, actor)
    if body.policy is not None:
        await c.orchestrator.set_policy(body.policy, actor)
    if body.decisions_enabled is not None:
        c.orchestrator.enabled = body.decisions_enabled
        await c.store.audit(actor, "set_decisions_enabled", {"enabled": body.decisions_enabled})
    return await get_settings_view(c)


@router.post("/briefing")
async def briefing(c: Container = Depends(container), actor: str = Depends(require_operator)) -> dict[str, Any]:
    s = _state(c)
    text, source = await c.explainer.briefing(s, c.cache.cycle.risks if c.cache.cycle else [],
                                              c.alerts.list_alerts("active"))
    return {"text": text, "source": source, "tick": s.instance.tick}


@router.get("/metrics/timeline")
async def metrics_timeline(limit: int = Query(500, ge=1, le=5000), c: Container = Depends(container)) -> list[dict]:
    return await c.store.metrics_timeline(c.cache.run_id, limit)


@router.get("/audit")
async def audit(limit: int = Query(100, ge=1, le=1000), c: Container = Depends(container),
                actor: str = Depends(require_operator)) -> list[dict]:
    return await c.store.recent_audit(limit)


@router.get("/stream")
async def stream(c: Container = Depends(container)) -> StreamingResponse:
    return StreamingResponse(c.broadcaster.stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# -------------------------------------------------------------------- chaos


def require_chaos(c: Container = Depends(container), actor: str = Depends(require_operator)) -> str:
    if not c.settings.chaos_enabled:
        raise HTTPException(403, "chaos proxy disabled (CHAOS_ENABLED=false)")
    return actor


class RelativeEvent(BaseModel):
    type: str
    start_in_ticks: int = Field(default=0, ge=0)
    duration_ticks: int = Field(gt=0)
    parameters: dict[str, Any] = Field(default_factory=dict)


@router.post("/chaos/events")
async def chaos_event(body: RelativeEvent, c: Container = Depends(container),
                      actor: str = Depends(require_chaos)) -> Any:
    tick = c.cache.tick or 0
    try:
        event = EventInjection(type=body.type, start_tick=tick + body.start_in_ticks,
                               duration_ticks=body.duration_ticks, parameters=body.parameters)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        result = await c.client.admin_inject_event(event)
    except SimError as exc:
        raise _sim_http(exc) from exc
    await c.store.audit(actor, "chaos.event", event.model_dump())
    c.sync.hint(force=True)
    return result


@router.post("/chaos/faults")
async def chaos_fault(body: FaultInjection, c: Container = Depends(container),
                      actor: str = Depends(require_chaos)) -> Any:
    try:
        result = await c.client.admin_inject_fault(body)
    except SimError as exc:
        raise _sim_http(exc) from exc
    await c.store.audit(actor, "chaos.fault", body.model_dump())
    return result


class DbFault(BaseModel):
    down: bool


@router.post("/chaos/app/database")
async def chaos_db(body: DbFault, c: Container = Depends(container), actor: str = Depends(require_chaos)) -> dict:
    c.store.force_fault(body.down)
    return {"database_down": body.down}


@router.post("/chaos/{action:path}")
async def chaos_action(action: Literal["run", "pause", "step", "reset", "faults/clear", "toggle"],
                       c: Container = Depends(container), actor: str = Depends(require_chaos)) -> Any:
    try:
        result = await c.client.admin_post(action)
    except SimError as exc:
        raise _sim_http(exc) from exc
    await c.store.audit(actor, f"chaos.{action}", {})
    c.sync.hint(force=True)
    return result


@router.get("/chaos/status")
async def chaos_status(c: Container = Depends(container)) -> dict[str, Any]:
    out: dict[str, Any] = {"database_down": c.store.forced_fault}
    for path in ("faults", "events"):
        try:
            out[path] = await c.client.admin_get(path)
        except SimError as exc:
            out[path] = {"error": exc.message}
    return out
