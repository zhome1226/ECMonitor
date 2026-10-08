"""Left-censored (non-detect) data handling: prompt contract, review-list display.

Verifies the patches from ``docs/research/review_standards_research/left_censored_data_handling.md``:
(1) the extractor prompt requires capturing LOD/LOQ for censored results, (2) the validator prompt
requires censored records to keep the censoring qualifier and carry numeric limits when reported,
(3) the review-list builder categorizes censored/no-value records and displays the LOD/LOQ column.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ecmonitor.fulltext_extraction.models import (
    ChemicalMatch,
    ChemicalResolution,
    EvidenceChunk,
)
from ecmonitor.fulltext_extraction.quality import (
    ConservativeEvidenceValidator,
    evidence_quality_gate,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (PROJECT_ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Prompt contracts
# ---------------------------------------------------------------------------


def test_extractor_prompt_requires_lod_loq_capture_for_censored() -> None:
    prompt = _read("prompts/extraction/occurrence_extractor.md")
    assert "occurrence-extractor-v2.0" in prompt
    assert "analytical_method.lod_raw" in prompt
    assert "analytical_method.loq_raw" in prompt
    assert "not_detected" in prompt
    # Never invent a limit and never replace the censored value with 0 or LOD.
    assert "Never invent an LOD/LOQ" in prompt
    assert "document_context.detection_limit_context" in prompt


def test_validator_prompt_has_lod_loq_check_and_reason_codes() -> None:
    prompt = _read("prompts/extraction/evidence_validator.md")
    assert "evidence-validator-v2.0" in prompt
    assert "20. **censored results preserve detection/quantification semantics**" in prompt
    assert "censored_lod_loq_missing" in prompt
    assert "censored_lod_loq_invented" in prompt
    assert "lod_loq_overapplied" in prompt
    assert "document_context.detection_limit_context" in prompt


def test_config_versions_match_prompts() -> None:
    config = _read("configs/extraction/fulltext_extraction_v1.yaml")
    assert "prompt_version: occurrence-extractor-v2.0" in config
    assert "prompt_version: evidence-validator-v2.0" in config


# ---------------------------------------------------------------------------
# Deterministic gate still escalates censored records as non-concentrations
# ---------------------------------------------------------------------------


def _resolution() -> ChemicalResolution:
    return ChemicalResolution(
        raw_name="PFOA",
        normalized_query="pfoa",
        status="resolved",
        resolver_name="test",
        matches=(
            ChemicalMatch(
                source="test",
                source_record_id="9554",
                canonical_name="Perfluorooctanoic acid",
                matched_alias="PFOA",
            ),
        ),
    )


def _chunk() -> EvidenceChunk:
    return EvidenceChunk(
        chunk_id="c1",
        document_id="d1",
        ordinal=0,
        chunk_type="section_text",
        text="PFOA below the LOQ in river water.",
        page_start=1,
        page_end=1,
        source_block_ids=("b1",),
    )


def _candidate(**overrides):
    base = {
        "observation_type": "field_measurement",
        "analyte": {
            "raw_name": "PFOA",
            "specificity_status": "individual_chemical",
            "is_individual_chemical": True,
        },
        "result": {
            "raw_value": "<0.5 ng/L",
            "raw_unit": "ng/L",
            "value_numeric": None,
            "qualifier": "less_than",
            "statistic": "single",
        },
        "sample": {"matrix_raw": "surface water", "matrix_normalized": "surface_water"},
        "location": {"city": "Lyon", "country": "France"},
        "sampling_time": {"year": 2020, "basis": "reported", "approximate": False},
        "analytical_method": {"method_name": "LC-MS/MS", "lod_raw": "0.5", "loq_raw": "0.5"},
        "evidence": {"quote": "PFOA was below the LOQ (0.5 ng/L)."},
    }
    base.update(overrides)
    return base


def test_numeric_left_censoring_is_eligible_for_dedicated_stream() -> None:
    # The deterministic gate preserves a numeric threshold instead of treating it as qualitative ND.
    assert evidence_quality_gate(_candidate()) is None


def test_conservative_validator_keeps_numeric_left_censoring_for_pilot_signoff() -> None:
    validator = ConservativeEvidenceValidator()
    decision = validator.validate(_candidate(), chunk=_chunk(), resolutions=(_resolution(),))
    assert decision.action == "escalate"
    assert decision.reason_codes == ("pilot_human_signoff_required",)
    assert decision.human_review_required is True


# ---------------------------------------------------------------------------
# Review-list builder surfaces LOD/LOQ and categories for censored records
# ---------------------------------------------------------------------------


def test_build_review_list_categorizes_censored_and_shows_lod_loq(tmp_path: Path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_review_list", PROJECT_ROOT / "scripts" / "build_review_list.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    candidate = _candidate()
    assert module._is_censored(candidate)
    assert module._categorize(set(), candidate) == "no_value"
    lod_loq = module._lod_loq(candidate)
    assert "LOD=0.5" in lod_loq and "LOQ=0.5" in lod_loq

    # Build a tiny control DB and verify the generated Markdown includes the LOD/LOQ column.
    db_path = tmp_path / "state" / "control.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(
        """
        CREATE TABLE document_assets (
            document_id TEXT PRIMARY KEY, source_path TEXT, source_sha256 TEXT UNIQUE
        );
        CREATE TABLE document_sessions (
            document_session_id TEXT PRIMARY KEY, document_id TEXT,
            registry_snapshot_version INTEGER, status TEXT
        );
        CREATE TABLE human_review_tasks (
            task_id TEXT PRIMARY KEY, document_session_id TEXT,
            candidate_id TEXT, priority TEXT, reason_codes_json TEXT, payload_json TEXT, status TEXT
        );
        """
    )
    con.execute(
        "INSERT INTO document_assets VALUES (?,?,?)",
        ("d1", "10.1000_example.pdf", "abc"),
    )
    con.execute(
        "INSERT INTO document_sessions VALUES (?,?,?,?)",
        ("s1", "d1", 0, "committed"),
    )
    con.execute(
        "INSERT INTO human_review_tasks VALUES (?,?,?,?,?,?,?)",
        (
            "task-00000000-0000-0000-0000-000000000001",
            "s1",
            "c1",
            "medium",
            json.dumps(["censored_no_measured_concentration"]),
            json.dumps({"candidate": candidate}),
            "pending",
        ),
    )
    con.commit()
    con.close()

    md, counts = module.build(tmp_path)
    assert counts["no_value"] == 1
    assert "LOD/LOQ" in md
    assert "LOD=0.5" in md
    assert "not concentrations" in md
