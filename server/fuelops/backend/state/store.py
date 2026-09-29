"""Durable storage on SQLite (stdlib; ADR-012). Best effort: failures never stop the decision loop.

Every public method catches database errors, marks the store degraded (visible on the health board and as the
fuelops_db_up metric) and returns an empty result. `force_fault` simulates a database outage for resilience demos.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, TypeVar

from fuelops.backend.domain import Alert, Decision, Incident, PlanRecord
from fuelops.shared.logging import get_logger
from fuelops.shared.metrics import DB_UP
from fuelops.shared.models import DemandPoint, WorldState

log = get_logger("store")
T = TypeVar("T")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sim_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at REAL, seed INTEGER, scenario_id TEXT, first_tick INTEGER);
CREATE TABLE IF NOT EXISTS demand_observations (
  run_id INTEGER, sim_id INTEGER, station_id TEXT, fuel_type TEXT, tick INTEGER, sim_time TEXT,
  demand REAL, served REAL, multiplier REAL, PRIMARY KEY (run_id, sim_id));
CREATE INDEX IF NOT EXISTS ix_obs_tick ON demand_observations (run_id, tick);
CREATE TABLE IF NOT EXISTS snapshots (
  run_id INTEGER, tick INTEGER, kind TEXT, entity_id TEXT, data TEXT, PRIMARY KEY (run_id, tick, kind, entity_id));
CREATE TABLE IF NOT EXISTS sim_metrics (
  run_id INTEGER, tick INTEGER, served REAL, unmet REAL, service_level REAL, allocation_liters REAL,
  allocation_failures INTEGER, PRIMARY KEY (run_id, tick));
CREATE TABLE IF NOT EXISTS alerts (id TEXT, run_id INTEGER, data TEXT, updated_at REAL, PRIMARY KEY (run_id, id));
CREATE TABLE IF NOT EXISTS incidents (id TEXT, run_id INTEGER, data TEXT, updated_at REAL, PRIMARY KEY (run_id, id));
CREATE TABLE IF NOT EXISTS plans (id INTEGER PRIMARY KEY, run_id INTEGER, tick INTEGER, data TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY, run_id INTEGER, plan_id INTEGER, data TEXT, at REAL);
CREATE TABLE IF NOT EXISTS app_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, actor TEXT, action TEXT, detail TEXT);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self.ok = True
        self.last_error: str | None = None
        self.forced_fault = False
        self.writes = 0

    # ------------------------------------------------------------ plumbing

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
        return self._conn

    def _run(self, fn: Callable[[sqlite3.Connection], T], default: T) -> T:
        with self._lock:
            try:
                if self.forced_fault:
                    raise sqlite3.OperationalError("database unavailable (injected fault)")
                result = fn(self._connect())
                self._connect().commit()
                self._mark(True)
                return result
            except sqlite3.Error as exc:
                self._mark(False, str(exc))
                return default

    async def _arun(self, fn: Callable[[sqlite3.Connection], T], default: T) -> T:
        return await asyncio.to_thread(self._run, fn, default)

    def _mark(self, ok: bool, error: str | None = None) -> None:
        if ok != self.ok:
            (log.info if ok else log.warning)("store.state_changed", ok=ok, error=error)
        self.ok = ok
        self.last_error = None if ok else error
        DB_UP.set(1 if ok else 0)
        if ok:
            self.writes += 1

    def force_fault(self, on: bool) -> None:
        self.forced_fault = on
        if on:
            self._mark(False, "database unavailable (injected fault)")

    async def init(self) -> bool:
        return await self._arun(lambda c: True, False)

    async def ping(self) -> bool:
        return await self._arun(lambda c: c.execute("SELECT 1").fetchone() is not None, False)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # --------------------------------------------------------------- runs

    async def new_run(self, seed: int | None, scenario_id: str, tick: int) -> int | None:
        def fn(c: sqlite3.Connection) -> int:
            cur = c.execute("INSERT INTO sim_runs (started_at, seed, scenario_id, first_tick) VALUES (?,?,?,?)",
                            (time.time(), seed, scenario_id, tick))
            return int(cur.lastrowid)
        return await self._arun(fn, None)

    # -------------------------------------------------------------- world

    async def insert_observations(self, run_id: int, points: Iterable[tuple[int, DemandPoint]]) -> int:
        rows = [(run_id, sim_id, p.station_id, p.fuel_type, p.tick, p.sim_time.isoformat(), p.demand,
                 p.served, p.multiplier) for sim_id, p in points]
        if not rows:
            return 0

        def fn(c: sqlite3.Connection) -> int:
            c.executemany("INSERT OR IGNORE INTO demand_observations VALUES (?,?,?,?,?,?,?,?,?)", rows)
            return len(rows)
        return await self._arun(fn, 0)

    async def save_snapshot(self, state: WorldState) -> None:
        run, tick = state.run_id, state.instance.tick
        rows = [(run, tick, "station", s.id, s.model_dump_json()) for s in state.stations]
        rows += [(run, tick, "depot", d.id, d.model_dump_json()) for d in state.depots]
        rows += [(run, tick, "route", r.id, r.model_dump_json()) for r in state.routes]
        m = state.metrics

        def fn(c: sqlite3.Connection) -> None:
            c.executemany("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?)", rows)
            if m is not None:
                c.execute("INSERT OR REPLACE INTO sim_metrics VALUES (?,?,?,?,?,?,?)",
                          (run, tick, m.served_demand_liters, m.unmet_demand_liters, m.service_level,
                           m.allocation_liters, m.allocation_failures))
        await self._arun(fn, None)

    async def metrics_timeline(self, run_id: int, limit: int = 500) -> list[dict[str, Any]]:
        def fn(c: sqlite3.Connection) -> list[dict[str, Any]]:
            cur = c.execute("SELECT tick, served, unmet, service_level, allocation_liters, allocation_failures "
                            "FROM sim_metrics WHERE run_id=? ORDER BY tick DESC LIMIT ?", (run_id, limit))
            cols = ["tick", "served", "unmet", "service_level", "allocation_liters", "allocation_failures"]
            return [dict(zip(cols, row, strict=True)) for row in reversed(cur.fetchall())]
        return await self._arun(fn, [])

    async def load_history(self, run_id: int, since_tick: int) -> list[DemandPoint]:
        def fn(c: sqlite3.Connection) -> list[DemandPoint]:
            cur = c.execute("SELECT station_id, fuel_type, tick, sim_time, demand, served, multiplier "
                            "FROM demand_observations WHERE run_id=? AND tick>=? ORDER BY tick", (run_id, since_tick))
            return [DemandPoint(station_id=r[0], fuel_type=r[1], tick=r[2], sim_time=r[3], demand=r[4], served=r[5],
                                multiplier=r[6]) for r in cur.fetchall()]
        return await self._arun(fn, [])

    # ------------------------------------------------------------ records

    async def upsert_alert(self, alert: Alert) -> None:
        await self._arun(lambda c: c.execute("INSERT OR REPLACE INTO alerts VALUES (?,?,?,?)",
                                             (alert.id, alert.run_id, alert.model_dump_json(), time.time())), None)

    async def upsert_incident(self, inc: Incident) -> None:
        await self._arun(lambda c: c.execute("INSERT OR REPLACE INTO incidents VALUES (?,?,?,?)",
                                             (inc.id, inc.run_id, inc.model_dump_json(), time.time())), None)

    async def upsert_plan(self, plan: PlanRecord) -> None:
        await self._arun(lambda c: c.execute("INSERT OR REPLACE INTO plans VALUES (?,?,?,?,?)",
                                             (plan.id, plan.run_id, plan.tick, plan.model_dump_json(), time.time())),
                         None)

    async def add_decision(self, d: Decision) -> None:
        await self._arun(lambda c: c.execute("INSERT OR REPLACE INTO decisions VALUES (?,?,?,?,?)",
                                             (d.id, d.run_id, d.plan_id, d.model_dump_json(), time.time())), None)

    async def audit(self, actor: str, action: str, detail: dict[str, Any]) -> None:
        await self._arun(lambda c: c.execute("INSERT INTO app_audit (at, actor, action, detail) VALUES (?,?,?,?)",
                                             (time.time(), actor, action, json.dumps(detail, default=str))), None)

    async def recent_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        def fn(c: sqlite3.Connection) -> list[dict[str, Any]]:
            cur = c.execute("SELECT id, at, actor, action, detail FROM app_audit ORDER BY id DESC LIMIT ?", (limit,))
            return [{"id": r[0], "at": r[1], "actor": r[2], "action": r[3], "detail": json.loads(r[4])}
                    for r in cur.fetchall()]
        return await self._arun(fn, [])

    async def load_recent(self) -> dict[str, list]:
        """Restore state after a backend restart (latest run only).

        Rows that no longer validate (e.g. written by an older schema) are skipped and counted, never fatal.
        """
        empty: dict[str, list] = {"plans": [], "decisions": [], "alerts": [], "incidents": [], "run": [],
                                  "skipped": []}

        def parse(model, rows) -> tuple[list, int]:
            ok, bad = [], 0
            for (data,) in rows:
                try:
                    ok.append(model.model_validate_json(data))
                except ValueError:
                    bad += 1
            return ok, bad

        def fn(c: sqlite3.Connection) -> dict[str, list]:
            row = c.execute("SELECT max(id) FROM sim_runs").fetchone()
            run = row[0] if row else None
            if run is None:
                return empty
            plans, b1 = parse(PlanRecord, c.execute(
                "SELECT data FROM plans WHERE run_id=? ORDER BY id DESC LIMIT 200", (run,)))
            decisions, b2 = parse(Decision, c.execute(
                "SELECT data FROM decisions WHERE run_id=? ORDER BY id DESC LIMIT 1000", (run,)))
            alerts, b3 = parse(Alert, c.execute(
                "SELECT data FROM alerts WHERE run_id=? ORDER BY updated_at DESC LIMIT 500", (run,)))
            incidents, b4 = parse(Incident, c.execute(
                "SELECT data FROM incidents WHERE run_id=? ORDER BY updated_at DESC LIMIT 200", (run,)))
            return {"plans": plans, "decisions": decisions, "alerts": alerts, "incidents": incidents, "run": [run],
                    "skipped": [b1 + b2 + b3 + b4]}
        return await self._arun(fn, empty)
