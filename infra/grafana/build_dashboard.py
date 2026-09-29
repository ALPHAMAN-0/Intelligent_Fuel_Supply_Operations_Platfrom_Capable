"""Generates infra/grafana/dashboards/fuelops.json. Edit the panel list here, then run:  python infra/grafana/build_dashboard.py"""

from __future__ import annotations

import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "prometheus"}
OUT = Path(__file__).parent / "dashboards" / "fuelops.json"
BACKEND_P = 'histogram_quantile({q}, sum by (le) (rate(fuelops_http_request_duration_seconds_bucket{{job="backend"}}[1m])))'


def target(expr: str, legend: str = "", ref: str = "A") -> dict:
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref}


def panel(kind: str, title: str, targets: list[dict], w: int, h: int, unit: str = "short", **extra) -> dict:
    p = {"type": kind, "title": title, "datasource": DS, "targets": targets,
         "gridPos": {"w": w, "h": h}, "fieldConfig": {"defaults": {"unit": unit}, "overrides": []}}
    if kind == "stat":
        p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background", "graphMode": "area"}
    p["fieldConfig"]["defaults"].update(extra.pop("defaults", {}))
    p.update(extra)
    return p


def thresholds(*steps: tuple[str, float | None]) -> dict:
    return {"thresholds": {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in steps]}}


def row(title: str) -> dict:
    return {"type": "row", "title": title, "collapsed": False, "panels": [], "gridPos": {"w": 24, "h": 1}}


