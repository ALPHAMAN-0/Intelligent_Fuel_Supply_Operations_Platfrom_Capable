"""Intelligence service: forecast, risk, rolling-horizon plan, what-if. Stateless per request."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from fuelops import __version__
from fuelops.intelligence.forecast import FittedModel, StructuralForecaster, load_params
from fuelops.intelligence.planner import plan
from fuelops.shared.config import get_settings
from fuelops.shared.logging import configure_logging, get_logger
from fuelops.shared.metrics import PLANNER_SOLVE, MetricsMiddleware
from fuelops.shared.models import (
    CycleRequest,
    CycleResponse,
    EvaluateRequest,
    EvaluateResponse,
    ForecastRequest,
    ModelInfo,
    SeriesForecast,
)
from fuelops.shared.risk import evaluate, score_risks
from fuelops.shared.world import supply_etas

log = get_logger("intelligence")


class Engine:
    def __init__(self, models_dir: str, version: str):
        self.params = load_params(models_dir, version)
        self.forecaster = StructuralForecaster(self.params)

    def fit(self, req) -> FittedModel:
        return self.forecaster.fit(req.state, req.history, req.stats)


def create_app(engine: Engine | None = None) -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging(settings.log_level, "intelligence")
        app.state.engine = engine or Engine(settings.models_dir, settings.model_version)
        log.info("intelligence.started", model_version=app.state.engine.params.version)
        yield

    app = FastAPI(title="FuelOps Intelligence", version=__version__, lifespan=lifespan)
    app.add_middleware(MetricsMiddleware, service="intelligence")
    if engine is not None:
        app.state.engine = engine

    def get_engine() -> Engine:
        return app.state.engine

    @app.get("/health")
    async def health():
        return {"status": "ok", "model_version": get_engine().params.version}

    @app.get("/model/info")
    async def model_info():
        p = get_engine().params
        return {"version": p.version, "kind": p.kind, "has_learned_prior": bool(p.rates),
                "trained_on": p.trained_on, "prior_strength": p.prior_strength}

    @app.post("/forecast", response_model=list[SeriesForecast])
    async def forecast(req: ForecastRequest):
        model = get_engine().fit(req)
        return model.series_forecasts(req.state, req.horizon_ticks)

    @app.post("/cycle", response_model=CycleResponse)
    def cycle(req: CycleRequest):
        # Sync handler: FastAPI runs it in a worker thread so the LP does not block the event loop.
        start = time.perf_counter()
        model = get_engine().fit(req)
        horizon = max(req.params.horizon_ticks, req.params.risk_horizon_ticks)
        fc = model.forecast(req.state, horizon)
        n_obs = {k: s.n_obs for k, s in model.series.items()}
        noise = {k: s.noise for k, s in model.series.items()}
        risks = score_risks(req.state, fc, n_obs, noise)
        conf = {(r.station_id, r.fuel_type): r.confidence for r in risks}
        result = plan(req.state, fc, req.params, conf)
        PLANNER_SOLVE.observe(result.solve_ms / 1000)
        log.info("intelligence.cycle", tick=req.state.instance.tick, lines=len(result.lines),
                 status=result.solver_status, solve_ms=result.solve_ms,
                 total_ms=round((time.perf_counter() - start) * 1000, 1))
        return CycleResponse(
            forecasts=model.series_forecasts(req.state, horizon),
            risks=risks,
            supply_etas=supply_etas(req.state),
            plan=result,
            model=model.info(),
        )

    @app.post("/evaluate", response_model=EvaluateResponse)
    def what_if(req: EvaluateRequest):
        model = get_engine().fit(req)
        fc = model.forecast(req.state, req.params.horizon_ticks)
        try:
            return evaluate(req.state, fc, req.lines, req.params.horizon_ticks)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/metrics")
    async def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()

__all__ = ["app", "create_app", "Engine", "ModelInfo"]
