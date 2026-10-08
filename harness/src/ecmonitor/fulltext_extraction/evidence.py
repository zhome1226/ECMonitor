"""Deterministic evidence-anchor checks and text normalization.

These utilities back the harness's cheap pre-review gates. They are pure string/rules
operations (no model call): they normalize PDF-extracted text so downstream matching is
robust to glyph corruption (``µ`` read as ``l``, Unicode sub/superscripts, Unicode dashes)
and they check that an extracted observation's value is actually supported by the chunk
text. A value that cannot be found anywhere in its source chunk is treated as a
*hallucination suspect* and routed to human review instead of spending a model call.
"""

from __future__ import annotations

import re
from typing import Any

from ecmonitor.fulltext_extraction.models import ParsedDocument, ParsedPage, TextBlock

_SUPERSCRIPTS = {
    "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5",
    "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9",
    "⁻": "-", "⁺": "+", "⁼": "=",
    "₀": "0", "₁": "1", "₂": "2", "₃": "3", "₄": "4",
    "₅": "5", "₆": "6", "₇": "7", "₈": "8", "₉": "9",
}

# PDF/OCR extraction frequently drops the ``µ`` prefix, turning ``µg/L`` into ``lg/L``.
# Only repair when ``lg`` looks like a mass-per-volume/amount unit prefix (followed by /L,
# /g, /kg, /mL, /m3 ...), never inside an ordinary word such as ``log/``.
_UNIT_LG = re.compile(r"(?i)(?<![a-z0-9])lg/(?=[a-zµμg])")

_DASHES = str.maketrans("‐‑‒–—−", "------")


def normalize_document_text(text: str) -> str:
    """Return a cleaned copy of PDF text used for extraction and evidence matching.

    Repairs the two artifacts that most often break chemical/unit fidelity:
    - Unicode sub/superscripts flattened to ASCII, so ``ng·g⁻¹`` reads ``ng·g-1``;
    - the ``µ`` prefix PDF extraction drops, so ``µg/L`` reads ``µg/L`` instead of ``lg/L``.
    """
    out = "".join(_SUPERSCRIPTS.get(ch, ch) for ch in text)
    out = _UNIT_LG.sub("µg/", out)
    return out


def fold_for_evidence(text: str) -> str:
    """Fold text into a canonical, whitespace-free form for substring matching."""
    out = normalize_document_text(text)
    out = out.casefold()
    out = out.replace("·", "/")
    out = out.replace("×", "x")
    out = out.translate(_DASHES)
    out = re.sub(r"(?<=\d),(?=\d)", ".", out)  # decimal comma -> dot
    out = out.replace(",", "")
    out = re.sub(r"\s+", "", out)
    return out


def _numeric_cores(value_str: str) -> list[str]:
    """Extract the numeric cores from a raw value string for presence checking."""
    folded = fold_for_evidence(value_str)
    segments = re.split(r"[±/]", folded)
    cores: list[str] = []
    for segment in segments:
        for match in re.finditer(r"\d+(?:[.,]\d+)*", segment):
            core = match.group(0).replace(",", ".")
            if core not in cores:
                cores.append(core)
    return cores


_CENSORING_QUALIFIERS = {"not_detected", "not_quantified"}


def evidence_anchor_reason(
    candidate: dict[str, Any], folded_chunk: str
) -> str | None:
    """Return a short reason if ``candidate``'s result value is NOT supported by the
    (already folded) chunk text, else ``None``.

    ND/DNQ markers and non-numeric values are skipped. The check is deliberately lenient:
    any numeric core present in the source text counts as supported, so this catches the
    strong hallucination signal (a value that appears nowhere in its own chunk) without
    generating noise from formatting differences.
    """
    result = candidate.get("result")
    if not isinstance(result, dict):
        return None
    qualifier = result.get("qualifier")
    raw_value = result.get("raw_value")
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    if qualifier in _CENSORING_QUALIFIERS:
        return None
    cores = _numeric_cores(raw_value)
    if not cores:
        return None
    if any(core in folded_chunk for core in cores):
        return None
    return f"value_not_found_in_text:{raw_value[:40]}"


def normalize_parsed(parsed: ParsedDocument) -> ParsedDocument:
    """Return a copy of ``parsed`` with every block's text passed through
    :func:`normalize_document_text` (glyph repair). Structure is preserved."""
    pages = tuple(
        ParsedPage(
            page_number=page.page_number,
            width=page.width,
            height=page.height,
            blocks=tuple(
                TextBlock(
                    block_id=block.block_id,
                    page_number=block.page_number,
                    text=normalize_document_text(block.text),
                    bbox=block.bbox,
                    block_type=block.block_type,
                    reading_order=block.reading_order,
                )
                for block in page.blocks
            ),
        )
        for page in parsed.pages
    )
    return ParsedDocument(
        document_id=parsed.document_id,
        source_path=parsed.source_path,
        source_sha256=parsed.source_sha256,
        parser_name=parsed.parser_name,
        parser_version=parsed.parser_version,
        pages=pages,
        warnings=parsed.warnings,
        metadata=dict(parsed.metadata),
    )


# A value adjacent to a mass/amount concentration unit. The chunk text is normalized before
# this runs, so PDF glyph corruption (``lg/L`` -> ``µg/L``, ``10⁻⁴`` -> ``10-4``) is already
# repaired. Detecting these regions powers the "focused second pass": when a whole extraction
# returns zero candidates but the document clearly contains concentration-like text, the
# harness re-prompts only the neighbourhoods of these hits instead of re-reading the whole
# document.
_CONCENTRATION_UNIT = r"(?:µg|μg|ng|pg|mg|g|kg|mmol|µmol|μmol|mol|mEq|ppm|ppb|ppt)"
_CONC_NUMBER = (
    r"(?:nd|nq|n\.d\.|below\s+(?:lod|loq|dl|detection\s*limit))?"
    r"[-−]?\s*\d+(?:[.,]\d+)*"
)
_CONC_SCI = r"(?:\s*[×x]\s*10\s*[-−⁺]?\s*\d+)?"
_CONC_RANGE = r"(?:\s*[-–—−]\s*\d+(?:[.,]\d+)*\s*(?:\s*[×x]\s*10\s*[-−⁺]?\s*\d+)?)?"
_CONCENTRATION_RE = re.compile(
    rf"(?i)(?<![a-z0-9]){_CONC_NUMBER}{_CONC_SCI}{_CONC_RANGE}"
    rf"\s*{_CONCENTRATION_UNIT}\s*/?\s*(?:L|kg|g|mL|m3|dm3|m2|m)?(?![a-z0-9])"
)


def concentration_hit_spans(
    text: str, *, max_hits: int = 40, padding: int = 240
) -> list[tuple[int, int]]:
    """Return merged ``[start, end)`` spans around concentration-like patterns.

    Only non-empty, capped, and non-overlapping ranges are returned so the focused second pass
    stays small (a few hundred characters around each hit, at most ``max_hits`` regions). An
    empty list means the text carries no obvious concentration signal and a second pass would
    only burn a model call.
    """
    raw: list[tuple[int, int]] = []
    for match in _CONCENTRATION_RE.finditer(text):
        start = max(0, match.start() - padding)
        end = min(len(text), match.end() + padding)
        raw.append((start, end))
        if len(raw) >= max_hits:
            break
    if not raw:
        return []
    merged: list[tuple[int, int]] = [raw[0]]
    for start, end in raw[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged
