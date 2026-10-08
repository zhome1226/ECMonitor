"""Offline, deterministic approximate geocoding for extracted occurrence records.

Coordinates are intentionally approximate: the resolver cascades from city-level to
admin1/state-level to country-centroid resolution so records that lack precise station
coordinates still carry a defensible latitude/longitude and a provenance tag.
"""

from __future__ import annotations

import csv
import json
import re
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_CITY_ALIASES: dict[str, str] = {
    "new york": "new york city",
    "nyc": "new york city",
    "washington dc": "washington",
    "washington d.c.": "washington",
    "ho chi minh": "ho chi minh city",
    "saigon": "ho chi minh city",
    "mexico df": "mexico city",
    "panama city": "panama city",
    "kansas city": "kansas city",
    "salt lake city": "salt lake city",
    "oklahoma city": "oklahoma city",
    "guatemala city": "guatemala",
    "luxembourg city": "luxembourg",
    "brussels": "brussels",
    "vatican city": "vatican city",
    "kota bharu": "kota bharu",
    "jerusalem": "jerusalem",
    "tel aviv": "tel aviv",
    "san jose": "san jose",
}


_COUNTRY_ALIASES: dict[str, str] = {
    "usa": "us",
    "u.s.a.": "us",
    "u.s.": "us",
    "united states of america": "us",
    "united states": "us",
    "america": "us",
    "uk": "gb",
    "u.k.": "gb",
    "united kingdom": "gb",
    "great britain": "gb",
    "england": "gb",
    "south korea": "kr",
    "korea": "kr",
    "korea, republic of": "kr",
    "r.o.k.": "kr",
    "viet nam": "vn",
    "vietnam": "vn",
    "russia": "ru",
    "russian federation": "ru",
    "czech republic": "cz",
    "czechia": "cz",
    "iran": "ir",
    "iran, islamic republic of": "ir",
    "taiwan": "tw",
    "taiwan, province of china": "tw",
    "laos": "la",
    "lao people's democratic republic": "la",
    "syria": "sy",
    "syrian arab republic": "sy",
    "bolivia": "bo",
    "vatican": "va",
    "congo (drc)": "cd",
    "dr congo": "cd",
    "democratic republic of the congo": "cd",
    "congo": "cg",
    "macedonia": "mk",
    "north macedonia": "mk",
    "moldova": "md",
    "burma": "mm",
    "myanmar": "mm",
    "swaziland": "sz",
    "eswatini": "sz",
    "cape verde": "cv",
    "ivory coast": "ci",
    "cote d'ivoire": "ci",
    "côte d'ivoire": "ci",
    "east timor": "tl",
    "timor-leste": "tl",
}

# Short, generic tokens that would otherwise match thousands of small settlements.
_STOP_TOKENS = {
    "west",
    "east",
    "north",
    "south",
    "central",
    "upper",
    "lower",
    "new",
    "old",
    "lake",
    "river",
    "bay",
    "gulf",
    "island",
    "city",
    "town",
    "village",
    "park",
    "port",
    "mount",
    "mt",
    "sea",
    "beach",
    "station",
    "plant",
    "farm",
    "creek",
    "spring",
    "falls",
    "rock",
    "hill",
    "view",
    "green",
    "point",
    "san",
    "santa",
    "las",
    "los",
    "st",
    "ste",
}


@dataclass(frozen=True, slots=True)
class GeocodeResult:
    latitude: float | None
    longitude: float | None
    source: str  # city | admin1 | country
    matched_name: str | None
    country_iso2: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "source": self.source,
            "matched_name": self.matched_name,
            "country_iso2": self.country_iso2,
        }


