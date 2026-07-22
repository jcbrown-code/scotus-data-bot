"""Tests for src.transform.dedup — collapse duplicate records of one decision.

Pure unit tests for the composite signals, plus data-quality tests over the real
staging DB (skipped when absent). The load-bearing invariant: a merge never crosses
two different non-null scdb_ids, so no distinct decision is destroyed.
"""

import os
import sqlite3

import pytest

from config import settings
from src.transform import dedup


def _cluster(cid, name, scdb=None, vol=6, page="10", text=""):
    return dedup.Cluster(cid, vol, page, name, scdb, dedup.build_shingles(text))


# ---- name + text primitives --------------------------------------------------


def test_canonicalize_is_order_and_noise_independent():
    assert dedup.canonicalize_case_name("Brown v. The Barry") == dedup.canonicalize_case_name(
        "Barry v. Brown"
    )


def test_overlap_is_containment_not_jaccard():
    # a short reprint fully inside a long opinion -> containment ~1 though Jaccard is low
    long = "alpha beta gamma delta epsilon zeta eta theta iota kappa lamda mu nu xi"
    short = "gamma delta epsilon zeta eta theta"
    coefficient, shared = dedup.overlap_coefficient(
        dedup.build_shingles(long, size=3), dedup.build_shingles(short, size=3)
    )
    assert coefficient == 1.0 and shared > 0


def test_overlap_empty_is_zero():
    assert dedup.overlap_coefficient(frozenset(), dedup.build_shingles("a b c d e f")) == (0.0, 0)


# ---- classify_pair -----------------------------------------------------------


def test_different_scdb_never_merges():
    a = _cluster(1, "Smith v. Jones", scdb="1810-001")
    b = _cluster(2, "Smith v. Jones", scdb="1810-002")  # identical name, different decision
    assert dedup.classify_pair(a, b) == (False, "different_scdb")


def test_high_name_similarity_merges():
    a = _cluster(1, "Ogle v. Lee")
    b = _cluster(2, "Ogle v. Lee")
    assert dedup.classify_pair(a, b) == (True, "name")


def test_low_name_high_text_merges():
    body = " ".join(f"word{i}" for i in range(200))
    a = _cluster(1, "The Alexander, Picket, Master", text=body)
    b = _cluster(2, "The Alexander", text=body)  # low name similarity, identical text
    assert dedup.score_name_similarity(a.case_name, b.case_name) < dedup.NAME_MERGE_THRESHOLD
    assert dedup.classify_pair(a, b) == (True, "text")


def test_low_name_low_text_stays_distinct():
    a = _cluster(1, "Sturges v. Crowninshield", text="one two three four five six seven eight")
    b = _cluster(
        2, "Bank of Columbia v. Okely", text="alpha bravo charlie delta echo foxtrot golf"
    )
    assert dedup.classify_pair(a, b)[0] is False


def test_small_shared_text_below_floor_does_not_merge():
    # coincidental short overlap must not trigger a merge (the "large enough" guard)
    a = _cluster(1, "Alpha v. Beta", text="the court below did err in this matter")
    b = _cluster(2, "Gamma v. Delta", text="the court below did err in another cause entirely")
    coefficient, shared = dedup.overlap_coefficient(a.shingles, b.shingles)
    assert shared < dedup.MIN_SHARED_SHINGLES
    assert dedup.classify_pair(a, b)[0] is False


# ---- grouping ----------------------------------------------------------------


def test_scdb_anchors_never_merge_via_a_shared_nonscdb():
    body = " ".join(f"w{i}" for i in range(200))
    anchor_a = _cluster(1, "Case A", scdb="1810-001", text=body)
    anchor_b = _cluster(2, "Case B", scdb="1810-002", text=body)  # same text, different decision
    stub = _cluster(3, "Case A", text=body)  # looks like both; must attach to only one
    groups = dedup.group_page_clusters([anchor_a, anchor_b, stub])
    # two distinct scdb decisions remain in separate groups
    assert len(groups) == 2
    ids = sorted(sorted(c.cluster_id for c in g) for g in groups)
    assert [1, 3] in ids and [2] in ids


