"""Immutable contracts shared by full-text extraction adapters."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class BoundingBox:
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(frozen=True, slots=True)
class TextBlock:
    block_id: str
    page_number: int
    text: str
    bbox: BoundingBox | None = None
    block_type: str = "text"
    reading_order: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ParsedPage:
    page_number: int
    width: float
    height: float
    blocks: tuple[TextBlock, ...]

    @property
    def text(self) -> str:
        return "\n".join(block.text for block in self.blocks if block.text.strip())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    document_id: str
    source_path: Path
    source_sha256: str
    parser_name: str
    parser_version: str
    pages: tuple[ParsedPage, ...]
    warnings: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def text_character_count(self) -> int:
        return sum(len(page.text) for page in self.pages)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["source_path"] = str(self.source_path)
        return payload


@dataclass(frozen=True, slots=True)
class EvidenceChunk:
    chunk_id: str
    document_id: str
    ordinal: int
    chunk_type: str
    text: str
    page_start: int
    page_end: int
    source_block_ids: tuple[str, ...]
    section_path: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ChemicalMatch:
    source: str
    source_record_id: str
    canonical_name: str
    matched_alias: str
    pubchem_cid: str | None = None
    cas_candidates: tuple[str, ...] = ()
    inchi: str | None = None
    inchikey: str | None = None
    canonical_smiles: str | None = None
    molecular_formula: str | None = None
    synonyms: tuple[str, ...] = ()
    raw_payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


ResolutionStatus = Literal["validated_local", "resolved", "ambiguous", "not_found", "error"]


@dataclass(frozen=True, slots=True)
class ChemicalResolution:
    raw_name: str
    normalized_query: str
    status: ResolutionStatus
    resolver_name: str
    matches: tuple[ChemicalMatch, ...] = ()
    warnings: tuple[str, ...] = ()
    registry_snapshot_version: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


TerminalStatus = Literal[
    "accepted_main",
    "accepted_censored",
    "accepted_tentative",
    "accepted_microplastic_surface_water",
    "rejected_scope",
    "rejected_secondary_source",
    "rejected_treatment_experiment",
    "rejected_non_observation",
    "rejected_non_individual",
    "rejected_source_conflict",
    "rejected_duplicate",
    "deferred_identity_evidence",
    "deferred_source_binding",
    "deferred_matrix_binding",
    "deferred_geographic_conflict",
    "model_failure",
    "parser_failure",
    "validator_failure",
    "invalid_model_output",
    "completed_zero_in_scope_records",
]

ValidationAction = Literal["accept", "retry", "escalate", "reject"]


@dataclass(frozen=True, slots=True)
class ValidationDecision:
    action: ValidationAction
    reason_codes: tuple[str, ...]
    failed_json_pointers: tuple[str, ...] = ()
    requested_context: tuple[str, ...] = ()
    human_review_required: bool = False
    pilot_accept_eligible: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RetryEvent:
    retry_id: str
    chunk_id: str
    attempt: int
    reason_codes: tuple[str, ...]
    failed_json_pointers: tuple[str, ...] = ()
    requested_context: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ObservationDisposition:
    record_id: str
    candidate_id: str
    disposition: Literal["accepted", "rejected", "pending_human_review"]
    canonical_name: str | None
    reported_name: str | None
    replacement_name: str | None
    payload: dict[str, Any]
    terminal_status: TerminalStatus = "accepted_main"
    output_stream: str = "accepted_observations"
    policy_rule_id: str = "OBS-01"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class HumanReviewTask:
    task_id: str
    candidate_id: str
    priority: Literal["low", "medium", "high", "critical"]
    reason_codes: tuple[str, ...]
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DocumentRunReport:
    document_id: str
    document_session_id: str
    source_path: str
    source_sha256: str
    registry_snapshot_version: int
    parser_name: str
    parser_version: str
    page_count: int
    chunk_count: int
    candidate_count: int
    review_count: int
    resolution_count: int
    committed: bool
    output_path: str | None
    accepted_count: int = 0
    rejected_count: int = 0
    pending_human_review_count: int = 0
    retry_count: int = 0
    warnings: tuple[str, ...] = ()
    bibliographic_metadata: dict[str, Any] = field(default_factory=dict)
    terminal_status_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
