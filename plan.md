# Plan — Fuel Supply Intelligence & Resilience Platform

> BUP CSE Fest 2026 Hackathon Finals (in association with Poridhi.io).
> Design details: [`architecture.md`](architecture.md) · Working rules: [`CLAUDE.md`](CLAUDE.md) · Briefs: [`ProblemSet/`](ProblemSet/)
> Progress tracker: [`plan.json`](plan.json) — requirement ids, task ids and status for every iteration below.
>
> **Build window: ≤ 24 h, team of 2–3.** Work in time-boxed vertical slices; every slice ends with a runnable demo.

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
| 1 sim day = 96 ticks; at default speed 8 ticks/s a sim day passes in **~12 s** wall-clock | Decision cycle must be milliseconds; LLM is async only; demos run at `SIMULATION_SPEED=1–2`; tests use pause + `/admin/step`; plans are approved in batches |
| Network demand ≈ **95,000 L/day** (Dhaka ≈ 45k, Chattogram ≈ 50k with 1.08 factor) vs depot stock ≈ 252k L (~2.6 days) | Depots are fine in baseline; supply delay/shortfall events make depot stock the binding constraint → rationing by risk |
| Initial station stock covers only **~0.75–1.6 days** (e.g. Karnaphuli diesel ≈ 18 h, Mirpur petrol < 1 day); capacities ≈ 1–1.3 days of demand | Stockouts appear on **day 1** without action; each (station, fuel) needs roughly daily refills → ~16+ shipments/day given 5–7k L `max_shipment` |
| Demand = base × region × hour-of-day × `demand_multiplier` × jitter; the multiplier is on `/v1/stations`, spikes are in `/v1/events` | A learned **structural** forecast reaches the jitter noise floor; heavy ML adds little. The value is in timing and constraints (planner) |
| Hour-of-day factors are strong (0.45–1.55) and profile-specific; sim starts at midnight (off-peak) | Forecast must be hour-aware; busy windows (06:00+) drive the first stockouts |
| **Tongi is reachable only from Gazipur; Cox's Bazar only from Patiya** | Single points of failure → pre-position stock before a scheduled disruption; alert "no alternative route" |
| Cross-region routes `gazipur→karnaphuli`, `patiya→mirpur` (4 ticks, 5k L) | The only reroute options for disruptions and depot constraints |
| Shipments arrive `1 + transit_ticks` after creation; dispatch capacity 12k / 11k L per tick | Decisions must be **timed across ticks** → rolling-horizon planner, not a single-tick rule |
| `DESTINATION_CAPACITY_EXCEEDED` checks on-hand only, not in-transit | Planner must use inventory position (on-hand + in-transit) to avoid overfilling |
| Events appear in `/v1/events` as `SCHEDULED` before `start_tick` | Proactive planning, not just reaction |
| `shipment_delay` / `supply_shortfall` are one-shot and not undone | Detect by diffing supply arrivals against first-seen values |
| `demand-history` returns ≤ 2000 rows (≈ 166 ticks) | Mirror into our DB every tick; never rely on the simulator for long history |

### 1.4 Open questions — verify in the I1 spike (task I1-T8)

1. What happens when an arrival would overflow station capacity (capped, lost, or rejected)?
2. Exact window of `DISPATCH_CAPACITY_EXCEEDED` ("in-flight + pending from this depot on this tick").
3. PENDING → IN_TRANSIT timing (example: created 5, departed 6, arrived 8 for transit 2).
4. Is demand recorded (as unmet) while a station is in `OUTAGE`?
5. Does `depot_constraint` reduce `dispatch_capacity_per_tick` numerically or only change status?
6. Idempotent replay: 201 (§5.4) or 200 (§9)? Handle both.
7. How the 22 supply arrivals split across depots/fuels (read `/v1/supply-arrivals` at tick 0).
8. Does region `demand_factor` multiply the profile base as assumed above?
9. How does the published profile map to per-tick demand (the guide's example row, ≈ 95 L at 03:00 for Mirpur diesel,
   does not match a naive ≈ 62 L), and is the jitter independent per tick?

