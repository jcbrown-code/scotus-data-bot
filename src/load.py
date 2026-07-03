"""Load stage: build the SCOTUS database from the staging files.

Default target is SQLite (stdlib `sqlite3`, zero dependencies) with an FTS5 full-text
index over opinion text. The same schema loads into Postgres (`--target postgres --dsn …`,
lazy-importing `psycopg`) using a tsvector + GIN index instead of FTS5.

Inputs (see config.settings):
  all_clusters.csv (processed)   -> clusters         (all 1,076, with bucket/dedup flags)
  raw_clusters.json (raw)        -> citations        (structured parallel cites)
  fulltext/<id>.json (raw)       -> opinions         (raw_html + plain_text)
  review_dispositions.csv (set)  -> review_dispositions
"""

import argparse
import csv
import glob
import json
import os
import subprocess

from config import settings
from src import transform

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
        plain_text       TEXT
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
        vol, page = transform.parse_us_cite(r["us_cite"])
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
    raw = json.load(open(raw_path))
    seen, out = set(), []
    for r in raw:
        cid = r["id"]
        for c in r.get("citations") or []:
            key = (cid, c.get("reporter"), str(c.get("volume")), str(c.get("page")))
            if key in seen:
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
    return len(out)


def _load_opinions(conn, ph, fulltext_dir):
    out = []
    for f in sorted(glob.glob(os.path.join(fulltext_dir, "*.json"))):
        j = json.load(open(f))
        for o in j["opinions"]:
            ocr = o.get("ocr")
            out.append(
                (
                    int(o["opinion_id"]),
                    int(j["cluster_id"]),
                    o.get("type"),
                    o.get("author") or "",
                    1 if ocr else (0 if ocr is False else None),
                    o.get("text_source"),
                    o.get("char_count"),
                    o.get("raw") or "",
                    o.get("text") or "",
                )
            )
    conn.executemany(f"INSERT INTO opinions VALUES ({','.join([ph] * 9)})", out)
    return len(out)


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
    if target == "sqlite":
        conn.execute(
            "CREATE VIRTUAL TABLE opinions_fts USING fts5("
            "plain_text, content='opinions', content_rowid='opinion_id')"
        )
        conn.execute(
            "INSERT INTO opinions_fts(rowid, plain_text) "
            "SELECT opinion_id, plain_text FROM opinions"
        )
    else:
        conn.execute(
            "ALTER TABLE opinions ADD COLUMN tsv tsvector "
            "GENERATED ALWAYS AS (to_tsvector('english', plain_text)) STORED"
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
    n_citations = _load_citations(conn, ph, target, raw_clusters)
    n_opinions = _load_opinions(conn, ph, fulltext_dir)
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
        "n_citations": n_citations,
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
