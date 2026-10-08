"""Novelty-aware query evaluation and deterministic scoring."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ecmonitor.retrieval_specialist.models import (
    CanonicalQuery,
    NormalizedRecord,
    QueryMetrics,
    ScreeningDecision,
)
from ecmonitor.retrieval_specialist.operators.time_utils import utc_now_iso


def clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class EvaluationContext:
    run_id: str
    target_novel_n: int
    source_count: int
    raw_result_count: int
    scanned_result_count: int
    duplicate_count: int
    source_completeness: str
    config_hash: str
    prompt_hash: str
    code_commit_sha: str


class QueryEvaluator:
    """Compute score components separately from the final score."""

    def __init__(self, scoring_config: dict[str, object]) -> None:
        self.positive_weights = self._weights(scoring_config["positive_weights"])
        self.penalty_weights = self._weights(scoring_config["penalty_weights"])

    def _weights(self, payload: object) -> dict[str, float]:
        if not isinstance(payload, Mapping):
            raise TypeError("scoring weights must be a mapping")
        return {str(key): float(value) for key, value in payload.items()}

    def evaluate(
        self,
        query: CanonicalQuery,
        records: Sequence[NormalizedRecord],
        decisions: Sequence[ScreeningDecision],
        context: EvaluationContext,
        parent_score: float = 0.0,
    ) -> QueryMetrics:
        novel_record_count = len(records)
        include_count = sum(1 for decision in decisions if decision.decision == "include")
        exclude_count = sum(1 for decision in decisions if decision.decision == "exclude")
        defer_count = sum(1 for decision in decisions if decision.decision.startswith("defer"))
        denominator = max(1, include_count + exclude_count + defer_count)
        novelty_sample_decisions = list(decisions[:20])
        evaluated_novel_sample = [
            decision
            for decision in novelty_sample_decisions
            if decision.decision in {"include", "exclude"}
        ]
        eligible_precision = include_count / max(1, include_count + exclude_count)
        novel_precision_at_20 = (
            sum(1 for decision in evaluated_novel_sample if decision.decision == "include")
            / max(1, len(evaluated_novel_sample))
        )
        evaluated_count = include_count + exclude_count
        defer_rate = defer_count / denominator
        novelty_rate = novel_record_count / max(1, context.scanned_result_count)
        normalized_yield = clamp(include_count / max(1, context.target_novel_n))
        source_names = {source for record in records for source in record.retrieved_from}
        cross_source_breadth = clamp(len(source_names) / max(1, context.source_count))
        metadata_completeness = self._metadata_completeness(records)
        scope_diversity = self._scope_diversity(records)
        excluded_matrix_rate = self._reason_rate(
            decisions, {"E_MIXED_MATRIX", "E_GROUNDWATER", "E_WATER_PLANT", "E_WASTEWATER"}
        )
        laboratory_rate = self._reason_rate(decisions, {"E_LAB_STUDY"})
        no_concentration_rate = self._reason_rate(decisions, {"E_NO_CONCENTRATION"})
        duplicate_rate = context.duplicate_count / max(1, context.raw_result_count)
        query_complexity = clamp(
            (
                len(query.emerging_contaminant_terms)
                + len(query.surface_water_terms)
                + len(query.monitoring_and_concentration_terms)
            )
            / 100
        )
        positive_score = (
            float(self.positive_weights["novel_precision_at_20"]) * novel_precision_at_20
            + float(self.positive_weights["normalized_novel_eligible_yield"]) * normalized_yield
            + float(self.positive_weights["novelty_rate"]) * novelty_rate
            + float(self.positive_weights["cross_source_breadth"]) * cross_source_breadth
            + float(self.positive_weights["scope_diversity"]) * scope_diversity
            + float(self.positive_weights["metadata_completeness"]) * metadata_completeness
        )
        penalty_score = (
            float(self.penalty_weights["defer_rate"]) * (defer_count / denominator)
            + float(self.penalty_weights["excluded_matrix_rate"]) * excluded_matrix_rate
            + float(self.penalty_weights["laboratory_study_rate"]) * laboratory_rate
            + float(self.penalty_weights["no_concentration_rate"]) * no_concentration_rate
            + float(self.penalty_weights["known_ineligible_overlap_rate"]) * 0.0
            + float(self.penalty_weights["query_complexity"]) * query_complexity
        )
        total_score = clamp(positive_score - penalty_score)
        score_delta = total_score - parent_score
        return QueryMetrics(
            run_id=context.run_id,
            iteration=query.iteration,
            query_id=query.query_id,
            parent_query_id=query.parent_query_id,
            raw_result_count=context.raw_result_count,
            scanned_result_count=context.scanned_result_count,
            known_record_count=0,
            novel_record_count=novel_record_count,
            target_novel_n=context.target_novel_n,
            actual_novel_n=novel_record_count,
            target_reached=novel_record_count >= context.target_novel_n,
            include_count=include_count,
            exclude_count=exclude_count,
            defer_count=defer_count,
            eligible_precision=eligible_precision,
            novel_precision_at_20=novel_precision_at_20,
            novel_eligible_yield=include_count,
            normalized_novel_eligible_yield=normalized_yield,
            marginal_relevant_yield=include_count / max(1, novel_record_count),
            novelty_rate=novelty_rate,
            cumulative_eligible_count=include_count,
            retrospective_query_coverage=0.0,
            cross_source_breadth=cross_source_breadth,
            metadata_completeness=metadata_completeness,
            scope_diversity=scope_diversity,
            excluded_matrix_rate=excluded_matrix_rate,
            laboratory_study_rate=laboratory_rate,
            no_concentration_rate=no_concentration_rate,
            duplicate_rate=duplicate_rate,
            known_eligible_overlap_rate=0.0,
            known_ineligible_overlap_rate=0.0,
            query_complexity=query_complexity,
            positive_score=positive_score,
            penalty_score=penalty_score,
            total_score=total_score,
            score_delta=score_delta,
            decision="accept" if query.parent_query_id is None else "reject",
            decision_reason=(
                "Initial query accepted as baseline."
                if query.parent_query_id is None
                else "Phase 1 does not test query variants."
            ),
            saturation_status="not_saturated",
            source_completeness=context.source_completeness,
            timestamp=utc_now_iso(),
            code_commit_sha=context.code_commit_sha,
            config_hash=context.config_hash,
            prompt_hash=context.prompt_hash,
            model_name="none",
            model_parameters={"llm_enabled": False},
            evaluated_record_count=evaluated_count,
            deferred_record_count=defer_count,
            defer_rate=defer_rate,
            marginal_eligible_count=include_count,
        )

    def _metadata_completeness(self, records: Sequence[NormalizedRecord]) -> float:
        if not records:
            return 0.0
        complete = sum(
            1 for record in records if record.title_original and record.abstract_original
        )
        return complete / len(records)

    def _scope_diversity(self, records: Sequence[NormalizedRecord]) -> float:
        matrices = {matrix for record in records for matrix in record.sampled_matrices}
        return clamp(len(matrices) / 6)

    def _reason_rate(self, decisions: Sequence[ScreeningDecision], reason_codes: set[str]) -> float:
        if not decisions:
            return 0.0
        count = sum(1 for decision in decisions if set(decision.reason_codes) & reason_codes)
        return count / len(decisions)
