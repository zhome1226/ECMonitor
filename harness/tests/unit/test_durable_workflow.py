from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ecmonitor.download_specialist.models import DownloadOutcome
from ecmonitor.download_specialist.routes import AuthorizedHttpsRoute, LocalInventoryRoute
from ecmonitor.orchestration.router import route_validation_outcome
from ecmonitor.orchestration.store import LeaseLost, WorkflowStore
from ecmonitor.security import redact


def test_atomic_successor_and_idempotency(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "queue.sqlite3")
    task_id = store.enqueue(run_id="run", stage="retrieval", payload={"version": 1})
    assert store.enqueue(run_id="run", stage="retrieval", payload={"version": 1}) == task_id
    with pytest.raises(ValueError, match="different input"):
        store.enqueue(run_id="run", stage="retrieval", payload={"version": 2})
    task = store.claim()
    assert task
    store.finish(task_id, task["lease_token"], {"ready": True}, successors=[
        {"stage": "download", "document_id": "doc", "payload": {"doi": "10.1/test"}}])
    following = store.claim()
    assert following and following["stage"] == "download"
    with store.connect() as db:
        assert db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()[0] == "completed"
        with pytest.raises(sqlite3.IntegrityError, match="Immutable"):
            db.execute("DELETE FROM events")


def test_stale_lease_cannot_commit(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "queue.sqlite3")
    task_id = store.enqueue(run_id="run", stage="retrieval", payload={})
    original = store.claim()
    assert original
    with store.connect() as db:
        db.execute("UPDATE tasks SET lease_until=0 WHERE id=?", (task_id,))
    reclaimed = store.claim()
    assert reclaimed and reclaimed["lease_token"] != original["lease_token"]
    with pytest.raises(LeaseLost):
        store.finish(task_id, original["lease_token"], {})
    store.finish(task_id, reclaimed["lease_token"], {})


def test_retry_budget_and_pause(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "queue.sqlite3")
    task_id = store.enqueue(run_id="run", stage="validation", payload={}, maximum_attempts=2)
    task = store.claim()
    assert task
    store.finish(task_id, task["lease_token"], {}, status="paused_human_review")
    assert store.claim() is None
    store.resume(task_id, reason="Source evidence checked")
    task = store.claim()
    assert task and task["attempts"] == 2
    store.fail(task_id, task["lease_token"], error_code="TransportError", backoff_seconds=0)
    assert store.claim() is None
    assert store.status()["counts"][0]["status"] == "failed_terminal"


def test_concurrent_claim_is_exclusive(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "queue.sqlite3")
    store.enqueue(run_id="run", stage="retrieval", payload={})
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: store.claim(), range(4)))
    assert len([claim for claim in claims if claim is not None]) == 1


def test_secret_safe_payloads_and_run_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = WorkflowStore(tmp_path / "queue.sqlite3")
    secret = "sk-" + "synthetic" * 5
    monkeypatch.setenv("TEST_API_KEY", secret)
    assert secret not in json.dumps(redact({"error": f"Bearer {secret}", "password": "unsafe"}))
    with pytest.raises(ValueError, match="credentials"):
        store.enqueue(run_id="run", stage="retrieval", payload={"authorization": secret})
    with pytest.raises(ValueError, match="Invalid"):
        store.enqueue(run_id="../outside", stage="retrieval", payload={})


def test_download_handoff_matches_schema() -> None:
    root = Path(__file__).resolve().parents[2]
    schema = json.loads((root / "schemas/handoff/download_result.schema.json").read_text())
    outcome = DownloadOutcome("doc", "downloaded", (), artifact_path=Path("source.pdf"), sha256="a" * 64)
    Draft202012Validator(schema).validate(outcome.to_handoff(idempotency_key="job:v1", download_job_id="job"))


def test_local_route_does_not_escape_root(tmp_path: Path) -> None:
    outside = tmp_path / "private.pdf"
    outside.write_bytes(b"%PDF-fake")
    root = tmp_path / "library"
    root.mkdir()
    assert LocalInventoryRoute(root).acquire({"local_path": "../private.pdf"}).status == "terminal_failure"


@pytest.mark.parametrize("url", ["http://example.org/file.pdf", "https://localhost/file.pdf",
                                "https://example.org/file.pdf?token=unsafe", "https://example.org/file.pdf"])
def test_http_routes_fail_closed_without_authorization(tmp_path: Path, url: str) -> None:
    route = AuthorizedHttpsRoute(tmp_path, frozenset({"example.org"}))
    assert route.acquire({"authorized_url": url}).reason_code == "explicit_authorization_required"


def test_identity_and_query_retry_budgets_are_bounded() -> None:
    assert route_validation_outcome("deferred_identity_evidence", targeted_attempts=99).target == "human_review"
    assert route_validation_outcome("rejected_scope", reason_codes=("QUERY-GAP-01",), targeted_attempts=99).target == "human_review"
