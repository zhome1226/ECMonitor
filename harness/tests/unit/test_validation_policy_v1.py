from __future__ import annotations

import sqlite3
from pathlib import Path

from ecmonitor.fulltext_extraction.models import ValidationDecision
from ecmonitor.fulltext_extraction.policy import (
    classify_terminal_status,
    coarse_disposition_for_terminal,
    materialize_censoring,
    output_stream_for_terminal,
    policy_rule_for_terminal,
    terminal_requires_human_review,
)
from ecmonitor.fulltext_extraction.quality import evidence_quality_gate
from ecmonitor.fulltext_extraction.registry import ChemicalRegistry
from ecmonitor.fulltext_extraction.storage import FulltextControlPlane


def _candidate(**overrides):
    candidate = {
        "observation_type": "field_measurement",
        "analyte": {
            "raw_name": "PFOA",
            "reported_name": "PFOA",
            "specificity_status": "individual_chemical",
            "is_individual_chemical": True,
        },
        "result": {
            "raw_value": "12",
            "raw_unit": "ng/L",
            "value_numeric": 12.0,
            "qualifier": "exact",
            "statistic": "single",
        },
        "sample": {"matrix_raw": "river water", "matrix_normalized": "surface_water"},
        "location": {"waterbody": "River A", "country": "France"},
        "sampling_time": {"year": 2020, "basis": "reported", "approximate": False},
        "analytical_method": {"method_name": "LC-MS/MS"},
        "evidence": {"quote": "PFOA was 12 ng/L in River A."},
    }
    candidate.update(overrides)
    return candidate


def test_numeric_left_censoring_materializes_and_routes_to_censored_stream() -> None:
    candidate = _candidate(
        result={
            "raw_value": "<0.5",
            "raw_unit": "ng/L",
            "value_numeric": None,
            "qualifier": "less_than",
            "statistic": "single",
        }
    )
    materialized = materialize_censoring(candidate)
    assert materialized["result"]["reported_raw_value"] == "<0.5"
    assert materialized["result"]["raw_value"] is None
    assert materialized["result"]["censoring_limit"] == 0.5
    assert materialized["result"]["not_a_zero_concentration"] is True
    assert evidence_quality_gate(materialized) is None
    status = classify_terminal_status(
        materialized, ValidationDecision(action="accept", reason_codes=("evidence_verified",))
    )
    assert status == "accepted_censored"
    assert coarse_disposition_for_terminal(status) == "accepted"
    assert output_stream_for_terminal(status) == "censored_observations"
    assert policy_rule_for_terminal(status) == "LC-02"


def test_qualitative_nd_is_rejected_from_numeric_stream() -> None:
    candidate = materialize_censoring(
        _candidate(
            result={
                "raw_value": "ND",
                "raw_unit": "ng/L",
                "value_numeric": None,
                "qualifier": "not_detected",
                "statistic": "single",
            }
        )
    )
    decision = evidence_quality_gate(candidate)
    assert decision is not None and decision.action == "reject"
    assert classify_terminal_status(candidate, decision) == "rejected_non_observation"


def test_wwtp_and_microplastic_bound_measurements_are_rejected_scope() -> None:
    wastewater = _candidate(
        sample={"matrix_raw": "secondary effluent", "matrix_normalized": "wastewater"},
        evidence={"quote": "Mean PFOA in secondary effluent was 12 ng/L."},
    )
    decision = evidence_quality_gate(wastewater)
    assert decision is not None and decision.action == "reject"
    assert classify_terminal_status(wastewater, decision) == "rejected_scope"

    particle_bound = _candidate(
        analyte={
            "raw_name": "Cr",
            "reported_name": "Cr",
            "specificity_status": "individual_chemical",
            "is_individual_chemical": True,
        },
        result={
            "raw_value": "32",
            "raw_unit": "ug/g",
            "value_numeric": 32.0,
            "qualifier": "exact",
            "statistic": "mean",
        },
        sample={
            "matrix_raw": "microplastics recovered from river water",
            "matrix_normalized": "microplastic particles",
            "phase_or_fraction": "particle-bound",
        },
        evidence={"quote": "Cr associated with microplastics was 32 ug/g."},
    )
    decision = evidence_quality_gate(particle_bound)
    assert decision is not None and decision.action == "reject"
    assert classify_terminal_status(particle_bound, decision) == "rejected_scope"


def test_surface_water_microplastic_abundance_has_separate_stream_without_cas() -> None:
    candidate = _candidate(
        analyte={
            "raw_name": "microplastics",
            "reported_name": "microplastics",
            "specificity_status": "polymer_or_particle_category",
            "is_individual_chemical": False,
        },
        result={
            "raw_value": "14",
            "raw_unit": "items/L",
            "value_numeric": 14.0,
            "qualifier": "exact",
            "statistic": "mean",
        },
        sample={"matrix_raw": "river surface water", "matrix_normalized": "surface_water"},
        evidence={"quote": "Microplastic abundance in river surface water was 14 items/L."},
    )
    assert evidence_quality_gate(candidate) is None
    status = classify_terminal_status(
        candidate, ValidationDecision(action="accept", reason_codes=("evidence_verified",))
    )
    assert status == "accepted_microplastic_surface_water"
    assert output_stream_for_terminal(status) == "microplastic_surface_water_observations"


