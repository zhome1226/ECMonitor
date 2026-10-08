from __future__ import annotations

import sys
from pathlib import Path

from ecmonitor.fulltext_extraction.adapters.json_command import (
    JsonCommandCandidateExtractor,
    JsonCommandError,
    JsonCommandEvidenceValidator,
)
from ecmonitor.fulltext_extraction.models import EvidenceChunk


def _write_stub(path: Path) -> None:
    path.write_text(
        """
import json
import os
import sys

PROCESS_CALL_COUNT = 0
payload = json.load(sys.stdin)
PROCESS_CALL_COUNT += 1

if payload["role"] == "occurrence_extractor":
    response = {
        "candidates": [
            {
                "process_id": os.getpid(),
                "process_call_count": PROCESS_CALL_COUNT,
                "attempt": payload["attempt"],
            }
        ]
    }
elif payload["role"] == "evidence_validator":
    response = {
        "action": "accept",
        "reason_codes": [f"pid:{os.getpid()}", f"calls:{PROCESS_CALL_COUNT}"],
    }
elif payload["role"] == "evidence_validator_batch":
    response = {
        "decisions": [
            {
                "candidate_id": item["candidate_id"],
                "action": "accept",
                "reason_codes": [f"pid:{os.getpid()}", f"calls:{PROCESS_CALL_COUNT}"],
                "failed_json_pointers": [],
                "requested_context": [],
                "human_review_required": False,
            }
            for item in payload["validation_items"]
        ]
    }
else:
    raise RuntimeError("unexpected role")

json.dump(response, sys.stdout)
""".strip(),
        encoding="utf-8",
    )


def _chunk() -> EvidenceChunk:
    return EvidenceChunk(
        chunk_id="chunk-1",
        document_id="doc-1",
        ordinal=0,
        chunk_type="text",
        text="PFOS was detected at 4.2 ng/L.",
        page_start=1,
        page_end=1,
        source_block_ids=("block-1",),
    )


def test_json_command_agents_start_a_fresh_process_for_every_request(tmp_path: Path) -> None:
    stub = tmp_path / "agent_stub.py"
    _write_stub(stub)
    command = (sys.executable, str(stub))

    extractor = JsonCommandCandidateExtractor(command)
    first = extractor.extract(_chunk())[0]
    second = extractor.extract(_chunk())[0]

    assert first["process_call_count"] == 1
    assert second["process_call_count"] == 1

    validator = JsonCommandEvidenceValidator(command)
    first_validation = validator.validate(first, chunk=_chunk(), resolutions=())
    second_validation = validator.validate(second, chunk=_chunk(), resolutions=())

    assert "calls:1" in first_validation.reason_codes
    assert "calls:1" in second_validation.reason_codes


def test_json_command_validator_batches_candidates_in_one_process(tmp_path: Path) -> None:
    stub = tmp_path / "agent_stub.py"
    _write_stub(stub)
    validator = JsonCommandEvidenceValidator((sys.executable, str(stub)))
    candidates = [
        {"candidate_id": "candidate-1", "analyte": {"raw_name": "PFOA"}},
        {"candidate_id": "candidate-2", "analyte": {"raw_name": "PFOS"}},
    ]

    decisions = validator.validate_batch(
        candidates, chunk=_chunk(), resolutions_by_candidate=[(), ()]
    )

    assert len(decisions) == 2
    first_pid = next(code for code in decisions[0].reason_codes if code.startswith("pid:"))
    second_pid = next(code for code in decisions[1].reason_codes if code.startswith("pid:"))
    assert first_pid == second_pid
    assert all("calls:1" in decision.reason_codes for decision in decisions)



def test_json_command_validator_splits_large_batches(tmp_path: Path) -> None:
    stub = tmp_path / "agent_stub.py"
    _write_stub(stub)
    validator = JsonCommandEvidenceValidator(
        (sys.executable, str(stub)), max_batch_size=2
    )
    candidates = [
        {"candidate_id": f"candidate-{index}", "analyte": {"raw_name": "PFOS"}}
        for index in range(5)
    ]

    decisions = validator.validate_batch(
        candidates, chunk=_chunk(), resolutions_by_candidate=[()] * 5
    )

    assert len(decisions) == 5
    pids = [
        next(code for code in decision.reason_codes if code.startswith("pid:"))
        for decision in decisions
    ]
    assert pids[0] == pids[1]
    assert pids[2] == pids[3]
    assert len({pids[0], pids[2], pids[4]}) == 3

def _write_transport_stub(path: Path) -> None:
    path.write_text(
        """
import json
import os
import sys

counter_path = os.environ["STUB_COUNTER"]
count = 0
try:
    with open(counter_path, encoding="utf-8") as handle:
        count = int(handle.read().strip())
except Exception:
    count = 0
count += 1
with open(counter_path, "w", encoding="utf-8") as handle:
    handle.write(str(count))

fail_calls = int(os.environ.get("STUB_FAIL_CALLS", "0"))
fail_retryable = os.environ.get("STUB_FAIL_RETRYABLE", "1") == "1"
if count <= fail_calls:
    if fail_retryable:
        print("model gateway returned empty message content", file=sys.stderr)
    else:
        print("boom: non-retryable failure", file=sys.stderr)
    sys.exit(2)

payload = json.load(sys.stdin)
json.dump({"candidates": [{"attempt": payload["attempt"], "call": count}]}, sys.stdout)
""".strip(),
        encoding="utf-8",
    )


def test_transport_retry_recovers_the_same_request(tmp_path: Path) -> None:
    stub = tmp_path / "transport_stub.py"
    _write_transport_stub(stub)
    counter = tmp_path / "counter.txt"
    counter.write_text("0", encoding="utf-8")
    extractor = JsonCommandCandidateExtractor(
        (sys.executable, str(stub)),
        transport_retries=3,
        transport_backoff_seconds=0.0,
    )
    import os

    os.environ["STUB_COUNTER"] = str(counter)
    os.environ["STUB_FAIL_CALLS"] = "2"
    os.environ["STUB_FAIL_RETRYABLE"] = "1"
    try:
        candidates = extractor.extract(_chunk())
    finally:
        os.environ.pop("STUB_COUNTER", None)
        os.environ.pop("STUB_FAIL_CALLS", None)
        os.environ.pop("STUB_FAIL_RETRYABLE", None)
    assert candidates[0]["call"] == 3
    assert counter.read_text(encoding="utf-8").strip() == "3"


def test_transport_non_retryable_failure_is_not_retried(tmp_path: Path) -> None:
    stub = tmp_path / "transport_stub.py"
    _write_transport_stub(stub)
    counter = tmp_path / "counter2.txt"
    counter.write_text("0", encoding="utf-8")
    extractor = JsonCommandCandidateExtractor(
        (sys.executable, str(stub)),
        transport_retries=3,
        transport_backoff_seconds=0.0,
    )
    import os

    os.environ["STUB_COUNTER"] = str(counter)
    os.environ["STUB_FAIL_CALLS"] = "1"
    os.environ["STUB_FAIL_RETRYABLE"] = "0"
    try:
        import pytest

        with pytest.raises(JsonCommandError):
            extractor.extract(_chunk())
    finally:
        os.environ.pop("STUB_COUNTER", None)
        os.environ.pop("STUB_FAIL_CALLS", None)
        os.environ.pop("STUB_FAIL_RETRYABLE", None)
    assert counter.read_text(encoding="utf-8").strip() == "1"
