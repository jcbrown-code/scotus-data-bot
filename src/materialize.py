"""Transform · materialize the Cluster -> Opinion hierarchy from the raw mirror.

Reads the verbatim raw mirror (data/raw/{clusters,opinions}/) and builds a normalized,
decision-independent staging database (scotus-staging.sqlite) that preserves the one-to-many
cluster -> opinion hierarchy with referential integrity and retains every candidate source-text
field. This stage makes no decisions: no scoping, classification, dedup, bucketing, cleaning,
source selection, reporter -> volume mapping, or name preference. Downstream stages read from here.
"""

import glob
import json
import os
import sqlite3

from config import settings

# Candidate transcription fields, retained one column each. Order is descriptive only, not a
# preference (source selection is a later stage). xml_scan (scanned-image OCR XML) is excluded --
# it is not a curated transcription and stays available in the raw store.
CANDIDATE_SOURCE_FIELDS = (
    "html_lawbox",
    "xml_harvard",
    "html",
    "html_columbia",
    "html_anon_2020",
    "html_with_citations",
    "plain_text",
)

# Per-citation CourtListener ingestion timestamps, stripped from the retained citations array so
# staging drops the same volatile fields we drop at the record level.
CITATION_VOLATILE = ("date_created", "date_modified")

CLUSTER_COLUMNS = (
    "cluster_id",
    "case_name",
    "case_name_full",
    "date_filed",
    "us_volume",
    "us_page",
    "us_cite",
    "scdb_id",
    "source",
    "citation_count",
    "precedential_status",
    "n_opinions",
    "citations_json",
    "sub_opinion_ids_json",
)
OPINION_SOURCE_COLUMNS = tuple(f"source_{field}" for field in CANDIDATE_SOURCE_FIELDS)
OPINION_COLUMNS = (
    "opinion_id",
    "cluster_id",
    "type",
    "author",
    "is_ocr_extracted",
    "ordering_key",
) + OPINION_SOURCE_COLUMNS


# ---- read + normalize ------------------------------------------------------


def clean_citations(citations):
    """Return the citations list with each citation's ingestion timestamps removed."""
    return [
        {k: v for k, v in citation.items() if k not in CITATION_VOLATILE}
        for citation in citations or []
    ]


def read_raw_records(directory):
    """Yield each verbatim JSON record from directory/*.json in deterministic filename order."""
    for path in sorted(glob.glob(os.path.join(directory, "*.json"))):
        with open(path) as f:
            yield json.load(f)


def parse_us_cite(citations):
    """Return (volume, page, "V U.S. P") for the first U.S. Reports cite, else (None, None, None).

    Matches reporter == "U.S." exactly, so "U.S. LEXIS"/"Dall." are skipped. The API stores volume
    as a string; it is coerced to int. Reporter-specific cite (Dall./Cranch/Wheat.) -> U.S. volume
    mapping is a later scope-stage concern, not this stage's."""
    for citation in citations or []:
        volume = str(citation.get("volume", ""))
        if citation.get("reporter") == "U.S." and volume.isdigit():
            page = str(citation.get("page"))
            return int(volume), page, f"{volume} U.S. {page}"
    return None, None, None


def resolve_cluster_id(raw_opinion):
    """Return the parent cluster id: the direct int field if present, else parsed from the URL."""
    cluster_id = raw_opinion.get("cluster_id")
    if isinstance(cluster_id, int):
        return cluster_id
    return int(str(raw_opinion["cluster"]).rstrip("/").rsplit("/", 1)[-1])


def parse_sub_opinion_ids(raw_cluster):
    """Return the declared child opinion ids parsed from the cluster's sub_opinions URL list."""
    return [
        int(str(url).rstrip("/").rsplit("/", 1)[-1])
        for url in raw_cluster.get("sub_opinions") or []
    ]


def normalize_cluster(raw_cluster):
    """Build a staging cluster record (plain dict) from one raw cluster. No decisions."""
    volume, page, cite = parse_us_cite(raw_cluster.get("citations"))
    return {
        "cluster_id": raw_cluster["id"],
        "case_name": raw_cluster["case_name"],
        "case_name_full": raw_cluster.get("case_name_full") or None,
        "date_filed": raw_cluster.get("date_filed"),
        "us_volume": volume,
        "us_page": page,
        "us_cite": cite,
        "scdb_id": raw_cluster.get("scdb_id", ""),
        "source": raw_cluster.get("source"),
        "citation_count": raw_cluster.get("citation_count"),
        "precedential_status": raw_cluster.get("precedential_status"),
        "n_opinions": 0,  # filled by link_hierarchy
        "citations": clean_citations(raw_cluster.get("citations")),
        "sub_opinion_ids": parse_sub_opinion_ids(raw_cluster),
    }


def normalize_opinion(raw_opinion):
    """Build a staging opinion record from one raw opinion, retaining all source fields."""
    sources = {
        field: raw_opinion[field]
        for field in CANDIDATE_SOURCE_FIELDS
        if isinstance(raw_opinion.get(field), str) and raw_opinion[field].strip()
    }
    return {
        "opinion_id": raw_opinion["id"],
        "cluster_id": resolve_cluster_id(raw_opinion),
        "type": raw_opinion.get("type"),
        "author": raw_opinion.get("author_str", ""),
        "is_ocr_extracted": bool(raw_opinion.get("extracted_by_ocr")),
        "ordering_key": raw_opinion.get("ordering_key"),
        "sources": sources,
    }


