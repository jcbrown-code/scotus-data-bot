"""Transform · dedup: collapse duplicate *records of the same decision* to one canonical.

scope produced the SCOTUS keep-candidates; many are the same decision recorded more
than once (CourtListener merges reporters imperfectly). This stage labels each
keep-candidate canonical or duplicate -- it does NOT touch opinions. A duplicate
cluster keeps all its opinion rows (they carry real, sometimes distinct, source
text), so the 1:many cluster->opinion hierarchy stays intact and no opinion data is
dropped; reconciling those source variants is a later stage's job.

No single field decides a duplicate (measured on the staging data):
- ``us_page`` co-locates DIFFERENT cases (three distinct decisions at 2 U.S. 401).
- ``case_name`` overlaps both ways: true duplicates fall as low as 0.20 similarity
  (``The Alexander`` vs ``The Alexander, Picket, Master``) while distinct co-located
  cases reach 0.73 (``Gracie v. Maryland Ins.`` vs ``Richards v. Maryland Ins.``).
- ``scdb_id`` is authoritative for *identity*: two different non-null scdb ids are
  two different decisions, always.

So the rule is composite, and page-local:
1. Different non-null ``scdb_id`` -> never merge (hard block; each is a decision).
2. Otherwise a pair is a duplicate when the captions are highly similar, OR their
   opinion texts overlap heavily -- ``shared / smaller`` containment (robust to the
   large length gaps between a full opinion and a short reprint) above a threshold
   AND above an absolute shared-shingle floor, so a few coincidental legal phrases
   (Sturges v. Crowninshield shares 11 with a page-mate) never trigger a merge.
Grouping is anchored on the scdb clusters so a non-scdb duplicate attaches to at
most one decision and can never transitively link two distinct scdb decisions.

The stage errs toward NOT merging: a missed merge leaves a duplicate that validate
flags against the reference, while a false merge irreversibly destroys a distinct
decision.
"""

import difflib
import re
import sqlite3
from typing import NamedTuple

from config import settings

# Captions this similar (0-1) are the same case by name alone.
NAME_MERGE_THRESHOLD = 0.85
# Opinion-text containment (shared shingles / smaller shingle set) this high, AND at
# least this many shared shingles, confirms a duplicate when the name is not decisive.
TEXT_OVERLAP_THRESHOLD = 0.5
MIN_SHARED_SHINGLES = 30
SHINGLE_SIZE = 5  # word n-gram length
# The same decision sometimes appears at two different page numbers in one volume
# (The Diana at 16 U.S. 27 and 58). Matching across pages is riskier -- distinct
# cases can share a recurring party name -- so it requires BOTH a high-similarity
# caption and text corroboration, not either alone.
OFFPAGE_NAME_THRESHOLD = 0.85

# Caption tokens dropped before comparison (legal connectives).
_STOP_WORDS = {"v", "the", "of", "a", "and", "et", "al", "in", "for"}
# Opinion source fields, richest-first-ish; the longest populated one represents the text.
_SOURCE_FIELDS = (
    "source_html_lawbox",
    "source_xml_harvard",
    "source_html",
    "source_html_columbia",
    "source_html_anon_2020",
    "source_html_with_citations",
    "source_plain_text",
)


def canonicalize_case_name(name: str) -> str:
    """Distinctive, order-independent token string for fuzzy caption comparison."""
    text = (name or "").lower().replace("m'", "mc")
    text = re.sub(r"'s\b", "", text)
    tokens = {
        word
        for word in re.sub(r"[^a-z0-9 ]", " ", text).split()
        if word not in _STOP_WORDS and len(word) > 1
    }
    return " ".join(sorted(tokens))


def score_name_similarity(left: str, right: str) -> float:
    """Fuzzy caption similarity in [0, 1] after canonicalization."""
    a, b = canonicalize_case_name(left), canonicalize_case_name(right)
    return difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def build_shingles(text: str, size: int = SHINGLE_SIZE) -> frozenset:
    """Word n-gram shingle set of an opinion's text (markup and punctuation stripped)."""
    stripped = re.sub(r"<[^>]+>", " ", text or "")
    words = re.sub(r"[^a-z0-9 ]", " ", stripped.lower()).split()
    return frozenset(tuple(words[i : i + size]) for i in range(len(words) - size + 1))


def overlap_coefficient(left: frozenset, right: frozenset) -> tuple[float, int]:
    """Containment overlap (shared / smaller set) and the shared-shingle count.

    Containment, not Jaccard: a short reprint is a subset of the full opinion, so
    Jaccard would score a true duplicate low; containment measures how much of the
    smaller text is present in the larger."""
    if not left or not right:
        return 0.0, 0
    shared = len(left & right)
    return shared / min(len(left), len(right)), shared


