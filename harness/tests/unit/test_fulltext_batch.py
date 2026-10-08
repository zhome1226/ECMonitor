from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest

from ecmonitor.fulltext_extraction.batch import SequentialLibraryRunner
from ecmonitor.fulltext_extraction.models import DocumentRunReport


class FakeControlPlane:
    def __init__(self) -> None:
        self.committed: set[str] = set()

    def has_committed_path(self, pdf_path: Path) -> bool:
        return str(pdf_path.resolve()) in self.committed


class FakeHarness:
    def __init__(self, control_plane: FakeControlPlane) -> None:
        self.control_plane = control_plane
        self.calls: list[tuple[Path, dict[str, object] | None]] = []

    def run_document(
        self,
        pdf_path: Path,
        *,
        bibliographic_metadata: dict[str, Any] | None = None,
    ) -> DocumentRunReport:
        resolved = pdf_path.resolve()
        self.calls.append((resolved, bibliographic_metadata))
        self.control_plane.committed.add(str(resolved))
        return DocumentRunReport(
            document_id=resolved.stem,
            document_session_id=f"session-{len(self.calls)}",
            source_path=str(resolved),
            source_sha256="sha256",
            registry_snapshot_version=0,
            parser_name="fake",
            parser_version="1",
            page_count=1,
            chunk_count=1,
            candidate_count=0,
            review_count=0,
            resolution_count=0,
            committed=True,
            output_path=None,
            bibliographic_metadata=bibliographic_metadata or {},
        )


def _runner(tmp_path: Path) -> tuple[SequentialLibraryRunner, FakeHarness]:
    control_plane = FakeControlPlane()
    harness = FakeHarness(control_plane)
    runner = SequentialLibraryRunner(
        harness, reports_jsonl=tmp_path / "events" / "reports.jsonl"
    )
    return runner, harness


def test_run_manifest_resolves_relative_paths_and_preserves_bibliographic_metadata(
    tmp_path: Path,
) -> None:
    pdf = tmp_path / "pdf" / "paper.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(b"%PDF-fake")
    manifest = tmp_path / "manifest.csv"
    with manifest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["pdf_path", "doi", "title", "journal"])
        writer.writeheader()
        writer.writerow(
            {
                "pdf_path": "pdf/paper.pdf",
                "doi": "10.1234/example",
                "title": "A test paper",
                "journal": "Journal of Tests",
            }
        )

    runner, harness = _runner(tmp_path)
    summary = runner.run_manifest(manifest)

    assert summary.discovered == 1
    assert summary.attempted == 1
    assert summary.committed == 1
    assert summary.failed == 0
    assert harness.calls == [
        (
            pdf.resolve(),
            {
                "doi": "10.1234/example",
                "title": "A test paper",
                "journal": "Journal of Tests",
            },
        )
    ]


def test_run_manifest_metadata_and_limit(tmp_path: Path) -> None:
    pdf1 = tmp_path / "paper1.pdf"
    pdf2 = tmp_path / "paper2.pdf"
    pdf1.write_bytes(b"%PDF-1")
    pdf2.write_bytes(b"%PDF-2")
    manifest = tmp_path / "manifest.csv"
    with manifest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["pdf_path", "doi", "title", "pmid"])
        writer.writeheader()
        writer.writerows(
            [
                {"pdf_path": str(pdf1), "doi": "10.1/one", "title": "One", "pmid": "1"},
                {"pdf_path": str(pdf2), "doi": "10.1/two", "title": "Two", "pmid": "2"},
            ]
        )

    runner, harness = _runner(tmp_path)
    summary = runner.run_manifest(manifest, limit=1)

    assert summary.discovered == 1
    assert summary.attempted == 1
    assert summary.committed == 1
    assert summary.failed == 0
    assert harness.calls == [
        (pdf1.resolve(), {"doi": "10.1/one", "title": "One", "pmid": "1"})
    ]
    event = json.loads(
        (tmp_path / "events" / "reports.jsonl").read_text(encoding="utf-8")
    )
    assert event["bibliographic_metadata"]["doi"] == "10.1/one"


def test_resume_skips_committed_documents(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-fake")
    runner, harness = _runner(tmp_path)

    first = runner.run_paths([pdf], source_label="test")
    second = runner.run_paths([pdf], source_label="test")

    assert first.attempted == 1
    assert first.committed == 1
    assert second.attempted == 0
    assert second.skipped_committed == 1
    assert len(harness.calls) == 1


def test_missing_manifest_pdf_fails_before_model_calls(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.csv"
    with manifest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["pdf_path"])
        writer.writeheader()
        writer.writerow({"pdf_path": "missing.pdf"})

    runner, harness = _runner(tmp_path)
    with pytest.raises(FileNotFoundError, match="missing PDFs"):
        runner.run_manifest(manifest)
    assert harness.calls == []
