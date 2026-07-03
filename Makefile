# SCOTUS corpus ETL — common tasks.
# Network stages need a token: prefix with `agentsecrets env --` (e.g. `make ingest`).

# Use the project venv's Python automatically when it exists (no `activate` needed),
# else fall back to python3. Override with `make <target> PY=/path/to/python`.
# Python tools are always invoked as `$(PY) -m <module>` so they never depend on a
# console script being on PATH.
PY ?= $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)
DB ?= data/processed/scotus.sqlite
DSN ?=

.PHONY: setup ingest clusters db test inspect serve dist pg clean help

help:
	@echo "make setup    - create .venv and install dev deps (pytest, datasette)"
	@echo "make ingest   - full pipeline (clusters + text + load)   [needs token]"
	@echo "make clusters - reprocess cached clusters, no network     (--from-cache)"
	@echo "make db       - build the SQLite database from staging files"
	@echo "make test     - run unit + data-quality tests"
	@echo "make inspect  - print a human-readable completeness report"
	@echo "make serve    - open the database in Datasette (browser UI)"
	@echo "make dist     - gzip the DB + write SHA256SUMS (release artifact)"
	@echo "make pg DSN=postgres://... - load the same schema into Postgres"

setup:
	python3 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -e ".[dev]"
	@echo "venv ready at .venv — make targets now use it automatically"

ingest:
	agentsecrets env -- $(PY) -m src.pipeline --stage all --validate

clusters:
	$(PY) -m src.pipeline --stage clusters --from-cache --validate

db:
	$(PY) -m src.load --target sqlite --db $(DB)

test:
	$(PY) -m pytest tests/ -v

inspect:
	sqlite3 $(DB) < db/inspect.sql

serve:
	$(PY) -m datasette $(DB)

dist:
	gzip -kf $(DB)
	cd $(dir $(DB)) && shasum -a 256 $(notdir $(DB)).gz > SHA256SUMS
	@echo "artifact: $(DB).gz  (+ SHA256SUMS)"

pg:
	$(PY) -m src.load --target postgres --dsn $(DSN)

clean:
	rm -f $(DB) $(DB).gz data/processed/*.csv
