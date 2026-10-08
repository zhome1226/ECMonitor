import pytest

from ecmonitor.retrieval_specialist.models import (
    CanonicalQuery,
    NormalizedRecord,
    ScreeningDecision,
)
from ecmonitor.retrieval_specialist.operators.evaluator import EvaluationContext, QueryEvaluator
from ecmonitor.retrieval_specialist.orchestration.state_machine import (
    InvalidStateTransition,
    RetrievalState,
    StateMachine,
)


def query() -> CanonicalQuery:
    return CanonicalQuery(
        query_id="Q0001",
        parent_query_id=None,
        iteration=1,
        date_from="2006-01-01",
        date_to="2026-07-09",
        document_types=["journal article"],
        emerging_contaminant_terms=["emerging contaminant*"],
        surface_water_terms=["river"],
        monitoring_and_concentration_terms=["concentration"],
        created_at="2026-07-09T00:00:00Z",
    )


def normalized(record_id: str) -> NormalizedRecord:
    return NormalizedRecord(
        global_record_id=record_id,
        source_records=[],
        doi=None,
        normalized_doi=None,
        pmid=None,
        openalex_id=None,
        semantic_scholar_id=None,
        crossref_id=None,
        title_original="A",
        title_normalized="a",
        abstract_original="Measured concentration.",
        abstract_source="mock",
        keywords=[],
        authors=[],
        first_author=None,
        publication_date=None,
        publication_year=2020,
        journal_title=None,
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


def scoring_config():
    return {
        "positive_weights": {
            "novel_precision_at_20": 0.35,
            "normalized_novel_eligible_yield": 0.25,
            "novelty_rate": 0.15,
            "cross_source_breadth": 0.10,
            "scope_diversity": 0.10,
            "metadata_completeness": 0.05,
        },
        "penalty_weights": {
            "defer_rate": 0.20,
            "excluded_matrix_rate": 0.25,
            "laboratory_study_rate": 0.20,
            "no_concentration_rate": 0.20,
            "known_ineligible_overlap_rate": 0.10,
            "query_complexity": 0.05,
        },
    }


def test_query_scoring_is_deterministic() -> None:
    records = [normalized("r1")]
    decisions = [
        ScreeningDecision(
            global_record_id="r1",
            decision="include",
            reason_codes=["I_SURFACE_WATER_CONCENTRATION"],
            evidence_spans=[],
            article_ec_scope="true",
        )
    ]
    context = EvaluationContext(
        run_id="run",
        target_novel_n=20,
        source_count=4,
        raw_result_count=1,
        scanned_result_count=1,
        duplicate_count=0,
        source_completeness="complete",
        config_hash="cfg",
        prompt_hash="prompt",
        code_commit_sha="sha",
    )
    evaluator = QueryEvaluator(scoring_config())
    first = evaluator.evaluate(query(), records, decisions, context)
    second = evaluator.evaluate(query(), records, decisions, context)
    assert first.total_score == second.total_score
    assert first.positive_score == second.positive_score
    assert first.penalty_score == second.penalty_score


def test_invalid_state_transition_raises_typed_exception(tmp_path) -> None:
    machine = StateMachine(tmp_path)
    with pytest.raises(InvalidStateTransition):
        machine.transition(RetrievalState.SEARCH_SOURCES)
