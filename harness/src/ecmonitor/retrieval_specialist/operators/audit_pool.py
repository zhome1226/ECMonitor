"""Seed, sentinel, and external-audit recall checks for retrieval runs."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from ecmonitor.retrieval_specialist.operators.metadata_normalizer import (
    normalize_doi,
    normalize_title,
)
from ecmonitor.retrieval_specialist.storage.atomic_io import read_yaml

AUDIT_POOL_ROLES = {"development_seed", "holdout_sentinel", "external_audit"}
EXPECTED_RELEVANCE = {"include", "defer", "exclude"}
TITLE_CONTAINS_MIN_LEN = 20
TITLE_FUZZY_THRESHOLD = 0.86
TITLE_TOKEN_SUBSET_MIN_TOKENS = 5
TITLE_TOKEN_SUBSET_MIN_COVERAGE = 0.85


@dataclass(frozen=True)
class AuditPoolEntry:
    audit_id: str
    role: str
    expected_relevance: str
    title: str
    doi: str | None = None
    pmid: str | None = None
    openalex_id: str | None = None
    semantic_scholar_id: str | None = None
    rationale: str = ""
    source_reference: str = ""

    @property
    def normalized_title(self) -> str:
        return normalize_title(self.title)

    @property
    def normalized_doi(self) -> str | None:
        return normalize_doi(self.doi)


class AuditPoolEvaluator:
    """Evaluate whether a run retrieved known seed/sentinel/audit records."""

    def __init__(self, entries: list[AuditPoolEntry]) -> None:
        self.entries = entries

    @classmethod
    def from_yaml(cls, path: Path) -> AuditPoolEvaluator:
        payload = read_yaml(path)
        if not isinstance(payload, dict):
            raise ValueError(f"Audit pool must be a mapping: {path}")
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list):
            raise ValueError(f"Audit pool must contain an entries list: {path}")
        entries = [_entry_from_mapping(item, path) for item in raw_entries]
        return cls(entries)

    def evaluate(self, documents: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        document_index = _DocumentIndex(documents)
        rows = [self._evaluate_entry(entry, document_index) for entry in self.entries]
        return {
            "audit_pool_recall": rows,
            "audit_pool_recall_summary": _summary_rows(rows),
        }

    def _evaluate_entry(
        self, entry: AuditPoolEntry, document_index: _DocumentIndex
    ) -> dict[str, Any]:
        match = document_index.match(entry)
        retrieved = match is not None
        return {
            "audit_id": entry.audit_id,
            "role": entry.role,
            "expected_relevance": entry.expected_relevance,
            "retrieved": int(retrieved),
            "match_status": "retrieved" if retrieved else "missing",
            "match_method": match["match_method"] if match else "",
            "global_record_id": match["global_record_id"] if match else "",
            "matched_title": match["canonical_title"] if match else "",
            "matched_query_ids": match["query_ids"] if match else [],
            "matched_sources": match["sources"] if match else [],
            "title": entry.title,
            "doi": entry.normalized_doi or "",
            "pmid": entry.pmid or "",
            "openalex_id": entry.openalex_id or "",
            "semantic_scholar_id": entry.semantic_scholar_id or "",
            "rationale": entry.rationale,
            "source_reference": entry.source_reference,
        }


class _DocumentIndex:
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self.by_doi: dict[str, dict[str, Any]] = {}
        self.by_pmid: dict[str, dict[str, Any]] = {}
        self.by_openalex: dict[str, dict[str, Any]] = {}
        self.by_semantic_scholar: dict[str, dict[str, Any]] = {}
        self.by_title: dict[str, dict[str, Any]] = {}
        for document in documents:
            self._add(self.by_doi, document.get("normalized_doi"), document)
            self._add(self.by_pmid, document.get("pmid"), document)
            self._add(self.by_openalex, document.get("openalex_id"), document)
            self._add(
                self.by_semantic_scholar,
                document.get("semantic_scholar_id"),
                document,
            )
            self._add(self.by_title, document.get("normalized_title"), document)

    def match(self, entry: AuditPoolEntry) -> dict[str, Any] | None:
        candidates = [
            ("doi", self.by_doi.get(entry.normalized_doi or "")),
            ("pmid", self.by_pmid.get(entry.pmid or "")),
            ("openalex_id", self.by_openalex.get(entry.openalex_id or "")),
            (
                "semantic_scholar_id",
                self.by_semantic_scholar.get(entry.semantic_scholar_id or ""),
            ),
            ("normalized_title", self.by_title.get(entry.normalized_title)),
        ]
        for method, document in candidates:
            if document is not None:
                return dict(document) | {"match_method": method}
        title_match = self._title_fallback(entry.normalized_title)
        if title_match is not None:
            document, method = title_match
            return dict(document) | {"match_method": method}
        return None

    def _add(
        self, index: dict[str, dict[str, Any]], value: object, document: dict[str, Any]
    ) -> None:
        if value is None:
            return
        key = str(value).strip()
        if key:
            index.setdefault(key, document)

    def _title_fallback(self, title: str) -> tuple[dict[str, Any], str] | None:
        if len(title) < TITLE_CONTAINS_MIN_LEN:
            return None
        loose_title = _loose_title(title)
        best_document: dict[str, Any] | None = None
        best_score = 0.0
        for candidate_title, document in self.by_title.items():
            if _safe_title_contains_match(title, candidate_title):
                return document, "normalized_title_contains"
            loose_candidate = _loose_title(candidate_title)
            if _safe_title_contains_match(loose_title, loose_candidate):
                return document, "normalized_title_contains"
            if _token_subset_match(loose_title, loose_candidate):
                return document, "normalized_title_token_subset"
            score = SequenceMatcher(None, title, candidate_title).ratio()
            if score > best_score:
                best_score = score
                best_document = document
        if best_document is not None and best_score >= TITLE_FUZZY_THRESHOLD:
            return best_document, "normalized_title_fuzzy"
        return None


def _loose_title(title: str) -> str:
    return " ".join(
        title.replace("-", " ")
        .replace("(", " ")
        .replace(")", " ")
        .replace("/", " ")
        .split()
    )


def _token_subset_match(title: str, candidate_title: str) -> bool:
    title_tokens = _significant_title_tokens(title)
    if len(title_tokens) < TITLE_TOKEN_SUBSET_MIN_TOKENS:
        return False
    candidate_tokens = set(_significant_title_tokens(candidate_title))
    if not candidate_tokens:
        return False
    covered = sum(1 for token in title_tokens if token in candidate_tokens)
    return covered / len(title_tokens) >= TITLE_TOKEN_SUBSET_MIN_COVERAGE


def _safe_title_contains_match(title: str, candidate_title: str) -> bool:
    """Avoid matching broad generic titles embedded in longer audit titles."""
    if title not in candidate_title and candidate_title not in title:
        return False
    shorter = title if len(title) <= len(candidate_title) else candidate_title
    longer = candidate_title if len(title) <= len(candidate_title) else title
    shorter_tokens = _significant_title_tokens(shorter)
    if len(shorter_tokens) < TITLE_TOKEN_SUBSET_MIN_TOKENS:
        return False
    return len(shorter) / max(len(longer), 1) >= 0.45


def _significant_title_tokens(title: str) -> list[str]:
    stopwords = {
        "and",
        "the",
        "for",
        "with",
        "using",
        "from",
        "into",
        "between",
        "among",
        "level",
        "levels",
        "trace",
    }
    tokens = []
    for token in title.casefold().split():
        stripped = "".join(char for char in token if char.isalnum())
        if len(stripped) >= 3 and stripped not in stopwords:
            tokens.append(stripped)
    return tokens


def _entry_from_mapping(payload: object, path: Path) -> AuditPoolEntry:
    if not isinstance(payload, dict):
        raise ValueError(f"Audit pool entry must be a mapping in {path}")
    audit_id = _required_string(payload, "audit_id", path)
    role = _required_string(payload, "role", path)
    expected_relevance = _required_string(payload, "expected_relevance", path)
    title = _required_string(payload, "title", path)
    if role not in AUDIT_POOL_ROLES:
        raise ValueError(f"Unsupported audit pool role for {audit_id}: {role}")
    if expected_relevance not in EXPECTED_RELEVANCE:
        raise ValueError(
            f"Unsupported expected_relevance for {audit_id}: {expected_relevance}"
        )
    return AuditPoolEntry(
        audit_id=audit_id,
        role=role,
        expected_relevance=expected_relevance,
        title=title,
        doi=_optional_string(payload.get("doi")),
        pmid=_optional_string(payload.get("pmid")),
        openalex_id=_optional_string(payload.get("openalex_id")),
        semantic_scholar_id=_optional_string(payload.get("semantic_scholar_id")),
        rationale=_optional_string(payload.get("rationale")) or "",
        source_reference=_optional_string(payload.get("source_reference")) or "",
    )


def _required_string(payload: dict[str, Any], key: str, path: Path) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Audit pool entry missing required string `{key}` in {path}")
    return value.strip()


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return str(value)
    stripped = value.strip()
    return stripped or None


def _summary_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    roles = sorted({str(row["role"]) for row in rows})
    summary = [_summary_row("all", rows)]
    summary.extend(
        _summary_row(role, [row for row in rows if row["role"] == role]) for role in roles
    )
    return summary


def _summary_row(role: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    retrieved = sum(int(row["retrieved"]) for row in rows)
    missing = total - retrieved
    return {
        "role": role,
        "total": total,
        "retrieved": retrieved,
        "missing": missing,
        "recall": retrieved / total if total else "",
        "missing_audit_ids": [
            str(row["audit_id"]) for row in rows if int(row["retrieved"]) == 0
        ],
    }
