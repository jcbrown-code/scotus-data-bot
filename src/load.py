"""Load stage: build the SCOTUS database from the staging files.

Default target is SQLite (stdlib `sqlite3`, zero dependencies) with an FTS5 full-text
index over opinion text. The same schema loads into Postgres (`--target postgres --dsn …`,
lazy-importing `psycopg`) using a tsvector + GIN index instead of FTS5.

Inputs (see config.settings):
  all_clusters.csv (dataset)     -> clusters         (all 1,076, with bucket/dedup flags)
  raw_clusters.json (raw)        -> citations        (structured parallel cites)
  fulltext/<id>.json (raw)       -> opinions         (raw_html + plain_text + derived clean_text)
                                    + page_breaks     (star-pagination map, from src.clean)
  review_dispositions.csv (set)  -> review_dispositions
"""

import argparse
import csv
import glob
import json
import os
import subprocess

from config import settings
from src import clean, transform_legacy

# ---- schema ----------------------------------------------------------------

DDL = [
    """CREATE TABLE clusters (
        cluster_id          INTEGER PRIMARY KEY,
        case_name           TEXT,
        us_cite             TEXT,
        volume              INTEGER,
        page                TEXT,
        date_filed          TEXT,
        scdb_id             TEXT,
        source              TEXT,
        citation_count      INTEGER,
        precedential_status TEXT,
        bucket              TEXT,
        dedup_role          TEXT,
        dup_of              INTEGER REFERENCES clusters(cluster_id)
    )""",
    """CREATE TABLE citations (
        cluster_id INTEGER REFERENCES clusters(cluster_id),
        reporter   TEXT,
        volume     TEXT,
        page       TEXT,
        type       INTEGER,
        PRIMARY KEY (cluster_id, reporter, volume, page)
    )""",
    """CREATE TABLE opinions (
        opinion_id       INTEGER PRIMARY KEY,
        cluster_id       INTEGER REFERENCES clusters(cluster_id),
        type             TEXT,
        author           TEXT,
        extracted_by_ocr INTEGER,
        text_source      TEXT,
        char_count       INTEGER,
        raw_html         TEXT,
        plain_text       TEXT,
        clean_text       TEXT,
        clean_version    INTEGER,
        ocr_suspect      TEXT
    )""",
    # Reporter page boundaries within clean_text: char_offset indexes the versioned clean_text
    # (where the reporter's page begins); anchor = the following words, for human/cross-version
    # verification. See src/clean.py and docs/clean-text-design.md.
    """CREATE TABLE page_breaks (
        opinion_id  INTEGER REFERENCES opinions(opinion_id),
        ordinal     INTEGER,
        page_label  TEXT,
        char_offset INTEGER,
        anchor      TEXT,
        PRIMARY KEY (opinion_id, ordinal)
    )""",
    """CREATE TABLE review_dispositions (
        cluster_id  INTEGER REFERENCES clusters(cluster_id),
        disposition TEXT,
        confidence  TEXT,
        rationale   TEXT
    )""",
    """CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)""",
    """CREATE VIEW scotus_decisions AS
        SELECT * FROM clusters WHERE bucket='KEEP' AND dedup_role='canonical'""",
]


def _connect(target, path=None, dsn=None):
    if target == "sqlite":
        import sqlite3

        if path and os.path.exists(path):
            os.remove(path)  # build fresh
        conn = sqlite3.connect(path or ":memory:")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn, "?"
    elif target == "postgres":
        import psycopg  # lazy: only needed for the PG path

        conn = psycopg.connect(dsn)
        return conn, "%s"
    raise ValueError(f"unknown target {target!r}")


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---- loaders ---------------------------------------------------------------


def _load_clusters(conn, ph, path):
    rows = list(csv.DictReader(open(path)))
    out = []
    for r in rows:
        vol, page = transform_legacy.parse_us_cite(r["us_cite"])
        out.append(
            (
                int(r["cluster_id"]),
                r["caseName"],
                r["us_cite"],
                vol,
                page,
                r["dateFiled"],
                r["scdb_id"],
                r["source"],
                _int(r["citation_count"]),
                r["precedential_status"],
                r["bucket"],
                r["dedup_role"],
                _int(r["dup_of"]),
            )
        )
    # Insert canonical rows (dup_of IS NULL) before duplicates so the self-referential
    # FK (duplicate -> its canonical) is always satisfied.
    out.sort(key=lambda row: row[12] is not None)
    conn.executemany(f"INSERT INTO clusters VALUES ({','.join([ph] * 13)})", out)
    return len(out)


def _load_citations(conn, ph, target, raw_path):
    """Load structured citations, collapsing exact-duplicate (cluster, reporter, volume,
    page) tuples. Returns (inserted, dropped) so the drop count is reported, not silent."""
    raw = json.load(open(raw_path))
    seen, out, dropped = set(), [], 0
    for r in raw:
        cid = r["id"]
        for c in r.get("citations") or []:
            key = (cid, c.get("reporter"), str(c.get("volume")), str(c.get("page")))
            if key in seen:
                dropped += 1
                continue
            seen.add(key)
            out.append(
                (
                    cid,
                    c.get("reporter"),
                    str(c.get("volume")),
                    str(c.get("page")),
                    _int(c.get("type")),
                )
            )
    conflict = "" if target == "sqlite" else " ON CONFLICT DO NOTHING"
    verb = "INSERT OR IGNORE INTO" if target == "sqlite" else "INSERT INTO"
    conn.executemany(f"{verb} citations VALUES ({','.join([ph] * 5)}){conflict}", out)
    return len(out), dropped


