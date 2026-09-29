"""Load test for the FuelOps backend (R-LOAD-01..03). SIMULATED DATA.

Two workloads against the real stack (docker compose --profile loadtest run --rm locust):
  * ConsoleViewer   - what an open operator console does: poll state, health, risks, alerts, plans, supply and a
                      station drawer. These are cache/DB reads (ADR-006) and never proxy to the simulator.
  * DecisionMaker   - the decision path: POST /api/evaluate (LP what-if, public, bounded by EVALUATE_MAX_CONCURRENCY
                      -> 429 is expected back-pressure, not an error) and POST /api/plans/compute (token-gated full
                      decision cycle; 409 while a cycle runs is expected single-flight behaviour).

The StepLoad shape ramps 10 -> 25 -> 50 -> 100 users so the report shows where latency bends, not one number.
Set LOAD_SHAPE=constant to use -u/-r/-t from the command line instead.
"""

from __future__ import annotations

import os
import random

from locust import HttpUser, LoadTestShape, between, events, task

TOKEN = os.environ.get("OPERATOR_TOKEN", "")
WRITE_WEIGHT = int(os.environ.get("LOAD_WRITE_WEIGHT", "1"))
# 1 = realistic operator think time; 0 = saturation test (every user fires back-to-back) to find the limits.
THINK = float(os.environ.get("LOAD_THINK_SCALE", "1"))
STATIONS = ["station-mirpur", "station-tongi", "station-karnaphuli", "station-coxsbazar"]
ROUTES: list[dict] = []


@events.test_start.add_listener
def discover_routes(environment, **_kwargs) -> None:
    import requests  # bundled with locust

    state = requests.get(f"{environment.host}/api/state", timeout=10).json()
    ROUTES.extend(state["world"]["routes"])


class ConsoleViewer(HttpUser):
    """An operator with the console open: the SPA refreshes a handful of read endpoints every few seconds."""

    weight = 4
    wait_time = between(1 * THINK, 3 * THINK)

    @task(5)
    def state(self) -> None:
        self.client.get("/api/state", name="GET /api/state")

    @task(3)
    def health(self) -> None:
        self.client.get("/api/health", name="GET /api/health")

    @task(3)
    def risks(self) -> None:
        self.client.get("/api/risks", name="GET /api/risks")

    @task(2)
    def alerts(self) -> None:
        self.client.get("/api/alerts?status=active", name="GET /api/alerts")

    @task(2)
    def plan(self) -> None:
        self.client.get("/api/plans/current", name="GET /api/plans/current")

    @task(1)
    def supply(self) -> None:
        self.client.get("/api/supply", name="GET /api/supply")

    @task(1)
    def regional(self) -> None:
        self.client.get("/api/demand/regional?ticks=4", name="GET /api/demand/regional")

    @task(1)
    def station(self) -> None:
        self.client.get(f"/api/stations/{random.choice(STATIONS)}", name="GET /api/stations/{id}")


class DecisionMaker(HttpUser):
    """The decision path: what-if evaluations and operator-triggered decision cycles."""

    weight = WRITE_WEIGHT
    wait_time = between(2 * THINK, 5 * THINK)

    @task(3)
    def what_if(self) -> None:
        if not ROUTES:
            return
        route = random.choice(ROUTES)
        body = {"lines": [{"route_id": route["id"], "fuel_type": random.choice(["DIESEL", "PETROL", "OCTANE"]),
                           "quantity": random.choice([1000, 2500, 5000])}]}
        with self.client.post("/api/evaluate", json=body, name="POST /api/evaluate", catch_response=True) as r:
            if r.status_code == 429:
                r.success()  # bounded concurrency working as designed
                events.request.fire(request_type="THROTTLE", name="POST /api/evaluate 429", response_time=0,
                                    response_length=0, exception=None, context={})

    @task(1)
    def compute(self) -> None:
        if not TOKEN:
            return
        with self.client.post("/api/plans/compute", name="POST /api/plans/compute", catch_response=True,
                              headers={"Authorization": f"Bearer {TOKEN}"}) as r:
            if r.status_code == 409:
                r.success()  # single-flight: a cycle is already running


# Locust uses any LoadTestShape defined in the module (and a tick() of None means "stop"), so the ramp only exists in
# steps mode; with LOAD_SHAPE=constant the -u/-r/-t command-line options apply.
if os.environ.get("LOAD_SHAPE", "steps") == "steps":

    class StepLoad(LoadTestShape):
        """Hold each concurrency level long enough for stable percentiles."""

        steps = [(10, 45), (25, 45), (50, 60), (100, 60)]  # (users, seconds)
        spawn_rate = 10

        def tick(self):
            elapsed = self.get_run_time()
            total = 0
            for users, duration in self.steps:
                total += duration
                if elapsed < total:
                    return users, self.spawn_rate
            return None
