# 04 — End-to-End Scenarios & Chaos

These tests need a **running** simulator. They follow the determinism contract (CLAUDE.md rule 9):
`POST /admin/pause → POST /admin/reset → act → POST /admin/step × N → assert`.

Targets: full stack via `scripts/run_scenario.py`, `scripts/benchmark.py`, the chaos drawer, and the operator API.

---

## TC-E-001 — Baseline: stockouts appear on day 1 without action
- **Requirement:** R-INT-P1, plan §1.3
- **Target:** scenario runner
- **Pre-reqs:** simulator `RUNNING`, baseline scenario, no allocations.
- **Steps:** step 1 simulated day (96 ticks).
- **Asserts:**
  - At least one (station, fuel) series has `served_liters == 0` while `demand_liters > 0`.
  - Tongi (single-route) and Karnaphuli diesel are the first to deplete.
  - Service level drops below 0.95 within the first day.

## TC-E-002 — Planner prevents stockouts on a 7-day baseline
- **Requirement:** R-PLAT-03, R-INT-X1
- **Target:** orchestrator + planner + actuator
- **Pre-reqs:** simulator `RUNNING`; backend + intelligence running; `operating_mode=assisted`; operator token set.
- **Steps:** run 7 simulated days, approving every plan.
- **Asserts:**
  - `service_level` stays above 0.98.
  - No allocation ends in `FAILED` status.
  - `fuelops_allocations_submitted_total{outcome="success"}` ≈ number of approved lines.

## TC-E-003 — Idempotent retry of an allocation returns the same id
- **Requirement:** CLAUDE.md rule 7
- **Target:** `actuator.create_allocation`
- **Pre-reqs:** planner emits one line.
- **Steps:** POST `/v1/allocations` twice with the same `idempotency_key` and body.
- **Asserts:** both responses have the same `id`; `allocation_failures` did not increment.

## TC-E-004 — Demand-spike crisis: simulator event triggers a plan within one tick
- **Requirement:** R-INT-D1, R-CRISIS-01
- **Target:** orchestrator + risk scorer
- **Pre-reqs:** simulator `RUNNING`, intelligence up.
- **Steps:**
  1. Pause, reset.
  2. Inject `demand_spike` (region-wide, multiplier=2.0, duration=8 ticks).
  3. Resume; step 2 ticks.
- **Asserts:**
  - `stations[d].demand_multiplier` reflects the active spike.
  - At least one plan line targets a station in the spiked region.
  - `stations_at_risk` gauge (or equivalent) increased compared to baseline.

## TC-E-005 — Route disruption forces a cross-region reroute
- **Requirement:** R-RES-01, R-INT-X1
- **Target:** planner reroute logic
- **Pre-reqs:** `RUNNING`, baseline scenario.
- **Steps:** inject `route_disruption SCHEDULED 20..40 route_ids=[route-gazipur-mirpur]`. Step 25 ticks.
- **Asserts:**
  - No allocation uses `route-gazipur-mirpur` while it is disrupted.
  - At least one Mirpur-targeted allocation uses `route-patiya-mirpur` (cross-region).
  - The Mirpur line's `signals` contain `"active route_disruption ..."` and `"cross-region reroute"`.

## TC-E-006 — Station outage does not consume fuel
- **Requirement:** R-INT-D1
- **Target:** risk scorer + planner
- **Pre-reqs:** baseline scenario.
- **Steps:** inject `station_outage SCHEDULED 30..50 station_ids=[station-tongi]`. Step 35 ticks.
- **Asserts:**
  - Tongi's `served_liters` remains 0 across the outage.
  - Tongi's `unmet_liters == demand_liters` for the outage window.
  - No allocation is routed to Tongi during the outage.
  - After the outage ends, Tongi's inventory is replenished by the next plan.

## TC-E-007 — Depot constraint reduces dispatch capacity and biases plans
- **Requirement:** R-INT-D2
- **Target:** planner + `committed_dispatch`
- **Steps:** inject `depot_constraint SCHEDULED 10..30 depot_ids=[depot-gazipur]`. Step 15 ticks.
- **Asserts:**
  - Plans targeting Gazipur within the window honour the reduced capacity.
  - `binding_constraints` lists `"Dispatch capacity binding at depot-gazipur"` or a depot-stock note.

## TC-E-008 — Shipment delay pushes the supply ETA
- **Requirement:** R-INT-D2
- **Target:** `supply_etas`
- **Steps:** inject `shipment_delay SCHEDULED 5..10 depot_ids=[depot-gazipur] delay_ticks=8`. Step 8 ticks.
- **Asserts:** `/api/supply` returns the affected arrival with `eta_tick = planned_tick + 8`, `delayed=True`.

