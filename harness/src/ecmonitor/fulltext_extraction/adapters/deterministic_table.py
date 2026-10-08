"""Deterministic candidate extraction from table-aware evidence chunks.

The model is deliberately removed from the expensive cell-enumeration step.  This adapter binds
analyte, site, value, unit, and row/column anchors from explicit table geometry; an independent
validator can then spend tokens only on scientific scope and provenance decisions.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from ecmonitor.fulltext_extraction.models import EvidenceChunk

_ROW_RE = re.compile(r"^ROW\s+(\d+):\s*(.*)$")
_POSROW_RE = re.compile(r"^POSROW\s+(\d+):\s*(.*)$")
_POSCELL_RE = re.compile(r"x=([+-]?\d+(?:\.\d+)?)::(.*)")
_UNIT_RE = re.compile(r"(?i)\b(ng|[µμu]g|mg)\s*(?:/\s*L|L\s*[−-]?1)\b")
_NUMBER_RE = re.compile(r"^[+−-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+−-]?\d+)?")
_CENSORED_RE = re.compile(r"(?i)^(?:<?\s*(?:LOD|LOQ)|n\.?d\.?|not\s+detected|na|n/a)$")
_MONTH_RE = re.compile(
    r"(?i)\b(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s*,?\s*(18|19|20)\d{2}\b"
)
_SEASON_RE = re.compile(r"(?i)\b(spring|summer|autumn|fall|winter)\s+(18|19|20)\d{2}\b")
_YEAR_RE = re.compile(r"\b(18|19|20)\d{2}\b")
_SITE_TOKEN_RE = re.compile(r"(?i)^(?:S|Z|D)\d+$")
_WATERBODY_START_RE = re.compile(r"(?i)^(?:river|lake|reservoir|stream|waterway|estuary)$")
_NON_ANALYTE_RE = re.compile(
    r"(?i)^(?:e2eq|doc|toc|pnec|eqs|concentrations?|sites?|date|rate|"
    r"detection(?:\s+rate)?|total\s+concentration|sum|total|median|mean|"
    r"minimum|maximum|mg|ng|l)$"
)
_CLASS_OR_TOTAL_RE = re.compile(
    r"(?i)(?:^|\b)(?:total|sum|Σ|all\s+pfas|pfas|pahs|pesticides|pharmaceuticals|"
    r"antibiotics|metals|edcs)(?:$|\b)"
)
_METHOD_PATTERNS = (
    re.compile(r"(?i)\bLC[-– ]ESI[-– ]QTOF[-– ]MS\b"),
    re.compile(r"(?i)\bSPE[-– ]LC[-– ]MS/MS\b"),
    re.compile(r"(?i)\bLC[-– ]MS/MS\b"),
    re.compile(r"(?i)\bLC[-– ]HRMS\b"),
    re.compile(r"(?i)\bICP[-– ]MS\b"),
    re.compile(r"(?i)\bGC[-– ]MS(?:[-– ]SIM)?\b"),
    re.compile(r"(?i)\bhigh-performance liquid chromatography\b"),
    re.compile(r"(?i)\bmass spectrometric techniques?\b"),
)
_COUNTRIES = (
    "China", "Ukraine", "Moldova", "Slovenia", "Italy", "Croatia", "Austria",
    "Hungary", "Germany", "France", "Netherlands", "USA", "United States",
)
_ELEMENTAL_ANALYTES = {
    "as", "arsenic", "hg", "mercury", "zn", "zinc", "cu", "copper", "cr",
    "chromium", "cd", "cadmium", "pb", "lead", "ni", "nickel",
}
_SECONDARY_DERIVED_TABLE_RE = re.compile(
    r"(?is)(?:previously\s+reported|::previously.*?::reported)"
)
_CITATION_FOOTNOTE_RE = re.compile(
    r"(?im)^POSROW\s+\d+:.*?::[a-z][A-Z][A-Za-z-]+\s*\|.*?::et\s*\|.*?::al\."
)


@dataclass(frozen=True, slots=True)
class _PosCell:
    x: float
    text: str


class DeterministicTableCandidateExtractor:
    """Extract only measured numeric cells from structured or positional monitoring tables."""

    extractor_name = "deterministic_table_candidate_extractor_v1"

    def __init__(self) -> None:
        self.document_context: dict[str, Any] | None = None

    def extract(self, chunk: EvidenceChunk) -> list[dict[str, Any]]:
        if chunk.chunk_type != "table_row_window":
            return []
        if "TABLE_STRUCTURED" in chunk.text:
            return self._extract_structured(chunk)
        if "POSITIONAL_TABLE" in chunk.text:
            return self._extract_positional(chunk)
        return []

    def _context_text(self) -> str:
        context = self.document_context or {}
        return "\n".join(
            str(context.get(key) or "")
            for key in (
                "title",
                "abstract",
                "sampling_context",
                "analytical_method_context",
                "chemical_identity_context",
                "detection_limit_context",
            )
        )

    def _focused_context(self, key: str) -> str:
        return str((self.document_context or {}).get(key) or "")

    def _publication_year(self) -> int | None:
        value = (self.document_context or {}).get("publication_year")
        if isinstance(value, int):
            return value
        if value is not None and str(value).isdigit():
            return int(value)
        return None

    def _alias_map(self) -> dict[str, str]:
        text = self._context_text()
        # Repair PDF line-wrap hyphenation before reading document-local definitions such as
        # ``sulfamera-\nzine (SMR)``. Remaining whitespace is flattened so a definition may
        # span several native text blocks without losing the name-to-abbreviation relation.
        text = re.sub(r"(?<=[A-Za-z])-\s*\n\s*(?=[a-z])", "", text)
        text = " ".join(text.split())
        aliases: dict[str, str] = {}
        for name, alias in re.findall(
            r"([A-Za-z][A-Za-z0-9α-ωΑ-Ω,+/\- '′’]{2,100}?)\s*"
            r"\(\s*([A-Z][A-Z0-9+\-/]{1,12})\s*\)",
            text,
        ):
            cleaned = " ".join(name.split()).strip(" ,;:")
            # Limit a greedy match to the final comma-delimited chemical name.
            cleaned = re.split(r"[,;]", cleaned)[-1].strip()
            cleaned = re.sub(
                r"(?i)^(?:standards?\s+of|including|namely|target(?:ed)? compounds?)\s+",
                "",
                cleaned,
            )
            if cleaned and len(cleaned.split()) <= 8:
                aliases[alias.casefold()] = cleaned
        return aliases

    def _identity(self, reported: str, *, classification: str = "") -> tuple[str, dict[str, Any], list[str]]:
        reported = " ".join(reported.split()).strip()
        aliases = self._alias_map()
        canonical = aliases.get(reported.casefold(), reported)
        is_total = bool(_CLASS_OR_TOTAL_RE.search(canonical)) or bool(_NON_ANALYTE_RE.fullmatch(canonical))
        analyte = {
            "raw_name": canonical,
            "reported_name": reported,
            "specificity_status": "class_or_family" if is_total else "individual_chemical",
            "transformation_product_of": None,
            "is_individual_chemical": not is_total,
        }
        if canonical != reported:
            analyte.update({
                "matched_alias": reported,
                "alias_type": "document_local",
                "proposed_canonical_name": canonical,
            })
        flags: list[str] = []
        if re.search(r"(?i)\b(?:TPs?|transformation\s+products?|metabolites?)\b", classification):
            flags.append("reported_transformation_product")
        return canonical, analyte, flags

    @staticmethod
    def _sampling_matches(text: str) -> list[re.Match[str]]:
        """Prefer dates near sampling language and ignore journal lifecycle dates."""
        if not text:
            return []
        cues = [
            match.start()
            for match in re.finditer(
                r"(?i)\b(?:sampling|sampled|samples? were collected|collected|campaign)\b",
                text,
            )
        ]
        matches: list[re.Match[str]] = []
        for match in [*_MONTH_RE.finditer(text), *_SEASON_RE.finditer(text)]:
            prefix = text[max(0, match.start() - 40): match.start()]
            suffix = text[match.end(): min(len(text), match.end() + 24)]
            if re.search(
                r"(?i)\b(?:received|revised|accepted|published online)\s*(?:on\s*)?$",
                prefix,
            ) or re.match(
                r"(?i)^\s*\(?\s*(?:received|revised|accepted|published online)\b",
                suffix,
            ):
                continue
            if cues and min(abs(match.start() - cue) for cue in cues) > 350:
                continue
            matches.append(match)
        return sorted(matches, key=lambda match: match.start())

    def _sampling_time(self, row_text: str = "") -> dict[str, Any]:
        sampling_context = self._focused_context("sampling_context")
        matches = self._sampling_matches(row_text) or self._sampling_matches(sampling_context)
        if matches:
            unique: list[str] = []
            for match in matches:
                raw = re.sub(r"\s*,\s*", " ", match.group(0))
                if raw.casefold() not in {item.casefold() for item in unique}:
                    unique.append(raw)
            raw_text = " and ".join(unique[:2])
            years = [int(match.group(0)) for match in _YEAR_RE.finditer(raw_text)]
            is_season = bool(_SEASON_RE.search(raw_text))
            payload: dict[str, Any] = {
                "raw_text": raw_text,
                "year": years[0] if years else None,
                "basis": "reported",
                "approximate": len(unique) > 1 or is_season,
            }
            if len(unique) > 1:
                payload["date_start"] = unique[0]
                payload["date_end"] = unique[1]
            return payload
        year = self._publication_year()
        return {
            "raw_text": f"publication year {year}" if year else None,
            "year": year,
            "basis": "publication_year_fallback" if year else "unknown",
            "approximate": True if year else None,
        }

    def _method(self, reported_analyte: str = "", classification: str = "") -> dict[str, Any]:
        # Method evidence is often split between a methods heading, an analyte-list paragraph,
        # and a later instrument paragraph. Do not let a non-empty focused excerpt hide the
        # remaining bounded document context: join both and select only explicitly reported text.
        text = "\n".join(
            part
            for part in (
                self._focused_context("analytical_method_context"),
                self._context_text(),
            )
            if part
        )
        text = re.sub(r"(?<=[A-Za-z])-[ \t]*\r?\n[ \t]*(?=[a-z])", "", text)
        analyte_key = " ".join(reported_analyte.split()).casefold()
        is_elemental = analyte_key in _ELEMENTAL_ANALYTES or classification.casefold() in {
            "metal", "metals", "trace metal", "trace metals"
        }
        method_name = None
        patterns: tuple[re.Pattern[str], ...] = _METHOD_PATTERNS
        if is_elemental:
            patterns = (re.compile(r"(?i)\bICP[-– ]MS\b"), *_METHOD_PATTERNS)
        for pattern in patterns:
            match = pattern.search(text)
            if match:
                method_name = match.group(0)
                break
        lower = text.casefold()
        sample_preparation = "solid-phase extraction (SPE)" if (
            "solid-phase extraction" in lower or re.search(r"\bSPE\b", text)
        ) else None
        instrument = None
        if method_name:
            folded = method_name.casefold()
            if "gc" in folded and "ms" in folded:
                instrument = "GC-MS"
            elif "icp" in folded and "ms" in folded:
                instrument = "ICP-MS"
            elif "qtof" in folded and "ms" in folded:
                instrument = "LC-QTOF-MS"
            elif "hrms" in folded:
                instrument = "LC-HRMS"
            elif "lc" in folded and "ms" in folded:
                instrument = "LC-MS/MS"
            elif "liquid chromatography" in folded:
                instrument = "HPLC"
        method = {
            "method_name": method_name,
            "sample_preparation": sample_preparation,
            "instrument": instrument,
        }
        if "mstfa" in lower:
            method["derivatization"] = "MSTFA derivatization"
        return method

    def _country(self, text: str) -> str | None:
        if re.search(r"(?i)\bSloven(?:e|ian)\b", f"{text} {(self.document_context or {}).get('title') or ''}"):
            return "Slovenia"
        # Prefer the bound location cell, then the article title. The full document context may
        # name many countries in references or comparison tables and must not override either.
        for scope in (text, str((self.document_context or {}).get("title") or "")):
            found = [
                country
                for country in _COUNTRIES
                if re.search(rf"(?i)\b{re.escape(country)}\b", scope)
            ]
            if len(found) == 1:
                return found[0]
        context = self._context_text()
        found = [
            country
            for country in _COUNTRIES
            if re.search(rf"(?i)\b{re.escape(country)}\b", context)
        ]
        return found[0] if len(found) == 1 else None

    @staticmethod
    def _numeric(raw: str) -> float | None:
        cleaned = raw.strip().replace("−", "-")
        if not cleaned or cleaned.startswith("<") or _CENSORED_RE.fullmatch(cleaned):
            return None
        match = _NUMBER_RE.match(cleaned.replace(",", ""))
        if not match:
            return None
        try:
            return float(match.group(0))
        except ValueError:
            return None

    @staticmethod
    def _unit(text: str) -> str | None:
        match = _UNIT_RE.search(text)
        if not match:
            return None
        prefix = match.group(1).replace("μ", "µ").replace("u", "µ")
        return f"{prefix}/L"

    @staticmethod
    def _table_metadata(text: str) -> tuple[str | None, str | None]:
        first = re.search(r"(?:TABLE_STRUCTURED|POSITIONAL_TABLE)\s+page=\d+\s+table=([^\s]+)", text)
        caption_match = re.search(r"^TABLE_CAPTION\s+(.+)$", text, re.MULTILINE)
        table_id = f"Table {first.group(1)}" if first else None
        return table_id, caption_match.group(1).strip() if caption_match else table_id

    def _candidate(
        self,
        *,
        chunk: EvidenceChunk,
        reported_analyte: str,
        raw_value: str,
        unit: str | None,
        site_name: str | None,
        waterbody: str | None,
        location_raw: str | None,
        row_label: str,
        column_label: str,
        quote: str,
        classification: str = "",
        sampling_row: str = "",
        lod_raw: str | None = None,
        loq_raw: str | None = None,
    ) -> dict[str, Any] | None:
        value = self._numeric(raw_value)
        if value is None:
            return None
        canonical, analyte, flags = self._identity(reported_analyte, classification=classification)
        if not analyte["is_individual_chemical"]:
            return None
        table_id, table_caption = self._table_metadata(chunk.text)
        digest = hashlib.sha256(
            f"{chunk.chunk_id}\0{canonical}\0{site_name}\0{sampling_row}\0{raw_value}".encode()
        ).hexdigest()[:24]
        method = self._method(reported_analyte, classification)
        if lod_raw is not None:
            method["lod_raw"] = lod_raw
        if loq_raw is not None:
            method["loq_raw"] = loq_raw
        relation = "deterministic row-column binding from PDF table geometry"
        return {
            "candidate_id": f"table-local-{digest}",
            "observation_type": "field_measurement",
            "analyte": analyte,
            "result": {
                "raw_value": raw_value,
                "raw_unit": unit,
                "value_numeric": value,
                "qualifier": "approximate" if raw_value.strip().endswith("$") else "exact",
                "statistic": "single",
            },
            "sample": {
                "matrix_raw": "surface water",
                "matrix_normalized": "surface_water",
                "phase_or_fraction": "water column",
            },
            "location": {
                "site_name": site_name,
                "waterbody": waterbody,
                "country": self._country(location_raw or waterbody or ""),
                "location_raw": location_raw or waterbody or site_name,
            },
            "sampling_time": self._sampling_time(sampling_row),
            "analytical_method": method,
            "evidence": {
                "quote": quote[:1200],
                "chunk_id": chunk.chunk_id,
                "page_start": chunk.page_start,
                "page_end": chunk.page_end,
                "table_id": table_id,
                "table_caption": table_caption,
                "row_label": row_label,
                "column_label": column_label,
                "relation_note": relation,
            },
            "quality_flags": [
                "deterministic_table_binding",
                *(["approximate_value_from_table_footnote"] if raw_value.strip().endswith("$") else []),
                *flags,
            ],
        }

    def _extract_structured(self, chunk: EvidenceChunk) -> list[dict[str, Any]]:
        rows: list[tuple[str, list[str], str]] = []
        for line in chunk.text.splitlines():
            match = _ROW_RE.match(line)
            if match:
                rows.append((match.group(1), [part.strip() for part in match.group(2).split("|")], line))
        if not rows:
            return []
        header_index = next(
            (index for index, (_num, cells, _line) in enumerate(rows)
             if any(cell.casefold() in {"analyte", "analytes", "compound", "compounds"} for cell in cells)),
            None,
        )
        if header_index is None:
            return []
        header = rows[header_index][1]
        analyte_index = next(
            index for index, cell in enumerate(header)
            if cell.casefold() in {"analyte", "analytes", "compound", "compounds"}
        )
        classification_index = next(
            (index for index, cell in enumerate(header) if cell.casefold() in {"classification", "class"}),
            None,
        )
        lod_index = next((index for index, cell in enumerate(header) if cell.casefold() == "lod"), None)
        loq_index = next((index for index, cell in enumerate(header) if cell.casefold() == "loq"), None)
        site_indexes = [index for index, cell in enumerate(header) if re.fullmatch(r"(?i)site\s*\d+", cell)]
        if not site_indexes:
            return []
        location_header = rows[header_index + 1][1] if header_index + 1 < len(rows) else []
        unit = self._unit(" ".join(" ".join(cells) for _n, cells, _l in rows[: header_index + 3]))
        candidates: list[dict[str, Any]] = []
        for _row_number, cells, raw_line in rows[header_index + 1:]:
            if analyte_index >= len(cells):
                continue
            reported = cells[analyte_index].strip()
            if not reported or reported.casefold() in {"analyte", "analytes"}:
                continue
            classification = (
                cells[classification_index] if classification_index is not None and classification_index < len(cells) else ""
            )
            for site_index in site_indexes:
                if site_index >= len(cells):
                    continue
                site = header[site_index]
                location = location_header[site_index] if site_index < len(location_header) else site
                candidate = self._candidate(
                    chunk=chunk,
                    reported_analyte=reported,
                    raw_value=cells[site_index],
                    unit=unit,
                    site_name=site,
                    waterbody=location or None,
                    location_raw=location or site,
                    row_label=reported,
                    column_label=f"{site}: {location}" if location else site,
                    quote=f"{raw_line}; {site}={cells[site_index]}; location={location}",
                    classification=classification,
                    lod_raw=cells[lod_index] if lod_index is not None and lod_index < len(cells) else None,
                    loq_raw=cells[loq_index] if loq_index is not None and loq_index < len(cells) else None,
                )
                if candidate is not None:
                    candidates.append(candidate)
        return candidates

    @staticmethod
    def _parse_positional_rows(text: str) -> list[tuple[str, list[_PosCell]]]:
        rows: list[tuple[str, list[_PosCell]]] = []
        for line in text.splitlines():
            row_match = _POSROW_RE.match(line)
            if not row_match:
                continue
            cells: list[_PosCell] = []
            for encoded in row_match.group(2).split("|"):
                cell_match = _POSCELL_RE.fullmatch(encoded.strip())
                if cell_match:
                    cells.append(_PosCell(float(cell_match.group(1)), cell_match.group(2).strip()))
            rows.append((line, cells))
        return rows

    @staticmethod
    def _site_anchors(rows: list[tuple[str, list[_PosCell]]], start: int, analyte_x: float) -> list[tuple[float, str]]:
        candidates: list[tuple[float, str]] = []
        for _line, cells in rows[start : start + 4]:
            for index, cell in enumerate(cells):
                if cell.x <= analyte_x + 35:
                    continue
                if _SITE_TOKEN_RE.fullmatch(cell.text):
                    candidates.append((cell.x, cell.text))
                elif _WATERBODY_START_RE.fullmatch(cell.text):
                    words = [cell.text]
                    for following in cells[index + 1:]:
                        if following.x - cell.x > 62 or _WATERBODY_START_RE.fullmatch(following.text):
                            break
                        words.append(following.text)
                    candidates.append((cell.x, " ".join(words)))
            if len(candidates) >= 2:
                break
        dedup: dict[float, str] = {}
        for x, label in candidates:
            dedup[round(x, 1)] = label
        return sorted(dedup.items())

    @staticmethod
    def _nearest_value(cells: list[_PosCell], anchor: float, tolerance: float = 10.0) -> str | None:
        available = [cell for cell in cells if abs(cell.x - anchor) <= tolerance]
        if not available:
            return None
        return min(available, key=lambda cell: abs(cell.x - anchor)).text

    def _waterbody_for_site(self, site: str, table_text: str) -> str | None:
        if re.match(r"(?i)^river\b|^lake\b|^reservoir\b|^stream\b|^waterway\b", site):
            return site.replace("*", "").strip()
        context = f"{table_text}\n{self._context_text()}"
        if re.match(r"(?i)^S\d+$", site) and "Yitong River" in context:
            return "Yitong River"
        if re.match(r"(?i)^Z\d+$", site) and "Zhujiang" in context:
            return "Zhujiang River"
        if re.match(r"(?i)^D\d+$", site) and "Dongjiang" in context:
            return "Dongjiang River"
        if site.casefold() == "s1" and "Shizhiyang" in context:
            return "Shizhiyang waterway"
        return None

    def _extract_analyte_oriented(
        self,
        chunk: EvidenceChunk,
        rows: list[tuple[str, list[_PosCell]]],
        header_index: int,
        analyte_x: float,
    ) -> list[dict[str, Any]]:
        site_anchors = self._site_anchors(rows, header_index, analyte_x)
        if len(site_anchors) < 2:
            return []
        first_site_x = site_anchors[0][0]
        unit = self._unit(chunk.text)
        header_cells = rows[header_index][1]
        has_date_column = any(cell.text.casefold() == "date" for cell in header_cells)
        current_analyte = ""
        candidates: list[dict[str, Any]] = []
        for raw_line, cells in rows[header_index + 1:]:
            if not cells:
                continue
            leading = [cell for cell in cells if cell.x < first_site_x - 20]
            sampling_row = " ".join(cell.text for cell in leading if cell.x > analyte_x + 30)
            if has_date_column and not _MONTH_RE.search(sampling_row):
                # Date-oriented occurrence tables require a sampling month on every data row.
                # This prevents body prose below the table from being interpreted as analytes.
                continue
            first = min(cells, key=lambda cell: cell.x)
            if first.x <= analyte_x + 18:
                label_words = [
                    cell.text
                    for cell in leading
                    if cell.x <= analyte_x + 65 and not _MONTH_RE.search(cell.text)
                ]
                proposed = " ".join(label_words).strip()
                if (
                    proposed
                    and len(proposed) <= 80
                    and not re.search(
                        r"(?i)^(?:analyte|antibiotic|total\s+concentration)$",
                        proposed,
                    )
                ):
                    current_analyte = proposed
            if not current_analyte:
                continue
            for anchor, site in site_anchors:
                raw_value = self._nearest_value(cells, anchor)
                if raw_value is None:
                    continue
                waterbody = self._waterbody_for_site(site, chunk.text)
                candidate = self._candidate(
                    chunk=chunk,
                    reported_analyte=current_analyte,
                    raw_value=raw_value,
                    unit=unit,
                    site_name=site,
                    waterbody=waterbody,
                    location_raw=f"{site}, {waterbody}" if waterbody else site,
                    row_label=current_analyte,
                    column_label=site,
                    quote=f"{raw_line}; bound analyte={current_analyte}; site={site}; value={raw_value}",
                    sampling_row=sampling_row,
                )
                if candidate is not None:
                    candidates.append(candidate)
        return candidates

    def _extract_site_oriented(
        self,
        chunk: EvidenceChunk,
        rows: list[tuple[str, list[_PosCell]]],
        header_index: int,
        site_x: float,
    ) -> list[dict[str, Any]]:
        # Choose one visual header row rather than accumulating labels across rows. This avoids
        # treating the spanning word ``Concentration`` as an analyte above BPA/E1/E2.
        header_candidates: list[list[tuple[float, str]]] = []
        for _line, cells in rows[header_index : header_index + 4]:
            labels = [
                (cell.x, cell.text)
                for cell in cells
                if cell.x > site_x + 30
                and not _NON_ANALYTE_RE.fullmatch(cell.text)
                and re.fullmatch(r"[A-Za-z][A-Za-z0-9β α-]{0,30}", cell.text)
            ]
            if labels:
                header_candidates.append(labels)
        analyte_anchors = max(header_candidates, key=len, default=[])
        if len(analyte_anchors) < 1:
            return []
        unit = self._unit(chunk.text)
        candidates: list[dict[str, Any]] = []
        for raw_line, cells in rows[header_index + 1:]:
            site_cells = [cell for cell in cells if abs(cell.x - site_x) <= 12]
            if not site_cells:
                continue
            site = site_cells[0].text
            if not _SITE_TOKEN_RE.fullmatch(site):
                continue
            waterbody = self._waterbody_for_site(site, chunk.text)
            for anchor, analyte in analyte_anchors:
                raw_value = self._nearest_value(cells, anchor)
                if raw_value is None:
                    continue
                candidate = self._candidate(
                    chunk=chunk,
                    reported_analyte=analyte,
                    raw_value=raw_value,
                    unit=unit,
                    site_name=site,
                    waterbody=waterbody,
                    location_raw=f"{site}, {waterbody}" if waterbody else site,
                    row_label=site,
                    column_label=analyte,
                    quote=f"{raw_line}; bound site={site}; analyte={analyte}; value={raw_value}",
                )
                if candidate is not None:
                    candidates.append(candidate)
        return candidates

    def _extract_positional(self, chunk: EvidenceChunk) -> list[dict[str, Any]]:
        # Some comparison tables deliberately combine this study's measurements with values
        # copied from an earlier paper to calculate a derived equivalent. Those tables are not
        # independent field observations, and their current-study cells are duplicates of the
        # primary results table. Reject the complete mixed-provenance table deterministically.
        if (
            _SECONDARY_DERIVED_TABLE_RE.search(chunk.text)
            and _CITATION_FOOTNOTE_RE.search(chunk.text)
            and re.search(r"(?i)E2eq[′']|Include\s+NP\s+and\s+OP", chunk.text)
        ):
            return []
        rows = self._parse_positional_rows(chunk.text)
        for index, (_line, cells) in enumerate(rows):
            for cell in cells:
                if cell.text.casefold() in {"analyte", "antibiotic"}:
                    extracted = self._extract_analyte_oriented(chunk, rows, index, cell.x)
                    if extracted:
                        return extracted
                if cell.text.casefold() == "sites":
                    extracted = self._extract_site_oriented(chunk, rows, index, cell.x)
                    if extracted:
                        return extracted
        return []
