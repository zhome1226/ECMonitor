"""Layout-preserving baseline chunker."""

from __future__ import annotations

import hashlib

from ecmonitor.fulltext_extraction.models import EvidenceChunk, ParsedDocument, TextBlock


class StructuralChunker:
    """Group parser blocks without splitting ordinary blocks across chunks."""

    def __init__(self, *, target_characters: int = 7_200, maximum_characters: int = 12_800) -> None:
        if target_characters <= 0 or maximum_characters < target_characters:
            raise ValueError("invalid chunk size limits")
        self.target_characters = target_characters
        self.maximum_characters = maximum_characters

    def chunk(self, document: ParsedDocument) -> list[EvidenceChunk]:
        chunks: list[EvidenceChunk] = []
        pending: list[TextBlock] = []
        pending_chars = 0
        for page in document.pages:
            for block in page.blocks:
                if len(block.text) > self.maximum_characters:
                    if pending:
                        chunks.append(self._build(document.document_id, len(chunks), pending))
                        pending = []
                        pending_chars = 0
                    for piece in self._split_oversized_block(block):
                        chunks.append(self._build(document.document_id, len(chunks), [piece]))
                    continue
                projected = pending_chars + len(block.text) + (2 if pending else 0)
                if pending and projected > self.maximum_characters:
                    chunks.append(self._build(document.document_id, len(chunks), pending))
                    pending = []
                    pending_chars = 0
                pending.append(block)
                pending_chars += len(block.text) + (2 if len(pending) > 1 else 0)
                if pending_chars >= self.target_characters:
                    chunks.append(self._build(document.document_id, len(chunks), pending))
                    pending = []
                    pending_chars = 0
        if pending:
            chunks.append(self._build(document.document_id, len(chunks), pending))
        return chunks

    def _split_oversized_block(self, block: TextBlock) -> list[TextBlock]:
        pieces: list[TextBlock] = []
        text = block.text
        start = 0
        piece_number = 0
        while start < len(text):
            end = min(start + self.maximum_characters, len(text))
            if end < len(text):
                paragraph_break = text.rfind("\n", start, end)
                sentence_break = text.rfind(". ", start, end)
                end = max(paragraph_break + 1, sentence_break + 2, start + 1)
            pieces.append(
                TextBlock(
                    block_id=f"{block.block_id}-part{piece_number}",
                    page_number=block.page_number,
                    text=text[start:end].strip(),
                    bbox=block.bbox,
                    block_type=block.block_type,
                    reading_order=block.reading_order,
                )
            )
            start = end
            piece_number += 1
        return [piece for piece in pieces if piece.text]

    @staticmethod
    def _build(document_id: str, ordinal: int, blocks: list[TextBlock]) -> EvidenceChunk:
        text = "\n\n".join(block.text for block in blocks)
        digest = hashlib.sha256(f"{document_id}\0{ordinal}\0{text}".encode()).hexdigest()[:24]
        pages = [block.page_number for block in blocks]
        return EvidenceChunk(
            chunk_id=f"chunk-{digest}",
            document_id=document_id,
            ordinal=ordinal,
            chunk_type="section_text",
            text=text,
            page_start=min(pages),
            page_end=max(pages),
            source_block_ids=tuple(block.block_id for block in blocks),
        )
