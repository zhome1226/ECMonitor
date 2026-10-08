"""Detection-limit context injection for censored (LOD/LOQ) results.

Verifies the per-chunk LOD/LOQ visibility fix: the harness assembles a bounded
document-local excerpt of numeric detection/quantification limits from the method
blocks and injects it into ``document_context.detection_limit_context`` so the
per-chunk extractor and validator can cross-reference a censored result against the
paper's stated limits even though the methods paragraph lives in a different chunk.
"""

from __future__ import annotations

from ecmonitor.fulltext_extraction.harness import _detection_limit_context
from ecmonitor.fulltext_extraction.models import EvidenceChunk


def _chunk(text: str, ordinal: int, *, pid: str = "doc") -> EvidenceChunk:
    return EvidenceChunk(
        chunk_id=f"chunk-{ordinal}",
        document_id=pid,
        ordinal=ordinal,
        chunk_type="section_text",
        text=text,
        page_start=2,
        page_end=3,
        source_block_ids=(),
    )


def test_detection_limit_context_captures_method_lod_loq_list() -> None:
    chunks = [
        _chunk(
            "Sample prep used 1 g of tissue. The limits of detection (LODs) of the "
            "ICP-OES for Al, Cd, Co, Cr, Fe, Mn, Ni, Pb, Zn were 25, 0.19, 0.32, 1.4, "
            "3.1, 0.08, 0.35, 3.5, 3.4 ug L-1 respectively.",
            0,
        ),
        _chunk("Atrazine was not detected in any sample (< LOD 0.5 ng/L).", 1),
    ]
    ctx = _detection_limit_context(chunks, max_chars=6000)
    assert "25, 0.19" in ctx
    assert "0.5 ng/L" in ctx
    assert "chunk 0" in ctx


def test_detection_limit_context_ignores_methods_without_limits() -> None:
    chunks = [
        _chunk("Calibration standards ranged 0.1-100 ng/L.", 0),
        _chunk("The river flow was measured daily.", 1),
        _chunk("Concentrations of Cd ranged from 0.05 to 0.9 ug/L.", 2),
    ]
    ctx = _detection_limit_context(chunks, max_chars=6000)
    assert ctx == ""


def test_detection_limit_context_dedupes_repeated_excerpt() -> None:
    text = "LOQ was 0.03-0.04 ng/L for all organophosphates."
    chunks = [_chunk(text, 0), _chunk(text, 1)]
    ctx = _detection_limit_context(chunks, max_chars=6000)
    assert ctx.count("LOQ was 0.03-0.04") == 1


def test_detection_limit_context_respects_max_chars() -> None:
    chunks = [_chunk("The limit of quantification (LOQ) was " + "9" * 500 + " ng/L.", 0)]
    ctx = _detection_limit_context(chunks, max_chars=200)
    assert len(ctx) <= 200
