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

The corpus (`data/`) is **gitignored** - you can get the latest  Two ways to get it:

- **Download** the prebuilt database from the latest [Release](https://github.com/somedingus/scotus-data-bot/releases/latest)
  (fastest; no token needed), or
- **Regenerate** it: `agentsecrets env -- make ingest` (needs the token; ~15 min due to API
  rate limits — it's resumable).

Once `data/processed/scotus.sqlite` exists, `make inspect` / `make serve` / the data-quality
tests all work.

## 3. How the pipeline fits together

Standard **extract → transform → load** ETL. Data flows:

```
CourtListener clusters endpoint ─(extract.fetch_clusters)→ data/raw/raw_clusters.json
        │ transform.classify + transform.assign_dedup  (SCOTUS filter + de-dup)
        ▼
   data/processed/*.csv + dataset/keep.csv (the 663 KEEP list)
        │ extract.fetch_opinions per KEEP cluster  (opinion text)
        ▼
   data/raw/fulltext/<cluster_id>.json  (raw HTML + stripped plain text)
        │ load.build_db  (schema + rows + FTS5)
        ▼
   data/processed/scotus.sqlite  →  make dist / make release
```

| Module | Responsibility |
|--------|----------------|
| `config/settings.py` | All paths + env (token, date range, DB path). No secrets hardcoded. |
| `src/extract.py` | CourtListener HTTP: `fetch_clusters` (year-chunked, cursor pagination), `fetch_opinions` (per-cluster, adaptive pacing). Retry/throttle handling lives in `_get`. |
| `src/transform.py` | Pure, stdlib, unit-tested: `classify` (KEEP/REVIEW filter), `dedup`/`assign_dedup` (union-find), citation parsing, `strip_html`. **This is where the domain logic is.** |
| `src/load.py` | Schema DDL + loaders + FTS5; SQLite default, Postgres-portable (`--target postgres`). |
| `src/pipeline.py` | Orchestrator (`--stage clusters/text/load/all`), the `scotus-pipeline` entry point. |
| `db/` | `inspect.sql` (completeness report) + schema docs (`db/README.md`). |
| `dataset/` | **Committed** small snapshot (keep.csv, manifest, REVIEW_NOTES) for provenance. |
| `data/` | **Gitignored** bulk: raw API dumps, processed staging, the `.sqlite`. |

The filter and de-duplication rules (and *why* they exist — the Dallas mixed-court and Harvard
duplicate-import quirks) are explained in the [README](README.md#method) and
[dataset/REVIEW_NOTES.md](dataset/REVIEW_NOTES.md). Read those before touching `transform.py`.

## 4. Development workflow

1. **Branch off `main`** (`git checkout -b feat/...` or `fix/...`); open a PR back to `main`.
2. Before pushing, run:
   ```bash
   make format     # ruff auto-format
   make lint       # ruff checks
   make test       # 41 tests
   make cov        # coverage report (currently ~80%)
   ```
3. **CI** (`.github/workflows/ci.yml`) runs ruff lint + format-check + pytest on Python
   3.10–3.12 for every PR. Keep it green.
   ```

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
- **`data/` and `*.sqlite` are gitignored** on purpose — they're regenerable/large. Commit only
  the small `dataset/` snapshot.
- **Determinism.** The pipeline is deterministic: a rebuild from the same cache produces a
  byte-identical dataset, which is why the small committed snapshot stays stable.

## 7. Where to look

- Big picture & results → [README.md](README.md)
- Schema & example queries → [db/README.md](db/README.md)
- Why REVIEW cases were dropped → [dataset/REVIEW_NOTES.md](dataset/REVIEW_NOTES.md)
- The domain logic → `src/transform.py` (+ its tests)
