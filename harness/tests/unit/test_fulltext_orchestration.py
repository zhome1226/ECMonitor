from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ecmonitor.fulltext_extraction.harness import FulltextExtractionHarness
from ecmonitor.fulltext_extraction.models import (
    ChemicalMatch,
    ChemicalResolution,
    EvidenceChunk,
    ParsedDocument,
    ParsedPage,
    TextBlock,
    ValidationDecision,
)
from ecmonitor.fulltext_extraction.registry import ChemicalRegistry
from ecmonitor.fulltext_extraction.signoff import SignedExampleStore
from ecmonitor.fulltext_extraction.storage import FulltextControlPlane


class OnePageParser:
    parser_name = "one-page"

    def parse(self, pdf_path: Path, *, document_id: str, source_sha256: str) -> ParsedDocument:
        return ParsedDocument(
            document_id=document_id,
            source_path=pdf_path,
            source_sha256=source_sha256,
            parser_name=self.parser_name,
            parser_version="1",
            pages=(
                ParsedPage(
                    page_number=1,
                    width=100,
                    height=100,
                    blocks=(TextBlock("p1-b1", 1, "PFOA 12 ng/L in river water"),),
                ),
            ),
        )


class FixedResolver:
    resolver_name = "fixed"

    def resolve(self, raw_name: str) -> ChemicalResolution:
        return ChemicalResolution(
            raw_name=raw_name,
            normalized_query=raw_name.casefold(),
            status="resolved",
            resolver_name=self.resolver_name,
            matches=(
                ChemicalMatch(
                    source="test",
                    source_record_id="9554",
                    canonical_name="Perfluorooctanoic acid",
                    matched_alias=raw_name,
                    pubchem_cid="9554",
                    cas_candidates=("335-67-1",),
                    inchikey="SNGREZUHAYWORS-UHFFFAOYSA-N",
                ),
            ),
        )


