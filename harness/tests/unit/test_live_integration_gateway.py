import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from ecmonitor.retrieval_specialist.adapters.external_metadata_gateway import (
    ExternalMetadataDiscoveryGateway,
)
from ecmonitor.retrieval_specialist.cli import main as cli_main
from ecmonitor.retrieval_specialist.models import CanonicalQuery
from ecmonitor.retrieval_specialist.operators.query_planner import QueryPlanner
from ecmonitor.retrieval_specialist.orchestration.checkpoints import CheckpointManager
from ecmonitor.retrieval_specialist.orchestration.phase11_runner import Phase11Runner
from ecmonitor.retrieval_specialist.orchestration.run_manager import RunManager
from ecmonitor.retrieval_specialist.storage.control_plane import ControlPlane


def _query(query_id: str = "Q0001", iteration: int = 1) -> CanonicalQuery:
    return CanonicalQuery(
        query_id=query_id,
        parent_query_id=None,
        iteration=iteration,
        date_from="2006-01-01",
        date_to="2026-07-09",
        document_types=["journal article"],
        emerging_contaminant_terms=["PFAS"],
        surface_water_terms=["river"],
        monitoring_and_concentration_terms=["concentration"],
    )


def test_protocol_q0001_surface_water_terms_prioritize_inland_surface_water() -> None:
    protocol = yaml.safe_load(
        (Path.cwd() / "configs" / "retrieval" / "protocol.yaml").read_text(
            encoding="utf-8"
        )
    )
    query = QueryPlanner().build_initial_query(protocol, "2026-07-10")

    assert query.surface_water_terms[:4] == [
        "ambient surface water",
        "surface water",
        "river",
        "stream",
    ]
    assert "ocean" not in query.surface_water_terms
    assert "open ocean" not in query.surface_water_terms
    assert "sea" not in query.surface_water_terms


def test_run_dry_run_flag_errors_instead_of_starting_live_run() -> None:
    with pytest.raises(SystemExit) as exc:
        cli_main(["run", "--date-to", "2026-07-10", "--dry-run"])

    assert exc.value.code == 2


def test_run_no_llm_flag_errors_instead_of_bypassing_live_screening() -> None:
    with pytest.raises(SystemExit) as exc:
        cli_main(["run", "--date-to", "2026-07-10", "--no-llm"])

    assert exc.value.code == 2


def test_dry_run_rejects_ignored_live_source_flags() -> None:
    with pytest.raises(SystemExit) as exc:
        cli_main(["dry-run", "--date-to", "2026-07-10", "--source", "crossref"])

    assert exc.value.code == 2


def _fake_ecfinder_skill(root: Path) -> Path:
    skill_dir = root / "skills" / "search" / "external_metadata_discovery"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(
        "skill_id: external_metadata_discovery_v1\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    (skill_dir / "instruction.md").write_text(
        "Run external metadata discovery.\n", encoding="utf-8"
    )
    (skill_dir / "input.schema.json").write_text("{}", encoding="utf-8")
    (skill_dir / "output.schema.json").write_text("{}", encoding="utf-8")
    runner_dir = root / "src" / "ecfinder" / "skills" / "external_metadata_discovery"
    runner_dir.mkdir(parents=True)
    for package in [
        root / "src" / "ecfinder",
        root / "src" / "ecfinder" / "skills",
        runner_dir,
    ]:
        (package / "__init__.py").write_text("", encoding="utf-8")
    (runner_dir / "runner.py").write_text(
        """
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    request = json.loads(Path(args.input).read_text(encoding="utf-8"))
    output_root = Path(request["output_root"])
    page_root = output_root / "provider_pages"
    page_root.mkdir(parents=True, exist_ok=True)
    page = page_root / "crossref_page_0001.jsonl"
    page.write_text(json.dumps({
        "source_provider": "crossref",
        "source_record_id": "10.5555/test",
        "provider_record_id": "10.5555/test",
        "rank": 1,
        "retrieval_page": 1,
        "query_text": "skill-generated-provider-query",
        "run_id": request["run_id"],
        "query_id": request["query_id"],
        "iteration": request["iteration"],
        "retrieved_at": "2026-07-09T00:00:00Z",
        "doi": "10.5555/test",
        "title": "PFAS in river water",
        "abstract": "Measured concentrations in river water.",
        "document_type": "journal-article",
        "language": "en"
    }, sort_keys=True) + "\\n", encoding="utf-8")
    _write_json(output_root / "source_status.json", {"crossref": "success"})
    _write_json(output_root / "provider_states.json", {
        "crossref": {
            "records": 1,
            "date_from": request["date_from"],
            "date_to": request["date_to"],
            "document_types": request["document_types"]
        }
    })
    _write_json(output_root / "memory_usage.json", {"process_rss_after_mb": 1})
    Path(args.output).write_text(json.dumps({
        "candidates_ref": str(output_root / "candidates.jsonl"),
        "deduped_ref": "",
        "new_sources_ref": "",
        "provider_page_refs": {"crossref": [str(page)]},
        "provider_states_ref": str(output_root / "provider_states.json"),
        "next_cursor_by_provider": {"crossref": "1"},
        "source_status_ref": str(output_root / "source_status.json"),
        "memory_usage_ref": str(output_root / "memory_usage.json"),
        "providers_attempted": ["crossref"],
        "providers_available": ["crossref"],
        "total_external_candidates": 1,
        "execution_status": "success"
    }, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
        encoding="utf-8",
    )
    return root


def _fake_ecfinder_skill_with_health(root: Path) -> Path:
    ecfinder_root = _fake_ecfinder_skill(root)
    runner_path = (
        ecfinder_root
        / "src"
        / "ecfinder"
        / "skills"
        / "external_metadata_discovery"
        / "runner.py"
    )
    text = runner_path.read_text(encoding="utf-8")
    text = text.replace(
        "    parser.add_argument(\"--input\", required=True)\n"
        "    parser.add_argument(\"--output\", required=True)\n"
        "    args = parser.parse_args()\n"
        "    request = json.loads(Path(args.input).read_text(encoding=\"utf-8\"))\n",
        "    parser.add_argument(\"--input\")\n"
        "    parser.add_argument(\"--output\", required=True)\n"
        "    parser.add_argument(\"--health-check\", action=\"store_true\")\n"
        "    args = parser.parse_args()\n"
        "    if args.health_check:\n"
        "        Path(args.output).write_text(json.dumps({\n"
        "            \"mode\": \"provider_health_check\",\n"
        "            \"environment_validation\": {\n"
        "                \"NCBI_EMAIL\": \"configured\",\n"
        "                \"NCBI_TOOL\": \"configured\",\n"
        "                \"NCBI_API_KEY\": \"configured\",\n"
        "                \"SEMANTIC_SCHOLAR_API_KEY\": \"missing\",\n"
        "                \"OPENALEX_API_KEY\": \"configured\",\n"
        "                \"CROSSREF_MAILTO\": \"configured\"\n"
        "            },\n"
        "            \"providers\": {\"crossref\": {\n"
        "                \"connectivity\": \"connected\",\n"
        "                \"authentication\": \"configured\",\n"
        "                \"api_response_status\": \"200\",\n"
        "                \"status\": \"success\"\n"
        "            }}\n"
        "        }, sort_keys=True), encoding=\"utf-8\")\n"
        "        return 0\n"
        "    request = json.loads(Path(args.input).read_text(encoding=\"utf-8\"))\n",
    )
    runner_path.write_text(text, encoding="utf-8")
    return ecfinder_root


def _seed_control_plane(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO runs (
                run_id, status, completeness, date_from, date_to,
                current_iteration, current_query_id, accepted_query_id,
                current_state, started_at, completed_at, code_commit_sha,
                git_branch, config_hash, prompt_hash, protocol_version,
                scoring_version, model_version, source_status_json, failure_reason
            )
            VALUES (
                'run', 'running', 'not_evaluated', '2006-01-01', '2026-07-09',
                1, 'Q0001', NULL, 'SEARCH_SOURCES', '2026-07-09T00:00:00Z',
                NULL, 'test', 'test', 'test', 'test', 'test',
                'test', 'test', '{}', NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO query_iterations (
                run_id, iteration, query_id, parent_query_id, branch_id,
                query_status, acceptance_status, score, score_delta,
                saturation_status, query_known_cutoff, started_at, finalized_at,
                completion_checksum
            )
            VALUES (
                'run', 1, 'Q0001', NULL, 'main', 'running', 'pending',
                NULL, NULL, 'not_evaluated', 0, '2026-07-09T00:00:00Z',
                NULL, NULL
            )
            """
        )


