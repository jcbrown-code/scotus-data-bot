"""Tests for src.transform.validate — reconcile the KEEP set against the reference.

Pure unit tests for the matcher, plus data-quality tests over the real staging DB
(skipped when absent) that pin the reconciliation outcomes the report is meant to
confirm.
"""

import csv
import os
import sqlite3

import pytest

from config import settings
from src.transform import validate


def _ref(vol, page, name, year="1810"):
    return validate.ReferenceCase(vol, page, name, year)


def _keep(cid, vol, page, name, date="1810-01-01"):
    return validate.KeepCluster(cid, vol, page, name, date)


# ---- matcher primitives ------------------------------------------------------


def test_canonicalize_drops_descriptors_and_roman():
    # roman repeat-suffix and descriptor words fall away; distinctive parties remain
    assert validate.canonicalize_name("Oswald v. New York II") == validate.canonicalize_name(
        "Oswald v. New York"
    )


def test_score_similarity_tolerates_spelling():
    assert (
        validate.score_name_similarity("Van Staphorst v. Maryland", "Vanstophorst v. Maryland")
        > 0.75
    )


# ---- match_volume_to_reference ----------------------------------------------


def test_match_reports_matched_missing_and_extra():
    reference = [_ref(5, "137", "Marbury v. Madison"), _ref(5, "1", "Talbot v. Seeman")]
    clusters = [
        _keep(1, 5, "137", "Marbury v. Madison"),  # matches
        _keep(2, 5, "999", "Nobody v. Nothing"),  # extra anomaly
    ]
    result = validate.match_volume_to_reference(reference, clusters)
    assert result.n_matched == 1
    assert [c.name for c in result.missing] == ["Talbot v. Seeman"]
    kinds = {cluster.cluster_id: kind for cluster, kind in result.extras}
    assert kinds == {2: "anomaly"}


def test_page_primary_disambiguates_two_cases_on_one_page():
    reference = [_ref(5, "1", "Alpha v. Beta"), _ref(5, "1", "Gamma v. Delta")]
    clusters = [_keep(1, 5, "1", "Gamma v. Delta"), _keep(2, 5, "1", "Alpha v. Beta")]
    result = validate.match_volume_to_reference(reference, clusters)
    assert result.n_matched == 2 and not result.missing and not result.extras


# ---- load_reference ----------------------------------------------------------


def test_load_reference_keys_on_us_reports_volume(tmp_path):
    path = tmp_path / "ref.csv"
    path.write_text(
        "reporter,rep_vol,us_vol,name,page,year\n"
        "Cranch,5,9,Marbury v. Madison,137,1803\n"  # rep_vol=5 is the U.S. volume
    )
    by_volume = validate.load_reference(str(path))
    assert set(by_volume) == {5} and by_volume[5][0].name == "Marbury v. Madison"


# ---- read_canonical_keep filters to canonical, corpus span -------------------


def test_read_canonical_keep_excludes_duplicates_and_vol19(tmp_path):
    db = str(tmp_path / "s.sqlite")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE stg_clusters (cluster_id INTEGER PRIMARY KEY, date_filed TEXT)")
    conn.execute(
        "CREATE TABLE stg_cluster_dedup (cluster_id INTEGER, us_volume INTEGER, us_page TEXT, "
        "case_name TEXT, dedup_role TEXT)"
    )
    conn.executemany(
        "INSERT INTO stg_clusters VALUES (?,?)",
        [(1, "1810-01-01"), (2, "1810-01-01"), (3, "1821-01-01")],
    )
    conn.executemany(
        "INSERT INTO stg_cluster_dedup VALUES (?,?,?,?,?)",
        [
            (1, 5, "1", "Kept v. Case", "canonical"),
            (2, 5, "1", "Dup v. Case", "duplicate"),  # excluded (not canonical)
            (3, 19, "1", "Buffer v. Case", "canonical"),  # excluded (vol 19 buffer)
        ],
    )
    conn.commit()
    conn.close()
    keep = validate.read_canonical_keep(db)
    assert list(keep) == [5]
    assert [c.cluster_id for c in keep[5]] == [1]


# ---- data-quality against real staging ---------------------------------------


def _real_results():
    if not os.path.exists(settings.STAGING_DB_PATH):
        pytest.skip("staging DB missing; run materialize + scope + dedup first")
    conn = sqlite3.connect(settings.STAGING_DB_PATH)
    has_dedup = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='stg_cluster_dedup'"
    ).fetchone()[0]
    conn.close()
    if not has_dedup:
        pytest.skip("stg_cluster_dedup missing; run the dedup stage first")
    return validate.reconcile(settings.STAGING_DB_PATH, settings.CASE_NAME_REFERENCE_CSV)


def test_corpus_is_volumes_2_to_18_only():
    """The final corpus never includes vol 19 (buffer) — a regression guard on scope."""
    results = _real_results()
    assert {r.volume for r in results} == set(range(2, 19))


def test_volume_4_reconciles_fully():
    """Volume 4 must reconcile 14/14 against the reference, Hazlehurst included.

    An scdb-only Dallas rule silently drops Hazlehurst (CourtListener never assigned
    it an scdb_id); the scope_review ledger keeps it, and this pins that outcome."""
    result = next(r for r in _real_results() if r.volume == 4)
    assert result.n_reference == 14
    assert result.missing == [], f"vol 4 missing: {[c.name for c in result.missing]}"


def test_report_csv_matches_reconciliation():
    """The committed report CSV, if present, agrees with a fresh reconciliation."""
    if not os.path.exists(settings.VALIDATE_REPORT_CSV):
        pytest.skip("no committed report yet")
    results = {r.volume: r for r in _real_results()}
    with open(settings.VALIDATE_REPORT_CSV, newline="") as handle:
        for row in csv.DictReader(handle):
            r = results[int(row["volume"])]
            assert int(row["n_keep"]) == r.n_keep and int(row["n_reference"]) == r.n_reference
