"""Core dataclasses used by the Retrieval Specialist harness."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class SourceExecutionStatus(StrEnum):
    """Allowed source execution outcomes."""

    SOURCE_SUCCESS = "source_success"
    SOURCE_NO_RESULTS = "source_no_results"
    SOURCE_PARTIAL = "source_partial"
    SOURCE_RATE_LIMITED = "source_rate_limited"
    SOURCE_FAILED = "source_failed"
    SOURCE_NOT_RUN = "source_not_run"


@dataclass(frozen=True)
class CanonicalQuery:
    """Source-independent query representation."""

    query_id: str
    parent_query_id: str | None
    iteration: int
    date_from: str
    date_to: str
    document_types: list[str]
    emerging_contaminant_terms: list[str]
    surface_water_terms: list[str]
    monitoring_and_concentration_terms: list[str]
    candidate_expansion_terms: list[str] = field(default_factory=list)
    optional_context_terms: list[str] = field(default_factory=list)
    prohibited_or_rejected_terms: list[str] = field(default_factory=list)
    added_terms: list[str] = field(default_factory=list)
    removed_terms: list[str] = field(default_factory=list)
    modified_concept_blocks: list[str] = field(default_factory=list)
    change_rationale: str = "Initial protocol-derived query."
    evidence_for_change: list[str] = field(default_factory=list)
    expected_effect: str = "Retrieve emerging-contaminant surface-water monitoring records."
    query_schema_version: str = "0.1.0"
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RawRecord:
    """Immutable raw source metadata wrapper."""

    source_name: str
    source_record_id: str
    rank: int
    raw: dict[str, Any]
    retrieval_timestamp: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class NormalizedRecord:
    """Normalized metadata while preserving original raw fields separately."""

    global_record_id: str
    source_records: list[dict[str, Any]]
    doi: str | None
    normalized_doi: str | None
    pmid: str | None
    openalex_id: str | None
    semantic_scholar_id: str | None
    crossref_id: str | None
    title_original: str
    title_normalized: str
    abstract_original: str | None
    abstract_source: str | None
    keywords: list[str]
    authors: list[str]
    first_author: str | None
    publication_date: str | None
    publication_year: int | None
    journal_title: str | None
    issn: list[str]
    eissn: list[str]
    document_type: str | None
    language: str | None
    source_rank: int
    source_relevance_score: float | None
    retrieved_from: list[str]
    retrieval_timestamp: str
    raw_metadata_path: str | None
    normalization_version: str = "0.1.0"
    sampled_matrices: list[str] = field(default_factory=list)
    study_type: str | None = None
    has_real_field_sample: bool | None = None
    has_concentration_evidence: bool | None = None
    article_ec_scope: str = "uncertain"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ScreeningDecision:
    """Structured title/abstract screening decision."""

    global_record_id: str
    decision: str
    reason_codes: list[str]
    evidence_spans: list[str]
    article_ec_scope: str
    query_term_evidence: list[dict[str, Any]] = field(default_factory=list)
    screening_schema_version: str = "1.1.0"
    screening_decision_id: str = ""
    run_id: str = ""
    query_id: str = ""
    iteration: int = 0
    screening_pass: str = "SCREEN_PASS_2"
    confidence: float | None = 1.0
    article_type_ok: bool | None = None
    date_ok: bool | None = None
    scie_status: str = "unknown"
    emerging_contaminant_context: bool | None = None
    surface_water_sample: bool | None = None
    included_waterbody_types: list[str] = field(default_factory=list)
    excluded_sample_matrices_present: bool = False
    excluded_sample_matrices: list[str] = field(default_factory=list)
    water_treatment_plant_samples_present: bool = False
    mixed_eligible_ineligible_matrices: bool = False
    field_environmental_samples: bool | None = None
    concentration_evidence: str = "likely_but_not_explicit"
    study_type: str | None = None
    prompt_version: str = "mock-screener-v1.1"
    prompt_hash: str = "not_applicable"
    model_name: str = "none"
    model_version: str = "none"
    model_parameters: dict[str, Any] = field(default_factory=lambda: {"llm_enabled": False})
    raw_model_response_path: str | None = None
    screening_timestamp: str = ""
    decision_actor: str = "Retrieval Specialist"
    screening_version: str = "0.1.0"
    audit_status: str = "not_audited"
    audit_sampled: bool = False
    audit_batch_id: str | None = None
    manager_decision: str | None = None
    manager_reason: str | None = None
    screening_disagreement: bool = False
    original_agent_decision: str | None = None
    override_decision: str | None = None
    override_actor: str | None = None
    override_reason: str | None = None
    override_timestamp: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class QueryMetrics:
    """Deterministic query-evaluation metrics and score components."""

    run_id: str
    iteration: int
    query_id: str
    parent_query_id: str | None
    raw_result_count: int
    scanned_result_count: int
    known_record_count: int
    novel_record_count: int
    target_novel_n: int
    actual_novel_n: int
    target_reached: bool
    include_count: int
    exclude_count: int
    defer_count: int
    eligible_precision: float
    novel_precision_at_20: float
    novel_eligible_yield: int
    normalized_novel_eligible_yield: float
    marginal_relevant_yield: float
    novelty_rate: float
    cumulative_eligible_count: int
    retrospective_query_coverage: float
    cross_source_breadth: float
    metadata_completeness: float
    scope_diversity: float
    excluded_matrix_rate: float
    laboratory_study_rate: float
    no_concentration_rate: float
    duplicate_rate: float
    known_eligible_overlap_rate: float
    known_ineligible_overlap_rate: float
    query_complexity: float
    positive_score: float
    penalty_score: float
    total_score: float
    score_delta: float
    decision: str
    decision_reason: str
    saturation_status: str
    source_completeness: str
    timestamp: str
    code_commit_sha: str
    config_hash: str
    prompt_hash: str
    model_name: str
    model_parameters: dict[str, Any]
    download_requests_emitted: int = 0
    duplicate_handoffs_suppressed: int = 0
    pending_download_jobs_at_iteration_end: int = 0
    handoff_backpressure_status: str = "not_evaluated"
    evaluated_record_count: int = 0
    deferred_record_count: int = 0
    defer_rate: float = 0.0
    marginal_eligible_count: int = 0
    retrieval_source_breadth: float = 0.0
    eligible_source_breadth: float = 0.0
    eligible_waterbody_diversity: float = 0.0
    coverage_scope: str = "run"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
