"""Deterministic validation-feedback routing; no model calls or hidden policy."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from ecmonitor.fulltext_extraction.models import TerminalStatus

RouteTarget = Literal["commit", "download", "extraction", "retrieval", "validation", "human_review", "failure"]

_ACCEPTED_OR_REJECTED: frozenset[str] = frozenset(
    {
        "accepted_main",
        "accepted_censored",
        "accepted_tentative",
        "accepted_microplastic_surface_water",
        "rejected_scope",
        "rejected_secondary_source",
        "rejected_treatment_experiment",
        "rejected_non_observation",
        "rejected_non_individual",
        "rejected_source_conflict",
        "rejected_duplicate",
        "completed_zero_in_scope_records",
    }
)


@dataclass(frozen=True, slots=True)
class FeedbackRoute:
    target: RouteTarget
    reason_code: str
    retryable: bool
    requested_context: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["requested_context"] = list(self.requested_context)
        return payload


def route_validation_outcome(
    terminal_status: TerminalStatus,
    *,
    reason_codes: tuple[str, ...] = (),
    human_review_required: bool = False,
    targeted_attempts: int = 0,
    maximum_targeted_attempts: int = 2,
) -> FeedbackRoute:
    """Route a validation outcome without granting the orchestrator scientific authority."""
    if human_review_required:
        return FeedbackRoute("human_review", "explicit_human_review_required", False)
    if any(code.startswith(("RETRIEVAL-GAP", "QUERY-GAP")) for code in reason_codes):
        if targeted_attempts >= maximum_targeted_attempts:
            return FeedbackRoute("human_review", "retrieval_retry_budget_exhausted", False)
        return FeedbackRoute("retrieval", "versioned_query_patch_required", True)
    if terminal_status in _ACCEPTED_OR_REJECTED:
        return FeedbackRoute("commit", "terminal_disposition_ready", False)
    if terminal_status == "deferred_identity_evidence":
        if targeted_attempts >= maximum_targeted_attempts:
            return FeedbackRoute("human_review", "identity_retry_budget_exhausted", False)
        return FeedbackRoute(
            "download",
            "additional_identity_or_supplementary_evidence_required",
            True,
            ("supplementary_information", "analyte_list", "identity_definition"),
        )
    if terminal_status in {"deferred_source_binding", "deferred_matrix_binding"}:
        if targeted_attempts < maximum_targeted_attempts:
            return FeedbackRoute(
                "extraction",
                "targeted_evidence_reextraction",
                True,
                ("target_pages", "table_caption", "headers", "footnotes", "two_dimensional_layout"),
            )
        return FeedbackRoute("human_review", "targeted_retry_budget_exhausted", False)
    if terminal_status == "deferred_geographic_conflict":
        return FeedbackRoute("human_review", "geographic_evidence_conflict", False)
    if terminal_status == "validator_failure":
        if targeted_attempts < maximum_targeted_attempts:
            return FeedbackRoute("validation", "bounded_validator_retry", True)
        return FeedbackRoute("failure", "validator_retry_budget_exhausted", False)
    if terminal_status in {"model_failure", "parser_failure", "invalid_model_output"}:
        if targeted_attempts < maximum_targeted_attempts:
            return FeedbackRoute("extraction", "bounded_extraction_route_retry", True)
        return FeedbackRoute("failure", "extraction_retry_budget_exhausted", False)
    return FeedbackRoute("failure", "unhandled_terminal_status", False)
