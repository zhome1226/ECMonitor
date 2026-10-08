import sqlite3
from pathlib import Path

from ecmonitor.retrieval_specialist.operators.audit_pool import (
    AuditPoolEntry,
    AuditPoolEvaluator,
)
from ecmonitor.retrieval_specialist.orchestration.phase11_runner import Phase11Runner
from ecmonitor.retrieval_specialist.storage.atomic_io import read_json, write_yaml_atomic
from ecmonitor.retrieval_specialist.storage.control_plane import ControlPlane


def test_audit_pool_matches_by_doi_and_title_fallback() -> None:
    evaluator = AuditPoolEvaluator(
        [
            AuditPoolEntry(
                audit_id="doi-match",
                role="holdout_sentinel",
                expected_relevance="include",
                title="Different title spelling",
                doi="https://doi.org/10.1000/abc",
            ),
            AuditPoolEntry(
                audit_id="title-match",
                role="external_audit",
                expected_relevance="include",
                title="Occurrence of emerging contaminants in surface water",
            ),
            AuditPoolEntry(
                audit_id="missing",
                role="external_audit",
                expected_relevance="include",
                title="Missing sentinel record",
            ),
            AuditPoolEntry(
                audit_id="contains-match",
                role="external_audit",
                expected_relevance="include",
                title=(
                    "Non-targeted identification of per- and polyfluoroalkyl "
                    "substances in surface water"
                ),
            ),
        ]
    )

    result = evaluator.evaluate(
        [
            {
                "global_record_id": "doi:10.1000/abc",
                "normalized_doi": "10.1000/abc",
                "canonical_title": "Resolved DOI title",
                "normalized_title": "resolved doi title",
                "query_ids": ["Q0001"],
                "sources": ["crossref"],
            },
            {
                "global_record_id": "title:1",
                "canonical_title": "Occurrence of emerging contaminants in surface water",
                "normalized_title": "occurrence of emerging contaminants in surface water",
                "query_ids": ["Q0001"],
                "sources": ["openalex"],
            },
            {
                "global_record_id": "title:pfas",
                "canonical_title": (
                    "Non-targeted identification of per- and polyfluoroalkyl substances "
                    "at trace level in surface water using fragment ion flagging"
                ),
                "normalized_title": (
                    "non targeted identification of per and polyfluoroalkyl substances "
                    "at trace level in surface water using fragment ion flagging"
                ),
                "query_ids": ["Q0001"],
                "sources": ["pubmed"],
            },
        ]
    )

    rows = {row["audit_id"]: row for row in result["audit_pool_recall"]}
    assert rows["doi-match"]["retrieved"] == 1
    assert rows["doi-match"]["match_method"] == "doi"
    assert rows["title-match"]["retrieved"] == 1
    assert rows["title-match"]["match_method"] == "normalized_title"
    assert rows["contains-match"]["retrieved"] == 1
    assert rows["contains-match"]["match_method"] == "normalized_title_token_subset"
    assert rows["missing"]["retrieved"] == 0

    summary = {row["role"]: row for row in result["audit_pool_recall_summary"]}
    assert summary["all"]["total"] == 4
    assert summary["all"]["retrieved"] == 3
    assert summary["all"]["missing_audit_ids"] == ["missing"]


def test_audit_pool_does_not_match_overly_generic_contained_title() -> None:
    evaluator = AuditPoolEvaluator(
        [
            AuditPoolEntry(
                audit_id="specific-cec-study",
                role="holdout_sentinel",
                expected_relevance="include",
                title=(
                    "A community-guided approach to monitoring contaminants of "
                    "emerging concern in freshwater systems using passive samplers"
                ),
            )
        ]
    )

    result = evaluator.evaluate(
        [
            {
                "global_record_id": "doi:10.1002/awwa.1057",
                "canonical_title": "Contaminants of Emerging Concern",
                "normalized_title": "contaminants of emerging concern",
                "query_ids": ["qfamily-passive-sampler"],
                "sources": ["crossref"],
            }
        ]
    )

    row = result["audit_pool_recall"][0]
    assert row["retrieved"] == 0
    assert row["match_status"] == "missing"


def test_audit_pool_yaml_validation_rejects_unknown_role(tmp_path: Path) -> None:
    path = tmp_path / "audit_pool.yaml"
    write_yaml_atomic(
        path,
        {
            "entries": [
                {
                    "audit_id": "bad-role",
                    "role": "not_a_role",
                    "expected_relevance": "include",
                    "title": "A title",
                }
            ]
        },
    )

    try:
        AuditPoolEvaluator.from_yaml(path)
    except ValueError as exc:
        assert "Unsupported audit pool role" in str(exc)
    else:
        raise AssertionError("Expected invalid audit pool role to fail")


