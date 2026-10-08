"""Provenance-aware deduplication."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ecmonitor.retrieval_specialist.models import NormalizedRecord


@dataclass(frozen=True)
class DeduplicationResult:
    records: list[NormalizedRecord]
    duplicate_count: int
    candidate_duplicates: list[dict[str, object]]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class Deduplicator:
    """Deduplicate stable identifiers and queue DOI-less title matches."""

    def deduplicate(self, records: list[NormalizedRecord]) -> DeduplicationResult:
        by_identifier: dict[str, NormalizedRecord] = {}
        candidate_groups: dict[tuple[str, int | None, str | None], list[str]] = {}
        duplicate_count = 0
        candidate_duplicates: list[dict[str, object]] = []
        for record in records:
            key = self._stable_key(record)
            if key and key in by_identifier:
                duplicate_count += 1
                by_identifier[key] = self._merge(by_identifier[key], record)
                continue
            if key:
                by_identifier[key] = record
            else:
                title_key = (record.title_normalized, record.publication_year, record.first_author)
                candidate_groups.setdefault(title_key, []).append(record.global_record_id)
                by_identifier[record.global_record_id] = record

        for values in candidate_groups.values():
            if len(values) > 1:
                candidate_duplicates.append(
                    {
                        "candidate_cluster_id": f"candidate:{len(candidate_duplicates) + 1:04d}",
                        "confidence": "candidate_only",
                        "matched_fields": ["title_normalized", "publication_year", "first_author"],
                        "conflicting_fields": [],
                        "source_provenance": values,
                        "merge_decision": "not_merged",
                        "merge_actor": "Retrieval Specialist",
                        "merge_timestamp": None,
                    }
                )

        return DeduplicationResult(
            records=list(by_identifier.values()),
            duplicate_count=duplicate_count,
            candidate_duplicates=candidate_duplicates,
        )

    def _stable_key(self, record: NormalizedRecord) -> str | None:
        if record.normalized_doi:
            return f"doi:{record.normalized_doi}"
        if record.pmid:
            return f"pmid:{record.pmid}"
        if record.openalex_id:
            return f"openalex:{record.openalex_id}"
        if record.semantic_scholar_id:
            return f"s2:{record.semantic_scholar_id}"
        if record.crossref_id:
            return f"crossref:{record.crossref_id}"
        return None

    def _merge(self, existing: NormalizedRecord, duplicate: NormalizedRecord) -> NormalizedRecord:
        existing_dict = existing.to_dict()
        existing_dict["source_records"] = existing.source_records + duplicate.source_records
        existing_dict["retrieved_from"] = sorted(
            set(existing.retrieved_from + duplicate.retrieved_from)
        )
        existing_dict["keywords"] = sorted(set(existing.keywords + duplicate.keywords))
        existing_dict["issn"] = sorted(set(existing.issn + duplicate.issn))
        existing_dict["eissn"] = sorted(set(existing.eissn + duplicate.eissn))
        return NormalizedRecord(**existing_dict)
