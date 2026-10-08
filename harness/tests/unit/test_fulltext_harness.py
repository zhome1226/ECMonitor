import sqlite3
from pathlib import Path
from typing import Any

from ecmonitor.fulltext_extraction.harness import (
    FulltextExtractionHarness,
    _candidate_lookup_names,
    _chemical_identity_context,
    _curated_specific_identity_resolution,
    _dedupe_payloads,
    _is_validator_failure,
    _split_chunks_for_extraction,
    _whole_doc_empty_fallback_decision,
)
from ecmonitor.fulltext_extraction.models import (
    ChemicalMatch,
    ChemicalResolution,
    EvidenceChunk,
    ParsedDocument,
    ParsedPage,
    TextBlock,
)
from ecmonitor.fulltext_extraction.registry import ChemicalRegistry
from ecmonitor.fulltext_extraction.storage import FulltextControlPlane


class MockParser:
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
                    blocks=(TextBlock("p1-b1", 1, "PFOA was measured in river water."),),
                ),
            ),
        )


class MockExtractor:
    extractor_name = "mock"

    def extract(self, chunk: Any) -> list[dict[str, Any]]:
        return [{"analyte": {"raw_name": "PFOA"}, "evidence": {}}]


class FlushTrackingResolver:
    resolver_name = "flush_tracking"

    def __init__(self) -> None:
        self.flush_calls = 0

    def resolve(self, raw_name: str) -> ChemicalResolution:
        return ChemicalResolution(
            raw_name=raw_name,
            normalized_query=raw_name,
            status="not_found",
            resolver_name=self.resolver_name,
        )

    def flush(self) -> None:
        self.flush_calls += 1


