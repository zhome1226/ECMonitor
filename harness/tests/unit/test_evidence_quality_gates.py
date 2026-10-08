"""Deterministic evidence-quality gates (industry-standard alignment).

Verifies that secondary literature and non-occurrence experiments are rejected, while
exceedance, censored/frequency/empty, identity-specificity, and un-bound numeric cases receive
the intended deterministic decision without wasting a model call.
"""

from __future__ import annotations

from typing import Any

from ecmonitor.fulltext_extraction.models import (
    ChemicalMatch,
    ChemicalResolution,
    EvidenceChunk,
    ValidationDecision,
)
from ecmonitor.fulltext_extraction.quality import (
    ConservativeEvidenceValidator,
    PolicyGatedEvidenceValidator,
    evidence_quality_gate,
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
    ) -> Any:
        del candidate, chunk, resolutions
        self.calls += 1
        from ecmonitor.fulltext_extraction.models import ValidationDecision

        return ValidationDecision(action="accept", reason_codes=("model_evidence_verified",))


def _chunk() -> EvidenceChunk:
    return EvidenceChunk(
        chunk_id="c1",
        document_id="d1",
        ordinal=0,
        chunk_type="section_text",
        text="PFOA was measured at 12 ng/L in river water.",
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


def _candidate(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "observation_type": "field_measurement",
        "analyte": {
            "raw_name": "PFOA",
            "specificity_status": "individual_chemical",
            "is_individual_chemical": True,
        },
        "result": {
            "raw_value": "12 ng/L",
            "raw_unit": "ng/L",
            "value_numeric": 12.0,
            "qualifier": "exact",
            "statistic": "single",
        },
        "sample": {"matrix_raw": "surface water", "matrix_normalized": "surface_water"},
        "location": {"city": "Lyon", "country": "France"},
        "sampling_time": {"year": 2020, "basis": "reported", "approximate": False},
        "analytical_method": {"method_name": "LC-MS/MS"},
        "evidence": {"quote": "PFOA was 12 ng/L."},
    }
    base.update(overrides)
    return base


def test_literature_summary_is_deterministically_rejected() -> None:
    decision = evidence_quality_gate(_candidate(observation_type="literature_summary"))
    assert decision is not None
    assert decision.action == "reject"
    assert decision.human_review_required is False
    assert "literature_summary_value" in decision.reason_codes
    assert "secondary_cited_study_not_primary_observation" in decision.reason_codes
    assert "missing_primary_observation_provenance" in decision.reason_codes


def test_secondary_cited_flag_is_rejected() -> None:
    candidate = _candidate(quality_flags=["secondary_cited_value"])
    decision = evidence_quality_gate(candidate)
    assert decision is not None
    assert decision.action == "reject"
    assert any(code.startswith("secondary_cited_value") for code in decision.reason_codes)


def test_exceedance_ratio_unit_is_rejected() -> None:
    candidate = _candidate(
        result={
            "raw_value": "5",
            "raw_unit": "fold",
            "value_numeric": 5.0,
            "qualifier": "exact",
            "statistic": "single",
        }
    )
    decision = evidence_quality_gate(candidate)
    assert decision is not None
    assert decision.action == "reject"
    assert any(code.startswith("exceedance_ratio_not_concentration") for code in decision.reason_codes)
    assert decision.human_review_required is False


def test_exceedance_ratio_pattern_in_raw_value_escalates() -> None:
    candidate = _candidate(
        result={
            "raw_value": "exceeded the limit by 4 times",
            "raw_unit": None,
            "value_numeric": None,
            "qualifier": "exact",
            "statistic": "single",
        }
    )
    decision = evidence_quality_gate(candidate)
    assert decision is not None
    assert any(code.startswith("exceedance_ratio_not_concentration") for code in decision.reason_codes)


def test_qualitative_nd_is_rejected_from_numeric_stream() -> None:
    candidate = _candidate(
        result={
            "raw_value": "nd",
            "raw_unit": "ng/L",
            "value_numeric": None,
            "qualifier": "not_detected",
            "statistic": "single",
        }
    )
    decision = evidence_quality_gate(candidate)
    assert decision is not None
    assert decision.action == "reject"
    assert any(code.startswith("qualitative_censored_without_numeric_observation") for code in decision.reason_codes)


def test_detection_frequency_escalates() -> None:
    candidate = _candidate(
        result={
            "raw_value": "100%",
            "raw_unit": "%",
            "value_numeric": 100.0,
            "qualifier": "exact",
            "statistic": "frequency",
        }
    )
    decision = evidence_quality_gate(candidate)
    assert decision is not None
    assert "detection_frequency_not_concentration" in decision.reason_codes


def test_empty_value_escalates() -> None:
    candidate = _candidate(
        result={
            "raw_value": None,
            "raw_unit": None,
            "value_numeric": None,
            "qualifier": "unknown",
            "statistic": "unknown",
        }
    )
    decision = evidence_quality_gate(candidate)
    assert decision is not None
    assert "no_measurable_concentration" in decision.reason_codes


def test_unbound_numeric_result_escalates() -> None:
    candidate = _candidate(
        sample={"matrix_raw": None, "matrix_normalized": None},
        location={},
        sampling_time={},
    )
    decision = evidence_quality_gate(candidate)
    assert decision is not None
    assert decision.action == "escalate"
    assert any(code.startswith("binding_missing") for code in decision.reason_codes)


def test_clean_field_measurement_passes_gate() -> None:
    assert evidence_quality_gate(_candidate()) is None


def test_policy_gate_escalates_before_model_call() -> None:
    delegate = AcceptingDelegate()
    validator = PolicyGatedEvidenceValidator(delegate, require_pilot_human_signoff=True)

    decision = validator.validate(
        _candidate(observation_type="literature_summary"),
        chunk=_chunk(),
        resolutions=(_resolution(),),
    )

    assert delegate.calls == 0
    assert decision.action == "reject"
    assert "literature_summary_value" in decision.reason_codes


def test_clean_candidate_reaches_model_and_pilot_escalates() -> None:
    delegate = AcceptingDelegate()
    validator = PolicyGatedEvidenceValidator(delegate, require_pilot_human_signoff=True)

    decision = validator.validate(_candidate(), chunk=_chunk(), resolutions=(_resolution(),))

    assert delegate.calls == 1
    assert decision.action == "escalate"
    assert "pilot_human_signoff_required" in decision.reason_codes


def test_conservative_validator_applies_quality_gate() -> None:
    validator = ConservativeEvidenceValidator()
    decision = validator.validate(
        _candidate(observation_type="literature_summary"),
        chunk=_chunk(),
        resolutions=(_resolution(),),
    )
    assert decision.action == "reject"
    assert "literature_summary_value" in decision.reason_codes


def test_policy_gate_batch_escalates_non_field_candidates() -> None:
    delegate = AcceptingDelegate()
    validator = PolicyGatedEvidenceValidator(delegate, require_pilot_human_signoff=True)
    candidates = [
        _candidate(observation_type="literature_summary"),
        _candidate(),
    ]
    decisions = validator.validate_batch(
        candidates,
        chunk=_chunk(),
        resolutions_by_candidate=[(_resolution(),), (_resolution(),)],
    )
    assert delegate.calls == 1  # only the clean candidate went to the model
    assert decisions[0].action == "reject"
    assert "literature_summary_value" in decisions[0].reason_codes
    assert decisions[1].action == "escalate"
    assert "pilot_human_signoff_required" in decisions[1].reason_codes


def test_genx_secondary_treatment_record_collects_all_rejection_reasons() -> None:
    candidate = _candidate(
        observation_type="laboratory_measurement",
        analyte={
            "raw_name": "GenX",
            "reported_name": "GenX",
            "proposed_canonical_name": "GenX",
            "specificity_status": "individual_chemical",
            "is_individual_chemical": True,
        },
        result={
            "raw_value": "<1000",
            "raw_unit": "ng/L",
            "value_numeric": None,
            "qualifier": "less_than",
            "statistic": "single",
        },
        sample={"matrix_raw": "surface waters and treated wastewater"},
        location={},
        sampling_time={},
        analytical_method={},
        quality_flags=["literature_summary"],
        evidence={
            "quote": (
                "Ateia et al. 2019 was able to remove 75-95% of PFSAs (C4-C8) "
                "and 60-80% of PFCAs (C4-C8) and GenX, each at <1000 ng/L from "
                "surface waters and treated wastewater at 24 h."
            ),
            "relation_note": "Reported concentration from cited literature, not this study.",
        },
    )
    decision = evidence_quality_gate(candidate)
    assert decision is not None
    assert decision.action == "reject"
    assert {
        "secondary_cited_study_not_primary_observation",
        "missing_primary_observation_provenance",
        "treatment_experiment_not_environmental_occurrence",
        "spiked_or_synthetic_matrix_not_excluded",
        "environmental_sample_provenance_unconfirmed",
        "chemical_identity_ambiguous_product_or_process_name",
    }.issubset(decision.reason_codes)


def test_laboratory_removal_experiment_without_field_provenance_is_rejected() -> None:
    candidate = _candidate(
        observation_type="laboratory_measurement",
        location={},
        sampling_time={},
        analytical_method={},
        evidence={"quote": "The initial concentration was 500 ng/L before removal at 24 h."},
    )
    decision = evidence_quality_gate(candidate)
    assert decision is not None
    assert decision.action == "reject"
    assert "treatment_experiment_not_environmental_occurrence" in decision.reason_codes
    assert "spiked_or_synthetic_matrix_not_excluded" in decision.reason_codes


def test_field_sampled_wwtp_influent_or_effluent_is_outside_main_scope() -> None:
    candidate = _candidate(
        sample={"matrix_raw": "WWTP influent and secondary effluent", "matrix_normalized": "wastewater"},
        evidence={"quote": "PFOA removal was calculated from sampled WWTP influent and effluent."},
    )
    decision = evidence_quality_gate(candidate)
    assert decision is not None
    assert decision.action == "reject"
    assert "wastewater_or_effluent_not_surface_water" in decision.reason_codes


def test_current_study_result_with_trailing_citation_is_not_secondary_by_citation_alone() -> None:
    candidate = _candidate(
        evidence={"quote": "PFOA was 12 ng/L at the Lyon site (Smith et al. 2019)."}
    )
    assert evidence_quality_gate(candidate) is None


def test_hch_isomer_specificity_cannot_collapse_to_parent_compound() -> None:
    candidate = _candidate(
        analyte={
            "raw_name": "Î²-HCH",
            "reported_name": "Î²-HCH",
            "canonical_name": "Hexachlorocyclohexane",
            "specificity_status": "individual_chemical",
            "is_individual_chemical": True,
        }
    )
    decision = evidence_quality_gate(candidate)
    assert decision is not None
    assert decision.action == "escalate"
    assert "chemical_identity_specificity_lost" in decision.reason_codes


def test_hch_isomer_specificity_is_preserved_by_specific_canonical_name() -> None:
    candidate = _candidate(
        analyte={
            "raw_name": "Î²-HCH",
            "reported_name": "Î²-HCH",
            "canonical_name": "beta-Hexachlorocyclohexane",
            "specificity_status": "individual_chemical",
            "is_individual_chemical": True,
        }
    )
    assert evidence_quality_gate(candidate) is None


def test_secondary_reject_precedes_unresolved_identity_escalation() -> None:
    delegate = AcceptingDelegate()
    validator = PolicyGatedEvidenceValidator(delegate, require_pilot_human_signoff=True)
    unresolved = ChemicalResolution(
        raw_name="GenX",
        normalized_query="genx",
        status="not_found",
        resolver_name="test",
    )
    candidate = _candidate(
        observation_type="laboratory_measurement",
        analyte={
            "raw_name": "GenX",
            "reported_name": "GenX",
            "specificity_status": "individual_chemical",
            "is_individual_chemical": True,
        },
        quality_flags=["literature_summary"],
        evidence={
            "quote": "Ateia et al. 2019 reported GenX below 1000 ng/L during removal.",
            "relation_note": "Cited literature, not this study.",
        },
    )
    decision = validator.validate(candidate, chunk=_chunk(), resolutions=(unresolved,))
    assert delegate.calls == 0
    assert decision.action == "reject"
    assert "secondary_cited_study_not_primary_observation" in decision.reason_codes
    assert "chemical_identity_ambiguous_product_or_process_name" in decision.reason_codes



def test_river_water_is_in_strict_surface_water_scope() -> None:
    assert evidence_quality_gate(
        _candidate(sample={"matrix_raw": "river water", "matrix_normalized": "river_water"})
    ) is None


def test_generic_water_is_accepted_only_with_surface_waterbody_provenance() -> None:
    accepted = _candidate(
        sample={"matrix_raw": "water", "matrix_normalized": "water"},
        location={"waterbody": "Chao Phraya River", "country": "Thailand"},
    )
    assert evidence_quality_gate(accepted) is None

    unconfirmed = _candidate(
        sample={"matrix_raw": "water", "matrix_normalized": "water"},
        location={"country": "Thailand"},
    )
    decision = evidence_quality_gate(unconfirmed)
    assert decision is not None
    assert "surface_water_provenance_unconfirmed" in decision.reason_codes


def test_groundwater_road_dust_air_and_biota_are_rejected() -> None:
    cases = [
        ({"matrix_raw": "groundwater", "matrix_normalized": "groundwater"}, "groundwater_not_surface_water"),
        ({"matrix_raw": "road dust", "matrix_normalized": "road dust"}, "non_surface_water_matrix"),
        ({"matrix_raw": "PM2.5", "matrix_normalized": "air particulate matter"}, "non_surface_water_matrix"),
        ({"matrix_raw": "mussel tissue", "matrix_normalized": "biota"}, "non_surface_water_matrix"),
    ]
    for sample, reason in cases:
        decision = evidence_quality_gate(_candidate(sample=sample))
        assert decision is not None
        assert decision.action == "reject"
        assert reason in decision.reason_codes


def test_surface_water_metal_concentration_is_eligible() -> None:
    candidate = _candidate(
        analyte={
            "raw_name": "Cr",
            "canonical_name": "Chromium",
            "specificity_status": "individual_chemical",
            "is_individual_chemical": True,
        },
        result={
            "raw_value": "0.005",
            "raw_unit": "mg/L",
            "value_numeric": 0.005,
            "qualifier": "exact",
            "statistic": "mean",
        },
        sample={"matrix_raw": "water samples", "matrix_normalized": "surface_water"},
        location={"waterbody": "Chao Phraya River", "country": "Thailand"},
        evidence={"quote": "Cr in water samples was 0.005 ± 0.00 mg/L."},
    )
    assert evidence_quality_gate(candidate) is None


def test_metal_attached_to_microplastics_is_not_a_water_column_record() -> None:
    candidate = _candidate(
        analyte={
            "raw_name": "Cr",
            "canonical_name": "Chromium",
            "specificity_status": "individual_chemical",
            "is_individual_chemical": True,
        },
        result={
            "raw_value": "32",
            "raw_unit": "µg/g",
            "value_numeric": 32.0,
            "qualifier": "exact",
            "statistic": "mean",
        },
        sample={
            "matrix_raw": "microplastics recovered from surface water",
            "matrix_normalized": "microplastics in surface water",
            "phase_or_fraction": "particle-bound",
        },
        evidence={"quote": "Cr associated with microplastics was 32 µg/g."},
    )
    decision = evidence_quality_gate(candidate)
    assert decision is not None
    assert decision.action == "reject"
    assert "microplastic_bound_measurement_not_water_column" in decision.reason_codes

def test_average_table_caption_retries_single_statistic() -> None:
    candidate = _candidate(
        result={
            "raw_value": "46.85",
            "raw_unit": "ng/L",
            "value_numeric": 46.85,
            "qualifier": "exact",
            "statistic": "single",
        },
        evidence={
            "table_id": "Table 1",
            "table_caption": (
                "Comparison of average antibiotic concentrations in surface water (ng/L)"
            ),
            "row_label": "ShiChuan River, China",
            "column_label": "EFX",
            "quote": "EFX 46.85; This study",
        },
    )
    decision = evidence_quality_gate(candidate)
    assert decision is not None
    assert decision.action == "retry"
    assert any(
        code.startswith("table_caption_statistic_mismatch:expected_mean")
        for code in decision.reason_codes
    )
    assert "table_cell_2d_layout" in decision.requested_context


def test_average_table_caption_accepts_mean_statistic() -> None:
    candidate = _candidate(
        result={
            "raw_value": "46.85",
            "raw_unit": "ng/L",
            "value_numeric": 46.85,
            "qualifier": "exact",
            "statistic": "mean",
        },
        evidence={
            "table_id": "Table 1",
            "table_caption": (
                "Comparison of average antibiotic concentrations in surface water (ng/L)"
            ),
            "row_label": "ShiChuan River, China",
            "column_label": "EFX",
            "quote": "EFX 46.85; This study",
        },
    )
    assert evidence_quality_gate(candidate) is None

class BatchRecordingDelegate:
    validator_name = "batch_recording"

    def __init__(self) -> None:
        self.batch_calls: list[list[str]] = []

    def validate(
        self,
        candidate: dict[str, Any],
        *,
        chunk: EvidenceChunk,
        resolutions: tuple[ChemicalResolution, ...],
    ) -> ValidationDecision:
        del chunk, resolutions
        self.batch_calls.append([str(candidate.get("candidate_id"))])
        return ValidationDecision(action="accept", reason_codes=("model_evidence_verified",))

    def validate_batch(
        self,
        candidates: list[dict[str, Any]],
        *,
        chunk: EvidenceChunk,
        resolutions_by_candidate: list[tuple[ChemicalResolution, ...]],
    ) -> list[ValidationDecision]:
        del chunk, resolutions_by_candidate
        self.batch_calls.append([str(item.get("candidate_id")) for item in candidates])
        return [
            ValidationDecision(action="accept", reason_codes=("model_evidence_verified",))
            for _ in candidates
        ]


def _stat_candidate(candidate_id: str, statistic: str, raw_value: str) -> dict[str, Any]:
    candidate = _candidate(
        result={
            "raw_value": raw_value,
            "raw_unit": "ng/L",
            "value_numeric": float(raw_value),
            "qualifier": "exact",
            "statistic": statistic,
        },
        evidence={
            "quote": "PFOA dry-season minimum, maximum, and mean concentrations.",
            "chunk_id": "c1",
            "page_start": 3,
            "page_end": 3,
            "table_id": "Table 1",
            "row_label": "PFOA",
            "column_label": "dry season",
        },
    )
    candidate["candidate_id"] = candidate_id
    return candidate


def test_review_equivalent_min_max_mean_use_one_representative_and_propagate() -> None:
    delegate = BatchRecordingDelegate()
    validator = PolicyGatedEvidenceValidator(delegate, require_pilot_human_signoff=False)
    candidates = [
        _stat_candidate("min", "minimum", "1"),
        _stat_candidate("max", "maximum", "9"),
        _stat_candidate("mean", "mean", "4"),
    ]

    decisions = validator.validate_batch(
        candidates,
        chunk=_chunk(),
        resolutions_by_candidate=[(_resolution(),)] * 3,
    )

    assert delegate.batch_calls == [["min"]]
    assert [item.action for item in decisions] == ["accept", "accept", "accept"]
    assert decisions[0] is decisions[1] is decisions[2]


def test_censored_record_is_not_grouped_with_exact_measurements() -> None:
    delegate = BatchRecordingDelegate()
    validator = PolicyGatedEvidenceValidator(delegate, require_pilot_human_signoff=False)
    censored = _stat_candidate("nd", "minimum", "1")
    censored["result"] = {
        "raw_value": "n.d.",
        "raw_unit": "ng/L",
        "value_numeric": None,
        "qualifier": "not_detected",
        "statistic": "minimum",
    }
    candidates = [
        censored,
        _stat_candidate("max", "maximum", "9"),
        _stat_candidate("mean", "mean", "4"),
    ]

    decisions = validator.validate_batch(
        candidates,
        chunk=_chunk(),
        resolutions_by_candidate=[(_resolution(),)] * 3,
    )

    assert delegate.batch_calls == [["max"]]
    assert decisions[0].action == "reject"
    assert any(
        code.startswith("qualitative_censored_without_numeric_observation")
        for code in decisions[0].reason_codes
    )
    assert [item.action for item in decisions[1:]] == ["accept", "accept"]


def test_review_group_requires_same_time_location_method_and_evidence() -> None:
    variants = (
        ("sampling_time", {"year": 2021, "basis": "reported", "approximate": False}),
        ("location", {"city": "Paris", "country": "France"}),
        ("analytical_method", {"method_name": "GC-MS"}),
        ("evidence", {"quote": "A different table cell.", "chunk_id": "c2", "page_start": 4}),
    )
    for field, replacement in variants:
        delegate = BatchRecordingDelegate()
        validator = PolicyGatedEvidenceValidator(delegate, require_pilot_human_signoff=False)
        first = _stat_candidate("min", "minimum", "1")
        second = _stat_candidate("max", "maximum", "9")
        second[field] = replacement

        decisions = validator.validate_batch(
            [first, second],
            chunk=_chunk(),
            resolutions_by_candidate=[(_resolution(),), (_resolution(),)],
        )

        assert delegate.batch_calls == [["min", "max"]], field
        assert [item.action for item in decisions] == ["accept", "accept"]


def test_candidate_specific_retry_is_not_propagated_to_sibling_statistics() -> None:
    class RetryThenAcceptDelegate(BatchRecordingDelegate):
        def validate_batch(
            self,
            candidates: list[dict[str, Any]],
            *,
            chunk: EvidenceChunk,
            resolutions_by_candidate: list[tuple[ChemicalResolution, ...]],
        ) -> list[ValidationDecision]:
            del chunk, resolutions_by_candidate
            ids = [str(item.get("candidate_id")) for item in candidates]
            self.batch_calls.append(ids)
            if len(self.batch_calls) == 1:
                return [
                    ValidationDecision(
                        action="retry",
                        reason_codes=("raw_value_needs_recheck",),
                        failed_json_pointers=("/result/raw_value",),
                    )
                ]
            return [
                ValidationDecision(action="accept", reason_codes=("model_evidence_verified",))
                for _ in candidates
            ]

    delegate = RetryThenAcceptDelegate()
    validator = PolicyGatedEvidenceValidator(delegate, require_pilot_human_signoff=False)
    candidates = [
        _stat_candidate("min", "minimum", "1"),
        _stat_candidate("max", "maximum", "9"),
        _stat_candidate("mean", "mean", "4"),
    ]

    decisions = validator.validate_batch(
        candidates,
        chunk=_chunk(),
        resolutions_by_candidate=[(_resolution(),)] * 3,
    )

    assert delegate.batch_calls == [["min"], ["max", "mean"]]
    assert [item.action for item in decisions] == ["retry", "accept", "accept"]


def test_duplicate_statistics_are_reviewed_independently() -> None:
    delegate = BatchRecordingDelegate()
    validator = PolicyGatedEvidenceValidator(delegate, require_pilot_human_signoff=False)
    candidates = [
        _stat_candidate("mean-1", "mean", "4"),
        _stat_candidate("mean-2", "mean", "5"),
    ]

    validator.validate_batch(
        candidates,
        chunk=_chunk(),
        resolutions_by_candidate=[(_resolution(),)] * 2,
    )

    assert delegate.batch_calls == [["mean-1", "mean-2"]]


def test_pilot_eligibility_requires_clean_prior_acceptance() -> None:
    validator = PolicyGatedEvidenceValidator(AcceptingDelegate(), require_pilot_human_signoff=True)
    clean = validator.validate(_candidate(), chunk=_chunk(), resolutions=(_resolution(),))
    assert clean.action == "escalate"
    assert clean.pilot_accept_eligible is True
    unresolved = validator.validate(_candidate(), chunk=_chunk(), resolutions=())
    assert unresolved.pilot_accept_eligible is False
    conflicting = validator._apply_pilot_policy(ValidationDecision(
        action="accept", reason_codes=("model_evidence_verified",), failed_json_pointers=("/evidence",)))
    assert conflicting.pilot_accept_eligible is False


def test_review_equivalence_preserves_input_order() -> None:
    class ActionByIdDelegate(BatchRecordingDelegate):
        def validate_batch(
            self,
            candidates: list[dict[str, Any]],
            *,
            chunk: EvidenceChunk,
            resolutions_by_candidate: list[tuple[ChemicalResolution, ...]],
        ) -> list[ValidationDecision]:
            del chunk, resolutions_by_candidate
            ids = [str(item.get("candidate_id")) for item in candidates]
            self.batch_calls.append(ids)
            return [
                ValidationDecision(
                    action="reject" if candidate_id == "median" else "accept",
                    reason_codes=("independent_decision",),
                )
                for candidate_id in ids
            ]

    delegate = ActionByIdDelegate()
    validator = PolicyGatedEvidenceValidator(delegate, require_pilot_human_signoff=False)
    median = _stat_candidate("median", "median", "3")
    candidates = [
        _stat_candidate("min", "minimum", "1"),
        median,
        _stat_candidate("max", "maximum", "9"),
        _stat_candidate("mean", "mean", "4"),
    ]

    decisions = validator.validate_batch(
        candidates,
        chunk=_chunk(),
        resolutions_by_candidate=[(_resolution(),)] * 4,
    )

    assert delegate.batch_calls == [["min", "median"]]
    assert [item.action for item in decisions] == ["accept", "reject", "accept", "accept"]
