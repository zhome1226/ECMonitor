#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any


DOI_PATTERN = re.compile(r"10\.\d{4,9}/\S+", re.IGNORECASE)
DEFAULT_WORLD_BANK_INDICATORS = [
    "SP.POP.TOTL",
    "EN.POP.DNST",
    "NY.GDP.PCAP.CD",
]
COUNTRY_TO_ISO3 = {
    "china": "CHN",
    "united states": "USA",
    "usa": "USA",
    "germany": "DEU",
    "argentina": "ARG",
    "canada": "CAN",
    "japan": "JPN",
    "south korea": "KOR",
    "australia": "AUS"
}


def load_rows(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    raise ValueError("Input JSON must be a list of row objects.")


def first_present(row: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, "", []):
            return value
    return None


def normalize_doi(value: Any) -> str | None:
    if value in (None, ""):
        return None
    match = DOI_PATTERN.search(str(value).strip())
    return match.group(0).lower() if match else None


def normalize_country_to_iso3(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if len(text) == 3 and text.isalpha():
        return text.upper()
    return COUNTRY_TO_ISO3.get(text.lower())


def build_crossref_url(doi: str) -> str:
    return f"https://api.crossref.org/works/{urllib.parse.quote(doi, safe='')}"


def build_openalex_url(doi: str) -> str:
    base = f"https://api.openalex.org/works/https://doi.org/{urllib.parse.quote(doi, safe='')}"
    api_key = os.getenv("OPENALEX_API_KEY")
    if api_key:
        return f"{base}?api_key={urllib.parse.quote(api_key)}"
    return base


def build_world_bank_url(country_iso3: str, indicator: str, start_year: int, end_year: int) -> str:
    query = urllib.parse.urlencode({"format": "json", "date": f"{start_year}:{end_year}", "per_page": 20000})
    return f"https://api.worldbank.org/v2/country/{country_iso3}/indicator/{indicator}?{query}"


def fetch_json(url: str, user_agent: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def load_json_if_exists(path: Path) -> Any | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_crossref_payload(payload: Any) -> dict[str, Any] | None:
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, dict):
        return None
    authors = message.get("author") or []
    published = message.get("published-print") or message.get("published-online") or message.get("created") or {}
    date_parts = published.get("date-parts") or [[]]
    year = date_parts[0][0] if date_parts and date_parts[0] else None
    return {
        "doi": normalize_doi(message.get("DOI")),
        "title": (message.get("title") or [None])[0],
        "publisher": message.get("publisher"),
        "journal": (message.get("container-title") or [None])[0],
        "year": year,
        "type": message.get("type"),
        "citation_count": message.get("is-referenced-by-count"),
        "author_count": len(authors),
        "url": message.get("URL"),
    }


def normalize_openalex_payload(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    primary_location = payload.get("primary_location") or {}
    source = primary_location.get("source") or {}
    concepts = payload.get("concepts") or []
    institutions = []
    for authorship in payload.get("authorships") or []:
        for institution in authorship.get("institutions") or []:
            display_name = institution.get("display_name")
            if display_name and display_name not in institutions:
                institutions.append(display_name)
    doi = payload.get("doi")
    if not doi:
        ids = payload.get("ids") or {}
        doi = ids.get("doi")
    normalized_doi = normalize_doi(doi)
    return {
        "doi": normalized_doi,
        "openalex_id": payload.get("id"),
        "title": payload.get("title"),
        "publication_year": payload.get("publication_year"),
        "cited_by_count": payload.get("cited_by_count"),
        "journal": source.get("display_name"),
        "is_oa": payload.get("open_access", {}).get("is_oa"),
        "oa_status": payload.get("open_access", {}).get("oa_status"),
        "top_concepts": [concept.get("display_name") for concept in concepts[:5] if concept.get("display_name")],
        "institutions": institutions[:10],
    }


def normalize_world_bank_payload(country_iso3: str, indicator: str, payload: Any) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[1], list):
        return [], None
    records: list[dict[str, Any]] = []
    for row in payload[1]:
        record = {
            "country_iso3": country_iso3,
            "indicator": indicator,
            "country_name": row.get("country", {}).get("value"),
            "indicator_name": row.get("indicator", {}).get("value"),
            "year": row.get("date"),
            "value": row.get("value"),
        }
        records.append(record)
    latest = next((record for record in records if record["value"] not in (None, "")), None)
    return records, latest


def normalize_saved_payloads(out_dir: Path) -> dict[str, Any]:
    crossref_rows: list[dict[str, Any]] = []
    openalex_rows: list[dict[str, Any]] = []
    world_bank_rows: list[dict[str, Any]] = []
    world_bank_latest: list[dict[str, Any]] = []

    for path in sorted((out_dir / "crossref").glob("*.json")) if (out_dir / "crossref").exists() else []:
        payload = load_json_if_exists(path)
        row = normalize_crossref_payload(payload)
        if row:
            crossref_rows.append(row)

    for path in sorted((out_dir / "openalex").glob("*.json")) if (out_dir / "openalex").exists() else []:
        payload = load_json_if_exists(path)
        row = normalize_openalex_payload(payload)
        if row:
            openalex_rows.append(row)

    for path in sorted((out_dir / "world_bank").glob("*/*.json")) if (out_dir / "world_bank").exists() else []:
        payload = load_json_if_exists(path)
        country_iso3 = path.parent.name
        indicator = path.stem
        records, latest = normalize_world_bank_payload(country_iso3, indicator, payload)
        world_bank_rows.extend(records)
        if latest:
            world_bank_latest.append(latest)

    normalized_dir = out_dir / "normalized"
    normalized_dir.mkdir(parents=True, exist_ok=True)
    (normalized_dir / "crossref_works.json").write_text(json.dumps(crossref_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (normalized_dir / "openalex_works.json").write_text(json.dumps(openalex_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (normalized_dir / "world_bank_records.json").write_text(json.dumps(world_bank_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (normalized_dir / "world_bank_latest.json").write_text(json.dumps(world_bank_latest, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = {
        "crossref_rows": len(crossref_rows),
        "openalex_rows": len(openalex_rows),
        "world_bank_rows": len(world_bank_rows),
        "world_bank_latest_rows": len(world_bank_latest),
        "crossref_publishers": dict(Counter(row["publisher"] for row in crossref_rows if row.get("publisher")).most_common(10)),
        "openalex_journals": dict(Counter(row["journal"] for row in openalex_rows if row.get("journal")).most_common(10)),
        "world_bank_indicators": dict(Counter(row["indicator"] for row in world_bank_latest if row.get("indicator")).most_common()),
    }
    (normalized_dir / "context_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "normalized_dir": str(normalized_dir),
        "summary": summary,
        "paths": {
            "crossref": str(normalized_dir / "crossref_works.json"),
            "openalex": str(normalized_dir / "openalex_works.json"),
            "world_bank_records": str(normalized_dir / "world_bank_records.json"),
            "world_bank_latest": str(normalized_dir / "world_bank_latest.json"),
            "context_summary": str(normalized_dir / "context_summary.json"),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build or fetch Crossref/OpenAlex/World Bank context adapters for validated ECMonitor rows.")
    parser.add_argument("--input", required=True, type=Path, help="Validated rows JSON.")
    parser.add_argument("--out-dir", required=True, type=Path, help="Directory for context outputs.")
    parser.add_argument("--fetch", action="store_true", help="Actually call the remote APIs. Without this flag, only a request manifest is created.")
    parser.add_argument("--start-year", type=int, default=2010)
    parser.add_argument("--end-year", type=int, default=2024)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    rows = load_rows(args.input)

    dois = sorted({doi for doi in (normalize_doi(row.get("doi")) for row in rows) if doi})
    countries = sorted(
        {
            iso3
            for iso3 in (
                normalize_country_to_iso3(first_present(row, ["country", "geographic_scope", "location"]))
                for row in rows
            )
            if iso3
        }
    )

    manifest = {
        "input_path": str(args.input),
        "out_dir": str(args.out_dir),
        "dois": dois,
        "countries": countries,
        "requests": {
            "crossref": [{"doi": doi, "url": build_crossref_url(doi)} for doi in dois],
            "openalex": [{"doi": doi, "url": build_openalex_url(doi)} for doi in dois],
            "world_bank": [
                {
                    "country_iso3": iso3,
                    "indicator": indicator,
                    "url": build_world_bank_url(iso3, indicator, args.start_year, args.end_year),
                }
                for iso3 in countries
                for indicator in DEFAULT_WORLD_BANK_INDICATORS
            ],
        },
        "fetched": False,
        "fetch_results": {"crossref": [], "openalex": [], "world_bank": []},
        "normalized_outputs": None,
    }

    if args.fetch:
        user_agent = "ECMonitorAnalyticsSpecialist/1.0 (context adapter)"
        for item in manifest["requests"]["crossref"]:
            try:
                payload = fetch_json(item["url"], user_agent)
                dest = args.out_dir / "crossref" / f"{item['doi'].replace('/', '_')}.json"
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                manifest["fetch_results"]["crossref"].append({"doi": item["doi"], "status": "ok", "path": str(dest)})
            except Exception as exc:
                manifest["fetch_results"]["crossref"].append({"doi": item["doi"], "status": "error", "message": str(exc)})

        for item in manifest["requests"]["openalex"]:
            try:
                payload = fetch_json(item["url"], user_agent)
                dest = args.out_dir / "openalex" / f"{item['doi'].replace('/', '_')}.json"
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                manifest["fetch_results"]["openalex"].append({"doi": item["doi"], "status": "ok", "path": str(dest)})
            except Exception as exc:
                manifest["fetch_results"]["openalex"].append({"doi": item["doi"], "status": "error", "message": str(exc)})

        for item in manifest["requests"]["world_bank"]:
            try:
                payload = fetch_json(item["url"], user_agent)
                dest = args.out_dir / "world_bank" / item["country_iso3"] / f"{item['indicator']}.json"
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                manifest["fetch_results"]["world_bank"].append(
                    {"country_iso3": item["country_iso3"], "indicator": item["indicator"], "status": "ok", "path": str(dest)}
                )
            except Exception as exc:
                manifest["fetch_results"]["world_bank"].append(
                    {"country_iso3": item["country_iso3"], "indicator": item["indicator"], "status": "error", "message": str(exc)}
                )

        manifest["fetched"] = True

    manifest["normalized_outputs"] = normalize_saved_payloads(args.out_dir)
    (args.out_dir / "context_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"dois": len(dois), "countries": len(countries), "fetched": args.fetch}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
