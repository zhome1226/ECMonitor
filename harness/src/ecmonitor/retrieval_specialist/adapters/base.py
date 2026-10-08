"""Adapter interfaces for metadata sources."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import asdict, dataclass
from typing import Protocol

from ecmonitor.retrieval_specialist.models import CanonicalQuery, RawRecord, SourceExecutionStatus


@dataclass(frozen=True)
class SourcePage:
    """Bounded page of source records."""

    source_name: str
    status: SourceExecutionStatus
    records: list[RawRecord]
    raw_result_count: int
    page_id: int
    next_cursor: str | None
    warnings: list[str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SourceResult:
    """Result bundle returned by a source adapter."""

    source_name: str
    status: SourceExecutionStatus
    records: list[RawRecord]
    raw_result_count: int
    scanned_result_count: int
    warnings: list[str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class SourceAdapter(Protocol):
    """Search interface implemented by all sources."""

    source_name: str

    def health_check(self) -> SourceExecutionStatus:
        """Return source health without running a full query."""

    def search(self, query: CanonicalQuery, limit: int) -> SourceResult:
        """Search a metadata source and return immutable raw records."""

    def iter_pages(
        self, query: CanonicalQuery, page_size: int, max_records: int
    ) -> Iterator[SourcePage]:
        """Yield bounded pages of immutable raw records."""
