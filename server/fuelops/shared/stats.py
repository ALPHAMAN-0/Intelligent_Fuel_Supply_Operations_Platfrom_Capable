"""Sufficient statistics of demand history (shared so the backend never imports the intelligence package)."""

from __future__ import annotations

from collections import defaultdict

from fuelops.shared.models import DemandPoint, HourStats


def history_stats(history: list[DemandPoint], tick_minutes: int) -> list[HourStats]:
    """Collapse raw demand points into per (station, fuel, hour) sums of the multiplier-free hourly rate."""
    tick_hours = tick_minutes / 60.0
    acc: dict[tuple[str, str, int], list[float]] = defaultdict(lambda: [0, 0.0, 0.0])
    for obs in history:
        rate = obs.demand / tick_hours / max(obs.multiplier, 0.01)
        a = acc[(obs.station_id, obs.fuel_type, obs.sim_time.hour)]
        a[0] += 1
        a[1] += rate
        a[2] += rate * rate
    return [HourStats(station_id=s, fuel_type=f, hour=h, n=int(v[0]), sum_rate=v[1], sum_rate_sq=v[2])
            for (s, f, h), v in acc.items()]
