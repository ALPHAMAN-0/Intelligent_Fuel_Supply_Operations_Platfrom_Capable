"""Deterministic explanations: the always-available fallback for the LLM (and the facts the LLM is given)."""

from __future__ import annotations

from typing import Any

from fuelops.backend.domain import Alert, Incident, PlanRecord
from fuelops.shared.models import RiskScore, WorldState


def plan_facts(plan: PlanRecord) -> dict[str, Any]:
    r = plan.result
    return {
        "simulated": True,
        "tick": plan.tick,
        "policy": plan.policy,
        "fallback_reason": plan.fallback_reason,
        "stale_inputs": plan.stale_inputs,
        "projected_unmet_liters_without_plan": r.projected_unmet_before,
        "projected_unmet_liters_with_plan": r.projected_unmet_after,
        "horizon_ticks": r.horizon_ticks,
        "binding_constraints": r.binding_constraints,
        "rationing": r.rationing,
        "lines": [
            {
                "line": ln.line_no, "status": ln.status, "route": ln.route_id, "from": ln.depot_id,
                "to": ln.station_id, "fuel": ln.fuel_type, "liters": ln.quantity, "arrival_tick": ln.arrival_tick,
                "signals": ln.signals, "binding": ln.binding, "confidence": ln.confidence,
                "risk_before": ln.risk_before.model_dump(), "risk_after": ln.risk_after.model_dump(),
                "alternatives": [a.model_dump() for a in ln.alternatives],
                "needs_review": ln.needs_review_reason,
            }
            for ln in plan.lines
        ],
    }


def explain_plan(plan: PlanRecord) -> str:
    r = plan.result
    if not plan.lines:
        head = f"Tick {plan.tick}: no shipment needed this tick ({plan.policy} policy)."
        if r.rationing:
            head += " Shortfall is projected even so: " + "; ".join(r.rationing[:2]) + "."
        return head
    total = sum(ln.quantity for ln in plan.lines)
    saved = r.projected_unmet_before - r.projected_unmet_after
    parts = [
        f"Tick {plan.tick}: {len(plan.lines)} shipment(s), {total:,.0f} L in total ({plan.policy} policy). "
        f"Projected unmet demand over the next {r.horizon_ticks} ticks falls from {r.projected_unmet_before:,.0f} L "
        f"to {r.projected_unmet_after:,.0f} L ({saved:,.0f} L avoided)."
    ]
    top = sorted(plan.lines, key=lambda ln: -ln.risk_before.p_stockout)[:3]
    for ln in top:
        before, after = ln.risk_before, ln.risk_after
        when = (f"stockout in {before.hours_to_stockout:.1f} h" if before.hours_to_stockout is not None
                else "no stockout within the horizon")
        parts.append(
            f"- {ln.station_id} {ln.fuel_type}: {when} without action; ship {ln.quantity:,.0f} L via {ln.route_id} "
            f"(arrives tick {ln.arrival_tick}); P(stockout within {before.horizon_hours:.0f} h) "
            f"{before.p_stockout:.0%} -> {after.p_stockout:.0%}; stock at horizon end "
            f"{before.end_inventory_liters:,.0f} -> {after.end_inventory_liters:,.0f} L; confidence {ln.confidence:.0%}."
        )
    if r.binding_constraints:
        parts.append("Constraints: " + "; ".join(r.binding_constraints[:3]) + ".")
    if plan.fallback_reason:
        parts.append(f"Fallback active: {plan.fallback_reason}.")
    if plan.stale_inputs:
        parts.append("Inputs were stale: automatic approval is blocked.")
    return "\n".join(parts)


def summarize_incident(inc: Incident, alerts: list[Alert]) -> str:
    active = [a for a in alerts if a.status != "resolved"]
    lines = [f"{inc.title} ({inc.severity}, {inc.status}). Opened at tick {inc.opened_tick}"
             + (f", resolved at tick {inc.resolved_tick}." if inc.resolved_tick is not None else ".")]
    if active:
        lines.append("Active signals: " + "; ".join(a.title for a in active[:4]) + ".")
    if inc.timeline:
        lines.append(f"Latest: {inc.timeline[-1].text}")
    return "\n".join(lines)


def network_briefing(state: WorldState, risks: list[RiskScore], alerts: list[Alert]) -> str:
    m = state.metrics
    at_risk = sorted((r for r in risks if r.p_stockout.get("12h", 0) >= 0.5),
                     key=lambda r: -r.p_stockout.get("12h", 0))
    critical = [a for a in alerts if a.status != "resolved" and a.severity == "critical"]
    lines = [f"Simulated network at tick {state.instance.tick} ({state.instance.sim_time:%Y-%m-%d %H:%M}), "
             f"simulation {state.instance.status.lower()}."]
    if m is not None:
        lines.append(f"Service level {m.service_level:.2%}; unmet demand {m.unmet_demand_liters:,.0f} L; "
                     f"{m.allocation_failures} failed allocations.")
    lines.append(f"{len(at_risk)} station/fuel series at >=50% stockout risk within 12 h"
                 + (": " + ", ".join(f"{r.station_id} {r.fuel_type}" for r in at_risk[:4]) if at_risk else "."))
    if critical:
        lines.append("Critical alerts: " + "; ".join(a.title for a in critical[:3]) + ".")
    if state.stale:
        lines.append("Warning: simulator data is stale.")
    return "\n".join(lines)
