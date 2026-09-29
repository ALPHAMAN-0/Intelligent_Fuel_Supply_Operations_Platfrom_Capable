"""Alert lifecycle (open -> acked -> resolved) and correlation of alerts into incidents."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fuelops.backend.alerts import detectors as det
from fuelops.backend.domain import Alert, Incident, TimelineEntry
from fuelops.backend.state.store import Store
from fuelops.shared.logging import get_logger
from fuelops.shared.metrics import ALERTS_OPEN, SHORTAGE_ALERTS
from fuelops.shared.models import DemandPoint, RiskScore, WorldState

log = get_logger("alerts")
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
STATE_KINDS = {"event", "network", "supply", "shortage", "demand_anomaly", "inventory_anomaly"}

Publisher = Callable[[str, dict[str, Any]], Awaitable[None] | None]
IncidentHook = Callable[[Incident], Awaitable[None] | None]


class AlertManager:
    def __init__(self, store: Store, publish: Publisher | None = None, on_incident: IncidentHook | None = None):
        self.store = store
        self.publish = publish
        self.on_incident = on_incident
        self.run_id = 0
        self.alerts: dict[str, Alert] = {}
        self.incidents: dict[str, Incident] = {}
        self.memory = det.AnomalyState()
        self.tick = 0

    def reset(self, run_id: int) -> None:
        self.run_id = run_id
        self.alerts.clear()
        self.incidents.clear()
        self.memory = det.AnomalyState()

    def restore(self, alerts: list[Alert], incidents: list[Incident], run_id: int) -> None:
        self.run_id = run_id
        self.alerts = {a.id: a for a in alerts}
        self.incidents = {i.id: i for i in incidents}

    # ------------------------------------------------------------- evaluate

    async def evaluate(self, state: WorldState, prev: WorldState | None, risks: list[RiskScore] | None,
                       z_scores: dict[tuple[str, str], float], first_seen: dict[str, tuple[int, float]],
                       new_points: list[DemandPoint]) -> None:
        if state.run_id != self.run_id:
            self.reset(state.run_id)
        self.tick = state.instance.tick
        candidates: list[det.Candidate] = []
        candidates += det.detect_events(state)
        candidates += det.detect_network(state)
        candidates += det.detect_supply(state, first_seen)
        if risks:
            candidates += det.detect_shortages(state, risks)
        candidates += det.detect_demand_anomalies(state, z_scores, self.memory)
        candidates += det.detect_inventory_anomalies(prev, state, new_points, self.memory)

        seen = set()
        for c in candidates:
            seen.add(c.key)
            await self._open_or_update(c)
        for alert in list(self.alerts.values()):
            if alert.status != "resolved" and alert.kind in STATE_KINDS and alert.id not in seen:
                if alert.kind == "shortage" and risks is None:
                    continue  # no fresh risk scores this cycle: keep shortage alerts as they are
                await self._resolve(alert, "condition cleared")
        self._export()

    async def _open_or_update(self, c: det.Candidate) -> Alert:
        existing = self.alerts.get(c.key)
        if existing is not None and existing.status != "resolved":
            changed = existing.severity != c.severity or existing.title != c.title
            existing.severity, existing.title, existing.detail = c.severity, c.title, c.detail
            existing.updated_tick = self.tick
            if changed:
                await self.store.upsert_alert(existing)
                await self._emit("alert.updated", existing)
                inc = self.incidents.get(existing.incident_id or "")
                if inc is not None:
                    self._escalate(inc, existing)
            return existing
        alert = Alert(id=c.key, run_id=self.run_id, kind=c.kind, severity=c.severity, entity_type=c.entity_type,
                      entity_id=c.entity_id, title=c.title, detail=c.detail, opened_tick=self.tick,
                      updated_tick=self.tick)
        self.alerts[c.key] = alert
        if c.kind == "shortage":
            SHORTAGE_ALERTS.inc()
        inc = await self._attach(alert, c.incident_key or c.key, c.incident_title or c.title)
        alert.incident_id = inc.id
        await self.store.upsert_alert(alert)
        log.info("alert.opened", alert=alert.id, severity=alert.severity, incident=inc.id, tick=self.tick)
        await self._emit("alert.opened", alert)
        return alert

    async def _attach(self, alert: Alert, key: str, title: str) -> Incident:
        inc = self.incidents.get(key)
        new = inc is None or inc.status == "resolved"
        if new:
            inc = Incident(id=key, run_id=self.run_id, key=key, title=title, severity=alert.severity,
                           opened_tick=self.tick)
            self.incidents[key] = inc
            inc.timeline.append(TimelineEntry(tick=self.tick, text=f"Incident opened: {title}"))
        if alert.id not in inc.alert_ids:
            inc.alert_ids.append(alert.id)
        inc.timeline.append(TimelineEntry(tick=self.tick, text=f"[{alert.severity}] {alert.title}"))
        self._escalate(inc, alert)
        inc.summary = self._template_summary(inc)
        inc.summary_source = "template"
        await self.store.upsert_incident(inc)
        await self._emit("incident.updated", inc)
        if new:
            log.info("incident.opened", incident=inc.id, tick=self.tick)
            if self.on_incident is not None:
                res = self.on_incident(inc)
                if hasattr(res, "__await__"):
                    await res
        return inc

    def _escalate(self, inc: Incident, alert: Alert) -> None:
        if SEVERITY_RANK[alert.severity] > SEVERITY_RANK[inc.severity]:
            inc.severity = alert.severity

    async def _resolve(self, alert: Alert, reason: str) -> None:
        alert.status = "resolved"
        alert.resolved_tick = self.tick
        alert.updated_tick = self.tick
        await self.store.upsert_alert(alert)
        await self._emit("alert.resolved", alert)
        inc = self.incidents.get(alert.incident_id or "")
        if inc is not None and inc.status == "open":
            inc.timeline.append(TimelineEntry(tick=self.tick, text=f"Resolved ({reason}): {alert.title}"))
            if all(self.alerts[a].status == "resolved" for a in inc.alert_ids if a in self.alerts):
                inc.status = "resolved"
                inc.resolved_tick = self.tick
                inc.timeline.append(TimelineEntry(tick=self.tick, text="Incident resolved: all alerts cleared"))
                log.info("incident.resolved", incident=inc.id, tick=self.tick)
            inc.summary = self._template_summary(inc) if inc.summary_source == "template" else inc.summary
            await self.store.upsert_incident(inc)
            await self._emit("incident.updated", inc)

    # ------------------------------------------------------------- system

    async def raise_system(self, name: str, severity: str, title: str, detail: dict[str, Any] | None = None) -> None:
        await self._open_or_update(det.Candidate(key=f"system:{name}", kind="system", severity=severity, title=title,
                                                 entity_type="component", entity_id=name, detail=detail or {},
                                                 incident_key=f"system:{name}", incident_title=title))
        self._export()

    async def clear_system(self, name: str, reason: str = "recovered") -> None:
        alert = self.alerts.get(f"system:{name}")
        if alert is not None and alert.status != "resolved":
            await self._resolve(alert, reason)
            log.info("recovery.completed", component=name, reason=reason)
        self._export()

    async def ack(self, alert_id: str, actor: str) -> Alert | None:
        alert = self.alerts.get(alert_id)
        if alert is None:
            return None
        if alert.status == "open":
            alert.status = "acked"
            alert.acked_by = actor
            await self.store.upsert_alert(alert)
            await self._emit("alert.updated", alert)
        return alert

    # -------------------------------------------------------------- views

    def list_alerts(self, status: str | None = None) -> list[Alert]:
        items = [a for a in self.alerts.values() if status is None or a.status == status
                 or (status == "active" and a.status != "resolved")]
        return sorted(items, key=lambda a: (a.status == "resolved", -SEVERITY_RANK[a.severity], -a.updated_tick))

    def list_incidents(self, status: str | None = None) -> list[Incident]:
        items = [i for i in self.incidents.values() if status is None or i.status == status]
        return sorted(items, key=lambda i: (i.status == "resolved", -SEVERITY_RANK[i.severity], -i.opened_tick))

    def _template_summary(self, inc: Incident) -> str:
        alerts = [self.alerts[a] for a in inc.alert_ids if a in self.alerts]
        active = [a for a in alerts if a.status != "resolved"]
        kinds = sorted({a.kind for a in alerts})
        state = "resolved" if inc.status == "resolved" else f"{len(active)} active alert(s)"
        lines = [f"{inc.title}. Opened at tick {inc.opened_tick}; {state}; signals: {', '.join(kinds)}."]
        for a in active[:4]:
            lines.append(f"- {a.title}")
        return "\n".join(lines)

    def _export(self) -> None:
        for sev in SEVERITY_RANK:
            ALERTS_OPEN.labels(sev).set(sum(1 for a in self.alerts.values()
                                            if a.status != "resolved" and a.severity == sev))

    async def _emit(self, event: str, obj: Any) -> None:
        if self.publish is None:
            return
        res = self.publish(event, obj.model_dump(mode="json"))
        if hasattr(res, "__await__"):
            await res
