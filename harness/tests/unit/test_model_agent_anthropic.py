from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_agent_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "fulltext_model_agent.py"
    spec = importlib.util.spec_from_file_location("fulltext_model_agent_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _anthropic_response(*, text: str = "", thinking: str = "", stop_reason: str = "end_turn") -> dict:
    content = []
    if thinking:
        content.append({"type": "thinking", "thinking": thinking})
    if text:
        content.append({"type": "text", "text": text})
    return {
        "id": "msg_anthropic_test",
        "type": "message",
        "role": "assistant",
        "model": "deepseek-v4-flash",
        "content": content,
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }


def test_parse_anthropic_response_joins_text_and_ignores_thinking() -> None:
    agent = _load_agent_module()
    response = _anthropic_response(
        thinking="chain of thought that must be ignored",
        text='{"candidates":[{"name":"x"}]}',
    )
    content, metadata = agent._parse_anthropic_response(
        response, endpoint="https://example.invalid/v1/messages", transport="requests"
    )
    assert content == '{"candidates":[{"name":"x"}]}'
    assert metadata["protocol"] == "anthropic"
    assert metadata["finish_reason"] == "end_turn"
    assert metadata["reasoning_content_length"] == len("chain of thought that must be ignored")
    assert metadata["used_reasoning_content_fallback"] is False
    assert metadata["response_model"] == "deepseek-v4-flash"


def test_parse_anthropic_response_maps_max_tokens_to_length() -> None:
    agent = _load_agent_module()
    response = _anthropic_response(
        text='{"candidates":[]}', stop_reason="max_tokens"
    )
    _, metadata = agent._parse_anthropic_response(
        response, endpoint="https://example.invalid/v1/messages", transport="requests"
    )
    assert metadata["finish_reason"] == "length"


def test_parse_anthropic_response_raises_on_empty_text() -> None:
    agent = _load_agent_module()
    response = _anthropic_response(text="", thinking="only reasoning")
    with pytest.raises(agent.AgentCommandError) as excinfo:
        agent._parse_anthropic_response(
            response, endpoint="https://example.invalid/v1/messages", transport="requests"
        )
    assert excinfo.value.category == "model_response_empty"
    assert excinfo.value.retryable is True


def test_parse_anthropic_response_raises_without_content_blocks() -> None:
    agent = _load_agent_module()
    response = {"id": "msg_1", "content": None, "stop_reason": "stop"}
    with pytest.raises(agent.AgentCommandError) as excinfo:
        agent._parse_anthropic_response(
            response, endpoint="https://example.invalid/v1/messages", transport="requests"
        )
    assert excinfo.value.category == "model_response_empty"


def test_call_model_anthropic_builds_messages_endpoint_and_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _load_agent_module()
    captured: dict = {}

    def fake_post(
        endpoint, body, *, headers, timeout_seconds, deadline=None, stream=True,
        ttft_timeout=60.0, idle_timeout=90.0,
    ):
        del timeout_seconds, deadline, ttft_timeout, idle_timeout
        captured["endpoint"] = endpoint
        captured["body"] = body
        captured["headers"] = headers
        captured["stream"] = stream
        return _anthropic_response(text='{"candidates":[]}')

    monkeypatch.setattr(agent, "_post_json_requests", fake_post)
    monkeypatch.delenv("ECMONITOR_HTTP_TRANSPORT", raising=False)
    monkeypatch.setenv("ECMONITOR_ENABLE_THINKING", "0")

    content, metadata = agent._call_model(
        system_prompt="system prompt",
        user_payload={"role": "occurrence_extractor", "chunk": {"document_id": "doc1"}},
        model="claude-opus-4-8",
        base_url="https://ai.b1ank.top/v1",
        api_key="test-key",
        timeout_seconds=60,
        max_tokens=12000,
        protocol="anthropic",
    )

    assert captured["endpoint"] == "https://ai.b1ank.top/v1/messages"
    assert captured["headers"]["x-api-key"] == "test-key"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    body = captured["body"]
    assert body["model"] == "claude-opus-4-8"
    assert body["max_tokens"] == 12000
    assert body["system"] == "system prompt"
    assert body["thinking"] == {"type": "disabled"}
    assert body["stream"] is True
    assert captured["stream"] is True
    assert body["messages"][0]["role"] == "user"
    assert "document_id" in body["messages"][0]["content"]
    assert "response_format" not in body
    assert content == '{"candidates":[]}'
    assert metadata["protocol"] == "anthropic"


def test_call_model_anthropic_respects_enable_thinking_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _load_agent_module()
    captured: dict = {}

    def fake_post(
        endpoint, body, *, headers, timeout_seconds, deadline=None, stream=True,
        ttft_timeout=60.0, idle_timeout=90.0,
    ):
        del endpoint, headers, timeout_seconds, deadline, stream, ttft_timeout, idle_timeout
        captured["body"] = body
        return _anthropic_response(text='{"candidates":[]}')

    monkeypatch.setattr(agent, "_post_json_requests", fake_post)
    monkeypatch.delenv("ECMONITOR_HTTP_TRANSPORT", raising=False)
    monkeypatch.setenv("ECMONITOR_ENABLE_THINKING", "1")

    agent._call_model(
        system_prompt="sys",
        user_payload={"role": "occurrence_extractor"},
        model="claude-opus-4-8",
        base_url="https://ai.b1ank.top/v1",
        api_key="test-key",
        timeout_seconds=60,
        max_tokens=12000,
        protocol="anthropic",
    )
    assert "thinking" not in captured["body"]


def test_call_model_openai_path_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _load_agent_module()
    captured: dict = {}

    def fake_post(
        endpoint, body, *, headers, timeout_seconds, deadline=None, stream=True,
        ttft_timeout=60.0, idle_timeout=90.0,
    ):
        del timeout_seconds, deadline, stream, ttft_timeout, idle_timeout
        captured["endpoint"] = endpoint
        captured["headers"] = headers
        return {
            "id": "chatcmpl_1",
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "message": {"role": "assistant", "content": '{"candidates":[]}'},
                    "finish_reason": "stop",
                }
            ],
        }

    monkeypatch.setattr(agent, "_post_json_requests", fake_post)
    monkeypatch.delenv("ECMONITOR_HTTP_TRANSPORT", raising=False)

    content, metadata = agent._call_model(
        system_prompt="sys",
        user_payload={"role": "occurrence_extractor"},
        model="claude-opus-4-8",
        base_url="https://ai.b1ank.top/v1",
        api_key="test-key",
        timeout_seconds=60,
        max_tokens=12000,
        protocol="openai",
    )
    assert captured["endpoint"] == "https://ai.b1ank.top/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert content == '{"candidates":[]}'
    assert metadata["protocol"] == "openai"