class Cluster(NamedTuple):
    """A keep-candidate cluster with the fields dedup compares on."""

    cluster_id: int
    us_volume: int | None
    us_page: str | None
    case_name: str
    scdb_id: str | None
    shingles: frozenset


def classify_pair(a: Cluster, b: Cluster) -> tuple[bool, str]:
    """Decide whether two clusters are the same decision, with the deciding signal."""
    if a.scdb_id and b.scdb_id and a.scdb_id != b.scdb_id:
        return False, "different_scdb"
    if score_name_similarity(a.case_name, b.case_name) >= NAME_MERGE_THRESHOLD:
        return True, "name"
    coefficient, shared = overlap_coefficient(a.shingles, b.shingles)
    if coefficient >= TEXT_OVERLAP_THRESHOLD and shared >= MIN_SHARED_SHINGLES:
        return True, "text"
    return False, "distinct"


def group_page_clusters(clusters: list[Cluster]) -> list[list[Cluster]]:
    """Group one page's clusters into same-decision sets, anchored on scdb clusters.

    Each distinct scdb_id is a decision anchor; a non-scdb cluster joins the single
    anchor it best matches (never two, so distinct scdb decisions stay apart), and
    non-scdb clusters that match no anchor group among themselves."""
    anchors: dict[str, list[Cluster]] = {}
    non_scdb: list[Cluster] = []
    for cluster in clusters:
        if cluster.scdb_id:
            anchors.setdefault(cluster.scdb_id, []).append(cluster)
        else:
            non_scdb.append(cluster)

    groups = list(anchors.values())
    unattached: list[Cluster] = []
    for cluster in non_scdb:
        best_group, best_score = None, 0.0
        for group in groups:
            matched, _ = classify_pair(cluster, group[0])
            if not matched:
                continue
            score = score_name_similarity(cluster.case_name, group[0].case_name)
            score = max(score, overlap_coefficient(cluster.shingles, group[0].shingles)[0])
            if score > best_score:
                best_group, best_score = group, score
        if best_group is not None:
            best_group.append(cluster)
        else:
            unattached.append(cluster)

    # Non-scdb clusters that matched no anchor: union-find among themselves.
    parent = {c.cluster_id: c.cluster_id for c in unattached}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(unattached):
        for b in unattached[i + 1 :]:
            if classify_pair(a, b)[0]:
                parent[find(a.cluster_id)] = find(b.cluster_id)
    by_root: dict[int, list[Cluster]] = {}
    for cluster in unattached:
        by_root.setdefault(find(cluster.cluster_id), []).append(cluster)
    groups.extend(by_root.values())
    return groups


def classify_offpage_pair(a: Cluster, b: Cluster) -> bool:
    """Whether two clusters at different pages of a volume are the same decision.

    Stricter than the same-page test: a high-similarity caption AND heavy text
    overlap, both, since a shared party name across pages is not enough on its own."""
    if a.scdb_id and b.scdb_id and a.scdb_id != b.scdb_id:
        return False
    if score_name_similarity(a.case_name, b.case_name) < OFFPAGE_NAME_THRESHOLD:
        return False
    coefficient, shared = overlap_coefficient(a.shingles, b.shingles)
    return coefficient >= TEXT_OVERLAP_THRESHOLD and shared >= MIN_SHARED_SHINGLES


def _canonical_sort_key(cluster: Cluster) -> tuple:
    """Canonical preference: scdb first, then more text, then lowest id (deterministic)."""
    total_shingles = len(cluster.shingles)
    return (0 if cluster.scdb_id else 1, -total_shingles, cluster.cluster_id)


class DedupRecord(NamedTuple):
    cluster_id: int
    us_volume: int | None
    us_page: str | None
    case_name: str
    scdb_id: str | None
    dedup_role: str  # "canonical" | "duplicate"
    dup_of: int | None
    dup_method: str | None  # signal that attached a duplicate; None for canonicals


