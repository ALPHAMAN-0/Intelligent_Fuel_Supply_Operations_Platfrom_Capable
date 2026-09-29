"""Structural demand forecaster (DEC-08, docs/model-selection.md).

    mu[s,f,t]    = rate[s,f,hour(t)] * tick_hours * multiplier[s](t)
    sigma[s,f,t] = k[s,f] * mu[s,f,t]

`rate` is liters per hour before the demand multiplier, learned per (station, fuel, hour of day) from mirrored
history as the mean of demand / multiplier, starting from the guide's spec prior and moving to the data as
observations accumulate. `k` is the relative jitter estimated from residuals. Rates are stored per hour (not per
tick) so a model stays valid if TICK_MINUTES changes; the hour of a target tick is taken from its sim_time.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from fuelops.shared import spec
from fuelops.shared.models import DemandPoint, HourStats, ModelInfo, SeriesForecast, WorldState
from fuelops.shared.stats import history_stats
from fuelops.shared.world import WorldIndex, demand_multiplier_at, hour_at


class ForecastParams(BaseModel):
    version: str = "v1"
    kind: str = "structural"
    prior_strength: float = Field(default=4.0, ge=0.0, description="Pseudo-observations per hour bucket for the prior")
    scale_strength: float = Field(default=8.0, ge=0.0, description="Pseudo-observations for the series scale")
    noise_strength: float = Field(default=20.0, ge=0.0, description="Pseudo-observations for the jitter estimate")
    # Optional learned prior: station -> fuel -> 24 hourly rates (liters/hour, before multiplier).
    rates: dict[str, dict[str, list[float]]] | None = None
    trained_on: dict | None = None


def load_params(models_dir: str, version: str) -> ForecastParams:
    path = Path(models_dir) / f"forecast-{version}.json"
    if path.exists():
        return ForecastParams.model_validate(json.loads(path.read_text(encoding="utf-8")))
    return ForecastParams(version=version)


@dataclass
class SeriesModel:
    rates: list[float]
    noise: float
    n_obs: int
    source: str


@dataclass
class FittedModel:
    params: ForecastParams
    series: dict[tuple[str, str], SeriesModel] = field(default_factory=dict)

    @property
    def observations(self) -> int:
        return sum(s.n_obs for s in self.series.values())

    def noise_floor(self) -> float:
        """Expected MAPE from jitter alone (mean absolute deviation of a normal = sigma * sqrt(2/pi))."""
        if not self.series:
            return 0.0
        mean_noise = sum(s.noise for s in self.series.values()) / len(self.series)
        return mean_noise * math.sqrt(2 / math.pi)

    def info(self) -> ModelInfo:
        return ModelInfo(version=self.params.version, kind=self.params.kind, fitted_series=len(self.series),
                         observations=self.observations, noise_floor=round(self.noise_floor(), 4))

    def forecast(self, state: WorldState, horizon: int) -> dict[tuple[str, str], tuple[list[float], list[float]]]:
        tick_hours = state.instance.tick_minutes / 60.0
        t0 = state.instance.tick
        out: dict[tuple[str, str], tuple[list[float], list[float]]] = {}
        for st in state.stations:
            for fuel in spec.FUELS:
                model = self.series.get((st.id, fuel))
                if model is None:
                    continue
                mu, sigma = [], []
                for h in range(1, horizon + 1):
                    t = t0 + h
                    m = model.rates[hour_at(state, t)] * tick_hours * demand_multiplier_at(state, st, t)
                    mu.append(m)
                    sigma.append(model.noise * m)
                out[(st.id, fuel)] = (mu, sigma)
        return out

    def series_forecasts(self, state: WorldState, horizon: int) -> list[SeriesForecast]:
        fc = self.forecast(state, horizon)
        out = []
        for (sid, fuel), (mu, sigma) in fc.items():
            m = self.series[(sid, fuel)]
            out.append(SeriesForecast(station_id=sid, fuel_type=fuel, first_tick=state.instance.tick + 1,
                                      mu=[round(v, 3) for v in mu], sigma=[round(v, 3) for v in sigma],
                                      source=m.source, n_obs=m.n_obs, noise=round(m.noise, 4)))
        return out


class StructuralForecaster:
    def __init__(self, params: ForecastParams | None = None):
        self.params = params or ForecastParams()

    def _prior_rates(self, state: WorldState, idx: WorldIndex, station_id: str, fuel: str) -> list[float]:
        learned = (self.params.rates or {}).get(station_id, {}).get(fuel)
        if learned and len(learned) == 24:
            return [float(v) for v in learned]
        st = idx.stations[station_id]
        region = idx.region_factor.get(st.region_id, 1.0)
        # Liters per hour = per-tick prior for a 60-minute tick.
        return [spec.prior_per_tick(st.demand_profile, fuel, h, 60, region) for h in range(24)]

    def fit(self, state: WorldState, history: list[DemandPoint] | None = None,
            stats: list[HourStats] | None = None) -> FittedModel:
        """Fit from sufficient statistics (preferred: tiny payload) or from raw history."""
        idx = WorldIndex(state)
        tick_hours = state.instance.tick_minutes / 60.0
        ticks_per_day = max(1, int(round(24 / tick_hours)))
        p = self.params
        if not stats and history:
            stats = history_stats(history, state.instance.tick_minutes)
        by_series: dict[tuple[str, str], dict[int, HourStats]] = defaultdict(dict)
        for s in stats or []:
            by_series[(s.station_id, s.fuel_type)][s.hour] = s

        fitted = FittedModel(params=p)
        for st in state.stations:
            base_noise = spec.noise(st.demand_profile)
            for fuel in spec.FUELS:
                prior = self._prior_rates(state, idx, st.id, fuel)
                hours = by_series.get((st.id, fuel), {})
                n = sum(h.n for h in hours.values())
                prior_mean = sum(prior) / 24 if prior else 0.0
                if n and prior_mean > 0:
                    sum_v = sum(h.sum_rate for h in hours.values())
                    sum_p = sum(prior[h.hour] * h.n for h in hours.values())
                    scale = (sum_v + p.scale_strength * prior_mean) / (sum_p + p.scale_strength * prior_mean)
                else:
                    scale = 1.0

                rates = []
                for h in range(24):
                    hs = hours.get(h)
                    nh, sv = (hs.n, hs.sum_rate) if hs else (0, 0.0)
                    prior_h = scale * prior[h]
                    denom = nh + p.prior_strength
                    rates.append((sv + p.prior_strength * prior_h) / denom if denom > 0 else prior_h)

                # Residual r = v / rate[h] - 1; sums per hour give sum(r) and sum(r^2) without raw points.
                sum_r = sum_r2 = 0.0
                n_r = 0
                for hs in hours.values():
                    L = rates[hs.hour]
                    if L <= 1e-9 or hs.n == 0:
                        continue
                    sum_r += hs.sum_rate / L - hs.n
                    sum_r2 += hs.sum_rate_sq / L**2 - 2 * hs.sum_rate / L + hs.n
                    n_r += hs.n
                var = max(0.0, (sum_r2 - sum_r**2 / n_r) / max(1, n_r - 1)) if n_r > 1 else 0.0
                k = math.sqrt((n_r * var + p.noise_strength * base_noise**2) / (n_r + p.noise_strength)) \
                    if (n_r + p.noise_strength) > 0 else base_noise
                source = "learned" if n >= ticks_per_day else ("prior+data" if n else "prior")
                fitted.series[(st.id, fuel)] = SeriesModel(rates=rates, noise=k, n_obs=n, source=source)
        return fitted


def export_rates(model: FittedModel) -> dict[str, dict[str, list[float]]]:
    out: dict[str, dict[str, list[float]]] = defaultdict(dict)
    for (sid, fuel), s in model.series.items():
        out[sid][fuel] = [round(v, 4) for v in s.rates]
    return dict(out)
