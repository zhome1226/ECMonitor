"""High-recall deterministic prefilter for model-bound evidence chunks."""

from __future__ import annotations

import re

from ecmonitor.fulltext_extraction.models import EvidenceChunk

_CONCENTRATION_UNIT = re.compile(
    r"(?i)(?:pg|fg|ng|µg|ug|mg|g|pmol|nmol|umol|mmol)\s*/\s*(?:l|ml|m3|m\^?3|kg|g|d|day)"
)
_NUMERIC_RESULT = re.compile(r"(?<![A-Za-z])(?:<|>|≤|≥|=)?\s*\d+(?:[.,]\d+)?")
_OCCURRENCE_TERMS = re.compile(
    r"(?i)\b(?:concentration|detected|quantified|occurrence|monitoring|sampling|"
    r"measured|median|mean|maximum|minimum|nd|dnq|lod|loq|below detection|"
    r"ng/l|µg/l|ug/l|mg/l)\b"
)
_METHOD_ONLY_TERMS = re.compile(
    r"(?i)\b(?:calibration|standard solution|spike|recovery|fortified|blank|"
    r"ec50|lc50|noec|loec|toxicity test)\b"
)


def likely_occurrence_chunk(chunk: EvidenceChunk) -> bool:
    """Return true for chunks worth sending to the semantic extractor.

    This is deliberately high-recall. It is a cost gate, not a scientific acceptance gate. A
    chunk is retained when it has a concentration unit, or when occurrence/result language and a
    number co-occur. Method-only chunks are retained when they also contain a numeric result so
    the model can distinguish field results from validation values.
    """

    text = chunk.text
    if not text.strip():
        return False
    if _CONCENTRATION_UNIT.search(text):
        return True
    if _OCCURRENCE_TERMS.search(text) and _NUMERIC_RESULT.search(text):
        return True
    if chunk.chunk_type in {"table", "supplementary_table"} and _NUMERIC_RESULT.search(text):
        return True
    if _METHOD_ONLY_TERMS.search(text) and not _NUMERIC_RESULT.search(text):
        return False
    return False
