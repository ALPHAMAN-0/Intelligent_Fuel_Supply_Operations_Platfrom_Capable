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
from fuelops.shared.models import DemandPoint, ModelInfo, SeriesForecast, WorldState
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

    def fit(self, state: WorldState, history: list[DemandPoint]) -> FittedModel:
        idx = WorldIndex(state)
        tick_hours = state.instance.tick_minutes / 60.0
        ticks_per_day = max(1, int(round(24 / tick_hours)))
        p = self.params

        grouped: dict[tuple[str, str], list[tuple[int, float]]] = defaultdict(list)
        for obs in history:
            if obs.station_id not in idx.stations:
                continue
            rate = obs.demand / tick_hours / max(obs.multiplier, 0.01)
            grouped[(obs.station_id, obs.fuel_type)].append((obs.sim_time.hour, rate))

        fitted = FittedModel(params=p)
        for st in state.stations:
            base_noise = spec.noise(st.demand_profile)
            for fuel in spec.FUELS:
                prior = self._prior_rates(state, idx, st.id, fuel)
                obs = grouped.get((st.id, fuel), [])
                n = len(obs)
                prior_mean = sum(prior) / 24 if prior else 0.0
                if n and prior_mean > 0:
                    sum_v = sum(v for _, v in obs)
                    sum_p = sum(prior[h] for h, _ in obs)
                    scale = (sum_v + p.scale_strength * prior_mean) / (sum_p + p.scale_strength * prior_mean)
                else:
                    scale = 1.0

                by_hour: dict[int, list[float]] = defaultdict(list)
                for h, v in obs:
                    by_hour[h].append(v)
                rates = []
                for h in range(24):
                    vals = by_hour.get(h, [])
                    nh = len(vals)
                    prior_h = scale * prior[h]
                    rates.append((sum(vals) + p.prior_strength * prior_h) / (nh + p.prior_strength)
                                 if (nh + p.prior_strength) > 0 else prior_h)

                residuals = [v / rates[h] - 1.0 for h, v in obs if rates[h] > 1e-9]
                if residuals:
                    mean_r = sum(residuals) / len(residuals)
                    var = sum((r - mean_r) ** 2 for r in residuals) / max(1, len(residuals) - 1)
                else:
                    var = 0.0
                k = math.sqrt((len(residuals) * var + p.noise_strength * base_noise**2)
                              / (len(residuals) + p.noise_strength))
                source = "learned" if n >= ticks_per_day else ("prior+data" if n else "prior")
                fitted.series[(st.id, fuel)] = SeriesModel(rates=rates, noise=k, n_obs=n, source=source)
        return fitted


def export_rates(model: FittedModel) -> dict[str, dict[str, list[float]]]:
    out: dict[str, dict[str, list[float]]] = defaultdict(dict)
    for (sid, fuel), s in model.series.items():
        out[sid][fuel] = [round(v, 4) for v in s.rates]
    return dict(out)
