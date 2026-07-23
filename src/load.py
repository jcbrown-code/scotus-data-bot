"""Load: build the shipped SQLite database from the Transform staging database.

Load is the L of the ETL — a separate phase, not another transform. It reads ONLY the
staging DB (every Transform stage must have run: materialize -> scope -> dedup ->
validate -> reselect -> clean) and rebuilds ``data/processed/scotus.sqlite`` from a
blank slate. It makes no decisions: every label it ships was decided upstream.

What ships, and why:
- ALL 1,120 clusters, fully labeled (``is_scotus`` + ``scope_evidence``,
  ``dedup_role`` + ``dup_of`` + ``dup_method``) — nothing is silently dropped; the
  corpus is exposed by the ``scotus_decisions`` VIEW (canonical + scotus + vols 2-18).
- ALL 1,160 opinion rows, so the 1:many cluster -> opinion hierarchy is visible for
  every cluster. Derived text (``clean_text`` + provenance) is populated only for the
  corpus opinions; elsewhere it is NULL (missing = NULL, never '').
- No raw source text: the Release-distributed raw mirror (pinned by CHECKSUMS) is the
  audit trail. Source structure ships as offset spans into ``clean_text`` — the
  ``page_breaks`` table (reporter pagination) and the ``ocr_suspects`` table (flagged
  spots, normalized from the cleaner's JSON) — with ``chosen_source`` and
  ``clean_version`` pinning the deterministic derivation.
"""

import json
import os
import sqlite3

from config import settings

# Staging tables the loader requires, with the stage that builds each — a missing one
# means that stage has not run against this staging DB, and the build must fail loudly.
_REQUIRED_STAGING = {
    "stg_clusters": "materialize",
    "stg_opinions": "materialize",
    "stg_cluster_scope": "scope",
    "stg_cluster_dedup": "dedup",
    "stg_opinion_source": "reselect",
    "stg_opinion_clean": "clean",
    "stg_page_break": "clean",
    "stg_meta": "materialize",
}

DDL = [
    """CREATE TABLE clusters (
        cluster_id          INTEGER PRIMARY KEY,
        case_name           TEXT NOT NULL,
        case_name_full      TEXT,
        us_cite             TEXT,
        us_volume           INTEGER,
        us_page             TEXT,
        date_filed          TEXT,
        scdb_id             TEXT,
        source              TEXT,
        citation_count      INTEGER,
        precedential_status TEXT,
        n_opinions          INTEGER,
        is_scotus           TEXT NOT NULL,
        scope_evidence      TEXT NOT NULL,
        dedup_role          TEXT,
        dup_of              INTEGER REFERENCES clusters(cluster_id),
        dup_method          TEXT
    )""",
    """CREATE TABLE citations (
        cluster_id INTEGER NOT NULL REFERENCES clusters(cluster_id),
        reporter   TEXT,
        volume     TEXT,
        page       TEXT,
        type       INTEGER,
        PRIMARY KEY (cluster_id, reporter, volume, page)
    )""",
    """CREATE TABLE opinions (
        opinion_id       INTEGER PRIMARY KEY,
        cluster_id       INTEGER NOT NULL REFERENCES clusters(cluster_id),
        type             TEXT,
        author           TEXT,
        is_ocr_extracted INTEGER,
        ordering_key     INTEGER,
        chosen_source    TEXT,
        is_ocr_dirty     INTEGER,
        clean_text       TEXT,
        clean_version    INTEGER
    )""",
    # Reporter page boundaries within clean_text: char_offset is where the page begins;
    # anchor repeats the following words for human / cross-version verification.
    """CREATE TABLE page_breaks (
        opinion_id  INTEGER NOT NULL REFERENCES opinions(opinion_id),
        ordinal     INTEGER NOT NULL,
        page_label  TEXT,
        char_offset INTEGER NOT NULL,
        anchor      TEXT,
        PRIMARY KEY (opinion_id, ordinal)
    )""",
    # OCR-suspect spots as offset spans into clean_text (normalized from the cleaner's
    # JSON so they are queryable; input to the future OCR-correction stage).
    """CREATE TABLE ocr_suspects (
        opinion_id  INTEGER NOT NULL REFERENCES opinions(opinion_id),
        ordinal     INTEGER NOT NULL,
        char_offset INTEGER NOT NULL,
        token       TEXT NOT NULL,
        PRIMARY KEY (opinion_id, ordinal)
    )""",
    """CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)""",
    """CREATE VIEW scotus_decisions AS
        SELECT * FROM clusters
        WHERE dedup_role = 'canonical' AND is_scotus = 'true'
          AND us_volume BETWEEN 2 AND 18""",
]


