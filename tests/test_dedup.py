"""Tests for src.transform.dedup — collapse duplicate records of one decision.

Pure unit tests for the composite signals and the human-review ledger, plus
data-quality tests over the real staging DB (skipped when absent). The load-bearing
invariant: the AUTOMATED passes never merge across two different non-null scdb_ids;
only an explicit, per-row-documented ledger pair may (and each such crossing is
enumerated by a test).
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


def test_offpage_far_page_requires_both_name_and_text():
    body = " ".join(f"w{i}" for i in range(200))
    a = _cluster(1, "The Diana", page="27", text=body)
    b = _cluster(2, "The Diana", page="58", text=body)  # same case, far page
    c = _cluster(3, "The Diana", page="99", text="wholly different opinion text here now")
    assert dedup.classify_offpage_pair(a, b) is True  # name + text
    assert dedup.classify_offpage_pair(a, c) is False  # name only, no text corroboration


def test_offpage_adjacent_identical_caption_merges_without_text():
    # a case's start page indexed a page apart, second copy a text-poor stub: an
    # obvious duplicate the text requirement must not block (Capron 126/127).
    body = " ".join(f"w{i}" for i in range(200))
    a = _cluster(1, "Capron v. Van Noorden", page="126", text=body)
    b = _cluster(2, "Capron v. Van Noorden", page="127", text="stub")  # no text overlap
    assert dedup.classify_offpage_pair(a, b) is True
    # a different case at the same adjacent distance is NOT merged without text
    c = _cluster(3, "Head v. Providence Insurance", page="127", text="stub")
    assert dedup.classify_offpage_pair(a, c) is False


def test_offpage_adjacent_distinct_series_not_merged():
    # numbered-series cases (The Frances IV vs V) at adjacent pages canonicalize
    # identically (roman numerals stripped) but are DISTINCT: both carry a substantial,
    # differing opinion, so the stub exception must not apply -- text keeps them apart.
    left = _cluster(1, "The Frances", page="358", text=" ".join(f"a{i}" for i in range(200)))
    right = _cluster(2, "The Frances", page="359", text=" ".join(f"b{i}" for i in range(200)))
    assert dedup.classify_offpage_pair(left, right) is False


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


# ---- the human-review ledger --------------------------------------------------


def test_load_dedup_review_parses_pairs(tmp_path):
    path = tmp_path / "review.csv"
    path.write_text(
        "cluster_id,dup_of,us_cite,case_name,disposition,rationale\n"
        "10,20,6 U.S. 1,Foo v. Bar,duplicate,same decision\n"
        "30,40,6 U.S. 2,Baz v. Qux,duplicate,same decision\n"
    )
    assert dedup.load_dedup_review(str(path)) == [(10, 20), (30, 40)]


def test_load_dedup_review_missing_file_is_empty(tmp_path):
    assert dedup.load_dedup_review(str(tmp_path / "absent.csv")) == []


def test_load_dedup_review_rejects_unknown_disposition(tmp_path):
    path = tmp_path / "review.csv"
    path.write_text(
        "cluster_id,dup_of,us_cite,case_name,disposition,rationale\n"
        "10,20,6 U.S. 1,Foo v. Bar,distinct,not supported yet\n"
    )
    with pytest.raises(ValueError, match="unknown disposition"):
        dedup.load_dedup_review(str(path))


def test_load_dedup_review_rejects_self_pair(tmp_path):
    path = tmp_path / "review.csv"
    path.write_text(
        "cluster_id,dup_of,us_cite,case_name,disposition,rationale\n"
        "10,10,6 U.S. 1,Foo v. Bar,duplicate,typo\n"
    )
    with pytest.raises(ValueError, match="paired with itself"):
        dedup.load_dedup_review(str(path))


def test_review_pair_unions_groups_with_human_review_method():
    # two records the machine keeps apart (far pages, low name, no shared text)
    folded = _cluster(1, "The Samuel", page="36", text="short stub of the decision here")
    target = _cluster(
        2,
        "The Samuel, Beach, Claimants",
        scdb="1818-006",
        page="77",
        text=" ".join(f"w{i}" for i in range(200)),
    )
    machine = {r.cluster_id: r for r in dedup.build_dedup_records([folded, target])}
    assert machine[1].dedup_role == "canonical" and machine[2].dedup_role == "canonical"
    records = {r.cluster_id: r for r in dedup.build_dedup_records([folded, target], [(1, 2)])}
    assert records[2].dedup_role == "canonical"  # scdb side wins canonical selection
    assert records[1].dedup_role == "duplicate" and records[1].dup_of == 2
    assert records[1].dup_method == "human_review"


def test_review_pair_direction_does_not_pick_the_canonical():
    # the ledger asserts identity, not precedence: canonical selection still favors
    # the scdb side even when the pair is written the other way around
    scdb_side = _cluster(1, "Alpha v. Beta", scdb="1810-001", page="10", text="a b c d e f g")
    other = _cluster(2, "Gamma v. Delta", page="50", text="h i j k l m n")
    records = {r.cluster_id: r for r in dedup.build_dedup_records([scdb_side, other], [(1, 2)])}
    assert records[1].dedup_role == "canonical"
    assert records[2].dedup_role == "duplicate" and records[2].dup_of == 1
    assert records[2].dup_method == "human_review"


def test_review_pair_overrides_the_scdb_block():
    # a documented erroneous tag (the Houston chimera): the machine's hard block
    # keeps different scdb ids apart; the ledger folds them anyway
    a = _cluster(1, "Houston v. Moore", scdb="1818-014", page="200", text="x y z w v u t")
    b = _cluster(2, "Houston v. Moore", scdb="1818-025", page="433", text="p q r s t u v")
    machine = {r.cluster_id: r for r in dedup.build_dedup_records([a, b])}
    assert machine[1].dedup_role == "canonical" and machine[2].dedup_role == "canonical"
    records = {r.cluster_id: r for r in dedup.build_dedup_records([a, b], [(1, 2)])}
    roles = {r_id: r.dedup_role for r_id, r in records.items()}
    assert sorted(roles.values()) == ["canonical", "duplicate"]
    duplicate = next(r for r in records.values() if r.dedup_role == "duplicate")
    assert duplicate.dup_method == "human_review"


def test_review_pair_unknown_cluster_raises():
    a = _cluster(1, "Alpha v. Beta")
    with pytest.raises(ValueError, match="non-keep-candidate"):
        dedup.build_dedup_records([a], [(1, 999)])


def test_review_pair_cross_volume_raises():
    a = _cluster(1, "Alpha v. Beta", vol=6)
    b = _cluster(2, "Alpha v. Beta", vol=7, text="")
    with pytest.raises(ValueError, match="crosses volumes"):
        dedup.build_dedup_records([a, b], [(1, 2)])


def test_redundant_review_pair_keeps_the_machine_method():
    # a pair the machine already merged: the ledger row is a no-op and the recorded
    # method stays the machine's signal, not human_review
    body = " ".join(f"w{i}" for i in range(200))
    a = _cluster(1, "Ogle v. Lee", scdb="1810-001", text=body)
    b = _cluster(2, "Ogle v. Lee", text=body)
    records = {r.cluster_id: r for r in dedup.build_dedup_records([a, b], [(2, 1)])}
    assert records[2].dedup_role == "duplicate" and records[2].dup_method == "name"


# ---- round trip --------------------------------------------------------------


def test_run_dedup_applies_the_ledger(tmp_path):
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
    conn.executemany(
        "INSERT INTO stg_cluster_scope VALUES (?,?,?,?,?,?)",
        [
            (1, 16, "36", "The Samuel", None, "true"),  # edition page; machine can't reach
            (2, 16, "77", "The Samuel, Beach", "1818-006", "true"),
        ],
    )
    conn.executemany(
        "INSERT INTO stg_opinions (opinion_id, cluster_id, source_plain_text) VALUES (?,?,?)",
        [(10, 1, "stub text"), (20, 2, "much longer opinion text body here")],
    )
    conn.commit()
    conn.close()
    ledger = tmp_path / "dedup_review.csv"
    ledger.write_text(
        "cluster_id,dup_of,us_cite,case_name,disposition,rationale\n"
        "1,2,16 U.S. 36,The Samuel,duplicate,edition pagination\n"
    )

    dedup.run_dedup(db_path, str(ledger))

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT dedup_role, dup_of, dup_method FROM stg_cluster_dedup WHERE cluster_id=1"
    ).fetchone()
    conn.close()
    assert row == ("duplicate", 2, "human_review")


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

    # an absent ledger path: the synthetic DB has none of the real ledger's clusters
    records = dedup.run_dedup(db_path, str(tmp_path / "no_ledger.csv"))
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
    return dedup.build_dedup_records(
        dedup.read_keep_candidates(settings.STAGING_DB_PATH), dedup.load_dedup_review()
    )


def test_automated_passes_never_cross_different_scdb():
    """The load-bearing safety invariant: no AUTOMATED merge points a duplicate at a
    canonical with a different non-null scdb_id (that would fuse two distinct
    decisions). Only a per-row-documented ledger pair may cross, and those crossings
    are enumerated by test_scdb_crossings_are_exactly_the_documented_ledger_rows."""
    records = _real_records()
    role = {r.cluster_id: r for r in records}
    offenders = [
        r.cluster_id
        for r in records
        if r.dedup_role == "duplicate"
        and r.dup_method != "human_review"
        and r.scdb_id
        and role[r.dup_of].scdb_id
        and r.scdb_id != role[r.dup_of].scdb_id
    ]
    assert offenders == [], f"automated merges fused different scdb decisions: {offenders}"


def test_scdb_crossings_are_exactly_the_documented_ledger_rows():
    """Every scdb-crossing merge is human-adjudicated, and there is exactly one: the
    Houston v. Moore chimera (cid 1101126, its 1818-014 tag belongs to Shepherd v.
    Hampton), folded into the real Houston record 85236."""
    records = _real_records()
    role = {r.cluster_id: r for r in records}
    crossings = {
        (r.cluster_id, r.dup_of)
        for r in records
        if r.dedup_role == "duplicate"
        and r.scdb_id
        and role[r.dup_of].scdb_id
        and r.scdb_id != role[r.dup_of].scdb_id
    }
    assert crossings == {(1101126, 85236)}
    assert role[1101126].dup_method == "human_review"


def test_ledger_folds_are_applied():
    """Every dedup_review pair ends up in one group: the cluster_id side resolves to
    the same canonical as the dup_of side, attributed to human_review."""
    records = _real_records()
    role = {r.cluster_id: r for r in records}

    def canonical_of(cluster_id):
        record = role[cluster_id]
        return record.dup_of if record.dedup_role == "duplicate" else cluster_id

    pairs = dedup.load_dedup_review()
    assert pairs, "dedup_review.csv missing or empty"
    for cluster_id, dup_of in pairs:
        assert canonical_of(cluster_id) == canonical_of(dup_of), (cluster_id, dup_of)
        assert role[cluster_id].dedup_role == "duplicate"
        assert role[cluster_id].dup_method == "human_review"


def test_adjudicated_totals():
    """Pin the full-artifact outcome: 916 keep-candidates -> 689 canonical decisions
    (648 corpus vols 2-18 + 41 vol-19 buffer) / 227 labeled duplicates."""
    records = _real_records()
    assert len(records) == 916
    canonical = [r for r in records if r.dedup_role == "canonical"]
    assert len(canonical) == 689
    corpus = [r for r in canonical if r.us_volume and 2 <= r.us_volume <= 18]
    assert len(corpus) == 648


def test_every_duplicate_points_at_a_canonical():
    records = _real_records()
    canonicals = {r.cluster_id for r in records if r.dedup_role == "canonical"}
    dangling = [
        r.cluster_id for r in records if r.dedup_role == "duplicate" and r.dup_of not in canonicals
    ]
    assert dangling == []
