"""Short-lived OpenAI-compatible model commands for full-text extraction.

The process reads one JSON request from stdin and writes one JSON response to stdout. It is
intentionally stateless: the existing JsonCommand adapters start this process afresh for every
extractor or validator request.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from ecmonitor.security import redact


class AgentCommandError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        category: str = "agent_error",
        retryable: bool = False,
        attempts: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.attempts = attempts or []


def _json_request_bytes(value: Any) -> bytes:
    """Serialize request JSON as UTF-8 without surrogate escape sequences."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


_VALIDATION_ACTIONS = {"accept", "retry", "escalate", "reject"}


def _scrub_json_surrogates(value: Any) -> Any:
    """Replace lone UTF-16 surrogates before sending JSON to strict gateways.

    Some PDF text extractors preserve malformed glyphs as lone surrogates. Python's
    JSON encoder can escape them, but NVIDIA's Rust request parser correctly rejects
    those escapes as invalid Unicode. This is a transport-only cleanup; it does not
    alter structured fields or decision semantics.
    """
    if isinstance(value, str):
        return value.encode("utf-8", "replace").decode("utf-8")
    if isinstance(value, list):
        return [_scrub_json_surrogates(item) for item in value]
    if isinstance(value, dict):
        return {key: _scrub_json_surrogates(item) for key, item in value.items()}
    return value

def _env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return max(0.5, float(value))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return max(0, int(value))
    except ValueError:
        return default


