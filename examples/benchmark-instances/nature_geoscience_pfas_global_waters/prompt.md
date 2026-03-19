# Prompt

Task:
Extract environmental monitoring concentration records from the source and reconstruct a record-level database.

Primary objective:
Recover as many observation-level or table-level environmental concentration records as possible.

Only extract:

- measured concentrations in environmental samples
- values from tables, supplements, main text, or clearly legible figures

Do not extract:

- toxicity thresholds
- treatment or removal experiments
- model outputs
- LOD or LOQ as concentration records

Granularity:

- split one pollutant + one matrix + one location + one sampling time + one value or range per row whenever reliable

Always preserve:

- original value text
- original unit
- evidence snippet
- page or table identifier

If no extractable record exists:

- return a paper-level object with a clear no-record flag
