# Extraction Profiles

## Nature-style profile

Goal:

- record-level or observation-level reconstruction

Focus:

- pollutant
- matrix
- location
- sampling period
- concentration value
- unit
- evidence

## Science-style profile

Goal:

- study-level plus monitoring-summary reconstruction

Focus:

- study metadata
- pollutant list
- matrix types
- location context
- key concentration statistics

## EST / TFA-style profile

Goal:

- rebuild a benchmark source table into a traceable literature database

Focus:

- benchmark source rows remain the row-level truth
- references are resolved from article references plus mapping documents
- DOI and URL backfill follow the mapping document first

## General constraints

- never invent DOI or values
- preserve original units unless normalization is explicitly reliable
- carry unresolved items into audit outputs
- keep conflict notes when supplement and main text disagree
