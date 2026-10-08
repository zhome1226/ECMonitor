from __future__ import annotations

from typing import Any

from ecmonitor.fulltext_extraction.models import (
    ChemicalMatch,
    ChemicalResolution,
    EvidenceChunk,
    ValidationDecision,
)
from ecmonitor.fulltext_extraction.quality import (
    PolicyGatedEvidenceValidator,
    retry_is_repairable_by_extraction,
)


class AcceptingDelegate:
    validator_name = "accepting"

    def __init__(self) -> None:
        self.calls = 0

    def validate(
        self,
        candidate: dict[str, Any],
        *,
        chunk: EvidenceChunk,
        resolutions: tuple[ChemicalResolution, ...],
    ) -> ValidationDecision:
        del candidate, chunk, resolutions
        self.calls += 1
        return ValidationDecision(action="accept", reason_codes=("model_evidence_verified",))




class BatchAcceptingDelegate(AcceptingDelegate):
    def __init__(self) -> None:
        super().__init__()
        self.batch_calls = 0

    def validate_batch(
        self,
        candidates: list[dict[str, Any]],
        *,
        chunk: EvidenceChunk,
        resolutions_by_candidate: list[tuple[ChemicalResolution, ...]],
    ) -> list[ValidationDecision]:
        del chunk, resolutions_by_candidate
        self.batch_calls += 1
        return [
            ValidationDecision(action="accept", reason_codes=("model_evidence_verified",))
            for _ in candidates
        ]


def _chunk() -> EvidenceChunk:
    return EvidenceChunk(
        chunk_id="c1",
        document_id="d1",
        ordinal=0,
        chunk_type="section_text",
        text="PFOA was measured at 12 ng/L.",
        page_start=1,
        page_end=1,
        source_block_ids=("b1",),
    )


def _resolution() -> ChemicalResolution:
    return ChemicalResolution(
        raw_name="PFOA",
        normalized_query="pfoa",
        status="resolved",
        resolver_name="test",
        matches=(
            ChemicalMatch(
                source="test",
                source_record_id="9554",
                canonical_name="Perfluorooctanoic acid",
                matched_alias="PFOA",
            ),
        ),
    )


def test_pilot_policy_converts_model_accept_to_human_review() -> None:
    delegate = AcceptingDelegate()
    validator = PolicyGatedEvidenceValidator(delegate, require_pilot_human_signoff=True)

    decision = validator.validate(
        {
            "analyte": {
                "raw_name": "PFOA",
                "specificity_status": "individual_substance",
                "is_individual_chemical": True,
            }
        },
        chunk=_chunk(),
        resolutions=(_resolution(),),
    )

    assert delegate.calls == 1
    assert decision.action == "escalate"
    assert decision.human_review_required is True
    assert "pilot_human_signoff_required" in decision.reason_codes


def test_policy_rejects_general_term_before_calling_model() -> None:
    delegate = AcceptingDelegate()
    validator = PolicyGatedEvidenceValidator(delegate)

    decision = validator.validate(
        {
            "analyte": {
                "raw_name": "PFAS",
                "specificity_status": "class_or_family",
                "is_individual_chemical": False,
            }
        },
        chunk=_chunk(),
        resolutions=(_resolution(),),
    )

    assert delegate.calls == 0
    assert decision.action == "reject"
    assert decision.reason_codes == ("not_an_individual_chemical",)


def test_policy_batch_rejects_general_terms_without_sending_them_to_model() -> None:
    delegate = BatchAcceptingDelegate()
    validator = PolicyGatedEvidenceValidator(delegate, require_pilot_human_signoff=True)
    candidates = [
        {
            "analyte": {
                "raw_name": "PFAS",
                "specificity_status": "class_or_family",
                "is_individual_chemical": False,
            }
        },
        {
            "analyte": {
                "raw_name": "PFOA",
                "specificity_status": "individual_substance",
                "is_individual_chemical": True,
            }
        },
    ]

    decisions = validator.validate_batch(
        candidates,
        chunk=_chunk(),
        resolutions_by_candidate=[(_resolution(),), (_resolution(),)],
    )

    assert delegate.batch_calls == 1
    assert decisions[0].action == "reject"
    assert decisions[0].reason_codes == ("not_an_individual_chemical",)
    assert decisions[1].action == "escalate"
    assert decisions[1].human_review_required is True
    assert "pilot_human_signoff_required" in decisions[1].reason_codes


def test_retry_is_repairable_by_extraction_classifies_reasons() -> None:
    assert retry_is_repairable_by_extraction(
        ValidationDecision(
            action="retry",
            reason_codes=("missing_name",),
            failed_json_pointers=("/analyte/raw_name",),
        )
    ) is True
    assert retry_is_repairable_by_extraction(
        ValidationDecision(
            action="retry",
            reason_codes=("value_missing_or_malformed",),
            failed_json_pointers=("/result/raw_value",),
        )
    ) is True


def test_retry_is_not_repairable_for_resolution_gaps() -> None:
    for reason in (
        "chemical_resolution_missing",
        "chemical_identity_unresolved",
        "chemical_identity_conflict",
        "registry_alias_missing",
    ):
        assert retry_is_repairable_by_extraction(
            ValidationDecision(action="retry", reason_codes=(reason,), failed_json_pointers=("/analyte",))
        ) is False, reason


def test_non_repairable_retry_with_mixed_reasons_is_not_repairable() -> None:
    decision = ValidationDecision(
        action="retry",
        reason_codes=("value_missing_or_malformed", "chemical_identity_unresolved"),
        failed_json_pointers=("/result/raw_value", "/analyte"),
    )
    assert retry_is_repairable_by_extraction(decision) is False
