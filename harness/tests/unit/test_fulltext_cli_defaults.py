from __future__ import annotations

from ecmonitor.fulltext_extraction.cli import build_parser


def test_model_runtime_defaults_are_fast_and_compact() -> None:
    args = build_parser().parse_args(["inspect-document", "paper.pdf"])

    assert args.pdf_parser == "pymupdf"
    assert args.extractor_model == "gemini-3.1-pro-preview"
    assert args.validator_model == "gemini-3.1-pro-preview"
    assert args.extractor_output_mode == "shared_context"
    assert args.agent_reasoning_effort == "low"
    assert args.validator_reasoning_effort == "none"
    assert args.extractor_max_tokens == 16000
    assert args.validator_batch_size == 20
    assert args.agent_timeout_seconds == 150
    assert args.agent_request_timeout_seconds == 120
    assert args.agent_empty_response_retries == 0
    assert args.agent_transport_failure_retries == 0
    assert args.transport_retries == 0
    assert args.whole_doc_empty_fallback == "auto"
