import hashlib
import json
import shutil
from pathlib import Path

import pytest

from ecmonitor.retrieval_specialist.models import ScreeningDecision
from ecmonitor.retrieval_specialist.operators.gpt_screening import ScreeningWorkerBlocked
from ecmonitor.retrieval_specialist.orchestration.checkpoints import CheckpointManager
from ecmonitor.retrieval_specialist.orchestration.run_manager import RunManager
from ecmonitor.retrieval_specialist.storage.atomic_io import (
    count_jsonl,
    ensure_dir,
    read_json,
    read_jsonl,
    read_yaml,
    write_csv_atomic,
    write_json_atomic,
)


def prepare_repo(tmp_path: Path) -> Path:
    source_root = Path(__file__).resolve().parents[2]
    for dirname in ["configs", "prompts", "registry"]:
        shutil.copytree(source_root / dirname, tmp_path / dirname)
    ensure_dir(tmp_path / "tests" / "fixtures")
    shutil.copy2(
        source_root / "tests" / "fixtures" / "mock_records.json",
        tmp_path / "tests" / "fixtures" / "mock_records.json",
    )
    return tmp_path


def test_mock_run_writes_checkpoints_exports_and_summary(tmp_path) -> None:
    repo = prepare_repo(tmp_path)
    manager = RunManager(repo)
    result = manager.mock_run(date_to="2026-07-09", run_id="retrieval_test")
    assert result["status"] == "completed"
    assert (repo / "runs" / "retrieval_test" / "RUN_SUMMARY.md").exists()
    assert (repo / "runs" / "retrieval_test" / "checkpoints" / "STOP.json").exists()
    assert (repo / "paper_exports" / "query_metrics_wide.csv").exists()
    assert (repo / "paper_exports" / "source_contribution.csv").exists()


def test_mock_run_accepts_runtime_date_from_override(tmp_path) -> None:
    repo = prepare_repo(tmp_path)
    manager = RunManager(repo)
    result = manager.mock_run(
        date_from="2010-01-01",
        date_to="2026-07-09",
        run_id="retrieval_date_from",
    )
    manifest = read_json(repo / "runs" / "retrieval_date_from" / "manifest.json")
    query = read_yaml(
        repo / "runs" / "retrieval_date_from" / "queries" / "Q0001" / "canonical_query.yaml"
    )

    assert result["status"] == "completed"
    assert manifest["date_from"] == "2010-01-01"
    assert query["date_from"] == "2010-01-01"


def test_completed_iteration_is_reconstructable_from_disk(tmp_path) -> None:
    repo = prepare_repo(tmp_path)
    manager = RunManager(repo)
    manager.mock_run(date_to="2026-07-09", run_id="retrieval_reconstruct")
    run_dir = repo / "runs" / "retrieval_reconstruct"

    completion = read_json(run_dir / "iteration_completion.json")
    assert completion["iteration_status"] == "COMPLETED"
    assert completion["counts"]["raw_records"] == count_jsonl(
        run_dir / "raw_metadata" / "raw_records.jsonl"
    )
    assert completion["counts"]["normalized_records"] == count_jsonl(
        run_dir / "normalized" / "records.jsonl"
    )
    assert completion["counts"]["screening_decisions"] == count_jsonl(
        run_dir / "screening" / "screening_decisions.jsonl"
    )
    assert (run_dir / "checksums" / "Q0001_iteration_completion.json").exists()


def test_export_paper_data_rebuilds_exports_from_persisted_records(tmp_path) -> None:
    repo = prepare_repo(tmp_path)
    manager = RunManager(repo)
    manager.mock_run(date_to="2026-07-09", run_id="retrieval_rebuild_exports")
    run_dir = repo / "runs" / "retrieval_rebuild_exports"

    shutil.rmtree(run_dir / "exports")
    shutil.rmtree(repo / "paper_exports")

    result = manager.export_paper_data("retrieval_rebuild_exports")
    assert result["rebuilt_exports"] == str(run_dir / "exports")
    assert (run_dir / "exports" / "query_metrics_wide.csv").exists()
    assert (run_dir / "exports" / "runtime_resource_trajectory.csv").exists()
    assert (repo / "paper_exports" / "download_handoff_trajectory.csv").exists()


def test_bounded_batches_memory_telemetry_and_handoffs_are_durable(tmp_path) -> None:
    repo = prepare_repo(tmp_path)
    manager = RunManager(repo)
    manager.mock_run(date_to="2026-07-09", run_id="retrieval_bounded")
    run_dir = repo / "runs" / "retrieval_bounded"

    assert list((run_dir / "raw_metadata" / "batches").glob("*.jsonl"))
    assert list((run_dir / "normalized" / "batches").glob("*.jsonl"))
    assert list((run_dir / "screening" / "batches").glob("*.jsonl"))

    memory_rows = list(read_jsonl(run_dir / "logs" / "memory_usage.jsonl"))
    assert memory_rows
    assert all(row["cleanup_performed"] for row in memory_rows)
    assert any(float(row["resident_memory_after_cleanup_mb"]) > 0 for row in memory_rows)

    decisions = list(read_jsonl(run_dir / "screening" / "screening_decisions.jsonl"))
    events = list(read_jsonl(run_dir / "handoff" / "download" / "download_events.jsonl"))
    included_ids = {row["global_record_id"] for row in decisions if row["decision"] == "include"}
    excluded_or_deferred_ids = {
        row["global_record_id"] for row in decisions if row["decision"] != "include"
    }
    event_ids = {row["global_record_id"] for row in events}
    assert event_ids == included_ids
    assert event_ids.isdisjoint(excluded_or_deferred_ids)
    assert len({row["idempotency_key"] for row in events}) == len(events)

    handoff_root = repo / "handoff" / "download"
    assert (handoff_root / "outbox" / "download_events.jsonl").exists()
    assert (handoff_root / "acknowledgements" / "download_acknowledgements.jsonl").exists()
    assert (handoff_root / "results" / "download_results.jsonl").exists()
    assert (handoff_root / "dead_letter" / "dead_letter_events.jsonl").exists()


def test_duplicate_download_handoffs_are_suppressed_across_runs(tmp_path) -> None:
    repo = prepare_repo(tmp_path)
    manager = RunManager(repo)
    manager.mock_run(date_to="2026-07-09", run_id="retrieval_first")
    first_metrics = read_json(repo / "runs" / "retrieval_first" / "metrics" / "metrics.json")
    root_event_log = repo / "handoff" / "download" / "outbox" / "download_events.jsonl"
    first_event_count = count_jsonl(root_event_log)

    manager.mock_run(date_to="2026-07-09", run_id="retrieval_second")
    second_metrics = read_json(repo / "runs" / "retrieval_second" / "metrics" / "metrics.json")

    assert first_metrics["download_requests_emitted"] > 0
    assert count_jsonl(root_event_log) == first_event_count
    assert second_metrics["download_requests_emitted"] == 0
    assert second_metrics["duplicate_handoffs_suppressed"] >= first_metrics["include_count"]


def test_iteration_completion_requires_required_artifacts(tmp_path) -> None:
    repo = prepare_repo(tmp_path)
    manager = RunManager(repo)
    run_dir = ensure_dir(repo / "runs" / "retrieval_missing_artifacts")

    with pytest.raises(RuntimeError, match="missing artifact"):
        manager._finalize_iteration(
            run_dir=run_dir,
            query_id="Q0001",
            expected_counts={
                "raw_records": 1,
                "normalized_records": 1,
                "deduplicated_records": 1,
                "screening_decisions": 1,
                "download_events": 1,
            },
        )

    assert not (run_dir / "iteration_completion.json").exists()


def test_source_pages_are_bounded_by_configured_page_size(tmp_path) -> None:
    repo = prepare_repo(tmp_path)
    manager = RunManager(repo)
    manager.mock_run(date_to="2026-07-09", run_id="retrieval_pages", max_scan_depth=4)
    run_dir = repo / "runs" / "retrieval_pages"
    runtime_snapshot = read_yaml(run_dir / "runtime_snapshot.yaml")
    configured_page_size = int(runtime_snapshot["runtime"]["retrieval_page_size"])
    for batch_path in (run_dir / "raw_metadata" / "batches").glob("*.jsonl"):
        batch_rows = [json.loads(line) for line in batch_path.read_text().splitlines() if line]
        assert len(batch_rows) <= configured_page_size


def test_resume_continues_interrupted_mock_run(tmp_path) -> None:
    repo = prepare_repo(tmp_path)
    manager = RunManager(repo)
    run_dir = ensure_dir(repo / "runs" / "retrieval_interrupted")
    ensure_dir(run_dir / "checkpoints")
    write_json_atomic(
        run_dir / "manifest.json", {"run_id": "retrieval_interrupted", "date_to": "2026-07-09"}
    )
    CheckpointManager(run_dir).save("NORMALIZE", {"interrupted": True})
    result = manager.resume("retrieval_interrupted")
    assert result["status"] == "completed"
    assert CheckpointManager(run_dir).latest()["state"] == "STOP"


def test_rollback_writes_active_query(tmp_path) -> None:
    repo = prepare_repo(tmp_path)
    manager = RunManager(repo)
    manager.mock_run(date_to="2026-07-09", run_id="retrieval_rollback")
    result = manager.rollback("retrieval_rollback", "Q0001")
    assert result["query_id"] == "Q0001"
    assert (repo / "runs" / "retrieval_rollback" / "active_query.json").exists()


def test_candidate_pool_analysis_scores_families_and_preserves_high_recall_goal(
    tmp_path,
) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    write_json_atomic(
        pool_root / "manifest.json",
        {
            "run_id": "pool",
            "raw_candidate_count": 3,
            "duplicate_link_count": 0,
        },
    )
    records = [
        {
            "candidate_pool_key": "doi:10.1/a",
            "title": "PFAS occurrence in river water",
            "doi": "10.1/a",
            "abstract": "Measured PFAS in river water.",
            "query_families": ["pfas"],
            "source_providers": ["crossref"],
            "document_type": "journal-article",
            "language": None,
        },
        {
            "candidate_pool_key": "doi:10.1/b",
            "title": "PFAS removal by adsorbent",
            "doi": "10.1/b",
            "abstract": "PFAS removal experiment.",
            "query_families": ["pfas"],
            "source_providers": ["openalex"],
            "document_type": "article",
            "language": "en",
        },
        {
            "candidate_pool_key": "doi:10.1/c",
            "title": "Endocrine compounds in surface water",
            "doi": "10.1/c",
            "abstract": "Endocrine compounds in surface water.",
            "query_families": ["hormones"],
            "source_providers": ["pubmed"],
            "document_type": "journal article",
            "language": "eng",
        },
        {
            "candidate_pool_key": "doi:10.1/d",
            "title": "Pesticides in river water",
            "doi": "10.1/d",
            "abstract": "Measured pesticides in river water.",
            "query_families": ["pfas", "pesticides"],
            "source_providers": ["openalex", "pubmed"],
            "document_type": "article",
            "language": "en",
        },
    ]
    decisions = [
        {
            "global_record_id": "doi:10.1/a",
            "decision": "include",
            "query_families": ["pfas"],
            "source_providers": ["crossref"],
            "reason_codes": ["I_SURFACE_WATER_CONCENTRATION"],
            "evidence_spans": ["Measured PFAS in river water."],
            "query_term_evidence": [
                {
                    "term": "river water",
                    "concept_block": "surface_water_terms",
                }
            ],
        },
        {
            "global_record_id": "doi:10.1/b",
            "decision": "exclude",
            "query_families": ["pfas"],
            "source_providers": ["openalex"],
            "reason_codes": ["E_LAB_STUDY"],
            "evidence_spans": ["PFAS removal experiment."],
            "query_term_evidence": [
                {
                    "term": "removal",
                    "concept_block": "exclusion_candidate_terms",
                }
            ],
        },
        {
            "global_record_id": "doi:10.1/c",
            "decision": "include",
            "query_families": ["hormones"],
            "source_providers": ["pubmed"],
            "reason_codes": ["I_SURFACE_WATER_CONCENTRATION"],
            "evidence_spans": ["Endocrine compounds in surface water."],
            "query_term_evidence": [],
        },
        {
            "global_record_id": "doi:10.1/d",
            "decision": "include",
            "query_families": ["pfas", "pesticides"],
            "source_providers": ["openalex", "pubmed"],
            "reason_codes": ["I_SURFACE_WATER_CONCENTRATION"],
            "evidence_spans": ["Measured pesticides in river water."],
            "query_term_evidence": [],
        },
    ]
    (pool_root / "records.jsonl").write_text(
        "\n".join(json.dumps(row) for row in records) + "\n",
        encoding="utf-8",
    )
    (pool_root / "screening_decisions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in decisions) + "\n",
        encoding="utf-8",
    )

    result = RunManager(repo).analyze_candidate_pool(pool_id="pool")
    analysis = read_json(pool_root / "candidate_pool_analysis.json")

    assert result["status"] == "completed"
    assert analysis["screening"]["screened_total"] == 4
    assert analysis["screening"]["unscreened_total"] == 0
    assert analysis["query_family_metrics"]["pfas"]["decision_counts"]["include"] == 2
    assert analysis["query_family_metrics"]["pfas"]["unique_include_count"] == 1
    assert analysis["query_family_metrics"]["hormones"]["unique_include_count"] == 1
    assert (
        analysis["query_family_metrics"]["hormones"]["recommended_role"]
        == "unique_recall_branch"
    )
    assert analysis["query_family_metrics"]["pesticides"]["unique_include_count"] == 0
    assert analysis["term_analysis"]["noise_terms_by_block"]["exclusion_candidate_terms"][0][
        "value"
    ] == "removal"
    assert "unique included records" in analysis["objective"]
    assert (pool_root / "CANDIDATE_POOL_ANALYSIS.md").exists()


def test_audit_candidate_pool_writes_targeted_loss_audit_samples(tmp_path) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    records = [
        {
            "candidate_pool_key": "doi:10.1/include",
            "title": "PFAS concentrations in river water near wastewater inputs",
            "doi": "10.1/include",
            "abstract": "Measured PFAS concentrations in river water and wastewater inputs.",
            "query_families": ["pfas"],
            "source_providers": ["crossref"],
            "document_type": "journal-article",
            "language": "en",
        },
        {
            "candidate_pool_key": "doi:10.1/exclude",
            "title": "Review of microplastic concentrations in river water",
            "doi": "10.1/exclude",
            "abstract": "A systematic review of microplastic concentration patterns in rivers.",
            "query_families": ["microplastics"],
            "source_providers": ["pubmed"],
            "document_type": "review",
            "language": "en",
        },
        {
            "candidate_pool_key": "doi:10.1/defer",
            "title": "Microplastic pollution in lake water",
            "doi": "10.1/defer",
            "abstract": "Microplastic monitoring in lake water.",
            "query_families": ["microplastics"],
            "source_providers": ["crossref"],
            "document_type": "article",
            "language": "en",
        },
    ]
    decisions = [
        {
            "global_record_id": "doi:10.1/include",
            "decision": "include",
            "query_families": ["pfas"],
            "source_providers": ["crossref"],
            "reason_codes": ["I_SURFACE_WATER_CONCENTRATION"],
            "evidence_spans": ["Measured PFAS concentrations in river water."],
            "query_term_evidence": [],
        },
        {
            "global_record_id": "doi:10.1/exclude",
            "decision": "exclude",
            "query_families": ["microplastics"],
            "source_providers": ["pubmed"],
            "reason_codes": ["E_REVIEW_OR_ARTIFACT"],
            "evidence_spans": ["Review of microplastic concentrations in river water."],
            "query_term_evidence": [],
        },
        {
            "global_record_id": "doi:10.1/defer",
            "decision": "defer_metadata",
            "query_families": ["microplastics"],
            "source_providers": ["crossref"],
            "reason_codes": ["D_AMBIGUOUS_CONCENTRATION"],
            "evidence_spans": ["Microplastic monitoring in lake water."],
            "query_term_evidence": [],
        },
    ]
    (pool_root / "records.jsonl").write_text(
        "\n".join(json.dumps(row) for row in records) + "\n",
        encoding="utf-8",
    )
    (pool_root / "screening_decisions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in decisions) + "\n",
        encoding="utf-8",
    )

    result = RunManager(repo).audit_candidate_pool(pool_id="pool", sample_size=10)
    summary = read_json(pool_root / "targeted_audit" / "targeted_audit_summary.json")

    assert result["status"] == "completed"
    assert summary["sample_counts"]["crossref_include_review_sample"] == 1
    assert summary["sample_counts"]["risky_exclude_false_negative_sample"] == 1
    assert summary["sample_counts"]["defer_resolution_sample"] == 1
    assert (pool_root / "targeted_audit" / "TARGETED_AUDIT_SUMMARY.md").exists()
    wastewater = next(
        row for row in summary["noise_term_loss_audit"] if row["term"] == "wastewater"
    )
    assert wastewater["include_occurrences"] == 1
    assert wastewater["loss_risk"] == "high"


