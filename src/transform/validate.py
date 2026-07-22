"""Transform · validate: reconcile the deduplicated KEEP set against the reference.

This is the acceptance check for the corpus. It matches the canonical (deduplicated)
SCOTUS decisions, per U.S. Reports volume, against ``dataset/case_name_reference.csv``
-- the authoritative by-volume case list (SCDB / Wikipedia ingest) -- and reports,
for each volume: how many we kept, how many the reference lists, how many matched,
which reference cases we are MISSING, and which of ours are EXTRA (a residual
duplicate or an anomaly). Per-volume is the sole reconciliation authority: a reporter
volume has a fixed table of contents, so its case count is a stable ground truth (year
counts drift on term-vs-decision-date attribution).

Final-corpus scope is U.S. Reports vols 2-18. Vol 19 (1821 Wheaton) is pulled into
staging only as an extract buffer so a year-based edge case cannot be dropped; it is
reported separately and excluded from the corpus totals.

This stage reads only (stg_cluster_dedup + the reference) and writes a report; it
changes no corpus data.

Column-naming trap (verified against the staging data): in the reference CSV the
column named ``rep_vol`` holds the U.S. Reports volume (2-19) that matches
``us_volume``; the column named ``us_vol`` is a different, unused numbering. Key on
``rep_vol``.

Matching (page-primary, ported from the validated standalone checker): group both
sides by page; within a page match by fuzzy name (disambiguates the several cases at
one page and tolerates OCR/spelling drift); a reference case with no cluster at its
page falls back to a volume-wide fuzzy name match; a cluster matching no reference
case is EXTRA (a duplicate of an already-matched cluster, or an anomaly to review).
"""

import collections
import csv
import difflib
import re
import sqlite3
from typing import NamedTuple

from config import settings

# Final V2 corpus span. Vol 19 is a staging buffer, reported apart and not counted.
CORPUS_MIN_VOLUME = 2
CORPUS_MAX_VOLUME = 18
BUFFER_VOLUME = 19

# Name-matching vocabulary and thresholds (ported from the validated checker).
_STOP_WORDS = {"v", "the", "of", "a", "and", "et", "al", "in"}
_DESCRIPTOR_WORDS = {
    "master",
    "claimant",
    "claimants",
    "others",
    "other",
    "administrator",
    "administratrix",
    "assignee",
    "executor",
    "executrix",
    "lessee",
    "surviving",
    "error",
    "use",
    "president",
    "directors",
    "company",
    "bank",
    "insurance",
    "ins",
    "co",
    "comp",
    "compy",
}
_ROMAN_NUMERALS = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"}
_PAGE_NAME_THRESHOLD = 0.35  # a page already pins the case; name only disambiguates
_VOLUME_NAME_THRESHOLD = 0.72  # no page support; name must carry it
_DUPLICATE_NAME_THRESHOLD = 0.7  # an extra this close to a matched name is a duplicate


def canonicalize_name(name: str) -> str:
    """Distinctive-token string: lowercase, m'->mc, drop possessive/roman/stop/descriptor."""
    text = (name or "").lower().replace("m'", "mc")
    text = re.sub(r"'s\b", "", text)
    tokens = [
        word
        for word in re.sub(r"[^a-z0-9 ]", " ", text).split()
        if word not in _STOP_WORDS
        and word not in _DESCRIPTOR_WORDS
        and word not in _ROMAN_NUMERALS
        and len(word) > 1
    ]
    return "".join(sorted(set(tokens)))


def score_name_similarity(left: str, right: str) -> float:
    """Fuzzy similarity in [0, 1] between two captions after canonicalization."""
    a, b = canonicalize_name(left), canonicalize_name(right)
    return difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0


class ReferenceCase(NamedTuple):
    us_volume: int
    page: str
    name: str
    year: str


class KeepCluster(NamedTuple):
    cluster_id: int
    us_volume: int
    us_page: str
    case_name: str
    date_filed: str


class VolumeReconciliation(NamedTuple):
    volume: int
    n_keep: int
    n_reference: int
    n_matched: int
    missing: list  # ReferenceCase with no cluster
    extras: list  # (KeepCluster, kind) matching no reference case


