"""In-process test stack: real backend container + simulator test double + intelligence app over ASGI transports.

No network, no Docker, deterministic (seeded demand, manual /admin/step).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from fuelops.backend.container import Container, build_container
from fuelops.intelligence.main import Engine
from fuelops.intelligence.main import create_app as create_intelligence
from fuelops.shared.config import Settings
from fuelops.simfake.app import create_app as create_simfake

MODELS_DIR = str(Path(__file__).resolve().parents[1] / "models")
TOKEN = "test-operator-token"


async def no_sleep(_seconds: float) -> None:
    return None


def down_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("intelligence down (test)", request=request)
    return httpx.MockTransport(handler)


class SwitchableTransport(httpx.AsyncBaseTransport):
    """Transport that can be switched off to simulate a dependency outage mid-test."""

    def __init__(self, inner: httpx.AsyncBaseTransport):
        self.inner = inner
        self.down = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("dependency down (test)", request=request)
        return await self.inner.handle_async_request(request)


@dataclass
class Stack:
    container: Container
    sim_app: object
    intel: SwitchableTransport
    admin: httpx.AsyncClient

    @property
    def world(self):
        return self.sim_app.state.sim.world

    async def step(self, n: int = 1) -> None:
        for _ in range(n):
            r = await self.admin.post("/admin/step")
            r.raise_for_status()
            await self.container.sync.sync_once()

    async def close(self) -> None:
        await self.admin.aclose()
        await self.container.stop()


async def make_stack(tmp_path: Path, **overrides) -> Stack:
    llm_client = overrides.pop("llm_client", None)
    sim_app = create_simfake(speed=100, running=False, background=False, seed=overrides.pop("seed", 12345))
    intel_app = create_intelligence(engine=Engine(MODELS_DIR, "v1"))
    intel = SwitchableTransport(httpx.ASGITransport(app=intel_app))
    settings = Settings(
        sim_base_url="http://sim", intelligence_url="http://intel",
        database_path=str(tmp_path / "fuelops-test.db"), operator_token=TOKEN, sse_enabled=False,
        **{"operating_mode": "auto", **overrides},
    )
    container = build_container(settings, sim_transport=httpx.ASGITransport(app=sim_app), intel_transport=intel,
                                sleep=no_sleep, enable_sse=False, llm_client=llm_client)
    await container.start(background=False)
    admin = httpx.AsyncClient(transport=httpx.ASGITransport(app=sim_app), base_url="http://sim")
    await container.sync.sync_once(force=True)
    return Stack(container=container, sim_app=sim_app, intel=intel, admin=admin)
