"""Shared client for the scenario runner and the policy benchmark.

Drives the simulator deterministically (pause -> reset -> step, CLAUDE.md rule 9) and reads the platform's view through
the backend API. /admin/* is used here because these are test/demo scripts (CLAUDE.md rule 3); the platform itself never
calls it outside the token-gated chaos drawer.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx

TICK_TIMEOUT_S = 30.0
POLL_S = 0.1


def operator_token() -> str:
    """OPERATOR_TOKEN from the environment, else from the repository's .env (never printed)."""
    token = os.environ.get("OPERATOR_TOKEN", "").strip()
    env_file = Path(__file__).resolve().parents[1] / ".env"
    if not token and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "OPERATOR_TOKEN":
                token = value.strip().strip('"').strip("'")
    if not token:
        raise SystemExit("OPERATOR_TOKEN is not set (env or .env); the scripts need it to change the operating mode")
    return token


class StackClient:
    def __init__(self, backend_url: str, sim_url: str, token: str):
        self.backend = httpx.Client(base_url=backend_url, timeout=15.0)
        self.sim = httpx.Client(base_url=sim_url, timeout=15.0)
        self.auth = {"Authorization": f"Bearer {token}"}

    # ------------------------------------------------------------------ simulator (admin)

    def admin(self, path: str, body: dict | None = None) -> Any:
        r = self.sim.post(f"/admin/{path}", json=body) if body is not None else self.sim.post(f"/admin/{path}")
        r.raise_for_status()
        return r.json() if r.content else None

    def sim_status(self) -> str:
        return self.sim.get("/v1/instance").json()["status"]

    # ------------------------------------------------------------------ backend

    def get(self, path: str, **params) -> Any:
        r = self.backend.get(path, params=params or None, headers=self.auth)
        r.raise_for_status()
        return r.json()

    def settings(self, **changes) -> dict:
        r = self.backend.put("/api/settings", json=changes, headers=self.auth)
        r.raise_for_status()
        return r.json()

    def state(self) -> dict:
        return self.get("/api/state")

    # ------------------------------------------------------------------ deterministic control

    def reset(self) -> dict:
        """Pause, clear faults, reset the simulator and wait until the backend has started the new run."""
        before = self._run_id()
        self.admin("pause")
        self.admin("faults/clear")
        self.admin("reset")
        deadline = time.monotonic() + TICK_TIMEOUT_S
        while time.monotonic() < deadline:
            state = self._try_state()
            if state and state["world"]["run_id"] != before and state["world"]["instance"]["tick"] == 0:
                return state
            time.sleep(POLL_S)
        raise TimeoutError("backend did not pick up the simulator reset")

    def step(self, expected_tick: int, wait_for_cycle: bool = True) -> dict:
        """Advance one tick and wait until the backend has synced it (and run its decision cycle)."""
        self.admin("step")
        deadline = time.monotonic() + TICK_TIMEOUT_S
        while time.monotonic() < deadline:
            state = self._try_state()
            if state and state["world"]["instance"]["tick"] >= expected_tick:
                decision = state["decision"]
                if not wait_for_cycle or not decision["enabled"] or (decision["last_cycle_tick"] or -1) >= expected_tick:
                    return state
            time.sleep(POLL_S)
        raise TimeoutError(f"backend did not process tick {expected_tick} within {TICK_TIMEOUT_S:.0f}s")

    def _run_id(self) -> int | None:
        state = self._try_state()
        return state["world"]["run_id"] if state else None

    def _try_state(self) -> dict | None:
        try:
            r = self.backend.get("/api/state")
            return r.json() if r.status_code == 200 else None
        except httpx.HTTPError:
            return None


def stockout_series(state: dict) -> int:
    """(station, fuel) series that are empty right now."""
    return sum(1 for st in state["world"]["stations"] for qty in st["inventory"].values() if qty < 1.0)


def service_level(state: dict) -> float:
    metrics = state["world"].get("metrics") or {}
    return float(metrics.get("service_level", 1.0))
