# Plan — Fuel Supply Intelligence & Resilience Platform

> BUP CSE Fest 2026 Hackathon Finals (in association with Poridhi.io).
> Design details: [`architecture.md`](architecture.md) · Working rules: [`CLAUDE.md`](CLAUDE.md) · Briefs: [`ProblemSet/`](ProblemSet/)
>
> **Before coding:** run `/grill-me` on this plan and update it with the answers.

---

## 1. Problem understanding

### 1.1 The challenge (participant brief)

Build and **operate** an intelligent decision-support platform for a *simulated* Bangladeshi fuel supply network that
helps an operations team:

- observe the network (inventory, depot/station status, demand, incoming supply, disruptions);
- identify emerging shortages and operational risks;
- decide how constrained fuel should be allocated — with inspectable reasoning;
- respond to unexpected disruptions (shipment delay, demand spike, depot constraint, route/regional disruption, combined crises);
- stay observable and usable when parts of the system fail;
- demonstrate measurable performance under load.

Engineering loop to demonstrate: **Observe → Detect → Predict → Decide → Simulate → Act → Monitor → Recover**.
"Not only a machine-learning challenge": application development, AI/decision intelligence and DevOps/reliability
are weighted together. *"Your job is not only to build the system. Your job is to keep it working."*

**Evaluation**

| Criterion | Weight | Assessed on |
|---|---|---|
| Working Product & User Experience | 20 % | Functional app, operational workflow, usability, completeness |
| Intelligence & Decision Quality | 20 % | Usefulness/quality of AI/ML/optimization/detection, methodology |
| Architecture & Integration | 15 % | Backend engineering, simulator integration, component design, coherence |
| DevOps & Engineering Quality | 15 % | Deployment, automation, testing, maintainability |
| Resilience & Incident Response | 10 % | Failure handling, crisis response, fallback, recovery |
| Observability & Performance | 10 % | Monitoring, metrics, logs, health visibility, load testing |
| Demo & Problem Understanding | 10 % | Clear explanation, constraints understanding, effective demo |

Organizers **may inject surprise domain and engineering events during development or judging** — the system must
cope with an environment that changes.

### 1.2 The simulator (integration guide)

- Image `asifmahmoud414/bup-fuel-supply-simulator:1.0.0` on `:8000`; env `SIMULATION_SPEED` (ticks/s, default 8),
  `TICK_MINUTES` (default 15), `SIMULATOR_START_MODE` (`paused` default).
- World: 2 regions, 2 depots, 4 stations, 6 routes, 3 fuels (DIESEL/PETROL/OCTANE), 22 scheduled supply arrivals,
  seed 12345, sim start `2026-01-01T00:00Z`.
- Reads: `/v1/{health,instance,regions,depots,stations,routes,supply-arrivals,events,allocations,demand-history,metrics}`.
- Only domain write: `POST /v1/allocations` (+ `POST /v1/allocations/{id}/cancel` for PENDING). `idempotency_key` in body.
- Push: SSE `/v1/stream` (`simulation.tick`, `allocation.status_changed`, `inventory.updated`, `simulator.notice`);
  hint only, no replay, 200-event buffer, 15 s keepalive.
- Admin (fault-free): run / pause / toggle / step / reset, inject crisis events (6 types), inject faults
  (latency, unavailable, error_rate, stale_data, stream_disconnect), audit log, HTML console at `/admin`.
- Metric that matters: `service_level = served / (served + unmet)`; also `allocation_failures`.

### 1.3 Derived domain insights (drive design choices)