## TC-E-009 — Supply shortfall halves a future arrival
- **Requirement:** R-INT-D2
- **Steps:** inject `supply_shortfall SCHEDULED 0..50 depot_ids=[depot-patiya] fuel_types=[PETROL] factor=0.5`.
- **Asserts:** the affected supply arrival has `expected_quantity = 0.5 * original` and `shortfall=True`.

## TC-E-010 — `latency` fault: simulator is slow but correct
- **Requirement:** R-RES-02
- **Target:** integration tolerance
- **Steps:** `admin_inject_fault(latency, 30s, parameters={"endpoint":"/v1/stations", "delay_ms": 2000})`. Run one tick.
- **Asserts:** backend's `get_stations` succeeds but `SIM_LATENCY` histogram shows ~2 s. No breaker open.

## TC-E-011 — `unavailable` fault: the client retries and eventually succeeds
- **Requirement:** R-RES-02, R-RES-04
- **Steps:** `admin_inject_fault(unavailable, 10s, parameters={"endpoint":"/v1/stations"})`.
- **Asserts:** with `retries=3`, the first attempts return `SimError(kind=FAULT)`; once faults are cleared
  (`admin_clear_faults()`), the call succeeds. `last_error` reflects the last failure.

## TC-E-012 — `error_rate` fault: every Nth call fails
- **Requirement:** R-RES-02
- **Steps:** `admin_inject_fault(error_rate, 10s, parameters={"endpoint":"/v1/stations", "rate":0.5})`.
- **Asserts:** out of 20 calls, ≈ half succeed; retry budget absorbs the rest; eventually a successful read.

## TC-E-013 — `stale_data` fault: backend marks state stale and blocks auto-approve
- **Requirement:** R-PLAT-04, CLAUDE.md rule 8
- **Steps:** `admin_inject_fault(stale_data, 30s, parameters={"endpoint":"/v1/stations"})`. Step 2 ticks.
- **Asserts:**
  - `GET /v1/stations` returns 200 + `X-Simulator-Stale: true`.
  - Backend `/api/state.stale == True`.
  - In `assisted` mode with `AUTO_APPROVE_MIN_CONFIDENCE` triggered, no allocation is auto-submitted until the
    fault is cleared and `X-Simulator-Stale: false` is observed.

## TC-E-014 — `stream_disconnect` fault: SSE listener reconnects with polling fallback
- **Requirement:** R-RES-02
- **Steps:** `admin_inject_fault(stream_disconnect, 15s, parameters={})`. Run 30 ticks at speed 2.
- **Asserts:**
  - `SSE_RECONNECTS` counter increments.
  - Backend falls back to polling (`SSE_POLLING=1`).
  - Decisions continue; `service_level` does not drop more than 5 % vs. baseline.

## TC-E-015 — Combined crisis: spike + disruption + supply shortfall
- **Requirement:** R-CRISIS-01
- **Steps:** inject within one tick: `demand_spike` (region-dhaka, ×2, 30 ticks), `route_disruption`
  (gazipur→mirpur, 40 ticks), `supply_shortfall` (patiya, DIESEL, 0.4 factor). Run 60 ticks.
- **Asserts:**
  - At least 3 alerts are open at some point (`SHORTAGE_ALERTS` increases).
  - The planner uses the cross-region route `patiya→mirpur` for Mirpur shipments.
  - No station runs out of fuel for more than 2 ticks.

## TC-E-016 — Determinism: same scenario + same seed + same actions = same plan sequence
- **Requirement:** CLAUDE.md rule 9
- **Steps:** reset, run scenario A for 100 ticks, record each plan. Reset, replay the exact same actions.
- **Asserts:** every plan line id and quantity matches tick for tick.

## TC-E-017 — `cancel_allocation` succeeds only for PENDING allocations
- **Requirement:** R-PLAT-03, guide §5.5
- **Steps:** create an allocation, immediately cancel before step advances; advance one tick so it becomes
  IN_TRANSIT, try to cancel.
- **Asserts:** first cancel returns 200 with `status=CANCELLED`; second returns 409 `CANNOT_CANCEL`.

## TC-E-018 — `idempotency_key` mismatch on retry returns 409 `IDEMPOTENCY_KEY_MISMATCH`
- **Requirement:** CLAUDE.md rule 7
- **Steps:** POST `/v1/allocations` with key K and body B1 (succeeds). POST with key K and body B2.
- **Asserts:** second response is 409 `IDEMPOTENCY_KEY_MISMATCH`. No new allocation is created.

## TC-E-019 — `DESTINATION_CAPACITY_EXCEEDED` is raised when an allocation would overflow the tank
- **Requirement:** R-PLAT-03
- **Steps:** fill Mirpur PETROL to `capacity - 100`. POST a 5 000 L shipment.
- **Asserts:** 409 `DESTINATION_CAPACITY_EXCEEDED`; inventory unchanged; the orchestrator marks this line as
  rejected and skips the next plan's attempt.

