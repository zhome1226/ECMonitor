from pathlib import Path

import pytest

from ecmonitor.fulltext_extraction.models import ChemicalMatch, ChemicalResolution
from ecmonitor.fulltext_extraction.registry import ChemicalRegistry


def _resolution() -> ChemicalResolution:
    return ChemicalResolution(
        raw_name="PFOA",
        normalized_query="PFOA",
        status="resolved",
        resolver_name="mock_pubchem",
        matches=(
            ChemicalMatch(
                source="PubChem",
                source_record_id="9554",
                canonical_name="Perfluorooctanoic acid",
                matched_alias="PFOA",
                pubchem_cid="9554",
                inchikey="SNGREZUHAYWORS-UHFFFAOYSA-N",
            ),
        ),
    )


def test_proposed_alias_is_invisible_until_promoted(tmp_path: Path) -> None:
    registry = ChemicalRegistry(tmp_path / "registry.sqlite3")
    proposal_id = registry.record_proposal(
        document_id="doc", document_session_id="session", resolution=_resolution()
    )
    assert registry.lookup_validated("PFOA") is None
    assert registry.version() == 0
    new_version = registry.promote_proposal(
        proposal_id,
        actor="human:test",
        alias_type="abbreviation",
        is_individual_chemical=True,
    )
    resolved = registry.lookup_validated("pfoa")
    assert new_version == 1
    assert resolved is not None
    assert resolved.status == "validated_local"
    assert resolved.matches[0].canonical_name == "Perfluorooctanoic acid"
    assert registry.lookup_validated("PFOA", at_version=0) is None
    assert registry.lookup_validated("PFOA", at_version=1) is not None


def test_non_individual_alias_cannot_be_promoted(tmp_path: Path) -> None:
    registry = ChemicalRegistry(tmp_path / "registry.sqlite3")
    proposal_id = registry.record_proposal(
        document_id="doc", document_session_id="session", resolution=_resolution()
    )
    with pytest.raises(ValueError, match="non-individual"):
        registry.promote_proposal(
            proposal_id,
            actor="human:test",
            alias_type="class_name",
            is_individual_chemical=False,
        )
