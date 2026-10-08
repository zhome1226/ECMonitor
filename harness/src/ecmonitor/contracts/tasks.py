"""Small, dependency-free workflow task contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

AgentName = Literal["retrieval", "download", "extraction", "validation", "orchestrator", "human"]
TaskStatus = Literal[
    "pending",
    "leased",
    "running",
    "completed",
    "failed_retryable",
    "failed_terminal",
    "cancelled",
]


@dataclass(frozen=True, slots=True)
class TaskEnvelope:
    """Idempotent message passed between workflow components."""

    schema_version: str
    task_id: str
    workflow_run_id: str
    document_id: str | None
    from_agent: AgentName
    to_agent: AgentName
    task_type: str
    idempotency_key: str
    attempt: int
    created_at: str
    status: TaskStatus = "pending"
    correlation_id: str | None = None
    causation_id: str | None = None
    priority: int = 100
    payload: dict[str, Any] = field(default_factory=dict)
    artifact_refs: tuple[str, ...] = ()
    policy_version: str | None = None

    def __post_init__(self) -> None:
        allowed_agents = {"retrieval", "download", "extraction", "validation", "orchestrator", "human"}
        if self.from_agent not in allowed_agents or self.to_agent not in allowed_agents:
            raise ValueError("Unknown workflow agent")
        if not self.schema_version:
            raise ValueError("schema_version is required")
        if not self.task_id or not self.workflow_run_id:
            raise ValueError("task_id and workflow_run_id are required")
        if not self.task_type or not self.idempotency_key:
            raise ValueError("task_type and idempotency_key are required")
        if self.attempt < 1:
            raise ValueError("attempt must be at least 1")
        if self.priority < 0:
            raise ValueError("priority must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["artifact_refs"] = list(self.artifact_refs)
        return payload
