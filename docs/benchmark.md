# Policy benchmark

*SIMULATED DATA.* Deterministic replays of the organizer's simulator (baseline scenario, seed 12345): pause -> reset -> inject events -> 288 ticks (3 simulated days) in Auto mode, then the simulator's own `/v1/metrics`. Reproduce with `docker compose run --rm tools scripts/benchmark.py`.

*Stockout hours* = sum over the 12 station/fuel series of hours spent empty. *Liters shipped* counts allocations that left the depot (IN_TRANSIT or ARRIVED).

## Summary

- **no-action**: service level 29.88%-30.74%, 579.5-602.5 stockout hours, 193,586-203,498 L unmet across 3 scenario(s).
- **heuristic**: service level 96.02%-96.79%, 76.5-93.0 stockout hours, 9,227-11,117 L unmet across 3 scenario(s).
- **planner**: service level 100.00%, 0.0 stockout hours, 0 L unmet across 3 scenario(s).

## baseline

| Policy | Service level | Unmet demand (L) | Stockout hours | Allocations | Liters shipped | Failed allocations |
|---|---|---|---|---|---|---|
| no-action | 30.74% | 193,586 | 579.5 | 0 | 0 | 0 |
| heuristic | 96.02% | 11,117 (94% less than no-action) | 93.0 | 170 | 194,200 | 0 |
| planner | 100.00% | 0 (100% less than no-action) | 0.0 | 171 | 221,100 | 0 |

## demand-spike

| Policy | Service level | Unmet demand (L) | Stockout hours | Allocations | Liters shipped | Failed allocations |
|---|---|---|---|---|---|---|
| no-action | 29.97% | 203,498 | 602.5 | 1 | 1,200 | 0 |
| heuristic | 96.68% | 9,653 (95% less than no-action) | 80.0 | 177 | 204,800 | 0 |
| planner | 100.00% | 0 (100% less than no-action) | 0.0 | 176 | 231,200 | 0 |

## combined-crisis

| Policy | Service level | Unmet demand (L) | Stockout hours | Allocations | Liters shipped | Failed allocations |
|---|---|---|---|---|---|---|
| no-action | 29.88% | 201,595 | 600.0 | 0 | 0 | 0 |
| heuristic | 96.79% | 9,227 (95% less than no-action) | 76.5 | 176 | 200,400 | 0 |
| planner | 100.00% | 0 (100% less than no-action) | 0.0 | 179 | 229,900 | 0 |
