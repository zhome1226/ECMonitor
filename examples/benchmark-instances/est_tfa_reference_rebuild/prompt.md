# Prompt

Task:
Use the benchmark source table as the row-level truth and rebuild a traceable literature database.

Primary objective:

- preserve every monitoring row from the benchmark table
- resolve each short reference to the best available full reference
- verify source DOIs and URLs against the cited literature and public bibliographic records

Rules:

- the benchmark article DOI must never be reused as a source DOI
- source-table values are authoritative over narrative descriptions
- if a source DOI cannot be verified, leave it blank
- unresolved or ambiguous references must be carried into a separate unresolved output

Always preserve:

- short reference
- full reference when resolved
- matched title
- source DOI
- source URL
- match status
- notes for ambiguity or grey literature
