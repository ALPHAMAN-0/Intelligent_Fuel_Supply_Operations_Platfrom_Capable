# CR-01 - Shipment delay

*SIMULATED DATA.* Every scheduled depot resupply arrives 12 ticks (3 h) later than planned. The platform must warn about the late supply before it bites, show the shortage impact, keep stations supplied from remaining depot stock, and recover.

Mode `auto`, policy `planner`, 48 ticks (12 simulated hours) after a simulator reset.

## Injected

- event `shipment_delay` start tick 1, 1 ticks, `{"delay_ticks": 12}`

## Checks

| Check | Result | Detail |
|---|---|---|
| alert raised: event | PASS | kinds seen ['event', 'supply'] |
| alert raised: supply | PASS | kinds seen ['event', 'supply'] |
| final service level >= 0.97 | PASS | 1.0 |

## Outcome

- Service level: final **100.00%**, minimum 100.00%
- Peak stockout risk: P(stockout 12 h) 100% for station-tongi DIESEL at tick 20
- Allocations submitted: 6 (23 auto-approved lines, 0 held for review); routes used: route-gazipur-tongi, route-patiya-karnaphuli
- Alert kinds raised: event, supply; still open at the end: supply
- Incident: Scheduled shipment delay on all entities (ticks 1-2) (opened tick 1, still open)

## Example explanation (first plan with lines)

> Tick 47: 1 shipment(s), 1,000 L in total (planner policy). Projected unmet demand over the next 24 ticks falls from 0 L to 0 L (0 L avoided).
- station-karnaphuli PETROL: stockout in 8.0 h without action; ship 1,000 L via route-patiya-karnaphuli (arrives tick 50); P(stockout within 6 h) 0% -> 0%; stock at horizon end 1,239 -> 2,239 L; confidence 80%.

## Timeline (ticks with activity)

| Tick | Service level | Empty series | Top risk (12 h) | What happened |
|---|---|---|---|---|
| 2 | 100.00% | 0 | station-mirpur DIESEL 0% | warning event: Scheduled shipment delay on all entities (ticks 1-2)<br>warning supply: Supply supply-001 to depot-gazipur (DIESEL) delayed 12 ticks (ETA tick 24)<br>warning supply: Supply supply-003 to depot-patiya (DIESEL) delayed 12 ticks (ETA tick 26)<br>warning supply: Supply supply-002 to depot-gazipur (PETROL) delayed 12 ticks (ETA tick 28)<br>warning supply: Supply supply-004 to depot-patiya (OCTANE) delayed 12 ticks (ETA tick 32)<br>warning supply: Supply supply-101 to depot-gazipur (DIESEL) delayed 12 ticks (ETA tick 76)<br>warning supply: Supply supply-102 to depot-gazipur (PETROL) delayed 12 ticks (ETA tick 80)<br>warning supply: Supply supply-103 to depot-gazipur (OCTANE) delayed 12 ticks (ETA tick 84)<br>warning supply: Supply supply-104 to depot-patiya (DIESEL) delayed 12 ticks (ETA tick 88)<br>warning supply: Supply supply-105 to depot-patiya (PETROL) delayed 12 ticks (ETA tick 92)<br>warning supply: Supply supply-106 to depot-patiya (OCTANE) delayed 12 ticks (ETA tick 96)<br>warning supply: Supply supply-201 to depot-gazipur (DIESEL) delayed 12 ticks (ETA tick 140)<br>warning supply: Supply supply-202 to depot-gazipur (PETROL) delayed 12 ticks (ETA tick 144)<br>warning supply: Supply supply-203 to depot-gazipur (OCTANE) delayed 12 ticks (ETA tick 148)<br>warning supply: Supply supply-204 to depot-patiya (DIESEL) delayed 12 ticks (ETA tick 152)<br>warning supply: Supply supply-205 to depot-patiya (PETROL) delayed 12 ticks (ETA tick 156)<br>warning supply: Supply supply-206 to depot-patiya (OCTANE) delayed 12 ticks (ETA tick 160)<br>warning supply: Supply supply-301 to depot-gazipur (DIESEL) delayed 12 ticks (ETA tick 204)<br>warning supply: Supply supply-302 to depot-gazipur (PETROL) delayed 12 ticks (ETA tick 208)<br>warning supply: Supply supply-303 to depot-gazipur (OCTANE) delayed 12 ticks (ETA tick 212)<br>warning supply: Supply supply-304 to depot-patiya (DIESEL) delayed 12 ticks (ETA tick 216)<br>warning supply: Supply supply-305 to depot-patiya (PETROL) delayed 12 ticks (ETA tick 220)<br>warning supply: Supply supply-306 to depot-patiya (OCTANE) delayed 12 ticks (ETA tick 224)<br>INCIDENT: Scheduled shipment delay on all entities (ticks 1-2) |
| 30 | 100.00% | 0 | station-tongi DIESEL 100% | plan 556 (planner, EXECUTED): depot-gazipur->station-tongi DIESEL 1000 L [PENDING] |
| 33 | 100.00% | 0 | station-tongi DIESEL 100% | plan 559 (planner, EXECUTED): depot-gazipur->station-tongi DIESEL 1600 L [PENDING] |
| 36 | 100.00% | 0 | station-tongi DIESEL 100% | plan 562 (planner, EXECUTED): depot-gazipur->station-tongi DIESEL 1200 L [PENDING] |
| 46 | 100.00% | 0 | station-mirpur PETROL 100% | plan 572 (planner, EXECUTED): depot-patiya->station-karnaphuli DIESEL 1200 L [PENDING] |
| 47 | 100.00% | 0 | station-mirpur PETROL 100% | plan 573 (planner, EXECUTED): depot-gazipur->station-tongi DIESEL 1000 L [PENDING] |
| 48 | 100.00% | 0 | station-mirpur PETROL 100% | plan 574 (planner, EXECUTED): depot-patiya->station-karnaphuli PETROL 1000 L [PENDING] |
