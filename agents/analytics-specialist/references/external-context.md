# External Context Adapters

AnalyticsSpecialist can enrich validated rows with three lightweight adapters:

- Crossref
  - DOI-first metadata and citation-facing bibliographic context
- OpenAlex
  - DOI-first work metadata, concepts, institutions, and open access signals
- World Bank
  - country-level contextual indicators for benchmark rows with geographic scope

Starter script:

- `scripts/fetch_context_adapters.py`

Recommended usage:

1. run validation first
2. create a request manifest without network
3. fetch remote payloads only when needed
4. preserve raw responses alongside derived analytics outputs
