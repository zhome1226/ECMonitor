"""Project-scope gate: heavy metals + transformation products are IN scope.

Domain decision 2026-08: heavy metals AND their transformation/ionic forms count as emerging
contaminants, so only unambiguous classic water-quality parameters (major nutrients/ions,
dissolved-oxygen/COD/BOD/TOC aggregates, conductivity/turbidity, major background cations
Na/K/Ca/Mg) are rejected by the deterministic gate. Organometallic organics and
transformation/intermediate products are in scope.
"""

from __future__ import annotations

from ecmonitor.fulltext_extraction.quality import (
    PolicyGatedEvidenceValidator,
    looks_out_of_scope_water_quality,
)


def _candidate(name: str) -> dict:
    return {
        "analyte": {
            "raw_name": name,
            "reported_name": name,
            "canonical_name": None,
            "specificity_status": "single_substance",
            "is_individual_chemical": True,
        },
        "result": {"raw_value": "1.2", "unit": "ug/L"},
    }


def test_heavy_metals_are_in_scope() -> None:
    # Heavy metals and their ionic/valence forms count as emerging contaminants.
    for name in [
        "Cd",
        "cadmium",
        "Al",
        "Aluminium",
        "lead",
        "Hg",
        "mercury",
        "total chromium",
        "dissolved Cu",
        "Fe(II)",
        "Zn2+",
        "Fe3+",
        "As",
        "Cr(VI)",
    ]:
        assert not looks_out_of_scope_water_quality(_candidate(name)), name


def test_transformation_products_are_in_scope() -> None:
    # Transformation/intermediate products and organometallic organics stay in scope.
    for name in ["methylmercury", "tributyltin", "trimethyltin", "desnitro-imidacloprid", "atrazine-desethyl"]:
        assert not looks_out_of_scope_water_quality(_candidate(name)), name


def test_looks_out_of_scope_water_quality_detects_classic_parameters() -> None:
    # Classic water-quality parameters that are not contaminants are excluded.
    for name in [
        "nitrate",
        "sulfate",
        "chloride",
        "ammonium",
        "phosphate",
        "dissolved oxygen",
        "conductivity",
        "turbidity",
        "salinity",
        "total organic carbon",
        "Na+",
        "K+",
        "Ca2+",
        "NH4+",
        "NO3-",
        "PO4 3-",
    ]:
        assert looks_out_of_scope_water_quality(_candidate(name)), name


def test_scope_gate_rejects_classic_parameter_without_model_call() -> None:
    decision = PolicyGatedEvidenceValidator._deterministic_decision(
        _candidate("nitrate"), ()
    )
    assert decision.action == "reject"
    assert "classic_water_quality_parameter_out_of_scope" in decision.reason_codes


def test_scope_gate_lets_heavy_metal_reach_resolution_check() -> None:
    # A heavy metal is in scope, so with no resolutions it escalates (resolution missing)
    # rather than being rejected by the scope gate.
    decision = PolicyGatedEvidenceValidator._deterministic_decision(
        _candidate("Cd"), ()
    )
    assert decision.action == "escalate"
    assert "chemical_resolution_missing" in decision.reason_codes