class OfflineGeocodeResolver:
    """Resolve a location description to approximate coordinates from bundled datasets."""

    resolver_name = "offline_geocoder_v1"

    def __init__(self, data_dir: Path | str) -> None:
        self.data_dir = Path(data_dir)
        self._countries_by_iso2: dict[str, dict[str, Any]] | None = None
        self._countries_by_name: dict[str, dict[str, Any]] | None = None
        self._states_by_name_country: dict[tuple[str, str], list[dict[str, Any]]] | None = None
        self._cities_by_name_country: dict[tuple[str, str], list[dict[str, Any]]] | None = None
        self._cities_by_name: dict[str, list[dict[str, Any]]] | None = None
        self._load_lock = threading.Lock()

    # ------------------------------------------------------------------ data
    def _load_countries(self) -> None:
        if self._countries_by_iso2 is not None:
            return
        iso2: dict[str, dict[str, Any]] = {}
        by_name: dict[str, dict[str, Any]] = {}
        path = self.data_dir / "countries.csv"
        if not path.is_file():
            self._countries_by_iso2 = iso2
            self._countries_by_name = by_name
            return
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                try:
                    latitude = float(row.get("latitude") or "")
                    longitude = float(row.get("longitude") or "")
                except ValueError:
                    continue
                record: dict[str, Any] = {
                    "name": (row.get("name") or "").strip(),
                    "iso2": (row.get("iso2") or "").strip().lower(),
                    "iso3": (row.get("iso3") or "").strip().lower(),
                    "latitude": latitude,
                    "longitude": longitude,
                }
                name_key = _normalize(record["name"])
                if record["iso2"]:
                    iso2[record["iso2"]] = record
                if name_key:
                    by_name.setdefault(name_key, record)
                for alias in _COUNTRY_ALIASES:
                    if _normalize(alias) == name_key:
                        by_name.setdefault(_normalize(alias), record)
        self._countries_by_iso2 = iso2
        self._countries_by_name = by_name

    def _load_states(self) -> None:
        if self._states_by_name_country is not None:
            return
        index: dict[tuple[str, str], list[dict[str, Any]]] = {}
        path = self.data_dir / "states.csv"
        if path.is_file():
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    try:
                        latitude = float(row.get("latitude") or "")
                        longitude = float(row.get("longitude") or "")
                    except ValueError:
                        continue
                    name = _normalize(row.get("name") or "")
                    country = _normalize(row.get("country_code") or "")
                    if not name or not country:
                        continue
                    index.setdefault((name, country), []).append(
                        {
                            "name": (row.get("name") or "").strip(),
                            "country": country,
                            "latitude": latitude,
                            "longitude": longitude,
                        }
                    )
        self._states_by_name_country = index

    def _load_cities(self) -> None:
        if self._cities_by_name_country is not None:
            return
        by_name_country: dict[tuple[str, str], list[dict[str, Any]]] = {}
        by_name: dict[str, list[dict[str, Any]]] = {}
        path = self.data_dir / "cities.json"
        if path.is_file():
            with path.open("r", encoding="utf-8") as handle:
                cities = json.load(handle)
            for city in cities:
                name = _normalize(city.get("name") or "")
                if not name or len(name) < 4:
                    continue
                try:
                    latitude = float(city.get("lat") or "")
                    longitude = float(city.get("lng") or "")
                except ValueError:
                    continue
                country = _normalize(city.get("country") or "")
                record = {
                    "name": (city.get("name") or "").strip(),
                    "country": country,
                    "latitude": latitude,
                    "longitude": longitude,
                }
                if country:
                    by_name_country.setdefault((name, country), []).append(record)
                by_name.setdefault(name, []).append(record)
        self._cities_by_name_country = by_name_country
        self._cities_by_name = by_name

    # ------------------------------------------------------------- resolving
    def resolve(self, location: dict[str, Any] | None) -> GeocodeResult | None:
        if not isinstance(location, dict):
            return None
        with self._load_lock:
            self._load_countries()
            self._load_states()
            self._load_cities()
        country_iso2 = _country_iso2(location.get("country"), self._countries_by_iso2 or {})
        if country_iso2 is None:
            country_iso2 = _country_iso2_from_text(
                _combined_text(location), self._countries_by_name or {}
            )

        city = _string_or_none(location.get("city"))
        if city:
            result = self._match_city(city, country_iso2)
            if result is not None:
                return result

        admin1 = _string_or_none(location.get("admin1"))
        if admin1:
            result = self._match_state(admin1, country_iso2)
            if result is not None:
                return result

        text = _combined_text(location)
        city_hit = self._match_city_in_text(text, country_iso2)
        if city_hit is not None:
            return city_hit
        state_hit = self._match_state_in_text(text, country_iso2)
        if state_hit is not None:
            return state_hit

        if country_iso2:
            record = (self._countries_by_iso2 or {}).get(country_iso2)
            if record is not None:
                return GeocodeResult(
                    latitude=record["latitude"],
                    longitude=record["longitude"],
                    source="country",
                    matched_name=record["name"],
                    country_iso2=country_iso2,
                )
        return None

    def validate_admin_hierarchy(self, location: dict[str, Any] | None) -> dict[str, Any]:
        """Validate country-admin1-city compatibility before approximate coordinates are trusted."""
        if not isinstance(location, dict):
            return {"validated": False, "consistent": True, "conflicts": []}
        with self._load_lock:
            self._load_countries()
            self._load_states()
            self._load_cities()
        country_iso2 = _country_iso2(location.get("country"), self._countries_by_iso2 or {})
        conflicts: list[str] = []
        city = _string_or_none(location.get("city"))
        if city:
            city_name = _CITY_ALIASES.get(_normalize(city), _normalize(city))
            city_candidates = (self._cities_by_name or {}).get(city_name, [])
            if country_iso2 and city_candidates and not any(
                (row.get("country") or "").lower() == country_iso2 for row in city_candidates
            ):
                conflicts.append("city_country_mismatch")
        admin1 = _string_or_none(location.get("admin1"))
        if admin1:
            admin_name = _normalize(admin1)
            state_candidates = [
                row
                for (name, _country), rows in (self._states_by_name_country or {}).items()
                if name == admin_name
                for row in rows
            ]
            if country_iso2 and state_candidates and not any(
                (row.get("country") or "").lower() == country_iso2 for row in state_candidates
            ):
                conflicts.append("admin1_country_mismatch")
        return {
            "validated": True,
            "consistent": not conflicts,
            "conflicts": conflicts,
            "country_iso2": country_iso2,
        }
    def _match_city(self, raw: str, country_iso2: str | None) -> GeocodeResult | None:
        name = _normalize(raw)
        if not name or len(name) < 3:
            return None
        name = _CITY_ALIASES.get(name, name)
        index = self._cities_by_name_country or {}
        if country_iso2 and (name, country_iso2) in index:
            record = index[(name, country_iso2)][0]
            return _city_result(record, country_iso2)
        candidates = (self._cities_by_name or {}).get(name, [])
        if not candidates:
            return None
        if len(candidates) == 1:
            record = candidates[0]
            return _city_result(record, record.get("country") or country_iso2)
        chosen = _pick_by_country(candidates, country_iso2)
        if chosen is None:
            return None
        return _city_result(chosen, chosen.get("country") or country_iso2)

    def _match_city_in_text(self, text: str, country_iso2: str | None) -> GeocodeResult | None:
        index = self._cities_by_name or {}
        country_names = self._countries_by_name or {}
        for ngram, _ in _text_ngrams(text):
            if ngram in country_names:
                continue
            if not _plausible_city_ngram(ngram):
                continue
            candidates = index.get(ngram)
            if not candidates:
                continue
            chosen = _pick_by_country(candidates, country_iso2)
            if chosen is None:
                continue
            if _score_city_candidate(chosen, country_iso2, len(ngram.split())) < 0.5:
                continue
            return _city_result(chosen, chosen.get("country") or country_iso2)
        return None

    def _match_state(self, raw: str, country_iso2: str | None) -> GeocodeResult | None:
        name = _normalize(raw)
        if not name:
            return None
        index = self._states_by_name_country or {}
        if country_iso2 and (name, country_iso2) in index:
            record = index[(name, country_iso2)][0]
            return GeocodeResult(
                latitude=record["latitude"],
                longitude=record["longitude"],
                source="admin1",
                matched_name=record["name"],
                country_iso2=country_iso2,
            )
        candidates = [
            record for (state, _), records in index.items() if state == name for record in records
        ]
        if not candidates:
            return None
        chosen = _pick_by_country(candidates, country_iso2)
        if chosen is None:
            return None
        return GeocodeResult(
            latitude=chosen["latitude"],
            longitude=chosen["longitude"],
            source="admin1",
            matched_name=chosen["name"],
            country_iso2=chosen.get("country") or country_iso2,
        )

    def _match_state_in_text(self, text: str, country_iso2: str | None) -> GeocodeResult | None:
        index = self._states_by_name_country or {}
        for ngram, _ in _text_ngrams(text):
            if len(ngram) < 3:
                continue
            if country_iso2 and (ngram, country_iso2) in index:
                record = index[(ngram, country_iso2)][0]
                return GeocodeResult(
                    latitude=record["latitude"],
                    longitude=record["longitude"],
                    source="admin1",
                    matched_name=record["name"],
                    country_iso2=country_iso2,
                )
        return None


