# Science Global Arsenic Groundwater Example

## Benchmark source

- Benchmark family: `Science2020`
- Benchmark paper:
  - `Global threat of arsenic in groundwater`
  - DOI: `10.1126/science.aba1510`
- Extraction style: study-level plus monitoring-summary reconstruction
- Example literature paper used here:
  - `Groundwater contamination with arsenic and other trace elements in an area of the pampa, province of Cordoba, Argentina`
  - DOI: `10.1007/BF01740581`

## Why this example was chosen

The benchmark paper models and maps the global threat from arsenic in groundwater using a very large literature-derived measurement base. The worked example in this folder uses one source paper from that reconstructed literature set because it shows the intended Science-style balance between study metadata and monitoring summary statistics.

This source paper is a good Science-style example because it preserves study context while reporting summary monitoring statistics rather than a massive raw observation table.

It shows the middle ground between Nature-style record splitting and benchmark-table rebuilding:

- study metadata matters
- spatial context matters
- summary concentration statistics still need to be retained faithfully

## What the study is about

The study examines arsenic and associated trace elements in groundwater in Cordoba, Argentina. It reports summarized concentration distributions, including minima, maxima, geometric means, and common ranges.

## Demonstration scope

This example comes from the full Science-style database rebuild for the Science arsenic benchmark, which currently contains:

- `82` source papers
- `141` extracted result rows

## Included files

- `prompt.md`
- `sample_results.json`

## Local source used for the sample

- `/mnt/d/paper_data/pdf/Science/extracted_json/10.1007_BF01740581.json`
