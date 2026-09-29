# 01 — Unit Tests: Domain Models, Spec & World Helpers

These tests cover the **pure** code that has no I/O, no async, no network. They run in milliseconds and form the
fast feedback loop. Everything else (planner, simulator client, SSE) builds on these guarantees.

Targets: `server/fuelops/shared/spec.py`, `server/fuelops/shared/world.py`, `server/fuelops/shared/models.py`,
`server/fuelops/shared/sim_models.py`, `server/tests/factories.py`.

---

## TC-D-001 — Spec prior for an hour bucket matches the integration guide
- **Requirement:** R-INT-P1 (spec prior is the always-available forecast)
- **Target:** `spec.prior_per_tick`
- **Pre-reqs:** none
- **Steps:**
  1. Call `spec.prior_per_tick("urban_high", "PETROL", 8, 60, 1.0)` (peak urban hour, 60-min tick, neutral region).
  2. Call `spec.prior_per_tick("urban_high", "PETROL", 3, 60, 1.0)` (off-peak hour).
- **Asserts:**
  - The peak-hour value is exactly `PROFILE_DAILY["urban_high"]["PETROL"] / 24 * 1.45` (the urban busy factor).
  - The off-peak value is exactly `PROFILE_DAILY["urban_high"]["PETROL"] / 24 * 0.70`.
- **Notes:** Locks down `hour_factor` and `_busy` boundaries. If this drifts, every forecast and the planner will
  drift too.

## TC-D-002 — Spec prior scales linearly with region factor and tick length
- **Requirement:** R-INT-P1
- **Target:** `spec.prior_per_tick`
- **Pre-reqs:** none
- **Steps:**
  1. Compare `prior_per_tick(profile, fuel, h, 60, 1.0)` with `prior_per_tick(profile, fuel, h, 60, 1.08)`.
  2. Compare `prior_per_tick(profile, fuel, h, 60, 1.0)` with `prior_per_tick(profile, fuel, h, 15, 1.0)`.
- **Asserts:**
  - Region-scaled value is exactly 1.08× the neutral value.
  - 15-minute tick value is exactly ¼ of the 60-minute value.

## TC-D-003 — Noise floor matches the per-profile table
- **Requirement:** R-INT-P1
- **Target:** `spec.noise`
- **Pre-reqs:** none
- **Steps:** call `spec.noise(profile)` for each profile and for an unknown one.
- **Asserts:** urban_high=0.10, industrial=0.08, highway=0.12, regional=0.10, unknown=DEFAULT_NOISE (0.10).

## TC-D-004 — `daily_demand` falls back to the average across profiles for unknown profiles
- **Requirement:** R-INT-P1
- **Target:** `spec.daily_demand`
- **Pre-reqs:** none
- **Steps:** call `daily_demand("marina", "DIESEL")`.
- **Asserts:** equals `mean(PROFILE_DAILY[p]["DIESEL"] for p in PROFILE_DAILY)`.

## TC-D-005 — Pydantic models reject unknown status and silently ignore unknown fields
- **Requirement:** R-RES-04 (parsing must not crash on simulator surprises)
- **Target:** `sim_models.SimModel` and concrete subclasses
- **Pre-reqs:** none
- **Steps:**
  1. Build `Station(...)` with an `extra="allow"` payload that adds a brand-new key, e.g. `"weather": "rain"`.
  2. Build `Station(...)` with `status="WEIRD_THING"`.
  3. Build `Station(...)` with `demand_multiplier=-0.1` (negative).
- **Asserts:**
  1. The unknown key is dropped silently (`extra="ignore"`).
  2. The unknown status is preserved verbatim — no enum coercion.
  3. `ValidationError` is raised with `demand_multiplier` mentioned.

## TC-D-006 — `AllocationRequest` enforces positive quantity and key length
- **Requirement:** R-PLAT-03 (POST /v1/allocations contract)
- **Target:** `sim_models.AllocationRequest`
- **Pre-reqs:** none
- **Steps:** build with `quantity=0`, `quantity=-5`, `idempotency_key=""`, `idempotency_key="x" * 151`.
- **Asserts:** each case raises `ValidationError`; a 150-char key and `quantity=0.01` succeed.

## TC-D-007 — `WorldState.synced_at` defaults to now and is serialisable
- **Requirement:** R-PLAT-04 (state sync)
- **Target:** `shared.models.WorldState`
- **Pre-reqs:** none
- **Steps:** build with `synced_at=datetime.now(UTC)`, `.model_dump_json()`, parse it back.
- **Asserts:** round-trip is equal; `stale=False` and `degraded=[]` by default.

