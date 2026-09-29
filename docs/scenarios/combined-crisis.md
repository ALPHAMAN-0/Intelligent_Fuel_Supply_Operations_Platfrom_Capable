# CR-05 - Combined crisis

*SIMULATED DATA.* Route disruption, late resupply and a Dhaka demand spike overlap, while the simulator API itself misbehaves: a burst of injected 503 errors and a stretch of stale data. The platform must keep deciding (retries, circuit breaker, cached state), hold automatic approval while data is stale, and still protect the service level.

Mode `assisted`, policy `planner`, 60 ticks (15 simulated hours) after a simulator reset.

## Injected

- event `route_disruption` start tick 2, 30 ticks, `{"route_ids": ["route-gazipur-mirpur"]}`
- event `shipment_delay` start tick 3, 1 ticks, `{"delay_ticks": 8}`
- event `demand_spike` start tick 6, 30 ticks, `{"multiplier": 1.6, "region_ids": ["region-dhaka"]}`
- fault `error_rate` at tick 10 for 8 s
- fault `stale_data` at tick 24 for 6 s

## Checks

| Check | Result | Detail |
|---|---|---|
| alert raised: event | PASS | kinds seen ['event', 'network', 'shortage', 'supply', 'system'] |
| alert raised: network | PASS | kinds seen ['event', 'network', 'shortage', 'supply', 'system'] |
| incident opened | PASS | 6 incident(s) |
| final service level >= 0.93 | PASS | 1.0 |
| stale data blocked auto-approval | PASS | 17 stale tick(s), 65 line(s) held for review |

## Outcome

- Service level: final **100.00%**, minimum 100.00%
- Peak stockout risk: P(stockout 12 h) 100% for station-tongi DIESEL at tick 11
- Allocations submitted: 21 (141 auto-approved lines, 65 held for review); routes used: route-gazipur-mirpur, route-gazipur-tongi, route-patiya-coxsbazar, route-patiya-karnaphuli
- Alert kinds raised: event, network, shortage, supply, system; still open at the end: supply
- Incident: Scheduled shipment delay on all entities (ticks 3-4) (opened tick 1, still open)
- Incident: Shortage risk at station-tongi (opened tick 44, resolved tick 45)
- Incident: Scheduled route disruption on route-gazipur-mirpur (ticks 2-32) (opened tick 1, resolved tick 33)
- Incident: Simulator reports stale data: automatic approval blocked (opened tick 24, resolved tick 41)
- Incident: Simulator API degraded (events): using last good values (opened tick 21, resolved tick 22)
- Incident: Scheduled demand spike on region-dhaka (ticks 6-36) (opened tick 1, resolved tick 37)

## Example explanation (first plan with lines)

> Tick 56: 2 shipment(s), 2,500 L in total (planner policy). Projected unmet demand over the next 24 ticks falls from 0 L to 0 L (0 L avoided).
- station-mirpur DIESEL: stockout in 8.0 h without action; ship 1,500 L via route-gazipur-mirpur (arrives tick 59); P(stockout within 6 h) 0% -> 0%; stock at horizon end 686 -> 2,186 L; confidence 82%.
- station-coxsbazar DIESEL: stockout in 9.0 h without action; ship 1,000 L via route-patiya-coxsbazar (arrives tick 60); P(stockout within 6 h) 0% -> 0%; stock at horizon end 738 -> 1,738 L; confidence 82%.

## Timeline (ticks with activity)

