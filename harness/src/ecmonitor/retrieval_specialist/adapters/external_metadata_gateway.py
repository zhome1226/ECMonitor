"""Gateway for ECfinder ``external_metadata_discovery_v1`` integration."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from jsonschema import validate

from ecmonitor.retrieval_specialist.models import CanonicalQuery, RawRecord
from ecmonitor.retrieval_specialist.operators.query_compiler import CanonicalQueryCompiler
from ecmonitor.retrieval_specialist.operators.time_utils import utc_now_iso
from ecmonitor.retrieval_specialist.storage.atomic_io import (
    ensure_dir,
    read_json,
    write_json_atomic,
)
from ecmonitor.retrieval_specialist.storage.control_plane import ControlPlane

PROVIDERS = ["crossref", "openalex", "semantic_scholar", "pubmed"]
SKILL_RELATIVE_DIR = Path("skills") / "search" / "external_metadata_discovery"
RUNNER_MODULE = "ecfinder.skills.external_metadata_discovery.runner"


@dataclass(frozen=True)
class ExternalMetadataDiscoveryGateway:
    """Durable file boundary around ECfinder external metadata discovery skill."""

    repo_root: Path
    db_path: Path
    code_commit_sha: str
    env_var: str = "ECFINDER_METADATA_SKILL_PATH"

    def entrypoint(self) -> dict[str, Any]:
        root = self._resolve_ecfinder_root()
        skill_dir = root / "skills" / "search" / "external_metadata_discovery"
        runner_path = (
            root
            / "src"
            / "ecfinder"
            / "skills"
            / "external_metadata_discovery"
            / "runner.py"
        )
        if not runner_path.exists():
            raise FileNotFoundError(
                "external_metadata_discovery_v1 has no public executable runner at "
                f"{runner_path}; add {RUNNER_MODULE} before running live discovery."
            )
        return {
            "skill_id": "external_metadata_discovery_v1",
            "root": str(root),
            "entrypoint_type": "python_module_subprocess",
            "entrypoint_module": RUNNER_MODULE,
            "runner_path": str(runner_path),
            "instruction_ref": str(skill_dir / "instruction.md"),
            "input_schema_ref": str(skill_dir / "input.schema.json"),
            "output_schema_ref": str(skill_dir / "output.schema.json"),
            "requires_external_worker": False,
            "provider_code_reused": True,
            "provider_modules": [],
        }

    def invoke(
        self,
        *,
        run_id: str,
        query: CanonicalQuery,
        run_dir: Path,
        providers: list[str] | None,
        page_size: int,
        max_candidates: int,
        max_scan_depth_per_provider: int,
        config_ref: Path | None = None,
    ) -> dict[str, Any]:
        root = Path(str(self.entrypoint()["root"]))
        output_root = ensure_dir(run_dir / "external_metadata" / query.query_id)
        queries_ref = self._write_queries_ref(output_root, query, providers or PROVIDERS)
        resume_state_ref = self._write_resume_state(output_root, run_id, query.query_id)
        output_ref = output_root / "external_metadata_discovery_output.json"
        payload = {
            "run_id": run_id,
            "query_id": query.query_id,
            "iteration": query.iteration,
            "skill_id": "external_metadata_discovery_v1",
            "canonical_query": query.to_dict(),
            "date_from": query.date_from,
            "date_to": query.date_to,
            "document_types": query.document_types,
            "queries_ref": str(queries_ref),
            "compiled_query_ref": str(queries_ref),
            "output_root": str(output_root),
            "expected_output_ref": str(output_ref),
            "providers": providers or PROVIDERS,
            "max_results_per_query": max_candidates,
            "max_candidates": max_candidates,
            "page_size": page_size,
            "max_scan_depth_per_provider": max_scan_depth_per_provider,
            "resume_state_ref": str(resume_state_ref),
            "config_ref": str(config_ref) if config_ref else "",
        }
        self._validate_input(payload)
        input_ref = output_root / "external_metadata_discovery_input.json"
        write_json_atomic(input_ref, payload)
        self._run_skill_subprocess(root=root, input_ref=input_ref, output_ref=output_ref)
        output = cast(dict[str, Any], read_json(output_ref))
        self._validate_output(output)
        return output | {
            "input_ref": str(input_ref),
            "output_ref": str(output_ref),
            "entrypoint": self.entrypoint(),
        }

    def invoke_metadata_enrichment(
        self,
        *,
        run_id: str,
        run_dir: Path,
        query_id: str,
        records: list[dict[str, Any]],
        output_root: Path,
        providers: list[str] | None = None,
        page_size: int | None = None,
        config_ref: Path | None = None,
    ) -> dict[str, Any]:
        root = Path(str(self.entrypoint()["root"]))
        output_root = ensure_dir(output_root)
        requested_providers = providers or ["crossref", "openalex", "pubmed"]
        queries_ref = self._write_metadata_enrichment_queries_ref(
            output_root,
            records,
            requested_providers,
        )
        query = self._metadata_enrichment_query(
            run_id=run_id,
            query_id=query_id,
            records=records,
            query_ref=queries_ref,
        )
        resume_state_ref = output_root / "resume_state.json"
        if not resume_state_ref.exists():
            write_json_atomic(resume_state_ref, {"completed_pages": []})
        output_ref = output_root / "external_metadata_discovery_output.json"
        payload = {
            "run_id": run_id,
            "query_id": query.query_id,
            "iteration": query.iteration,
            "skill_id": "external_metadata_discovery_v1",
            "canonical_query": query.to_dict(),
            "date_from": query.date_from,
            "date_to": query.date_to,
            "document_types": query.document_types,
            "queries_ref": str(queries_ref),
            "compiled_query_ref": str(queries_ref),
            "output_root": str(output_root),
            "expected_output_ref": str(output_ref),
            "providers": requested_providers,
            "max_results_per_query": max(1, len(records)),
            "max_candidates": max(1, len(records)),
            "page_size": page_size or max(1, len(records)),
            "max_scan_depth_per_provider": 1,
            "resume_state_ref": str(resume_state_ref),
            "config_ref": str(config_ref) if config_ref else "",
        }
        self._validate_input(payload)
        input_ref = output_root / "external_metadata_discovery_input.json"
        signature_ref = output_root / "request_signature.json"
        request_signature = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
        ).hexdigest()
        if output_ref.exists() and signature_ref.exists():
            signature_payload = read_json(signature_ref)
            if signature_payload.get("sha256") != request_signature:
                raise RuntimeError(
                    "metadata enrichment output directory contains a completed result "
                    "for a different request"
                )
            output = cast(dict[str, Any], read_json(output_ref))
            self._validate_output(output)
            self._validate_output_references(output)
            return output | {
                "input_ref": str(input_ref),
                "output_ref": str(output_ref),
                "entrypoint": self.entrypoint(),
                "reused_completed_output": True,
            }
        write_json_atomic(input_ref, payload)
        write_json_atomic(signature_ref, {"sha256": request_signature})
        self._run_skill_subprocess(root=root, input_ref=input_ref, output_ref=output_ref)
        output = cast(dict[str, Any], read_json(output_ref))
        self._validate_output(output)
        self._validate_output_references(output)
        return output | {
            "input_ref": str(input_ref),
            "output_ref": str(output_ref),
            "entrypoint": self.entrypoint(),
            "reused_completed_output": False,
        }

    @staticmethod
    def _validate_output_references(output: dict[str, Any]) -> None:
        required_refs = [
            "candidates_ref",
            "provider_states_ref",
            "source_status_ref",
            "memory_usage_ref",
        ]
        missing = [
            key
            for key in required_refs
            if not output.get(key) or not Path(str(output[key])).exists()
        ]
        if missing:
            raise FileNotFoundError(
                "external_metadata_discovery_v1 completed output has missing file "
                f"references: {', '.join(missing)}"
            )

    def _run_skill_subprocess(self, *, root: Path, input_ref: Path, output_ref: Path) -> None:
        env = os.environ.copy()
        pythonpath_parts = [str(root / "src")]
        if env.get("PYTHONPATH"):
            pythonpath_parts.append(str(env["PYTHONPATH"]))
        env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
        timeout_seconds = int(env.get("ECMONITOR_EXTERNAL_METADATA_TIMEOUT_SECONDS", "900"))
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                RUNNER_MODULE,
                "--input",
                str(input_ref),
                "--output",
                str(output_ref),
            ],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "external_metadata_discovery_v1 runner failed with exit code "
                f"{result.returncode}: {result.stderr.strip() or result.stdout.strip()}"
            )
        if not output_ref.exists():
            raise FileNotFoundError(
                f"external_metadata_discovery_v1 runner did not write {output_ref}"
            )

    def import_into_control_plane(
        self,
        *,
        run_id: str,
        query: CanonicalQuery,
        discovery_output: dict[str, Any],
    ) -> dict[str, Any]:
        raw_result_count = 0
        scanned_result_count = 0
        imported_pages = 0
        source_statuses = dict(read_json(Path(str(discovery_output["source_status_ref"]))))
        for provider, refs in dict(discovery_output["provider_page_refs"]).items():
            for index, ref in enumerate(refs, start=1):
                page_path = Path(str(ref))
                page_records = _read_jsonl(page_path)
                if not page_records:
                    continue
                batch_id = f"{provider}_page_{index:04d}"
                if self._checkpoint_completed(run_id, query, provider, batch_id):
                    scanned_result_count += len(page_records)
                    raw_result_count += len(page_records)
                    continue
                with ControlPlane(self.db_path, self.code_commit_sha).transaction() as connection:
                    for payload in page_records:
                        source_record_id = str(payload["source_record_id"])
                        rank = int(payload["rank"])
                        raw_record = RawRecord(
                            source_name=str(provider),
                            source_record_id=source_record_id,
                            rank=rank,
                            raw=dict(payload),
                            retrieval_timestamp=str(payload.get("retrieved_at") or utc_now_iso()),
                        )
                        raw_json = json.dumps(
                            raw_record.to_dict(), ensure_ascii=True, sort_keys=True
                        )
                        checksum = hashlib.sha256(raw_json.encode()).hexdigest()
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO source_records (
                                source_name, source_record_id, query_id, run_id,
                                source_rank, retrieval_page, retrieval_cursor,
                                raw_metadata_path, raw_payload_checksum,
                                raw_payload_json, retrieved_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                provider,
                                source_record_id,
                                query.query_id,
                                run_id,
                                rank,
                                int(payload.get("retrieval_page") or index),
                                str((index - 1) * max(1, len(page_records))),
                                self._display_path(page_path),
                                checksum,
                                raw_json,
                                raw_record.retrieval_timestamp,
                            ),
                        )
                    self._record_checkpoint(
                        connection=connection,
                        run_id=run_id,
                        query=query,
                        operator="SEARCH_SOURCES",
                        batch_id=batch_id,
                        source_name=str(provider),
                        page_cursor=str((index - 1) * max(1, len(page_records))),
                        next_page_cursor=str(index * max(1, len(page_records))),
                        processed_count=len(page_records),
                        persisted_count=len(page_records),
                    )
                imported_pages += 1
                scanned_result_count += len(page_records)
                raw_result_count += len(page_records)
        return {
            "raw_result_count": raw_result_count,
            "scanned_result_count": scanned_result_count,
            "imported_pages": imported_pages,
            "source_statuses": source_statuses,
            "providers_attempted": discovery_output["providers_attempted"],
            "providers_available": discovery_output["providers_available"],
            "total_external_candidates": discovery_output["total_external_candidates"],
            "execution_status": discovery_output["execution_status"],
        }

    def _resolve_ecfinder_root(self) -> Path:
        value = os.environ.get(self.env_var, "").strip()
        if not value:
            raise RuntimeError(
                f"{self.env_var} is not set; point it to the external ECfinder root "
                "containing skills/search/external_metadata_discovery and src/ecfinder."
            )
        root = Path(value).expanduser().resolve()
        if (root / "skill.yaml").exists():
            root = root.parents[2]
        if not (root / "skills" / "search" / "external_metadata_discovery").exists():
            raise FileNotFoundError(
                f"{self.env_var} does not point to a valid ECfinder root: {root}"
            )
        return root

    def _write_queries_ref(
        self, output_root: Path, query: CanonicalQuery, providers: list[str]
    ) -> Path:
        compiler = CanonicalQueryCompiler()
        compiled = compiler.compile_for_source(query, "canonical")
        rows = []
        for provider in providers:
            rows.append(
                {
                    "query_id": query.query_id,
                    "provider": provider,
                    "source_name": provider,
                    "compiled_query": compiled.compiled_query,
                    "query_text": compiled.compiled_query,
                    "canonical_query": query.to_dict(),
                    "date_from": query.date_from,
                    "date_to": query.date_to,
                    "document_types": query.document_types,
                    "compiler_version": compiled.compiler_version,
                }
            )
        path = output_root / "queries_ref.json"
        write_json_atomic(path, rows)
        return path

    def _write_metadata_enrichment_queries_ref(
        self,
        output_root: Path,
        records: list[dict[str, Any]],
        providers: list[str],
    ) -> Path:
        rows = []
        for provider in providers:
            identifiers = self._metadata_enrichment_identifiers(records, provider)
            rows.append(
                {
                    "query_id": "CANDIDATE_POOL_METADATA_ENRICHMENT",
                    "provider": provider,
                    "source_name": provider,
                    "compiled_query": " OR ".join(identifiers),
                    "query_text": " OR ".join(identifiers),
                    "lookup_mode": "metadata_enrichment_by_identifier",
                    "metadata_enrichment_records": [
                        {
                            "candidate_pool_key": record.get("candidate_pool_key"),
                            "doi": record.get("doi"),
                            "pmid": record.get("pmid"),
                            "openalex_id": record.get("openalex_id"),
                            "provider_record_id": record.get("provider_record_id"),
                            "title": record.get("title"),
                        }
                        for record in records
                    ],
                    "canonical_query": {
                        "lookup_mode": "metadata_enrichment_by_identifier",
                        "identifiers": identifiers,
                    },
                    "compiler_version": "metadata-enrichment-v1",
                }
            )
        path = output_root / "queries_ref.json"
        write_json_atomic(path, rows)
        return path

    @staticmethod
    def _metadata_enrichment_identifiers(
        records: list[dict[str, Any]], provider: str
    ) -> list[str]:
        values: list[str] = []
        for record in records:
            if provider == "pubmed" and record.get("pmid"):
                values.append(str(record["pmid"]))
            elif provider == "openalex" and record.get("openalex_id"):
                values.append(str(record["openalex_id"]))
            elif record.get("doi"):
                values.append(str(record["doi"]))
        return list(dict.fromkeys(value for value in values if value.strip()))

    @staticmethod
    def _metadata_enrichment_query(
        *, run_id: str, query_id: str, records: list[dict[str, Any]], query_ref: Path
    ) -> CanonicalQuery:
        terms = [
            str(record.get("doi") or record.get("pmid") or record.get("openalex_id") or "")
            for record in records
        ]
        return CanonicalQuery(
            query_id=query_id,
            parent_query_id=None,
            iteration=1,
            date_from="",
            date_to="",
            document_types=["journal article", "research article"],
            emerging_contaminant_terms=[term for term in terms if term],
            surface_water_terms=[],
            monitoring_and_concentration_terms=[],
            change_rationale=f"{run_id} candidate-pool metadata enrichment by identifier.",
            expected_effect="Enrich missing title/abstract metadata without PDF download.",
            evidence_for_change=[str(query_ref)],
        )

    def _write_resume_state(self, output_root: Path, run_id: str, query_id: str) -> Path:
        rows: list[dict[str, Any]] = []
        if self.db_path.exists():
            with ControlPlane(self.db_path, self.code_commit_sha).connect() as connection:
                rows = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT operator, source_name, batch_id, page_cursor,
                               next_page_cursor, status
                        FROM operator_checkpoints
                        WHERE run_id = ? AND query_id = ?
                          AND operator = 'SEARCH_SOURCES'
                        ORDER BY source_name, batch_id
                        """,
                        (run_id, query_id),
                    )
                ]
        path = output_root / "resume_state.json"
        write_json_atomic(path, {"completed_pages": rows})
        return path

    def _validate_input(self, payload: dict[str, Any]) -> None:
        validate(
            instance=payload,
            schema=read_json(
                self.repo_root
                / SKILL_RELATIVE_DIR
                / "input.schema.json"
            ),
        )

    def _validate_output(self, payload: dict[str, Any]) -> None:
        validate(
            instance=payload,
            schema=read_json(
                self.repo_root
                / SKILL_RELATIVE_DIR
                / "output.schema.json"
            ),
        )

    def _checkpoint_completed(
        self, run_id: str, query: CanonicalQuery, provider: str, batch_id: str
    ) -> bool:
        if not self.db_path.exists():
            return False
        with ControlPlane(self.db_path, self.code_commit_sha).connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM operator_checkpoints
                WHERE run_id = ? AND query_id = ? AND iteration = ?
                  AND operator = 'SEARCH_SOURCES'
                  AND source_name = ? AND batch_id = ? AND status = 'completed'
                """,
                (run_id, query.query_id, query.iteration, provider, batch_id),
            ).fetchone()
        return row is not None

    def _record_checkpoint(
        self,
        *,
        connection: Any,
        run_id: str,
        query: CanonicalQuery,
        operator: str,
        batch_id: str,
        source_name: str,
        page_cursor: str,
        next_page_cursor: str,
        processed_count: int,
        persisted_count: int,
    ) -> None:
        checksum = hashlib.sha256(
            f"{run_id}|{query.query_id}|{operator}|{source_name}|{batch_id}|"
            f"{processed_count}|{persisted_count}".encode()
        ).hexdigest()
        now = utc_now_iso()
        existing = connection.execute(
            """
            SELECT checkpoint_id
            FROM operator_checkpoints
            WHERE run_id = ? AND query_id = ? AND iteration = ?
              AND operator = ? AND COALESCE(source_name, '') = COALESCE(?, '')
              AND batch_id = ?
            """,
            (run_id, query.query_id, query.iteration, operator, source_name, batch_id),
        ).fetchone()
        if existing:
            connection.execute(
                """
                UPDATE operator_checkpoints
                SET page_cursor = ?,
                    next_page_cursor = ?,
                    input_checksum = ?,
                    output_checksum = ?,
                    processed_count = ?,
                    persisted_count = ?,
                    status = 'completed',
                    completed_at = ?,
                    retry_count = 0,
                    error_message = NULL
                WHERE checkpoint_id = ?
                """,
                (
                    page_cursor,
                    next_page_cursor,
                    checksum,
                    checksum,
                    processed_count,
                    persisted_count,
                    now,
                    int(existing[0]),
                ),
            )
            return
        connection.execute(
            """
            INSERT INTO operator_checkpoints (
                run_id, query_id, iteration, operator, source_name, batch_id,
                page_cursor, next_page_cursor, input_checksum, output_checksum,
                processed_count, persisted_count, status, started_at, completed_at,
                retry_count, error_message
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, 0, NULL)
            """,
            (
                run_id,
                query.query_id,
                query.iteration,
                operator,
                source_name,
                batch_id,
                page_cursor,
                next_page_cursor,
                checksum,
                checksum,
                processed_count,
                persisted_count,
                now,
                now,
            ),
        )

    def _display_path(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.repo_root.resolve()))
        except ValueError:
            return str(path)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        for line in handle:
            if line.strip():
                records.append(json.loads(line))
    return records
