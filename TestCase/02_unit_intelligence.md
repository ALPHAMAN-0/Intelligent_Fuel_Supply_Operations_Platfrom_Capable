# 02 — Unit Tests: Intelligence (Forecast, Risk, Planner, What-If)

These tests cover the **decision intelligence** that turns world state into plans and risk scores. They run on
synthetic `WorldState` fixtures built from `tests/factories.py` so no simulator is required.

Targets: `server/fuelops/intelligence/forecast.py`, `risk.py`, `planner.py`, `evaluate.py`.

---

## TC-I-001 — `prior_forecast` matches the structural model on an empty history
- **Requirement:** R-INT-P1
- **Target:** `forecast.StructuralForecaster.fit().forecast()` vs `world.prior_forecast()`
- **Pre-reqs:** `make_world(tick=0, tick_minutes=60)`; no `params.rates`.
- **Steps:**
  1. `model = StructuralForecaster(ForecastParams()).fit(state, history=[])`
  2. `structural = model.forecast(state, horizon=24)`
  3. `prior = world.prior_forecast(state, horizon=24)`
- **Asserts:** `structural[(sid, fuel)][0] == prior[(sid, fuel)][0]` within 1e-9 for every (sid, fuel);
  `sigma` is within 1e-6 (both fall back to `spec.noise`).

## TC-I-002 — Structural model learns hour-of-day rates from synthetic history
- **Requirement:** R-INT-P1
- **Target:** `forecast.StructuralForecaster.fit`
- **Pre-reqs:** `make_world(tick=96)` (one simulated day) and 96 ticks of `synth_history(scale=2.0)`.
- **Steps:** fit the forecaster, inspect `series[(station-mirpur, PETROL)].rates[8]` (peak hour).
- **Asserts:**
  - `rates[8] ≈ 2.0 × baseline` (since `synth_history` doubled the demand before jittering).
  - `n_obs >= 4` for that hour bucket (15-minute ticks → 4 observations per hour in a 96-tick day at 15-min tick).
  - `series.source == "learned"` once `n_obs >= ticks_per_day`.

## TC-I-003 — Forecast noise estimate tracks `spec.noise` when observations are scarce
- **Requirement:** R-INT-P1
- **Target:** `forecast.StructuralForecaster.fit`
- **Pre-reqs:** `make_world(tick=10)`, `history=[]`.
- **Steps:** fit the forecaster, inspect `noise` for an industrial station.
- **Asserts:** `noise ≈ spec.noise("industrial")` (the prior pulls it back).

## TC-I-004 — `forecast` output respects live `demand_multiplier` for SCHEDULED spikes
- **Requirement:** R-INT-P1, R-INT-D1
- **Target:** `forecast.FittedModel.forecast`
- **Pre-reqs:** fit on empty history, station `demand_multiplier=1.0`, one
  `demand_spike SCHEDULED 5..9 multiplier=2.0 station_ids=[sid]` matching the station.
- **Steps:** call `forecast(state, horizon=12)` and read `(sid, fuel)` at h=3..7.
- **Asserts:** values inside the spike window are exactly 2.0× the base; values outside are the base.

## TC-I-005 — `SeriesForecast` round-trips through pydantic and preserves decimal precision
- **Requirement:** R-INT-P1
- **Target:** `models.SeriesForecast`
- **Pre-reqs:** one fitted series.
- **Steps:** serialize to JSON, parse back.
- **Asserts:** `mu`, `sigma` lists equal element-wise to 3-decimal rounding.

## TC-I-006 — `series_confidence` drops when state is stale or many observations are missing
- **Requirement:** R-INT-X5
- **Target:** `risk.series_confidence`
- **Pre-reqs:** `make_world(tick=0)`, ticks_per_day=96.
- **Steps:**
  1. Compute confidence at `n_obs=0`, `n_obs=48`, `n_obs=96`, with `noise=0.10`, `event_nearby=False`,
     `stale=False`.
  2. Set `state.stale=True` (only on the copy).
  3. Set `event_nearby=True`.
