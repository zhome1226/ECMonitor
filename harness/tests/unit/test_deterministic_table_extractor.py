from __future__ import annotations

from ecmonitor.fulltext_extraction.adapters.deterministic_table import (
    DeterministicTableCandidateExtractor,
)
from ecmonitor.fulltext_extraction.harness import _sampling_context
from ecmonitor.fulltext_extraction.models import EvidenceChunk


def _chunk(text: str, *, chunk_type: str = "table_row_window") -> EvidenceChunk:
    return EvidenceChunk(
        chunk_id="chunk-1",
        document_id="10.test/example",
        ordinal=0,
        chunk_type=chunk_type,
        text=text,
        page_start=1,
        page_end=1,
        source_block_ids=("block-1",),
    )


def test_sampling_context_accepts_sampling_locations_and_reported_date_range() -> None:
    chunk = _chunk(
        "Sampling locations are shown in Fig. 1. Surface water samples were collected "
        "in the dry season in December, 2006 and February, 2007.",
        chunk_type="body",
    )
    context = _sampling_context([chunk])
    extractor = DeterministicTableCandidateExtractor()
    extractor.document_context = {"sampling_context": context, "publication_year": 2008}

    assert "December, 2006" in context
    assert extractor._sampling_time() == {
        "raw_text": "December 2006 and February 2007",
        "year": 2006,
        "basis": "reported",
        "approximate": True,
        "date_start": "December 2006",
        "date_end": "February 2007",
    }


def test_sampling_time_ignores_journal_lifecycle_dates() -> None:
    extractor = DeterministicTableCandidateExtractor()
    extractor.document_context = {
        "sampling_context": "Received December 2017. Samples were collected in spring 2016. Accepted May 2018.",
        "publication_year": 2018,
    }

    assert extractor._sampling_time() == {
        "raw_text": "spring 2016",
        "year": 2016,
        "basis": "reported",
        "approximate": True,
    }


def test_method_preserves_real_hyphens_and_repairs_only_line_wraps() -> None:
    extractor = DeterministicTableCandidateExtractor()
    extractor.document_context = {
        "analytical_method_context": (
            "Analytes were determined by solid-phase extraction high-performance liquid chro-\n"
            "matography."
        )
    }

    assert extractor._method("OFX") == {
        "method_name": "high-performance liquid chromatography",
        "sample_preparation": "solid-phase extraction (SPE)",
        "instrument": "HPLC",
    }


def test_method_combines_focused_and_other_bounded_context() -> None:
    extractor = DeterministicTableCandidateExtractor()
    extractor.document_context = {
        "analytical_method_context": "Instrumentation used LC-ESI-QTOF-MS.",
        "chemical_identity_context": "Samples underwent solid-phase extraction using Oasis HLB disks.",
    }

    assert extractor._method("Atrazine") == {
        "method_name": "LC-ESI-QTOF-MS",
        "sample_preparation": "solid-phase extraction (SPE)",
        "instrument": "LC-QTOF-MS",
    }


def test_censored_and_detection_limit_tokens_are_not_observations() -> None:
    extractor = DeterministicTableCandidateExtractor()

    for raw in ("nd", "n.d.", "LOD", "LOQ", "<LOD", "<LOQ", "not detected"):
        assert extractor._numeric(raw) is None


def test_total_or_family_labels_are_not_individual_chemical_candidates() -> None:
    extractor = DeterministicTableCandidateExtractor()
    candidate = extractor._candidate(
        chunk=_chunk("POSITIONAL_TABLE page=1 table=1\nTABLE_CAPTION Results (ng/L)"),
        reported_analyte="total PFAS",
        raw_value="12.4",
        unit="ng/L",
        site_name="S1",
        waterbody="River Test",
        location_raw="S1, River Test",
        row_label="total PFAS",
        column_label="S1",
        quote="total PFAS at S1 = 12.4 ng/L",
    )

    assert candidate is None


def test_one_candidate_contains_one_numeric_statistic_and_approximate_flag() -> None:
    extractor = DeterministicTableCandidateExtractor()
    extractor.document_context = {"sampling_context": "Samples were collected in May 2019."}
    candidate = extractor._candidate(
        chunk=_chunk("POSITIONAL_TABLE page=1 table=2\nTABLE_CAPTION Results (ng/L)"),
        reported_analyte="Caffeine",
        raw_value="1.2$",
        unit="ng/L",
        site_name="S1",
        waterbody="River Test",
        location_raw="S1, River Test",
        row_label="Caffeine",
        column_label="S1",
        quote="Caffeine at S1 = 1.2$ ng/L",
    )

    assert candidate is not None
    assert candidate["result"] == {
        "raw_value": "1.2$",
        "raw_unit": "ng/L",
        "value_numeric": 1.2,
        "qualifier": "approximate",
        "statistic": "single",
    }
    assert "approximate_value_from_table_footnote" in candidate["quality_flags"]


def test_secondary_derived_comparison_table_is_rejected_wholesale() -> None:
    extractor = DeterministicTableCandidateExtractor()
    chunk = _chunk(
        "POSITIONAL_TABLE page=8 table=4\n"
        "TABLE_CAPTION Previously reported values used to calculate E2eq′ Include NP and OP\n"
        "POSROW 1: 10::aChen | 20::et | 30::al.\n"
        "POSROW 2: 10::Sites | 100::BPA\n"
        "POSROW 3: 10::S1 | 100::43.5"
    )

    assert extractor.extract(chunk) == []
