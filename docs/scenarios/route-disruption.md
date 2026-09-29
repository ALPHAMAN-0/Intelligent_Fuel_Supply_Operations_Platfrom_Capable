# CR-04 - Regional disruption

*SIMULATED DATA.* The direct Gazipur -> Mirpur route is DISRUPTED for 9 hours while Mirpur's demand doubles, so Mirpur must be resupplied before the route reopens; Cox's Bazar station also has a 2-hour outage (a closed station serves nothing, so its demand counts as unmet whatever the platform does). The platform must raise network alerts, reroute Mirpur through the longer Patiya -> Mirpur route, stop shipping to the closed station, and clear the alerts on recovery.

Mode `auto`, policy `planner`, 48 ticks (12 simulated hours) after a simulator reset.

## Injected

- event `route_disruption` start tick 2, 36 ticks, `{"route_ids": ["route-gazipur-mirpur"]}`
- event `demand_spike` start tick 2, 36 ticks, `{"multiplier": 2.2, "station_ids": ["station-mirpur"]}`
- event `station_outage` start tick 6, 8 ticks, `{"station_ids": ["station-coxsbazar"]}`

## Checks

| Check | Result | Detail |
|---|---|---|
| alert raised: event | PASS | kinds seen ['event', 'network', 'shortage'] |
| alert raised: network | PASS | kinds seen ['event', 'network', 'shortage'] |
| plan line to station-mirpur | PASS | ['station-coxsbazar', 'station-karnaphuli', 'station-mirpur', 'station-tongi'] |
| allocation via route-patiya-mirpur | PASS | ['route-gazipur-tongi', 'route-patiya-karnaphuli', 'route-patiya-mirpur'] |
| no allocation via route-gazipur-mirpur | PASS | ['route-gazipur-tongi', 'route-patiya-karnaphuli', 'route-patiya-mirpur'] |
| network alerts resolved by the end | PASS | still open: [] |
| final service level >= 0.95 | PASS | 0.9781 |

## Outcome

- Service level: final **97.81%**, minimum 90.35%
- Peak stockout risk: P(stockout 12 h) 100% for station-coxsbazar DIESEL at tick 8
- Allocations submitted: 17 (119 auto-approved lines, 35 held for review); routes used: route-gazipur-tongi, route-patiya-karnaphuli, route-patiya-mirpur
- Alert kinds raised: event, network, shortage; still open at the end: none
- Incident: Scheduled station outage on station-coxsbazar (ticks 6-14) (opened tick 1, resolved tick 15)
- Incident: Scheduled route disruption on route-gazipur-mirpur (ticks 2-38) (opened tick 1, resolved tick 39)
- Incident: Scheduled demand spike on station-mirpur (ticks 2-38) (opened tick 1, resolved tick 39)

## Example explanation (first plan with lines)

> Tick 47: 1 shipment(s), 1,000 L in total (planner policy). Projected unmet demand over the next 24 ticks falls from 0 L to 0 L (0 L avoided).
- station-karnaphuli PETROL: stockout in 8.0 h without action; ship 1,000 L via route-patiya-karnaphuli (arrives tick 50); P(stockout within 6 h) 0% -> 0%; stock at horizon end 1,239 -> 2,239 L; confidence 80%.

## Timeline (ticks with activity)

