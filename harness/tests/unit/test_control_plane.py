from pathlib import Path

from ecmonitor.retrieval_specialist.orchestration.run_manager import RunManager
from ecmonitor.retrieval_specialist.storage.control_plane import SCHEMA_VERSION, ControlPlane


def test_control_plane_migration_creates_required_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "ecmonitor_control.sqlite3"
    plane = ControlPlane(db_path, code_commit_sha="test-sha")

    migration = plane.migrate()
    status = plane.status()
    integrity = plane.integrity_check()

    assert migration["schema_version"] == SCHEMA_VERSION
    assert status["schema_version"] == SCHEMA_VERSION
    assert status["table_counts"]["schema_migrations"] == 1
    for table in [
        "runs",
        "query_iterations",
        "operator_checkpoints",
        "source_records",
        "documents",
        "document_query_membership",
        "screening_decisions",
        "download_outbox",
        "download_jobs",
        "metric_values",
        "audit_events",
    ]:
        assert table in status["table_counts"]
    assert integrity["status"] == "ok"


def test_run_manager_db_commands_use_repo_state_database(tmp_path: Path) -> None:
    manager = RunManager(tmp_path)
    migration = manager.db_migrate()
    status = manager.db_status()

    assert migration["status"] == "ok"
    assert status["database"].endswith("state\\ecmonitor_control.sqlite3") or status[
        "database"
    ].endswith("state/ecmonitor_control.sqlite3")
    assert manager.db_integrity_check()["status"] == "ok"
