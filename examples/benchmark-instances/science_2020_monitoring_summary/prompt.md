# Prompt

Task:
Extract monitoring-oriented study information and key measured occurrence results from the source.

Primary objective:
Recover structured monitoring information including pollutant identity, environmental matrix, geographic context, sampling context, and key measured concentration results.

Extraction level:

- study-level metadata must always be extracted
- result-level concentration data should be extracted when clearly reported
- full observation-level splitting is optional only when table structure makes it reliable

Never extract:

- model predictions
- toxicity thresholds
- treatment experiments
- methodological QA or calibration values as monitoring records

Always preserve:

- study metadata
- result evidence
- page or table location
- geo-locatability where possible
