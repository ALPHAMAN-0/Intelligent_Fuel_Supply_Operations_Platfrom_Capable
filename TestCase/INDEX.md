# INDEX — All Test Cases

Every test case in this folder, with its target module, requirement id, and short description.
Source-of-truth traceability back to `plan.json` requirement ids.

## Legend

- **Domain (D-)** — pure unit tests on shared code (no I/O).
- **Intelligence (I-)** — forecast, risk, planner, what-if.
- **Sim/SSE (S-)** — simulator client, SSE listener, breaker, error parsing.
- **E2E (E-)** — requires a running simulator / full stack.

---

## 01 — Domain (`01_unit_domain.md`) · 30 cases

| ID | Title | Target | Requirement |
|---|---|---|---|
| TC-D-001 | Spec prior matches the integration guide hour formula | `spec.prior_per_tick` | R-INT-P1 |
| TC-D-002 | Spec prior scales linearly with region factor and tick length | `spec.prior_per_tick` | R-INT-P1 |
| TC-D-003 | Noise floor matches the per-profile table | `spec.noise` | R-INT-P1 |
| TC-D-004 | `daily_demand` falls back to the average for unknown profiles | `spec.daily_demand` | R-INT-P1 |
| TC-D-005 | Pydantic models silently ignore unknown fields, keep unknown statuses | `sim_models.*` | R-RES-04 |
| TC-D-006 | `AllocationRequest` enforces positive quantity and key length | `sim_models.AllocationRequest` | R-PLAT-03 |
| TC-D-007 | `WorldState.synced_at` defaults to now and round-trips through JSON | `shared.models.WorldState` | R-PLAT-04 |
| TC-D-008 | `sim_time_at` / `hour_at` walk the simulator clock correctly | `world.sim_time_at`, `world.hour_at` | R-INT-P5 |
| TC-D-009 | Tick/time conversions are inverse and never zero | `world.ticks_per_hour` etc. | R-INT-P5 |
| TC-D-010 | `WorldIndex` builds correct route tables, `is_cross_region` is accurate | `world.WorldIndex` | R-INT-P4 |
| TC-D-011 | `demand_multiplier_at` applies spikes at the right tick | `world.demand_multiplier_at` | R-INT-D1, R-INT-D4 |
| TC-D-012 | `route_available_at` returns False inside a disruption window | `world.route_available_at` | R-RES-01 |
| TC-D-013 | `station_open_at` mirrors route behaviour for outages | `world.station_open_at` | R-INT-D1 |
| TC-D-014 | `route_usable` requires both departure and arrival ticks clear | `world.route_usable` | R-INT-D3 |
| TC-D-015 | `arrival_tick` honours `expected_arrival_tick` when present | `world.arrival_tick` | R-INT-X2 |
| TC-D-016 | `in_transit_arrivals` aggregates active allocations | `world.in_transit_arrivals` | R-PLAT-04 |
| TC-D-017 | `committed_dispatch` only counts this-tick allocations | `world.committed_dispatch` | R-INT-D2 |
| TC-D-018 | `supply_etas` honours `shipment_delay` and `supply_shortfall` | `world.supply_etas` | R-INT-D2 |
| TC-D-019 | `prior_forecast` returns the spec prior × multiplier for every series | `world.prior_forecast` | R-INT-P1 |
| TC-D-020 | `project_series` matches a hand-rolled spreadsheet | `world.project_series` | R-INT-P3, R-INT-X1 |
| TC-D-021 | `p_stockout` is monotone and bounded in [0, 1] | `world.p_stockout` | R-INT-P2 |
| TC-D-022 | `relative_arrivals` strips past ticks and re-bases to t0 | `world.relative_arrivals` | R-PLAT-03 |
| TC-D-023 | Event hit filters follow the empty-list-means-all convention | `world.event_hits_*` | R-INT-D1 |
| TC-D-024 | `factories.make_world` matches the §8 baseline | `tests/factories.py` | R-PLAT-04 |
| TC-D-025 | `synth_history` generates the expected number of rows | `tests/factories.synth_history` | R-PLAT-04, R-INT-P1 |
| TC-D-026 | `add_event` / `add_allocation` produce deterministic ids | `tests/factories.py` | R-PLAT-04 |
| TC-D-027 | `LatencyWindow.snapshot` returns valid percentiles | `shared.metrics.LatencyWindow` | R-DEVOPS-01 |
| TC-D-028 | `Settings.load_settings` reads `.env` before the environment | `shared.config.load_settings` | R-DEVOPS-02 |
| TC-D-029 | `configure_logging` switches to JSON and silences noisy libraries | `shared.logging.configure_logging` | R-DEVOPS-01 |
| TC-D-030 | Domain numeric fields reject negative and NaN values | `sim_models.*` | R-RES-04 |