Record findings in `docs/simulator-findings.md` and update the planner constraints accordingly.
Open decision for the team: is an Anthropic API key available at the venue, and which model (see `plan.json` Q-10)?

---

## 2. Solution summary

A docker-compose platform (see `architecture.md`):

- **backend** (FastAPI): resilient simulator client, SSE listener with polling fallback, tick-driven state sync that
  mirrors history into Postgres, detectors (alerts/incidents), decision orchestrator producing one **plan per
  cycle** with Manual / Assisted / Auto approval, preflight-guarded actuator, heuristic fallback, Claude explanations
  with template fallback, operator API + SSE.
- **intelligence** (FastAPI, same Python project and image): structural demand forecaster, risk scorer
  (time-to-stockout, P(stockout), supply ETA), **rolling-horizon LP planner** that times shipments across transit
  lags, dispatch caps and scheduled disruptions, and a what-if evaluator using the same model.
- **frontend** (React): Overview, Plans, Alerts & incidents, Supply & shipments, Health (+ chaos drawer).
- **ops**: Postgres, Prometheus + one Grafana dashboard + 5 alert rules, structured logs, Locust load test,
  GitHub Actions CI.

---

## 3. Team and lanes (2–3 people)

| Lane | Owns |
|---|---|
| **App (A)** | Simulator client, state sync, actuator, backend API, frontend |
| **AI (B)** | Spike, forecaster, risk, planner, evaluator, benchmark, LLM explanations |
| **DevOps (C)** | Scaffold, compose, CI, metrics/dashboard/alerts, chaos runs, scenarios, load test, evidence docs |

With **2 people**: A also takes scaffold, compose, CI and metrics; B also takes scenarios and the load test.

---

## 4. Iterations — vertical slices, time-boxed

**Rules**

1. Each iteration ends with a **runnable, demoable system on `main`** (walking skeleton first, then deepen).
2. Time boxes are hard: if an iteration overruns, cut its *should/could* tasks — do not extend the box.
3. Grill once up front (done: best-solution review) and again only when a surprise changes the plan.
4. **MVP line after I3 (hour 18). Feature freeze at hour 21.** I4 may be partial.
5. Track task status and evidence in `plan.json`.

| Iteration | Hours | Ends with (demoable) |
|---|---|---|
| **I1 Walking skeleton** | 0–6 | Fuel moves automatically end to end; heuristic beats no-action; CI green; spike answered |
| **I2 Decision intelligence** | 6–13 | Explained, approvable plans from the rolling-horizon planner; benchmark shows it beats the heuristic |
| **I3 Resilience & observability** | 13–18 | Detect → degrade → recover for every failure mode; alerts, Grafana, Health view, chaos drawer |
| **I4 Crisis, GenAI, load** *(partial ok)* | 18–21 | Crisis scenarios, plan/incident explanations, load-test report |
| **I5 Delivery** | 21–24 | README, evidence docs, rehearsed demo, buffer for surprise events |

**Who does what in parallel**

| | App (A) | AI (B) | DevOps (C) |
|---|---|---|---|
| I1 | Sim client, state sync, actuator, Overview | Spike (Q-01..Q-09), heuristic policy | Scaffold, compose, CI + integration test, `/metrics` |
| I2 | Plans lifecycle + modes, Plans and Supply views | Forecaster, risk, planner, evaluator | Benchmark harness, metrics wiring |
| I3 | Breakers, SSE fallback, detectors, Health view + chaos drawer | Fallback path, detector tuning | Grafana dashboard, alert rules, logs, chaos runs |
| I4 | UI polish, explanation display | Claude explanations + templates | Scenarios, load test + report |
| I5 | README | Benchmark doc | Evidence docs, demo rehearsal |

### I1 — Walking skeleton (hours 0–6) · must