| Tick | Service level | Empty series | Top risk (12 h) | What happened |
|---|---|---|---|---|
| 2 | 100.00% | 0 | station-mirpur DIESEL 0% | warning event: Scheduled demand spike on region-dhaka (ticks 6-36)<br>warning event: Scheduled shipment delay on all entities (ticks 3-4)<br>warning event: Scheduled route disruption on route-gazipur-mirpur (ticks 2-32)<br>warning supply: Supply supply-001 to depot-gazipur (DIESEL) delayed 8 ticks (ETA tick 20)<br>warning supply: Supply supply-003 to depot-patiya (DIESEL) delayed 8 ticks (ETA tick 22)<br>warning supply: Supply supply-002 to depot-gazipur (PETROL) delayed 8 ticks (ETA tick 24)<br>warning supply: Supply supply-004 to depot-patiya (OCTANE) delayed 8 ticks (ETA tick 28)<br>warning supply: Supply supply-101 to depot-gazipur (DIESEL) delayed 8 ticks (ETA tick 72)<br>warning supply: Supply supply-102 to depot-gazipur (PETROL) delayed 8 ticks (ETA tick 76)<br>warning supply: Supply supply-103 to depot-gazipur (OCTANE) delayed 8 ticks (ETA tick 80)<br>warning supply: Supply supply-104 to depot-patiya (DIESEL) delayed 8 ticks (ETA tick 84)<br>warning supply: Supply supply-105 to depot-patiya (PETROL) delayed 8 ticks (ETA tick 88)<br>warning supply: Supply supply-106 to depot-patiya (OCTANE) delayed 8 ticks (ETA tick 92)<br>warning supply: Supply supply-201 to depot-gazipur (DIESEL) delayed 8 ticks (ETA tick 136)<br>warning supply: Supply supply-202 to depot-gazipur (PETROL) delayed 8 ticks (ETA tick 140)<br>warning supply: Supply supply-203 to depot-gazipur (OCTANE) delayed 8 ticks (ETA tick 144)<br>warning supply: Supply supply-204 to depot-patiya (DIESEL) delayed 8 ticks (ETA tick 148)<br>warning supply: Supply supply-205 to depot-patiya (PETROL) delayed 8 ticks (ETA tick 152)<br>warning supply: Supply supply-206 to depot-patiya (OCTANE) delayed 8 ticks (ETA tick 156)<br>warning supply: Supply supply-301 to depot-gazipur (DIESEL) delayed 8 ticks (ETA tick 200)<br>warning supply: Supply supply-302 to depot-gazipur (PETROL) delayed 8 ticks (ETA tick 204)<br>warning supply: Supply supply-303 to depot-gazipur (OCTANE) delayed 8 ticks (ETA tick 208)<br>warning supply: Supply supply-304 to depot-patiya (DIESEL) delayed 8 ticks (ETA tick 212)<br>warning supply: Supply supply-305 to depot-patiya (PETROL) delayed 8 ticks (ETA tick 216)<br>warning supply: Supply supply-306 to depot-patiya (OCTANE) delayed 8 ticks (ETA tick 220)<br>INCIDENT: Scheduled demand spike on region-dhaka (ticks 6-36)<br>INCIDENT: Scheduled shipment delay on all entities (ticks 3-4)<br>INCIDENT: Scheduled route disruption on route-gazipur-mirpur (ticks 2-32) |
| 4 | 100.00% | 0 | station-mirpur DIESEL 0% | warning network: Route route-gazipur-mirpur disrupted; reroute via route-patiya-mirpur |
| 11 | 100.00% | 0 | station-tongi DIESEL 100% | INCIDENT: Simulator API degraded (stations): using last good values |
| 12 | 100.00% | 0 | station-tongi DIESEL 100% | plan 894 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 1100 L [PROPOSED] |
| 13 | 100.00% | 0 | station-tongi DIESEL 100% | plan 895 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 1400 L [PROPOSED] |
| 14 | 100.00% | 0 | station-tongi DIESEL 100% | plan 896 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 1800 L [PROPOSED] |
| 15 | 100.00% | 0 | station-tongi DIESEL 100% | plan 897 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 2200 L [PROPOSED] |
| 16 | 100.00% | 0 | station-tongi DIESEL 100% | plan 898 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 2500 L [PROPOSED] |
| 17 | 100.00% | 0 | station-tongi DIESEL 100% | plan 899 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 2600 L [PROPOSED] |
| 18 | 100.00% | 0 | station-tongi DIESEL 100% | plan 900 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 2700 L [PROPOSED] |
| 19 | 100.00% | 0 | station-tongi DIESEL 100% | plan 901 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 2800 L [PROPOSED] |
| 20 | 100.00% | 0 | station-tongi DIESEL 100% | plan 902 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 2900 L [PROPOSED] |
| 21 | 100.00% | 0 | station-tongi DIESEL 100% | plan 903 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 3000 L [PROPOSED] |
| 22 | 100.00% | 0 | station-tongi DIESEL 100% | plan 904 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 3000 L [PROPOSED] |
| 23 | 100.00% | 0 | station-tongi DIESEL 100% | plan 905 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 3200 L [PROPOSED] |
| 24 | 100.00% | 0 | station-tongi DIESEL 100% | plan 906 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 3200 L [PROPOSED] |
| 25 | 100.00% | 0 | station-tongi DIESEL 100% | INCIDENT: Simulator reports stale data: automatic approval blocked<br>plan 907 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 3100 L [PROPOSED] |
| 26 | 100.00% | 0 | station-tongi DIESEL 100% | plan 908 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 3000 L [PROPOSED] |
| 27 | 100.00% | 0 | station-mirpur PETROL 100% | plan 909 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 3200 L [PROPOSED] |
| 28 | 100.00% | 0 | station-mirpur PETROL 100% | plan 910 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 3200 L [PROPOSED] |
| 29 | 100.00% | 0 | station-mirpur PETROL 100% | plan 911 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 3400 L [PROPOSED] |
| 30 | 100.00% | 0 | station-mirpur PETROL 100% | plan 912 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 3600 L [PROPOSED] |
| 31 | 100.00% | 0 | station-mirpur PETROL 100% | plan 913 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 3800 L [PROPOSED] |
| 32 | 100.00% | 0 | station-mirpur PETROL 100% | plan 914 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 3900 L [PROPOSED] |
| 33 | 100.00% | 0 | station-mirpur PETROL 100% | plan 915 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 4100 L [PROPOSED] |
| 34 | 100.00% | 0 | station-mirpur PETROL 100% | plan 916 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 4300 L [PROPOSED] |
| 35 | 100.00% | 0 | station-mirpur PETROL 100% | plan 917 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 4500 L [PROPOSED] |
| 36 | 100.00% | 0 | station-mirpur PETROL 100% | plan 918 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 4700 L [PROPOSED] |
| 37 | 100.00% | 0 | station-mirpur PETROL 100% | plan 919 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 5100 L [PROPOSED] |
| 38 | 100.00% | 0 | station-mirpur PETROL 100% | plan 920 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 5400 L [PROPOSED] |
| 39 | 100.00% | 0 | station-mirpur PETROL 100% | plan 921 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 5700 L [PROPOSED] |
| 40 | 100.00% | 0 | station-mirpur PETROL 100% | warning shortage: Shortage risk station-tongi DIESEL: stockout in 4.0 h, P(4h) 51%<br>INCIDENT: Shortage risk at station-tongi<br>plan 922 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 5900 L [PROPOSED] |
| 41 | 100.00% | 0 | station-mirpur PETROL 100% | plan 923 (planner, PROPOSED): depot-gazipur->station-tongi DIESEL 6100 L [PROPOSED] |
| 42 | 100.00% | 0 | station-mirpur PETROL 100% | plan 924 (planner, EXECUTED): depot-gazipur->station-tongi DIESEL 6300 L [PENDING] |
| 44 | 100.00% | 0 | station-mirpur PETROL 100% | plan 926 (planner, EXECUTED): depot-gazipur->station-mirpur PETROL 1100 L [PENDING] |
| 45 | 100.00% | 0 | station-mirpur DIESEL 100% | plan 927 (planner, EXECUTED): depot-gazipur->station-tongi DIESEL 6500 L [PENDING] |
| 46 | 100.00% | 0 | station-mirpur DIESEL 100% | plan 928 (planner, EXECUTED): depot-patiya->station-karnaphuli DIESEL 1200 L [PENDING] |
| 47 | 100.00% | 0 | station-mirpur DIESEL 100% | plan 929 (planner, EXECUTED): depot-gazipur->station-mirpur PETROL 1900 L [PENDING] |
| 48 | 100.00% | 0 | station-mirpur DIESEL 100% | plan 930 (planner, EXECUTED): depot-gazipur->station-tongi DIESEL 1400 L [PENDING]; depot-patiya->station-karnaphuli PETROL 1000 L [PENDING] |
| 49 | 100.00% | 0 | station-mirpur DIESEL 100% | plan 931 (planner, EXECUTED): depot-patiya->station-karnaphuli DIESEL 1900 L [PENDING]; depot-gazipur->station-mirpur OCTANE 1000 L [PENDING] |
| 50 | 100.00% | 0 | station-mirpur DIESEL 100% | plan 932 (planner, EXECUTED): depot-gazipur->station-mirpur PETROL 1500 L [PENDING]; depot-patiya->station-karnaphuli OCTANE 1000 L [PENDING] |
| 51 | 100.00% | 0 | station-mirpur DIESEL 100% | plan 933 (planner, EXECUTED): depot-patiya->station-karnaphuli PETROL 1700 L [PENDING] |
| 52 | 100.00% | 0 | station-mirpur DIESEL 100% | plan 934 (planner, EXECUTED): depot-patiya->station-karnaphuli DIESEL 1400 L [PENDING]; depot-gazipur->station-mirpur OCTANE 1400 L [PENDING]; depot-patiya->station-coxsbazar PETROL 1000 L [PENDING] |
| 53 | 100.00% | 0 | station-mirpur DIESEL 100% | plan 935 (planner, EXECUTED): depot-patiya->station-karnaphuli OCTANE 1500 L [PENDING] |
| 54 | 100.00% | 0 | station-mirpur DIESEL 100% | plan 936 (planner, EXECUTED): depot-patiya->station-karnaphuli PETROL 1400 L [PENDING]; depot-gazipur->station-mirpur DIESEL 1000 L [PENDING] |
| 56 | 100.00% | 0 | station-karnaphuli OCTANE 100% | plan 938 (planner, EXECUTED): depot-patiya->station-coxsbazar PETROL 1400 L [PENDING] |
| 57 | 100.00% | 0 | station-mirpur DIESEL 100% | plan 939 (planner, EXECUTED): depot-gazipur->station-mirpur DIESEL 1500 L [PENDING]; depot-patiya->station-coxsbazar DIESEL 1000 L [PENDING] |