def test_offpage_requires_both_name_and_text():
    body = " ".join(f"w{i}" for i in range(200))
    a = _cluster(1, "The Diana", page="27", text=body)
    b = _cluster(2, "The Diana", page="58", text=body)  # same case, different page
    c = _cluster(3, "The Diana", page="99", text="wholly different opinion text here now")
    assert dedup.classify_offpage_pair(a, b) is True  # name + text
    assert dedup.classify_offpage_pair(a, c) is False  # name only, no text corroboration


def test_build_records_labels_canonical_and_duplicates():
    body = " ".join(f"w{i}" for i in range(200))
    clusters = [
        _cluster(1, "The Diana", scdb="1818-001", page="27", text=body),
        _cluster(2, "The Diana", page="58", text=body),  # off-page dup
        _cluster(3, "Unrelated v. Party", page="27", text="alpha beta gamma delta epsilon zeta"),
    ]
    records = {r.cluster_id: r for r in dedup.build_dedup_records(clusters)}
    assert records[1].dedup_role == "canonical"  # scdb wins
    assert records[2].dedup_role == "duplicate" and records[2].dup_of == 1
    assert records[2].dup_method == "off_page"
    assert records[3].dedup_role == "canonical"  # distinct, untouched


# ---- round trip --------------------------------------------------------------


def test_run_dedup_writes_table(tmp_path):
    db_path = str(tmp_path / "staging.sqlite")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE stg_cluster_scope (cluster_id INTEGER PRIMARY KEY, us_volume INTEGER, "
        "us_page TEXT, case_name TEXT, scdb_id TEXT, is_scotus TEXT)"
    )
    source_cols = ", ".join(f"{f} TEXT" for f in dedup._SOURCE_FIELDS)
    conn.execute(
        f"CREATE TABLE stg_opinions (opinion_id INTEGER PRIMARY KEY, cluster_id INTEGER, "
        f"{source_cols})"
    )
    body = " ".join(f"w{i}" for i in range(200))
    conn.executemany(
        "INSERT INTO stg_cluster_scope VALUES (?,?,?,?,?,?)",
        [
            (1, 6, "10", "Ogle v. Lee", None, "true"),
            (2, 6, "10", "Ogle v. Lee", None, "true"),  # same-page dup
            (3, 6, "20", "Faw v. Marsteller", None, "true"),  # distinct
        ],
    )
    conn.executemany(
        "INSERT INTO stg_opinions (opinion_id, cluster_id, source_plain_text) VALUES (?,?,?)",
        [(10, 1, body), (20, 2, body), (30, 3, "alpha beta gamma delta")],
    )
    conn.commit()
    conn.close()

    records = dedup.run_dedup(db_path)
    roles = {r.cluster_id: r.dedup_role for r in records}
    assert roles == {1: "canonical", 2: "duplicate", 3: "canonical"}
    conn = sqlite3.connect(db_path)
    stored = conn.execute("SELECT count(*) FROM stg_cluster_dedup WHERE dedup_role='duplicate'")
    assert stored.fetchone()[0] == 1
    conn.close()


# ---- data-quality against real staging ---------------------------------------


def _real_records():
    if not os.path.exists(settings.STAGING_DB_PATH):
        pytest.skip("staging DB missing; run materialize + scope first")
    conn = sqlite3.connect(settings.STAGING_DB_PATH)
    has_scope = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='stg_cluster_scope'"
    ).fetchone()[0]
    conn.close()
    if not has_scope:
        pytest.skip("stg_cluster_scope missing; run the scope stage first")
    return dedup.build_dedup_records(dedup.read_keep_candidates(settings.STAGING_DB_PATH))


def test_no_merge_crosses_different_scdb():
    """The load-bearing safety invariant: a duplicate never points at a canonical
    with a different non-null scdb_id (that would fuse two distinct decisions)."""
    records = _real_records()
    role = {r.cluster_id: r for r in records}
    offenders = [
        r.cluster_id
        for r in records
        if r.dedup_role == "duplicate"
        and r.scdb_id
        and role[r.dup_of].scdb_id
        and r.scdb_id != role[r.dup_of].scdb_id
    ]
    assert offenders == [], f"merges fused different scdb decisions: {offenders}"


def test_every_duplicate_points_at_a_canonical():
    records = _real_records()
    canonicals = {r.cluster_id for r in records if r.dedup_role == "canonical"}
    dangling = [
        r.cluster_id for r in records if r.dedup_role == "duplicate" and r.dup_of not in canonicals
    ]
    assert dangling == []