- I1-T1 Scaffold: `server/` (one Python project: `fuelops.backend`, `fuelops.intelligence`, `fuelops.shared`),
  `frontend/` (Vite), `infra/`, `scenarios/`, `scripts/`, `loadtest/`, `docs/`.
- I1-T2 `docker-compose.yml` (simulator, postgres, backend, intelligence, frontend, prometheus, grafana) with health
  checks and restart policies; `.env.example`.
- I1-T3 Pydantic simulator models + async client (timeouts, basic retry, error-envelope parsing, response validation).
- I1-T4 State sync (poll `/v1/instance`, resync per tick) + world cache + Postgres mirror (history, snapshots, metrics).
- I1-T5 Heuristic order-up-to policy + preflight + actuator (idempotency keys, splitting, 409 map), Auto mode.
- I1-T6 `/api/state` + Overview view (inventory, statuses, regional demand, service level, incoming supply,
  recent allocations, `SIMULATED DATA` badge).
- I1-T7 CI: ruff, pytest, eslint/tsc, integration test against the real simulator image, docker build.
- I1-T8 Spike: answer Q-01..Q-09 → `docs/simulator-findings.md`.
- I1-T9 `/metrics` baseline (request rate, latency, errors).
- **Done when:** `docker compose up` → fuel moves automatically; heuristic beats no-action over 1 sim day (step
  mode); CI green; spike answered.

### I2 — Decision intelligence (hours 6–13) · must

- I2-T1 Structural forecaster (spec prior → learned hour-of-day levels × `demand_multiplier`, σ from jitter,
  residual check, online MAE/MAPE vs noise floor, versioned params JSON).
- I2-T2 Risk scorer: inventory position, time-to-stockout, P(stockout) at 4/12/24 h, supply ETA, confidence.
- I2-T3 Rolling-horizon LP planner (H = 24 ticks; station/depot balance, dispatch caps, route/capacity limits,
  scheduled events; objective = unmet liters) with binding constraints, rationing and alternatives.
- I2-T4 What-if evaluator (no-action vs plan vs operator edit → risk before → after).
- I2-T5 Plans lifecycle: per-cycle plans, TTL, Manual/Assisted/Auto with confidence gate, batch approval, audit trail.
- I2-T6 Plans view (line cards with reasoning, approve/modify/reject, mode switch, decision-history tab) + station
  drawer (forecast band, projected inventory, time-to-stockout).
- I2-T7 Benchmark harness (`scripts/benchmark.py`): no-action vs heuristic vs planner, deterministic replay.
- I2-T8 Supply & shipments view (arrival schedule with delays, allocation ledger, cancel PENDING).
- **Done when:** planner ≥ heuristic > no-action on the benchmark; an operator approves a plan in the UI and watches
  it arrive; every plan line shows why, signals, constraints, impact, confidence, alternatives.

### I3 — Resilience & observability (hours 13–18) · must

- I3-T1 Circuit breakers, retries with jitter, stale-data handling (blocks auto-approval), degraded mode.
- I3-T2 SSE listener (reconnect, watchdog) with polling fallback.
- I3-T3 Intelligence fallback (timeout + breaker → heuristic, tagged), reset detection / run epochs, DB-down degrade.
- I3-T4 Detectors + incidents (events, network status, supply delay/shortfall, demand residual, inventory delta) +
  Alerts & incidents view.
- I3-T5 Prometheus metrics (integration, intelligence, business), JSON logs, one Grafana dashboard, 5 alert rules.
- I3-T6 `/api/health` + Health view + chaos drawer + operator token + stale/degraded/fallback banners.
- **Done when:** stopping intelligence and each simulator fault show detect → degrade → recover in the UI and
  Grafana without manual restarts. **← MVP line**

### I4 — Crisis, GenAI, load (hours 18–21) · must, partial allowed

- I4-T1 Scenario files (`scenarios/*.yaml`: demand-spike, shipment-delay, depot-constraint, route-disruption,
  combined-crisis) + `scripts/run_scenario.py`; record evidence in `docs/resilience-evidence.md`.