| Tick | Service level | Empty series | Top risk (12 h) | What happened |
|---|---|---|---|---|
| 2 | 100.00% | 0 | station-mirpur PETROL 40% | warning event: Scheduled station outage on station-coxsbazar (ticks 6-14)<br>warning event: Scheduled demand spike on station-mirpur (ticks 2-38)<br>warning event: Scheduled route disruption on route-gazipur-mirpur (ticks 2-38)<br>warning shortage: Shortage risk station-coxsbazar DIESEL: stockout in 1.2 h, P(4h) 0%<br>warning shortage: Shortage risk station-coxsbazar PETROL: stockout in 1.2 h, P(4h) 0%<br>warning shortage: Shortage risk station-coxsbazar OCTANE: stockout in 1.2 h, P(4h) 0%<br>INCIDENT: Scheduled station outage on station-coxsbazar (ticks 6-14)<br>INCIDENT: Scheduled demand spike on station-mirpur (ticks 2-38)<br>INCIDENT: Scheduled route disruption on route-gazipur-mirpur (ticks 2-38) |
| 4 | 100.00% | 0 | station-mirpur PETROL 99% | warning network: Route route-gazipur-mirpur disrupted; reroute via route-patiya-mirpur |
| 7 | 97.65% | 0 | station-mirpur PETROL 100% | plan 840 (planner, EXECUTED): depot-patiya->station-mirpur PETROL 1300 L [PENDING] |
| 8 | 95.89% | 0 | station-coxsbazar DIESEL 100% | critical network: Station station-coxsbazar outage: no fuel served |
| 9 | 94.45% | 0 | station-coxsbazar DIESEL 100% | plan 842 (planner, EXECUTED): depot-patiya->station-mirpur PETROL 1000 L [PENDING]; depot-patiya->station-mirpur OCTANE 1000 L [PENDING] |
| 11 | 92.57% | 0 | station-coxsbazar DIESEL 100% | plan 844 (planner, EXECUTED): depot-patiya->station-mirpur PETROL 1100 L [PENDING] |
| 12 | 91.81% | 0 | station-coxsbazar DIESEL 100% | plan 845 (planner, EXECUTED): depot-patiya->station-mirpur PETROL 1800 L [PENDING]; depot-patiya->station-mirpur DIESEL 1200 L [PENDING] |
| 13 | 91.29% | 0 | station-coxsbazar DIESEL 100% | plan 846 (planner, EXECUTED): depot-patiya->station-mirpur OCTANE 1100 L [PENDING] |
| 14 | 90.79% | 0 | station-coxsbazar DIESEL 100% | plan 847 (planner, EXECUTED): depot-patiya->station-mirpur OCTANE 1300 L [PENDING] |
| 15 | 90.35% | 0 | station-coxsbazar DIESEL 100% | plan 848 (planner, EXECUTED): depot-patiya->station-mirpur DIESEL 1000 L [PENDING] |
| 16 | 90.98% | 0 | station-mirpur DIESEL 0% | plan 849 (planner, EXECUTED): depot-patiya->station-mirpur PETROL 1200 L [PENDING] |
| 17 | 91.54% | 0 | station-tongi DIESEL 2% | plan 850 (planner, EXECUTED): depot-patiya->station-mirpur DIESEL 1200 L [PENDING] |
| 30 | 95.87% | 0 | station-tongi DIESEL 100% | plan 863 (planner, EXECUTED): depot-gazipur->station-tongi DIESEL 1000 L [PENDING] |
| 33 | 96.52% | 0 | station-tongi DIESEL 100% | plan 866 (planner, EXECUTED): depot-gazipur->station-tongi DIESEL 1600 L [PENDING] |
| 36 | 97.00% | 0 | station-tongi DIESEL 100% | plan 869 (planner, EXECUTED): depot-gazipur->station-tongi DIESEL 1200 L [PENDING] |
| 46 | 97.73% | 0 | station-karnaphuli DIESEL 100% | plan 879 (planner, EXECUTED): depot-patiya->station-karnaphuli DIESEL 1200 L [PENDING] |
| 47 | 97.77% | 0 | station-karnaphuli DIESEL 100% | plan 880 (planner, EXECUTED): depot-gazipur->station-tongi DIESEL 1000 L [PENDING] |
| 48 | 97.81% | 0 | station-karnaphuli DIESEL 100% | plan 881 (planner, EXECUTED): depot-patiya->station-karnaphuli PETROL 1000 L [PENDING] |