| Insight | Implication |
|---|---|
| 1 sim day = 96 ticks; at default speed 8 ticks/s a sim day passes in **~12 s** wall-clock | Decision cycle must be milliseconds; LLM is async only; demos run at `SIMULATION_SPEED=1–2`; tests use pause + `/admin/step` |
| Network demand ≈ **95,000 L/day** (Dhaka ≈ 45k, Chattogram ≈ 50k with 1.08 factor) vs depot stock ≈ 252k L (~2.6 days) | Depots are fine in baseline; supply delay/shortfall events make depot stock the binding constraint → rationing by risk |
| Initial station stock covers only **~0.75–1.6 days** (e.g. Karnaphuli diesel ≈ 18 h, Mirpur petrol < 1 day); capacities ≈ 1–1.3 days of demand | Stockouts appear on **day 1** without action; each (station, fuel) needs roughly daily refills → ~16+ shipments/day given 5–7k L `max_shipment` |
| Hour-of-day factors are strong (0.45–1.55) and profile-specific; sim starts at midnight (off-peak) | Forecast must be hour-aware; busy windows (06:00+) drive the first stockouts |
| **Tongi is reachable only from Gazipur; Cox's Bazar only from Patiya** | Single points of failure → pre-position stock when a disruption is SCHEDULED; alert "no alternative route" |
| Cross-region routes `gazipur→karnaphuli`, `patiya→mirpur` (4 ticks, 5k L) | The only reroute options for disruptions and depot constraints |
| `DESTINATION_CAPACITY_EXCEEDED` checks on-hand only, not in-transit | Planner must use inventory position (on-hand + in-transit) to avoid overfilling |
| Dispatch capacity 12k (Gazipur) / 11k (Patiya) per tick | Binding in bursts (e.g. 7k + 6.5k in one tick) → spread shipments across ticks |
| Events appear in `/v1/events` as `SCHEDULED` before `start_tick` | Proactive planning, not just reaction |
| `shipment_delay` / `supply_shortfall` are one-shot and not undone | Detect by diffing supply arrivals against first-seen values |
| `demand-history` returns ≤ 2000 rows (≈ 166 ticks) | Mirror into our DB every tick; never rely on the simulator for long history |

### 1.4 Open questions — verify in the Phase 1 spike

1. What happens when an arrival would overflow station capacity (capped, lost, or rejected)?
2. Exact window of `DISPATCH_CAPACITY_EXCEEDED` ("in-flight + pending from this depot on this tick").
3. PENDING → IN_TRANSIT timing (example: created 5, departed 6, arrived 8 for transit 2).
4. Is demand recorded (as unmet) while a station is in `OUTAGE`?
5. Does `depot_constraint` reduce `dispatch_capacity_per_tick` numerically or only change status?
6. Idempotent replay: 201 (§5.4) or 200 (§9)? Handle both.
7. How the 22 supply arrivals split across depots/fuels (read `/v1/supply-arrivals` at tick 0).
8. Does region `demand_factor` multiply the profile base as assumed above?

Record findings in `docs/simulator-findings.md` and update the optimizer constraints accordingly.

---

## 2. Solution summary

A docker-compose platform (see `architecture.md`):

- **backend** (FastAPI): resilient simulator client, SSE listener with polling fallback, tick-driven state sync that
  mirrors history into Postgres, decision orchestrator with Manual / Assisted / Auto modes, preflight-guarded actuator,
  heuristic fallback policy, Claude-powered explanations with template fallback, operator API + SSE.
- **intelligence** (FastAPI): hour-aware demand forecaster (seasonal baseline + LightGBM residual), risk scorer
  (time-to-stockout, P(stockout)), anomaly/disruption detector, LP allocation optimizer, what-if projector.
- **frontend** (React): network overview, station detail, alerts/incidents, recommendation inspector, supply &
  shipments, decision history, system health, chaos panel.
- **ops**: Postgres, Prometheus + Grafana (dashboards + alert rules), structured logs, Locust load tests,
  GitHub Actions CI/CD to GHCR.

---

## 3. Team roles

| Role | Owns |
|---|---|
| **App** | Frontend console, backend `/api`, recommendation workflow UX |
| **AI** | Intelligence service, forecasting/risk/detection/optimizer, benchmark, LLM prompts |
| **DevOps** | Compose, CI/CD, observability stack, resilience/chaos scenarios, load testing |

Everyone: tests for their code, docs for their component, demo rehearsal.

---

## 4. Phases

Priority: **M** = must (MVP), **S** = should, **C** = could. The MVP line is after Phase 6 — at that point every
required deliverable exists in at least basic form.