def test_secondary_genx_treatment_value_is_rejected() -> None:
    candidate = _candidate(
        observation_type="laboratory_measurement",
        analyte={
            "raw_name": "GenX",
            "reported_name": "GenX",
            "specificity_status": "mixture_or_product",
            "is_individual_chemical": False,
        },
        sample={"matrix_raw": "surface waters and treated wastewater"},
        location={},
        sampling_time={},
        analytical_method={},
        evidence={
            "quote": "Ateia et al. removed PFAS and GenX, each initially below 1000 ng/L.",
            "relation_note": "Value from cited treatment literature.",
        },
    )
    decision = evidence_quality_gate(candidate)
    assert decision is not None and decision.action == "reject"
    assert classify_terminal_status(candidate, decision) in {
        "rejected_secondary_source",
        "rejected_treatment_experiment",
    }


def test_tentative_level_two_and_transformation_product_route_separately() -> None:
    candidate = _candidate(
        analyte={
            "raw_name": "TP-1",
            "reported_name": "TP-1",
            "identification_level": "Level 2",
            "identity_status": "tentative transformation product",
            "is_tentative": True,
            "specificity_status": "individual_chemical",
            "is_individual_chemical": True,
        },
        result={
            "raw_value": "8.2",
            "raw_unit": "ng/L",
            "value_numeric": 8.2,
            "qualifier": "exact",
            "statistic": "single",
            "semi_quantitative": True,
        },
    )
    status = classify_terminal_status(
        candidate, ValidationDecision(action="accept", reason_codes=("tentative_evidence_verified",))
    )
    assert status == "accepted_tentative"
    assert output_stream_for_terminal(status) == "tentative_observations"


def test_identity_deferral_is_nonblocking_but_binding_conflict_requires_human() -> None:
    candidate = _candidate()
    identity_decision = ValidationDecision(
        action="escalate",
        reason_codes=("chemical_resolution_missing",),
        human_review_required=True,
    )
    identity_status = classify_terminal_status(candidate, identity_decision)
    assert identity_status == "deferred_identity_evidence"
    assert terminal_requires_human_review(identity_status, identity_decision) is False

    binding_decision = ValidationDecision(
        action="escalate",
        reason_codes=("result_binding_incomplete:location",),
        human_review_required=True,
    )
    binding_status = classify_terminal_status(candidate, binding_decision)
    assert binding_status == "deferred_source_binding"
    assert terminal_requires_human_review(binding_status, binding_decision) is True


def test_publication_year_fallback_requires_approximate_true() -> None:
    candidate = _candidate(
        sampling_time={"year": 2023, "basis": "publication_year_fallback", "approximate": False}
    )
    decision = evidence_quality_gate(candidate)
    assert decision is not None and decision.action == "retry"
    assert "publication_year_fallback_requires_approximate_true" in decision.reason_codes


def test_geographic_conflict_routes_to_deferred_geographic_conflict() -> None:
    candidate = _candidate(
        location={
            "city": "Paris",
            "admin1": "California",
            "country": "France",
            "admin_hierarchy_consistent": False,
            "admin_hierarchy_conflicts": ["city_admin1", "admin1_country"],
        }
    )
    decision = evidence_quality_gate(candidate)
    assert decision is not None and decision.action == "escalate"
    assert classify_terminal_status(candidate, decision) == "deferred_geographic_conflict"


def test_document_local_alias_does_not_leak_between_documents(tmp_path: Path) -> None:
    registry = ChemicalRegistry(tmp_path / "registry.sqlite3")
    registry.record_document_local_alias(
        document_id="doc-a",
        doi="10.1000/a",
        alias_text="TCPP",
        canonical_name="paper-defined TCPP entity",
        identity_status="paper_verified",
        evidence_quote="TCPP was defined in Table S1 as the named standard.",
        identifiers={"cas_candidates": ["000-00-0"]},
    )
    assert registry.lookup_document_local("TCPP", document_id="doc-a") is not None
    assert registry.lookup_document_local("TCPP", document_id="doc-b") is None
    assert registry.lookup_validated("TCPP") is None


def test_sqlite_v2_observation_table_migrates_to_v3(tmp_path: Path) -> None:
    database = tmp_path / "control.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT);
            INSERT INTO schema_migrations(version, applied_at) VALUES (1, CURRENT_TIMESTAMP);
            INSERT INTO schema_migrations(version, applied_at) VALUES (2, CURRENT_TIMESTAMP);
            CREATE TABLE observation_records (
                record_id TEXT PRIMARY KEY,
                document_session_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                disposition TEXT NOT NULL,
                canonical_name TEXT,
                reported_name TEXT,
                replacement_name TEXT,
                payload_json TEXT NOT NULL
            );
            """
        )
    plane = FulltextControlPlane(database)
    assert plane.status()["schema_version"] == 3
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(observation_records)")}
        versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    assert {"terminal_status", "output_stream", "policy_rule_id"}.issubset(columns)
    assert 3 in versions
