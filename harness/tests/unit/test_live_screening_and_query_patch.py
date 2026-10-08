import json
from pathlib import Path

import pytest

from ecmonitor.retrieval_specialist.models import NormalizedRecord
from ecmonitor.retrieval_specialist.models.records import QueryMetrics, ScreeningDecision
from ecmonitor.retrieval_specialist.operators.gpt_screening import (
    ScreeningWorkerBlocked,
    TitleAbstractScreeningWorkerExecutor,
)
from ecmonitor.retrieval_specialist.operators.protocol_loader import LoadedConfig, ProtocolLoader
from ecmonitor.retrieval_specialist.operators.query_compiler import CanonicalQueryCompiler
from ecmonitor.retrieval_specialist.operators.query_patch import QueryPatch, QueryPatchApplier
from ecmonitor.retrieval_specialist.operators.query_planner import QueryPlanner
from ecmonitor.retrieval_specialist.operators.query_refinement import (
    QueryRefinementBlocked,
    QueryRefinementWorkerExecutor,
)
from ecmonitor.retrieval_specialist.orchestration.phase11_runner import (
    CandidateQueryPlan,
    IterationPlan,
    Phase11Runner,
)
from ecmonitor.retrieval_specialist.storage.atomic_io import (
    ensure_dir,
    read_json,
    write_json_atomic,
)
from ecmonitor.retrieval_specialist.storage.control_plane import ControlPlane


def _record() -> NormalizedRecord:
    return NormalizedRecord(
        global_record_id="doi:10.1000/live",
        source_records=[],
        doi="10.1000/live",
        normalized_doi="10.1000/live",
        pmid=None,
        openalex_id=None,
        semantic_scholar_id=None,
        crossref_id=None,
        title_original="PFAS concentrations in river water",
        title_normalized="pfas concentrations in river water",
        abstract_original="Measured PFAS concentrations in river water samples.",
        abstract_source="crossref",
        keywords=["PFAS", "river"],
        authors=["Chen"],
        first_author="Chen",
        publication_date=None,
        publication_year=2024,
        journal_title="Water Research",
        issn=[],
        eissn=[],
        document_type="journal article",
        language="en",
        source_rank=1,
        source_relevance_score=None,
        retrieved_from=["crossref"],
        retrieval_timestamp="2026-07-09T00:00:00Z",
        raw_metadata_path=None,
    )


def test_live_screening_writes_one_document_request_then_ingests_result(
    tmp_path: Path,
) -> None:
    executor = TitleAbstractScreeningWorkerExecutor(
        repo_root=Path.cwd(),
        run_dir=tmp_path,
        schema_path=Path.cwd() / "schemas" / "retrieval" / "screening_decision.schema.json",
    )
    record = _record()

    with pytest.raises(ScreeningWorkerBlocked) as blocked:
        executor.screen_one(
            record,
            run_id="run",
            query_id="Q0001",
            iteration=1,
            audit_batch_id="screening_one",
            protocol={},
            allowed_reason_codes={},
            scie_status="unknown",
        )
    request_ref = Path.cwd() / blocked.value.payload["request_ref"]
    result_ref = Path.cwd() / blocked.value.payload["result_ref"]
    assert request_ref.exists()
    request_text = request_ref.read_text(encoding="utf-8")
    assert "one_document_per_conversation" in request_text
    assert "query_term_evidence" in request_text
    assert "exclusion_candidate_terms" in request_text
    assert "direct natural surface-water occurrence" in request_text
    assert "primary-data screening posture" in request_text
    assert "critical assessments" in request_text
    assert "review/assessment/method-only" in request_text

    write_json_atomic(
        result_ref,
        {
            "decision": "include",
            "confidence": 0.92,
            "article_type_ok": True,
            "date_ok": True,
            "scie_status": "unknown",
            "emerging_contaminant_context": True,
            "surface_water_sample": True,
            "included_waterbody_types": ["river"],
            "field_environmental_samples": True,
            "concentration_evidence": "explicit_quantified",
            "study_type": "field monitoring",
            "reason_codes": ["I_SURFACE_WATER_CONCENTRATION"],
            "evidence_spans": ["Measured PFAS concentrations in river water samples."],
            "query_term_evidence": [
                {
                    "term": "field monitoring",
                    "concept_block": "monitoring_and_concentration_terms",
                    "evidence_span": "Measured PFAS concentrations in river water samples.",
                    "why_relevant": "describes field monitoring evidence",
                    "confidence": 0.91,
                }
            ],
            "article_ec_scope": "true",
        },
    )
    decision = executor.screen_one(
        record,
        run_id="run",
        query_id="Q0001",
        iteration=1,
        audit_batch_id="screening_one",
        protocol={},
        allowed_reason_codes={},
        scie_status="unknown",
    )
    assert decision.decision == "include"
    assert decision.decision_actor == "TitleAbstractScreeningWorker"
    assert decision.raw_model_response_path
    assert decision.query_term_evidence[0]["term"] == "field monitoring"


def test_invalid_live_screening_retries_then_defers_safely(tmp_path: Path) -> None:
    executor = TitleAbstractScreeningWorkerExecutor(
        repo_root=Path.cwd(),
        run_dir=tmp_path,
        schema_path=Path.cwd() / "schemas" / "retrieval" / "screening_decision.schema.json",
        max_schema_retries=1,
    )
    record = _record()
    with pytest.raises(ScreeningWorkerBlocked) as blocked:
        executor.screen_one(
            record,
            run_id="run",
            query_id="Q0001",
            iteration=1,
            audit_batch_id="screening_one",
            protocol={},
            allowed_reason_codes={},
            scie_status="unknown",
        )
    result_ref = Path.cwd() / blocked.value.payload["result_ref"]
    write_json_atomic(result_ref, {"decision": "maybe"})
    with pytest.raises(ScreeningWorkerBlocked):
        executor.screen_one(
            record,
            run_id="run",
            query_id="Q0001",
            iteration=1,
            audit_batch_id="screening_one",
            protocol={},
            allowed_reason_codes={},
            scie_status="unknown",
        )
    write_json_atomic(result_ref, {"decision": "maybe"})
    decision = executor.screen_one(
        record,
        run_id="run",
        query_id="Q0001",
        iteration=1,
        audit_batch_id="screening_one",
        protocol={},
        allowed_reason_codes={},
        scie_status="unknown",
    )
    assert decision.decision == "defer_metadata"
    assert decision.reason_codes == ["D_METADATA_MISSING"]


def test_live_screening_normalizes_common_worker_concentration_labels(
    tmp_path: Path,
) -> None:
    executor = TitleAbstractScreeningWorkerExecutor(
        repo_root=Path.cwd(),
        run_dir=tmp_path,
        schema_path=Path.cwd() / "schemas" / "retrieval" / "screening_decision.schema.json",
    )
    record = _record()
    with pytest.raises(ScreeningWorkerBlocked) as blocked:
        executor.screen_one(
            record,
            run_id="run",
            query_id="Q0001",
            iteration=1,
            audit_batch_id="screening_one",
            protocol={},
            allowed_reason_codes={},
            scie_status="unknown",
        )
    result_ref = Path.cwd() / blocked.value.payload["result_ref"]
    write_json_atomic(
        result_ref,
        {
            "decision": "include",
            "confidence": 0.92,
            "article_type_ok": True,
            "date_ok": True,
            "scie_status": "unknown",
            "emerging_contaminant_context": True,
            "surface_water_sample": True,
            "included_waterbody_types": ["river"],
            "field_environmental_samples": True,
            "concentration_evidence": "present",
            "study_type": "field monitoring",
            "reason_codes": ["I_SURFACE_WATER_CONCENTRATION"],
            "evidence_spans": ["Measured PFAS concentrations in river water samples."],
            "article_ec_scope": "true",
        },
    )

    decision = executor.screen_one(
        record,
        run_id="run",
        query_id="Q0001",
        iteration=1,
        audit_batch_id="screening_one",
        protocol={},
        allowed_reason_codes={},
        scie_status="unknown",
    )

    assert decision.decision == "include"
    assert decision.concentration_evidence == "explicit_quantified"


def test_live_screening_accepts_worker_result_with_utf8_bom(tmp_path: Path) -> None:
    executor = TitleAbstractScreeningWorkerExecutor(
        repo_root=Path.cwd(),
        run_dir=tmp_path,
        schema_path=Path.cwd() / "schemas" / "retrieval" / "screening_decision.schema.json",
    )
    record = _record()
    with pytest.raises(ScreeningWorkerBlocked) as blocked:
        executor.screen_one(
            record,
            run_id="run",
            query_id="Q0001",
            iteration=1,
            audit_batch_id="screening_one",
            protocol={},
            allowed_reason_codes={},
            scie_status="unknown",
        )
    result_ref = Path.cwd() / blocked.value.payload["result_ref"]
    result_ref.parent.mkdir(parents=True, exist_ok=True)
    result_ref.write_text(
        json.dumps(
            {
                "decision": "exclude",
                "confidence": 0.9,
                "article_type_ok": False,
                "date_ok": True,
                "scie_status": "unknown",
                "emerging_contaminant_context": True,
                "surface_water_sample": False,
                "field_environmental_samples": False,
                "concentration_evidence": "absent",
                "study_type": "review",
                "reason_codes": ["E_REVIEW"],
                "evidence_spans": ["Review for water purification"],
            }
        ),
        encoding="utf-8-sig",
    )

    decision = executor.screen_one(
        record,
        run_id="run",
        query_id="Q0001",
        iteration=1,
        audit_batch_id="screening_one",
        protocol={},
        allowed_reason_codes={},
        scie_status="unknown",
    )

    assert decision.decision == "exclude"
    assert decision.reason_codes == ["E_REVIEW"]


def test_query_patch_modifies_active_block_and_compiled_query() -> None:
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal article"]},
            "emerging_contaminant_terms": ["PFAS"],
            "surface_water_terms": ["river"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-09",
    )
    patch = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-estuary",
        parent_query_id="Q0001",
        target_concept_block="surface_water_terms",
        operation="add",
        terms_added=["estuary"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/live"],
        rationale="Included records mention estuaries.",
        expected_effect="Increase estuary-specific novelty.",
        possible_drift_risk="May broaden beyond rivers.",
    )

    result = QueryPatchApplier().apply(parent, patch, child_query_id="Q0002", iteration=2)

    assert result.status == "applied"
    assert "estuary" in result.child_query.surface_water_terms
    parent_compiled = CanonicalQueryCompiler().compile_for_source(parent, "crossref")
    child_compiled = CanonicalQueryCompiler().compile_for_source(
        result.child_query, "crossref"
    )
    assert parent_compiled.compiled_query != child_compiled.compiled_query


def test_duplicate_query_patch_addition_is_rejected() -> None:
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal article"]},
            "emerging_contaminant_terms": ["PFAS"],
            "surface_water_terms": ["river"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-09",
    )
    patch = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-river",
        parent_query_id="Q0001",
        target_concept_block="surface_water_terms",
        operation="add",
        terms_added=["river"],
        terms_removed=[],
        evidence_document_ids=[],
        rationale="Duplicate term.",
        expected_effect="No change.",
        possible_drift_risk="No change.",
    )

    result = QueryPatchApplier().apply(parent, patch, child_query_id="Q0002", iteration=2)

    assert result.status == "rejected"
    assert result.reason == "duplicate_addition"


def test_noise_constraint_patch_updates_prohibited_terms_and_compiled_query() -> None:
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal article"]},
            "emerging_contaminant_terms": ["microplastic*"],
            "surface_water_terms": ["river"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-09",
    )
    patch = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-exclude-fish",
        parent_query_id="Q0001",
        target_concept_block="prohibited_or_rejected_terms",
        operation="add",
        terms_added=["fish"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/noise"],
        rationale="Excluded records repeatedly mention fish biota.",
        expected_effect="Reduce biota noise without changing positive scope.",
        possible_drift_risk="May remove water studies that mention fish incidentally.",
    )

    result = QueryPatchApplier().apply(parent, patch, child_query_id="Q0002", iteration=2)
    compiled = CanonicalQueryCompiler().compile_for_source(result.child_query, "crossref")

    assert result.status == "applied"
    assert result.child_query.prohibited_or_rejected_terms == ["fish"]
    assert result.child_query.modified_concept_blocks == ["prohibited_or_rejected_terms"]
    assert 'NOT ("fish")' in compiled.compiled_query
    assert result.parent_compiled["crossref"] != result.child_compiled["crossref"]


def test_non_noise_patch_preserves_existing_prohibited_terms() -> None:
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal article"]},
            "emerging_contaminant_terms": ["microplastic*"],
            "surface_water_terms": ["river"],
            "monitoring_and_concentration_terms": ["concentration"],
            "prohibited_or_rejected_terms": ["fish", "toxicity"],
        },
        "2026-07-09",
    )
    patch = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-monitoring-context",
        parent_query_id="Q0001",
        target_concept_block="monitoring_and_concentration_terms",
        operation="add",
        terms_added=["field sampling"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/include"],
        rationale="Included records repeatedly mention field sampling.",
        expected_effect="Increase monitoring-context precision.",
        possible_drift_risk="May over-focus on sampled studies.",
    )

    result = QueryPatchApplier().apply(parent, patch, child_query_id="Q0002", iteration=2)
    compiled = CanonicalQueryCompiler().compile_for_source(result.child_query, "crossref")

    assert result.status == "applied"
    assert result.child_query.prohibited_or_rejected_terms == ["fish", "toxicity"]
    assert "field sampling" in result.child_query.monitoring_and_concentration_terms
    assert 'NOT ("fish" OR "toxicity")' in compiled.compiled_query


def test_initial_query_preserves_protocol_negative_terms() -> None:
    query = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal article"]},
            "emerging_contaminant_terms": ["microplastic*"],
            "surface_water_terms": ["river"],
            "monitoring_and_concentration_terms": ["concentration"],
            "optional_context_terms": ["field monitoring"],
            "prohibited_or_rejected_terms": ["fish", "review"],
        },
        "2026-07-09",
    )

    compiled = CanonicalQueryCompiler().compile_for_source(query, "crossref")

    assert query.optional_context_terms == ["field monitoring"]
    assert query.prohibited_or_rejected_terms == ["fish", "review"]
    assert 'AND ("field monitoring")' in compiled.compiled_query
    assert 'NOT ("fish" OR "review")' in compiled.compiled_query


def test_noise_constraint_duplicate_patch_is_rejected() -> None:
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal article"]},
            "emerging_contaminant_terms": ["microplastic*"],
            "surface_water_terms": ["river"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-09",
    )
    parent = QueryPatchApplier().apply(
        parent,
        QueryPatch(
            query_patch_schema_version="1.0.0",
            patch_id="patch-exclude-fish",
            parent_query_id="Q0001",
            target_concept_block="prohibited_or_rejected_terms",
            operation="add",
            terms_added=["fish"],
            terms_removed=[],
            evidence_document_ids=[],
            rationale="Initial noise term.",
            expected_effect="Reduce fish noise.",
            possible_drift_risk="May over-filter.",
        ),
        child_query_id="Q0002",
        iteration=2,
    ).child_query
    duplicate = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-exclude-fish-again",
        parent_query_id="Q0002",
        target_concept_block="prohibited_or_rejected_terms",
        operation="add",
        terms_added=["fish"],
        terms_removed=[],
        evidence_document_ids=[],
        rationale="Duplicate noise term.",
        expected_effect="No change.",
        possible_drift_risk="No change.",
    )

    result = QueryPatchApplier().apply(
        parent, duplicate, child_query_id="Q0003", iteration=3
    )

    assert result.status == "rejected"
    assert result.reason == "duplicate_addition"


def test_query_refinement_writes_deterministic_candidate_selection(tmp_path: Path) -> None:
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal article"]},
            "emerging_contaminant_terms": ["PFAS"],
            "surface_water_terms": ["river"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-09",
    )
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    patches = [
        QueryPatch(
            query_patch_schema_version="1.0.0",
            patch_id="patch-estuary",
            parent_query_id="Q0001",
            target_concept_block="surface_water_terms",
            operation="add",
            terms_added=["estuary"],
            terms_removed=[],
            evidence_document_ids=["doi:10.1000/live"],
            rationale="Included records mention estuaries.",
            expected_effect="Increase estuary-specific novelty.",
            possible_drift_risk="May broaden beyond rivers.",
        ),
        QueryPatch(
            query_patch_schema_version="1.0.0",
            patch_id="patch-remove-river",
            parent_query_id="Q0001",
            target_concept_block="surface_water_terms",
            operation="remove",
            terms_added=[],
            terms_removed=["river"],
            evidence_document_ids=["doi:10.1000/live"],
            rationale="River term is too broad.",
            expected_effect="Reduce broad river noise.",
            possible_drift_risk="May lose river studies.",
        ),
    ]
    candidates = []
    applier = QueryPatchApplier()
    metrics = _metrics(
        iteration=1,
        query_id="Q0001",
        parent_query_id=None,
        defer_rate=0.1,
    )
    for patch in patches:
        result = applier.apply(parent, patch, child_query_id=patch.patch_id, iteration=2)
        assert result.status == "applied"
        candidates.append(
            type(
                "Candidate",
                (),
                {
                    "query": result.child_query,
                    "patch_result": result,
                    "limited_evaluation": None,
                },
            )()
        )
    selected = runner._select_query_candidate(candidates, parent_metrics=metrics)  # noqa: SLF001
    runner._write_query_refinement_decision(  # noqa: SLF001
        run_dir=tmp_path,
        parent=parent,
        selected=selected,
        candidates=candidates,
        rejected_refs=["runs/test/query_refinement/applied/rejected/query_patch.json"],
        metrics=metrics,
    )

    decision = json.loads(
        (
            tmp_path
            / "query_refinement"
            / f"{selected.query.query_id}_candidate_selection.json"
        ).read_text(encoding="utf-8")
    )
    assert decision["selection_actor"] == "Retrieval Specialist"
    assert decision["selection_method"] == "deterministic_candidate_selector"
    assert decision["selected_patch_id"] == "patch-remove-river"
    assert decision["candidate_patch_ids"] == ["patch-estuary", "patch-remove-river"]
    assert decision["acceptance_thresholds"]["source_execution_must_be_complete"] is True


