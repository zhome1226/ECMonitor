"""Adapter interfaces and built-in implementations."""

from ecmonitor.fulltext_extraction.adapters.base import (
    CandidateExtractor,
    ChemicalResolver,
    EvidenceValidator,
    PdfParserAdapter,
    RetryAwareCandidateExtractor,
)
from ecmonitor.fulltext_extraction.adapters.deterministic_table import (
    DeterministicTableCandidateExtractor,
)
from ecmonitor.fulltext_extraction.adapters.json_command import (
    JsonCommandCandidateExtractor,
    JsonCommandEvidenceValidator,
)

__all__ = [
    "CandidateExtractor",
    "DeterministicTableCandidateExtractor",
    "ChemicalResolver",
    "EvidenceValidator",
    "JsonCommandCandidateExtractor",
    "JsonCommandEvidenceValidator",
    "PdfParserAdapter",
    "RetryAwareCandidateExtractor",
]