- I4-T2 Claude explanations for plans and incidents + network briefing (async, cached, timeout) with template fallback.
- I4-T3 Locust: dashboard read path + end-to-end decision request; ramp; avg/p50/p95/p99, throughput, errors,
  `docker stats`; `docs/load-test-report.md` explaining limits.
- **Done when:** each crisis shows detect / evaluate / respond / explain / monitor recovery; every plan has an
  explanation (LLM or template); the load report explains the knee point. **Feature freeze at hour 21.**

### I5 — Delivery (hours 21–24) · must

- I5-T1 README (setup, configuration, run, demo, data sources and assumptions) + rollback procedure.
- I5-T2 Architecture diagram export + docs index.
- I5-T3 Demo rehearsal ×2 with §5, including a surprise-event drill; keep the rest as buffer.
- I5-T4 *(should)* Push images to GHCR tagged by SHA; compose smoke-test job.

### Optional (could — only if time remains after I5 core)

Transport-delay prediction · operator investigation Q&A · RL policy vs planner · tracing / Loki / cAdvisor ·
LightGBM comparison against the structural forecast.
**Dropped for this build:** Kubernetes/Helm, multi-agent systems, drift detection, migration tooling, multiple
dashboards, DB write-buffer replay.

---

## 5. Demo script (maps the brief's 14-step story)

Setup: `SIMULATION_SPEED=1`, mode **Assisted**, UI + Grafana side by side.

| # | Story step | What we do / show |
|---|---|---|
| 1 | Normal operations | Simulation running; service level ≈ 1.0; all health components green |
| 2 | Operator dashboard | Overview: inventory gauges, regional demand, incoming supply, health board |
| 3 | Demand starts increasing | Chaos drawer: `demand_spike` on `region-dhaka`, ×1.8, starting in 4 ticks (appears as SCHEDULED) |
| 4 | System detects risk | Pre-emptive alert on the scheduled event → incident; residual alert if demand moves beyond the multiplier |
| 5 | Intelligence predicts shortage | Mirpur petrol time-to-stockout drops; P(stockout) and projected inventory in the station drawer |
| 6 | Recommendation generated | Planner's plan: Gazipur→Mirpur shipments timed before the spike; binding constraint = dispatch cap |
| 7 | Operator inspects | Plan line card: signals, constraints, confidence, alternatives, explanation → Approve |
| 8 | Allocation is simulated | What-if risk before → after; submit; watch PENDING → IN_TRANSIT → ARRIVED |
| 9 | Crisis event occurs | `route_disruption` on `route-gazipur-mirpur` + `shipment_delay` at Gazipur (combined crisis) |
| 10 | System adapts | Planner reroutes via `route-patiya-mirpur`, pre-stocks Tongi (single route); incident summary updated |
| 11 | Failure injected | `docker stop intelligence` and a simulator `error_rate` fault (0.5, 60 s) |
| 12 | Monitoring detects | Grafana `IntelligenceDown` / `SimulatorApiDegraded` fire; Health view amber; breaker OPEN |
| 13 | Fallback activates | Heuristic plans tagged `fallback`; cached state with degraded banner; retries visible |
| 14 | Operations continue | Restart intelligence, fault expires → breakers close, planner resumes, service level recovers |

Surprise-event drill: any organizer-injected event must appear in the UI within one sync cycle, open an incident and
change the next plan — rehearse with random injections.

---

## 6. Decision-quality benchmark

`scripts/benchmark.py` — deterministic replay for a fair comparison:

```
for scenario in [baseline, demand-spike, combined-crisis]:
  for policy in [no-action, heuristic, planner]:
    POST /admin/pause ; POST /admin/reset ; inject scenario events
    repeat 288 ticks (3 sim days): decision cycle (Auto mode) → POST /admin/step
    record /v1/metrics: service_level, unmet liters, allocation_failures, liters shipped, #allocations, stockout-hours
```

Output: `docs/benchmark.md` table. This is the evidence for "Intelligence & Decision Quality" (and the bar any
optional RL policy must clear).

