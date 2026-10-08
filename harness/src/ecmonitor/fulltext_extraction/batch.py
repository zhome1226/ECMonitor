"""Sequential library runner with one-document commit and cleanup barriers."""

from __future__ import annotations

import csv
import gc
import json
import os
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypedDict

from ecmonitor.fulltext_extraction.errors import (
    RETRYABLE_ERROR_CATEGORIES,
    TRANSPORT_STRESS_CATEGORIES,
    classify_document_error,
)
from ecmonitor.fulltext_extraction.harness import FulltextExtractionHarness


class _DocumentProcessingResult(TypedDict):
    events: list[dict[str, object]]
    counts: dict[str, int]


# Backwards-compatible aliases used by tests and older call sites.
_RETRYABLE_ERROR_CATEGORIES = RETRYABLE_ERROR_CATEGORIES
_TRANSPORT_STRESS_CATEGORIES = TRANSPORT_STRESS_CATEGORIES


class ConcurrencyGovernor:
    """Adaptive cap on the number of in-flight documents.

    Reacts to model transport strain instead of a fixed ``--workers``: when a document
    observes retryable transport/gateway errors it steps the in-flight limit down; after a
    streak of clean successes it steps back up toward the configured ceiling. Throughput
    stays high on a healthy gateway and the runner stops piling onto one that is already
    timing out.
    """

    def __init__(
        self,
        cap: int,
        *,
        floor: int = 1,
        success_streak_to_grow: int = 3,
    ) -> None:
        if cap < 1:
            raise ValueError("cap must be at least 1")
        if floor < 1 or floor > cap:
            raise ValueError("floor must be in [1, cap]")
        if success_streak_to_grow < 1:
            raise ValueError("success_streak_to_grow must be at least 1")
        self.cap = cap
        self.floor = floor
        self.success_streak_to_grow = success_streak_to_grow
        self.current = cap
        self._success_streak = 0

    @property
    def limit(self) -> int:
        return self.current

    def on_success(self) -> None:
        self._success_streak += 1
        if self._success_streak >= self.success_streak_to_grow and self.current < self.cap:
            self.current += 1
            self._success_streak = 0

    def on_transport_strain(self) -> None:
        self._success_streak = 0
        self.current = max(self.floor, self.current - 1)

    def on_failure(self) -> None:
        self._success_streak = 0
        self.current = max(self.floor, self.current - 1)


@dataclass(frozen=True, slots=True)
class LibraryRunSummary:
    pdf_dir: str
    discovered: int
    attempted: int
    committed: int
    skipped_committed: int
    failed: int
    reports_jsonl: str
    document_attempts: int
    retried_documents: int
    retry_attempts: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _load_manifest(
    manifest_path: Path,
) -> tuple[list[Path], dict[str, dict[str, object]]]:
    manifest_path = manifest_path.resolve()
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    metadata_by_path: dict[str, dict[str, object]] = {}
    with manifest_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("pilot manifest has no header")
        path_column = next(
            (
                name
                for name in ("pdf_path", "unified_pdf_path", "source_path")
                if name in reader.fieldnames
            ),
            None,
        )
        if path_column is None:
            raise ValueError("pilot manifest requires pdf_path, unified_pdf_path, or source_path")
        pdfs: list[Path] = []
        for row in reader:
            if not row.get(path_column):
                continue
            path = Path(row[path_column])
            if not path.is_absolute():
                path = manifest_path.parent / path
            path = path.resolve()
            pdfs.append(path)
            metadata_by_path[str(path)] = {
                key: row[key]
                for key in ("doi", "title", "journal", "pmid", "url")
                if row.get(key)
            }
    missing = [str(path) for path in pdfs if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "pilot manifest contains missing PDFs: " + ", ".join(missing[:10])
        )
    return pdfs, metadata_by_path


