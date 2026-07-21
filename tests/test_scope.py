"""Tests for src.transform.scope — the is_scotus determination.

Two layers: pure counterfactual unit tests for the predicate, and data-quality
tests that run the determination over the real staging DB and reconcile it
against the human answer key (dataset/review_dispositions.csv). The data-quality
tests skip when staging is absent.
"""

import csv
import os
import sqlite3

import pytest

from config import settings
from src.transform import scope


def _make_cluster(**over):
    base = {
        "cluster_id": 1,
        "case_name": "Smith v. Jones",
        "us_cite": "9 U.S. 137",
        "us_volume": 9,
        "us_page": "137",
        "scdb_id": None,
    }
    base.update(over)
    return base


# ---- determine_is_scotus counterfactuals -------------------------------------


@pytest.mark.parametrize(
    "cluster, expected_verdict, expected_evidence",
    [
        # no U.S. Reports cite at all (Meade v. Deputy Marshal)
        (
            _make_cluster(us_cite=None, us_volume=None, us_page=None),
            scope.IsScotus.FALSE,
            "no_us_reports_cite",
        ),
        # out-of-scope volumes: pre-SCOTUS Dallas vol 1, and the 1822-term vol-20 straggler
        (
            _make_cluster(us_volume=1, us_cite="1 U.S. 1"),
            scope.IsScotus.FALSE,
            "out_of_scope_volume",
        ),
        (
            _make_cluster(us_volume=20, us_cite="20 U.S. 1"),
            scope.IsScotus.FALSE,
            "out_of_scope_volume",
        ),
        # SCOTUS-only reporter (Cranch/Wheaton): TRUE by reporter authority, scdb or not
        (
            _make_cluster(us_volume=6, scdb_id="1805-001"),
            scope.IsScotus.TRUE,
            "scotus_reporter+scdb",
        ),
        (_make_cluster(us_volume=6), scope.IsScotus.TRUE, "scotus_only_reporter"),
        (
            _make_cluster(us_volume=19, scdb_id="1821-001"),
            scope.IsScotus.TRUE,
            "scotus_reporter+scdb",
        ),
        # Dallas: an scdb entry keeps it; without one it is not a SCOTUS case
        (
            _make_cluster(us_volume=3, scdb_id="1799-001", case_name="Calder v. Bull"),
            scope.IsScotus.TRUE,
            "scdb_id",
        ),
        (
            _make_cluster(us_volume=4, us_page="200", case_name="Ordinary Case v. Someone"),
            scope.IsScotus.FALSE,
            "dallas_not_in_scdb",
        ),
    ],
)
def test_determine_is_scotus_verdicts(cluster, expected_verdict, expected_evidence):
    verdict, evidence = scope.determine_is_scotus(cluster)
    assert verdict == expected_verdict
    assert evidence == expected_evidence


def test_dallas_respublica_is_false_with_tell():
    verdict, evidence = scope.determine_is_scotus(
        _make_cluster(us_volume=2, us_page="298", case_name="Respublica v. Oswald", scdb_id=None)
    )
    assert verdict == scope.IsScotus.FALSE
    assert evidence.startswith("dallas_not_in_scdb:")
    assert "respublica/commonwealth" in evidence


def test_curated_exception_is_true():
    # Hazlehurst v. United States (4 U.S. 6): genuine, texted, reference-listed, and
    # its only cluster carries no scdb_id -- the one human-verified override.
    verdict, evidence = scope.determine_is_scotus(
        _make_cluster(
            cluster_id=6725725,
            case_name="Hazlehurst v. United States",
            us_cite="4 U.S. 6",
            us_volume=4,
            us_page="6",
        )
    )
    assert verdict == scope.IsScotus.TRUE
    assert evidence == "curated_exception"


# ---- collect_not_scotus_tells --------------------------------------------------------


@pytest.mark.parametrize(
    "name, volume, page, expected",
    [
        ("Respublica v. Oswald", 2, "298", "respublica/commonwealth"),
        ("Pennsylvania v. Commonwealth", 3, "10", "respublica/commonwealth"),
        ("United States v. Worrall", 2, "384", "us_criminal_caption"),
        ("Den ex dem. Smith's Lessee v. Doe", 4, "1", "lessee_ejectment"),
        ("Georgia v. Brailsford", 2, "402", ""),  # p402 >= 401, genuine SCOTUS region
    ],
)
def test_collect_not_scotus_tells(name, volume, page, expected):
    tells = scope.collect_not_scotus_tells(
        {"case_name": name, "us_volume": volume, "us_page": page}
    )
    if expected:
        assert expected in tells
    else:
        assert tells == ""


def test_page_before_scotus_start_tell():
    tells = scope.collect_not_scotus_tells(
        {"case_name": "Some PA Case", "us_volume": 2, "us_page": "384"}
    )
    assert "page_before_scotus_start" in tells