def main() -> int:
    args = _parse_args()
    request = _read_json_stdin()
    request_role = request.get("role")
    role = "extractor" if args.role == "extractor" else "validator"
    if args.role == "validator" and request_role == "evidence_validator_batch":
        role = "validator_batch"
    schema = _load_schema(args.schema)
    prompt = request.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise AgentCommandError("request.prompt must be a non-empty string")

    compact_extraction = role == "extractor" and args.extractor_output_mode == "shared_context"
    compact_validation_batch = role == "validator_batch"
    response_schema = _response_schema(
        role,
        schema,
        compact_extraction=compact_extraction,
        compact_validation_batch=compact_validation_batch,
    )
    final_response_schema = _response_schema(role, schema)
    system_prompt = _build_system_prompt(
        role=role,
        contract_prompt=prompt,
        response_schema=response_schema,
        compact_extraction=compact_extraction,
        compact_validation_batch=compact_validation_batch,
    )
    user_payload = dict(request)
    user_payload.pop("prompt", None)
    deadline = time.monotonic() + args.timeout_seconds
    models = _unique_models(args.model, args.fallback_model)
    try:
        raw_response, metadata = _call_model_resilient(
            system_prompt=system_prompt,
            user_payload=user_payload,
            models=models,
            base_url=args.base_url or os.environ.get("OPENAI_BASE_URL"),
            api_key=os.environ.get("OPENAI_API_KEY"),
            deadline=deadline,
            request_timeout_seconds=args.request_timeout_seconds,
            max_tokens=args.max_tokens,
            empty_response_retries=args.empty_response_retries,
            phase="initial",
            protocol=args.protocol,
            reasoning_effort=args.reasoning_effort,
            transport_failure_retries=args.transport_failure_retries,
        )
    except AgentCommandError as exc:
        _write_failure_audit(args.audit_dir, exc, request, models, role, phase="initial")
        raise
    try:
        parsed = _normalize_model_response(
            _parse_model_json(raw_response),
            role,
            compact_extraction=compact_extraction,
            request=request,
        )
        if compact_validation_batch:
            parsed = _coerce_compact_validation_response(parsed)
        errors = _validate_response(parsed, response_schema, role)
        if not errors and compact_extraction:
            parsed = _expand_shared_context_response(parsed, request)
            errors = _validate_response(parsed, final_response_schema, role)
        elif not errors and compact_validation_batch:
            parsed = _expand_compact_validation_response(parsed)
            errors = _validate_response(parsed, final_response_schema, role)
    except AgentCommandError as exc:
        parsed = None
        errors = [str(exc)]
    completion_errors = _completion_errors(metadata)
    if completion_errors:
        exc = AgentCommandError(
            completion_errors[0],
            category="model_response_invalid",
            retryable=True,
            attempts=list(metadata.get("attempts") or []),
        )
        _write_failure_audit(args.audit_dir, exc, request, models, role, phase="initial")
        raise exc
    if errors:
        repair_prompt = _build_repair_prompt(
            role=role,
            response_schema=response_schema,
            raw_response=raw_response,
            errors=errors,
        )
        try:
            repaired_raw, repair_metadata = _call_model_resilient(
                system_prompt=system_prompt + "\n\n" + repair_prompt,
                user_payload=user_payload,
                models=models,
                base_url=args.base_url or os.environ.get("OPENAI_BASE_URL"),
                api_key=os.environ.get("OPENAI_API_KEY"),
                deadline=deadline,
                request_timeout_seconds=args.request_timeout_seconds,
                max_tokens=args.max_tokens,
                empty_response_retries=args.empty_response_retries,
                phase="repair",
                protocol=args.protocol,
                reasoning_effort=args.reasoning_effort,
                transport_failure_retries=args.transport_failure_retries,
            )
        except AgentCommandError as exc:
            _write_failure_audit(args.audit_dir, exc, request, models, role, phase="repair")
            raise
        try:
            parsed = _normalize_model_response(
                _parse_model_json(repaired_raw),
                role,
                compact_extraction=compact_extraction,
                request=request,
            )
            if compact_validation_batch:
                parsed = _coerce_compact_validation_response(parsed)
            errors = _validate_response(parsed, response_schema, role)
            if not errors and compact_extraction:
                parsed = _expand_shared_context_response(parsed, request)
                errors = _validate_response(parsed, final_response_schema, role)
            elif not errors and compact_validation_batch:
                parsed = _expand_compact_validation_response(parsed)
                errors = _validate_response(parsed, final_response_schema, role)
        except AgentCommandError as exc:
            parsed = None
            errors = [str(exc)]
        errors = [*_completion_errors(repair_metadata), *errors]
        metadata["repair"] = repair_metadata
        if errors:
            metadata["requested_model"] = args.model
            metadata["role"] = role
            metadata["timestamp_utc"] = datetime.now(UTC).isoformat()
            metadata["validation_errors"] = errors[:20]
            _write_audit(args.audit_dir, metadata, request)
            raise AgentCommandError(
                "model response failed local validation after one repair: " + "; ".join(errors[:8])
            )

    metadata["requested_model"] = args.model
    metadata["role"] = role
    metadata["timestamp_utc"] = datetime.now(UTC).isoformat()
    _write_audit(args.audit_dir, metadata, request)
    # PDF text can contain lone UTF-16 surrogates; ASCII escaping keeps the JSON pipe safe.
    print(json.dumps(parsed, ensure_ascii=True, separators=(",", ":")))
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ECMonitor short-lived model command")
    parser.add_argument("--role", choices=("extractor", "validator"), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--base-url")
    parser.add_argument("--audit-dir", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--request-timeout-seconds", type=int, default=120)
    parser.add_argument("--empty-response-retries", type=int, default=1)
    parser.add_argument("--fallback-model", action="append", default=[])
    parser.add_argument("--max-tokens", type=int, default=12000)
    parser.add_argument(
        "--protocol",
        choices=("openai", "anthropic"),
        default=os.environ.get("ECMONITOR_PROTOCOL")
        or ("anthropic" if os.environ.get("ECMONITOR_ANTHROPIC_API") == "1" else "openai"),
        help=(
            "model API protocol: anthropic uses POST /v1/messages + thinking.disabled, which "
            "the deepseek-backed gateway honours (OpenAI /chat/completions ignores enable_thinking)"
        ),
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=("none", "minimal", "low", "medium", "high"),
        default=os.environ.get("ECMONITOR_REASONING_EFFORT", "low"),
        help="OpenAI-compatible reasoning effort; low is the fast extraction default",
    )
    parser.add_argument(
        "--transport-failure-retries",
        type=int,
        default=_env_int("ECMONITOR_AGENT_TRANSPORT_FAILURE_RETRIES", 0),
        help="retries inside this short-lived agent after a gateway/timeout failure",
    )
    parser.add_argument(
        "--extractor-output-mode",
        choices=("shared_context", "full"),
        default=os.environ.get("ECMONITOR_EXTRACTOR_OUTPUT_MODE", "shared_context"),
        help="shared_context emits table/document context once and expands rows locally",
    )
    return parser.parse_args()


def _read_json_stdin() -> dict[str, Any]:
    try:
        value = json.load(__import__("sys").stdin)
    except json.JSONDecodeError as exc:
        raise AgentCommandError(f"invalid JSON request: {exc}") from exc
    if not isinstance(value, dict):
        raise AgentCommandError("request must be a JSON object")
    return value


def _load_schema(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentCommandError(f"cannot load schema {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AgentCommandError("schema must be a JSON object")
    return value


def _validation_decision_schema(*, include_candidate_id: bool = False) -> dict[str, Any]:
    required = [
        "action",
        "reason_codes",
        "failed_json_pointers",
        "requested_context",
        "human_review_required",
    ]
    properties: dict[str, Any] = {
        "action": {"enum": sorted(_VALIDATION_ACTIONS)},
        "reason_codes": {"type": "array", "items": {"type": "string"}},
        "failed_json_pointers": {"type": "array", "items": {"type": "string"}},
        "requested_context": {"type": "array", "items": {"type": "string"}},
        "human_review_required": {"type": "boolean"},
    }
    if include_candidate_id:
        required.insert(0, "candidate_id")
        properties["candidate_id"] = {"type": "string", "minLength": 1}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


def _shared_context_extractor_schema() -> dict[str, Any]:
    """Small transport schema that groups statistics sharing one analyte/context."""
    nullable_string = {"type": ["string", "null"]}
    qualifier = {"enum": [
        "exact", "less_than", "greater_than", "not_detected",
        "not_quantified", "range", "unknown",
    ]}
    statistic = {"enum": [
        "single", "mean", "median", "minimum", "maximum", "range",
        "percentile", "frequency", "unknown",
    ]}
    kind = {"enum": ["individual", "transformation_product", "ambiguous", None]}
    context = {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "matrix", "evidence"],
        "properties": {
            "id": {"type": "string", "minLength": 1},
            "matrix": nullable_string,
            "site": nullable_string,
            "waterbody": nullable_string,
            "city": nullable_string,
            "admin1": nullable_string,
            "country": nullable_string,
            "time": nullable_string,
            "method": nullable_string,
            "page": {"type": ["integer", "string", "null"]},
            "table": nullable_string,
            "evidence": {"type": "string", "minLength": 1, "maxLength": 1200},
        },
    }
    measurement = {
        "type": "array",
        "minItems": 3,
        "maxItems": 3,
        "prefixItems": [nullable_string, qualifier, statistic],
        "items": False,
    }
    grouped_row = {
        "type": "array",
        "minItems": 4,
        "maxItems": 8,
        "prefixItems": [
            {"type": "string", "minLength": 1},
            {"type": "string", "minLength": 1},
            nullable_string,
            {"type": "array", "minItems": 1, "items": measurement},
            kind,
            nullable_string,
            nullable_string,
            nullable_string,
        ],
        "items": False,
    }
    legacy_row = {
        "type": "array",
        "minItems": 6,
        "maxItems": 11,
        "prefixItems": [
            {"type": "string", "minLength": 1},
            {"type": "string", "minLength": 1},
            nullable_string,
            nullable_string,
            qualifier,
            statistic,
            kind,
            nullable_string,
            {"type": ["string", "null"], "maxLength": 1200},
            nullable_string,
            nullable_string,
        ],
        "items": False,
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["contexts", "rows"],
        "properties": {
            "contexts": {"type": "array", "items": context},
            "rows": {"type": "array", "items": {"oneOf": [grouped_row, legacy_row]}},
        },
    }


def _compact_validation_batch_schema() -> dict[str, Any]:
    decision_prefix = [
            {"type": "string", "minLength": 1},
            {"enum": sorted(_VALIDATION_ACTIONS)},
            {"type": "array", "items": {"type": "string"}},
            {"type": "array", "items": {"type": "string"}},
            {"type": "array", "items": {"type": "string"}},
        ]
    decision = {
        "type": "array",
        "minItems": 6,
        "maxItems": 6,
        "prefixItems": [*decision_prefix, {"type": "boolean"}],
        "items": False,
    }
    decision_without_review_flag = {
        "type": "array",
        "minItems": 5,
        "maxItems": 5,
        "prefixItems": decision_prefix,
        "items": False,
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["decisions"],
        "properties": {"decisions": {"type": "array", "items": {"oneOf": [decision, decision_without_review_flag]}}},
    }


def _response_schema(
    role: str,
    occurrence_schema: dict[str, Any],
    *,
    compact_extraction: bool = False,
    compact_validation_batch: bool = False,
) -> dict[str, Any]:
    if role == "extractor" and compact_extraction:
        return _shared_context_extractor_schema()
    if role == "validator_batch" and compact_validation_batch:
        return _compact_validation_batch_schema()
    if role == "validator":
        return _validation_decision_schema()
    if role == "validator_batch":
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["decisions"],
            "properties": {
                "decisions": {
                    "type": "array",
                    "items": _validation_decision_schema(include_candidate_id=True),
                }
            },
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["candidates"],
        "properties": {
            "candidates": {
                "type": "array",
                "items": occurrence_schema,
            }
        },
    }


def _build_system_prompt(
    *,
    role: str,
    contract_prompt: str,
    response_schema: dict[str, Any],
    compact_extraction: bool = False,
    compact_validation_batch: bool = False,
) -> str:
    if role == "extractor" and compact_extraction:
        instruction = (
            "Return {contexts:[],rows:[]} using the supplied grouped shared-context schema. Define "
            "matrix/location/time/method/page/table/evidence once in contexts and reference it "
            "from every row. Each row position is: [context_id, raw_chemical_name, raw_unit, "
            "measurements, optional_kind, optional_parent, optional_row_label, "
            "optional_column_label]. measurements is one or more [raw_value, qualifier, statistic] "
            "triples for exactly one chemical and context. Do not repeat context or chemical fields "
            "for multiple statistics. kind is individual, transformation_product, ambiguous, or "
            "null. For A-B (C +/- D), put minimum A, maximum B, and mean C as three measurement "
            "triples in the same row; do not emit D as a concentration. Never combine multiple "
            "chemicals. The harness expands every measurement deterministically into one record."
        )
    elif role == "extractor":
        instruction = (
            "Return an object with candidates[]. Each candidate must match the compact model "
            "candidate JSON Schema. The harness will add document/session lineage and "
            "registry-backed identifiers later. Use null or explicit "
            "not_reported/unknown enum values when the evidence does not report a field. "
            "Do not invent identifiers, dates, sites, methods, or concentrations."
        )
    elif role == "validator_batch" and compact_validation_batch:
        instruction = (
            "Review every review_items entry independently against the shared chunk. Return "
            "decisions as arrays in the same order: [candidate_id, action, reason_codes, "
            "failed_json_pointers, requested_context, human_review_required]. Preserve every "
            "candidate_id exactly once. Do not rewrite candidates. Use retry only for bounded "
            "repairable extraction failures, reject unsupported/out-of-scope observations, and "
            "escalate genuine ambiguity."
        )
    elif role == "validator_batch":
        instruction = (
            "Review every item in review_items independently against the shared chunk. Return one "
            "decisions[] entry for every input candidate_id, preserving each candidate_id exactly "
            "once and in the same order. Do not rewrite candidates. Use retry for local repairable "
            "errors, reject for unsupported/non-field evidence, and escalate for ambiguity or "
            "unresolved identity. Do not let one candidate's evidence justify another candidate."
        )
    else:
        instruction = (
            "Return exactly the validator decision object matching the supplied JSON Schema. "
            "Do not rewrite the candidate. Use retry for local repairable errors, reject for "
            "unsupported/non-field evidence, and escalate for ambiguity or unresolved identity."
        )
    return (
        "You are a stateless scientific extraction service. This is one isolated request; do not "
        "use memory outside the JSON supplied in this request.\n\n"
        + contract_prompt
        + "\n\n"
        + instruction
        + "\n\nRESPONSE JSON SCHEMA:\n"
        + json.dumps(response_schema, ensure_ascii=False, separators=(",", ":"))
        + "\nReturn JSON only, with no Markdown fences or explanation."
    )


def _build_repair_prompt(
    *, role: str, response_schema: dict[str, Any], raw_response: str, errors: list[str]
) -> str:
    del role
    return (
        "Your previous response was invalid. Produce a corrected JSON response now. Preserve all "
        "supported facts and evidence, do not add unsupported facts, and output JSON only.\n"
        "Validation errors:\n"
        + "\n".join(f"- {item}" for item in errors[:20])
        + "\nSchema:\n"
        + json.dumps(response_schema, ensure_ascii=False, separators=(",", ":"))
        + "\nPrevious response:\n"
        + raw_response[:50000]
    )



def _unique_models(primary: str, fallbacks: list[str] | None = None) -> list[str]:
    models: list[str] = []
    for item in [primary, *(fallbacks or [])]:
        name = str(item or "").strip()
        if name and name not in models:
            models.append(name)
    if not models:
        raise AgentCommandError("at least one model must be configured")
    return models


def _remaining_seconds(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _sleep_until_deadline(seconds: float, deadline: float) -> None:
    delay = min(max(0.0, seconds), _remaining_seconds(deadline))
    if delay > 0:
        time.sleep(delay)


def _error_category(message: str) -> str:
    lowered = message.casefold()
    if (
        "empty message content" in lowered
        or "response has no choices" in lowered
        or "response has no content blocks" in lowered
    ):
        return "model_response_empty"
    if "read timed out" in lowered or "model transport failed" in lowered:
        return "model_transport_timeout"
    if "429" in lowered or "rate limit" in lowered:
        return "rate_limited"
    if "model gateway" in lowered:
        return "model_gateway_error"
    if "budget exhausted" in lowered or "deadline" in lowered:
        return "agent_deadline_exhausted"
    return "agent_error"


def _is_retryable_category(category: str) -> bool:
    return category in {
        "model_response_empty",
        "model_transport_timeout",
        "rate_limited",
        "model_gateway_error",
        "agent_deadline_exhausted",
    }


def _classify_exception(exc: Exception) -> AgentCommandError:
    if isinstance(exc, AgentCommandError):
        if exc.category == "agent_error":
            category = _error_category(str(exc))
            return AgentCommandError(
                str(exc),
                category=category,
                retryable=_is_retryable_category(category),
                attempts=exc.attempts,
            )
        return exc
    message = f"model transport failed: {exc}"
    category = _error_category(message)
    return AgentCommandError(
        message,
        category=category,
        retryable=_is_retryable_category(category),
    )


def _call_model_resilient(
    *,
    system_prompt: str,
    user_payload: dict[str, Any],
    models: list[str],
    base_url: str | None,
    api_key: str | None,
    deadline: float,
    request_timeout_seconds: int,
    max_tokens: int,
    empty_response_retries: int,
    phase: str,
    protocol: str = "openai",
    reasoning_effort: str = "low",
    transport_failure_retries: int = 0,
) -> tuple[str, dict[str, Any]]:
    if request_timeout_seconds < 1:
        raise AgentCommandError("request timeout must be positive")
    if empty_response_retries < 0:
        raise AgentCommandError("empty-response retries may not be negative")

    attempts: list[dict[str, Any]] = []
    last_error: AgentCommandError | None = None

    for model_index, model in enumerate(models):
        empty_failures = 0
        transport_failures = 0
        while True:
            remaining = _remaining_seconds(deadline)
            if remaining <= 1.0:
                last_error = AgentCommandError(
                    "model call budget exhausted before a usable response",
                    category="agent_deadline_exhausted",
                    retryable=True,
                    attempts=attempts,
                )
                break

            per_request_timeout = max(1, min(request_timeout_seconds, int(remaining)))
            attempt_started = time.monotonic()
            try:
                content, metadata = _call_model(
                    system_prompt=system_prompt,
                    user_payload=user_payload,
                    model=model,
                    base_url=base_url,
                    api_key=api_key,
                    timeout_seconds=per_request_timeout,
                    max_tokens=max_tokens,
                    deadline=deadline,
                    protocol=protocol,
                    reasoning_effort=reasoning_effort,
                )
            except Exception as exc:  # noqa: BLE001 - classify every transport/model failure
                error = _classify_exception(exc)
                attempt_record = {
                    "phase": phase,
                    "model": model,
                    "model_index": model_index,
                    "elapsed_seconds": round(time.monotonic() - attempt_started, 3),
                    "request_timeout_seconds": per_request_timeout,
                    "category": error.category,
                    "retryable": error.retryable,
                    "error": str(error)[:1000],
                }
                attempts.append(attempt_record)
                last_error = AgentCommandError(
                    str(error),
                    category=error.category,
                    retryable=error.retryable,
                    attempts=attempts,
                )
                if not error.retryable or _remaining_seconds(deadline) <= 1.0:
                    break
                if error.category == "model_response_empty":
                    empty_failures += 1
                    if empty_failures > empty_response_retries:
                        break
                    _sleep_until_deadline(1.0 * empty_failures, deadline)
                    continue
                transport_failures += 1
                if transport_failures > transport_failure_retries:
                    break
                _sleep_until_deadline(min(2.0 * transport_failures, 6.0), deadline)
                continue

            metadata = dict(metadata)
            metadata["phase"] = phase
            metadata["requested_model"] = model
            metadata["model_index"] = model_index
            metadata["attempt_count"] = len(attempts) + 1
            metadata["fallback_models"] = models[1:]
            metadata["attempts"] = attempts
            metadata["elapsed_seconds"] = round(time.monotonic() - attempt_started, 3)
            return content, metadata

        # Move to the next fallback model when the current model is exhausted.
        if model_index + 1 < len(models) and _remaining_seconds(deadline) > 1.0:
            _sleep_until_deadline(1.0, deadline)
            continue
        break

    if last_error is None:
        last_error = AgentCommandError(
            "model call failed without a usable response",
            category="agent_error",
            retryable=False,
            attempts=attempts,
        )
    raise AgentCommandError(
        str(last_error),
        category=last_error.category,
        retryable=last_error.retryable,
        attempts=attempts,
    )


def _call_model(
    *,
    system_prompt: str,
    user_payload: dict[str, Any],
    model: str,
    base_url: str | None,
    api_key: str | None,
    timeout_seconds: int,
    max_tokens: int,
    deadline: float | None = None,
    protocol: str | None = None,
    reasoning_effort: str = "low",
) -> tuple[str, dict[str, Any]]:
    if not base_url:
        raise AgentCommandError(
            "OPENAI_BASE_URL is not configured",
            category="agent_error",
            retryable=False,
        )
    if not api_key:
        raise AgentCommandError(
            "OPENAI_API_KEY is not configured",
            category="agent_error",
            retryable=False,
        )
    if timeout_seconds < 1:
        raise AgentCommandError(
            "timeout_seconds must be positive",
            category="agent_error",
            retryable=False,
        )
    protocol = (protocol or os.environ.get("ECMONITOR_PROTOCOL", "openai")).strip().casefold()
    if protocol not in {"openai", "anthropic"}:
        raise AgentCommandError(
            f"unsupported model protocol: {protocol!r}",
            category="agent_error",
            retryable=False,
        )

    pdf_path = _direct_pdf_path(user_payload)
    image_paths = _direct_image_paths(user_payload)
    if pdf_path is not None and protocol != "openai":
        raise AgentCommandError(
            "PDF direct transport currently requires the OpenAI-compatible protocol",
            category="agent_error",
            retryable=False,
        )
    payload_for_model = dict(user_payload)
    payload_for_model.pop("pdf_path", None)
    payload_for_model.pop("image_paths", None)
    if pdf_path is not None:
        chunk = payload_for_model.get("chunk")
        if isinstance(chunk, dict) and isinstance(chunk.get("text"), str):
            chunk = dict(chunk)
            chunk["text"] = "[omitted because complete PDF is attached]"
            payload_for_model["chunk"] = chunk
    payload_for_model = _scrub_json_surrogates(payload_for_model)
    # Send UTF-8 directly after scrubbing malformed surrogates.  Escaping all
    # non-ASCII characters would reintroduce surrogate escapes for astral glyphs,
    # which NVIDIA's strict JSON parser rejects.
    user_content = json.dumps(payload_for_model, ensure_ascii=False, separators=(",", ":"))
    if protocol == "anthropic":
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_content}],
        }
        # The deepseek-backed gateway ignores `enable_thinking` on /chat/completions but
        # honours thinking.disabled on the Anthropic /v1/messages shape. Override with
        # ECMONITOR_ENABLE_THINKING=1 to re-enable reasoning if a provider needs it.
        if os.environ.get("ECMONITOR_ENABLE_THINKING", "0") != "1":
            body["thinking"] = {"type": "disabled"}
        endpoint = base_url.rstrip("/") + "/messages"
        headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
    else:
        user_message_content: Any = user_content
        if pdf_path is not None:
            encoded_pdf = base64.b64encode(pdf_path.read_bytes()).decode("ascii")
            user_message_content = [
                {
                    "type": "text",
                    "text": (
                        "The attached PDF is the primary evidence for this scientific extraction/review request. "
                        "Use the local metadata and schema in the JSON text, but read the PDF directly.\n"
                        + user_content
                    ),
                },
                {
                    "type": "file",
                    "file": {
                        "filename": pdf_path.name,
                        "file_data": "data:application/pdf;base64," + encoded_pdf,
                    },
                },
            ]
        elif image_paths:
            user_message_content = [
                {
                    "type": "text",
                    "text": (
                        "The attached page images are primary visual evidence for this scientific "
                        "review request. Inspect table geometry, blank cells, captions, headers and "
                        "footnotes together with the JSON evidence.\n" + user_content
                    ),
                }
            ]
            for image_path in image_paths:
                mime = "image/jpeg" if image_path.suffix.casefold() in {".jpg", ".jpeg"} else "image/png"
                encoded_image = base64.b64encode(image_path.read_bytes()).decode("ascii")
                user_message_content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{encoded_image}"},
                    }
                )
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message_content},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": max_tokens,
            # Extraction and review are audit-sensitive structured tasks.  Keep the default
            # deterministic, while allowing a provider-specific override when a gateway does
            # not support temperature=0.
            "temperature": _env_float("ECMONITOR_TEMPERATURE", 0.0),
        }
        # The configured gateway backend is a reasoning model that can burn the whole token
        # budget on `reasoning_content` and return empty/truncated `content`. Disabling
        # thinking (when the gateway honours it) keeps the answer in `content` and is much
        # faster. Override with ECMONITOR_ENABLE_THINKING=1 if a provider needs it enabled.
        if os.environ.get("ECMONITOR_ENABLE_THINKING", "0") != "1" and "integrate.api.nvidia.com" not in base_url.casefold():
            body["enable_thinking"] = False
        # NVIDIA Nemotron exposes its thinking switch through the documented
        # chat-template extension rather than the generic OpenAI field.  Without
        # this provider-specific override it can spend most of the request budget
        # emitting reasoning_content even when the harness asks for a compact JSON
        # decision.  Keep this limited to NVIDIA's official endpoint.
        if "integrate.api.nvidia.com" in base_url.casefold() and os.environ.get("ECMONITOR_ENABLE_THINKING", "0") != "1":
            body["chat_template_kwargs"] = {"enable_thinking": False}
            body["reasoning_budget"] = 0
        if reasoning_effort and reasoning_effort != "none":
            body["reasoning_effort"] = reasoning_effort
        endpoint = base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
    transport = os.environ.get("ECMONITOR_HTTP_TRANSPORT", "requests").strip().casefold()
    body = _scrub_json_surrogates(body)
    # Streaming with a time-to-first-token (TTFT) + idle deadline turns gateway hangs
    # that previously burned 300s x N retries into ~1-2 min aborts, after which the
    # harness falls back to per-chunk mode. Disable via ECMONITOR_STREAM=0 if a
    # gateway misbehaves with stream=true.
    use_stream = _env_flag("ECMONITOR_STREAM", True)
    ttft_timeout = _env_float("ECMONITOR_TTFT_TIMEOUT", 60.0)
    stream_idle_timeout = _env_float("ECMONITOR_STREAM_IDLE_TIMEOUT", 90.0)
    if use_stream:
        body["stream"] = True
    effective_deadline = deadline if deadline is not None else time.monotonic() + timeout_seconds
    remaining = _remaining_seconds(effective_deadline)
    if remaining <= 0:
        raise AgentCommandError(
            "model call budget exhausted before request dispatch",
            category="agent_deadline_exhausted",
            retryable=True,
        )
    request_timeout = max(1, min(timeout_seconds, int(remaining)))
    if transport == "requests":
        response = _post_json_requests(
            endpoint,
            body,
            headers=headers,
            timeout_seconds=request_timeout,
            deadline=effective_deadline,
            stream=use_stream,
            ttft_timeout=ttft_timeout,
            idle_timeout=stream_idle_timeout,
        )
    elif transport == "curl":
        response = _post_json_curl(
            endpoint,
            body,
            headers=headers,
            timeout_seconds=request_timeout,
        )
    else:
        raise AgentCommandError(
            "ECMONITOR_HTTP_TRANSPORT must be 'requests' or 'curl'",
            category="agent_error",
            retryable=False,
        )
    if response.get("error"):
        raise AgentCommandError(
            f"model gateway error: {str(response['error'])[:2000]}",
            category="model_gateway_error",
            retryable=True,
        )
    if protocol == "anthropic":
        content, metadata = _parse_anthropic_response(response, endpoint=endpoint, transport=transport)
    else:
        content, metadata = _parse_openai_response(response, endpoint=endpoint, transport=transport)
    metadata["pdf_direct"] = pdf_path is not None
    if pdf_path is not None:
        metadata["pdf_filename"] = pdf_path.name
        metadata["pdf_bytes"] = pdf_path.stat().st_size
    metadata["image_direct"] = bool(image_paths)
    if image_paths:
        metadata["image_count"] = len(image_paths)
        metadata["image_bytes"] = sum(path.stat().st_size for path in image_paths)
    return content, metadata


