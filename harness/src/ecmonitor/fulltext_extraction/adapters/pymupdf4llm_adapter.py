"""PDF-to-structure parser backed by pymupdf4llm (markdown layout + tables).

Unlike the raw PyMuPDF block parser, this adapter emits page-level Markdown that keeps
headings, tables, and reading order intact. Tables become Markdown tables in the chunk text,
which materially improves LLM extraction of methods / sampling tables.

The layout flag is kept off on purpose: the GNN layout engine loads a model per process and is
not safe to share across parallel threads. The legacy rag path is pure PyMuPDF, thread-safe, and
still renders tables as Markdown.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ecmonitor.fulltext_extraction.models import ParsedDocument, ParsedPage, TextBlock


class PyMuPDF4LLMUnavailableError(RuntimeError):
    pass


class PyMuPDF4LLMParser:
    parser_name = "pymupdf4llm"

    def __init__(self, *, table_strategy: str = "lines_strict") -> None:
        self.table_strategy = table_strategy

    def parse(self, pdf_path: Path, *, document_id: str, source_sha256: str) -> ParsedDocument:
        try:
            import pymupdf
            import pymupdf4llm  # type: ignore[import-not-found,import-untyped,unused-ignore]
        except ImportError as exc:  # pragma: no cover
            raise PyMuPDF4LLMUnavailableError(
                "pymupdf4llm is not installed; install the fulltext-pdf optional dependency"
            ) from exc

        with pymupdf.open(pdf_path) as pdf:  # type: ignore[no-untyped-call]
            if pdf.needs_pass:
                raise ValueError(f"Encrypted PDF requires a password: {pdf_path}")
            metadata = {str(key): value for key, value in (pdf.metadata or {}).items()}

        raw_pages = pymupdf4llm.to_markdown(
            str(pdf_path),
            page_chunks=True,
            layout=False,
            table_strategy=self.table_strategy,
            show_progress=False,
        )
        warnings: list[str] = []
        pages: list[ParsedPage] = []
        # raw_pages is a list of per-page chunks keyed by metadata["page_number"].
        for raw in raw_pages:
            page_number = int((raw.get("metadata") or {}).get("page_number") or 0)
            text = raw.get("text") or ""
            blocks = _page_blocks(text, page_number)
            if not blocks:
                warnings.append(f"zero_native_text_page:{page_number}")
            pages.append(
                ParsedPage(
                    page_number=page_number,
                    width=0.0,
                    height=0.0,
                    blocks=tuple(blocks),
                )
            )
        # Sort by page number in case the reader returned pages out of order.
        pages.sort(key=lambda page: page.page_number)
        zero_pages = sum(not page.text.strip() for page in pages)
        if pages and zero_pages / len(pages) > 0.20:
            warnings.append("native_text_quality_below_threshold:route_to_ocr_or_alternate_parser")
        parser_version = str(_module_version(pymupdf4llm))
        return ParsedDocument(
            document_id=document_id,
            source_path=pdf_path.resolve(),
            source_sha256=source_sha256,
            parser_name=self.parser_name,
            parser_version=parser_version,
            pages=tuple(pages),
            warnings=tuple(warnings),
            metadata=metadata,
        )


def _module_version(module: Any) -> str:
    return str(getattr(module, "__version__", "unknown"))


def _page_blocks(markdown_text: str, page_number: int) -> list[TextBlock]:
    """Split one page of Markdown into paragraph blocks with deterministic ids."""
    blocks: list[TextBlock] = []
    paragraphs = _split_paragraphs(markdown_text)
    for index, paragraph in enumerate(paragraphs):
        stripped = paragraph.strip()
        if not stripped:
            continue
        blocks.append(
            TextBlock(
                block_id=f"p{page_number}-b{index}",
                page_number=page_number,
                text=stripped,
                bbox=None,
                block_type=_block_type(stripped),
                reading_order=index,
            )
        )
    return blocks


def _split_paragraphs(markdown_text: str) -> list[str]:
    parts = []
    buffer: list[str] = []
    for line in markdown_text.split("\n"):
        if line.strip():
            buffer.append(line)
        elif buffer:
            parts.append("\n".join(buffer))
            buffer = []
    if buffer:
        parts.append("\n".join(buffer))
    return parts


def _block_type(paragraph: str) -> str:
    # A paragraph is treated as a table block when every non-empty line contains a pipe.
    lines = [line for line in paragraph.split("\n") if line.strip()]
    if lines and all("|" in line for line in lines):
        return "table"
    return "text"
