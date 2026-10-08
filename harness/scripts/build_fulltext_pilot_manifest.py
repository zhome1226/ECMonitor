"""Build a stratified, auditable pilot manifest for full-text extraction."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pfas", ("pfas", "per- and polyfluoro", "perfluoro", "polyfluoro")),
    ("pesticides", ("pesticide", "herbicide", "insecticide", "fungicide")),
    (
        "pharmaceuticals_antibiotics",
        ("pharmaceutical", "antibiotic", "antimicrobial", "drug residue", "medicine"),
    ),
    (
        "flame_retardants_industrial_additives",
        ("flame retardant", "organophosphate", "plasticizer", "industrial additive"),
    ),
    ("hormones", ("hormone", "estrogen", "oestrogen", "endocrine")),
    ("microplastics_nanoplastics", ("microplastic", "nanoplastic", "plastic particle")),
    ("tire_wear_6ppd", ("tire wear", "tyre wear", "6ppd", "rubber-derived")),
)

NEGATIVE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "method_validation_negative",
        ("method validation", "validation of", "analytical method", "method development"),
    ),
    ("toxicology_negative", ("toxicology", "toxicity", "toxicological", "cytotoxicity")),
    ("review_negative", ("review", "systematic review", "meta-analysis", "overview")),
)


def _normalise(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def _sample_pdf(path: Path, sample_pages: int) -> dict[str, Any]:
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("PyMuPDF is required; install the fulltext-pdf extra") from exc

    with pymupdf.open(path) as pdf:  # type: ignore[no-untyped-call]
        page_count = len(pdf)
        pages = [pdf[index] for index in range(min(page_count, sample_pages))]
        texts: list[str] = []
        block_counts: list[int] = []
        x_clusters: list[int] = []
        table_pages = 0
        for page in pages:
            text = str(page.get_text("text") or "").strip()
            texts.append(text)
            blocks = page.get_text("blocks")
            block_counts.append(len(blocks))
            starts = sorted(float(block[0]) for block in blocks if len(block) >= 4)
            clusters = 0
            previous_start: float | None = None
            for start in starts:
                if previous_start is None or start - previous_start > 90:
                    clusters += 1
                previous_start = start
            x_clusters.append(clusters)
            lower = text.lower()
            if (
                ("table" in lower or "supplementary" in lower)
                and sum(char.isdigit() for char in text) >= 12
            ):
                table_pages += 1
        sampled = len(texts)
        zero_pages = sum(not text for text in texts)
        combined = "\n".join(texts)
        return {
            "page_count": page_count,
            "sampled_pages": sampled,
            "sampled_zero_text_fraction": round(zero_pages / sampled, 4) if sampled else 1.0,
            "sampled_text_chars": len(combined),
            "sampled_block_count": sum(block_counts),
            "sampled_table_pages": table_pages,
            "sampled_two_column_pages": sum(clusters >= 2 for clusters in x_clusters),
            "sample_text": combined[:50000],
        }


def _tags(row: dict[str, str], sample: dict[str, Any]) -> list[str]:
    text = _normalise(f"{row.get('title', '')} {sample.get('sample_text', '')}")
    tags: list[str] = []
    for tag, terms in CATEGORY_RULES:
        if any(term in text for term in terms):
            tags.append(tag)
    for tag, terms in NEGATIVE_RULES:
        if any(term in text for term in terms):
            tags.append(tag)

    if sample["sampled_zero_text_fraction"] >= 0.5 or sample["sampled_text_chars"] < 500:
        tags.append("low_native_text")
    else:
        tags.append("native_text")
    if sample["sampled_two_column_pages"]:
        tags.append("double_column")
    if sample["sampled_table_pages"]:
        tags.append("complex_table")
    if sample["sampled_table_pages"] >= 2:
        tags.append("multi_page_table")
    if re.search(r"\b(multiple|several|simultaneous|multi[- ]analyte|compounds?)\b", text):
        tags.append("multi_analyte")
    if re.search(
        r"\b(groundwater|surface water|wastewater|sediment|soil|urine|blood|air|dust)\b",
        text,
    ):
        tags.append("multi_matrix")
    if re.search(
        r"\b(site|sites|location|locations|river|basin|city|cities|country|countries)\b",
        text,
    ):
        tags.append("multi_site")
    return list(dict.fromkeys(tags))


def _stable_score(row: dict[str, str]) -> str:
    return hashlib.sha1(row.get("doi", row.get("unified_pdf_path", "")).encode()).hexdigest()


def build_manifest(
    source_manifest: Path,
    output_manifest: Path,
    output_summary: Path,
    *,
    target: int,
    sample_pages: int,
) -> dict[str, Any]:
    with source_manifest.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    candidates: list[dict[str, Any]] = []
    missing = 0
    errors = 0
    for row in rows:
        path_value = row.get("unified_pdf_path") or row.get("pdf_path") or row.get("source_path")
        if not path_value:
            missing += 1
            continue
        path = Path(path_value)
        if not path.is_file():
            missing += 1
            continue
        try:
            sample = _sample_pdf(path, sample_pages)
        except Exception as exc:  # keep one corrupt PDF from aborting pilot construction
            errors += 1
            sample = {
                "page_count": 0,
                "sampled_pages": 0,
                "sampled_zero_text_fraction": 1.0,
                "sampled_text_chars": 0,
                "sampled_block_count": 0,
                "sampled_table_pages": 0,
                "sampled_two_column_pages": 0,
                "sample_text": "",
                "sampling_error": f"{type(exc).__name__}: {exc}",
            }
        tags = _tags(row, sample)
        candidates.append({"row": row, "path": str(path.resolve()), "tags": tags, **sample})

    required = [tag for tag, _ in CATEGORY_RULES] + [tag for tag, _ in NEGATIVE_RULES]
    required += [
        "low_native_text",
        "native_text",
        "double_column",
        "complex_table",
        "multi_page_table",
        "multi_analyte",
        "multi_matrix",
        "multi_site",
    ]
    selected: list[dict[str, Any]] = []
    remaining = sorted(candidates, key=lambda item: _stable_score(item["row"]))
    for tag in required:
        match = next((item for item in remaining if tag in item["tags"]), None)
        if match is not None:
            selected.append(match)
            remaining.remove(match)
    selected.extend(remaining[: max(0, target - len(selected))])
    selected = selected[:target]

    output_manifest.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "pilot_order", "pdf_path", "doi", "title", "journal", "strata", "selection_reason",
        "page_count", "sampled_pages", "sampled_zero_text_fraction", "sampled_text_chars",
        "sampled_block_count", "sampled_table_pages", "sampled_two_column_pages", "sampling_error",
    ]
    with output_manifest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for index, item in enumerate(selected, start=1):
            row = item["row"]
            strata = ";".join(item["tags"])
            writer.writerow({
                "pilot_order": index,
                "pdf_path": item["path"],
                "doi": row.get("doi", ""),
                "title": row.get("title", ""),
                "journal": row.get("journal", ""),
                "strata": strata,
                "selection_reason": f"coverage={strata}",
                **{
                    key: item.get(key, "")
                    for key in fields
                    if key.startswith("page_count")
                    or key.startswith("sampled_")
                    or key == "sampling_error"
                },
            })

    summary = {
        "source_manifest": str(source_manifest.resolve()),
        "output_manifest": str(output_manifest.resolve()),
        "target": target,
        "selected": len(selected),
        "source_rows": len(rows),
        "usable_candidates": len(candidates),
        "missing": missing,
        "sampling_errors": errors,
        "strata_counts": dict(Counter(tag for item in selected for tag in item["tags"])),
        "selection_policy": (
            "one deterministic representative per requested stratum, then stable hash fill"
        ),
    }
    output_summary.parent.mkdir(parents=True, exist_ok=True)
    output_summary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--output-manifest", type=Path, required=True)
    parser.add_argument("--output-summary", type=Path, required=True)
    parser.add_argument("--target", type=int, default=40)
    parser.add_argument("--sample-pages", type=int, default=6)
    args = parser.parse_args()
    if args.target < 1 or args.sample_pages < 1:
        parser.error("--target and --sample-pages must be positive")
    print(json.dumps(build_manifest(**vars(args)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