### Phase 0 — Foundations (M) · DevOps

- Repo scaffold (`backend/`, `intelligence/`, `frontend/`, `infra/`, `loadtest/`, `scenarios/`, `docs/`).
- `docker-compose.yml` with simulator + postgres + skeleton services and health checks; `.env.example`.
- CI skeleton: lint + unit tests on push/PR.
- **Done when:** `docker compose up` starts everything healthy; `curl :8000/v1/health` OK; CI green.

### Phase 1 — Simulator integration & state (M) · App + DevOps

- Pydantic models for every simulator schema; typed async client with timeouts, retries, breaker, error-envelope
  parsing, stale-header handling.
- SSE listener (reconnect, watchdog, polling fallback); tick-driven resync; reset detection / run epochs.
- Postgres schema + Alembic; mirror demand history, snapshots, metrics, status changes.
- `GET /api/state`, `/api/health`.
- **Spike:** answer §1.4 open questions with a scripted pause → allocate → step experiment.
- **Done when:** integration test (reset → allocate → step 3 → ARRIVED) passes in CI against the real image;
  backend survives each fault type without crashing.

### Phase 2 — Prediction & detection (M) · AI

- Seasonal-naive forecaster (also backend fallback) → seasonal baseline × multiplier → LightGBM residual model.
- Forecast storage + online MAE/MAPE; `train.py` + versioned artifacts (`models/forecast-<ver>.{pkl,json}`).
- Risk scorer: inventory position, time-to-stockout, P(stockout) at 4/12/24 h; confidence score.
- Detectors: demand anomaly (z/CUSUM, regional correlation), supply delay/shortfall diff, network status changes,
  scheduled events, dispatch bottleneck.
- Alerts + incidents persisted and exposed via API.
- **Done when:** forecast MAPE reported per horizon; a demand_spike injection raises a regional alert within a few
  ticks; supply delay raises a supply-risk alert.

### Phase 3 — Decision engine (M) · AI + App

- Heuristic order-up-to policy (backend) — first working auto loop.
- LP optimizer (OR-Tools) with binding-constraint and alternatives output; what-if projector.
- Recommendation lifecycle (PROPOSED → … → ARRIVED/FAILED), expiry, preflight, idempotency keys, 409 handling map.
- Modes: Manual / Assisted / Auto; decision audit trail.
- **Done when:** in Auto mode over 3 sim days (baseline) the LP beats the heuristic and no-action baselines on
  service level (target ≥ 0.98, calibrate after the spike) with zero 409s caused by our own bugs; fallback switches
  automatically when intelligence is stopped.

### Phase 4 — Operator console (M) · App

- Network overview, station/depot detail with forecast charts, alerts & incidents, recommendation inspector
  (approve / modify with what-if / reject), supply & shipments (cancel PENDING), decision history, system health,
  chaos panel. Live updates via `/api/stream`. Stale/degraded/fallback banners. `SIMULATED DATA` badge.
- **Done when:** an operator can run the 14-step demo story entirely from the UI (plus Grafana).

### Phase 5 — Generative AI assist (S) · AI

- Claude explanations for recommendations, incident summaries, network briefing, grounded "ask" endpoint.
- Template fallback, caching, timeouts, metrics (`llm_requests_total{outcome}`, latency).
- **Done when:** every recommendation has an explanation (LLM or template); removing the API key changes only the
  explanation source.

### Phase 6 — Observability (M) · DevOps

- Prometheus metrics (app, integration, intelligence, business); JSON logs with run/tick/request ids.
- Grafana dashboards (Operations, Platform, Intelligence) + Prometheus alert rules, provisioned as code.
- UI health board fed by `/api/health`.
- **Done when:** injecting a fault visibly moves dashboard panels and fires the matching alert.

**——— MVP line ———**

### Phase 7 — Resilience hardening & crisis scenarios (M) · DevOps + AI

