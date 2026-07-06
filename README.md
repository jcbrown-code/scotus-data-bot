# scotus-data-bot

[![CI](https://github.com/somedingus/scotus-data-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/somedingus/scotus-data-bot/actions/workflows/ci.yml)

Version 1.0: A python ETL pipeline that builds a clean, de-duplicated, full-text corpus of **U.S. Supreme
Court decisions, 1790–1820** from the [CourtListener](https://www.courtlistener.com/)
API and loads it into a lightweight, queryable **SQLite database**.

**663 distinct decisions · 690 opinions · ~9.5M characters of text.**

## Download the prebuilt database

Don't want to run the pipeline? Grab the built SQLite database from the latest
[**Release**](https://github.com/somedingus/scotus-data-bot/releases/latest):

```bash
# download scotus.sqlite.gz + SHA256SUMS from the Release, then:
shasum -a 256 -c SHA256SUMS      # verify integrity
gunzip scotus.sqlite.gz
sqlite3 scotus.sqlite "SELECT count(*) FROM scotus_decisions;"   # -> 663
datasette scotus.sqlite          # or browse it in the browser
```

(Maintainers publish it with `make release VERSION=v1.0.0`.)

## The problem

Problem: A naïve `docket__court=scotus` pull for 1790–1820 returns **1,076 clusters** — but only
~660 are distinct Supreme Court decisions, primarily because of two issues:

1. **Non-SCOTUS cases.** Early *U.S. Reports* (Dallas reporters, vols 2–4) reprinted
   Pennsylvania state-court and federal circuit cases that CourtListener tags `scotus`.
2. **Duplicate clusters.** CourtListener's 2025 Harvard CAP import (`source="U"`) was only
   partially merged, leaving ~200 early cases with an unmerged duplicate cluster.

## Method

- **Source:** the database-backed `clusters` endpoint (not `search`, which the docs call the
  relevance-ranked, non-canonical view), fetched one year at a time with structured `citations`.
- **SCOTUS filter:** **KEEP** if U.S. reporter volume ≥ 5 (Cranch/Wheaton = exclusively SCOTUS)
  **or** the cluster has an `scdb_id`; else **REVIEW** (all non-SCOTUS — see
  [dataset/REVIEW_NOTES.md](dataset/REVIEW_NOTES.md); 0 genuine decisions wrongly excluded).
- **De-duplication:** collapse same-case clusters (transitively) by identical *(normalized name,
  year)* **or** identical U.S. citation + ≥0.5 name-token overlap; keep the best record (prefer
  `scdb_id`, then merged / non-`U` source, then citation count). Companion cases sharing a
  starting page have ~zero name overlap and stay distinct.
- **Full text:** fetched per cluster from the `opinions` endpoint (the only filter it supports),
  preferring `html_with_citations`; both raw HTML and tag-stripped plain text are stored.

**Validation:** the 663 per-year counts track [Wikipedia's annual SCOTUS totals](https://en.wikipedia.org/wiki/Number_of_U.S._Supreme_Court_cases_decided_by_year)
— 647 (+16), most years exact or ±1 (residual = the 1791 term-vs-calendar shift and
companion-case granularity). 
All landmarks present (Marbury, McCulloch, Martin v. Hunter,
Dartmouth, Gibbons, Fletcher).

## Repository layout

```
pyproject.toml         package metadata + deps (extras: [dev], [postgres]) + entry points
config/settings.py     paths + env (token, date range, DB path)
src/extract.py         CourtListener API: clusters + opinions (auth, pagination, pacing)
src/transform.py       filter + dedup + citation parse + HTML strip   (stdlib; unit-tested)
src/load.py            schema + loader + FTS    (SQLite default; --target postgres portable)
src/pipeline.py        orchestrator: clusters → text → load
dataset/               COMMITTED snapshot: keep.csv, fulltext_manifest.csv, review_* (reviewable)
data/                  GITIGNORED: raw API dumps + processed staging + the .sqlite
db/inspect.sql         human-readable completeness report (`make inspect`)
tests/                 unit tests (transforms) + data-quality tests (loaded DB)
```

## Install

Runtime is stdlib-only; the package is installed editable to get the dev tools + console
entry points. `make setup` creates a `.venv` and installs everything:

```bash
make setup                       # python -m venv .venv && pip install -e ".[dev]"
# or manually, in your own environment:
pip install -e ".[dev]"          # pytest + datasette;  add [postgres] for the Postgres target
```

Installing exposes the `scotus-pipeline` and `scotus-load` console commands, and lets
`from config import …` / `from src import …` resolve without any `sys.path` juggling. The
`make` targets auto-use `.venv/bin/python` when present — no `activate` needed.

## Usage

The CourtListener endpoints require a token, managed with
[agentsecrets](https://github.com/The-17/agentsecrets) (zero-knowledge — the value is injected
into the child process, never printed). Network stages run under `agentsecrets env --`.

```bash
make ingest          # full pipeline: fetch clusters + text, filter, dedup, load   [needs token]
make clusters        # reprocess cached clusters offline (--from-cache --validate)
make db              # build data/processed/scotus.sqlite from staging files
make test            # unit + data-quality tests
make inspect         # human-readable completeness report
make serve           # browse/query/visualize in Datasette
make dist            # gzip the DB + SHA256SUMS (release artifact)
```

Equivalently via the console entry point (or `python -m src.pipeline`):
`agentsecrets env -- scotus-pipeline --stage all --validate`.

## The database

Single SQLite file (`data/processed/scotus.sqlite`) with FTS5 full-text search; the same schema
loads into Postgres via `python -m src.load --target postgres --dsn …`. Tables: `clusters`,
`citations`, `opinions`, `review_dispositions`, `meta`, and the `scotus_decisions` view (the
canonical 663). See [db/README.md](db/README.md) for the schema and example queries.

**Inspect / confirm completeness** — by eye or by SQL:
```bash
make inspect                              # provenance, totals, 0-textless check, per-year vs Wikipedia
datasette data/processed/scotus.sqlite    # web UI: browse, full-text search, facet, export
sqlite3 data/processed/scotus.sqlite "SELECT count(*) FROM scotus_decisions"   # -> 663
```
The `tests/test_data_quality.py` suite asserts the same completeness facts automatically.

## Distribution

The corpus is regenerable from `src/` + the committed `dataset/` snapshot, so the bulk data
(`data/`, the `.sqlite`) is gitignored. The built database is published as a **GitHub Release
asset** (`scotus.sqlite.gz`, ~7 MB) rather than committed.

## Status

- [x] Clusters endpoint ingest, SCOTUS filter, de-duplication, Wikipedia validation
- [x] Human review of the REVIEW bucket (all non-SCOTUS)
- [x] Full-text retrieval for all 663 decisions
- [x] ETL restructure + SQLite database with FTS, tests, and inspection
- [x] Packaging (pyproject), ruff lint/format, 80% coverage, CI

## Contributing

New here? See **[CONTRIBUTING.md](CONTRIBUTING.md)** for developer onboarding — setup, the
architecture/data-flow map, the dev workflow (ruff, tests, CI, commit conventions), and how to
extend the corpus.

## License

Code is released under the [MIT License](LICENSE). The underlying court opinions are
U.S. government works in the public domain.