def test_screen_candidate_pool_filters_by_provider_and_family(
    tmp_path, monkeypatch
) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    records = [
        {
            "candidate_pool_key": "doi:10.1/crossref",
            "title": "PFAS concentrations in river water",
            "doi": "10.1/crossref",
            "abstract": "Measured PFAS in river water.",
            "query_families": ["pfas"],
            "source_providers": ["crossref"],
            "document_type": "journal-article",
            "language": "en",
        },
        {
            "candidate_pool_key": "doi:10.1/pubmed-pfas",
            "title": "Microplastic occurrence in lake water",
            "doi": "10.1/pubmed-pfas",
            "abstract": "Measured microplastics in lake water.",
            "query_families": ["pfas", "microplastics"],
            "source_providers": ["pubmed"],
            "document_type": "journal-article",
            "language": "en",
        },
        {
            "candidate_pool_key": "doi:10.1/pubmed-hormones",
            "title": "Estradiol in river water",
            "doi": "10.1/pubmed-hormones",
            "abstract": "Measured estradiol in river water.",
            "query_families": ["hormones"],
            "source_providers": ["pubmed"],
            "document_type": "journal-article",
            "language": "en",
        },
    ]
    (pool_root / "records.jsonl").write_text(
        "\n".join(json.dumps(row) for row in records) + "\n",
        encoding="utf-8",
    )

    seen_ids: list[str] = []

    def fake_screen_one(self, record, **kwargs):  # noqa: ANN001
        del self, kwargs
        seen_ids.append(record.global_record_id)
        return ScreeningDecision(
            screening_decision_id=f"screen_{record.global_record_id}",
            global_record_id=record.global_record_id,
            run_id="pool",
            query_id="CANDIDATE_POOL",
            iteration=0,
            decision="include",
            confidence=0.95,
            article_type_ok=True,
            date_ok=True,
            emerging_contaminant_context=True,
            surface_water_sample=True,
            field_environmental_samples=True,
            concentration_evidence="explicit_quantified",
            study_type="field monitoring",
            reason_codes=["I_SURFACE_WATER_CONCENTRATION"],
            evidence_spans=[record.title_original],
            article_ec_scope="in_scope",
        )

    monkeypatch.setattr(
        "ecmonitor.retrieval_specialist.operators.gpt_screening."
        "TitleAbstractScreeningWorkerExecutor.screen_one",
        fake_screen_one,
    )

    result = RunManager(repo).screen_candidate_pool(
        pool_id="pool",
        max_records=10,
        providers=["pubmed"],
        query_families=["pfas"],
    )
    decisions = list(read_jsonl(pool_root / "screening_decisions.jsonl"))

    assert result["status"] == "completed"
    assert result["provider_filter"] == ["pubmed"]
    assert result["query_family_filter"] == ["pfas"]
    assert seen_ids == ["doi:10.1/pubmed-pfas"]
    assert [row["global_record_id"] for row in decisions] == ["doi:10.1/pubmed-pfas"]


def test_screen_candidate_pool_prioritizes_abstracts_and_family_coverage(
    tmp_path,
    monkeypatch,
) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    records = [
        {
            "candidate_pool_key": "doi:10.1/no-abstract-a",
            "title": "Endocrine disrupting chemicals",
            "doi": "10.1/no-abstract-a",
            "abstract": "",
            "query_families": ["hormones"],
            "source_providers": ["crossref"],
            "source_rank": 1,
        },
        {
            "candidate_pool_key": "doi:10.1/abstract-a",
            "title": "PFAS concentrations in river water",
            "doi": "10.1/abstract-a",
            "abstract": "Measured PFAS in river water.",
            "query_families": ["pfas"],
            "source_providers": ["crossref"],
            "source_rank": 20,
        },
        {
            "candidate_pool_key": "doi:10.1/abstract-b",
            "title": "Microplastics in lake water",
            "doi": "10.1/abstract-b",
            "abstract": "Measured microplastics in lake water.",
            "query_families": ["microplastics"],
            "source_providers": ["crossref"],
            "source_rank": 30,
        },
        {
            "candidate_pool_key": "doi:10.1/abstract-a2",
            "title": "PFAS occurrence in streams",
            "doi": "10.1/abstract-a2",
            "abstract": "Measured PFAS in streams.",
            "query_families": ["pfas"],
            "source_providers": ["crossref"],
            "source_rank": 1,
        },
    ]
    (pool_root / "records.jsonl").write_text(
        "\n".join(json.dumps(row) for row in records) + "\n",
        encoding="utf-8",
    )
    seen_ids: list[str] = []

    def fake_screen_one(self, record, **kwargs):  # noqa: ANN001
        del self, kwargs
        seen_ids.append(record.global_record_id)
        return ScreeningDecision(
            screening_decision_id=f"screen_{record.global_record_id}",
            global_record_id=record.global_record_id,
            run_id="pool",
            query_id="CANDIDATE_POOL",
            iteration=0,
            decision="include",
            confidence=0.95,
            article_type_ok=True,
            date_ok=True,
            emerging_contaminant_context=True,
            surface_water_sample=True,
            field_environmental_samples=True,
            concentration_evidence="explicit_quantified",
            study_type="field monitoring",
            reason_codes=["I_SURFACE_WATER_CONCENTRATION"],
            evidence_spans=[record.title_original],
            article_ec_scope="in_scope",
        )

    monkeypatch.setattr(
        "ecmonitor.retrieval_specialist.operators.gpt_screening."
        "TitleAbstractScreeningWorkerExecutor.screen_one",
        fake_screen_one,
    )

    RunManager(repo).screen_candidate_pool(pool_id="pool", max_records=3)

    assert seen_ids == [
        "doi:10.1/abstract-b",
        "doi:10.1/abstract-a2",
        "doi:10.1/abstract-a",
    ]


