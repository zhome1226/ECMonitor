import json
from pathlib import Path

import pytest
from jsonschema import ValidationError, validate

from ecmonitor.retrieval_specialist.models import NormalizedRecord
from ecmonitor.retrieval_specialist.operators.screener import TitleAbstractScreener


def _schema(name: str) -> dict[str, object]:
    path = Path(__file__).resolve().parents[2] / "schemas" / "retrieval" / name
    return json.loads(path.read_text(encoding="utf-8"))


def _record() -> NormalizedRecord:
    return NormalizedRecord(
        global_record_id="doi:10.1000/schema",
        source_records=[],
        doi="10.1000/schema",
        normalized_doi="10.1000/schema",
        pmid=None,
        openalex_id=None,
        semantic_scholar_id=None,
        crossref_id=None,
        title_original="Emerging contaminants in river water",
        title_normalized="emerging contaminants in river water",
        abstract_original="Measured concentrations in river water.",
        abstract_source="mock",
        keywords=["river"],
        authors=["Chen"],
        first_author="Chen",
        publication_date="2020-01-01",
        publication_year=2020,
        journal_title="Mock Journal",
        issn=[],
        eissn=[],
        document_type="journal article",
        language="en",
        source_rank=1,
        source_relevance_score=None,
        retrieved_from=["crossref"],
        retrieval_timestamp="2026-07-09T00:00:00Z",
        raw_metadata_path=None,
        sampled_matrices=["river"],
        study_type="field monitoring",
        has_real_field_sample=True,
        has_concentration_evidence=True,
        article_ec_scope="true",
    )


def test_screening_decision_validates_against_full_schema() -> None:
    decision = TitleAbstractScreener().screen_one(
        _record(), run_id="run", query_id="Q0001", iteration=1
    )
    validate(instance=decision.to_dict(), schema=_schema("screening_decision.schema.json"))


def test_invalid_decision_enum_fails_schema_validation() -> None:
    decision = TitleAbstractScreener().screen_one(
        _record(), run_id="run", query_id="Q0001", iteration=1
    ).to_dict()
    decision["decision"] = "maybe"
    with pytest.raises(ValidationError):
        validate(instance=decision, schema=_schema("screening_decision.schema.json"))


def test_invalid_concentration_enum_fails_schema_validation() -> None:
    decision = TitleAbstractScreener().screen_one(
        _record(), run_id="run", query_id="Q0001", iteration=1
    ).to_dict()
    decision["concentration_evidence"] = "numeric-ish"
    with pytest.raises(ValidationError):
        validate(instance=decision, schema=_schema("screening_decision.schema.json"))


def test_missing_required_screening_field_fails_schema_validation() -> None:
    decision = TitleAbstractScreener().screen_one(
        _record(), run_id="run", query_id="Q0001", iteration=1
    ).to_dict()
    del decision["screening_decision_id"]
    with pytest.raises(ValidationError):
        validate(instance=decision, schema=_schema("screening_decision.schema.json"))
