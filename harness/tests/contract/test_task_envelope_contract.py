from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from ecmonitor.contracts import TaskEnvelope


def test_task_envelope_matches_shared_schema() -> None:
    root = Path(__file__).resolve().parents[2]
    schema = json.loads((root / "schemas/common/task_envelope.schema.json").read_text(encoding="utf-8"))
    task = TaskEnvelope(
        schema_version="task-envelope-v1",
        task_id="task-1",
        workflow_run_id="run-1",
        document_id="doc-1",
        from_agent="retrieval",
        to_agent="download",
        task_type="download_requested",
        idempotency_key="run-1:doc-1:download:v1",
        attempt=1,
        created_at="2026-08-25T00:00:00Z",
        correlation_id=None,
        causation_id=None,
        policy_version="workflow-v1",
    )
    Draft202012Validator(schema).validate(task.to_dict())


def test_task_envelope_rejects_zero_attempt() -> None:
    try:
        TaskEnvelope(
            schema_version="v1",
            task_id="task-1",
            workflow_run_id="run-1",
            document_id=None,
            from_agent="orchestrator",
            to_agent="retrieval",
            task_type="start",
            idempotency_key="run-1:start",
            attempt=0,
            created_at="2026-08-25T00:00:00Z",
        )
    except ValueError as exc:
        assert "attempt" in str(exc)
    else:
        raise AssertionError("zero attempt was accepted")
