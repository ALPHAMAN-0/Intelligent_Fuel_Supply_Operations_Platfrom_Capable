# CR-03 - Depot constraint

*SIMULATED DATA.* Gazipur depot becomes CONSTRAINED for 6 hours and its next resupply delivers only 40% of the planned volume. The platform must surface the constraint and the shortfall, keep planning within the reduced stock, and show the impact.

Mode `auto`, policy `planner`, 48 ticks (12 simulated hours) after a simulator reset.

## Injected

- event `depot_constraint` start tick 2, 24 ticks, `{"depot_ids": ["depot-gazipur"]}`
- event `supply_shortfall` start tick 2, 1 ticks, `{"factor": 0.4, "depot_ids": ["depot-gazipur"]}`

## Checks

| Check | Result | Detail |
|---|---|---|
| alert raised: event | PASS | kinds seen ['event', 'network', 'supply'] |
| alert raised: network | PASS | kinds seen ['event', 'network', 'supply'] |
| alert raised: supply | PASS | kinds seen ['event', 'network', 'supply'] |
| network alerts resolved by the end | PASS | still open: ['supply'] |
| final service level >= 0.95 | PASS | 1.0 |

## Outcome

- Service level: final **100.00%**, minimum 100.00%
- Peak stockout risk: P(stockout 12 h) 100% for station-tongi DIESEL at tick 20
- Allocations submitted: 6 (42 auto-approved lines, 0 held for review); routes used: route-gazipur-tongi, route-patiya-karnaphuli
- Alert kinds raised: event, network, supply; still open at the end: supply
- Incident: Scheduled supply shortfall on depot-gazipur (ticks 2-3) (opened tick 1, still open)
- Incident: Scheduled depot constraint on depot-gazipur (ticks 2-26) (opened tick 1, resolved tick 27)

## Example explanation (first plan with lines)

> Tick 47: 1 shipment(s), 1,000 L in total (planner policy). Projected unmet demand over the next 24 ticks falls from 0 L to 0 L (0 L avoided).
- station-karnaphuli PETROL: stockout in 8.0 h without action; ship 1,000 L via route-patiya-karnaphuli (arrives tick 50); P(stockout within 6 h) 0% -> 0%; stock at horizon end 1,239 -> 2,239 L; confidence 80%.

## Timeline (ticks with activity)

| Tick | Service level | Empty series | Top risk (12 h) | What happened |
|---|---|---|---|---|
| 1 | 100.00% | 0 | station-mirpur DIESEL 0% | warning event: Scheduled supply shortfall on depot-gazipur (ticks 2-3)<br>INCIDENT: Scheduled supply shortfall on depot-gazipur (ticks 2-3)<br>INCIDENT: Scheduled depot constraint on depot-gazipur (ticks 2-26) |
| 2 | 100.00% | 0 | station-mirpur DIESEL 0% | warning event: Scheduled depot constraint on depot-gazipur (ticks 2-26)<br>warning supply: Supply supply-001 to depot-gazipur (DIESEL) quantity 7200 L vs 18000 L first announced<br>warning supply: Supply supply-002 to depot-gazipur (PETROL) quantity 5600 L vs 14000 L first announced<br>warning supply: Supply supply-101 to depot-gazipur (DIESEL) quantity 4800 L vs 12000 L first announced<br>warning supply: Supply supply-102 to depot-gazipur (PETROL) quantity 4000 L vs 10000 L first announced<br>warning supply: Supply supply-103 to depot-gazipur (OCTANE) quantity 2800 L vs 7000 L first announced<br>warning supply: Supply supply-201 to depot-gazipur (DIESEL) quantity 4800 L vs 12000 L first announced<br>warning supply: Supply supply-202 to depot-gazipur (PETROL) quantity 4000 L vs 10000 L first announced<br>warning supply: Supply supply-203 to depot-gazipur (OCTANE) quantity 2800 L vs 7000 L first announced<br>warning supply: Supply supply-301 to depot-gazipur (DIESEL) quantity 4800 L vs 12000 L first announced<br>warning supply: Supply supply-302 to depot-gazipur (PETROL) quantity 4000 L vs 10000 L first announced<br>warning supply: Supply supply-303 to depot-gazipur (OCTANE) quantity 2800 L vs 7000 L first announced |
| 4 | 100.00% | 0 | station-mirpur DIESEL 0% | warning network: Depot depot-gazipur constrained |
| 30 | 100.00% | 0 | station-tongi DIESEL 100% | plan 654 (planner, EXECUTED): depot-gazipur->station-tongi DIESEL 1000 L [PENDING] |
| 33 | 100.00% | 0 | station-tongi DIESEL 100% | plan 657 (planner, EXECUTED): depot-gazipur->station-tongi DIESEL 1600 L [PENDING] |
| 36 | 100.00% | 0 | station-tongi DIESEL 100% | plan 660 (planner, EXECUTED): depot-gazipur->station-tongi DIESEL 1200 L [PENDING] |
| 46 | 100.00% | 0 | station-mirpur PETROL 100% | plan 670 (planner, EXECUTED): depot-patiya->station-karnaphuli DIESEL 1200 L [PENDING] |
| 47 | 100.00% | 0 | station-mirpur PETROL 100% | plan 671 (planner, EXECUTED): depot-gazipur->station-tongi DIESEL 1000 L [PENDING] |
| 48 | 100.00% | 0 | station-mirpur PETROL 100% | plan 672 (planner, EXECUTED): depot-patiya->station-karnaphuli PETROL 1000 L [PENDING] |
