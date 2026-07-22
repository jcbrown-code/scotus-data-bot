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
RAW_DIR = os.path.join(DATA_DIR, "raw")  # raw mirror (Release-distributed) + gitignored caches
PROCESSED_DIR = os.path.join(DATA_DIR, "processed")  # gitignored: working CSVs + the .sqlite
DATASET_DIR = os.path.join(ROOT, "dataset")  # committed: small reviewable snapshot

# reporter apparatus (headmatter/summary/…) — a separate, optional pull; see src/apparatus.py
RAW_APPARATUS = os.path.join(RAW_DIR, "raw_apparatus.json")

# Verbatim raw mirror (Extract stage): one JSON per record, EVERY cluster, FULL API fields, no
# reshaping. Distributed as a GitHub Release asset (not committed, to keep clones slim); the
# committed CHECKSUMS.sha256 + extract_manifest.json pin and trace it, and `--stage fetch-mirror`
# downloads + verifies it (see src/mirror.py). Apparatus rides on the cluster record, so
# RAW_CLUSTERS_DIR is also the apparatus source.
RAW_CLUSTERS_DIR = os.path.join(RAW_DIR, "clusters")
RAW_OPINIONS_DIR = os.path.join(RAW_DIR, "opinions")
EXTRACT_MANIFEST = os.path.join(RAW_DIR, "extract_manifest.json")  # committed provenance
CHECKSUMS_PATH = os.path.join(RAW_DIR, "CHECKSUMS.sha256")  # committed per-record + archive ledger
# The raw mirror is a GitHub Release asset; these locate it for --stage fetch-mirror.
GITHUB_REPO = "jcbrown-code/scotus-data-bot"
RAW_MIRROR_TAG = "raw-mirror-v1"

# committed snapshot (the human-reviewable provenance / audit trail)
# all_clusters.csv is the V1 cluster snapshot the apparatus stage still keys its corpus
# resolution on; it is replaced when the V2 load stage ships its own corpus export.
ALL_CLUSTERS_CSV = os.path.join(DATASET_DIR, "all_clusters.csv")
# committed snapshot of reporter-apparatus coverage (which clusters carry which apparatus kinds)
APPARATUS_MANIFEST_CSV = os.path.join(DATASET_DIR, "apparatus_manifest.csv")
# human-review ledger: dispositions a person adjudicated against the reference/record that
# override scope's automated rule (propose -> review -> execute). Built against V2/reference
# data (an earlier V1 review artifact was retired as unreliable).
SCOPE_REVIEW_CSV = os.path.join(DATASET_DIR, "scope_review.csv")
# authoritative per-volume case list (SCDB / Wikipedia by-volume ingest); the answer key the
# validate stage reconciles the deduplicated KEEP set against, per U.S. Reports volume.
CASE_NAME_REFERENCE_CSV = os.path.join(DATASET_DIR, "case_name_reference.csv")
# committed per-volume reconciliation report the validate stage writes for human review.
VALIDATE_REPORT_CSV = os.path.join(DATASET_DIR, "validate_report.csv")

# database artifacts
# separate, optional apparatus asset (ATTACH-able, keyed on cluster_id); core DB stays untouched
APPARATUS_DB_PATH = os.environ.get(
    "SCOTUS_APPARATUS_DB_PATH", os.path.join(PROCESSED_DIR, "scotus-apparatus.sqlite")
)
# Transform staging DB: the normalized cluster -> opinion hierarchy from the raw mirror
# (decision-independent; downstream Transform stages read from here). See src/materialize.py.
STAGING_DB_PATH = os.environ.get(
    "SCOTUS_STAGING_DB_PATH", os.path.join(PROCESSED_DIR, "scotus-staging.sqlite")
)

# ---- run parameters --------------------------------------------------------
AFTER = os.environ.get("SCOTUS_AFTER", "1790-01-01")
# Generous upper bound (1821, not 1820): date_filed carries term-vs-decision-date drift, so a hard
# 1820 cutoff can silently clip a vol-18 (5 Wheat) case decided in early 1821. Extract mirrors this
# superset; precise scoping to reporter volumes 2–18 is a downstream transform concern.
BEFORE = os.environ.get("SCOTUS_BEFORE", "1821-12-31")
PIPELINE_VERSION = "2.0"


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


def git_commit():
    """Short git commit of the build tree, or 'unknown'. Used to version-pin built assets so a
    consumer can confirm two files (e.g. core DB + apparatus asset) came from the same build."""
    import subprocess

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


def ensure_dirs():
    for d in (
        RAW_DIR,
        PROCESSED_DIR,
        DATASET_DIR,
        RAW_CLUSTERS_DIR,
        RAW_OPINIONS_DIR,
    ):
        os.makedirs(d, exist_ok=True)