## TC-D-008 — `sim_time_at` and `hour_at` walk the simulator clock correctly
- **Requirement:** R-INT-P5 (hour of day drives the forecast)
- **Target:** `world.sim_time_at`, `world.hour_at`
- **Pre-reqs:** WorldState with `tick=0, sim_time=2026-01-01T00:00, tick_minutes=15`
- **Steps:** call `sim_time_at(state, 4)` (1 h later) and `hour_at(state, 4*96)` (96 ticks = 24 h).
- **Asserts:** returns `2026-01-01T01:00` and `0` (next-day midnight wraps to 0).

## TC-D-009 — `ticks_per_hour` / `ticks_to_hours` / `hours_to_ticks` are inverse and never zero
- **Requirement:** R-INT-P5
- **Target:** `world.ticks_per_hour`, `world.ticks_to_hours`, `world.hours_to_ticks`
- **Pre-reqs:** tick_minutes=15, then tick_minutes=60
- **Steps:** round-trip `1h → ticks → h`, `0.25h → ticks → h`.
- **Asserts:**
  - `hours_to_ticks(state, 0)` clamps to 1.
  - 1 h ≈ 4 ticks (15-min) and 1 tick (60-min).
  - 0.25 h ≈ 1 tick (15-min).

## TC-D-010 — `WorldIndex` builds correct route tables and `is_cross_region` is accurate
- **Requirement:** R-INT-P4 (planner uses routes and region info)
- **Target:** `world.WorldIndex`
- **Pre-reqs:** `factories.make_world()` (Gazipur↔Dhaka stations, Patiya↔Chattogram stations)
- **Steps:**
  1. Inspect `routes_to["station-mirpur"]`, `routes_to["station-coxsbazar"]`, `routes_from["depot-gazipur"]`.
  2. Call `idx.is_cross_region(route)` for each route.
- **Asserts:**
  - Mirpur receives from `route-gazipur-mirpur` and `route-patiya-mirpur` (the cross-region one).
  - Cox's Bazar receives only from `route-patiya-coxsbazar`.
  - `route-patiya-mirpur` and `route-gazipur-karnaphuli` return `is_cross_region == True`; the four same-region
    routes return `False`.

## TC-D-011 — `demand_multiplier_at` applies SCHEDULED spikes at the right tick and clears ACTIVE spikes after end
- **Requirement:** R-INT-D1, R-INT-D4
- **Target:** `world.demand_multiplier_at`
- **Pre-reqs:** station with `demand_multiplier=1.0`; one `demand_spike SCHEDULED 10..14 multiplier=2.0 region_ids=[region]` and one `demand_spike ACTIVE 20..24 multiplier=1.5`.
- **Steps:**
  - Call `demand_multiplier_at(state, station, t)` for t = 5, 12, 15, 22, 25.
- **Asserts:**
  - t=5: 1.0 (before scheduled).
  - t=12: 2.0 (inside scheduled window).
  - t=15: 1.0 (scheduled ended at tick 14, so t=15 is back to base).
  - t=22: 1.5 (active spike applied — code only undoes an active spike at *or after* its `end_tick`; verify which
    branch is taken at t=22 vs t=24).
  - t=25: 1.0 (active cleared).

## TC-D-012 — `route_available_at` returns False when SCHEDULED disruption is inside its window
- **Requirement:** R-RES-01
- **Target:** `world.route_available_at`
- **Pre-reqs:** route status=AVAILABLE, one `route_disruption SCHEDULED 30..34 route_ids=[r.id]`.
- **Steps:** evaluate at t=20, 32, 40; also with route status=DISRUPTED and no events.
- **Asserts:**
  - t=20: True.
  - t=32: False.
  - t=40: True.
  - DISRUPTED route with no events: False.

## TC-D-013 — `station_open_at` mirrors route behaviour but for `station_outage`
- **Requirement:** R-INT-D1
- **Target:** `world.station_open_at`
- **Pre-reqs:** station status=OPEN, outage SCHEDULED 50..54 station_ids=[sid].
- **Steps:** evaluate at t=40, 52, 60; OUTAGE station with no events.
- **Asserts:** True, False, True, False respectively.

## TC-D-014 — `route_usable` requires both departure and arrival ticks to be clear
- **Requirement:** R-INT-D3
- **Target:** `world.route_usable`
- **Pre-reqs:** route + depot + station; outage starting at the create_tick+1 boundary.
- **Steps:**
  1. `create_tick = 10`, outage SCHEDULED 11..15.
  2. `create_tick = 10`, outage SCHEDULED 10..14.
  3. `create_tick = 10`, route SCHEDULED disruption 11..15.
  4. `create_tick = 10`, station outage SCHEDULED 10..14.
  5. Clean baseline.
- **Asserts:** (1) True, (2) False, (3) False, (4) False, (5) True.