def _assert_staging_complete(staging):
    present = {
        row[0] for row in staging.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = {name: stage for name, stage in _REQUIRED_STAGING.items() if name not in present}
    if missing:
        need = ", ".join(f"{table} (run --stage {stage})" for table, stage in missing.items())
        raise RuntimeError(f"staging DB is missing required tables: {need}")


def _load_clusters(staging, out):
    """Ship every cluster with its scope + dedup labels; canonicals before duplicates
    so the self-referential dup_of foreign key always resolves."""
    rows = staging.execute(
        "SELECT c.cluster_id, c.case_name, c.case_name_full, c.us_cite, c.us_volume, "
        "c.us_page, c.date_filed, c.scdb_id, c.source, c.citation_count, "
        "c.precedential_status, c.n_opinions, "
        "s.is_scotus, s.evidence, d.dedup_role, d.dup_of, d.dup_method "
        "FROM stg_clusters c "
        "JOIN stg_cluster_scope s USING (cluster_id) "
        "LEFT JOIN stg_cluster_dedup d USING (cluster_id) "
        "ORDER BY (d.dup_of IS NOT NULL), c.cluster_id"
    ).fetchall()
    out.executemany("INSERT INTO clusters VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def _load_citations(staging, out):
    """Parse each cluster's retained citations array; collapse exact-duplicate
    (cluster, reporter, volume, page) tuples and report the drop count."""
    seen, rows, dropped = set(), [], 0
    for cluster_id, payload in staging.execute(
        "SELECT cluster_id, citations_json FROM stg_clusters ORDER BY cluster_id"
    ):
        for citation in json.loads(payload or "[]"):
            key = (
                cluster_id,
                citation.get("reporter"),
                str(citation.get("volume")),
                str(citation.get("page")),
            )
            if key in seen:
                dropped += 1
                continue
            seen.add(key)
            rows.append(key + (citation.get("type"),))
    out.executemany("INSERT INTO citations VALUES (?,?,?,?,?)", rows)
    return len(rows), dropped


def _load_opinions(staging, out):
    """Ship every opinion row; derived text columns only where the clean stage produced
    them (the corpus opinions) — NULL elsewhere, never ''."""
    rows = staging.execute(
        "SELECT o.opinion_id, o.cluster_id, o.type, o.author, o.is_ocr_extracted, "
        "o.ordering_key, src.chosen_source, src.is_ocr_dirty, cl.clean_text, "
        "cl.clean_version "
        "FROM stg_opinions o "
        "LEFT JOIN stg_opinion_source src USING (opinion_id) "
        "LEFT JOIN stg_opinion_clean cl USING (opinion_id) "
        "ORDER BY o.opinion_id"
    ).fetchall()
    out.executemany("INSERT INTO opinions VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    n_corpus = sum(1 for row in rows if row[8] is not None)
    return len(rows), n_corpus


def _load_page_breaks(staging, out):
    rows = staging.execute(
        "SELECT opinion_id, ordinal, page_label, char_offset, anchor "
        "FROM stg_page_break ORDER BY opinion_id, ordinal"
    ).fetchall()
    out.executemany("INSERT INTO page_breaks VALUES (?,?,?,?,?)", rows)
    return len(rows)


def _load_ocr_suspects(staging, out):
    """Normalize the cleaner's ocr_suspect JSON ({count, hits:[{offset, token}]}) into
    queryable offset-span rows."""
    rows = []
    for opinion_id, payload in staging.execute(
        "SELECT opinion_id, ocr_suspect FROM stg_opinion_clean "
        "WHERE ocr_suspect IS NOT NULL ORDER BY opinion_id"
    ):
        for ordinal, hit in enumerate(json.loads(payload)["hits"], 1):
            rows.append((opinion_id, ordinal, hit["offset"], hit["token"]))
    out.executemany("INSERT INTO ocr_suspects VALUES (?,?,?,?)", rows)
    return len(rows)


def _build_fts(out):
    """FTS5 over the corpus opinions' clean_text (diacritic-folded index; the stored
    column stays strict NFC)."""
    out.execute(
        "CREATE VIRTUAL TABLE opinions_fts USING fts5("
        "clean_text, content='opinions', content_rowid='opinion_id', "
        'tokenize="unicode61 remove_diacritics 2")'
    )
    out.execute(
        "INSERT INTO opinions_fts(rowid, clean_text) "
        "SELECT opinion_id, clean_text FROM opinions WHERE clean_text IS NOT NULL"
    )


def _write_meta(staging, out, counts):
    staging_meta = dict(staging.execute("SELECT key, value FROM stg_meta"))
    meta = {
        "pipeline_version": settings.PIPELINE_VERSION,
        "built_at": settings.build_timestamp(),
        "git_commit": settings.git_commit(),
        # lineage: which staging build (and therefore which mirror processing) fed this DB
        **{f"staging_{key}": value for key, value in staging_meta.items()},
        **{key: str(value) for key, value in counts.items()},
    }
    out.executemany("INSERT INTO meta VALUES (?, ?)", sorted(meta.items()))


def build_db(
    staging_db_path: str = settings.STAGING_DB_PATH,
    db_path: str = settings.DB_PATH,
):
    """Build the shipped database from staging (blank-slate rebuild); return counts."""
    staging = sqlite3.connect(staging_db_path)
    try:
        _assert_staging_complete(staging)
        if db_path != ":memory:" and os.path.exists(db_path):
            os.remove(db_path)  # build fresh
        out = sqlite3.connect(db_path)
        try:
            out.execute("PRAGMA foreign_keys = ON")
            for statement in DDL:
                out.execute(statement)
            n_clusters = _load_clusters(staging, out)
            n_citations, n_citation_dupes_dropped = _load_citations(staging, out)
            n_opinions, n_corpus_opinions = _load_opinions(staging, out)
            n_page_breaks = _load_page_breaks(staging, out)
            n_ocr_suspects = _load_ocr_suspects(staging, out)
            _build_fts(out)
            n_decisions = out.execute("SELECT count(*) FROM scotus_decisions").fetchone()[0]
            n_duplicates = out.execute(
                "SELECT count(*) FROM clusters WHERE dedup_role = 'duplicate'"
            ).fetchone()[0]
            n_review_folds = out.execute(
                "SELECT count(*) FROM clusters WHERE dup_method = 'human_review'"
            ).fetchone()[0]
            counts = {
                "n_clusters": n_clusters,
                "n_decisions": n_decisions,
                "n_opinions": n_opinions,
                "n_corpus_opinions": n_corpus_opinions,
                "n_duplicates": n_duplicates,
                "n_review_ledger_folds": n_review_folds,
                "n_citations": n_citations,
                "n_citation_dupes_dropped": n_citation_dupes_dropped,
                "n_page_breaks": n_page_breaks,
                "n_ocr_suspects": n_ocr_suspects,
            }
            _write_meta(staging, out, counts)
            out.commit()
        finally:
            out.close()
    finally:
        staging.close()
    return counts