---

## 7. Traceability

### 7.1 Required deliverables (brief §19)

| # | Deliverable | Where / how | Iteration |
|---|---|---|---|
| 1 | Working application | `docker compose up` → full platform | I1–I3 |
| 2 | Source repository | This repo: code, README, `.env.example`, deployment docs | I1, I5 |
| 3 | Simulator integration | `fuelops.backend.integration`, state sync, actuator | I1, I3 |
| 4 | Intelligence component | Forecaster, risk scorer, rolling-horizon planner, detectors | I2, I3 |
| 5 | Operator interface | `frontend/` five views | I1–I3 |
| 6 | Architecture diagram | `architecture.md` §2 (mermaid) + exported image in `docs/` | PLAN, I5 |
| 7 | Deployment | docker-compose, CI, versioned images | I1, I5 |
| 8 | Observability evidence | Grafana dashboard, alert rules, logs, Health view | I3 |
| 9 | Resilience demonstration | Chaos runs + crisis scenarios + `docs/resilience-evidence.md` | I3, I4 |
| 10 | Load-test evidence | `loadtest/` + `docs/load-test-report.md` | I4 |
| 11 | Final demo | §5 script, rehearsed | I5 |

### 7.2 Recommended deliverables (brief §20)

| Item | Plan |
|---|---|
| CI/CD | GitHub Actions lint → test → integration → build (→ GHCR push, smoke test) |
| Automated tests | pytest (unit + integration vs real simulator), vitest |
| Experiment tracking | Forecast metrics per model version + benchmark results |
| Model versioning | `models/forecast-<ver>.json`, `MODEL_VERSION`, `/model/info` |
| Decision audit history | `decisions` table + decision-history tab |
| Deployment versioning | Images tagged by git SHA (`IMAGE_TAG`) or git tags |
| Simulation replay | Deterministic benchmark via reset + step |
| Scenario configuration | `scenarios/*.yaml` + runner |
| Automated fallback | Heuristic on intelligence failure; templates on LLM failure |
| Rollback | Image/git tag rollback; model version rollback; planner ↔ heuristic switch |

### 7.3 Evaluation criteria → evidence

| Criterion | Evidence we show |
|---|---|
| Product & UX (20) | Five focused views, end-to-end workflow with batch approval, live updates |
| Intelligence (20) | Forecast error vs noise floor, risk scores, detectors, explained rolling-horizon plans, benchmark table |
| Architecture (15) | `architecture.md` + ADRs, clean service boundary, resilient simulator integration |
| DevOps (15) | One-command deploy, CI with real-simulator integration test, versioned images, rollback |
| Resilience (10) | Chaos runs + crisis scenarios with detect → degrade → recover evidence |
| Observability & Perf (10) | Grafana dashboard, alert rules, Health view, load-test report |
| Demo (10) | §5 script; §1.3 constraint insights explained |

---

## 8. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Running out of time | Vertical slices (always demoable), hard time boxes, MVP line at hour 18, feature freeze at hour 21 |
| Simulator semantics differ from our assumptions | I1 spike; preflight + 409 handling; findings doc |
| Planner bugs (infeasible/odd plans) | Heuristic fallback always available; benchmark catches regressions; unit tests on small cases |
| Sim speed too fast for human approval | Demo at speed 1–2; batch approval; Assisted mode; plan TTL |
| LLM latency, cost, outage or no API key | Async, cached, timeout, templates; never on decision path |
| Surprise organizer events | Generic handling from `/v1/events` + state diffs; drills in I5 |
| Windows/Docker environment differences | Everything in containers; pinned versions; CI uses the same compose |
| History lost after simulator reset | Run epochs in Postgres; mirror history every tick |

---

## 9. Next steps

1. Start **I1** at hour 0: scaffold + compose (C/A) while B runs the spike against the simulator.
2. Decide Q-10 (API key / model) before I4.
3. Optional: `/grill-me` on this plan if the team wants another pass (PLAN-T7).