## 02 — Intelligence (`02_unit_intelligence.md`) · 34 cases

| ID | Title | Target | Requirement |
|---|---|---|---|
| TC-I-001 | `prior_forecast` matches the structural model on empty history | `forecast.StructuralForecaster.fit` | R-INT-P1 |
| TC-I-002 | Structural model learns hour-of-day rates from history | `forecast.StructuralForecaster.fit` | R-INT-P1 |
| TC-I-003 | Forecast noise tracks `spec.noise` when observations are scarce | `forecast.StructuralForecaster.fit` | R-INT-P1 |
| TC-I-004 | Forecast respects live `demand_multiplier` for spikes | `forecast.FittedModel.forecast` | R-INT-P1, R-INT-D1 |
| TC-I-005 | `SeriesForecast` round-trips through pydantic | `models.SeriesForecast` | R-INT-P1 |
| TC-I-006 | `series_confidence` drops when state is stale or observations missing | `risk.series_confidence` | R-INT-X5 |
| TC-I-007 | `score_risks` returns one `RiskScore` per (station, fuel) | `risk.score_risks` | R-INT-P2 |
| TC-I-008 | `p_stockout` is 1.0 when station is closed | `risk.score_risks` | R-INT-P2 |
| TC-I-009 | Single-route / no-route-available annotations appear in notes | `risk.score_risks` | R-INT-X1 |
| TC-I-010 | `in_transit` and `inventory_position` use only ACTIVE allocations | `risk.score_risks` | R-PLAT-04 |
| TC-I-011 | `series_unmet` and `_snapshot` reflect the safety-z buffer | `planner._snapshot` | R-INT-P3, R-PLAT-03 |
| TC-I-012 | Planner returns OPTIMAL and at least one line on a clear baseline | `planner.plan` | R-PLAT-03, R-INT-X1 |
| TC-I-013 | Planner produces no lines when inventory already covers the horizon | `planner.plan` | R-PLAT-03 |
| TC-I-014 | `split_parts` quantises to 100 L, honours `max_shipment` | `planner.split_parts` | R-INT-X2 |
| TC-I-015 | Planner respects dispatch capacity at the source depot | `planner.plan` | R-INT-D2 |
| TC-I-016 | Cross-region reroute is annotated as a signal | `planner._signals` | R-INT-X1 |
| TC-I-017 | Single-route station generates a `single-route station` signal | `planner._signals` | R-INT-X1 |
| TC-I-018 | Live demand-spike or station-outage events surface as signals | `planner._signals` | R-INT-X1, R-INT-D1 |
| TC-I-019 | Each plan line includes a "Skip this shipment" alternative | `planner.plan` | R-INT-X1 |
| TC-I-020 | An alternative route is offered when one exists | `planner.plan` | R-INT-X1 |
| TC-I-021 | Capacity binding constraint appears when a shipment fills the tank | `planner.plan` | R-PLAT-03 |
| TC-I-022 | Excluded routes (status != AVAILABLE) are not used | `planner.plan` | R-RES-01 |
| TC-I-023 | In-transit arrivals reduce unmet liters | `planner.plan` | R-PLAT-03 |
| TC-I-024 | Terminal cover constraint forces end-of-horizon inventory | `planner._build` | R-PLAT-03 |
| TC-I-025 | `evaluate` computes before/after projections and improves them | `evaluate.evaluate` | R-INT-X3 |
| TC-I-026 | `evaluate` rejects an unknown route with `ValueError` | `evaluate.evaluate` | R-INT-X3 |
| TC-I-027 | `/forecast` returns one `SeriesForecast` per series | `intelligence.main.create_app` | R-INT-P1 |
| TC-I-028 | `/cycle` returns `CycleResponse` with forecasts, risks, ETAs, plan, model | `intelligence.main.create_app` | R-PLAT-03, R-INT-X1 |
| TC-I-029 | `/evaluate` rejects unknown routes with HTTP 422 | `intelligence.main.create_app` | R-INT-X3 |
| TC-I-030 | `/health` and `/model/info` expose the right metadata | `intelligence.main.create_app` | R-DEVOPS-02 |
| TC-I-031 | `noise_floor` of an empty fitted model is 0 | `forecast.FittedModel.noise_floor` | R-INT-P1 |
| TC-I-032 | `model.info()` reports `fitted_series` and `observations` correctly | `forecast.FittedModel.info` | R-INT-P1 |
| TC-I-033 | Heuristic fallback reaches the same risk score as the planner | heuristic fallback | R-RES-05 |
| TC-I-034 | Plan lines compute `arrival_tick = tick + 1 + transit_ticks` | `planner.plan` | R-INT-X2 |

