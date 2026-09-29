"""Resilience drills against the running compose stack (R-RES-01..05, D-09). SIMULATED DATA.

    python scripts/resilience_drill.py            # host Python 3.11, stdlib only; needs docker compose + OPERATOR_TOKEN

Each drill breaks one thing the way it breaks in real life, measures how long the platform takes to notice and to
recover, and records what the operator sees. Simulator faults go through the platform's own token-gated chaos proxy;
service outages use `docker compose stop/start/restart`. Nothing is reset; the simulator keeps running.
Writes docs/resilience-drills.md.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = "http://localhost:8080"
TIMEOUT_S = 90.0


def token() -> str:
    found = ""
    env = ROOT / ".env"
    for line in (env.read_text(encoding="utf-8").splitlines() if env.exists() else []):
        key, _, value = line.partition("=")
        if key.strip() == "OPERATOR_TOKEN" and value.strip():
            found = value.strip()
    if not found:
        raise SystemExit("set OPERATOR_TOKEN in .env first (the drills use the token-gated chaos proxy)")
    return found


TOKEN = token()


def call(method: str, path: str, body: dict | None = None, auth: bool = False) -> tuple[int, object]:
    headers = {"Content-Type": "application/json"} | ({"Authorization": f"Bearer {TOKEN}"} if auth else {})
    req = urllib.request.Request(BACKEND + path, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        return 0, str(e)


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True, capture_output=True)


def health() -> dict:
    status, body = call("GET", "/api/health")
    return body if status == 200 else {"status": f"HTTP {status}", "components": []}


def component(name: str) -> dict:
    return next((c for c in health().get("components", []) if c["name"] == name), {"status": "?", "detail": ""})


def state() -> dict | None:
    status, body = call("GET", "/api/state")
    return body if status == 200 else None


def wait_for(check: Callable[[], bool], timeout: float = TIMEOUT_S) -> float | None:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            if check():
                return round(time.monotonic() - start, 1)
        except (KeyError, TypeError):
            pass
        time.sleep(0.5)
    return None


def open_system_alert(key: str) -> dict | None:
    _, alerts = call("GET", "/api/alerts?status=active")
    return next((a for a in alerts or [] if a["id"] == f"system:{key}"), None) if isinstance(alerts, list) else None


class Drill:
    def __init__(self, name: str, breaks: str, expect: str):
        self.name, self.breaks, self.expect = name, breaks, expect
        self.rows: list[tuple[str, str]] = []
        self.ok = True

    def note(self, what: str, value: object) -> None:
        self.rows.append((what, str(value)))
        print(f"   {what}: {value}", flush=True)

    def check(self, what: str, passed: bool, detail: object = "") -> None:
        self.ok &= bool(passed)
        self.note(f"{'PASS' if passed else 'FAIL'} {what}", detail)


def drill_intelligence_outage() -> Drill:
    d = Drill("Intelligence service outage", "`docker compose stop intelligence`",
              "heuristic fallback keeps deciding; system alert; health shows it; planner resumes on restart")
    compose("stop", "intelligence")
    t_detect = wait_for(lambda: state()["decision"]["fallback_active"])
    d.check("fallback activated", t_detect is not None, f"after {t_detect} s")
    s = state() or {}
    d.note("fallback reason", s.get("decision", {}).get("fallback_reason"))
    d.check("system alert raised", open_system_alert("intelligence") is not None,
            (open_system_alert("intelligence") or {}).get("title"))
    d.note("health: intelligence", f"{component('intelligence')['status']} ({component('intelligence')['detail']})")
    before = s.get("decision", {}).get("cycles", 0)
    time.sleep(8)
    s = state() or {}
    d.check("decision cycles continue during the outage", s["decision"]["cycles"] > before,
            f"{s['decision']['cycles'] - before} cycles in 8 s, last policy {s['decision']['last_policy_used']}")
    compose("start", "intelligence")
    t_recover = wait_for(lambda: not state()["decision"]["fallback_active"])
    d.check("planner back after restart", t_recover is not None, f"after {t_recover} s")
    d.check("alert cleared", wait_for(lambda: open_system_alert("intelligence") is None, 20) is not None)
    return d


def drill_simulator_unavailable() -> Drill:
    d = Drill("Simulator API unavailable (20 s)", "chaos fault `unavailable`: every /v1/* call returns 503",
              "retries, circuit breaker opens, UI served from cached state, degraded banner; automatic recovery")
    tick = (state() or {}).get("world", {}).get("instance", {}).get("tick")
    code, _ = call("POST", "/api/chaos/faults", {"type": "unavailable", "duration_seconds": 20}, auth=True)
    d.note("fault injected", f"HTTP {code} at tick {tick}")
    t_open = wait_for(lambda: "OPEN" in component("simulator_api")["detail"], 30)
    d.check("circuit breaker opened", t_open is not None, f"after {t_open} s: {component('simulator_api')['detail']}")
    s = state()
    d.check("state still served from cache (no 5xx to the UI)", s is not None,
            f"degraded={s['freshness']['degraded'] if s else '-'}, sync_ok={s['freshness']['sync_ok'] if s else '-'}")
    t_close = wait_for(lambda: "OPEN" not in component("simulator_api")["detail"] and state()["freshness"]["sync_ok"])
    d.check("recovered after the fault expired", t_close is not None,
            f"after {t_close} s more: {component('simulator_api')['detail']}")
    return d


def drill_stream_disconnect() -> Drill:
    d = Drill("SSE stream disconnect (20 s)", "chaos fault `stream_disconnect`: GET /v1/stream returns 503",
              "switch to polling, keep syncing, reconnect when the stream is back")
    call("POST", "/api/chaos/faults", {"type": "stream_disconnect", "duration_seconds": 20}, auth=True)
    tick0 = state()["world"]["instance"]["tick"]
    t_poll = wait_for(lambda: component("event_stream")["status"] != "ok", 30)
    d.check("stream loss detected", t_poll is not None, f"after {t_poll} s: {component('event_stream')['detail']}")
    time.sleep(5)
    d.check("ticks keep syncing by polling", state()["world"]["instance"]["tick"] > tick0,
            f"tick {tick0} -> {state()['world']['instance']['tick']}")
    t_back = wait_for(lambda: component("event_stream")["status"] == "ok")
    d.check("stream reconnected", t_back is not None, f"after {t_back} s: {component('event_stream')['detail']}")
    return d


def drill_stale_data() -> Drill:
    d = Drill("Stale simulator data (20 s)", "chaos fault `stale_data`: X-Simulator-Stale: true on /v1/* reads",
              "state marked stale, banner + alert, automatic approval blocked; clears when fresh")
    call("POST", "/api/chaos/faults", {"type": "stale_data", "duration_seconds": 20}, auth=True)
    t = wait_for(lambda: state()["freshness"]["stale"], 30)
    d.check("stale state detected", t is not None, f"after {t} s")
    d.check("system alert raised", wait_for(lambda: open_system_alert("stale_data") is not None, 15) is not None)
    _, plan = call("GET", "/api/plans/current")
    held = [ln.get("needs_review_reason") for ln in (plan or {}).get("lines", []) if ln.get("needs_review_reason")]
    d.note("current plan lines held for review", held[:2] or "no lines proposed this tick")
    t_fresh = wait_for(lambda: not state()["freshness"]["stale"])
    d.check("fresh again after the fault expired", t_fresh is not None, f"after {t_fresh} s")
    return d


def drill_database_down() -> Drill:
    d = Drill("Database unavailable", "chaos toggle: SQLite store forced down",
              "backend keeps running from memory, health + alert show it, history resumes on restore")
    call("POST", "/api/chaos/app/database", {"down": True}, auth=True)
    t = wait_for(lambda: component("database")["status"] == "down", 30)
    d.check("health shows database down", t is not None, f"after {t} s: {component('database')['detail']}")
    d.check("API still serving", state() is not None)
    d.check("system alert raised", wait_for(lambda: open_system_alert("database") is not None, 20) is not None)
    call("POST", "/api/chaos/app/database", {"down": False}, auth=True)
    t2 = wait_for(lambda: component("database")["status"] == "ok")
    d.check("database back", t2 is not None, f"after {t2} s")
    return d


def drill_backend_restart() -> Drill:
    d = Drill("Backend restart", "`docker compose restart backend`",
              "the current run's plans and decision history are restored from SQLite; decisions resume")
    run = state()["world"]["run_id"]
    _, plans = call("GET", "/api/plans?limit=300")
    _, decisions = call("GET", "/api/decisions?limit=2000")
    # The store restores the current run's latest 200 plans and 1000 decisions (store.load_recent).
    plan_ids = sorted(p["id"] for p in plans or [] if p["run_id"] == run)[-200:]
    decision_ids = sorted(x["id"] for x in decisions or [] if x["run_id"] == run)[-1000:]
    cycles_before = state()["decision"]["cycles"]
    compose("restart", "backend")
    t = wait_for(lambda: state() is not None, 120)
    d.check("back and synced", t is not None, f"after {t} s")
    _, plans_after = call("GET", "/api/plans?limit=300")
    _, decisions_after = call("GET", "/api/decisions?limit=2000")
    kept_plans = set(plan_ids) & {p["id"] for p in plans_after or []}
    kept_decisions = set(decision_ids) & {x["id"] for x in decisions_after or []}
    d.check("plans restored", len(kept_plans) == len(plan_ids), f"{len(kept_plans)} of {len(plan_ids)} (run {run})")
    d.check("decision history restored", len(kept_decisions) == len(decision_ids),
            f"{len(kept_decisions)} of {len(decision_ids)} (run {run})")
    d.check("decision loop resumed", wait_for(lambda: state()["decision"]["cycles"] > 0, 30) is not None,
            f"{state()['decision']['cycles']} cycles since restart (was {cycles_before} before)")
    return d


def to_markdown(drills: list[Drill]) -> str:
    out = ["# Resilience drills", "",
           "*SIMULATED DATA.* Generated by `python scripts/resilience_drill.py` against the running compose stack "
           f"({time.strftime('%Y-%m-%d %H:%M %Z')}). The simulator keeps running throughout; nothing is reset.", "",
           "| Drill | Break | Result |", "|---|---|---|"]
    out += [f"| {d.name} | {d.breaks} | {'PASS' if d.ok else 'FAIL'} |" for d in drills]
    for d in drills:
        out += ["", f"## {d.name}", "", f"**Break:** {d.breaks}  ", f"**Expected:** {d.expect}", "",
                "| Observation | Value |", "|---|---|"]
        out += [f"| {what} | {value.replace('|', '/')} |" for what, value in d.rows]
    return "\n".join(out) + "\n"


def main() -> int:
    if state() is None:
        raise SystemExit("backend is not serving state; start the stack with docker compose up -d")
    call("POST", "/api/chaos/run", auth=True)
    drills = []
    for fn in (drill_intelligence_outage, drill_simulator_unavailable, drill_stream_disconnect, drill_stale_data,
               drill_database_down, drill_backend_restart):
        print(f"== {fn.__name__}", flush=True)
        drills.append(fn())
        call("POST", "/api/chaos/faults/clear", auth=True)
        time.sleep(3)
    (ROOT / "docs" / "resilience-drills.md").write_text(to_markdown(drills), encoding="utf-8")
    print("wrote docs/resilience-drills.md")
    return 0 if all(d.ok for d in drills) else 1


if __name__ == "__main__":
    sys.exit(main())
