"""Replay a crisis scenario against the running stack and record how the platform detects, evaluates, responds,
explains and recovers (R-CRISIS-01, plan.json crisis_scenarios). RESETS THE SIMULATOR.

    docker compose run --rm tools scripts/run_scenario.py scenarios/demand-spike.yaml
    docker compose run --rm tools scripts/run_scenario.py scenarios/*.yaml        # all five, writes docs/scenarios/

Each scenario file sets the operating mode, the crisis events (ticks relative to the reset), optional simulator
faults, how many ticks to run and machine-checked expectations. Exit code 1 if any expectation fails.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fuelops_client import StackClient, operator_token, service_level, stockout_series  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def run(client: StackClient, scn: dict[str, Any]) -> dict[str, Any]:
    previous = client.get("/api/settings")
    client.reset()
    client.settings(mode=scn.get("mode", "auto"), policy=scn.get("policy", "planner"), decisions_enabled=True)
    for ev in scn.get("events", []):
        client.admin("events", {"type": ev["type"], "start_tick": ev.get("start_tick", 0),
                                "duration_ticks": ev["duration_ticks"], "parameters": ev.get("parameters", {})})
    faults = {f["at_tick"]: f for f in scn.get("faults", [])}

    timeline, seen_alerts, seen_incidents, min_sl = [], {}, {}, 1.0
    for tick in range(1, scn["ticks"] + 1):
        if tick in faults:
            f = faults[tick]
            client.admin("faults", {"type": f["type"], "duration_seconds": f["duration_seconds"],
                                    "parameters": f.get("parameters", {})})
        state = client.step(tick)
        alerts = client.get("/api/alerts", status="all")
        incidents = client.get("/api/incidents", status="all")
        plan = client.get("/api/plans/current") or {}
        top = next(iter(client.get("/api/risks")), None)
        new_alerts = [a for a in alerts if a["id"] not in seen_alerts and a["kind"] != "system"]
        seen_alerts.update({a["id"]: a for a in alerts})
        opened = [i for i in incidents if i["id"] not in seen_incidents]
        seen_incidents.update({i["id"]: i for i in incidents})
        sl = service_level(state)
        min_sl = min(min_sl, sl)
        timeline.append({
            "tick": tick, "service_level": round(sl, 4), "stockout_series": stockout_series(state),
            "top_risk": ({"series": f"{top['station_id']} {top['fuel_type']}",
                          "p_stockout_12h": round(top["p_stockout"].get("12h", 0.0), 3)} if top else None),
            "open_alerts": sum(a["status"] != "resolved" for a in alerts),
            "new_alerts": [f"{a['severity']} {a['kind']}: {a['title']}" for a in new_alerts],
            "incidents_opened": [i["title"] for i in opened],
            "plan": {"id": plan.get("id"), "policy": plan.get("policy"), "status": plan.get("status"),
                     "lines": [f"{ln['depot_id']}->{ln['station_id']} {ln['fuel_type']} {ln['quantity']:.0f} L "
                               f"[{ln['status']}]" for ln in plan.get("lines", [])]},
            "fallback": state["decision"]["fallback_active"], "stale": state["world"].get("stale", False),
        })

    final_state = client.state()
    alerts = client.get("/api/alerts", status="all")
    incidents = client.get("/api/incidents", status="all")
    allocations = client.get("/api/allocations")
    decisions = client.get("/api/decisions", limit=2000)
    plans = client.get("/api/plans", limit=300)
    explanation = next((p["explanation"] for p in plans if p["lines"]), "")
    peak = max((t for t in timeline if t["top_risk"]), key=lambda t: t["top_risk"]["p_stockout_12h"], default=None)
    client.settings(mode=previous["mode"], policy=previous["policy"], decisions_enabled=previous["decisions_enabled"])

    facts = {
        "alert_kinds": sorted({a["kind"] for a in alerts}),
        "open_alert_kinds_at_end": sorted({a["kind"] for a in alerts if a["status"] != "resolved"}),
        "incidents": [{"title": i["title"], "status": i["status"], "opened_tick": i["opened_tick"],
                       "resolved_tick": i["resolved_tick"]} for i in incidents],
        "plan_line_stations": sorted({ln["station_id"] for p in plans for ln in p["lines"]}),
        "allocation_routes": sorted({a["route_id"] for a in allocations}),
        "allocations": len(allocations),
        "auto_approved": sum(d["action"] == "auto_approve" for d in decisions),
        "needs_review": sum(1 for p in plans for ln in p["lines"] if ln.get("needs_review_reason")),
        "final_service_level": round(service_level(final_state), 4),
        "min_service_level": round(min_sl, 4),
        "peak_risk": ({"tick": peak["tick"], **peak["top_risk"]} if peak else None),
        "fallback_ticks": sum(t["fallback"] for t in timeline),
        "stale_ticks": sum(t["stale"] for t in timeline),
    }
    return {"scenario": scn, "timeline": timeline, "facts": facts, "explanation": explanation,
            "checks": check(scn.get("expect", {}), facts)}


def check(expect: dict[str, Any], facts: dict[str, Any]) -> list[dict[str, Any]]:
    out = []

    def add(name: str, ok: bool, detail: str) -> None:
        out.append({"check": name, "ok": bool(ok), "detail": detail})

    for kind in expect.get("alerts_raised", []):
        add(f"alert raised: {kind}", kind in facts["alert_kinds"], f"kinds seen {facts['alert_kinds']}")
    if expect.get("incident_opened"):
        add("incident opened", facts["incidents"], f"{len(facts['incidents'])} incident(s)")
    for station in expect.get("plan_lines_to", []):
        add(f"plan line to {station}", station in facts["plan_line_stations"], f"{facts['plan_line_stations']}")
    for route in expect.get("allocations_via", []):
        add(f"allocation via {route}", route in facts["allocation_routes"], f"{facts['allocation_routes']}")
    for route in expect.get("no_allocations_via", []):
        add(f"no allocation via {route}", route not in facts["allocation_routes"], f"{facts['allocation_routes']}")
    for kind in expect.get("resolved_by_end", []):
        add(f"{kind} alerts resolved by the end", kind not in facts["open_alert_kinds_at_end"],
            f"still open: {facts['open_alert_kinds_at_end']}")
    if "min_final_service_level" in expect:
        add(f"final service level >= {expect['min_final_service_level']}",
            facts["final_service_level"] >= expect["min_final_service_level"], f"{facts['final_service_level']}")
    if "risk_rises_above" in expect:
        peak = (facts["peak_risk"] or {}).get("p_stockout_12h", 0.0)
        add(f"peak P(stockout 12h) > {expect['risk_rises_above']:.0%}", peak > expect["risk_rises_above"],
            f"peak {peak:.0%} ({(facts['peak_risk'] or {}).get('series', '-')})")
    if expect.get("fallback_used"):
        add("heuristic fallback used", facts["fallback_ticks"] > 0, f"{facts['fallback_ticks']} tick(s)")
    if expect.get("auto_approval_blocked"):
        add("stale data blocked auto-approval", facts["stale_ticks"] > 0 and facts["needs_review"] > 0,
            f"{facts['stale_ticks']} stale tick(s), {facts['needs_review']} line(s) held for review")
    return out


def to_markdown(result: dict[str, Any]) -> str:
    scn, facts = result["scenario"], result["facts"]
    lines = [f"# {scn['id']} - {scn['name']}", "", f"*SIMULATED DATA.* {scn['description'].strip()}", "",
             f"Mode `{scn.get('mode', 'auto')}`, policy `{scn.get('policy', 'planner')}`, {scn['ticks']} ticks "
             f"({scn['ticks'] * 15 / 60:.0f} simulated hours) after a simulator reset.", "", "## Injected", ""]
    lines += [f"- event `{e['type']}` start tick {e.get('start_tick', 0)}, {e['duration_ticks']} ticks, "
              f"`{json.dumps(e.get('parameters', {}))}`" for e in scn.get("events", [])]
    lines += [f"- fault `{f['type']}` at tick {f['at_tick']} for {f['duration_seconds']} s" for f in scn.get("faults", [])]
    lines += ["", "## Checks", "", "| Check | Result | Detail |", "|---|---|---|"]
    lines += [f"| {c['check']} | {'PASS' if c['ok'] else 'FAIL'} | {c['detail']} |" for c in result["checks"]]
    peak = facts["peak_risk"]
    lines += ["", "## Outcome", "", f"- Service level: final **{facts['final_service_level']:.2%}**, "
              f"minimum {facts['min_service_level']:.2%}",
              "- Peak stockout risk: "
              + (f"P(stockout 12 h) {peak['p_stockout_12h']:.0%} for {peak['series']} at tick {peak['tick']}"
                 if peak else "-"),
              f"- Allocations submitted: {facts['allocations']} ({facts['auto_approved']} auto-approved lines, "
              f"{facts['needs_review']} held for review); routes used: {', '.join(facts['allocation_routes']) or '-'}",
              f"- Alert kinds raised: {', '.join(facts['alert_kinds'])}; still open at the end: "
              f"{', '.join(facts['open_alert_kinds_at_end']) or 'none'}"]
    lines += [f"- Incident: {i['title']} (opened tick {i['opened_tick']}, "
              f"{'resolved tick ' + str(i['resolved_tick']) if i['resolved_tick'] is not None else 'still open'})"
              for i in facts["incidents"]]
    if result["explanation"]:
        lines += ["", "## Example explanation (first plan with lines)", "", f"> {result['explanation']}"]
    lines += ["", "## Timeline (ticks with activity)", "",
              "| Tick | Service level | Empty series | Top risk (12 h) | What happened |", "|---|---|---|---|---|"]
    for t in result["timeline"]:
        happened = t["new_alerts"] + [f"INCIDENT: {x}" for x in t["incidents_opened"]]
        if t["plan"]["lines"] and t["plan"]["status"] not in ("EMPTY", None):
            happened.append(f"plan {t['plan']['id']} ({t['plan']['policy']}, {t['plan']['status']}): "
                            + "; ".join(t["plan"]["lines"][:3]) + (" ..." if len(t["plan"]["lines"]) > 3 else ""))
        if t["fallback"]:
            happened.append("heuristic fallback active")
        if happened:
            risk = f"{t['top_risk']['series']} {t['top_risk']['p_stockout_12h']:.0%}" if t["top_risk"] else "-"
            lines.append(f"| {t['tick']} | {t['service_level']:.2%} | {t['stockout_series']} | {risk} | "
                         + "<br>".join(x.replace("|", "/") for x in happened) + " |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenarios", nargs="+", type=Path)
    parser.add_argument("--backend", default="http://backend:8080")
    parser.add_argument("--sim", default="http://simulator:8000")
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "scenarios")
    parser.add_argument("--resume", action="store_true", help="set the simulator RUNNING again afterwards")
    args = parser.parse_args()

    client = StackClient(args.backend, args.sim, operator_token())
    args.out.mkdir(parents=True, exist_ok=True)
    failed = 0
    for path in args.scenarios:
        scn = yaml.safe_load(path.read_text(encoding="utf-8"))
        print(f"== {scn['id']} {scn['name']} ({scn['ticks']} ticks)", flush=True)
        result = run(client, scn)
        (args.out / f"{path.stem}.json").write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
        (args.out / f"{path.stem}.md").write_text(to_markdown(result), encoding="utf-8")
        for c in result["checks"]:
            print(f"   {'PASS' if c['ok'] else 'FAIL'}  {c['check']}  ({c['detail']})")
        f = result["facts"]
        print(f"   service level final {f['final_service_level']:.2%} (min {f['min_service_level']:.2%}), "
              f"{f['allocations']} allocations, incidents {len(f['incidents'])}", flush=True)
        failed += sum(not c["ok"] for c in result["checks"])
    if args.resume:
        client.admin("run")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
