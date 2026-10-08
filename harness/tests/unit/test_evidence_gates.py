"""Tests for deterministic evidence-anchor gates and text normalization (P0/P1a/P1b)."""
import sqlite3
from pathlib import Path
from typing import Any

from ecmonitor.fulltext_extraction.evidence import (
    _numeric_cores,
    evidence_anchor_reason,
    fold_for_evidence,
    normalize_document_text,
    normalize_parsed,
)
from ecmonitor.fulltext_extraction.harness import FulltextExtractionHarness
from ecmonitor.fulltext_extraction.models import (
    ParsedDocument,
    ParsedPage,
    TextBlock,
)
from ecmonitor.fulltext_extraction.registry import ChemicalRegistry
from ecmonitor.fulltext_extraction.storage import FulltextControlPlane


def test_normalize_document_text_repairs_unit_and_superscripts() -> None:
    assert normalize_document_text("concentration was 5 µg/L") == "concentration was 5 µg/L"
    assert normalize_document_text("5 lg/L") == "5 µg/L"
    assert normalize_document_text("0.5 ng·g⁻¹ w.w.") == "0.5 ng·g-1 w.w."
    assert normalize_document_text("NO₃⁻ in water") == "NO3- in water"


def test_normalize_document_text_does_not_touch_words() -> None:
    # "log/" and "logistical" must not be damaged by the lg/->µg/ repair.
    assert normalize_document_text("log/ and catalog") == "log/ and catalog"


def test_fold_for_evidence() -> None:
    folded = fold_for_evidence("Mean: 0,385 ± 0.172  ng·g⁻¹ wet weight")
    assert "0.385" in folded
    assert " " not in folded


def test_numeric_cores() -> None:
    assert "0.385" in _numeric_cores("0.385 ± 0.172")
    assert "0.037" in _numeric_cores("nd–0.037")
    assert "1.70" in _numeric_cores("1.70 × 10⁻⁴")


def test_evidence_anchor_reason() -> None:
    chunk = fold_for_evidence("PFOA measured at 5.2 ng/L in river water.")
    supported = {
        "result": {"qualifier": "exact", "raw_value": "5.2 ng/L"},
    }
    assert evidence_anchor_reason(supported, chunk) is None
    hallucinated = {
        "result": {"qualifier": "exact", "raw_value": "999.9 ng/L"},
    }
    reason = evidence_anchor_reason(hallucinated, chunk)
    assert reason is not None and reason.startswith("value_not_found_in_text")
    # ND markers and empty results are skipped.
    assert (
        evidence_anchor_reason({"result": {"qualifier": "not_detected", "raw_value": "nd"}}, chunk)
        is None
    )
    assert evidence_anchor_reason({"result": {}}, chunk) is None


def test_normalize_parsed_preserves_structure() -> None:
    parsed = ParsedDocument(
        document_id="sha256:x",
        source_path=Path("paper.pdf"),
        source_sha256="x",
        parser_name="mock",
        parser_version="1",
        pages=(
            ParsedPage(
                page_number=1,
                width=100,
                height=100,
                blocks=(TextBlock("b1", 1, "value 5 lg/L"),),
            ),
        ),
    )
    out = normalize_parsed(parsed)
    assert out.pages[0].blocks[0].text == "value 5 µg/L"
    assert out.document_id == parsed.document_id


# ---------------------------------------------------------------------------
# Harness integration
# ---------------------------------------------------------------------------

class AnchorMockParser:
    parser_name = "mock"

    def parse(self, pdf_path: Path, *, document_id: str, source_sha256: str) -> ParsedDocument:
        return ParsedDocument(
            document_id=document_id,
            source_path=pdf_path,
            source_sha256=source_sha256,
            parser_name="mock",
            parser_version="1",
            pages=(
                ParsedPage(
                    page_number=1,
                    width=100,
                    height=100,
                    blocks=(
                        TextBlock(
                            "p1-b1",
                            1,
                            "PFOA was measured at 5.2 ng/L in river water near the plant.",
                        ),
                    ),
                ),
            ),
        )


class AnchorMockExtractor:
    extractor_name = "mock"

    def extract(self, chunk: Any) -> list[dict[str, Any]]:
        del chunk
        return [
            {
                "analyte": {"raw_name": "PFOA", "is_individual_chemical": True},
                "result": {"raw_value": "5.2 ng/L", "qualifier": "exact", "statistic": "mean"},
                "evidence": {"quote": "PFOA was measured at 5.2 ng/L"},
            },
            {
                "analyte": {"raw_name": "PFOA", "is_individual_chemical": True},
                "result": {"raw_value": "999.9 ng/L", "qualifier": "exact", "statistic": "mean"},
                "evidence": {"quote": "concentrations were reported in the same study"},
            },
        ]


class CountingValidator:
    calls = 0

    def validate(self, candidate: dict[str, Any], **kwargs: Any) -> Any:
        type(self).calls += 1
        from ecmonitor.fulltext_extraction.models import ValidationDecision

        return ValidationDecision(
            action="escalate", reason_codes=("pilot_human_signoff_required",), human_review_required=True
        )


