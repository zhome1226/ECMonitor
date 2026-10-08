"""Process RSS memory telemetry for bounded-memory retrieval execution."""

from __future__ import annotations

import gc
import time
import tracemalloc
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import Any

import psutil

from ecmonitor.retrieval_specialist.operators.time_utils import utc_now_iso
from ecmonitor.retrieval_specialist.storage.atomic_io import append_jsonl


def process_rss_mb() -> float:
    """Return total process resident-set size in MB."""

    return float(psutil.Process().memory_info().rss / (1024.0 * 1024.0))


def python_traced_memory_mb() -> float:
    """Return Python allocation tracing memory in MB as a secondary diagnostic."""

    if not tracemalloc.is_tracing():
        tracemalloc.start()
    current, _peak = tracemalloc.get_traced_memory()
    return current / (1024.0 * 1024.0)


def resident_memory_mb() -> float:
    """Backward-compatible alias for process RSS in MB."""

    return process_rss_mb()


class MemoryTelemetry(AbstractContextManager["MemoryTelemetry"]):
    """Context manager that writes one RSS telemetry event per batch/operator."""

    def __init__(
        self,
        *,
        run_dir: Path,
        run_id: str,
        query_id: str,
        iteration: int,
        operator: str,
        batch_id: str,
    ) -> None:
        self.run_dir = run_dir
        self.run_id = run_id
        self.query_id = query_id
        self.iteration = iteration
        self.operator = operator
        self.batch_id = batch_id
        self.records_processed = 0
        self.bytes_written = 0
        self._start_time = 0.0
        self._rss_before_mb = 0.0
        self._rss_peak_mb = 0.0

    def __enter__(self) -> MemoryTelemetry:
        self._start_time = time.perf_counter()
        self._rss_before_mb = process_rss_mb()
        self._rss_peak_mb = self._rss_before_mb
        return self

    def add_records(self, count: int) -> None:
        self.records_processed += count
        self._rss_peak_mb = max(self._rss_peak_mb, process_rss_mb())

    def add_bytes(self, count: int) -> None:
        self.bytes_written += count

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        gc.collect()
        rss_after_mb = process_rss_mb()
        traced_mb = python_traced_memory_mb()
        self._rss_peak_mb = max(self._rss_peak_mb, rss_after_mb)
        telemetry = {
            "run_id": self.run_id,
            "query_id": self.query_id,
            "iteration": self.iteration,
            "operator": self.operator,
            "batch_id": self.batch_id,
            "records_processed": self.records_processed,
            "process_rss_before_mb": round(self._rss_before_mb, 3),
            "process_rss_peak_mb": round(self._rss_peak_mb, 3),
            "process_rss_after_cleanup_mb": round(rss_after_mb, 3),
            "python_traced_memory_mb": round(traced_mb, 3),
            "resident_memory_before_mb": round(self._rss_before_mb, 3),
            "resident_memory_peak_mb": round(self._rss_peak_mb, 3),
            "resident_memory_after_cleanup_mb": round(rss_after_mb, 3),
            "bytes_written": self.bytes_written,
            "duration_seconds": round(time.perf_counter() - self._start_time, 6),
            "cleanup_performed": True,
            "timestamp": utc_now_iso(),
        }
        append_jsonl(self.run_dir / "logs" / "memory_usage.jsonl", telemetry)


def release_iteration_memory(*objects: Any) -> None:
    """Force collection after iteration-scoped references have gone out of scope."""

    del objects
    gc.collect()
