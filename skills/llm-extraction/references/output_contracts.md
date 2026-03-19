# Output Contracts

## Minimum provenance fields

Every extracted row should preserve:

- source paper identifier
- benchmark profile
- evidence snippet
- page, table, or figure location
- extraction confidence
- notes for ambiguity or conflict

## Confidence labels

Use only:

- `high`
- `medium`
- `low`

## Unresolved handling

Use unresolved outputs when:

- reference identity is ambiguous
- source is grey literature without DOI
- page content is inaccessible
- values are hinted at but not explicitly reported

## Conflict handling

If the same dataset appears in both main text and supplement:

- prefer supplement values
- if values conflict, keep both and annotate the conflict source
