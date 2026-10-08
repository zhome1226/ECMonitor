"""Canonical metadata normalization."""

from __future__ import annotations

import re
from hashlib import sha256
from typing import Any

from ecmonitor.retrieval_specialist.models import NormalizedRecord, RawRecord


def normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    value = doi.strip().lower()
    value = re.sub(r"^https?://(dx\.)?doi\.org/", "", value)
    value = re.sub(r"^doi:\s*", "", value)
    return value or None


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.strip().lower())


class MetadataNormalizer:
    """Normalize source records while retaining original fields in raw metadata."""

    def normalize(
        self, record: RawRecord, raw_metadata_path: str | None = None
    ) -> NormalizedRecord:
        raw = record.raw
        source_raw = self._source_raw(raw)
        title_original = self._str_value(source_raw.get("title")) or ""
        title_normalized = normalize_title(title_original)
        doi = self._str_value(source_raw.get("doi"))
        normalized_doi = normalize_doi(doi)
        authors = self._str_list(source_raw.get("authors"))
        year = source_raw.get("publication_year") or source_raw.get("year")
        if year is not None:
            year = int(year)
        global_record_id = self._global_record_id(record, normalized_doi, title_normalized, year)
        return NormalizedRecord(
            global_record_id=global_record_id,
            source_records=[
                {
                    "source_name": record.source_name,
                    "source_record_id": record.source_record_id,
                    "rank": record.rank,
                }
            ],
            doi=doi,
            normalized_doi=normalized_doi,
            pmid=self._str_value(source_raw.get("pmid")),
            openalex_id=self._str_value(source_raw.get("openalex_id")),
            semantic_scholar_id=self._str_value(source_raw.get("semantic_scholar_id")),
            crossref_id=self._str_value(source_raw.get("crossref_id")),
            title_original=title_original,
            title_normalized=title_normalized,
            abstract_original=self._str_value(source_raw.get("abstract")),
            abstract_source=self._str_value(source_raw.get("abstract_source")),
            keywords=self._str_list(source_raw.get("keywords")),
            authors=authors,
            first_author=authors[0] if authors else None,
            publication_date=self._str_value(source_raw.get("publication_date")),
            publication_year=year,
            journal_title=self._str_value(
                source_raw.get("journal_title") or source_raw.get("journal")
            ),
            issn=self._str_list(source_raw.get("issn")),
            eissn=self._str_list(source_raw.get("eissn")),
            document_type=self._str_value(source_raw.get("document_type")),
            language=self._str_value(source_raw.get("language")),
            source_rank=record.rank,
            source_relevance_score=self._float_value(source_raw.get("source_relevance_score")),
            retrieved_from=[record.source_name],
            retrieval_timestamp=record.retrieval_timestamp,
            raw_metadata_path=raw_metadata_path,
            sampled_matrices=[
                value.lower() for value in self._str_list(source_raw.get("sampled_matrices"))
            ],
            study_type=self._str_value(source_raw.get("study_type")),
            has_real_field_sample=self._bool_value(source_raw.get("has_real_field_sample")),
            has_concentration_evidence=self._bool_value(
                source_raw.get("has_concentration_evidence")
            ),
            article_ec_scope=self._str_value(source_raw.get("article_ec_scope")) or "uncertain",
        )

    def _source_raw(self, raw: dict[str, Any]) -> dict[str, Any]:
        nested = raw.get("raw")
        if isinstance(nested, dict):
            return nested
        return raw

    def _str_value(self, value: object) -> str | None:
        if value is None:
            return None
        return str(value)

    def _str_list(self, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item) for item in value]
        if isinstance(value, tuple):
            return [str(item) for item in value]
        return [str(value)]

    def _float_value(self, value: object) -> float | None:
        if value is None:
            return None
        if isinstance(value, int | float | str):
            return float(value)
        return None

    def _bool_value(self, value: object) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        return bool(value)

    def _global_record_id(
        self, record: RawRecord, normalized_doi: str | None, title_normalized: str, year: int | None
    ) -> str:
        if normalized_doi:
            return f"doi:{normalized_doi}"
        raw = record.raw
        source_raw = self._source_raw(raw)
        if source_raw.get("pmid"):
            return f"pmid:{source_raw['pmid']}"
        if source_raw.get("openalex_id"):
            return f"openalex:{source_raw['openalex_id']}"
        if source_raw.get("semantic_scholar_id"):
            return f"s2:{source_raw['semantic_scholar_id']}"
        digest = sha256(f"{title_normalized}|{year}|{record.source_name}".encode()).hexdigest()
        return f"title:{digest[:16]}"
