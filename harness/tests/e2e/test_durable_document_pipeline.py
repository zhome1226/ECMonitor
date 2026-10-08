from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ecmonitor.fulltext_extraction.adapters.pymupdf_adapter import PyMuPDFParser
from ecmonitor.fulltext_extraction.harness import FulltextExtractionHarness
from ecmonitor.fulltext_extraction.registry import ChemicalRegistry
from ecmonitor.fulltext_extraction.storage import FulltextControlPlane
from ecmonitor.orchestration.store import WorkflowStore
from ecmonitor.orchestration.worker import Runtime, WorkflowWorker


class IncompleteEvidenceExtractor:
    extractor_name = "synthetic-test-only"

    def extract(self, chunk: Any) -> list[dict[str, Any]]:
        return [{"analyte": {"raw_name": "PFOA"}, "evidence": {}}]


def test_document_acquisition_extraction_validation_pause_and_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fitz = pytest.importorskip("fitz")
    root = Path(__file__).resolve().parents[2]
    source = tmp_path / "sources"
    source.mkdir()
    pdf = source / "study.pdf"
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), "PFOA was measured in river water at 2 ng/L.")
        document.save(pdf)
    runtime = Runtime(root, tmp_path / "runtime", source, model_agents=False)
    store = WorkflowStore(runtime.data_root / "state/workflow.sqlite3")
    worker = WorkflowWorker(store, runtime)
    plane = FulltextControlPlane(runtime.data_root / "state/fulltext.sqlite3")
    scientific_engine = FulltextExtractionHarness(parser=PyMuPDFParser(), control_plane=plane,
        chemical_registry=ChemicalRegistry(runtime.data_root / "state/chemicals.sqlite3"),
        output_dir=runtime.data_root / "documents", extractor=IncompleteEvidenceExtractor())
    monkeypatch.setattr("ecmonitor.orchestration.worker._build_harness", lambda _: scientific_engine)
    payload = {"runtime": worker.signature, "records": [{"global_record_id": "doc", "local_path": "study.pdf",
                "screening_decision": "include"}]}
    store.enqueue(run_id="run", stage="retrieval", payload=payload)
    for _ in range(4):
        assert worker.run_once()
    assert not worker.run_once()
    with store.connect() as db:
        paused = db.execute("SELECT id FROM tasks WHERE status='paused_human_review'").fetchone()
        assert paused is not None
        assert db.execute("SELECT COUNT(*) FROM tasks WHERE stage='commit'").fetchone()[0] == 0
    with plane._connect() as db:
        human_tasks = db.execute("SELECT task_id FROM human_review_tasks WHERE status='pending'").fetchall()
    assert human_tasks
    for task in human_tasks:
        plane.resolve_human_review_task(task[0], disposition="rejected", note="Insufficient source evidence")
    store.resume(paused[0], reason="Signed source-evidence rejection recorded")
    assert worker.run_once()
    assert worker.run_once()
    assert not worker.run_once()
    with store.connect() as db:
        committed = json.loads(db.execute("SELECT result FROM tasks WHERE stage='commit'").fetchone()[0])
        assert committed["release_ready"] is False
        assert db.execute("SELECT COUNT(*) FROM tasks WHERE status='completed'").fetchone()[0] == 5
    assert plane.status()["table_counts"]["document_sessions"] == 1


def test_configuration_change_blocks_resume(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    runtime = Runtime(root, tmp_path / "runtime", tmp_path, model_agents=False)
    store = WorkflowStore(runtime.data_root / "state/workflow.sqlite3")
    store.enqueue(run_id="run", stage="retrieval", payload={"runtime": {"runtime_digest": "old"}, "records": []})
    assert WorkflowWorker(store, runtime).run_once()
    assert store.status()["counts"][0]["status"] == "failed_terminal"
