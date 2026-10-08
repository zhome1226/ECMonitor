"""Protocols for replaceable PDF, chemical, extractor, and validator components."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from ecmonitor.fulltext_extraction.geocode import GeocodeResult
from ecmonitor.fulltext_extraction.models import (
    ChemicalResolution,
    EvidenceChunk,
    ParsedDocument,
    ValidationDecision,
)


class PdfParserAdapter(Protocol):
    parser_name: str

    def parse(self, pdf_path: Path, *, document_id: str, source_sha256: str) -> ParsedDocument:
        """Parse one PDF into evidence-addressable pages and blocks."""


class DocumentChunker(Protocol):
    def chunk(self, document: ParsedDocument) -> list[EvidenceChunk]:
        """Split one parsed document into evidence-addressable chunks."""


class GeocodeResolver(Protocol):
    def resolve(self, location: dict[str, Any] | None) -> GeocodeResult | None:
        """Resolve approximate coordinates for an extracted location."""

    def validate_admin_hierarchy(
        self, location: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Check country/admin consistency without blocking extraction."""


class ChemicalResolver(Protocol):
    resolver_name: str

    def resolve(self, raw_name: str) -> ChemicalResolution:
        """Return candidates without mutating the validated global registry."""


class CandidateExtractor(Protocol):
    extractor_name: str

    def extract(self, chunk: EvidenceChunk) -> list[dict[str, Any]]:
        """Extract schema-shaped candidates from one evidence chunk."""


class RetryAwareCandidateExtractor(Protocol):
    extractor_name: str

    def extract_with_feedback(
        self,
        chunk: EvidenceChunk,
        *,
        attempt: int,
        reason_codes: tuple[str, ...],
        failed_json_pointers: tuple[str, ...],
        requested_context: tuple[str, ...],
        failed_candidates: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
        prior_candidates: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Re-extract a chunk in a fresh request with bounded structured feedback.

        ``failed_candidates`` carries the candidate payloads whose validation requested retry (the repair
        targets) and ``prior_candidates`` the full previous candidate list, so the model can fill
        only the missing fields without regressing candidates that already passed. Adapters that
        predate these fields may simply ignore them.
        """


class EvidenceValidator(Protocol):
    validator_name: str

    def validate(
        self,
        candidate: dict[str, Any],
        *,
        chunk: EvidenceChunk,
        resolutions: tuple[ChemicalResolution, ...],
    ) -> ValidationDecision:
        """Validate evidence independently and return a state-transition proposal."""


class BatchEvidenceValidator(Protocol):
    validator_name: str

    def validate_batch(
        self,
        candidates: list[dict[str, Any]],
        *,
        chunk: EvidenceChunk,
        resolutions_by_candidate: list[tuple[ChemicalResolution, ...]],
    ) -> list[ValidationDecision]:
        """Validate all candidates from one chunk in one isolated request."""