def _direct_pdf_path(user_payload: dict[str, Any]) -> Path | None:
    if os.environ.get("ECMONITOR_PDF_DIRECT", "0").strip().casefold() not in {"1", "true", "yes", "on"}:
        return None
    request_role = user_payload.get("role")
    validator_pdf_enabled = os.environ.get("ECMONITOR_REVIEWER_PDF_DIRECT", "0").strip().casefold() in {
        "1", "true", "yes", "on"
    }
    if request_role != "occurrence_extractor" and not (
        validator_pdf_enabled and request_role in {"evidence_validator", "evidence_validator_batch"}
    ):
        return None
    raw_path = user_payload.get("pdf_path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        # The harness removes pdf_path for local text-only fallback chunks so the same PDF is
        # not uploaded again. Keep those fallback calls valid while requiring a path for the
        # primary merged-document direct-upload request.
        chunk = user_payload.get("chunk")
        chunk_type = chunk.get("chunk_type") if isinstance(chunk, dict) else None
        if chunk_type != "merged_document":
            return None
        raise AgentCommandError(
            "ECMONITOR_PDF_DIRECT=1 but request.pdf_path is missing",
            category="agent_error",
            retryable=False,
        )
    pdf_path = Path(raw_path).expanduser().resolve()
    if not pdf_path.is_file():
        raise AgentCommandError(
            f"direct PDF path is not a file: {pdf_path}",
            category="agent_error",
            retryable=False,
        )
    return pdf_path


def _direct_image_paths(user_payload: dict[str, Any]) -> list[Path]:
    """Resolve bounded local page images supplied by a validator request."""
    raw_paths = user_payload.get("image_paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        return []
    if user_payload.get("role") not in {"evidence_validator", "evidence_validator_batch"}:
        return []
    paths: list[Path] = []
    for raw_path in raw_paths[:8]:
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file() or path.suffix.casefold() not in {".png", ".jpg", ".jpeg"}:
            raise AgentCommandError(
                f"validator image path is invalid: {path}",
                category="agent_error",
                retryable=False,
            )
        paths.append(path)
    return paths


def _parse_openai_response(
    response: dict[str, Any], *, endpoint: str, transport: str
) -> tuple[str, dict[str, Any]]:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AgentCommandError(
            "model gateway response has no choices",
            category="model_response_empty",
            retryable=True,
        )
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    reasoning = message.get("reasoning_content") if isinstance(message, dict) else None
    used_reasoning_fallback = False
    if not isinstance(content, str) or not content.strip():
        # Reasoning-model gateways may return the answer in `reasoning_content` while
        # `content` stays empty (often after `finish_reason=length`). Try to salvage the
        # answer before declaring an empty response; parsing still validates it later.
        if isinstance(reasoning, str) and reasoning.strip():
            content = reasoning
            used_reasoning_fallback = True
        else:
            raise AgentCommandError(
                "model gateway returned empty message content",
                category="model_response_empty",
                retryable=True,
            )
    metadata = {
        "protocol": "openai",
        "endpoint": endpoint,
        "transport": transport,
        "response_model": response.get("model"),
        "finish_reason": choices[0].get("finish_reason") if isinstance(choices[0], dict) else None,
        "content_length": len(content),
        "reasoning_content_length": len(reasoning) if isinstance(reasoning, str) else 0,
        "used_reasoning_content_fallback": used_reasoning_fallback,
        "usage": response.get("usage"),
        "request_id": response.get("id"),
    }
    stream_meta = response.get("ecmonitor_stream") or {}
    metadata["stream"] = stream_meta.get("stream")
    metadata["ttft_seconds"] = stream_meta.get("ttft_seconds")
    return content, metadata


def _parse_anthropic_response(
    response: dict[str, Any], *, endpoint: str, transport: str
) -> tuple[str, dict[str, Any]]:
    content_blocks = response.get("content")
    if not isinstance(content_blocks, list):
        raise AgentCommandError(
            "model gateway response has no content blocks",
            category="model_response_empty",
            retryable=True,
        )
    text_parts: list[str] = []
    reasoning_chars = 0
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text" and isinstance(block.get("text"), str):
            text_parts.append(block["text"])
        elif block_type in {"thinking", "redacted_thinking"} and isinstance(
            block.get("thinking"), str
        ):
            reasoning_chars += len(block["thinking"])
    content = "".join(text_parts)
    if not content.strip():
        raise AgentCommandError(
            "model gateway returned empty message content",
            category="model_response_empty",
            retryable=True,
        )
    stop_reason = response.get("stop_reason")
    finish_reason = "length" if stop_reason == "max_tokens" else (stop_reason or None)
    metadata = {
        "protocol": "anthropic",
        "endpoint": endpoint,
        "transport": transport,
        "response_model": response.get("model"),
        "finish_reason": finish_reason,
        "content_length": len(content),
        "reasoning_content_length": reasoning_chars,
        "used_reasoning_content_fallback": False,
        "usage": response.get("usage"),
        "request_id": response.get("id"),
    }
    stream_meta = response.get("ecmonitor_stream") or {}
    metadata["stream"] = stream_meta.get("stream")
    metadata["ttft_seconds"] = stream_meta.get("ttft_seconds")
    return content, metadata



class _StreamNotSupported(RuntimeError):
    """Raised when a gateway rejects stream=true so the request can retry non-streaming."""


def _post_json_requests(
    endpoint: str,
    body: dict[str, Any],
    *,
    headers: dict[str, str],
    timeout_seconds: int,
    deadline: float | None = None,
    stream: bool = True,
    ttft_timeout: float = 60.0,
    idle_timeout: float = 90.0,
) -> dict[str, Any]:
    """POST one JSON body and parse the model response, streaming first.

    ``stream=True`` requests an SSE stream and aborts when no first token arrives within
    ``ttft_timeout`` seconds or when the stream idles for ``idle_timeout`` seconds. This
    converts gateway hangs that previously burned 300s x N retries into ~1-2 min aborts so
    the harness can fall back to per-chunk extraction quickly. If the gateway ignores or
    rejects stream=true, the same attempt retries non-streaming automatically.
    """
    try:
        import requests
    except ImportError as exc:  # pragma: no cover
        raise AgentCommandError(
            "requests is required for the default model transport",
            category="agent_error",
            retryable=False,
        ) from exc

    effective_deadline = deadline if deadline is not None else time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    for attempt in range(1, 4):
        remaining = _remaining_seconds(effective_deadline)
        if remaining <= 0:
            break
        try:
            if stream:
                try:
                    return _post_json_streaming(
                        endpoint,
                        body,
                        headers=headers,
                        timeout_seconds=timeout_seconds,
                        deadline=effective_deadline,
                        ttft_timeout=ttft_timeout,
                        idle_timeout=idle_timeout,
                    )
                except _StreamNotSupported:
                    # The gateway rejected stream=true; strip it before retrying so the
                    # plain POST in this same attempt is a non-streaming request.
                    body.pop("stream", None)
                    stream = False
                    continue
            result = requests.post(
                endpoint,
                headers=headers,
                data=_json_request_bytes(body),
                timeout=(min(30.0, remaining), min(float(timeout_seconds), remaining)),
            )
            if (result.status_code == 429 or result.status_code >= 500) and attempt < 3:
                _sleep_until_deadline(min(2.0 * attempt, remaining), effective_deadline)
                continue
            result.raise_for_status()
            response = result.json()
            if not isinstance(response, dict):
                raise AgentCommandError(
                    "model gateway response must be an object",
                    category="model_gateway_error",
                    retryable=True,
                )
            return response
        except AgentCommandError as exc:
            if exc.retryable and attempt < 3 and _remaining_seconds(effective_deadline) > 0:
                last_error = exc
                _sleep_until_deadline(min(2.0 * attempt, _remaining_seconds(effective_deadline)), effective_deadline)
                continue
            raise
        except requests.RequestException as exc:
            detail = ""
            response = getattr(exc, "response", None)
            if response is not None:
                detail = ": " + (getattr(response, "text", "") or "")[:1000]
            last_error = RuntimeError(str(exc) + detail)
            if attempt < 3 and _remaining_seconds(effective_deadline) > 0:
                _sleep_until_deadline(min(2.0 * attempt, _remaining_seconds(effective_deadline)), effective_deadline)
                continue
        except ValueError as exc:
            raise AgentCommandError(
                "model gateway returned invalid JSON",
                category="model_gateway_error",
                retryable=True,
            ) from exc
    if isinstance(last_error, AgentCommandError):
        raise last_error
    raise AgentCommandError(
        f"model transport failed: {last_error}",
        category="model_transport_timeout",
        retryable=True,
    )


def _post_json_streaming(
    endpoint: str,
    body: dict[str, Any],
    *,
    headers: dict[str, str],
    timeout_seconds: int,
    deadline: float | None,
    ttft_timeout: float,
    idle_timeout: float,
) -> dict[str, Any]:
    import requests

    started = time.monotonic()
    try:
        result = requests.post(
            endpoint,
            headers=headers,
            data=_json_request_bytes(body),
            stream=True,
            timeout=(min(30.0, timeout_seconds), timeout_seconds),
        )
    except requests.RequestException as exc:
        raise AgentCommandError(
            f"model transport failed: {exc}",
            category="model_transport_timeout",
            retryable=True,
        ) from exc
    content_type = (result.headers.get("Content-Type") or "").casefold()
    with result:
        if result.status_code == 429 or result.status_code >= 500:
            raise requests.HTTPError(f"{result.status_code} Server Error", response=result)
        if "text/event-stream" not in content_type:
            # Gateway ignored stream=true; parse the full JSON body non-streaming.
            if result.status_code >= 400:
                if result.status_code in {400, 404, 422}:
                    raise _StreamNotSupported(
                        f"gateway rejected stream (status {result.status_code}): "
                        + (result.text or "")[:1000]
                    )
                raise requests.HTTPError(f"{result.status_code} error", response=result)
            result.raise_for_status()
            response = result.json()
            if not isinstance(response, dict):
                raise AgentCommandError(
                    "model gateway response must be an object",
                    category="model_gateway_error",
                    retryable=True,
                )
            response["ecmonitor_stream"] = {"stream": "non_sse", "ttft_seconds": None}
            return response
        if result.status_code >= 400:
            raise requests.HTTPError(f"{result.status_code} error", response=result)
        result.raise_for_status()
        return _consume_sse(
            result,
            endpoint=endpoint,
            started=started,
            deadline=deadline,
            ttft_timeout=ttft_timeout,
            idle_timeout=idle_timeout,
        )


def _consume_sse(
    response: Any,
    *,
    endpoint: str,
    started: float,
    deadline: float | None,
    ttft_timeout: float,
    idle_timeout: float,
) -> dict[str, Any]:
    """Consume one SSE body and assemble a response dict equivalent to non-streaming JSON."""
    protocol = "anthropic" if endpoint.rstrip("/").endswith("/messages") else "openai"
    first_at: float | None = None
    last_at = time.monotonic()
    event_name: str | None = None
    data_lines: list[str] = []
    openai_state: dict[str, Any] = {"content": [], "reasoning": [], "finish_reason": None,
                                    "model": None, "id": None, "usage": None}
    anthropic_state: dict[str, Any] = {"text": [], "model": None, "id": None,
                                       "usage": None, "stop_reason": None}

    def flush() -> None:
        nonlocal event_name, data_lines
        if not data_lines:
            return
        payload = "".join(data_lines)
        data_lines = []
        event_name = None
        if payload.strip() == "[DONE]":
            return
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            return
        if not isinstance(obj, dict):
            return
        if protocol == "openai":
            _fold_openai_event(obj, openai_state)
        else:
            _fold_anthropic_event(obj, anthropic_state)

    for raw_line in response.iter_lines(decode_unicode=True):
        if deadline is not None and _remaining_seconds(deadline) <= 0:
            raise AgentCommandError(
                "stream deadline exhausted before completion",
                category="model_transport_timeout",
                retryable=True,
            )
        now = time.monotonic()
        if first_at is None and raw_line is not None:
            first_at = now
            if now - started > ttft_timeout:
                raise AgentCommandError(
                    f"no first token within {ttft_timeout:.0f}s",
                    category="model_transport_timeout",
                    retryable=True,
                )
        if raw_line is not None:
            if now - last_at > idle_timeout:
                raise AgentCommandError(
                    f"stream idle for {idle_timeout:.0f}s",
                    category="model_transport_timeout",
                    retryable=True,
                )
            last_at = now
            line = raw_line.strip()
            if not line:
                flush()
                continue
            if line.startswith("event:"):
                event_name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
    flush()

    stream_meta = {
        "stream": "sse",
        "ttft_seconds": round(first_at - started, 3) if first_at is not None else None,
    }
    if protocol == "openai":
        content = "".join(openai_state["content"])
        reasoning = "".join(openai_state["reasoning"])
        response = {
            "id": openai_state["id"],
            "model": openai_state["model"],
            "choices": [
                {
                    "message": {"content": content, "reasoning_content": reasoning or None},
                    "finish_reason": openai_state["finish_reason"],
                }
            ],
            "usage": openai_state["usage"],
            "ecmonitor_stream": stream_meta,
        }
        return response
    text = "".join(anthropic_state["text"])
    response = {
        "id": anthropic_state["id"],
        "model": anthropic_state["model"],
        "content": [{"type": "text", "text": text}] if text else [],
        "stop_reason": anthropic_state["stop_reason"],
        "usage": anthropic_state["usage"],
        "ecmonitor_stream": stream_meta,
    }
    return response


def _fold_openai_event(obj: dict[str, Any], state: dict[str, Any]) -> None:
    if obj.get("id"):
        state["id"] = obj["id"]
    if obj.get("model"):
        state["model"] = obj["model"]
    if obj.get("usage"):
        state["usage"] = obj["usage"]
    choices = obj.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        choice = choices[0]
        delta = choice.get("delta")
        if isinstance(delta, dict):
            content = delta.get("content")
            if isinstance(content, str):
                state["content"].append(content)
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str):
                state["reasoning"].append(reasoning)
        if choice.get("finish_reason"):
            state["finish_reason"] = choice["finish_reason"]


def _fold_anthropic_event(obj: dict[str, Any], state: dict[str, Any]) -> None:
    event_type = obj.get("type")
    if event_type == "error":
        error = obj.get("error") or {}
        raise AgentCommandError(
            f"model gateway error: {str(error)[:2000]}",
            category="model_gateway_error",
            retryable=True,
        )
    if event_type == "message_start":
        message = obj.get("message")
        if isinstance(message, dict):
            if message.get("id"):
                state["id"] = message["id"]
            if message.get("model"):
                state["model"] = message["model"]
            if message.get("usage"):
                state["usage"] = message["usage"]
        return
    if event_type == "content_block_delta":
        delta = obj.get("delta")
        if isinstance(delta, dict):
            text = delta.get("text")
            if isinstance(text, str):
                state["text"].append(text)
        return
    if event_type == "message_delta":
        delta = obj.get("delta")
        if isinstance(delta, dict) and delta.get("stop_reason"):
            state["stop_reason"] = delta["stop_reason"]
        usage = obj.get("usage")
        if usage:
            state["usage"] = usage
        return


def _post_json_curl(
    endpoint: str,
    body: dict[str, Any],
    *,
    headers: dict[str, str],
    timeout_seconds: int,
) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
        request_path = Path(handle.name)
        # Escaping non-ASCII also prevents malformed PDF text from breaking UTF-8 writes.
        json.dump(body, handle, ensure_ascii=True, separators=(",", ":"))
    try:
        result = subprocess.run(
            [
                os.environ.get("ECMONITOR_CURL", r"C:\Windows\System32\curl.exe"),
                "-4",
                "-sS",
                "--http1.1",
                "--tlsv1.2",
                "--retry",
                "2",
                "--retry-all-errors",
                "--retry-delay",
                "2",
                "--connect-timeout",
                "30",
                "--max-time",
                str(timeout_seconds),
                *(
                    part
                    for key, value in headers.items()
                    for part in ("-H", f"{key}: {value}")
                ),
                "--data-binary",
                f"@{request_path}",
                endpoint,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    finally:
        request_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise AgentCommandError(
            f"model transport failed: {result.stderr[-2000:]}",
            category="model_transport_timeout",
            retryable=True,
        )
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AgentCommandError(
            "model gateway returned invalid JSON",
            category="model_gateway_error",
            retryable=True,
        ) from exc
    if not isinstance(response, dict):
        raise AgentCommandError(
            "model gateway response must be an object",
            category="model_gateway_error",
            retryable=True,
        )
    return response


def _write_failure_audit(
    audit_dir: Path | None,
    exc: AgentCommandError,
    request: dict[str, Any],
    models: list[str],
    role: str,
    *,
    phase: str,
) -> None:
    metadata = {
        "status": "failed",
        "phase": phase,
        "role": role,
        "category": exc.category,
        "retryable": exc.retryable,
        "error": str(exc)[:2000],
        "models": models,
        "attempts": exc.attempts[:20],
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "finish_reason": "error",
    }
    _write_audit(audit_dir, metadata, request)



def _completion_errors(metadata: Mapping[str, Any]) -> list[str]:
    if metadata.get("finish_reason") == "length":
        return ["model response was truncated (finish_reason=length)"]
    return []


def _expand_compact_validation_response(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("decisions"), list):
        raise AgentCommandError("compact validator response requires decisions[]")
    decisions: list[dict[str, Any]] = []
    for index, row in enumerate(value["decisions"], start=1):
        if not isinstance(row, list) or len(row) not in {5, 6}:
            raise AgentCommandError(
                f"compact validator decision {index} must contain five or six items"
            )
        decisions.append(
            {
                "candidate_id": str(row[0]),
                "action": row[1],
                "reason_codes": row[2],
                "failed_json_pointers": row[3],
                "requested_context": row[4],
                "human_review_required": row[5] if len(row) == 6 else row[1] in {"retry", "escalate"} or bool(row[4]),
            }
        )
    return {"decisions": decisions}


def _coerce_compact_validation_response(value: Any) -> Any:
    """Normalize common provider variants before applying the strict transport schema.

    NVIDIA models occasionally serialize the three list fields as strings (for
    example ``"[table_cell_binding_error]"``) or append one explanatory field
    to the six-element compact tuple.  These are transport-shape variants, not
    scientific decisions; normalize them locally while preserving every
    candidate id, action and reason.
    """
    if not isinstance(value, dict) or not isinstance(value.get("decisions"), list):
        return value

    def as_list(item: Any) -> Any:
        if isinstance(item, list):
            return item
        if item is None:
            return []
        if isinstance(item, str):
            text = item.strip()
            if not text:
                return []
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                pass
            if text.startswith("[") and text.endswith("]"):
                text = text[1:-1].strip()
            return [part.strip().strip("'\"") for part in text.split(",") if part.strip()]
        return [str(item)]

    normalized: list[Any] = []
    for row in value["decisions"]:
        if not isinstance(row, list) or len(row) < 5:
            normalized.append(row)
            continue
        # The contract is [id, action, reasons, failed_pointers,
        # requested_context, human_review_required].  If a provider appends
        # prose/status fields, retain the final boolean when present and drop
        # only those non-contract extras.
        human = None
        for item in reversed(row[5:]):
            if isinstance(item, bool):
                human = item
                break
            if isinstance(item, str) and item.strip().casefold() in {"true", "false"}:
                human = item.strip().casefold() == "true"
                break
        if human is None:
            human = str(row[1]).casefold() in {"retry", "escalate"} or bool(as_list(row[4]))
        normalized.append([
            row[0], row[1], as_list(row[2]), as_list(row[3]), as_list(row[4]), human
        ])
    return {"decisions": normalized}


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _page_number(value: Any, fallback: Any = None) -> int | None:
    for candidate in (value, fallback):
        if isinstance(candidate, int) and candidate >= 1:
            return candidate
        if isinstance(candidate, str):
            match = re.search(r"\d+", candidate)
            if match and int(match.group()) >= 1:
                return int(match.group())
    return None


def _expand_shared_context_response(
    value: Any, request: Mapping[str, Any]
) -> dict[str, Any]:
    """Expand grouped or legacy compact rows into the stable candidate schema locally."""
    if not isinstance(value, dict):
        raise AgentCommandError("compact extractor response must be an object")
    raw_contexts = value.get("contexts")
    rows = value.get("rows")
    if not isinstance(raw_contexts, list) or not isinstance(rows, list):
        raise AgentCommandError("compact extractor response requires contexts[] and rows[]")

    contexts: dict[str, dict[str, Any]] = {}
    for context in raw_contexts:
        if not isinstance(context, dict):
            raise AgentCommandError("compact context must be an object")
        context_id = _optional_text(context.get("id"))
        if context_id is None:
            raise AgentCommandError("compact context id is missing")
        if context_id in contexts:
            raise AgentCommandError(f"duplicate compact context id: {context_id}")
        contexts[context_id] = context

    chunk = request.get("chunk") if isinstance(request, Mapping) else None
    chunk = chunk if isinstance(chunk, Mapping) else {}
    chunk_id = _optional_text(chunk.get("chunk_id"))
    fallback_page_start = chunk.get("page_start")
    fallback_page_end = chunk.get("page_end")
    qualifier_aliases = {"<": "less_than", ">": "greater_than", "nd": "not_detected"}
    statistic_aliases = {"min": "minimum", "max": "maximum", "avg": "mean", "average": "mean"}
    candidates: list[dict[str, Any]] = []

    def append_candidate(
        *,
        context: Mapping[str, Any],
        raw_name: str,
        raw_value: str | None,
        raw_unit: str | None,
        qualifier: Any,
        statistic: Any,
        kind: str | None,
        parent: str | None,
        quote_override: str | None,
        row_label: str | None,
        column_label: str | None,
    ) -> None:
        normalized_qualifier = qualifier_aliases.get(
            str(qualifier).strip().casefold(), str(qualifier)
        )
        normalized_statistic = statistic_aliases.get(
            str(statistic).strip().casefold(), str(statistic)
        )
        evidence_quote = quote_override or _optional_text(context.get("evidence"))
        if evidence_quote is None:
            raise AgentCommandError("compact row has no evidence quote")
        specificity = "ambiguous" if kind == "ambiguous" else "individual_chemical"
        is_individual = kind != "ambiguous"
        page = _page_number(context.get("page"), fallback_page_start)
        page_end = page or _page_number(None, fallback_page_end)
        candidate_id = f"compact-{len(candidates) + 1}"
        candidates.append(
            {
                "candidate_id": candidate_id,
                "observation_type": "field_measurement",
                "analyte": {
                    "raw_name": raw_name,
                    "reported_name": raw_name,
                    "specificity_status": specificity,
                    "transformation_product_of": (
                        parent if kind == "transformation_product" else None
                    ),
                    "is_individual_chemical": is_individual,
                },
                "result": {
                    "raw_value": raw_value,
                    "raw_unit": raw_unit,
                    "qualifier": normalized_qualifier,
                    "statistic": normalized_statistic,
                },
                "sample": {"matrix_raw": _optional_text(context.get("matrix"))},
                "location": {
                    "site_name": _optional_text(context.get("site")),
                    "waterbody": _optional_text(context.get("waterbody")),
                    "city": _optional_text(context.get("city")),
                    "admin1": _optional_text(context.get("admin1")),
                    "country": _optional_text(context.get("country")),
                },
                "sampling_time": {
                    "raw_text": _optional_text(context.get("time")),
                    "basis": "reported" if _optional_text(context.get("time")) else "unknown",
                },
                "analytical_method": {"method_name": _optional_text(context.get("method"))},
                "evidence": {
                    "quote": evidence_quote[:1200],
                    "chunk_id": chunk_id,
                    "page_start": page,
                    "page_end": page_end,
                    "table_caption": _optional_text(context.get("table")),
                    "row_label": row_label or raw_name,
                    "column_label": column_label,
                },
            }
        )

    for row_index, row in enumerate(rows, start=1):
        if not isinstance(row, list) or len(row) < 4:
            raise AgentCommandError(f"compact row {row_index} must contain at least four items")
        context_id = _optional_text(row[0])
        context = contexts.get(context_id or "")
        if context is None:
            raise AgentCommandError(
                f"compact row {row_index} references unknown context {context_id!r}"
            )
        raw_name = _optional_text(row[1])
        if raw_name is None:
            raise AgentCommandError(f"compact row {row_index} has no chemical name")

        if isinstance(row[3], list):
            raw_unit = _optional_text(row[2])
            measurements = row[3]
            kind = _optional_text(row[4]) if len(row) > 4 else "individual"
            parent = _optional_text(row[5]) if len(row) > 5 else None
            row_label = _optional_text(row[6]) if len(row) > 6 else None
            column_label = _optional_text(row[7]) if len(row) > 7 else None
            if not measurements:
                raise AgentCommandError(f"compact grouped row {row_index} has no measurements")
            for measurement_index, measurement in enumerate(measurements, start=1):
                if not isinstance(measurement, list) or len(measurement) != 3:
                    raise AgentCommandError(
                        f"compact grouped row {row_index} measurement {measurement_index} "
                        "must contain exactly three items"
                    )
                append_candidate(
                    context=context,
                    raw_name=raw_name,
                    raw_value=_optional_text(measurement[0]),
                    raw_unit=raw_unit,
                    qualifier=measurement[1],
                    statistic=measurement[2],
                    kind=kind,
                    parent=parent,
                    quote_override=None,
                    row_label=row_label,
                    column_label=column_label,
                )
            continue

        if len(row) < 6:
            raise AgentCommandError(f"legacy compact row {row_index} must contain six items")
        append_candidate(
            context=context,
            raw_name=raw_name,
            raw_value=_optional_text(row[2]),
            raw_unit=_optional_text(row[3]),
            qualifier=row[4],
            statistic=row[5],
            kind=_optional_text(row[6]) if len(row) > 6 else "individual",
            parent=_optional_text(row[7]) if len(row) > 7 else None,
            quote_override=_optional_text(row[8]) if len(row) > 8 else None,
            row_label=_optional_text(row[9]) if len(row) > 9 else None,
            column_label=_optional_text(row[10]) if len(row) > 10 else None,
        )
    return {"candidates": candidates}


def _normalize_model_response(
    value: Any,
    role: str,
    *,
    compact_extraction: bool = False,
    request: Mapping[str, Any] | None = None,
) -> Any:
    """Accept the common direct-single-candidate form from extractor models.

    The transport contract is an object with ``candidates[]``. Some otherwise valid model
    responses emit one candidate object directly; wrapping it is deterministic and does not
    relax the candidate schema or invent any fields.
    """
    # A model may correctly find no eligible observation in a chunk but emit the empty JSON
    # array ``[]`` instead of the object required by the transport contract. Coerce only this
    # exact empty value. Non-empty arrays and malformed text remain validation/parse failures,
    # so a model failure cannot silently become a zero-record paper.
    if role == "extractor" and isinstance(value, list) and not value:
        return {"contexts": [], "rows": []} if compact_extraction else {"candidates": []}
    if role == "extractor" and compact_extraction:
        # Some gateways follow the grouped-row contract but omit the extra nesting
        # around a single measurement, returning e.g. ["0.005", "exact", "mean"]
        # where measurements must be [["0.005", "exact", "mean"]].  This is a
        # lossless structural repair: it does not add a chemical, value, or context;
        # the normal compact schema and final candidate schema are still validated
        # immediately afterwards.
        value = _normalize_flat_single_measurements(value)
        # A few gateway/model combinations still emit the pre-v1 compact contract as
        # ``{"candidates": [[matrix, name, unit, value, qualifier, statistic, kind, ...]]}``
        # (or the bare list of rows) despite receiving the shared-context schema.  Recover
        # only this tightly-recognisable shape into the current transport contract.  The
        # harness still performs the normal candidate-schema, policy, and evidence review;
        # missing context fields remain null rather than being invented.
        legacy_rows = _legacy_compact_rows(value)
        if legacy_rows is not None:
            return _legacy_rows_to_shared_context(legacy_rows, request or {})
    if role == "validator_batch":
        # Some OpenAI-compatible endpoints ignore the compact array instruction and
        # return the equivalent verbose decision objects.  Convert only the six
        # transport fields that the compact contract already carries; extra prose or
        # provider-specific fields are discarded, while action/reason/evidence values
        # are left unchanged and still pass through both JSON-schema validations.
        if isinstance(value, dict) and isinstance(value.get("decisions"), list):
            compact_rows: list[list[Any]] = []
            verbose_rows = value["decisions"]
            for row in verbose_rows:
                if isinstance(row, list) and 5 <= len(row) <= 7:
                    source_row = list(row)

                    def flat_strings(item: Any) -> list[str] | None:
                        if not isinstance(item, list):
                            return None
                        values: list[str] = []
                        for part in item:
                            if isinstance(part, str):
                                values.append(part)
                            elif isinstance(part, list) and all(isinstance(x, str) for x in part):
                                values.extend(part)
                            else:
                                return None
                        return values

                    reasons = flat_strings(source_row[2])
                    pointers = flat_strings(source_row[3])
                    # A common five-item variant omits requested_context and places
                    # the human-review flag in position five.
                    if len(source_row) == 5 and (
                        isinstance(source_row[4], bool)
                        or (isinstance(source_row[4], str) and source_row[4].strip().casefold() in {"true", "false"})
                    ):
                        requested = []
                        review_required = source_row[4]
                    elif len(source_row) == 5 and isinstance(source_row[4], str):
                        requested = [source_row[4]]
                        review_required = source_row[1] in {"retry", "escalate"}
                    elif len(source_row) >= 6 and (
                        isinstance(source_row[4], bool)
                        or (isinstance(source_row[4], str) and source_row[4].strip().casefold() in {"true", "false"})
                    ):
                        # Another provider variant places the review flag at
                        # position five and appends a second status flag.
                        requested = []
                        review_required = source_row[4]
                    else:
                        requested = flat_strings(source_row[4])
                        review_required = source_row[5] if len(source_row) >= 6 else None
                    if isinstance(review_required, str) and review_required.strip().casefold() in {"true", "false"}:
                        review_required = review_required.strip().casefold() == "true"
                    if source_row[1] in {"retry", "escalate"}:
                        review_required = True
                    elif not isinstance(review_required, bool):
                        review_required = bool(requested)
                    normalized_row = [source_row[0], source_row[1], reasons, pointers, requested, review_required]
                    if _is_compact_validation_row(normalized_row):
                        compact_rows.append(normalized_row)
                        continue
                    compact_rows = []
                    break
                if not isinstance(row, dict):
                    compact_rows = []
                    break
                candidate_id = row.get("candidate_id")
                action = row.get("action")
                reason_codes = row.get("reason_codes")
                failed_pointers = row.get("failed_json_pointers")
                requested_context = row.get("requested_context")
                if (
                    not isinstance(candidate_id, str)
                    or action not in _VALIDATION_ACTIONS
                    or not isinstance(reason_codes, list)
                    or not all(isinstance(item, str) for item in reason_codes)
                    or not isinstance(failed_pointers, list)
                    or not all(isinstance(item, str) for item in failed_pointers)
                    or not isinstance(requested_context, list)
                    or not all(isinstance(item, str) for item in requested_context)
                ):
                    compact_rows = []
                    break
                review_required = row.get("human_review_required")
                if not isinstance(review_required, bool):
                    if isinstance(review_required, str) and review_required.strip().casefold() in {"true", "false"}:
                        review_required = review_required.strip().casefold() == "true"
                    else:
                        review_required = action in {"retry", "escalate"} or bool(requested_context)
                compact_rows.append(
                    [candidate_id, action, reason_codes, failed_pointers, requested_context, review_required]
                )
            if compact_rows or not verbose_rows:
                return {"decisions": compact_rows}
        # Some gateways return the compact decision array directly (or return a list of
        # compact decision arrays) instead of wrapping it in ``{"decisions": [...]}``.
        # Recover only rows that satisfy the complete six-position transport contract;
        # an arbitrary string/list is deliberately left invalid and cannot become an
        # implicit accept/reject decision.
        review_rows = _legacy_compact_validation_rows(value)
        if review_rows is not None:
            return {"decisions": review_rows}
    if (
        role == "extractor"
        and isinstance(value, dict)
        and "candidates" not in value
        and "analyte" in value
        and "evidence" in value
    ):
        return {"candidates": [value]}
    return value


def _normalize_flat_single_measurements(value: Any) -> Any:
    """Wrap a lone grouped measurement triple in the required measurements list.

    The shared-context contract uses ``measurements: [[value, qualifier, statistic]]``.
    Small/fast model variants occasionally emit ``measurements: [value, qualifier,
    statistic]`` when only one statistic is present.  Restrict the repair to a response
    that already has the expected contexts/rows container and to the two enum positions;
    arbitrary arrays are intentionally left untouched so malformed model output cannot
    be silently interpreted as a valid observation.
    """
    if not isinstance(value, dict):
        return value
    contexts = value.get("contexts")
    rows = value.get("rows")
    if not isinstance(contexts, list) or not isinstance(rows, list):
        return value
    normalized_rows: list[Any] = []
    changed = False
    for row in rows:
        if (
            isinstance(row, list)
            and len(row) >= 4
            and isinstance(row[3], list)
            and len(row[3]) == 3
            and isinstance(row[3][1], str)
            and isinstance(row[3][2], str)
            and row[3][1] in _LEGACY_QUALIFIERS
            and row[3][2] in _LEGACY_STATISTICS
        ):
            row = list(row)
            row[3] = [list(row[3])]
            changed = True
        normalized_rows.append(row)
    if not changed:
        return value
    normalized = dict(value)
    normalized["rows"] = normalized_rows
    return normalized


def _legacy_compact_validation_rows(value: Any) -> list[list[Any]] | None:
    """Recognise a bare compact validator decision or decision list."""
    rows: Any = value
    if not isinstance(rows, list) or not rows:
        return None
    if len(rows) == 6 and _is_compact_validation_row(rows):
        rows = [rows]
    if not all(_is_compact_validation_row(row) for row in rows):
        return None
    return rows


def _is_compact_validation_row(row: Any) -> bool:
    return (
        isinstance(row, list)
        and len(row) == 6
        and isinstance(row[0], str)
        and bool(row[0].strip())
        and isinstance(row[1], str)
        and row[1] in _VALIDATION_ACTIONS
        and isinstance(row[2], list)
        and all(isinstance(item, str) for item in row[2])
        and isinstance(row[3], list)
        and all(isinstance(item, str) for item in row[3])
        and isinstance(row[4], list)
        and all(isinstance(item, str) for item in row[4])
        and isinstance(row[5], bool)
    )


_LEGACY_QUALIFIERS = {
    "exact",
    "less_than",
    "less_than_or_equal",
    "greater_than",
    "not_detected",
    "not_quantified",
    "range",
    "unknown",
}
_LEGACY_STATISTICS = {
    "single",
    "mean",
    "median",
    "minimum",
    "maximum",
    "range",
    "percentile",
    "frequency",
    "unknown",
}


def _legacy_compact_rows(value: Any) -> list[list[Any]] | None:
    """Recognise the historical row-array response without relaxing arbitrary JSON."""
    rows: Any = value
    if isinstance(value, dict) and set(value) == {"candidates"}:
        rows = value.get("candidates")
    if not isinstance(rows, list) or not rows:
        return None
    # Some compact-capable models emit one legacy row directly instead of wrapping it
    # in ``candidates: [row]`` (for example ``[matrix, name, unit, value, ...]``).
    # Recognise only the same tightly constrained shape used below, then normalize it
    # to the historical row-list form.  This does not turn arbitrary non-empty arrays
    # into zero or valid records.
    if (6 <= len(rows) <= 11 and isinstance(rows[0], str) and rows[0].strip()
            and isinstance(rows[1], str) and rows[1].strip()
            and rows[4] in _LEGACY_QUALIFIERS and rows[5] in _LEGACY_STATISTICS):
        rows = [rows]
    if not all(isinstance(row, list) for row in rows):
        return None
    if not all(6 <= len(row) <= 11 for row in rows):
        return None
    # Historical order is [context_or_matrix, name, unit, value, qualifier, statistic, ...].
    # Requiring the two enum positions prevents ordinary non-empty JSON arrays from being
    # silently accepted as extraction results.
    for row in rows:
        if not isinstance(row[0], str) or not row[0].strip():
            return None
        if not isinstance(row[1], str) or not row[1].strip():
            return None
        if row[4] not in _LEGACY_QUALIFIERS or row[5] not in _LEGACY_STATISTICS:
            return None
    return rows


def _legacy_rows_to_shared_context(
    rows: list[list[Any]], request: Mapping[str, Any]
) -> dict[str, Any]:
    chunk = request.get("chunk") if isinstance(request, Mapping) else None
    chunk = chunk if isinstance(chunk, Mapping) else {}
    chunk_text = str(chunk.get("text") or "")
    page = chunk.get("page_start")
    page = page if isinstance(page, (int, str)) else None
    contexts: list[dict[str, Any]] = []
    compact_rows: list[list[Any]] = []
    for index, row in enumerate(rows, start=1):
        context_id = f"legacy-{index}"
        raw_name = str(row[1]).strip()
        raw_unit = row[2] if isinstance(row[2], str) else None
        raw_value = row[3] if isinstance(row[3], str) else None
        kind = row[6] if len(row) > 6 and row[6] in {"individual", "transformation_product", "ambiguous", None} else "individual"
        parent = row[7] if len(row) > 7 and isinstance(row[7], str) else None
        row_label = row[8] if len(row) > 8 and isinstance(row[8], str) else raw_name
        column_label = row[9] if len(row) > 9 and isinstance(row[9], str) else None
        contexts.append(
            {
                "id": context_id,
                "matrix": None,
                "site": None,
                "waterbody": None,
                "city": None,
                "admin1": None,
                "country": None,
                "time": None,
                "method": None,
                "page": page,
                "table": None,
                "evidence": _legacy_evidence_quote(
                    chunk_text, raw_name, raw_value
                ),
            }
        )
        compact_rows.append(
            [
                context_id,
                raw_name,
                raw_unit,
                [[raw_value, row[4], row[5]]],
                kind,
                parent,
                row_label,
                column_label,
            ]
        )
    return {"contexts": contexts, "rows": compact_rows}


def _legacy_evidence_quote(text: str, raw_name: str, raw_value: str | None) -> str:
    if not text:
        return f"Legacy compact row: {raw_name} {raw_value or ''}".strip()
    needles = [item for item in (raw_name, raw_value) if item]
    start = next((text.casefold().find(item.casefold()) for item in needles if text.casefold().find(item.casefold()) >= 0), 0)
    left = max(0, start - 300)
    return text[left : left + 1200]


def _parse_model_json(content: str) -> Any:
    candidate = content.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", candidate, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as original_error:
        decoder = json.JSONDecoder()
        parsed: list[Any] = []
        for match in re.finditer(r"[\[{]", candidate):
            try:
                value, _ = decoder.raw_decode(candidate[match.start() :])
                parsed.append(value)
            except json.JSONDecodeError:
                continue
        if parsed:
            # Reasoning models may dump a long chain of thought and finish with the real
            # answer; the last complete JSON value is the best candidate.
            return parsed[-1]
        raise AgentCommandError("model content was not valid JSON") from original_error


def _validate_response(value: Any, schema: dict[str, Any], role: str) -> list[str]:
    validation_errors = sorted(
        Draft202012Validator(schema).iter_errors(value), key=lambda item: list(item.path)
    )
    messages = [
        "/" + "/".join(str(part) for part in error.path) + ": " + error.message
        for error in validation_errors
    ]
    if role == "extractor" and isinstance(value, dict):
        candidates = value.get("candidates")
        if isinstance(candidates, list) and len(candidates) > 1000:
            messages.append("/candidates: unsafe size greater than 1000")
    return messages


def _write_audit(audit_dir: Path | None, metadata: dict[str, Any], request: dict[str, Any]) -> None:
    if audit_dir is None:
        return
    audit_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(redact(metadata))
    payload["role_from_request"] = request.get("role")
    payload["document_id"] = request.get("chunk", {}).get("document_id")
    with (audit_dir / "model_calls.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(redact(payload), ensure_ascii=True, separators=(",", ":")) + "\n")
        handle.flush()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AgentCommandError as exc:
        import sys as _sys

        message = str(redact(str(exc)))
        try:
            _sys.stderr.buffer.write(message.encode("utf-8", "replace") + b"\n")
            _sys.stderr.buffer.flush()
        except Exception:  # noqa: BLE001 - fall back when stderr has no buffer
            print(message, file=_sys.stderr)
        raise SystemExit(2) from exc
