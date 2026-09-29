# Policy benchmark

*SIMULATED DATA.* Deterministic replays of the organizer's simulator (baseline scenario, seed 12345): pause -> reset -> inject events -> 288 ticks (3 simulated days) in Auto mode, then the simulator's own `/v1/metrics`. Reproduce with `docker compose run --rm tools scripts/benchmark.py`.

*Stockout hours* = sum over the 12 station/fuel series of hours spent empty. *Liters shipped* counts allocations that left the depot (IN_TRANSIT or ARRIVED).

## baseline

| Policy | Service level | Unmet demand (L) | Stockout hours | Allocations | Liters shipped | Failed allocations |
|---|---|---|---|---|---|---|
| no-action | 30.74% | 193,586 | 579.5 | 0 | 0 | 0 |
| heuristic | 96.02% | 11,117 (+94% vs no-action) | 93.0 | 170 | 194,200 | 0 |
| planner | 100.00% | 0 (+100% vs no-action) | 0.0 | 171 | 221,100 | 0 |

## demand-spike

| Policy | Service level | Unmet demand (L) | Stockout hours | Allocations | Liters shipped | Failed allocations |
|---|---|---|---|---|---|---|
| no-action | 29.97% | 203,498 | 602.5 | 1 | 1,200 | 0 |
