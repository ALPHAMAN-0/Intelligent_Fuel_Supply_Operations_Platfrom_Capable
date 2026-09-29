# Load test report

*SIMULATED DATA.* Brief §17 (R-LOAD-01..03, D-10): load-test a meaningful path, report average, p50/p95/p99 latency,
throughput, error rate, concurrency and resource usage, and explain the behaviour and limits.

## Setup

- Full compose stack on one laptop: Docker Desktop, 8 CPUs, 8 GB (Windows 11). Backend and intelligence are one
  uvicorn worker each (the default image).
- **The system was live during the test**: simulator RUNNING at 1 tick/s, the decision loop in Assisted mode
  planning every tick, SSE connected, Prometheus scraping every 5 s. The load came on top of real work.
- Tool: Locust 2.32.4 in the `loadtest` compose profile ([`loadtest/locustfile.py`](../loadtest/locustfile.py)).
  Raw results in [`loadtest/results/`](../loadtest/results) (CSV + `report.html`); CPU/memory sampled every ~3 s with
  `docker stats` by [`loadtest/sample_resources.py`](../loadtest/sample_resources.py).

## Workload

| User class | Share | Think time | What it does |
|---|---|---|---|
| `ConsoleViewer` | 4 of 5 users | 1–3 s | What an open operator console polls: `/api/state` (weight 5), `/api/health` (3), `/api/risks` (3), `/api/alerts` (2), `/api/plans/current` (2), `/api/supply`, `/api/demand/regional`, `/api/stations/{id}` (1 each) |
| `DecisionMaker` | 1 of 5 users | 2–5 s | The decision path: `POST /api/evaluate` (LP what-if, weight 3) and token-gated `POST /api/plans/compute` (a full decision cycle, weight 1) |

Two runs:

1. **Realistic ramp** (`docker compose --profile loadtest run --rm locust`): 10 → 25 → 50 → 100 concurrent users,
   45–60 s per step, 210 s total.
2. **Saturation** (`LOAD_SHAPE=constant LOAD_THINK_SCALE=0`, 50 users, 60 s): every user fires back-to-back, to find
   the ceiling.

## Results: realistic ramp

Zero failures in 4,779 requests. Steady-state per step (from `run_stats_history.csv`, ramp seconds excluded):

| Concurrent users | Throughput | p50 | p95 | p99 | Errors |
|---|---|---|---|---|---|
| 10 | 4.6 req/s | 10 ms | 150 ms | 370 ms | 0 |
| 25 | 11.1 req/s | 7 ms | 110 ms | 240 ms | 0 |
| 50 | 22.2 req/s | 7 ms | 94 ms | 210 ms | 0 |
| 100 | 44.9 req/s | 8 ms | 130 ms | 320 ms | 0 |

Whole run, per endpoint (client-side, ms):

| Endpoint | Requests | Avg | p50 | p95 | p99 |
|---|---|---|---|---|---|
| GET /api/state | 1,182 | 21 | 8 | 77 | 260 |
| GET /api/health | 726 | 20 | 6 | 69 | 290 |
| GET /api/risks | 684 | 23 | 5 | 93 | 320 |
| GET /api/plans/current | 488 | 21 | 5 | 98 | 280 |
| GET /api/alerts | 466 | 22 | 6 | 100 | 370 |
| GET /api/supply | 224 | 27 | 6 | 150 | 340 |
| GET /api/stations/{id} | 216 | 34 | 18 | 110 | 380 |
| GET /api/demand/regional | 206 | 20 | 8 | 89 | 270 |
| POST /api/evaluate (LP what-if) | 429 | 83 | 48 | 270 | 590 |
| POST /api/plans/compute (full decision cycle) | 158 | 162 | 130 | 470 | 790 |
| **All** | **4,779** | **32** | **9** | **150** | **400** |

Server-side (Prometheus `fuelops_http_request_duration_seconds`, backend): p50 5 ms / p95 124 ms / p99 290 ms at
100 users; p50 4 ms / p95 132 ms / p99 243 ms at 10 users.

## Results: saturation

50 users, no think time, 60 s: **7,146 requests, 0 failures, 121 req/s**. Client-side p50 300 ms, p95 990 ms,
p99 1.5 s (server-side p50 181 ms / p95 715 ms / p99 1.09 s; the rest is queueing). 52 what-if requests (0.9/s) were
turned away with **429 + Retry-After** by the `EVALUATE_MAX_CONCURRENCY=4` limiter; `plans/compute` returned 409
while a cycle was already running (single-flight), which Locust records as success.

## Resource usage

| Container | Realistic ramp, 100 users (mean / max CPU) | Saturation (mean / max CPU) | Memory |
|---|---|---|---|
| backend | 47% / 77% | **100% / 103%** | 80–90 MiB |
| intelligence | 39% / 95% | 39% / 52% | ~52 MiB |
| simulator | 21% / 36% | 18% / 32% | ~87 MiB |
| prometheus | ~0% | 1% / 2% | ~58 MiB |

(100% = one CPU core.)

## Behaviour and limits

- **Linear up to 100 operators.** Throughput grows in proportion to users (4.6 → 44.9 req/s) while p95 stays around
  100–150 ms and nothing fails. Reads come from the in-memory cache and SQLite, never from the simulator (ADR-006),
  so console load does not reach the simulator at all (its CPU does not rise with load).
- **The ceiling is one CPU core in the backend: ~120 req/s.** At saturation the backend sits at exactly 100% of a
  core (one Python worker) while intelligence, simulator and Prometheus have headroom. With realistic think time
  (~0.45 req/s per console) that is roughly 250 simultaneous open consoles before latency degrades. That is far above
  what an operations room needs, but it is the number to know.
- **Past the ceiling it queues, it does not fail.** Latency rises (p95 ~1 s) but the error rate stays at 0%.
  Expensive work is shed deliberately: extra what-if evaluations get 429 (the LP cannot starve the planner) and
  operator-triggered cycles are single-flight.
- **The decision loop is protected.** During saturation the tick lag stayed at **0** (the backend never fell behind
  the simulator), decisions kept running every tick, and the decision-cycle p95 rose from ~0.2 s to ~1 s (peak
  1.7 s), still inside the 15-minute simulated tick and close to the 1 s wall-clock tick at demo speed.
- **What would move the ceiling.** More uvicorn workers do not work as-is because the cache and orchestrator live in
  process. The next step would be a per-tick response cache for the read endpoints (the world only changes once per
  tick, so `/api/state`, `/api/risks` and friends could be serialised once per tick), then a separate read replica
  process fed by the same SQLite mirror.

## Reproduce

```bash
docker compose up -d --build
python loadtest/sample_resources.py 225 loadtest/results/docker-stats.csv &        # optional resource sampling
docker compose --profile loadtest run --rm locust                                   # realistic ramp
docker compose --profile loadtest run --rm -e LOAD_SHAPE=constant -e LOAD_THINK_SCALE=0 locust \
  -f /mnt/loadtest/locustfile.py --host http://backend:8080 --headless -u 50 -r 25 -t 60s \
  --csv /mnt/loadtest/results/saturation-50u/run --only-summary                     # saturation
```

Set `OPERATOR_TOKEN` in `.env` so the `plans/compute` part of the decision path is exercised (`LOAD_WRITE_WEIGHT=0`
runs read-only).
