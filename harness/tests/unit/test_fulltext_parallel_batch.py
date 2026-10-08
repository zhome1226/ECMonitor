"""Tests for the parallel library runner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ecmonitor.fulltext_extraction.batch import ConcurrencyGovernor, ParallelLibraryRunner
from ecmonitor.fulltext_extraction.models import DocumentRunReport


class FakeControlPlane:
    def __init__(self) -> None:
        self.committed: set[str] = set()
        self._lock_holder = None  # not used; kept simple

    def has_committed_path(self, pdf_path: Path) -> bool:
        return str(pdf_path.resolve()) in self.committed


class FakeHarness:
    def __init__(self, control_plane: FakeControlPlane) -> None:
        self.control_plane = control_plane
        self.calls: list[tuple[Path, dict[str, object] | None]] = []
        self.serializer = 0

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
            candidate_count=1,
            review_count=1,
            resolution_count=1,
            committed=True,
            output_path=None,
            bibliographic_metadata=bibliographic_metadata or {},
        )


def _runner(tmp_path: Path, workers: int = 3) -> tuple[ParallelLibraryRunner, FakeHarness]:
    control_plane = FakeControlPlane()
    harness = FakeHarness(control_plane)
    runner = ParallelLibraryRunner(
        harness,
        reports_jsonl=tmp_path / "events" / "reports.jsonl",
        max_workers=workers,
    )
    return runner, harness


def test_parallel_processes_all_documents(tmp_path: Path) -> None:
    pdfs = []
    for index in range(6):
        pdf = tmp_path / f"paper{index}.pdf"
        pdf.write_bytes(f"%PDF-{index}".encode())
        pdfs.append(pdf)

    runner, harness = _runner(tmp_path, workers=3)
    summary = runner.run_paths(pdfs, source_label="test")

    assert summary.discovered == 6
    assert summary.attempted == 6
    assert summary.committed == 6
    assert summary.failed == 0
    assert summary.skipped_committed == 0
    assert len(harness.calls) == 6
    assert summary.document_attempts == 6


def test_parallel_resume_skips_committed(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-fake")
    runner, harness = _runner(tmp_path, workers=2)

    first = runner.run_paths([pdf], source_label="test")
    second = runner.run_paths([pdf], source_label="test")

    assert first.committed == 1
    assert second.attempted == 0
    assert second.skipped_committed == 1
    assert len(harness.calls) == 1


def test_parallel_events_jsonl_contains_committed(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-fake")
    runner, _ = _runner(tmp_path, workers=2)
    runner.run_paths([pdf], source_label="test")

    lines = [
        json.loads(line)
        for line in (tmp_path / "events" / "reports.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert lines[0]["status"] == "committed"
    assert lines[0]["source_path"] == str(pdf.resolve())


def test_parallel_retry_then_success(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-fake")
    control_plane = FakeControlPlane()
    harness = FakeHarness(control_plane)

    original = harness.run_document

    def flaky(pdf_path, *, bibliographic_metadata=None):
        if len(harness.calls) == 0:
            harness.calls.append((pdf_path.resolve(), bibliographic_metadata))
            raise RuntimeError("model transport failed: read timed out")
        return original(pdf_path, bibliographic_metadata=bibliographic_metadata)

    harness.run_document = flaky  # type: ignore[method-assign]
    runner = ParallelLibraryRunner(
        harness,
        reports_jsonl=tmp_path / "events" / "reports.jsonl",
        max_document_attempts=3,
        max_workers=2,
    )
    summary = runner.run_paths([pdf], source_label="test")

    assert summary.committed == 1
    assert summary.failed == 0
    assert summary.retry_attempts == 1
    assert summary.document_attempts == 2
    assert summary.retried_documents == 1


# ---------------------------------------------------------------------------
# Adaptive concurrency governor (P1c)
# ---------------------------------------------------------------------------


def test_concurrency_governor_steps_down_and_grows() -> None:
    governor = ConcurrencyGovernor(cap=4, floor=1, success_streak_to_grow=3)
    assert governor.limit == 4
    governor.on_transport_strain()
    assert governor.limit == 3
    governor.on_transport_strain()
    assert governor.limit == 2
    governor.on_failure()
    assert governor.limit == 1  # floor
    governor.on_transport_strain()
    assert governor.limit == 1  # never below floor
    # three clean successes grow it back
    governor.on_success()
    governor.on_success()
    governor.on_success()
    assert governor.limit == 2
    governor.on_success()
    governor.on_success()
    governor.on_success()
    assert governor.limit == 3
    governor.on_success()
    governor.on_success()
    governor.on_success()
    assert governor.limit == 4  # capped at cap
    governor.on_success()
    assert governor.limit == 4


def test_concurrency_governor_validation() -> None:
    try:
        ConcurrencyGovernor(cap=0)
    except ValueError:
        pass
    else:
        raise AssertionError("cap=0 should raise")
    try:
        ConcurrencyGovernor(cap=2, floor=3)
    except ValueError:
        pass
    else:
        raise AssertionError("floor>cap should raise")


class StrainHarness:
    """Fails the first ``n`` documents with transport errors, then succeeds."""

    def __init__(self, control_plane: FakeControlPlane, strain_count: int) -> None:
        self.control_plane = control_plane
        self.strain_count = strain_count
        self.calls: list[Path] = []
        self.attempts = 0

    def run_document(
        self, pdf_path: Path, *, bibliographic_metadata: dict[str, Any] | None = None
    ) -> DocumentRunReport:
        resolved = pdf_path.resolve()
        self.calls.append(resolved)
        if len(self.calls) <= self.strain_count:
            self.attempts += 1
            raise RuntimeError("model transport failed: read timed out")
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
            candidate_count=1,
            review_count=1,
            resolution_count=1,
            committed=True,
            output_path=None,
            bibliographic_metadata=bibliographic_metadata or {},
        )


def test_adaptive_runner_handles_transport_strain_and_finishes(tmp_path: Path) -> None:
    pdfs = []
    for index in range(6):
        pdf = tmp_path / f"strain{index}.pdf"
        pdf.write_bytes(b"%PDF-fake")
        pdfs.append(pdf)
    control_plane = FakeControlPlane()
    harness = StrainHarness(control_plane, strain_count=2)
    runner = ParallelLibraryRunner(
        harness,
        reports_jsonl=tmp_path / "events" / "reports.jsonl",
        max_document_attempts=3,
        max_workers=3,
        concurrency_floor=1,
        concurrency_growth_streak=2,
    )
    summary = runner.run_paths(pdfs, source_label="test")
    assert summary.discovered == 6
    assert summary.committed == 6  # strained docs recovered via retry
    assert summary.failed == 0
    assert summary.retry_attempts == 2  # two docs hit transport strain once each
    assert summary.retried_documents == 2