- **Asserts:** confidence monotone increasing in `n_obs`; `stale=True` subtracts 0.3; `event_nearby=True`
  subtracts 0.15; the value is clamped to `[0.05, 0.99]`.

## TC-I-007 — `score_risks` returns one `RiskScore` per (station, fuel) and inventories match
- **Requirement:** R-INT-P2
- **Target:** `risk.score_risks`
- **Pre-reqs:** `make_world()`; `forecasts` from `prior_forecast(state, horizon=24)`; `n_obs` empty; `noise={}`.
- **Steps:** call `score_risks(state, forecasts)`.
- **Asserts:**
  - `len(risks) == 4 stations * 3 fuels == 12`.
  - For each risk, `on_hand == station.inventory[fuel]` and `capacity == station.capacity[fuel]`.
  - `demand_next_24h == sum(mu[:horizon_24h])` (computed from `hours_to_ticks(state, 24)`).

## TC-I-008 — `p_stockout` is 1.0 when the station is closed (`status != OPEN`)
- **Requirement:** R-INT-P2
- **Target:** `risk.score_risks`
- **Pre-reqs:** one station with `status="CLOSED"`, the rest OPEN.
- **Steps:** call `score_risks(state, prior_forecast(state, 24))`.
- **Asserts:** every risk for the closed station has `p_stockout = {4h: 1.0, 12h: 1.0, 24h: 1.0}` and a note
  `"station closed: no fuel is served"`.

## TC-I-009 — Single-route and no-route-available annotations appear in `notes`
- **Requirement:** R-INT-X1
- **Target:** `risk.score_risks`
- **Pre-reqs:** station `station-tongi` (single-route), station `station-karnaphuli` with all routes disrupted
  by `route_disruption ACTIVE` events covering their planning window.
- **Steps:** call `score_risks`.
- **Asserts:**
  - Tongi risks contain `"single-route station"`.
  - Karnaphuli risks contain `"no route available"`.

## TC-I-010 — `in_transit` and `inventory_position` use only ACTIVE allocations
- **Requirement:** R-PLAT-04
- **Target:** `risk.score_risks`
- **Pre-reqs:** one PENDING allocation to (S1, DIESEL) qty 1500 arriving tick 3, one ARRIVED (ignored), one
  IN_TRANSIT (counted).
- **Steps:** call `score_risks`.
- **Asserts:** `(S1, DIESEL).in_transit` equals the sum of active quantities, `inventory_position = on_hand + in_transit`.

## TC-I-011 — `series_unmet` and `_snapshot` reflect the safety-z buffer
- **Requirement:** R-INT-P3, R-PLAT-03
- **Target:** `planner._snapshot`, `planner.series_unmet`
- **Pre-reqs:** `make_world()`, `PlannerParams(safety_z=2.0)`, `forecasts` from `prior_forecast`.
- **Steps:**
  1. `risk = _snapshot(state, station, fuel, fc, {}, horizon=24)`
  2. `risk_with_z = _snapshot` using a forecaster whose `sigma` is zero (i.e. we skip safety_z).
  3. Call `series_unmet(state, fc, {}, horizon=24)`.
- **Asserts:** the safety-z demand is `mu[k-1] + 2 * sigma[k-1]`; `series_unmet` sum equals the unmet liters
  reported per series.

## TC-I-012 — Planner returns `solver_status="OPTIMAL"` and at least one line on a clear baseline
- **Requirement:** R-PLAT-03, R-INT-X1
- **Target:** `planner.plan`
- **Pre-reqs:** `make_world()` with depleted inventory (set every station's fuel inventory to 1 000 L to force
  action within the horizon).
