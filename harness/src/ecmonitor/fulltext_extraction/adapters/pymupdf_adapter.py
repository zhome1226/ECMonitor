"""Fast native-text PDF parser backed by PyMuPDF.

This lightweight baseline does not claim reliable table structure or OCR. Quality gates route
difficult documents to Docling/GROBID/OCR adapters.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ecmonitor.fulltext_extraction.models import BoundingBox, ParsedDocument, ParsedPage, TextBlock


class PyMuPDFUnavailableError(RuntimeError):
    pass


class PyMuPDFParser:
    parser_name = "pymupdf"

    def __init__(self, *, sort_reading_order: bool = True) -> None:
        self.sort_reading_order = sort_reading_order

    def parse(self, pdf_path: Path, *, document_id: str, source_sha256: str) -> ParsedDocument:
        try:
            import pymupdf
        except ImportError as exc:  # pragma: no cover
            raise PyMuPDFUnavailableError(
                "PyMuPDF is not installed; install the fulltext-pdf optional dependency"
            ) from exc

        warnings: list[str] = []
        pages: list[ParsedPage] = []
        with pymupdf.open(pdf_path) as pdf:  # type: ignore[no-untyped-call]
            if pdf.needs_pass:
                raise ValueError(f"Encrypted PDF requires a password: {pdf_path}")
            for page_index, page in enumerate(pdf):
                raw_blocks: list[tuple[Any, ...]] = page.get_text(
                    "blocks", sort=self.sort_reading_order
                )
                blocks: list[TextBlock] = []
                for reading_order, raw in enumerate(raw_blocks):
                    if len(raw) < 7:
                        continue
                    x0, y0, x1, y1, text, block_number, block_type = raw[:7]
                    if int(block_type) != 0 or not str(text).strip():
                        continue
                    blocks.append(
                        TextBlock(
                            block_id=f"p{page_index + 1}-b{int(block_number)}",
                            page_number=page_index + 1,
                            text=str(text).strip(),
                            bbox=BoundingBox(float(x0), float(y0), float(x1), float(y1)),
                            reading_order=reading_order,
                        )
                    )
                if not blocks:
                    warnings.append(f"zero_native_text_page:{page_index + 1}")
                pages.append(
                    ParsedPage(
                        page_number=page_index + 1,
                        width=float(page.rect.width),
                        height=float(page.rect.height),
                        blocks=tuple(blocks),
                    )
                )
            metadata = {str(key): value for key, value in (pdf.metadata or {}).items()}
            parser_version = str(getattr(pymupdf, "VersionBind", "unknown"))

        zero_pages = sum(not page.text.strip() for page in pages)
        if pages and zero_pages / len(pages) > 0.20:
            warnings.append("native_text_quality_below_threshold:route_to_ocr_or_alternate_parser")
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
