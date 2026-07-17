"""Transform · scope: decide, per cluster, whether it is a genuine SCOTUS decision.

CourtListener's ``docket__court=scotus`` tag is imperfect -- it stamps ``scotus``
on district, circuit, and state cases too (e.g. Meade v. Deputy Marshal, a
district-of-Virginia habeas) -- and the HTML header "Supreme Court of United
States" is stamped on every Dallas reprint, so neither can be trusted. The
determination is binary and rests on two authorities:

- **Reporter.** Cranch (vols 5-13) and Wheaton (14-19) were official SCOTUS-only
  reporters, so any in-scope cluster from vol 5 up is SCOTUS by the authority of
  the reporter it appears in. Dallas (2-4) covered three courts (PA state, U.S.
  Circuit-PA, and SCOTUS), so a Dallas citation alone proves nothing.
- **SCDB, within Dallas.** An ``scdb_id`` (SCDB is a SCOTUS-only catalog) keeps a
  Dallas cluster; without one the cluster is not a SCOTUS case. This rule is
  empirical and Dallas-only: the completed human review (dataset/REVIEW_NOTES.md
  + review_dispositions.csv) examined every one of the 205 non-scdb Dallas
  clusters and found 168 PA-state/circuit cases, 3 administrative orders (a
  decisions-only corpus excludes them), and 34 duplicate stubs whose genuine
  twins survive via their own scdb clusters -- with the single exception curated
  below. Do NOT generalize the rule past Dallas: CourtListener's scdb tagging is
  incomplete in vols 5-19 (289 genuine clusters there carry no scdb_id), so
  scdb-absence means "not SCOTUS" only where the review verified it.

Deliberately out of scope here: matching captions against the per-volume
reference list. That is fuzzy name matching -- a separate concern for the dedup
and validate-against-reference stages -- and folding it in makes the predicate
fragile (two "United States v. ___" captions look alike).

The stage is propose-only and non-destructive: it labels each cluster with an
``is_scotus`` verdict, the ``evidence`` behind it, and a proposed disposition
(keep / drop). Nothing is deleted here.
"""

import sqlite3
from enum import Enum
from typing import NamedTuple

from config import settings

# In-scope U.S. Reports volumes for the 1790-1821 corpus. The upper bound is 19
# (6 Wheaton, 1821), which aligns the corpus with the reference list's coverage;
# vol 20+ (an scdb 1822-term straggler caught by the date window) is out of scope.
SCOPE_MIN_VOLUME = 2
SCOPE_MAX_VOLUME = 19
# Dallas (2-4) is the only mixed-court reporter and must be adjudicated. From vol 5
# (Cranch) onward the reporters were SCOTUS-only, so reporter authority alone is
# dispositive.
FIRST_SCOTUS_ONLY_VOLUME = 5
# In 2 U.S. (Dallas vol 2) the SCOTUS cases begin at page 401 (Hollingsworth /
# West v. Barnes); earlier pages are Pennsylvania cases. A corroborating tell only.
DALLAS_SCOTUS_START_PAGE = 401

# Human-verified overrides of the Dallas scdb rule, keyed by cluster_id. Each entry
# is a genuine SCOTUS decision whose ONLY CourtListener record carries no scdb_id,
# verified case-by-case against the per-volume reference list and the record's text.
# Kept deliberately tiny and explicit -- an exception here must name its evidence.
CURATED_SCOTUS_EXCEPTIONS = {
    # In the reference list (vol 4 p6); its one cluster has real Harvard text (a
    # short writ-of-error report) and no scdb twin exists anywhere in the mirror.
    # review_dispositions.csv mislabels it "DROP-duplicate: stub/placeholder" -- it
    # has no duplicate and is not textless. v1 dropped it, which is why the shipped
    # corpus counted 13 vol-4 cases against the reference's 14.
    6725725: "Hazlehurst v. United States, 4 U.S. 6 (1799)",
}


class IsScotus(str, Enum):
    """Whether a cluster is a genuine SCOTUS decision. ``str`` so it serializes."""

    TRUE = "true"
    FALSE = "false"


# Proposed disposition implied by the verdict (executed by a later stage, not here).
DISPOSITION_KEEP = "keep"
DISPOSITION_DROP = "drop"

_DISPOSITION_BY_VERDICT = {
    IsScotus.TRUE: DISPOSITION_KEEP,
    IsScotus.FALSE: DISPOSITION_DROP,
}


def _to_page_number(page) -> int | None:
    """Leading integer of a us_page string (stored as TEXT; may be None or non-numeric)."""
    if page is None:
        return None
    digits = ""
    for char in str(page):
        if char.isdigit():
            digits += char
        else:
            break
    return int(digits) if digits else None


