"""Reporter apparatus: the front matter the opinion body omits.

For the early reporters (Dallas, Cranch, Wheaton) the published report carries a large amount of
text that is NOT part of any opinion: the reporter's syllabus, the procedural summary, and the
extensively-reported arguments of counsel. CourtListener exposes this at the *cluster* level
(`headmatter`, `summary`, …); the core ETL never captured it (see docs/clean-text-design.md §2).

This module builds a SEPARATE, optional SQLite asset (`scotus-apparatus.sqlite`) keyed on
`cluster_id`, so the audited core corpus (`scotus.sqlite`) stays byte-for-byte frozen. Consumers
who want the apparatus `ATTACH` the file and join on `cluster_id`. Text is stored RAW — cleaning
is deferred (same tiering as opinion `raw_html` → `clean_text`).
"""

import json
import os

from config import settings

# Apparatus text fields, canonical order. Stored raw, one row per (cluster, non-empty kind).
APPARATUS_KINDS = [
    "syllabus",
    "headnotes",
    "summary",
    "headmatter",
    "arguments",
    "disposition",
    "history",
    "procedural_history",
]

# Small cluster-level metadata captured alongside (scalars, not long text).
META_FIELDS = ["case_name_full", "attorneys", "judges"]

DDL = [
    # cluster_id = where the apparatus actually lives (its true source cluster, canonical OR a
    # dedup'd duplicate). canonical_cluster_id = the canonical cluster it resolves to, so apparatus
    # joins directly to scotus_decisions without a dup_of traversal (much apparatus arrived on the
    # Harvard 'U' *duplicate*, not the canonical record we keep — see docs/clean-text-design.md).
    """CREATE TABLE cluster_text (
        cluster_id           INTEGER,
        canonical_cluster_id INTEGER,
        kind                 TEXT,
        char_count           INTEGER,
        raw_text             TEXT,
        PRIMARY KEY (cluster_id, kind)
    )""",
    # both cluster_id and canonical_cluster_id join to clusters(cluster_id) in the core
    # scotus.sqlite (separate file: no enforced FK).
    """CREATE TABLE cluster_meta (
        cluster_id     INTEGER PRIMARY KEY,
        case_name_full TEXT,
        attorneys      TEXT,
        judges         TEXT
    )""",
    """CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)""",
    "CREATE INDEX ix_cluster_text_canonical ON cluster_text(canonical_cluster_id)",
]


def apparatus_rows(raw_cluster):
    """Return [(kind, char_count, raw_text), …] for each non-empty apparatus field of one raw
    cluster dict (apparatus-fields shape from the clusters endpoint). Empty/whitespace-only
    fields are skipped, so a cluster with no apparatus yields no rows."""
    out = []
    for kind in APPARATUS_KINDS:
        v = raw_cluster.get(kind)
        if v and v.strip():
            out.append((kind, len(v), v))
    return out


def meta_row(raw_cluster):
    """Return (case_name_full, attorneys, judges), each None if empty, or None if all three are.

    Absent fields become NULL rather than '' so the asset has a single 'no data' representation —
    the source (CourtListener/Django) returns a mix of '' and null; normalized at this boundary."""
    vals = [((raw_cluster.get(f) or "").strip() or None) for f in META_FIELDS]
    return tuple(vals) if any(vals) else None


def build_apparatus_db(path=None, raw_apparatus=None, corpus=None):
    """Build the apparatus SQLite from the raw apparatus pull.

    `corpus`: {cluster_id: canonical_cluster_id} for the frozen corpus — clusters not in it are
    skipped and counted, and each row is stamped with its canonical (dedup-resolved) cluster so the
    apparatus joins straight to scotus_decisions. If None, every cluster is kept and
    canonical_cluster_id = cluster_id. Returns (conn, counts)."""
    import sqlite3

    path = path or settings.APPARATUS_DB_PATH
    raw_apparatus = raw_apparatus or settings.RAW_APPARATUS
    raw = json.load(open(raw_apparatus))
    if corpus is not None:
        corpus = {int(k): int(v) for k, v in corpus.items()}

    if path and os.path.exists(path):
        os.remove(path)  # build fresh
    conn = sqlite3.connect(path or ":memory:")
    for stmt in DDL:
        conn.execute(stmt)

    text_rows, meta_rows, kinds, clusters_with_text, skipped = [], [], {}, set(), 0
    for rc in raw:
        cid = int(rc["id"])
        if corpus is not None and cid not in corpus:
            skipped += 1
            continue
        canonical = corpus[cid] if corpus is not None else cid
        for kind, cc, txt in apparatus_rows(rc):
            text_rows.append((cid, canonical, kind, cc, txt))
            kinds[kind] = kinds.get(kind, 0) + 1
            clusters_with_text.add(cid)
        mr = meta_row(rc)
        if mr:
            meta_rows.append((cid, *mr))

    conn.executemany("INSERT INTO cluster_text VALUES (?,?,?,?,?)", text_rows)
    conn.executemany("INSERT INTO cluster_meta VALUES (?,?,?,?)", meta_rows)

    counts = {
        "n_clusters_with_apparatus": len(clusters_with_text),
        "n_text_rows": len(text_rows),
        "n_meta_rows": len(meta_rows),
        "n_skipped_out_of_corpus": skipped,
        **{f"n_{k}": v for k, v in sorted(kinds.items())},
    }
    meta = {
        "asset": "scotus-apparatus",
        "pipeline_version": settings.PIPELINE_VERSION,
        "build_timestamp": settings.build_timestamp(),
        # version pin: must match scotus.sqlite's meta.git_commit for the ATTACH join to be valid
        "git_commit": settings.git_commit(),
        "corpus_n_clusters": len(corpus) if corpus is not None else "",
        "note": "raw reporter apparatus; join canonical_cluster_id -> core clusters.cluster_id",
        **{k: str(v) for k, v in counts.items()},
    }
    conn.executemany("INSERT INTO meta VALUES (?,?)", list(meta.items()))
    conn.commit()
    return conn, counts
