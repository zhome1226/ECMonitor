from __future__ import annotations

from ecmonitor.fulltext_extraction.models import ValidationDecision
from ecmonitor.validation_specialist import finalize_candidate_validation


def test_validation_boundary_emits_policy_backed_scope_rejection() -> None:
    outcome = finalize_candidate_validation(
        {"sample": {"matrix": "wastewater_effluent"}},
        ValidationDecision(action="reject", reason_codes=("not_surface_water",)),
    )
    assert outcome.terminal_status == "rejected_scope"
    assert outcome.coarse_disposition == "rejected"
    assert outcome.output_stream == "rejected_audit"
    assert outcome.policy_rule_id == "SCOPE-01"
    assert not outcome.human_review_required