## TC-D-015 — `arrival_tick` uses `expected_arrival_tick` when present, else `created_tick + 1 + transit_ticks`
- **Requirement:** R-INT-X2
- **Target:** `world.arrival_tick`
- **Pre-reqs:** route with `transit_ticks=3`.
- **Steps:** created_tick=10; call with `expected=None`, `expected=999`, `expected=10`.
- **Asserts:** 14, 999, 10.

## TC-D-016 — `in_transit_arrivals` aggregates active allocations by (station, fuel, arrival_tick)
- **Requirement:** R-PLAT-04 (planner reads in-transit arrivals)
- **Target:** `world.in_transit_arrivals`
- **Pre-reqs:** two PENDING allocations to (S1, DIESEL) arriving tick 8 (qty 1500 each), one IN_TRANSIT to (S1, DIESEL) arriving tick 8 (qty 2000), one ARRIVED (ignored), one PENDING to (S2, PETROL) arriving tick 9.
- **Steps:** call `in_transit_arrivals(state)`.
- **Asserts:** `{(S1, DIESEL): {8: 5000.0}, (S2, PETROL): {9: <qty>}}`.

## TC-D-017 — `committed_dispatch` only counts allocations created on the *current* tick
- **Requirement:** R-INT-D2 (planner must subtract already-committed dispatch)
- **Target:** `world.committed_dispatch`
- **Pre-reqs:** two allocations created_tick=current-1 (PENDING), three created_tick=current (IN_TRANSIT).
- **Steps:** call `committed_dispatch(state)`.
- **Asserts:** only the three this-tick allocations contribute to the per-depot sum.

## TC-D-018 — `supply_etas` honours `shipment_delay` and `supply_shortfall` events
- **Requirement:** R-INT-D2
- **Target:** `world.supply_etas`
- **Pre-reqs:** one `supply_arrivals` planned tick 16; one `shipment_delay SCHEDULED 10..12 depot_ids=[depot] delay_ticks=4`; one `supply_shortfall SCHEDULED 8..20 depot_ids=[depot] factor=0.6 fuel_types=[fuel]`.
- **Steps:** call `supply_etas(state, first_seen_quantity={sa.id: original_qty})`.
- **Asserts:**
  - The shipment's `eta_tick` is 16+4=20 (delay applied); `delayed=True` if planned_tick < eta_tick.
  - The supply arrival's `expected_quantity` is multiplied by 0.6; `shortfall=True`.
  - The "first seen" quantity is the original, so the shortfall flag is detectable.

## TC-D-019 — `prior_forecast` returns the spec prior × multiplier for every series
- **Requirement:** R-INT-P1 (fallback forecast)
- **Target:** `world.prior_forecast`
- **Pre-reqs:** `make_world()`, tick_minutes=60, station `demand_multiplier=1.2`, no events.
- **Steps:** call `prior_forecast(state, horizon=24)` and pick (station-mirpur, PETROL).
- **Asserts:**
  - `mu[h]` = `spec.prior_per_tick("urban_high", "PETROL", h%24, 60, 1.0) * 1.2` for h = 1..24.
  - `sigma[h]` = `spec.noise("urban_high") * mu[h]`.
  - All 12 series (4 stations × 3 fuels) are present.

## TC-D-020 — `project_series` matches a hand-rolled spreadsheet on a 3-tick scenario
- **Requirement:** R-INT-P3, R-INT-X1
- **Target:** `world.project_series`
- **Pre-reqs:** `on_hand=1000, capacity=2000, mu=[300, 300, 300], arrivals={2: 500}, open_at=None`.
- **Steps:** call `project_series(...)`.
- **Asserts:**
  - Inventory series: `[1000, 700, 900, 600]`.
  - `unmet_total=0`, `first_short=None`.
- **Cases to also cover:** demand > on-hand → `first_short=1`, `unmet_total=expected`. An outage tick counts all
  demand as unmet.

## TC-D-021 — `p_stockout` is monotone in horizon and bounded in [0, 1]
- **Requirement:** R-INT-P2 (stockout probability)
- **Target:** `world.p_stockout`
- **Pre-reqs:** deterministic mu, sigma.
- **Steps:**
  1. `p_stockout(on_hand=0, mu=[100]*10, sigma=[10]*10, arrivals={}, within=1)` vs `within=10`.
  2. `p_stockout(on_hand=10**9, mu=[100]*10, sigma=[10]*10, arrivals={}, within=10)`.
  3. `p_stockout(on_hand=0, mu=[0]*5, sigma=[0]*5, arrivals={}, within=3)`.
- **Asserts:**
  1. `p(within=10) >= p(within=1)`.
  2. `p == 0`.
  3. `p == 0` (no demand) — guarding the `sd <= 1e-9` branch.

