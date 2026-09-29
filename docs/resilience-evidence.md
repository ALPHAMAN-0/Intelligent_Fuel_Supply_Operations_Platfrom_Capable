# Resilience evidence

*SIMULATED DATA.* Brief §11 asks for evidence of how the platform behaves under failure (D-09, R-RES-01..05). This
page maps every required failure to its mechanism, and to where it is demonstrated against the organizer's real
simulator image and tested in CI.

## Required behaviour (brief §11)

| Failure | Mechanism | Live evidence | Automated tests |
|---|---|---|---|
| ML model unavailable → fallback allocation policy | Intelligence calls time out (3 s) → heuristic policy for that cycle, tagged on the plan; `system:intelligence` alert; planner resumes automatically | [Drill 1](resilience-drills.md#intelligence-service-outage): fallback in 1–2 s, decisions every tick during the outage, planner back after restart | `test_e2e.py::test_intelligence_outage_falls_back_and_recovers`, `TestCase` |
| Invalid simulator response → reject input and raise alert | Every `/v1/*` payload is validated with pydantic; invalid bodies raise `INVALID_RESPONSE`, keep the last good value for that endpoint, count `fuelops_sim_invalid_responses_total` and raise an alert | Cannot be injected into the published image (it only serves valid JSON) | `test_integration_layer.py::test_invalid_payload_is_rejected_and_reported`, `::test_non_json_body_is_invalid_response` |
| Prediction confidence too low → human review | Assisted mode only auto-approves lines with confidence ≥ `AUTO_APPROVE_MIN_CONFIDENCE`, ≤ `AUTO_APPROVE_MAX_LITERS` and no open critical alert on the entity; otherwise `needs_review_reason` | [CR-05](scenarios/combined-crisis.md): lines held for review | `test_e2e.py::test_assisted_mode_sends_low_confidence_lines_to_review` |
| Backend dependency unavailable → retry / cached state / degraded mode | Retries with jittered backoff; read/write circuit breakers; UI served from the cache (never proxied, ADR-006); degraded endpoints flagged; SSE → polling fallback; SQLite down → run from memory and write back on recovery | [Drills 2, 3 and 5](resilience-drills.md) | `test_integration_layer.py`, `test_hardening.py` |

Also demonstrated: stale simulator data blocks automatic approval ([Drill 4](resilience-drills.md#stale-simulator-data-20-s),
[CR-05](scenarios/combined-crisis.md)), a backend restart restores plans and decisions from SQLite
([Drill 6](resilience-drills.md#backend-restart)), and a simulator reset starts a new run epoch without mixing data.

## Crisis response (brief §10)

Five crisis scenarios replayed deterministically on the real simulator, each with machine-checked expectations
(detect → evaluate → respond → explain → recover): [shipment delay](scenarios/shipment-delay.md),
[demand spike](scenarios/demand-spike.md), [depot constraint](scenarios/depot-constraint.md),
[regional disruption](scenarios/route-disruption.md), [combined crisis](scenarios/combined-crisis.md).
The [policy benchmark](benchmark.md) shows the effect of the decisions: the LP planner kept a 100% service level with
zero stockout hours in all three benchmark scenarios over 3 simulated days, the heuristic fallback kept 96–97%, and
doing nothing collapsed to about 30%.

## Bugs the drills found (and how they were fixed)

Running the drills against the real stack found two failures that the in-process test suite had not:

1. **The simulator client never recovered from an outage.** When a fault ended while the read breaker was
   HALF_OPEN, the probe request failed, its retry was refused by the breaker, and the probe was never released, so
   every later call was refused: the backend showed frozen data forever while the simulator had recovered. Fix: a
   failed HALF_OPEN probe re-opens the breaker immediately (no retry into a refusal), and a probe that never reports
   back is abandoned after one cooldown. Tests: `test_hardening.py::test_client_recovers_after_a_failed_half_open_probe_once_the_simulator_is_back`,
   `::test_a_probe_that_never_reports_back_does_not_wedge_the_breaker`. Drill 2 now recovers ~20 s after the fault.
2. **Decisions made during a database outage were lost.** The loop correctly kept deciding from memory, but those
   plans and decisions were never written once SQLite came back, so a later restart lost them. Fix: when the store
   recovers, the orchestrator writes back the restore window (latest 200 plans / 1000 decisions of the run). Test:
   `test_hardening.py::test_plans_and_decisions_made_during_a_database_outage_are_written_once_it_returns`.

Earlier code review also fixed approval/cycle races (ADR-014) and added a bounded what-if limiter (429 + Retry-After).

## Reproduce

```bash
docker compose up -d --build
python scripts/resilience_drill.py                                       # writes docs/resilience-drills.md
docker compose run --rm tools scripts/run_scenario.py scenarios/*.yaml   # writes docs/scenarios/ (resets the simulator)
```

## Known limits

- The simulator's `stream_disconnect` fault only refuses *new* stream connections; an open stream keeps flowing. The
  drill therefore restarts the backend while the fault is active to exercise the polling fallback.
- Invalid payloads are exercised only by unit tests (the published simulator cannot be made to send them).
- If the backend itself is down there is no UI; the simulator keeps running and the backend resynchronises (new run
  epoch only after a simulator reset) when it comes back.
