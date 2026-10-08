from ecmonitor.retrieval_specialist.models import RawRecord
from ecmonitor.retrieval_specialist.operators.deduplicator import Deduplicator
from ecmonitor.retrieval_specialist.operators.metadata_normalizer import (
    MetadataNormalizer,
    normalize_doi,
)


def raw(source: str, source_id: str, payload: dict) -> RawRecord:
    return RawRecord(
        source_name=source,
        source_record_id=source_id,
        rank=1,
        raw=payload,
        retrieval_timestamp="2026-07-09T00:00:00Z",
    )


def test_doi_normalization_strips_prefixes_and_case() -> None:
    assert normalize_doi("https://doi.org/10.1000/ABC") == "10.1000/abc"
    assert normalize_doi("DOI: 10.1000/XYZ") == "10.1000/xyz"


def test_normalizer_reads_nested_gateway_raw_payload() -> None:
    normalized = MetadataNormalizer().normalize(
        raw(
            "crossref",
            "10.1000/NESTED",
            {
                "source_name": "crossref",
                "raw": {
                    "doi": "10.1000/NESTED",
                    "title": "Nested Gateway Title",
                    "abstract": "Nested abstract",
                    "authors": ["A"],
                    "year": "2024",
                    "journal": "Water Research",
                    "document_type": "journal-article",
                    "language": "en",
                },
            },
        )
    )

    assert normalized.title_original == "Nested Gateway Title"
    assert normalized.abstract_original == "Nested abstract"
    assert normalized.normalized_doi == "10.1000/nested"
    assert normalized.publication_year == 2024
    assert normalized.journal_title == "Water Research"
    assert normalized.document_type == "journal-article"
    assert normalized.language == "en"


def test_identical_doi_records_are_merged_across_sources() -> None:
    normalizer = MetadataNormalizer()
    records = [
        normalizer.normalize(
            raw(
                "crossref",
                "a",
                {"doi": "10.1000/A", "title": "A", "authors": ["A"], "publication_year": 2020},
            )
        ),
        normalizer.normalize(
            raw(
                "openalex",
                "b",
                {
                    "doi": "https://doi.org/10.1000/a",
                    "title": "A",
                    "authors": ["A"],
                    "publication_year": 2020,
                },
            )
        ),
    ]
    result = Deduplicator().deduplicate(records)
    assert result.duplicate_count == 1
    assert len(result.records) == 1
    assert result.records[0].retrieved_from == ["crossref", "openalex"]


def test_pmid_records_are_merged() -> None:
    normalizer = MetadataNormalizer()
    records = [
        normalizer.normalize(
            raw(
                "pubmed",
                "a",
                {"pmid": "123", "title": "A", "authors": ["A"], "publication_year": 2020},
            )
        ),
        normalizer.normalize(
            raw(
                "crossref",
                "b",
                {"pmid": "123", "title": "A", "authors": ["A"], "publication_year": 2020},
            )
        ),
    ]
    result = Deduplicator().deduplicate(records)
    assert result.duplicate_count == 1
    assert len(result.records) == 1


def test_doi_less_title_matches_are_candidate_duplicates_only() -> None:
    normalizer = MetadataNormalizer()
    records = [
        normalizer.normalize(
            raw(
                "openalex", "a", {"title": "Same title", "authors": ["A"], "publication_year": 2020}
            )
        ),
        normalizer.normalize(
            raw(
                "semantic_scholar",
                "b",
                {"title": "Same title", "authors": ["A"], "publication_year": 2020},
            )
        ),
    ]
    result = Deduplicator().deduplicate(records)
    assert result.duplicate_count == 0
    assert len(result.records) == 2
    assert len(result.candidate_duplicates) == 1
