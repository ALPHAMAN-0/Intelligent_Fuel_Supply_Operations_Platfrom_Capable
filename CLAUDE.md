# CLAUDE.md

Guidance for Claude Code (and humans) working in this repository.

## Project

**Fuel Supply Intelligence & Resilience Platform** — BUP CSE Fest 2026 Hackathon Finals. A decision-support platform
that observes the organizer's **BUP Fuel Supply Simulator**, predicts shortages, plans and executes fuel allocations
with human review, and keeps working when components fail. **Build window ≤ 24 h, team of 2–3.**

- [`plan.md`](plan.md) — problem understanding, time-boxed iterations I1–I5, demo script, traceability. Read first.
- [`architecture.md`](architecture.md) — components, data flow, intelligence design, resilience, observability, ADRs.
- [`plan.json`](plan.json) — progress tracker: fixed main goal, requirement ids, simulator contract, iterations,
  task status, open questions, decisions. Follow its `iteration_process`; update task `status` + `evidence` as work
  lands; never edit `main_goal`.
- [`ProblemSet/`](ProblemSet/) — original briefs (participant brief scan + simulator integration guide). The
  integration guide is the authoritative API contract.

## Layout (planned — created in I1)

```
server/                 One Python project and image, two services
  fuelops/shared/       config, logging, metrics helpers, domain models shared by both services
  fuelops/backend/      simulator client, SSE listener, state sync, detectors, orchestrator, actuator, explanations, /api
  fuelops/intelligence/ structural forecaster, risk scorer, rolling-horizon LP planner, what-if evaluator
  tests/
frontend/               React + Vite + TypeScript operator console (five views)
infra/                  prometheus/ (scrape + alert rules), grafana/ (provisioned dashboard)
loadtest/               Locust scenarios + results/
scenarios/              Crisis scenario YAMLs injected via /admin/events
scripts/                run_scenario.py, benchmark.py, helpers
docs/                   simulator findings, resilience evidence, load-test report, benchmark
.claude/skills/         Project skills (grill-me, grilling)
```

## Commands (planned — keep this section in sync once the scaffold exists)

```bash
cp .env.example .env && docker compose up -d                        # full stack
curl -s http://localhost:8000/v1/health                              # simulator liveness
cd server && uvicorn fuelops.backend.main:app --reload --port 8080
cd server && uvicorn fuelops.intelligence.main:app --reload --port 8090
cd frontend && npm run dev
cd server && ruff check . && pytest
cd frontend && npm run lint && npm test
python scripts/benchmark.py                                          # policy benchmark (resets the simulator)
docker compose --profile loadtest up locust                          # load test
```

URLs: UI :3000 · backend :8080/docs · intelligence :8090/docs · simulator :8000/docs and :8000/admin ·
Grafana :3001 · Prometheus :9090.

## Hard rules

1. **Never modify, fork or rebuild the simulator.** Judges run the published image
   `asifmahmoud414/bup-fuel-supply-simulator:1.0.0`.
2. **The only domain write is `POST /v1/allocations`** (and `/cancel`), and only through
   `server/fuelops/backend/decision/actuator.py`.
3. **`/admin/*` is for tests, scripts and the token-gated chaos drawer only** — never part of the decision loop.
4. **REST is the source of truth.** SSE events are hints: always re-GET after an event and after every reconnect.
5. **Always pass `limit`** to `/v1/demand-history` (clamped 1–2000); mirror history into Postgres.
6. Handle **both error envelopes**: `{"detail": {...}}` (domain 404/409, and stream 503) and
   `{"error": {...}}` (injected faults); 422 is `{"detail": [...]}`. Treat 200 and 201 as success on replay.
7. **Idempotency keys** are deterministic: `fo-{run_id}-{plan_id}-{line}-{part}`; retry with the same key, never
   reuse a key for a different body.
8. Honour `X-Simulator-Stale: true`: mark state stale and block automatic approval.
9. **Deterministic tests**: `POST /admin/pause` → `POST /admin/reset` → act → `POST /admin/step` × N → assert.
10. **LLM is never on the critical decision path**: async, timeout, cached, template fallback.
11. Every plan line must be inspectable (why at risk, signals, binding constraints, impact before → after,
    confidence, alternatives) and consequential actions keep human review (Manual/Assisted modes).
12. **The heuristic fallback must always work** — the decision loop never depends on the intelligence service being up.
13. **No secrets in code.** Config via env / `.env` (gitignored); document every variable in `.env.example`.
14. Label everything as simulated data; never touch real fuel infrastructure or real credentials.
15. **`main` must always run** with `docker compose up` (vertical slices; merge small and often).

## Conventions

- **Python 3.11**: FastAPI, pydantic v2, httpx async, SQLAlchemy 2 async (tables via `create_all`, no migrations),
  OR-Tools GLOP for the planner, structlog JSON logs, prometheus-client; type hints; `ruff`; `pytest`
  (+ `pytest-asyncio`).
- **TypeScript**: strict mode, ESLint, Vitest, TanStack Query, Tailwind, Recharts.
- Metrics are prefixed `fuelops_`; log events use `domain.action` names (e.g. `decision.submitted`).
- Keep simulator schemas in `fuelops/backend/integration/sim_models.py` in sync with the integration guide.
- Small, focused commits; conventional-commit style (`feat:`, `fix:`, `docs:`, `test:`, `ci:`).
- When a change affects architecture or scope, update `architecture.md` / `plan.md` / `plan.json` in the same commit.

## Environment variables

| Variable | Used by | Purpose |
|---|---|---|
| `SIM_BASE_URL` | backend | Simulator URL (`http://simulator:8000` in compose) |
| `SIMULATION_SPEED`, `TICK_MINUTES`, `SIMULATOR_START_MODE` | simulator | Simulator clock (demo at speed 1–2) |
| `DATABASE_URL` | backend | Postgres DSN |
| `INTELLIGENCE_URL`, `INTELLIGENCE_TIMEOUT_S` | backend | Intelligence service URL and call timeout (default 1.5 s) |
| `PLANNER_POLICY` | backend | `planner` (default) or `heuristic` (forced fallback / policy rollback) |
| `PLAN_HORIZON_TICKS`, `PLAN_SAFETY_Z`, `PLAN_TTL_TICKS` | intelligence, backend | Planner horizon (24), demand quantile (1.0), plan expiry (2) |
| `AUTO_APPROVE_MIN_CONFIDENCE`, `AUTO_APPROVE_MAX_LITERS` | backend | Assisted-mode auto-approval gate |
| `ANTHROPIC_API_KEY` | backend | Claude API key (optional — templates used without it) |
| `LLM_MODEL`, `LLM_TIMEOUT_S` | backend | Claude model id (default `claude-opus-5-5`) and timeout (8 s) |
| `OPERATOR_TOKEN` | backend | Bearer token for mutating operator actions and the chaos proxy; the operator enters it in the UI — never bake it into the frontend bundle |
| `CHAOS_ENABLED` | backend | Enable `/api/chaos/*` proxy |
| `MODEL_VERSION` | intelligence | Forecast parameter file version (`models/forecast-<ver>.json`) |
| `IMAGE_TAG` | compose | Image version for deploy / rollback |

## Skills

- `/grill-me` — relentless round-based interview to stress-test a plan or design (delegates to `grilling`).
  Use it when a surprise or finding changes the plan (see `plan.json` → `iteration_process`).
