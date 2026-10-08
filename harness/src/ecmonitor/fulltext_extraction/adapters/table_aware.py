"""Table-aware PyMuPDF parsing and row-window chunking for dense monitoring papers.

This adapter keeps ordinary pages as native text, but turns detected PDF tables into explicit,
header-repeated row windows.  It is intentionally conservative: table values remain evidence
text and the model still decides whether a row is an in-scope surface-water observation.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from ecmonitor.fulltext_extraction.adapters.pymupdf_adapter import PyMuPDFParser
from ecmonitor.fulltext_extraction.models import EvidenceChunk, ParsedDocument, TextBlock

_HEADER_CUES = re.compile(r"(?i)\b(?:analyte|compound|antibiotic|classification|site|location|date|concentration|lod|loq)\b")
_TABLE_NUMBER_RE = re.compile(r"(?i)^table\s+(\d+)\b")
_SITE_TOKEN_RE = re.compile(r"(?i)^(?:site\s*)?(?:S|Z|D)\d+$")
_WATERBODY_RE = re.compile(r"(?i)^(?:river|lake|reservoir|stream|waterway|estuary)$")


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\n", " ").split())


def _positioned_rows(page: Any) -> list[list[tuple[float, str]]]:
    """Return visual text rows while retaining each word's x coordinate.

    ``Page.get_text('text')`` flattens wide tables column-by-column. These rows preserve the
    two-dimensional layout for borderless tables that ``find_tables()`` cannot detect.
    """
    words = sorted(page.get_text("words"), key=lambda item: (float(item[1]), float(item[0])))
    rows: list[list[tuple[float, str]]] = []
    row_y: list[float] = []
    for word in words:
        x0, y0, _x1, _y1, text, *_ = word
        cleaned = _clean(text)
        if not cleaned:
            continue
        nearest = None
        nearest_distance = 99.0
        for index, existing_y in enumerate(row_y):
            distance = abs(float(y0) - existing_y)
            if distance <= 2.2 and distance < nearest_distance:
                nearest = index
                nearest_distance = distance
        if nearest is None:
            row_y.append(float(y0))
            rows.append([(float(x0), cleaned)])
        else:
            rows[nearest].append((float(x0), cleaned))
            row_y[nearest] = min(row_y[nearest], float(y0))
    ordered = sorted(zip(row_y, rows, strict=True), key=lambda item: item[0])
    return [sorted(row, key=lambda cell: cell[0]) for _y, row in ordered]


def _positional_occurrence_table_number(rows: list[list[tuple[float, str]]]) -> str | None:
    """Return a table number only when a real occurrence-table header is visible.

    Mentions such as ``values are shown in Table 2`` previously caused an entire prose page to
    be encoded as a positional table. The fallback now requires a nearby table caption plus a
    geometry-preserving header: either ``Sites`` followed by chemical columns, or
    ``Analyte/Antibiotic`` followed by at least two site/waterbody columns.
    """
    for header_index, row in enumerate(rows):
        tokens = [text.strip() for _x, text in row]
        folded = [token.casefold() for token in tokens]
        analyte_header = any(
            token in {"analyte", "analytes", "antibiotic"} for token in folded
        )
        site_header = "sites" in folded
        if not analyte_header and not site_header:
            continue

        nearby = rows[header_index : header_index + 4]
        nearby_tokens = [text.strip() for candidate in nearby for _x, text in candidate]
        site_count = sum(
            bool(_SITE_TOKEN_RE.fullmatch(token) or _WATERBODY_RE.fullmatch(token))
            for token in nearby_tokens
        )
        if analyte_header and site_count < 2:
            continue

        if site_header:
            chemical_labels = [
                token
                for token in nearby_tokens
                if re.fullmatch(r"[A-Za-z][A-Za-z0-9β-]{0,20}", token)
                and token.casefold()
                not in {
                    "sites",
                    "site",
                    "concentration",
                    "concentrations",
                    "date",
                    "rate",
                    "detection",
                    "doc",
                    "toc",
                    "e2eq",
                    "mg",
                    "ng",
                    "l",
                }
            ]
            if len(chemical_labels) < 2:
                continue

        for caption_index in range(header_index, max(-1, header_index - 10), -1):
            caption = " ".join(text for _x, text in rows[caption_index]).strip()
            match = _TABLE_NUMBER_RE.match(caption)
            if match:
                return match.group(1)
    return None


def _positioned_table_text(page: Any, *, page_number: int, table_number: str) -> str:
    lines = [
        f"POSITIONAL_TABLE page={page_number} table={table_number}",
        "POSITIONAL_FORMAT x-coordinate::word; visual rows retained from PDF",
    ]
    for row_index, row in enumerate(_positioned_rows(page)):
        encoded = " | ".join(f"x={x:.1f}::{text}" for x, text in row)
        lines.append(f"POSROW {row_index}: {encoded}")
    return "\n".join(lines)


class TableAwarePyMuPDFParser:
    parser_name = "pymupdf-table-aware"

    def __init__(self, *, sort_reading_order: bool = True) -> None:
        self._base = PyMuPDFParser(sort_reading_order=sort_reading_order)

    def parse(self, pdf_path: Path, *, document_id: str, source_sha256: str) -> ParsedDocument:
        parsed = self._base.parse(pdf_path, document_id=document_id, source_sha256=source_sha256)
        try:
            import pymupdf
        except ImportError:
            return parsed
        pages: list[Any] = []
        carried_headers: dict[int, list[list[str]]] = {}
        with pymupdf.open(pdf_path) as pdf:  # type: ignore[no-untyped-call]
            for parsed_page, page in zip(parsed.pages, pdf, strict=True):
                blocks = list(parsed_page.blocks)
                try:
                    tables = list(page.find_tables().tables)
                except Exception:
                    tables = []
                for table_index, table in enumerate(tables):
                    rows = [[_clean(cell) for cell in row] for row in table.extract()]
                    rows = [row for row in rows if any(row)]
                    if not rows:
                        continue
                    # A continuation page often has only data rows. Repeat the last known
                    # header so a row window remains interpretable in isolation.
                    first_has_header = bool(
                        rows
                        and len(rows[0]) >= 2
                        and rows[0][0].casefold() in {"classification", "class"}
                        and rows[0][1].casefold() in {"analyte", "analytes", "compound", "compounds"}
                    )
                    if first_has_header:
                        carried_headers[table_index] = rows[: min(3, len(rows))]
                    elif table_index in carried_headers:
                        rows = [*carried_headers[table_index], *rows]
                    caption = ""
                    page_text = parsed_page.text
                    match = re.search(r"(?i)(table\s+\d+[^\n]{0,280})", page_text)
                    if match:
                        caption = " ".join(match.group(1).split())
                    table_text = [
                        f"TABLE_STRUCTURED page={parsed_page.page_number} table={table_index + 1}",
                        f"TABLE_CAPTION {caption}" if caption else "TABLE_CAPTION unknown",
                    ]
                    for row_index, row in enumerate(rows):
                        table_text.append(f"ROW {row_index}: " + " | ".join(row))
                    blocks.append(
                        TextBlock(
                            block_id=f"p{parsed_page.page_number}-table{table_index + 1}",
                            page_number=parsed_page.page_number,
                            text="\n".join(table_text),
                            block_type="table",
                            reading_order=10_000 + table_index,
                        )
                    )
                # Borderless tables are common in Springer PDFs. When PyMuPDF cannot detect a
                # table, retain visual x/y layout as a deterministic fallback rather than asking
                # an LLM to reconstruct columns from flattened reading order.
                if not tables:
                    positioned_rows = _positioned_rows(page)
                    table_number = _positional_occurrence_table_number(positioned_rows)
                else:
                    table_number = None
                if table_number is not None:
                    blocks.append(
                        TextBlock(
                            block_id=f"p{parsed_page.page_number}-positional-table{table_number}",
                            page_number=parsed_page.page_number,
                            text=_positioned_table_text(
                                page,
                                page_number=parsed_page.page_number,
                                table_number=table_number,
                            ),
                            block_type="positional_table",
                            reading_order=20_000,
                        )
                    )
                pages.append(
                    type(parsed_page)(
                        page_number=parsed_page.page_number,
                        width=parsed_page.width,
                        height=parsed_page.height,
                        blocks=tuple(blocks),
                    )
                )
        return ParsedDocument(
            document_id=parsed.document_id,
            source_path=parsed.source_path,
            source_sha256=parsed.source_sha256,
            parser_name=self.parser_name,
            parser_version=parsed.parser_version,
            pages=tuple(pages),
            warnings=tuple(parsed.warnings) + ("table_structured_blocks_added",),
            metadata={**parsed.metadata, "table_aware": True},
        )


class TableAwareChunker:
    """Emit small header-repeated windows for table blocks and normal chunks elsewhere."""

    def __init__(self, *, target_characters: int = 2_400, maximum_characters: int = 4_800, table_rows: int = 3) -> None:
        if target_characters <= 0 or maximum_characters < target_characters or table_rows < 1:
            raise ValueError("invalid table-aware chunk settings")
        self.target_characters = target_characters
        self.maximum_characters = maximum_characters
        self.table_rows = table_rows

    def chunk(self, document: ParsedDocument) -> list[EvidenceChunk]:
        chunks: list[EvidenceChunk] = []
        pending: list[TextBlock] = []
        pending_chars = 0

        def flush() -> None:
            nonlocal pending, pending_chars
            if not pending:
                return
            text = "\n\n".join(block.text for block in pending)
            digest = hashlib.sha256(f"{document.document_id}\0{len(chunks)}\0{text}".encode()).hexdigest()[:24]
            chunks.append(EvidenceChunk(
                chunk_id=f"chunk-{digest}", document_id=document.document_id,
                ordinal=len(chunks), chunk_type="section_text", text=text,
                page_start=min(b.page_number for b in pending), page_end=max(b.page_number for b in pending),
                source_block_ids=tuple(b.block_id for b in pending),
            ))
            pending, pending_chars = [], 0

        for page in document.pages:
            table_blocks = [
                b for b in page.blocks if b.block_type in {"table", "positional_table"}
            ]
            if table_blocks:
                flush()
                for block in table_blocks:
                    if block.block_type == "positional_table":
                        chunks.append(EvidenceChunk(
                            chunk_id=f"{block.block_id}-window-0",
                            document_id=document.document_id,
                            ordinal=len(chunks), chunk_type="table_row_window", text=block.text,
                            page_start=page.page_number, page_end=page.page_number,
                            source_block_ids=(block.block_id,),
                            warnings=("positional_table_fallback", "table_row_window"),
                        ))
                        continue
                    lines = block.text.splitlines()
                    prefix = lines[:2]
                    row_lines = [line for line in lines[2:] if line.startswith("ROW ")]
                    # The first three ROW lines are the column header in a monitoring
                    # table (analyte/site names plus units).  Keep them with every window;
                    # on continuation pages the parser carries the same three rows.
                    has_structured_header = bool(
                        row_lines
                        and ("Classification" in row_lines[0] or "Analytes" in row_lines[0])
                    )
                    header_rows = row_lines[:3] if has_structured_header else []
                    rows = row_lines[3:] if has_structured_header else row_lines
                    header = [*prefix, *header_rows]
                    for start in range(0, len(rows), self.table_rows):
                        body = "\n".join([*header, *rows[start:start + self.table_rows]])
                        chunks.append(EvidenceChunk(
                            chunk_id=f"{block.block_id}-window-{start // self.table_rows}",
                            document_id=document.document_id,
                            ordinal=len(chunks), chunk_type="table_row_window", text=body,
                            page_start=page.page_number, page_end=page.page_number,
                            source_block_ids=(block.block_id,),
                            warnings=("table_header_repeated", "table_row_window"),
                        ))
                continue
            for block in page.blocks:
                projected = pending_chars + len(block.text) + (2 if pending else 0)
                if pending and projected > self.maximum_characters:
                    flush()
                pending.append(block)
                pending_chars += len(block.text) + (2 if len(pending) > 1 else 0)
                if pending_chars >= self.target_characters:
                    flush()
        flush()
        return chunks