- Scenario files `scenarios/*.yaml` (baseline, demand-spike, shipment-delay, depot-constraint, route-disruption,
  combined-crisis) + `scripts/run_scenario.py` that injects them via `/admin/events` at given ticks.
- Chaos runs for every row of the resilience matrix (`architecture.md` §9); record evidence (screenshots, metrics,
  logs) in `docs/resilience-evidence.md`.
- **Done when:** each failure mode shows detect → degrade → recover without manual restarts.

### Phase 8 — Load testing (M) · DevOps

- Locust scenarios: dashboard read path (`GET /api/state`, `/api/risks`), end-to-end decision request
  (`POST /api/recommendations/compute` without LLM), intelligence `/forecast` + `/optimize`.
- Ramp concurrency (e.g. 10 → 50 → 100 → 200 users); report avg, p50, p95, p99, throughput, error rate,
  concurrency, CPU/memory (docker stats / cAdvisor); find the knee point and explain the bottleneck.
- Results in `loadtest/results/` + `docs/load-test-report.md`.
- **Done when:** report shows limits and behaviour, not just a number.

### Phase 9 — Delivery (M) · all

- CD: build + push images to GHCR tagged by SHA; compose smoke test job; rollback procedure documented.
- README (setup, config, run, demo), architecture diagram export, docs index.
- Benchmark report (§6). Demo rehearsal ×2 with the script in §5, including a surprise-event drill.
- **C:** RL policy vs heuristic/LP, K8s/Helm manifests, Loki, multi-agent decision system, drift detection.

---

## 5. Demo script (maps the brief's 14-step story)

Setup: `SIMULATION_SPEED=1`, mode **Assisted**, UI + Grafana side by side.

| # | Story step | What we do / show |
|---|---|---|
| 1 | Normal operations | Simulation running; service level ≈ 1.0; all health components green |
| 2 | Operator dashboard | Network overview, inventory gauges, incoming supply, health board |
| 3 | Demand starts increasing | Chaos panel: `demand_spike` on `region-dhaka`, ×1.8, starting in 4 ticks (appears as SCHEDULED) |
| 4 | System detects risk | Pre-emptive alert on the scheduled event, then demand-anomaly alert (residual z > 3 at Mirpur/Tongi) → incident |
| 5 | Intelligence predicts shortage | Mirpur petrol time-to-stockout drops (e.g. 14 h → 5 h); P(stockout) chart rises |
| 6 | Recommendation generated | LP proposes direct Gazipur→Mirpur shipment + extra cover; binding constraint = dispatch capacity |
| 7 | Operator inspects | Recommendation card: signals, constraints, confidence, alternatives, Claude explanation → Approve |
| 8 | Allocation is simulated | What-if risk before → after; submit; watch PENDING → IN_TRANSIT → ARRIVED |
| 9 | Crisis event occurs | `route_disruption` on `route-gazipur-mirpur` + `shipment_delay` at Gazipur (combined crisis) |
| 10 | System adapts | Reroute via `route-patiya-mirpur`; Tongi (single-route) prioritised; incident summary updated |
| 11 | Failure injected | `docker stop intelligence` and a simulator `error_rate` fault (0.5, 60 s) |
| 12 | Monitoring detects | Grafana `IntelligenceDown` / `SimulatorApiDegraded` fire; health board amber; breaker OPEN |
| 13 | Fallback activates | Heuristic recommendations tagged `fallback`; cached state with degraded banner; retries visible |
| 14 | Operations continue | Restart intelligence, fault expires → breakers close, LP resumes, service level recovers |

Surprise-event drill: any organizer-injected event must show up in the UI within one sync cycle, be classified into an
incident, and trigger re-optimization — rehearse with random scenario injections.

---

## 6. Decision-quality benchmark

`scripts/benchmark.py` — deterministic replay for fair comparison:

```
for scenario in [baseline, demand-spike, route-disruption, combined-crisis]:
  for policy in [no-action, naive-threshold, heuristic, lp]:
    POST /admin/pause ; POST /admin/reset ; inject scenario events
    repeat 288 ticks (3 sim days): decision cycle (Auto mode) → POST /admin/step
    record /v1/metrics: service_level, unmet liters, allocation_failures, liters shipped, #allocations, stockout-hours
```