## 03 — Integration (`03_integration_sim.md`) · 32 cases

| ID | Title | Target | Requirement |
|---|---|---|---|
| TC-S-001 | `get_health` parses the `/v1/health` envelope | `sim_client.SimClient.get_health` | R-PLAT-04 |
| TC-S-002 | Successful calls mark the endpoint as not stale | `sim_client._call` | R-RES-04 |
| TC-S-003 | Stale header sets `stale_endpoints` until cleared | `sim_client._call` | R-PLAT-04, R-RES-04 |
| TC-S-004 | Retry with full-jitter backoff on 503 FAULT_INJECTED | `sim_client._call` | R-RES-01 |
| TC-S-005 | Domain 404 / 409 errors are NOT retried | `sim_client._call` | R-RES-04 |
| TC-S-006 | Idempotency-key mismatch is detected as non-retryable 409 | `sim_client._call`, `parse_error` | R-PLAT-03 |
| TC-S-007 | POST /v1/allocations treats 200 and 201 both as success | `sim_client.create_allocation` | R-PLAT-03 |
| TC-S-008 | Validation error raises INVALID_RESPONSE | `sim_client._get` | R-RES-04 |
| TC-S-009 | Non-JSON body raises INVALID_RESPONSE | `sim_client._call` | R-RES-04 |
| TC-S-010 | Transport errors retry up to `retries` times | `sim_client._call` | R-RES-01 |
| TC-S-011 | Read breaker opens after N consecutive retryable failures | `sim_client`, `breaker.CircuitBreaker` | R-RES-01 |
| TC-S-012 | Write breaker is independent from the read breaker | `sim_client` | R-RES-01 |
| TC-S-013 | Breaker transitions to HALF_OPEN after `reset_timeout_s` and probes | `breaker.CircuitBreaker` | R-RES-01 |
| TC-S-014 | Successful 4xx/404 does NOT count against the breaker | `sim_client._call` | R-RES-04 |
| TC-S-015 | `/admin/*` bypasses the breakers and bypasses faults | `sim_client.admin_post` | R-PLAT-02 |
| TC-S-016 | `/v1/health` is fault-free and breaker-free | `sim_client.get_health` | R-RES-04 |
| TC-S-017 | `get_demand_history` clamps `limit` and forwards `station_id` | `sim_client.get_demand_history` | R-PLAT-04 |
| TC-S-018 | `/admin/events` body forwarded verbatim and rejects malformed shapes | `sim_client.admin_inject_event` | R-PLAT-04 |
| TC-S-019 | `admin_audit` honours the `limit` clamp | `sim_client.admin_audit` | R-PLAT-04 |
| TC-S-020 | `parse_error` distinguishes the two error envelopes | `sim_errors.parse_error` | (rule 6) |
| TC-S-021 | `SimError.retryable` follows the documented rule | `sim_errors.SimError.retryable` | (rule 4) |
| TC-S-022 | `parse_sse_lines` returns `(name, data)` correctly | `sse_listener.parse_sse_lines` | R-PLAT-04 |
| TC-S-023 | `SSEListener` reconnects with backoff and stops cleanly | `sse_listener.SSEListener` | R-RES-01 |
| TC-S-024 | `SSEListener` raises on long silence while RUNNING | `sse_listener.SSEListener._consume` | (guide §6) |
| TC-S-025 | `SSEListener` does not raise on silence when paused | `sse_listener.SSEListener._consume` | (guide §6) |
| TC-S-026 | `SSEListener` dispatches stream.connected and stream.disconnected | `sse_listener.SSEListener` | R-RES-04 |
| TC-S-027 | `SimClient` exposes `last_ok_at` and `last_error` | `sim_client.SimClient` | R-RES-04 |
| TC-S-028 | `_on_invalid` callback is invoked on validation failure | `sim_client._invalid` | R-RES-04 |
| TC-S-029 | `SimClient.aclose` closes the underlying httpx transport | `sim_client.SimClient.aclose` | R-DEVOPS-02 |
| TC-S-030 | `MetricsMiddleware` skips `/metrics` and `/api/stream` | `shared.metrics.MetricsMiddleware` | R-DEVOPS-01 |
| TC-S-031 | `LatencyWindow` is thread-safe under concurrent updates | `shared.metrics.LatencyWindow` | R-DEVOPS-01 |
| TC-S-032 | `SimClient._call` increments `SIM_REQUESTS` for every outcome | `sim_client._call` | R-DEVOPS-01 |

