"""Saturation detection for query iteration stopping."""

from __future__ import annotations

from ecmonitor.retrieval_specialist.models import QueryMetrics


class SaturationDetector:
    """Phase 1 saturation detector."""

    def detect(self, metrics: QueryMetrics, max_iterations_reached: bool = False) -> str:
        if max_iterations_reached:
            return "saturated_success"
        if metrics.novelty_rate < 0.05:
            return "saturated_noise"
        if metrics.novel_eligible_yield <= 2 and not metrics.target_reached:
            return "saturated_narrow"
        return "not_saturated"