# ------------------------------------------------------------------- helpers


def _city_result(record: dict[str, Any], country_iso2: str | None) -> GeocodeResult:
    return GeocodeResult(
        latitude=record["latitude"],
        longitude=record["longitude"],
        source="city",
        matched_name=record["name"],
        country_iso2=country_iso2,
    )


def _pick_by_country(candidates: list[dict[str, Any]], country_iso2: str | None) -> dict[str, Any] | None:
    if not candidates:
        return None
    if country_iso2:
        for record in candidates:
            if (record.get("country") or "").lower() == country_iso2:
                return record
        return None
    return candidates[0]


def _score_city_candidate(record: dict[str, Any], country_iso2: str | None, words: int) -> float:
    score = 0.6
    if words >= 2:
        score += 0.2
    if country_iso2 and (record.get("country") or "").lower() == country_iso2:
        score += 0.2
    return score


def _plausible_city_ngram(name: str) -> bool:
    tokens = name.split()
    if len(tokens) >= 2:
        return all(len(token) >= 3 for token in tokens)
    token = tokens[0]
    return len(token) >= 4 and token not in _STOP_TOKENS


def _text_ngrams(text: str, max_words: int = 4) -> list[tuple[str, float]]:
    tokens = _normalize(text).split()
    results: list[tuple[str, float]] = []
    for size in range(max_words, 0, -1):
        for start in range(0, max(0, len(tokens) - size + 1)):
            ngram = " ".join(tokens[start : start + size])
            results.append((ngram, float(size)))
    return results