## 04 — E2E & Chaos (`04_e2e_scenarios.md`) · 40 cases

| ID | Title | Target | Requirement |
|---|---|---|---|
| TC-E-001 | Baseline: stockouts appear on day 1 without action | scenario runner | R-INT-P1 |
| TC-E-002 | Planner prevents stockouts on a 7-day baseline | full stack | R-PLAT-03, R-INT-X1 |
| TC-E-003 | Idempotent retry of an allocation returns the same id | `actuator.create_allocation` | (rule 7) |
| TC-E-004 | Demand-spike crisis triggers a plan within one tick | orchestrator + risk | R-INT-D1, R-CRISIS-01 |
| TC-E-005 | Route disruption forces a cross-region reroute | planner reroute | R-RES-01, R-INT-X1 |
| TC-E-006 | Station outage does not consume fuel | risk + planner | R-INT-D1 |
| TC-E-007 | Depot constraint reduces dispatch capacity and biases plans | planner + `committed_dispatch` | R-INT-D2 |
| TC-E-008 | Shipment delay pushes the supply ETA | `supply_etas` | R-INT-D2 |
| TC-E-009 | Supply shortfall halves a future arrival | `supply_etas` | R-INT-D2 |
| TC-E-010 | `latency` fault: simulator is slow but correct | integration tolerance | R-RES-02 |
| TC-E-011 | `unavailable` fault: client retries and eventually succeeds | resilience | R-RES-02, R-RES-04 |
| TC-E-012 | `error_rate` fault: every Nth call fails | resilience | R-RES-02 |
| TC-E-013 | `stale_data` fault: backend marks state stale and blocks auto-approve | stale handling | R-PLAT-04, (rule 8) |
| TC-E-014 | `stream_disconnect` fault: SSE listener reconnects with polling fallback | SSE | R-RES-02 |
| TC-E-015 | Combined crisis: spike + disruption + supply shortfall | end-to-end | R-CRISIS-01 |
| TC-E-016 | Determinism: same scenario + seed + actions = same plan sequence | determinism | (rule 9) |
| TC-E-017 | `cancel_allocation` succeeds only for PENDING allocations | actuator | R-PLAT-03 |
| TC-E-018 | `idempotency_key` mismatch on retry returns 409 | actuator | (rule 7) |
| TC-E-019 | `DESTINATION_CAPACITY_EXCEEDED` blocks an over-sized allocation | simulator contract | R-PLAT-03 |
| TC-E-020 | `DISPATCH_CAPACITY_EXCEEDED` accounts for in-flight + pending this tick | simulator contract | R-PLAT-03 |
| TC-E-021 | `ROUTE_DISRUPTED` blocks new allocations to that route | simulator contract | R-PLAT-03 |
| TC-E-022 | `INSUFFICIENT_INVENTORY` blocks an over-sized allocation at the depot | simulator contract | R-PLAT-03 |
| TC-E-023 | `OPERATOR_TOKEN` gates `/api/allocations`, `/api/chaos/*`, `/admin/*` | operator API | R-DEVOPS-02 |
| TC-E-024 | Chaos drawer proxies `/admin/faults` only when `CHAOS_ENABLED=true` | chaos drawer | R-RES-02 |
| TC-E-025 | LLM explanation endpoint times out gracefully and falls back to template | explain | R-RES-04 |
| TC-E-026 | Plan TTL expires and a new plan is generated | orchestrator | R-PLAT-03 |
| TC-E-027 | `/admin/reset` wipes allocations and reverts state | admin | R-PLAT-04, (rule 3) |
| TC-E-028 | Auto-approve gate blocks shipments that exceed `AUTO_APPROVE_MAX_LITERS` | approval | R-PLAT-03 |
| TC-E-029 | Auto-approve gate blocks low-confidence plans | approval | R-PLAT-03 |
| TC-E-030 | Heuristic fallback engages when the intelligence service is down | fallback | R-RES-05, (rule 12) |
| TC-E-031 | Allocation retry storm collapses after the write breaker opens | breaker | R-RES-01 |
| TC-E-032 | History mirroring caps at the 2 000-row window | state sync | R-PLAT-04, (rule 5) |
| TC-E-033 | Demand forecast MAE matches the spec noise floor on baseline data | benchmark | R-INT-P1, R-DEVOPS-01 |
| TC-E-034 | Load test under 100 RPS keeps p95 latency below 250 ms | loadtest | R-DEVOPS-01, R-RES-04 |
| TC-E-035 | `docker compose up` brings the entire stack to healthy in under 60 s | ops | R-DEVOPS-01 |
| TC-E-036 | Rolling image rollback restores a previous release | ops | R-DEVOPS-01 |
| TC-E-037 | Metrics endpoint exposes all expected `fuelops_*` series | metrics | R-DEVOPS-01 |
| TC-E-038 | SSE `simulation.tick` events never trigger writes | REST discipline | (rule 4) |
| TC-E-039 | Operator approval of a plan creates allocations in order | approval | R-PLAT-03 |
| TC-E-040 | Decision audit log captures plan → approval → submission → completion | audit | R-INT-X5 |

