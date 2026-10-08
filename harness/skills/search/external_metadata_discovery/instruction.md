# External Metadata Discovery Skill

This skill is adapted from ECfinder `external_metadata_discovery_v1` for
ECMonitor Retrieval Specialist.

It discovers scholarly metadata through bounded, provider-specific adapters. It
returns metadata references and provenance only. It never downloads full text,
never writes literature evidence directly to extraction tables, and never
modifies source code or Git state.

Required behavior:

- Run only configured query families with explicit result, page, and candidate
  limits.
- Use Crossref, OpenAlex, Semantic Scholar, and PubMed provider adapters when
  available.
- Persist each provider page or batch before normalization or screening.
- Preserve provider, query_id, query_text, title, abstract, DOI, PMID, OpenAlex
  ID, Semantic Scholar ID, Crossref ID, year, journal, authors, keywords, URL,
  open-access hints, source metadata path, and hashes.
- Gracefully skip unavailable providers or failed queries while reporting source
  status.
- Deduplicate through Retrieval Specialist's persistent document registry before
  screening.
- Keep output JSONL strict one-object-per-line.
- Do not emit PDF download jobs directly; Retrieval Specialist emits durable
  handoff events only after final include screening decisions.

The formal ECMonitor protocol remains in `configs/retrieval/*.yaml`; this skill
is a provider contract and field-mapping reference, not the source of protocol
truth.
