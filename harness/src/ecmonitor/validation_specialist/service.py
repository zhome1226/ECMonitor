"""Convert validator decisions into versioned terminal outcomes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ecmonitor.fulltext_extraction.models import TerminalStatus, ValidationDecision
from ecmonitor.fulltext_extraction.policy import (
    classify_terminal_status,
    coarse_disposition_for_terminal,
    materialize_censoring,
    output_stream_for_terminal,
    policy_rule_for_terminal,
    terminal_requires_human_review,
)


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    candidate: dict[str, Any]
    terminal_status: TerminalStatus
    coarse_disposition: str
    output_stream: str
    policy_rule_id: str
    reason_codes: tuple[str, ...]
    human_review_required: bool

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reason_codes"] = list(self.reason_codes)
        return payload


def finalize_candidate_validation(
    candidate: dict[str, Any],
    decision: ValidationDecision,
) -> ValidationOutcome:
    """Apply deterministic policy after independent evidence validation."""
    materialized = materialize_censoring(candidate)
    status = classify_terminal_status(materialized, decision)
    return ValidationOutcome(
        candidate=materialized,
        terminal_status=status,
        coarse_disposition=coarse_disposition_for_terminal(status),
        output_stream=output_stream_for_terminal(status),
        policy_rule_id=policy_rule_for_terminal(status),
        reason_codes=decision.reason_codes,
        human_review_required=terminal_requires_human_review(status, decision),
    )
