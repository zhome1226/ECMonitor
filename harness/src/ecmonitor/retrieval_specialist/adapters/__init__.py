"""Source adapters for Retrieval Specialist."""

from ecmonitor.retrieval_specialist.adapters.base import SourceAdapter, SourcePage, SourceResult
from ecmonitor.retrieval_specialist.adapters.mock import MockSourceAdapter

__all__ = ["MockSourceAdapter", "SourceAdapter", "SourcePage", "SourceResult"]