- **Steps:** call `plan(state, prior_forecast(state, 24), PlannerParams(horizon_ticks=24))`.
- **Asserts:**
  - `result.solver_status == "OPTIMAL"`.
  - `len(result.lines) > 0`.
  - `result.projected_unmet_after < result.projected_unmet_before` (the plan reduces unmet liters).
  - Every line has `parts` summing to its `quantity` and `parts[i] ≤ route.max_shipment`.

## TC-I-013 — Planner produces no lines when inventory already covers the horizon
- **Requirement:** R-PLAT-03
- **Target:** `planner.plan`
- **Pre-reqs:** `make_world()` with `inventory={"DIESEL": cap, "PETROL": cap, "OCTANE": cap}` (tanks full).
- **Steps:** call `plan(...)`.
- **Asserts:** `result.lines == []`, `result.notes` contains `"No shipment needed this tick"`.

## TC-I-014 — `split_parts` quantises to 100 L, honours `max_shipment`, drops tiny remainders
- **Requirement:** R-INT-X2
- **Target:** `planner.split_parts`
- **Pre-reqs:** `max_shipment=7000`, `min_shipment=500`.
- **Steps:** call with `quantity=14500`, `quantity=7400`, `quantity=400`, `quantity=0`.
- **Asserts:**
  - 14500 → `[7000, 7000]`, sum 14000 (remainder 500 ≥ min_shipment → also emitted? Actually the loop emits
    max_shipment chunks until remaining < max_shipment, then emits the rest if ≥ min(500, 100). So:
    14500 → `[7000, 7000, 500]`, 7400 → `[7000, 400]`, 400 → `[]`, 0 → `[]`.
  - Every emitted part is a multiple of 100 L.

## TC-I-015 — Planner respects dispatch capacity at the source depot
- **Requirement:** R-INT-D2
- **Target:** `planner.plan`
- **Pre-reqs:** set `depot-gazipur.dispatch_capacity_per_tick = 1500` (very tight). Demand on Mirpur + Tongi
  forces a small plan.
- **Steps:** call `plan(state, prior_forecast(state, 24), PlannerParams(horizon_ticks=24))`.
- **Asserts:** sum of `lines[i].quantity for lines[i].depot_id == depot-gazipur` ≤ 1 500; the `binding_constraints`
  list contains `"Dispatch capacity binding at depot-gazipur"`.

## TC-I-016 — Cross-region reroute is annotated as a signal
- **Requirement:** R-INT-X1
- **Target:** `planner._signals`
- **Pre-reqs:** baseline world; planner is forced to pick `route-patiya-mirpur` by removing Gazipur→Mirpur and
  Tongi routes (or emptying Gazipur).
- **Steps:** call `plan(...)`.
- **Asserts:** at least one line has a signal `"cross-region reroute"`.

## TC-I-017 — Single-route station generates a `"single-route station"` signal
- **Requirement:** R-INT-X1
- **Target:** `planner._signals`
- **Pre-reqs:** baseline world; Tongi has only `route-gazipur-tongi`.
- **Steps:** force a plan that ships to Tongi (deplete Tongi inventory), inspect the line's signals.
- **Asserts:** contains `"single-route station"`.

## TC-I-018 — Live demand-spike or station-outage events surface as signals
- **Requirement:** R-INT-X1, R-INT-D1
- **Target:** `planner._signals`
- **Pre-reqs:** inject a `station_outage SCHEDULED 5..10 station_ids=[station-tongi]` and a
  `demand_spike ACTIVE 3..8 multiplier=1.8 region_ids=[region-dhaka]`.
- **Steps:** call `plan(...)`, inspect the line for Tongi.
- **Asserts:** signals include both `"active station_outage ticks 5-10"` and `"active demand_spike ticks 3-8"`.

## TC-I-019 — Each plan line includes a "Skip this shipment" alternative with positive `delta_unmet_liters`
- **Requirement:** R-INT-X1
- **Target:** `planner.plan`
- **Pre-reqs:** a plan with at least one line (deplete Mirpur PETROL).
- **Steps:** inspect `lines[0].alternatives`.
- **Asserts:** the first alternative has `description="Skip this shipment"` and `delta_unmet_liters > 0`.

## TC-I-020 — An alternative route is offered when one exists
- **Requirement:** R-INT-X1
- **Target:** `planner.plan`
- **Pre-reqs:** baseline world; force a shipment to Mirpur via Gazipur.
- **Steps:** inspect alternatives for that line.
- **Asserts:** at least one alternative has `description` starting with `"Ship " " L via route-patiya-mirpur"`
  and `route_id="route-patiya-mirpur"`.

## TC-I-021 — Capacity binding constraint appears when a shipment fills the tank
- **Requirement:** R-PLAT-03
- **Target:** `planner.plan`
- **Pre-reqs:** station inventory almost full (within 1 unit of capacity), planner emits a shipment to it.
- **Steps:** call `plan(...)`, inspect `result.binding_constraints`.
- **Asserts:** a constraint `"station-mirpur PETROL tank capacity"` appears in either `binding_constraints` or
  the line's `binding` field.

## TC-I-022 — Excluded routes (status != AVAILABLE) are not used
- **Requirement:** R-RES-01
- **Target:** `planner.plan`
- **Pre-reqs:** set `route-gazipur-mirpur.status="DISRUPTED"`.
- **Steps:** call `plan(state, prior_forecast(state, 24), PlannerParams(horizon_ticks=24))` and verify no line
  uses that route id.
- **Asserts:** `all(line.route_id != "route-gazipur-mirpur" for line in result.lines)` and the
  `binding_constraints` contain `"route-gazipur-mirpur unavailable (disrupted)"`.

## TC-I-023 — In-transit arrivals reduce unmet liters
- **Requirement:** R-PLAT-03
- **Target:** `planner.plan`
- **Pre-reqs:** a PENDING allocation arriving tick 3 with qty 8 000 to a station that would otherwise run dry
  at tick 5.
- **Steps:** call `plan` and compare `projected_unmet_after` to a baseline without the in-transit allocation.
- **Asserts:** in-transit allocation strictly reduces unmet liters within the horizon.

## TC-I-024 — Terminal cover constraint forces inventory at the end of the horizon
- **Requirement:** R-PLAT-03
- **Target:** `planner._build`
- **Pre-reqs:** `PlannerParams(terminal_cover_ticks=8, terminal_weight=2.0)`, depleted inventory.
- **Steps:** call `plan(...)`, compare to a run with `terminal_weight=0.0`.
- **Asserts:** the weighted run ends the horizon with strictly higher closing inventory for at least one series.

## TC-I-025 — `evaluate` computes before/after projections and improves them
- **Requirement:** R-INT-X3
- **Target:** `evaluate.evaluate`
- **Pre-reqs:** state + forecasts + one `FixedLine` of 4 000 L on `route-gazipur-mirpur`.
- **Steps:** call `evaluate(state, fc, [FixedLine(...)], horizon=24)`.
- **Asserts:**
  - `len(response.before) == 12`, `len(response.after) == 12`.
  - `unmet_after < unmet_before`.
  - `response.after[i].stockout_tick` is later than `response.before[i].stockout_tick` (or `None`).

## TC-I-026 — `evaluate` rejects an unknown route with a `ValueError`
- **Requirement:** R-INT-X3
- **Target:** `evaluate.evaluate`
- **Pre-reqs:** `FixedLine(route_id="route-not-real", ...)`.
- **Steps:** call `evaluate(...)`.
- **Asserts:** raises `ValueError("unknown route route-not-real")` (translated to HTTP 422 in `main.py`).

## TC-I-027 — Engine `/forecast` returns one `SeriesForecast` per series
- **Requirement:** R-INT-P1
- **Target:** `intelligence.main.create_app` (`/forecast`)
- **Pre-reqs:** FastAPI `TestClient` with `Engine(models_dir=..., version="v1")` and a `ForecastRequest` with
  `state = make_world()`, `history = synth_history(96)`, `horizon_ticks = 24`.
- **Steps:** POST `/forecast`.
- **Asserts:** 200, JSON contains 12 items, each with `mu` and `sigma` lists of length 24.

## TC-I-028 — Engine `/cycle` returns a `CycleResponse` with forecasts, risks, ETAs, plan, and model info
- **Requirement:** R-PLAT-03, R-INT-X1
- **Target:** `intelligence.main.create_app` (`/cycle`)
- **Pre-reqs:** Same as TC-I-027 but with `CycleRequest(history=..., params=PlannerParams(...))`.
- **Steps:** POST `/cycle`.
- **Asserts:** 200, `len(forecasts) == 12`, `len(risks) == 12`, `plan.solver_status` in `{"OPTIMAL", "NOT_SOLVED"}`,
  `model.version == "v1"`, every forecast's `first_tick == state.instance.tick + 1`.

## TC-I-029 — Engine `/evaluate` rejects unknown routes with HTTP 422
- **Requirement:** R-INT-X3
- **Target:** `intelligence.main.create_app` (`/evaluate`)
- **Pre-reqs:** `EvaluateRequest` with `lines=[FixedLine(route_id="nope", fuel_type="PETROL", quantity=500)]`.
- **Steps:** POST `/evaluate`.
- **Asserts:** 422 with `detail` containing `"unknown route nope"`.

## TC-I-030 — Engine `/health` and `/model/info` expose the right metadata
- **Requirement:** R-DEVOPS-02
- **Target:** `intelligence.main.create_app`
- **Pre-reqs:** default `Engine`.
- **Steps:** GET `/health`, GET `/model/info`.
- **Asserts:**
  - `/health` → `{"status": "ok", "model_version": "v1"}`.
  - `/model/info` → `{"version": "v1", "kind": "structural", "prior_strength": 4.0, ...}`.
  - `/metrics` returns Prometheus text (starts with `# HELP` or `# TYPE`).

## TC-I-031 — `noise_floor` of an empty fitted model is 0
- **Requirement:** R-INT-P1
- **Target:** `forecast.FittedModel.noise_floor`
- **Pre-reqs:** `FittedModel(params=ForecastParams())`.
- **Steps:** call `model.noise_floor()`.
- **Asserts:** returns `0.0`.

## TC-I-032 — `model.info()` reports `fitted_series` and `observations` correctly
- **Requirement:** R-INT-P1
- **Target:** `forecast.FittedModel.info`
- **Pre-reqs:** fit on empty history (12 series, 0 obs each) and on 96-tick history.
- **Steps:** call `info()`.
- **Asserts:**
  - Empty: `fitted_series=12`, `observations=0`, `noise_floor≈spec.noise(profile)` average.
  - Populated: `observations >= 96 * 12` and `noise_floor` is finite and < 0.12 (jitter floor).

## TC-I-033 — Heuristic fallback (planned) reaches the same risk score as the planner on an empty world
- **Requirement:** R-RES-05 (heuristic fallback always works)
- **Target:** shared/heuristic fallback (architecture §5.5).
- **Pre-reqs:** `make_world()`, `prior_forecast(state, 24)`.
- **Steps:** When the intelligence service is "down" (skip plan), the backend must still produce risk scores
  using only the spec prior and the mirrored world state.
- **Asserts:** risks for OPEN stations match `score_risks(state, prior_forecast(state, 24))`; closed-station
  behaviour preserved; `notes` does not mention "intelligence" (the fallback should not claim it knows).

## TC-I-034 — Plan lines correctly compute `arrival_tick = tick + 1 + transit_ticks`
- **Requirement:** R-INT-X2
- **Target:** `planner.plan`
- **Pre-reqs:** baseline world, one forced line.
- **Steps:** inspect the line.
- **Asserts:** `line.arrival_tick == state.instance.tick + 1 + route.transit_ticks`.
