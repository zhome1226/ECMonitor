"""Per-document orchestration, retry control, and commit barrier."""

from __future__ import annotations

import copy
import hashlib
import inspect
import re
import shutil
import tempfile
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeVar

from ecmonitor.fulltext_extraction.adapters.base import (
    CandidateExtractor,
    ChemicalResolver,
    DocumentChunker,
    EvidenceValidator,
    GeocodeResolver,
    PdfParserAdapter,
)
from ecmonitor.fulltext_extraction.chunking import StructuralChunker
from ecmonitor.fulltext_extraction.errors import (
    classify_document_error,
    is_transport_stress_category,
)
from ecmonitor.fulltext_extraction.evidence import (
    concentration_hit_spans,
    evidence_anchor_reason,
    fold_for_evidence,
    normalize_parsed,
)
from ecmonitor.fulltext_extraction.models import (
    ChemicalMatch,
    ChemicalResolution,
    DocumentRunReport,
    EvidenceChunk,
    HumanReviewTask,
    ObservationDisposition,
    RetryEvent,
    ValidationDecision,
)
from ecmonitor.fulltext_extraction.policy import (
    coarse_disposition_for_terminal,
    is_microplastic_surface_water_observation,
    materialize_censoring,
    terminal_requires_human_review,
)
from ecmonitor.fulltext_extraction.quality import (
    ConservativeEvidenceValidator,
    chemical_name_fields,
    looks_non_individual,
    looks_out_of_scope_water_quality,
    materialize_chemical_identity,
    retry_is_repairable_by_extraction,
)
from ecmonitor.fulltext_extraction.registry import ChemicalRegistry
from ecmonitor.fulltext_extraction.storage import FulltextControlPlane
from ecmonitor.retrieval_specialist.storage.atomic_io import write_json_atomic
from ecmonitor.validation_specialist.service import finalize_candidate_validation


class NoopCandidateExtractor:
    extractor_name = "noop_candidate_extractor"

    def extract(self, chunk: EvidenceChunk) -> list[dict[str, Any]]:
        del chunk
        return []


_ComponentT = TypeVar("_ComponentT")


@dataclass
class _ChunkResult:
    """Everything one chunk produced, merged by the caller in chunk order."""

    candidate_rows: list[tuple[str, str, dict[str, Any]]]
    resolution_rows: list[tuple[str | None, dict[str, Any]]]
    review_rows: list[tuple[str, str, ValidationDecision]]
    retry_events: list[RetryEvent]
    observation_records: list[ObservationDisposition]
    human_review_tasks: list[HumanReviewTask]
    proposal_resolutions: dict[str, ChemicalResolution]

@dataclass
class _ChunkBatchResult:
    """Aggregate of many per-chunk results for one extraction pass of a document."""

    candidate_rows: list[tuple[str, str, dict[str, Any]]]
    resolution_rows: list[tuple[str | None, dict[str, Any]]]
    review_rows: list[tuple[str, str, ValidationDecision]]
    retry_events: list[RetryEvent]
    observation_records: list[ObservationDisposition]
    human_review_tasks: list[HumanReviewTask]
    proposal_resolutions: dict[str, ChemicalResolution]


def _is_transport_stress(exc: Exception) -> bool:
    """True for retryable gateway/transport failures that whole-doc fallback can dodge."""
    classified = classify_document_error(exc)
    return classified.retryable and is_transport_stress_category(classified.category)


def _is_truncated_model_response(exc: Exception) -> bool:
    """True when a dense whole-document answer exhausted its completion budget."""
    message = str(exc).casefold()
    return "finish_reason=length" in message or "model response was truncated" in message


def _is_invalid_model_response(exc: Exception) -> bool:
    """Retry malformed whole-document responses with bounded evidence chunks."""
    classified = classify_document_error(exc)
    message = str(exc).casefold()
    return classified.retryable and (
        classified.category == "model_response_invalid" or any(marker in message for marker in (
            "model content was not valid json", "model response failed local validation",
            "model response is not valid json",
        ))
    )


def _is_validator_failure(exc: Exception) -> bool:
    """Keep validator failures from re-running an already successful extraction pass."""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ == "JsonCommandValidatorError":
            return True
        current = current.__cause__ or current.__context__
    return False


_MICROPLASTIC_ONLY_TITLE_RE = re.compile(
    r"(?i)\b(?:microplastics?|macroplastics?|plastic\s+litter|plastic\s+pollution|"
    r"plastic\s+debris|debris\s+rafts?)\b"
)
_CHEMICAL_CO_CONTAMINANT_RE = re.compile(
    r"(?i)\b(?:metal(?:s|loid)?|element(?:s)?|contaminant(?:s)?|pollutant(?:s)?|"
    r"chemical(?:s)?|analyte(?:s)?|pesticide(?:s)?|pfas|pfoa|pfos|edc(?:s)?|atrazine|"
    r"pharmaceutical(?:s)?|antibiotic(?:s)?|hormone(?:s)?)\b"
)
_WATER_CONTEXT_RE = re.compile(
    r"(?i)\b(?:surface\s+water|river|stream|lake|reservoir|canal|water\s+column|"
    r"freshwater|groundwater|estuary|coastal\s+water)\b"
)
_OCCURRENCE_SIGNAL_RE = re.compile(
    r"(?i)\b(?:concentration(?:s)?|detected|quantified|occurrence|monitoring|"
    r"sampling|measured|mean|median|maximum|minimum|range|ng\s*/\s*l|"
    r"µg\s*/\s*l|μg\s*/\s*l|ug\s*/\s*l|mg\s*/\s*l)\b"
)


