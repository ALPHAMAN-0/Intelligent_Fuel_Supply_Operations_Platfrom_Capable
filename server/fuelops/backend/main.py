"""Backend service: simulator integration, state, decisions, operator API and the static operator console."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from fuelops import __version__
from fuelops.backend.api.routes import router
from fuelops.backend.container import Container, build_container
from fuelops.shared.config import Settings, get_settings
from fuelops.shared.logging import configure_logging, get_logger
from fuelops.shared.metrics import MetricsMiddleware

log = get_logger("backend")


def create_app(settings: Settings | None = None, container: Container | None = None,
               background: bool = True) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging(settings.log_level, "backend")
        c = app.state.container
        await c.start(background=background)
        log.info("backend.started", sim=settings.sim_base_url, intelligence=settings.intelligence_url,
                 mode=c.orchestrator.mode, policy=c.orchestrator.policy, llm=c.explainer.configured)
        try:
            yield
        finally:
            await c.stop()

    app = FastAPI(title="FuelOps Backend", version=__version__, lifespan=lifespan,
                  description="Operator API for the Fuel Supply Intelligence & Resilience Platform (simulated data).")
    app.state.container = container or build_container(settings)
    app.add_middleware(MetricsMiddleware, service="backend", window=app.state.container.latency)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_methods=["*"],
                       allow_headers=["Authorization", "Content-Type"])
    app.include_router(router)

    @app.middleware("http")
    async def security_headers(request: Request, call_next) -> Response:
        # Script/style policy lives in each console page's <meta> CSP; frame-ancestors only works as a header and
        # stops the approve buttons being framed (clickjacking). Kept minimal so /docs (Swagger CDN) still loads.
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        if not request.url.path.startswith("/api/"):
            # Static console files: revalidate every load (cheap 304 via ETag) so an upgrade is never masked by a
            # heuristically cached old build or a cached 404.
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    static = Path(settings.static_dir)
    if static.is_dir() and (static / "index.html").exists():
        app.mount("/", StaticFiles(directory=static, html=True), name="console")
    return app


app = create_app()