def test_phase11_evaluate_audit_pool_exports_recall_tables(tmp_path: Path) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=tmp_path / "configs",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "state.sqlite3",
    )
    run_id = "audit-pool-run"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    ControlPlane(tmp_path / "state.sqlite3", runner._git_sha()).migrate()  # noqa: SLF001
    _seed_run_for_audit_pool(tmp_path / "state.sqlite3", run_id)
    audit_pool = tmp_path / "audit_pool.yaml"
    write_yaml_atomic(
        audit_pool,
        {
            "entries": [
                {
                    "audit_id": "retrieved-doi",
                    "role": "holdout_sentinel",
                    "expected_relevance": "include",
                    "title": "Different title",
                    "doi": "10.1000/retrieved",
                },
                {
                    "audit_id": "missing-title",
                    "role": "external_audit",
                    "expected_relevance": "include",
                    "title": "Not retrieved by this run",
                },
            ]
        },
    )

    result = runner.evaluate_audit_pool(run_id, audit_pool)

    assert result["total"] == 2
    assert result["retrieved"] == 1
    assert result["missing"] == 1
    assert (run_dir / "exports" / "audit_pool_recall.csv").exists()
    assert (run_dir / "exports" / "audit_pool_recall_summary.csv").exists()
    assert (tmp_path / "paper_exports" / "audit_pool_recall.csv").exists()
    manifest = read_json(run_dir / "exports" / "export_manifest.json")
    assert manifest["row_counts"]["audit_pool_recall.csv"] == 2


def test_phase11_evaluate_candidate_pool_audit_exports_recall_tables(
    tmp_path: Path,
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=tmp_path / "configs",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "state.sqlite3",
    )
    pool_id = "candidate-pool-audit"
    pool_root = tmp_path / "runs" / pool_id / "candidate_pool"
    pool_root.mkdir(parents=True)
    (pool_root / "records.jsonl").write_text(
        "\n".join(
            [
                (
                    '{"candidate_pool_key":"doi:10.2000/seed","doi":"https://doi.org/10.2000/seed",'
                    '"title":"Known surface water emerging contaminant seed",'
                    '"normalized_title":"known surface water emerging contaminant seed",'
                    '"source_providers":["crossref","pubmed"],'
                    '"query_families":["qcore"]}'
                ),
                (
                    '{"candidate_pool_key":"title:sentinel",'
                    '"title":"Trace organic contaminants in river water",'
                    '"normalized_title":"trace organic contaminants in river water",'
                    '"source_provider":"pubmed","query_family":"qwater"}'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    audit_pool = tmp_path / "audit_pool.yaml"
    write_yaml_atomic(
        audit_pool,
        {
            "entries": [
                {
                    "audit_id": "seed-doi",
                    "role": "development_seed",
                    "expected_relevance": "include",
                    "title": "Different title",
                    "doi": "10.2000/seed",
                },
                {
                    "audit_id": "sentinel-title",
                    "role": "holdout_sentinel",
                    "expected_relevance": "include",
                    "title": "Trace organic contaminants in river water",
                },
                {
                    "audit_id": "missing",
                    "role": "external_audit",
                    "expected_relevance": "include",
                    "title": "Not in this candidate pool",
                },
            ]
        },
    )

    result = runner.evaluate_candidate_pool_audit(
        pool_id=pool_id,
        audit_pool_path=audit_pool,
    )

    assert result["total"] == 3
    assert result["retrieved"] == 2
    export_dir = tmp_path / "paper_exports" / pool_id
    assert (export_dir / "audit_pool_recall.csv").exists()
    assert (export_dir / "audit_pool_recall_summary.csv").exists()
    assert (export_dir / "candidate_pool_audit_recall.json").exists()
    assert (export_dir / "CANDIDATE_POOL_AUDIT_RECALL.md").exists()


def _seed_run_for_audit_pool(db_path: Path, run_id: str) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            INSERT INTO runs (
                run_id, status, completeness, date_from, date_to, current_iteration,
                current_query_id, current_state, started_at, code_commit_sha,
                git_branch, config_hash, prompt_hash, protocol_version, scoring_version,
                model_version, source_status_json
            )
            VALUES (?, 'completed', 'complete', '2006-01-01', '2026-07-10', 1,
                    'Q0001', 'STOP', '2026-07-10T00:00:00Z', 'sha', 'branch',
                    'config', 'prompt', 'protocol', 'scoring', 'model', '{}')
            """,
            (run_id,),
        )
        connection.execute(
            """
            INSERT INTO documents (
                global_record_id, normalized_doi, canonical_title, normalized_title,
                keywords_json, authors_json, issn_json, eissn_json,
                sampled_matrices_json, article_ec_scope, first_seen_run_id,
                first_seen_query_id, first_seen_at, latest_metadata_version
            )
            VALUES ('doi:10.1000/retrieved', '10.1000/retrieved',
                    'Retrieved sentinel title', 'retrieved sentinel title',
                    '[]', '[]', '[]', '[]', '[]', 'uncertain', ?, 'Q0001',
                    '2026-07-10T00:00:00Z', 'test')
            """,
            (run_id,),
        )
        connection.execute(
            """
            INSERT INTO document_query_membership (
                global_record_id, run_id, query_id, iteration, source_name,
                source_rank, first_seen_in_query, already_known_before_query,
                included_in_novelty_sample, created_at
            )
            VALUES ('doi:10.1000/retrieved', ?, 'Q0001', 1, 'crossref',
                    1, 1, 0, 1, '2026-07-10T00:00:00Z')
            """,
            (run_id,),
        )
        connection.commit()
    finally:
        connection.close()