def _load_opinions(conn, ph, fulltext_dir):
    """Load opinions and derive, at build time, the cleaned column + page-break map from the cached
    raw_html (non-destructive: raw_html/plain_text are stored untouched). Returns (n_opinions,
    n_page_breaks)."""
    out, breaks = [], []
    for f in sorted(glob.glob(os.path.join(fulltext_dir, "*.json"))):
        j = json.load(open(f))
        for o in j["opinions"]:
            ocr = o.get("ocr")
            oid = int(o["opinion_id"])
            raw = o.get("raw") or ""
            clean_text, page_breaks, ocr_suspect = clean.clean_opinion(raw)
            out.append(
                (
                    oid,
                    int(j["cluster_id"]),
                    o.get("type"),
                    o.get("author") or "",
                    1 if ocr else (0 if ocr is False else None),
                    o.get("text_source"),
                    o.get("char_count"),
                    raw,
                    o.get("text") or "",
                    clean_text,
                    clean.CLEAN_VERSION,
                    clean.ocr_suspect_json(ocr_suspect),
                )
            )
            for pb in page_breaks:
                breaks.append(
                    (oid, pb["ordinal"], pb["page_label"], pb["char_offset"], pb["anchor"])
                )
    conn.executemany(f"INSERT INTO opinions VALUES ({','.join([ph] * 12)})", out)
    conn.executemany(f"INSERT INTO page_breaks VALUES ({','.join([ph] * 5)})", breaks)
    return len(out), len(breaks)


def _load_dispositions(conn, ph, path):
    if not os.path.exists(path):
        return 0
    out = [
        (int(r["cluster_id"]), r.get("disposition"), r.get("confidence"), r.get("rationale"))
        for r in csv.DictReader(open(path))
    ]
    conn.executemany(f"INSERT INTO review_dispositions VALUES ({','.join([ph] * 4)})", out)
    return len(out)


def _build_fts(conn, target):
    # Index the canonical clean_text. The canonical column stays strict NFC; the FTS index gets a
    # diacritic-folded projection for recall via the tokenizer (see docs/clean-text-design.md).
    if target == "sqlite":
        conn.execute(
            "CREATE VIRTUAL TABLE opinions_fts USING fts5("
            "clean_text, content='opinions', content_rowid='opinion_id', "
            'tokenize="unicode61 remove_diacritics 2")'
        )
        conn.execute(
            "INSERT INTO opinions_fts(rowid, clean_text) "
            "SELECT opinion_id, clean_text FROM opinions"
        )
    else:
        conn.execute(
            "ALTER TABLE opinions ADD COLUMN tsv tsvector "
            "GENERATED ALWAYS AS (to_tsvector('english', clean_text)) STORED"
        )
        conn.execute("CREATE INDEX opinions_tsv_gin ON opinions USING GIN (tsv)")


def _git_commit():
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL
            )
            .decode()
            .strip()
        )
    except Exception:
        return "unknown"


def _write_meta(conn, ph, counts):
    meta = {
        "pipeline_version": settings.PIPELINE_VERSION,
        "build_timestamp": settings.build_timestamp(),
        "date_range": f"{settings.AFTER}..{settings.BEFORE}",
        "source": "CourtListener clusters + opinions endpoints",
        "git_commit": _git_commit(),
        **{k: str(v) for k, v in counts.items()},
    }
    conn.executemany(f"INSERT INTO meta VALUES ({ph},{ph})", list(meta.items()))


def build_db(
    target="sqlite",
    path=None,
    dsn=None,
    all_clusters=None,
    raw_clusters=None,
    fulltext_dir=None,
    dispositions=None,
):
    all_clusters = all_clusters or settings.ALL_CLUSTERS_CSV
    raw_clusters = raw_clusters or settings.RAW_CLUSTERS
    fulltext_dir = fulltext_dir or settings.FULLTEXT_DIR
    dispositions = dispositions or settings.REVIEW_DISPOSITIONS_CSV

    conn, ph = _connect(target, path, dsn)
    for stmt in DDL:
        conn.execute(stmt)
    n_clusters = _load_clusters(conn, ph, all_clusters)
    n_citations, n_citation_dupes_dropped = _load_citations(conn, ph, target, raw_clusters)
    n_opinions, n_page_breaks = _load_opinions(conn, ph, fulltext_dir)
    n_disp = _load_dispositions(conn, ph, dispositions)
    _build_fts(conn, target)

    cur = conn.execute("SELECT count(*) FROM scotus_decisions")
    n_keep = cur.fetchone()[0]
    n_review = conn.execute(
        "SELECT count(*) FROM clusters WHERE bucket='REVIEW' AND dedup_role='canonical'"
    ).fetchone()[0]
    n_dup = conn.execute("SELECT count(*) FROM clusters WHERE dedup_role='duplicate'").fetchone()[
        0
    ]
    counts = {
        "n_clusters": n_clusters,
        "n_keep_decisions": n_keep,
        "n_review": n_review,
        "n_duplicates": n_dup,
        "n_opinions": n_opinions,
        "n_page_breaks": n_page_breaks,
        "n_citations": n_citations,
        "n_citation_dupes_dropped": n_citation_dupes_dropped,
        "n_review_dispositions": n_disp,
    }
    _write_meta(conn, ph, counts)
    conn.commit()
    return conn, counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["sqlite", "postgres"], default="sqlite")
    ap.add_argument("--db", default=settings.DB_PATH, help="sqlite file path")
    ap.add_argument("--dsn", default=settings.DB_DSN, help="postgres connection string")
    args = ap.parse_args()
    settings.ensure_dirs()
    conn, counts = build_db(args.target, path=args.db, dsn=args.dsn)
    where = args.db if args.target == "sqlite" else args.dsn
    print(f"built {args.target} database at {where}")
    for k, v in counts.items():
        print(f"  {k:24} {v}")
    conn.close()


if __name__ == "__main__":
    main()
