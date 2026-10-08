"""Paper-ready export writers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ecmonitor.retrieval_specialist.models import CanonicalQuery, QueryMetrics, ScreeningDecision
from ecmonitor.retrieval_specialist.storage.atomic_io import (
    ensure_dir,
    write_csv_atomic,
    write_text_atomic,
)


class Exporter:
    """Write manuscript-friendly CSV and Markdown artifacts."""

    def __init__(self, paper_exports_dir: Path) -> None:
        self.paper_exports_dir = ensure_dir(paper_exports_dir)

    def export_iteration(
        self,
        query: CanonicalQuery,
        metrics: QueryMetrics,
        decisions: list[ScreeningDecision],
        source_summaries: list[dict[str, Any]],
    ) -> None:
        self._write_query_metrics_wide(metrics)
        self._write_query_metrics_long(metrics)
        self._write_query_evolution(query, metrics)
        self._write_term_evolution(query)
        self._write_source_contribution(source_summaries)
        self._write_exclusion_reason_evolution(metrics, decisions)
        self._write_saturation_trajectory(metrics)
        self._write_screening_decision_evolution(metrics, decisions)
        self._write_run_index(metrics)
        self._write_readmes()

    def _write_query_metrics_wide(self, metrics: QueryMetrics) -> None:
        path = self.paper_exports_dir / "query_metrics_wide.csv"
        rows = [metrics.to_dict()]
        self._write_csv(path, rows, list(rows[0].keys()))

    def _write_query_metrics_long(self, metrics: QueryMetrics) -> None:
        numeric_items = {
            key: value
            for key, value in metrics.to_dict().items()
            if isinstance(value, int | float | bool)
        }
        rows = [
            {
                "run_id": metrics.run_id,
                "iteration": metrics.iteration,
                "query_id": metrics.query_id,
                "metric_name": key,
                "metric_value": value,
                "metric_unit": "unitless",
                "metric_version": "0.1.0",
                "timestamp": metrics.timestamp,
            }
            for key, value in numeric_items.items()
        ]
        self._write_csv(
            self.paper_exports_dir / "query_metrics_long.csv",
            rows,
            [
                "run_id",
                "iteration",
                "query_id",
                "metric_name",
                "metric_value",
                "metric_unit",
                "metric_version",
                "timestamp",
            ],
        )

    def _write_query_evolution(self, query: CanonicalQuery, metrics: QueryMetrics) -> None:
        self._write_csv(
            self.paper_exports_dir / "query_evolution.csv",
            [
                {
                    "run_id": metrics.run_id,
                    "iteration": query.iteration,
                    "query_id": query.query_id,
                    "parent_query_id": query.parent_query_id or "",
                    "added_terms": ";".join(query.added_terms),
                    "removed_terms": ";".join(query.removed_terms),
                    "modified_blocks": ";".join(query.modified_concept_blocks),
                    "change_rationale": query.change_rationale,
                    "decision": metrics.decision,
                    "score": metrics.total_score,
                    "timestamp": metrics.timestamp,
                }
            ],
            [
                "run_id",
                "iteration",
                "query_id",
                "parent_query_id",
                "added_terms",
                "removed_terms",
                "modified_blocks",
                "change_rationale",
                "decision",
                "score",
                "timestamp",
            ],
        )

    def _write_term_evolution(self, query: CanonicalQuery) -> None:
        rows: list[dict[str, Any]] = []
        for block_name, terms in [
            ("emerging_contaminant_terms", query.emerging_contaminant_terms),
            ("surface_water_terms", query.surface_water_terms),
            ("monitoring_and_concentration_terms", query.monitoring_and_concentration_terms),
        ]:
            rows.extend(
                {
                    "query_id": query.query_id,
                    "term": term,
                    "concept_block": block_name,
                    "action": "initial",
                    "previous_status": "",
                    "new_status": "active",
                    "reason": "Protocol seed term.",
                    "supporting_positive_documents": "",
                    "supporting_negative_documents": "",
                    "discriminative_score": "",
                }
                for term in terms
            )
        self._write_csv(
            self.paper_exports_dir / "term_evolution.csv",
            rows,
            [
                "query_id",
                "term",
                "concept_block",
                "action",
                "previous_status",
                "new_status",
                "reason",
                "supporting_positive_documents",
                "supporting_negative_documents",
                "discriminative_score",
            ],
        )

    def _write_source_contribution(self, source_summaries: list[dict[str, Any]]) -> None:
        rows = [
            {
                "source": summary["source_name"],
                "returned_records": summary["raw_result_count"],
                "scanned_records": summary["scanned_result_count"],
                "novel_records": summary["scanned_result_count"],
                "novel_eligible_records": summary.get("novel_eligible_records", 0),
                "duplicate_records": summary.get("duplicate_records", 0),
                "missing_abstracts": summary.get("missing_abstracts", 0),
                "deferred_records": summary.get("deferred_records", 0),
                "API_failures": 0 if summary["status"] == "source_success" else 1,
                "unique_contributions": summary["scanned_result_count"],
                "overlap_counts_with_other_sources": 0,
            }
            for summary in source_summaries
        ]
        self._write_csv(
            self.paper_exports_dir / "source_contribution.csv",
            rows,
            [
                "source",
                "returned_records",
                "scanned_records",
                "novel_records",
                "novel_eligible_records",
                "duplicate_records",
                "missing_abstracts",
                "deferred_records",
                "API_failures",
                "unique_contributions",
                "overlap_counts_with_other_sources",
            ],
        )

    def _write_exclusion_reason_evolution(
        self, metrics: QueryMetrics, decisions: list[ScreeningDecision]
    ) -> None:
        counts: dict[str, int] = {}
        for decision in decisions:
            for reason in decision.reason_codes:
                counts[reason] = counts.get(reason, 0) + 1
        rows = [
            {
                "run_id": metrics.run_id,
                "iteration": metrics.iteration,
                "query_id": metrics.query_id,
                "reason_code": reason,
                "count": count,
                "timestamp": metrics.timestamp,
            }
            for reason, count in sorted(counts.items())
        ]
        self._write_csv(
            self.paper_exports_dir / "exclusion_reason_evolution.csv",
            rows,
            ["run_id", "iteration", "query_id", "reason_code", "count", "timestamp"],
        )

    def _write_saturation_trajectory(self, metrics: QueryMetrics) -> None:
        self._write_csv(
            self.paper_exports_dir / "saturation_trajectory.csv",
            [
                {
                    "run_id": metrics.run_id,
                    "iteration": metrics.iteration,
                    "query_id": metrics.query_id,
                    "saturation_status": metrics.saturation_status,
                    "novelty_rate": metrics.novelty_rate,
                    "score_delta": metrics.score_delta,
                    "timestamp": metrics.timestamp,
                }
            ],
            [
                "run_id",
                "iteration",
                "query_id",
                "saturation_status",
                "novelty_rate",
                "score_delta",
                "timestamp",
            ],
        )

    def _write_screening_decision_evolution(
        self, metrics: QueryMetrics, decisions: list[ScreeningDecision]
    ) -> None:
        rows = [
            {
                "run_id": metrics.run_id,
                "iteration": metrics.iteration,
                "query_id": metrics.query_id,
                "global_record_id": decision.global_record_id,
                "decision": decision.decision,
                "reason_codes": ";".join(decision.reason_codes),
                "timestamp": metrics.timestamp,
            }
            for decision in decisions
        ]
        self._write_csv(
            self.paper_exports_dir / "screening_decision_evolution.csv",
            rows,
            [
                "run_id",
                "iteration",
                "query_id",
                "global_record_id",
                "decision",
                "reason_codes",
                "timestamp",
            ],
        )

    def _write_run_index(self, metrics: QueryMetrics) -> None:
        self._write_csv(
            self.paper_exports_dir / "run_index.csv",
            [
                {
                    "run_id": metrics.run_id,
                    "latest_iteration": metrics.iteration,
                    "latest_query_id": metrics.query_id,
                    "run_status": "completed",
                    "source_completeness": metrics.source_completeness,
                    "timestamp": metrics.timestamp,
                }
            ],
            [
                "run_id",
                "latest_iteration",
                "latest_query_id",
                "run_status",
                "source_completeness",
                "timestamp",
            ],
        )

    def _write_readmes(self) -> None:
        write_text_atomic(
            self.paper_exports_dir / "FIGURE_DATA_README.md",
            "# Figure Data\n\n"
            "Phase 1 mock-run exports for query evolution and screening metrics.\n",
        )
        write_text_atomic(
            self.paper_exports_dir / "METRIC_DEFINITIONS.md",
            "# Metric Definitions\n\n"
            "Metrics are computed from the stratified cross-source novelty sample.\n",
        )
        write_text_atomic(
            self.paper_exports_dir / "QUERY_EVOLUTION_SUMMARY.md",
            "# Query Evolution Summary\n\n"
            "Phase 1 records the initial protocol-derived query only.\n",
        )

    def _write_csv(self, path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
        write_csv_atomic(path, rows, fieldnames)
