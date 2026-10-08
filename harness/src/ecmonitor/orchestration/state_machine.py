"""Auditable workflow state machine with explicit legal transitions."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from typing import Any


class WorkflowState(StrEnum):
    CREATED = "created"
    RETRIEVING = "retrieving"
    SCREENING = "screening"
    DOWNLOAD_QUEUED = "download_queued"
    DOWNLOADING = "downloading"
    EXTRACTING = "extracting"
    VALIDATING = "validating"
    COMMITTING = "committing"
    PAUSED_HUMAN_REVIEW = "paused_human_review"
    BLOCKED_ACCESS = "blocked_access"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


_ALLOWED: dict[WorkflowState, frozenset[WorkflowState]] = {
    WorkflowState.CREATED: frozenset({WorkflowState.RETRIEVING, WorkflowState.CANCELLED}),
    WorkflowState.RETRIEVING: frozenset(
        {WorkflowState.SCREENING, WorkflowState.FAILED_RETRYABLE, WorkflowState.FAILED_TERMINAL, WorkflowState.CANCELLED}
    ),
    WorkflowState.SCREENING: frozenset(
        {WorkflowState.DOWNLOAD_QUEUED, WorkflowState.COMPLETED, WorkflowState.PAUSED_HUMAN_REVIEW, WorkflowState.FAILED_RETRYABLE, WorkflowState.FAILED_TERMINAL}
    ),
    WorkflowState.DOWNLOAD_QUEUED: frozenset(
        {WorkflowState.DOWNLOADING, WorkflowState.CANCELLED, WorkflowState.FAILED_TERMINAL}
    ),
    WorkflowState.DOWNLOADING: frozenset(
        {WorkflowState.EXTRACTING, WorkflowState.BLOCKED_ACCESS, WorkflowState.FAILED_RETRYABLE, WorkflowState.FAILED_TERMINAL, WorkflowState.CANCELLED}
    ),
    WorkflowState.BLOCKED_ACCESS: frozenset(
        {WorkflowState.DOWNLOAD_QUEUED, WorkflowState.PAUSED_HUMAN_REVIEW, WorkflowState.CANCELLED}
    ),
    WorkflowState.EXTRACTING: frozenset(
        {WorkflowState.VALIDATING, WorkflowState.FAILED_RETRYABLE, WorkflowState.FAILED_TERMINAL, WorkflowState.CANCELLED}
    ),
    WorkflowState.VALIDATING: frozenset(
        {
            WorkflowState.COMMITTING,
            WorkflowState.DOWNLOAD_QUEUED,
            WorkflowState.EXTRACTING,
            WorkflowState.RETRIEVING,
            WorkflowState.PAUSED_HUMAN_REVIEW,
            WorkflowState.FAILED_RETRYABLE,
            WorkflowState.FAILED_TERMINAL,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.PAUSED_HUMAN_REVIEW: frozenset(
        {WorkflowState.VALIDATING, WorkflowState.COMMITTING, WorkflowState.DOWNLOAD_QUEUED, WorkflowState.EXTRACTING, WorkflowState.CANCELLED}
    ),
    WorkflowState.COMMITTING: frozenset(
        {WorkflowState.COMPLETED, WorkflowState.FAILED_RETRYABLE, WorkflowState.FAILED_TERMINAL}
    ),
    WorkflowState.FAILED_RETRYABLE: frozenset(
        {WorkflowState.RETRIEVING, WorkflowState.DOWNLOAD_QUEUED, WorkflowState.EXTRACTING, WorkflowState.VALIDATING, WorkflowState.CANCELLED}
    ),
    WorkflowState.FAILED_TERMINAL: frozenset(),
    WorkflowState.COMPLETED: frozenset(),
    WorkflowState.CANCELLED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class TransitionEvent:
    from_state: WorkflowState
    to_state: WorkflowState
    occurred_at: str
    actor: str
    reason_code: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["from_state"] = self.from_state.value
        payload["to_state"] = self.to_state.value
        return payload


@dataclass(frozen=True, slots=True)
class WorkflowSnapshot:
    workflow_run_id: str
    state: WorkflowState = WorkflowState.CREATED
    revision: int = 0
    history: tuple[TransitionEvent, ...] = ()

    def transition(
        self,
        to_state: WorkflowState,
        *,
        occurred_at: str,
        actor: str,
        reason_code: str,
        expected_revision: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> WorkflowSnapshot:
        if expected_revision is not None and expected_revision != self.revision:
            raise RuntimeError(
                f"stale workflow revision: expected {expected_revision}, current {self.revision}"
            )
        if to_state not in _ALLOWED[self.state]:
            raise ValueError(f"illegal workflow transition: {self.state.value} -> {to_state.value}")
        event = TransitionEvent(
            from_state=self.state,
            to_state=to_state,
            occurred_at=occurred_at,
            actor=actor,
            reason_code=reason_code,
            metadata={} if metadata is None else dict(metadata),
        )
        return replace(
            self,
            state=to_state,
            revision=self.revision + 1,
            history=(*self.history, event),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_run_id": self.workflow_run_id,
            "state": self.state.value,
            "revision": self.revision,
            "history": [event.to_dict() for event in self.history],
        }