def test_hallucination_suspect_bypasses_model_review(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-mock")
    plane = FulltextControlPlane(tmp_path / "state.sqlite3")
    validator = CountingValidator()
    CountingValidator.calls = 0
    harness = FulltextExtractionHarness(
        parser=AnchorMockParser(),
        control_plane=plane,
        chemical_registry=ChemicalRegistry(tmp_path / "registry.sqlite3"),
        output_dir=tmp_path / "runs",
        extractor=AnchorMockExtractor(),
        validator=validator,
        merge_chunks_for_extraction=True,
    )
    report = harness.run_document(pdf)
    assert report.committed is True
    assert report.candidate_count == 2
    assert validator.calls == 1  # only the supported candidate hit the model validator
    con = sqlite3.connect(tmp_path / "state.sqlite3")
    rows = con.execute("SELECT payload_json FROM extraction_candidates").fetchall()
    flagged = [r[0] for r in rows if "hallucination_suspect" in r[0]]
    supported = [r[0] for r in rows if "hallucination_suspect" not in r[0]]
    assert len(flagged) == 1
    assert "999.9" in flagged[0]
    assert "5.2" in supported[0]
    con.close()


def test_pubchem_skipped_for_non_individual(tmp_path: Path) -> None:
    from ecmonitor.fulltext_extraction.models import ChemicalMatch, ChemicalResolution

    class ResolverSpy:
        resolver_name = "spy"
        calls = 0

        def resolve(self, raw_name: str) -> ChemicalResolution:
            type(self).calls += 1
            return ChemicalResolution(
                raw_name=raw_name,
                normalized_query=raw_name,
                status="resolved",
                resolver_name=self.resolver_name,
                matches=(ChemicalMatch(source="spy", source_record_id="1", canonical_name="X", matched_alias="X"),),
            )

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-mock")
    plane = FulltextControlPlane(tmp_path / "state.sqlite3")

    class NonIndividualExtractor:
        extractor_name = "nonind"
        total_calls = 0

        def extract(self, chunk: Any) -> list[dict[str, Any]]:
            type(self).total_calls += 1
            return [
                {
                    "analyte": {
                        "raw_name": "Total antibiotics",
                        "is_individual_chemical": False,
                        "specificity_status": "sum_or_total_parameter",
                    },
                    "result": {"raw_value": "15 ng/L", "qualifier": "range", "statistic": "range"},
                    "evidence": {"quote": "Total antibiotics ranged from 15 to 400 ng/L"},
                },
                {
                    "analyte": {"raw_name": "PFOA", "is_individual_chemical": True},
                    "result": {"raw_value": "5.2 ng/L", "qualifier": "exact", "statistic": "mean"},
                    "evidence": {"quote": "PFOA 5.2 ng/L"},
                },
            ]

    resolver = ResolverSpy()
    harness = FulltextExtractionHarness(
        parser=AnchorMockParser(),
        control_plane=plane,
        chemical_registry=ChemicalRegistry(tmp_path / "registry.sqlite3"),
        output_dir=tmp_path / "runs",
        extractor=NonIndividualExtractor(),
        chemical_resolver=resolver,
        merge_chunks_for_extraction=True,
    )
    harness.run_document(pdf)
    # Only the individual chemical should have reached the external resolver.
    assert ResolverSpy.calls == 1



def test_concentration_hit_spans_detects_concentration_text() -> None:
    from ecmonitor.fulltext_extraction.evidence import concentration_hit_spans

    assert concentration_hit_spans("PFOA measured at 12.5 ng/L in river water")
    assert concentration_hit_spans("ranged nd-5.2 µg/L across sites")
    assert concentration_hit_spans("PFOS 0.5-1.2 µg/L; PFOA 0.08 µg/kg wet weight")
    assert concentration_hit_spans("below 0.01 ng/L at all stations")
    assert concentration_hit_spans("1.2 x 10-4 ng/L")  # normalized scientific notation


def test_concentration_hit_spans_empty_without_signal() -> None:
    from ecmonitor.fulltext_extraction.evidence import concentration_hit_spans

    assert not concentration_hit_spans("We found no pesticide residues in any sample.")
    assert not concentration_hit_spans("The sampling campaign lasted six months.")
    assert not concentration_hit_spans("")


def test_concentration_hit_spans_merge_and_cap() -> None:
    from ecmonitor.fulltext_extraction.evidence import concentration_hit_spans

    # Many nearby hits collapse into one merged span.
    spans = concentration_hit_spans(
        "A 1.2 ng/L; B 2.3 ng/L; C 3.4 ng/L; D 4.5 ng/L; E 5.6 ng/L", padding=10
    )
    assert len(spans) == 1
    # The cap limits how many regions are returned (hits are kept well separated so they do
    # not merge into one span).
    text = " ".join(f"{i} ng/L " + "x" * 80 for i in range(100))
    capped = concentration_hit_spans(text, max_hits=3, padding=5)
    assert len(capped) == 3
