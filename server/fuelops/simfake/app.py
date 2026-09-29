"""HTTP surface of the simulator test double: /v1/* (with fault injection), /v1/stream (SSE), /admin/*.

Run: uvicorn fuelops.simfake.app:app --port 8000
Env: SIMULATION_SPEED (ticks/s, default 8), TICK_MINUTES (15), SIMULATOR_START_MODE (paused|running), SIM_SEED.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from fuelops.simfake.world import World

EVENT_TYPES = {"demand_spike", "route_disruption", "station_outage", "depot_constraint", "shipment_delay",
               "supply_shortfall"}
FAULT_TYPES = {"latency", "unavailable", "error_rate", "stale_data", "stream_disconnect"}


class EventIn(BaseModel):
    type: str
    start_tick: int = Field(ge=0)
    duration_ticks: int = Field(gt=0)
    parameters: dict[str, Any] = Field(default_factory=dict)


class FaultIn(BaseModel):
    type: str
    duration_seconds: int = Field(gt=0, le=3600)
    parameters: dict[str, Any] = Field(default_factory=dict)


class AllocationIn(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=150)
    source_depot_id: str
    destination_station_id: str
    route_id: str
    fuel_type: str
    quantity: float = Field(gt=0)


class Sim:
    def __init__(self, speed: float, tick_minutes: int, running: bool, seed: int):
        self.speed = speed
        self.tick_minutes = tick_minutes
        self.seed = seed
        self.world = World(seed=seed, tick_minutes=tick_minutes, status="RUNNING" if running else "PAUSED")
        self.faults: list[dict] = []
        self.subscribers: set[asyncio.Queue] = set()
        self.fault_rng = random.Random(seed + 1)
        self.lock = asyncio.Lock()

    def active_faults(self) -> list[dict]:
        now = time.time()
        for f in self.faults:
            if f["active"] and f["end"] <= now:
                f["active"] = False
        return [f for f in self.faults if f["active"]]

    def publish(self, event: str, data: dict) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait((event, data))
            except asyncio.QueueFull:
                self.subscribers.discard(q)  # silently dropped, like the real simulator

    async def step(self) -> dict:
        async with self.lock:
            changes = self.world.tick_once()
        for a in changes["allocations"]:
            self.publish("allocation.status_changed", a)
        for depot_id in set(changes["depots"]):
            d = self.world.depot(depot_id)
            self.publish("inventory.updated", {"entity_type": "depot", "entity_id": depot_id,
                                               "inventory": d["inventory"]})
        payload = {"tick": self.world.tick, "sim_time": self.world.sim_time.isoformat()}
        self.publish("simulation.tick", payload)
        return payload

    def reset(self) -> None:
        running = self.world.status == "RUNNING"
        self.world = World(seed=self.seed, tick_minutes=self.tick_minutes, status="RUNNING" if running else "PAUSED")
        self.faults.clear()
        self.publish("simulator.notice", {"message": "Simulation reset"})


def create_app(speed: float | None = None, tick_minutes: int | None = None, running: bool | None = None,
               seed: int | None = None, background: bool = True) -> FastAPI:
    sim = Sim(
        speed=speed if speed is not None else float(os.environ.get("SIMULATION_SPEED", "8")),
        tick_minutes=tick_minutes or int(os.environ.get("TICK_MINUTES", "15")),
        running=running if running is not None else os.environ.get("SIMULATOR_START_MODE", "paused") == "running",
        seed=seed if seed is not None else int(os.environ.get("SIM_SEED", "12345")),
    )

    async def runner() -> None:
        while True:
            if sim.world.status == "RUNNING":
                try:
                    await sim.step()
                except Exception as exc:  # noqa: BLE001
                    sim.publish("simulator.notice", {"level": "error", "message": str(exc)})
                await asyncio.sleep(1.0 / max(sim.speed, 0.01))
            else:
                await asyncio.sleep(0.05)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(runner()) if background else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="BUP Fuel Supply Simulator (test double)", lifespan=lifespan)
    app.state.sim = sim

    @app.middleware("http")
    async def faults(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/v1/") or path == "/v1/health":
            return await call_next(request)
        active = sim.active_faults()
        stale = False
        for f in active:
            t = f["type"]
            if t == "latency":
                await asyncio.sleep(float(f["parameters"].get("delay_ms", 500)) / 1000)
            elif t == "unavailable":
                return JSONResponse({"error": {"code": "FAULT_INJECTED",
                                               "message": "Simulator API temporarily unavailable."}}, 503)
            elif t == "error_rate" and sim.fault_rng.random() < float(f["parameters"].get("rate", 0.25)):
                return JSONResponse({"error": {"code": "FAULT_INJECTED", "message": "Injected transient API error."}},
                                    503)
            elif t == "stream_disconnect" and path == "/v1/stream":
                return JSONResponse({"detail": {"code": "FAULT_INJECTED"}}, 503)
            elif t == "stale_data":
                stale = True
        response = await call_next(request)
        if stale and request.method == "GET" and path != "/v1/stream":
            response.headers["X-Simulator-Stale"] = "true"
        return response

    w = lambda: sim.world  # noqa: E731

    @app.get("/v1/health")
    async def health():
        return {"status": "ok", "database": "ok", "simulation": {"status": w().status, "tick": w().tick}}

    @app.get("/v1/instance")
    async def instance():
        return w().instance()

    @app.get("/v1/regions")
    async def regions():
        return w().regions

    @app.get("/v1/depots")
    async def depots():
        return w().depots

    @app.get("/v1/depots/{entity_id}")
    async def depot(entity_id: str):
        d = w().depot(entity_id)
        return d if d else JSONResponse({"detail": {"code": "NOT_FOUND", "message": "depot not found"}}, 404)

    @app.get("/v1/stations")
    async def stations():
        return w().stations

    @app.get("/v1/stations/{entity_id}")
    async def station(entity_id: str):
        s = w().station(entity_id)
        return s if s else JSONResponse({"detail": {"code": "NOT_FOUND", "message": "station not found"}}, 404)

    @app.get("/v1/routes")
    async def routes():
        return w().routes

    @app.get("/v1/supply-arrivals")
    async def supply():
        return sorted(w().supply, key=lambda s: s["planned_tick"])

    @app.get("/v1/events")
    async def events():
        return sorted(w().events, key=lambda e: -e["id"])

    @app.get("/v1/allocations")
    async def allocations():
        return sorted(w().allocations, key=lambda a: -a["id"])

    @app.get("/v1/demand-history")
    async def demand_history(station_id: str | None = None, limit: int = Query(200)):
        limit = max(1, min(2000, limit))
        rows = [r for r in w().demand if station_id is None or r["station_id"] == station_id]
        return rows[-limit:]

    @app.get("/v1/metrics")
    async def metrics():
        return w().metrics()

    @app.post("/v1/allocations", status_code=201)
    async def create_allocation(body: AllocationIn):
        if body.fuel_type not in ("DIESEL", "PETROL", "OCTANE"):
            return JSONResponse({"detail": [{"msg": "fuel_type must be DIESEL | PETROL | OCTANE"}]}, 422)
        async with sim.lock:
            status, payload = w().create_allocation(body.model_dump())
        if status == 201 and payload.get("created_tick") == w().tick:
            sim.publish("allocation.status_changed", payload)
        return JSONResponse(payload, status)

    @app.post("/v1/allocations/{allocation_id}/cancel")
    async def cancel(allocation_id: int):
        async with sim.lock:
            status, payload = w().cancel_allocation(allocation_id)
        if status == 200:
            sim.publish("allocation.status_changed", payload)
        return JSONResponse(payload, status)

    @app.get("/v1/stream")
    async def stream():
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        sim.subscribers.add(q)

        async def gen():
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        event, data = await asyncio.wait_for(q.get(), timeout=15)
                    except TimeoutError:
                        if q not in sim.subscribers:
                            return
                        yield ": keepalive\n\n"
                        continue
                    yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
            finally:
                sim.subscribers.discard(q)

        return StreamingResponse(gen(), media_type="text/event-stream")

    # ------------------------------------------------------------ admin

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_console():
        m = w().metrics()
        return (f"<html><head><meta http-equiv='refresh' content='2'><title>Simulator (test double)</title></head>"
                f"<body><h1>Simulator test double</h1><p>tick {w().tick} ({w().status}) sim_time "
                f"{w().sim_time.isoformat()}</p><p>service level {m['service_level']:.4f}</p></body></html>")

    @app.post("/admin/run")
    async def run():
        w().status = "RUNNING"
        w().log("admin.run")
        return w().instance()

    @app.post("/admin/pause")
    async def pause():
        w().status = "PAUSED"
        w().log("admin.pause")
        return w().instance()

    @app.post("/admin/toggle")
    async def toggle():
        w().status = "PAUSED" if w().status == "RUNNING" else "RUNNING"
        return w().instance()

    @app.post("/admin/step")
    async def step():
        return await sim.step()

    @app.post("/admin/reset")
    async def reset():
        sim.reset()
        return {"status": "reset", "instance": w().instance()}

    @app.post("/admin/events", status_code=201)
    async def inject_event(body: EventIn):
        if body.type not in EVENT_TYPES:
            return JSONResponse({"detail": [{"msg": f"type must be one of {sorted(EVENT_TYPES)}"}]}, 422)
        async with sim.lock:
            return w().add_event(body.type, body.start_tick, body.duration_ticks, body.parameters)

    @app.post("/admin/faults", status_code=201)
    async def inject_fault(body: FaultIn):
        if body.type not in FAULT_TYPES:
            return JSONResponse({"detail": [{"msg": f"type must be one of {sorted(FAULT_TYPES)}"}]}, 422)
        now = time.time()
        fault = {"id": len(sim.faults) + 1, "type": body.type, "parameters": body.parameters, "active": True,
                 "start": now, "end": now + body.duration_seconds, "duration_seconds": body.duration_seconds}
        sim.faults.append(fault)
        w().log("fault.created", "fault", fault["id"], {"type": body.type})
        return fault

    @app.post("/admin/faults/clear")
    async def clear_faults():
        for f in sim.faults:
            f["active"] = False
        w().log("fault.clear_all")
        return {"status": "cleared"}

    @app.get("/admin/faults")
    async def list_faults():
        sim.active_faults()
        return sorted(sim.faults, key=lambda f: -f["id"])[:50]

    @app.get("/admin/events")
    async def list_events():
        return sorted(w().events, key=lambda e: -e["id"])[:50]

    @app.get("/admin/audit")
    async def audit(limit: int = Query(200)):
        limit = max(1, min(1000, limit))
        return list(reversed(w().audit[-limit:]))

    return app


app = create_app()
