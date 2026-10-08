# Full-Text Extraction Tooling Decision

Status: pilot implementation baseline.

## Decision summary

Use replaceable adapters rather than binding the harness to one parser or one chemical database.
The first runnable path is intentionally lightweight:

1. **PyMuPDF baseline** for fast native text blocks, page numbers, and bounding boxes.
2. **Docling benchmark adapter** for layout, table structure, and OCR-capable difficult PDFs.
3. **GROBID sidecar** for scholarly metadata, section structure, references, and TEI coordinates.
4. **OCR fallback** using OCRmyPDF/Tesseract or a benchmarked local OCR engine for image PDFs.
5. **Versioned local chemical registry** is authoritative for cross-document aliases.
6. **PubChem PUG REST** generates external identifier/name proposals.
7. **RDKit MolStandardize** validates and canonicalizes structures after a structure is available;
   it is not treated as a general chemical-name resolver.
8. **OPSIN** is optional for systematic chemical names that fail direct registry/PubChem resolution.
9. **EPA CompTox/DSSTox and ChEBI** are secondary cross-check sources for environmental identifiers,
   ontology/class semantics, and conflict detection.

No external lookup may directly promote a global alias. External results enter the `proposed` tier,
and promotion requires the review gates in the extraction architecture.

## Parser routing

| Condition | Initial adapter | Fallback |
| --- | --- | --- |
| Native text, ordinary layout | PyMuPDF | Docling |
| Complex or multi-page tables | Docling | alternate parser plus visual/OCR review |
| Header, DOI, references, section metadata | GROBID | Crossref plus first-page extraction |
| More than 20% zero-text pages | OCR route | alternate OCR/parser |
| Parser disagreement changes analyte-value binding | persist both outputs | human review |

PyMuPDF is a throughput baseline, not the final table extractor. The 30-50 document pilot must compare
parsers using evidence-anchor accuracy and analyte-value-unit binding, not text character count alone.

## Chemical resolution order

```text
validated local alias
-> exact identifier supplied in article
-> PubChem name/identifier proposal
-> OPSIN for systematic names
-> CompTox/DSSTox and ChEBI cross-check
-> unresolved or human-review queue
```

CAS-like strings discovered in synonym lists are stored as `cas_candidates`; they are not promoted as
an authoritative CAS RN without corroboration. Structure identifiers such as InChIKey are preferred for
identity conflict detection.

## Implemented in the first scaffold

- adapter protocols for PDF parsers, chemical resolvers, extractor, and validator;
- PyMuPDF block parser;
- layout-preserving baseline chunker;
- PubChem PUG REST resolver with throttling and injectable offline transport;
- versioned SQLite chemical registry with invisible proposal tier;
- explicit human promotion gate for aliases;
- transactional per-document SQLite persistence and outbox event;
- one-document CLI that parses, chunks, commits, writes a report, and releases temporary resources;
- sequential library runner with resume, one-document commit barriers, durable JSONL events, and
  forced garbage collection before the next PDF;
- fresh extractor/validator instances for every document, plus short-lived JSON command adapters that
  start a new process for every agent request;
- bounded chunk retry with structured reason codes, failed JSON pointers, and requested context;
- explicit accepted/rejected/pending-human observation records and self-contained human-review tasks;
- canonical name, reported name, and replacement-name columns in authoritative observation records;
- deterministic rejection of explicit class/sum/general terms from the individual-chemical table;
- PubChem CAS-like synonym filtering with CAS checksum validation;
- `ecmonitor-fulltext tools` diagnostics for installed and missing optional tooling;
- an OpenAI-compatible extractor/validator command adapter with isolated subprocess calls, JSON Schema
  validation, bounded transport retries, repair requests, and key-free audit metadata;
- a stratified 40-document pilot-manifest builder and `run-pilot` command;
- manifest bibliographic metadata (`doi`, `title`, `journal`, `pmid`, and `url`) carried into each
  durable document report;
- offline tests for chunk lineage, registry promotion, retry behavior, name mapping, PubChem mapping,
  non-individual term rejection, document commit, manifest metadata, limit, resume, and fail-fast
  behavior for missing PDFs.

## Not implemented yet

- Docling/GROBID/OCR adapters and parser routing benchmark;
- table-cell and caption/footnote structural chunks;
- DOI reconciliation when the PDF, manifest, and external metadata disagree;
- completeness audit over candidate tables and supplementary files;
- human-review UI and alias promotion workflow;
- a queue-level subprocess supervisor with worker recycling and cost/rate-limit control for the full
  2,176-document manifest.


## Local availability snapshot (2026-08-10)

The project virtual environment currently has PyMuPDF and pypdf, so the native-text baseline is
runnable now. Docling, pdfplumber, OCRmyPDF/Tesseract, RDKit, and Pint are not currently installed.
Neither Docker nor Java is currently available, so GROBID cannot yet be launched locally without an
additional runtime or a remote sidecar.

Use:

```powershell
.\.venv\Scripts\python.exe -m ecmonitor.fulltext_extraction.cli tools
```

Recommended installation order for the pilot is:

1. keep PyMuPDF as the fast baseline and label parser-quality failures rather than trusting them;
2. add Docling for the 30-50 document parser benchmark, especially tables and complex layout;
3. add one OCR route only after the zero-native-text inventory identifies how many PDFs need it;
4. add Pint before automatic unit conversion is enabled;
5. add RDKit only when structures are available and need standardization/identity checks;
6. add GROBID only if section/reference/DOI structure materially improves retrieval or completeness;
7. add CompTox/DSSTox and OPSIN adapters after PubChem conflict cases have been measured.

This order avoids installing several heavy tools before the pilot reveals which failure modes dominate.