class RetryThenExtract:
    extractor_name = "retry-then-extract"

    def extract(self, chunk: EvidenceChunk) -> list[dict[str, Any]]:
        return self.extract_with_feedback(
            chunk,
            attempt=1,
            reason_codes=(),
            failed_json_pointers=(),
            requested_context=(),
        )

    def extract_with_feedback(
        self,
        chunk: EvidenceChunk,
        *,
        attempt: int,
        reason_codes: tuple[str, ...],
        failed_json_pointers: tuple[str, ...],
        requested_context: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        del chunk, reason_codes, failed_json_pointers, requested_context
        if attempt == 1:
            return [{"analyte": {}, "evidence": {}}]
        return [{"analyte": {"raw_name": "PFOA"}, "evidence": {}}]


class AcceptResolvedValidator:
    validator_name = "accept-resolved"

    def validate(
        self,
        candidate: dict[str, Any],
        *,
        chunk: EvidenceChunk,
        resolutions: tuple[ChemicalResolution, ...],
    ) -> ValidationDecision:
        del chunk
        analyte = candidate.get("analyte")
        if not isinstance(analyte, dict) or not analyte.get("raw_name"):
            return ValidationDecision(
                action="retry",
                reason_codes=("missing_name",),
                failed_json_pointers=("/analyte/raw_name",),
            )
        assert resolutions
        return ValidationDecision(action="accept", reason_codes=("evidence_verified",))


class AggregateExtractor:
    extractor_name = "aggregate"

    def extract(self, chunk: EvidenceChunk) -> list[dict[str, Any]]:
        del chunk
        return [{"analyte": {"raw_name": "PFAS"}, "evidence": {}}]


def _harness(
    tmp_path: Path,
    *,
    extractor: Any,
    validator: Any | None = None,
    maximum_extraction_attempts: int = 1,
    resolver: Any | None = None,
) -> tuple[FulltextExtractionHarness, FulltextControlPlane]:
    plane = FulltextControlPlane(tmp_path / "state.sqlite3")
    harness = FulltextExtractionHarness(
        parser=OnePageParser(),
        control_plane=plane,
        chemical_registry=ChemicalRegistry(tmp_path / "registry.sqlite3"),
        output_dir=tmp_path / "runs",
        chemical_resolver=resolver,
        extractor=extractor,
        validator=validator,
        maximum_extraction_attempts=maximum_extraction_attempts,
    )
    return harness, plane


def test_retry_is_bounded_and_final_record_keeps_canonical_and_reported_names(
    tmp_path: Path,
) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-test")
    harness, plane = _harness(
        tmp_path,
        extractor=RetryThenExtract(),
        validator=AcceptResolvedValidator(),
        maximum_extraction_attempts=2,
        resolver=FixedResolver(),
    )
    report = harness.run_document(pdf)
    assert report.retry_count == 1
    assert report.candidate_count == 2
    assert report.review_count == 2
    assert report.accepted_count == 1
    assert report.pending_human_review_count == 0
    with sqlite3.connect(plane.database_path) as connection:
        row = connection.execute(
            """SELECT canonical_name, reported_name, replacement_name, disposition
            FROM observation_records"""
        ).fetchone()
    assert row == ("Perfluorooctanoic acid", "PFOA", "PFOA", "accepted")


def test_explicit_chemical_class_is_rejected_as_non_individual(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-test")
    harness, plane = _harness(tmp_path, extractor=AggregateExtractor())
    report = harness.run_document(pdf)
    assert report.rejected_count == 1
    assert report.pending_human_review_count == 0
    status = plane.status()
    assert status["table_counts"]["observation_records"] == 1
    assert status["table_counts"]["human_review_tasks"] == 0


class CountingExtractor:
    extractor_name = "counting"

    def __init__(self) -> None:
        self.calls = 0

    def __deepcopy__(self, memo: dict[int, Any]) -> CountingExtractor:
        # The harness deep-copies template components; keep the same instance so the test
        # can observe call counts directly.
        del memo
        return self

    def extract(self, chunk: EvidenceChunk) -> list[dict[str, Any]]:
        del chunk
        self.calls += 1
        return [{"analyte": {"raw_name": "Novel-X"}, "evidence": {"quote": "x"}}]


class NonRepairableRetryValidator:
    validator_name = "non-repairable"

    def validate(
        self,
        candidate: dict[str, Any],
        *,
        chunk: EvidenceChunk,
        resolutions: tuple[ChemicalResolution, ...],
    ) -> ValidationDecision:
        del candidate, chunk, resolutions
        return ValidationDecision(
            action="retry",
            reason_codes=("chemical_resolution_missing",),
            failed_json_pointers=("/analyte",),
        )


class FeedbackCapturingExtractor:
    extractor_name = "feedback-capturing"

    def __init__(self) -> None:
        self.calls = 0
        self.captured: list[dict[str, Any]] = []

    def __deepcopy__(self, memo: dict[int, Any]) -> FeedbackCapturingExtractor:
        # Keep the same instance (see CountingExtractor).
        del memo
        return self

    def extract(self, chunk: EvidenceChunk) -> list[dict[str, Any]]:
        return self.extract_with_feedback(
            chunk,
            attempt=1,
            reason_codes=(),
            failed_json_pointers=(),
            requested_context=(),
        )

    def extract_with_feedback(
        self,
        chunk: EvidenceChunk,
        *,
        attempt: int,
        reason_codes: tuple[str, ...],
        failed_json_pointers: tuple[str, ...],
        requested_context: tuple[str, ...],
        failed_candidates: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
        prior_candidates: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        del chunk
        self.calls += 1
        self.captured.append(
            {
                "attempt": attempt,
                "reason_codes": reason_codes,
                "failed_json_pointers": failed_json_pointers,
                "requested_context": requested_context,
                "failed_candidates": list(failed_candidates or []),
                "prior_candidates": list(prior_candidates or []),
            }
        )
        if attempt == 1:
            return [{"analyte": {}, "evidence": {}}]
        return [{"analyte": {"raw_name": "PFOA"}, "evidence": {}}]


def test_non_repairable_resolution_retry_defers_without_reextraction_or_human_task(
    tmp_path: Path,
) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-test")
    extractor = CountingExtractor()
    harness, plane = _harness(
        tmp_path,
        extractor=extractor,
        validator=NonRepairableRetryValidator(),
        maximum_extraction_attempts=3,
    )
    report = harness.run_document(pdf)
    # Missing identity/SI evidence cannot be repaired by rereading the same text. It is routed
    # to the non-blocking deferred-evidence stream rather than burning another model call or
    # creating an immediate human-review task.
    assert extractor.calls == 1
    assert report.retry_count == 0
    assert report.pending_human_review_count == 1
    assert report.rejected_count == 0
    assert report.terminal_status_counts == {"deferred_identity_evidence": 1}
    with sqlite3.connect(plane.database_path) as connection:
        row = connection.execute(
            "SELECT disposition, terminal_status, output_stream, policy_rule_id "
            "FROM observation_records"
        ).fetchone()
        task_count = connection.execute(
            "SELECT COUNT(*) FROM human_review_tasks"
        ).fetchone()[0]
    assert row == (
        "pending_human_review",
        "deferred_identity_evidence",
        "deferred_evidence",
        "CHEM-DEFER-01",
    )
    assert task_count == 0


def test_repairable_retry_forwards_failed_and_prior_candidates(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-test")
    extractor = FeedbackCapturingExtractor()
    harness, _ = _harness(
        tmp_path,
        extractor=extractor,
        validator=AcceptResolvedValidator(),
        maximum_extraction_attempts=2,
        resolver=FixedResolver(),
    )
    report = harness.run_document(pdf)
    assert report.retry_count == 1
    assert report.accepted_count == 1
    assert extractor.calls == 2
    second = extractor.captured[1]
    assert second["attempt"] == 2
    assert second["reason_codes"] == ("missing_name",)
    assert second["failed_json_pointers"] == ("/analyte/raw_name",)
    assert len(second["failed_candidates"]) == 1
    assert len(second["prior_candidates"]) == 1
    # The failed candidate is the one the validator asked to repair.
    assert second["failed_candidates"][0]["analyte"].get("raw_name") is None



class FocusedPassExtractor:
    extractor_name = "focused-pass"

    def __init__(self) -> None:
        self.calls = 0
        self.saw_focus = False

    def __deepcopy__(self, memo: dict[int, Any]) -> FocusedPassExtractor:
        del memo
        return self

    def extract(self, chunk: EvidenceChunk) -> list[dict[str, Any]]:
        self.calls += 1
        if chunk.chunk_type == "focused_concentration_second_pass":
            self.saw_focus = True
            return [
                {
                    "analyte": {"raw_name": "PFOA"},
                    "result": {"raw_value": "12 ng/L"},
                    "evidence": {"quote": "12 ng/L"},
                }
            ]
        return []


def test_focused_second_pass_rescues_empty_extraction(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-test")
    extractor = FocusedPassExtractor()
    harness, plane = _harness(
        tmp_path,
        extractor=extractor,
        validator=AcceptResolvedValidator(),
        maximum_extraction_attempts=2,
        resolver=FixedResolver(),
    )
    report = harness.run_document(pdf)
    # Attempt 1 on the full chunk returned nothing; the harness spent one small focused call
    # on the concentration-bearing region and recovered the observation.
    assert extractor.calls == 2
    assert extractor.saw_focus is True
    assert report.candidate_count == 1
    assert report.accepted_count == 1
    assert report.retry_count == 1  # the focused pass is recorded as a retry event
    with sqlite3.connect(plane.database_path) as connection:
        row = connection.execute(
            "SELECT disposition FROM observation_records"
        ).fetchone()
    assert row == ("accepted",)



class EscalateValidator:
    validator_name = "escalate"

    def validate(
        self,
        candidate: dict[str, Any],
        *,
        chunk: EvidenceChunk,
        resolutions: tuple[ChemicalResolution, ...],
    ) -> ValidationDecision:
        del candidate, chunk, resolutions
        return ValidationDecision(
            action="escalate",
            reason_codes=("needs_human",),
            human_review_required=True,
        )


class DocumentContextExtractor:
    extractor_name = "doc-ctx"

    def __init__(self) -> None:
        self.document_context: dict[str, Any] | None = None

    def __deepcopy__(self, memo: dict[int, Any]) -> DocumentContextExtractor:
        del memo
        return self

    def extract(self, chunk: EvidenceChunk) -> list[dict[str, Any]]:
        del chunk
        return []


def test_human_review_task_resolves_and_records_signed_example(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-test")
    harness, plane = _harness(
        tmp_path,
        extractor=CountingExtractor(),
        validator=EscalateValidator(),
        resolver=FixedResolver(),
    )
    report = harness.run_document(pdf)
    assert report.pending_human_review_count == 1
    with sqlite3.connect(plane.database_path) as connection:
        task_id = connection.execute("SELECT task_id FROM human_review_tasks").fetchone()[0]
    task = plane.get_human_review_task(task_id)
    assert task is not None
    assert task["status"] == "pending"

    resolution = plane.resolve_human_review_task(
        task_id, disposition="rejected", note="not a real measurement"
    )
    assert resolution["disposition"] == "rejected"
    with sqlite3.connect(plane.database_path) as connection:
        status = connection.execute(
            "SELECT status FROM human_review_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
    assert status == "resolved"

    store = SignedExampleStore(tmp_path / "signed.jsonl")
    store.record(
        document_id="doc-1",
        document_session_id=task["document_session_id"],
        candidate_id=task["candidate_id"],
        disposition="rejected",
        reason_codes=task["reason_codes"],
        payload={"candidate": task["payload"].get("candidate", task["payload"])},
    )
    examples = store.fewshot_examples()
    assert len(examples) == 1
    assert examples[0].disposition == "rejected"


def test_pilot_acceptance_materializes_only_eligible_records(tmp_path: Path) -> None:
    class PilotEligibleValidator(EscalateValidator):
        def validate(self, candidate: dict[str, Any], *, chunk: EvidenceChunk,
                     resolutions: tuple[ChemicalResolution, ...]) -> ValidationDecision:
            return ValidationDecision(action="escalate", reason_codes=("pilot_human_signoff_required",),
                                      human_review_required=True, pilot_accept_eligible=True)

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-test")
    harness, plane = _harness(tmp_path, extractor=CountingExtractor(),
                              validator=PilotEligibleValidator(), resolver=FixedResolver())
    harness.run_document(pdf)
    with sqlite3.connect(plane.database_path) as connection:
        task_id = connection.execute("SELECT task_id FROM human_review_tasks").fetchone()[0]
    resolution = plane.resolve_human_review_task(task_id, disposition="accepted")
    with sqlite3.connect(plane.database_path) as connection:
        row = connection.execute("SELECT disposition, payload_json FROM observation_records").fetchone()
    assert row[0] == "accepted"
    assert json.loads(row[1])["human_resolution_id"] == resolution["resolution_id"]


def test_acceptance_cannot_override_unresolved_evidence(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-test")
    harness, plane = _harness(tmp_path, extractor=CountingExtractor(),
                              validator=EscalateValidator(), resolver=FixedResolver())
    harness.run_document(pdf)
    with sqlite3.connect(plane.database_path) as connection:
        task_id = connection.execute("SELECT task_id FROM human_review_tasks").fetchone()[0]
    plane.resolve_human_review_task(task_id, disposition="accepted")
    with sqlite3.connect(plane.database_path) as connection:
        assert connection.execute("SELECT disposition FROM observation_records").fetchone()[0] == "pending_human_review"


def test_signed_examples_are_injected_into_extractor_context(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-test")
    store = SignedExampleStore(tmp_path / "signed.jsonl")
    store.record(
        document_id="doc-1",
        document_session_id="session-1",
        candidate_id="candidate-1",
        disposition="accepted",
        reason_codes=("evidence_verified",),
        payload={"candidate": {"analyte": {"raw_name": "PFOA"}, "evidence": {"quote": "x"}}},
    )
    extractor = DocumentContextExtractor()
    harness = FulltextExtractionHarness(
        parser=OnePageParser(),
        control_plane=FulltextControlPlane(tmp_path / "state.sqlite3"),
        chemical_registry=ChemicalRegistry(tmp_path / "registry.sqlite3"),
        output_dir=tmp_path / "runs",
        extractor=extractor,
        signed_example_store=store,
    )
    harness.run_document(pdf)
    assert extractor.document_context is not None
    examples = extractor.document_context.get("signed_examples")
    assert isinstance(examples, list) and len(examples) == 1
    assert examples[0]["disposition"] == "accepted"
    assert examples[0]["candidate"]["analyte"]["raw_name"] == "PFOA"
