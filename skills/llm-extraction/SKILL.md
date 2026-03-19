---
name: llm-extraction
description: Extract structured environmental monitoring data from validated literature sources using an LLM. Use when full text, supplementary files, or benchmark source tables are ready and the task is to produce schema-constrained records with evidence, unresolved notes, and benchmark-specific outputs.
---

# LLM Extraction

Use this skill after full text has already been retrieved and validated.

## Core rule

Do not hard-code paper understanding in parsing scripts. Use scripts only to prepare context, isolate relevant sections, and validate outputs. The model should perform the actual semantic extraction.

## Expected inputs

- validated PDF, HTML, supplement, or benchmark source table
- benchmark profile
- schema or output contract
- evidence expectations

## Required outputs

- structured records in the benchmark schema
- provenance fields
- evidence snippet
- page or table location
- unresolved list for anything ambiguous or missing

## Extraction priority

1. supplementary tables
2. main-text tables
3. main-text result paragraphs
4. figures only when values are explicit and legible

## When to load references

- Read `references/profiles.md` to pick the correct extraction level.
- Read `references/output_contracts.md` to keep field behavior consistent.
- Read the benchmark prompt template that matches the task:
  - `references/prompt_nature.md`
  - `references/prompt_science.md`
  - `references/prompt_est_tfa.md`