## TC-E-020 — `DISPATCH_CAPACITY_EXCEEDED` accounts for in-flight + pending this tick
- **Requirement:** R-PLAT-03
- **Steps:** with `dispatch_capacity_per_tick = 6000`, schedule 6 000 L at tick 0 and a 1 000 L additional at
  tick 0 from the same depot.
- **Asserts:** second allocation is 409 `DISPATCH_CAPACITY_EXCEEDED`. Submitting a 1 000 L allocation at tick 1
  succeeds.

## TC-E-021 — `ROUTE_DISRUPTED` blocks new allocations to that route
- **Requirement:** R-PLAT-03
- **Steps:** set `route-gazipur-mirpur.status=DISRUPTED` via `/v1/routes` (read-only? if not, simulate via
  `route_disruption` event). Try to POST.
- **Asserts:** 409 `ROUTE_DISRUPTED`.

## TC-E-022 — `INSUFFICIENT_INVENTORY` blocks an over-sized allocation at the depot
- **Requirement:** R-PLAT-03
- **Steps:** drain `depot-gazipur` PETROL to 200 L. POST a 5 000 L shipment from it.
- **Asserts:** 409 `INSUFFICIENT_INVENTORY`. `fuelops_allocations_submitted_total{outcome="insufficient"}` increments.

## TC-E-023 — `OPERATOR_TOKEN` gates `/api/allocations`, `/api/chaos/*`, and `/admin/*`
- **Requirement:** R-DEVOPS-02
- **Steps:** call each route without the header, with a wrong header, with the right header.
- **Asserts:** 401/403 without or wrong; 200/202 with the right token. `OPERATOR_TOKEN` is not present in any
  response payload or log line.

## TC-E-024 — Chaos drawer proxies `/admin/faults` only when `CHAOS_ENABLED=true`
- **Requirement:** R-RES-02
- **Steps:** with `CHAOS_ENABLED=false`, POST `/api/chaos/faults`; with `CHAOS_ENABLED=true`, do the same and
  then call `admin_clear_faults`.