ROWS: list[tuple[dict, list[dict]]] = [
    (row("Simulated network (business)"), [
        panel("stat", "Service level", [target("fuelops_service_level")], 6, 5, "percentunit",
              defaults={"decimals": 2, **thresholds(("red", None), ("orange", 0.9), ("green", 0.97))}),
        panel("stat", "Unmet demand (cumulative)", [target("fuelops_unmet_demand_liters")], 6, 5, "litre"),
        panel("stat", "Series at stockout risk (12 h)", [target("fuelops_stations_at_risk")], 6, 5,
              defaults=thresholds(("green", None), ("orange", 1), ("red", 3))),
        panel("stat", "Simulator tick", [target("fuelops_sim_tick")], 6, 5, colorMode="none"),
    ]),
    (row("Operator API (golden signals)"), [
        panel("timeseries", "Request rate by route", [target(
            'sum by (route) (rate(fuelops_http_requests_total{job="backend"}[1m]))', "{{route}}")], 12, 8, "reqps"),
        panel("timeseries", "Latency p50 / p95 / p99", [
            target(BACKEND_P.format(q=0.5), "p50", "A"), target(BACKEND_P.format(q=0.95), "p95", "B"),
            target(BACKEND_P.format(q=0.99), "p99", "C")], 12, 8, "s"),
        panel("timeseries", "5xx error rate", [target(
            'sum(rate(fuelops_http_requests_total{job="backend",status=~"5.."}[1m])) '
            '/ clamp_min(sum(rate(fuelops_http_requests_total{job="backend"}[1m])), 1e-9)', "error rate")],
            12, 7, "percentunit"),
        panel("stat", "Service availability (up)", [target("up{job=~\"backend|intelligence\"}", "{{job}}")], 12, 7,
              defaults={"mappings": [{"type": "value", "options": {"0": {"text": "DOWN", "color": "red"},
                                                                   "1": {"text": "UP", "color": "green"}}}]}),
    ]),
    (row("Simulator integration & resilience"), [
        panel("timeseries", "Simulator calls by outcome", [target(
            "sum by (outcome) (rate(fuelops_sim_requests_total[1m]))", "{{outcome}}")], 8, 8, "reqps"),
        panel("timeseries", "Circuit breaker (0 closed, 1 half-open, 2 open)", [target(
            "fuelops_sim_breaker_state", "{{group}}")], 8, 8, defaults={"min": 0, "max": 2}),
        panel("timeseries", "Stale data / SSE / invalid payloads", [
            target("fuelops_sim_stale", "stale", "A"), target("fuelops_sse_connected", "sse connected", "B"),
            target("fuelops_sse_polling", "polling fallback", "C"),
            target("increase(fuelops_sim_invalid_responses_total[5m])", "invalid (5m) {{endpoint}}", "D")], 8, 8),
    ]),
    (row("Intelligence & decisions"), [
        panel("timeseries", "Decision cycles by policy", [target(
            "sum by (policy) (rate(fuelops_decision_cycles_total[5m])) * 60", "{{policy}}")], 8, 8, "cpm"),
        panel("timeseries", "Fallback activations (5m)", [target(
            "sum by (reason) (increase(fuelops_fallback_activations_total[5m]))", "{{reason}}")], 8, 8),
        panel("timeseries", "Forecast MAPE vs noise floor", [
            target("fuelops_forecast_mape", "MAPE {{horizon}}", "A"),
            target("fuelops_forecast_noise_floor", "noise floor", "B")], 8, 8, "percentunit"),
        panel("timeseries", "Plan confidence", [target("fuelops_plan_confidence", "confidence")], 8, 7,
              "percentunit", defaults={"min": 0, "max": 1}),
        panel("timeseries", "Plan lines & allocations (5m)", [
            target("sum by (status) (increase(fuelops_plan_lines_total[5m]))", "line {{status}}", "A"),
            target("sum by (outcome) (increase(fuelops_allocations_submitted_total[5m]))", "submit {{outcome}}",
                   "B")], 8, 7),
        panel("timeseries", "Alerts open / shortage alert rate", [
            target("fuelops_alerts_open", "open {{severity}}", "A"),
            target("increase(fuelops_shortage_alerts_total[5m])", "shortage alerts (5m)", "B")], 8, 7),
        panel("timeseries", "Planner solve p95 / decision cycle p95", [
            target("histogram_quantile(0.95, sum by (le) (rate(fuelops_planner_solve_seconds_bucket[5m])))",
                   "LP solve p95", "A"),
            target("histogram_quantile(0.95, sum by (le) (rate(fuelops_decision_cycle_duration_seconds_bucket[5m])))",
                   "cycle p95", "B")], 12, 7, "s"),
        panel("timeseries", "LLM explanations by outcome (5m)", [target(
            "sum by (outcome) (increase(fuelops_llm_requests_total[5m]))", "{{outcome}}")], 12, 7),
    ]),
    (row("System resources"), [
        panel("timeseries", "CPU (cores) per service", [target(
            "rate(process_cpu_seconds_total{job=~\"backend|intelligence\"}[1m])", "{{job}}")], 12, 7),
        panel("timeseries", "Resident memory per service", [target(
            "process_resident_memory_bytes{job=~\"backend|intelligence\"}", "{{job}}")], 12, 7, "bytes"),
    ]),
]


def layout() -> list[dict]:
    panels, y, pid = [], 0, 1
    for header, items in ROWS:
        header = {**header, "id": pid, "gridPos": {**header["gridPos"], "x": 0, "y": y}}
        panels.append(header)
        pid, y, x, row_h = pid + 1, y + 1, 0, 0
        for p in items:
            w, h = p["gridPos"]["w"], p["gridPos"]["h"]
            if x + w > 24:
                x, y, row_h = 0, y + row_h, 0
            panels.append({**p, "id": pid, "gridPos": {"x": x, "y": y, "w": w, "h": h}})
            pid, x, row_h = pid + 1, x + w, max(row_h, h)
        y += row_h
    return panels


def dashboard() -> dict:
    return {
        "uid": "fuelops", "title": "FuelOps - simulated fuel network", "tags": ["fuelops", "simulated"],
        "timezone": "browser", "refresh": "5s", "schemaVersion": 39, "version": 1, "editable": False,
        "time": {"from": "now-30m", "to": "now"},
        "description": "SIMULATED DATA. Business, API, integration, intelligence and system metrics for FuelOps.",
        "panels": layout(),
    }


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(dashboard(), indent=1) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(dashboard()['panels'])} panels)")
