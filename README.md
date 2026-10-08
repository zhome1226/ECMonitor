# ECMonitor

ECMonitor is an auditable literature-to-evidence workflow for emerging-contaminant monitoring research. This repository contains the source code, versioned contracts, documentation, and small illustrative examples for the current release candidate.

**[ECMonitor website](https://eco-website.pages.dev/)** · [Website methodology](https://eco-website.pages.dev/methodology/) · [Runnable harness](harness/) · [Published-literature examples](examples/benchmark-instances/)

The website is the live data-browsing entry point. Its records and totals can change over time; the files in this repository are not a copy of the live database. See [how the repository and website relate](docs/data-portal.md).

## Current release

The runnable code is under [`harness/`](harness/), at **0.2.0rc1** for Python 3.12. It implements Retrieval, Download, Extraction, and Validation with a deterministic SQLite workflow worker. This is a source release candidate. Synthetic tests do not establish live provider availability, production readiness, or scientific accuracy on a new corpus.

```text
harness/
  src/ecmonitor/       Python package and command-line tools
  configs/             Runtime and policy defaults
  schemas/             Versioned handoff and evidence contracts
  prompts/             Model instructions
  docs/                Architecture and operator runbooks
  examples/            Synthetic format examples
  tests/               Unit, contract, integration, and synthetic end-to-end tests
  deploy/              Container and Compose files
examples/benchmark-instances/  Introductions to three published studies
.github/workflows/              Release-candidate checks
```

For setup, commands, and operational limits, begin with the [harness README](harness/README.md) and [release-candidate runbook](harness/docs/runbooks/release_candidate.md). The package is designed for authorized local full text or explicitly authorized public HTTPS acquisition; credentials and source documents are supplied privately by the operator.

## Literature examples

[`examples/benchmark-instances/`](examples/benchmark-instances/) introduces three published studies in *Nature Geoscience*, *Science Advances*, and *Environmental Science & Technology*. ECMonitor has been used to reconstruct literature-linked records associated with these sources. The example files show formats and source context; they are neither complete reconstructed datasets nor a quantitative evaluation. Detailed results will be reported with the manuscript.

## Public-source boundary

This repository contains no article PDFs, raw run logs, private credentials, signed examples, research databases, or unpublished evaluation tables. Its synthetic examples are for understanding file shapes and contracts. Code and documentation are released under the [MIT License](LICENSE); rights to cited publications and their supporting information remain with their respective owners.
