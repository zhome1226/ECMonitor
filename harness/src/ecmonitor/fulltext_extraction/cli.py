"""Command-line entry point for the full-text extraction harness."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from ecmonitor.fulltext_extraction.adapters.base import (
    CandidateExtractor,
    DocumentChunker,
    EvidenceValidator,
    PdfParserAdapter,
)
from ecmonitor.fulltext_extraction.adapters.deterministic_table import (
    DeterministicTableCandidateExtractor,
)
from ecmonitor.fulltext_extraction.adapters.json_command import (
    JsonCommandCandidateExtractor,
    JsonCommandEvidenceValidator,
)
from ecmonitor.fulltext_extraction.adapters.pubchem import PubChemPugRestResolver
from ecmonitor.fulltext_extraction.adapters.pymupdf4llm_adapter import PyMuPDF4LLMParser
from ecmonitor.fulltext_extraction.adapters.pymupdf_adapter import PyMuPDFParser
from ecmonitor.fulltext_extraction.adapters.table_aware import (
    TableAwareChunker,
    TableAwarePyMuPDFParser,
)
from ecmonitor.fulltext_extraction.batch import (
    ParallelLibraryRunner,
    SequentialLibraryRunner,
)
from ecmonitor.fulltext_extraction.chunk_selection import likely_occurrence_chunk
from ecmonitor.fulltext_extraction.geocode import OfflineGeocodeResolver
from ecmonitor.fulltext_extraction.harness import FulltextExtractionHarness
from ecmonitor.fulltext_extraction.models import EvidenceChunk
from ecmonitor.fulltext_extraction.quality import PolicyGatedEvidenceValidator
from ecmonitor.fulltext_extraction.registry import ChemicalRegistry
from ecmonitor.fulltext_extraction.signoff import SignedExampleStore
from ecmonitor.fulltext_extraction.storage import FulltextControlPlane
from ecmonitor.fulltext_extraction.tooling import tool_availability
from ecmonitor.paths import project_root

_DEFAULT_DATABASE = Path("state/fulltext_extraction_control.sqlite3")
_DEFAULT_REGISTRY = Path("state/chemical_registry.sqlite3")
_DEFAULT_OUTPUT = Path("runs/fulltext_extraction/documents")
_DEFAULT_PUBCHEM_CACHE = Path("state/pubchem_resolution_cache.json")
_DEFAULT_SIGNED_EXAMPLES = Path("state/signed_review_examples.jsonl")
_PROJECT_ROOT = project_root()
_DEFAULT_AGENT_SCRIPT = _PROJECT_ROOT / "scripts" / "fulltext_model_agent.py"
_DEFAULT_SCHEMA = (
    _PROJECT_ROOT / "schemas" / "extraction" / "model_occurrence_candidate.schema.json"
)
_DEFAULT_EXTRACTOR_PROMPT = _PROJECT_ROOT / "prompts" / "extraction" / "occurrence_extractor_compact.md"
_DEFAULT_VALIDATOR_PROMPT = _PROJECT_ROOT / "prompts" / "extraction" / "evidence_validator.md"


def _env_model_list(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ECMonitor full-text extraction harness")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect = subparsers.add_parser(
        "inspect-document",
        help="parse, chunk, and transactionally persist one PDF",
    )
    inspect.add_argument("pdf", type=Path)
    _add_runtime_arguments(inspect)

    library = subparsers.add_parser(
        "run-library",
        help="process PDFs sequentially with a commit and cleanup barrier after every document",
    )
    library.add_argument("pdf_dir", type=Path)
    _add_runtime_arguments(library)
    _add_batch_arguments(library)

    pilot = subparsers.add_parser(
        "run-pilot",
        help="process the PDFs listed in a pilot manifest sequentially",
    )
    pilot.add_argument("manifest", type=Path)
    _add_runtime_arguments(pilot)
    _add_batch_arguments(pilot)

    status = subparsers.add_parser("status", help="show persistent harness status")
    status.add_argument("--database", type=Path, default=_DEFAULT_DATABASE)
    signoff = subparsers.add_parser(
        "signoff",
        help="record a human disposition on a pending review task (feeds extractor few-shot)",
    )
    signoff.add_argument("task_id", type=str)
    signoff.add_argument(
        "--disposition",
        choices=("accepted", "rejected"),
        required=True,
    )
    signoff.add_argument("--note", default=None, help="optional human review note")
    signoff.add_argument("--database", type=Path, default=_DEFAULT_DATABASE)
    signoff.add_argument(
        "--signed-examples-path",
        type=Path,
        default=_DEFAULT_SIGNED_EXAMPLES,
        help="JSONL log of signed human review decisions used as extractor few-shot",
    )
    subparsers.add_parser("tools", help="show installed and missing optional extraction tools")
    return parser


def _add_batch_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=1, help="concurrent document workers (threads)")
    parser.add_argument("--concurrency-floor", type=int, default=1, help="adaptive concurrency floor (never below this many in-flight docs)")
    parser.add_argument("--concurrency-growth-streak", type=int, default=3, help="clean successes needed to raise the adaptive concurrency limit")
    parser.add_argument(
        "--chunk-workers",
        type=int,
        default=2,
        help=(
            "concurrent chunk workers inside one document (extractor/validator calls for a "
            "document's selected chunks run in parallel instead of one-at-a-time)"
        ),
    )
    parser.add_argument(
        "--merge-chunks",
        action="store_true",
        help=(
            "whole-document single call: skip the occurrence prefilter and send the entire "
            "document text as one merged body in a single model call (extractor + validator), "
            "so methods/sampling context is always present; falls back to per-chunk mode when "
            "the merged input would exceed --max-merged-input-chars"
        ),
    )
    parser.add_argument(
        "--max-merged-input-chars",
        type=int,
        default=120_000,
        help="cap on merged whole-document input characters before falling back to per-chunk",
    )
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--document-max-attempts", type=int, default=3)
    parser.add_argument("--document-retry-backoff-seconds", type=float, default=5.0)
    parser.add_argument(
        "--reports-jsonl",
        type=Path,
        default=Path("runs/fulltext_extraction/library_events.jsonl"),
    )


def _add_runtime_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database", type=Path, default=_DEFAULT_DATABASE)
    parser.add_argument("--registry", type=Path, default=_DEFAULT_REGISTRY)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT)
    parser.add_argument("--enable-pubchem", action="store_true")
    parser.add_argument(
        "--pubchem-cache",
        type=Path,
        default=_DEFAULT_PUBCHEM_CACHE,
        help="persistent JSON cache for PubChem chemical-name resolutions (shared across runs)",
    )
    parser.add_argument(
        "--signed-examples-path",
        type=Path,
        default=_DEFAULT_SIGNED_EXAMPLES,
        help="JSONL log of human-signed review decisions injected as bounded extractor few-shot",
    )
    parser.add_argument(
        "--geocode-data-dir",
        type=Path,
        default=_PROJECT_ROOT / "local_assets" / "geo",
        help="directory with countries.csv, states.csv, cities.json for approximate geocoding",
    )
    parser.add_argument(
        "--no-geocode",
        action="store_true",
        help="disable approximate location geocoding",
    )
    parser.add_argument(
        "--pdf-parser",
        choices=("pymupdf", "pymupdf4llm"),
        default="pymupdf",
        help=(
            "PDF parser backend (fast native PyMuPDF is the default; use pymupdf4llm "
            "explicitly when Markdown table reconstruction is required)"
        ),
    )
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument(
        "--model-agents",
        action="store_true",
        help="bind short-lived extractor and validator commands to the configured model API",
    )
    parser.add_argument(
        "--extractor-model",
        default=os.environ.get("ECMONITOR_EXTRACTOR_MODEL", "gemini-3.1-pro-preview"),
    )
    parser.add_argument(
        "--validator-model",
        default=os.environ.get("ECMONITOR_VALIDATOR_MODEL", "gemini-3.1-pro-preview"),
    )
    parser.add_argument(
        "--extractor-fallback-model",
        action="append",
        default=_env_model_list("ECMONITOR_EXTRACTOR_FALLBACK_MODELS"),
    )
    parser.add_argument(
        "--validator-fallback-model",
        action="append",
        default=_env_model_list("ECMONITOR_VALIDATOR_FALLBACK_MODELS"),
    )
    parser.add_argument("--agent-script", type=Path, default=_DEFAULT_AGENT_SCRIPT)
    parser.add_argument("--agent-schema", type=Path, default=_DEFAULT_SCHEMA)
    parser.add_argument("--extractor-prompt", type=Path, default=_DEFAULT_EXTRACTOR_PROMPT)
    parser.add_argument("--validator-prompt", type=Path, default=_DEFAULT_VALIDATOR_PROMPT)
    parser.add_argument(
        "--agent-timeout-seconds",
        type=int,
        default=150,
        help="total deadline shared by initial and repair model calls",
    )
    parser.add_argument("--agent-request-timeout-seconds", type=int, default=120)
    parser.add_argument("--agent-empty-response-retries", type=int, default=0)
    parser.add_argument(
        "--agent-transport-failure-retries",
        type=int,
        default=0,
        help="gateway/timeout retries inside one short-lived model subprocess",
    )
    parser.add_argument(
        "--transport-retries",
        type=int,
        default=0,
        help=(
            "same-request retries for retryable model transport failures (timeouts, gateway, "
            "rate limit) before a whole-document re-run is considered"
        ),
    )
    parser.add_argument(
        "--transport-backoff-seconds",
        type=float,
        default=2.0,
        help="base backoff for same-request transport retries (doubles per attempt)",
    )
    parser.add_argument("--extractor-max-tokens", type=int, default=16000)
    parser.add_argument("--validator-max-tokens", type=int, default=8000)
    parser.add_argument(
        "--validator-batch-size",
        type=int,
        default=20,
        help="maximum candidates per validator request to bound output tokens",
    )
    parser.add_argument(
        "--agent-reasoning-effort",
        choices=("none", "minimal", "low", "medium", "high"),
        default=os.environ.get("ECMONITOR_REASONING_EFFORT", "low"),
        help="extractor reasoning effort sent to OpenAI-compatible gateways",
    )
    parser.add_argument(
        "--validator-reasoning-effort",
        choices=("none", "minimal", "low", "medium", "high"),
        default=os.environ.get("ECMONITOR_VALIDATOR_REASONING_EFFORT", "none"),
        help="validator reasoning effort; none avoids hidden-token truncation",
    )
    parser.add_argument(
        "--extractor-output-mode",
        choices=("shared_context", "full"),
        default=os.environ.get("ECMONITOR_EXTRACTOR_OUTPUT_MODE", "shared_context"),
        help="emit shared table/document context once, then expand compact rows locally",
    )
    parser.add_argument(
        "--pdf-direct",
        action="store_true",
        help="attach source PDF directly to the OpenAI-compatible extractor request",
    )
    parser.add_argument(
        "--table-aware-pdf",
        action="store_true",
        help=(
            "use PyMuPDF table detection and header-repeated small row windows for dense "
            "monitoring tables"
        ),
    )
    parser.add_argument(
        "--table-window-rows",
        type=int,
        default=2,
        help="number of analyte rows per table-aware model window (small values preserve column binding)",
    )
    parser.add_argument(
        "--table-aware-focus",
        action="store_true",
        help=(
            "when table-aware parsing is enabled, send table row windows plus only the early "
            "methods/site context pages to the extractor"
        ),
    )
    parser.add_argument(
        "--deterministic-table-extractor",
        action="store_true",
        help=(
            "bind numeric monitoring-table cells locally and use the model only for review; "
            "requires --table-aware-pdf and --model-agents"
        ),
    )
    parser.add_argument(
        "--agent-protocol",
        choices=("openai", "anthropic"),
        default=os.environ.get("ECMONITOR_PROTOCOL")
        or ("anthropic" if os.environ.get("ECMONITOR_ANTHROPIC_API") == "1" else "openai"),
        help=(
            "agent model API protocol: anthropic uses POST /v1/messages with thinking.disabled "
            "(the deepseek-backed gateway ignores enable_thinking on /chat/completions)"
        ),
    )
    parser.add_argument(
        "--agent-stream",
        action="store_true",
        help="request an SSE stream from the model gateway and abort fast when no first "
        "token arrives (TTFT) or the stream idles, instead of waiting out a full timeout on "
        "hung requests (default: on, see --no-agent-stream)"
    )
    parser.add_argument(
        "--no-agent-stream",
        action="store_true",
        help="disable streaming model requests (ECMONITOR_STREAM=0)"
    )
    parser.add_argument(
        "--agent-ttft-seconds",
        type=float,
        default=60.0,
        help="abort a streaming request if no first token arrives within this many seconds "
        "(ECMONITOR_TTFT_TIMEOUT)"
    )
    parser.add_argument(
        "--agent-stream-idle-seconds",
        type=float,
        default=90.0,
        help="abort a streaming request if no SSE event arrives for this many seconds "
        "(ECMONITOR_STREAM_IDLE_TIMEOUT)"
    )
    parser.add_argument(
        "--agent-audit-dir",
        type=Path,
        default=Path("runs/fulltext_extraction/model_audit"),
    )
    parser.add_argument(
        "--allow-model-accept",
        action="store_true",
        help="allow validator accept decisions without pilot human signoff (not recommended)",
    )
    parser.add_argument(
        "--no-chunk-prefilter",
        action="store_true",
        help="send every chunk to the model instead of the high-recall concentration prefilter",
    )
    parser.add_argument(
        "--no-focused-second-pass",
        action="store_true",
        help=(
            "disable the focused concentration second pass (one small model call on the "
            "concentration-bearing regions when a first extraction returns zero candidates)"
        ),
    )
    parser.add_argument(
        "--max-focus-chars",
        type=int,
        default=40_000,
        help="character cap for the P2b focused concentration second pass",
    )
    parser.add_argument(
        "--detection-limit-context-max-chars",
        type=int,
        default=6_000,
        help="cap for document-local LOD/LOQ context injected into each model request",
    )
    parser.add_argument(
        "--chemical-identity-context-max-chars",
        type=int,
        default=8_000,
        help="cap for document-local analyte/abbreviation context injected into each model request",
    )
    parser.add_argument(
        "--windowed-bundles",
        action="store_true",
        help="preserve contiguous windows of oversized PDF chunks instead of head/tail truncation",
    )
    parser.add_argument(
        "--whole-doc-empty-fallback",
        choices=("auto", "always", "never"),
        default="auto",
        help=(
            "behavior after a whole-document PDF call returns zero candidates: auto applies "
            "a cheap local occurrence-likelihood gate, always preserves legacy per-chunk "
            "fallback, and never disables the fallback"
        ),
    )


def _build_harness(args: argparse.Namespace) -> FulltextExtractionHarness:
    extractor_factory: Callable[[], CandidateExtractor] | None = None
    validator_factory: Callable[[], EvidenceValidator] | None = None
    if args.model_agents:
        _validate_model_runtime(args)
        extractor_command = _model_command(
            args, role="extractor", model=args.extractor_model, max_tokens=args.extractor_max_tokens
        )
        validator_command = _model_command(
            args, role="validator", model=args.validator_model, max_tokens=args.validator_max_tokens
        )

        if getattr(args, "deterministic_table_extractor", False):
            if not getattr(args, "table_aware_pdf", False):
                raise ValueError("--deterministic-table-extractor requires --table-aware-pdf")

            extractor_factory = DeterministicTableCandidateExtractor
        else:
            def build_extractor() -> CandidateExtractor:
                return JsonCommandCandidateExtractor(
                    extractor_command,
                    prompt_path=args.extractor_prompt.resolve(),
                    timeout_seconds=args.agent_timeout_seconds + 30,
                    cwd=_PROJECT_ROOT,
                    transport_retries=args.transport_retries,
                    transport_backoff_seconds=args.transport_backoff_seconds,
                )

            extractor_factory = build_extractor

        def build_validator() -> EvidenceValidator:
            delegate = JsonCommandEvidenceValidator(
                validator_command,
                prompt_path=args.validator_prompt.resolve(),
                timeout_seconds=args.agent_timeout_seconds + 30,
                cwd=_PROJECT_ROOT,
                transport_retries=args.transport_retries,
                transport_backoff_seconds=args.transport_backoff_seconds,
                max_batch_size=args.validator_batch_size,
            )
            return PolicyGatedEvidenceValidator(
                delegate, require_pilot_human_signoff=not args.allow_model_accept
            )

        validator_factory = build_validator

    geocode_resolver = (
        None
        if args.no_geocode
        else OfflineGeocodeResolver(args.geocode_data_dir)
    )
    parser: PdfParserAdapter
    chunker: DocumentChunker | None
    if getattr(args, "table_aware_pdf", False):
        if args.table_window_rows < 1:
            raise ValueError("--table-window-rows must be positive")
        parser = TableAwarePyMuPDFParser()
        chunker = TableAwareChunker(table_rows=args.table_window_rows)
    else:
        parser = (
            PyMuPDFParser()
            if args.pdf_parser == "pymupdf"
            else PyMuPDF4LLMParser()
        )
        chunker = None
    return FulltextExtractionHarness(
        parser=parser,
        control_plane=FulltextControlPlane(args.database),
        chemical_registry=ChemicalRegistry(args.registry),
        output_dir=args.output_dir,
        signed_example_store=SignedExampleStore(args.signed_examples_path),
        chemical_resolver=(
            PubChemPugRestResolver(cache_path=args.pubchem_cache)
            if args.enable_pubchem
            else None
        ),
        geocode_resolver=geocode_resolver,
        extractor_factory=extractor_factory,
        validator_factory=validator_factory,
        chunker=chunker,
        chunk_selector=(
            _deterministic_table_selector
            if args.model_agents and getattr(args, "deterministic_table_extractor", False)
            else (
                _table_aware_focus_selector
                if args.model_agents and getattr(args, "table_aware_focus", False)
                else (
                    likely_occurrence_chunk
                    if args.model_agents and not args.no_chunk_prefilter
                    else None
                )
            )
        ),
        maximum_extraction_attempts=args.max_attempts,
        chunk_parallelism=getattr(args, "chunk_workers", 1),
        merge_chunks_for_extraction=getattr(args, "merge_chunks", False),
        max_merged_input_chars=getattr(args, "max_merged_input_chars", 120_000),
        focused_second_pass_on_empty=not args.no_focused_second_pass,
        max_focus_chars=args.max_focus_chars,
        whole_doc_empty_fallback=getattr(args, "whole_doc_empty_fallback", "auto"),
        detection_limit_context_max_chars=args.detection_limit_context_max_chars,
        chemical_identity_context_max_chars=args.chemical_identity_context_max_chars,
        windowed_bundles=args.windowed_bundles,
    )


def _deterministic_table_selector(chunk: EvidenceChunk) -> bool:
    """The local extractor consumes table geometry only; prose remains validator context."""
    return getattr(chunk, "chunk_type", "") == "table_row_window"


def _table_aware_focus_selector(chunk: EvidenceChunk) -> bool:
    """Keep dense table windows and the early methods/site pages; skip references/results prose."""
    if getattr(chunk, "chunk_type", "") == "table_row_window":
        return True
    return getattr(chunk, "page_end", 9999) <= 5


def _model_command(
    args: argparse.Namespace, *, role: str, model: str, max_tokens: int
) -> tuple[str, ...]:
    return (
        sys.executable,
        str(args.agent_script.resolve()),
        "--role",
        role,
        "--model",
        model,
        "--schema",
        str(args.agent_schema.resolve()),
        "--audit-dir",
        str(args.agent_audit_dir.resolve()),
        "--timeout-seconds",
        str(args.agent_timeout_seconds),
        "--request-timeout-seconds",
        str(args.agent_request_timeout_seconds),
        "--empty-response-retries",
        str(args.agent_empty_response_retries),
        "--transport-failure-retries",
        str(args.agent_transport_failure_retries),
        "--max-tokens",
        str(max_tokens),
        "--protocol",
        args.agent_protocol,
        "--reasoning-effort",
        (
            args.agent_reasoning_effort
            if role == "extractor"
            else args.validator_reasoning_effort
        ),
        *(
            ("--extractor-output-mode", args.extractor_output_mode)
            if role == "extractor"
            else ()
        ),
        *_fallback_model_arguments(args, role=role),
    )


def _fallback_model_arguments(args: argparse.Namespace, *, role: str) -> tuple[str, ...]:
    models = (
        args.extractor_fallback_model
        if role == "extractor"
        else args.validator_fallback_model
    )
    return tuple(part for model in models for part in ("--fallback-model", model))


def _validate_model_runtime(args: argparse.Namespace) -> None:
    required_paths = (
        args.agent_script,
        args.agent_schema,
        args.extractor_prompt,
        args.validator_prompt,
    )
    missing = [str(path) for path in required_paths if not path.resolve().is_file()]
    if missing:
        raise FileNotFoundError("missing model-agent files: " + ", ".join(missing))
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required with --model-agents")
    if not os.environ.get("OPENAI_BASE_URL"):
        raise RuntimeError("OPENAI_BASE_URL is required with --model-agents")
    if args.agent_timeout_seconds < 1:
        raise ValueError("agent timeout must be positive")
    if args.agent_request_timeout_seconds < 1:
        raise ValueError("agent request timeout must be positive")
    if args.agent_request_timeout_seconds > args.agent_timeout_seconds:
        raise ValueError("agent request timeout may not exceed the total agent timeout")
    if args.agent_empty_response_retries < 0:
        raise ValueError("agent empty-response retries may not be negative")
    if args.agent_transport_failure_retries < 0:
        raise ValueError("agent transport-failure retries may not be negative")
    if args.transport_retries < 0:
        raise ValueError("adapter transport retries may not be negative")


def _run_signoff(args: argparse.Namespace) -> int:
    """Record a human disposition and push the signed example into the extractor pool."""
    plane = FulltextControlPlane(args.database)
    task = plane.get_human_review_task(args.task_id)
    if task is None:
        print(json.dumps({"error": "task_not_found", "task_id": args.task_id}, indent=2))
        return 2
    if task["status"] != "pending":
        print(
            json.dumps(
                {"error": "task_not_pending", "task_id": args.task_id, "status": task["status"]},
                indent=2,
            )
        )
        return 2
    resolution = plane.resolve_human_review_task(
        args.task_id, disposition=args.disposition, note=args.note
    )
    store = SignedExampleStore(args.signed_examples_path)
    candidate = task["payload"].get("candidate") or task["payload"]
    store.record(
        document_id=task.get("document_id") or "",
        document_session_id=task["document_session_id"],
        candidate_id=task["candidate_id"],
        disposition=args.disposition,
        reason_codes=task.get("reason_codes"),
        payload={"candidate": candidate},
    )
    print(json.dumps({**resolution, "signed_examples_path": str(args.signed_examples_path)}, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "pdf_direct", False):
        if args.agent_protocol != "openai":
            raise ValueError("--pdf-direct currently requires --agent-protocol openai")
        os.environ["ECMONITOR_PDF_DIRECT"] = "1"
    if getattr(args, "no_agent_stream", False):
        os.environ["ECMONITOR_STREAM"] = "0"
    elif getattr(args, "agent_stream", False):
        os.environ["ECMONITOR_STREAM"] = "1"
    if getattr(args, "agent_ttft_seconds", None) is not None:
        os.environ["ECMONITOR_TTFT_TIMEOUT"] = str(args.agent_ttft_seconds)
    if getattr(args, "agent_stream_idle_seconds", None) is not None:
        os.environ["ECMONITOR_STREAM_IDLE_TIMEOUT"] = str(args.agent_stream_idle_seconds)
    if args.command == "status":
        print(json.dumps(FulltextControlPlane(args.database).status(), indent=2, sort_keys=True))
        return 0
    if args.command == "tools":
        print(json.dumps(tool_availability(), indent=2, sort_keys=True))
        return 0
    if args.command == "signoff":
        return _run_signoff(args)
    harness = _build_harness(args)
    if args.command == "inspect-document":
        report = harness.run_document(args.pdf)
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return 0
    runner: ParallelLibraryRunner | SequentialLibraryRunner
    if args.workers > 1:
        runner = ParallelLibraryRunner(
            harness,
            reports_jsonl=args.reports_jsonl,
            continue_on_error=not args.stop_on_error,
            max_document_attempts=args.document_max_attempts,
            retry_backoff_seconds=args.document_retry_backoff_seconds,
            max_workers=args.workers,
            concurrency_floor=args.concurrency_floor,
            concurrency_growth_streak=args.concurrency_growth_streak,
        )
    else:
        runner = SequentialLibraryRunner(
            harness,
            reports_jsonl=args.reports_jsonl,
            continue_on_error=not args.stop_on_error,
            max_document_attempts=args.document_max_attempts,
            retry_backoff_seconds=args.document_retry_backoff_seconds,
        )
    if args.command == "run-pilot":
        summary = runner.run_manifest(
            args.manifest, limit=args.limit, resume=not args.no_resume
        )
    else:
        summary = runner.run(args.pdf_dir, limit=args.limit, resume=not args.no_resume)
    print(json.dumps(summary.to_dict(), indent=2, sort_keys=True))
    return 0 if summary.failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
