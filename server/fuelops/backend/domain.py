"""Backend records: alerts, incidents, plans, decisions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from fuelops.shared.models import PlanLine, PlanResult

Severity = Literal["info", "warning", "critical"]
Mode = Literal["manual", "assisted", "auto"]


def utcnow() -> datetime:
    return datetime.now(UTC)


class Alert(BaseModel):
    id: str  # dedupe key, e.g. "shortage:station-mirpur:PETROL"
    run_id: int
    kind: str  # event | network | supply | demand_anomaly | inventory_anomaly | shortage | system
    severity: Severity
    entity_type: str | None = None
    entity_id: str | None = None
    title: str
    detail: dict[str, Any] = Field(default_factory=dict)
    status: Literal["open", "acked", "resolved"] = "open"
    opened_tick: int
    updated_tick: int
    resolved_tick: int | None = None
    incident_id: str | None = None
    acked_by: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class TimelineEntry(BaseModel):
    tick: int
    at: datetime = Field(default_factory=utcnow)
    text: str


class Incident(BaseModel):
    id: str
    run_id: int
    key: str
    title: str
    severity: Severity
    status: Literal["open", "resolved"] = "open"
    opened_tick: int
    resolved_tick: int | None = None
    alert_ids: list[str] = Field(default_factory=list)
    timeline: list[TimelineEntry] = Field(default_factory=list)
    summary: str = ""
    summary_source: Literal["template", "llm"] = "template"


class Submission(BaseModel):
    part: int
    quantity: float
    idempotency_key: str
    allocation_id: int | None = None
    status: str = "SUBMITTING"
    http_status: int | None = None
    error: str | None = None


LineStatus = Literal[
    "PROPOSED", "APPROVED", "MODIFIED", "REJECTED", "EXPIRED", "BLOCKED", "SUBMITTED", "SIM_REJECTED",
    "PENDING", "IN_TRANSIT", "ARRIVED", "FAILED", "CANCELLED", "PARTIAL",
]


class PlanLineRecord(PlanLine):
    status: LineStatus = "PROPOSED"
    submissions: list[Submission] = Field(default_factory=list)
    approved_by: str | None = None
    decision_reason: str | None = None
    needs_review_reason: str | None = None


class PlanRecord(BaseModel):
    id: int
    run_id: int
    tick: int
    policy: str
    mode: Mode
    status: Literal["PROPOSED", "PARTIAL", "APPROVED", "REJECTED", "EXPIRED", "EXECUTED", "EMPTY"] = "PROPOSED"
    created_at: datetime = Field(default_factory=utcnow)
    expires_tick: int
    stale_inputs: bool = False
    fallback_reason: str | None = None
    result: PlanResult
    lines: list[PlanLineRecord] = Field(default_factory=list)
    explanation: str = ""
    explanation_source: Literal["template", "llm", "pending"] = "template"

    def refresh_status(self) -> None:
        if not self.lines:
            self.status = "EMPTY"
            return
        states = {ln.status for ln in self.lines}
        if states <= {"REJECTED"}:
            self.status = "REJECTED"
        elif states <= {"EXPIRED", "REJECTED"}:
            self.status = "EXPIRED"
        elif states & {"PROPOSED", "MODIFIED"}:
            self.status = "PARTIAL" if states - {"PROPOSED", "MODIFIED"} else "PROPOSED"
        elif states & {"APPROVED", "SUBMITTED"}:
            self.status = "APPROVED"
        else:
            self.status = "EXECUTED"


class Decision(BaseModel):
    id: int
    run_id: int
    plan_id: int
    line_no: int | None = None
    actor: str  # "operator" | "auto:<mode>" | "system"
    action: str  # approve | reject | modify | expire | cancel | auto_approve | submit
    reason: str | None = None
    tick: int
    at: datetime = Field(default_factory=utcnow)
    detail: dict[str, Any] = Field(default_factory=dict)
