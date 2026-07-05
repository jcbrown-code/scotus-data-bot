# SCOTUS corpus 1790–1820

A clean, de-duplicated, full-text database of **U.S. Supreme Court decisions, 1790–1820**,
built from the [CourtListener](https://www.courtlistener.com/) API by this project's pipeline.

## Contents

| | |
|---|---|
| Distinct SCOTUS decisions | **663** (`scotus_decisions` view) |
| Opinions (with full text) | 690 (13 seriatim cases have several) |
| Structured citations | 3,426 |
| All clusters (incl. REVIEW + duplicates, with provenance) | 1,076 |
| Full text | ~9.5M characters (`html_with_citations` + tag-stripped `plain_text`) |

Validated against [Wikipedia's annual SCOTUS decision totals](https://en.wikipedia.org/wiki/Number_of_U.S._Supreme_Court_cases_decided_by_year)
(663 vs 647; most years exact or ±1). All landmark cases present (Marbury, McCulloch,
Martin v. Hunter, Dartmouth, Gibbons, Fletcher).

## Asset

- `scotus.sqlite.gz` — gzipped SQLite database (~7 MB compressed, ~21 MB unpacked) with FTS5
  full-text search over opinion text.
- `SHA256SUMS` — checksum for verification.

## Use

```bash
gunzip scotus.sqlite.gz
sqlite3 scotus.sqlite "SELECT count(*) FROM scotus_decisions;"   # -> 663
# or explore in the browser:
datasette scotus.sqlite
```

Tables: `clusters`, `citations`, `opinions`, `review_dispositions`, `meta`, and the
`scotus_decisions` view. See `db/README.md` for the schema and example queries.

## Provenance & license

Regenerable from source with `make ingest` (see the repo README). The `meta` table records
the exact build (pipeline version, timestamp, git commit). Project code is MIT-licensed; the
court opinions themselves are public-domain U.S. government works.
