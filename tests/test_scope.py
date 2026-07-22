"""Tests for src.transform.scope — the is_scotus determination.

Three layers, built from what scope is *supposed* to do:
  1. the automated rule (citation + reporter authority + Dallas scdb),
  2. the human-review ledger, which must OVERRIDE the automated rule either way,
  3. outcome tests over the real staging DB (skip when absent) checking scope keeps
     SCOTUS decisions and drops lower-court cases -- anchored on hand-picked landmark
     cases and structural invariants, not a self-fulfilling replay of a fixture.
"""

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


# ---- the automated rule (no ledger) ------------------------------------------


@pytest.mark.parametrize(
    "cluster, expected_verdict, expected_evidence",
    [
        # not reported in the U.S. Reports at all (Meade v. Deputy Marshal)
        (
            _make_cluster(us_cite=None, us_volume=None, us_page=None),
            scope.IsScotus.FALSE,
            "no_us_reports_cite",
        ),
        # out of the 1790-1821 corpus span (pre-SCOTUS Dallas vol 1; 1822-term vol-20 straggler)
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
        # SCOTUS-only reporters (Cranch 5-13, Wheaton 14-19): TRUE by reporter authority
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
        # Dallas (mixed-court): an scdb entry keeps it; without one it is not a SCOTUS case
        (
            _make_cluster(us_volume=3, scdb_id="1799-001", case_name="Calder v. Bull"),
            scope.IsScotus.TRUE,
            "scdb_id",
        ),
        (
            _make_cluster(us_volume=4, us_page="200", case_name="Ordinary v. Case"),
            scope.IsScotus.FALSE,
            "dallas_not_in_scdb",
        ),
    ],
)
def test_automated_rule_verdicts(cluster, expected_verdict, expected_evidence):
    assert scope.determine_is_scotus(cluster) == (expected_verdict, expected_evidence)


def test_dallas_respublica_is_false_with_tell():
    verdict, evidence = scope.determine_is_scotus(
        _make_cluster(us_volume=2, us_page="298", case_name="Respublica v. Oswald")
    )
    assert verdict == scope.IsScotus.FALSE
    assert "respublica/commonwealth" in evidence


# ---- the human-review ledger OVERRIDES the automated rule --------------------


def test_ledger_keep_overrides_an_automated_drop():
    # a Dallas non-scdb decision the rule would drop, kept by human review
    cluster = _make_cluster(
        cluster_id=8403274, us_volume=2, us_page="402", case_name="Oswald v. New-York"
    )
    assert scope.determine_is_scotus(cluster)[0] == scope.IsScotus.FALSE  # rule alone drops it
    assert scope.determine_is_scotus(cluster, {8403274: "keep"}) == (
        scope.IsScotus.TRUE,
        "human_review",
    )


def test_ledger_drop_overrides_an_automated_keep():
    # an scdb cluster the rule would keep, dropped by human review
    cluster = _make_cluster(cluster_id=99, us_volume=6, scdb_id="1805-001")
    assert scope.determine_is_scotus(cluster)[0] == scope.IsScotus.TRUE  # rule alone keeps it
    assert scope.determine_is_scotus(cluster, {99: "drop"}) == (
        scope.IsScotus.FALSE,
        "human_review",
    )


def test_load_scope_review_parses_dispositions(tmp_path):
    path = tmp_path / "review.csv"
    path.write_text(
        "cluster_id,us_cite,case_name,disposition,rationale\n"
        "42,3 U.S. 1,Foo v. Bar,keep,a SCOTUS decision\n"
        "43,3 U.S. 2,Baz v. Qux,drop,not scotus\n"
    )
    assert scope.load_scope_review(str(path)) == {42: "keep", 43: "drop"}


def test_load_scope_review_missing_file_is_empty(tmp_path):
    assert scope.load_scope_review(str(tmp_path / "absent.csv")) == {}


# ---- not_scotus tells (Dallas drop evidence) --------------------------------