def not_scotus_tells(cluster: dict) -> str:
    """Corroborating signs that a Dallas cluster is a lower court's, not SCOTUS.

    Attached to a Dallas drop proposal as audit evidence for a human scanning the
    drops; never decisive on their own (a genuine SCOTUS case can carry a
    "United States v." caption). These are cheap, deterministic caption/page
    checks -- not name matching.
    """
    tells = []
    name = (cluster.get("case_name") or "").lower()
    if name.startswith("respublica") or "commonwealth" in name:
        tells.append("respublica/commonwealth")  # Pennsylvania prosecutions
    if name.startswith("united states v"):
        tells.append("us_criminal_caption")  # Circuit-PA criminal (e.g. Whiskey Rebellion)
    if "lessee" in name:
        tells.append("lessee_ejectment")  # ejectment / circuit land cases
    page = _to_page_number(cluster.get("us_page"))
    if cluster.get("us_volume") == 2 and page is not None and page < DALLAS_SCOTUS_START_PAGE:
        tells.append("page_before_scotus_start")
    return ";".join(tells)


def determine_is_scotus(cluster: dict) -> tuple[IsScotus, str]:
    """Classify one cluster as SCOTUS TRUE / FALSE with an evidence string."""
    if cluster.get("us_cite") is None:
        return IsScotus.FALSE, "no_us_reports_cite"  # not in the U.S. Reports at all (Meade)

    volume = cluster.get("us_volume")
    if volume is None or not (SCOPE_MIN_VOLUME <= volume <= SCOPE_MAX_VOLUME):
        return IsScotus.FALSE, "out_of_scope_volume"

    if volume >= FIRST_SCOTUS_ONLY_VOLUME:  # Cranch + Wheaton = SCOTUS-only reporters
        if cluster.get("scdb_id"):
            return IsScotus.TRUE, "scotus_reporter+scdb"
        return IsScotus.TRUE, "scotus_only_reporter"

    # Dallas (2-4), mixed-court: an scdb entry (or a curated, human-verified
    # exception) keeps it; everything else is not a SCOTUS case (see module doc).
    if cluster.get("scdb_id"):
        return IsScotus.TRUE, "scdb_id"
    if cluster.get("cluster_id") in CURATED_SCOTUS_EXCEPTIONS:
        return IsScotus.TRUE, "curated_exception"
    tells = not_scotus_tells(cluster)
    return IsScotus.FALSE, f"dallas_not_in_scdb:{tells}" if tells else "dallas_not_in_scdb"


class ScopeProposal(NamedTuple):
    """One cluster's scope verdict and the disposition it implies (propose-only)."""

    cluster_id: int
    us_volume: int | None
    us_page: str | None
    case_name: str
    scdb_id: str | None
    is_scotus: str
    evidence: str
    proposed_disposition: str


def build_scope_proposals(clusters: list[dict]) -> list[ScopeProposal]:
    """Apply the determination to every cluster (pure; no I/O)."""
    proposals = []
    for cluster in clusters:
        verdict, evidence = determine_is_scotus(cluster)
        proposals.append(
            ScopeProposal(
                cluster_id=cluster["cluster_id"],
                us_volume=cluster.get("us_volume"),
                us_page=cluster.get("us_page"),
                case_name=cluster.get("case_name") or "",
                scdb_id=cluster.get("scdb_id"),
                is_scotus=verdict.value,
                evidence=evidence,
                proposed_disposition=_DISPOSITION_BY_VERDICT[verdict],
            )
        )
    return proposals


def read_staging_clusters(staging_db_path: str) -> list[dict]:
    """Read stg_clusters as plain dicts, in cluster-id order."""
    conn = sqlite3.connect(staging_db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM stg_clusters ORDER BY cluster_id").fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


_SCOPE_COLUMNS = (
    "cluster_id",
    "us_volume",
    "us_page",
    "case_name",
    "scdb_id",
    "is_scotus",
    "evidence",
    "proposed_disposition",
)


def write_scope_table(staging_db_path: str, proposals: list[ScopeProposal]) -> None:
    """Write the derived stg_cluster_scope table (clean rebuild; idempotent)."""
    columns = ", ".join(_SCOPE_COLUMNS)
    placeholders = ", ".join("?" for _ in _SCOPE_COLUMNS)
    conn = sqlite3.connect(staging_db_path)
    try:
        conn.execute("DROP TABLE IF EXISTS stg_cluster_scope")
        # No FOREIGN KEY to stg_clusters: a derived table must not block the materialize
        # stage's clean rebuild of the base tables (stages own and rebuild their artifacts
        # independently). Referential integrity is asserted by the data-quality tests.
        conn.execute(
            "CREATE TABLE stg_cluster_scope ("
            "cluster_id INTEGER PRIMARY KEY, "
            "us_volume INTEGER, us_page TEXT, case_name TEXT, scdb_id TEXT, "
            "is_scotus TEXT NOT NULL, evidence TEXT NOT NULL, proposed_disposition TEXT NOT NULL)"
        )
        conn.executemany(
            f"INSERT INTO stg_cluster_scope ({columns}) VALUES ({placeholders})",
            [tuple(proposal) for proposal in proposals],
        )
        conn.commit()
    finally:
        conn.close()


def run_scope(staging_db_path: str = settings.STAGING_DB_PATH) -> list[ScopeProposal]:
    """Read staging, classify every cluster, write the scope table.

    Returns the proposals in cluster-id order.
    """
    clusters = read_staging_clusters(staging_db_path)
    proposals = build_scope_proposals(clusters)
    write_scope_table(staging_db_path, proposals)
    return proposals
