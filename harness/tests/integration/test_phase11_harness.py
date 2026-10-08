import csv
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from ecmonitor.retrieval_specialist.orchestration.run_manager import RunManager
from ecmonitor.retrieval_specialist.storage.atomic_io import ensure_dir, sha256_file


def prepare_repo(tmp_path: Path) -> Path:
    source_root = Path(__file__).resolve().parents[2]
    for dirname in ["configs", "prompts", "registry", "schemas"]:
        shutil.copytree(source_root / dirname, tmp_path / dirname)
    ensure_dir(tmp_path / "tests" / "fixtures")
    shutil.copy2(
        source_root / "tests" / "fixtures" / "mock_records.json",
        tmp_path / "tests" / "fixtures" / "mock_records.json",
    )
    if (source_root / "uv.lock").exists():
        shutil.copy2(source_root / "uv.lock", tmp_path / "uv.lock")
    return tmp_path


@pytest.fixture(scope="module")
def phase11_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    repo = prepare_repo(tmp_path_factory.mktemp("phase11_repo"))
    manager = RunManager(repo)
    manager.mock_run(date_to="2026-07-09", run_id="retrieval_phase11_first")
    manager.mock_run(date_to="2026-07-09", run_id="retrieval_phase11_second")
    return repo


def _connect(repo: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(repo / "state" / "ecmonitor_control.sqlite3")
    connection.row_factory = sqlite3.Row
    return connection


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_phase11_multi_iteration_accept_reject_rollback_and_saturation(
    phase11_repo: Path,
) -> None:
    with _connect(phase11_repo) as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT iteration, query_id, parent_query_id, acceptance_status,
                       saturation_status
                FROM query_iterations
                WHERE run_id = 'retrieval_phase11_first'
                ORDER BY iteration
                """
            )
        ]

    assert [row["query_id"] for row in rows] == ["Q0001", "Q0002", "Q0003", "Q0004", "Q0005"]
    assert rows[0]["parent_query_id"] is None
    assert rows[2]["parent_query_id"] == "Q0002"
    assert rows[3]["acceptance_status"] == "rollback"
    assert {row["acceptance_status"] for row in rows} >= {"accepted", "rejected", "rollback"}
    assert rows[0]["saturation_status"] == "not_saturated"
    assert rows[-1]["saturation_status"] == "saturated_narrow"

    transitions = (
        phase11_repo
        / "runs"
        / "retrieval_phase11_first"
        / "logs"
        / "state_transition_log.jsonl"
    ).read_text(encoding="utf-8")
    transition_rows = [json.loads(line) for line in transitions.splitlines() if line]
    assert any(
        row["previous_state"] == "RELEASE_ITERATION_MEMORY"
        and row["next_state"] == "BUILD_QUERY"
        for row in transition_rows
    )


def test_global_novelty_persists_across_runs(phase11_repo: Path) -> None:
    with _connect(phase11_repo) as connection:
        q1_second = connection.execute(
            """
            SELECT
                COUNT(DISTINCT CASE WHEN already_known_before_query = 1
                    THEN global_record_id END) AS known_count,
                COUNT(DISTINCT CASE WHEN already_known_before_query = 0
                    THEN global_record_id END) AS novel_count
            FROM document_query_membership
            WHERE run_id = 'retrieval_phase11_second' AND query_id = 'Q0001'
            """
        ).fetchone()
        q2_first = connection.execute(
            """
            SELECT COUNT(*) AS known_memberships
            FROM document_query_membership
            WHERE run_id = 'retrieval_phase11_first'
              AND query_id = 'Q0002'
              AND already_known_before_query = 1
            """
        ).fetchone()

    assert int(q1_second["known_count"]) > 0
    assert int(q1_second["novel_count"]) == 0
    assert int(q2_first["known_memberships"]) > 0


def test_corrected_metrics_and_json_csv_serialization(phase11_repo: Path) -> None:
    rows = _csv_rows(
        phase11_repo / "runs" / "retrieval_phase11_first" / "exports" / "query_metrics_wide.csv"
    )
    assert len(rows) == 5
    for row in rows:
        assert 0.0 <= float(row["novel_precision_at_20"]) <= 1.0
        assert float(row["eligible_precision"]) <= 1.0
        assert float(row["defer_rate"]) >= 0.0
        assert float(row["marginal_relevant_yield"]) <= 1.0
        assert float(row["marginal_eligible_count"]).is_integer()
        json.loads(row["added_terms"])

    exclusion_rows = _csv_rows(
        phase11_repo
        / "runs"
        / "retrieval_phase11_first"
        / "exports"
        / "exclusion_reason_evolution.csv"
    )
    assert any(row["reason_code"] == "E_WASTEWATER" for row in exclusion_rows)


def test_transactional_handoff_and_download_queue_states(phase11_repo: Path) -> None:
    manager = RunManager(phase11_repo)
    status_before = manager.download_queue_status()
    assert status_before["pending_download_jobs"] > 0

    claim = manager.mock_download_claim(worker_id="test-worker", lease_seconds=60)
    assert claim["status"] == "claimed"
    key = claim["claimed"]["idempotency_key"]
    status_claimed = manager.download_queue_status()
    assert status_claimed["state_counts"]["claimed"] >= 1

    completed = manager.mock_download_complete(idempotency_key=key, final_status="skipped_existing")
    assert completed["status"] == "skipped_existing"
    assert completed["pending_download_jobs"] == status_before["pending_download_jobs"] - 1

    with _connect(phase11_repo) as connection:
        orphan = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM download_outbox o
            WHERE NOT EXISTS (
                SELECT 1 FROM screening_decisions s
                WHERE s.global_record_id = o.global_record_id
                  AND s.run_id = o.run_id
                  AND s.query_id = o.query_id
                  AND s.decision = 'include'
            )
            """
        ).fetchone()
    assert int(orphan["count"]) == 0