def match_volume_to_reference(reference_cases: list, clusters: list) -> VolumeReconciliation:
    """Match one volume's canonical clusters to its reference cases (page then name)."""
    matched: dict[int, ReferenceCase] = {}
    reference_done: set[int] = set()
    matched_pages: set[str] = set()

    clusters_by_page: dict[str, list] = collections.defaultdict(list)
    for cluster in clusters:
        clusters_by_page[str(cluster.us_page)].append(cluster)

    # Phase 1: within-page greedy fuzzy-name matching.
    reference_by_page: dict[str, list[int]] = collections.defaultdict(list)
    for index, case in enumerate(reference_cases):
        reference_by_page[case.page].append(index)
    for page, indexes in reference_by_page.items():
        candidates = [c for c in clusters_by_page.get(page, []) if c.cluster_id not in matched]
        scored = sorted(
            (
                (score_name_similarity(reference_cases[i].name, c.case_name), i, c)
                for i in indexes
                for c in candidates
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        used: set[int] = set()
        for similarity, index, cluster in scored:
            if (
                index in reference_done
                or cluster.cluster_id in matched
                or cluster.cluster_id in used
                or similarity < _PAGE_NAME_THRESHOLD
            ):
                continue
            matched[cluster.cluster_id] = reference_cases[index]
            reference_done.add(index)
            used.add(cluster.cluster_id)
            matched_pages.add(page)

    # Phase 2: volume-wide fallback for reference cases still unmatched (page typos).
    for index, case in enumerate(reference_cases):
        if index in reference_done:
            continue
        best_similarity, best_cluster = 0.0, None
        for cluster in clusters:
            if cluster.cluster_id in matched:
                continue
            similarity = score_name_similarity(case.name, cluster.case_name)
            if similarity > best_similarity:
                best_similarity, best_cluster = similarity, cluster
        if best_cluster is not None and best_similarity >= _VOLUME_NAME_THRESHOLD:
            matched[best_cluster.cluster_id] = case
            reference_done.add(index)
            matched_pages.add(str(best_cluster.us_page))

    missing = [case for i, case in enumerate(reference_cases) if i not in reference_done]
    matched_names = [c.case_name for c in clusters if c.cluster_id in matched]
    extras = []
    for cluster in clusters:
        if cluster.cluster_id in matched:
            continue
        best_name = max(
            (score_name_similarity(cluster.case_name, name) for name in matched_names), default=0.0
        )
        if best_name >= _DUPLICATE_NAME_THRESHOLD:
            kind = "residual_duplicate"
        elif str(cluster.us_page) in matched_pages:
            kind = "residual_duplicate"
        else:
            kind = "anomaly"
        extras.append((cluster, kind))
    volume = (
        reference_cases[0].us_volume
        if reference_cases
        else (clusters[0].us_volume if clusters else 0)
    )
    return VolumeReconciliation(
        volume, len(clusters), len(reference_cases), len(matched), missing, extras
    )


def load_reference(path: str) -> dict[int, list]:
    """Reference cases grouped by U.S. Reports volume (the rep_vol column)."""
    by_volume: dict[int, list] = collections.defaultdict(list)
    with open(path, newline="") as handle:
        for row in csv.DictReader(handle):
            volume = int(row["rep_vol"])
            by_volume[volume].append(
                ReferenceCase(volume, str(row["page"]), row["name"], row["year"])
            )
    return dict(by_volume)


def read_canonical_keep(staging_db_path: str) -> dict[int, list]:
    """Canonical (deduplicated) KEEP clusters in the corpus span, grouped by volume."""
    conn = sqlite3.connect(staging_db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT d.cluster_id, d.us_volume, d.us_page, d.case_name, c.date_filed "
            "FROM stg_cluster_dedup d JOIN stg_clusters c USING (cluster_id) "
            "WHERE d.dedup_role = 'canonical' AND d.us_volume BETWEEN ? AND ? "
            "ORDER BY d.cluster_id",
            (CORPUS_MIN_VOLUME, CORPUS_MAX_VOLUME),
        ).fetchall()
    finally:
        conn.close()
    by_volume: dict[int, list] = collections.defaultdict(list)
    for row in rows:
        by_volume[row["us_volume"]].append(
            KeepCluster(
                row["cluster_id"],
                row["us_volume"],
                row["us_page"] or "",
                row["case_name"] or "",
                row["date_filed"] or "",
            )
        )
    return dict(by_volume)


def reconcile(staging_db_path: str, reference_path: str) -> list:
    """Per-volume reconciliation of the KEEP set against the reference (corpus span)."""
    reference = load_reference(reference_path)
    keep = read_canonical_keep(staging_db_path)
    results = []
    for volume in range(CORPUS_MIN_VOLUME, CORPUS_MAX_VOLUME + 1):
        results.append(match_volume_to_reference(reference.get(volume, []), keep.get(volume, [])))
    return results


_VOLUME_COLUMNS = ("volume", "n_keep", "n_reference", "n_matched", "n_missing", "n_extra")


def write_report(staging_db_path: str, report_csv_path: str, results: list) -> None:
    """Write the per-volume summary + a per-case detail table (SQLite) and a committed CSV."""
    conn = sqlite3.connect(staging_db_path)
    try:
        conn.execute("DROP TABLE IF EXISTS stg_validate_volume")
        conn.execute("DROP TABLE IF EXISTS stg_validate_detail")
        conn.execute(
            "CREATE TABLE stg_validate_volume (volume INTEGER PRIMARY KEY, n_keep INTEGER, "
            "n_reference INTEGER, n_matched INTEGER, n_missing INTEGER, n_extra INTEGER)"
        )
        conn.execute(
            "CREATE TABLE stg_validate_detail (volume INTEGER, kind TEXT, cluster_id INTEGER, "
            "page TEXT, name TEXT)"
        )
        for r in results:
            conn.execute(
                "INSERT INTO stg_validate_volume VALUES (?,?,?,?,?,?)",
                (r.volume, r.n_keep, r.n_reference, r.n_matched, len(r.missing), len(r.extras)),
            )
            for case in r.missing:
                conn.execute(
                    "INSERT INTO stg_validate_detail VALUES (?,?,?,?,?)",
                    (r.volume, "missing", None, case.page, case.name),
                )
            for cluster, kind in r.extras:
                conn.execute(
                    "INSERT INTO stg_validate_detail VALUES (?,?,?,?,?)",
                    (r.volume, kind, cluster.cluster_id, cluster.us_page, cluster.case_name),
                )
        conn.commit()
    finally:
        conn.close()
    with open(report_csv_path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(_VOLUME_COLUMNS)
        for r in results:
            writer.writerow(
                (r.volume, r.n_keep, r.n_reference, r.n_matched, len(r.missing), len(r.extras))
            )


def format_report(results: list) -> str:
    """Human-readable per-volume reconciliation of the final corpus vs the reference."""
    lines = ["", "PER-VOLUME reconciliation vs reference (final corpus, U.S. vols 2-18):"]
    lines.append(
        f"  {'vol':>3} {'keep':>5} {'ref':>4} {'match':>5} {'miss':>4} {'extra':>5}  status"
    )
    total = collections.Counter()
    for r in results:
        for key, value in (
            ("keep", r.n_keep),
            ("ref", r.n_reference),
            ("match", r.n_matched),
            ("miss", len(r.missing)),
            ("extra", len(r.extras)),
        ):
            total[key] += value
        status = (
            "OK"
            if not r.missing and not r.extras
            else f"{len(r.missing)}miss/{len(r.extras)}extra"
        )
        lines.append(
            f"  {r.volume:>3} {r.n_keep:>5} {r.n_reference:>4} {r.n_matched:>5} "
            f"{len(r.missing):>4} {len(r.extras):>5}  {status}"
        )
    lines.append(
        f"  TOT {total['keep']:>5} {total['ref']:>4} {total['match']:>5} "
        f"{total['miss']:>4} {total['extra']:>5}"
    )
    lines.append(
        f"  corpus KEEP={total['keep']} vs reference={total['ref']} "
        f"(Δ {total['keep'] - total['ref']:+d})"
    )

    detail = [
        f"    vol{r.volume} MISSING p{c.page} {c.name!r}" for r in results for c in r.missing
    ]
    detail += [
        f"    vol{r.volume} EXTRA[{k}] cid={cl.cluster_id} p{cl.us_page} {cl.case_name!r}"
        for r in results
        for cl, k in r.extras
    ]
    if detail:
        lines.append("  detail (reference cases missing, and kept clusters not in the reference):")
        lines.extend(detail)
    return "\n".join(lines)


def run_validate(
    staging_db_path: str = settings.STAGING_DB_PATH,
    reference_path: str = settings.CASE_NAME_REFERENCE_CSV,
    report_csv_path: str = settings.VALIDATE_REPORT_CSV,
) -> list:
    """Reconcile the KEEP set against the reference, write the report, return the results."""
    results = reconcile(staging_db_path, reference_path)
    write_report(staging_db_path, report_csv_path, results)
    return results