- **Asserts:** 404 in the disabled case; 202 in the enabled case; faults are listed in `GET /v1/stations`
  response shape (or simulator's admin panel) after the second case.

## TC-E-025 — LLM explanation endpoint times out gracefully and falls back to a template
- **Requirement:** R-RES-04
- **Steps:** set `LLM_TIMEOUT_S=1` and point `ANTHROPIC_BASE_URL` at a black-hole address. Trigger an
  explanation for a plan.
- **Asserts:** endpoint returns 200 within 2 s; the response body uses the template fallback (no `model` field
  or marked as `template`). `LLM_REQUESTS{outcome="timeout"}` increments.

## TC-E-026 — Plan TTL expires and a new plan is generated
- **Requirement:** R-PLAT-03, `plan_ttl_ticks=2`
- **Steps:** generate a plan, store its id, do not submit. Step 3 ticks. Request a new plan.
- **Asserts:** the cached plan is invalidated; the new plan may differ (same situation, but planner re-runs
  with the new state).

## TC-E-027 — `/admin/reset` wipes allocations and reverts state to the baked scenario
- **Requirement:** R-PLAT-04, CLAUDE.md rule 3
- **Steps:** create three allocations, then `admin_reset`. GET `/v1/allocations`.
- **Asserts:** response is `[]`. `GET /v1/instance.tick` is the scenario's start tick.

## TC-E-028 — Auto-approve gate blocks shipments that exceed `AUTO_APPROVE_MAX_LITERS`
- **Requirement:** R-PLAT-03
- **Steps:** `operating_mode=assisted`, `AUTO_APPROVE_MAX_LITERS=3000`. Generate a plan with a 7 000 L line.
- **Asserts:** line appears in the operator queue (`/api/plans/{id}/lines`) with `auto_approved=False` and is
  not submitted until the operator approves it.

## TC-E-029 — Auto-approve gate blocks low-confidence plans
- **Requirement:** R-PLAT-03
- **Steps:** `AUTO_APPROVE_MIN_CONFIDENCE=0.9`. Generate a plan with `confidence < 0.9`.
- **Asserts:** line is queued, not auto-submitted. The UI shows "needs review".

## TC-E-030 — Heuristic fallback engages when the intelligence service is down
- **Requirement:** R-RES-05, CLAUDE.md rule 12
- **Steps:** stop the intelligence container (`docker compose stop intelligence`). Step 5 ticks.
- **Asserts:**
  - `fuelops_intelligence_up` gauge is 0.
  - `/api/plans` still returns plans produced by the heuristic fallback.
  - `FALLBACK_ACTIVATIONS{reason="intelligence_down"}` increments.
  - No exception is raised; the operator console remains responsive.

## TC-E-031 — `POST /v1/allocations` retry storm collapses after the breaker opens
- **Requirement:** R-RES-01
- **Steps:** inject `error_rate` fault with `rate=1.0` for 60 s on `/v1/allocations`.
- **Asserts:** after `breaker_failure_threshold=5`, the write breaker opens; subsequent actuator calls fail
  fast with `SimError(kind=BREAKER_OPEN)` and do not hammer the simulator.

## TC-E-032 — History mirroring caps at the 2 000-row window
- **Requirement:** R-PLAT-04, CLAUDE.md rule 5
- **Steps:** step 250 ticks; query `/api/demand-history?limit=5000`.
- **Asserts:** response size ≤ 2 000 rows; total mirrored rows in our DB grows monotonically; oldest mirrored
  rows are dropped when the window is exceeded.

## TC-E-033 — Demand forecast MAE matches the spec noise floor on baseline data
- **Requirement:** R-INT-P1, R-DEVOPS-01
- **Steps:** run `scripts/generate_training_data.py --days 7`; run `scripts/benchmark.py` (planner policy);
  read the metrics emitted to `/metrics` or the JSON report.
- **Asserts:** `fuelops_forecast_mape{horizon="4"} ≤ 0.13` (spec noise floor 8–12 % + small planning slack).

## TC-E-034 — Load test under 100 RPS keeps p95 latency below 250 ms
- **Requirement:** R-DEVOPS-01, R-RES-04
- **Steps:** `docker compose --profile loadtest up locust --headless -u 100 -r 20 --run-time 5m`.
- **Asserts:** Locust report shows p95 of `/api/state` < 250 ms; 0% errors; SSE remains connected on the polling
  fallback or the live stream.

## TC-E-035 — `docker compose up` brings the entire stack to healthy in under 60 s
- **Requirement:** R-DEVOPS-01
- **Steps:** `docker compose down -v && docker compose up -d && time -- bash -c 'until curl -fs
  http://localhost:8080/health && curl -fs http://localhost:8090/health && curl -fs http://localhost:8000/v1/health;
  do sleep 1; done'`.
- **Asserts:** all three services return 200 within 60 s. `/health` of the backend reports
  `{"status": "ok", "dependencies": {"simulator": "ok", "intelligence": "ok", "db": "ok"}}`.

## TC-E-036 — Rolling image rollback restores a previous release
- **Requirement:** R-DEVOPS-01
- **Steps:** `IMAGE_TAG=v0.2.0 docker compose up -d`; verify. `IMAGE_TAG=v0.1.0 docker compose up -d`; verify.
- **Asserts:** both versions boot; `fuelops_service_level` non-zero within 30 s of start; the running images
  match the requested tag (`docker inspect --format '{{ index .Config.Labels "image.tag" }}'`).

## TC-E-037 — Metrics endpoint exposes all expected `fuelops_*` series
- **Requirement:** R-DEVOPS-01
- **Steps:** run the platform for 60 s under nominal load, GET `/metrics` from both backend and intelligence.
- **Asserts:** each of the names defined in `shared.metrics` is present (`fuelops_http_requests_total`,
  `fuelops_sim_requests_total`, `fuelops_planner_solve_seconds`, `fuelops_sse_connected`, `fuelops_forecast_mape`,
  ...). No metrics from other services leak into the wrong service's endpoint.

## TC-E-038 — SSE `simulation.tick` events never trigger writes
- **Requirement:** CLAUDE.md rule 4 (REST is the source of truth)
- **Steps:** inject 50 ticks of SSE traffic with no allocations. Capture the backend's outbound HTTP traffic.
- **Asserts:** zero POSTs to `/v1/allocations` or `/v1/allocations/*/cancel`.

## TC-E-039 — Operator approval of a plan creates allocations in order
- **Requirement:** R-PLAT-03
- **Steps:** generate a plan with three lines; the operator approves them one by one.
- **Asserts:** `GET /v1/allocations` shows the three allocations in the order they were approved, with
  matching `idempotency_key = fo-{run_id}-{plan_id}-{line}-{part}`.

## TC-E-040 — Decision audit log captures plan → approval → submission → completion
- **Requirement:** R-INT-X5
- **Steps:** generate a plan, approve it, step 5 ticks, then `GET /admin/audit?limit=100`.
- **Asserts:** audit entries include `decision.plan_generated`, `decision.plan_approved`, `allocation.created`,
  `allocation.arrived` (when applicable). Each entry has `sim_time`, `tick`, and `metadata_json` with the
  `idempotency_key` and quantities.
