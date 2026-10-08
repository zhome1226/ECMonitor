from pathlib import Path

from ecmonitor.fulltext_extraction.chunking import StructuralChunker
from ecmonitor.fulltext_extraction.models import ParsedDocument, ParsedPage, TextBlock


def test_structural_chunker_preserves_block_lineage() -> None:
    document = ParsedDocument(
        document_id="doc",
        source_path=Path("paper.pdf"),
        source_sha256="abc",
        parser_name="mock",
        parser_version="1",
        pages=(
            ParsedPage(
                page_number=1,
                width=100,
                height=100,
                blocks=(TextBlock("p1-b1", 1, "A" * 20), TextBlock("p1-b2", 1, "B" * 20)),
            ),
        ),
    )
    chunks = StructuralChunker(target_characters=20, maximum_characters=30).chunk(document)
    assert len(chunks) == 2
    assert chunks[0].source_block_ids == ("p1-b1",)
    assert chunks[1].source_block_ids == ("p1-b2",)
    assert chunks[0].page_start == chunks[0].page_end == 1
