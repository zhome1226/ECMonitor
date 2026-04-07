# Science Prompt Template

Use when the benchmark needs synthesis-level metadata plus monitoring-oriented summary results.

## Prompt skeleton

Task:
Extract monitoring-oriented study information and key measured occurrence results from the source.

Primary objective:
Recover structured monitoring information including pollutant or pollutant-group identity, environmental matrix, geographic context, temporal window, reported trend or summary statistics, and any explicit ecological risk statements.

Extraction level:

- study-level metadata must always be extracted
- synthesis-level result rows should be extracted when clearly reported
- region × period × pollutant-group splitting is preferred when the paper is a global review or meta-synthesis
- full observation-level splitting is optional only when table structure makes it reliable

Never extract:

- cited-source measurements as if they were newly measured in the benchmark paper
- unsupported extrapolations
- toxicity thresholds
- treatment experiments
- methodological QA or calibration values as monitoring records

Always preserve:

- study metadata
- result evidence
- page or table location
- whether the row is a concentration summary, a temporal trend, or an ecological-risk statement
- geo-locatability where possible