---

## Coverage summary

- **Total cases:** 136
- **Pure unit (no I/O):** 64 (Domain + Intelligence)
- **Integration (respx / FastAPI TestClient):** 32
- **End-to-end (running simulator):** 40

## Requirement coverage map

| Requirement id | Test cases |
|---|---|
| R-PLAT-02 | TC-S-015 |
| R-PLAT-03 | TC-D-006, TC-S-006, TC-S-007, TC-D-016, TC-I-011..014, TC-I-021, TC-I-023, TC-I-024, TC-I-028, TC-E-017..022, TC-E-026, TC-E-028, TC-E-029, TC-E-039 |
| R-PLAT-04 | TC-D-007, TC-D-010, TC-D-016, TC-D-024..026, TC-I-010, TC-S-001, TC-S-003, TC-S-017..019, TC-S-022, TC-E-013, TC-E-027, TC-E-032 |
| R-INT-P1 | TC-D-001..004, TC-D-019, TC-D-025, TC-I-001..005, TC-I-027, TC-I-031, TC-I-032, TC-E-001, TC-E-033 |
| R-INT-P2 | TC-D-021, TC-I-007, TC-I-008 |
| R-INT-P3 | TC-D-020, TC-I-011 |
| R-INT-P4 | TC-D-010 |
| R-INT-P5 | TC-D-008, TC-D-009 |
| R-INT-X1 | TC-D-020, TC-I-009, TC-I-012, TC-I-016..020, TC-I-028, TC-E-002, TC-E-005 |
| R-INT-X2 | TC-D-015, TC-I-014, TC-I-034 |
| R-INT-X3 | TC-I-025, TC-I-026, TC-I-029 |
| R-INT-X5 | TC-I-006, TC-E-040 |
| R-INT-D1 | TC-D-011, TC-D-013, TC-D-023, TC-I-004, TC-I-018, TC-E-004, TC-E-006 |
| R-INT-D2 | TC-D-017, TC-D-018, TC-I-015, TC-E-007..009 |
| R-INT-D3 | TC-D-014 |
| R-INT-D4 | TC-D-011 |
| R-RES-01 | TC-D-012, TC-I-022, TC-S-004, TC-S-010..013, TC-S-023, TC-E-005, TC-E-031 |
| R-RES-02 | TC-E-010..014, TC-E-024 |
| R-RES-04 | TC-D-005, TC-D-030, TC-S-002, TC-S-003, TC-S-005, TC-S-008, TC-S-009, TC-S-014, TC-S-016, TC-S-026..028, TC-E-025, TC-E-034 |
| R-RES-05 | TC-I-033, TC-E-030 |
| R-DEVOPS-01 | TC-D-027, TC-D-029, TC-S-030..032, TC-E-033..037 |
| R-DEVOPS-02 | TC-D-028, TC-I-030, TC-S-029, TC-E-023 |
| R-CRISIS-01 | TC-E-004, TC-E-015 |
| CLAUDE.md rule 3 | TC-S-015, TC-E-027 |
| CLAUDE.md rule 4 | TC-S-005, TC-S-021, TC-E-038 |
| CLAUDE.md rule 5 | TC-S-017, TC-E-032 |
| CLAUDE.md rule 6 | TC-S-007, TC-S-020 |
| CLAUDE.md rule 7 | TC-S-006, TC-E-003, TC-E-018 |
| CLAUDE.md rule 8 | TC-E-013 |
| CLAUDE.md rule 9 | TC-E-016 |
| CLAUDE.md rule 12 | TC-E-030 |
| Guide §6 (SSE) | TC-S-022..026 |
