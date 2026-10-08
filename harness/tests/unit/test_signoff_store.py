"""Tests for the bounded human-review signoff store (P2c)."""
from __future__ import annotations

from pathlib import Path

from ecmonitor.fulltext_extraction.signoff import SignedDecision, SignedExampleStore


def _decision(
    disposition: str,
    *,
    document_id: str = "doc-1",
    record_id: str,
    reason_codes: tuple[str, ...] = (),
) -> SignedDecision:
    return SignedDecision(
        record_id=record_id,
        document_id=document_id,
        document_session_id="session-1",
        candidate_id=f"candidate-{record_id}",
        disposition=disposition,  # type: ignore[arg-type]
        reason_codes=reason_codes,
        payload={"candidate": {"analyte": {"raw_name": "PFOA"}, "evidence": {"quote": "x"}}},
        recorded_at_utc=f"2026-08-14T10:00:{record_id}",
    )


def test_store_records_and_loads_roundtrip(tmp_path: Path) -> None:
    store = SignedExampleStore(tmp_path / "signed.jsonl", max_examples=5)
    store.record(
        document_id="doc-1",
        document_session_id="session-1",
        candidate_id="candidate-1",
        disposition="accepted",
        reason_codes=("evidence_verified",),
        payload={"candidate": {"analyte": {"raw_name": "PFOA"}}},
    )
    loaded = store.load()
    assert len(loaded) == 1
    assert loaded[0].disposition == "accepted"
    assert loaded[0].reason_codes == ("evidence_verified",)
    example = loaded[0].to_example_dict()
    assert example["disposition"] == "accepted"
    assert example["candidate"]["analyte"]["raw_name"] == "PFOA"


def test_store_bounds_the_log_to_newest(tmp_path: Path) -> None:
    store = SignedExampleStore(tmp_path / "signed.jsonl", max_examples=3)
    for index in range(1, 6):
        store.record(
            document_id="doc-1",
            document_session_id="session-1",
            candidate_id=f"candidate-{index}",
            disposition="accepted",
            payload={},
        )
    loaded = store.load()
    assert len(loaded) == 3  # bounded to the newest max_examples
    assert len({item.candidate_id for item in loaded}) == 3


def test_fewshot_prefers_mixed_bundle_bounded(tmp_path: Path) -> None:
    store = SignedExampleStore(tmp_path / "signed.jsonl", max_examples=100)
    for index in range(1, 8):
        store.record(
            document_id="doc-1",
            document_session_id="session-1",
            candidate_id=f"candidate-{index}",
            disposition="accepted" if index % 2 else "rejected",
            reason_codes=("ok",) if index % 2 else ("not_an_individual_chemical",),
            payload={},
        )
    examples = store.fewshot_examples(limit=4)
    assert 1 <= len(examples) <= 4
    dispositions = {item.disposition for item in examples}
    assert "accepted" in dispositions
    assert "rejected" in dispositions


def test_store_rejects_bad_disposition(tmp_path: Path) -> None:
    store = SignedExampleStore(tmp_path / "signed.jsonl")
    try:
        store.record(
            document_id="doc-1",
            document_session_id="session-1",
            candidate_id="candidate-1",
            disposition="pending",
            payload={},
        )
    except ValueError:
        return
    raise AssertionError("expected ValueError for unsupported disposition")