def _whole_doc_empty_fallback_decision(
    chunks: list[EvidenceChunk],
    bibliographic_metadata: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Cheap local gate for an empty whole-document extraction.

    The whole-document call is already the high-recall pass. Re-reading every chunk after an
    empty answer is useful for dense chemical papers, but wasteful for papers whose subject is
    only plastic-particle abundance or litter. This gate intentionally has one conservative
    negative rule (obvious plastic-only titles) and otherwise requires a water/occurrence signal
    before allowing the expensive fallback.
    """
    metadata = bibliographic_metadata or {}
    title = str(metadata.get("title") or "")
    body = "\n".join(chunk.text or "" for chunk in chunks)
    title_or_body = f"{title}\n{body}"

    plastic_only_title = bool(_MICROPLASTIC_ONLY_TITLE_RE.search(title)) and not bool(
        _CHEMICAL_CO_CONTAMINANT_RE.search(title)
    )
    if plastic_only_title:
        return False, "microplastic_or_plastic_only_title"

    has_water = bool(_WATER_CONTEXT_RE.search(title_or_body))
    has_occurrence = bool(_OCCURRENCE_SIGNAL_RE.search(title_or_body))
    has_chemical = bool(_CHEMICAL_CO_CONTAMINANT_RE.search(title_or_body))
    has_concentration = any(concentration_hit_spans(chunk.text or "") for chunk in chunks)

    if has_water and has_chemical and (has_occurrence or has_concentration):
        return True, "water_chemical_occurrence_signal"
    if has_water and has_chemical and title:
        return True, "water_chemical_title_signal"
    return False, "no_local_occurrence_likelihood_signal"


def _fallback_chunks(
    chunks: list[EvidenceChunk],
    *,
    chunk_selector: Callable[[EvidenceChunk], bool] | None,
) -> list[EvidenceChunk]:
    """Prefer locally promising chunks when whole-document extraction is empty."""
    if chunk_selector is not None:
        selected = [chunk for chunk in chunks if chunk_selector(chunk)]
        if selected:
            return selected
    selected = [chunk for chunk in chunks if concentration_hit_spans(chunk.text or "")]
    return selected or chunks


def _run_without_direct_pdf(
    extractor: CandidateExtractor,
    callback: Callable[[], _ChunkBatchResult],
) -> _ChunkBatchResult:
    """Run a chunk fallback with local text only, not another copy of the complete PDF."""
    if not hasattr(extractor, "pdf_path"):
        return callback()
    original_pdf_path = extractor.pdf_path
    extractor.pdf_path = None
    try:
        return callback()
    finally:
        extractor.pdf_path = original_pdf_path


def _dedupe_payloads(payloads: list[dict[str, Any]], *, cap: int = 120) -> list[dict[str, Any]]:
    """Drop exact-duplicate candidate payloads before resolution/validation.

    Reasoning models frequently emit the same observation multiple times (prose + table,
    repeated sentences). PubChem resolution and the batch validator both scale with candidate
    count, so deduplicating identical (analyte, evidence) payloads removes the biggest
    downstream cost without losing distinct observations. Fingerprints are case/space folded.
    """
    if not payloads:
        return []
    seen: set[str] = set()
    seen_table_cells: set[str] = set()
    result: list[dict[str, Any]] = []
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        fingerprint = _payload_fingerprint(payload)
        table_cell_fingerprint = _table_cell_fingerprint(payload)
        if fingerprint in seen or (
            table_cell_fingerprint is not None and table_cell_fingerprint in seen_table_cells
        ):
            continue
        seen.add(fingerprint)
        if table_cell_fingerprint is not None:
            seen_table_cells.add(table_cell_fingerprint)
        result.append(payload)
        if len(result) >= cap:
            break
    return result


def _payload_fingerprint(payload: dict[str, Any]) -> str:
    import json as _json

    analyte = payload.get("analyte")
    evidence = payload.get("evidence")
    def fold(value: Any) -> str:
        if isinstance(value, str):
            return " ".join(value.split()).casefold()
        if isinstance(value, dict):
            return _json.dumps({k: fold(v) for k, v in value.items()}, sort_keys=True, ensure_ascii=True)
        if isinstance(value, list):
            return _json.dumps([fold(v) for v in value], ensure_ascii=True)
        return str(value)
    return fold({"analyte": analyte, "evidence": evidence})


def _table_cell_fingerprint(payload: dict[str, Any]) -> str | None:
    """Identify duplicate emissions of the same explicitly anchored table cell.

    Quotes from a reasoning model may differ only by how much neighboring flattened-table text
    was copied. When table ID, row label, and column label are all present, those stable anchors
    plus the structured observation identify the cell more safely than the free-text quote.
    """
    import json as _json

    evidence = payload.get("evidence")
    if not isinstance(evidence, dict):
        return None
    anchors = {
        key: evidence.get(key)
        for key in ("table_id", "row_label", "column_label", "page_start", "page_end")
    }
    required_anchors = ("table_id", "row_label", "column_label")
    if not all(anchors.get(key) not in {None, ""} for key in required_anchors):
        return None

    analyte = payload.get("analyte") or {}
    stable = {
        "analyte": {
            key: analyte.get(key)
            for key in ("raw_name", "reported_name", "proposed_canonical_name")
        },
        "result": payload.get("result"),
        "sample": payload.get("sample"),
        "location": payload.get("location"),
        "sampling_time": payload.get("sampling_time"),
        "anchors": anchors,
    }

    def fold(value: Any) -> Any:
        if isinstance(value, str):
            return " ".join(value.split()).casefold()
        if isinstance(value, dict):
            return {key: fold(item) for key, item in value.items()}
        if isinstance(value, list):
            return [fold(item) for item in value]
        return value

    return _json.dumps(fold(stable), sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _merge_chunks_for_extraction(
    chunks: list[EvidenceChunk],
    *,
    document_id: str,
    max_chars: int,
) -> EvidenceChunk | None:
    """Collapse selected chunks into one evidence body for a single model call.

    Each source chunk is delimited with its ordinal and page range so the extractor can still
    attribute facts to a page, and the validator still sees the full document context. Returns
    None (caller falls back to per-chunk mode) when the merged body would exceed ``max_chars``.
    """
    total = sum(len(chunk.text) for chunk in chunks)
    if total > max_chars:
        return None
    parts = []
    for chunk in chunks:
        parts.append(
            f"=== CHUNK ordinal={chunk.ordinal} chunk_id={chunk.chunk_id} "
            f"pages={chunk.page_start}-{chunk.page_end} ===\n{chunk.text}"
        )
    return EvidenceChunk(
        chunk_id=f"merged-{document_id}",
        document_id=document_id,
        ordinal=max(chunk.ordinal for chunk in chunks) + 1,
        chunk_type="merged_document",
        text="\n\n".join(parts),
        page_start=min(chunk.page_start for chunk in chunks),
        page_end=max(chunk.page_end for chunk in chunks),
        source_block_ids=tuple(chunk.chunk_id for chunk in chunks),
        warnings=("merged_chunks_for_extraction",),
    )


_BUNDLE_CONTEXT_RE = re.compile(
    r"(?i)\b(?:abstract|introduction|materials?\s+and\s+methods?|methods?|sampling|sample|"
    r"site|location|study\s+area|river|lake|water\s+body|collected|collection|"
    r"analysis|analytical|instrument|lc[-\s]?ms|gc[-\s]?ms|icp[-\s]?ms)\b"
)


def _bundle_context_chunks(chunks: list[EvidenceChunk], *, max_chunks: int = 4) -> list[EvidenceChunk]:
    """Choose a small, high-value context set for bounded document bundles."""
    if not chunks:
        return []
    ranked: list[tuple[int, int, EvidenceChunk]] = []
    for chunk in chunks:
        text = chunk.text or ""
        early_bonus = 0 if chunk.ordinal < 2 else 1
        signal_bonus = 0 if _BUNDLE_CONTEXT_RE.search(text) else 1
        ranked.append((early_bonus + signal_bonus, chunk.ordinal, chunk))
    ranked.sort(key=lambda item: (item[0], item[1]))
    selected = [item[2] for item in ranked[:max_chunks]]
    return sorted(selected, key=lambda chunk: chunk.ordinal)


def _truncate_bundle_text(text: str, max_chars: int) -> str:
    """Bound a context/source excerpt while retaining both its beginning and ending.

    Bundle sizing is based on Python string length, so this helper is deliberately strict: the
    returned value is never longer than ``max_chars``.  Keeping both ends is useful for source
    chunks because table headers/method labels are often near the beginning while units, values,
    and footnotes are often near the end.
    """
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    marker = "\n[excerpt truncated]\n"
    if max_chars <= len(marker):
        return text[:max_chars]
    available = max_chars - len(marker)
    head = (available + 1) // 2
    tail = available - head
    return text[:head] + marker + text[-tail:] if tail else text[:head] + marker


def _serialize_bundle_context(chunks: list[EvidenceChunk], *, max_chars: int) -> str:
    """Serialize bounded context with a hard upper bound on the returned string."""
    if max_chars <= 0 or not chunks:
        return ""
    parts: list[str] = []
    used = 0
    for chunk in chunks:
        part = (
            f"=== SHARED DOCUMENT CONTEXT chunk_id={chunk.chunk_id} "
            f"pages={chunk.page_start}-{chunk.page_end} ===\n{chunk.text}"
        )
        separator = 2 if parts else 0
        remaining = max_chars - used - separator
        if remaining <= 0:
            break
        bounded = _truncate_bundle_text(part, remaining)
        if not bounded:
            break
        parts.append(bounded.rstrip())
        used = len("\n\n".join(parts))
        if len(part) > remaining:
            break
    result = "\n\n".join(parts)
    # Defensive bound: never let context serialization violate the budget used by the packer.
    return result[:max_chars]


def _bundle_header(bundle_index: int) -> str:
    return f"=== CURRENT BUNDLE {bundle_index + 1} ===\n"


def _bundle_source_header(chunk: EvidenceChunk) -> str:
    return (
        f"=== BUNDLE SOURCE chunk_id={chunk.chunk_id} "
        f"pages={chunk.page_start}-{chunk.page_end} ===\n"
    )


def _windowize_source_chunks(
    chunks: list[EvidenceChunk],
    *,
    max_chars: int,
    context_chars: int,
    overlap_chars: int = 500,
) -> list[EvidenceChunk]:
    """Split oversized parsed chunks into contiguous windows instead of head/tail excerpts.

    Flattened PDF tables commonly put analyte names and values in the middle of a large parsed
    chunk. The legacy bounded-bundle path kept only the beginning and end, silently discarding
    those rows. Windowing preserves every character (with a small overlap) and keeps the original
    chunk/page/source-block lineage.
    """
    if not chunks:
        return []
    context_budget = min(max(0, context_chars), max_chars // 3)
    source_header_budget = max(len(_bundle_source_header(chunk)) for chunk in chunks)
    body_budget = max(256, max_chars - context_budget - len(_bundle_header(len(chunks))) - source_header_budget - 4)
    stride = max(1, body_budget - max(0, overlap_chars))
    result: list[EvidenceChunk] = []
    for chunk in chunks:
        text = chunk.text or ""
        if len(text) <= body_budget:
            result.append(chunk)
            continue
        start = 0
        window_index = 0
        while start < len(text):
            end = min(len(text), start + body_budget)
            result.append(
                EvidenceChunk(
                    chunk_id=f"{chunk.chunk_id}:window-{window_index}",
                    document_id=chunk.document_id,
                    ordinal=chunk.ordinal,
                    chunk_type="source_window",
                    text=text[start:end],
                    page_start=chunk.page_start,
                    page_end=chunk.page_end,
                    source_block_ids=chunk.source_block_ids,
                    section_path=chunk.section_path,
                    warnings=tuple(chunk.warnings) + ("contiguous_source_window",),
                )
            )
            if end >= len(text):
                break
            start += stride
            window_index += 1
    return result


def _split_chunks_for_extraction(
    chunks: list[EvidenceChunk],
    *,
    document_id: str,
    max_chars: int,
    context_chars: int = 12_000,
    windowed_source_chunks: bool = False,
) -> list[EvidenceChunk]:
    """Build bounded multi-chunk bundles with repeated document context.

    This is the middle path between one oversized PDF request and one model call per local
    chunk. Every bundle contains adjacent source chunks plus a bounded context excerpt carrying
    opening/method/site information. Source IDs and page ranges remain traceable. The final
    serialized text of *every* bundle is guaranteed to be at most ``max_chars``.
    """
    if not chunks:
        return []
    if max_chars < 1:
        raise ValueError("max_chars must be positive")

    if windowed_source_chunks:
        chunks = _windowize_source_chunks(
            chunks, max_chars=max_chars, context_chars=context_chars
        )

    context_chunks = _bundle_context_chunks(chunks)
    # Reserve space for the largest likely bundle header, one source header, and one source
    # character. This prevents a large context prefix from making even the first source block
    # exceed max_chars. The actual packer below performs exact per-bundle accounting.
    max_bundle_header_len = len(_bundle_header(len(chunks) - 1))
    max_source_header_len = max(len(_bundle_source_header(chunk)) for chunk in chunks)
    context_budget = max(
        0,
        min(
            max(0, context_chars),
            max_chars // 3,
            max_chars - max_bundle_header_len - max_source_header_len - 1,
        ),
    )
    context_text = _serialize_bundle_context(context_chunks, max_chars=context_budget)
    context_prefix = context_text + "\n\n" if context_text else ""

    bundles: list[EvidenceChunk] = []
    current: list[EvidenceChunk] = []
    current_chars = 0

    def start_budget(bundle_index: int) -> int:
        return max_chars - len(context_prefix) - len(_bundle_header(bundle_index))

    for source_chunk in chunks:
        bundle_index = len(bundles)
        body_budget = start_budget(bundle_index)
        source_header = _bundle_source_header(source_chunk)

        # With the reserved context budget this should be positive for normal limits. If a
        # caller deliberately passes an extremely small limit, omit context rather than ever
        # emitting an over-sized request.
        if body_budget < len(source_header):
            context_prefix = ""
            body_budget = start_budget(bundle_index)
        if body_budget < len(source_header):
            raise ValueError(
                "max_chars is too small for a bounded bundle source header; "
                f"required at least {len(source_header) + len(_bundle_header(bundle_index))}"
            )

        separator = 2 if current else 0
        available = body_budget - current_chars - separator - len(source_header)
        if current and available <= 0:
            bundles.append(_build_document_bundle(current, context_prefix, document_id, bundle_index))
            current = []
            current_chars = 0
            bundle_index = len(bundles)
            body_budget = start_budget(bundle_index)
            separator = 0
            available = body_budget - len(source_header)

        # A single source chunk can be larger than the remaining bundle budget. Keep a bounded
        # excerpt, retaining its original chunk_id/page range/source_block_ids for validation.
        bounded_text = _truncate_bundle_text(source_chunk.text, max(0, available))
        part_len = len(source_header) + len(bounded_text)
        if current and current_chars + 2 + part_len > body_budget:
            bundles.append(_build_document_bundle(current, context_prefix, document_id, bundle_index))
            current = []
            current_chars = 0
            bundle_index = len(bundles)
            body_budget = start_budget(bundle_index)
            available = body_budget - len(source_header)
            bounded_text = _truncate_bundle_text(source_chunk.text, max(0, available))
            part_len = len(source_header) + len(bounded_text)

        current.append(
            EvidenceChunk(
                chunk_id=source_chunk.chunk_id,
                document_id=source_chunk.document_id,
                ordinal=source_chunk.ordinal,
                chunk_type=source_chunk.chunk_type,
                text=bounded_text,
                page_start=source_chunk.page_start,
                page_end=source_chunk.page_end,
                source_block_ids=source_chunk.source_block_ids,
                section_path=source_chunk.section_path,
                warnings=source_chunk.warnings,
            )
        )
        current_chars += part_len + (2 if len(current) > 1 else 0)

    if current:
        bundles.append(_build_document_bundle(current, context_prefix, document_id, len(bundles)))

    # This is intentionally a hard invariant: if future header/context edits change the
    # arithmetic above, tests and production runs fail close to the cause instead of sending an
    # unexpectedly large request to the gateway.
    oversized = [bundle for bundle in bundles if len(bundle.text) > max_chars]
    if oversized:
        raise AssertionError(
            f"bounded bundle exceeded max_chars={max_chars}: "
            f"{[(bundle.chunk_id, len(bundle.text)) for bundle in oversized]}"
        )
    return bundles


def _build_document_bundle(
    source_chunks: list[EvidenceChunk],
    context_prefix: str,
    document_id: str,
    bundle_index: int,
) -> EvidenceChunk:
    source_text = "\n\n".join(
        f"=== BUNDLE SOURCE chunk_id={chunk.chunk_id} "
        f"pages={chunk.page_start}-{chunk.page_end} ===\n{chunk.text}"
        for chunk in source_chunks
    )
    text = f"{context_prefix}{_bundle_header(bundle_index)}{source_text}"
    return EvidenceChunk(
        chunk_id=f"bundle-{document_id}-{bundle_index}",
        document_id=document_id,
        ordinal=bundle_index,
        chunk_type="document_bundle",
        text=text,
        page_start=min(chunk.page_start for chunk in source_chunks),
        page_end=max(chunk.page_end for chunk in source_chunks),
        source_block_ids=tuple(
            block_id for chunk in source_chunks for block_id in chunk.source_block_ids
        ),
        warnings=("bounded_document_bundle", "shared_context_repeated"),
    )

def _focused_subchunk(chunk: EvidenceChunk, *, max_chars: int) -> EvidenceChunk | None:
    """Build a small sub-chunk around concentration-like patterns in ``chunk``.

    Used by the P2b focused second pass: when a first extraction returns nothing but the text
    clearly contains concentration patterns (flattened tables, unusual units, over-conservative
    reads), this returns a compact body covering only those regions so one cheap model call can
    rescue the observations. Returns ``None`` when the chunk carries no concentration signal.
    """
    spans = concentration_hit_spans(chunk.text)
    if not spans:
        return None
    selected: list[tuple[int, int]] = []
    total = 0
    for start, end in spans:
        size = end - start
        if selected and total + size > max_chars:
            break
        selected.append((start, end))
        total += size
    if not selected:
        return None
    parts = [
        f"=== FOCUS REGION {index} (source {chunk.chunk_id}) ===\n{chunk.text[start:end]}"
        for index, (start, end) in enumerate(selected, start=1)
    ]
    return EvidenceChunk(
        chunk_id=f"{chunk.chunk_id}:focus2",
        document_id=chunk.document_id,
        ordinal=chunk.ordinal,
        chunk_type="focused_concentration_second_pass",
        text="\n\n".join(parts),
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        source_block_ids=chunk.source_block_ids,
        warnings=("focused_concentration_second_pass",),
    )


_LONG_DETECTION_LIMIT_RE = re.compile(
    r"(?i)"
    r"(?:"
    r"limit(?:s)?\s+of\s+(?:detection|quantification|quantitation)"
    r"|detection\s+limit(?:s)?"
    r"|quantification\s+limit(?:s)?"
    r"|quantitation\s+limit(?:s)?"
    r"|reporting\s+limit(?:s)?"
    r"|method\s+detection\s+limit(?:s)?"
    r"|method\s+quantification\s+limit(?:s)?"
    r"|detectable\s+limit(?:s)?"
    r"|minimum\s+(?:detectable|quantifiable)"
    r"|below(?:\s+the)?\s+(?:method\s+)?(?:detection|quantification)\s+limit"
    r")"
)
_SHORT_DETECTION_LIMIT_RE = re.compile(r"(?i)\b(?:lod|loq|mdl|mql)\b")
_DETECTION_LIMIT_NEARBY_DIGIT = re.compile(r"\d")


def _detection_limit_context(
    chunks: list[EvidenceChunk], *, max_chars: int = 6_000
) -> str:
    """Collect a bounded, document-local excerpt of numerical detection limits.

    The per-chunk extractor sees only one bounded evidence package, so a methods paragraph that
    states numeric LOD/LOQ/MDL/MQL values is normally invisible when a results-table chunk is
    being processed. This scans every chunk for detection/quantification-limit language and
    returns a compact excerpt (chunk-tagged) so the extractor and validator can cross-reference
    a censored result against the paper's stated limits. Returns ``""`` when the document never
    mentions numeric detection limits.
    """
    if max_chars < 1:
        raise ValueError("max_chars must be at least one")
    regions: list[tuple[str, str]] = []
    for chunk in chunks:
        text = chunk.text or ""
        if not text.strip():
            continue
        matches: list[re.Match[str]] = list(_LONG_DETECTION_LIMIT_RE.finditer(text))
        for match in _SHORT_DETECTION_LIMIT_RE.finditer(text):
            lo = max(0, match.start() - 120)
            hi = min(len(text), match.end() + 120)
            if _DETECTION_LIMIT_NEARBY_DIGIT.search(text[lo:hi]):
                matches.append(match)
        if not matches:
            continue
        spans = sorted(
            (max(0, m.start() - 400), min(len(text), m.end() + 700)) for m in matches
        )
        merged: list[tuple[int, int]] = []
        for start, end in spans:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        for start, end in merged:
            segment = text[start:end].strip()
            if segment:
                regions.append(
                    (
                        f"chunk {chunk.ordinal} (pages {chunk.page_start}-{chunk.page_end})",
                        segment,
                    )
                )
    seen: set[str] = set()
    parts: list[str] = []
    total = 0
    for label, segment in regions:
        key = " ".join(segment.split()).casefold()
        if key in seen:
            continue
        seen.add(key)
        block = f"[{label}]\n{segment}"
        if len(block) > max_chars:
            # A single oversized region is truncated rather than dropped so the cap is
            # respected even when the excerpt is dominated by one long passage.
            block = block[: max_chars - 24] + "\n...(truncated)"
            parts.append(block)
            break
        if parts and total + len(block) + 2 > max_chars:
            continue
        parts.append(block)
        total += len(block) + 2
        if total >= max_chars:
            break
    return "\n\n".join(parts)


_CHEMICAL_IDENTITY_CUE_RE = re.compile(
    r"(?i)\b(?:analyte(?:s| list)?|target(?:ed)? (?:analytes?|compounds?)|"
    r"compounds? (?:analy[sz]ed|investigated|determined|measured|included)|"
    r"abbreviation(?:s| list)?|chemical names?|PFAS(?:s)? (?:analytes?|compounds?|names?)|"
    r"analytical standards?|target list)\b"
)
_DOCUMENT_LOCAL_DEFINITION_RE = re.compile(
    r"(?i)(?:[A-Za-z][A-Za-z0-9α-ωΑ-Ω,+/\- '′’()]{3,120})"
    r"\(\s*[A-Z][A-Za-z0-9+\-/]{1,24}\s*\)"
)


_SAMPLING_CONTEXT_CUE_RE = re.compile(
    r"(?i)\b(?:sampling campaign|sampling sites?|sampling locations?|sample collection|samples? were collected|"
    r"surface water was sampled|all samplings took place|collected in the (?:dry|wet) season)\b"
)
_ANALYTICAL_METHOD_CONTEXT_CUE_RE = re.compile(
    r"(?i)\b(?:analytical methods?|analytical procedure|sample preparation|instrumentation|"
    r"solid[- ]phase extraction|SPE|HPLC|UHPLC|UPLC|LC[-– ](?:HRMS|MS/MS|ESI[-– ]QTOF[-– ]MS)|"
    r"GC[-– ]MS(?:[-– ]SIM)?|ICP[-– ]MS|mass spectrometric techniques?)\b"
)


def _bounded_cue_context(
    chunks: list[EvidenceChunk],
    *,
    cue_pattern: re.Pattern[str],
    max_chars: int,
    before: int = 500,
    after: int = 1_300,
) -> str:
    """Return early, deduplicated source excerpts around document-level evidence cues."""
    if max_chars < 1:
        raise ValueError("max_chars must be at least one")
    regions: list[tuple[str, str]] = []
    for chunk in chunks:
        text = chunk.text or ""
        if not text.strip():
            continue
        matches = list(cue_pattern.finditer(text))
        if not matches:
            continue
        spans = sorted(
            (max(0, match.start() - before), min(len(text), match.end() + after))
            for match in matches
        )
        merged: list[tuple[int, int]] = []
        for start, end in spans:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        for start, end in merged:
            segment = text[start:end].strip()
            if segment:
                regions.append((
                    f"chunk {chunk.ordinal} (pages {chunk.page_start}-{chunk.page_end})",
                    segment,
                ))

    seen: set[str] = set()
    parts: list[str] = []
    total = 0
    for label, segment in regions:
        key = " ".join(segment.split()).casefold()
        if key in seen:
            continue
        seen.add(key)
        block = f"[{label}]\n{segment}"
        if len(block) > max_chars:
            parts.append(block[: max_chars - 24] + "\n...(truncated)")
            break
        if parts and total + len(block) + 2 > max_chars:
            continue
        parts.append(block)
        total += len(block) + 2
        if total >= max_chars:
            break
    return "\n\n".join(parts)


def _sampling_context(chunks: list[EvidenceChunk], *, max_chars: int = 6_000) -> str:
    return _bounded_cue_context(
        chunks,
        cue_pattern=_SAMPLING_CONTEXT_CUE_RE,
        max_chars=max_chars,
        after=3_200,
    )


def _analytical_method_context(
    chunks: list[EvidenceChunk], *, max_chars: int = 8_000
) -> str:
    return _bounded_cue_context(
        chunks,
        cue_pattern=_ANALYTICAL_METHOD_CONTEXT_CUE_RE,
        max_chars=max_chars,
    )


def _chemical_identity_context(
    chunks: list[EvidenceChunk], *, max_chars: int = 8_000
) -> str:
    """Collect article-local analyte and abbreviation evidence before external lookup.

    Chunked extraction can otherwise see a short token in a results table without the methods
    page that defines it. This bounded context deliberately preserves the source wording and
    chunk/page anchors; it is evidence for a document-local mapping, not an automatically trusted
    global alias.
    """
    if max_chars < 1:
        raise ValueError("max_chars must be at least one")
    regions: list[tuple[str, str]] = []
    for chunk in chunks:
        text = chunk.text or ""
        if not text.strip():
            continue
        matches = list(_CHEMICAL_IDENTITY_CUE_RE.finditer(text))
        matches.extend(_DOCUMENT_LOCAL_DEFINITION_RE.finditer(text))
        if not matches:
            continue
        spans = sorted(
            (max(0, match.start() - 500), min(len(text), match.end() + 1_100))
            for match in matches
        )
        merged: list[tuple[int, int]] = []
        for start, end in spans:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        for start, end in merged:
            segment = text[start:end].strip()
            if segment:
                regions.append(
                    (
                        f"chunk {chunk.ordinal} (pages {chunk.page_start}-{chunk.page_end})",
                        segment,
                    )
                )

    seen: set[str] = set()
    parts: list[str] = []
    total = 0
    for label, segment in regions:
        key = " ".join(segment.split()).casefold()
        if key in seen:
            continue
        seen.add(key)
        block = f"[{label}]\n{segment}"
        if len(block) > max_chars:
            block = block[: max_chars - 24] + "\n...(truncated)"
            parts.append(block)
            break
        if parts and total + len(block) + 2 > max_chars:
            continue
        parts.append(block)
        total += len(block) + 2
        if total >= max_chars:
            break
    return "\n\n".join(parts)


class FulltextExtractionHarness:
    """Run exactly one document with fresh agent instances and transactional persistence."""

    def __init__(
        self,
        *,
        parser: PdfParserAdapter,
        control_plane: FulltextControlPlane,
        chemical_registry: ChemicalRegistry,
        output_dir: Path,
        chemical_resolver: ChemicalResolver | None = None,
        geocode_resolver: GeocodeResolver | None = None,
        extractor: CandidateExtractor | None = None,
        validator: EvidenceValidator | None = None,
        extractor_factory: Callable[[], CandidateExtractor] | None = None,
        validator_factory: Callable[[], EvidenceValidator] | None = None,
        chunker: DocumentChunker | None = None,
        temporary_root: Path | None = None,
        chunk_selector: Callable[[EvidenceChunk], bool] | None = None,
        maximum_extraction_attempts: int = 1,
        chunk_parallelism: int = 1,
        merge_chunks_for_extraction: bool = False,
        max_merged_input_chars: int = 120_000,
        evidence_anchor_check: bool = True,
        normalize_document_text: bool = True,
        focused_second_pass_on_empty: bool = True,
        max_focus_chars: int = 40_000,
        whole_doc_empty_fallback: Literal["auto", "always", "never"] = "auto",
        detection_limit_context_max_chars: int = 6_000,
        chemical_identity_context_max_chars: int = 8_000,
        windowed_bundles: bool = False,
        signed_example_store: Any | None = None,
    ) -> None:
        if maximum_extraction_attempts < 1:
            raise ValueError("maximum_extraction_attempts must be at least one")
        if chunk_parallelism < 1:
            raise ValueError("chunk_parallelism must be at least one")
        if max_merged_input_chars < 1:
            raise ValueError("max_merged_input_chars must be at least one")
        if max_focus_chars < 1:
            raise ValueError("max_focus_chars must be at least one")
        if whole_doc_empty_fallback not in {"auto", "always", "never"}:
            raise ValueError(
                "whole_doc_empty_fallback must be one of: auto, always, never"
            )
        if detection_limit_context_max_chars < 0 or chemical_identity_context_max_chars < 0:
            raise ValueError("document context caps may not be negative")
        if extractor is not None and extractor_factory is not None:
            raise ValueError("provide extractor or extractor_factory, not both")
        if validator is not None and validator_factory is not None:
            raise ValueError("provide validator or validator_factory, not both")
        self.parser = parser
        self.control_plane = control_plane
        self.chemical_registry = chemical_registry
        self.output_dir = output_dir
        self.chemical_resolver = chemical_resolver
        self.geocode_resolver = geocode_resolver
        self._extractor_template = extractor
        self._validator_template = validator
        self._extractor_factory = extractor_factory
        self._validator_factory = validator_factory
        self.chunker = chunker or StructuralChunker()
        self.temporary_root = temporary_root
        self.chunk_selector = chunk_selector
        self.maximum_extraction_attempts = maximum_extraction_attempts
        self.chunk_parallelism = chunk_parallelism
        self.merge_chunks_for_extraction = merge_chunks_for_extraction
        self.max_merged_input_chars = max_merged_input_chars
        self.evidence_anchor_check = evidence_anchor_check
        self.normalize_document_text = normalize_document_text
        self.focused_second_pass_on_empty = focused_second_pass_on_empty
        self.max_focus_chars = max_focus_chars
        self.whole_doc_empty_fallback = whole_doc_empty_fallback
        self.detection_limit_context_max_chars = detection_limit_context_max_chars
        self.chemical_identity_context_max_chars = chemical_identity_context_max_chars
        self.windowed_bundles = windowed_bundles
        self.signed_example_store = signed_example_store

    def run_document(
        self, pdf_path: Path, *, bibliographic_metadata: dict[str, Any] | None = None
    ) -> DocumentRunReport:
        pdf_path = pdf_path.resolve()
        if not pdf_path.is_file():
            raise FileNotFoundError(pdf_path)
        source_sha256 = _sha256(pdf_path)
        document_id = f"sha256:{source_sha256}"
        document_session_id = f"session-{uuid.uuid4()}"
        registry_snapshot_version = self.chemical_registry.version()
        self.control_plane.begin_session(
            document_id=document_id,
            document_session_id=document_session_id,
            source_path=pdf_path,
            source_sha256=source_sha256,
            registry_snapshot_version=registry_snapshot_version,
        )
        if self.temporary_root is not None:
            self.temporary_root.mkdir(parents=True, exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(prefix=f"{document_session_id}-", dir=self.temporary_root))
        extractor = self._fresh_extractor()
        validator = self._fresh_validator()
        # Direct-PDF transport is intentionally extractor-only; the validator uses local text
        # and evidence anchors to independently verify the candidate.
        if hasattr(extractor, "pdf_path"):
            extractor.pdf_path = pdf_path
        document_context = _document_context(bibliographic_metadata)
        if self.signed_example_store is not None:
            examples = self.signed_example_store.fewshot_examples()
            if examples:
                document_context["signed_examples"] = [
                    example.to_example_dict() for example in examples
                ]
        if hasattr(extractor, "document_context"):
            extractor.document_context = document_context
        if hasattr(validator, "document_context"):
            validator.document_context = document_context
        committed = False
        warnings: list[str] = []
        try:
            parsed = self.parser.parse(
                pdf_path, document_id=document_id, source_sha256=source_sha256
            )
            if self.normalize_document_text:
                parsed = normalize_parsed(parsed)
            warnings.extend(parsed.warnings)
            chunks = self.chunker.chunk(parsed)
            detection_limit_context = _detection_limit_context(
                chunks, max_chars=self.detection_limit_context_max_chars
            )
            if detection_limit_context:
                document_context["detection_limit_context"] = detection_limit_context
            sampling_context = _sampling_context(chunks)
            if sampling_context:
                document_context["sampling_context"] = sampling_context
            analytical_method_context = _analytical_method_context(chunks)
            if analytical_method_context:
                document_context["analytical_method_context"] = analytical_method_context
            chemical_identity_context = _chemical_identity_context(
                chunks, max_chars=self.chemical_identity_context_max_chars
            )
            if chemical_identity_context:
                document_context["chemical_identity_context"] = chemical_identity_context
            if hasattr(extractor, "document_context"):
                extractor.document_context = document_context
            if hasattr(validator, "document_context"):
                validator.document_context = document_context
            merged_chunk: EvidenceChunk | None = None
            bundled_document_attempt = False
            text_only_processed_chunks = False
            if self.merge_chunks_for_extraction:
                # Preferred path: send the complete document once so methods/sampling context
                # and result tables are visible together. If it is too large, use bounded
                # multi-chunk bundles with repeated context instead of isolated chunk calls.
                merged_chunk = _merge_chunks_for_extraction(
                    chunks,
                    document_id=document_id,
                    max_chars=self.max_merged_input_chars,
                )
                if merged_chunk is not None:
                    processed_chunks = [merged_chunk]
                    warnings.append(f"whole_document_single_call:{len(chunks)}")
                else:
                    processed_chunks = _split_chunks_for_extraction(
                        chunks,
                        document_id=document_id,
                        max_chars=self.max_merged_input_chars,
                        windowed_source_chunks=self.windowed_bundles,
                    )
                    bundled_document_attempt = True
                    text_only_processed_chunks = True
                    warnings.append("merged_chunks_skipped:input_too_large")
                    warnings.append(
                        f"bounded_document_bundles:{len(processed_chunks)}/{len(chunks)}"
                    )
            else:
                processed_chunks = (
                    [chunk for chunk in chunks if self.chunk_selector(chunk)]
                    if self.chunk_selector is not None
                    else chunks
                )
                if self.chunk_selector is not None and len(processed_chunks) != len(chunks):
                    warnings.append(
                        f"chunk_prefilter_skipped:{len(chunks) - len(processed_chunks)}"
                    )
            whole_doc_attempt = self.merge_chunks_for_extraction and (
                merged_chunk is not None or bundled_document_attempt
            )
            per_chunk_fallback_performed = False
            try:
                def run_batch() -> _ChunkBatchResult:
                    return self._run_chunk_batch(
                        processed_chunks,
                        extractor=extractor,
                        validator=validator,
                        document_session_id=document_session_id,
                        registry_snapshot_version=registry_snapshot_version,
                    )
                batch = (
                    _run_without_direct_pdf(extractor, run_batch)
                    if text_only_processed_chunks
                    else run_batch()
                )
            except Exception as exc:
                transport_stress = _is_transport_stress(exc)
                truncated_response = _is_truncated_model_response(exc)
                invalid_response = _is_invalid_model_response(exc)
                validator_failure = _is_validator_failure(exc)
                if whole_doc_attempt and not validator_failure and (
                    transport_stress or truncated_response or invalid_response
                ):
                    # Guardrail A: a whole-document call can either hang at the gateway or
                    # exhaust the model completion budget on a dense table. Split the already-
                    # parsed document into bounded text chunks; do not upload the complete PDF
                    # again for every chunk.
                    warnings.append(
                        "whole_doc_transport_fallback_per_chunk"
                        if transport_stress
                        else ("whole_doc_invalid_response_fallback_per_chunk" if invalid_response
                              else "whole_doc_truncated_fallback_per_chunk")
                    )
                    if getattr(extractor, "pdf_path", None) is not None:
                        warnings.append("whole_doc_fallback_text_only")
                    batch = _run_without_direct_pdf(
                        extractor,
                        lambda: self._run_chunk_batch(
                            chunks,
                            extractor=extractor,
                            validator=validator,
                            document_session_id=document_session_id,
                            registry_snapshot_version=registry_snapshot_version,
                        ),
                    )
                    per_chunk_fallback_performed = True
                else:
                    raise
            if (
                whole_doc_attempt
                and not per_chunk_fallback_performed
                and not batch.candidate_rows
            ):
                # Guardrail B: an empty whole-document answer only receives an expensive
                # per-chunk retry when local title/text signals say a real water concentration
                # may have been missed. Obvious plastic-particle-only papers commit the empty
                # result immediately.
                fallback_allowed = self.whole_doc_empty_fallback == "always"
                fallback_reason = "forced_by_configuration"
                if self.whole_doc_empty_fallback == "auto":
                    fallback_allowed, fallback_reason = _whole_doc_empty_fallback_decision(
                        chunks, bibliographic_metadata
                    )
                elif self.whole_doc_empty_fallback == "never":
                    fallback_allowed = False
                    fallback_reason = "disabled_by_configuration"

                if fallback_allowed:
                    fallback_chunks = _fallback_chunks(
                        chunks, chunk_selector=self.chunk_selector
                    )
                    warnings.append("whole_doc_empty_fallback_per_chunk")
                    warnings.append(f"whole_doc_empty_fallback_reason:{fallback_reason}")
                    if len(fallback_chunks) != len(chunks):
                        warnings.append(
                            "whole_doc_empty_fallback_selected_chunks:"
                            f"{len(fallback_chunks)}/{len(chunks)}"
                        )
                    if getattr(extractor, "pdf_path", None) is not None:
                        warnings.append("whole_doc_fallback_text_only")
                    batch = _run_without_direct_pdf(
                        extractor,
                        lambda: self._run_chunk_batch(
                            fallback_chunks,
                            extractor=extractor,
                            validator=validator,
                            document_session_id=document_session_id,
                            registry_snapshot_version=registry_snapshot_version,
                        ),
                    )
                else:
                    warnings.append(
                        f"whole_doc_empty_fallback_skipped:{fallback_reason}"
                    )
            candidate_rows = batch.candidate_rows
            resolution_rows = batch.resolution_rows
            review_rows = batch.review_rows
            retry_events = batch.retry_events
            observation_records = batch.observation_records
            human_review_tasks = batch.human_review_tasks
            proposal_resolutions = batch.proposal_resolutions
            outbox_payload = {
                "workflow_identity": (bibliographic_metadata or {}).get("workflow_identity"),
                "workflow_signature": (bibliographic_metadata or {}).get("workflow_signature"),
                "document_id": document_id,
                "document_session_id": document_session_id,
                "source_sha256": source_sha256,
                "registry_snapshot_version": registry_snapshot_version,
                "chunk_count": len(chunks),
                "candidate_count": len(candidate_rows),
                "review_count": len(review_rows),
                "retry_count": len(retry_events),
                "accepted_count": _count_disposition(observation_records, "accepted"),
                "rejected_count": _count_disposition(observation_records, "rejected"),
                "pending_human_review_count": _count_disposition(
                    observation_records, "pending_human_review"
                ),
                "terminal_status_counts": _terminal_status_counts(observation_records),
            }
            generated_context_chunks = [
                *([merged_chunk] if merged_chunk is not None else []),
                *(processed_chunks if bundled_document_attempt else []),
            ]
            commit_chunks = [*generated_context_chunks, *chunks]
            self.control_plane.commit_document(
                document_session_id=document_session_id,
                parsed_document=parsed,
                chunks=commit_chunks,
                candidates=candidate_rows,
                resolutions=resolution_rows,
                reviews=review_rows,
                retry_events=retry_events,
                observation_records=observation_records,
                human_review_tasks=human_review_tasks,
                outbox_payload=outbox_payload,
            )
            committed = True

            for resolution in proposal_resolutions.values():
                try:
                    self.chemical_registry.record_proposal(
                        document_id=document_id,
                        document_session_id=document_session_id,
                        resolution=resolution,
                    )
                except Exception as exc:  # document commit remains authoritative
                    warnings.append(f"chemical_proposal_persist_failed:{type(exc).__name__}")

            report_filename = f"{document_session_id.removeprefix('session-')}.json"
            report_path = self.output_dir / source_sha256[:16] / report_filename
            report = DocumentRunReport(
                document_id=document_id,
                document_session_id=document_session_id,
                source_path=str(pdf_path),
                source_sha256=source_sha256,
                registry_snapshot_version=registry_snapshot_version,
                parser_name=parsed.parser_name,
                parser_version=parsed.parser_version,
                page_count=parsed.page_count,
                chunk_count=len(chunks),
                candidate_count=len(candidate_rows),
                review_count=len(review_rows),
                resolution_count=len(resolution_rows),
                committed=True,
                output_path=str(report_path),
                accepted_count=_count_disposition(observation_records, "accepted"),
                rejected_count=_count_disposition(observation_records, "rejected"),
                pending_human_review_count=_count_disposition(
                    observation_records, "pending_human_review"
                ),
                retry_count=len(retry_events),
                terminal_status_counts=_terminal_status_counts(observation_records),
                warnings=tuple(warnings),
                bibliographic_metadata=dict(bibliographic_metadata or {}),
            )
            write_json_atomic(report_path, report.to_dict())
            return report
        except Exception as exc:
            if not committed:
                self.control_plane.fail_session(document_session_id, repr(exc))
            raise
        finally:
            _flush_component(self.chemical_resolver)
            _close_component(extractor)
            _close_component(validator)
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _fresh_extractor(self) -> CandidateExtractor:
        if self._extractor_factory is not None:
            return self._extractor_factory()
        if self._extractor_template is None:
            return NoopCandidateExtractor()
        return _copy_component(self._extractor_template, "extractor")

    def _fresh_validator(self) -> EvidenceValidator:
        if self._validator_factory is not None:
            return self._validator_factory()
        if self._validator_template is None:
            return ConservativeEvidenceValidator()
        return _copy_component(self._validator_template, "validator")

    def _run_chunk_batch(
        self,
        chunks_to_process: list[EvidenceChunk],
        *,
        extractor: CandidateExtractor,
        validator: EvidenceValidator,
        document_session_id: str,
        registry_snapshot_version: int,
    ) -> _ChunkBatchResult:
        """Run extract -> resolve -> review for many chunks and aggregate the results.

        Extracted from the document loop so the whole-document merge path can fall back to
        per-chunk mode (transport hang or empty result) without duplicating dispatch logic.
        """
        chunk_results: list[_ChunkResult] = []
        if len(chunks_to_process) > 1 and self.chunk_parallelism > 1:
            with ThreadPoolExecutor(
                max_workers=self.chunk_parallelism, thread_name_prefix="chunk"
            ) as executor:
                futures = [
                    executor.submit(
                        self._process_chunk,
                        extractor,
                        validator,
                        chunk,
                        document_session_id,
                        registry_snapshot_version,
                    )
                    for chunk in chunks_to_process
                ]
                # Merge in chunk order so the committed rows keep a stable ordering; any
                # chunk failure propagates and is handled at document level.
                for future in futures:
                    chunk_results.append(future.result())
        else:
            for chunk in chunks_to_process:
                chunk_results.append(
                    self._process_chunk(
                        extractor,
                        validator,
                        chunk,
                        document_session_id,
                        registry_snapshot_version,
                    )
                )
        candidate_rows: list[tuple[str, str, dict[str, Any]]] = []
        resolution_rows: list[tuple[str | None, dict[str, Any]]] = []
        review_rows: list[tuple[str, str, ValidationDecision]] = []
        retry_events: list[RetryEvent] = []
        observation_records: list[ObservationDisposition] = []
        human_review_tasks: list[HumanReviewTask] = []
        proposal_resolutions: dict[str, ChemicalResolution] = {}
        for result in chunk_results:
            candidate_rows.extend(result.candidate_rows)
            resolution_rows.extend(result.resolution_rows)
            review_rows.extend(result.review_rows)
            retry_events.extend(result.retry_events)
            observation_records.extend(result.observation_records)
            human_review_tasks.extend(result.human_review_tasks)
            for key, resolution in result.proposal_resolutions.items():
                proposal_resolutions.setdefault(key, resolution)
        return _ChunkBatchResult(
            candidate_rows=candidate_rows,
            resolution_rows=resolution_rows,
            review_rows=review_rows,
            retry_events=retry_events,
            observation_records=observation_records,
            human_review_tasks=human_review_tasks,
            proposal_resolutions=proposal_resolutions,
        )

    @staticmethod
    def _extract(
        extractor: CandidateExtractor,
        chunk: EvidenceChunk,
        *,
        attempt: int,
        reason_codes: tuple[str, ...],
        failed_json_pointers: tuple[str, ...],
        requested_context: tuple[str, ...],
        failed_candidates: tuple[dict[str, Any], ...] = (),
        prior_candidates: tuple[dict[str, Any], ...] = (),
    ) -> list[dict[str, Any]]:
        retry_method = getattr(extractor, "extract_with_feedback", None)
        if callable(retry_method):
            kwargs: dict[str, Any] = {
                "attempt": attempt,
                "reason_codes": reason_codes,
                "failed_json_pointers": failed_json_pointers,
                "requested_context": requested_context,
            }
            if failed_candidates or prior_candidates:
                accepted = _method_kwargs(retry_method, "failed_candidates", "prior_candidates")
                if "failed_candidates" in accepted:
                    kwargs["failed_candidates"] = failed_candidates
                if "prior_candidates" in accepted:
                    kwargs["prior_candidates"] = prior_candidates
            result = retry_method(chunk, **kwargs)
        else:
            result = extractor.extract(chunk)
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise TypeError("extractor must return list[dict[str, Any]]")
        return result

    def _process_chunk(
        self,
        extractor: CandidateExtractor,
        validator: EvidenceValidator,
        chunk: EvidenceChunk,
        document_session_id: str,
        registry_snapshot_version: int,
    ) -> _ChunkResult:
        """Run extract -> resolve -> review -> disposition for one chunk.

        Each selected chunk is processed independently so a chunk-level retry loop never
        bleeds feedback into a sibling chunk. This method is called from a worker thread when
        ``chunk_parallelism > 1``; it touches only thread-safe components (registry lookups
        open their own sqlite connections, PubChem uses a token-bucket rate limiter, and all
        accumulated rows are merged by the caller after collection).
        """
        candidate_rows: list[tuple[str, str, dict[str, Any]]] = []
        resolution_rows: list[tuple[str | None, dict[str, Any]]] = []
        review_rows: list[tuple[str, str, ValidationDecision]] = []
        retry_events: list[RetryEvent] = []
        observation_records: list[ObservationDisposition] = []
        human_review_tasks: list[HumanReviewTask] = []
        proposal_resolutions: dict[str, ChemicalResolution] = {}

        folded_chunk = fold_for_evidence(chunk.text)

        feedback_reasons: tuple[str, ...] = ()
        feedback_pointers: tuple[str, ...] = ()
        requested_context: tuple[str, ...] = ()
        feedback_failed_candidates: tuple[dict[str, Any], ...] = ()
        feedback_prior_candidates: tuple[dict[str, Any], ...] = ()
        focus_chunk: EvidenceChunk | None = None
        for attempt in range(1, self.maximum_extraction_attempts + 1):
            extraction_chunk = focus_chunk if focus_chunk is not None else chunk
            payloads = self._extract(
                extractor,
                extraction_chunk,
                attempt=attempt,
                reason_codes=feedback_reasons,
                failed_json_pointers=feedback_pointers,
                requested_context=requested_context,
                failed_candidates=feedback_failed_candidates,
                prior_candidates=feedback_prior_candidates,
            )
            payloads = _dedupe_payloads(payloads)
            # P2b: a whole-document/extraction that returned zero candidates but whose text is
            # full of concentration-like patterns was almost certainly over-conservative (e.g.
            # flattened tables or unusual units). Spend one small focused call on the hit
            # regions before giving up on the chunk.
            if (
                not payloads
                and attempt == 1
                and self.focused_second_pass_on_empty
                and focus_chunk is None
                and self.maximum_extraction_attempts >= 2
            ):
                focus_candidate = _focused_subchunk(chunk, max_chars=self.max_focus_chars)
                if focus_candidate is not None:
                    focus_chunk = focus_candidate
                    feedback_reasons = ()
                    feedback_pointers = ()
                    requested_context = ("concentration_hit_regions",)
                    feedback_failed_candidates = ()
                    feedback_prior_candidates = ()
                    retry_events.append(
                        RetryEvent(
                            retry_id=f"retry-{uuid.uuid4()}",
                            chunk_id=chunk.chunk_id,
                            attempt=attempt,
                            reason_codes=("focused_concentration_second_pass",),
                            failed_json_pointers=(),
                            requested_context=("concentration_hit_regions",),
                        )
                    )
                    continue
            prepared_candidates: list[
                tuple[str, dict[str, Any], tuple[ChemicalResolution, ...]]
            ] = []
            for payload in payloads:
                source_candidate_id = str(
                    payload.get("candidate_id") or f"candidate-{uuid.uuid4()}"
                )
                candidate_id = (
                    f"{document_session_id}:a{attempt}:{source_candidate_id}:{uuid.uuid4().hex[:8]}"
                )
                materialized = dict(payload)
                materialized["candidate_id"] = candidate_id
                materialized["extraction_attempt"] = attempt
                materialized = materialize_censoring(materialized)
                materialized.setdefault("evidence", {})
                if isinstance(materialized["evidence"], dict):
                    materialized["evidence"].setdefault("chunk_id", extraction_chunk.chunk_id)
                    materialized["evidence"].setdefault("page_start", extraction_chunk.page_start)
                    materialized["evidence"].setdefault("page_end", extraction_chunk.page_end)
                resolutions = tuple(
                    self._resolve_candidate(
                        materialized, registry_snapshot_version, document_id=chunk.document_id
                    )
                )
                materialized = materialize_chemical_identity(materialized, resolutions)
                materialized = self._enrich_location(materialized)
                if self.evidence_anchor_check:
                    anchor_reason = evidence_anchor_reason(materialized, folded_chunk)
                    if anchor_reason:
                        flags = list(materialized.get("quality_flags") or [])
                        flags.append("hallucination_suspect:" + anchor_reason)
                        materialized["quality_flags"] = flags
                candidate_rows.append((candidate_id, chunk.chunk_id, materialized))
                prepared_candidates.append((candidate_id, materialized, resolutions))
                for resolution in resolutions:
                    resolution_rows.append((candidate_id, resolution.to_dict()))
                    if resolution.resolver_name != "ecmonitor_chemical_registry":
                        proposal_resolutions.setdefault(
                            _resolution_dedup_key(resolution), resolution
                        )

            # Candidates whose extracted value is not supported by the source text
            # (hallucination suspects) skip the model validator entirely and go straight
            # to human review. This spends zero model calls on the strongest error mode.
            batch_validate = getattr(validator, "validate_batch", None)
            decisions: list[ValidationDecision] = [None] * len(prepared_candidates)  # type: ignore[list-item]
            suspect_indices = {
                i
                for i, item in enumerate(prepared_candidates)
                if any(
                    str(flag).startswith("hallucination_suspect")
                    for flag in (item[1].get("quality_flags") or [])
                )
            }
            normal_indices = [i for i in range(len(prepared_candidates)) if i not in suspect_indices]
            if normal_indices:
                normal_items = [prepared_candidates[i] for i in normal_indices]
                if callable(batch_validate):
                    sub_decisions = batch_validate(
                        [item[1] for item in normal_items],
                        chunk=extraction_chunk,
                        resolutions_by_candidate=[item[2] for item in normal_items],
                    )
                    if len(sub_decisions) != len(normal_items):
                        raise RuntimeError(
                            "batch validator returned a decision count that does not "
                            "match candidates"
                        )
                else:
                    sub_decisions = [
                        validator.validate(
                            materialized,
                            chunk=extraction_chunk,
                            resolutions=resolutions,
                        )
                        for _, materialized, resolutions in normal_items
                    ]
                for idx, decision in zip(normal_indices, sub_decisions, strict=True):
                    decisions[idx] = decision
            for idx in suspect_indices:
                decisions[idx] = ValidationDecision(
                    action="escalate",
                    reason_codes=("hallucination_suspect",),
                    failed_json_pointers=(),
                    requested_context=(),
                    human_review_required=True,
                )
            attempt_results = []
            for (candidate_id, materialized, resolutions), decision in zip(
                prepared_candidates, decisions, strict=True
            ):
                review_rows.append((f"review-{uuid.uuid4()}", candidate_id, decision))
                attempt_results.append((candidate_id, materialized, resolutions, decision))

            repairable_retries = [
                item
                for item in attempt_results
                if item[3].action == "retry"
                and retry_is_repairable_by_extraction(item[3])
            ]
            # Only re-prompt when a repair is actually possible. Registry/identity retries
            # cannot be fixed by re-reading the same text, so re-extracting for them would burn
            # a full model call to end in the same escalate. The feedback below is also precise:
            # reason codes/pointers come from the repairable candidates only, and the extractor
            # receives the failing candidates plus the full prior list so it repairs the missing
            # fields without regressing the candidates that already passed.
            if repairable_retries and attempt < self.maximum_extraction_attempts:
                repairable_decisions = [item[3] for item in repairable_retries]
                feedback_reasons = _ordered_unique(
                    code
                    for decision in repairable_decisions
                    for code in decision.reason_codes
                )
                feedback_pointers = _ordered_unique(
                    pointer
                    for decision in repairable_decisions
                    for pointer in decision.failed_json_pointers
                )
                requested_context = _ordered_unique(
                    context
                    for decision in repairable_decisions
                    for context in decision.requested_context
                )
                feedback_failed_candidates = tuple(dict(item[1]) for item in repairable_retries)
                feedback_prior_candidates = tuple(dict(item[1]) for item in attempt_results)
                retry_events.append(
                    RetryEvent(
                        retry_id=f"retry-{uuid.uuid4()}",
                        chunk_id=chunk.chunk_id,
                        attempt=attempt,
                        reason_codes=feedback_reasons,
                        failed_json_pointers=feedback_pointers,
                        requested_context=requested_context,
                    )
                )
                continue

            for candidate_id, materialized, resolutions, decision in attempt_results:
                final_decision = decision
                if decision.action == "retry":
                    # Reached here because the retry budget was exhausted OR the retry was not
                    # repairable by re-extraction; either way it must go to a human.
                    escalate_reason = (
                        "retry_not_repairable"
                        if not retry_is_repairable_by_extraction(decision)
                        else "retry_budget_exhausted"
                    )
                    final_decision = ValidationDecision(
                        action="escalate",
                        reason_codes=_ordered_unique((escalate_reason, *decision.reason_codes)),
                        failed_json_pointers=decision.failed_json_pointers,
                        requested_context=decision.requested_context,
                        human_review_required=True,
                    )
                self._materialize_disposition(
                    candidate_id=candidate_id,
                    candidate=materialized,
                    chunk=extraction_chunk,
                    resolutions=resolutions,
                    decision=final_decision,
                    observation_records=observation_records,
                    human_review_tasks=human_review_tasks,
                )
            break
        return _ChunkResult(
            candidate_rows=candidate_rows,
            resolution_rows=resolution_rows,
            review_rows=review_rows,
            retry_events=retry_events,
            observation_records=observation_records,
            human_review_tasks=human_review_tasks,
            proposal_resolutions=proposal_resolutions,
        )


    def _resolve_candidate(
        self,
        candidate: dict[str, Any],
        registry_snapshot_version: int,
        *,
        document_id: str | None = None,
    ) -> list[ChemicalResolution]:
        resolutions: list[ChemicalResolution] = []
        # Classes, sum/total parameters, mixtures, polymers, etc. have no single compound
        # identity: spend only the cheap local-registry lookup, never a PubChem call.
        allow_external_resolution = (
            not looks_non_individual(candidate)
            and not is_microplastic_surface_water_observation(candidate)
            and not looks_out_of_scope_water_quality(candidate)
        )
        for raw_name in _candidate_raw_names(candidate):
            if document_id is not None:
                document_local = self.chemical_registry.lookup_document_local(
                    raw_name, document_id=document_id
                )
                if document_local is not None:
                    resolutions.append(document_local)
                    continue
            curated = _curated_specific_identity_resolution(
                raw_name, registry_snapshot_version=registry_snapshot_version
            )
            if curated is not None:
                resolutions.append(curated)
                continue
            queries = _candidate_lookup_names(candidate, raw_name)
            selected: ChemicalResolution | None = None
            selected_query = raw_name
            for query in queries:
                local = self.chemical_registry.lookup_validated(
                    query, at_version=registry_snapshot_version
                )
                if local is not None:
                    selected = _relabel_resolution(
                        local,
                        raw_name=raw_name,
                        registry_snapshot_version=registry_snapshot_version,
                        fallback_query=query,
                    )
                    selected_query = query
                    break
                if self.chemical_resolver is None or not allow_external_resolution:
                    continue
                external = self.chemical_resolver.resolve(query)
                selected = ChemicalResolution(
                    raw_name=raw_name,
                    normalized_query=external.normalized_query,
                    status=external.status,
                    resolver_name=external.resolver_name,
                    matches=external.matches,
                    warnings=external.warnings,
                    registry_snapshot_version=registry_snapshot_version,
                )
                selected_query = query
                # A canonical/proposed fallback is useful after a composite
                # document label such as ``serum cortisol`` is not a PubChem
                # synonym. Do not hide ambiguity or transport failures by trying
                # more names after the resolver has returned a meaningful result.
                if external.status != "not_found":
                    break
            if selected is not None:
                if selected_query != raw_name:
                    selected = _relabel_resolution(
                        selected,
                        raw_name=raw_name,
                        registry_snapshot_version=registry_snapshot_version,
                        fallback_query=selected_query,
                    )
                resolutions.append(selected)
        return resolutions

    def _enrich_location(self, candidate: dict[str, Any]) -> dict[str, Any]:
        """Fill approximate coordinates and validate the administrative hierarchy."""
        if self.geocode_resolver is None:
            return candidate
        location = candidate.get("location")
        if not isinstance(location, dict):
            return candidate

        enriched = dict(location)
        if enriched.get("latitude") is None or enriched.get("longitude") is None:
            try:
                result = self.geocode_resolver.resolve(enriched)
            except Exception:  # geocoding is best-effort and never blocks commit
                result = None
            if result is not None:
                if enriched.get("latitude") is None:
                    enriched["latitude"] = result.latitude
                if enriched.get("longitude") is None:
                    enriched["longitude"] = result.longitude
                enriched.setdefault("geocode_source", result.source)
                enriched.setdefault("geocode_matched_name", result.matched_name)
                enriched.setdefault("geocode_country_iso2", result.country_iso2)

        validator = getattr(self.geocode_resolver, "validate_admin_hierarchy", None)
        if callable(validator):
            try:
                validation = validator(enriched)
            except Exception:
                validation = None
            if isinstance(validation, dict):
                enriched["admin_hierarchy_validated"] = bool(validation.get("validated"))
                enriched["admin_hierarchy_consistent"] = bool(
                    validation.get("consistent", True)
                )
                enriched["admin_hierarchy_conflicts"] = list(
                    validation.get("conflicts") or []
                )
                enriched.setdefault("country_iso2", validation.get("country_iso2"))

        candidate = dict(candidate)
        candidate["location"] = enriched
        return candidate

    @staticmethod
    def _materialize_disposition(
        *,
        candidate_id: str,
        candidate: dict[str, Any],
        chunk: EvidenceChunk,
        resolutions: tuple[ChemicalResolution, ...],
        decision: ValidationDecision,
        observation_records: list[ObservationDisposition],
        human_review_tasks: list[HumanReviewTask],
    ) -> None:
        outcome = finalize_candidate_validation(candidate, decision)
        terminal_status = outcome.terminal_status
        disposition = coarse_disposition_for_terminal(terminal_status)
        output_stream = outcome.output_stream
        policy_rule_id = outcome.policy_rule_id
        canonical, reported, replacement = chemical_name_fields(candidate)
        review_payload = {
            "candidate": candidate,
            "decision": decision.to_dict(),
            "resolutions": [item.to_dict() for item in resolutions],
            "evidence_chunk": chunk.to_dict(),
            "terminal_status": terminal_status,
            "output_stream": output_stream,
            "policy_rule_id": policy_rule_id,
        }
        observation_records.append(
            ObservationDisposition(
                record_id=f"observation-{uuid.uuid4()}",
                candidate_id=candidate_id,
                disposition=disposition,
                canonical_name=canonical,
                reported_name=reported,
                replacement_name=replacement,
                payload=review_payload,
                terminal_status=terminal_status,
                output_stream=output_stream,
                policy_rule_id=policy_rule_id,
            )
        )
        if disposition == "pending_human_review" and terminal_requires_human_review(
            terminal_status, decision
        ):
            human_review_tasks.append(
                HumanReviewTask(
                    task_id=f"human-review-{uuid.uuid4()}",
                    candidate_id=candidate_id,
                    priority=_human_priority(decision),
                    reason_codes=decision.reason_codes,
                    payload=review_payload,
                )
            )

def _copy_component(component: _ComponentT, label: str) -> _ComponentT:
    try:
        return copy.deepcopy(component)
    except Exception as exc:
        raise TypeError(
            f"{label} cannot be safely copied for a fresh document; provide {label}_factory"
        ) from exc


def _flush_component(component: object | None) -> None:
    """Persist shared caches at every document barrier without masking document results."""
    if component is None:
        return
    flush = getattr(component, "flush", None)
    if callable(flush):
        # The committed document/control-plane rows remain authoritative. A later run can
        # rebuild the optional external-resolution cache from resolution events.
        with suppress(Exception):
            flush()


def _close_component(component: object) -> None:
    close = getattr(component, "close", None)
    if callable(close):
        close()


_CURATED_HCH_ISOMERS = {
    "alpha": ("alpha-Hexachlorocyclohexane", "319-84-6"),
    "beta": ("beta-Hexachlorocyclohexane", "319-85-7"),
    "gamma": ("gamma-Hexachlorocyclohexane", "58-89-9"),
}


def _curated_specific_identity_resolution(
    raw_name: str, *, registry_snapshot_version: int
) -> ChemicalResolution | None:
    """Resolve reviewed HCH isomer aliases without trusting PubChem's generic HCH synonym hit."""
    repaired = _repair_chemical_text(raw_name)
    ascii_name = repaired
    for greek, word in _GREEK_QUERY_NAMES.items():
        ascii_name = ascii_name.replace(greek, word)
    folded = re.sub(r"[^a-z0-9]+", "-", ascii_name.casefold()).strip("-")
    if "hch" not in folded and "hexachlorocyclohexane" not in folded:
        return None
    isomers = [
        name
        for name in _CURATED_HCH_ISOMERS
        if re.search(rf"(?:^|-)({name})(?:-|$)", folded)
    ]
    if len(isomers) != 1:
        return None
    isomer = isomers[0]
    canonical_name, cas_rn = _CURATED_HCH_ISOMERS[isomer]
    return ChemicalResolution(
        raw_name=raw_name,
        normalized_query=ascii_name,
        status="validated_local",
        resolver_name="ecmonitor_curated_hch_isomer_map",
        matches=(
            ChemicalMatch(
                source="human_reviewed_curated_identity",
                source_record_id=f"CAS:{cas_rn}",
                canonical_name=canonical_name,
                matched_alias=repaired,
                cas_candidates=(cas_rn,),
                synonyms=(f"{isomer}-HCH",),
            ),
        ),
        warnings=("human_reviewed_isomer_mapping",),
        registry_snapshot_version=registry_snapshot_version,
    )


_MOJIBAKE_GREEK = {
    "Î±": "α",
    "Î²": "β",
    "Î³": "γ",
    "Î´": "δ",
}
_GREEK_QUERY_NAMES = {"α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta"}
_ISOMER_QUALIFIER = re.compile(
    r"(?:[αβγδ]|(?<![a-z])(?:alpha|beta|gamma|delta|cis|trans)(?![a-z]))",
    re.IGNORECASE,
)


def _repair_chemical_text(value: str) -> str:
    repaired = value
    for broken, greek in _MOJIBAKE_GREEK.items():
        repaired = repaired.replace(broken, greek)
    return " ".join(repaired.split())


def _chemical_query_variants(value: str) -> list[str]:
    repaired = _repair_chemical_text(value)
    ascii_variant = repaired
    for greek, name in _GREEK_QUERY_NAMES.items():
        ascii_variant = ascii_variant.replace(greek, name)
    return [repaired] if ascii_variant == repaired else [repaired, ascii_variant]


def _candidate_lookup_names(candidate: dict[str, Any], raw_name: str) -> list[str]:
    """Return resolver queries without discarding document-local or isomer specificity.

    A document-defined short abbreviation such as ``tetracycline (TC)`` must query the
    expanded local definition before the globally ambiguous token ``TC``. Conversely, a raw
    name carrying an isomer qualifier must stay ahead of a generic proposed parent name. PDF
    mojibake for Greek letters is repaired only for lookup; the reported name remains verbatim.
    """
    analyte = candidate.get("analyte")
    if not isinstance(analyte, dict):
        return _chemical_query_variants(raw_name)

    proposed = analyte.get("proposed_canonical_name")
    matched = analyte.get("matched_alias")
    replacement = analyte.get("replacement_name")
    raw_repaired = _repair_chemical_text(raw_name)
    has_isomer_qualifier = bool(_ISOMER_QUALIFIER.search(raw_repaired))
    document_local = analyte.get("alias_type") == "document_local"

    ordered: list[str] = []
    if has_isomer_qualifier:
        ordered.extend(_chemical_query_variants(raw_name))
        for value in (matched, proposed, replacement):
            if isinstance(value, str) and value.strip():
                ordered.extend(_chemical_query_variants(value))
    elif document_local and isinstance(proposed, str) and proposed.strip():
        ordered.extend(_chemical_query_variants(proposed))
        for value in (matched, raw_name, replacement):
            if isinstance(value, str) and value.strip():
                ordered.extend(_chemical_query_variants(value))
    else:
        for value in (raw_name, matched, proposed, replacement):
            if isinstance(value, str) and value.strip():
                ordered.extend(_chemical_query_variants(value))

    result: list[str] = []
    seen: set[str] = set()
    for value in ordered:
        normalized = " ".join(value.split())
        folded = normalized.casefold()
        if normalized and folded not in seen:
            result.append(normalized)
            seen.add(folded)
    return result


def _relabel_resolution(
    resolution: ChemicalResolution,
    *,
    raw_name: str,
    registry_snapshot_version: int,
    fallback_query: str,
) -> ChemicalResolution:
    warnings = list(resolution.warnings)
    if fallback_query.casefold() != raw_name.casefold():
        warnings.append(f"chemical_query_fallback:{fallback_query}")
    return ChemicalResolution(
        raw_name=raw_name,
        normalized_query=resolution.normalized_query,
        status=resolution.status,
        resolver_name=resolution.resolver_name,
        matches=resolution.matches,
        warnings=tuple(dict.fromkeys(warnings)),
        registry_snapshot_version=registry_snapshot_version,
    )


def _candidate_raw_names(candidate: dict[str, Any]) -> list[str]:
    analyte = candidate.get("analyte")
    if not isinstance(analyte, dict):
        return []
    raw = analyte.get("raw_name")
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def _resolution_dedup_key(resolution: ChemicalResolution) -> str:
    match_ids = ",".join(item.source_record_id for item in resolution.matches)
    return f"{resolution.normalized_query}\0{resolution.resolver_name}\0{match_ids}"


def _ordered_unique(values: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values if str(value)))