def test_query_refinement_selects_completed_limited_novelty_evaluation(
    tmp_path: Path,
) -> None:
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal article"]},
            "emerging_contaminant_terms": ["PFAS"],
            "surface_water_terms": ["river"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-09",
    )
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    ControlPlane(tmp_path / "control.sqlite3", "test").migrate()
    applier = QueryPatchApplier()
    metrics = _metrics(
        iteration=1,
        query_id="Q0001",
        parent_query_id=None,
        defer_rate=0.1,
    )
    patch_a = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-a",
        parent_query_id="Q0001",
        target_concept_block="surface_water_terms",
        operation="add",
        terms_added=["estuary"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/a"],
        rationale="Estuary evidence.",
        expected_effect="More estuary records.",
        possible_drift_risk="May broaden scope.",
    )
    patch_b = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-b",
        parent_query_id="Q0001",
        target_concept_block="monitoring_and_concentration_terms",
        operation="add",
        terms_added=["measured concentration"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/b"],
        rationale="Measured concentration evidence.",
        expected_effect="More measured concentration records.",
        possible_drift_risk="May narrow recall.",
    )
    result_a = applier.apply(parent, patch_a, child_query_id="Q0002", iteration=2)
    result_b = applier.apply(parent, patch_b, child_query_id="Q0002_C2", iteration=2)
    candidates = [
        type(
            "Candidate",
            (),
            {
                "query": result_a.child_query,
                "patch_result": result_a,
                "limited_evaluation": {
                    "evaluation_status": "completed",
                    "score_delta": 0.01,
                    "conservative_utility": -1.0,
                    "pairwise_safety_gate": "pass",
                    "novel_precision_at_20": 0.4,
                    "defer_rate": 0.1,
                    "excluded_matrix_rate": 0.1,
                    "source_completeness": "complete",
                },
            },
        )(),
        type(
            "Candidate",
            (),
            {
                "query": result_b.child_query,
                "patch_result": result_b,
                "limited_evaluation": {
                    "evaluation_status": "completed",
                    "score_delta": 0.03,
                    "conservative_utility": 1.0,
                    "pairwise_safety_gate": "pass",
                    "gain_count": 3,
                    "loss_count": 0,
                    "novel_precision_at_20": 0.5,
                    "defer_rate": 0.1,
                    "excluded_matrix_rate": 0.0,
                    "source_completeness": "complete",
                },
            },
        )(),
    ]

    selected = runner._select_query_candidate(candidates, parent_metrics=metrics)  # noqa: SLF001

    assert selected.patch_result.patch.patch_id == "patch-b"


def test_query_refinement_preserves_selected_candidate_query_id(
    tmp_path: Path,
) -> None:
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal article"]},
            "emerging_contaminant_terms": ["PFAS"],
            "surface_water_terms": ["river"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-09",
    )
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    applier = QueryPatchApplier()
    metrics = _metrics(
        iteration=1,
        query_id="Q0001",
        parent_query_id=None,
        defer_rate=0.1,
    )
    patch_a = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-a",
        parent_query_id="Q0001",
        target_concept_block="surface_water_terms",
        operation="add",
        terms_added=["estuary"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/a"],
        rationale="Estuary evidence.",
        expected_effect="More estuary records.",
        possible_drift_risk="May broaden scope.",
    )
    patch_b = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-b",
        parent_query_id="Q0001",
        target_concept_block="monitoring_and_concentration_terms",
        operation="add",
        terms_added=["measured concentration"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/b"],
        rationale="Measured concentration evidence.",
        expected_effect="More measured concentration records.",
        possible_drift_risk="May narrow recall.",
    )
    result_a = applier.apply(parent, patch_a, child_query_id="Q0002", iteration=2)
    result_b = applier.apply(parent, patch_b, child_query_id="Q0002_C2", iteration=2)
    candidates = [
        type(
            "Candidate",
            (),
            {
                "query": result_a.child_query,
                "patch_result": result_a,
                "limited_evaluation": {
                    "evaluation_status": "completed",
                    "score_delta": 0.01,
                    "conservative_utility": -1.0,
                    "pairwise_safety_gate": "pass",
                    "novel_precision_at_20": 0.4,
                    "defer_rate": 0.1,
                    "excluded_matrix_rate": 0.1,
                    "source_completeness": "complete",
                },
            },
        )(),
        type(
            "Candidate",
            (),
            {
                "query": result_b.child_query,
                "patch_result": result_b,
                "limited_evaluation": {
                    "evaluation_status": "completed",
                    "score_delta": 0.03,
                    "conservative_utility": 1.0,
                    "pairwise_safety_gate": "pass",
                    "gain_count": 3,
                    "loss_count": 0,
                    "novel_precision_at_20": 0.5,
                    "defer_rate": 0.1,
                    "excluded_matrix_rate": 0.0,
                    "source_completeness": "complete",
                },
            },
        )(),
    ]

    selected = runner._select_query_candidate(candidates, parent_metrics=metrics)  # noqa: SLF001
    runner._write_query_refinement_decision(  # noqa: SLF001
        run_dir=tmp_path,
        parent=parent,
        selected=selected,
        candidates=candidates,
        rejected_refs=[],
        metrics=metrics,
    )

    decision = read_json(tmp_path / "query_refinement" / "Q0002_C2_candidate_selection.json")
    assert selected.query.query_id == "Q0002_C2"
    assert decision["selected_query_id"] == "Q0002_C2"
    assert decision["selected_patch_id"] == "patch-b"


def test_query_refinement_candidate_selector_penalizes_incomplete_sources(
    tmp_path: Path,
) -> None:
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal article"]},
            "emerging_contaminant_terms": ["PFAS"],
            "surface_water_terms": ["river"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-09",
    )
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    applier = QueryPatchApplier()
    metrics = _metrics(
        iteration=1,
        query_id="Q0001",
        parent_query_id=None,
        defer_rate=0.1,
    )
    patch_partial = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-partial",
        parent_query_id="Q0001",
        target_concept_block="surface_water_terms",
        operation="add",
        terms_added=["estuary"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/a"],
        rationale="Estuary evidence.",
        expected_effect="More estuary records.",
        possible_drift_risk="May broaden scope.",
    )
    patch_complete = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-complete",
        parent_query_id="Q0001",
        target_concept_block="monitoring_and_concentration_terms",
        operation="add",
        terms_added=["measured concentration"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/b"],
        rationale="Measured concentration evidence.",
        expected_effect="More measured concentration records.",
        possible_drift_risk="May narrow recall.",
    )
    result_partial = applier.apply(
        parent, patch_partial, child_query_id="Q0002", iteration=2
    )
    result_complete = applier.apply(
        parent, patch_complete, child_query_id="Q0002_C2", iteration=2
    )
    candidates = [
        type(
            "Candidate",
            (),
            {
                "query": result_partial.child_query,
                "patch_result": result_partial,
                "limited_evaluation": {
                    "evaluation_status": "completed",
                    "score_delta": 0.5,
                    "conservative_utility": 10.0,
                    "pairwise_safety_gate": "pass",
                    "gain_count": 5,
                    "loss_count": 0,
                    "novel_precision_at_20": 0.5,
                    "defer_rate": 0.1,
                    "excluded_matrix_rate": 0.0,
                    "source_completeness": "partial",
                },
            },
        )(),
        type(
            "Candidate",
            (),
            {
                "query": result_complete.child_query,
                "patch_result": result_complete,
                "limited_evaluation": {
                    "evaluation_status": "completed",
                    "score_delta": 0.03,
                    "conservative_utility": 1.0,
                    "pairwise_safety_gate": "pass",
                    "gain_count": 3,
                    "loss_count": 0,
                    "novel_precision_at_20": 0.5,
                    "defer_rate": 0.1,
                    "excluded_matrix_rate": 0.0,
                    "source_completeness": "complete",
                },
            },
        )(),
    ]

    selected = runner._select_query_candidate(candidates, parent_metrics=metrics)  # noqa: SLF001

    assert selected.patch_result.patch.patch_id == "patch-complete"


def test_query_refinement_candidate_selector_prefers_threshold_passing_candidate(
    tmp_path: Path,
) -> None:
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal article"]},
            "emerging_contaminant_terms": ["PFAS"],
            "surface_water_terms": ["river"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-09",
    )
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    applier = QueryPatchApplier()
    metrics = _metrics(
        iteration=1,
        query_id="Q0001",
        parent_query_id=None,
        novel_precision_at_20=0.5,
        defer_rate=0.1,
        excluded_matrix_rate=0.0,
    )
    patch_fails = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-score-only",
        parent_query_id="Q0001",
        target_concept_block="surface_water_terms",
        operation="add",
        terms_added=["estuary"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/a"],
        rationale="High score but precision collapse.",
        expected_effect="Possibly higher score.",
        possible_drift_risk="Precision drift.",
    )
    patch_passes = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-threshold-pass",
        parent_query_id="Q0001",
        target_concept_block="monitoring_and_concentration_terms",
        operation="add",
        terms_added=["measured concentration"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/b"],
        rationale="Measured concentration evidence.",
        expected_effect="Higher precision.",
        possible_drift_risk="May narrow recall.",
    )
    result_fails = applier.apply(parent, patch_fails, child_query_id="Q0002", iteration=2)
    result_passes = applier.apply(
        parent,
        patch_passes,
        child_query_id="Q0002_C2",
        iteration=2,
    )
    candidates = [
        type(
            "Candidate",
            (),
            {
                "query": result_fails.child_query,
                "patch_result": result_fails,
                "limited_evaluation": {
                    "evaluation_status": "completed",
                    "score_delta": 0.2,
                    "conservative_utility": 10.0,
                    "pairwise_safety_gate": "pass",
                    "gain_count": 6,
                    "loss_count": 0,
                    "novel_precision_at_20": 0.0,
                    "defer_rate": 0.1,
                    "excluded_matrix_rate": 0.0,
                    "source_completeness": "complete",
                },
            },
        )(),
        type(
            "Candidate",
            (),
            {
                "query": result_passes.child_query,
                "patch_result": result_passes,
                "limited_evaluation": {
                    "evaluation_status": "completed",
                    "score_delta": 0.03,
                    "conservative_utility": 1.0,
                    "pairwise_safety_gate": "pass",
                    "gain_count": 3,
                    "loss_count": 0,
                    "novel_precision_at_20": 0.48,
                    "defer_rate": 0.12,
                    "excluded_matrix_rate": 0.0,
                    "source_completeness": "complete",
                },
            },
        )(),
    ]

    selected = runner._select_query_candidate(candidates, parent_metrics=metrics)  # noqa: SLF001

    assert selected.patch_result.patch.patch_id == "patch-threshold-pass"


def test_query_refinement_candidate_selector_rejects_all_failed_candidates(
    tmp_path: Path,
) -> None:
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal article"]},
            "emerging_contaminant_terms": ["PFAS"],
            "surface_water_terms": ["river"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-09",
    )
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    applier = QueryPatchApplier()
    metrics = _metrics(
        iteration=1,
        query_id="Q0001",
        parent_query_id=None,
        novel_precision_at_20=0.5,
        defer_rate=0.1,
        excluded_matrix_rate=0.0,
    )
    patch_low_score = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-low-score",
        parent_query_id="Q0001",
        target_concept_block="surface_water_terms",
        operation="add",
        terms_added=["estuary"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/a"],
        rationale="Insufficient score improvement.",
        expected_effect="Small precision change.",
        possible_drift_risk="May broaden scope.",
    )
    patch_partial = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-partial-source",
        parent_query_id="Q0001",
        target_concept_block="monitoring_and_concentration_terms",
        operation="add",
        terms_added=["measured concentration"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/b"],
        rationale="Apparent improvement from incomplete source execution.",
        expected_effect="Higher score.",
        possible_drift_risk="May hide source failure.",
    )
    result_low_score = applier.apply(
        parent,
        patch_low_score,
        child_query_id="Q0002",
        iteration=2,
    )
    result_partial = applier.apply(
        parent,
        patch_partial,
        child_query_id="Q0002_C2",
        iteration=2,
    )
    candidates = [
        type(
            "Candidate",
            (),
            {
                "query": result_low_score.child_query,
                "patch_result": result_low_score,
                "limited_evaluation": {
                    "evaluation_status": "completed",
                    "score_delta": 0.019,
                    "conservative_utility": 1.0,
                    "pairwise_safety_gate": "pass",
                    "gain_count": 2,
                    "loss_count": 0,
                    "novel_precision_at_20": 0.5,
                    "defer_rate": 0.1,
                    "excluded_matrix_rate": 0.0,
                    "source_completeness": "complete",
                },
            },
        )(),
        type(
            "Candidate",
            (),
            {
                "query": result_partial.child_query,
                "patch_result": result_partial,
                "limited_evaluation": {
                    "evaluation_status": "completed",
                    "score_delta": 0.2,
                    "conservative_utility": 10.0,
                    "pairwise_safety_gate": "fail",
                    "novel_precision_at_20": 0.5,
                    "defer_rate": 0.1,
                    "excluded_matrix_rate": 0.0,
                    "source_completeness": "partial",
                },
            },
        )(),
    ]

    selected = runner._select_query_candidate(candidates, parent_metrics=metrics)  # noqa: SLF001
    runner._write_query_refinement_decision(  # noqa: SLF001
        run_dir=tmp_path,
        parent=parent,
        selected=selected,
        candidates=candidates,
        rejected_refs=[],
        metrics=metrics,
    )

    decision = read_json(
        tmp_path / "query_refinement" / "Q0001_no_candidate_accepted_candidate_selection.json"
    )
    assert selected is None
    assert decision["selected_query_id"] is None
    assert decision["selected_patch_id"] is None
    assert "parent query remains accepted" in decision["selection_reason"]


def test_expansion_candidate_can_pass_with_included_gain_and_bounded_loss_risk(
    tmp_path: Path,
) -> None:
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal article"]},
            "emerging_contaminant_terms": ["PFAS"],
            "surface_water_terms": ["river"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-09",
    )
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    metrics = _metrics(
        iteration=1,
        query_id="Q0001",
        parent_query_id=None,
        defer_rate=0.1,
    )
    patch = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-negative-utility",
        parent_query_id="Q0001",
        target_concept_block="surface_water_terms",
        operation="add",
        terms_added=["estuary"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/a"],
        rationale="Looks good on score but fails conservative utility.",
        expected_effect="Higher score.",
        possible_drift_risk="Could lose parent relevant records.",
    )
    result = QueryPatchApplier().apply(parent, patch, child_query_id="Q0002", iteration=2)
    candidates = [
        type(
            "Candidate",
            (),
            {
                "query": result.child_query,
                "patch_result": result,
                "limited_evaluation": {
                    "evaluation_status": "completed",
                    "score_delta": 0.5,
                    "patch_type": "expansion",
                    "result_set_change_status": "changed",
                    "conservative_utility": -0.01,
                    "pairwise_safety_gate": "pass",
                    "gain_count": 8,
                    "loss_count": 4,
                    "gain_include_count": 2,
                    "known_loss_include_count": 0,
                    "loss_audit_include_count": 0,
                    "novel_precision_at_20": 0.5,
                    "defer_rate": 0.1,
                    "excluded_matrix_rate": 0.0,
                    "source_completeness": "complete",
                },
            },
        )()
    ]

    selected = runner._select_query_candidate(candidates, parent_metrics=metrics)  # noqa: SLF001

    assert selected is candidates[0]


def test_expansion_candidate_rejects_include_loss_even_with_positive_utility(
    tmp_path: Path,
) -> None:
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal article"]},
            "emerging_contaminant_terms": ["PFAS"],
            "surface_water_terms": ["river"],
            "monitoring_and_concentration_terms": ["occurrence"],
        },
        "2026-07-09",
    )
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    patch = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-loss-risk",
        parent_query_id="Q0001",
        target_concept_block="monitoring_and_concentration_terms",
        operation="add",
        terms_added=["field monitoring"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/include-a"],
        rationale="Included evidence supports field monitoring.",
        expected_effect="Retrieve more field monitoring records.",
        possible_drift_risk="Could displace known include records under source rank limits.",
    )
    result = QueryPatchApplier().apply(parent, patch, child_query_id="Q0002", iteration=2)
    candidates = [
        type(
            "Candidate",
            (),
            {
                "query": result.child_query,
                "patch_result": result,
                "limited_evaluation": {
                    "evaluation_status": "completed",
                    "patch_type": "expansion",
                    "result_set_change_status": "changed",
                    "score_delta": 0.5,
                    "conservative_utility": 10.0,
                    "pairwise_safety_gate": "pass",
                    "gain_count": 8,
                    "loss_count": 1,
                    "gain_include_count": 4,
                    "known_loss_include_count": 1,
                    "loss_audit_include_count": 0,
                    "novel_precision_at_20": 0.5,
                    "defer_rate": 0.1,
                    "excluded_matrix_rate": 0.0,
                    "source_completeness": "complete",
                },
            },
        )()
    ]

    selected = runner._select_query_candidate(  # noqa: SLF001
        candidates,
        parent_metrics=_metrics(
            iteration=1,
            query_id="Q0001",
            parent_query_id=None,
            novel_precision_at_20=0.4,
            defer_rate=0.1,
            excluded_matrix_rate=0.0,
        ),
    )

    assert selected is None