def test_harness_commits_one_document_and_writes_report(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-mock")
    plane = FulltextControlPlane(tmp_path / "state.sqlite3")
    harness = FulltextExtractionHarness(
        parser=MockParser(),
        control_plane=plane,
        chemical_registry=ChemicalRegistry(tmp_path / "registry.sqlite3"),
        output_dir=tmp_path / "runs",
        extractor=MockExtractor(),
    )
    report = harness.run_document(pdf)
    assert report.committed is True
    assert report.candidate_count == 1
    assert report.review_count == 1
    assert Path(report.output_path or "").is_file()
    status = plane.status()
    assert status["integrity"] == "ok"
    assert status["table_counts"]["document_sessions"] == 1
    assert status["table_counts"]["evidence_chunks"] == 1


def test_harness_flushes_shared_chemical_cache_at_document_barrier(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-mock")
    resolver = FlushTrackingResolver()
    harness = FulltextExtractionHarness(
        parser=MockParser(),
        control_plane=FulltextControlPlane(tmp_path / "state.sqlite3"),
        chemical_registry=ChemicalRegistry(tmp_path / "registry.sqlite3"),
        output_dir=tmp_path / "runs",
        chemical_resolver=resolver,
        extractor=MockExtractor(),
    )

    report = harness.run_document(pdf)

    assert report.committed is True
    assert resolver.flush_calls == 1


def test_same_document_can_run_in_a_fresh_session(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-mock")
    plane = FulltextControlPlane(tmp_path / "state.sqlite3")
    harness = FulltextExtractionHarness(
        parser=MockParser(),
        control_plane=plane,
        chemical_registry=ChemicalRegistry(tmp_path / "registry.sqlite3"),
        output_dir=tmp_path / "runs",
        extractor=MockExtractor(),
    )
    first = harness.run_document(pdf)
    second = harness.run_document(pdf)
    assert first.document_session_id != second.document_session_id
    assert first.output_path != second.output_path
    assert Path(first.output_path or "").is_file()
    assert Path(second.output_path or "").is_file()
    status = plane.status()
    assert status["table_counts"]["document_assets"] == 1
    assert status["table_counts"]["document_sessions"] == 2
    assert status["table_counts"]["evidence_chunks"] == 2


class CompositeNameExtractor:
    extractor_name = "composite-name"

    def extract(self, chunk: Any) -> list[dict[str, Any]]:
        del chunk
        return [
            {
                "analyte": {
                    "raw_name": "serum cortisol",
                    "proposed_canonical_name": "cortisol",
                },
                "evidence": {},
            }
        ]


class FallbackChemicalResolver:
    resolver_name = "fake"

    def __init__(self) -> None:
        self.queries: list[str] = []

    def resolve(self, raw_name: str) -> ChemicalResolution:
        self.queries.append(raw_name)
        if raw_name == "serum cortisol":
            return ChemicalResolution(
                raw_name=raw_name,
                normalized_query=raw_name,
                status="not_found",
                resolver_name=self.resolver_name,
            )
        return ChemicalResolution(
            raw_name=raw_name,
            normalized_query=raw_name,
            status="resolved",
            resolver_name=self.resolver_name,
            matches=(
                ChemicalMatch(
                    source="fake",
                    source_record_id="1",
                    canonical_name="Cortisol",
                    matched_alias="cortisol",
                ),
            ),
        )


def test_harness_falls_back_to_proposed_canonical_name_for_composite_mentions(
    tmp_path: Path,
) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-mock")
    resolver = FallbackChemicalResolver()
    plane = FulltextControlPlane(tmp_path / "state.sqlite3")
    harness = FulltextExtractionHarness(
        parser=MockParser(),
        control_plane=plane,
        chemical_registry=ChemicalRegistry(tmp_path / "registry.sqlite3"),
        output_dir=tmp_path / "runs",
        extractor=CompositeNameExtractor(),
        chemical_resolver=resolver,
    )

    report = harness.run_document(pdf)

    assert report.committed is True
    assert resolver.queries == ["serum cortisol", "cortisol"]
    with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
        row = connection.execute(
            "SELECT canonical_name, reported_name, replacement_name "
            "FROM observation_records"
        ).fetchone()
    assert row == ("Cortisol", "serum cortisol", "serum cortisol")


def test_harness_parallel_chunks_merge_in_order(tmp_path: Path) -> None:
    """chunk_parallelism>1 must process every selected chunk and keep commit ordering."""

    class MultiChunkParser(MockParser):
        def parse(self, pdf_path, *, document_id, source_sha256):
            pages = []
            for page_number in (1, 2):
                blocks = tuple(
                    TextBlock(
                        f"p{page_number}-b{i}",
                        page_number,
                        f"Atrazine was detected at {i * 10} ng/L in river water.\n" * 170,
                    )
                    for i in range(2)
                )
                pages.append(
                    ParsedPage(page_number=page_number, width=100, height=100, blocks=blocks)
                )
            return ParsedDocument(
                document_id=document_id,
                source_path=pdf_path,
                source_sha256=source_sha256,
                parser_name="mock",
                parser_version="1",
                pages=tuple(pages),
            )

    class OrderExtractor:
        extractor_name = "mock"
        def extract(self, chunk: Any) -> list[dict[str, Any]]:
            # Emulate a slow-ish per-chunk model call; two chunks interleave.
            import time
            time.sleep(0.05)
            return [{"analyte": {"raw_name": f"compound-{chunk.ordinal}"}, "evidence": {}}]

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-mock")
    plane = FulltextControlPlane(tmp_path / "state.sqlite3")
    harness = FulltextExtractionHarness(
        parser=MultiChunkParser(),
        control_plane=plane,
        chemical_registry=ChemicalRegistry(tmp_path / "registry.sqlite3"),
        output_dir=tmp_path / "runs",
        extractor=OrderExtractor(),
        chunk_parallelism=2,
    )
    report = harness.run_document(pdf)
    assert report.committed is True
    assert report.chunk_count == 4
    assert report.candidate_count == 4
    assert report.review_count == 4
    # Candidate rows must be committed in chunk order (ordinal 0..3).
    with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
        rows = connection.execute(
            "SELECT payload_json FROM extraction_candidates ORDER BY rowid"
        ).fetchall()
    names = [__import__("json").loads(r[0])["analyte"]["raw_name"] for r in rows]
    assert names == ["compound-0", "compound-1", "compound-2", "compound-3"]


def test_harness_whole_document_single_call_bypasses_prefilter(tmp_path: Path) -> None:
    """merge_chunks_for_extraction sends the whole document in one extractor call."""

    class MultiChunkParser(MockParser):
        def parse(self, pdf_path, *, document_id, source_sha256):
            pages = []
            for page_number in (1, 2):
                blocks = tuple(
                    TextBlock(
                        f"p{page_number}-b{i}",
                        page_number,
                        f"Atrazine was detected at {i * 10} ng/L in river water.\n" * 170,
                    )
                    for i in range(2)
                )
                pages.append(
                    ParsedPage(page_number=page_number, width=100, height=100, blocks=blocks)
                )
            return ParsedDocument(
                document_id=document_id,
                source_path=pdf_path,
                source_sha256=source_sha256,
                parser_name="mock",
                parser_version="1",
                pages=tuple(pages),
            )

    calls: list[str] = []

    class MergeExtractor:
        extractor_name = "mock"
        def extract(self, chunk: Any) -> list[dict[str, Any]]:
            calls.append(chunk.chunk_type)
            return [{"analyte": {"raw_name": f"merged-{chunk.chunk_type}"}, "evidence": {}}]

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-mock")
    plane = FulltextControlPlane(tmp_path / "state.sqlite3")
    harness = FulltextExtractionHarness(
        parser=MultiChunkParser(),
        control_plane=plane,
        chemical_registry=ChemicalRegistry(tmp_path / "registry.sqlite3"),
        output_dir=tmp_path / "runs",
        extractor=MergeExtractor(),
        chunk_selector=lambda _chunk: False,  # would reject everything if not bypassed
        merge_chunks_for_extraction=True,
        max_merged_input_chars=200_000,
    )
    report = harness.run_document(pdf)
    assert report.committed is True
    assert report.chunk_count == 4
    assert report.candidate_count == 1
    assert calls == ["merged_document"]  # one single call, prefilter bypassed
    assert "whole_document_single_call:4" in report.warnings

class _EmptyMergedExtractor:
    """Whole-document (merged) calls return nothing; per-chunk calls return candidates."""

    extractor_name = "mock"

    def __init__(self, per_chunk_candidates: list[dict[str, Any]]) -> None:
        self.per_chunk_candidates = per_chunk_candidates

    def extract(self, chunk: Any) -> list[dict[str, Any]]:
        chunk_id = str(getattr(chunk, "chunk_id", ""))
        if chunk_id.startswith("merged-"):
            return []
        return self.per_chunk_candidates


class _TransportFailMergedExtractor:
    """Whole-document (merged) calls raise a gateway read-timeout; per-chunk calls succeed."""

    extractor_name = "mock"

    def __init__(self, per_chunk_candidates: list[dict[str, Any]]) -> None:
        self.per_chunk_candidates = per_chunk_candidates

    def extract(self, chunk: Any) -> list[dict[str, Any]]:
        chunk_id = str(getattr(chunk, "chunk_id", ""))
        if chunk_id.startswith("merged-"):
            raise RuntimeError(
                "model transport failed: HTTPSConnectionPool host=ai.b1ank.top read timed out"
            )
        return self.per_chunk_candidates


class _TruncatedMergedExtractor:
    """Whole-document output hits max tokens; bounded per-chunk output succeeds."""

    extractor_name = "mock"

    def __init__(self, per_chunk_candidates: list[dict[str, Any]]) -> None:
        self.per_chunk_candidates = per_chunk_candidates

    def extract(self, chunk: Any) -> list[dict[str, Any]]:
        chunk_id = str(getattr(chunk, "chunk_id", ""))
        if chunk_id.startswith("merged-"):
            raise RuntimeError("model response was truncated (finish_reason=length)")
        return self.per_chunk_candidates


def test_whole_doc_empty_result_falls_back_to_per_chunk(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-mock")
    plane = FulltextControlPlane(tmp_path / "state.sqlite3")
    harness = FulltextExtractionHarness(
        parser=MockParser(),
        control_plane=plane,
        chemical_registry=ChemicalRegistry(tmp_path / "registry.sqlite3"),
        output_dir=tmp_path / "runs",
        extractor=_EmptyMergedExtractor(
            [{"analyte": {"raw_name": "PFOA"}, "evidence": {}}]
        ),
        merge_chunks_for_extraction=True,
        max_merged_input_chars=100_000,
    )
    report = harness.run_document(pdf)
    assert report.committed is True
    assert report.candidate_count == 1
    assert "whole_document_single_call:1" in report.warnings
    assert "whole_doc_empty_fallback_per_chunk" in report.warnings


def test_whole_doc_transport_timeout_falls_back_to_per_chunk(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-mock")
    plane = FulltextControlPlane(tmp_path / "state.sqlite3")
    harness = FulltextExtractionHarness(
        parser=MockParser(),
        control_plane=plane,
        chemical_registry=ChemicalRegistry(tmp_path / "registry.sqlite3"),
        output_dir=tmp_path / "runs",
        extractor=_TransportFailMergedExtractor(
            [{"analyte": {"raw_name": "PFOA"}, "evidence": {}}]
        ),
        merge_chunks_for_extraction=True,
        max_merged_input_chars=100_000,
    )
    report = harness.run_document(pdf)
    assert report.committed is True
    assert report.candidate_count == 1
    assert "whole_document_single_call:1" in report.warnings
    assert "whole_doc_transport_fallback_per_chunk" in report.warnings


def test_whole_doc_truncation_falls_back_to_per_chunk(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-mock")
    plane = FulltextControlPlane(tmp_path / "state.sqlite3")
    harness = FulltextExtractionHarness(
        parser=MockParser(),
        control_plane=plane,
        chemical_registry=ChemicalRegistry(tmp_path / "registry.sqlite3"),
        output_dir=tmp_path / "runs",
        extractor=_TruncatedMergedExtractor(
            [{"analyte": {"raw_name": "PFOA"}, "evidence": {}}]
        ),
        merge_chunks_for_extraction=True,
        max_merged_input_chars=100_000,
    )
    report = harness.run_document(pdf)
    assert report.committed is True
    assert report.candidate_count == 1
    assert "whole_document_single_call:1" in report.warnings
    assert "whole_doc_truncated_fallback_per_chunk" in report.warnings



def test_validator_failure_is_not_an_extraction_fallback_signal() -> None:
    class JsonCommandValidatorError(RuntimeError):
        pass

    inner = JsonCommandValidatorError("validator batch failed")
    outer = RuntimeError("document run failed")
    outer.__cause__ = inner

    assert _is_validator_failure(outer) is True
    assert _is_validator_failure(RuntimeError("model response was truncated")) is False

def test_document_local_abbreviation_queries_expanded_name_before_short_token() -> None:
    candidate = {
        "analyte": {
            "raw_name": "TC",
            "reported_name": "TC",
            "proposed_canonical_name": "tetracycline",
            "alias_type": "document_local",
        }
    }
    assert _candidate_lookup_names(candidate, "TC")[:2] == ["tetracycline", "TC"]


def test_hch_greek_mojibake_queries_specific_isomer_before_generic_parent() -> None:
    cases = {
        "Î±-HCH": "alpha-HCH",
        "Î²-HCH": "beta-HCH",
        "Î³-HCH": "gamma-HCH",
    }
    for raw_name, ascii_name in cases.items():
        candidate = {
            "analyte": {
                "raw_name": raw_name,
                "proposed_canonical_name": "Hexachlorocyclohexane",
                "alias_type": "abbreviation",
            }
        }
        queries = _candidate_lookup_names(candidate, raw_name)
        assert ascii_name in queries
        assert queries.index(ascii_name) < queries.index("Hexachlorocyclohexane")
        assert not queries[0].startswith("Î")


def test_curated_hch_isomer_mapping_preserves_distinct_cas_numbers() -> None:
    cases = {
        "Î±-HCH": ("alpha-Hexachlorocyclohexane", "319-84-6"),
        "Î²-HCH": ("beta-Hexachlorocyclohexane", "319-85-7"),
        "Î³-HCH": ("gamma-Hexachlorocyclohexane", "58-89-9"),
    }
    for raw_name, (canonical_name, cas_rn) in cases.items():
        resolution = _curated_specific_identity_resolution(
            raw_name, registry_snapshot_version=4
        )
        assert resolution is not None
        assert resolution.status == "validated_local"
        assert resolution.matches[0].canonical_name == canonical_name
        assert resolution.matches[0].cas_candidates == (cas_rn,)


def test_dedupe_payloads_ignores_quote_length_for_same_table_cell() -> None:
    base = {
        "analyte": {"raw_name": "ofloxacin", "reported_name": "OFX"},
        "result": {
            "raw_value": "3.43", "raw_unit": "ng/L", "value_numeric": 3.43,
            "qualifier": "exact", "statistic": "single",
        },
        "sample": {"matrix_normalized": "surface water"},
        "location": {"waterbody": "Shichuan River", "admin1": "Shaanxi"},
        "sampling_time": {"year": 2024, "raw_text": "November 2024"},
        "evidence": {
            "table_id": "Table 1", "row_label": "OFX",
            "column_label": "ShiChuan River, China", "page_start": 12, "page_end": 12,
            "quote": "OFX 3.43 48.37",
        },
    }
    duplicate = {**base, "evidence": {**base["evidence"], "quote": "OFX 3.43 48.37 38.31 73.88"}}
    assert _dedupe_payloads([base, duplicate]) == [base]


def test_dedupe_payloads_keeps_different_table_cells() -> None:
    first = {
        "analyte": {"raw_name": "ofloxacin", "reported_name": "OFX"},
        "result": {"raw_value": "3.43", "raw_unit": "ng/L", "value_numeric": 3.43},
        "evidence": {
            "table_id": "Table 1", "row_label": "OFX",
            "column_label": "ShiChuan River, China", "page_start": 12, "page_end": 12,
        },
    }
    second = {**first, "evidence": {**first["evidence"], "column_label": "Yangtze River, China"}}
    assert _dedupe_payloads([first, second]) == [first, second]


def test_chemical_identity_context_captures_article_local_pfas_definition() -> None:
    chunks = [
        EvidenceChunk(
            chunk_id="c-methods",
            document_id="d1",
            ordinal=2,
            chunk_type="section_text",
            text=(
                "Target analytes and abbreviations. "
                "Perfluorooctane sulfonic acid (PFOS) and "
                "perfluorooctanoic acid (PFOA) were analyzed by LC-MS/MS."
            ),
            page_start=4,
            page_end=4,
            source_block_ids=("b4",),
        ),
        EvidenceChunk(
            chunk_id="c-results",
            document_id="d1",
            ordinal=5,
            chunk_type="table",
            text="PFOS 20 ng/L",
            page_start=8,
            page_end=8,
            source_block_ids=("b8",),
        ),
    ]
    context = _chemical_identity_context(chunks)
    assert "Perfluorooctane sulfonic acid (PFOS)" in context
    assert "pages 4-4" in context
    assert "PFOS 20 ng/L" not in context


def test_chemical_identity_context_is_bounded() -> None:
    chunk = EvidenceChunk(
        chunk_id="c1",
        document_id="d1",
        ordinal=0,
        chunk_type="section_text",
        text="Target analytes " + ("PFAS analyte list " * 500),
        page_start=1,
        page_end=2,
        source_block_ids=("b1",),
    )
    context = _chemical_identity_context([chunk], max_chars=300)
    assert len(context) <= 300
    assert context.endswith("...(truncated)")


def test_empty_whole_doc_gate_skips_plastic_only_papers() -> None:
    chunk = EvidenceChunk(
        chunk_id="c1",
        document_id="doc",
        ordinal=0,
        chunk_type="page",
        text="Microplastic particles were 0.11 g/m2 in open water.",
        page_start=1,
        page_end=1,
        source_block_ids=("b1",),
    )
    allowed, reason = _whole_doc_empty_fallback_decision(
        [chunk],
        {
            "title": (
                "Plastic litter is a part of the carbon cycle in an urban river: "
                "Microplastic and macroplastic accumulate with organic matter"
            )
        },
    )
    assert allowed is False
    assert reason == "microplastic_or_plastic_only_title"


def test_empty_whole_doc_fallback_uses_text_only_for_chunk_retry(tmp_path: Path) -> None:
    calls: list[tuple[str, object]] = []

    class DirectPdfEmptyExtractor:
        extractor_name = "mock"

        def __init__(self) -> None:
            self.pdf_path = None

        def extract(self, chunk: Any) -> list[dict[str, Any]]:
            calls.append((chunk.chunk_type, self.pdf_path))
            if str(chunk.chunk_id).startswith("merged-"):
                return []
            return [{"analyte": {"raw_name": "PFOA"}, "evidence": {}}]

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-mock")
    harness = FulltextExtractionHarness(
        parser=MockParser(),
        control_plane=FulltextControlPlane(tmp_path / "state.sqlite3"),
        chemical_registry=ChemicalRegistry(tmp_path / "registry.sqlite3"),
        output_dir=tmp_path / "runs",
        extractor=DirectPdfEmptyExtractor(),
        merge_chunks_for_extraction=True,
        max_merged_input_chars=100_000,
    )
    report = harness.run_document(
        pdf,
        bibliographic_metadata={"title": "PFOA occurrence in river surface water"},
    )

    assert report.candidate_count == 1
    assert calls[0][0] == "merged_document"
    assert calls[0][1] == pdf.resolve()
    assert calls[1][1] is None
    assert "whole_doc_fallback_text_only" in report.warnings


def _bundle_test_chunks() -> list[EvidenceChunk]:
    return [
        EvidenceChunk(
            chunk_id=f"chunk-{index}",
            document_id="doc-1",
            ordinal=index,
            chunk_type="text",
            text=(
                "Methods: surface water samples collected at River A. "
                "LC-MS/MS measured PFOA at 12 ng/L. "
                + ("x" * 1600)
            ),
            page_start=index + 1,
            page_end=index + 1,
            source_block_ids=(f"block-{index}",),
        )
        for index in range(5)
    ]


def test_bounded_document_bundles_respect_hard_length_and_keep_context() -> None:
    chunks = _bundle_test_chunks()
    bundles = _split_chunks_for_extraction(
        chunks, document_id="doc-1", max_chars=5000, context_chars=1200
    )

    assert len(bundles) >= 2
    for bundle in bundles:
        assert len(bundle.text) <= 5000
        assert "SHARED DOCUMENT CONTEXT" in bundle.text
        assert "CURRENT BUNDLE" in bundle.text
        assert "BUNDLE SOURCE" in bundle.text
        assert bundle.source_block_ids
        assert bundle.page_start <= bundle.page_end

    source_ids = [source_id for bundle in bundles for source_id in bundle.source_block_ids]
    assert source_ids == [f"block-{index}" for index in range(5)]


def test_bounded_document_bundle_truncates_oversized_source_without_losing_anchor() -> None:
    chunk = EvidenceChunk(
        chunk_id="huge",
        document_id="doc-2",
        ordinal=0,
        chunk_type="table",
        text="table header\n" + ("PFOA 12 ng/L\n" * 2000),
        page_start=17,
        page_end=18,
        source_block_ids=("block-huge",),
    )

    bundles = _split_chunks_for_extraction(
        [chunk], document_id="doc-2", max_chars=1200, context_chars=600
    )

    assert len(bundles) == 1
    bundle = bundles[0]
    assert len(bundle.text) <= 1200
    assert "chunk_id=huge" in bundle.text
    assert "pages=17-18" in bundle.text
    assert "excerpt truncated" in bundle.text
    assert bundle.source_block_ids == ("block-huge",)