def test_missing_ecfinder_env_is_clear_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ECFINDER_METADATA_SKILL_PATH", raising=False)
    gateway = ExternalMetadataDiscoveryGateway(
        repo_root=Path.cwd(), db_path=tmp_path / "state.sqlite3", code_commit_sha="test"
    )
    with pytest.raises(RuntimeError, match="ECFINDER_METADATA_SKILL_PATH is not set"):
        gateway.entrypoint()


def test_missing_runner_is_clear_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_dir = tmp_path / "ECfinder" / "skills" / "search" / "external_metadata_discovery"
    skill_dir.mkdir(parents=True)
    monkeypatch.setenv("ECFINDER_METADATA_SKILL_PATH", str(tmp_path / "ECfinder"))
    gateway = ExternalMetadataDiscoveryGateway(
        repo_root=Path.cwd(), db_path=tmp_path / "state.sqlite3", code_commit_sha="test"
    )
    with pytest.raises(FileNotFoundError, match="public executable runner"):
        gateway.entrypoint()


def test_gateway_invokes_skill_runner_without_provider_module_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ecfinder_root = _fake_ecfinder_skill(tmp_path / "ECfinder")
    monkeypatch.setenv("ECFINDER_METADATA_SKILL_PATH", str(ecfinder_root))
    db_path = tmp_path / "state.sqlite3"
    ControlPlane(db_path, "test").migrate()
    _seed_control_plane(db_path)
    run_dir = tmp_path / "runs" / "live"
    run_dir.mkdir(parents=True)
    gateway = ExternalMetadataDiscoveryGateway(
        repo_root=Path.cwd(), db_path=db_path, code_commit_sha="test"
    )

    entrypoint = gateway.entrypoint()
    assert entrypoint["entrypoint_type"] == "python_module_subprocess"
    assert entrypoint["entrypoint_module"] == "ecfinder.skills.external_metadata_discovery.runner"
    assert entrypoint["requires_external_worker"] is False
    assert entrypoint["provider_modules"] == []

    output = gateway.invoke(
        run_id="run",
        query=_query(),
        run_dir=run_dir,
        providers=["crossref"],
        page_size=2,
        max_candidates=1,
        max_scan_depth_per_provider=1,
    )
    summary = gateway.import_into_control_plane(
        run_id="run", query=_query(), discovery_output=output
    )

    request_ref = (
        run_dir / "external_metadata" / "Q0001" / "external_metadata_discovery_input.json"
    )
    request = json.loads(request_ref.read_text(encoding="utf-8"))
    assert request["skill_id"] == "external_metadata_discovery_v1"
    assert "agent_name" not in request
    assert request["date_from"] == "2006-01-01"
    assert request["date_to"] == "2026-07-09"
    assert request["document_types"] == ["journal article"]
    assert summary["raw_result_count"] == 1
    with sqlite3.connect(db_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM source_records").fetchone()[0]
    assert count == 1


def test_metadata_enrichment_gateway_reuses_matching_completed_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ecfinder_root = _fake_ecfinder_skill(tmp_path / "ECfinder")
    monkeypatch.setenv("ECFINDER_METADATA_SKILL_PATH", str(ecfinder_root))
    gateway = ExternalMetadataDiscoveryGateway(
        repo_root=Path.cwd(),
        db_path=tmp_path / "state.sqlite3",
        code_commit_sha="test",
    )
    invocation_count = 0

    def fake_run_skill_subprocess(self, *, root, input_ref, output_ref):  # noqa: ANN001
        nonlocal invocation_count
        del self, root
        invocation_count += 1
        request = json.loads(input_ref.read_text(encoding="utf-8"))
        output_root = Path(request["output_root"])
        candidates_ref = output_root / "candidates.jsonl"
        candidates_ref.write_text("", encoding="utf-8")
        source_status_ref = output_root / "source_status.json"
        provider_states_ref = output_root / "provider_states.json"
        memory_usage_ref = output_root / "memory_usage.json"
        source_status_ref.write_text('{"openalex":"no-results"}', encoding="utf-8")
        provider_states_ref.write_text("{}", encoding="utf-8")
        memory_usage_ref.write_text("{}", encoding="utf-8")
        output_ref.write_text(
            json.dumps(
                {
                    "candidates_ref": str(candidates_ref),
                    "deduped_ref": "",
                    "new_sources_ref": "",
                    "provider_page_refs": {"openalex": []},
                    "provider_states_ref": str(provider_states_ref),
                    "next_cursor_by_provider": {"openalex": ""},
                    "source_status_ref": str(source_status_ref),
                    "memory_usage_ref": str(memory_usage_ref),
                    "providers_attempted": ["openalex"],
                    "providers_available": ["openalex"],
                    "total_external_candidates": 0,
                    "execution_status": "success",
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(
        ExternalMetadataDiscoveryGateway,
        "_run_skill_subprocess",
        fake_run_skill_subprocess,
    )
    kwargs = {
        "run_id": "pool",
        "run_dir": tmp_path / "runs" / "pool",
        "query_id": "CANDIDATE_POOL_METADATA_ENRICHMENT_BATCH",
        "records": [
            {
                "candidate_pool_key": "doi:10.1/test",
                "doi": "10.1/test",
                "title": "Test record",
            }
        ],
        "output_root": tmp_path / "runs" / "pool" / "enrichment" / "batch",
        "providers": ["openalex"],
        "page_size": 1,
    }

    first = gateway.invoke_metadata_enrichment(**kwargs)
    second = gateway.invoke_metadata_enrichment(**kwargs)

    assert first["reused_completed_output"] is False
    assert second["reused_completed_output"] is True
    assert invocation_count == 1


def test_health_check_uses_external_skill_without_secret_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ecfinder_root = _fake_ecfinder_skill_with_health(tmp_path / "ECfinder")
    monkeypatch.setenv("ECFINDER_METADATA_SKILL_PATH", str(ecfinder_root))
    monkeypatch.setenv("NCBI_EMAIL", "operator@example.org")
    monkeypatch.setenv("NCBI_API_KEY", "secret")
    result = RunManager(repo_root=tmp_path).health_check()
    dumped = json.dumps(result, sort_keys=True)
    assert result["mode"] == "provider_health_check"
    assert result["environment_validation"]["NCBI_EMAIL"] == "configured"
    assert result["environment_validation"]["SEMANTIC_SCHOLAR_API_KEY"] == "missing"
    assert result["providers"]["crossref"]["connectivity"] == "connected"
    assert "secret" not in dumped
    assert "operator@example.org" not in dumped


def test_live_run_blocks_when_requested_provider_health_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = RunManager(repo_root=tmp_path)
    monkeypatch.setenv("ECFINDER_METADATA_SKILL_PATH", str(tmp_path / "ECfinder"))
    monkeypatch.setattr(
        manager,
        "health_check",
        lambda: {
            "mode": "provider_health_check",
            "environment_validation": {
                "NCBI_EMAIL": "configured",
                "SEMANTIC_SCHOLAR_API_KEY": "missing",
            },
            "providers": {
                "crossref": {
                    "status": "success",
                    "connectivity": "connected",
                    "authentication": "configured",
                    "api_response_status": "success",
                },
                "pubmed": {
                    "status": "failed",
                    "connectivity": "failed",
                    "authentication": "configured",
                    "api_response_status": "ncbi_blocked_html",
                    "error_classification": "ncbi_blocked_html",
                },
            },
        },
    )

    result = manager.live_run(date_to="2026-07-10", providers=["crossref", "pubmed"])

    assert result["status"] == "preflight_blocked"
    assert result["reason"] == "provider_health_preflight_failed"
    assert result["blocked_providers"]["pubmed"]["api_response_status"] == "ncbi_blocked_html"
    assert "crossref" not in result["blocked_providers"]


def test_live_run_allows_explicit_healthy_provider_subset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = RunManager(repo_root=tmp_path)
    monkeypatch.setenv("ECFINDER_METADATA_SKILL_PATH", str(tmp_path / "ECfinder"))
    monkeypatch.setattr(
        manager,
        "health_check",
        lambda: {
            "mode": "provider_health_check",
            "environment_validation": {"CROSSREF_MAILTO": "configured"},
            "providers": {
                "crossref": {
                    "status": "success",
                    "connectivity": "connected",
                    "authentication": "configured",
                    "api_response_status": "success",
                },
            },
        },
    )

    captured: dict[str, Any] = {}

    class FakeRunner:
        def run(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"status": "completed", "run_id": "run"}

    monkeypatch.setattr(manager, "_phase11_runner", lambda: FakeRunner())
    result = manager.live_run(date_to="2026-07-10", providers=["crossref"])

    assert result["status"] == "completed"
    assert captured["live_mode"] is True
    assert captured["providers"] == ["crossref"]


def test_live_run_without_explicit_providers_requires_degraded_acknowledgement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = RunManager(repo_root=tmp_path)
    monkeypatch.setenv("ECFINDER_METADATA_SKILL_PATH", str(tmp_path / "ECfinder"))
    monkeypatch.setattr(
        manager,
        "health_check",
        lambda: {
            "mode": "provider_health_check",
            "environment_validation": {"SEMANTIC_SCHOLAR_API_KEY": "missing"},
            "providers": {
                "crossref": {
                    "status": "success",
                    "connectivity": "connected",
                    "authentication": "configured",
                    "api_response_status": "success",
                },
                "openalex": {
                    "status": "success",
                    "connectivity": "connected",
                    "authentication": "configured",
                    "api_response_status": "success",
                },
                "semantic_scholar": {
                    "status": "rate-limited",
                    "connectivity": "connected",
                    "authentication": "anonymous",
                    "api_response_status": "rate_limited",
                    "error_classification": "rate_limited",
                },
                "pubmed": {
                    "status": "partial",
                    "connectivity": "connected",
                    "authentication": "configured",
                    "api_response_status": "eutils_blocked_pubmed_html_fallback",
                    "error_classification": "eutils_blocked_pubmed_html_fallback",
                    "record_count": 2,
                    "diagnostics": {"fallback": "pubmed_web_html"},
                },
            },
        },
    )

    result = manager.live_run(date_to="2026-07-10")

    assert result["status"] == "preflight_blocked"
    assert result["reason"] == "degraded_provider_set_requires_explicit_acknowledgement"
    assert result["required_acknowledgement"] == "allow_degraded_sources"
    assert result["selected_providers"] == ["crossref", "openalex", "pubmed"]
    assert "semantic_scholar" in result["unavailable_providers"]


def test_live_run_without_explicit_providers_can_acknowledge_degraded_subset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = RunManager(repo_root=tmp_path)
    monkeypatch.setenv("ECFINDER_METADATA_SKILL_PATH", str(tmp_path / "ECfinder"))
    monkeypatch.setattr(
        manager,
        "health_check",
        lambda: {
            "mode": "provider_health_check",
            "environment_validation": {"SEMANTIC_SCHOLAR_API_KEY": "missing"},
            "providers": {
                "crossref": {"status": "success"},
                "openalex": {"status": "success"},
                "semantic_scholar": {"status": "rate-limited"},
                "pubmed": {
                    "status": "partial",
                    "api_response_status": "eutils_blocked_pubmed_html_fallback",
                    "record_count": 2,
                    "diagnostics": {"fallback": "pubmed_web_html"},
                },
            },
        },
    )
    captured: dict[str, Any] = {}

    class FakeRunner:
        def run(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"status": "completed", "run_id": "run"}

    monkeypatch.setattr(manager, "_phase11_runner", lambda: FakeRunner())

    result = manager.live_run(
        date_to="2026-07-10", allow_degraded_sources=True
    )

    assert result["status"] == "completed"
    assert captured["providers"] == ["crossref", "openalex", "pubmed"]


def test_live_run_blocks_anonymous_semantic_scholar_even_when_health_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = RunManager(repo_root=tmp_path)
    monkeypatch.setenv("ECFINDER_METADATA_SKILL_PATH", str(tmp_path / "ECfinder"))
    monkeypatch.setattr(
        manager,
        "health_check",
        lambda: {
            "mode": "provider_health_check",
            "environment_validation": {"SEMANTIC_SCHOLAR_API_KEY": "missing"},
            "providers": {
                "crossref": {"status": "success"},
                "openalex": {"status": "success"},
                "semantic_scholar": {
                    "status": "success",
                    "authentication": "anonymous",
                },
                "pubmed": {"status": "success"},
            },
        },
    )

    result = manager.live_run(date_to="2026-07-10")

    assert result["status"] == "preflight_blocked"
    assert result["reason"] == "degraded_provider_set_requires_explicit_acknowledgement"
    assert result["required_acknowledgement"] == "allow_degraded_sources"
    assert result["unavailable_providers"]["semantic_scholar"]["status"] == (
        "configuration_required"
    )
    assert result["unavailable_providers"]["semantic_scholar"]["error_classification"] == (
        "missing_stable_api_key"
    )


def test_live_run_can_acknowledge_anonymous_semantic_scholar_risk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = RunManager(repo_root=tmp_path)
    monkeypatch.setenv("ECFINDER_METADATA_SKILL_PATH", str(tmp_path / "ECfinder"))
    monkeypatch.setattr(
        manager,
        "health_check",
        lambda: {
            "mode": "provider_health_check",
            "environment_validation": {"SEMANTIC_SCHOLAR_API_KEY": "missing"},
            "providers": {
                "crossref": {"status": "success"},
                "openalex": {"status": "success"},
                "semantic_scholar": {
                    "status": "success",
                    "authentication": "anonymous",
                },
                "pubmed": {"status": "success"},
            },
        },
    )
    captured: dict[str, Any] = {}

    class FakeRunner:
        def run(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"status": "completed", "run_id": "run"}

    monkeypatch.setattr(manager, "_phase11_runner", lambda: FakeRunner())

    result = manager.live_run(
        date_to="2026-07-10", allow_degraded_sources=True
    )

    assert result["status"] == "completed"
    assert captured["providers"] == ["crossref", "openalex", "pubmed"]


def test_live_preflight_reports_degraded_provider_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = RunManager(repo_root=tmp_path)
    monkeypatch.setenv("ECFINDER_METADATA_SKILL_PATH", str(tmp_path / "ECfinder"))
    monkeypatch.setattr(
        manager,
        "health_check",
        lambda: {
            "mode": "provider_health_check",
            "environment_validation": {"SEMANTIC_SCHOLAR_API_KEY": "missing"},
            "providers": {
                "crossref": {"status": "success"},
                "semantic_scholar": {
                    "status": "rate-limited",
                    "api_response_status": "rate_limited",
                    "error_classification": "rate_limited",
                },
            },
        },
    )

    result = manager._live_provider_preflight(  # noqa: SLF001
        ["crossref", "semantic_scholar"], strict=False
    )

    assert result["status"] == "ok"
    assert result["reason"] == "provider_health_preflight_degraded"
    assert result["selected_providers"] == ["crossref"]
    assert result["unavailable_providers"]["semantic_scholar"]["status"] == (
        "configuration_required"
    )
    assert result["unavailable_providers"]["semantic_scholar"]["error_classification"] == (
        "missing_stable_api_key"
    )


def test_live_preflight_defaults_to_degraded_subset_without_starting_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = RunManager(repo_root=tmp_path)
    monkeypatch.setenv("ECFINDER_METADATA_SKILL_PATH", str(tmp_path / "ECfinder"))
    monkeypatch.setattr(
        manager,
        "health_check",
        lambda: {
            "mode": "provider_health_check",
            "environment_validation": {"SEMANTIC_SCHOLAR_API_KEY": "missing"},
            "providers": {
                "crossref": {"status": "success"},
                "openalex": {"status": "success"},
                "semantic_scholar": {"status": "rate-limited"},
                "pubmed": {
                    "status": "partial",
                    "api_response_status": "eutils_blocked_pubmed_html_fallback",
                    "record_count": 1,
                    "diagnostics": {"fallback": "pubmed_web_html"},
                },
            },
        },
    )

    result = manager.live_preflight()

    assert result["status"] == "ok"
    assert result["reason"] == "provider_health_preflight_degraded"
    assert result["selected_providers"] == ["crossref", "openalex", "pubmed"]
    assert "semantic_scholar" in result["unavailable_providers"]
    assert result["unavailable_providers"]["semantic_scholar"]["status"] == (
        "configuration_required"
    )
    assert result["unavailable_providers"]["semantic_scholar"]["error_classification"] == (
        "missing_stable_api_key"
    )


def test_live_preflight_explicit_provider_is_strict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = RunManager(repo_root=tmp_path)
    monkeypatch.setenv("ECFINDER_METADATA_SKILL_PATH", str(tmp_path / "ECfinder"))
    monkeypatch.setattr(
        manager,
        "health_check",
        lambda: {
            "mode": "provider_health_check",
            "environment_validation": {"SEMANTIC_SCHOLAR_API_KEY": "missing"},
            "providers": {
                "semantic_scholar": {
                    "status": "rate-limited",
                    "api_response_status": "rate_limited",
                }
            },
        },
    )

    result = manager.live_preflight(providers=["semantic_scholar"])

    assert result["status"] == "blocked"
    assert result["reason"] == "provider_health_preflight_failed"
    assert result["blocked_providers"]["semantic_scholar"]["status"] == (
        "configuration_required"
    )
    assert result["blocked_providers"]["semantic_scholar"]["error_classification"] == (
        "missing_stable_api_key"
    )


def test_live_run_excludes_anonymous_semantic_scholar_even_if_health_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = RunManager(repo_root=tmp_path)
    monkeypatch.setenv("ECFINDER_METADATA_SKILL_PATH", str(tmp_path / "ECfinder"))
    monkeypatch.setattr(
        manager,
        "health_check",
        lambda: {
            "mode": "provider_health_check",
            "environment_validation": {"SEMANTIC_SCHOLAR_API_KEY": "missing"},
            "providers": {
                "crossref": {"status": "success"},
                "semantic_scholar": {
                    "status": "success",
                    "connectivity": "connected",
                    "authentication": "anonymous",
                    "api_response_status": "200",
                },
            },
        },
    )

    captured: dict[str, Any] = {}

    class FakeRunner:
        def run(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"status": "completed", "run_id": "run"}

    monkeypatch.setattr(manager, "_phase11_runner", lambda: FakeRunner())
    result = manager.live_run(
        date_to="2026-07-10",
        providers=["crossref", "semantic_scholar"],
        allow_degraded_sources=True,
    )

    assert result["status"] == "completed"
    assert captured["providers"] == ["crossref"]


def test_live_run_allows_pubmed_partial_html_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = RunManager(repo_root=tmp_path)
    monkeypatch.setenv("ECFINDER_METADATA_SKILL_PATH", str(tmp_path / "ECfinder"))
    monkeypatch.setattr(
        manager,
        "health_check",
        lambda: {
            "mode": "provider_health_check",
            "environment_validation": {"NCBI_EMAIL": "configured"},
            "providers": {
                "pubmed": {
                    "status": "partial",
                    "connectivity": "connected",
                    "authentication": "configured",
                    "api_response_status": "eutils_blocked_pubmed_html_fallback",
                    "error_classification": "eutils_blocked_pubmed_html_fallback",
                    "record_count": 1,
                    "diagnostics": {"fallback": "pubmed_web_html"},
                },
            },
        },
    )

    captured: dict[str, Any] = {}

    class FakeRunner:
        def run(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"status": "completed", "run_id": "run"}

    monkeypatch.setattr(manager, "_phase11_runner", lambda: FakeRunner())
    result = manager.live_run(date_to="2026-07-10", providers=["pubmed"])

    assert result["status"] == "completed"
    assert captured["live_mode"] is True
    assert captured["providers"] == ["pubmed"]


def test_live_run_blocks_pubmed_partial_without_usable_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = RunManager(repo_root=tmp_path)
    monkeypatch.setenv("ECFINDER_METADATA_SKILL_PATH", str(tmp_path / "ECfinder"))
    monkeypatch.setattr(
        manager,
        "health_check",
        lambda: {
            "mode": "provider_health_check",
            "providers": {
                "pubmed": {
                    "status": "partial",
                    "connectivity": "connected",
                    "authentication": "configured",
                    "api_response_status": "eutils_blocked_pubmed_html_fallback",
                    "record_count": 0,
                    "diagnostics": {"fallback": "pubmed_web_html"},
                },
            },
        },
    )

    result = manager.live_run(date_to="2026-07-10", providers=["pubmed"])

    assert result["status"] == "preflight_blocked"
    assert result["blocked_providers"]["pubmed"]["status"] == "partial"
    assert result["blocked_providers"]["pubmed"]["record_count"] == 0
    assert result["blocked_providers"]["pubmed"]["diagnostics"] == {
        "fallback": "pubmed_web_html"
    }


def test_live_preflight_reports_safe_provider_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = RunManager(repo_root=tmp_path)
    monkeypatch.setenv("ECFINDER_METADATA_SKILL_PATH", str(tmp_path / "ECfinder"))
    monkeypatch.setattr(
        manager,
        "health_check",
        lambda: {
            "mode": "provider_health_check",
            "providers": {
                "semantic_scholar": {
                    "status": "rate-limited",
                    "connectivity": "connected_rate_limited",
                    "authentication": "anonymous",
                    "api_response_status": "rate_limited",
                    "record_count": 0,
                    "error_classification": "rate_limited",
                    "diagnostics": {
                        "classification": "rate_limited",
                        "attempts": 3,
                        "retry_after": "2",
                        "response_snippet": "must not be surfaced",
                        "blocked_by_ncbi": True,
                        "final_url_host": "misuse.ncbi.nlm.nih.gov",
                        "final_url_path": "/error/abuse.shtml",
                        "suggested_action": "safe remediation text",
                    },
                }
            },
        },
    )

    result = manager.live_run(
        date_to="2026-07-10", providers=["semantic_scholar"]
    )

    blocked = result["blocked_providers"]["semantic_scholar"]
    assert result["status"] == "preflight_blocked"
    assert blocked["record_count"] == 0
    assert blocked["diagnostics"] == {
        "classification": "rate_limited",
        "attempts": 3,
        "retry_after": "2",
        "blocked_by_ncbi": True,
        "final_url_host": "misuse.ncbi.nlm.nih.gov",
        "final_url_path": "/error/abuse.shtml",
        "suggested_action": "safe remediation text",
    }
    assert "response_snippet" not in blocked["diagnostics"]


def test_discovery_dry_run_manifest_marks_external_skill_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.db",
    )

    def fake_search_sources_external(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["providers"] == ["crossref"]
        assert kwargs["max_records_per_provider"] == 2
        return {
            "raw_result_count": 0,
            "scanned_result_count": 0,
            "imported_pages": 0,
            "source_statuses": {"crossref": "success"},
            "providers_attempted": ["crossref"],
            "providers_available": ["crossref"],
            "total_external_candidates": 0,
            "execution_status": "success",
        }

    monkeypatch.setattr(
        runner, "_search_sources_external", fake_search_sources_external
    )

    result = runner.discovery_dry_run(
        date_to="2026-07-10",
        run_id="dry",
        providers=["crossref"],
        max_records_per_provider=2,
    )
    manifest = json.loads(
        (tmp_path / "runs" / "dry" / "manifest.json").read_text(encoding="utf-8")
    )

    assert result["status"] == "completed"
    assert manifest["run_mode"] == "live_discovery_dry_run"
    assert manifest["runtime_source_health_mode"] == "external_metadata_discovery_v1"
    assert manifest["live_providers"] == ["crossref"]
    assert manifest["run_completeness"] == "complete"


def test_discovery_dry_run_passes_runtime_date_from_to_q0001(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.db",
    )
    captured: dict[str, Any] = {}

    def fake_search_sources_external(**kwargs: Any) -> dict[str, Any]:
        query = kwargs["query"]
        captured["query_date_from"] = query.date_from
        captured["query_date_to"] = query.date_to
        return {
            "raw_result_count": 0,
            "scanned_result_count": 0,
            "imported_pages": 0,
            "source_statuses": {"crossref": "success"},
            "providers_attempted": ["crossref"],
            "providers_available": ["crossref"],
            "total_external_candidates": 0,
            "execution_status": "success",
        }

    monkeypatch.setattr(
        runner, "_search_sources_external", fake_search_sources_external
    )

    result = runner.discovery_dry_run(
        date_from="2010-01-01",
        date_to="2026-07-10",
        run_id="dry-date-from",
        providers=["crossref"],
        max_records_per_provider=2,
    )
    manifest = json.loads(
        (tmp_path / "runs" / "dry-date-from" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )

    assert result["date_from"] == "2010-01-01"
    assert manifest["date_from"] == "2010-01-01"
    assert captured == {
        "query_date_from": "2010-01-01",
        "query_date_to": "2026-07-10",
    }


def test_live_search_resume_reuses_search_sources_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.db",
    )
    query = _query()
    run_dir = tmp_path / "runs" / "run"
    checkpoint_dir = run_dir / "checkpoints"
    checkpoint_dir.mkdir(parents=True)
    payload = {
        "query_id": "Q0001",
        "raw_result_count": 2,
        "scanned_result_count": 2,
        "imported_pages": 1,
        "source_statuses": {"crossref": "success"},
        "providers_attempted": ["crossref"],
        "providers_available": ["crossref"],
        "total_external_candidates": 2,
        "execution_status": "success",
    }
    (checkpoint_dir / "SEARCH_SOURCES.json").write_text(
        json.dumps(
            {
                "state": "SEARCH_SOURCES",
                "timestamp": "2026-07-09T00:00:00Z",
                "payload": payload,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    def fail_invoke(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("resume must not invoke external metadata discovery again")

    monkeypatch.setattr(ExternalMetadataDiscoveryGateway, "invoke", fail_invoke)

    summary = runner._search_sources_external(  # noqa: SLF001
        run_id="run",
        run_dir=run_dir,
        query=query,
        runtime={"retrieval_page_size": 2},
        max_scan_depth=1,
        providers=["crossref"],
    )

    assert summary == payload


def test_live_search_resume_ignores_checkpoint_for_different_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.db",
    )
    query = _query(query_id="Q0002")
    run_dir = tmp_path / "runs" / "run"
    checkpoint_dir = run_dir / "checkpoints"
    checkpoint_dir.mkdir(parents=True)
    (checkpoint_dir / "SEARCH_SOURCES.json").write_text(
        json.dumps(
            {
                "state": "SEARCH_SOURCES",
                "timestamp": "2026-07-09T00:00:00Z",
                "payload": {
                    "query_id": "Q0001",
                    "raw_result_count": 2,
                    "scanned_result_count": 2,
                    "imported_pages": 1,
                    "source_statuses": {"crossref": "success"},
                    "providers_attempted": ["crossref"],
                    "providers_available": ["crossref"],
                    "total_external_candidates": 2,
                    "execution_status": "success",
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    fresh_output = {
        "source_status_ref": str(tmp_path / "source_status.json"),
        "providers_attempted": ["crossref"],
        "providers_available": ["crossref"],
        "total_external_candidates": 3,
        "execution_status": "success",
        "output_ref": str(tmp_path / "output.json"),
    }
    (tmp_path / "output.json").write_text("{}", encoding="utf-8")
    invoked: list[str] = []

    def fake_invoke(self: ExternalMetadataDiscoveryGateway, **kwargs: Any) -> dict[str, Any]:
        invoked.append(kwargs["query"].query_id)
        return fresh_output

    def fake_import(
        self: ExternalMetadataDiscoveryGateway, **kwargs: Any
    ) -> dict[str, Any]:
        return {
            "raw_result_count": 3,
            "scanned_result_count": 3,
            "imported_pages": 1,
            "source_statuses": {"crossref": "success"},
            "providers_attempted": ["crossref"],
            "providers_available": ["crossref"],
            "total_external_candidates": 3,
            "execution_status": "success",
        }

    monkeypatch.setattr(ExternalMetadataDiscoveryGateway, "invoke", fake_invoke)
    monkeypatch.setattr(
        ExternalMetadataDiscoveryGateway, "import_into_control_plane", fake_import
    )

    summary = runner._search_sources_external(  # noqa: SLF001
        run_id="run",
        run_dir=run_dir,
        query=query,
        runtime={"retrieval_page_size": 2},
        max_scan_depth=1,
        providers=["crossref"],
    )

    assert invoked == ["Q0002"]
    assert summary["query_id"] == "Q0002"
    assert summary["raw_result_count"] == 3


def test_live_resume_does_not_rewrite_search_sources_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.db",
    )
    query = _query()
    run_dir = tmp_path / "runs" / "run"
    checkpoint_dir = run_dir / "checkpoints"
    checkpoint_dir.mkdir(parents=True)
    payload = {
        "query_id": "Q0001",
        "raw_result_count": 2,
        "scanned_result_count": 2,
        "imported_pages": 1,
        "source_statuses": {"crossref": "success"},
        "providers_attempted": ["crossref"],
        "providers_available": ["crossref"],
        "total_external_candidates": 2,
        "execution_status": "success",
    }
    original_checkpoint = {
        "state": "SEARCH_SOURCES",
        "timestamp": "2026-07-09T00:00:00Z",
        "payload": payload,
    }
    (checkpoint_dir / "SEARCH_SOURCES.json").write_text(
        json.dumps(original_checkpoint, sort_keys=True),
        encoding="utf-8",
    )
    saved: list[tuple[str, dict[str, Any]]] = []

    def fake_save(self: CheckpointManager, state: str, payload: dict[str, Any]) -> None:
        saved.append((state, payload))

    monkeypatch.setattr(CheckpointManager, "save", fake_save)

    def fail_invoke(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("resume must not invoke external metadata discovery again")

    monkeypatch.setattr(ExternalMetadataDiscoveryGateway, "invoke", fail_invoke)
    monkeypatch.setattr(runner, "_start_query_iteration", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_write_query_artifacts", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_compile_query", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_update_live_source_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_normalize_and_register", lambda **kwargs: -1)

    result = runner._execute_iteration(  # noqa: SLF001
        run_id="run",
        run_dir=run_dir,
        query=query,
        plan=runner.variant_generator.plan(1),
        loaded=type("Loaded", (), {"protocol": {}, "stopping": {}, "scoring": {}})(),
        runtime={"retrieval_page_size": 2, "normalization_batch_size": 2},
        target_novel_records=1,
        max_scan_depth=1,
        machine=type(
            "Machine",
            (),
            {"state": None, "transition": lambda self, state: setattr(self, "state", state)},
        )(),
        checkpoints=CheckpointManager(run_dir),
        fail_after_operator=None,
        fail_after_batch=None,
        fail_after_source_page=None,
        stress_records_per_source=None,
        stress_mode=False,
        live_mode=True,
        providers=["crossref"],
    )

    assert result.saturation_status == "interrupted_injected"
    assert ("SEARCH_SOURCES", payload) not in saved
    checkpoint_after = json.loads(
        (checkpoint_dir / "SEARCH_SOURCES.json").read_text(encoding="utf-8")
    )
    assert checkpoint_after == original_checkpoint


def test_live_evaluate_query_uses_configured_target_novel_records(
    tmp_path: Path,
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.db",
    )
    run_id = "target-override"
    query = _query()
    ControlPlane(tmp_path / "control.db", "test").migrate()
    with sqlite3.connect(tmp_path / "control.db") as connection:
        connection.execute(
            """
            INSERT INTO runs (
                run_id, status, completeness, date_from, date_to, current_iteration,
                current_query_id, accepted_query_id, current_state, started_at,
                completed_at, code_commit_sha, git_branch, config_hash, prompt_hash,
                protocol_version, scoring_version, model_version, source_status_json,
                failure_reason
            )
            VALUES (
                ?, 'running', 'partial', '2006-01-01', '2026-07-09', 1,
                'Q0001', NULL, 'EVALUATE_QUERY', '2026-07-09T00:00:00Z',
                NULL, 'test', 'test', 'test', 'test', 'test', 'test', 'test',
                '{}', NULL
            )
            """,
            (run_id,),
        )
        connection.execute(
            """
            INSERT INTO document_query_membership (
                global_record_id, run_id, query_id, iteration, source_name,
                source_rank, first_seen_in_query, already_known_before_query,
                included_in_novelty_sample, novelty_sample_position,
                screening_status_at_iteration, created_at
            )
            VALUES (
                'doc-1', ?, 'Q0001', 1, 'crossref', 1, 1, 0, 1, 1,
                NULL, '2026-07-09T00:00:00Z'
            )
            """,
            (run_id,),
        )
        connection.commit()

    metrics = runner._evaluate_query(  # noqa: SLF001
        run_id=run_id,
        query=query,
        plan=runner.variant_generator.plan(1),
        loaded=type(
            "Loaded",
            (),
            {
                "stopping": {"evaluation": {"target_novel_records_total": 20}},
                "scoring": {
                    "positive_weights": {
                        "novel_precision_at_20": 0,
                        "normalized_novel_eligible_yield": 1,
                        "novelty_rate": 0,
                        "cross_source_breadth": 0,
                        "scope_diversity": 0,
                        "metadata_completeness": 0,
                    },
                    "penalty_weights": {
                        "defer_rate": 0,
                        "excluded_matrix_rate": 0,
                        "laboratory_study_rate": 0,
                        "no_concentration_rate": 0,
                        "known_ineligible_overlap_rate": 0,
                        "query_complexity": 0,
                    },
                },
                "config_hash": "test",
            },
        )(),
        source_counts={"raw_result_count": 1, "scanned_result_count": 1},
        duplicate_count=0,
        handoff_counts={
            "download_requests_emitted": 0,
            "duplicate_handoffs_suppressed": 0,
            "pending_download_jobs": 0,
            "handoff_backpressure_status": "ok",
        },
        target_novel_records=3,
    )

    assert metrics.target_novel_n == 3
    assert metrics.target_reached is False


def test_live_acceptance_reference_reads_source_completeness_from_audit_payload(
    tmp_path: Path,
) -> None:
    runner = Phase11Runner(
        repo_root=Path.cwd(),
        config_dir=Path.cwd() / "configs" / "retrieval",
        runs_dir=tmp_path / "runs",
        paper_exports_dir=tmp_path / "paper_exports",
        db_path=tmp_path / "control.db",
    )
    run_id = "reference-completeness"
    ControlPlane(tmp_path / "control.db", "test").migrate()
    with sqlite3.connect(tmp_path / "control.db") as connection:
        connection.execute(
            """
            INSERT INTO runs (
                run_id, status, completeness, date_from, date_to, current_iteration,
                current_query_id, accepted_query_id, current_state, started_at,
                completed_at, code_commit_sha, git_branch, config_hash, prompt_hash,
                protocol_version, scoring_version, model_version, source_status_json,
                failure_reason
            )
            VALUES (
                ?, 'running', 'partial', '2006-01-01', '2026-07-09', 1,
                'Q0001', 'Q0001', 'FINALIZE_ITERATION', '2026-07-09T00:00:00Z',
                NULL, 'test', 'test', 'test', 'test', 'test', 'test', 'test',
                '{}', NULL
            )
            """,
            (run_id,),
        )
        connection.execute(
            """
            INSERT INTO query_iterations (
                run_id, iteration, query_id, parent_query_id, branch_id,
                query_status, acceptance_status, score, score_delta,
                saturation_status, started_at, finalized_at
            )
            VALUES (
                ?, 1, 'Q0001', NULL, 'main', 'finalized', 'accepted',
                0.5, 0.5, 'not_saturated', '2026-07-09T00:00:00Z',
                '2026-07-09T00:00:00Z'
            )
            """,
            (run_id,),
        )
        payload = {
            "total_score": 0.5,
            "novel_precision_at_20": 0.5,
            "defer_rate": 0.1,
            "excluded_matrix_rate": 0.0,
            "source_completeness": "partial",
        }
        connection.execute(
            """
            INSERT INTO audit_events (
                audit_event_id, run_id, query_id, global_record_id, event_type,
                payload_json, actor, created_at
            )
            VALUES (
                'audit-1', ?, 'Q0001', NULL, 'query_metrics', ?,
                'QueryEvaluator', '2026-07-09T00:00:00Z'
            )
            """,
            (run_id, json.dumps(payload, sort_keys=True)),
        )
        connection.commit()

    reference = runner._live_acceptance_reference(run_id, "Q0001")  # noqa: SLF001

    assert reference is not None
    assert reference.source_completeness == "partial"
