"""Generate a reproducible demand-history training dataset from the BUP Fuel Supply Simulator.

All data produced here is SIMULATED (the organizer's simulator), not real fuel data.

What it does
    1. Checks /v1/health, then asks before resetting (``--yes`` skips the prompt).
    2. POST /admin/pause -> /admin/reset -> /admin/pause, so the run starts from the baked scenario.
    3. Injects a fixed schedule of ``demand_spike`` events (POST /admin/events) so the data contains spikes.
    4. Advances one tick at a time with POST /admin/step. After every step it reads that tick's new
       /v1/demand-history rows and every station's ``demand_multiplier`` (/v1/stations).
    5. Validates the result and writes ``<out>.csv`` plus ``<out>.meta.json`` (seed, scenario,
       tick_minutes, injected events, check results).

Simulator behaviour this relies on (checked against image 1.0.0; the checks below fail loudly if it changes)
    - The step that returns tick T writes the rows labelled tick T-1 (``row_tick_minus_step_tick`` = -1).
    - A demand_spike is active for row ticks start_tick..end_tick INCLUSIVE, i.e. duration_ticks + 1 ticks. The
      station snapshots taken between steps are therefore wrong on one edge of every spike, so the CSV's
      ``demand_multiplier`` is computed from the injected events and cross-checked against those snapshots.
    - demand_liters is true demand: it stays > 0 while a station is empty (unmet_liters = demand - served).

The simulator is deterministic (same scenario + seed + events + actions => identical data), so one person runs
this and everyone uses the same file. The script never POSTs allocations (CLAUDE.md rule 2); it uses /admin/*
because it is an offline script (rule 3).

WARNING: /admin/reset wipes the simulator. Do not run this against a simulator that is being used for a demo.

Usage
    pip install httpx
    python scripts/generate_training_data.py                 # 7 simulated days, asks before reset
    python scripts/generate_training_data.py --days 3 --yes  # no prompt
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

FUEL_TYPES = ("DIESEL", "PETROL", "OCTANE")
DEMAND_FIELDS = ("demand_liters", "served_liters", "unmet_liters")
GET_ATTEMPTS = 4
REPO_ROOT = Path(__file__).resolve().parent.parent

# Demand spikes, in simulated hours from the start of the run. Days 1-5 (hours 0-120) are the training window and
# days 6-7 the test window (docs/model-selection.md); both contain spikes. No filters = all stations.
SPIKE_SCHEDULE: list[dict[str, Any]] = [
    {"start_hour": 20, "hours": 6, "parameters": {"region_ids": ["region-dhaka"], "multiplier": 1.8}},
    {"start_hour": 44, "hours": 4, "parameters": {"station_ids": ["station-karnaphuli"], "multiplier": 2.0}},
    {"start_hour": 70, "hours": 8, "parameters": {"multiplier": 1.3}},
    {"start_hour": 100, "hours": 5, "parameters": {"region_ids": ["region-chattogram"], "multiplier": 1.6}},
    {
        "start_hour": 130,
        "hours": 6,
        "parameters": {"station_ids": ["station-tongi", "station-mirpur"], "multiplier": 1.5},
    },
    {"start_hour": 150, "hours": 4, "parameters": {"region_ids": ["region-dhaka"], "multiplier": 2.2}},
]

CSV_COLUMNS = (
    "id",
    "step_tick",
    "tick",
    "sim_time",
    "station_id",
    "region_id",
    "demand_profile",
    "fuel_type",
    "demand_liters",
    "served_liters",
    "unmet_liters",
    "demand_multiplier",
)


class SimulatorError(RuntimeError):
    """The simulator returned something this script cannot safely use."""


def describe_error(response: httpx.Response) -> str:
    """Render either error envelope ({"detail": ...} for domain/422, {"error": ...} for faults) as one line."""
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}: {response.text[:200]}"
    payload = (body.get("error") or body.get("detail")) if isinstance(body, dict) else body
    if isinstance(payload, dict):
        return f"HTTP {response.status_code} {payload.get('code')}: {payload.get('message', '')}"
    return f"HTTP {response.status_code}: {payload}"


class Simulator:
    def __init__(self, base_url: str, timeout_s: float) -> None:
        self._http = httpx.Client(base_url=base_url, timeout=timeout_s)

    def close(self) -> None:
        self._http.close()

    def get(self, path: str, **params: Any) -> Any:
        """GET with backoff on connection errors and 5xx. Refuses stale data."""
        error = ""
        for attempt in range(1, GET_ATTEMPTS + 1):
            try:
                response = self._http.get(path, params=params or None)
            except httpx.TransportError as exc:
                error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code == 200:
                    if response.headers.get("X-Simulator-Stale", "").lower() == "true":
                        raise SimulatorError(
                            f"GET {path}: X-Simulator-Stale is set. Clear faults (POST /admin/faults/clear) and rerun."
                        )
                    return response.json()
                if response.status_code < 500:
                    raise SimulatorError(f"GET {path}: {describe_error(response)}")
                error = describe_error(response)
            if attempt < GET_ATTEMPTS:
                time.sleep(0.5 * 2 ** (attempt - 1))
        raise SimulatorError(f"GET {path} failed after {GET_ATTEMPTS} attempts: {error}")

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        """POST without blind retry: /admin/step and /admin/events are not idempotent.

        The guide documents JSON bodies only for step, events and faults/clear; run/pause/reset may answer with a
        redirect back to the admin console, so any 2xx/3xx counts as success and non-JSON bodies return None.
        """
        try:
            response = self._http.post(path, json=body)
        except httpx.TransportError as exc:
            raise SimulatorError(f"POST {path}: {type(exc).__name__}: {exc}") from exc
        if not 200 <= response.status_code < 400:
            raise SimulatorError(f"POST {path}: {describe_error(response)}")
        if "application/json" not in response.headers.get("content-type", ""):
            return None
        return response.json()

    def step(self, expected_tick: int) -> int:
        """Advance exactly one tick. If a response is lost, check the tick before trying again."""
        for _ in range(3):
            try:
                result = self.post("/admin/step")
                if not isinstance(result, dict) or "tick" not in result:
                    raise SimulatorError(f"POST /admin/step returned {result!r}, expected {{'tick': ...}}")
                return int(result["tick"])
            except SimulatorError:
                tick = int(self.get("/v1/instance")["tick"])
                if tick == expected_tick:
                    return tick
                if tick > expected_tick:
                    raise SimulatorError(f"tick is {tick}, past {expected_tick}; is something else stepping?") from None
        raise SimulatorError(f"could not advance to tick {expected_tick}")


def read_multipliers(sim: Simulator) -> dict[str, float]:
    return {s["id"]: float(s["demand_multiplier"]) for s in sim.get("/v1/stations")}


def covers(event: dict[str, Any], station: dict[str, Any]) -> bool:
    """station_ids / region_ids are filters; an empty or missing list matches every station."""
    params = event.get("parameters") or {}
    station_ids = params.get("station_ids") or []
    region_ids = params.get("region_ids") or []
    return (not station_ids or station["id"] in station_ids) and (not region_ids or station["region_id"] in region_ids)


def applied_multiplier(initial: float, events: list[dict[str, Any]], station: dict[str, Any], row_tick: int) -> float:
    """demand_multiplier that produced the row labelled row_tick (spikes cover start_tick..end_tick inclusive)."""
    multiplier = initial
    for event in events:
        if event["start_tick"] <= row_tick <= event["end_tick"] and covers(event, station):
            multiplier *= float(event["parameters"].get("multiplier", 1.5))
    return multiplier


def validate_row(row: dict[str, Any], station_ids: set[str]) -> None:
    missing = [k for k in ("id", "station_id", "fuel_type", "tick", "sim_time", *DEMAND_FIELDS) if k not in row]
    if missing:
        raise SimulatorError(f"demand-history row {row.get('id')} is missing {missing}")
    if row["station_id"] not in station_ids or row["fuel_type"] not in FUEL_TYPES:
        raise SimulatorError(f"demand-history row {row['id']} has unknown station/fuel: {row}")
    for key in DEMAND_FIELDS:
        value = row[key]
        if not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
            raise SimulatorError(f"demand-history row {row['id']} has invalid {key}={value!r}")


def confirm_reset(base_url: str) -> bool:
    if not sys.stdin.isatty():
        print("Refusing to reset without a terminal to confirm. Pass --yes to skip the prompt.")
        return False
    try:
        answer = input(f"This will RESET the simulator at {base_url} (all its state is wiped). Continue? [y/N] ")
    except EOFError:  # e.g. stdin is NUL on Windows, which still reports isatty()
        print("\nNo answer on stdin. Pass --yes to skip the prompt.")
        return False
    return answer.strip().lower() in ("y", "yes")


def schedule_spikes(sim: Simulator, start_tick: int, ticks_per_hour: float, total_ticks: int) -> list[dict[str, Any]]:
    events = []
    for spike in SPIKE_SCHEDULE:
        start = start_tick + round(spike["start_hour"] * ticks_per_hour)
        duration = max(1, round(spike["hours"] * ticks_per_hour))
        if start + duration > start_tick + total_ticks:
            continue  # outside this run (e.g. --days < 7)
        body = {
            "type": "demand_spike",
            "start_tick": start,
            "duration_ticks": duration,
            "parameters": spike["parameters"],
        }
        events.append(sim.post("/admin/events", body) or {**body, "end_tick": start + duration})
    return events


def run(args: argparse.Namespace) -> int:
    sim = Simulator(args.base_url, args.timeout)
    try:
        health = sim.get("/v1/health")
        if health.get("status") != "ok":
            raise SimulatorError(f"simulator is not healthy: {health}")
        if not args.yes and not confirm_reset(args.base_url):
            print("Aborted; nothing was changed.")
            return 1

        sim.post("/admin/pause")
        sim.post("/admin/reset")
        sim.post("/admin/pause")  # reset reloads the scenario; make sure the background runner stays idle
        instance = sim.get("/v1/instance")
        if instance["status"] != "PAUSED":
            raise SimulatorError(f"simulator did not pause: {instance}")

        tick_minutes = int(instance["tick_minutes"])
        ticks_per_hour = 60 / tick_minutes
        total_ticks = round(args.days * 24 * ticks_per_hour)
        start_tick = int(instance["tick"])
        stations = {s["id"]: s for s in sim.get("/v1/stations")}
        rows_per_tick = len(stations) * len(FUEL_TYPES)
        events = [] if args.no_spikes else schedule_spikes(sim, start_tick, ticks_per_hour, total_ticks)

        print(
            f"Scenario {instance['scenario_id']} (seed {instance.get('seed')}), tick = {tick_minutes} min, "
            f"{args.days} days = {total_ticks} ticks, {len(events)} demand spikes, {rows_per_tick} rows per tick"
        )

        # Rows that already exist (none after a clean reset) are not part of this run.
        seen_ids = {int(r["id"]) for r in sim.get("/v1/demand-history", limit=2000)}
        rows: list[dict[str, Any]] = []
        initial = before = read_multipliers(sim)
        multiplier_mismatches = 0
        previous_tick: int | None = None
        started = time.monotonic()

        for i in range(1, total_ticks + 1):
            step_tick = sim.step(expected_tick=start_tick + i)
            if previous_tick is not None and step_tick != previous_tick + 1:
                raise SimulatorError(f"tick jumped from {previous_tick} to {step_tick}; is something else stepping?")
            previous_tick = step_tick
            after = read_multipliers(sim)

            latest = sim.get("/v1/demand-history", limit=rows_per_tick * 2)
            new_rows = [r for r in latest if int(r["id"]) not in seen_ids]
            if len(new_rows) != rows_per_tick:
                raise SimulatorError(
                    f"expected {rows_per_tick} new demand rows after tick {step_tick}, got {len(new_rows)} "
                    "(does /v1/demand-history return the most recent rows first?)"
                )
            for row in new_rows:
                validate_row(row, set(stations))
                seen_ids.add(int(row["id"]))
                sid = row["station_id"]
                station = stations[sid]
                multiplier = applied_multiplier(initial[sid], events, station, int(row["tick"]))
                # Between steps the snapshot is either pre-activation or post-resolution, so on a spike edge the
                # applied multiplier equals one of them; anything else means the event model above is wrong.
                if not any(math.isclose(multiplier, snap[sid], rel_tol=1e-9) for snap in (before, after)):
                    multiplier_mismatches += 1
                rows.append(
                    {
                        **{k: row[k] for k in ("id", "tick", "sim_time", "station_id", "fuel_type", *DEMAND_FIELDS)},
                        "step_tick": step_tick,
                        "region_id": station["region_id"],
                        "demand_profile": station["demand_profile"],
                        "demand_multiplier": multiplier,
                    }
                )
            before = after

            if i % round(24 * ticks_per_hour) == 0 or i == total_ticks:
                print(f"  day {i / (24 * ticks_per_hour):.0f}: tick {step_tick}, {len(rows)} rows")

        final_tick = int(sim.get("/v1/instance")["tick"])
        checks = run_checks(
            rows, total_ticks, rows_per_tick, final_tick, start_tick + total_ticks, multiplier_mismatches
        )
        write_outputs(args.out, rows, instance, stations, events, checks, args, final_tick)
        print(f"Done in {time.monotonic() - started:.0f}s. The simulator is left PAUSED at tick {final_tick}.")
        return 0 if checks["passed"] else 2
    except (SimulatorError, httpx.HTTPError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        sim.close()


def run_checks(
    rows: list[dict[str, Any]],
    total_ticks: int,
    rows_per_tick: int,
    final_tick: int,
    expected_final_tick: int,
    multiplier_mismatches: int,
) -> dict[str, Any]:
    balance_errors = sum(
        1
        for r in rows
        if abs(r["demand_liters"] - r["served_liters"] - r["unmet_liters"]) > 1e-6 * max(1.0, r["demand_liters"])
    )
    checks: dict[str, Any] = {
        "rows": len(rows),
        "rows_expected": total_ticks * rows_per_tick,
        "final_tick": final_tick,
        "final_tick_expected": expected_final_tick,
        # Offset between a row's own `tick` field and the tick /admin/step returned (expected: [-1]).
        "row_tick_minus_step_tick": sorted({int(r["tick"]) - int(r["step_tick"]) for r in rows}),
        "demand_multiplier_mismatches": multiplier_mismatches,
        "demand_ne_served_plus_unmet": balance_errors,
        "rows_with_zero_demand": sum(1 for r in rows if r["demand_liters"] == 0),
        "rows_with_stockout": sum(1 for r in rows if r["served_liters"] == 0 and r["demand_liters"] > 0),
        "max_multiplier": max((r["demand_multiplier"] for r in rows), default=None),
    }
    checks["passed"] = (
        checks["rows"] == checks["rows_expected"] and final_tick == expected_final_tick and multiplier_mismatches == 0
    )
    if multiplier_mismatches:
        print(f"  ERROR: {multiplier_mismatches} rows where demand_multiplier matches neither station snapshot")
    for key in ("demand_ne_served_plus_unmet", "rows_with_zero_demand"):
        if checks[key]:
            print(f"  WARNING: {key} = {checks[key]} (see docs/model-selection.md forecast contract)")
    if checks["rows_with_stockout"]:
        print(f"  note: {checks['rows_with_stockout']} rows are stockouts (expected: this run makes no allocations)")
    return checks


def write_outputs(
    out: Path,
    rows: list[dict[str, Any]],
    instance: dict[str, Any],
    stations: dict[str, dict[str, Any]],
    events: list[dict[str, Any]],
    checks: dict[str, Any],
    args: argparse.Namespace,
    final_tick: int,
) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: (r["step_tick"], r["station_id"], r["fuel_type"])))

    meta = {
        "label": "SIMULATED DATA from the BUP Fuel Supply Simulator; not real fuel operations",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "script": "scripts/generate_training_data.py",
        "args": {"days": args.days, "spikes": not args.no_spikes},
        "simulator": {
            "base_url": args.base_url,
            "scenario_id": instance.get("scenario_id"),
            "scenario_version": instance.get("scenario_version"),
            "seed": instance.get("seed"),
            "tick_minutes": instance.get("tick_minutes"),
            "start_tick": instance.get("tick"),
            "start_sim_time": instance.get("sim_time"),
            "final_tick": final_tick,
        },
        "stations": [{k: s[k] for k in ("id", "region_id", "demand_profile", "capacity")} for s in stations.values()],
        "events": events,
        "checks": checks,
    }
    meta_path = out.with_suffix(".meta.json")
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(rows)} rows to {out} and run details to {meta_path}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", default=os.environ.get("SIM_BASE_URL", "http://localhost:8000"))
    parser.add_argument("--days", type=float, default=7.0, help="simulated days to generate (default 7)")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "data" / "demand_history.csv")
    parser.add_argument("--no-spikes", action="store_true", help="do not inject demand_spike events")
    parser.add_argument("--timeout", type=float, default=10.0, help="per-request timeout in seconds")
    parser.add_argument("--yes", action="store_true", help="reset the simulator without asking")
    args = parser.parse_args(argv)
    if args.days <= 0:
        parser.error("--days must be > 0")
    return args


if __name__ == "__main__":
    sys.exit(run(parse_args()))