def test_candidate_pairwise_evaluation_counts_defer_as_screened_gain(
    tmp_path: Path,
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    evaluation = runner._candidate_pairwise_evaluation(  # noqa: SLF001
        "run",
        None,
        "Q0002",
        patch_type="expansion",
        include_count=0,
        exclude_count=0,
        defer_count=2,
        excluded_matrix_rate=0.0,
    )

    assert evaluation["gain_screened_count"] == 2
    assert evaluation["gain_defer_count"] == 2


def test_query_refinement_candidate_selector_rejects_unchanged_result_set(
    tmp_path: Path,
) -> None:
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal article"]},
            "emerging_contaminant_terms": ["PFAS"],
            "surface_water_terms": ["river"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-09",
    )
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    patch = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-no-result-change",
        parent_query_id="Q0001",
        target_concept_block="surface_water_terms",
        operation="add",
        terms_added=["estuary"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/a"],
        rationale="Apparently useful but no result-set change.",
        expected_effect="Higher score.",
        possible_drift_risk="None observed.",
    )
    result = QueryPatchApplier().apply(parent, patch, child_query_id="Q0002", iteration=2)
    candidates = [
        type(
            "Candidate",
            (),
            {
                "query": result.child_query,
                "patch_result": result,
                "limited_evaluation": {
                    "evaluation_status": "completed",
                    "source_completeness": "complete",
                    "pairwise_safety_gate": "pass",
                    "conservative_utility": 5.0,
                    "score_delta": 0.5,
                    "gain_count": 0,
                    "loss_count": 0,
                    "novel_precision_at_20": 0.5,
                    "defer_rate": 0.0,
                    "excluded_matrix_rate": 0.0,
                },
            },
        )()
    ]

    selected = runner._select_query_candidate(  # noqa: SLF001
        candidates,
        parent_metrics=_metrics(iteration=1, query_id="Q0001", parent_query_id=None),
    )

    assert selected is None


def test_noise_reduction_candidate_can_pass_without_score_delta_threshold(
    tmp_path: Path,
) -> None:
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal article"]},
            "emerging_contaminant_terms": ["PFAS"],
            "surface_water_terms": ["river"],
            "monitoring_and_concentration_terms": ["concentration"],
            "prohibited_or_rejected_terms": ["review"],
        },
        "2026-07-09",
    )
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    metrics = _metrics(
        iteration=1,
        query_id="Q0001",
        parent_query_id=None,
        novel_precision_at_20=0.2,
        defer_rate=0.1,
        excluded_matrix_rate=0.2,
    )
    patch = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-noise-remediation",
        parent_query_id="Q0001",
        target_concept_block="prohibited_or_rejected_terms",
        operation="add",
        terms_added=["remediation"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/noise"],
        rationale="Repeated treatment/remediation noise.",
        expected_effect="Lower screening burden.",
        possible_drift_risk="Could remove field studies mentioning remediation.",
    )
    result = QueryPatchApplier().apply(parent, patch, child_query_id="Q0002", iteration=2)
    candidates = [
        type(
            "Candidate",
            (),
            {
                "query": result.child_query,
                "patch_result": result,
                "limited_evaluation": {
                    "evaluation_status": "completed",
                    "patch_type": "noise_reduction",
                    "score_delta": 0.005,
                    "conservative_utility": 5.0,
                    "noise_reduction_utility": 5.0,
                    "pairwise_safety_gate": "pass",
                    "loss_count": 20,
                    "loss_audit_target_records": 10,
                    "loss_audit_screened_count": 10,
                    "loss_audit_include_count": 0,
                    "novel_precision_at_20": 0.0,
                    "defer_rate": 0.0,
                    "excluded_matrix_rate": 0.1,
                    "source_completeness": "complete",
                },
            },
        )()
    ]

    selected = runner._select_query_candidate(candidates, parent_metrics=metrics)  # noqa: SLF001

    assert selected is candidates[0]


def test_expansion_candidate_can_pass_with_positive_conservative_utility_without_score_delta(
    tmp_path: Path,
) -> None:
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal article"]},
            "emerging_contaminant_terms": ["emerging contaminant*"],
            "surface_water_terms": ["river"],
            "monitoring_and_concentration_terms": ["occurrence"],
        },
        "2026-07-09",
    )
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    metrics = _metrics(
        iteration=1,
        query_id="Q0001",
        parent_query_id=None,
        novel_precision_at_20=0.333,
        defer_rate=0.16,
        excluded_matrix_rate=0.16,
    )
    patch = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-microplastics-expansion",
        parent_query_id="Q0001",
        target_concept_block="emerging_contaminant_terms",
        operation="add",
        terms_added=["microplastic*"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/include-a", "doi:10.1000/include-b"],
        rationale="Included evidence repeatedly names microplastics.",
        expected_effect="Retrieve more natural-water microplastic occurrence records.",
        possible_drift_risk="Could add biota or treatment noise.",
    )
    result = QueryPatchApplier().apply(parent, patch, child_query_id="Q0002", iteration=2)
    candidates = [
        type(
            "Candidate",
            (),
            {
                "query": result.child_query,
                "patch_result": result,
                "limited_evaluation": {
                    "evaluation_status": "completed",
                    "patch_type": "expansion",
                    "result_set_change_status": "changed",
                    "source_completeness": "complete",
                    "pairwise_safety_gate": "pass",
                    "conservative_utility": 2.1,
                    "score_delta": -0.14,
                    "gain_count": 16,
                    "loss_count": 16,
                    "gain_include_count": 4,
                    "known_loss_include_count": 0,
                    "loss_audit_include_count": 0,
                    "novel_precision_at_20": 0.4,
                    "defer_rate": 0.166,
                    "excluded_matrix_rate": 0.0,
                },
            },
        )()
    ]

    selected = runner._select_query_candidate(candidates, parent_metrics=metrics)  # noqa: SLF001

    assert selected is candidates[0]


def test_expansion_candidate_rejects_loss_audit_include_even_with_positive_utility(
    tmp_path: Path,
) -> None:
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal article"]},
            "emerging_contaminant_terms": ["emerging contaminant*"],
            "surface_water_terms": ["river"],
            "monitoring_and_concentration_terms": ["occurrence"],
        },
        "2026-07-09",
    )
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    patch = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-risky-expansion",
        parent_query_id="Q0001",
        target_concept_block="emerging_contaminant_terms",
        operation="add",
        terms_added=["microplastic*"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/include-a"],
        rationale="Included evidence names microplastics.",
        expected_effect="Retrieve more microplastic records.",
        possible_drift_risk="Could lose parent relevant records.",
    )
    result = QueryPatchApplier().apply(parent, patch, child_query_id="Q0002", iteration=2)
    candidates = [
        type(
            "Candidate",
            (),
            {
                "query": result.child_query,
                "patch_result": result,
                "limited_evaluation": {
                    "evaluation_status": "completed",
                    "patch_type": "expansion",
                    "result_set_change_status": "changed",
                    "source_completeness": "complete",
                    "pairwise_safety_gate": "pass",
                    "conservative_utility": 5.0,
                    "score_delta": 0.2,
                    "gain_count": 20,
                    "loss_count": 5,
                    "gain_include_count": 6,
                    "known_loss_include_count": 1,
                    "loss_audit_include_count": 1,
                    "novel_precision_at_20": 0.6,
                    "defer_rate": 0.0,
                    "excluded_matrix_rate": 0.0,
                },
            },
        )()
    ]

    selected = runner._select_query_candidate(  # noqa: SLF001
        candidates,
        parent_metrics=_metrics(iteration=1, query_id="Q0001", parent_query_id=None),
    )

    assert selected is None


def test_noise_reduction_candidate_needs_loss_audit_evidence(
    tmp_path: Path,
) -> None:
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal article"]},
            "emerging_contaminant_terms": ["PFAS"],
            "surface_water_terms": ["river"],
            "monitoring_and_concentration_terms": ["concentration"],
            "prohibited_or_rejected_terms": ["review"],
        },
        "2026-07-09",
    )
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    metrics = _metrics(iteration=1, query_id="Q0001", parent_query_id=None)
    patch = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-noise-remediation",
        parent_query_id="Q0001",
        target_concept_block="prohibited_or_rejected_terms",
        operation="add",
        terms_added=["remediation"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/noise"],
        rationale="Repeated treatment/remediation noise.",
        expected_effect="Lower screening burden.",
        possible_drift_risk="Could remove field studies mentioning remediation.",
    )
    result = QueryPatchApplier().apply(parent, patch, child_query_id="Q0002", iteration=2)
    candidates = [
        type(
            "Candidate",
            (),
            {
                "query": result.child_query,
                "patch_result": result,
                "limited_evaluation": {
                    "evaluation_status": "completed",
                    "patch_type": "noise_reduction",
                    "score_delta": 0.2,
                    "conservative_utility": 5.0,
                    "noise_reduction_utility": 5.0,
                    "pairwise_safety_gate": "pass",
                    "loss_count": 20,
                    "loss_audit_target_records": 10,
                    "loss_audit_screened_count": 2,
                    "loss_audit_include_count": 0,
                    "novel_precision_at_20": 0.0,
                    "defer_rate": 0.0,
                    "excluded_matrix_rate": 0.0,
                    "source_completeness": "complete",
                },
            },
        )()
    ]

    selected = runner._select_query_candidate(candidates, parent_metrics=metrics)  # noqa: SLF001

    assert selected is None


def test_candidate_pairwise_evaluation_penalizes_known_parent_include_loss(
    tmp_path: Path,
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    plane = ControlPlane(tmp_path / "control.sqlite3", "test")
    plane.migrate()
    run_id = "run-pairwise"
    with plane.transaction() as connection:
        connection.execute(
            """
            INSERT INTO runs (
                run_id, status, completeness, date_from, date_to, current_state,
                started_at, code_commit_sha, git_branch, config_hash, prompt_hash,
                protocol_version, scoring_version, model_version, source_status_json
            )
            VALUES (?, 'running', 'complete', '2006-01-01', '2026-07-09',
                    'EVALUATE_QUERY', '2026-07-09T00:00:00Z', 'test', 'test',
                    'test', 'test', 'test', 'test', 'test', '{}')
            """,
            (run_id,),
        )
        for gid, status in [
            ("doi:10.1000/lost-include", "include"),
            ("doi:10.1000/overlap", "exclude"),
            ("doi:10.1000/gain", None),
        ]:
            connection.execute(
                """
                INSERT INTO documents (
                    global_record_id, canonical_title, normalized_title,
                    keywords_json, authors_json, issn_json, eissn_json,
                    sampled_matrices_json, article_ec_scope, first_seen_run_id,
                    first_seen_query_id, first_seen_at, latest_metadata_version,
                    current_screening_status
                )
                VALUES (?, ?, ?, '[]', '[]', '[]', '[]', '[]', 'uncertain',
                        ?, ?, '2026-07-09T00:00:00Z', 'test', ?)
                """,
                (gid, gid, gid, run_id, "Q0001", status),
            )
        for gid in ["doi:10.1000/lost-include", "doi:10.1000/overlap"]:
            connection.execute(
                """
                INSERT INTO document_query_membership (
                    global_record_id, run_id, query_id, iteration, source_name,
                    source_rank, first_seen_in_query, already_known_before_query,
                    included_in_novelty_sample, novelty_sample_position,
                    screening_status_at_iteration, created_at
                )
                VALUES (?, ?, 'Q0001', 1, 'crossref', 1, 1, 0, 0, NULL, NULL,
                        '2026-07-09T00:00:00Z')
                """,
                (gid, run_id),
            )
        for gid in ["doi:10.1000/overlap", "doi:10.1000/gain"]:
            connection.execute(
                """
                INSERT INTO document_query_membership (
                    global_record_id, run_id, query_id, iteration, source_name,
                    source_rank, first_seen_in_query, already_known_before_query,
                    included_in_novelty_sample, novelty_sample_position,
                    screening_status_at_iteration, created_at
                )
                VALUES (?, ?, 'Q0002', 2, 'crossref', 1, 1, 0, 1, 1, NULL,
                        '2026-07-09T00:00:00Z')
                """,
                (gid, run_id),
            )

    evaluation = runner._candidate_pairwise_evaluation(  # noqa: SLF001
        run_id,
        "Q0001",
        "Q0002",
        include_count=1,
        exclude_count=0,
        defer_count=0,
        excluded_matrix_rate=0.0,
    )

    assert evaluation["gain_count"] == 1
    assert evaluation["loss_count"] == 1
    assert evaluation["known_loss_include_count"] == 1
    assert evaluation["pairwise_safety_gate"] == "pass"
    assert evaluation["conservative_utility"] < 0


def test_candidate_pairwise_evaluation_hard_fails_replacement_include_loss(
    tmp_path: Path,
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    plane = ControlPlane(tmp_path / "control.sqlite3", "test")
    plane.migrate()
    run_id = "run-replacement-loss"
    with plane.transaction() as connection:
        connection.execute(
            """
            INSERT INTO runs (
                run_id, status, completeness, date_from, date_to, current_state,
                started_at, code_commit_sha, git_branch, config_hash, prompt_hash,
                protocol_version, scoring_version, model_version, source_status_json
            )
            VALUES (?, 'running', 'complete', '2006-01-01', '2026-07-09',
                    'EVALUATE_QUERY', '2026-07-09T00:00:00Z', 'test', 'test',
                    'test', 'test', 'test', 'test', 'test', '{}')
            """,
            (run_id,),
        )
        connection.execute(
            """
            INSERT INTO documents (
                global_record_id, canonical_title, normalized_title,
                keywords_json, authors_json, issn_json, eissn_json,
                sampled_matrices_json, article_ec_scope, first_seen_run_id,
                first_seen_query_id, first_seen_at, latest_metadata_version,
                current_screening_status
            )
            VALUES ('doi:10.1000/lost-include', 'lost', 'lost', '[]', '[]',
                    '[]', '[]', '[]', 'uncertain', ?, 'Q0001',
                    '2026-07-09T00:00:00Z', 'test', 'include')
            """,
            (run_id,),
        )
        connection.execute(
            """
            INSERT INTO document_query_membership (
                global_record_id, run_id, query_id, iteration, source_name,
                source_rank, first_seen_in_query, already_known_before_query,
                included_in_novelty_sample, novelty_sample_position,
                screening_status_at_iteration, created_at
            )
            VALUES ('doi:10.1000/lost-include', ?, 'Q0001', 1, 'crossref', 1,
                    1, 0, 0, NULL, NULL, '2026-07-09T00:00:00Z')
            """,
            (run_id,),
        )

    evaluation = runner._candidate_pairwise_evaluation(  # noqa: SLF001
        run_id,
        "Q0001",
        "Q0002",
        patch_type="replacement",
        include_count=2,
        exclude_count=0,
        defer_count=0,
        excluded_matrix_rate=0.0,
    )

    assert evaluation["known_loss_include_count"] == 1
    assert evaluation["pairwise_safety_gate"] == "fail"


def test_expansion_candidate_rejects_include_loss_from_topk_displacement(
    tmp_path: Path,
) -> None:
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal article"]},
            "emerging_contaminant_terms": ["emerging contaminant"],
            "surface_water_terms": ["river"],
            "monitoring_and_concentration_terms": ["occurrence"],
        },
        "2026-07-09",
    )
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    metrics = _metrics(
        iteration=1,
        query_id="Q0001",
        parent_query_id=None,
        novel_precision_at_20=0.25,
        defer_rate=0.2,
        excluded_matrix_rate=0.16,
    )
    patch = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-microplastics",
        parent_query_id="Q0001",
        target_concept_block="emerging_contaminant_terms",
        operation="add",
        terms_added=["microplastics"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/include-a"],
        rationale="Supported contaminant class with strong gain sample.",
        expected_effect="Increase topical surface-water occurrence hits.",
        possible_drift_risk="May displace some parent top-k records.",
    )
    result = QueryPatchApplier().apply(parent, patch, child_query_id="Q0002", iteration=2)
    candidates = [
        type(
            "Candidate",
            (),
            {
                "query": result.child_query,
                "patch_result": result,
                "limited_evaluation": {
                    "evaluation_status": "completed",
                    "patch_type": "expansion",
                    "source_completeness": "complete",
                    "pairwise_safety_gate": "pass",
                    "conservative_utility": 2.0,
                    "score_delta": 0.12,
                    "gain_count": 17,
                    "loss_count": 16,
                    "gain_include_count": 4,
                    "known_loss_include_count": 0,
                    "loss_audit_include_count": 1,
                    "novel_precision_at_20": 0.63,
                    "defer_rate": 0.083,
                    "excluded_matrix_rate": 0.166,
                },
            },
        )()
    ]

    selected = runner._select_query_candidate(candidates, parent_metrics=metrics)  # noqa: SLF001

    assert selected is None


