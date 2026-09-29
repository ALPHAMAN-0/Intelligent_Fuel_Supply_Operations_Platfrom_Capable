# Model selection — demand forecast

Status: **decided** (DEC-08, 2026-09-29) · Owner: AI · Tasks: I2-T1 (structural forecaster), OPT-T5 (LightGBM
challenger) · Detail: [`architecture.md` §5.1](../architecture.md)

All data here is simulated, produced by the organizer's BUP Fuel Supply Simulator.

## Decision

| Role | Model | Status |
|---|---|---|
| **Primary forecaster** | Structural model: learned hour-of-day level × live `demand_multiplier` | Must (I2-T1) |
| **Challenger** | LightGBM on the structural model's residuals | Could (OPT-T5); ships only if it wins the test below |
| **Fallback** | Spec prior (from the integration guide §8.5–8.6) × `demand_multiplier` | Always available, no training needed |

## Why the structural model is primary

The simulator generates demand from a known formula (DI-12):

```
demand = profile base × region factor × hour-of-day factor × demand_multiplier × jitter (8–12 %)
```

- Every term except the jitter is either documented or readable live (`demand_multiplier` on `/v1/stations`,
  upcoming spikes on `/v1/events`). A model that learns the hour-of-day level per (station, fuel) and multiplies by
  the current multiplier reaches the jitter noise floor. No model can beat the noise floor.
- It works **from tick 0** using the spec prior, before any history exists. LightGBM cannot.
- It reacts to a demand spike **on the tick it starts**, because the multiplier is an input, not something inferred
  from lagged demand.
- It is fast to build and easy to explain to judges, which fits the 24 h build window (DEC-09). The brief says
  complexity alone does not raise the score.

```
μ[s,f,t] = b[s,f,hour(t)] × m[s](t)          σ[s,f,t] = k[s,f] × μ[s,f,t]
```

`b` starts at the spec prior and moves to the mean of `demand / demand_multiplier` per hour bucket as history
accumulates; `k` is the relative jitter estimated from residuals.

## LightGBM challenger (OPT-T5)

- **Target:** residual of the structural forecast, `demand_liters / μ − 1`, at target tick `t+h`.
- **Features:** station, fuel, region, demand profile, hour-of-day of the **target** tick (from `sim_time`), horizon
  `h`, current `demand_multiplier`, recent residuals (last 4 ticks) and the residual at the same tick yesterday.
- **One global model** across all 12 station × fuel series, with station and fuel as categorical features.
- **Expected result:** if the simulator's jitter is independent from tick to tick, the residuals are pure noise and
  LightGBM learns roughly zero. That is still worth reporting: it shows the primary model is already at the floor.

## Forecast contract (applies to every candidate)

| Item | Rule | Why |
|---|---|---|
| Target | `demand_liters`, never `served_liters` | `served_liters` is capped when a station runs dry |
| Horizon | every tick up to 96 ticks (24 h) | Shipments arrive 1 + `transit_ticks` = 3–5 ticks after creation (DI-08); the planner looks 24 ticks ahead; risk is scored at 4 h, 12 h and 24 h |
| Tick length | read `tick_minutes` from `/v1/instance` | `TICK_MINUTES` is configurable; never hardcode 15 |
| Hour of day | derive from `sim_time` of the target tick | The tick index does not give the hour directly if tick length changes |
| Output | mean `μ` and spread `σ` per (station, fuel, tick) | Risk scoring needs `σ` to compute P(stockout) |
| Training data | mirrored history in Postgres | `/v1/demand-history` returns at most 2000 rows (DI-11) |

## Selection test

1. **Generate data** with a script (the `/admin/*` API is for scripts only): pause → reset → inject several
   `demand_spike` events → step about 7 simulated days (672 ticks) → mirror history.
2. **Split by time**, never randomly: train on days 1–5 and test on days 6–7. Include spikes in both parts.
3. **Measure** MAE and MAPE at horizons 1, 4, 16, 48 and 96 ticks, plus the error of the demand summed over lead
   time (3–5 ticks). Compare each against the spec noise floor (8–12 %).
4. **Rule:** LightGBM replaces the structural model only if it cuts MAE by at least 5 % (relative) at the planner
   horizons (≤ 24 ticks) without worse `σ` calibration. Otherwise the structural model stays. The 5 % margin is a
   proposal; confirm it once real numbers exist.
5. **Record** the results table in `docs/benchmark.md` and save parameters as `models/forecast-<version>.json`.

## Candidates rejected

| Model | Reason |
|---|---|
| ARIMA / Prophet | One model per series (12 in total), and they cannot easily use `demand_multiplier` or station features |
| LSTM / other deep learning | Needs far more data and tuning, slower, harder to explain, no expected gain on demand this regular |
| LightGBM as the only forecaster | Cannot forecast at tick 0 and reacts to spikes a few ticks late; kept as the challenger instead |

## Other models in the system (for reference)

| Job | Choice | Record |
|---|---|---|
| Allocation decisions | Rolling-horizon linear program (OR-Tools GLOP), heuristic fallback | DEC-07, `architecture.md` §5.3 |
| Anomaly detection | Residual vs latest forecast, z-score > 3 over consecutive ticks | `architecture.md` §4 (detectors) |
| Reinforcement learning | Not used: optional in the brief, and the LP already covers the decisions | — |
| Explanation text (LLM) | Claude API with template fallback, per `architecture.md` §4.7. A free local model via Ollama was proposed on 2026-09-29 but not decided | open |
