"""Constants published in the simulator integration guide (§8.5-8.6).

Used as the forecast prior and by the heuristic fallback. The learned structural model overrides these as soon as
history exists, so small mismatches between the guide and the running simulator do not matter.
"""

from __future__ import annotations

FUELS: tuple[str, ...] = ("DIESEL", "PETROL", "OCTANE")

# Liters per simulated day.
PROFILE_DAILY: dict[str, dict[str, float]] = {
    "urban_high": {"DIESEL": 8500.0, "PETROL": 10500.0, "OCTANE": 5600.0},
    "industrial": {"DIESEL": 14000.0, "PETROL": 4500.0, "OCTANE": 2200.0},
    "highway": {"DIESEL": 10500.0, "PETROL": 11000.0, "OCTANE": 6200.0},
    "regional": {"DIESEL": 7200.0, "PETROL": 7600.0, "OCTANE": 3600.0},
}

PROFILE_NOISE: dict[str, float] = {"urban_high": 0.10, "industrial": 0.08, "highway": 0.12, "regional": 0.10}
DEFAULT_NOISE = 0.10


def _busy(profile: str, hour: int) -> bool:
    if profile == "industrial":
        return 6 <= hour <= 17
    if profile == "highway":
        return 6 <= hour <= 9 or 16 <= hour <= 20
    if profile == "urban_high":
        return 7 <= hour <= 9 or 16 <= hour <= 20
    if profile == "regional":
        return 7 <= hour <= 20
    return 7 <= hour <= 20


_FACTORS: dict[str, tuple[float, float]] = {
    "industrial": (1.55, 0.45),
    "highway": (1.35, 0.75),
    "urban_high": (1.45, 0.70),
    "regional": (1.25, 0.65),
}


def hour_factor(profile: str, hour: int) -> float:
    busy, off = _FACTORS.get(profile, (1.3, 0.7))
    return busy if _busy(profile, hour) else off


def daily_demand(profile: str, fuel: str) -> float:
    if profile in PROFILE_DAILY:
        return PROFILE_DAILY[profile].get(fuel, 0.0)
    # Unknown profile (e.g. a surprise station): average of the known profiles.
    values = [p.get(fuel, 0.0) for p in PROFILE_DAILY.values()]
    return sum(values) / len(values)


def prior_per_tick(profile: str, fuel: str, hour: int, tick_minutes: int, region_factor: float = 1.0) -> float:
    """Spec-prior demand in liters for one tick of `tick_minutes` starting at `hour` (before demand_multiplier)."""
    ticks_per_day = 1440.0 / max(tick_minutes, 1)
    return daily_demand(profile, fuel) / ticks_per_day * region_factor * hour_factor(profile, hour)


def noise(profile: str) -> float:
    return PROFILE_NOISE.get(profile, DEFAULT_NOISE)