def test_screen_candidate_pool_overlays_enriched_abstract_and_keeps_pending_status(
    tmp_path,
) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    record = {
        "candidate_pool_key": "doi:10.1/enriched",
        "title": "PFAS occurrence in river water",
        "doi": "10.1/enriched",
        "abstract": "",
        "query_families": ["pfas"],
        "source_providers": ["crossref"],
    }
    (pool_root / "records.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )
    ensure_dir(pool_root / "metadata_enrichment")
    (pool_root / "metadata_enrichment" / "enriched_records.jsonl").write_text(
        json.dumps(
            {
                "candidate_pool_key": "doi:10.1/enriched",
                "abstract": "Measured PFAS concentrations in river water.",
                "enrichment_provider": "openalex",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    write_json_atomic(
        pool_root / "manifest.json",
        {
            "run_id": "pool",
            "run_status": "completed",
            "query_family_counts": {"pfas": 1},
        },
    )

    first = RunManager(repo).screen_candidate_pool(pool_id="pool", max_records=1)
    ledger = list(read_jsonl(pool_root / "screening_worker_requests.jsonl"))
    request = read_json(repo / str(ledger[0]["request_ref"]))

    assert first["status"] == "paused_screening_worker_required"
    assert first["worker_requests_created"] == 1
    assert first["pending_worker_results"] == 1
    assert request["document"]["abstract"] == (
        "Measured PFAS concentrations in river water."
    )

    second = RunManager(repo).screen_candidate_pool(pool_id="pool", max_records=1)

    assert second["status"] == "paused_screening_worker_required"
    assert second["worker_requests_created"] == 0
    assert second["pending_worker_results"] == 1
    assert len(list(read_jsonl(pool_root / "screening_worker_requests.jsonl"))) == 1

    RunManager(repo).analyze_candidate_pool(pool_id="pool")
    analysis = read_json(pool_root / "candidate_pool_analysis.json")
    assert analysis["candidate_pool"]["missing_abstracts_before_enrichment"] == 1
    assert analysis["candidate_pool"]["abstracts_recovered_by_enrichment"] == 1
    assert analysis["candidate_pool"]["missing_abstracts"] == 0


def test_review_candidate_pool_audit_creates_isolated_review_requests(
    tmp_path, monkeypatch
) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    records = [
        {
            "candidate_pool_key": "doi:10.1/include",
            "title": "PFAS treatment review in river water",
            "doi": "10.1/include",
            "abstract": "Review of PFAS treatment in river water.",
            "query_families": ["pfas"],
            "source_providers": ["crossref"],
        },
        {
            "candidate_pool_key": "doi:10.1/exclude",
            "title": "Microplastic occurrence in lake water",
            "doi": "10.1/exclude",
            "abstract": "Measured microplastic abundance in lake water.",
            "query_families": ["microplastics"],
            "source_providers": ["pubmed"],
        },
    ]
    decisions = [
        {
            "global_record_id": "doi:10.1/include",
            "decision": "include",
            "query_families": ["pfas"],
            "source_providers": ["crossref"],
            "reason_codes": ["I_SURFACE_WATER_CONCENTRATION"],
            "evidence_spans": ["PFAS treatment in river water."],
        },
        {
            "global_record_id": "doi:10.1/exclude",
            "decision": "exclude",
            "query_families": ["microplastics"],
            "source_providers": ["pubmed"],
            "reason_codes": ["E_LAB_STUDY"],
            "evidence_spans": ["Microplastic occurrence in lake water."],
        },
    ]
    (pool_root / "records.jsonl").write_text(
        "\n".join(json.dumps(row) for row in records) + "\n",
        encoding="utf-8",
    )
    (pool_root / "screening_decisions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in decisions) + "\n",
        encoding="utf-8",
    )
    RunManager(repo).audit_candidate_pool(pool_id="pool", sample_size=10)

    def fake_screen_one(self, record, **kwargs):  # noqa: ANN001
        del self, kwargs
        if record.global_record_id == "doi:10.1/exclude":
            return ScreeningDecision(
                screening_decision_id="screen_review_exclude",
                global_record_id=record.global_record_id,
                run_id="pool",
                query_id="CANDIDATE_POOL_AUDIT",
                iteration=0,
                decision="include",
                confidence=0.95,
                article_type_ok=True,
                date_ok=True,
                emerging_contaminant_context=True,
                surface_water_sample=True,
                field_environmental_samples=True,
                concentration_evidence="explicit_quantified",
                study_type="field monitoring",
                reason_codes=["I_SURFACE_WATER_CONCENTRATION"],
                evidence_spans=["Measured microplastic abundance in lake water."],
                article_ec_scope="true",
            )
        raise ScreeningWorkerBlocked(
            {
                "status": "paused_screening_worker_required",
                "request_ref": "runs/pool/screening/worker_requests/request.json",
                "result_ref": "runs/pool/screening/worker_results/result.json",
            }
        )

    monkeypatch.setattr(
        "ecmonitor.retrieval_specialist.operators.gpt_screening."
        "TitleAbstractScreeningWorkerExecutor.screen_one",
        fake_screen_one,
    )

    result = RunManager(repo).review_candidate_pool_audit(pool_id="pool")
    summary = read_json(pool_root / "audit_review_summary.json")
    review_rows = list(read_jsonl(pool_root / "audit_review_decisions.jsonl"))
    request_rows = list(read_jsonl(pool_root / "audit_review_worker_requests.jsonl"))

    assert result["status"] == "paused_screening_worker_required"
    assert summary["reviewed_total"] == 1
    assert summary["review_outcome_counts"]["false_negative_risk"] == 1
    assert review_rows[0]["review_outcome"] == "false_negative_risk"
    assert request_rows[0]["global_record_id"] == "doi:10.1/include"
    assert (pool_root / "AUDIT_REVIEW_SUMMARY.md").exists()

    result = RunManager(repo).review_candidate_pool_audit(pool_id="pool", max_records=1)

    assert result["worker_requests_created"] == 1


def test_reuse_candidate_pool_screening_copies_by_candidate_pool_key(tmp_path) -> None:
    repo = prepare_repo(tmp_path)
    target_root = ensure_dir(repo / "runs" / "target" / "candidate_pool")
    source_root = ensure_dir(repo / "runs" / "source" / "candidate_pool")
    target_records = [
        {
            "candidate_pool_key": "doi:10.1/reused",
            "title": "PFAS occurrence in river water",
            "doi": "10.1/reused",
            "query_families": ["pfas-expanded"],
            "source_providers": ["crossref"],
        },
        {
            "candidate_pool_key": "doi:10.1/new",
            "title": "Microplastic occurrence in lake water",
            "doi": "10.1/new",
            "query_families": ["microplastics"],
            "source_providers": ["pubmed"],
        },
    ]
    source_decisions = [
        {
            "global_record_id": "doi:10.1/reused",
            "run_id": "source",
            "query_id": "CANDIDATE_POOL",
            "decision": "include",
            "reason_codes": ["I_SURFACE_WATER_CONCENTRATION"],
            "evidence_spans": ["PFAS occurrence in river water."],
            "query_families": ["pfas"],
            "source_providers": ["pubmed"],
        }
    ]
    (target_root / "records.jsonl").write_text(
        "\n".join(json.dumps(row) for row in target_records) + "\n",
        encoding="utf-8",
    )
    (source_root / "screening_decisions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in source_decisions) + "\n",
        encoding="utf-8",
    )

    result = RunManager(repo).reuse_candidate_pool_screening(
        pool_id="target",
        source_pool_ids=["source"],
    )
    decisions = list(read_jsonl(target_root / "screening_decisions.jsonl"))

    assert result["reused_decisions"] == 1
    assert result["missing_decisions"] == 1
    assert decisions[0]["run_id"] == "target"
    assert decisions[0]["query_families"] == ["pfas-expanded"]
    assert decisions[0]["source_providers"] == ["crossref"]
    assert decisions[0]["candidate_pool_reuse_source"] == "source"
    assert (target_root / "screening_reuse_summary.json").exists()


def test_safe_defer_candidate_pool_screening_completes_unscreened_records(tmp_path) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    records = [
        {
            "candidate_pool_key": "doi:10.1/unscreened",
            "title": "Emerging contaminant occurrence in river water",
            "doi": "10.1/unscreened",
            "abstract": "",
            "query_families": ["generic"],
            "source_providers": ["crossref"],
            "document_type": "journal-article",
        }
    ]
    (pool_root / "records.jsonl").write_text(
        "\n".join(json.dumps(row) for row in records) + "\n",
        encoding="utf-8",
    )
    (pool_root / "manifest.json").write_text(
        json.dumps({"run_id": "pool"}), encoding="utf-8"
    )

    result = RunManager(repo).safe_defer_candidate_pool_screening(pool_id="pool")
    decisions = list(read_jsonl(pool_root / "screening_decisions.jsonl"))
    requests = list(read_jsonl(pool_root / "screening_worker_requests.jsonl"))

    assert result["status"] == "completed"
    assert result["safe_defer_results_created"] == 1
    assert result["screening_decisions_completed"] == 1
    assert decisions[0]["decision"] == "defer_metadata"
    assert decisions[0]["reason_codes"] == ["D_METADATA_MISSING"]
    assert decisions[0]["candidate_pool_safe_defer"] is True
    assert requests[0]["status"] == "safe_defer_result_created"


def test_export_candidate_pool_review_uses_review_overrides(tmp_path) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    records = [
        {
            "candidate_pool_key": "doi:10.1/include",
            "title": "PFAS treatment review in river water",
            "doi": "10.1/include",
            "abstract": "Review of PFAS treatment in river water.",
            "query_families": ["pfas"],
            "source_providers": ["crossref"],
            "document_type": "review",
            "language": "en",
        },
        {
            "candidate_pool_key": "doi:10.1/exclude",
            "title": "Microplastic occurrence in lake water",
            "doi": "10.1/exclude",
            "abstract": "Measured microplastic abundance in lake water.",
            "query_families": ["microplastics"],
            "source_providers": ["pubmed"],
            "document_type": "journal article",
            "language": "eng",
        },
    ]
    decisions = [
        {
            "global_record_id": "doi:10.1/include",
            "decision": "include",
            "query_families": ["pfas"],
            "source_providers": ["crossref"],
            "reason_codes": ["I_SURFACE_WATER_CONCENTRATION"],
            "evidence_spans": ["PFAS treatment in river water."],
        },
        {
            "global_record_id": "doi:10.1/exclude",
            "decision": "exclude",
            "query_families": ["microplastics"],
            "source_providers": ["pubmed"],
            "reason_codes": ["E_LAB_STUDY"],
            "evidence_spans": ["Microplastic occurrence in lake water."],
        },
    ]
    review_decisions = [
        {
            "global_record_id": "doi:10.1/exclude",
            "decision": "include",
            "review_outcome": "false_negative_risk",
            "reason_codes": ["I_SURFACE_WATER_CONCENTRATION"],
            "evidence_spans": ["Measured microplastic abundance in lake water."],
        }
    ]
    (pool_root / "records.jsonl").write_text(
        "\n".join(json.dumps(row) for row in records) + "\n",
        encoding="utf-8",
    )
    (pool_root / "screening_decisions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in decisions) + "\n",
        encoding="utf-8",
    )
    (pool_root / "audit_review_decisions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in review_decisions) + "\n",
        encoding="utf-8",
    )

    result = RunManager(repo).export_candidate_pool_review(pool_id="pool")
    export_dir = repo / "docs" / "retrieval_runs" / "exports" / "pool"
    manifest = read_json(export_dir / "audit_review_export_manifest.json")
    included_text = (export_dir / "final_included_candidates.csv").read_text(
        encoding="utf-8"
    )

    assert result["status"] == "completed"
    assert manifest["counts"]["final_include"] == 1
    assert manifest["counts"]["include_candidate_needs_review"] == 1
    assert manifest["counts"]["false_negative_risk_review"] == 1
    assert manifest["source_state"] == {
        "screening_decision_rows": 2,
        "screened_records": 2,
        "audit_review_decision_rows": 1,
        "missing_abstracts_after_enrichment": 0,
        "records_with_metadata_enrichment": 0,
    }
    assert "doi:10.1/exclude" in included_text
    assert (export_dir / "records_needing_review.csv").exists()
    assert (export_dir / "include_candidate_needs_review.csv").exists()
    assert (export_dir / "AUDIT_REVIEW_EXPORT_MANIFEST.md").exists()

    analysis = {
        "screening": {"decision_rows_total": 2, "screened_total": 2},
        "candidate_pool": {
            "missing_abstracts": 0,
            "records_with_metadata_enrichment": 0,
        },
    }
    runner = RunManager(repo)._phase11_runner()  # noqa: SLF001
    current = runner._candidate_pool_final_export_state(  # noqa: SLF001
        pool_id="pool",
        export_dir=export_dir,
        analysis=analysis,
        audit_review_rows=review_decisions,
    )
    assert current["current"] is True

    analysis["screening"]["decision_rows_total"] = 3
    stale = runner._candidate_pool_final_export_state(  # noqa: SLF001
        pool_id="pool",
        export_dir=export_dir,
        analysis=analysis,
        audit_review_rows=review_decisions,
    )
    assert stale["current"] is False
    assert stale["reason"] == "source_state_mismatch"


def test_build_candidate_pool_review_priority_ranks_include_candidates(
    tmp_path,
) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    records = [
        {
            "candidate_pool_key": "doi:10.1/include",
            "title": "PFAS occurrence in river water",
            "doi": "10.1/include",
            "abstract": (
                "Measured PFAS concentrations and occurrence in river water samples "
                "from a monitoring survey."
            ),
            "query_families": ["pfas"],
            "source_providers": ["pubmed"],
            "document_type": "journal article",
            "language": "eng",
        },
        {
            "candidate_pool_key": "doi:10.1/defer",
            "title": "PFAS in surface water",
            "doi": "10.1/defer",
            "abstract": "",
            "query_families": ["pfas"],
            "source_providers": ["crossref"],
            "document_type": "journal-article",
            "language": "",
        },
    ]
    decisions = [
        {
            "global_record_id": "doi:10.1/include",
            "decision": "include",
            "query_families": ["pfas"],
            "source_providers": ["pubmed"],
            "reason_codes": ["I_SURFACE_WATER_CONCENTRATION"],
            "evidence_spans": [
                "Measured PFAS concentrations and occurrence in river water samples."
            ],
        },
        {
            "global_record_id": "doi:10.1/defer",
            "decision": "defer_metadata",
            "query_families": ["pfas"],
            "source_providers": ["crossref"],
            "reason_codes": ["D_METADATA_MISSING"],
            "evidence_spans": ["PFAS in surface water."],
        },
    ]
    (pool_root / "records.jsonl").write_text(
        "\n".join(json.dumps(row) for row in records) + "\n",
        encoding="utf-8",
    )
    (pool_root / "screening_decisions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in decisions) + "\n",
        encoding="utf-8",
    )
    RunManager(repo).export_candidate_pool_review(pool_id="pool")

    result = RunManager(repo).build_candidate_pool_review_priority(
        pool_id="pool",
        limit=10,
    )
    export_dir = repo / "docs" / "retrieval_runs" / "exports" / "pool"
    summary = read_json(export_dir / "review_priority" / "review_priority_summary.json")
    queue_text = (
        export_dir / "review_priority" / "review_priority_queue_top10.csv"
    ).read_text(encoding="utf-8")

    assert result["status"] == "completed"
    assert summary["priority_queue_exported_top_n"] == 2
    assert summary["exported_provider_counts"]["pubmed"] == 1
    assert summary["exported_provider_counts"]["crossref"] == 1
    assert "doi:10.1/include" in queue_text
    assert "doi:10.1/defer" in queue_text
    assert "candidate_include_needs_confirmation" in queue_text
    assert (export_dir / "review_priority" / "REVIEW_PRIORITY_QUEUE.md").exists()


def test_export_candidate_pool_review_applies_audit_calibration_policy(
    tmp_path,
) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    records = [
        {
            "candidate_pool_key": "doi:10.1/defer",
            "title": "Microplastic pollution in surface water of Lake Victoria",
            "doi": "10.1/defer",
            "abstract": "",
            "query_families": ["microplastics"],
            "source_providers": ["crossref"],
            "document_type": "journal-article",
            "language": None,
        },
        {
            "candidate_pool_key": "doi:10.1/review",
            "title": "PFAS in surface water: a systematic review",
            "doi": "10.1/review",
            "abstract": "Systematic review of PFAS in surface water.",
            "query_families": ["pfas"],
            "source_providers": ["crossref"],
            "document_type": "review",
            "language": "en",
        },
        {
            "candidate_pool_key": "doi:10.1/model",
            "title": "Dynamic modeling of pesticide load in surface water",
            "doi": "10.1/model",
            "abstract": "",
            "query_families": ["pesticides"],
            "source_providers": ["crossref"],
            "document_type": "journal-article",
            "language": "en",
        },
    ]
    decisions = [
        {
            "global_record_id": "doi:10.1/defer",
            "decision": "defer_metadata",
            "query_families": ["microplastics"],
            "source_providers": ["crossref"],
            "reason_codes": ["D_METADATA_MISSING"],
            "evidence_spans": ["Microplastic pollution in surface water."],
        },
        {
            "global_record_id": "doi:10.1/review",
            "decision": "defer_metadata",
            "query_families": ["pfas"],
            "source_providers": ["crossref"],
            "reason_codes": ["D_METADATA_MISSING"],
            "evidence_spans": ["PFAS in surface water."],
        },
        {
            "global_record_id": "doi:10.1/model",
            "decision": "defer_metadata",
            "query_families": ["pesticides"],
            "source_providers": ["crossref"],
            "reason_codes": ["D_METADATA_MISSING"],
            "evidence_spans": ["Pesticide load in surface water."],
        },
    ]
    (pool_root / "records.jsonl").write_text(
        "\n".join(json.dumps(row) for row in records) + "\n",
        encoding="utf-8",
    )
    (pool_root / "screening_decisions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in decisions) + "\n",
        encoding="utf-8",
    )

    RunManager(repo).export_candidate_pool_review(pool_id="pool")
    export_dir = repo / "docs" / "retrieval_runs" / "exports" / "pool"
    manifest = read_json(export_dir / "audit_review_export_manifest.json")
    included_text = (export_dir / "final_included_candidates.csv").read_text(
        encoding="utf-8"
    )
    resolved_text = (export_dir / "resolved_exclude_or_defer.csv").read_text(
        encoding="utf-8"
    )

    assert (
        manifest["calibration_policy_version"]
        == "candidate-pool-audit-calibration-v1.0"
    )
    review_text = (export_dir / "include_candidate_needs_review.csv").read_text(
        encoding="utf-8"
    )

    assert "doi:10.1/defer" not in included_text
    assert "doi:10.1/defer" in review_text
    assert "doi:10.1/review" in resolved_text
    assert "calibrated_review_or_publication_artifact" in resolved_text
    assert "doi:10.1/model" not in included_text


def test_export_candidate_pool_review_splits_risky_includes_from_strict_final(
    tmp_path,
) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    records = [
        {
            "candidate_pool_key": "doi:10.1/strict",
            "title": "Microplastic abundance in river water",
            "doi": "10.1/strict",
            "abstract": (
                "This study measured microplastic abundance and concentrations "
                "in river water samples."
            ),
            "query_families": ["microplastics"],
            "source_providers": ["pubmed"],
            "document_type": "journal article",
            "language": "eng",
        },
        {
            "candidate_pool_key": "doi:10.1/risky",
            "title": "Organochlorine pesticide residues in fish from Osun River",
            "doi": "10.1/risky",
            "abstract": "Fish tissue residues were measured from the Osun River.",
            "query_families": ["pesticides"],
            "source_providers": ["pubmed"],
            "document_type": "journal article",
            "language": "eng",
        },
    ]
    decisions = [
        {
            "global_record_id": "doi:10.1/strict",
            "decision": "include",
            "query_families": ["microplastics"],
            "source_providers": ["pubmed"],
            "reason_codes": ["I_SURFACE_WATER_CONCENTRATION"],
            "evidence_spans": ["microplastic abundance in river water"],
        },
        {
            "global_record_id": "doi:10.1/risky",
            "decision": "include",
            "query_families": ["pesticides"],
            "source_providers": ["pubmed"],
            "reason_codes": ["I_SURFACE_WATER_CONCENTRATION"],
            "evidence_spans": ["fish from Osun River"],
        },
    ]
    (pool_root / "records.jsonl").write_text(
        "\n".join(json.dumps(row) for row in records) + "\n",
        encoding="utf-8",
    )
    (pool_root / "screening_decisions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in decisions) + "\n",
        encoding="utf-8",
    )

    RunManager(repo).export_candidate_pool_review(pool_id="pool")
    export_dir = repo / "docs" / "retrieval_runs" / "exports" / "pool"
    manifest = read_json(export_dir / "audit_review_export_manifest.json")
    included_text = (export_dir / "final_included_candidates.csv").read_text(
        encoding="utf-8"
    )
    include_review_text = (
        export_dir / "include_candidate_needs_review.csv"
    ).read_text(encoding="utf-8")
    review_text = (export_dir / "records_needing_review.csv").read_text(
        encoding="utf-8"
    )

    assert manifest["counts"]["final_include"] == 1
    assert manifest["counts"]["include_candidate_needs_review"] == 1
    assert "doi:10.1/strict" in included_text
    assert "doi:10.1/risky" not in included_text
    assert "doi:10.1/risky" in include_review_text
    assert "doi:10.1/risky" in review_text


def test_export_candidate_pool_review_sends_ecological_risk_includes_to_review(
    tmp_path,
) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    records = [
        {
            "candidate_pool_key": "doi:10.1/direct",
            "title": "PFAS occurrence in river surface water",
            "doi": "10.1/direct",
            "abstract": (
                "This field monitoring study measured PFAS concentrations in "
                "river surface water samples."
            ),
            "query_families": ["pfas"],
            "source_providers": ["pubmed"],
            "document_type": "journal article",
            "language": "eng",
        },
        {
            "candidate_pool_key": "doi:10.1/risk",
            "title": (
                "Pesticides override other stressors to drive stream "
                "macroinvertebrate diversity loss: unraveling interactive "
                "effects and ecological thresholds"
            ),
            "doi": "10.1/risk",
            "abstract": (
                "This ecological risk assessment evaluates pesticide stressors "
                "and stream macroinvertebrate effects."
            ),
            "query_families": ["pesticides"],
            "source_providers": ["pubmed"],
            "document_type": "journal article",
            "language": "eng",
        },
    ]
    decisions = [
        {
            "global_record_id": "doi:10.1/direct",
            "decision": "include",
            "query_families": ["pfas"],
            "source_providers": ["pubmed"],
            "reason_codes": ["I_SURFACE_WATER_CONCENTRATION"],
            "evidence_spans": ["measured PFAS concentrations in river surface water"],
        },
        {
            "global_record_id": "doi:10.1/risk",
            "decision": "include",
            "query_families": ["pesticides"],
            "source_providers": ["pubmed"],
            "reason_codes": ["I_SURFACE_WATER_CONCENTRATION"],
            "evidence_spans": ["stream macroinvertebrate diversity loss"],
        },
    ]
    (pool_root / "records.jsonl").write_text(
        "\n".join(json.dumps(row) for row in records) + "\n",
        encoding="utf-8",
    )
    (pool_root / "screening_decisions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in decisions) + "\n",
        encoding="utf-8",
    )

    RunManager(repo).export_candidate_pool_review(pool_id="pool")
    export_dir = repo / "docs" / "retrieval_runs" / "exports" / "pool"
    included_text = (export_dir / "final_included_candidates.csv").read_text(
        encoding="utf-8"
    )
    include_review_text = (
        export_dir / "include_candidate_needs_review.csv"
    ).read_text(encoding="utf-8")

    assert "doi:10.1/direct" in included_text
    assert "doi:10.1/risk" not in included_text
    assert "doi:10.1/risk" in include_review_text


def test_plan_query_family_construction_preserves_unique_include_and_adds_gaps(
    tmp_path,
) -> None:
    repo = prepare_repo(tmp_path)
    ensure_dir(
        repo / "configs" / "retrieval_experiments" / "qfamily_tire_wear_6ppd_surface_water"
    )
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    analysis = {
        "pool_id": "pool",
        "candidate_pool": {"deduplicated_candidate_count": 3},
        "screening": {"screened_total": 3},
        "provider_metrics": {"crossref": {"candidate_count": 2}},
        "query_family_metrics": {
            "0.1.0-qfamily-pfas-occurrence-surface-water": {
                "recommended_role": "keep_as_recall_branch_with_noise_controls",
                "unique_include_count": 1,
                "screened_count": 2,
                "decision_counts": {"include": 1, "defer_metadata": 1},
            },
            "0.1.0-qfamily-uv-filters-benzotriazoles-surface-water": {
                "recommended_role": "metadata_limited_branch",
                "unique_include_count": 0,
                "screened_count": 120,
                "decision_counts": {"include": 0, "defer_metadata": 100},
            },
        },
        "term_analysis": {
            "positive_terms_by_block": {
                "surface_water_terms": [{"value": "river", "count": 1}]
            },
            "noise_terms_by_block": {
                "exclusion_candidate_terms": [{"value": "review", "count": 2}]
            },
        },
    }
    write_json_atomic(pool_root / "candidate_pool_analysis.json", analysis)

    result = RunManager(repo).plan_query_family_construction(pool_id="pool")
    plan_path = (
        repo
        / "docs"
        / "retrieval_runs"
        / "exports"
        / "pool"
        / "next_query_family_construction_plan.json"
    )
    summary_path = plan_path.with_name("NEXT_QUERY_FAMILY_CONSTRUCTION_PLAN.md")
    plan = read_json(plan_path)

    assert result["status"] == "completed"
    assert plan["plan_version"] == "query-family-construction-v1"
    assert any(
        item["config_name"] == "qfamily_pfas_occurrence_surface_water"
        for item in plan["preserve_productive_families"]
    )
    assert any(
        item["config_name"] == "qfamily_tire_wear_6ppd_surface_water"
        for item in plan["expand_missing_or_undercovered_families"]
    )
    assert "one precision-optimized Boolean query" in plan["objective"]
    assert summary_path.exists()


def test_plan_query_family_construction_flags_low_yield_noisy_branches(
    tmp_path,
) -> None:
    repo = prepare_repo(tmp_path)
    ensure_dir(
        repo
        / "configs"
        / "retrieval_experiments"
        / "qfamily_disinfection_quat_surface_water_refined_v2"
    )
    pool_root = ensure_dir(repo / "runs" / "retrieval_pool_v3_200cap" / "candidate_pool")
    analysis = {
        "pool_id": "retrieval_pool_v3_200cap",
        "candidate_pool": {"deduplicated_candidate_count": 200},
        "screening": {
            "screened_total": 24,
            "decision_counts": {"include": 2, "exclude": 20, "defer_metadata": 2},
        },
        "provider_metrics": {"crossref": {"candidate_count": 200}},
        "query_family_metrics": {
            "0.1.1-qfamily-disinfection-quat-surface-water-refined": {
                "recommended_role": "keep_as_recall_branch_with_noise_controls",
                "unique_include_count": 0,
                "screened_count": 12,
                "decision_counts": {"include": 1, "exclude": 11, "defer_metadata": 0},
            },
            "0.1.0-qfamily-nontarget-suspect-screening-surface-water": {
                "recommended_role": "unique_recall_branch",
                "unique_include_count": 2,
                "screened_count": 12,
                "decision_counts": {"include": 3, "exclude": 7, "defer_metadata": 2},
            },
        },
        "term_analysis": {
            "positive_terms_by_block": {
                "monitoring_and_concentration_terms": [
                    {"value": "suspect screening", "count": 1}
                ]
            },
            "noise_terms_by_block": {
                "exclusion_candidate_terms": [
                    {"value": "disinfection byproduct formation", "count": 4}
                ]
            },
        },
    }
    write_json_atomic(pool_root / "candidate_pool_analysis.json", analysis)

    result = RunManager(repo).plan_query_family_construction(
        pool_id="retrieval_pool_v3_200cap"
    )
    plan = read_json(
        repo
        / "docs"
        / "retrieval_runs"
        / "exports"
        / "retrieval_pool_v3_200cap"
        / "next_query_family_construction_plan.json"
    )

    assert result["recommended_next_pool_id"] == "retrieval_pool_v4_200cap"
    noisy = plan["refine_weak_or_noisy_families"]
    assert any(
        item["config_name"] == "qfamily_disinfection_quat_surface_water_refined_v2"
        and item["reason"] == "low unique include yield with high noise burden"
        and item["candidate_refinement_focus"]
        for item in noisy
    )
    preserved = plan["preserve_productive_families"]
    assert any(
        item["config_name"] == "qfamily_nontarget_suspect_screening_surface_water"
        and item["action"] == "preserve"
        for item in preserved
    )
    assert any(
        item["config_name"] == "qfamily_disinfection_quat_surface_water_refined_v2"
        for item in plan["held_for_refinement_family_config_dirs"]
    )
    assert not any(
        path.endswith("qfamily_disinfection_quat_surface_water_refined_v2")
        for path in plan["recommended_next_family_config_dirs"]
    )


def test_plan_query_family_construction_does_not_readd_recently_held_family(
    tmp_path,
) -> None:
    repo = prepare_repo(tmp_path)
    previous_export = ensure_dir(
        repo
        / "docs"
        / "retrieval_runs"
        / "exports"
        / "hr_v18_core_pest_refined_200_20260731"
    )
    write_json_atomic(
        previous_export / "next_query_family_construction_plan.json",
        {
            "held_for_refinement_family_config_dirs": [
                {
                    "config_name": "qfamily_artificial_sweeteners_tracers_surface_water",
                    "config_dir": (
                        "configs/retrieval_experiments/"
                        "qfamily_artificial_sweeteners_tracers_surface_water"
                    ),
                    "query_family": (
                        "0.1.0-qfamily-artificial-sweeteners-tracers-surface-water"
                    ),
                    "reason": "low unique include yield with high noise burden",
                }
            ],
        },
    )
    pool_root = ensure_dir(
        repo / "runs" / "hr_v19_core_pest_refined_200_20260731" / "candidate_pool"
    )
    analysis = {
        "pool_id": "hr_v19_core_pest_refined_200_20260731",
        "candidate_pool": {"deduplicated_candidate_count": 100},
        "screening": {"screened_total": 10, "decision_counts": {"include": 2}},
        "provider_metrics": {"crossref": {"candidate_count": 100}},
        "query_family_metrics": {
            "0.1.0-qfamily-pfas-occurrence-surface-water": {
                "recommended_role": "unique_recall_branch",
                "unique_include_count": 2,
                "screened_count": 10,
                "decision_counts": {"include": 2, "exclude": 8},
            }
        },
    }
    write_json_atomic(pool_root / "candidate_pool_analysis.json", analysis)

    result = RunManager(repo).plan_query_family_construction(
        pool_id="hr_v19_core_pest_refined_200_20260731"
    )
    plan = read_json(
        repo
        / "docs"
        / "retrieval_runs"
        / "exports"
        / "hr_v19_core_pest_refined_200_20260731"
        / "next_query_family_construction_plan.json"
    )

    assert result["status"] == "completed"
    assert not any(
        item["config_name"] == "qfamily_artificial_sweeteners_tracers_surface_water"
        for item in plan["expand_missing_or_undercovered_families"]
    )
    assert not any(
        path.endswith("qfamily_artificial_sweeteners_tracers_surface_water")
        for path in plan["recommended_next_family_config_dirs"]
    )


def test_safe_defer_candidate_pool_audit_review_completes_pending_requests(
    tmp_path,
) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    result_ref = (
        "runs/pool/screening/worker_results/CANDIDATE_POOL_AUDIT/review-safe.json"
    )
    records = [
        {
            "candidate_pool_key": "doi:10.1/review-safe",
            "title": "PFAS occurrence in river surface water",
            "doi": "10.1/review-safe",
            "abstract": "",
            "query_families": ["pfas"],
            "source_providers": ["crossref"],
            "document_type": "journal-article",
        }
    ]
    decisions = [
        {
            "global_record_id": "doi:10.1/review-safe",
            "decision": "include",
            "query_families": ["pfas"],
            "source_providers": ["crossref"],
            "reason_codes": ["I_SURFACE_WATER_CONCENTRATION"],
            "evidence_spans": ["PFAS occurrence in river surface water"],
        }
    ]
    requests = [
        {
            "global_record_id": "doi:10.1/review-safe",
            "audit_category": "include_with_noise_loss_audit",
            "provisional_decision": "include",
            "result_ref": result_ref,
        }
    ]
    (pool_root / "records.jsonl").write_text(
        "\n".join(json.dumps(row) for row in records) + "\n",
        encoding="utf-8",
    )
    (pool_root / "screening_decisions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in decisions) + "\n",
        encoding="utf-8",
    )
    (pool_root / "audit_review_worker_requests.jsonl").write_text(
        "\n".join(json.dumps(row) for row in requests) + "\n",
        encoding="utf-8",
    )

    result = RunManager(repo).safe_defer_candidate_pool_audit_review(pool_id="pool")
    review_rows = list(read_jsonl(pool_root / "audit_review_decisions.jsonl"))
    written_result = read_json(repo / result_ref)

    assert result["status"] == "completed"
    assert result["safe_defer_results_created"] == 1
    assert review_rows[0]["decision"] == "defer_metadata"
    assert review_rows[0]["review_outcome"] == "include_downgraded"
    assert review_rows[0]["candidate_pool_audit_safe_defer"] is True
    assert written_result["query_id"] == "CANDIDATE_POOL_AUDIT"


def test_plan_candidate_pool_post_review_prioritizes_metadata_before_expansion(
    tmp_path,
) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    export_root = ensure_dir(repo / "docs" / "retrieval_runs" / "exports" / "pool")
    priority_root = ensure_dir(export_root / "review_priority")
    write_json_atomic(
        pool_root / "candidate_pool_analysis.json",
        {
            "candidate_pool": {
                "deduplicated_candidate_count": 10,
                "missing_abstracts": 7,
            },
            "screening": {
                "screened_total": 10,
                "decision_counts": {"include": 1, "defer_metadata": 8, "exclude": 1},
            },
            "query_family_metrics": {
                "0.1.0-qfamily-pfas-occurrence-surface-water": {
                    "candidate_count": 5,
                    "unique_include_count": 1,
                    "decision_counts": {"include": 1, "defer_metadata": 3},
                    "recommended_role": "keep_as_recall_branch_with_noise_controls",
                },
                "0.1.0-qfamily-tire-wear-6ppd-surface-water": {
                    "candidate_count": 5,
                    "unique_include_count": 0,
                    "decision_counts": {"defer_metadata": 5},
                    "recommended_role": "high_noise_or_low_yield_branch",
                },
            },
        },
    )
    write_json_atomic(
        pool_root / "audit_review_summary.json",
        {
            "reviewed_total": 3,
            "remaining_review_queue": 2,
            "review_decision_counts": {"defer_metadata": 3},
        },
    )
    write_json_atomic(
        priority_root / "review_priority_summary.json",
        {
            "priority_queue_total": 5,
            "priority_queue_exported_top_n": 5,
            "source_counts": {"include_candidate_needs_review": 2},
            "exported_top_risk_flags": {"missing_abstract": 3},
        },
    )

    result = RunManager(repo).plan_candidate_pool_post_review(pool_id="pool")
    plan = read_json(export_root / "post_review_action_plan.json")
    markdown = (export_root / "POST_REVIEW_ACTION_PLAN.md").read_text(encoding="utf-8")

    assert result["status"] == "completed"
    assert (
        plan["recommended_next_action"]
        == "metadata_enrichment_and_priority_review_before_expansion"
    )
    assert any(row["action"] == "preserve" for row in plan["family_actions"])
    assert any(
        row["action"] == "pause_until_metadata_enriched"
        for row in plan["family_actions"]
    )
    assert "missing_abstract" in markdown


def test_plan_candidate_pool_metadata_enrichment_prioritizes_missing_abstracts(
    tmp_path,
) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    export_root = ensure_dir(repo / "docs" / "retrieval_runs" / "exports" / "pool")
    priority_root = ensure_dir(export_root / "review_priority")
    records = [
        {
            "candidate_pool_key": "doi:10.1/include",
            "title": "PFAS occurrence in river water",
            "abstract": "",
            "doi": "10.1/include",
            "query_families": ["qfamily_pfas_occurrence_surface_water"],
            "source_providers": ["crossref"],
        },
        {
            "candidate_pool_key": "pmid:123",
            "title": "Micropollutants in lake water",
            "abstract": None,
            "pmid": "123",
            "query_families": ["qfamily_generic_cec_surface_water"],
            "source_providers": ["pubmed"],
        },
        {
            "candidate_pool_key": "title:has-abstract",
            "title": "Measured pesticides in rivers",
            "abstract": "Measured pesticides in rivers.",
            "query_families": ["qfamily_pesticides_surface_water"],
            "source_providers": ["crossref"],
        },
    ]
    (pool_root / "records.jsonl").write_text(
        "\n".join(json.dumps(row) for row in records) + "\n",
        encoding="utf-8",
    )
    decisions = [
        {
            "global_record_id": "doi:10.1/include",
            "decision": "include",
        },
        {
            "global_record_id": "pmid:123",
            "decision": "defer_metadata",
        },
    ]
    (pool_root / "screening_decisions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in decisions) + "\n",
        encoding="utf-8",
    )
    write_csv_atomic(
        priority_root / "review_priority_queue_top300.csv",
        [
            {
                "review_rank_score": 120,
                "global_record_id": "pmid:123",
            }
        ],
        ["review_rank_score", "global_record_id"],
    )

    result = RunManager(repo).plan_candidate_pool_metadata_enrichment(
        pool_id="pool",
        limit=10,
    )
    plan = read_json(export_root / "metadata_enrichment_plan.json")
    markdown = (export_root / "METADATA_ENRICHMENT_PLAN.md").read_text(encoding="utf-8")

    assert result["status"] == "completed"
    assert plan["summary"]["records_missing_abstract"] == 2
    assert plan["summary"]["enrichment_queue_exported"] == 2
    assert plan["enrichment_queue"][0]["candidate_pool_key"] == "doi:10.1/include"
    assert {
        row["preferred_enrichment_route"] for row in plan["enrichment_queue"]
    } == {"crossref_openalex_by_doi", "pubmed_by_pmid"}
    assert "Do not download PDFs" in markdown


def test_plan_candidate_pool_metadata_enrichment_skips_already_enriched_records(
    tmp_path,
) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    ensure_dir(pool_root / "metadata_enrichment")
    records = [
        {
            "candidate_pool_key": "doi:10.1/done",
            "title": "PFAS occurrence in river water",
            "abstract": "",
            "doi": "10.1/done",
            "query_families": ["qfamily_pfas_occurrence_surface_water"],
            "source_providers": ["crossref"],
        },
        {
            "candidate_pool_key": "doi:10.1/next",
            "title": "Microplastics in lake water",
            "abstract": "",
            "doi": "10.1/next",
            "query_families": ["qfamily_microplastics_nanoplastics_surface_water"],
            "source_providers": ["crossref"],
        },
    ]
    (pool_root / "records.jsonl").write_text(
        "\n".join(json.dumps(row) for row in records) + "\n",
        encoding="utf-8",
    )
    (pool_root / "screening_decisions.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"global_record_id": "doi:10.1/done", "decision": "include"}),
                json.dumps({"global_record_id": "doi:10.1/next", "decision": "include"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (pool_root / "metadata_enrichment" / "enriched_records.jsonl").write_text(
        json.dumps(
            {
                "candidate_pool_key": "doi:10.1/done",
                "abstract": "Measured PFAS concentrations in river water.",
                "enrichment_provider": "openalex",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    RunManager(repo).plan_candidate_pool_metadata_enrichment(pool_id="pool", limit=10)
    plan = read_json(
        repo / "docs" / "retrieval_runs" / "exports" / "pool" / "metadata_enrichment_plan.json"
    )

    assert plan["summary"]["records_missing_abstract"] == 1
    assert plan["summary"]["records_with_recovered_abstract"] == 1
    assert plan["summary"]["enrichment_queue_exported"] == 1
    assert plan["enrichment_queue"][0]["candidate_pool_key"] == "doi:10.1/next"


def test_plan_candidate_pool_metadata_enrichment_skips_attempted_records(
    tmp_path,
) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    ensure_dir(pool_root / "metadata_enrichment")
    records = [
        {
            "candidate_pool_key": "doi:10.1/attempted",
            "title": "PFAS occurrence in river water",
            "abstract": "",
            "doi": "10.1/attempted",
            "query_families": ["qfamily_pfas_occurrence_surface_water"],
            "source_providers": ["crossref"],
        },
        {
            "candidate_pool_key": "doi:10.1/next",
            "title": "Microplastics in lake water",
            "abstract": "",
            "doi": "10.1/next",
            "query_families": ["qfamily_microplastics_nanoplastics_surface_water"],
            "source_providers": ["crossref"],
        },
    ]
    (pool_root / "records.jsonl").write_text(
        "\n".join(json.dumps(row) for row in records) + "\n",
        encoding="utf-8",
    )
    (pool_root / "screening_decisions.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"global_record_id": "doi:10.1/attempted", "decision": "include"}),
                json.dumps({"global_record_id": "doi:10.1/next", "decision": "include"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (pool_root / "metadata_enrichment" / "enrichment_attempts.jsonl").write_text(
        json.dumps(
            {
                "candidate_pool_key": "10.1/attempted",
                "providers": ["crossref", "openalex"],
                "output_ref": "runs/pool/candidate_pool/metadata_enrichment/result.json",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    RunManager(repo).plan_candidate_pool_metadata_enrichment(pool_id="pool", limit=10)
    plan = read_json(
        repo / "docs" / "retrieval_runs" / "exports" / "pool" / "metadata_enrichment_plan.json"
    )

    assert plan["summary"]["records_missing_abstract"] == 2
    assert plan["summary"]["enrichment_terminal_attempts"] == 1
    assert plan["summary"]["metadata_lookup_outcome_complete"] is False
    assert plan["summary"]["enrichment_queue_exported"] == 1
    assert plan["enrichment_queue"][0]["candidate_pool_key"] == "doi:10.1/next"


def test_plan_candidate_pool_metadata_enrichment_accepts_explicit_review_only_outcome(
    tmp_path,
) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    record = {
        "candidate_pool_key": "title:unstable-record",
        "title": "A record without a stable identifier",
        "abstract": "",
        "query_families": ["generic"],
        "source_providers": ["crossref"],
    }
    (pool_root / "records.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )
    (pool_root / "screening_decisions.jsonl").write_text("", encoding="utf-8")

    RunManager(repo).plan_candidate_pool_metadata_enrichment(pool_id="pool", limit=10)
    plan = read_json(
        repo / "docs" / "retrieval_runs" / "exports" / "pool" / "metadata_enrichment_plan.json"
    )

    assert plan["summary"]["review_only_missing_abstract"] == 1
    assert plan["summary"]["enrichment_queue_total"] == 0
    assert plan["summary"]["metadata_outcomes_accounted"] == 1
    assert plan["summary"]["metadata_lookup_outcome_complete"] is True


def test_enrich_candidate_pool_metadata_uses_external_skill_boundary(
    tmp_path,
    monkeypatch,
) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    records = [
        {
            "candidate_pool_key": "doi:10.1/include",
            "title": "PFAS occurrence in river water",
            "abstract": "",
            "doi": "10.1/include",
            "query_families": ["qfamily_pfas_occurrence_surface_water"],
            "source_providers": ["crossref"],
        }
    ]
    (pool_root / "records.jsonl").write_text(
        "\n".join(json.dumps(row) for row in records) + "\n",
        encoding="utf-8",
    )
    (pool_root / "screening_decisions.jsonl").write_text(
        json.dumps({"global_record_id": "doi:10.1/include", "decision": "include"}) + "\n",
        encoding="utf-8",
    )
    RunManager(repo).plan_candidate_pool_metadata_enrichment(pool_id="pool", limit=10)

    captured: dict[str, object] = {}

    def fake_invoke_metadata_enrichment(self, **kwargs):
        del self
        captured.update(kwargs)
        output_root = ensure_dir(kwargs["output_root"])
        candidates_ref = output_root / "candidates.jsonl"
        candidates_ref.write_text(
            json.dumps(
                {
                    "candidate_pool_key": "doi:10.1/include",
                    "source_provider": "crossref",
                    "source_record_id": "10.1/include",
                    "doi": "10.1/include",
                    "title": "PFAS occurrence in river water",
                    "abstract": "Measured PFAS concentrations in river water.",
                    "document_type": "journal-article",
                    "language": "en",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        source_status_ref = output_root / "source_status.json"
        provider_states_ref = output_root / "provider_states.json"
        output_ref = output_root / "external_metadata_discovery_output.json"
        source_status_ref.write_text(json.dumps({"crossref": "success"}), encoding="utf-8")
        provider_states_ref.write_text(json.dumps({"crossref": {}}), encoding="utf-8")
        output_ref.write_text(json.dumps({"status": "success"}), encoding="utf-8")
        return {
            "candidates_ref": str(candidates_ref),
            "input_ref": str(output_root / "external_metadata_discovery_input.json"),
            "output_ref": str(output_ref),
            "source_status_ref": str(source_status_ref),
            "provider_states_ref": str(provider_states_ref),
        }

    monkeypatch.setattr(
        "ecmonitor.retrieval_specialist.orchestration.phase11_runner."
        "ExternalMetadataDiscoveryGateway.invoke_metadata_enrichment",
        fake_invoke_metadata_enrichment,
    )

    result = RunManager(repo).enrich_candidate_pool_metadata(pool_id="pool", limit=1)
    enriched = list(
        read_jsonl(
            repo
            / "runs"
            / "pool"
            / "candidate_pool"
            / "metadata_enrichment"
            / "enriched_records.jsonl"
        )
    )

    assert result["status"] == "completed"
    assert result["requested_records"] == 1
    assert result["enriched_records"] == 1
    assert result["records_with_abstract"] == 1
    assert captured["providers"] == ["openalex", "semantic_scholar", "pubmed"]
    assert str(captured["query_id"]).startswith("CANDIDATE_POOL_METADATA_ENRICHMENT_")
    assert enriched[0]["abstract"] == "Measured PFAS concentrations in river water."


def test_enrich_candidate_pool_metadata_refreshes_plan_and_batches_records(
    tmp_path,
    monkeypatch,
) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    enrichment_root = ensure_dir(pool_root / "metadata_enrichment")
    records = [
        {
            "candidate_pool_key": f"doi:10.1/{index}",
            "title": f"Field record {index}",
            "abstract": "",
            "doi": f"10.1/{index}",
            "query_families": ["generic"],
            "source_providers": ["crossref"],
        }
        for index in range(3)
    ]
    (pool_root / "records.jsonl").write_text(
        "\n".join(json.dumps(row) for row in records) + "\n",
        encoding="utf-8",
    )
    RunManager(repo).plan_candidate_pool_metadata_enrichment(pool_id="pool", limit=3)
    (enrichment_root / "enrichment_attempts.jsonl").write_text(
        json.dumps(
            {
                "candidate_pool_key": "doi:10.1/0",
                "status": "completed_no_abstract",
                "providers": ["openalex", "semantic_scholar"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    calls: list[dict[str, object]] = []

    def fake_invoke(self, **kwargs):  # noqa: ANN001
        del self
        calls.append(kwargs)
        output_root = ensure_dir(kwargs["output_root"])
        candidate_rows = [
            {
                "candidate_pool_key": row["candidate_pool_key"],
                "source_provider": "openalex",
                "source_record_id": f"W{index}",
                "doi": row["doi"],
                "title": row["title"],
                "abstract": f"Recovered abstract {index}",
            }
            for index, row in enumerate(kwargs["records"], start=1)
        ]
        candidates_ref = output_root / "candidates.jsonl"
        candidates_ref.write_text(
            "\n".join(json.dumps(row) for row in candidate_rows) + "\n",
            encoding="utf-8",
        )
        for name in ["source_status.json", "provider_states.json"]:
            (output_root / name).write_text("{}", encoding="utf-8")
        return {
            "candidates_ref": str(candidates_ref),
            "input_ref": str(output_root / "external_metadata_discovery_input.json"),
            "output_ref": str(output_root / "external_metadata_discovery_output.json"),
            "source_status_ref": str(output_root / "source_status.json"),
            "provider_states_ref": str(output_root / "provider_states.json"),
            "reused_completed_output": False,
        }

    monkeypatch.setattr(
        "ecmonitor.retrieval_specialist.orchestration.phase11_runner."
        "ExternalMetadataDiscoveryGateway.invoke_metadata_enrichment",
        fake_invoke,
    )

    result = RunManager(repo).enrich_candidate_pool_metadata(
        pool_id="pool",
        limit=3,
        batch_size=10,
    )

    assert result["requested_records"] == 2
    assert result["batch_count"] == 1
    assert result["enriched_records"] == 2
    assert len(calls) == 1
    assert len(calls[0]["records"]) == 2  # type: ignore[arg-type]
    assert calls[0]["providers"] == ["openalex", "semantic_scholar", "pubmed"]
    assert result["remaining_enrichment_queue"] == 0


def test_metadata_enrichment_retries_only_incomplete_provider(
    tmp_path,
    monkeypatch,
) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    record = {
        "candidate_pool_key": "doi:10.1/retry",
        "title": "PFAS occurrence in river water",
        "abstract": "",
        "doi": "10.1/retry",
        "query_families": ["pfas"],
        "source_providers": ["crossref"],
    }
    (pool_root / "records.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )
    calls: list[list[str]] = []

    def fake_invoke(self, **kwargs):  # noqa: ANN001
        del self
        providers = list(kwargs["providers"])
        calls.append(providers)
        output_root = ensure_dir(kwargs["output_root"])
        candidates_ref = output_root / "candidates.jsonl"
        source_status_ref = output_root / "source_status.json"
        provider_states_ref = output_root / "provider_states.json"
        if len(calls) == 1:
            candidates_ref.write_text("", encoding="utf-8")
            statuses = {
                "openalex": "success",
                "semantic_scholar": "rate-limited",
                "pubmed": "no-results",
            }
        else:
            candidates_ref.write_text(
                json.dumps(
                    {
                        **record,
                        "source_provider": "semantic_scholar",
                        "source_record_id": "S2-retry",
                        "abstract": "Measured PFAS concentrations in river water.",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            statuses = {"semantic_scholar": "success"}
        write_json_atomic(source_status_ref, statuses)
        write_json_atomic(provider_states_ref, {})
        return {
            "candidates_ref": str(candidates_ref),
            "input_ref": str(output_root / "external_metadata_discovery_input.json"),
            "output_ref": str(output_root / "external_metadata_discovery_output.json"),
            "source_status_ref": str(source_status_ref),
            "provider_states_ref": str(provider_states_ref),
            "reused_completed_output": False,
        }

    monkeypatch.setattr(
        "ecmonitor.retrieval_specialist.orchestration.phase11_runner."
        "ExternalMetadataDiscoveryGateway.invoke_metadata_enrichment",
        fake_invoke,
    )

    first = RunManager(repo).enrich_candidate_pool_metadata(pool_id="pool", limit=1)
    second = RunManager(repo).enrich_candidate_pool_metadata(pool_id="pool", limit=1)
    attempts = list(
        read_jsonl(
            pool_root / "metadata_enrichment" / "enrichment_attempts.jsonl"
        )
    )

    assert first["status"] == "partial"
    assert first["retryable_records"] == 1
    assert first["remaining_enrichment_queue"] == 1
    assert second["status"] == "completed"
    assert second["records_with_abstract"] == 1
    assert second["remaining_enrichment_queue"] == 0
    assert calls == [
        ["openalex", "semantic_scholar", "pubmed"],
        ["semantic_scholar"],
    ]
    assert attempts[0]["status"] == "retryable_partial"
    assert attempts[0]["provider_statuses"]["semantic_scholar"] == "rate-limited"
    assert attempts[-1]["status"] == "completed_with_abstract"


def test_metadata_enrichment_stops_after_bounded_provider_retries(
    tmp_path,
    monkeypatch,
) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    record = {
        "candidate_pool_key": "doi:10.1/exhaust",
        "title": "PFAS occurrence in river water",
        "abstract": "",
        "doi": "10.1/exhaust",
        "query_families": ["pfas"],
        "source_providers": ["crossref"],
    }
    (pool_root / "records.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )
    calls: list[list[str]] = []

    def fake_invoke(self, **kwargs):  # noqa: ANN001
        del self
        providers = list(kwargs["providers"])
        calls.append(providers)
        output_root = ensure_dir(kwargs["output_root"])
        candidates_ref = output_root / "candidates.jsonl"
        candidates_ref.write_text("", encoding="utf-8")
        statuses = dict.fromkeys(providers, "rate-limited")
        if "openalex" in statuses:
            statuses["openalex"] = "success"
        source_status_ref = output_root / "source_status.json"
        provider_states_ref = output_root / "provider_states.json"
        write_json_atomic(source_status_ref, statuses)
        write_json_atomic(provider_states_ref, {})
        return {
            "candidates_ref": str(candidates_ref),
            "input_ref": str(output_root / "external_metadata_discovery_input.json"),
            "output_ref": str(output_root / "external_metadata_discovery_output.json"),
            "source_status_ref": str(source_status_ref),
            "provider_states_ref": str(provider_states_ref),
            "reused_completed_output": False,
        }

    monkeypatch.setattr(
        "ecmonitor.retrieval_specialist.orchestration.phase11_runner."
        "ExternalMetadataDiscoveryGateway.invoke_metadata_enrichment",
        fake_invoke,
    )

    first = RunManager(repo).enrich_candidate_pool_metadata(pool_id="pool", limit=1)
    second = RunManager(repo).enrich_candidate_pool_metadata(pool_id="pool", limit=1)
    third = RunManager(repo).enrich_candidate_pool_metadata(pool_id="pool", limit=1)
    fourth = RunManager(repo).enrich_candidate_pool_metadata(pool_id="pool", limit=1)
    attempts = list(
        read_jsonl(
            pool_root / "metadata_enrichment" / "enrichment_attempts.jsonl"
        )
    )

    assert first["remaining_enrichment_queue"] == 1
    assert second["remaining_enrichment_queue"] == 1
    assert third["retry_exhausted_records"] == 1
    assert third["remaining_enrichment_queue"] == 0
    assert third["metadata_lookup_outcome_complete"] is True
    assert fourth["requested_records"] == 0
    assert calls == [
        ["openalex", "semantic_scholar", "pubmed"],
        ["semantic_scholar", "pubmed"],
        ["semantic_scholar", "pubmed"],
    ]
    assert attempts[-1]["status"] == "completed_retry_exhausted"


def test_candidate_pool_analysis_uses_latest_decision_per_document(tmp_path) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    record = {
        "candidate_pool_key": "doi:10.1/latest",
        "title": "PFAS occurrence in river water",
        "abstract": "Measured PFAS concentrations in river water.",
        "doi": "10.1/latest",
        "query_families": ["generic"],
        "source_providers": ["crossref"],
    }
    (pool_root / "records.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )
    (pool_root / "screening_decisions.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {"global_record_id": "doi:10.1/latest", "decision": "exclude"}
                ),
                json.dumps(
                    {"global_record_id": "doi:10.1/latest", "decision": "include"}
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    write_json_atomic(
        pool_root / "manifest.json",
        {
            "run_id": "pool",
            "run_status": "completed",
            "query_family_counts": {"generic": 1},
        },
    )

    RunManager(repo).analyze_candidate_pool(pool_id="pool")
    analysis = read_json(pool_root / "candidate_pool_analysis.json")

    assert analysis["screening"]["screened_total"] == 1
    assert analysis["screening"]["decision_rows_total"] == 2
    assert analysis["screening"]["superseded_decision_rows"] == 1
    assert analysis["screening"]["decision_counts"] == {"include": 1}
    assert analysis["screening"]["unscreened_total"] == 0
    assert analysis["completion_assessment"]["status"] == "not_ready"


def test_candidate_pool_next_action_follows_acceptance_gate_order(tmp_path) -> None:
    repo = prepare_repo(tmp_path)
    runner = RunManager(repo)._phase11_runner()  # noqa: SLF001
    gates = {
        "pool_build_complete": True,
        "metadata_lookup_complete": False,
        "screening_complete": False,
        "exclude_audit_complete": False,
        "false_negative_audit_clear": False,
        "query_family_saturation_complete": True,
        "final_export_present": True,
        "final_export_current": True,
    }

    assert runner._candidate_pool_acceptance_next_action(  # noqa: SLF001
        {"gates": gates}
    ) == "exhaust_identifier_backed_metadata_lookup_queue"

    gates.update(
        {
            "metadata_lookup_complete": True,
            "screening_complete": True,
            "exclude_audit_complete": True,
            "false_negative_audit_clear": True,
        }
    )
    assert runner._candidate_pool_acceptance_next_action(  # noqa: SLF001
        {"gates": gates}
    ) == "candidate_pool_acceptance_complete"

    gates["final_export_current"] = False
    assert runner._candidate_pool_acceptance_next_action(  # noqa: SLF001
        {"gates": gates}
    ) == "refresh_final_audited_candidate_export"


def test_large_control_plane_uses_reference_instead_of_per_run_copy(
    tmp_path,
    monkeypatch,
) -> None:
    repo = prepare_repo(tmp_path)
    manager = RunManager(repo)
    manager.db_migrate()
    runner = manager._phase11_runner()  # noqa: SLF001
    run_dir = ensure_dir(repo / "runs" / "pool")
    monkeypatch.setenv("ECMONITOR_RUN_DB_SNAPSHOT_MAX_BYTES", "1")

    runner._snapshot_run_db(run_dir)  # noqa: SLF001

    state_dir = run_dir / "state"
    reference = read_json(state_dir / "control_plane_reference.json")
    assert reference["snapshot_status"] == "shared_control_plane_referenced"
    assert reference["run_id"] == "pool"
    assert not (state_dir / "control.sqlite3").exists()


def test_rescreen_enriched_candidate_pool_metadata_appends_audited_decision(
    tmp_path,
    monkeypatch,
) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    ensure_dir(pool_root / "metadata_enrichment")
    record = {
        "candidate_pool_key": "doi:10.1/include",
        "title": "PFAS occurrence in river water",
        "abstract": "",
        "doi": "10.1/include",
        "query_families": ["qfamily_pfas_occurrence_surface_water"],
        "source_providers": ["crossref"],
    }
    enriched = {
        **record,
        "abstract": "Measured PFAS concentrations in river water.",
        "enrichment_provider": "openalex",
        "enrichment_source_record_id": "https://openalex.org/W1",
        "document_type": "article",
        "language": "en",
    }
    (pool_root / "records.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    (pool_root / "manifest.json").write_text(json.dumps({"run_id": "pool"}), encoding="utf-8")
    (pool_root / "screening_decisions.jsonl").write_text(
        json.dumps(
            {
                "global_record_id": "doi:10.1/include",
                "decision": "defer_metadata",
                "reason_codes": ["D_METADATA_MISSING"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (pool_root / "metadata_enrichment" / "enriched_records.jsonl").write_text(
        json.dumps(enriched) + "\n",
        encoding="utf-8",
    )
    seen_abstracts: list[str | None] = []

    def fake_screen_one(self, normalized_record, **kwargs):  # noqa: ANN001
        del self
        seen_abstracts.append(normalized_record.abstract_original)
        return ScreeningDecision(
            screening_decision_id="screen_enriched",
            global_record_id=normalized_record.global_record_id,
            run_id="pool",
            query_id=kwargs["query_id"],
            iteration=0,
            decision="include",
            confidence=0.95,
            article_type_ok=True,
            date_ok=True,
            emerging_contaminant_context=True,
            surface_water_sample=True,
            field_environmental_samples=True,
            concentration_evidence="explicit_quantified",
            study_type="field monitoring",
            reason_codes=["I_SURFACE_WATER_CONCENTRATION"],
            evidence_spans=[normalized_record.abstract_original or ""],
            article_ec_scope="direct_surface_water_occurrence",
        )

    monkeypatch.setattr(
        "ecmonitor.retrieval_specialist.operators.gpt_screening."
        "TitleAbstractScreeningWorkerExecutor.screen_one",
        fake_screen_one,
    )

    result = RunManager(repo).rescreen_enriched_candidate_pool_metadata(pool_id="pool")
    rescreened = list(
        read_jsonl(
            pool_root / "metadata_enrichment" / "rescreening_decisions.jsonl"
        )
    )
    all_decisions = list(read_jsonl(pool_root / "screening_decisions.jsonl"))

    assert result["status"] == "completed"
    assert result["rescreened_decisions"] == 1
    assert result["decision_changed_count"] == 1
    assert result["decision_counts"] == {"include": 1}
    assert seen_abstracts == ["Measured PFAS concentrations in river water."]
    assert rescreened[0]["metadata_enrichment_rescreen"] is True
    assert rescreened[0]["previous_decision"] == "defer_metadata"
    assert all_decisions[-1]["decision"] == "include"


def test_export_candidate_pool_review_calibrates_reviewed_method_includes(
    tmp_path,
) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    records = [
        {
            "candidate_pool_key": "doi:10.1/sensor",
            "title": "Fluorescent sensor for PFAS detection in river water",
            "doi": "10.1/sensor",
            "abstract": (
                "A laboratory probe was developed for PFAS detection in river "
                "water samples."
            ),
            "query_families": ["pfas"],
            "source_providers": ["pubmed"],
            "document_type": "journal article",
            "language": "eng",
        }
    ]
    decisions = [
        {
            "global_record_id": "doi:10.1/sensor",
            "decision": "include",
            "query_families": ["pfas"],
            "source_providers": ["pubmed"],
            "reason_codes": ["I_SURFACE_WATER_CONCENTRATION"],
            "evidence_spans": ["PFAS detection in river water."],
        }
    ]
    review_decisions = [
        {
            "global_record_id": "doi:10.1/sensor",
            "decision": "include",
            "review_outcome": "confirmed",
            "reason_codes": ["I_SURFACE_WATER_CONCENTRATION"],
            "evidence_spans": ["A laboratory probe was developed."],
        }
    ]
    (pool_root / "records.jsonl").write_text(
        "\n".join(json.dumps(row) for row in records) + "\n",
        encoding="utf-8",
    )
    (pool_root / "screening_decisions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in decisions) + "\n",
        encoding="utf-8",
    )
    (pool_root / "audit_review_decisions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in review_decisions) + "\n",
        encoding="utf-8",
    )

    RunManager(repo).export_candidate_pool_review(pool_id="pool")
    export_dir = repo / "docs" / "retrieval_runs" / "exports" / "pool"
    included_text = (export_dir / "final_included_candidates.csv").read_text(
        encoding="utf-8"
    )
    review_text = (export_dir / "records_needing_review.csv").read_text(
        encoding="utf-8"
    )
    resolved_text = (export_dir / "resolved_exclude_or_defer.csv").read_text(
        encoding="utf-8"
    )

    assert "doi:10.1/sensor" not in included_text
    assert "doi:10.1/sensor" not in review_text
    assert "calibrated_review_include_method_or_toxicity_guard" in resolved_text


def test_export_candidate_pool_review_keeps_field_occurrence_method_mentions(
    tmp_path,
) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    records = [
        {
            "candidate_pool_key": "doi:10.1/field-method",
            "title": (
                "Target and suspect screening of pharmaceuticals in the Klip "
                "River, South Africa"
            ),
            "doi": "10.1/field-method",
            "abstract": (
                "This field study measured the occurrence and concentrations of "
                "pharmaceuticals in river surface water samples using ultra-high "
                "performance liquid chromatography-mass spectrometry."
            ),
            "query_families": ["pharmaceuticals"],
            "source_providers": ["pubmed"],
            "document_type": "journal article",
            "language": "eng",
        }
    ]
    decisions = [
        {
            "global_record_id": "doi:10.1/field-method",
            "decision": "include",
            "query_families": ["pharmaceuticals"],
            "source_providers": ["pubmed"],
            "reason_codes": ["I_SURFACE_WATER_CONCENTRATION"],
            "evidence_spans": ["measured occurrence in river surface water"],
        }
    ]
    review_decisions = [
        {
            "global_record_id": "doi:10.1/field-method",
            "decision": "include",
            "review_outcome": "confirmed",
            "reason_codes": ["I_SURFACE_WATER_CONCENTRATION"],
            "evidence_spans": ["occurrence and concentrations in river surface water"],
        }
    ]
    (pool_root / "records.jsonl").write_text(
        "\n".join(json.dumps(row) for row in records) + "\n",
        encoding="utf-8",
    )
    (pool_root / "screening_decisions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in decisions) + "\n",
        encoding="utf-8",
    )
    (pool_root / "audit_review_decisions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in review_decisions) + "\n",
        encoding="utf-8",
    )

    RunManager(repo).export_candidate_pool_review(pool_id="pool")
    export_dir = repo / "docs" / "retrieval_runs" / "exports" / "pool"
    included_text = (export_dir / "final_included_candidates.csv").read_text(
        encoding="utf-8"
    )
    review_text = (export_dir / "records_needing_review.csv").read_text(
        encoding="utf-8"
    )

    assert "doi:10.1/field-method" in included_text
    assert "calibrated_review_include_method_or_toxicity_guard" not in review_text


def test_export_candidate_pool_review_calibrates_concentration_method_includes(
    tmp_path,
) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    records = [
        {
            "candidate_pool_key": "doi:10.1/quickconc",
            "title": "QuickConc: A rapid eDNA concentration method",
            "doi": "10.1/quickconc",
            "abstract": (
                "This study presents a power-free concentration method with "
                "cationic-assisted capture for environmental DNA samples."
            ),
            "query_families": ["qac"],
            "source_providers": ["pubmed"],
            "document_type": "journal article",
            "language": "eng",
        }
    ]
    decisions = [
        {
            "global_record_id": "doi:10.1/quickconc",
            "decision": "include",
            "query_families": ["qac"],
            "source_providers": ["pubmed"],
            "reason_codes": ["I_SURFACE_WATER_CONCENTRATION"],
            "evidence_spans": ["concentration method"],
        }
    ]
    review_decisions = [
        {
            "global_record_id": "doi:10.1/quickconc",
            "decision": "include",
            "review_outcome": "confirmed",
            "reason_codes": ["I_SURFACE_WATER_CONCENTRATION"],
            "evidence_spans": ["concentration method"],
        }
    ]
    (pool_root / "records.jsonl").write_text(
        "\n".join(json.dumps(row) for row in records) + "\n",
        encoding="utf-8",
    )
    (pool_root / "screening_decisions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in decisions) + "\n",
        encoding="utf-8",
    )
    (pool_root / "audit_review_decisions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in review_decisions) + "\n",
        encoding="utf-8",
    )

    RunManager(repo).export_candidate_pool_review(pool_id="pool")
    export_dir = repo / "docs" / "retrieval_runs" / "exports" / "pool"
    included_text = (export_dir / "final_included_candidates.csv").read_text(
        encoding="utf-8"
    )
    review_text = (export_dir / "records_needing_review.csv").read_text(
        encoding="utf-8"
    )

    assert "doi:10.1/quickconc" not in included_text
    assert "calibrated_review_include_method_or_toxicity_guard" not in included_text
    assert "doi:10.1/quickconc" not in review_text


def test_export_candidate_pool_review_resolves_hard_method_noise(
    tmp_path,
) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    records = [
        {
            "candidate_pool_key": "doi:10.1/sensor",
            "title": "Electrochemical sensor for antibiotic detection in river water",
            "doi": "10.1/sensor",
            "abstract": (
                "A laboratory electrode platform was developed for detection in "
                "river water samples."
            ),
            "query_families": ["pharmaceuticals"],
            "source_providers": ["crossref"],
            "document_type": "journal-article",
            "language": "en",
        },
        {
            "candidate_pool_key": "doi:10.1/clinical",
            "title": "Lateral flow assay to measure thyroid hormones in human serum",
            "doi": "10.1/clinical",
            "abstract": "Clinical serum samples were tested with an assay platform.",
            "query_families": ["pharmaceuticals"],
            "source_providers": ["pubmed"],
            "document_type": "journal article",
            "language": "eng",
        },
        {
            "candidate_pool_key": "doi:10.1/wastewater",
            "title": "Microextraction of endocrine disruptors in wastewater",
            "doi": "10.1/wastewater",
            "abstract": "The method was developed for endocrine disruptors in wastewater samples.",
            "query_families": ["endocrine"],
            "source_providers": ["pubmed"],
            "document_type": "journal article",
            "language": "eng",
        },
        {
            "candidate_pool_key": "doi:10.1/method-defer",
            "title": "Rapid determination of an emerging contaminant in surface water",
            "doi": "10.1/method-defer",
            "abstract": "",
            "query_families": ["pharmaceuticals"],
            "source_providers": ["crossref"],
            "document_type": "journal-article",
            "language": None,
        },
    ]
    decisions = [
        {
            "global_record_id": row["candidate_pool_key"],
            "decision": "include",
            "query_families": row["query_families"],
            "source_providers": row["source_providers"],
            "reason_codes": ["I_SURFACE_WATER_CONCENTRATION"],
            "evidence_spans": [str(row["title"])],
        }
        for row in records
    ]
    review_decisions = [
        {
            "global_record_id": row["candidate_pool_key"],
            "decision": "include",
            "review_outcome": "confirmed",
            "reason_codes": ["I_SURFACE_WATER_CONCENTRATION"],
            "evidence_spans": [str(row["title"])],
        }
        for row in records
    ]
    (pool_root / "records.jsonl").write_text(
        "\n".join(json.dumps(row) for row in records) + "\n",
        encoding="utf-8",
    )
    (pool_root / "screening_decisions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in decisions) + "\n",
        encoding="utf-8",
    )
    (pool_root / "audit_review_decisions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in review_decisions) + "\n",
        encoding="utf-8",
    )

    RunManager(repo).export_candidate_pool_review(pool_id="pool")
    export_dir = repo / "docs" / "retrieval_runs" / "exports" / "pool"
    included_text = (export_dir / "final_included_candidates.csv").read_text(
        encoding="utf-8"
    )
    review_text = (export_dir / "records_needing_review.csv").read_text(
        encoding="utf-8"
    )
    resolved_text = (export_dir / "resolved_exclude_or_defer.csv").read_text(
        encoding="utf-8"
    )

    assert "doi:10.1/sensor" not in included_text
    assert "doi:10.1/clinical" not in included_text
    assert "doi:10.1/wastewater" not in included_text
    assert "doi:10.1/sensor" in resolved_text
    assert "doi:10.1/clinical" in resolved_text
    assert "doi:10.1/wastewater" in resolved_text
    assert "doi:10.1/method-defer" in review_text


def test_export_candidate_pool_review_keeps_method_with_environmental_detection(
    tmp_path,
) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    records = [
        {
            "candidate_pool_key": "doi:10.1/field-detection",
            "title": (
                "Determination of pharmaceuticals and personal care products in "
                "water by LC-MS/MS"
            ),
            "doi": "10.1/field-detection",
            "abstract": (
                "The validated method was applied to raw water, treated water, "
                "and river water samples from Hangzhou, detecting 47 compounds "
                "at concentrations ranging from nondetected to 359 ng/L."
            ),
            "query_families": ["pharmaceuticals"],
            "source_providers": ["pubmed"],
            "document_type": "journal article",
            "language": "eng",
        }
    ]
    decisions = [
        {
            "global_record_id": "doi:10.1/field-detection",
            "decision": "include",
            "query_families": ["pharmaceuticals"],
            "source_providers": ["pubmed"],
            "reason_codes": ["I_SURFACE_WATER_CONCENTRATION"],
            "evidence_spans": ["river water samples, detecting 47 compounds"],
        }
    ]
    review_decisions = [
        {
            "global_record_id": "doi:10.1/field-detection",
            "decision": "include",
            "review_outcome": "confirmed",
            "reason_codes": ["I_SURFACE_WATER_CONCENTRATION"],
            "evidence_spans": ["river water samples from Hangzhou, detecting 47 compounds"],
        }
    ]
    (pool_root / "records.jsonl").write_text(
        "\n".join(json.dumps(row) for row in records) + "\n",
        encoding="utf-8",
    )
    (pool_root / "screening_decisions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in decisions) + "\n",
        encoding="utf-8",
    )
    (pool_root / "audit_review_decisions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in review_decisions) + "\n",
        encoding="utf-8",
    )

    RunManager(repo).export_candidate_pool_review(pool_id="pool")
    export_dir = repo / "docs" / "retrieval_runs" / "exports" / "pool"
    included_text = (export_dir / "final_included_candidates.csv").read_text(
        encoding="utf-8"
    )
    review_text = (export_dir / "records_needing_review.csv").read_text(
        encoding="utf-8"
    )

    assert "doi:10.1/field-detection" in included_text
    assert "doi:10.1/field-detection" not in review_text


def test_export_candidate_pool_review_does_not_queue_resolved_missing_signal_excludes(
    tmp_path,
) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    records = [
        {
            "candidate_pool_key": "doi:10.1/synthesis",
            "title": "Synthesis of a macrolide antibiotic intermediate",
            "doi": "10.1/synthesis",
            "abstract": "A laboratory synthesis route for a macrolide is reported.",
            "query_families": ["macrolide"],
            "source_providers": ["crossref"],
            "document_type": "journal-article",
            "language": "en",
        },
        {
            "candidate_pool_key": "doi:10.1/surface-defer",
            "title": "PFAS occurrence in river water",
            "doi": "10.1/surface-defer",
            "abstract": "",
            "query_families": ["pfas"],
            "source_providers": ["crossref"],
            "document_type": "journal-article",
            "language": "en",
        },
    ]
    decisions = [
        {
            "global_record_id": "doi:10.1/synthesis",
            "decision": "exclude",
            "query_families": ["macrolide"],
            "source_providers": ["crossref"],
            "reason_codes": ["X_TOPIC_OR_MATRIX_MISMATCH"],
            "evidence_spans": ["laboratory synthesis"],
        },
        {
            "global_record_id": "doi:10.1/surface-defer",
            "decision": "defer_metadata",
            "query_families": ["pfas"],
            "source_providers": ["crossref"],
            "reason_codes": ["D_METADATA_MISSING"],
            "evidence_spans": ["PFAS occurrence in river water"],
        },
    ]
    (pool_root / "records.jsonl").write_text(
        "\n".join(json.dumps(row) for row in records) + "\n",
        encoding="utf-8",
    )
    (pool_root / "screening_decisions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in decisions) + "\n",
        encoding="utf-8",
    )

    RunManager(repo).export_candidate_pool_review(pool_id="pool")
    export_dir = repo / "docs" / "retrieval_runs" / "exports" / "pool"
    review_text = (export_dir / "records_needing_review.csv").read_text(
        encoding="utf-8"
    )
    resolved_text = (export_dir / "resolved_exclude_or_defer.csv").read_text(
        encoding="utf-8"
    )

    assert "doi:10.1/synthesis" not in review_text
    assert "doi:10.1/synthesis" in resolved_text
    assert "doi:10.1/surface-defer" in review_text


def test_export_candidate_pool_review_resolves_audit_calibrated_method_defers(
    tmp_path,
) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    records = [
        {
            "candidate_pool_key": "doi:10.1/model",
            "title": "Contaminant fate transport modeling in distribution systems",
            "doi": "10.1/model",
            "abstract": "A water quality model was developed for distribution systems.",
            "query_families": ["generic"],
            "source_providers": ["crossref"],
            "document_type": "journal-article",
            "language": "en",
        },
        {
            "candidate_pool_key": "doi:10.1/metadata",
            "title": "PFAS occurrence in river water",
            "doi": "10.1/metadata",
            "abstract": "",
            "query_families": ["pfas"],
            "source_providers": ["crossref"],
            "document_type": "journal-article",
            "language": "en",
        },
    ]
    decisions = [
        {
            "global_record_id": "doi:10.1/model",
            "decision": "exclude",
            "query_families": ["generic"],
            "source_providers": ["crossref"],
            "reason_codes": ["E_NO_FIELD_SAMPLE"],
            "evidence_spans": ["water quality model"],
        },
        {
            "global_record_id": "doi:10.1/metadata",
            "decision": "defer_metadata",
            "query_families": ["pfas"],
            "source_providers": ["crossref"],
            "reason_codes": ["D_METADATA_MISSING"],
            "evidence_spans": ["PFAS occurrence in river water"],
        },
    ]
    (pool_root / "records.jsonl").write_text(
        "\n".join(json.dumps(row) for row in records) + "\n",
        encoding="utf-8",
    )
    (pool_root / "screening_decisions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in decisions) + "\n",
        encoding="utf-8",
    )

    RunManager(repo).export_candidate_pool_review(pool_id="pool")
    export_dir = repo / "docs" / "retrieval_runs" / "exports" / "pool"
    review_text = (export_dir / "records_needing_review.csv").read_text(
        encoding="utf-8"
    )
    resolved_text = (export_dir / "resolved_exclude_or_defer.csv").read_text(
        encoding="utf-8"
    )

    assert "doi:10.1/model" not in review_text
    assert "doi:10.1/model" in resolved_text
    assert "calibrated_method_treatment_or_model_ambiguous" in resolved_text
    assert "doi:10.1/metadata" in review_text


def test_build_high_recall_candidate_pool_runs_family_branches_and_unions_records(
    tmp_path,
    monkeypatch,
) -> None:
    repo = prepare_repo(tmp_path)
    family_a = tmp_path / "family_a"
    family_b = tmp_path / "family_b"
    shutil.copytree(repo / "configs" / "retrieval", family_a)
    shutil.copytree(repo / "configs" / "retrieval", family_b)
    protocol_a = read_yaml(family_a / "protocol.yaml")
    protocol_b = read_yaml(family_b / "protocol.yaml")
    protocol_a["protocol_version"] = "0.1.0-qfamily-alpha"
    protocol_b["protocol_version"] = "0.1.0-qfamily-beta"
    (family_a / "protocol.yaml").write_text(
        json.dumps(protocol_a),
        encoding="utf-8",
    )
    (family_b / "protocol.yaml").write_text(
        json.dumps(protocol_b),
        encoding="utf-8",
    )

    def fake_discovery_dry_run(
        self,
        *,
        date_to,
        date_from=None,
        max_records_per_provider=100,
        run_id=None,
        providers=None,
    ):
        del date_to, date_from, max_records_per_provider
        run_dir = ensure_dir(repo / "runs" / str(run_id))
        ensure_dir(run_dir / "external_metadata" / "Q0001")
        protocol = read_yaml(self.config_dir / "protocol.yaml")
        (run_dir / "protocol_snapshot.yaml").write_text(
            json.dumps(protocol),
            encoding="utf-8",
        )
        write_json_atomic(
            run_dir / "manifest.json",
            {
                "run_id": run_id,
                "run_status": "completed",
                "normalized_record_count": 1,
            },
        )
        family = str(protocol["protocol_version"])
        provider = (providers or ["crossref"])[0]
        record = {
            "source_record_id": f"{family}:{provider}:1",
            "provider_record_id": f"{family}:{provider}:1",
            "source_provider": provider,
            "rank": 1,
            "title": f"{family} surface water occurrence",
            "abstract": "Measured emerging contaminants in river water.",
            "doi": f"10.1226/{family}",
            "document_type": "journal-article",
            "language": "en",
            "query_id": "Q0001",
        }
        (
            run_dir / "external_metadata" / "Q0001" / "candidates.jsonl"
        ).write_text(json.dumps(record) + "\n", encoding="utf-8")
        return {
            "run_id": run_id,
            "status": "completed",
            "normalized_record_count": 1,
            "source_statuses": {provider: "success"},
        }

    monkeypatch.setattr(
        "ecmonitor.retrieval_specialist.orchestration.phase11_runner.Phase11Runner.discovery_dry_run",
        fake_discovery_dry_run,
    )

    manager = RunManager(repo)
    result = manager.build_high_recall_candidate_pool(
        family_config_dirs=[family_a, family_b],
        date_to="2026-07-09",
        pool_id="pool_high_recall",
        max_records_per_provider=2,
        providers=["crossref", "openalex"],
    )
    manifest = read_json(repo / "runs" / "pool_high_recall" / "candidate_pool" / "manifest.json")
    summary = read_json(
        repo
        / "runs"
        / "pool_high_recall"
        / "candidate_pool"
        / "high_recall_build_summary.json"
    )
    records = read_jsonl(repo / "runs" / "pool_high_recall" / "candidate_pool" / "records.jsonl")

    assert result["status"] == "completed"
    assert result["mode"] == "high_recall_query_family_candidate_pool"
    assert len(result["source_run_ids"]) == 2
    assert all(
        (repo / "runs" / run_id / "manifest.json").exists()
        for run_id in result["source_run_ids"]
    )
    assert manifest["run_mode"] == "candidate_pool_union"
    assert summary["providers"] == ["crossref", "openalex"]
    assert records
    assert {
        "0.1.0-qfamily-alpha",
        "0.1.0-qfamily-beta",
    }.issubset(set(manifest["query_family_counts"]))


def test_high_recall_family_run_id_bounds_long_family_keys(tmp_path) -> None:
    repo = prepare_repo(tmp_path)
    manager = RunManager(repo)
    family_key = (
        "0_1_2_qfamily_pesticide_gap_neonicotinoid_fipronil_glyphosate_"
        "surface_water_refined_with_extra_long_context"
    )

    run_id = manager._high_recall_family_run_id("pool", family_key)  # noqa: SLF001

    assert run_id.startswith("pool__0_1_2_qfamily_pesticide_gap_neonicotinoid_fipronil")
    assert len(run_id) <= len("pool__") + 56 + 1 + 10
    assert run_id.endswith("_" + hashlib.sha1(family_key.encode("utf-8")).hexdigest()[:10])


def test_build_high_recall_candidate_pool_keeps_successful_family_when_one_fails(
    tmp_path,
    monkeypatch,
) -> None:
    repo = prepare_repo(tmp_path)
    family_a = tmp_path / "family_a"
    family_b = tmp_path / "family_b"
    shutil.copytree(repo / "configs" / "retrieval", family_a)
    shutil.copytree(repo / "configs" / "retrieval", family_b)
    protocol_a = read_yaml(family_a / "protocol.yaml")
    protocol_b = read_yaml(family_b / "protocol.yaml")
    protocol_a["protocol_version"] = "0.1.0-qfamily-alpha"
    protocol_b["protocol_version"] = "0.1.0-qfamily-beta"
    (family_a / "protocol.yaml").write_text(json.dumps(protocol_a), encoding="utf-8")
    (family_b / "protocol.yaml").write_text(json.dumps(protocol_b), encoding="utf-8")

    def fake_discovery_dry_run(
        self,
        *,
        date_to,
        date_from=None,
        max_records_per_provider=100,
        run_id=None,
        providers=None,
    ):
        del date_to, date_from, max_records_per_provider, providers
        protocol = read_yaml(self.config_dir / "protocol.yaml")
        family = str(protocol["protocol_version"])
        if family.endswith("beta"):
            raise TimeoutError("provider branch timed out")
        run_dir = ensure_dir(repo / "runs" / str(run_id))
        ensure_dir(run_dir / "external_metadata" / "Q0001")
        (run_dir / "protocol_snapshot.yaml").write_text(
            json.dumps(protocol),
            encoding="utf-8",
        )
        write_json_atomic(
            run_dir / "manifest.json",
            {
                "run_id": run_id,
                "run_status": "completed",
                "normalized_record_count": 1,
            },
        )
        record = {
            "source_record_id": f"{family}:crossref:1",
            "provider_record_id": f"{family}:crossref:1",
            "source_provider": "crossref",
            "rank": 1,
            "title": f"{family} surface water occurrence",
            "abstract": "Measured emerging contaminants in river water.",
            "doi": f"10.1226/{family}",
            "document_type": "journal-article",
            "language": "en",
            "query_id": "Q0001",
        }
        (
            run_dir / "external_metadata" / "Q0001" / "candidates.jsonl"
        ).write_text(json.dumps(record) + "\n", encoding="utf-8")
        return {
            "run_id": run_id,
            "status": "completed",
            "normalized_record_count": 1,
            "source_statuses": {"crossref": "success"},
        }

    monkeypatch.setattr(
        "ecmonitor.retrieval_specialist.orchestration.phase11_runner.Phase11Runner.discovery_dry_run",
        fake_discovery_dry_run,
    )

    result = RunManager(repo).build_high_recall_candidate_pool(
        family_config_dirs=[family_a, family_b],
        date_to="2026-07-09",
        pool_id="pool_high_recall_partial",
        max_records_per_provider=2,
        providers=["crossref"],
    )
    summary = read_json(
        repo
        / "runs"
        / "pool_high_recall_partial"
        / "candidate_pool"
        / "high_recall_build_summary.json"
    )
    manifest = read_json(
        repo / "runs" / "pool_high_recall_partial" / "candidate_pool" / "manifest.json"
    )

    assert result["status"] == "partial"
    assert len(result["source_run_ids"]) == 1
    assert len(result["failed_source_runs"]) == 1
    assert summary["failed_source_runs"][0]["error_class"] == "TimeoutError"
    assert manifest["query_family_counts"] == {"0.1.0-qfamily-alpha": 1}


def test_build_high_recall_candidate_pool_reuses_completed_family_run(
    tmp_path,
    monkeypatch,
) -> None:
    repo = prepare_repo(tmp_path)
    family_a = tmp_path / "family_a"
    family_b = tmp_path / "family_b"
    shutil.copytree(repo / "configs" / "retrieval", family_a)
    shutil.copytree(repo / "configs" / "retrieval", family_b)
    protocol_a = read_yaml(family_a / "protocol.yaml")
    protocol_b = read_yaml(family_b / "protocol.yaml")
    protocol_a["protocol_version"] = "0.1.0-qfamily-alpha"
    protocol_b["protocol_version"] = "0.1.0-qfamily-beta"
    (family_a / "protocol.yaml").write_text(json.dumps(protocol_a), encoding="utf-8")
    (family_b / "protocol.yaml").write_text(json.dumps(protocol_b), encoding="utf-8")
    alpha_run_id = RunManager._high_recall_family_run_id("pool_high_recall", "qfamily_alpha")  # noqa: SLF001
    beta_run_id = RunManager._high_recall_family_run_id("pool_high_recall", "qfamily_beta")  # noqa: SLF001
    existing_run = repo / "runs" / alpha_run_id
    ensure_dir(existing_run / "external_metadata" / "Q0001")
    (existing_run / "protocol_snapshot.yaml").write_text(
        json.dumps(protocol_a),
        encoding="utf-8",
    )
    write_json_atomic(
        existing_run / "manifest.json",
        {
                "run_id": alpha_run_id,
            "run_status": "completed",
            "normalized_record_count": 1,
            "source_health_status": {"crossref": "success"},
        },
    )
    (existing_run / "external_metadata" / "Q0001" / "candidates.jsonl").write_text(
        json.dumps(
            {
                "source_record_id": "alpha:crossref:1",
                "provider_record_id": "alpha:crossref:1",
                "source_provider": "crossref",
                "rank": 1,
                "title": "Alpha surface water occurrence",
                "abstract": "Measured emerging contaminants in river water.",
                "doi": "10.1226/alpha",
                "document_type": "journal-article",
                "language": "en",
                "query_id": "Q0001",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    called_run_ids: list[str | None] = []

    def fake_discovery_dry_run(
        self,
        *,
        date_to,
        date_from=None,
        max_records_per_provider=100,
        run_id=None,
        providers=None,
    ):
        del date_to, date_from, max_records_per_provider
        called_run_ids.append(run_id)
        run_dir = ensure_dir(repo / "runs" / str(run_id))
        ensure_dir(run_dir / "external_metadata" / "Q0001")
        protocol = read_yaml(self.config_dir / "protocol.yaml")
        (run_dir / "protocol_snapshot.yaml").write_text(
            json.dumps(protocol),
            encoding="utf-8",
        )
        write_json_atomic(
            run_dir / "manifest.json",
            {
                "run_id": run_id,
                "run_status": "completed",
                "normalized_record_count": 1,
                "source_health_status": {"crossref": "success"},
            },
        )
        (
            run_dir / "external_metadata" / "Q0001" / "candidates.jsonl"
        ).write_text(
            json.dumps(
                {
                    "source_record_id": "beta:crossref:1",
                    "provider_record_id": "beta:crossref:1",
                    "source_provider": (providers or ["crossref"])[0],
                    "rank": 1,
                    "title": "Beta surface water occurrence",
                    "abstract": "Measured emerging contaminants in lake water.",
                    "doi": "10.1226/beta",
                    "document_type": "journal-article",
                    "language": "en",
                    "query_id": "Q0001",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return {
            "run_id": run_id,
            "status": "completed",
            "normalized_record_count": 1,
            "raw_result_count": 1,
            "source_statuses": {"crossref": "success"},
        }

    monkeypatch.setattr(
        "ecmonitor.retrieval_specialist.orchestration.phase11_runner.Phase11Runner.discovery_dry_run",
        fake_discovery_dry_run,
    )

    result = RunManager(repo).build_high_recall_candidate_pool(
        family_config_dirs=[family_a, family_b],
        date_to="2026-07-09",
        pool_id="pool_high_recall",
        max_records_per_provider=2,
        providers=["crossref"],
    )
    summary = read_json(
        repo
        / "runs"
        / "pool_high_recall"
        / "candidate_pool"
        / "high_recall_build_summary.json"
    )

    assert called_run_ids == [beta_run_id]
    assert result["status"] == "completed"
    assert summary["skipped_source_runs"][0]["status"] == "reused_completed"
    assert summary["source_runs"][0]["reused_existing_run"] is True
    assert len(result["source_run_ids"]) == 2


def test_build_high_recall_candidate_pool_does_not_reuse_zero_candidate_run(
    tmp_path,
    monkeypatch,
) -> None:
    repo = prepare_repo(tmp_path)
    family_a = tmp_path / "family_a"
    family_b = tmp_path / "family_b"
    shutil.copytree(repo / "configs" / "retrieval", family_a)
    shutil.copytree(repo / "configs" / "retrieval", family_b)
    protocol_a = read_yaml(family_a / "protocol.yaml")
    protocol_b = read_yaml(family_b / "protocol.yaml")
    protocol_a["protocol_version"] = "0.1.0-qfamily-alpha"
    protocol_b["protocol_version"] = "0.1.0-qfamily-beta"
    (family_a / "protocol.yaml").write_text(json.dumps(protocol_a), encoding="utf-8")
    (family_b / "protocol.yaml").write_text(json.dumps(protocol_b), encoding="utf-8")
    alpha_run_id = RunManager._high_recall_family_run_id("pool_zero", "qfamily_alpha")  # noqa: SLF001
    existing_run = repo / "runs" / alpha_run_id
    ensure_dir(existing_run / "external_metadata" / "Q0001")
    write_json_atomic(
        existing_run / "manifest.json",
        {
            "run_id": alpha_run_id,
            "run_status": "completed",
            "run_completeness": "failed",
            "normalized_record_count": 0,
            "source_health_status": {"crossref": "failed", "pubmed": "failed"},
        },
    )
    (existing_run / "external_metadata" / "Q0001" / "candidates.jsonl").write_text(
        "",
        encoding="utf-8",
    )

    def fake_discovery_dry_run(
        self,
        *,
        date_to,
        date_from=None,
        max_records_per_provider=100,
        run_id=None,
        providers=None,
    ):
        del date_to, date_from, max_records_per_provider, providers
        run_dir = ensure_dir(repo / "runs" / str(run_id))
        ensure_dir(run_dir / "external_metadata" / "Q0001")
        protocol = read_yaml(self.config_dir / "protocol.yaml")
        write_json_atomic(
            run_dir / "manifest.json",
            {
                "run_id": run_id,
                "run_status": "completed",
                "normalized_record_count": 1,
                "source_health_status": {"crossref": "success"},
            },
        )
        record = {
            "source_record_id": "beta:crossref:1",
            "provider_record_id": "beta:crossref:1",
            "source_provider": "crossref",
            "rank": 1,
            "title": "Beta surface water occurrence",
            "abstract": "Measured emerging contaminants in lake water.",
            "doi": "10.1226/beta",
            "document_type": "journal-article",
            "language": "en",
            "query_id": "Q0001",
        }
        (
            run_dir / "external_metadata" / "Q0001" / "candidates.jsonl"
        ).write_text(json.dumps(record) + "\n", encoding="utf-8")
        return {
            "run_id": run_id,
            "status": "completed",
            "normalized_record_count": 1,
            "raw_result_count": 1,
            "source_statuses": {"crossref": "success"},
            "query_family": protocol["protocol_version"],
        }

    monkeypatch.setattr(
        "ecmonitor.retrieval_specialist.orchestration.phase11_runner.Phase11Runner.discovery_dry_run",
        fake_discovery_dry_run,
    )

    result = RunManager(repo).build_high_recall_candidate_pool(
        family_config_dirs=[family_a, family_b],
        date_to="2026-07-09",
        pool_id="pool_zero",
        max_records_per_provider=2,
        providers=["crossref"],
    )
    summary = read_json(
        repo / "runs" / "pool_zero" / "candidate_pool" / "high_recall_build_summary.json"
    )
    manifest = read_json(repo / "runs" / "pool_zero" / "candidate_pool" / "manifest.json")

    assert result["status"] == "partial"
    assert result["source_run_ids"] == [
        RunManager._high_recall_family_run_id("pool_zero", "qfamily_beta")  # noqa: SLF001
    ]
    assert summary["skipped_source_runs"][0]["status"] == (
        "skipped_completed_no_reusable_candidates"
    )
    assert summary["failed_source_runs"][0]["error_class"] == "NoReusableCandidates"
    assert manifest["query_family_counts"] == {
        RunManager._high_recall_family_run_id("pool_zero", "qfamily_beta"): 1  # noqa: SLF001
    }


def test_build_high_recall_candidate_pool_records_family_config_reference(
    tmp_path,
    monkeypatch,
) -> None:
    repo = prepare_repo(tmp_path)
    family = tmp_path / "family_ref"
    shutil.copytree(repo / "configs" / "retrieval", family)
    protocol = read_yaml(family / "protocol.yaml")
    protocol["protocol_version"] = "0.1.0-qfamily-family-ref"
    (family / "protocol.yaml").write_text(json.dumps(protocol), encoding="utf-8")

    def fake_discovery_dry_run(
        self,
        *,
        date_to,
        date_from=None,
        max_records_per_provider=100,
        run_id=None,
        providers=None,
    ):
        del date_to, date_from, max_records_per_provider, providers
        run_dir = ensure_dir(repo / "runs" / str(run_id))
        ensure_dir(run_dir / "external_metadata" / "Q0001")
        protocol_snapshot = read_yaml(self.config_dir / "protocol.yaml")
        (run_dir / "protocol_snapshot.yaml").write_text(
            json.dumps(protocol_snapshot),
            encoding="utf-8",
        )
        write_json_atomic(
            run_dir / "manifest.json",
            {
                "run_id": run_id,
                "run_status": "completed",
                "normalized_record_count": 1,
                "source_health_status": {"crossref": "success"},
            },
        )
        (run_dir / "external_metadata" / "Q0001" / "candidates.jsonl").write_text(
            json.dumps(
                {
                    "source_record_id": "family-ref:crossref:1",
                    "provider_record_id": "family-ref:crossref:1",
                    "source_provider": "crossref",
                    "rank": 1,
                    "title": "PFAS occurrence in river water",
                    "abstract": "Measured PFAS concentrations in river water.",
                    "doi": "10.1226/family-ref",
                    "document_type": "journal-article",
                    "language": "en",
                    "query_id": "Q0001",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return {
            "run_id": run_id,
            "status": "completed",
            "normalized_record_count": 1,
            "raw_result_count": 1,
            "source_statuses": {"crossref": "success"},
        }

    monkeypatch.setattr(
        "ecmonitor.retrieval_specialist.orchestration.phase11_runner.Phase11Runner.discovery_dry_run",
        fake_discovery_dry_run,
    )

    result = RunManager(repo).build_high_recall_candidate_pool(
        family_config_dirs=[family],
        date_to="2026-07-09",
        pool_id="pool_config_ref",
        max_records_per_provider=2,
        providers=["crossref"],
    )
    source_run_id = result["source_run_ids"][0]
    config_ref = read_json(repo / "runs" / source_run_id / "query_family_config_ref.json")
    manifest = read_json(
        repo / "runs" / "pool_config_ref" / "candidate_pool" / "manifest.json"
    )

    assert config_ref["config_name"] == "family_ref"
    assert manifest["source_runs"][0]["config_name"] == "family_ref"
    assert manifest["source_runs"][0]["config_dir"].endswith("family_ref")


def test_live_discovery_dry_run_does_not_overfetch_when_limit_is_below_page_size(
    tmp_path,
    monkeypatch,
) -> None:
    repo = prepare_repo(tmp_path)
    captured: dict[str, int] = {}

    def fake_search_sources_external(self, **kwargs):
        del self
        captured["page_size"] = int(kwargs["runtime"]["retrieval_page_size"])
        captured["max_scan_depth"] = int(kwargs["max_scan_depth"])
        captured["max_records_per_provider"] = int(kwargs["max_records_per_provider"])
        return {
            "source_statuses": {"crossref": "source_success"},
            "raw_result_count": 0,
            "imported_pages": 0,
        }

    monkeypatch.setattr(
        "ecmonitor.retrieval_specialist.orchestration.phase11_runner.Phase11Runner._search_sources_external",
        fake_search_sources_external,
    )
    monkeypatch.setattr(
        "ecmonitor.retrieval_specialist.orchestration.phase11_runner.Phase11Runner._normalize_and_register",
        lambda *args, **kwargs: 0,
    )

    result = RunManager(repo).discovery_dry_run(
        date_to="2026-07-09",
        run_id="retrieval_small_limit",
        max_records_per_provider=5,
        providers=["crossref"],
    )

    assert result["status"] == "completed"
    assert captured == {
        "page_size": 5,
        "max_scan_depth": 1,
        "max_records_per_provider": 5,
    }


def test_metadata_enrichment_gateway_failures_are_bounded(
    tmp_path,
    monkeypatch,
) -> None:
    repo = prepare_repo(tmp_path)
    pool_root = ensure_dir(repo / "runs" / "pool" / "candidate_pool")
    record = {
        "candidate_pool_key": "doi:10.1/gateway-failure",
        "title": "PFAS occurrence in river water",
        "abstract": "",
        "doi": "10.1/gateway-failure",
        "query_families": ["pfas"],
        "source_providers": ["crossref"],
    }
    (pool_root / "records.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )
    calls = 0

    def fail_invoke(self, **kwargs):  # noqa: ANN001
        nonlocal calls
        del self, kwargs
        calls += 1
        raise RuntimeError("isolated skill runner failed")

    monkeypatch.setattr(
        "ecmonitor.retrieval_specialist.orchestration.phase11_runner."
        "ExternalMetadataDiscoveryGateway.invoke_metadata_enrichment",
        fail_invoke,
    )

    first = RunManager(repo).enrich_candidate_pool_metadata(pool_id="pool", limit=1)
    second = RunManager(repo).enrich_candidate_pool_metadata(pool_id="pool", limit=1)
    third = RunManager(repo).enrich_candidate_pool_metadata(pool_id="pool", limit=1)
    fourth = RunManager(repo).enrich_candidate_pool_metadata(pool_id="pool", limit=1)
    attempts = list(
        read_jsonl(
            pool_root / "metadata_enrichment" / "enrichment_attempts.jsonl"
        )
    )

    assert first["retryable_records"] == 1
    assert first["remaining_enrichment_queue"] == 1
    assert second["remaining_enrichment_queue"] == 1
    assert third["retry_exhausted_records"] == 1
    assert third["remaining_enrichment_queue"] == 0
    assert fourth["requested_records"] == 0
    assert calls == 3
    assert attempts[-1]["provider_statuses"] == {
        "openalex": "failed",
        "semantic_scholar": "failed",
        "pubmed": "failed",
    }
    assert attempts[-1]["status"] == "completed_retry_exhausted"


def test_metadata_enrichment_legacy_status_respects_provider_outcome(tmp_path) -> None:
    runner = RunManager(prepare_repo(tmp_path))._phase11_runner()  # noqa: SLF001

    assert not runner._metadata_enrichment_attempt_is_terminal(  # noqa: SLF001
        {
            "status": "",
            "provider_statuses": {
                "openalex": "success",
                "semantic_scholar": "rate-limited",
            },
        }
    )
    assert runner._metadata_enrichment_attempt_is_terminal(  # noqa: SLF001
        {
            "status": "",
            "provider_statuses": {
                "openalex": "success",
                "semantic_scholar": "no-results",
            },
        }
    )
