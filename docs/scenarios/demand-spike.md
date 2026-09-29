# CR-02 - Demand spike

*SIMULATED DATA.* Demand across the Dhaka region jumps to 1.8x for 8 hours, starting 1 hour after the reset. The platform must flag the scheduled event, raise the stockout risk for Dhaka stations and ship ahead of the stockouts. The spike is announced by a simulator event and the forecaster reads each station's live demand multiplier, so it is explained demand: the residual-based demand_anomaly detector (unexplained demand, tests/test_backend_units.py) correctly stays quiet.

Mode `auto`, policy `planner`, 48 ticks (12 simulated hours) after a simulator reset.

## Injected

- event `demand_spike` start tick 4, 32 ticks, `{"multiplier": 1.8, "region_ids": ["region-dhaka"]}`

## Checks

| Check | Result | Detail |
|---|---|---|
| alert raised: event | PASS | kinds seen ['event'] |
| incident opened | PASS | 1 incident(s) |
| plan line to station-mirpur | PASS | ['station-karnaphuli', 'station-mirpur', 'station-tongi'] |
| plan line to station-tongi | PASS | ['station-karnaphuli', 'station-mirpur', 'station-tongi'] |
| final service level >= 0.95 | PASS | 1.0 |
| peak P(stockout 12h) > 20% | PASS | peak 100% (station-tongi DIESEL) |

## Outcome

- Service level: final **100.00%**, minimum 100.00%
- Peak stockout risk: P(stockout 12 h) 100% for station-tongi DIESEL at tick 6
- Allocations submitted: 12 (35 auto-approved lines, 0 held for review); routes used: route-gazipur-mirpur, route-gazipur-tongi, route-patiya-karnaphuli
- Alert kinds raised: event; still open at the end: none
- Incident: Scheduled demand spike on region-dhaka (ticks 4-36) (opened tick 1, resolved tick 37)

## Example explanation (first plan with lines)

> Tick 47: 1 shipment(s), 1,000 L in total (planner policy). Projected unmet demand over the next 24 ticks falls from 0 L to 0 L (0 L avoided).
- station-karnaphuli PETROL: stockout in 8.0 h without action; ship 1,000 L via route-patiya-karnaphuli (arrives tick 50); P(stockout within 6 h) 0% -> 0%; stock at horizon end 1,239 -> 2,239 L; confidence 80%.

## Timeline (ticks with activity)

| Tick | Service level | Empty series | Top risk (12 h) | What happened |
|---|---|---|---|---|
| 2 | 100.00% | 0 | station-tongi DIESEL 0% | warning event: Scheduled demand spike on region-dhaka (ticks 4-36)<br>INCIDENT: Scheduled demand spike on region-dhaka (ticks 4-36) |
| 10 | 100.00% | 0 | station-tongi DIESEL 100% | plan 585 (planner, EXECUTED): depot-gazipur->station-tongi DIESEL 1300 L [PENDING] |
| 12 | 100.00% | 0 | station-tongi DIESEL 100% | plan 587 (planner, EXECUTED): depot-gazipur->station-tongi DIESEL 1500 L [PENDING] |
| 13 | 100.00% | 0 | station-tongi DIESEL 100% | plan 588 (planner, EXECUTED): depot-gazipur->station-tongi DIESEL 1600 L [PENDING]; depot-gazipur->station-mirpur PETROL 1100 L [PENDING] |
| 16 | 100.00% | 0 | station-tongi DIESEL 81% | plan 591 (planner, EXECUTED): depot-gazipur->station-mirpur PETROL 1600 L [PENDING]; depot-gazipur->station-tongi DIESEL 1300 L [PENDING] |
| 40 | 100.00% | 0 | station-mirpur DIESEL 100% | plan 615 (planner, EXECUTED): depot-gazipur->station-tongi DIESEL 1100 L [PENDING] |
| 43 | 100.00% | 0 | station-mirpur DIESEL 100% | plan 618 (planner, EXECUTED): depot-gazipur->station-tongi DIESEL 1700 L [PENDING] |
| 46 | 100.00% | 0 | station-mirpur DIESEL 100% | plan 621 (planner, EXECUTED): depot-gazipur->station-tongi DIESEL 1300 L [PENDING]; depot-gazipur->station-mirpur OCTANE 1000 L [PENDING] |
| 48 | 100.00% | 0 | station-mirpur DIESEL 100% | plan 623 (planner, EXECUTED): depot-patiya->station-karnaphuli PETROL 1000 L [PENDING] |
