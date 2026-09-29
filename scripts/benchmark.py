"""Policy benchmark (plan.json "benchmark", R-INT-01): no-action vs heuristic vs LP planner on the same deterministic
replays. RESETS THE SIMULATOR.

    docker compose run --rm tools scripts/benchmark.py                       # 288 ticks = 3 simulated days
    docker compose run --rm tools scripts/benchmark.py --ticks 96 --scenarios baseline

Each run: pause -> reset -> inject the scenario's crisis events -> step N ticks with the policy in Auto mode (every line
executes, so the policy alone decides) -> read the simulator's own metrics. Simulator faults in a scenario file are
skipped: they are timed in wall-clock seconds, so they would not replay identically across policies.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fuelops_client import StackClient, operator_token, stockout_series  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
POLICIES = ("no-action", "heuristic", "planner")
TICK_HOURS = 15 / 60


def run_policy(client: StackClient, events: list[dict], policy: str, ticks: int) -> dict:
    client.reset()
    if policy == "no-action":
        client.settings(decisions_enabled=False)
    else:
        client.settings(decisions_enabled=True, mode="auto", policy=policy)
    for ev in events:
        client.admin("events", {"type": ev["type"], "start_tick": ev.get("start_tick", 0),
                                "duration_ticks": ev["duration_ticks"], "parameters": ev.get("parameters", {})})
    empty_series_ticks, started = 0, time.monotonic()
    for tick in range(1, ticks + 1):
        state = client.step(tick)
        empty_series_ticks += stockout_series(state)
    metrics = state["world"]["metrics"] or {}
    allocations = client.get("/api/allocations")
    shipped = [a for a in allocations if a["status"] in ("IN_TRANSIT", "ARRIVED")]
    return {
        "policy": policy,
        "service_level": round(metrics.get("service_level", 0.0), 4),
        "unmet_demand_liters": round(metrics.get("unmet_demand_liters", 0.0)),
        "served_demand_liters": round(metrics.get("served_demand_liters", 0.0)),
        "allocation_failures": metrics.get("allocation_failures", 0),
        "allocations": len(allocations),
        "liters_shipped": round(sum(a["quantity"] for a in shipped)),
        "stockout_hours": round(empty_series_ticks * TICK_HOURS, 1),
        "wall_seconds": round(time.monotonic() - started, 1),
    }


def summary(results: dict[str, list[dict]]) -> list[str]:
    """One sentence per policy with its range across scenarios, computed from the results."""
    by_policy: dict[str, list[dict]] = {}
    for rows in results.values():
        for r in rows:
            by_policy.setdefault(r["policy"], []).append(r)
    if not by_policy:
        return []

    def span(values: list[float], fmt: str) -> str:
        lo, hi = min(values), max(values)
        return format(lo, fmt) if lo == hi else f"{format(lo, fmt)}-{format(hi, fmt)}"

    out = ["## Summary", ""]
    for policy in POLICIES:
        rows = by_policy.get(policy)
        if rows:
            out.append(f"- **{policy}**: service level {span([r['service_level'] for r in rows], '.2%')}, "
                       f"{span([r['stockout_hours'] for r in rows], '.1f')} stockout hours, "
                       f"{span([r['unmet_demand_liters'] for r in rows], ',')} L unmet "
                       f"across {len(rows)} scenario(s).")
    return out + [""]


def to_markdown(results: dict[str, list[dict]], ticks: int) -> str:
    lines = ["# Policy benchmark", "",
             f"*SIMULATED DATA.* Deterministic replays of the organizer's simulator (baseline scenario, seed 12345): "
             f"pause -> reset -> inject events -> {ticks} ticks ({ticks * TICK_HOURS / 24:.0f} simulated days) in Auto "
             "mode, then the simulator's own `/v1/metrics`. Reproduce with `docker compose run --rm tools "
             "scripts/benchmark.py`.", "",
             "*Stockout hours* = sum over the 12 station/fuel series of hours spent empty. *Liters shipped* counts "
             "allocations that left the depot (IN_TRANSIT or ARRIVED).", ""]
    lines += summary(results)
    for scenario, rows in results.items():
        base = next((r for r in rows if r["policy"] == "no-action"), None)
        lines += [f"## {scenario}", "", "| Policy | Service level | Unmet demand (L) | Stockout hours | Allocations | "
                  "Liters shipped | Failed allocations |", "|---|---|---|---|---|---|---|"]
        for r in rows:
            delta = ""
            if base and r is not base and base["unmet_demand_liters"]:
                cut = 1 - r["unmet_demand_liters"] / base["unmet_demand_liters"]
                delta = f" ({cut:.0%} less than no-action)" if cut >= 0 else f" ({-cut:.0%} more than no-action)"
            lines.append(f"| {r['policy']} | {r['service_level']:.2%} | {r['unmet_demand_liters']:,}{delta} | "
                         f"{r['stockout_hours']} | {r['allocations']} | {r['liters_shipped']:,} | "
                         f"{r['allocation_failures']} |")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ticks", type=int, default=288)
    parser.add_argument("--scenarios", nargs="+", default=["baseline", "demand-spike", "combined-crisis"])
    parser.add_argument("--policies", nargs="+", default=list(POLICIES), choices=POLICIES)
    parser.add_argument("--backend", default="http://backend:8080")
    parser.add_argument("--sim", default="http://simulator:8000")
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "benchmark.md")
    parser.add_argument("--resume", action="store_true", help="keep finished runs from a previous (interrupted) run")
    parser.add_argument("--render-only", action="store_true", help="rewrite the report from the saved JSON; no runs")
    args = parser.parse_args()
    if args.render_only:
        save(args.out, load_previous(args.out, args.ticks), args.ticks)
        print(f"wrote {args.out}")
        return 0

    client = StackClient(args.backend, args.sim, operator_token())
    previous = client.get("/api/settings")
    results = load_previous(args.out, args.ticks) if args.resume else {}
    try:
        for name in args.scenarios:
            events = [] if name == "baseline" else \
                yaml.safe_load((ROOT / "scenarios" / f"{name}.yaml").read_text(encoding="utf-8")).get("events", [])
            for policy in args.policies:
                if any(r["policy"] == policy for r in results.get(name, [])):
                    continue
                row = run_policy(client, events, policy, args.ticks)
                results.setdefault(name, []).append(row)
                save(args.out, results, args.ticks)  # after every run, so an interruption loses at most one
                print(f"{name:16s} {policy:10s} service {row['service_level']:.2%}  unmet {row['unmet_demand_liters']:>8,} L"
                      f"  stockout {row['stockout_hours']:>6} h  allocations {row['allocations']:>3}  "
                      f"({row['wall_seconds']} s)", flush=True)
    finally:
        client.settings(mode=previous["mode"], policy=previous["policy"],
                        decisions_enabled=previous["decisions_enabled"])
    print(f"wrote {args.out}")
    return 0


def load_previous(out: Path, ticks: int) -> dict[str, list[dict]]:
    path = out.with_suffix(".json")
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["results"] if data.get("ticks") == ticks else {}


def save(out: Path, results: dict[str, list[dict]], ticks: int) -> None:
    ordered = {name: sorted(rows, key=lambda r: POLICIES.index(r["policy"])) for name, rows in results.items()}
    out.write_text(to_markdown(ordered, ticks), encoding="utf-8")
    out.with_suffix(".json").write_text(json.dumps({"ticks": ticks, "results": ordered}, indent=1), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
