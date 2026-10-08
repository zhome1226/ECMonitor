"""Short-lived JSON command adapters for isolated extractor and validator agents.

Each call starts a new process, sends one self-contained JSON request on stdin, and accepts one JSON
response on stdout. This provides a model-provider-neutral isolation boundary: no process,
conversation, or hidden model state is reused between calls.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from ecmonitor.fulltext_extraction.errors import classify_document_error
from ecmonitor.fulltext_extraction.models import (
    ChemicalResolution,
    EvidenceChunk,
    ValidationDecision,
)


class JsonCommandError(RuntimeError):
    pass


class JsonCommandValidatorError(JsonCommandError):
    """Marks validator transport failures so extraction fallback does not re-run the PDF."""

    pass


class JsonCommandCandidateExtractor:
    extractor_name = "json_command_candidate_extractor"

    def __init__(
        self,
        command: tuple[str, ...],
        *,
        prompt_path: Path | None = None,
        timeout_seconds: int = 300,
        cwd: Path | None = None,
        transport_retries: int = 2,
        transport_backoff_seconds: float = 2.0,
    ) -> None:
        if not command:
            raise ValueError("extractor command may not be empty")
        if transport_retries < 0:
            raise ValueError("transport_retries must be non-negative")
        if transport_backoff_seconds < 0:
            raise ValueError("transport_backoff_seconds must be non-negative")
        self.command = command
        self.prompt_path = prompt_path
        self.timeout_seconds = timeout_seconds
        self.cwd = cwd
        self.transport_retries = transport_retries
        self.transport_backoff_seconds = transport_backoff_seconds
        self.document_context: dict[str, Any] | None = None
        self.pdf_path: Path | None = None

    def extract(self, chunk: EvidenceChunk) -> list[dict[str, Any]]:
        return self.extract_with_feedback(
            chunk,
            attempt=1,
            reason_codes=(),
            failed_json_pointers=(),
            requested_context=(),
        )

    def extract_with_feedback(
        self,
        chunk: EvidenceChunk,
        *,
        attempt: int,
        reason_codes: tuple[str, ...],
        failed_json_pointers: tuple[str, ...],
        requested_context: tuple[str, ...],
        failed_candidates: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
        prior_candidates: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "role": "occurrence_extractor",
            "attempt": attempt,
            "prompt": _read_prompt(self.prompt_path),
            "chunk": chunk.to_dict(),
            "retry_feedback": {
                "reason_codes": reason_codes,
                "failed_json_pointers": failed_json_pointers,
                "requested_context": requested_context,
                "failed_candidates": list(failed_candidates or []),
                "prior_candidates": list(prior_candidates or []),
            },
        }
        if self.document_context:
            payload["document_context"] = dict(self.document_context)
        if self.pdf_path is not None:
            payload["pdf_path"] = str(self.pdf_path)
        response = _run_json_command(
            self.command,
            payload,
            timeout_seconds=self.timeout_seconds,
            cwd=self.cwd,
            transport_retries=self.transport_retries,
            transport_backoff_seconds=self.transport_backoff_seconds,
        )
        candidates = response.get("candidates") if isinstance(response, dict) else response
        if not isinstance(candidates, list) or any(
            not isinstance(item, dict) for item in candidates
        ):
            raise JsonCommandError(
                "extractor response must be a list or an object with candidates[]"
            )
        return candidates


class JsonCommandEvidenceValidator:
    validator_name = "json_command_evidence_validator"

    def __init__(
        self,
        command: tuple[str, ...],
        *,
        prompt_path: Path | None = None,
        timeout_seconds: int = 300,
        cwd: Path | None = None,
        transport_retries: int = 2,
        transport_backoff_seconds: float = 2.0,
        max_batch_size: int = 20,
    ) -> None:
        if not command:
            raise ValueError("validator command may not be empty")
        if transport_retries < 0:
            raise ValueError("transport_retries must be non-negative")
        if transport_backoff_seconds < 0:
            raise ValueError("transport_backoff_seconds must be non-negative")
        if max_batch_size < 1:
            raise ValueError("max_batch_size must be positive")
        self.command = command
        self.prompt_path = prompt_path
        self.timeout_seconds = timeout_seconds
        self.cwd = cwd
        self.transport_retries = transport_retries
        self.transport_backoff_seconds = transport_backoff_seconds
        self.max_batch_size = max_batch_size
        self.document_context: dict[str, Any] | None = None

    def validate(
        self,
        candidate: dict[str, Any],
        *,
        chunk: EvidenceChunk,
        resolutions: tuple[ChemicalResolution, ...],
    ) -> ValidationDecision:
        payload = {
            "role": "evidence_validator",
            "prompt": _read_prompt(self.prompt_path),
            "candidate": candidate,
            "chunk": chunk.to_dict(),
            "chemical_resolutions": [item.to_dict() for item in resolutions],
        }
        if self.document_context:
            payload["document_context"] = dict(self.document_context)
        try:
            response = _run_json_command(
                self.command,
                payload,
                timeout_seconds=self.timeout_seconds,
                cwd=self.cwd,
                transport_retries=self.transport_retries,
                transport_backoff_seconds=self.transport_backoff_seconds,
            )
        except JsonCommandError as exc:
            raise JsonCommandValidatorError(f"validator request failed: {exc}") from exc
        if not isinstance(response, dict):
            raise JsonCommandError("validator response must be a JSON object")
        return _decision_from_response(response)

    def validate_batch(
        self,
        candidates: list[dict[str, Any]],
        *,
        chunk: EvidenceChunk,
        resolutions_by_candidate: list[tuple[ChemicalResolution, ...]],
    ) -> list[ValidationDecision]:
        if len(candidates) != len(resolutions_by_candidate):
            raise ValueError("candidates and resolutions_by_candidate must have equal length")
        if not candidates:
            return []
        candidate_ids = [str(item.get("candidate_id", "")) for item in candidates]
        if any(not item for item in candidate_ids) or len(set(candidate_ids)) != len(candidate_ids):
            raise JsonCommandError("batch validator requires unique non-empty candidate_id values")

        ordered: list[ValidationDecision] = []
        for start in range(0, len(candidates), self.max_batch_size):
            stop = min(start + self.max_batch_size, len(candidates))
            batch_candidates = candidates[start:stop]
            batch_resolutions = resolutions_by_candidate[start:stop]
            batch_ids = candidate_ids[start:stop]
            payload = {
                "role": "evidence_validator_batch",
                "prompt": _read_prompt(self.prompt_path),
                "chunk": chunk.to_dict(),
                "validation_items": [
                    {
                        "candidate_id": candidate_id,
                        "candidate": _compact_candidate_for_validation(candidate),
                        "chemical_resolutions": [
                            _compact_resolution_for_validation(item) for item in resolutions
                        ],
                    }
                    for candidate_id, candidate, resolutions in zip(
                        batch_ids, batch_candidates, batch_resolutions, strict=True
                    )
                ],
            }
            if self.document_context:
                payload["document_context"] = dict(self.document_context)
            try:
                response = _run_json_command(
                    self.command,
                    payload,
                    timeout_seconds=self.timeout_seconds,
                    cwd=self.cwd,
                    transport_retries=self.transport_retries,
                    transport_backoff_seconds=self.transport_backoff_seconds,
                )
            except JsonCommandError as exc:
                raise JsonCommandValidatorError(f"validator batch failed: {exc}") from exc
            decisions = response.get("decisions") if isinstance(response, dict) else None
            if not isinstance(decisions, list) or any(
                not isinstance(item, dict) for item in decisions
            ):
                raise JsonCommandError("batch validator response must contain decisions[]")
            by_id: dict[str, dict[str, Any]] = {}
            for item in decisions:
                candidate_id = str(item.get("candidate_id", ""))
                if not candidate_id or candidate_id in by_id:
                    raise JsonCommandError(
                        "batch validator returned missing or duplicate candidate_id"
                    )
                by_id[candidate_id] = item
            if set(by_id) != set(batch_ids):
                missing = sorted(set(batch_ids) - set(by_id))
                extra = sorted(set(by_id) - set(batch_ids))
                raise JsonCommandError(
                    f"batch validator candidate_id mismatch; missing={missing[:5]!r} "
                    f"extra={extra[:5]!r}"
                )
            ordered.extend(
                _decision_from_response(by_id[candidate_id]) for candidate_id in batch_ids
            )
        return ordered


def _compact_candidate_for_validation(candidate: dict[str, Any]) -> dict[str, Any]:
    """Keep validator-critical facts while dropping duplicated enrichment payload."""
    keys = (
        "candidate_id",
        "observation_type",
        "analyte",
        "result",
        "sample",
        "location",
        "sampling_time",
        "analytical_method",
        "evidence",
        "quality_flags",
    )
    return {key: candidate[key] for key in keys if key in candidate}


def _compact_resolution_for_validation(resolution: ChemicalResolution) -> dict[str, Any]:
    """Avoid sending large PubChem raw payloads and synonym arrays to the validator."""
    return {
        "raw_name": resolution.raw_name,
        "normalized_query": resolution.normalized_query,
        "status": resolution.status,
        "resolver_name": resolution.resolver_name,
        "matches": [
            {
                "source": match.source,
                "source_record_id": match.source_record_id,
                "canonical_name": match.canonical_name,
                "matched_alias": match.matched_alias,
                "pubchem_cid": match.pubchem_cid,
                "cas_candidates": list(match.cas_candidates[:5]),
                "inchikey": match.inchikey,
            }
            for match in resolution.matches[:5]
        ],
        "warnings": list(resolution.warnings[:10]),
    }


def _run_json_command(
    command: tuple[str, ...],
    payload: dict[str, Any],
    *,
    timeout_seconds: int,
    cwd: Path | None,
    transport_retries: int = 2,
    transport_backoff_seconds: float = 2.0,
) -> object:
    """Run one JSON command, retrying the same request on model-transport failures.

    The short-lived agent process already retries gateway jitter internally (bounded by its own
    deadline). When that budget is exhausted it exits non-zero and the error is almost always a
    transient gateway problem. Retrying the *same self-contained request* a bounded number of
    times here with short backoff recovers most of those cases without paying for a whole-document
    re-run (re-parse, re-extract, re-review). Non-retryable errors propagate immediately.
    """
    last_error: Exception | None = None
    max_attempts = 1 + transport_retries
    for attempt in range(1, max_attempts + 1):
        try:
            return _run_json_command_once(
                command, payload, timeout_seconds=timeout_seconds, cwd=cwd
            )
        except (subprocess.TimeoutExpired, JsonCommandError) as exc:
            last_error = exc
            classified = classify_document_error(exc)
            if not classified.retryable or attempt >= max_attempts:
                raise
            delay = transport_backoff_seconds * (2 ** (attempt - 1))
            if delay:
                time.sleep(delay)
    assert last_error is not None
    raise last_error


def _run_json_command_once(
    command: tuple[str, ...],
    payload: dict[str, Any],
    *,
    timeout_seconds: int,
    cwd: Path | None,
) -> object:
    result = subprocess.run(
        list(command),
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
        cwd=cwd,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        message = stderr or stdout
        raise JsonCommandError(
            f"agent command exited with {result.returncode}: {message[:2000]}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise JsonCommandError("agent command did not return valid JSON on stdout") from exc


def _read_prompt(path: Path | None) -> str | None:
    return path.read_text(encoding="utf-8") if path is not None else None


def _decision_from_response(response: dict[str, Any]) -> ValidationDecision:
    action = response.get("action")
    if action not in {"accept", "retry", "escalate", "reject"}:
        raise JsonCommandError(f"invalid validator action: {action!r}")
    return ValidationDecision(
        action=action,
        reason_codes=_string_tuple(response.get("reason_codes")),
        failed_json_pointers=_string_tuple(response.get("failed_json_pointers")),
        requested_context=_string_tuple(response.get("requested_context")),
        human_review_required=bool(response.get("human_review_required", False)),
    )


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if str(item))
