"""Wires the backend components together (one instance per process; tests build their own)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from fuelops.backend.alerts.manager import AlertManager
from fuelops.backend.api.broadcast import Broadcaster
from fuelops.backend.decision.actuator import Actuator
from fuelops.backend.decision.intelligence_client import IntelligenceClient
from fuelops.backend.decision.orchestrator import Orchestrator
from fuelops.backend.domain import Incident
from fuelops.backend.explain.llm import Explainer
from fuelops.backend.integration.sim_client import SimClient
from fuelops.backend.integration.sim_errors import SimError
from fuelops.backend.integration.sse_listener import SSEListener
from fuelops.backend.state.cache import WorldCache
from fuelops.backend.state.store import Store
from fuelops.backend.state.sync import SyncService
from fuelops.shared.config import Settings
from fuelops.shared.logging import get_logger
from fuelops.shared.metrics import LatencyWindow

log = get_logger("container")


@dataclass
class Container:
    settings: Settings
    client: SimClient
    store: Store
    cache: WorldCache
    intel: IntelligenceClient
    explainer: Explainer
    broadcaster: Broadcaster
    alerts: AlertManager
    actuator: Actuator
    orchestrator: Orchestrator
    sync: SyncService
    sse: SSEListener | None
    latency: LatencyWindow = field(default_factory=LatencyWindow)
    started_at: float = field(default_factory=time.time)
    sim_alive: bool | None = None
    sim_health_error: str | None = None
    tasks: list[asyncio.Task] = field(default_factory=list)
    _bg: set = field(default_factory=set)

    # ------------------------------------------------------------ lifecycle

    async def start(self, background: bool = True) -> None:
        await self.store.init()
        try:
            restored = await self.store.load_recent()
            if restored["run"]:
                run_id = restored["run"][0]
                self.orchestrator.restore(restored["plans"], restored["decisions"])
                self.alerts.restore(restored["alerts"], restored["incidents"], run_id)
                log.info("container.restored", run_id=run_id, plans=len(restored["plans"]),
                         alerts=len(restored["alerts"]), skipped=sum(restored.get("skipped") or [0]))
        except Exception as exc:  # noqa: BLE001 - a bad restore must never block startup
            log.exception("container.restore_failed", error=str(exc))
        if not background:
            return
        self.tasks.append(asyncio.create_task(self.sync.run(), name="sync"))
        if self.sse is not None:
            self.tasks.append(asyncio.create_task(self.sse.run(), name="sse"))
        self.tasks.append(asyncio.create_task(self._probe_loop(), name="probes"))

    async def stop(self) -> None:
        self.sync.stop()
        if self.sse is not None:
            self.sse.stop()
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.client.aclose()
        await self.intel.aclose()
        self.store.close()

    async def _probe_loop(self, every_s: float = 5.0) -> None:
        while True:
            try:
                await self.client.get_health()
                self.sim_alive, self.sim_health_error = True, None
            except SimError as exc:
                self.sim_alive, self.sim_health_error = False, f"{exc.kind.value}: {exc.message}"
            if self.orchestrator.policy == "planner":
                await self.intel.health()
            await self.store.ping()
            await asyncio.sleep(every_s)

    # ----------------------------------------------------------- incidents

    def on_incident(self, inc: Incident) -> None:
        if not self.explainer.configured or self.explainer.mode == "off":
            return
        task = asyncio.create_task(self._summarize(inc))
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _summarize(self, inc: Incident) -> None:
        alerts = [self.alerts.alerts[a] for a in inc.alert_ids if a in self.alerts.alerts]
        text, source = await self.explainer.summarize_incident(inc, alerts)
        inc.summary, inc.summary_source = text, source  # type: ignore[assignment]
        await self.store.upsert_incident(inc)
        self.broadcaster.publish("incident.updated", inc.model_dump(mode="json"))

    # --------------------------------------------------------------- health

    def health(self) -> dict[str, Any]:
        s = self.settings
        state = self.cache.state
        comps = []

        def comp(name: str, status: str, detail: str, **extra: Any) -> None:
            comps.append({"name": name, "status": status, "detail": detail, **extra})

        comp("backend", "ok", f"up {int(time.time() - self.started_at)} s")
        comp("database", "ok" if self.store.ok else "down",
             "SQLite reachable" if self.store.ok else f"{self.store.last_error}; running from memory")
        if self.sim_alive is None:
            comp("simulator", "unknown", "liveness not probed yet")
        else:
            comp("simulator", "ok" if self.sim_alive else "down",
                 "GET /v1/health ok" if self.sim_alive else (self.sim_health_error or "unreachable"))
        breakers = [self.client.read_breaker.snapshot(), self.client.write_breaker.snapshot(),
                    self.intel.breaker.snapshot()]
        read_open = self.client.read_breaker.state.value != "CLOSED"
        api_status = "down" if read_open else ("degraded" if (state and (state.stale or state.degraded)) else "ok")
        detail = (f"read breaker {self.client.read_breaker.state.value}, write breaker "
                  f"{self.client.write_breaker.state.value}")
        if state is not None and state.stale:
            detail += "; stale data"
        if state is not None and state.degraded:
            detail += f"; degraded: {', '.join(state.degraded)}"
        if self.client.last_error is not None and api_status != "ok":
            detail += f"; last error {self.client.last_error.code or self.client.last_error.kind.value}"
        comp("simulator_api", api_status, detail)
        if self.sse is None:
            comp("event_stream", "degraded", "SSE disabled; polling /v1/instance")
        else:
            comp("event_stream", "ok" if self.sse.connected else "degraded",
                 "SSE connected" if self.sse.connected else f"polling fallback ({self.sse.last_error or 'connecting'})",
                 reconnects=self.sse.reconnects)
        orch = self.orchestrator
        if orch.policy != "planner":
            comp("intelligence", "disabled", "policy set to heuristic")
        else:
            up = self.intel.up
            comp("intelligence", "ok" if up else ("unknown" if up is None else "down"),
                 f"model {self.intel.model_version or '?'}; last call "
                 f"{round(self.intel.last_latency_ms or 0)} ms" if up else (self.intel.last_error or "not called yet"))
        if not orch.enabled:
            comp("decision_engine", "disabled", "decisions disabled")
        else:
            lag = (state.instance.tick - orch.last_cycle_tick) if (state and orch.last_cycle_tick is not None) else None
            status = "degraded" if orch.fallback_active else ("ok" if orch.last_cycle_tick is not None else "unknown")
            if lag is not None and lag > 10 * s.decision_every_n_ticks and state and state.instance.status == "RUNNING":
                status = "down"
            comp("decision_engine", status,
                 f"{orch.last_policy_used or orch.policy} policy, mode {orch.mode}"
                 + (f"; fallback: {orch.fallback_reason}" if orch.fallback_active else "")
                 + (f"; last cycle {orch.last_cycle_ms:.0f} ms" if orch.last_cycle_ms else ""))
        llm = self.explainer.status()
        comp("llm", "disabled" if not llm["configured"] else ("ok" if llm["last_outcome"] in ("ok", "idle")
                                                                else "degraded"),
             "no API key: template explanations" if not llm["configured"]
             else f"{llm['model']} ({llm['last_outcome']})")
        worst = "ok"
        for c in comps:
            if c["name"] in ("simulator", "simulator_api") and c["status"] == "down":
                worst = "down"
            elif c["status"] in ("down", "degraded") and worst == "ok":
                worst = "degraded"
        return {
            "status": worst,
            "components": comps,
            "breakers": breakers,
            "latency": self.latency.snapshot(),
            "sim": {"tick": state.instance.tick, "status": state.instance.status,
                    "sim_time": state.instance.sim_time.isoformat(), "run_id": state.run_id} if state else None,
            "sync": {"ok": self.cache.last_sync_ok, "error": self.cache.last_sync_error,
                     "last_sync_at": self.cache.last_sync_at, "count": self.cache.sync_count,
                     "duration_ms": round(self.sync.last_duration_ms, 1)},
            "stream_clients": self.broadcaster.clients,
        }


def build_container(settings: Settings, *, sim_transport: httpx.AsyncBaseTransport | None = None,
                    intel_transport: httpx.AsyncBaseTransport | None = None, llm_client: Any | None = None,
                    sleep=None, enable_sse: bool | None = None) -> Container:
    broadcaster = Broadcaster()
    store = Store(settings.database_path)
    cache = WorldCache(settings.history_window_ticks)
    alerts_ref: dict[str, AlertManager] = {}

    async def on_invalid(endpoint: str, message: str) -> None:
        mgr = alerts_ref.get("mgr")
        if mgr is not None:
            await mgr.raise_system("invalid_response", "warning",
                                   f"Rejected invalid simulator response from {endpoint}", {"error": message})

    kwargs: dict[str, Any] = {}
    if sleep is not None:
        kwargs["sleep"] = sleep
    client = SimClient(settings.sim_base_url, connect_timeout_s=settings.sim_connect_timeout_s,
                       read_timeout_s=settings.sim_read_timeout_s, retries=settings.sim_retries,
                       breaker_failure_threshold=settings.breaker_failure_threshold,
                       breaker_reset_timeout_s=settings.breaker_reset_timeout_s, transport=sim_transport,
                       on_invalid=on_invalid, **kwargs)
    intel = IntelligenceClient(settings.intelligence_url, settings.intelligence_timeout_s, transport=intel_transport)
    explainer = Explainer(settings, client=llm_client)
    alerts = AlertManager(store, broadcaster.publish)
    alerts_ref["mgr"] = alerts
    actuator = Actuator(client, cache, settings.min_shipment_l)
    orchestrator = Orchestrator(settings, cache, intel, actuator, alerts, store, explainer, broadcaster.publish)
    sync = SyncService(client, cache, store, poll_interval_s=settings.poll_interval_ms / 1000)
    sync.listeners.append(orchestrator.on_state)
    use_sse = settings.sse_enabled if enable_sse is None else enable_sse
    sse = None
    if use_sse:
        def is_running() -> bool:
            return bool(cache.state and cache.state.instance.status == "RUNNING")

        sse = SSEListener(settings.sim_base_url, sync.on_sse_event, is_running=is_running, transport=sim_transport)
        sync.sse_connected = lambda: sse.connected
    container = Container(settings=settings, client=client, store=store, cache=cache, intel=intel,
                          explainer=explainer, broadcaster=broadcaster, alerts=alerts, actuator=actuator,
                          orchestrator=orchestrator, sync=sync, sse=sse)
    alerts.on_incident = container.on_incident
    return container
