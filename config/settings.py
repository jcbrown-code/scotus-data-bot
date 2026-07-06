"""Central configuration: paths and environment for the SCOTUS ETL pipeline.

No secrets are hardcoded. The CourtListener API token is read from the
COURTLISTENER_API_TOKEN environment variable, which is injected by agentsecrets:

    agentsecrets env -- python -m src.pipeline ...
"""

import os
from datetime import datetime, timezone

# ---- paths -----------------------------------------------------------------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DATA_DIR = os.path.join(ROOT, "data")
RAW_DIR = os.path.join(DATA_DIR, "raw")  # gitignored: API dumps
PROCESSED_DIR = os.path.join(DATA_DIR, "processed")  # gitignored: working CSVs + the .sqlite
DATASET_DIR = os.path.join(ROOT, "dataset")  # committed: small reviewable snapshot

# raw API dumps
RAW_CLUSTERS = os.path.join(RAW_DIR, "raw_clusters.json")
FULLTEXT_DIR = os.path.join(RAW_DIR, "fulltext")

# processed (gitignored) staging
REVIEW_CSV = os.path.join(PROCESSED_DIR, "review.csv")
DUPLICATES_CSV = os.path.join(PROCESSED_DIR, "duplicates.csv")
# durable per-run log of KEEP clusters whose opinion-text fetch failed (should be empty)
FAILURES_CSV = os.path.join(PROCESSED_DIR, "text_fetch_failures.csv")

# committed snapshot (the human-reviewable provenance / audit trail)
# all_clusters.csv holds ALL 1,076 clusters with bucket + dedup_role + dup_of — the full,
# git-visible record of every filtered/de-duplicated row (nothing dropped without a trace).
ALL_CLUSTERS_CSV = os.path.join(DATASET_DIR, "all_clusters.csv")
KEEP_CSV = os.path.join(DATASET_DIR, "keep.csv")
MANIFEST_CSV = os.path.join(DATASET_DIR, "fulltext_manifest.csv")
REVIEW_DISPOSITIONS_CSV = os.path.join(DATASET_DIR, "review_dispositions.csv")

# database artifact
DB_PATH = os.environ.get("SCOTUS_DB_PATH", os.path.join(PROCESSED_DIR, "scotus.sqlite"))
DB_DSN = os.environ.get("SCOTUS_DB_DSN")  # postgres connection string (optional)

# ---- run parameters --------------------------------------------------------
AFTER = os.environ.get("SCOTUS_AFTER", "1790-01-01")
BEFORE = os.environ.get("SCOTUS_BEFORE", "1820-12-31")
PIPELINE_VERSION = "2.0"

# Wikipedia "Number of U.S. Supreme Court cases decided by year", 1791-1820 — the
# external benchmark the de-duplicated KEEP counts are validated against.
WIKI_ANNUAL = {
    1791: 4,
    1792: 3,
    1793: 2,
    1794: 1,
    1795: 6,
    1796: 16,
    1797: 8,
    1798: 5,
    1799: 9,
    1800: 10,
    1801: 5,
    1802: 0,
    1803: 19,
    1804: 14,
    1805: 24,
    1806: 28,
    1807: 19,
    1808: 32,
    1809: 46,
    1810: 39,
    1811: 0,
    1812: 40,
    1813: 46,
    1814: 48,
    1815: 40,
    1816: 43,
    1817: 42,
    1818: 38,
    1819: 33,
    1820: 27,
}

# CSV column order for the cluster staging files
CLUSTER_COLS = [
    "cluster_id",
    "caseName",
    "us_cite",
    "volume",
    "scdb_id",
    "source",
    "citation_count",
    "precedential_status",
    "dateFiled",
    "bucket",
    "dedup_role",
    "dup_of",
]
MANIFEST_COLS = [
    "cluster_id",
    "caseName",
    "us_cite",
    "dateFiled",
    "n_opinions",
    "total_chars",
    "text_sources",
]


def get_token():
    """Return the CourtListener API token or raise a clear error."""
    tok = os.environ.get("COURTLISTENER_API_TOKEN")
    if not tok:
        raise SystemExit(
            "ERROR: COURTLISTENER_API_TOKEN required (clusters/opinions endpoints need auth).\n"
            "Run via: agentsecrets env -- python -m src.pipeline ..."
        )
    return tok


def build_timestamp():
    """Build timestamp for the meta table; overridable for reproducible builds."""
    return os.environ.get("SCOTUS_BUILD_TIMESTAMP") or datetime.now(timezone.utc).isoformat()


def ensure_dirs():
    for d in (RAW_DIR, PROCESSED_DIR, DATASET_DIR, FULLTEXT_DIR):
        os.makedirs(d, exist_ok=True)
