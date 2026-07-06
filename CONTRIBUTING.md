# Contributing / Developer onboarding

New developer environment setup and onboarding for **scotus-data-bot**. High level project 
project info can be found in the [README](README.md). 

## 1. Prerequisites

- **Python 3.10+**, `git`, and `sqlite3` (preinstalled on macOS).
- Optional: [`gh`](https://cli.github.com/) (releases/PRs), `datasette` (installed by `make setup`).
- **A CourtListener API token** — only used to *fetch* from the API (the network stages).
  You do **not** need it to work on the transforms, the loader, the database, or the tests

## 2. Setup

```bash
git clone https://github.com/somedingus/scotus-data-bot.git
cd scotus-data-bot
make setup          # creates .venv and runs `pip install -e ".[dev]"`
make test           # sanity check — unit tests should pass
```

`make setup` installs the project **editable** with dev tools (pytest, ruff, datasette) and
exposes the `scotus-pipeline` / `scotus-load` console commands. The `make` targets auto-detect
`.venv/bin/python`, so you never have to `activate`.

### Getting the data

The corpus (`data/`) is **gitignored** — it isn't in the repo. What you need depends on what
you're doing:

**To use / explore the data** (recommended; no token) — download the prebuilt database from the
latest [Release](https://github.com/somedingus/scotus-data-bot/releases/latest) and unpack it to
where the tooling expects:

```bash
gh release download v1.0.0 --repo somedingus/scotus-data-bot --pattern 'scotus.sqlite.gz'
mkdir -p data/processed
gunzip -c scotus.sqlite.gz > data/processed/scotus.sqlite
make inspect        # works now; also `make serve`, and sqlite3/datasette queries
```

**To rebuild the database or run the full test suite** (needs the token) — regenerate the raw
staging files with `agentsecrets env -- make ingest` (~15 min; resumable).

> **Why the distinction:** the data-quality tests (`tests/test_data_quality.py`) rebuild the DB
> from the raw **staging** files (`data/raw/…`), which are **not** shipped in the Release. So if
> you only downloaded `scotus.sqlite`, those tests auto-skip — the unit tests still run and
> `make test` passes with the data-quality suite skipped. Regenerate via `make ingest` to run them.

## 3. How the pipeline fits together

Standard **extract → transform → load** ETL. Every raw record is retained and *labeled* — the
pipeline never silently drops or mutates data (see "Data-lineage guarantees" below).

```
CourtListener clusters endpoint ─ extract.fetch_clusters ─▶ data/raw/raw_clusters.json   (1,076)
     │ transform.classify      →  bucket = KEEP | REVIEW              (labeled, never dropped)
     │ transform.assign_dedup  →  dedup_role = canonical | duplicate, dup_of   (dupes marked)
     ▼
   dataset/all_clusters.csv  (all 1,076 — committed audit trail)
   dataset/keep.csv (663)  +  data/processed/{review,duplicates}.csv
     │ extract.fetch_opinions per KEEP cluster   (resumable, paced)
     │   failures ─▶ data/processed/text_fetch_failures.csv   (durable log)
     ▼
   data/raw/fulltext/<cluster_id>.json   (raw HTML + stripped plain text)
     │ load.build_db  →  clusters (all 1,076) + citations + opinions + review_dispositions + meta + FTS5
     ▼
   data/processed/scotus.sqlite  →  scotus_decisions view (663)  →  make dist / make release
```

| Module | Responsibility |
|--------|----------------|
| `config/settings.py` | All paths + env (token, date range, DB path). No secrets hardcoded. |
| `src/extract.py` | CourtListener HTTP: `fetch_clusters` (year-chunked, cursor pagination), `fetch_opinions` (per-cluster, adaptive pacing). Retry/throttle handling lives in `_get`. |
| `src/transform.py` | Pure, stdlib, unit-tested: `classify` (KEEP/REVIEW filter), `dedup`/`assign_dedup` (union-find), citation parsing, `strip_html`. **This is where the domain logic is.** |
| `src/load.py` | Schema DDL + loaders + FTS5; SQLite default, Postgres-portable (`--target postgres`). |
| `src/pipeline.py` | Orchestrator (`--stage clusters/text/load/all`), the `scotus-pipeline` entry point. |
| `db/` | `inspect.sql` (completeness report) + schema docs (`db/README.md`). |
| `dataset/` | **Committed** audit snapshot: `all_clusters.csv` (all 1,076, labeled), `keep.csv`, manifest, `review_dispositions.csv`, `REVIEW_NOTES.md`. |
| `data/` | **Gitignored** bulk + per-run logs: raw API dumps, processed staging, `text_fetch_failures.csv`, the `.sqlite`. |

### Step-by-step lineage (what happens to each record)

| Stage | Input → output | What's excluded, and how it's recorded |
|-------|----------------|----------------------------------------|
| extract clusters | API → `raw_clusters.json` (1,076) | in-run id-dupes skipped (idempotent) |
| classify (filter) | raw → records + `bucket` | nothing dropped — non-SCOTUS kept as `bucket=REVIEW` |
| dedup | records → `dedup_role`, `dup_of` | duplicates **marked** and pointed at their canonical, not deleted |
| write staging | → `all_clusters.csv` (committed) + keep/review/duplicates | full 1,076-row labeled record is git-visible |
| fetch text | keep → `fulltext/*.json` | fetch failures → `text_fetch_failures.csv` (durable) |
| load | staging → `scotus.sqlite` | `clusters` holds all 1,076; `meta` records counts + `n_citation_dupes_dropped` |

The 1,076 → **663** narrowing is a **view** (`scotus_decisions`), *not* a deletion — every excluded
row is still in the `clusters` table with its reason (`bucket` / `dedup_role` / `dup_of`).

### Two views of the same pipeline

Same code, two mindsets:

- **System / operational:** move data efficiently and reproducibly — year-chunked fetch,
  throttle-safe resumable text stage, deterministic rebuilds, restartable per stage (`--stage`).
- **Contributing developer:** *preserve auditability.* The logic doesn't change; your job when
  editing it is to keep the guarantees below.

**Data-lineage guarantees — don't break these:**

1. **Conserve rows.** `count(clusters) == count(raw_clusters.json)`. To exclude a record, *label*
   it (a new `bucket`/`dedup_role` value or a new column) — never delete it.
2. **The reason travels with the row:** `bucket` (filter), `dedup_role`+`dup_of` (dedup),
   `review_dispositions` (human call). A new exclusion needs a column that records *why*.
3. **Log every drop** to a persisted file under `data/processed/` (like `text_fetch_failures.csv`)
   — never stderr-only.
4. **Assert it.** `tests/test_data_quality.py` checks counts, referential integrity, 0-textless,
   and the filter/dedup rules. Ship any pipeline change with a matching assertion.
5. **Keep `scotus_decisions` a view, never a table** — the view is what keeps the narrowing reversible.

> **Decisions vs. opinions.** "663 decisions" is *case-level* (`scotus_decisions`, one row per
> cluster); "690 opinions" is *document-level* (the `opinions` table). The 27 extra come from
> seriatim cases where each Justice filed a separate opinion (e.g. *The Venus*, *Brown v. United
> States*) — many opinions link to one decision via `cluster_id`. It is not a double-count.

The filter/de-dup rules (and *why* — the Dallas mixed-court and Harvard duplicate-import quirks)
are in the [README](README.md#method) and [dataset/REVIEW_NOTES.md](dataset/REVIEW_NOTES.md). Read
those before touching `transform.py`.

## 4. Development workflow

1. **Branch off `main`** (`git checkout -b feat/...` or `fix/...`); open a PR back to `main`.
2. Before pushing, run:
   ```bash
   make format     # ruff auto-format
   make lint       # ruff checks
   make test       # 43 tests
   make cov        # coverage report (currently ~86%)
   ```
3. **CI** (`.github/workflows/ci.yml`) runs ruff lint + format-check + pytest on Python
   3.10–3.12 for every PR. Keep it green.

### Testing conventions (see `tests/`)

- Small, focused, deterministic tests; **no real network or randomness** — HTTP is mocked by
  monkeypatching `extract._get` (see `tests/test_extract.py`), and shared sample data lives in
  the `sample_raw_clusters` fixture (`tests/conftest.py`).
- Use `@pytest.mark.parametrize` for input/output cases (see `tests/test_transform.py`).
- The `db` fixture in `conftest.py` builds a real SQLite DB from staging files — that's the
  **integration layer** (`tests/test_data_quality.py`); it auto-skips when the data isn't present
  (e.g. in CI), so those tests won't fail a fresh checkout.
- Coverage target is *reasonable*, not 100%: network HTTP loops, the Postgres path, and
  `__main__` glue are intentionally left uncovered.

## 5. Common tasks

| Task | Command |
|------|---------|
| Re-run the whole pipeline | `agentsecrets env -- make ingest` |
| Reprocess cached clusters (no network) | `make clusters` |
| Rebuild just the database | `make db` |
| Inspect / confirm completeness | `make inspect` |
| Explore in a browser UI | `make serve` |
| Publish a Release | `make release VERSION=v1.2.0` *(needs `gh`)* |
| Load into Postgres | `make pg DSN=postgres://…` |

### Extending the corpus

- **Different date range:** set `SCOTUS_AFTER` / `SCOTUS_BEFORE` (see `config/settings.py`), then
  `agentsecrets env -- make ingest`.
- **New transform / rule change:** edit `src/transform.py`, add a unit test in
  `tests/test_transform.py`, re-run `make clusters` and check `--validate` vs the historical counts.
- **Schema change:** edit the DDL in `src/load.py`, update `db/README.md`, and add/adjust a
  `tests/test_data_quality.py` assertion.

## 6. Gotchas

- **Rate limits.** The `opinions` endpoint only filters by exact `cluster=<id>` (no batching), so
  text is fetched one case at a time with adaptive pacing; the run is **resumable** (skips
  already-downloaded clusters). Don't parallelize — it trips the throttle harder.
- **The token is never committed.** It's injected at runtime by `agentsecrets env --`; there is no
  token in the repo, and `config.settings.get_token()` reads it from the environment.
- **`data/` and `*.sqlite` are gitignored** on purpose — they're regenerable/large. The committed
  `dataset/` snapshot (incl. `all_clusters.csv`) is the git-visible audit trail; `data/` holds the
  bulk plus per-run logs.
- **Determinism.** The pipeline is deterministic: a rebuild from the same cache produces a
  byte-identical dataset — which is why the committed `dataset/` snapshot stays stable, and why a
  diff there means the underlying data actually changed.

## 7. Where to look

- Big picture & results → [README.md](README.md)
- Schema & example queries → [db/README.md](db/README.md)
- Why REVIEW cases were dropped → [dataset/REVIEW_NOTES.md](dataset/REVIEW_NOTES.md)
- The domain logic → `src/transform.py` (+ its tests)
