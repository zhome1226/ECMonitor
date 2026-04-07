#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import urllib.parse
import urllib.request
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

    (args.out_dir / "context_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"dois": len(dois), "countries": len(countries), "fetched": args.fetch}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