@pytest.mark.parametrize(
    "name, volume, page, expected",
    [
        ("Respublica v. Oswald", 2, "298", "respublica/commonwealth"),
        ("Pennsylvania v. Commonwealth", 3, "10", "respublica/commonwealth"),
        ("United States v. Worrall", 2, "384", "us_criminal_caption"),
        ("Den ex dem. Smith's Lessee v. Doe", 4, "1", "lessee_ejectment"),
        ("Georgia v. Brailsford", 2, "402", ""),  # p402 >= 401, the SCOTUS region of 2 Dall.
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


# ---- round trip through the staging table (ledger applied) -------------------


def test_run_scope_applies_the_ledger(tmp_path):
    db = str(tmp_path / "staging.sqlite")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE stg_clusters (cluster_id INTEGER PRIMARY KEY, case_name TEXT, "
        "us_cite TEXT, us_volume INTEGER, us_page TEXT, scdb_id TEXT)"
    )
    conn.executemany(
        "INSERT INTO stg_clusters VALUES (?,?,?,?,?,?)",
        [
            (1, "Marbury v. Madison", "5 U.S. 137", 5, "137", None),  # reporter authority -> keep
            (2, "Respublica v. Oswald", "2 U.S. 298", 2, "298", None),  # Dallas non-scdb -> drop
            (
                3,
                "Ledger v. Dallas",  # synthetic name -- not a real case
                "3 U.S. 400",
                3,
                "400",
                None,
            ),  # Dallas non-scdb, but ledger keep
        ],
    )
    conn.commit()
    conn.close()
    ledger = str(tmp_path / "review.csv")
    with open(ledger, "w") as handle:
        handle.write(
            "cluster_id,us_cite,case_name,disposition,rationale\n"
            "3,3 U.S. 400,Ledger v. Dallas,keep,test\n"
        )
    verdicts = {p.cluster_id: (p.is_scotus, p.evidence) for p in scope.run_scope(db, ledger)}
    assert verdicts[1][0] == "true"
    assert verdicts[2][0] == "false"
    assert verdicts[3] == ("true", "human_review")  # the ledger override reached the table


# ---- outcome tests over the real staging DB ---------------------------------


def _real_scope():
    if not os.path.exists(settings.STAGING_DB_PATH):
        pytest.skip("staging DB missing; run the materialize stage first")
    clusters = scope.read_staging_clusters(settings.STAGING_DB_PATH)
    review = scope.load_scope_review()
    return clusters, {p.cluster_id: p for p in scope.build_scope_proposals(clusters, review)}


def _verdicts_for(clusters, by_id, name_substring, us_cite):
    hits = [
        c
        for c in clusters
        if name_substring.lower() in (c["case_name"] or "").lower() and c.get("us_cite") == us_cite
    ]
    if not hits:
        pytest.skip(f"{name_substring} {us_cite} not in this staging build")
    return {by_id[c["cluster_id"]].is_scotus for c in hits}


@pytest.mark.parametrize(
    "name, cite",
    [
        ("Chisholm", "2 U.S. 419"),  # Dallas landmark, scdb
        ("Calder v. Bull", "3 U.S. 386"),  # Dallas, scdb
        ("Marbury", "5 U.S. 137"),  # Cranch, reporter authority
        ("Hazlehurst", "4 U.S. 6"),  # Dallas, kept only via the ledger
        ("Oswald", "2 U.S. 402"),  # Dallas, kept only via the ledger (third Oswald)
        ("Hallowell", "3 U.S. 410"),  # Dallas, kept only via the ledger (admission transfer)
    ],
)
def test_landmark_decisions_are_kept(name, cite):
    """Real SCOTUS decisions -- across scdb, reporter authority, and the ledger -- are kept."""
    clusters, by_id = _real_scope()
    assert _verdicts_for(clusters, by_id, name, cite) == {"true"}, f"{name} {cite} not kept"


def test_a_known_circuit_case_is_dropped():
    """United States v. Worrall (2 U.S. 384) is a Circuit-PA criminal case, not SCOTUS."""
    clusters, by_id = _real_scope()
    assert _verdicts_for(clusters, by_id, "United States v. Worrall", "2 U.S. 384") == {"false"}


def test_reporter_only_volumes_are_all_true():
    """Every in-scope cluster from a SCOTUS-only reporter (vols 5-19) is kept."""
    clusters, by_id = _real_scope()
    stragglers = [
        c["cluster_id"]
        for c in clusters
        if c.get("us_volume")
        and 5 <= c["us_volume"] <= 19
        and by_id[c["cluster_id"]].is_scotus != "true"
    ]
    assert stragglers == []


def test_dallas_keeps_are_only_scdb_or_ledger():
    """No Dallas cluster is kept unless it has an scdb_id or the ledger keeps it -- so no
    Pennsylvania-state / circuit case (which have neither) can slip into the corpus."""
    clusters, by_id = _real_scope()
    review = scope.load_scope_review()
    kept_without_authority = [
        c["cluster_id"]
        for c in clusters
        if c.get("us_volume") in (2, 3, 4)
        and by_id[c["cluster_id"]].is_scotus == "true"
        and not (c.get("scdb_id") or review.get(c["cluster_id"]) == "keep")
    ]
    assert kept_without_authority == [], (
        f"Dallas kept without scdb/ledger: {kept_without_authority}"
    )
