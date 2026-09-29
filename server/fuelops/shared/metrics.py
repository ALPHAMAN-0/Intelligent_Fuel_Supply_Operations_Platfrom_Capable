"""Prometheus metrics (prefix fuelops_). Each service process exposes the ones it updates on /metrics."""

from __future__ import annotations

import time
from collections import deque
from threading import Lock

from prometheus_client import Counter, Gauge, Histogram
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

# Application
HTTP_REQUESTS = Counter("fuelops_http_requests_total", "HTTP requests", ["service", "route", "method", "status"])
HTTP_LATENCY = Histogram("fuelops_http_request_duration_seconds", "HTTP request latency", ["service", "route", "method"],
                         buckets=LATENCY_BUCKETS)

# Simulator integration
SIM_REQUESTS = Counter("fuelops_sim_requests_total", "Simulator API calls", ["endpoint", "outcome"])
SIM_LATENCY = Histogram("fuelops_sim_request_duration_seconds", "Simulator API latency", ["endpoint"],
                        buckets=LATENCY_BUCKETS)
SIM_BREAKER_STATE = Gauge("fuelops_sim_breaker_state", "Circuit breaker state (0 closed, 1 half-open, 2 open)", ["group"])
SIM_STALE = Gauge("fuelops_sim_stale", "1 when the simulator reports stale data")
SIM_TICK = Gauge("fuelops_sim_tick", "Latest simulator tick seen")
TICK_LAG = Gauge("fuelops_tick_lag", "Simulator tick minus last processed tick")
SSE_CONNECTED = Gauge("fuelops_sse_connected", "1 when the SSE stream is connected")
SSE_POLLING = Gauge("fuelops_sse_polling", "1 when running on the polling fallback")
SSE_RECONNECTS = Counter("fuelops_sse_reconnects_total", "SSE reconnect attempts")
SIM_INVALID_RESPONSES = Counter("fuelops_sim_invalid_responses_total", "Simulator payloads rejected by validation",
                                ["endpoint"])

# Intelligence / decisions
DECISION_CYCLES = Counter("fuelops_decision_cycles_total", "Decision cycles", ["policy", "outcome"])
DECISION_DURATION = Histogram("fuelops_decision_cycle_duration_seconds", "Decision cycle duration",
                              buckets=LATENCY_BUCKETS)
PLANNER_SOLVE = Histogram("fuelops_planner_solve_seconds", "LP solve time", buckets=LATENCY_BUCKETS)
FALLBACK_ACTIVATIONS = Counter("fuelops_fallback_activations_total", "Heuristic fallback activations", ["reason"])
INTELLIGENCE_UP = Gauge("fuelops_intelligence_up", "1 when the last intelligence call succeeded")
FORECAST_MAE = Gauge("fuelops_forecast_mae", "Rolling forecast MAE (liters per tick)", ["horizon"])
FORECAST_MAPE = Gauge("fuelops_forecast_mape", "Rolling forecast MAPE (ratio)", ["horizon"])
FORECAST_NOISE_FLOOR = Gauge("fuelops_forecast_noise_floor", "Expected MAPE from simulator jitter alone")
PLAN_CONFIDENCE = Gauge("fuelops_plan_confidence", "Confidence of the latest plan")
PLAN_LINES = Counter("fuelops_plan_lines_total", "Plan lines by final status", ["status"])
ALLOCATIONS_SUBMITTED = Counter("fuelops_allocations_submitted_total", "Allocation submissions", ["outcome"])
SHORTAGE_ALERTS = Counter("fuelops_shortage_alerts_total", "Shortage alerts raised")
ALERTS_OPEN = Gauge("fuelops_alerts_open", "Open alerts", ["severity"])
LLM_REQUESTS = Counter("fuelops_llm_requests_total", "Explanation requests", ["outcome"])
LLM_LATENCY = Histogram("fuelops_llm_latency_seconds", "LLM latency", buckets=(0.5, 1, 2, 4, 8, 16, 30))
DB_UP = Gauge("fuelops_db_up", "1 when the last database operation succeeded")

# Business (copied from /v1/metrics and our risk scores)
SERVICE_LEVEL = Gauge("fuelops_service_level", "Simulator service level (served / (served + unmet))")
UNMET_LITERS = Gauge("fuelops_unmet_demand_liters", "Cumulative unmet demand (liters)")
SERVED_LITERS = Gauge("fuelops_served_demand_liters", "Cumulative served demand (liters)")
ALLOCATION_FAILURES = Gauge("fuelops_allocation_failures", "Allocations FAILED in the simulator")
STATIONS_AT_RISK = Gauge("fuelops_stations_at_risk", "(station, fuel) series with P(stockout 12h) >= 0.5")


class LatencyWindow:
    """Rolling window of recent request latencies/outcomes for the health board (p95, error rate)."""

    def __init__(self, size: int = 2000):
        self._items: deque[tuple[float, bool]] = deque(maxlen=size)
        self._lock = Lock()

    def add(self, seconds: float, error: bool) -> None:
        with self._lock:
            self._items.append((seconds, error))

    def snapshot(self) -> dict[str, float | int]:
        with self._lock:
            items = list(self._items)
        if not items:
            return {"count": 0, "p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0, "error_rate": 0.0}
        lat = sorted(s for s, _ in items)

        def pct(p: float) -> float:
            return lat[min(len(lat) - 1, int(p * len(lat)))] * 1000

        return {
            "count": len(items),
            "p50_ms": round(pct(0.50), 2),
            "p95_ms": round(pct(0.95), 2),
            "p99_ms": round(pct(0.99), 2),
            "error_rate": round(sum(1 for _, e in items if e) / len(items), 4),
        }


class MetricsMiddleware(BaseHTTPMiddleware):
    """Records request count/latency with the route template (not the raw path) as label."""

    def __init__(self, app, service: str, window: LatencyWindow | None = None):
        super().__init__(app)
        self.service = service
        self.window = window

    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            elapsed = time.perf_counter() - start
            route = request.scope.get("route")
            template = getattr(route, "path", "unmatched")
            if template not in ("/metrics", "/api/stream"):
                HTTP_REQUESTS.labels(self.service, template, request.method, str(status)).inc()
                HTTP_LATENCY.labels(self.service, template, request.method).observe(elapsed)
                if self.window is not None:
                    self.window.add(elapsed, status >= 500)
