"""Mock source adapter used for Phase 1 harness validation."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from ecmonitor.retrieval_specialist.adapters.base import SourcePage, SourceResult
from ecmonitor.retrieval_specialist.models import CanonicalQuery, RawRecord, SourceExecutionStatus
from ecmonitor.retrieval_specialist.operators.time_utils import utc_now_iso
from ecmonitor.retrieval_specialist.storage.atomic_io import read_json


class MockSourceAdapter:
    """Deterministic adapter backed by a JSON fixture."""

    def __init__(self, source_name: str, fixture_path: Path) -> None:
        self.source_name = source_name
        self.fixture_path = fixture_path

    def health_check(self) -> SourceExecutionStatus:
        if self.fixture_path.exists():
            return SourceExecutionStatus.SOURCE_SUCCESS
        return SourceExecutionStatus.SOURCE_FAILED

    def search(self, query: CanonicalQuery, limit: int) -> SourceResult:
        del query
        payload = read_json(self.fixture_path)
        source_records = payload.get(self.source_name, [])
        raw_records: list[RawRecord] = []
        for rank, record in enumerate(source_records[:limit], start=1):
            raw_records.append(
                RawRecord(
                    source_name=self.source_name,
                    source_record_id=str(record["source_record_id"]),
                    rank=rank,
                    raw=dict(record),
                    retrieval_timestamp=utc_now_iso(),
                )
            )
        status = (
            SourceExecutionStatus.SOURCE_SUCCESS
            if raw_records
            else SourceExecutionStatus.SOURCE_NO_RESULTS
        )
        return SourceResult(
            source_name=self.source_name,
            status=status,
            records=raw_records,
            raw_result_count=len(source_records),
            scanned_result_count=len(raw_records),
            warnings=[],
        )

    def iter_pages(
        self, query: CanonicalQuery, page_size: int, max_records: int
    ) -> Iterator[SourcePage]:
        del query
        payload = read_json(self.fixture_path)
        source_records = list(payload.get(self.source_name, []))
        raw_result_count = len(source_records)
        emitted = 0
        page_id = 0
        while emitted < min(max_records, raw_result_count):
            page_id += 1
            chunk = source_records[emitted : emitted + page_size]
            records = [
                RawRecord(
                    source_name=self.source_name,
                    source_record_id=str(record["source_record_id"]),
                    rank=emitted + offset,
                    raw=dict(record),
                    retrieval_timestamp=utc_now_iso(),
                )
                for offset, record in enumerate(chunk, start=1)
            ]
            emitted += len(records)
            yield SourcePage(
                source_name=self.source_name,
                status=SourceExecutionStatus.SOURCE_SUCCESS
                if records
                else SourceExecutionStatus.SOURCE_NO_RESULTS,
                records=records,
                raw_result_count=raw_result_count,
                page_id=page_id,
                next_cursor=str(emitted) if emitted < min(max_records, raw_result_count) else None,
                warnings=[],
            )