class SequentialLibraryRunner:
    """Process documents serially; no next document starts before cleanup of the previous one."""

    def __init__(
        self,
        harness: FulltextExtractionHarness,
        *,
        reports_jsonl: Path,
        continue_on_error: bool = True,
        max_document_attempts: int = 1,
        retry_backoff_seconds: float = 0.0,
    ) -> None:
        if max_document_attempts < 1:
            raise ValueError("max_document_attempts must be at least 1")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds may not be negative")
        self.harness = harness
        self.reports_jsonl = reports_jsonl
        self.continue_on_error = continue_on_error
        self.max_document_attempts = max_document_attempts
        self.retry_backoff_seconds = retry_backoff_seconds

    def run(
        self,
        pdf_dir: Path,
        *,
        limit: int | None = None,
        resume: bool = True,
    ) -> LibraryRunSummary:
        pdf_dir = pdf_dir.resolve()
        if not pdf_dir.is_dir():
            raise NotADirectoryError(pdf_dir)
        pdfs = sorted(path for path in pdf_dir.rglob("*.pdf") if path.is_file())
        return self.run_paths(pdfs, source_label=str(pdf_dir), limit=limit, resume=resume)

    def run_manifest(
        self, manifest_path: Path, *, limit: int | None = None, resume: bool = True
    ) -> LibraryRunSummary:
        pdfs, metadata_by_path = _load_manifest(manifest_path)
        return self.run_paths(
            pdfs,
            source_label=str(manifest_path.resolve()),
            limit=limit,
            resume=resume,
            metadata_by_path=metadata_by_path,
        )

    def run_paths(
        self,
        pdfs: list[Path],
        *,
        source_label: str,
        limit: int | None = None,
        resume: bool = True,
        metadata_by_path: dict[str, dict[str, object]] | None = None,
    ) -> LibraryRunSummary:
        if limit is not None:
            pdfs = pdfs[:limit]
        self.reports_jsonl.parent.mkdir(parents=True, exist_ok=True)
        attempted = committed = skipped = failed = 0
        document_attempts = retried_documents = retry_attempts = 0
        for pdf_path in pdfs:
            if resume and self.harness.control_plane.has_committed_path(pdf_path):
                skipped += 1
                self._append_event({"status": "skipped_committed", "source_path": str(pdf_path)})
                continue
            attempted += 1
            attempt_errors: list[dict[str, object]] = []
            document_was_retried = False
            for attempt_number in range(1, self.max_document_attempts + 1):
                document_attempts += 1
                try:
                    report = self.harness.run_document(
                        pdf_path,
                        bibliographic_metadata=(metadata_by_path or {}).get(str(pdf_path)),
                    )
                    committed += int(report.committed)
                    self._append_event(
                        {
                            "status": "committed",
                            **report.to_dict(),
                            "document_attempt_count": attempt_number,
                            "prior_attempt_errors": attempt_errors,
                        }
                    )
                    break
                except Exception as exc:
                    error = classify_document_error(exc)
                    attempt_errors.append(
                        {
                            "attempt": attempt_number,
                            "error_type": type(exc).__name__,
                            "error_category": error.category,
                            "retryable": error.retryable,
                            "error": str(exc)[:4000],
                        }
                    )
                    should_retry = error.retryable and attempt_number < self.max_document_attempts
                    if should_retry:
                        retry_attempts += 1
                        document_was_retried = True
                        gc.collect()
                        delay = self.retry_backoff_seconds * (2 ** (attempt_number - 1))
                        if delay:
                            time.sleep(delay)
                        continue
                    failed += 1
                    self._append_event(
                        {
                            "status": "failed",
                            "source_path": str(pdf_path),
                            "error_type": type(exc).__name__,
                            "error_category": error.category,
                            "retryable": error.retryable,
                            "error": str(exc)[:4000],
                            "document_attempt_count": attempt_number,
                            "attempt_errors": attempt_errors,
                        }
                    )
                    if not self.continue_on_error:
                        raise
                    break
                finally:
                    # A retry starts a fresh document session and fresh short-lived agents.
                    gc.collect()
            if document_was_retried:
                retried_documents += 1
        return LibraryRunSummary(
            pdf_dir=source_label,
            discovered=len(pdfs),
            attempted=attempted,
            committed=committed,
            skipped_committed=skipped,
            failed=failed,
            reports_jsonl=str(self.reports_jsonl),
            document_attempts=document_attempts,
            retried_documents=retried_documents,
            retry_attempts=retry_attempts,
        )

    def _append_event(self, payload: dict[str, object]) -> None:
        with self.reports_jsonl.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