Output: `docs/benchmark.md` table + chart. This is the evidence for "Intelligence & Decision Quality" and the
required justification if RL is attempted (RL must beat the heuristic here to be worth showing).

---

## 7. Traceability

### 7.1 Required deliverables (brief §19)

| # | Deliverable | Where / how | Phase |
|---|---|---|---|
| 1 | Working application | `docker compose up` → full platform | 0–4 |
| 2 | Source repository | This repo: code, README, `.env.example`, deployment docs | 0, 9 |
| 3 | Simulator integration | `backend/app/integration`, state sync, actuator | 1 |
| 4 | Intelligence component | `intelligence/` forecaster, risk, detection, LP optimizer | 2–3 |
| 5 | Operator interface | `frontend/` console | 4 |
| 6 | Architecture diagram | `architecture.md` §2 (mermaid) + exported PNG in `docs/` | 0, 9 |
| 7 | Deployment | docker-compose, GHCR images, CI/CD | 0, 9 |
| 8 | Observability evidence | Grafana dashboards, alert rules, logs, health board | 6 |
| 9 | Resilience demonstration | Chaos scenarios + `docs/resilience-evidence.md` | 7 |
| 10 | Load-test evidence | `loadtest/` + `docs/load-test-report.md` | 8 |
| 11 | Final demo | §5 script, rehearsed | 9 |

### 7.2 Recommended deliverables (brief §20)

| Item | Plan |
|---|---|
| CI/CD | GitHub Actions lint → test → integration → build → push → smoke |
| Automated tests | pytest (unit + integration vs real simulator), vitest |
| Experiment tracking | JSON metrics per model version in `models/` (MLflow optional) |
| Model versioning | `MODEL_VERSION` artifacts; `/model/info` |
| Decision audit history | `decisions` table + UI page |
| Deployment versioning | Images tagged by git SHA, `IMAGE_TAG` |
| Simulation replay | Deterministic benchmark/replay via reset + step |
| Scenario configuration | `scenarios/*.yaml` + runner |
| Automated fallback | Heuristic policy on intelligence failure; templates on LLM failure |
| Rollback | Image tag rollback; model version rollback; LP ↔ heuristic policy switch |

### 7.3 Evaluation criteria → evidence

| Criterion | Evidence we show |
|---|---|
| Product & UX (20) | End-to-end operator workflow in the console; HITL approval; live updates |
| Intelligence (20) | Forecast error metrics, risk scores, detection alerts, LP with explanations, benchmark table |
| Architecture (15) | `architecture.md`, clean service boundaries, contract-first API, simulator integration quality |
| DevOps (15) | One-command deploy, CI/CD pipeline, tests, versioned images, rollback |
| Resilience (10) | Chaos scenarios with detect → degrade → recover evidence |
| Observability & Perf (10) | Dashboards, alert rules, health board, load-test report |
| Demo (10) | §5 script, §1.3 constraint insights explained |

---

## 8. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Simulator semantics differ from our assumptions | Phase 1 spike; preflight + 409 handling; findings doc |
| Sim speed too fast for human approval | Demo at speed 1–2; Assisted mode auto-approves low-risk; recommendations expire |
| LLM latency, cost or outage | Async, cached, timeout, templates; never on decision path |
| Over-engineering (K8s, RL) steals time | MVP line; optional items only after Phase 9 core |
| Frontend/backend contract drift | OpenAPI-generated TS types; contract changes via PR |
| Surprise organizer events | Generic event/incident handling driven by `/v1/events` + state diffs, rehearsed drills |
| Windows/Docker environment differences | Everything runs in containers; pinned versions; CI uses the same compose |
| Losing history after simulator reset | Run epochs in Postgres; mirror history every tick |

---

## 9. Next steps

1. `/grill-me` on this plan → settle open decisions, update this file.
2. Phase 0 scaffold + compose + CI.
3. Phase 1 spike to answer §1.4 before building the optimizer constraints.