def test_candidate_pairwise_evaluation_scores_noise_reduction_loss_audit(
    tmp_path: Path,
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    plane = ControlPlane(tmp_path / "control.sqlite3", "test")
    plane.migrate()
    run_id = "run-noise-audit"
    with plane.transaction() as connection:
        connection.execute(
            """
            INSERT INTO runs (
                run_id, status, completeness, date_from, date_to, current_state,
                started_at, code_commit_sha, git_branch, config_hash, prompt_hash,
                protocol_version, scoring_version, model_version, source_status_json
            )
            VALUES (?, 'running', 'complete', '2006-01-01', '2026-07-09',
                    'EVALUATE_QUERY', '2026-07-09T00:00:00Z', 'test', 'test',
                    'test', 'test', 'test', 'test', 'test', '{}')
            """,
            (run_id,),
        )
        for index in range(8):
            gid = f"doi:10.1000/lost-exclude-{index}"
            connection.execute(
                """
                INSERT INTO documents (
                    global_record_id, canonical_title, normalized_title,
                    keywords_json, authors_json, issn_json, eissn_json,
                    sampled_matrices_json, article_ec_scope, first_seen_run_id,
                    first_seen_query_id, first_seen_at, latest_metadata_version,
                    current_screening_status
                )
                VALUES (?, ?, ?, '[]', '[]', '[]', '[]', '[]', 'uncertain',
                        ?, 'Q0001', '2026-07-09T00:00:00Z', 'test', 'exclude')
                """,
                (gid, gid, gid, run_id),
            )
            connection.execute(
                """
                INSERT INTO document_query_membership (
                    global_record_id, run_id, query_id, iteration, source_name,
                    source_rank, first_seen_in_query, already_known_before_query,
                    included_in_novelty_sample, novelty_sample_position,
                    screening_status_at_iteration, created_at
                )
                VALUES (?, ?, 'Q0001', 1, 'crossref', ?, 1, 0, 0, NULL, NULL,
                        '2026-07-09T00:00:00Z')
                """,
                (gid, run_id, index + 1),
            )

    evaluation = runner._candidate_pairwise_evaluation(  # noqa: SLF001
        run_id,
        "Q0001",
        "Q0002",
        patch_type="noise_reduction",
        include_count=0,
        exclude_count=0,
        defer_count=0,
        excluded_matrix_rate=0.0,
        loss_audit_target_records=5,
    )

    assert evaluation["patch_type"] == "noise_reduction"
    assert evaluation["loss_count"] == 8
    assert evaluation["loss_audit_screened_count"] == 5
    assert evaluation["loss_audit_exclude_count"] == 5
    assert evaluation["noise_reduction_utility"] > 0
    assert evaluation["conservative_utility"] == evaluation["noise_reduction_utility"]


def test_candidate_loss_audit_screens_unknown_parent_loss_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    plane = ControlPlane(tmp_path / "control.sqlite3", "test")
    plane.migrate()
    run_id = "run-loss-audit-screening"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    with plane.transaction() as connection:
        connection.execute(
            """
            INSERT INTO runs (
                run_id, status, completeness, date_from, date_to, current_state,
                started_at, code_commit_sha, git_branch, config_hash, prompt_hash,
                protocol_version, scoring_version, model_version, source_status_json
            )
            VALUES (?, 'running', 'complete', '2006-01-01', '2026-07-09',
                    'EVALUATE_QUERY', '2026-07-09T00:00:00Z', 'test', 'test',
                    'test', 'test', 'test', 'test', 'test', '{}')
            """,
            (run_id,),
        )
        for gid in ["doi:10.1000/lost-1", "doi:10.1000/overlap"]:
            connection.execute(
                """
                INSERT INTO documents (
                    global_record_id, canonical_title, normalized_title,
                    keywords_json, authors_json, issn_json, eissn_json,
                    sampled_matrices_json, article_ec_scope, first_seen_run_id,
                    first_seen_query_id, first_seen_at, latest_metadata_version
                )
                VALUES (?, ?, ?, '[]', '[]', '[]', '[]', '[]', 'uncertain',
                        ?, 'Q0001', '2026-07-09T00:00:00Z', 'test')
                """,
                (gid, gid, gid, run_id),
            )
        for query_id, gid in [
            ("Q0001", "doi:10.1000/lost-1"),
            ("Q0001", "doi:10.1000/overlap"),
            ("Q0002", "doi:10.1000/overlap"),
        ]:
            connection.execute(
                """
                INSERT INTO document_query_membership (
                    global_record_id, run_id, query_id, iteration, source_name,
                    source_rank, first_seen_in_query, already_known_before_query,
                    included_in_novelty_sample, novelty_sample_position,
                    screening_status_at_iteration, created_at
                )
                VALUES (?, ?, ?, 1, 'crossref', 1, 1, 0, 0, NULL, NULL,
                        '2026-07-09T00:00:00Z')
                """,
                (gid, run_id, query_id),
            )

    def fake_screen_one(
        self: TitleAbstractScreeningWorkerExecutor, record: NormalizedRecord, **kwargs: object
    ) -> ScreeningDecision:
        del self
        return ScreeningDecision(
            global_record_id=record.global_record_id,
            decision="exclude",
            reason_codes=["E_REVIEW"],
            evidence_spans=[record.title_original],
            article_ec_scope="false",
            screening_decision_id="screen-loss-audit",
            run_id=run_id,
            query_id="Q0001",
            iteration=0,
            audit_batch_id="loss_audit_test",
            prompt_version="test",
            prompt_hash="test",
            model_name="test",
            model_version="test",
            screening_timestamp="2026-07-09T00:00:01Z",
        )

    monkeypatch.setattr(TitleAbstractScreeningWorkerExecutor, "screen_one", fake_screen_one)
    monkeypatch.setattr(runner.screener, "prefilter_one", lambda *args, **kwargs: None)

    counts = runner._screen_candidate_loss_audit_live(  # noqa: SLF001
        run_id=run_id,
        run_dir=run_dir,
        parent_query_id="Q0001",
        candidate_query_id="Q0002",
        loaded=type(
            "Loaded",
            (),
            {
                "protocol": {"scie": {"registry_path": "missing-scie.csv"}},
                "stopping": {},
                "scoring": {},
            },
        )(),
        loss_audit_target_records=10,
    )

    assert counts["screening_decisions"] == 1
    with plane.connect() as connection:
        row = connection.execute(
            """
            SELECT decision
            FROM screening_decisions
            WHERE run_id = ? AND query_id = 'Q0001'
              AND global_record_id = 'doi:10.1000/lost-1'
              AND is_current = 1
            """,
            (run_id,),
        ).fetchone()
    assert row["decision"] == "exclude"


def test_query_refinement_writes_pending_limited_evaluation_ref(tmp_path: Path) -> None:
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal article"]},
            "emerging_contaminant_terms": ["PFAS"],
            "surface_water_terms": ["river"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-09",
    )
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    ControlPlane(tmp_path / "control.sqlite3", "test").migrate()
    patch = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-estuary",
        parent_query_id="Q0001",
        target_concept_block="surface_water_terms",
        operation="add",
        terms_added=["estuary"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/live"],
        rationale="Included records mention estuaries.",
        expected_effect="Increase estuary-specific novelty.",
        possible_drift_risk="May broaden beyond rivers.",
    )
    result = QueryPatchApplier().apply(parent, patch, child_query_id="Q0002", iteration=2)
    candidate = CandidateQueryPlan(
        query=result.child_query,
        plan=IterationPlan(
            query_id="Q0002",
            parent_query_id="Q0001",
            branch_id="test",
            acceptance_status="candidate",
            decision="accept",
            decision_reason="test",
            added_terms=patch.terms_added,
            removed_terms=patch.terms_removed,
            modified_blocks=[patch.target_concept_block],
            expected_effect=patch.expected_effect,
        ),
        patch_result=result,
    )

    evaluated = runner._attach_limited_candidate_evaluation(tmp_path, candidate)  # noqa: SLF001

    assert evaluated.limited_evaluation["evaluation_status"] == (
        "pending_limited_novelty_sample"
    )
    assert (
        tmp_path
        / "query_refinement"
        / "applied"
        / "Q0002"
        / "limited_novelty_evaluation.json"
    ).exists()
    assert (
        tmp_path
        / "query_refinement"
        / "candidate_evaluations"
        / "requests"
        / "Q0002.json"
    ).exists()


def test_term_mining_falls_back_to_text_when_prefilter_payload_has_empty_evidence(
    tmp_path: Path,
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    document = {
        "global_record_id": "doi:10.1000/include",
        "canonical_title": "Occurrence and distribution of PFAS in river surface water",
        "abstract_original": "Field sampling measured PFAS concentrations in river water.",
        "keywords_json": "[]",
        "study_type": "field monitoring",
        "payload_json": json.dumps(
            {
                "query_term_evidence": [],
                "included_waterbody_types": [],
                "study_type": "field monitoring",
            }
        ),
    }

    terms = runner._candidate_terms_from_document(document)  # noqa: SLF001

    assert "pfas" in terms
    assert "field sampling" in terms
    assert "surface water" in terms
    assert "concern surface water" not in terms
    assert "monitoring contaminants" not in terms
    assert "activities occurrence" not in terms
    assert "costs sampling" not in terms


def test_candidate_evaluation_uses_patch_specific_ref_when_query_id_is_reused(
    tmp_path: Path,
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal article"]},
            "emerging_contaminant_terms": ["PFAS"],
            "surface_water_terms": ["river"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-09",
    )
    first_patch = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-first",
        parent_query_id=parent.query_id,
        target_concept_block="surface_water_terms",
        operation="add",
        terms_added=["lake"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/a"],
        rationale="First candidate.",
        expected_effect="Test first candidate.",
        possible_drift_risk="Low.",
    )
    second_patch = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-second",
        parent_query_id=parent.query_id,
        target_concept_block="monitoring_and_concentration_terms",
        operation="add",
        terms_added=["field monitoring"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/b"],
        rationale="Retry candidate reusing Q0002.",
        expected_effect="Test retry candidate.",
        possible_drift_risk="Low.",
    )
    applier = QueryPatchApplier()
    first_result = applier.apply(parent, first_patch, child_query_id="Q0002", iteration=2)
    second_result = applier.apply(parent, second_patch, child_query_id="Q0002", iteration=2)
    runner._write_query_patch_result(tmp_path, parent, first_result)  # noqa: SLF001
    first_candidate = CandidateQueryPlan(
        query=first_result.child_query,
        plan=IterationPlan(
            query_id="Q0002",
            parent_query_id=parent.query_id,
            branch_id="first",
            acceptance_status="candidate",
            decision="accept",
            decision_reason="test",
            added_terms=["lake"],
            removed_terms=[],
            modified_blocks=["surface_water_terms"],
            expected_effect="test",
        ),
        patch_result=first_result,
    )
    runner._attach_limited_candidate_evaluation(tmp_path, first_candidate)  # noqa: SLF001
    write_json_atomic(
        tmp_path / "query_refinement" / "candidate_evaluations" / "Q0002.json",
        {
            "evaluation_status": "completed",
            "query_id": "Q0002",
            "patch_id": "patch-first",
        },
    )

    runner._write_query_patch_result(tmp_path, parent, second_result)  # noqa: SLF001
    second_candidate = CandidateQueryPlan(
        query=second_result.child_query,
        plan=IterationPlan(
            query_id="Q0002",
            parent_query_id=parent.query_id,
            branch_id="second",
            acceptance_status="candidate",
            decision="accept",
            decision_reason="test",
            added_terms=["field monitoring"],
            removed_terms=[],
            modified_blocks=["monitoring_and_concentration_terms"],
            expected_effect="test",
        ),
        patch_result=second_result,
    )
    evaluated = runner._attach_limited_candidate_evaluation(  # noqa: SLF001
        tmp_path, second_candidate
    )

    assert evaluated.limited_evaluation["patch_id"] == "patch-second"
    assert evaluated.limited_evaluation["evaluation_ref"].replace("\\", "/").endswith(
        "query_refinement/candidate_evaluations/Q0002__patch-second.json"
    )
    assert (
        tmp_path
        / "query_refinement"
        / "candidate_evaluations"
        / "requests"
        / "Q0002__patch-second.json"
    ).exists()
    assert (
        tmp_path
        / "query_refinement"
        / "applied"
        / "Q0002__patch-second"
        / "query_patch.json"
    ).exists()


def test_query_refinement_executes_limited_candidate_evaluation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal article"]},
            "emerging_contaminant_terms": ["PFAS"],
            "surface_water_terms": ["river"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-09",
    )
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    ControlPlane(tmp_path / "control.sqlite3", "test").migrate()
    patch = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-estuary",
        parent_query_id="Q0001",
        target_concept_block="surface_water_terms",
        operation="add",
        terms_added=["estuary"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/live"],
        rationale="Included records mention estuaries.",
        expected_effect="Increase estuary-specific novelty.",
        possible_drift_risk="May broaden beyond rivers.",
    )
    result = QueryPatchApplier().apply(parent, patch, child_query_id="Q0002", iteration=2)
    candidate = CandidateQueryPlan(
        query=result.child_query,
        plan=IterationPlan(
            query_id="Q0002",
            parent_query_id="Q0001",
            branch_id="test",
            acceptance_status="candidate",
            decision="accept",
            decision_reason="test",
            added_terms=patch.terms_added,
            removed_terms=patch.terms_removed,
            modified_blocks=[patch.target_concept_block],
            expected_effect=patch.expected_effect,
        ),
        patch_result=result,
    )
    calls: list[str] = []

    monkeypatch.setattr(
        runner,
        "_start_query_iteration",
        lambda *args, **kwargs: calls.append("start"),
    )
    monkeypatch.setattr(
        runner,
        "_search_sources_external",
        lambda **kwargs: {
            "raw_result_count": 2,
            "scanned_result_count": 2,
            "source_statuses": {"crossref": "success"},
            "execution_status": "success",
        },
    )
    monkeypatch.setattr(runner, "_normalize_and_register", lambda **kwargs: 2)
    monkeypatch.setattr(
        runner,
        "_screen_and_handoff_live",
        lambda **kwargs: {
            "screening_decisions": 2,
            "download_requests_emitted": 1,
            "duplicate_handoffs_suppressed": 0,
            "handoff_failures": 0,
            "pending_download_jobs": 1,
            "handoff_backpressure_status": "ok",
        },
    )

    class Metrics:
        total_score = 0.42
        source_completeness = "complete"
        raw_result_count = 2
        scanned_result_count = 2
        novel_record_count = 2
        include_count = 1
        exclude_count = 1
        defer_count = 0
        novel_precision_at_20 = 0.5
        defer_rate = 0.0
        excluded_matrix_rate = 0.0
        download_requests_emitted = 1

    captured_target: dict[str, object] = {}

    def fake_evaluate_query(**kwargs: object) -> Metrics:
        captured_target["target_novel_records"] = kwargs.get("target_novel_records")
        return Metrics()

    monkeypatch.setattr(runner, "_evaluate_query", fake_evaluate_query)
    monkeypatch.setattr(
        runner,
        "_live_acceptance_reference",
        lambda *args, **kwargs: type(
            "Reference",
            (),
            {
                "score": 0.25,
                "novel_precision_at_20": 0.5,
                "defer_rate": 0.0,
                "excluded_matrix_rate": 0.0,
                "source_completeness": "complete",
            },
        )(),
    )
    monkeypatch.setattr(
        runner,
        "_remove_candidate_query_iteration",
        lambda *args, **kwargs: calls.append("cleanup"),
    )

    evaluated = runner._attach_limited_candidate_evaluation(  # noqa: SLF001
        tmp_path,
        candidate,
        run_id="run",
        loaded=type("Loaded", (), {"protocol": {}, "stopping": {}, "scoring": {}})(),
        runtime={"normalization_batch_size": 10},
    )

    assert calls == ["start", "cleanup"]
    assert evaluated.limited_evaluation["evaluation_status"] == "completed"
    assert evaluated.limited_evaluation["score_delta"] == pytest.approx(0.17)
    assert evaluated.limited_evaluation["selection_eligible"] is True
    assert captured_target["target_novel_records"] == 20
    assert (
        tmp_path / "query_refinement" / "candidate_evaluations" / "Q0002.json"
    ).exists()


def test_query_refinement_resumes_pending_candidate_screening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal article"]},
            "emerging_contaminant_terms": ["PFAS"],
            "surface_water_terms": ["river"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-09",
    )
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    ControlPlane(tmp_path / "control.sqlite3", "test").migrate()
    patch = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-estuary",
        parent_query_id="Q0001",
        target_concept_block="surface_water_terms",
        operation="add",
        terms_added=["estuary"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/live"],
        rationale="Included records mention estuaries.",
        expected_effect="Increase estuary-specific novelty.",
        possible_drift_risk="May broaden beyond rivers.",
    )
    result = QueryPatchApplier().apply(parent, patch, child_query_id="Q0002", iteration=2)
    candidate = CandidateQueryPlan(
        query=result.child_query,
        plan=IterationPlan(
            query_id="Q0002",
            parent_query_id="Q0001",
            branch_id="test",
            acceptance_status="candidate",
            decision="accept",
            decision_reason="test",
            added_terms=patch.terms_added,
            removed_terms=patch.terms_removed,
            modified_blocks=[patch.target_concept_block],
            expected_effect=patch.expected_effect,
        ),
        patch_result=result,
    )
    evaluation_ref = tmp_path / "query_refinement" / "candidate_evaluations" / "Q0002.json"
    write_json_atomic(
        evaluation_ref,
        {
            "evaluation_status": "pending_screening_worker_required",
            "query_id": "Q0002",
            "patch_id": "patch-estuary",
            "source_counts": {
                "raw_result_count": 2,
                "scanned_result_count": 2,
                "source_statuses": {"crossref": "success"},
            },
            "normalized_record_count": 2,
            "selection_eligible": False,
        },
    )
    monkeypatch.setattr(
        runner,
        "_screen_and_handoff_live",
        lambda **kwargs: {
            "screening_decisions": 2,
            "download_requests_emitted": 1,
            "duplicate_handoffs_suppressed": 0,
            "handoff_failures": 0,
            "pending_download_jobs": 1,
            "handoff_backpressure_status": "ok",
        },
    )

    class Metrics:
        total_score = 0.5
        source_completeness = "complete"
        raw_result_count = 2
        scanned_result_count = 2
        novel_record_count = 2
        include_count = 1
        exclude_count = 1
        defer_count = 0
        novel_precision_at_20 = 0.5
        defer_rate = 0.0
        excluded_matrix_rate = 0.0
        download_requests_emitted = 1

    monkeypatch.setattr(runner, "_evaluate_query", lambda **kwargs: Metrics())
    monkeypatch.setattr(
        runner,
        "_live_acceptance_reference",
        lambda *args, **kwargs: type(
            "Reference",
            (),
            {
                "score": 0.4,
                "novel_precision_at_20": 0.5,
                "defer_rate": 0.0,
                "excluded_matrix_rate": 0.0,
                "source_completeness": "complete",
            },
        )(),
    )

    evaluated = runner._attach_limited_candidate_evaluation(  # noqa: SLF001
        tmp_path,
        candidate,
        run_id="run",
        loaded=type("Loaded", (), {"protocol": {}, "stopping": {}, "scoring": {}})(),
        runtime={"normalization_batch_size": 10},
    )

    assert evaluated.limited_evaluation["evaluation_status"] == "completed"
    assert evaluated.limited_evaluation["resumed_from"] == "pending_screening_worker_required"
    assert evaluated.limited_evaluation["score_delta"] == pytest.approx(0.1)


def test_live_resume_preserves_live_mode_and_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    run_id = "paused-live"
    write_json_atomic(
        tmp_path / "runs" / run_id / "manifest.json",
        {
            "run_id": run_id,
            "run_status": "paused_screening_worker_required",
            "run_mode": "live_retrieval",
            "date_to": "2026-07-09",
            "configured_max_iterations": 2,
            "configured_target_novel_records": 3,
            "configured_max_scan_depth": 1,
            "live_providers": ["crossref"],
            "adapter_versions": {
                "external_metadata_discovery_v1": "file_based_skill_boundary"
            },
            "model_parameters": {"llm_enabled": True},
        },
    )
    captured: dict[str, object] = {}

    def fake_run(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"status": "captured"}

    monkeypatch.setattr(runner, "run", fake_run)

    assert runner.resume(run_id)["status"] == "captured"
    assert captured["resume"] is True
    assert captured["live_mode"] is True
    assert captured["providers"] == ["crossref"]
    assert captured["max_iterations"] == 2
    assert captured["target_novel_records"] == 3
    assert captured["max_scan_depth"] == 1


def test_live_pause_manifest_marks_run_paused_and_preserves_source_completeness(
    tmp_path: Path,
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    run_id = "paused-live-completeness"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    write_json_atomic(
        run_dir / "manifest.json",
        {
            "run_id": run_id,
            "run_status": "running",
            "run_completeness": "unknown",
            "source_completeness": "unknown",
            "source_health_status": {},
        },
    )
    ControlPlane(tmp_path / "control.sqlite3", runner._git_sha()).migrate()  # noqa: SLF001
    with ControlPlane(tmp_path / "control.sqlite3", runner._git_sha()).transaction() as connection:  # noqa: SLF001
        connection.execute(
            """
            INSERT INTO runs (
                run_id, status, completeness, date_from, date_to, current_iteration,
                current_query_id, current_state, started_at, code_commit_sha,
                git_branch, config_hash, prompt_hash, protocol_version, scoring_version,
                model_version, source_status_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                "running",
                "complete",
                "2006-01-01",
                "2026-07-10",
                1,
                "Q0001",
                "PERSIST_FINAL_SCREENING",
                "2026-07-11T00:00:00Z",
                "sha",
                "branch",
                "hash",
                "prompt",
                "protocol",
                "scoring",
                "model",
                json.dumps({"crossref": "success"}),
            ),
        )

    runner._write_manifest_update(  # noqa: SLF001
        run_dir,
        {},
        runner._paused_manifest_update(  # noqa: SLF001
            run_id, "paused_screening_worker_required"
        ),
    )

    manifest = read_json(run_dir / "manifest.json")
    assert manifest["run_status"] == "paused_screening_worker_required"
    assert manifest["run_completeness"] == "paused"
    assert manifest["source_completeness"] == "complete"
    with ControlPlane(tmp_path / "control.sqlite3", runner._git_sha()).connect() as connection:  # noqa: SLF001
        row = connection.execute(
            "SELECT status, current_state, completeness FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    assert row is not None
    assert row["status"] == "paused_screening_worker_required"
    assert row["current_state"] == "PAUSED_SCREENING_WORKER_REQUIRED"
    assert row["completeness"] == "complete"


def test_next_screening_batch_only_uses_novelty_sample(tmp_path: Path) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    run_id = "screen-novelty-only"
    query_id = "Q0001"
    ControlPlane(tmp_path / "control.sqlite3", runner._git_sha()).migrate()  # noqa: SLF001
    with ControlPlane(tmp_path / "control.sqlite3", runner._git_sha()).transaction() as connection:  # noqa: SLF001
        connection.execute(
            """
            INSERT INTO runs (
                run_id, status, completeness, date_from, date_to, current_iteration,
                current_query_id, current_state, started_at, code_commit_sha,
                git_branch, config_hash, prompt_hash, protocol_version, scoring_version,
                model_version, source_status_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                "running",
                "complete",
                "2006-01-01",
                "2026-07-10",
                1,
                query_id,
                "PERSIST_FINAL_SCREENING",
                "2026-07-11T00:00:00Z",
                "sha",
                "branch",
                "hash",
                "prompt",
                "protocol",
                "scoring",
                "model",
                json.dumps({"crossref": "success"}),
            ),
        )
        for suffix, included in (("novel", 1), ("extra", 0)):
            gid = f"doi:10.1000/{suffix}"
            connection.execute(
                """
                INSERT INTO documents (
                    global_record_id, normalized_doi, canonical_title,
                    normalized_title, abstract_original, abstract_source,
                    keywords_json, authors_json, issn_json, eissn_json,
                    document_type, language, sampled_matrices_json,
                    article_ec_scope, first_seen_run_id, first_seen_query_id,
                    first_seen_at, latest_metadata_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    gid,
                    f"10.1000/{suffix}",
                    f"{suffix} title",
                    f"{suffix} title",
                    f"{suffix} abstract",
                    "crossref",
                    "[]",
                    "[]",
                    "[]",
                    "[]",
                    "journal-article",
                    "en",
                    "[]",
                    "uncertain",
                    run_id,
                    query_id,
                    "2026-07-11T00:00:00Z",
                    "test-normalization",
                ),
            )
            connection.execute(
                """
                INSERT INTO document_sources (
                    global_record_id, source_name, source_record_id, source_rank,
                    run_id, query_id, raw_metadata_path, metadata_version, retrieved_at
                )
                VALUES (?, 'crossref', ?, 1, ?, ?, '', 'test-normalization', ?)
                """,
                (
                    gid,
                    gid,
                    run_id,
                    query_id,
                    "2026-07-11T00:00:00Z",
                ),
            )
            connection.execute(
                """
                INSERT INTO document_query_membership (
                    global_record_id, run_id, query_id, iteration, source_name,
                    source_rank, first_seen_in_query, already_known_before_query,
                    included_in_novelty_sample, novelty_sample_position,
                    screening_status_at_iteration, created_at
                )
                VALUES (?, ?, ?, 1, 'crossref', 1, 1, 0, ?, ?, NULL, ?)
                """,
                (
                    gid,
                    run_id,
                    query_id,
                    included,
                    1 if included else None,
                    "2026-07-11T00:00:00Z",
                ),
            )

    batch = runner._next_screening_batch(  # noqa: SLF001
        run_id, query_id, 10, novelty_sample_only=True
    )

    assert [record.global_record_id for record in batch] == ["doi:10.1000/novel"]


def test_next_screening_batch_can_limit_candidate_evaluation_to_pairwise_gain(
    tmp_path: Path,
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    run_id = "screen-pairwise-gain"
    parent_query_id = "Q0001"
    candidate_query_id = "Q0002"
    ControlPlane(tmp_path / "control.sqlite3", runner._git_sha()).migrate()  # noqa: SLF001
    with ControlPlane(tmp_path / "control.sqlite3", runner._git_sha()).transaction() as connection:  # noqa: SLF001
        connection.execute(
            """
            INSERT INTO runs (
                run_id, status, completeness, date_from, date_to, current_iteration,
                current_query_id, current_state, started_at, code_commit_sha,
                git_branch, config_hash, prompt_hash, protocol_version, scoring_version,
                model_version, source_status_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                "running",
                "complete",
                "2006-01-01",
                "2026-07-10",
                2,
                candidate_query_id,
                "PERSIST_FINAL_SCREENING",
                "2026-07-11T00:00:00Z",
                "sha",
                "branch",
                "hash",
                "prompt",
                "protocol",
                "scoring",
                "model",
                json.dumps({"crossref": "success"}),
            ),
        )
        for suffix in ("parent-overlap", "candidate-gain"):
            gid = f"doi:10.1000/{suffix}"
            connection.execute(
                """
                INSERT INTO documents (
                    global_record_id, normalized_doi, canonical_title,
                    normalized_title, abstract_original, abstract_source,
                    keywords_json, authors_json, issn_json, eissn_json,
                    document_type, language, sampled_matrices_json,
                    article_ec_scope, first_seen_run_id, first_seen_query_id,
                    first_seen_at, latest_metadata_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    gid,
                    f"10.1000/{suffix}",
                    f"{suffix} title",
                    f"{suffix} title",
                    f"{suffix} abstract",
                    "crossref",
                    "[]",
                    "[]",
                    "[]",
                    "[]",
                    "journal-article",
                    "en",
                    "[]",
                    "uncertain",
                    run_id,
                    candidate_query_id,
                    "2026-07-11T00:00:00Z",
                    "test-normalization",
                ),
            )
            for query_id in (
                (parent_query_id, candidate_query_id)
                if suffix == "parent-overlap"
                else (candidate_query_id,)
            ):
                connection.execute(
                    """
                    INSERT INTO document_query_membership (
                        global_record_id, run_id, query_id, iteration, source_name,
                        source_rank, first_seen_in_query, already_known_before_query,
                        included_in_novelty_sample, novelty_sample_position,
                        screening_status_at_iteration, created_at
                    )
                    VALUES (?, ?, ?, 2, 'crossref', 1, 1, 0, 1, 1, NULL, ?)
                    """,
                    (gid, run_id, query_id, "2026-07-11T00:00:00Z"),
                )

    batch = runner._next_screening_batch(  # noqa: SLF001
        run_id,
        candidate_query_id,
        10,
        novelty_sample_only=True,
        novelty_against_query_id=parent_query_id,
    )

    assert [record.global_record_id for record in batch] == ["doi:10.1000/candidate-gain"]


def test_pairwise_gain_screening_uses_gain_records_beyond_novelty_cap(
    tmp_path: Path,
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    run_id = "screen-pairwise-gain-no-cap"
    parent_query_id = "Q0001"
    candidate_query_id = "Q0002"
    ControlPlane(tmp_path / "control.sqlite3", runner._git_sha()).migrate()  # noqa: SLF001
    with ControlPlane(tmp_path / "control.sqlite3", runner._git_sha()).transaction() as connection:  # noqa: SLF001
        connection.execute(
            """
            INSERT INTO runs (
                run_id, status, completeness, date_from, date_to, current_iteration,
                current_query_id, current_state, started_at, code_commit_sha,
                git_branch, config_hash, prompt_hash, protocol_version, scoring_version,
                model_version, source_status_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                "running",
                "complete",
                "2006-01-01",
                "2026-07-10",
                2,
                candidate_query_id,
                "PERSIST_FINAL_SCREENING",
                "2026-07-11T00:00:00Z",
                "sha",
                "branch",
                "hash",
                "prompt",
                "protocol",
                "scoring",
                "model",
                json.dumps({"crossref": "success"}),
            ),
        )
        for suffix, query_ids, novelty_sample in (
            ("parent-overlap", (parent_query_id, candidate_query_id), 1),
            ("candidate-gain-outside-cap", (candidate_query_id,), 0),
        ):
            gid = f"doi:10.1000/{suffix}"
            connection.execute(
                """
                INSERT INTO documents (
                    global_record_id, normalized_doi, canonical_title,
                    normalized_title, abstract_original, abstract_source,
                    keywords_json, authors_json, issn_json, eissn_json,
                    document_type, language, sampled_matrices_json,
                    article_ec_scope, first_seen_run_id, first_seen_query_id,
                    first_seen_at, latest_metadata_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    gid,
                    f"10.1000/{suffix}",
                    f"{suffix} title",
                    f"{suffix} title",
                    f"{suffix} abstract",
                    "crossref",
                    "[]",
                    "[]",
                    "[]",
                    "[]",
                    "journal-article",
                    "en",
                    "[]",
                    "uncertain",
                    run_id,
                    candidate_query_id,
                    "2026-07-11T00:00:00Z",
                    "test-normalization",
                ),
            )
            for query_id in query_ids:
                connection.execute(
                    """
                    INSERT INTO document_query_membership (
                        global_record_id, run_id, query_id, iteration, source_name,
                        source_rank, first_seen_in_query, already_known_before_query,
                        included_in_novelty_sample, novelty_sample_position,
                        screening_status_at_iteration, created_at
                    )
                    VALUES (?, ?, ?, 2, 'crossref', 1, 1, 0, ?, ?, NULL, ?)
                    """,
                    (
                        gid,
                        run_id,
                        query_id,
                        novelty_sample,
                        1 if novelty_sample else None,
                        "2026-07-11T00:00:00Z",
                    ),
                )

    batch = runner._next_screening_batch(  # noqa: SLF001
        run_id,
        candidate_query_id,
        10,
        novelty_sample_only=True,
        novelty_against_query_id=parent_query_id,
    )

    assert [record.global_record_id for record in batch] == [
        "doi:10.1000/candidate-gain-outside-cap"
    ]


def test_live_download_event_uses_live_lane_and_real_screening_evidence_path(
    tmp_path: Path,
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    executor = TitleAbstractScreeningWorkerExecutor(
        repo_root=Path.cwd(),
        run_dir=tmp_path,
        schema_path=Path.cwd() / "schemas" / "retrieval" / "screening_decision.schema.json",
    )
    record = _record()
    with pytest.raises(ScreeningWorkerBlocked) as blocked:
        executor.screen_one(
            record,
            run_id="run",
            query_id="Q0001",
            iteration=1,
            audit_batch_id="screening_one",
            protocol={},
            allowed_reason_codes={},
            scie_status="unknown",
        )
    result_ref = Path.cwd() / blocked.value.payload["result_ref"]
    write_json_atomic(
        result_ref,
        {
            "decision": "include",
            "confidence": 0.92,
            "article_type_ok": True,
            "date_ok": True,
            "scie_status": "unknown",
            "emerging_contaminant_context": True,
            "surface_water_sample": True,
            "included_waterbody_types": ["river"],
            "field_environmental_samples": True,
            "concentration_evidence": "explicit_quantified",
            "study_type": "field monitoring",
            "reason_codes": ["I_SURFACE_WATER_CONCENTRATION"],
            "evidence_spans": ["Measured PFAS concentrations in river water samples."],
            "article_ec_scope": "true",
        },
    )
    decision = executor.screen_one(
        record,
        run_id="run",
        query_id="Q0001",
        iteration=1,
        audit_batch_id="screening_one",
        protocol={},
        allowed_reason_codes={},
        scie_status="unknown",
    )
    loaded = LoadedConfig(
        protocol={"scie": {"registry_path": "registry/scie_journals.csv"}},
        scoring={},
        sources={},
        model={},
        stopping={},
        logging={},
        runtime={},
        config_hash="test",
    )

    event = runner._download_event(record, decision, loaded)  # noqa: SLF001

    assert event["retrieval_lane"] == "live_retrieval"
    assert event["screening_evidence_path"] == (
        "runs/run/screening/batches/Q0001_screening_one.jsonl"
    )


def _metrics(**overrides: object) -> QueryMetrics:
    payload = {
        "run_id": "run",
        "iteration": 2,
        "query_id": "Q0002",
        "parent_query_id": "Q0001",
        "raw_result_count": 10,
        "scanned_result_count": 10,
        "known_record_count": 0,
        "novel_record_count": 10,
        "target_novel_n": 20,
        "actual_novel_n": 10,
        "target_reached": False,
        "include_count": 5,
        "exclude_count": 5,
        "defer_count": 0,
        "eligible_precision": 0.5,
        "novel_precision_at_20": 0.5,
        "novel_eligible_yield": 5,
        "normalized_novel_eligible_yield": 0.25,
        "marginal_relevant_yield": 0.5,
        "novelty_rate": 1.0,
        "cumulative_eligible_count": 5,
        "retrospective_query_coverage": 0.0,
        "cross_source_breadth": 1.0,
        "metadata_completeness": 1.0,
        "scope_diversity": 0.5,
        "excluded_matrix_rate": 0.0,
        "laboratory_study_rate": 0.0,
        "no_concentration_rate": 0.0,
        "duplicate_rate": 0.0,
        "known_eligible_overlap_rate": 0.0,
        "known_ineligible_overlap_rate": 0.0,
        "query_complexity": 0.2,
        "positive_score": 0.5,
        "penalty_score": 0.0,
        "total_score": 0.52,
        "score_delta": 0.02,
        "decision": "accept",
        "decision_reason": "pending",
        "saturation_status": "not_saturated",
        "source_completeness": "complete",
        "timestamp": "2026-07-09T00:00:00Z",
        "code_commit_sha": "test",
        "config_hash": "test",
        "prompt_hash": "test",
        "model_name": "codex-gpt",
        "model_parameters": {"llm_enabled": True},
    }
    payload.update(overrides)
    return QueryMetrics(**payload)


def test_live_query_selector_uses_configured_acceptance_thresholds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    reference = runner._live_acceptance_reference  # noqa: SLF001
    monkeypatch.setattr(
        runner,
        "_live_acceptance_reference",
        lambda run_id, parent_query_id: type(
            "Reference",
            (),
            {
                "score": 0.5,
                "novel_precision_at_20": 0.5,
                "defer_rate": 0.1,
                "excluded_matrix_rate": 0.0,
                "source_completeness": "complete",
            },
        )(),
    )
    query = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal article"]},
            "emerging_contaminant_terms": ["PFAS"],
            "surface_water_terms": ["river"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-09",
    )
    query = query.__class__(
        **(
            query.to_dict()
            | {"query_id": "Q0002", "parent_query_id": "Q0001", "iteration": 2}
        )
    )

    assert runner._select_live_query(_metrics(), query).decision == "accept"  # noqa: SLF001
    assert runner._select_live_query(  # noqa: SLF001
        _metrics(score_delta=0.019, total_score=0.519, marginal_eligible_count=1),
        query,
    ).decision == "accept"
    assert runner._select_live_query(  # noqa: SLF001
        _metrics(score_delta=0.019, total_score=0.519, marginal_eligible_count=0),
        query,
    ).decision == "reject"
    assert runner._select_live_query(  # noqa: SLF001
        _metrics(novel_precision_at_20=0.29), query
    ).decision == "reject"
    assert runner._select_live_query(  # noqa: SLF001
        _metrics(defer_rate=0.251), query
    ).decision == "reject"
    assert runner._select_live_query(  # noqa: SLF001
        _metrics(excluded_matrix_rate=0.21), query
    ).decision == "reject"
    assert runner._select_live_query(  # noqa: SLF001
        _metrics(source_completeness="partial"), query
    ).decision == "reject"
    monkeypatch.setattr(runner, "_live_acceptance_reference", reference)


def test_live_query_rejects_selected_candidate_when_pairwise_acceptance_no_longer_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    monkeypatch.setattr(
        runner,
        "_live_acceptance_reference",
        lambda run_id, parent_query_id: type(
            "Reference",
            (),
            {
                "score": 0.5,
                "novel_precision_at_20": 0.5,
                "defer_rate": 0.1,
                "excluded_matrix_rate": 0.0,
                "source_completeness": "complete",
            },
        )(),
    )
    run_dir = tmp_path / "runs" / "run" / "query_refinement"
    run_dir.mkdir(parents=True)
    write_json_atomic(
        run_dir / "Q0002_candidate_selection.json",
        {
            "selected_query_id": "Q0002",
            "candidate_evaluations": [
                {
                    "query_id": "Q0002",
                    "evaluation_status": "completed",
                    "patch_type": "expansion",
                    "result_set_change_status": "changed",
                    "source_completeness": "complete",
                    "pairwise_safety_gate": "pass",
                    "conservative_utility": 5.0,
                    "score_delta": 0.2,
                    "gain_count": 10,
                    "loss_count": 2,
                    "gain_include_count": 2,
                    "known_loss_include_count": 1,
                    "loss_audit_include_count": 1,
                    "novel_precision_at_20": 0.6,
                    "defer_rate": 0.0,
                    "excluded_matrix_rate": 0.0,
                }
            ],
        },
    )
    query = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal article"]},
            "emerging_contaminant_terms": ["PFAS"],
            "surface_water_terms": ["river"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-09",
    )
    query = query.__class__(
        **(
            query.to_dict()
            | {"query_id": "Q0002", "parent_query_id": "Q0001", "iteration": 2}
        )
    )

    selected = runner._select_live_query(  # noqa: SLF001
        _metrics(run_id="run", score_delta=0.2, marginal_eligible_count=2),
        query,
    )

    assert selected.decision == "reject"
    assert "pairwise gain/loss" in selected.decision_reason


def test_live_query_rejects_pairwise_noise_reduction_that_collapses_yield(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    monkeypatch.setattr(
        runner,
        "_live_acceptance_reference",
        lambda run_id, parent_query_id: type(
            "Reference",
            (),
            {
                "score": 0.2,
                "novel_precision_at_20": 0.5,
                "defer_rate": 0.2,
                "excluded_matrix_rate": 0.0,
                "source_completeness": "complete",
            },
        )(),
    )
    run_dir = tmp_path / "runs" / "run" / "query_refinement"
    run_dir.mkdir(parents=True)
    write_json_atomic(
        run_dir / "Q0002_candidate_selection.json",
        {
            "selected_query_id": "Q0002",
            "candidate_evaluations": [
                {
                    "query_id": "Q0002",
                    "evaluation_status": "completed",
                    "patch_type": "noise_reduction",
                    "result_set_change_status": "changed",
                    "source_completeness": "complete",
                    "pairwise_safety_gate": "pass",
                    "conservative_utility": 2.0,
                    "noise_reduction_utility": 2.0,
                    "gain_count": 2,
                    "loss_count": 3,
                    "known_loss_include_count": 0,
                    "loss_audit_include_count": 0,
                    "loss_audit_screened_count": 3,
                    "loss_audit_target_records": 50,
                    "novel_precision_at_20": 0.0,
                    "defer_rate": 0.1,
                    "excluded_matrix_rate": 0.0,
                }
            ],
        },
    )
    query = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal article"]},
            "emerging_contaminant_terms": ["PFAS"],
            "surface_water_terms": ["river"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-09",
    )
    query = query.__class__(
        **(
            query.to_dict()
            | {"query_id": "Q0002", "parent_query_id": "Q0001", "iteration": 2}
        )
    )

    selected = runner._select_live_query(  # noqa: SLF001
        _metrics(
            run_id="run",
            score_delta=-0.2,
            total_score=0.0,
            marginal_eligible_count=0,
        ),
        query,
    )

    assert selected.decision == "reject"
    assert "pairwise gain/loss" in selected.decision_reason


def test_unaccepted_candidates_request_retry_when_all_have_negative_utility(
    tmp_path: Path,
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    candidates = [
        type(
            "Candidate",
            (),
            {
                "limited_evaluation": {
                    "patch_id": f"patch-{index}",
                    "patch_type": patch_type,
                    "conservative_utility": -1.0,
                    "gain_include_count": 0,
                    "known_loss_include_count": known_loss,
                    "loss_audit_include_count": known_loss,
                }
            },
        )()
        for index, (patch_type, known_loss) in enumerate(
            [("expansion", 1), ("expansion", 0), ("noise_reduction", 0)],
            start=1,
        )
    ]

    assert runner._should_continue_after_unaccepted_candidates(  # noqa: SLF001
        candidates,
        parent_metrics=_metrics(iteration=1, query_id="Q0001", parent_query_id=None),
    )


def test_unaccepted_candidates_retry_when_one_patch_was_no_op(
    tmp_path: Path,
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    candidates = [
        type(
            "Candidate",
            (),
            {
                "limited_evaluation": {
                    "patch_id": f"patch-{index}",
                    "patch_type": patch_type,
                    "conservative_utility": -1.0,
                    "gain_include_count": 0,
                    "known_loss_include_count": known_loss,
                    "loss_audit_include_count": known_loss,
                }
            },
        )()
        for index, (patch_type, known_loss) in enumerate(
            [("expansion", 1), ("noise_reduction", 0)],
            start=1,
        )
    ]

    assert runner._should_continue_after_unaccepted_candidates(  # noqa: SLF001
        candidates,
        parent_metrics=_metrics(iteration=1, query_id="Q0001", parent_query_id=None),
    )


def test_unaccepted_candidates_stop_after_retry_budget_is_exhausted(
    tmp_path: Path,
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    candidates = [
        type(
            "Candidate",
            (),
            {
                "limited_evaluation": {
                    "patch_id": f"patch-{index}",
                    "patch_type": "expansion",
                    "conservative_utility": -1.0,
                    "gain_include_count": 0,
                    "known_loss_include_count": 1,
                    "loss_audit_include_count": 1,
                }
            },
        )()
        for index in range(9)
    ]

    assert not runner._should_continue_after_unaccepted_candidates(  # noqa: SLF001
        candidates,
        parent_metrics=_metrics(iteration=1, query_id="Q0001", parent_query_id=None),
    )


def test_high_recall_expansion_accepts_screenable_precision_drop(
    tmp_path: Path,
) -> None:
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal article"]},
            "emerging_contaminant_terms": ["emerging contaminant*"],
            "surface_water_terms": ["river"],
            "monitoring_and_concentration_terms": ["occurrence"],
        },
        "2026-07-09",
    )
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    patch = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-high-recall-expansion",
        parent_query_id="Q0001",
        target_concept_block="monitoring_and_concentration_terms",
        operation="add",
        terms_added=["field monitoring"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/include-a"],
        rationale="Included evidence supports field monitoring.",
        expected_effect="Retrieve more screenable natural-water monitoring records.",
        possible_drift_risk="May add manageable screening burden.",
    )
    result = QueryPatchApplier().apply(parent, patch, child_query_id="Q0002", iteration=2)
    candidates = [
        type(
            "Candidate",
            (),
            {
                "query": result.child_query,
                "patch_result": result,
                "limited_evaluation": {
                    "evaluation_status": "completed",
                    "patch_type": "expansion",
                    "result_set_change_status": "changed",
                    "source_completeness": "complete",
                    "pairwise_safety_gate": "pass",
                    "conservative_utility": 1.5,
                    "score_delta": -0.05,
                    "gain_count": 30,
                    "loss_count": 12,
                    "gain_include_count": 3,
                    "known_loss_include_count": 0,
                    "loss_audit_include_count": 0,
                    "novel_precision_at_20": 0.31,
                    "defer_rate": 0.22,
                    "excluded_matrix_rate": 0.18,
                },
            },
        )()
    ]

    selected = runner._select_query_candidate(  # noqa: SLF001
        candidates,
        parent_metrics=_metrics(
            iteration=1,
            query_id="Q0001",
            parent_query_id=None,
            novel_precision_at_20=0.5,
            defer_rate=0.1,
            excluded_matrix_rate=0.0,
        ),
    )

    assert selected is candidates[0]


def test_live_source_completeness_treats_success_and_no_results_as_complete(
    tmp_path: Path,
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )

    assert (
        runner._source_completeness_from_statuses(  # noqa: SLF001
            {"source_statuses": {"crossref": "success", "openalex": "no-results"}}
        )
        == "complete"
    )
    assert (
        runner._source_completeness_from_statuses(  # noqa: SLF001
            {"source_statuses": {"crossref": "success", "semantic_scholar": "rate-limited"}}
        )
        == "partial"
    )
    assert (
        runner._source_completeness_from_statuses(  # noqa: SLF001
            {"source_statuses": {"pubmed": "failed"}}
        )
        == "failed"
    )


def test_complete_live_run_preserves_partial_source_completeness(
    tmp_path: Path,
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    run_id = "partial-complete"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    ControlPlane(tmp_path / "control.sqlite3", "test").migrate()
    plane = ControlPlane(tmp_path / "control.sqlite3", "test")
    plane.migrate()
    with plane.transaction() as connection:
        connection.execute(
            """
            INSERT INTO runs (
                run_id, status, completeness, date_from, date_to, current_iteration,
                current_query_id, accepted_query_id, current_state, started_at,
                completed_at, code_commit_sha, git_branch, config_hash, prompt_hash,
                protocol_version, scoring_version, model_version, source_status_json,
                failure_reason
            )
            VALUES (
                ?, 'running', 'partial', '2006-01-01', '2026-07-09', 1,
                'Q0001', 'Q0001', 'EVALUATE_QUERY', '2026-07-09T00:00:00Z',
                NULL, 'test', 'test', 'test', 'test', 'test', 'test', 'test',
                ?, NULL
            )
            """,
            (run_id, '{"crossref":"success","pubmed":"partial"}'),
        )

    result = type(
        "Result",
        (),
        {"iteration": 1, "query_id": "Q0001"},
    )()
    runner._complete_run(run_id, result, completeness="partial")  # noqa: SLF001

    with ControlPlane(tmp_path / "control.sqlite3", "test").connect() as connection:
        row = connection.execute(
            "SELECT status, completeness FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()

    assert row["status"] == "completed"
    assert row["completeness"] == "partial"


def test_candidate_evaluation_uses_configured_scan_depth(tmp_path: Path) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    run_dir = tmp_path / "runs" / "run"
    run_dir.mkdir(parents=True)
    write_json_atomic(
        run_dir / "manifest.json",
        {"configured_max_scan_depth": 200, "live_providers": ["crossref", "openalex"]},
    )
    configured_page_size = int(
        runner._runtime_settings(ProtocolLoader(Path.cwd() / "configs" / "retrieval").load())[
            "retrieval_page_size"
        ]
    )

    assert runner._configured_max_scan_depth(run_dir) == 200  # noqa: SLF001
    assert runner._configured_max_records_per_provider(run_dir) == (  # noqa: SLF001
        configured_page_size * 200
    )


def test_previous_query_changes_include_rejected_candidate_evaluations(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    ensure_dir(run_dir / "queries" / "Q0001")
    ensure_dir(run_dir / "query_refinement")
    write_json_atomic(
        run_dir / "queries" / "Q0001" / "query_change.json",
        {"query_id": "Q0001", "parent_query_id": None, "decision": "accept"},
    )
    write_json_atomic(
        run_dir / "query_refinement" / "Q0002_candidate_selection.json",
        {
            "parent_query_id": "Q0001",
            "parent_metrics": {"run_id": "run"},
            "candidate_evaluations": [
                {
                    "patch_id": "patch-a",
                    "query_id": "Q0002",
                    "score": 0.0,
                    "score_delta": -0.1,
                    "source_completeness": "complete",
                    "include_count": 0,
                    "exclude_count": 0,
                    "defer_count": 0,
                }
            ],
        },
    )
    write_json_atomic(
        run_dir / "query_refinement" / "Q0002_duplicate_candidate_selection.json",
        {
            "parent_query_id": "Q0001",
            "parent_metrics": {"run_id": "run"},
            "candidate_evaluations": [
                {
                    "patch_id": "patch-a",
                    "query_id": "Q0002",
                    "score": 0.0,
                    "score_delta": -0.1,
                    "source_completeness": "complete",
                    "include_count": 0,
                    "exclude_count": 0,
                    "defer_count": 0,
                }
            ],
        },
    )
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )

    changes = runner._previous_query_changes(run_dir)  # noqa: SLF001

    assert [change.get("query_id") for change in changes] == ["Q0001", "Q0002"]
    assert changes[1]["patch_id"] == "patch-a"
    assert changes[1]["decision"] == "reject"


def test_query_refinement_result_key_changes_after_rejected_parent_attempt(
    tmp_path: Path,
) -> None:
    executor = QueryRefinementWorkerExecutor(
        repo_root=Path.cwd(),
        run_dir=tmp_path,
        schema_path=Path.cwd() / "schemas" / "retrieval" / "query_patch.schema.json",
    )

    assert executor._refinement_key("Q0001", []) == "Q0001"  # noqa: SLF001
    assert (
        executor._refinement_key(  # noqa: SLF001
            "Q0001",
            [
                {"query_id": "Q0001", "parent_query_id": None, "decision": "accept"},
                {"query_id": "Q0002", "parent_query_id": "Q0001", "decision": "reject"},
                {"query_id": "Q0003", "parent_query_id": "Q0009", "decision": "reject"},
            ],
        )
        == "Q0001_attempt_002"
    )


def test_positive_term_mining_finds_included_absent_relevant_terms(
    tmp_path: Path,
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    run_id = "term_mining_run"
    query = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal-article"]},
            "emerging_contaminant_terms": ["emerging contaminant"],
            "surface_water_terms": ["surface water"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-10",
    )
    record = _record()
    record = NormalizedRecord(
        **{
            **record.to_dict(),
            "global_record_id": "doi:10.1000/pfas-river",
            "title_original": "PFAS occurrence in river water",
            "title_normalized": "pfas occurrence in river water",
            "abstract_original": (
                "Field monitoring measured PFAS concentrations in river water samples."
            ),
            "keywords": ["PFAS", "field monitoring", "river water"],
        }
    )
    second_record = NormalizedRecord(
        **{
            **record.to_dict(),
            "global_record_id": "doi:10.1000/pfas-lake",
            "doi": "10.1000/pfas-lake",
            "normalized_doi": "10.1000/pfas-lake",
            "title_original": "PFAS occurrence in lake water",
            "title_normalized": "pfas occurrence in lake water",
            "abstract_original": (
                "Surface water monitoring quantified PFAS concentrations in lake samples."
            ),
            "keywords": ["PFAS", "surface water monitoring", "lake"],
        }
    )
    plane = ControlPlane(tmp_path / "control.sqlite3", "test")
    plane.migrate()
    with plane.transaction() as connection:
        connection.execute(
            """
            INSERT INTO runs (
                run_id, status, completeness, date_from, date_to, current_state,
                started_at, code_commit_sha, git_branch, config_hash, prompt_hash,
                protocol_version, scoring_version, model_version, source_status_json
            )
            VALUES (?, 'running', 'unknown', '2006-01-01', '2026-07-10',
                    'MINE_TERMS', '2026-07-10T00:00:00Z', 'test', 'test',
                    'test', 'test', 'test', 'test', 'test', '{}')
            """,
            (run_id,),
        )
        runner._resolve_or_insert_document(connection, run_id, query, record)  # noqa: SLF001
        runner._resolve_or_insert_document(  # noqa: SLF001
            connection, run_id, query, second_record
        )
        connection.execute(
            """
            INSERT INTO document_query_membership (
                global_record_id, run_id, query_id, iteration, source_name, source_rank,
                first_seen_in_query, already_known_before_query,
                included_in_novelty_sample, novelty_sample_position,
                screening_status_at_iteration, created_at
            )
            VALUES (?, ?, ?, 1, 'crossref', 1, 1, 0, 1, 1, 'include', '2026-07-10T00:00:00Z')
            """,
            (record.global_record_id, run_id, query.query_id),
        )
        connection.execute(
            """
            INSERT INTO document_query_membership (
                global_record_id, run_id, query_id, iteration, source_name, source_rank,
                first_seen_in_query, already_known_before_query,
                included_in_novelty_sample, novelty_sample_position,
                screening_status_at_iteration, created_at
            )
            VALUES (?, ?, ?, 1, 'openalex', 2, 1, 0, 1, 2, 'include', '2026-07-10T00:00:00Z')
            """,
            (second_record.global_record_id, run_id, query.query_id),
        )
        runner._insert_screening_decision(  # noqa: SLF001
            connection,
            ScreeningDecision(
                global_record_id=record.global_record_id,
                decision="include",
                reason_codes=["I_SURFACE_WATER_CONCENTRATION"],
                evidence_spans=[
                    "Field monitoring measured PFAS concentrations in river water samples."
                ],
                query_term_evidence=[
                    {
                        "term": "PFAS",
                        "concept_block": "emerging_contaminant_terms",
                        "evidence_span": "PFAS occurrence in river water",
                        "why_relevant": "pollutant class relevant to emerging contaminants",
                        "confidence": 0.94,
                    },
                    {
                        "term": "field monitoring",
                        "concept_block": "monitoring_and_concentration_terms",
                        "evidence_span": (
                            "Field monitoring measured PFAS concentrations in river water samples."
                        ),
                        "why_relevant": "field monitoring language matches the target scope",
                        "confidence": 0.9,
                    },
                    {
                        "term": "pollution load index",
                        "concept_block": "monitoring_and_concentration_terms",
                        "evidence_span": "pollution load index",
                        "why_relevant": "semi-quantitative field burden phrase",
                        "confidence": 0.88,
                    },
                    {
                        "term": "microplastic pollution",
                        "concept_block": "emerging_contaminant_terms",
                        "evidence_span": "microplastic pollution in river water",
                        "why_relevant": "narrower contaminant-occurrence phrase",
                        "confidence": 0.9,
                    },
                ],
                article_ec_scope="true",
                screening_decision_id="screen-term-mining",
                run_id=run_id,
                query_id=query.query_id,
                iteration=1,
                prompt_version="test",
                prompt_hash="test",
                model_name="test",
                model_version="test",
                screening_timestamp="2026-07-10T00:00:01Z",
            ),
        )
        runner._insert_screening_decision(  # noqa: SLF001
            connection,
            ScreeningDecision(
                global_record_id=second_record.global_record_id,
                decision="include",
                reason_codes=["I_SURFACE_WATER_CONCENTRATION"],
                evidence_spans=[
                    "Surface water monitoring quantified PFAS concentrations in lake samples."
                ],
                query_term_evidence=[
                    {
                        "term": "PFAS",
                        "concept_block": "emerging_contaminant_terms",
                        "evidence_span": "PFAS occurrence in lake water",
                        "why_relevant": "pollutant class relevant to emerging contaminants",
                        "confidence": 0.93,
                    },
                    {
                        "term": "microplastic pollution",
                        "concept_block": "emerging_contaminant_terms",
                        "evidence_span": "microplastic pollution in lake water",
                        "why_relevant": "narrower contaminant-occurrence phrase",
                        "confidence": 0.9,
                    }
                ],
                article_ec_scope="true",
                screening_decision_id="screen-term-mining-second",
                run_id=run_id,
                query_id=query.query_id,
                iteration=1,
                prompt_version="test",
                prompt_hash="test",
                model_name="test",
                model_version="test",
                screening_timestamp="2026-07-10T00:00:02Z",
            ),
        )

    rows = runner._mine_positive_term_candidates(run_id, query)  # noqa: SLF001
    terms = {row["term"]: row for row in rows}

    assert "pfas" in terms
    assert terms["pfas"]["concept_block"] == "emerging_contaminant_terms"
    assert "microplastic pollution" in terms
    assert terms["microplastic pollution"]["concept_block"] == "emerging_contaminant_terms"
    assert terms["microplastic pollution"]["supporting_positive_documents"] == sorted([
        record.global_record_id,
        second_record.global_record_id,
    ])
    assert terms["pfas"]["supporting_positive_documents"] == sorted([
        record.global_record_id,
        second_record.global_record_id,
    ])
    assert "field monitoring" in terms
    assert terms["field monitoring"]["concept_block"] == "monitoring_and_concentration_terms"
    assert "pollution load index" in terms
    assert terms["pollution load index"]["concept_block"] == (
        "monitoring_and_concentration_terms"
    )
    assert "concentration" not in terms
    assert "river water" not in terms
    assert "the" not in terms


def test_query_refinement_request_references_positive_term_candidates(
    tmp_path: Path,
) -> None:
    executor = QueryRefinementWorkerExecutor(
        repo_root=Path.cwd(),
        run_dir=tmp_path,
        schema_path=Path.cwd() / "schemas" / "retrieval" / "query_patch.schema.json",
    )
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal-article"]},
            "emerging_contaminant_terms": ["emerging contaminant"],
            "surface_water_terms": ["surface water"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-10",
    )

    with pytest.raises(QueryRefinementBlocked):
        executor.propose(
            accepted_query=parent,
            metrics=_metrics(query_id=parent.query_id),
            evidence_refs={
                "term_ledger": "runs/test/exports/term_evolution.csv",
                "topical_fit_profile": "runs/test/exports/topical_fit_profile.json",
                "positive_term_candidates": "runs/test/exports/term_evolution.csv",
                "negative_noise_candidates": "runs/test/exports/term_evolution.csv",
            },
            previous_changes=[],
        )
    request = read_json(tmp_path / "query_refinement" / "worker_requests" / "Q0001.json")

    assert "topical_fit_profile" in request["evidence_refs"]
    assert "positive_term_candidates" in request["evidence_refs"]
    assert "topical-fit outcome" in request["instruction"]
    assert "high-frequency in included title/abstract evidence" in request["instruction"]
    assert "negative_noise_candidates" in request["evidence_refs"]
    assert "prohibited_or_rejected_terms" in request["instruction"]
    assert "Broad pollutant-class additions" in request["instruction"]
    assert "narrower contaminant-occurrence phrase" in request["instruction"]
    assert "Prefer surface-water occurrence" in request["instruction"]
    assert "likely no-op terms" in request["instruction"]
    assert "candidate_evidence_preview" in request


def test_query_refinement_request_embeds_candidate_evidence_preview(
    tmp_path: Path,
) -> None:
    (tmp_path / "exports").mkdir()
    (tmp_path / "exports" / "positive_term_candidates.csv").write_text(
        "\n".join(
            [
                "run_id,query_id,iteration,term,concept_block,action,previous_status,new_status,reason,supporting_positive_documents,supporting_negative_documents,discriminative_score,created_at",
                (
                    'run,Q0001,1,field monitoring,monitoring_and_concentration_terms,'
                    'positive_candidate,absent,candidate,Included evidence,"[""doi:a""]",[],1.0,now'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "exports" / "negative_noise_candidates.csv").write_text(
        "\n".join(
            [
                "run_id,query_id,iteration,term,concept_block,action,previous_status,new_status,reason,supporting_positive_documents,supporting_negative_documents,discriminative_score,created_at",
                (
                    'run,Q0001,1,removal,prohibited_or_rejected_terms,'
                    'negative_noise_candidate,absent,candidate,Excluded evidence,[],'
                    '"[""doi:b""]",1.0,now'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    executor = QueryRefinementWorkerExecutor(
        repo_root=Path.cwd(),
        run_dir=tmp_path,
        schema_path=Path.cwd() / "schemas" / "retrieval" / "query_patch.schema.json",
    )
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal-article"]},
            "emerging_contaminant_terms": ["emerging contaminant"],
            "surface_water_terms": ["surface water"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-10",
    )

    with pytest.raises(QueryRefinementBlocked):
        executor.propose(
            accepted_query=parent,
            metrics=_metrics(query_id=parent.query_id),
            evidence_refs={
                "positive_term_candidates": str(
                    tmp_path / "exports" / "positive_term_candidates.csv"
                ),
                "negative_noise_candidates": str(
                    tmp_path / "exports" / "negative_noise_candidates.csv"
                ),
            },
            previous_changes=[],
        )

    request = read_json(tmp_path / "query_refinement" / "worker_requests" / "Q0001.json")
    preview = request["candidate_evidence_preview"]
    assert preview["positive_term_candidates"][0]["term"] == "field monitoring"
    assert preview["positive_term_candidates"][0]["positive_support_count"] == 1
    assert preview["negative_noise_candidates"][0]["term"] == "removal"
    assert preview["negative_noise_candidates"][0]["negative_support_count"] == 1


def test_query_refinement_preview_prioritizes_specific_noise_phrases(
    tmp_path: Path,
) -> None:
    (tmp_path / "exports").mkdir()
    (tmp_path / "exports" / "negative_noise_candidates.csv").write_text(
        "\n".join(
            [
                "run_id,query_id,iteration,term,concept_block,action,previous_status,new_status,reason,supporting_positive_documents,supporting_negative_documents,discriminative_score,created_at",
                (
                    'run,Q0001,1,removal,prohibited_or_rejected_terms,'
                    'negative_noise_candidate,absent,candidate,Excluded evidence,[],'
                    '"[""doi:a"", ""doi:b""]",1.0,now'
                ),
                (
                    'run,Q0001,1,wastewater treatment,prohibited_or_rejected_terms,'
                    'negative_noise_candidate,absent,candidate,Excluded evidence,[],'
                    '"[""doi:c""]",1.0,now'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    executor = QueryRefinementWorkerExecutor(
        repo_root=Path.cwd(),
        run_dir=tmp_path,
        schema_path=Path.cwd() / "schemas" / "retrieval" / "query_patch.schema.json",
    )

    preview = executor._candidate_evidence_preview(  # noqa: SLF001
        {"negative_noise_candidates": str(tmp_path / "exports" / "negative_noise_candidates.csv")}
    )

    assert preview["negative_noise_candidates"][0]["term"] == "wastewater treatment"
    assert preview["negative_noise_candidates"][1]["term"] == "removal"


def test_query_refinement_preview_prioritizes_context_positive_phrases(
    tmp_path: Path,
) -> None:
    (tmp_path / "exports").mkdir()
    (tmp_path / "exports" / "positive_term_candidates.csv").write_text(
        "\n".join(
            [
                "run_id,query_id,iteration,term,concept_block,action,previous_status,new_status,reason,supporting_positive_documents,supporting_negative_documents,discriminative_score,created_at",
                (
                    'run,Q0001,1,organic micropollutants,emerging_contaminant_terms,'
                    'positive_candidate,absent,candidate,Included evidence,'
                    '"[""doi:a"", ""doi:b"", ""doi:c""]",[],1.0,now'
                ),
                (
                    'run,Q0001,1,micropollutant concentrations,'
                    'monitoring_and_concentration_terms,positive_candidate,absent,'
                    'candidate,Included evidence,"[""doi:d""]",[],1.0,now'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    executor = QueryRefinementWorkerExecutor(
        repo_root=Path.cwd(),
        run_dir=tmp_path,
        schema_path=Path.cwd() / "schemas" / "retrieval" / "query_patch.schema.json",
    )

    preview = executor._candidate_evidence_preview(  # noqa: SLF001
        {"positive_term_candidates": str(tmp_path / "exports" / "positive_term_candidates.csv")}
    )

    assert preview["positive_term_candidates"][0]["term"] == "micropollutant concentrations"
    assert preview["positive_term_candidates"][1]["term"] == "organic micropollutants"


def test_query_refinement_evidence_refreshes_term_ledger_before_request(
    tmp_path: Path,
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    run_id = "term-refresh-run"
    run_dir = tmp_path / "runs" / run_id
    ensure_dir(run_dir / "queries" / "Q0001")
    query = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal-article"]},
            "emerging_contaminant_terms": ["emerging contaminant"],
            "surface_water_terms": ["surface water"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-10",
    )
    write_json_atomic(run_dir / "manifest.json", {"run_id": run_id})
    write_json_atomic(run_dir / "queries" / "Q0001" / "metrics.json", _metrics().to_dict())
    (run_dir / "queries" / "Q0001" / "canonical_query.yaml").write_text(
        "\n".join(
            [
                "query_id: Q0001",
                "parent_query_id: null",
                "iteration: 1",
                "date_from: '2006-01-01'",
                "date_to: '2026-07-10'",
                "document_types: ['journal-article']",
                "emerging_contaminant_terms: ['emerging contaminant']",
                "surface_water_terms: ['surface water']",
                "monitoring_and_concentration_terms: ['concentration']",
                "candidate_expansion_terms: []",
                "optional_context_terms: []",
                "prohibited_or_rejected_terms: []",
                "added_terms: []",
                "removed_terms: []",
                "modified_concept_blocks: []",
                "change_rationale: Initial.",
                "evidence_for_change: []",
                "expected_effect: Retrieve.",
                "query_schema_version: 0.1.0",
                "created_at: '2026-07-10T00:00:00Z'",
            ]
        ),
        encoding="utf-8",
    )
    plane = ControlPlane(tmp_path / "control.sqlite3", "test")
    plane.migrate()
    record = NormalizedRecord(
        **{
            **_record().to_dict(),
            "global_record_id": "doi:10.1000/pfas-river-refresh",
            "title_original": "PFAS occurrence in river water",
            "title_normalized": "pfas occurrence in river water",
            "abstract_original": "Field monitoring measured PFAS concentrations in river water.",
            "keywords": ["PFAS", "field monitoring"],
        }
    )
    with plane.transaction() as connection:
        connection.execute(
            """
            INSERT INTO runs (
                run_id, status, completeness, date_from, date_to, current_state,
                started_at, code_commit_sha, git_branch, config_hash, prompt_hash,
                protocol_version, scoring_version, model_version, source_status_json
            )
            VALUES (?, 'running', 'complete', '2006-01-01', '2026-07-10',
                    'MINE_TERMS', '2026-07-10T00:00:00Z', 'test', 'test',
                    'test', 'test', 'test', 'test', 'test', '{}')
            """,
            (run_id,),
        )
        connection.execute(
            """
            INSERT INTO query_iterations (
                run_id, iteration, query_id, parent_query_id, branch_id,
                query_status, acceptance_status, score, score_delta,
                saturation_status, query_known_cutoff, started_at, finalized_at
            )
            VALUES (?, 1, 'Q0001', NULL, 'main', 'completed', 'accepted',
                    0.1, 0.1, 'not_saturated', 0,
                    '2026-07-10T00:00:00Z', '2026-07-10T00:00:01Z')
            """,
            (run_id,),
        )
        runner._resolve_or_insert_document(connection, run_id, query, record)  # noqa: SLF001
        connection.execute(
            """
            INSERT INTO document_query_membership (
                global_record_id, run_id, query_id, iteration, source_name, source_rank,
                first_seen_in_query, already_known_before_query,
                included_in_novelty_sample, novelty_sample_position,
                screening_status_at_iteration, created_at
            )
            VALUES (?, ?, 'Q0001', 1, 'crossref', 1, 1, 0, 1, 1, 'include',
                    '2026-07-10T00:00:00Z')
            """,
            (record.global_record_id, run_id),
        )
        runner._insert_screening_decision(  # noqa: SLF001
            connection,
            ScreeningDecision(
                global_record_id=record.global_record_id,
                decision="include",
                reason_codes=["I_SURFACE_WATER_CONCENTRATION"],
                evidence_spans=["Field monitoring measured PFAS concentrations in river water."],
                query_term_evidence=[
                    {
                        "term": "field monitoring",
                        "concept_block": "monitoring_and_concentration_terms",
                        "evidence_span": "Field monitoring measured PFAS concentrations.",
                        "why_relevant": "field monitoring language matches the target scope",
                        "confidence": 0.9,
                    }
                ],
                article_ec_scope="true",
                screening_decision_id="screen-term-refresh",
                run_id=run_id,
                query_id="Q0001",
                iteration=1,
                prompt_version="test",
                prompt_hash="test",
                model_name="test",
                model_version="test",
                screening_timestamp="2026-07-10T00:00:01Z",
            ),
        )

    refs = runner._query_refinement_evidence_refs(run_dir, run_id)  # noqa: SLF001

    assert "positive_term_candidates.csv" in refs["positive_term_candidates"]
    assert "negative_noise_candidates.csv" in refs["negative_noise_candidates"]
    assert (run_dir / "exports" / "positive_term_candidates.csv").exists()
    assert (run_dir / "exports" / "negative_noise_candidates.csv").exists()
    with plane.connect() as connection:
        row = connection.execute(
            """
            SELECT term, action
            FROM term_ledger
            WHERE run_id = ? AND query_id = 'Q0001'
              AND term = 'field monitoring'
            """,
            (run_id,),
        ).fetchone()
    assert row["action"] == "positive_candidate"


def test_topical_fit_profile_summarizes_true_and_false_positive_patterns(
    tmp_path: Path,
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.sqlite3",
    )
    payloads = [
        json.dumps(
            {
                "global_record_id": "doi:include",
                "decision": "include",
                "reason_codes": ["I_SURFACE_WATER_CONCENTRATION"],
                "query_term_evidence": [
                    {
                        "term": "target quantification",
                        "concept_block": "monitoring_and_concentration_terms",
                    },
                    {"term": "river", "concept_block": "surface_water_terms"},
                ],
            }
        ),
        json.dumps(
            {
                "global_record_id": "doi:exclude",
                "decision": "exclude",
                "reason_codes": ["E_WATER_PLANT", "E_NO_FIELD_SAMPLE"],
                "query_term_evidence": [
                    {
                        "term": "wastewater treatment plants",
                        "concept_block": "exclusion_candidate_terms",
                    },
                    {"term": "removal", "concept_block": "exclusion_candidate_terms"},
                ],
            }
        ),
    ]

    profile = runner._topical_fit_profile(payloads)  # noqa: SLF001

    assert profile["decision_counts"] == {"include": 1, "exclude": 1}
    assert profile["dominant_false_positive_reasons"][0] == {
        "value": "E_NO_FIELD_SAMPLE",
        "count": 1,
    }
    assert profile["true_positive_signals"]["monitoring_and_concentration_terms"] == [
        {"value": "target quantification", "count": 1}
    ]
    assert profile["false_positive_signals"]["exclusion_candidate_terms"] == [
        {"value": "removal", "count": 1},
        {"value": "wastewater treatment plants", "count": 1},
    ]
    assert "Improve retrieval topical fit" in profile["objective"]


def test_query_patch_rejects_unguarded_broad_pollutant_class() -> None:
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal-article"]},
            "emerging_contaminant_terms": ["emerging contaminant"],
            "surface_water_terms": ["surface water"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-10",
    )
    patch = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-broad-antibiotics",
        parent_query_id="Q0001",
        target_concept_block="emerging_contaminant_terms",
        operation="add",
        terms_added=["antibiotics"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/a"],
        rationale="Antibiotics appeared in one included document.",
        expected_effect="Retrieve more antibiotics records.",
        possible_drift_risk="Could broaden scope.",
    )

    result = QueryPatchApplier().apply(parent, patch, child_query_id="Q0002", iteration=2)

    assert result.status == "rejected"
    assert result.reason == "broad_pollutant_class_without_context"


def test_query_patch_allows_context_guarded_broad_pollutant_class() -> None:
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal-article"]},
            "emerging_contaminant_terms": ["emerging contaminant"],
            "surface_water_terms": ["surface water"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-10",
    )
    patch = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-guarded-antibiotics",
        parent_query_id="Q0001",
        target_concept_block="emerging_contaminant_terms",
        operation="add",
        terms_added=["antibiotics"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/a", "doi:10.1000/b"],
        rationale="Included evidence concerns antibiotics in river surface water.",
        expected_effect="Retrieve natural-water occurrence and concentration monitoring records.",
        possible_drift_risk="Reject if gains are laboratory, resistance, or treatment records.",
    )

    result = QueryPatchApplier().apply(parent, patch, child_query_id="Q0002", iteration=2)

    assert result.status == "applied"
    assert result.reason == "compiled_query_changed"


def test_query_patch_rejects_single_document_broad_pollutant_class_support() -> None:
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal-article"]},
            "emerging_contaminant_terms": ["emerging contaminant"],
            "surface_water_terms": ["surface water"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-10",
    )
    patch = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-single-doc-antibiotics",
        parent_query_id="Q0001",
        target_concept_block="emerging_contaminant_terms",
        operation="add",
        terms_added=["antibiotics"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/a"],
        rationale="Included evidence concerns antibiotics in river surface water.",
        expected_effect="Retrieve natural-water occurrence and concentration monitoring records.",
        possible_drift_risk="Reject if gains are laboratory, resistance, or treatment records.",
    )

    result = QueryPatchApplier().apply(parent, patch, child_query_id="Q0002", iteration=2)

    assert result.status == "rejected"
    assert result.reason == "broad_pollutant_class_insufficient_support"


def test_term_mining_extracts_negative_noise_candidates(tmp_path: Path) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        db_path=tmp_path / "control.sqlite3",
        paper_exports_dir=tmp_path / "exports",
    )
    run_id = "run-negative-term-mining"
    query = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal-article"]},
            "emerging_contaminant_terms": ["contaminant*", "microplastic*"],
            "surface_water_terms": ["surface water", "river"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-10",
    )
    record = NormalizedRecord(
        **{
            **_record().to_dict(),
            "global_record_id": "doi:10.1000/fish-noise",
            "title_original": "Microplastic ingestion by fish in river systems",
            "title_normalized": "microplastic ingestion by fish in river systems",
            "abstract_original": (
                "This review evaluates fish biota risk rather than measured "
                "contaminant concentration in natural surface water."
            ),
            "keywords": ["fish", "biota", "review"],
        }
    )
    plane = ControlPlane(tmp_path / "control.sqlite3", "test")
    plane.migrate()
    with plane.transaction() as connection:
        connection.execute(
            """
            INSERT INTO runs (
                run_id, status, completeness, date_from, date_to, current_state,
                started_at, code_commit_sha, git_branch, config_hash, prompt_hash,
                protocol_version, scoring_version, model_version, source_status_json
            )
            VALUES (?, 'running', 'unknown', '2006-01-01', '2026-07-10',
                    'MINE_TERMS', '2026-07-10T00:00:00Z', 'test', 'test',
                    'test', 'test', 'test', 'test', 'test', '{}')
            """,
            (run_id,),
        )
        runner._resolve_or_insert_document(connection, run_id, query, record)  # noqa: SLF001
        connection.execute(
            """
            INSERT INTO document_query_membership (
                global_record_id, run_id, query_id, iteration, source_name, source_rank,
                first_seen_in_query, already_known_before_query,
                included_in_novelty_sample, novelty_sample_position,
                screening_status_at_iteration, created_at
            )
            VALUES (?, ?, ?, 1, 'crossref', 1, 1, 0, 1, 1, 'exclude', '2026-07-10T00:00:00Z')
            """,
            (record.global_record_id, run_id, query.query_id),
        )
        runner._insert_screening_decision(  # noqa: SLF001
            connection,
            ScreeningDecision(
                global_record_id=record.global_record_id,
                decision="exclude",
                reason_codes=["E_REVIEW_OR_ARTIFACT"],
                evidence_spans=["review evaluates fish biota risk"],
                query_term_evidence=[
                    {
                        "term": "fish",
                        "concept_block": "exclusion_candidate_terms",
                        "evidence_span": "fish biota risk",
                        "why_relevant": "off-scope biota endpoint rather than water monitoring",
                        "confidence": 0.94,
                    },
                    {
                        "term": "review",
                        "concept_block": "exclusion_candidate_terms",
                        "evidence_span": "This review evaluates",
                        "why_relevant": "review artifact is excluded by protocol",
                        "confidence": 0.91,
                    },
                ],
                article_ec_scope="false",
                screening_decision_id="screen-negative-term-mining",
                run_id=run_id,
                query_id=query.query_id,
                iteration=1,
                prompt_version="test",
                prompt_hash="test",
                model_name="test",
                model_version="test",
                screening_timestamp="2026-07-10T00:00:01Z",
            ),
        )

    rows = runner._mine_negative_term_candidates(run_id, query)  # noqa: SLF001
    terms = {row["term"]: row for row in rows}

    assert terms["fish"]["concept_block"] == "prohibited_or_rejected_terms"
    assert terms["fish"]["action"] == "negative_noise_candidate"
    assert terms["fish"]["supporting_negative_documents"] == [record.global_record_id]
    assert "review" in terms
    assert "concentration" not in terms


def test_negative_term_mining_prioritizes_specific_noise_phrases(
    tmp_path: Path,
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        db_path=tmp_path / "control.sqlite3",
        paper_exports_dir=tmp_path / "exports",
    )
    run_id = "run-specific-noise-term-mining"
    query = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal-article"]},
            "emerging_contaminant_terms": ["contaminant*"],
            "surface_water_terms": ["surface water", "river"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-10",
    )
    plane = ControlPlane(tmp_path / "control.sqlite3", "test")
    plane.migrate()
    records = [
        (
            "doi:10.1000/removal-noise",
            "Treatment removal of contaminants from water",
            "The study reports removal efficiency in a laboratory treatment system.",
            "removal",
        ),
        (
            "doi:10.1000/wastewater-treatment-noise",
            "Wastewater treatment plant removal of contaminants",
            (
                "This excluded record focuses on wastewater treatment rather than "
                "natural water monitoring."
            ),
            "wastewater treatment",
        ),
    ]
    with plane.transaction() as connection:
        connection.execute(
            """
            INSERT INTO runs (
                run_id, status, completeness, date_from, date_to, current_state,
                started_at, code_commit_sha, git_branch, config_hash, prompt_hash,
                protocol_version, scoring_version, model_version, source_status_json
            )
            VALUES (?, 'running', 'unknown', '2006-01-01', '2026-07-10',
                    'MINE_TERMS', '2026-07-10T00:00:00Z', 'test', 'test',
                    'test', 'test', 'test', 'test', 'test', '{}')
            """,
            (run_id,),
        )
        for rank, (global_id, title, abstract, evidence_term) in enumerate(records, start=1):
            record = NormalizedRecord(
                **{
                    **_record().to_dict(),
                    "global_record_id": global_id,
                    "doi": global_id.removeprefix("doi:"),
                    "normalized_doi": global_id.removeprefix("doi:"),
                    "title_original": title,
                    "title_normalized": title.lower(),
                    "abstract_original": abstract,
                    "keywords": [evidence_term],
                }
            )
            runner._resolve_or_insert_document(connection, run_id, query, record)  # noqa: SLF001
            connection.execute(
                """
                INSERT INTO document_query_membership (
                    global_record_id, run_id, query_id, iteration, source_name, source_rank,
                    first_seen_in_query, already_known_before_query,
                    included_in_novelty_sample, novelty_sample_position,
                    screening_status_at_iteration, created_at
                )
                VALUES (?, ?, ?, 1, 'crossref', ?, 1, 0, 1, ?, 'exclude',
                        '2026-07-10T00:00:00Z')
                """,
                (global_id, run_id, query.query_id, rank, rank),
            )
            runner._insert_screening_decision(  # noqa: SLF001
                connection,
                ScreeningDecision(
                    global_record_id=global_id,
                    decision="exclude",
                    reason_codes=["E_LAB_STUDY"],
                    evidence_spans=[abstract],
                    query_term_evidence=[
                        {
                            "term": evidence_term,
                            "concept_block": "exclusion_candidate_terms",
                            "evidence_span": abstract,
                            "why_relevant": "off-scope treatment noise",
                            "confidence": 0.95,
                        }
                    ],
                    article_ec_scope="false",
                    screening_decision_id=f"screen-{rank}",
                    run_id=run_id,
                    query_id=query.query_id,
                    iteration=1,
                    prompt_version="test",
                    prompt_hash="test",
                    model_name="test",
                    model_version="test",
                    screening_timestamp="2026-07-10T00:00:01Z",
                ),
            )

    rows = runner._mine_negative_term_candidates(run_id, query)  # noqa: SLF001

    assert [row["term"] for row in rows[:2]] == ["wastewater treatment", "removal"]


def test_query_patch_rejects_specific_pollutant_in_emerging_block() -> None:
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal-article"]},
            "emerging_contaminant_terms": ["emerging contaminant"],
            "surface_water_terms": ["surface water"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-10",
    )
    patch = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-specific-pollutant-no-context",
        parent_query_id="Q0001",
        target_concept_block="emerging_contaminant_terms",
        operation="add",
        terms_added=["oxypurinol"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/a"],
        rationale="Oxypurinol appeared in one included document.",
        expected_effect="Retrieve more oxypurinol records.",
        possible_drift_risk="Could broaden scope.",
    )

    result = QueryPatchApplier().apply(parent, patch, child_query_id="Q0002", iteration=2)

    assert result.status == "rejected"
    assert result.reason == "specific_pollutant_not_allowed_in_emerging_block"


def test_query_patch_allows_specific_pollutant_as_optional_context() -> None:
    parent = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal-article"]},
            "emerging_contaminant_terms": ["emerging contaminant"],
            "surface_water_terms": ["surface water"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-10",
    )
    patch = QueryPatch(
        query_patch_schema_version="1.0.0",
        patch_id="patch-specific-pollutant-context",
        parent_query_id="Q0001",
        target_concept_block="optional_context_terms",
        operation="add",
        terms_added=["oxypurinol"],
        terms_removed=[],
        evidence_document_ids=["doi:10.1000/a"],
        rationale="Included evidence reports oxypurinol in surface water.",
        expected_effect="Retrieve field occurrence or concentration records in natural water.",
        possible_drift_risk="Reject if records lack surface-water monitoring context.",
    )

    result = QueryPatchApplier().apply(parent, patch, child_query_id="Q0002", iteration=2)

    assert result.status == "applied"
    assert result.reason == "compiled_query_changed"


def test_term_mining_splits_model_noise_phrases(tmp_path: Path) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        db_path=tmp_path / "control.sqlite3",
        paper_exports_dir=tmp_path / "exports",
    )
    run_id = "run-split-noise-term-mining"
    query = QueryPlanner().build_initial_query(
        {
            "date_range": {"date_from": "2006-01-01"},
            "publication_type": {"prefer": ["journal-article"]},
            "emerging_contaminant_terms": ["contaminant*"],
            "surface_water_terms": ["surface water", "river"],
            "monitoring_and_concentration_terms": ["concentration"],
        },
        "2026-07-10",
    )
    record = NormalizedRecord(
        **{
            **_record().to_dict(),
            "global_record_id": "doi:10.1000/modeling-noise",
            "title_original": "Water quality model for contaminant concentration in rivers",
            "title_normalized": "water quality model for contaminant concentration in rivers",
            "abstract_original": (
                "A hydrodynamic prediction algorithm simulates contaminant "
                "concentration rather than reporting field measurements."
            ),
            "keywords": ["hydrodynamic model", "prediction"],
        }
    )
    plane = ControlPlane(tmp_path / "control.sqlite3", "test")
    plane.migrate()
    with plane.transaction() as connection:
        connection.execute(
            """
            INSERT INTO runs (
                run_id, status, completeness, date_from, date_to, current_state,
                started_at, code_commit_sha, git_branch, config_hash, prompt_hash,
                protocol_version, scoring_version, model_version, source_status_json
            )
            VALUES (?, 'running', 'unknown', '2006-01-01', '2026-07-10',
                    'MINE_TERMS', '2026-07-10T00:00:00Z', 'test', 'test',
                    'test', 'test', 'test', 'test', 'test', '{}')
            """,
            (run_id,),
        )
        runner._resolve_or_insert_document(connection, run_id, query, record)  # noqa: SLF001
        connection.execute(
            """
            INSERT INTO document_query_membership (
                global_record_id, run_id, query_id, iteration, source_name, source_rank,
                first_seen_in_query, already_known_before_query,
                included_in_novelty_sample, novelty_sample_position,
                screening_status_at_iteration, created_at
            )
            VALUES (?, ?, ?, 1, 'crossref', 1, 1, 0, 1, 1, 'exclude',
                    '2026-07-10T00:00:00Z')
            """,
            (record.global_record_id, run_id, query.query_id),
        )
        runner._insert_screening_decision(  # noqa: SLF001
            connection,
            ScreeningDecision(
                global_record_id=record.global_record_id,
                decision="exclude",
                reason_codes=["E_LAB_STUDY", "E_NO_CONCENTRATION"],
                evidence_spans=["modeling or prediction record"],
                query_term_evidence=[
                    {
                        "term": "modeling or prediction",
                        "concept_block": "exclusion_candidate_terms",
                        "evidence_span": "hydrodynamic prediction algorithm",
                        "why_relevant": "off-scope model rather than field monitoring",
                        "confidence": 0.9,
                    }
                ],
                article_ec_scope="false",
                screening_decision_id="screen-split-noise-term-mining",
                run_id=run_id,
                query_id=query.query_id,
                iteration=1,
                prompt_version="test",
                prompt_hash="test",
                model_name="test",
                model_version="test",
                screening_timestamp="2026-07-10T00:00:01Z",
            ),
        )

    terms = {
        row["term"]: row
        for row in runner._mine_negative_term_candidates(run_id, query)  # noqa: SLF001
    }

    assert terms["modeling"]["concept_block"] == "prohibited_or_rejected_terms"
    assert terms["prediction"]["concept_block"] == "prohibited_or_rejected_terms"
