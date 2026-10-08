from ecmonitor.fulltext_extraction.chunk_selection import likely_occurrence_chunk
from ecmonitor.fulltext_extraction.models import EvidenceChunk


def _chunk(text: str, *, chunk_type: str = "section_text") -> EvidenceChunk:
    return EvidenceChunk(
        chunk_id="c1",
        document_id="d1",
        ordinal=0,
        chunk_type=chunk_type,
        text=text,
        page_start=1,
        page_end=1,
        source_block_ids=("b1",),
    )


def test_prefilter_keeps_concentration_evidence() -> None:
    assert likely_occurrence_chunk(_chunk("PFOA was detected at 12 ng/L in river water."))
    assert likely_occurrence_chunk(_chunk("Median concentrations were 3.2 to 7.8."))


def test_prefilter_drops_plain_background_text() -> None:
    assert not likely_occurrence_chunk(_chunk("This article discusses environmental policy."))
    assert not likely_occurrence_chunk(_chunk("A standard solution was prepared."))


def test_prefilter_keeps_numeric_table() -> None:
    assert likely_occurrence_chunk(_chunk("PFOA | 12 | 24", chunk_type="table"))
