"""In-memory world state: latest snapshot, demand-history window, first-seen supply values and forecast accuracy."""

from __future__ import annotations

import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from fuelops.shared.metrics import FORECAST_MAE, FORECAST_MAPE
from fuelops.shared.models import CycleResponse, DemandPoint, HourStats, SeriesForecast, WorldState
from fuelops.shared.stats import history_stats

HORIZONS = (1, 4, 16)


@dataclass
class ForecastTracker:
    """Scores forecasts once the actual demand arrives; also yields 1-step residual z-scores for anomaly detection."""

    window: int = 600
    pending: dict[tuple[str, str, int, int], tuple[float, float]] = field(default_factory=dict)
    errors: dict[int, deque] = field(default_factory=lambda: {h: deque(maxlen=600) for h in HORIZONS})
    last_z: dict[tuple[str, str], float] = field(default_factory=dict)

    def record(self, forecasts: list[SeriesForecast]) -> None:
        for s in forecasts:
            for h in HORIZONS:
                if h <= len(s.mu):
                    key = (s.station_id, s.fuel_type, s.first_tick + h - 1, h)
                    self.pending.setdefault(key, (s.mu[h - 1], s.sigma[h - 1]))

    def observe(self, points: list[DemandPoint]) -> dict[tuple[str, str], float]:
        z_scores: dict[tuple[str, str], float] = {}
        for p in points:
            for h in HORIZONS:
                pred = self.pending.pop((p.station_id, p.fuel_type, p.tick, h), None)
                if pred is None:
                    continue
                mu, sigma = pred
                self.errors[h].append((abs(p.demand - mu), p.demand))
                if h == 1 and sigma > 1e-9:
                    z = (p.demand - mu) / sigma
                    z_scores[(p.station_id, p.fuel_type)] = z
                    self.last_z[(p.station_id, p.fuel_type)] = z
        if points:
            oldest = min(p.tick for p in points) - 200
            for key in [k for k in self.pending if k[2] < oldest]:
                del self.pending[key]
        self._export()
        return z_scores

    def mae(self, h: int) -> float | None:
        e = self.errors[h]
        return sum(a for a, _ in e) / len(e) if e else None

    def mape(self, h: int) -> float | None:
        e = [(a, y) for a, y in self.errors[h] if y > 1e-6]
        return sum(a / y for a, y in e) / len(e) if e else None

    def summary(self) -> dict[str, dict[str, float | int | None]]:
        return {f"{h}": {"mae": _r(self.mae(h)), "mape": _r(self.mape(h)), "samples": len(self.errors[h])}
                for h in HORIZONS}

    def _export(self) -> None:
        for h in HORIZONS:
            mae, mape = self.mae(h), self.mape(h)
            if mae is not None:
                FORECAST_MAE.labels(str(h)).set(mae)
            if mape is not None:
                FORECAST_MAPE.labels(str(h)).set(mape)


def _r(v: float | None) -> float | None:
    return None if v is None or math.isnan(v) else round(v, 4)


class WorldCache:
    def __init__(self, window_ticks: int = 672):
        self.window_ticks = window_ticks
        self.state: WorldState | None = None
        self.previous: WorldState | None = None
        self.run_id = 0
        self.history: deque[DemandPoint] = deque()
        self.last_history_id = 0
        self.last_history_tick = -1
        self.first_seen_supply: dict[str, tuple[int, float]] = {}
        self.multipliers: dict[str, dict[int, float]] = defaultdict(dict)
        self.tracker = ForecastTracker()
        self.cycle: CycleResponse | None = None
        self.cycle_source: str = "none"
        self.cycle_tick: int | None = None
        self.last_sync_at: float | None = None
        self.last_sync_ok = False
        self.last_sync_error: str | None = None
        self.sync_count = 0
        self.degraded: list[str] = []

    # ------------------------------------------------------------------ runs

    def reset_run(self, run_id: int) -> None:
        self.run_id = run_id
        self.state = None
        self.previous = None
        self.history.clear()
        self.last_history_id = 0
        self.last_history_tick = -1
        self.first_seen_supply.clear()
        self.multipliers.clear()
        self.tracker = ForecastTracker()
        self.cycle = None
        self.cycle_tick = None

    # ---------------------------------------------------------------- update

    @property
    def tick(self) -> int | None:
        return self.state.instance.tick if self.state else None

    def update_state(self, state: WorldState) -> None:
        self.previous, self.state = self.state, state
        tick = state.instance.tick
        for st in state.stations:
            series = self.multipliers[st.id]
            series[tick] = st.demand_multiplier
            if len(series) > 2 * self.window_ticks:
                for t in sorted(series)[: len(series) - self.window_ticks]:
                    del series[t]
        for sa in state.supply_arrivals:
            self.first_seen_supply.setdefault(sa.id, (sa.planned_tick, sa.quantity))
        self.last_sync_at = time.time()
        self.sync_count += 1

    def multiplier_at(self, station_id: str, tick: int) -> float:
        series = self.multipliers.get(station_id)
        if not series:
            return 1.0
        if tick in series:
            return series[tick]
        earlier = [t for t in series if t <= tick]
        return series[max(earlier)] if earlier else series[min(series)]

    def add_history(self, points: list[DemandPoint]) -> None:
        for p in points:
            self.history.append(p)
            self.last_history_tick = max(self.last_history_tick, p.tick)
        cutoff = self.last_history_tick - self.window_ticks
        while self.history and self.history[0].tick <= cutoff:
            self.history.popleft()

    def stats(self) -> list[HourStats]:
        if not self.history or self.state is None:
            return []
        return history_stats(list(self.history), self.state.instance.tick_minutes)

    def recent_history(self, station_id: str | None = None, ticks: int = 96) -> list[DemandPoint]:
        if not self.history:
            return []
        cutoff = self.last_history_tick - ticks
        return [p for p in self.history if p.tick > cutoff and (station_id is None or p.station_id == station_id)]

    def first_seen_quantity(self) -> dict[str, float]:
        return {k: v[1] for k, v in self.first_seen_supply.items()}