def _method_kwargs(method: Callable[..., Any], *names: str) -> set[str]:
    """Which of ``names`` a callable accepts (explicitly or via **kwargs)."""
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return set()
    parameters = signature.parameters
    accepted = {name for name in names if name in parameters}
    if any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        accepted.update(names)
    return accepted


def _human_priority(
    decision: ValidationDecision,
) -> Literal["low", "medium", "high", "critical"]:
    reasons = set(decision.reason_codes)
    if reasons & {"chemical_identity_conflict", "analyte_value_binding_conflict"}:
        return "critical"
    if reasons & {"retry_budget_exhausted", "chemical_identity_unresolved"}:
        return "high"
    return "medium"


def _count_disposition(
    records: list[ObservationDisposition], disposition: str
) -> int:
    return sum(record.disposition == disposition for record in records)

def _terminal_status_counts(records: list[ObservationDisposition]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        counts[record.terminal_status] = counts.get(record.terminal_status, 0) + 1
    return dict(sorted(counts.items()))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def publication_year_from_doi(doi: str | None) -> int | None:
    """Best-effort publication year derived from a DOI string.

    Conservative on purpose: ambiguous or ISSN-like digit groups are skipped rather than
    misreported. Handles common Elsevier/Springer 4-digit placements and the two-digit
    year used by MDPI-style journal slugs.
    """
    if not doi or not isinstance(doi, str):
        return None
    body = doi.split("/", 1)[1] if "/" in doi else doi
    # Drop ISSN-like journal prefixes (e.g. ``s1872-2040``) so their year-like block is ignored.
    body = re.sub(r"(?i)^s\d{3,}\s*-\s*\d{4}\s*", "", body)
    compact = re.sub(r"[^0-9a-z]+", "", body, flags=re.IGNORECASE)
    # Preferred: a 4-digit year bounded by non-digits (Elsevier ``.../YYYY.xxx`` etc.).
    for match in re.finditer(r"(?<![0-9])((?:19|20)\d{2})(?![0-9])", body):
        year = int(match.group(1))
        if 1950 <= year <= 2100:
            return year
    # MDPI-style: journal slug + two-digit year + month + article id (e.g. atmos15060665 -> 2015).
    slug_year = re.search(
        r"(?i)^([a-z]+)(\d{2})(\d{2})(\d{4,5})$", compact
    )
    if slug_year:
        yy = int(slug_year.group(2))
        return 2000 + yy if yy <= 60 else 1900 + yy
    return None


def _document_context(bibliographic_metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not bibliographic_metadata:
        return {}
    context = {key: value for key, value in bibliographic_metadata.items() if value is not None}
    context.setdefault("publication_year", publication_year_from_doi(context.get("doi")))
    return context
