from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_agent_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "fulltext_model_agent.py"
    spec = importlib.util.spec_from_file_location("fulltext_model_agent_compact", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shared_context_expands_and_validates_full_candidates() -> None:
    agent = _load_agent_module()
    compact = {
        "contexts": [
            {
                "id": "dry",
                "matrix": "surface water",
                "site": "River A",
                "waterbody": "River A",
                "city": "Example City",
                "country": "Exampleland",
                "time": "April 2018",
                "method": "SPE-LC-MS/MS",
                "page": "3",
                "table": "Table 1",
                "evidence": "Table 1 reports concentrations in River A water.",
            }
        ],
        "rows": [
            ["dry", "PFOS", "2.1", "ng/L", "exact", "mean"],
            [
                "dry",
                "TP-1",
                "0.4",
                "ng/L",
                "exact",
                "single",
                "transformation_product",
                "Parent A",
            ],
        ],
    }
    expanded = agent._expand_shared_context_response(
        compact,
        {"chunk": {"chunk_id": "merged-doc", "page_start": 1, "page_end": 5}},
    )
    schema_path = Path(__file__).resolve().parents[2] / "schemas" / "extraction" / "model_occurrence_candidate.schema.json"
    occurrence_schema = json.loads(schema_path.read_text(encoding="utf-8"))
    response_schema = agent._response_schema("extractor", occurrence_schema)

    assert agent._validate_response(expanded, response_schema, "extractor") == []
    assert len(expanded["candidates"]) == 2
    assert expanded["candidates"][0]["result"]["statistic"] == "mean"
    assert expanded["candidates"][1]["analyte"]["transformation_product_of"] == "Parent A"



def test_grouped_multistat_row_expands_to_one_candidate_per_measurement() -> None:
    agent = _load_agent_module()
    compact = {
        "contexts": [
            {
                "id": "dry",
                "matrix": "surface water",
                "time": "April 2018",
                "page": 3,
                "evidence": "Table 1 dry-season river concentrations.",
            }
        ],
        "rows": [
            [
                "dry",
                "PFOS",
                "ng/L",
                [
                    ["1.2", "exact", "minimum"],
                    ["4.8", "exact", "maximum"],
                    ["2.7", "exact", "mean"],
                ],
                "individual",
                None,
                "PFOS",
                "Dry season",
            ]
        ],
    }
    compact_schema = agent._response_schema(
        "extractor", {}, compact_extraction=True
    )
    assert agent._validate_response(compact, compact_schema, "extractor") == []

    expanded = agent._expand_shared_context_response(
        compact,
        {"chunk": {"chunk_id": "merged-doc", "page_start": 1, "page_end": 5}},
    )

    assert len(expanded["candidates"]) == 3
    assert [item["result"]["statistic"] for item in expanded["candidates"]] == [
        "minimum",
        "maximum",
        "mean",
    ]
    assert [item["candidate_id"] for item in expanded["candidates"]] == [
        "compact-1",
        "compact-2",
        "compact-3",
    ]
    assert all(item["evidence"]["page_start"] == 3 for item in expanded["candidates"])

def test_openai_request_includes_reasoning_effort(monkeypatch) -> None:
    agent = _load_agent_module()
    captured = {}

    def fake_post(endpoint, body, *, headers, timeout_seconds, deadline=None, stream=True,
                  ttft_timeout=60.0, idle_timeout=90.0):
        del endpoint, headers, timeout_seconds, deadline, stream, ttft_timeout, idle_timeout
        captured.update(body)
        return {
            "id": "chatcmpl-test",
            "model": "gemini-3.1-pro-preview",
            "choices": [{"message": {"content": '{"contexts":[],"rows":[]}'}, "finish_reason": "stop"}],
        }

    monkeypatch.setattr(agent, "_post_json_requests", fake_post)
    content, _ = agent._call_model(
        system_prompt="sys",
        user_payload={"role": "occurrence_extractor"},
        model="gemini-3.1-pro-preview",
        base_url="https://example.invalid/v1",
        api_key="test-key",
        timeout_seconds=30,
        max_tokens=12000,
        protocol="openai",
        reasoning_effort="low",
    )

    assert content == '{"contexts":[],"rows":[]}'
    assert captured["reasoning_effort"] == "low"
    assert captured["enable_thinking"] is False



def test_openai_pdf_direct_request_uses_file_content(monkeypatch, tmp_path) -> None:
    agent = _load_agent_module()
    captured = {}
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"%PDF-1.4 test bytes")

    def fake_post(endpoint, body, *, headers, timeout_seconds, deadline=None, stream=True,
                  ttft_timeout=60.0, idle_timeout=90.0):
        del endpoint, headers, timeout_seconds, deadline, stream, ttft_timeout, idle_timeout
        captured.update(body)
        return {
            "id": "chatcmpl-pdf",
            "model": "gemini-3.1-pro-preview",
            "choices": [{"message": {"content": '{"contexts":[],"rows":[]}'}, "finish_reason": "stop"}],
        }

    monkeypatch.setenv("ECMONITOR_PDF_DIRECT", "1")
    monkeypatch.setattr(agent, "_post_json_requests", fake_post)
    content, metadata = agent._call_model(
        system_prompt="sys",
        user_payload={
            "role": "occurrence_extractor",
            "pdf_path": str(pdf),
            "chunk": {"text": "full merged document text", "page_start": 1},
        },
        model="gemini-3.1-pro-preview",
        base_url="https://example.invalid/v1",
        api_key="test-key",
        timeout_seconds=30,
        max_tokens=12000,
        protocol="openai",
        reasoning_effort="none",
    )

    assert content == '{"contexts":[],"rows":[]}'
    message_content = captured["messages"][1]["content"]
    assert isinstance(message_content, list)
    assert [item["type"] for item in message_content] == ["text", "file"]
    assert "full merged document text" not in message_content[0]["text"]
    assert message_content[1]["file"]["filename"] == "sample.pdf"
    assert message_content[1]["file"]["file_data"].startswith("data:application/pdf;base64,")
    assert metadata["pdf_direct"] is True
    assert metadata["pdf_bytes"] == len(b"%PDF-1.4 test bytes")


def test_validator_does_not_require_pdf_when_direct_mode_enabled(monkeypatch) -> None:
    agent = _load_agent_module()
    captured = {}

    def fake_post(endpoint, body, *, headers, timeout_seconds, deadline=None, stream=True,
                  ttft_timeout=60.0, idle_timeout=90.0):
        del endpoint, headers, timeout_seconds, deadline, stream, ttft_timeout, idle_timeout
        captured.update(body)
        return {
            "id": "chatcmpl-review",
            "model": "gemini-3.1-pro-preview",
            "choices": [{"message": {"content": '{"action":"reject","reason_codes":[],"failed_json_pointers":[],"requested_context":[],"human_review_required":false}'}, "finish_reason": "stop"}],
        }

    monkeypatch.setenv("ECMONITOR_PDF_DIRECT", "1")
    monkeypatch.setattr(agent, "_post_json_requests", fake_post)
    content, metadata = agent._call_model(
        system_prompt="sys",
        user_payload={"role": "evidence_validator", "chunk": {"text": "local evidence"}},
        model="gemini-3.1-pro-preview",
        base_url="https://example.invalid/v1",
        api_key="test-key",
        timeout_seconds=30,
        max_tokens=12000,
        protocol="openai",
        reasoning_effort="none",
    )

    assert "local evidence" in captured["messages"][1]["content"]
    assert metadata["pdf_direct"] is False
    assert content.startswith('{"action":"reject"')
