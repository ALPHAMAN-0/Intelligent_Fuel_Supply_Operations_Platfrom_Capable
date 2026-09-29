"""Runtime configuration for both services. Every variable is documented in .env.example.

Values come from environment variables (upper-case field names), falling back to a local `.env` file, then to the
defaults below. Implemented with plain pydantic so the services have no extra settings dependency.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr


class Settings(BaseModel):
    # Simulator integration
    sim_base_url: str = "http://localhost:8000"
    sim_connect_timeout_s: float = 2.0
    sim_read_timeout_s: float = 5.0
    sim_retries: int = 3
    breaker_failure_threshold: int = 5
    breaker_reset_timeout_s: float = 10.0
    poll_interval_ms: int = 1000
    sse_enabled: bool = True

    # Storage (SQLite file; see ADR-012)
    database_path: str = "./data/fuelops.db"

    # Intelligence service
    intelligence_url: str = "http://localhost:8090"
    intelligence_timeout_s: float = 3.0
    models_dir: str = "./models"
    model_version: str = "v1"

    # Decisions
    decisions_enabled: bool = True
    planner_policy: Literal["planner", "heuristic"] = "planner"
    operating_mode: Literal["manual", "assisted", "auto"] = "assisted"
    decision_every_n_ticks: int = Field(default=1, ge=1)
    plan_horizon_ticks: int = Field(default=24, ge=4, le=96)
    plan_safety_z: float = Field(default=1.0, ge=0.0, le=3.0)
    plan_ttl_ticks: int = Field(default=2, ge=1)
    min_shipment_l: float = Field(default=1000.0, ge=0.0)
    auto_approve_min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    auto_approve_max_liters: float = Field(default=7000.0, ge=0.0)
    history_window_ticks: int = Field(default=672, ge=96)

    # Explanations (Claude API, template fallback)
    anthropic_api_key: SecretStr | None = None
    anthropic_base_url: str = "https://api.anthropic.com"
    llm_model: str = "claude-opus-5-5"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    llm_max_tokens: int = Field(default=4096, ge=256)
    llm_timeout_s: float = 30.0
    llm_auto_explain: Literal["off", "review", "all"] = "review"

    # Operator access
    operator_token: SecretStr | None = None
    chaos_enabled: bool = True
    cors_origins: str = "http://localhost:8080,http://localhost:5173"
    static_dir: str = "../frontend"

    log_level: str = "INFO"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_settings(env: dict[str, str] | None = None, dotenv: Path | None = Path(".env")) -> Settings:
    source = dict(_read_dotenv(dotenv)) if dotenv else {}
    source.update(env if env is not None else os.environ)
    data = {}
    for name in Settings.model_fields:
        value = source.get(name.upper())
        if value is not None and value != "":
            data[name] = value
    return Settings.model_validate(data)


@lru_cache
def get_settings() -> Settings:
    return load_settings()
