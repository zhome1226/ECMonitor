# Science Advances Legacy POPs Global Ocean Example

## Benchmark source

- Benchmark family: `ScienceAdv2024`
- Benchmark paper:
  - `Exploring global oceanic persistence and ecological effects of legacy persistent organic pollutants across five decades`
  - DOI: `10.1126/sciadv.ado5534`
- Extraction style: synthesis-level region × period × pollutant-group reconstruction with ecological risk notes

## Why this example was chosen

This paper is a strong Science-style benchmark for ECMonitor because it is not a single-site monitoring article. Instead, it synthesizes a very large global marine evidence base and turns it into temporal trend, spatial persistence, and ecological effect statements.

That makes it useful for testing the middle layer between raw observation extraction and benchmark-table rebuilding:

- study-level metadata still matters
- pollutant-group identity matters
- geographic scope and time windows matter
- trend and risk statements must be preserved with figure-level provenance

## What the benchmark is about

The paper compiles more than `10,000` legacy POP measurements from global marine environments from `1980` to `2023`, examines long-term persistence across ocean regions, and highlights ecological implications where cumulative legacy POP risk remains elevated.

## Demonstration scope

This example focuses on synthesis rows rather than raw source-paper rows. The intended output shape is:

- pollutant group
- matrix or environmental compartment
- geographic scope
- temporal window
- reported trend or summary signal
- ecological risk note when explicit
- figure or section provenance

## Included files

- `prompt.md`
- `sample_results.json`
