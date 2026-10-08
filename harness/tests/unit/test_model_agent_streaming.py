"""Unit tests for streaming model requests with TTFT / idle-timeout abort."""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest


def _load_agent_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "fulltext_model_agent.py"
    spec = importlib.util.spec_from_file_location("fulltext_model_agent_streaming_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeIterLines:
    """Minimal stand-in for requests.Response.iter_lines."""

    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self, decode_unicode=True):
        del decode_unicode
        yield from self._lines


ANTHROPIC_SSE = [
    "event: message_start",
    'data: {"type":"message_start","message":{"id":"msg_1","model":"deepseek-v4-flash",'
    '"usage":{"input_tokens":10,"output_tokens":0}}}',
    "",
    "event: content_block_start",
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
    "",
    "event: content_block_delta",
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello "}}',
    "",
    "event: content_block_delta",
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"world"}}',
    "",
    "event: message_delta",
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":9}}',
    "",
    "event: message_stop",
    'data: {"type":"message_stop"}',
    "",
]

OPENAI_SSE = [
    'data: {"id":"chatcmpl_1","model":"deepseek-v4-flash","choices":[{"delta":{"role":"assistant"},'
    '"finish_reason":null}]}',
    "",
    'data: {"id":"chatcmpl_1","model":"deepseek-v4-flash","choices":[{"delta":{"content":"Hello "},'
    '"finish_reason":null}]}',
    "",
    'data: {"id":"chatcmpl_1","model":"deepseek-v4-flash","choices":[{"delta":{"content":"world"},'
    '"finish_reason":"stop"}]}',
    "",
    'data: {"id":"chatcmpl_1","model":"deepseek-v4-flash","choices":[],"usage":{"total_tokens":20}}',
    "",
    "data: [DONE]",
    "",
]


def _consume(agent, lines, *, endpoint, ttft=60.0, idle=90.0, started=None):
    return agent._consume_sse(
        _FakeIterLines(lines),
        endpoint=endpoint,
        started=started if started is not None else time.monotonic(),
        deadline=None,
        ttft_timeout=ttft,
        idle_timeout=idle,
    )


def test_consume_anthropic_sse_assembles_text() -> None:
    agent = _load_agent_module()
    response = _consume(agent, ANTHROPIC_SSE, endpoint="https://example.invalid/v1/messages")
    assert response["content"] == [{"type": "text", "text": "Hello world"}]
    assert response["stop_reason"] == "end_turn"
    assert response["model"] == "deepseek-v4-flash"
    assert response["id"] == "msg_1"
    assert response["ecmonitor_stream"]["stream"] == "sse"
    assert response["ecmonitor_stream"]["ttft_seconds"] is not None


def test_consume_openai_sse_assembles_content_and_usage() -> None:
    agent = _load_agent_module()
    response = _consume(agent, OPENAI_SSE, endpoint="https://example.invalid/v1/chat/completions")
    assert response["choices"][0]["message"]["content"] == "Hello world"
    assert response["choices"][0]["finish_reason"] == "stop"
    assert response["usage"] == {"total_tokens": 20}
    assert response["id"] == "chatcmpl_1"


def test_consume_sse_aborts_when_no_first_token() -> None:
    agent = _load_agent_module()
    lines = [
        "event: message_start",
        'data: {"type":"message_start","message":{"id":"msg_1","model":"m"}}',
    ]
    # First event arrives 100s after the request started, well past the 5s TTFT budget.
    with pytest.raises(agent.AgentCommandError) as excinfo:
        _consume(agent, lines, endpoint="https://example.invalid/v1/messages", ttft=5.0,
                 started=time.monotonic() - 100.0)
    assert excinfo.value.category == "model_transport_timeout"
    assert excinfo.value.retryable is True


def test_consume_sse_aborts_on_idle_stall() -> None:
    agent = _load_agent_module()

    class _SlowLines:
        def iter_lines(self, decode_unicode=True):
            del decode_unicode
            yield "event: message_start"
            yield 'data: {"type":"message_start","message":{"id":"m","model":"m"}}'
            time.sleep(2.5)  # idle longer than the 0.5s idle timeout
            yield "event: content_block_delta"
            yield 'data: {"type":"content_block_delta","index":0,"delta":{"text":"x"}}'

    with pytest.raises(agent.AgentCommandError) as excinfo:
        agent._consume_sse(
            _SlowLines(),
            endpoint="https://example.invalid/v1/messages",
            started=time.monotonic(),
            deadline=None,
            ttft_timeout=60.0,
            idle_timeout=0.5,
        )
    assert excinfo.value.category == "model_transport_timeout"


def test_anthropic_error_event_raises_gateway_error() -> None:
    agent = _load_agent_module()
    lines = [
        "event: error",
        'data: {"type":"error","error":{"type":"overloaded_error","message":"upstream busy"}}',
    ]
    with pytest.raises(agent.AgentCommandError) as excinfo:
        _consume(agent, lines, endpoint="https://example.invalid/v1/messages")
    assert excinfo.value.category == "model_gateway_error"


def test_stream_metadata_flows_into_anthropic_parser() -> None:
    agent = _load_agent_module()
    response = _consume(agent, ANTHROPIC_SSE, endpoint="https://example.invalid/v1/messages")
    content, metadata = agent._parse_anthropic_response(
        response, endpoint="https://example.invalid/v1/messages", transport="requests"
    )
    assert content == "Hello world"
    assert metadata["stream"] == "sse"
    assert metadata["ttft_seconds"] is not None


def test_post_json_requests_falls_back_when_stream_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _load_agent_module()
    captured = {"stream_calls": 0, "plain_calls": 0}

    class _FakeResult:
        status_code = 200
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            return None

        def json(self):
            return {"id": "plain", "model": "m", "choices": [{"message": {"content": "x"}}]}

    def fake_post(*args, **kwargs):
        if kwargs.get("stream"):
            captured["stream_calls"] += 1
            raise agent._StreamNotSupported("gateway rejected stream")
        captured["plain_calls"] += 1
        captured["plain_body"] = json.loads(kwargs["data"])
        return _FakeResult()

    monkeypatch.setattr("requests.post", fake_post)
    response = agent._post_json_requests(
        "https://example.invalid/v1/chat/completions",
        {"model": "m", "stream": True},
        headers={"Authorization": "Bearer k"},
        timeout_seconds=30,
        stream=True,
    )
    assert captured["stream_calls"] == 1
    assert captured["plain_calls"] == 1
    assert captured["plain_body"]["model"] == "m"
    assert "stream" not in captured["plain_body"]
    assert response["id"] == "plain"


def test_post_json_streaming_non_sse_response_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _load_agent_module()

    class _FakeResult:
        status_code = 200
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            return None

        def json(self):
            return {"id": "full", "model": "m", "choices": [{"message": {"content": "y"}}]}

    def fake_post(*args, **kwargs):
        return _FakeResult()

    monkeypatch.setattr("requests.post", fake_post)
    response = agent._post_json_streaming(
        "https://example.invalid/v1/chat/completions",
        {"model": "m"},
        headers={"Authorization": "Bearer k"},
        timeout_seconds=30,
        deadline=None,
        ttft_timeout=60.0,
        idle_timeout=90.0,
    )
    assert response["id"] == "full"
    assert response["ecmonitor_stream"]["stream"] == "non_sse"
