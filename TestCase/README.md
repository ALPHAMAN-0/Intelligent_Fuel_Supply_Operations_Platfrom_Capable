# Test Cases — Fuel Supply Intelligence & Resilience Platform

This folder is the **authoritative test plan** for the project. It is split into three layers, each file being a
black-box or white-box specification that an engineer (human or agent) can pick up and execute.

> Every test below is derived from a real behaviour in the codebase (`server/fuelops/**`) and/or in the simulator
> integration guide (`ProblemSet/BUP_Fuel_Supply_Simulator_Integration_Guide_Final.pdf`). The `requirement` tag maps
> each test back to `plan.json` so coverage is traceable end-to-end.

## Layout

| Layer | File | What it covers |
|---|---|---|
| **Domain — pure logic** | [`01_unit_domain.md`](01_unit_domain.md) | `fuelops/shared/spec.py`, `fuelops/shared/world.py`, pydantic models, factories |
| **Intelligence — forecast / risk / planner / what-if** | [`02_unit_intelligence.md`](02_unit_intelligence.md) | `intelligence/forecast.py`, `risk.py`, `planner.py`, `evaluate.py` |
| **Integration — simulator client, SSE, breaker, errors** | [`03_integration_sim.md`](03_integration_sim.md) | `backend/integration/sim_client.py`, `sse_listener.py`, `breaker.py`, `sim_errors.py` |
| **End-to-end scenarios + chaos** | [`04_e2e_scenarios.md`](04_e2e_scenarios.md) | Crisis scenarios, fault injection, recovery, benchmark |
| **Index of test cases** | [`INDEX.md`](INDEX.md) | Flat list of every TC id with title + requirement |

## How to run

```bash
# Pure unit tests (no simulator): pytest, fastapi TestClient, respx mocks
cd server && pytest -m 'not integration'

# Integration tests (need a running simulator at SIM_BASE_URL):
cd server && SIM_BASE_URL=http://localhost:8000 pytest -m integration

# Scenario replay (uses /admin/*; resets the simulator)
python scripts/run_scenario.py scenarios/combined_crisis.yaml

# Policy benchmark (resets the simulator, runs both policies)
python scripts/benchmark.py
```

## Conventions used inside each file

```
TC-ID       Short, unique identifier (e.g. TC-W-001)
Title       One-line description
Requirement plan.json id this test guards (e.g. R-INT-P2, R-RES-01)
Target      File / function exercised
Pre-reqs    Setup state, fixtures, simulator status
Steps       Ordered actions
Asserts     Observable, deterministic checks
Notes       Edge cases, why it matters, link to ADR/rule
```

Tests are written to be **portable**: the same TC-IDs can be implemented as pytest functions, manual smoke-test
checklists, or scenario YAMLs. The Steps and Asserts are written so they survive refactors of the implementation
as long as the contract stays the same.