class ParallelLibraryRunner:
    """Process documents concurrently while preserving one-document isolation.

    The dominant per-document cost is the model subprocess and network I/O, both of which
    release the GIL, so a bounded thread pool scales throughput without the pickling and
    spawn constraints of a process pool on Windows. Shared persistence components are
    thread-safe by construction (fresh SQLite connections per operation with WAL + busy
    timeout); events are serialized under a lock in the coordinating thread.
    """

    def __init__(
        self,
        harness: FulltextExtractionHarness,
        *,
        reports_jsonl: Path,
        continue_on_error: bool = True,
        max_document_attempts: int = 1,
        retry_backoff_seconds: float = 0.0,
        max_workers: int = 3,
        concurrency_floor: int = 1,
        concurrency_growth_streak: int = 3,
    ) -> None:
        if max_document_attempts < 1:
            raise ValueError("max_document_attempts must be at least 1")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds may not be negative")
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self.harness = harness
        self.reports_jsonl = reports_jsonl
        self.continue_on_error = continue_on_error
        self.max_document_attempts = max_document_attempts
        self.retry_backoff_seconds = retry_backoff_seconds
        self.max_workers = max_workers
        self.concurrency_floor = concurrency_floor
        self.concurrency_growth_streak = concurrency_growth_streak
        self._event_lock = threading.Lock()

    def run(
        self,
        pdf_dir: Path,
        *,
        limit: int | None = None,
        resume: bool = True,
    ) -> LibraryRunSummary:
        pdf_dir = pdf_dir.resolve()
        if not pdf_dir.is_dir():
            raise NotADirectoryError(pdf_dir)
        pdfs = sorted(path for path in pdf_dir.rglob("*.pdf") if path.is_file())
        return self.run_paths(pdfs, source_label=str(pdf_dir), limit=limit, resume=resume)

    def run_manifest(
        self, manifest_path: Path, *, limit: int | None = None, resume: bool = True
    ) -> LibraryRunSummary:
        pdfs, metadata_by_path = _load_manifest(manifest_path)
        return self.run_paths(
            pdfs,
            source_label=str(manifest_path.resolve()),
            limit=limit,
            resume=resume,
            metadata_by_path=metadata_by_path,
        )

    def run_paths(
        self,
        pdfs: list[Path],
        *,
        source_label: str,
        limit: int | None = None,
        resume: bool = True,
        metadata_by_path: dict[str, dict[str, object]] | None = None,
    ) -> LibraryRunSummary:
        if limit is not None:
            pdfs = pdfs[:limit]
        self.reports_jsonl.parent.mkdir(parents=True, exist_ok=True)
        skipped = 0
        tasks: list[tuple[Path, dict[str, object] | None]] = []
        for pdf_path in pdfs:
            if resume and self.harness.control_plane.has_committed_path(pdf_path):
                skipped += 1
                self._append_event({"status": "skipped_committed", "source_path": str(pdf_path)})
                continue
            tasks.append((pdf_path, (metadata_by_path or {}).get(str(pdf_path))))
        counts: dict[str, int] = {
            "attempted": len(tasks),
            "committed": 0,
            "failed": 0,
            "document_attempts": 0,
            "retried_documents": 0,
            "retry_attempts": 0,
        }
        if tasks:
            workers = min(self.max_workers, len(tasks))
            governor = ConcurrencyGovernor(
                cap=workers,
                floor=min(self.concurrency_floor, workers),
                success_streak_to_grow=self.concurrency_growth_streak,
            )
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="doc") as executor:
                pending: dict[
                    Future[_DocumentProcessingResult], tuple[Path, dict[str, object] | None]
                ] = {}
                task_iter = iter(tasks)

                def _submit_upto() -> None:
                    while len(pending) < governor.limit:
                        item = next(task_iter, None)
                        if item is None:
                            break
                        pdf_path, metadata = item
                        pending[
                            executor.submit(self._process_document, pdf_path, metadata)
                        ] = item

                _submit_upto()
                while pending:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        pending.pop(future)
                        # future.result() propagates an abort when continue_on_error is False.
                        result = future.result()
                        for event in result["events"]:
                            self._append_event(event)
                        for key, value in result["counts"].items():
                            if key != "transport_retries":
                                counts[key] += value
                        transport_retries = result["counts"].get("transport_retries", 0)
                        if result["counts"]["failed"] > 0:
                            governor.on_failure()
                        elif transport_retries > 0:
                            governor.on_transport_strain()
                        else:
                            governor.on_success()
                    _submit_upto()
        return LibraryRunSummary(
            pdf_dir=source_label,
            discovered=len(pdfs),
            attempted=counts["attempted"],
            committed=counts["committed"],
            skipped_committed=skipped,
            failed=counts["failed"],
            reports_jsonl=str(self.reports_jsonl),
            document_attempts=counts["document_attempts"],
            retried_documents=counts["retried_documents"],
            retry_attempts=counts["retry_attempts"],
        )

    def _process_document(
        self, pdf_path: Path, metadata: dict[str, object] | None
    ) -> _DocumentProcessingResult:
        events: list[dict[str, object]] = []
        counts: dict[str, int] = {
            "committed": 0,
            "failed": 0,
            "document_attempts": 0,
            "retried_documents": 0,
            "retry_attempts": 0,
            "transport_retries": 0,
        }
        attempt_errors: list[dict[str, object]] = []
        document_was_retried = False
        for attempt_number in range(1, self.max_document_attempts + 1):
            counts["document_attempts"] += 1
            try:
                report = self.harness.run_document(pdf_path, bibliographic_metadata=metadata)
                counts["committed"] += int(report.committed)
                events.append(
                    {
                        "status": "committed",
                        **report.to_dict(),
                        "document_attempt_count": attempt_number,
                        "prior_attempt_errors": attempt_errors,
                    }
                )
                break
            except Exception as exc:
                error = classify_document_error(exc)
                attempt_errors.append(
                    {
                        "attempt": attempt_number,
                        "error_type": type(exc).__name__,
                        "error_category": error.category,
                        "retryable": error.retryable,
                        "error": str(exc)[:4000],
                    }
                )
                should_retry = error.retryable and attempt_number < self.max_document_attempts
                if should_retry:
                    counts["retry_attempts"] += 1
                    if error.category in _TRANSPORT_STRESS_CATEGORIES:
                        counts["transport_retries"] += 1
                    document_was_retried = True
                    gc.collect()
                    delay = self.retry_backoff_seconds * (2 ** (attempt_number - 1))
                    if delay:
                        time.sleep(delay)
                    continue
                counts["failed"] += 1
                events.append(
                    {
                        "status": "failed",
                        "source_path": str(pdf_path),
                        "error_type": type(exc).__name__,
                        "error_category": error.category,
                        "retryable": error.retryable,
                        "error": str(exc)[:4000],
                        "document_attempt_count": attempt_number,
                        "attempt_errors": attempt_errors,
                    }
                )
                if not self.continue_on_error:
                    raise
                break
            finally:
                gc.collect()
        if document_was_retried:
            counts["retried_documents"] += 1
        return {"events": events, "counts": counts}

    def _append_event(self, payload: dict[str, object]) -> None:
        with self._event_lock, self.reports_jsonl.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
