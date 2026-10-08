"""PubChem PUG REST chemical-name resolver.

Returned matches are proposals. They must not become cross-document validated aliases until
identifier/structure checks and validator or human policy gates have passed.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from ecmonitor.fulltext_extraction.models import (
    ChemicalMatch,
    ChemicalResolution,
    ResolutionStatus,
)

JsonTransport = Callable[[str], Mapping[str, Any]]
_CAS_PATTERN = re.compile(r"^(\d{2,7})-(\d{2})-(\d)$")
_TRAILING_ALIAS = re.compile(r"^(.+?)\s*\(([A-Za-z][A-Za-z0-9+_.-]{1,20})\)\s*$")


def _default_transport(url: str) -> Mapping[str, Any]:
    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "ECMonitor/0.1 fulltext-extraction"},
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed HTTPS API base
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("PubChem returned a non-object JSON payload")
    return payload


class TokenBucket:
    """Small thread-safe rate limiter that allows concurrency up to a burst cap.

    The previous design held one global mutex across the whole HTTP call, which serialized every
    PubChem lookup even across parallel document workers. A token bucket still caps the average
    request rate (PubChem asks for ~5 requests/second) but lets up to ``burst`` requests be in
    flight at once, so ``--workers 3`` no longer queue behind each other.
    """

    def __init__(self, rate_per_second: float, burst: int) -> None:
        if rate_per_second <= 0 or burst < 1:
            raise ValueError("rate_per_second must be positive and burst at least 1")
        self._rate = float(rate_per_second)
        self._burst = float(burst)
        self._tokens = float(burst)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self._burst, self._tokens + (now - self._last_refill) * self._rate
                )
                self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
            # Sleep outside the lock so other threads can refill and acquire tokens.
            time.sleep(min(wait, 0.5))


def _match_from_dict(value: Mapping[str, Any]) -> ChemicalMatch:
    return ChemicalMatch(
        source=str(value.get("source") or ""),
        source_record_id=str(value.get("source_record_id") or ""),
        canonical_name=str(value.get("canonical_name") or ""),
        matched_alias=str(value.get("matched_alias") or ""),
        pubchem_cid=value.get("pubchem_cid"),
        cas_candidates=tuple(str(item) for item in value.get("cas_candidates") or ()),
        inchi=value.get("inchi"),
        inchikey=value.get("inchikey"),
        canonical_smiles=value.get("canonical_smiles"),
        molecular_formula=value.get("molecular_formula"),
        synonyms=tuple(str(item) for item in value.get("synonyms") or ()),
        raw_payload=dict(value.get("raw_payload") or {}),
    )


def _resolution_from_dict(value: Mapping[str, Any]) -> ChemicalResolution:
    status_text = str(value.get("status") or "not_found")
    if status_text not in {"validated_local", "resolved", "ambiguous", "not_found", "error"}:
        status_text = "error"
    status = cast(ResolutionStatus, status_text)
    return ChemicalResolution(
        raw_name=str(value.get("raw_name") or ""),
        normalized_query=str(value.get("normalized_query") or ""),
        status=status,
        resolver_name=str(value.get("resolver_name") or "pubchem_pug_rest"),
        matches=tuple(_match_from_dict(item) for item in value.get("matches") or ()),
        warnings=tuple(str(item) for item in value.get("warnings") or ()),
        registry_snapshot_version=value.get("registry_snapshot_version"),
    )


class PubChemPugRestResolver:
    resolver_name = "pubchem_pug_rest"
    api_base = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"

    def __init__(
        self,
        *,
        transport: JsonTransport | None = None,
        minimum_request_interval_seconds: float = 0.25,
        maximum_synonyms: int = 200,
        rate_per_second: float = 3.0,
        burst: int = 3,
        cache_path: Path | str | None = None,
        cache_persist_interval: int = 25,
    ) -> None:
        self._transport = transport or _default_transport
        self._minimum_interval = minimum_request_interval_seconds
        self._maximum_synonyms = maximum_synonyms
        self._bucket = TokenBucket(rate_per_second, burst)
        self._cache_path = Path(cache_path) if cache_path else None
        self._cache_persist_interval = cache_persist_interval
        self._cache: dict[str, dict[str, Any]] = {}
        self._cache_lock = threading.Lock()
        self._new_entries_since_save = 0
        if self._cache_path is not None and self._cache_path.is_file():
            try:
                loaded = json.loads(self._cache_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._cache = {
                        str(key): value
                        for key, value in loaded.items()
                        if isinstance(value, dict)
                    }
            except (OSError, json.JSONDecodeError):
                # A corrupt cache must never block extraction; fall back to an empty cache.
                self._cache = {}

    def _cached(self, query: str) -> ChemicalResolution | None:
        with self._cache_lock:
            value = self._cache.get(query)
        if value is None:
            return None
        return _resolution_from_dict(value)

    def _remember(self, query: str, resolution: ChemicalResolution) -> None:
        # Transient transport/HTTP errors should not poison the cache; they can be retried.
        if resolution.status == "error":
            return
        with self._cache_lock:
            self._cache[query] = resolution.to_dict()
            self._new_entries_since_save += 1
            should_save = (
                self._cache_path is not None and self._new_entries_since_save >= self._cache_persist_interval
            )
        if should_save:
            self.flush()

    def flush(self) -> None:
        if self._cache_path is None:
            return
        with self._cache_lock:
            snapshot = dict(self._cache)
            self._new_entries_since_save = 0
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._cache_path.with_suffix(self._cache_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(self._cache_path)

    def _get(self, url: str) -> Mapping[str, Any]:
        last_error: HTTPError | None = None
        for attempt in range(1, 4):
            self._bucket.acquire()
            try:
                return self._transport(url)
            except HTTPError as exc:
                if exc.code in {429, 500, 502, 503, 504} and attempt < 3:
                    last_error = exc
                    # Back off on throttling/transient 5xx before retrying.
                    time.sleep(1.0 * attempt)
                    continue
                raise
        assert last_error is not None  # pragma: no cover - loop always re-raises by attempt 3
        raise last_error

    def resolve(self, raw_name: str) -> ChemicalResolution:
        reported_query = " ".join(raw_name.strip().split())
        query = _preferred_lookup_query(reported_query)
        if not query:
            return ChemicalResolution(
                raw_name=raw_name,
                normalized_query=query,
                status="not_found",
                resolver_name=self.resolver_name,
                warnings=("empty_chemical_name",),
            )
        cached = self._cached(query)
        if cached is not None:
            return _relabel_raw_name(cached, raw_name)

        encoded = quote(query, safe="")
        property_url = (
            f"{self.api_base}/compound/name/{encoded}/property/"
            "Title,IUPACName,CanonicalSMILES,IsomericSMILES,InChI,InChIKey,MolecularFormula/JSON"
        )
        try:
            property_payload = self._get(property_url)
        except HTTPError as exc:
            if exc.code == 404:
                result = ChemicalResolution(
                    raw_name=raw_name,
                    normalized_query=query,
                    status="not_found",
                    resolver_name=self.resolver_name,
                    warnings=("pubchem_not_found",),
                )
            else:
                result = self._error(raw_name, query, f"pubchem_http_error:{exc.code}")
            self._remember(query, result)
            return result
        except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            return self._error(raw_name, query, f"pubchem_request_error:{type(exc).__name__}")

        properties = property_payload.get("PropertyTable", {}).get("Properties", [])
        if not isinstance(properties, list) or not properties:
            result = ChemicalResolution(
                raw_name=raw_name,
                normalized_query=query,
                status="not_found",
                resolver_name=self.resolver_name,
                warnings=("pubchem_empty_property_table",),
            )
            self._remember(query, result)
            return result

        matches: list[ChemicalMatch] = []
        warnings: list[str] = []
        if query != reported_query:
            warnings.append(f"pubchem_query_variant:{query}")
        for prop in properties:
            if not isinstance(prop, dict) or "CID" not in prop:
                continue
            cid = str(prop["CID"])
            synonyms: tuple[str, ...] = ()
            try:
                synonym_payload = self._get(f"{self.api_base}/compound/cid/{cid}/synonyms/JSON")
                info = synonym_payload.get("InformationList", {}).get("Information", [])
                if isinstance(info, list) and info and isinstance(info[0], dict):
                    raw_synonyms = info[0].get("Synonym", [])
                    if isinstance(raw_synonyms, list):
                        synonyms = tuple(
                            str(value) for value in raw_synonyms[: self._maximum_synonyms]
                        )
            except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError):
                warnings.append(f"pubchem_synonym_lookup_failed:{cid}")

            canonical_name = str(prop.get("Title") or prop.get("IUPACName") or query)
            cas_candidates = tuple(
                value
                for value in synonyms
                if _CAS_PATTERN.fullmatch(value) and is_valid_cas_rn(value)
            )
            matched_alias = next(
                (value for value in synonyms if value.casefold() == query.casefold()), query
            )
            matches.append(
                ChemicalMatch(
                    source="PubChem",
                    source_record_id=cid,
                    canonical_name=canonical_name,
                    matched_alias=matched_alias,
                    pubchem_cid=cid,
                    cas_candidates=cas_candidates,
                    inchi=_optional_string(prop.get("InChI")),
                    inchikey=_optional_string(prop.get("InChIKey")),
                    canonical_smiles=_optional_string(
                        prop.get("ConnectivitySMILES")
                        or prop.get("CanonicalSMILES")
                        or prop.get("SMILES")
                    ),
                    molecular_formula=_optional_string(prop.get("MolecularFormula")),
                    synonyms=synonyms,
                    raw_payload=dict(prop),
                )
            )

        if not matches:
            result = ChemicalResolution(
                raw_name=raw_name,
                normalized_query=query,
                status="not_found",
                resolver_name=self.resolver_name,
                warnings=("pubchem_no_valid_candidates",),
            )
        else:
            result = ChemicalResolution(
                raw_name=raw_name,
                normalized_query=query,
                status="resolved" if len(matches) == 1 else "ambiguous",
                resolver_name=self.resolver_name,
                matches=tuple(matches),
                warnings=tuple(warnings),
            )
        self._remember(query, result)
        return result

    def _error(self, raw_name: str, query: str, warning: str) -> ChemicalResolution:
        return ChemicalResolution(
            raw_name=raw_name,
            normalized_query=query,
            status="error",
            resolver_name=self.resolver_name,
            warnings=(warning,),
        )


def _preferred_lookup_query(value: str) -> str:
    """Strip a trailing document abbreviation while preserving the reported name elsewhere."""

    match = _TRAILING_ALIAS.fullmatch(value)
    if match is None:
        return value
    expanded = " ".join(match.group(1).split())
    return expanded or value


def _relabel_raw_name(resolution: ChemicalResolution, raw_name: str) -> ChemicalResolution:
    """Return a cached resolution with the current reported name, not the first-seen one."""
    if resolution.raw_name == raw_name:
        return resolution
    return ChemicalResolution(
        raw_name=raw_name,
        normalized_query=resolution.normalized_query,
        status=resolution.status,
        resolver_name=resolution.resolver_name,
        matches=resolution.matches,
        warnings=resolution.warnings,
        registry_snapshot_version=resolution.registry_snapshot_version,
    )


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None and str(value).strip() else None


def is_valid_cas_rn(value: str) -> bool:
    """Validate CAS Registry Number syntax and checksum."""
    match = _CAS_PATTERN.fullmatch(value.strip())
    if match is None:
        return False
    digits = "".join(match.group(1, 2))
    checksum = sum(index * int(digit) for index, digit in enumerate(reversed(digits), 1)) % 10
    return checksum == int(match.group(3))