def link_hierarchy(stg_clusters, stg_opinions):
    """Assert cluster -> opinion referential integrity and cascade n_opinions onto each cluster.

    Raises RuntimeError on an orphan opinion (parent cluster not materialized) or a cluster that
    declares a sub_opinion id we did not materialize. Explicit raises, not bare asserts (which `-O`
    strips)."""
    by_id = {cluster["cluster_id"]: cluster for cluster in stg_clusters}
    children = {}
    for opinion in stg_opinions:
        parent_id = opinion["cluster_id"]
        if parent_id not in by_id:
            raise RuntimeError(
                f"orphan opinion {opinion['opinion_id']} -> missing cluster {parent_id}"
            )
        children.setdefault(parent_id, []).append(opinion["opinion_id"])
    for cluster in stg_clusters:
        present = set(children.get(cluster["cluster_id"], []))
        cluster["n_opinions"] = len(present)
        missing = set(cluster["sub_opinion_ids"]) - present
        if missing:
            raise RuntimeError(
                f"cluster {cluster['cluster_id']} declares unmaterialized sub_opinions "
                f"{sorted(missing)}"
            )


# ---- persist ---------------------------------------------------------------


def _create_tables(conn):
    """Drop and recreate the staging tables for a clean rebuild."""
    conn.execute("DROP TABLE IF EXISTS stg_opinions")
    conn.execute("DROP TABLE IF EXISTS stg_clusters")
    conn.execute("DROP TABLE IF EXISTS stg_meta")
    conn.execute(
        "CREATE TABLE stg_clusters ("
        "cluster_id INTEGER PRIMARY KEY, case_name TEXT NOT NULL, case_name_full TEXT, "
        "date_filed TEXT, us_volume INTEGER, us_page TEXT, us_cite TEXT, scdb_id TEXT, "
        "source TEXT, citation_count INTEGER, precedential_status TEXT, n_opinions INTEGER, "
        "citations_json TEXT, sub_opinion_ids_json TEXT)"
    )
    source_ddl = ", ".join(f"{col} TEXT" for col in OPINION_SOURCE_COLUMNS)
    conn.execute(
        "CREATE TABLE stg_opinions ("
        "opinion_id INTEGER PRIMARY KEY, "
        "cluster_id INTEGER NOT NULL REFERENCES stg_clusters(cluster_id), "
        "type TEXT, author TEXT, is_ocr_extracted INTEGER, ordering_key INTEGER, "
        f"{source_ddl})"
    )
    conn.execute("CREATE TABLE stg_meta (key TEXT PRIMARY KEY, value TEXT)")


def _build_cluster_row(cluster):
    return (
        cluster["cluster_id"],
        cluster["case_name"],
        cluster["case_name_full"],
        cluster["date_filed"],
        cluster["us_volume"],
        cluster["us_page"],
        cluster["us_cite"],
        cluster["scdb_id"],
        cluster["source"],
        cluster["citation_count"],
        cluster["precedential_status"],
        cluster["n_opinions"],
        json.dumps(cluster["citations"], sort_keys=True),
        json.dumps(cluster["sub_opinion_ids"]),
    )


def _build_opinion_row(opinion):
    fixed = (
        opinion["opinion_id"],
        opinion["cluster_id"],
        opinion["type"],
        opinion["author"],
        int(opinion["is_ocr_extracted"]),
        opinion["ordering_key"],
    )
    sources = tuple(opinion["sources"].get(field) for field in CANDIDATE_SOURCE_FIELDS)
    return fixed + sources


def _build_insert_sql(table, columns):
    return f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})"


def persist(stg_clusters, stg_opinions, staging_db_path):
    """Write the staging tables to staging_db_path (clean rebuild), with provenance in stg_meta.

    Clusters are inserted before opinions so the opinion -> cluster foreign key resolves."""
    conn = sqlite3.connect(staging_db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        _create_tables(conn)
        conn.executemany(
            _build_insert_sql("stg_clusters", CLUSTER_COLUMNS),
            [_build_cluster_row(cluster) for cluster in stg_clusters],
        )
        conn.executemany(
            _build_insert_sql("stg_opinions", OPINION_COLUMNS),
            [_build_opinion_row(opinion) for opinion in stg_opinions],
        )
        meta = {
            "etl_source_system": "courtlistener",
            "etl_git_commit": settings.git_commit(),
            "etl_job_id": settings.git_commit(),
            "built_at": settings.build_timestamp(),
            "n_clusters": str(len(stg_clusters)),
            "n_opinions": str(len(stg_opinions)),
        }
        conn.executemany("INSERT INTO stg_meta (key, value) VALUES (?, ?)", sorted(meta.items()))
        conn.commit()
    finally:
        conn.close()


def materialize_hierarchy(clusters_dir, opinions_dir, staging_db_path):
    """Read the raw mirror, normalize + link the cluster -> opinion hierarchy, write staging DB.

    Returns (stg_clusters, stg_opinions) in numeric id order."""
    stg_clusters = [normalize_cluster(record) for record in read_raw_records(clusters_dir)]
    stg_opinions = [normalize_opinion(record) for record in read_raw_records(opinions_dir)]
    stg_clusters.sort(key=lambda cluster: cluster["cluster_id"])
    stg_opinions.sort(key=lambda opinion: opinion["opinion_id"])
    link_hierarchy(stg_clusters, stg_opinions)
    persist(stg_clusters, stg_opinions, staging_db_path)
    return stg_clusters, stg_opinions
