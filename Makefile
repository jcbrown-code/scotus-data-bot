# SCOTUS corpus ETL — common tasks.
# Network stages need a token: prefix with `agentsecrets env --` (e.g. `make ingest`).

# Use the project venv's Python automatically when it exists (no `activate` needed),
# else fall back to python3. Override with `make <target> PY=/path/to/python`.
# Python tools are always invoked as `$(PY) -m <module>` so they never depend on a
# console script being on PATH.
PY ?= $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)
DB ?= data/processed/scotus.sqlite
VERSION ?= v1.0.0

.PHONY: setup test cov lint format inspect serve dist release clean help

help:
	@echo "make setup    - create .venv and install dev deps (pytest, ruff, datasette)"
	@echo "make test     - run unit + data-quality tests"
	@echo "make cov      - run tests with a coverage report"
	@echo "make lint     - ruff lint checks"
	@echo "make format   - apply ruff formatting"
	@echo "make inspect  - print a human-readable completeness report"
	@echo "make serve    - open the database in Datasette (browser UI)"
	@echo "make dist     - gzip the DB + write SHA256SUMS (release artifact)"
	@echo "make release VERSION=v1.0.0 - build + publish the corpus as a GitHub Release [needs gh]"
	@echo "pipeline stages run individually: python -m src.pipeline --stage <name>"

setup:
	python3 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -e ".[dev]"
	@echo "venv ready at .venv — make targets now use it automatically"

test:
	$(PY) -m pytest tests/ -v

cov:
	$(PY) -m pytest tests/ --cov=src --cov=config --cov-report=term-missing

lint:
	$(PY) -m ruff check src config tests

format:
	$(PY) -m ruff format src config tests

inspect:
	sqlite3 $(DB) < db/inspect.sql

serve:
	$(PY) -m datasette $(DB)

dist:
	gzip -kf $(DB)
	cd $(dir $(DB)) && shasum -a 256 $(notdir $(DB)).gz > SHA256SUMS
	@echo "artifact: $(DB).gz  (+ SHA256SUMS)"

# Build the artifact and publish it as a GitHub Release (requires the `gh` CLI, authed).
# The .sqlite corpus is gitignored, so the Release is how the built database is distributed.
release: dist
	gh release create $(VERSION) "$(DB).gz" "$(dir $(DB))SHA256SUMS" \
		--title "SCOTUS corpus 1790-1820 ($(VERSION))" \
		--notes-file RELEASE_NOTES.md

clean:
	rm -f $(DB) $(DB).gz data/processed/*.csv
