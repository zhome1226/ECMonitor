"""Human-review signoff store and bounded few-shot examples for the extractor.

The harness routes everything it cannot decide deterministically to a human review task. Once
a human signs a decision (``accepted`` or ``rejected``) those decisions are the strongest
quality signal available: they show the extractor what a good observation looks like and what
mistake to avoid next time. This module persists signed decisions as a bounded JSONL log and
selects a compact, capped few-shot bundle that the harness injects into the next document's
extractor context.

Safety rules:
- The pool is bounded (``max_examples``) so the log cannot grow without bound.
- The few-shot bundle is capped (``limit``) and strips non-essential fields so it stays small.
- Signed examples are treated as teaching exemplars only; the extractor prompt forbids copying
  their values/chemicals/sites/dates into a new document.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

_ACCEPTED = "accepted"
_REJECTED = "rejected"

_SIGNED_FIELDS = (
    "candidate",
    "disposition",
    "review_note",
    "reason_codes",
)

_DISPOSITION: set[str] = {_ACCEPTED, _REJECTED}


@dataclass(frozen=True, slots=True)
class SignedDecision:
    record_id: str
    document_id: str
    document_session_id: str
    candidate_id: str
    disposition: Literal["accepted", "rejected"]
    reason_codes: tuple[str, ...]
    payload: dict[str, Any]
    recorded_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["reason_codes"] = list(self.reason_codes)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SignedDecision:
        return cls(
            record_id=str(data["record_id"]),
            document_id=str(data["document_id"]),
            document_session_id=str(data["document_session_id"]),
            candidate_id=str(data["candidate_id"]),
            disposition=str(data["disposition"]),  # type: ignore[arg-type]
            reason_codes=tuple(str(item) for item in data.get("reason_codes") or []),
            payload=dict(data.get("payload") or {}),
            recorded_at_utc=str(data["recorded_at_utc"]),
        )

    def to_example_dict(self, *, max_evidence_quote: int = 300) -> dict[str, Any]:
        """Compact teaching example for the extractor prompt."""
        payload = dict(self.payload)
        candidate = dict(payload.get("candidate") or payload)
        for key in ("quality_flags", "extraction_attempt", "candidate_id"):
            candidate.pop(key, None)
        evidence = candidate.get("evidence")
        if isinstance(evidence, dict):
            evidence = dict(evidence)
            quote = evidence.get("quote")
            if isinstance(quote, str) and len(quote) > max_evidence_quote:
                evidence["quote"] = quote[:max_evidence_quote] + "..."
            candidate["evidence"] = evidence
        note = "human-accepted example" if self.disposition == _ACCEPTED else (
            "human-rejected example: " + ", ".join(self.reason_codes[:4]) if self.reason_codes
            else "human-rejected example"
        )
        return {
            "disposition": self.disposition,
            "candidate": candidate,
            "review_note": note,
        }


class SignedExampleStore:
    """Bounded JSONL-backed log of human-signed review decisions."""

    def __init__(self, path: Path, *, max_examples: int = 200) -> None:
        if max_examples < 1:
            raise ValueError("max_examples must be at least one")
        self.path = path
        self.max_examples = max_examples
        self._lock = threading.Lock()

    def record(
        self,
        *,
        document_id: str,
        document_session_id: str,
        candidate_id: str,
        disposition: str,
        reason_codes: tuple[str, ...] | list[str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> SignedDecision:
        if disposition not in _DISPOSITION:
            raise ValueError(f"disposition must be one of {sorted(_DISPOSITION)}")
        decision = SignedDecision(
            record_id=f"signoff-{uuid.uuid4().hex[:12]}",
            document_id=document_id,
            document_session_id=document_session_id,
            candidate_id=candidate_id,
            disposition=disposition,  # type: ignore[arg-type]
            reason_codes=tuple(reason_codes or ()),
            payload=dict(payload or {}),
            recorded_at_utc=datetime.now(UTC).isoformat(),
        )
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(decision.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
            # Trim the log to the newest ``max_examples`` decisions.
            self._trim()
        return decision

    def _trim(self) -> None:
        """Rewrite the log keeping only the newest ``max_examples`` decisions."""
        decisions = self.load()
        if len(decisions) <= self.max_examples:
            return
        kept = decisions[-self.max_examples :]
        with self.path.open("w", encoding="utf-8") as handle:
            for decision in kept:
                handle.write(
                    json.dumps(decision.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
                )
                handle.flush()

    def load(self) -> list[SignedDecision]:
        if not self.path.is_file():
            return []
        decisions: list[SignedDecision] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    decisions.append(SignedDecision.from_dict(json.loads(line)))
                except (ValueError, KeyError, TypeError):
                    continue
        return decisions

    def fewshot_examples(
        self, *, limit: int = 5, accepted_budget: int | None = None
    ) -> list[SignedDecision]:
        """Return a bounded, mixed few-shot bundle (most recent first).

        Prefers a majority of ``accepted`` exemplars plus at least one recent ``rejected``
        counter-example when available, capped at ``limit`` total.
        """
        if limit < 1:
            return []
        decisions = self.load()
        if not decisions:
            return []
        accepted = [item for item in decisions if item.disposition == _ACCEPTED]
        rejected = [item for item in decisions if item.disposition == _REJECTED]
        if accepted_budget is None:
            accepted_budget = max(1, int(limit * 0.6))
        accepted_take = accepted[-accepted_budget:]
        rejected_take = rejected[-max(1, limit - len(accepted_take)) :]
        combined = [*accepted_take, *rejected_take]
        # Preserve most-recent-first ordering across the whole bundle.
        combined.sort(key=lambda item: item.recorded_at_utc, reverse=True)
        return combined[:limit]

    def reason_code_stats(self) -> dict[str, dict[str, int]]:
        """Aggregate signed accepted/rejected counts per review reason code.

        This is the quantitative input for rule adoption: a reason code that is consistently
        signed ``rejected`` (for example ``literature_summary_value``) can be promoted to a
        deterministic gate for the next run, and ``accepted`` codes can seed the auto-pass
        pattern pool. Returns ``{reason_code: {"accepted": n, "rejected": n}}``.
        """
        stats: dict[str, dict[str, int]] = {}
        for decision in self.load():
            for code in decision.reason_codes:
                bucket = stats.setdefault(str(code), {"accepted": 0, "rejected": 0})
                bucket[decision.disposition] = bucket.get(decision.disposition, 0) + 1
        return stats