## TC-D-022 — `relative_arrivals` strips past ticks and re-bases to t0
- **Requirement:** R-PLAT-03
- **Target:** `world.relative_arrivals`
- **Pre-reqs:** `t0=10`, arrivals `{5: 100, 11: 200, 12: 300}`.
- **Steps:** call `relative_arrivals(abs_arrivals, 10)`.
- **Asserts:** returns `{1: 200, 2: 300}`.

## TC-D-023 — `event_hits_station` and `event_hits` follow the empty-list-means-all convention
- **Requirement:** R-INT-D1
- **Target:** `world.event_hits_station`, `world.event_hits`
- **Pre-reqs:** two stations S1 (region R-A), S2 (region R-B).
- **Steps:** evaluate event with `station_ids=[]`, `region_ids=[]`, `station_ids=["S9"]`, `region_ids=["R-A"]`.
- **Asserts:** empty lists hit both stations; explicit S9 hits nothing of ours; region R-A hits S1 only.

## TC-D-024 — `factories.make_world` matches the §8 baseline
- **Requirement:** R-PLAT-04
- **Target:** `tests/factories.py`
- **Pre-reqs:** none
- **Steps:** build a world and snapshot the depot/station/route ids, capacities and inventory.
- **Asserts:** 2 depots, 4 stations, 6 routes, all fuel dicts cover the three fuels, dispatch capacities are
  12 000 and 11 000 L/tick as documented.

## TC-D-025 — `factories.synth_history` generates the expected number of rows and respects `end_tick`
- **Requirement:** R-PLAT-04, R-INT-P1
- **Target:** `tests/factories.synth_history`
- **Pre-reqs:** `make_world(tick=96)` (day 1 at 15-min ticks).
- **Steps:** call `synth_history(state, ticks=96, end_tick=96)` and `end_tick=10`.
- **Asserts:**
  - First call: `96 * 4 stations * 3 fuels = 1152` points, every `sim_time` is `START + i*15min`.
  - Second call: 11 ticks × 12 series = 132 points; `tick` ranges from 0..10.
  - `demand_liters >= 0` everywhere; mean across hour buckets tracks `spec.prior_per_tick` within ±15 %.

## TC-D-026 — `factories.add_event` / `add_allocation` produce deterministic ids and propagate status
- **Requirement:** R-PLAT-04
- **Target:** `tests/factories.py`
- **Pre-reqs:** `make_world()`
- **Steps:** add three events and two allocations, snapshot ids.
- **Asserts:** event ids increment from 1, allocation `expected_arrival_tick = created_tick + 1 + transit_ticks`
  for the matching route.

## TC-D-027 — `LatencyWindow.snapshot` returns valid percentiles for any input
- **Requirement:** R-DEVOPS-01 (metrics correctness)
- **Target:** `shared.metrics.LatencyWindow`
- **Pre-reqs:** none
- **Steps:** push 0 items, 1 item, 200 items (uniform), all-error items.
- **Asserts:**
  - 0 items: `count=0`, all other fields 0.
  - 1 item: p50 = p95 = p99 = item latency (ms).
  - 200 uniform items: p50 close to median of population; error_rate exact.

## TC-D-028 — `Settings.load_settings` reads `.env` before the environment
- **Requirement:** R-DEVOPS-02 (no secrets in code)
- **Target:** `shared.config.load_settings`
- **Pre-reqs:** write a temporary `.env` with `SIM_BASE_URL=http://sim:8000\nLOG_LEVEL=DEBUG`.
- **Steps:** call `load_settings(env={"LOG_LEVEL": "WARNING"})`.
- **Asserts:** `sim_base_url` comes from `.env`, `log_level` comes from the env argument (env wins when both set).

## TC-D-029 — `configure_logging` switches to JSON and silences noisy libraries
- **Requirement:** R-DEVOPS-01
- **Target:** `shared.logging.configure_logging`
- **Pre-reqs:** none
- **Steps:** configure, emit one log line, assert stdout contains valid JSON with `event` key. Set
  `level="WARNING"`, emit a debug line, assert it is not present.
- **Asserts:** JSON parseable, `service` defaults to `"fuelops"`, `httpx`/`httpcore` loggers raised to WARNING.

## TC-D-030 — Domain numeric fields reject negative and NaN values
- **Requirement:** R-RES-04
- **Target:** `sim_models.Station`, `Depot`, `Route`, `SupplyArrival`
- **Pre-reqs:** none
- **Steps:** instantiate each with negative capacity, `float("nan")` inventory, `transit_ticks=-1`.
- **Asserts:** every case raises `ValidationError` with the offending field named in the error.
