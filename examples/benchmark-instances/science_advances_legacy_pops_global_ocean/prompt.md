# Prompt

Task:
Extract synthesis-level monitoring evidence and ecological-risk statements from the benchmark paper.

Primary objective:
Recover structured benchmark rows describing pollutant-group persistence, marine compartment or matrix, geographic scope, temporal window, reported trend direction or summary signal, and any explicit ecological effect or risk statement.

Extraction level:

- benchmark-level metadata must always be extracted
- region × period × pollutant-group rows should be extracted when clearly supported
- figure-driven trend summaries are valid when the source makes them explicit
- do not force raw observation splitting when the paper only reports synthesis summaries

Never extract:

- cited-source measurements as if they were newly measured in this paper
- unsupported extrapolations
- generic background chemistry definitions
- methods-only calibration details as monitoring results

Always preserve:

- paper metadata
- pollutant-group identity
- geographic scope
- temporal window
- evidence page, figure, or section
- whether the row is a trend, persistence statement, or ecological-risk statement
