from __future__ import annotations

import pytest

from ecmonitor.orchestration.router import route_validation_outcome
from ecmonitor.orchestration.state_machine import WorkflowSnapshot, WorkflowState


def test_happy_path_state_transitions_are_auditable() -> None:
    snapshot = WorkflowSnapshot("run-1")
    for state in (
        WorkflowState.RETRIEVING,
        WorkflowState.SCREENING,
        WorkflowState.DOWNLOAD_QUEUED,
        WorkflowState.DOWNLOADING,
        WorkflowState.EXTRACTING,
        WorkflowState.VALIDATING,
        WorkflowState.COMMITTING,
        WorkflowState.COMPLETED,
    ):
        snapshot = snapshot.transition(
            state,
            occurred_at="2026-08-25T00:00:00Z",
            actor="orchestrator",
            reason_code="test",
            expected_revision=snapshot.revision,
        )
    assert snapshot.state is WorkflowState.COMPLETED
    assert snapshot.revision == 8
    assert len(snapshot.history) == 8


def test_illegal_transition_is_rejected() -> None:
    with pytest.raises(ValueError, match="illegal workflow transition"):
        WorkflowSnapshot("run-1").transition(
            WorkflowState.COMPLETED,
            occurred_at="2026-08-25T00:00:00Z",
            actor="orchestrator",
            reason_code="skip",
        )


def test_validation_feedback_routes_to_bounded_targeted_extraction() -> None:
    route = route_validation_outcome("deferred_source_binding", targeted_attempts=0)
    assert route.target == "extraction"
    assert route.retryable


def test_exhausted_targeted_retry_routes_to_human() -> None:
    route = route_validation_outcome("deferred_matrix_binding", targeted_attempts=2)
    assert route.target == "human_review"
    assert not route.retryable


def test_systematic_gap_routes_to_versioned_retrieval_patch() -> None:
    route = route_validation_outcome(
        "rejected_scope",
        reason_codes=("RETRIEVAL-GAP-01",),
    )
    assert route.target == "retrieval"