def _combined_text(location: dict[str, Any]) -> str:
    parts = [
        location.get("site_name"),
        location.get("waterbody"),
        location.get("city"),
        location.get("admin1"),
        location.get("country"),
        location.get("location_raw"),
    ]
    return " ".join(
        text for part in parts if (text := _string_or_none(part)) is not None
    )


def _country_iso2(value: object, by_iso2: dict[str, dict[str, Any]]) -> str | None:
    text = _normalize(value)
    if not text:
        return None
    if text in by_iso2:
        return text
    for alias, iso2 in _COUNTRY_ALIASES.items():
        if _normalize(alias) == text:
            return iso2
    # fuzzy: drop trailing country suffixes
    if text.endswith(" republic") or text.endswith(" states") or text.endswith(" kingdom"):
        pass
    return None


def _country_iso2_from_text(
    text: str, by_name: dict[str, dict[str, Any]]
) -> str | None:
    for ngram, _ in _text_ngrams(text):
        record = by_name.get(ngram)
        if record is not None:
            return record["iso2"] or None
    return None


def _normalize(value: object) -> str:
    text = _string_or_none(value)
    if text is None:
        return ""
    lowered = unicodedata.normalize("NFKD", text.casefold())
    lowered = lowered.encode("ascii", "ignore").decode("ascii")
    lowered = re.sub(r"[^0-9a-z ]+", " ", lowered)
    return " ".join(lowered.split())


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
