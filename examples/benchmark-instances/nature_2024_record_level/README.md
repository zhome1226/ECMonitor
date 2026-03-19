# Nature 2024 Record-Level Example

## Benchmark source

- Benchmark family: `Nature2024`
- Extraction style: record-level or observation-level reconstruction
- Example literature paper used here:
  - `Pilot Investigation of Perfluorinated Compounds in River Water, Sediment, Soil and Fish in Tianjin, China`
  - DOI: `10.1007/s00128-011-0313-0`

## Why this example was chosen

This paper is a strong Nature-style example because it contains dense tabular monitoring data across multiple media:

- river water
- soil
- sediment
- fish

It demonstrates why Nature-style extraction needs very fine row splitting:

- one pollutant
- one matrix
- one location
- one sampling slice
- one reported value

## What the study is about

The study investigates perfluorinated compounds in multiple environmental media in Tianjin, China. It reports measured concentrations across site-level monitoring tables and can be reconstructed into hundreds of record-level rows.

## Demonstration scope

This example is drawn from the full Nature-style database rebuild, which currently contains:

- `327` source papers
- `1055` extracted records

The sample file in this folder shows just a few rows from one high-quality paper so the extraction shape is easy to inspect.

## Included files

- `prompt.md`
- `sample_records.json`

## Local source used for the sample

- `/mnt/d/paper_data/pdf/Nature/extracted_json/10.1007_s00128-011-0313-0.json`
