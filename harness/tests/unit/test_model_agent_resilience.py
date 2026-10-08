from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from ecmonitor.fulltext_extraction.batch import SequentialLibraryRunner, classify_document_error
from ecmonitor.fulltext_extraction.models import DocumentRunReport


def _load_agent_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "fulltext_model_agent.py"
    spec = importlib.util.spec_from_file_location("fulltext_model_agent", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeControlPlane:
    def __init__(self) -> None:
        self.committed: set[str] = set()

    def has_committed_path(self, pdf_path: Path) -> bool:
        return str(pdf_path.resolve()) in self.committed


class FlakyHarness:
    def __init__(self, control_plane: FakeControlPlane, failures_before_success: int) -> None:
        self.control_plane = control_plane
        self.failures_before_success = failures_before_success
        self.calls = 0

    def run_document(self, pdf_path: Path, *, bibliographic_metadata=None):
        del bibliographic_metadata
        self.calls += 1
        if self.calls <= self.failures_before_success:
            raise RuntimeError(
                "agent command exited with 2: model gateway returned empty message content"
            )
        resolved = pdf_path.resolve()
        self.control_plane.committed.add(str(resolved))
        return DocumentRunReport(
            document_id=resolved.stem,
            document_session_id=f"session-{self.calls}",
            source_path=str(resolved),
            source_sha256="sha256",
            registry_snapshot_version=0,
            parser_name="fake",
            parser_version="1",
            page_count=1,
            chunk_count=1,
            candidate_count=0,
            review_count=0,
            resolution_count=0,
            committed=True,
            output_path=None,
            bibliographic_metadata={},
        )


def test_unique_models_preserves_order_and_deduplicates() -> None:
    agent = _load_agent_module()
    assert agent._unique_models("primary", ["fallback-a", "primary", "fallback-b"]) == [
        "primary",
        "fallback-a",
        "fallback-b",
    ]


def test_call_model_resilient_retries_empty_then_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _load_agent_module()
    calls: list[str] = []

    def fake_call_model(**kwargs):
        model = kwargs["model"]
        calls.append(model)
        if model == "primary":
            raise agent.AgentCommandError(
                "model gateway returned empty message content",
                category="model_response_empty",
                retryable=True,
            )
        return (
            '{"candidates":[]}',
            {
                "response_model": model,
                "finish_reason": "stop",
                "content_length": 16,
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    monkeypatch.setattr(agent, "_call_model", fake_call_model)
    monkeypatch.setattr(agent.time, "sleep", lambda *_args, **_kwargs: None)

    content, metadata = agent._call_model_resilient(
        system_prompt="sys",
        user_payload={"role": "occurrence_extractor"},
        models=["primary", "fallback"],
        base_url="https://example.invalid/v1",
        api_key="test-key",
        deadline=agent.time.monotonic() + 30,
        request_timeout_seconds=10,
        max_tokens=128,
        empty_response_retries=1,
        phase="initial",
    )

    assert content == '{"candidates":[]}'
    assert calls == ["primary", "primary", "fallback"]
    assert metadata["requested_model"] == "fallback"
    assert metadata["phase"] == "initial"
    assert len(metadata["attempts"]) == 2


def test_call_model_resilient_respects_shared_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _load_agent_module()
    calls = {"count": 0}

    def fake_call_model(**kwargs):
        calls["count"] += 1
        raise agent.AgentCommandError(
            "model transport failed: read timed out",
            category="model_transport_timeout",
            retryable=True,
        )

    monkeypatch.setattr(agent, "_call_model", fake_call_model)
    monkeypatch.setattr(agent.time, "sleep", lambda *_args, **_kwargs: None)

    with pytest.raises(agent.AgentCommandError) as excinfo:
        agent._call_model_resilient(
            system_prompt="sys",
            user_payload={"role": "occurrence_extractor"},
            models=["primary", "fallback"],
            base_url="https://example.invalid/v1",
            api_key="test-key",
            deadline=agent.time.monotonic() + 0.01,
            request_timeout_seconds=5,
            max_tokens=128,
            empty_response_retries=2,
            phase="initial",
        )

    assert excinfo.value.category in {"model_transport_timeout", "agent_deadline_exhausted"}
    assert calls["count"] <= 2


def test_classify_document_error_marks_gateway_failures_retryable() -> None:
    empty = classify_document_error(RuntimeError("model gateway returned empty message content"))
    timeout = classify_document_error(
        RuntimeError("model transport failed: HTTPSConnectionPool Read timed out")
    )
    ttft_timeout = classify_document_error(
        RuntimeError("agent command exited with 2: no first content token within 60s")
    )
    stream_idle = classify_document_error(
        RuntimeError("agent command exited with 2: stream idle for 90s")
    )
    stream_deadline = classify_document_error(
        RuntimeError("agent command exited with 2: stream deadline exhausted before completion")
    )
    process_timeout = classify_document_error(
        subprocess.TimeoutExpired(cmd=["python"], timeout=270)
    )
    bad_pdf = classify_document_error(FileNotFoundError("missing.pdf"))

    assert empty.retryable and empty.category == "model_response_empty"
    budget = classify_document_error(
        RuntimeError("agent command exited with 2: model call budget exhausted before a usable response")
    )
    assert budget.retryable and budget.category == "agent_deadline_exhausted"
    assert timeout.retryable and timeout.category == "model_transport_timeout"
    assert ttft_timeout.retryable and ttft_timeout.category == "model_transport_timeout"
    assert stream_idle.retryable and stream_idle.category == "model_transport_timeout"
    assert stream_deadline.retryable and stream_deadline.category == "model_transport_timeout"
    assert process_timeout.retryable and process_timeout.category == "agent_process_timeout"
    assert not bad_pdf.retryable


def test_document_level_retry_recovers_from_empty_gateway_response(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-fake")
    control_plane = FakeControlPlane()
    harness = FlakyHarness(control_plane, failures_before_success=1)
    runner = SequentialLibraryRunner(
        harness,  # type: ignore[arg-type]
        reports_jsonl=tmp_path / "events.jsonl",
        max_document_attempts=3,
        retry_backoff_seconds=0.0,
    )

    summary = runner.run_paths([pdf], source_label="test")

    assert summary.attempted == 1
    assert summary.committed == 1
    assert summary.failed == 0
    assert summary.retry_attempts == 1
    assert harness.calls == 2
