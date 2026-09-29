# CLAUDE.md

Guidance for Claude Code (and humans) working in this repository.

## Project

**Fuel Supply Intelligence & Resilience Platform** — BUP CSE Fest 2026 Hackathon Finals. A decision-support platform
that observes the organizer's **BUP Fuel Supply Simulator**, predicts shortages, recommends/executes fuel allocations
with human review, and keeps working when components fail.

- [`plan.md`](plan.md) — problem understanding, phases, demo script, traceability. Read first.
- [`architecture.md`](architecture.md) — components, data flow, intelligence design, resilience, observability.
- [`ProblemSet/`](ProblemSet/) — original briefs (participant brief scan + simulator integration guide). The
  integration guide is the authoritative API contract.

## Layout (planned — create as phases land)

```
backend/        FastAPI: simulator client, SSE listener, state sync, orchestrator, actuator, explanations, /api
intelligence/   FastAPI: forecaster, risk scorer, detectors, LP optimizer, what-if; train.py; models/
frontend/       React + Vite + TypeScript operator console
infra/          prometheus/ (scrape + alert rules), grafana/ (provisioned dashboards)
loadtest/       Locust scenarios + results/
scenarios/      Crisis scenario YAMLs injected via /admin/events
scripts/        run_scenario.py, benchmark.py, helpers
docs/           simulator findings, resilience evidence, load-test report, benchmark
.claude/skills/ Project skills (grill-me, grilling)
```

## Commands (planned — keep this section in sync once the scaffold exists)

```bash
cp .env.example .env && docker compose up -d          # full stack
curl -s http://localhost:8000/v1/health                # simulator liveness
cd backend && uvicorn app.main:app --reload --port 8080
cd intelligence && uvicorn app.main:app --reload --port 8090
cd frontend && npm run dev
cd backend && ruff check . && pytest                   # same for intelligence/
cd frontend && npm run lint && npm test
docker compose --profile loadtest up locust            # load test
```

URLs: UI :3000 · backend :8080/docs · intelligence :8090/docs · simulator :8000/docs and :8000/admin ·
Grafana :3001 · Prometheus :9090.

## Hard rules

1. **Never modify, fork or rebuild the simulator.** Judges run the published image
   `asifmahmoud414/bup-fuel-supply-simulator:1.0.0`.
2. **The only domain write is `POST /v1/allocations`** (and `/cancel`), and only through `backend/app/decision/actuator.py`.
3. **`/admin/*` is for tests, scripts and the token-gated chaos panel only** — never part of the decision loop.
4. **REST is the source of truth.** SSE events are hints: always re-GET after an event and after every reconnect.
5. **Always pass `limit`** to `/v1/demand-history` (clamped 1–2000); mirror history into Postgres.
6. Handle **both error envelopes**: `{"detail": {...}}` (domain 404/409, and stream 503) and
   `{"error": {...}}` (injected faults); 422 is `{"detail": [...]}`. Treat 200 and 201 as success on replay.
7. **Idempotency keys** are deterministic: `fo-{run_id}-{recommendation_id}-{part}`; retry with the same key, never
   reuse a key for a different body.
8. Honour `X-Simulator-Stale: true`: mark state stale and block automatic actions.
9. **Deterministic tests**: `POST /admin/pause` → `POST /admin/reset` → act → `POST /admin/step` × N → assert.
10. **LLM is never on the critical decision path**: async, timeout, cached, template fallback.
11. Every recommendation must be inspectable (signals, constraints, impact, confidence, alternatives) and
    consequential actions keep human review (Manual/Assisted modes).
12. **No secrets in code.** Config via env / `.env` (gitignored); document every variable in `.env.example`.
13. Label everything as simulated data; never touch real fuel infrastructure or real credentials.

## Conventions

- **Python 3.11**: FastAPI, pydantic v2, httpx async, SQLAlchemy 2 async + Alembic, structlog JSON logs,
  prometheus-client; type hints everywhere; `ruff` for lint/format; `pytest` (+ `pytest-asyncio`).
- **TypeScript**: strict mode, ESLint, Vitest; API types generated from the backend OpenAPI (`openapi-typescript`).
- Metrics are prefixed `fuelops_`; log events use `domain.action` names (e.g. `decision.submitted`).
- Keep simulator schemas in `integration/sim_models.py` in sync with the integration guide.
- Small, focused commits/PRs; conventional-commit style (`feat:`, `fix:`, `docs:`, `test:`, `ci:`).
- When a change affects architecture or scope, update `architecture.md` / `plan.md` in the same PR.

## Environment variables

| Variable | Used by | Purpose |
|---|---|---|
| `SIM_BASE_URL` | backend | Simulator URL (`http://simulator:8000` in compose) |
| `SIMULATION_SPEED`, `TICK_MINUTES`, `SIMULATOR_START_MODE` | simulator | Simulator clock (demo at speed 1–2) |
| `DATABASE_URL` | backend, intelligence (training) | Postgres DSN |
| `INTELLIGENCE_URL` | backend | Intelligence service URL |
| `ANTHROPIC_API_KEY` | backend | Claude API key (optional — templates used without it) |
| `LLM_MODEL` | backend | Claude model id (default `claude-opus-5-5`) |
| `OPERATOR_TOKEN` | backend, frontend | Bearer token for mutating operator actions and chaos proxy |
| `CHAOS_ENABLED` | backend | Enable `/api/chaos/*` proxy |
| `MODEL_VERSION` | intelligence | Forecast model artifact version |
| `IMAGE_TAG` | compose | Image version for deploy / rollback |

## Skills

- `/grill-me` — relentless round-based interview to stress-test a plan or design (delegates to `grilling`).
  Use it on `plan.md` before starting a phase and on any significant design change.