# ---- round trip through the staging table ------------------------------------


def _build_staging(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE stg_clusters (cluster_id INTEGER PRIMARY KEY, case_name TEXT, "
        "us_cite TEXT, us_volume INTEGER, us_page TEXT, scdb_id TEXT)"
    )
    conn.executemany(
        "INSERT INTO stg_clusters VALUES (?,?,?,?,?,?)",
        [
            (1, "Marbury v. Madison", "5 U.S. 137", 5, "137", None),  # reporter authority
            (2, "Respublica v. Oswald", "2 U.S. 298", 2, "298", None),  # Dallas not-scotus
            (3, "Calder v. Bull", "3 U.S. 386", 3, "386", "1798-001"),  # Dallas scdb keep
        ],
    )
    conn.commit()
    conn.close()


def test_run_scope_writes_table(tmp_path):
    db_path = str(tmp_path / "staging.sqlite")
    _build_staging(db_path)
    proposals = scope.run_scope(db_path)
    verdicts = {p.cluster_id: (p.is_scotus, p.proposed_disposition) for p in proposals}
    assert verdicts[1] == ("true", "keep")
    assert verdicts[2] == ("false", "drop")
    assert verdicts[3] == ("true", "keep")

    conn = sqlite3.connect(db_path)
    stored = dict(conn.execute("SELECT cluster_id, is_scotus FROM stg_cluster_scope").fetchall())
    conn.close()
    assert stored == {1: "true", 2: "false", 3: "true"}


def test_scope_table_does_not_block_staging_rebuild(tmp_path):
    """The derived scope table must never block materialize's clean rebuild.

    materialize drops and recreates the base tables with foreign_keys=ON; a FOREIGN
    KEY from stg_cluster_scope to stg_clusters would make that drop fail (observed on
    the real staging DB). Stages own and rebuild their artifacts independently.
    """
    db_path = str(tmp_path / "staging.sqlite")
    _build_staging(db_path)
    scope.run_scope(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DROP TABLE stg_clusters")  # must not raise despite stg_cluster_scope
    conn.close()


# ---- data-quality: reconcile against the human answer key --------------------


def _build_real_scope_proposals():
    """Score the real staging DB in memory (no write); skip if it is absent."""
    if not os.path.exists(settings.STAGING_DB_PATH):
        pytest.skip("staging DB missing; run the materialize stage first")
    clusters = scope.read_staging_clusters(settings.STAGING_DB_PATH)
    return {p.cluster_id: p for p in scope.build_scope_proposals(clusters)}


def _load_answer_key():
    with open(settings.REVIEW_DISPOSITIONS_CSV, newline="") as handle:
        return {
            int(r["cluster_id"]): r["disposition"]
            for r in csv.DictReader(handle)
            if r["cluster_id"]
        }


def test_no_not_scotus_case_is_kept():
    """No case the reviewers marked DROP-not-scotus may be classified SCOTUS TRUE."""
    by_id = _build_real_scope_proposals()
    kept = [
        cid
        for cid, disposition in _load_answer_key().items()
        if disposition == "DROP-not-scotus" and by_id.get(cid) and by_id[cid].is_scotus == "true"
    ]
    assert kept == [], f"not-scotus cases wrongly kept: {kept}"


def test_reporter_only_volumes_are_all_true():
    """Every in-scope cluster from a SCOTUS-only reporter (vols 5-19) is TRUE."""
    by_id = _build_real_scope_proposals()
    stragglers = [
        p
        for p in by_id.values()
        if p.us_volume and 5 <= p.us_volume <= 19 and p.is_scotus != "true"
    ]
    assert stragglers == []


def test_dallas_drops_were_all_human_reviewed():
    """Every Dallas cluster scope drops was examined by the human review.

    The Dallas scdb rule is empirical -- it is safe only because the review covered
    the whole non-scdb set. If a future mirror adds a Dallas cluster the review never
    saw, this fails rather than silently dropping it.
    """
    by_id = _build_real_scope_proposals()
    reviewed = set(_load_answer_key())
    dropped = {
        cid for cid, p in by_id.items() if p.us_volume in (2, 3, 4) and p.is_scotus == "false"
    }
    assert dropped - reviewed == set(), f"dropped but never reviewed: {dropped - reviewed}"


def test_curated_exception_applies_on_real_data():
    """Hazlehurst (cid 6725725) is kept via the curated exception, not scdb."""
    by_id = _build_real_scope_proposals()
    hazlehurst = by_id.get(6725725)
    if hazlehurst is None:
        pytest.skip("Hazlehurst cluster not present in this staging build")
    assert hazlehurst.is_scotus == "true"
    assert hazlehurst.evidence == "curated_exception"
    assert not hazlehurst.scdb_id
