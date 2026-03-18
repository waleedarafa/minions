from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


class TaskCreate(BaseModel):
    """Payload for creating a new task."""

    description: str
    repo: str
    branch_base: str = "main"
    context_links: list[str] = Field(default_factory=list)
    blueprint: str = "default"
    tools: list[str] = Field(default_factory=list)
    rules_override: list[str] = Field(default_factory=list)
    priority: str = "normal"


class Task(TaskCreate):
    """A persisted task with status tracking."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: str = "api"
    status: Literal["pending", "running", "completed", "failed"] = "pending"
    created_by: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RunStatus(BaseModel):
    """Live status of a running task."""

    task_id: str
    run_id: str
    source: str = "api"
    priority: str = "normal"
    created_by: str = ""
    status: str
    current_step: str
    steps_completed: int
    total_steps: int
    started_at: datetime
    tokens_used: int = 0
    hydration_trace: list[dict[str, Any]] = Field(default_factory=list)
    rule_trace: list[dict[str, Any]] = Field(default_factory=list)
    rule_scope_targets: list[str] = Field(default_factory=list)
    ci_rounds: int = 0
    ci_run_id: str | None = None
    ci_conclusion: str | None = None
    ci_summary: str = ""
    sandbox_mode: str = ""
    sandbox_backend: str = ""
    sandbox_network_enabled: bool = False
    sandbox_host_fallback: bool = False
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    security_events: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


class RunResult(BaseModel):
    """Final result of a completed run."""

    task_id: str
    run_id: str
    source: str = "api"
    priority: str = "normal"
    created_by: str = ""
    success: bool
    pr_url: str | None = None
    steps: list[dict[str, Any]] = Field(default_factory=list)
    tokens_used: int = 0
    hydration_trace: list[dict[str, Any]] = Field(default_factory=list)
    rule_trace: list[dict[str, Any]] = Field(default_factory=list)
    rule_scope_targets: list[str] = Field(default_factory=list)
    ci_rounds: int = 0
    ci_run_id: str | None = None
    ci_conclusion: str | None = None
    ci_summary: str = ""
    sandbox_mode: str = ""
    sandbox_backend: str = ""
    sandbox_network_enabled: bool = False
    sandbox_host_fallback: bool = False
    security_events: list[dict[str, Any]] = Field(default_factory=list)
    duration_seconds: float = 0.0
    error: str | None = None