def test_export_rebuild_is_complete_and_checksum_stable(phase11_repo: Path) -> None:
    run_exports = phase11_repo / "runs" / "retrieval_phase11_first" / "exports"
    before = {
        path.name: sha256_file(path)
        for path in run_exports.glob("*")
        if path.is_file() and path.name != "export_manifest.json"
    }
    shutil.rmtree(run_exports)
    result = RunManager(phase11_repo).export_paper_data("retrieval_phase11_first")
    after = {
        path.name: sha256_file(path)
        for path in run_exports.glob("*")
        if path.is_file() and path.name != "export_manifest.json"
    }
    assert result["row_counts"]["query_metrics_wide"] == 5
    assert before == after
    assert (run_exports / "export_manifest.json").exists()


def test_failure_after_source_page_resumes_without_duplicate_rows(tmp_path: Path) -> None:
    repo = prepare_repo(tmp_path)
    manager = RunManager(repo)
    interrupted = manager.mock_run(
        date_to="2026-07-09",
        run_id="retrieval_resume_source_page",
        max_iterations=2,
        fail_after_source_page=2,
    )
    assert interrupted["status"] == "interrupted_injected"

    with _connect(repo) as connection:
        before = connection.execute(
            "SELECT COUNT(*) AS count FROM source_records WHERE run_id = ?",
            ("retrieval_resume_source_page",),
        ).fetchone()["count"]

    resumed = manager.resume("retrieval_resume_source_page")
    assert resumed["status"] == "completed"

    with _connect(repo) as connection:
        after = connection.execute(
            "SELECT COUNT(*) AS count FROM source_records WHERE run_id = ?",
            ("retrieval_resume_source_page",),
        ).fetchone()["count"]
        distinct_keys = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM (
                SELECT DISTINCT query_id, source_name, source_record_id
                FROM source_records
                WHERE run_id = ?
            )
            """,
            ("retrieval_resume_source_page",),
        ).fetchone()["count"]

    assert after >= before
    assert after == distinct_keys
