import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

from ecmonitor.fulltext_extraction.adapters.pubchem import PubChemPugRestResolver, TokenBucket


def test_pubchem_resolver_preserves_alias_and_identifier_candidates() -> None:
    def transport(url: str) -> dict[str, Any]:
        if "/property/" in url:
            return {
                "PropertyTable": {
                    "Properties": [
                        {
                            "CID": 9554,
                            "Title": "Perfluorooctanoic acid",
                            "InChIKey": "SNGREZUHAYWORS-UHFFFAOYSA-N",
                            "MolecularFormula": "C8HF15O2",
                            "CanonicalSMILES": "C(=O)(C(F)(F)F)O",
                        }
                    ]
                }
            }
        return {
            "InformationList": {
                "Information": [
                    {
                        "CID": 9554,
                        "Synonym": ["PFOA", "335-67-1", "Perfluorooctanoic acid"],
                    }
                ]
            }
        }

    resolution = PubChemPugRestResolver(
        transport=transport, minimum_request_interval_seconds=0
    ).resolve("PFOA")
    assert resolution.status == "resolved"
    assert resolution.matches[0].matched_alias == "PFOA"
    assert resolution.matches[0].pubchem_cid == "9554"
    assert resolution.matches[0].cas_candidates == ("335-67-1",)


def test_pubchem_resolver_strips_trailing_document_abbreviation_for_lookup() -> None:
    requested_urls: list[str] = []

    def transport(url: str) -> dict[str, Any]:
        requested_urls.append(url)
        if "/property/" in url:
            return {
                "PropertyTable": {
                    "Properties": [{"CID": 1763, "Title": "Perfluorooctanesulfonate"}]
                }
            }
        return {
            "InformationList": {
                "Information": [{"CID": 1763, "Synonym": ["PFOS"]}]
            }
        }

    resolution = PubChemPugRestResolver(
        transport=transport, minimum_request_interval_seconds=0
    ).resolve("perfluorooctanesulfonate (PFOS)")
    assert resolution.status == "resolved"
    assert resolution.normalized_query == "perfluorooctanesulfonate"
    assert resolution.raw_name == "perfluorooctanesulfonate (PFOS)"
    assert any("perfluorooctanesulfonate/property" in url for url in requested_urls)
    assert resolution.warnings == ("pubchem_query_variant:perfluorooctanesulfonate",)



def _transport_counter(calls: list[str]):
    def transport(url: str) -> dict[str, Any]:
        calls.append(url)
        if "/property/" in url:
            return {
                "PropertyTable": {
                    "Properties": [{"CID": 9554, "Title": "Perfluorooctanoic acid"}]
                }
            }
        return {"InformationList": {"Information": [{"CID": 9554, "Synonym": ["PFOA", "335-67-1"]}]}}

    return transport


def test_pubchem_resolver_caches_repeated_queries() -> None:
    calls: list[str] = []
    resolver = PubChemPugRestResolver(
        transport=_transport_counter(calls), minimum_request_interval_seconds=0
    )
    first = resolver.resolve("PFOA")
    second = resolver.resolve("PFOA")
    variant = resolver.resolve("PFOA (acid)")  # same normalized query, different raw name
    assert first.status == second.status == variant.status == "resolved"
    assert len(calls) == 2  # property + synonyms, only for the first unique query
    # raw_name is relabelled per request even when served from cache.
    assert second.raw_name == "PFOA"
    assert variant.raw_name == "PFOA (acid)"
    assert variant.normalized_query == "PFOA"


def test_pubchem_resolver_cache_persists_to_file(tmp_path: Path) -> None:
    cache_path = tmp_path / "pubchem_cache.json"
    calls: list[str] = []
    first = PubChemPugRestResolver(
        transport=_transport_counter(calls),
        minimum_request_interval_seconds=0,
        cache_path=cache_path,
        cache_persist_interval=1,
    )
    first.resolve("PFOA")
    first.flush()
    assert cache_path.is_file()

    def explode(url: str) -> dict[str, Any]:
        raise AssertionError(f"cache miss hit the network: {url}")

    second = PubChemPugRestResolver(
        transport=explode,
        minimum_request_interval_seconds=0,
        cache_path=cache_path,
    )
    cached = second.resolve("PFOA")
    assert cached.status == "resolved"
    assert cached.matches[0].pubchem_cid == "9554"


def test_pubchem_cache_does_not_poison_transient_errors() -> None:
    def flaky(url: str) -> dict[str, Any]:
        raise TimeoutError("network down")

    resolver = PubChemPugRestResolver(transport=flaky, minimum_request_interval_seconds=0)
    result = resolver.resolve("PFOA")
    assert result.status == "error"
    assert resolver._cached("PFOA") is None


def test_token_bucket_allows_burst_then_throttles() -> None:
    bucket = TokenBucket(rate_per_second=20.0, burst=3)
    start = time.monotonic()
    for _ in range(3):
        bucket.acquire()
    burst_elapsed = time.monotonic() - start
    assert burst_elapsed < 0.15  # burst tokens are available immediately

    start = time.monotonic()
    bucket.acquire()  # bucket is now empty; must wait for refill
    throttled = time.monotonic() - start
    assert 0.03 <= throttled < 0.5



def test_pubchem_resolver_retries_throttling_then_succeeds() -> None:
    attempts = {"count": 0}

    def flaky(url: str) -> dict[str, Any]:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise HTTPError(url, 429, "Too Many Requests", None, None)
        if "/property/" in url:
            return {"PropertyTable": {"Properties": [{"CID": 9554, "Title": "Perfluorooctanoic acid"}]}}
        return {"InformationList": {"Information": [{"CID": 9554, "Synonym": ["PFOA", "335-67-1"]}]}}

    resolver = PubChemPugRestResolver(transport=flaky, minimum_request_interval_seconds=0)
    resolution = resolver.resolve("PFOA")
    assert resolution.status == "resolved"
    assert attempts["count"] >= 2