def _merge_offpage_groups(groups: list[list[Cluster]]) -> list[list[Cluster]]:
    """Union same-page groups within one volume whose representatives are the same
    decision at different pages (The Diana at pages 27 and 58)."""
    representatives = [min(group, key=_canonical_sort_key) for group in groups]
    parent = list(range(len(groups)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            if representatives[i].us_page != representatives[j].us_page and classify_offpage_pair(
                representatives[i], representatives[j]
            ):
                parent[find(i)] = find(j)

    merged: dict[int, list[Cluster]] = {}
    for index, group in enumerate(groups):
        merged.setdefault(find(index), []).extend(group)
    return list(merged.values())


def build_dedup_records(clusters: list[Cluster]) -> list[DedupRecord]:
    """Group keep-candidates (same-page, then off-page within a volume); label each."""
    by_page: dict[tuple, list[Cluster]] = {}
    for cluster in clusters:
        by_page.setdefault((cluster.us_volume, cluster.us_page), []).append(cluster)

    groups_by_volume: dict[object, list[list[Cluster]]] = {}
    for (volume, _page), page_clusters in by_page.items():
        groups_by_volume.setdefault(volume, []).extend(group_page_clusters(page_clusters))

    records: list[DedupRecord] = []
    for groups in groups_by_volume.values():
        for group in _merge_offpage_groups(groups):
            canonical = min(group, key=_canonical_sort_key)
            for cluster in group:
                if cluster.cluster_id == canonical.cluster_id:
                    records.append(_record(cluster, "canonical", None, None))
                    continue
                if cluster.us_page != canonical.us_page:
                    method = "off_page"
                else:
                    method = classify_pair(cluster, canonical)[1]
                    if method in ("distinct", "different_scdb"):
                        method = "grouped"  # transitively linked within the page group
                records.append(_record(cluster, "duplicate", canonical.cluster_id, method))
    records.sort(key=lambda record: record.cluster_id)
    return records


def _record(cluster: Cluster, role: str, dup_of: int | None, method: str | None) -> DedupRecord:
    return DedupRecord(
        cluster_id=cluster.cluster_id,
        us_volume=cluster.us_volume,
        us_page=cluster.us_page,
        case_name=cluster.case_name,
        scdb_id=cluster.scdb_id,
        dedup_role=role,
        dup_of=dup_of,
        dup_method=method,
    )


def read_keep_candidates(staging_db_path: str) -> list[Cluster]:
    """Load scope keep-candidates with their longest opinion text as shingles."""
    conn = sqlite3.connect(staging_db_path)
    try:
        conn.row_factory = sqlite3.Row
        scoped = conn.execute(
            "SELECT cluster_id, us_volume, us_page, case_name, scdb_id "
            "FROM stg_cluster_scope WHERE is_scotus = 'true' ORDER BY cluster_id"
        ).fetchall()
        source_expr = ", ".join(_SOURCE_FIELDS)
        text_by_cluster: dict[int, str] = {}
        for opinion in conn.execute(f"SELECT cluster_id, {source_expr} FROM stg_opinions"):
            longest = max((opinion[f] or "" for f in _SOURCE_FIELDS), key=len, default="")
            if len(longest) > len(text_by_cluster.get(opinion["cluster_id"], "")):
                text_by_cluster[opinion["cluster_id"]] = longest
    finally:
        conn.close()
    return [
        Cluster(
            cluster_id=row["cluster_id"],
            us_volume=row["us_volume"],
            us_page=row["us_page"],
            case_name=row["case_name"] or "",
            scdb_id=row["scdb_id"],
            shingles=build_shingles(text_by_cluster.get(row["cluster_id"], "")),
        )
        for row in scoped
    ]


_DEDUP_COLUMNS = (
    "cluster_id",
    "us_volume",
    "us_page",
    "case_name",
    "scdb_id",
    "dedup_role",
    "dup_of",
    "dup_method",
)


def write_dedup_table(staging_db_path: str, records: list[DedupRecord]) -> None:
    """Write the derived stg_cluster_dedup table (clean rebuild; idempotent)."""
    columns = ", ".join(_DEDUP_COLUMNS)
    placeholders = ", ".join("?" for _ in _DEDUP_COLUMNS)
    conn = sqlite3.connect(staging_db_path)
    try:
        conn.execute("DROP TABLE IF EXISTS stg_cluster_dedup")
        conn.execute(
            "CREATE TABLE stg_cluster_dedup ("
            "cluster_id INTEGER PRIMARY KEY, us_volume INTEGER, us_page TEXT, case_name TEXT, "
            "scdb_id TEXT, dedup_role TEXT NOT NULL, dup_of INTEGER, dup_method TEXT)"
        )
        conn.executemany(
            f"INSERT INTO stg_cluster_dedup ({columns}) VALUES ({placeholders})",
            [tuple(record) for record in records],
        )
        conn.commit()
    finally:
        conn.close()


def run_dedup(staging_db_path: str = settings.STAGING_DB_PATH) -> list[DedupRecord]:
    """Read keep-candidates, label duplicates, write the dedup table (cluster-id order)."""
    clusters = read_keep_candidates(staging_db_path)
    records = build_dedup_records(clusters)
    write_dedup_table(staging_db_path, records)
    return records
