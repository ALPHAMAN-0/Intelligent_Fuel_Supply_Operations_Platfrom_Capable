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

## Layout

```
server/                 One Python project and image, two services (ADR-009)
  fuelops/shared/       config, logging, metrics helpers, domain models shared by both services
  fuelops/backend/      simulator client, SSE listener, state sync, detectors, orchestrator, actuator, explanations, /api
  fuelops/intelligence/ structural forecaster, risk scorer, rolling-horizon LP planner (PuLP), what-if evaluator
  fuelops/simfake/      in-process simulator test double (tests only)
  tests/                pytest (unit, in-process e2e, contract, hardening)
frontend/               static ES-module operator console + login, served by the backend (ADR-013); npm is dev-only
infra/                  prometheus/ (scrape + alert rules), grafana/ (provisioning + generated dashboard)
loadtest/               Locust workload + results/
scenarios/              Crisis scenario YAMLs (CR-01..CR-05) for scripts/run_scenario.py
scripts/                run_scenario.py, benchmark.py, resilience_drill.py, fuelops_client.py, generate_training_data.py
docs/                   benchmark, scenarios/, resilience evidence + drills, load-test report, model selection
TestCase/               the team's test-case specifications + their pytest implementation
.github/workflows/      CI: lint, tests, contract tests vs the published simulator, compose smoke, GHCR push
.claude/skills/         Project skills (grill-me, grilling)
```

## Commands

```bash
cp .env.example .env && docker compose up -d --build                # full stack (set OPERATOR_TOKEN in .env first)
curl -s http://localhost:8080/api/health                             # platform health board
cd server && ruff check . ../TestCase ../scripts && pytest           # backend (host needs requirements-dev.txt)
cd server && CONTRACT_SIM_URL=http://localhost:8000 pytest tests/test_simulator_contract.py   # vs real simulator (resets it)
python -m pytest TestCase                                            # team test-case suite
cd frontend && npm ci && npm run lint && npm run test:coverage       # console + login
docker compose run --rm tools scripts/run_scenario.py scenarios/*.yaml   # crisis evidence (resets the simulator)
docker compose run --rm tools scripts/benchmark.py                   # policy benchmark (resets the simulator)
python scripts/resilience_drill.py                                   # resilience drills (stdlib; stops/starts services)
docker compose --profile loadtest run --rm locust                    # load test
```

No host Python deps? Run backend tests in the image: `docker build --build-arg INSTALL_DEV=1 -t fuelops-server:dev
-f server/Dockerfile .` then mount the repo and run `python -m pytest` inside it.

URLs: console :8080 (login `/login.html`) · backend :8080/docs · intelligence :8090/docs · simulator :8000/docs and
:8000/admin · Grafana :3001 · Prometheus :9090.

## Hard rules

1. **Never modify, fork or rebuild the simulator.** Judges run the published image
   `asifmahmoud414/bup-fuel-supply-simulator:1.0.0`.
2. **The only domain write is `POST /v1/allocations`** (and `/cancel`), and only through
   `server/fuelops/backend/decision/actuator.py`.
3. **`/admin/*` is for tests, scripts and the token-gated chaos drawer only** — never part of the decision loop.
4. **REST is the source of truth.** SSE events are hints: always re-GET after an event and after every reconnect.
5. **Always pass `limit`** to `/v1/demand-history` (clamped 1–2000); mirror history into the SQLite store (ADR-012).
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

- **Python 3.11**: FastAPI, pydantic v2, httpx async, `sqlite3` store (tables created at startup, no migrations),
  PuLP + CBC for the planner (pinned `<4`), stdlib JSON logs, prometheus-client; type hints; `ruff`; `pytest`
  (+ `pytest-asyncio`, `respx`, `pytest-cov`; backend coverage gate 80%).
- **Frontend**: dependency-free ES modules; build DOM only via `js/dom.js` (`textContent`, never `innerHTML`); strict
  per-page CSP; mutations only through `authorizedFetch`; ESLint; Vitest + jsdom with an 80% coverage gate.
- Metrics are prefixed `fuelops_`; log events use `domain.action` names (e.g. `decision.submitted`).
- Keep simulator schemas in `fuelops/backend/integration/sim_models.py` in sync with the integration guide.
- Small, focused commits; conventional-commit style (`feat:`, `fix:`, `docs:`, `test:`, `ci:`).
- When a change affects architecture or scope, update `architecture.md` / `plan.md` / `plan.json` in the same commit.

## Environment variables

| Variable | Used by | Purpose |
|---|---|---|
| `SIM_BASE_URL` | backend | Simulator URL (`http://simulator:8000` in compose) |
| `SIMULATION_SPEED`, `TICK_MINUTES`, `SIMULATOR_START_MODE` | simulator | Simulator clock (demo at speed 1–2) |
| `DATABASE_PATH` | backend | SQLite file (compose: on the `backend-data` volume) |
| `INTELLIGENCE_URL`, `INTELLIGENCE_TIMEOUT_S` | backend | Intelligence service URL and call timeout (default 3 s) |
| `EVALUATE_MAX_CONCURRENCY` | backend | Concurrent public what-if evaluations (default 4; extra get 429) |
| `PLANNER_POLICY` | backend | `planner` (default) or `heuristic` (forced fallback / policy rollback) |
| `PLAN_HORIZON_TICKS`, `PLAN_SAFETY_Z`, `PLAN_TTL_TICKS` | intelligence, backend | Planner horizon (24), demand quantile (1.0), plan expiry (2) |
| `AUTO_APPROVE_MIN_CONFIDENCE`, `AUTO_APPROVE_MAX_LITERS` | backend | Assisted-mode auto-approval gate |
| `ANTHROPIC_API_KEY` | backend | Claude API key (optional — templates used without it) |
| `LLM_MODEL`, `LLM_TIMEOUT_S` | backend | Claude model id (default `claude-opus-5-5`) and timeout (30 s; async, never on the decision path) |
| `OPERATOR_TOKEN` | backend | Bearer token for mutating operator actions and the chaos proxy; the operator enters it in the UI — never bake it into the frontend bundle |
| `CHAOS_ENABLED` | backend | Enable `/api/chaos/*` proxy |
| `MODEL_VERSION` | intelligence | Forecast parameter file version (`models/forecast-<ver>.json`) |
| `IMAGE_REPO`, `IMAGE_TAG` | compose | Image for backend + intelligence; deploy / roll back by tag (CI pushes `ghcr.io/<owner>/fuelops-server:<sha>`) |

`.env.example` is the complete, authoritative list (every `Settings` field plus compose/load-test variables).

## Skills

- `/grill-me` — relentless round-based interview to stress-test a plan or design (delegates to `grilling`).
  Use it when a surprise or finding changes the plan (see `plan.json` → `iteration_process`).
