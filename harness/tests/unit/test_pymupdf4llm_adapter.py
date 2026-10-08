"""Tests for the pymupdf4llm PDF parser adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from ecmonitor.fulltext_extraction.adapters.pymupdf4llm_adapter import PyMuPDF4LLMParser

pymupdf = pytest.importorskip("pymupdf")
pymupdf4llm = pytest.importorskip("pymupdf4llm")


def _make_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((72, 72), "Methods section heading", fontsize=14)
    page.insert_text((72, 120), "Samples were collected in Warsaw in 2019.", fontsize=10)
    page.insert_text((72, 180), "Concentration (ng/L) value 12.5", fontsize=10)
    document.save(path)
    document.close()


def test_pymupdf4llm_parses_into_pages_and_blocks(tmp_path: Path) -> None:
    pdf = tmp_path / "sample.pdf"
    _make_pdf(pdf)

    parser = PyMuPDF4LLMParser()
    parsed = parser.parse(pdf, document_id="doc1", source_sha256="sha")

    assert parsed.parser_name == "pymupdf4llm"
    assert parsed.page_count >= 1
    total_blocks = sum(len(page.blocks) for page in parsed.pages)
    assert total_blocks >= 1
    # every block keeps its page number and a reading order
    for page in parsed.pages:
        for block in page.blocks:
            assert block.page_number == page.page_number
            assert block.reading_order >= 0


def test_pymupdf4llm_chunks_into_evidence_chunks(tmp_path: Path) -> None:
    from ecmonitor.fulltext_extraction.chunking import StructuralChunker

    pdf = tmp_path / "sample.pdf"
    _make_pdf(pdf)

    parser = PyMuPDF4LLMParser()
    parsed = parser.parse(pdf, document_id="doc1", source_sha256="sha")
    chunks = StructuralChunker().chunk(parsed)

    assert chunks
    joined = "\n".join(chunk.text for chunk in chunks)
    assert "Methods" in joined or "Concentration" in joined or "Warsaw" in joined
