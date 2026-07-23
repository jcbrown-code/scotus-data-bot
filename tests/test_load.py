"""Tests for src.load — build the shipped database from staging.

Unit tests run against a small synthetic staging DB (offline, deterministic) and
verify the loader's contract: everything ships labeled, text only on the corpus
subset, structure as offset spans, the view exposes exactly the corpus, and a
missing upstream stage fails loudly. The data-quality suite (the ``db`` fixture,
skipped when staging is absent) pins the full-artifact outcome.
"""

import json
import sqlite3

import pytest

from src import load


def _make_staging(tmp_path):
    """A minimal but complete staging DB: one corpus decision with two opinions
    (one with page breaks + OCR flags), its labeled duplicate, one scope-dropped
    cluster, and one vol-19 buffer cluster."""
    path = str(tmp_path / "staging.sqlite")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE stg_clusters (cluster_id INTEGER PRIMARY KEY, case_name TEXT,
          case_name_full TEXT, date_filed TEXT, us_volume INTEGER, us_page TEXT,
          us_cite TEXT, scdb_id TEXT, source TEXT, citation_count INTEGER,
          precedential_status TEXT, n_opinions INTEGER, citations_json TEXT,
          sub_opinion_ids_json TEXT);
        CREATE TABLE stg_opinions (opinion_id INTEGER PRIMARY KEY, cluster_id INTEGER,
          type TEXT, author TEXT, is_ocr_extracted INTEGER, ordering_key INTEGER,
          source_html_lawbox TEXT, source_xml_harvard TEXT, source_html TEXT,
          source_html_columbia TEXT, source_html_anon_2020 TEXT,
          source_html_with_citations TEXT, source_plain_text TEXT);
        CREATE TABLE stg_cluster_scope (cluster_id INTEGER PRIMARY KEY,
          us_volume INTEGER, us_page TEXT, case_name TEXT, scdb_id TEXT,
          is_scotus TEXT, evidence TEXT, proposed_disposition TEXT);
        CREATE TABLE stg_cluster_dedup (cluster_id INTEGER PRIMARY KEY,
          us_volume INTEGER, us_page TEXT, case_name TEXT, scdb_id TEXT,
          dedup_role TEXT, dup_of INTEGER, dup_method TEXT);
        CREATE TABLE stg_opinion_source (opinion_id INTEGER PRIMARY KEY,
          cluster_id INTEGER, type TEXT, chosen_source TEXT, is_ocr_dirty INTEGER);
        CREATE TABLE stg_opinion_clean (opinion_id INTEGER PRIMARY KEY,
          cluster_id INTEGER, clean_text TEXT, clean_version INTEGER, ocr_suspect TEXT);
        CREATE TABLE stg_page_break (opinion_id INTEGER, ordinal INTEGER,
          page_label TEXT, char_offset INTEGER, anchor TEXT);
        CREATE TABLE stg_meta (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    citations = json.dumps(
        [
            {"reporter": "U.S.", "volume": "5", "page": "137", "type": 1},
            {"reporter": "Cranch", "volume": "1", "page": "137", "type": 5},
            {"reporter": "U.S.", "volume": "5", "page": "137", "type": 1},  # exact dupe
        ]
    )
    conn.executemany(
        "INSERT INTO stg_clusters VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                1,
                "Marbury v. Madison",
                None,
                "1803-02-24",
                5,
                "137",
                "5 U.S. 137",
                "1803-005",
                "LR",
                10,
                "Published",
                2,
                citations,
                "[10, 11]",
            ),
            (
                2,
                "Marbury v. Madison",
                None,
                "1803-02-15",
                5,
                "137",
                "5 U.S. 137",
                None,
                "U",
                0,
                "Published",
                1,
                "[]",
                "[12]",
            ),
            (
                3,
                "Respublica v. Passmore",
                None,
                "1802-12-01",
                4,
                "9",
                "4 U.S. 9",
                None,
                "L",
                0,
                "Published",
                1,
                "[]",
                "[13]",
            ),
            (
                4,
                "Buffer v. Case",
                None,
                "1821-02-01",
                19,
                "1",
                "19 U.S. 1",
                "1821-001",
                "R",
                0,
                "Published",
                1,
                "[]",
                "[14]",
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO stg_opinions (opinion_id, cluster_id, type, author, "
        "is_ocr_extracted, ordering_key, source_html_lawbox) VALUES (?,?,?,?,?,?,?)",
        [
            (10, 1, "020lead", "Marshall", 0, 1, "<p>lead text</p>"),
            (11, 1, "030concurrence", None, 0, 2, "<p>concurrence</p>"),
            (12, 2, "010combined", None, 1, None, "<p>dup text</p>"),
            (13, 3, "010combined", None, 0, None, "<p>state case</p>"),
            (14, 4, "010combined", None, 0, None, "<p>buffer text</p>"),
        ],
    )
    conn.executemany(
        "INSERT INTO stg_cluster_scope VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                1,
                5,
                "137",
                "Marbury v. Madison",
                "1803-005",
                "true",
                "scotus_reporter+scdb",
                "keep",
            ),
            (2, 5, "137", "Marbury v. Madison", None, "true", "scotus_only_reporter", "keep"),
            (3, 4, "9", "Respublica v. Passmore", None, "false", "dallas_not_in_scdb", "drop"),
            (4, 19, "1", "Buffer v. Case", "1821-001", "true", "scotus_reporter+scdb", "keep"),
        ],
    )
    conn.executemany(
        "INSERT INTO stg_cluster_dedup VALUES (?,?,?,?,?,?,?,?)",
        [
            (1, 5, "137", "Marbury v. Madison", "1803-005", "canonical", None, None),
            (2, 5, "137", "Marbury v. Madison", None, "duplicate", 1, "name"),
            (4, 19, "1", "Buffer v. Case", "1821-001", "canonical", None, None),
        ],
    )
    # reselect + clean cover the corpus opinions only (cluster 1; not the duplicate,
    # not the dropped cluster; the vol-19 buffer is corpus-excluded by the view, but
    # its text pipeline runs — mirror that: cluster 4's opinion is cleaned too)
    conn.executemany(
        "INSERT INTO stg_opinion_source VALUES (?,?,?,?,?)",
        [
            (10, 1, "020lead", "source_html_lawbox", 0),
            (11, 1, "030concurrence", "source_html_lawbox", 0),
            (14, 4, "010combined", "source_html_lawbox", 0),
        ],
    )
    suspect = json.dumps(
        {"count": 2, "hits": [{"offset": 0, "token": "tbe"}, {"offset": 9, "token": "■"}]}
    )
    conn.executemany(
        "INSERT INTO stg_opinion_clean VALUES (?,?,?,?,?)",
        [
            (10, 1, "tbe lead ■ text of the opinion", 2, suspect),
            (11, 1, "concurrence text", 2, None),
            (14, 4, "buffer text", 2, None),
        ],
    )
    conn.execute(
        "INSERT INTO stg_page_break VALUES (10, 1, '138', 4, 'lead')",
    )
    conn.executemany(
        "INSERT INTO stg_meta VALUES (?,?)",
        [("etl_job_id", "abc123@2026-01-01T00:00:00+00:00"), ("n_clusters", "4")],
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def built(tmp_path):
    staging = _make_staging(tmp_path)
    db_path = str(tmp_path / "scotus.sqlite")
    counts = load.build_db(staging, db_path)
    conn = sqlite3.connect(db_path)
    yield conn, counts
    conn.close()


# ---- unit: the loader's contract ---------------------------------------------


def test_everything_ships_labeled(built):
    conn, counts = built
    assert counts["n_clusters"] == 4 and counts["n_opinions"] == 5
    labels = {
        row[0]: row[1:]
        for row in conn.execute("SELECT cluster_id, is_scotus, dedup_role, dup_of FROM clusters")
    }
    assert labels[1] == ("true", "canonical", None)
    assert labels[2] == ("true", "duplicate", 1)
    assert labels[3] == ("false", None, None)  # scope-dropped: no dedup row, shipped anyway
    assert labels[4] == ("true", "canonical", None)


def test_view_exposes_exactly_the_corpus(built):
    conn, counts = built
    decisions = [r[0] for r in conn.execute("SELECT cluster_id FROM scotus_decisions")]
    assert decisions == [1]  # not the duplicate, not the dropped, not the vol-19 buffer
    assert counts["n_decisions"] == 1


def test_text_only_on_clean_opinions(built):
    conn, _ = built
    populated = {
        row[0]: row[1] is not None
        for row in conn.execute("SELECT opinion_id, clean_text FROM opinions")
    }
    assert populated == {10: True, 11: True, 12: False, 13: False, 14: True}
    # missing = NULL, never '' (the staging convention survives the load)
    assert (
        conn.execute(
            "SELECT count(*) FROM opinions WHERE clean_text = '' OR chosen_source = ''"
        ).fetchone()[0]
        == 0
    )


def test_structure_ships_as_offset_spans(built):
    conn, counts = built
    assert conn.execute(
        "SELECT ordinal, page_label, char_offset, anchor FROM page_breaks WHERE opinion_id = 10"
    ).fetchall() == [(1, "138", 4, "lead")]
    assert conn.execute(
        "SELECT ordinal, char_offset, token FROM ocr_suspects WHERE opinion_id = 10 "
        "ORDER BY ordinal"
    ).fetchall() == [(1, 0, "tbe"), (2, 9, "■")]
    assert counts["n_ocr_suspects"] == 2


def test_citations_parsed_and_exact_dupes_collapsed(built):
    conn, counts = built
    rows = conn.execute(
        "SELECT reporter, volume, page, type FROM citations WHERE cluster_id = 1 ORDER BY reporter"
    ).fetchall()
    assert rows == [("Cranch", "1", "137", 5), ("U.S.", "5", "137", 1)]
    assert counts["n_citation_dupes_dropped"] == 1


def test_fts_indexes_corpus_text_only(built):
    conn, _ = built
    hits = [
        r[0]
        for r in conn.execute(
            "SELECT rowid FROM opinions_fts WHERE opinions_fts MATCH 'concurrence'"
        )
    ]
    assert hits == [11]
    # the duplicate's and dropped cluster's raw text never entered the index
    assert (
        conn.execute(
            "SELECT count(*) FROM opinions_fts WHERE opinions_fts MATCH 'dup OR state'"
        ).fetchone()[0]
        == 0
    )


def test_meta_carries_staging_lineage(built):
    conn, _ = built
    meta = dict(conn.execute("SELECT key, value FROM meta"))
    assert meta["staging_etl_job_id"] == "abc123@2026-01-01T00:00:00+00:00"
    assert meta["pipeline_version"] == "2.0"
    assert meta["n_decisions"] == "1"


def test_missing_stage_fails_loudly(tmp_path):
    staging = _make_staging(tmp_path)
    conn = sqlite3.connect(staging)
    conn.execute("DROP TABLE stg_opinion_clean")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="stg_opinion_clean.*--stage clean"):
        load.build_db(staging, str(tmp_path / "out.sqlite"))


def test_rebuild_is_blank_slate(tmp_path):
    staging = _make_staging(tmp_path)
    db_path = str(tmp_path / "scotus.sqlite")
    load.build_db(staging, db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE stray (x)")
    conn.commit()
    conn.close()
    load.build_db(staging, db_path)  # second build starts fresh
    conn = sqlite3.connect(db_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "stray" not in tables


# ---- data-quality against the real staging DB (the db fixture) ---------------


def _one(db, sql):
    return db.execute(sql).fetchone()[0]


def test_full_artifact_counts(db):
    assert _one(db, "SELECT count(*) FROM clusters") == 1120
    assert _one(db, "SELECT count(*) FROM opinions") == 1160
    assert _one(db, "SELECT count(*) FROM scotus_decisions") == 648
    assert _one(db, "SELECT count(*) FROM opinions WHERE clean_text IS NOT NULL") == 674
    assert _one(db, "SELECT count(*) FROM clusters WHERE dup_method='human_review'") == 20


def test_view_matches_the_committed_validate_report(db):
    import csv

    from config import settings

    with open(settings.VALIDATE_REPORT_CSV, newline="") as handle:
        expected = {int(row["volume"]): int(row["n_keep"]) for row in csv.DictReader(handle)}
    actual = dict(
        db.execute("SELECT us_volume, count(*) FROM scotus_decisions GROUP BY us_volume")
    )
    assert actual == expected


def test_referential_integrity(db):
    assert (
        _one(
            db,
            "SELECT count(*) FROM opinions o LEFT JOIN clusters c USING (cluster_id) "
            "WHERE c.cluster_id IS NULL",
        )
        == 0
    )
    assert (
        _one(
            db,
            "SELECT count(*) FROM clusters WHERE dup_of IS NOT NULL AND dup_of NOT IN "
            "(SELECT cluster_id FROM clusters)",
        )
        == 0
    )
    assert (
        _one(
            db,
            "SELECT count(*) FROM citations ci LEFT JOIN clusters c USING (cluster_id) "
            "WHERE c.cluster_id IS NULL",
        )
        == 0
    )
    assert (
        _one(
            db,
            "SELECT count(*) FROM page_breaks pb LEFT JOIN opinions o USING (opinion_id) "
            "WHERE o.opinion_id IS NULL",
        )
        == 0
    )
    assert (
        _one(
            db,
            "SELECT count(*) FROM ocr_suspects s LEFT JOIN opinions o USING (opinion_id) "
            "WHERE o.opinion_id IS NULL",
        )
        == 0
    )


def test_every_decision_has_text(db):
    assert (
        _one(
            db,
            "SELECT count(*) FROM scotus_decisions d WHERE NOT EXISTS ("
            "SELECT 1 FROM opinions o WHERE o.cluster_id = d.cluster_id "
            "AND o.clean_text IS NOT NULL)",
        )
        == 0
    )


def test_offset_spans_index_into_clean_text(db):
    assert (
        _one(
            db,
            "SELECT count(*) FROM page_breaks pb JOIN opinions o USING (opinion_id) "
            "WHERE pb.char_offset < 0 OR pb.char_offset > length(o.clean_text)",
        )
        == 0
    )
    assert (
        _one(
            db,
            "SELECT count(*) FROM ocr_suspects s JOIN opinions o USING (opinion_id) "
            "WHERE s.char_offset < 0 OR s.char_offset >= length(o.clean_text)",
        )
        == 0
    )


def test_clean_version_is_uniform(db):
    assert (
        _one(
            db,
            "SELECT count(DISTINCT clean_version) FROM opinions WHERE clean_version IS NOT NULL",
        )
        == 1
    )


def test_no_empty_string_sentinels(db):
    offenders = []
    for table in ("clusters", "opinions"):
        for _cid, name, column_type, *_rest in db.execute(f"PRAGMA table_info({table})"):
            if column_type == "TEXT":
                count = _one(db, f"SELECT count(*) FROM {table} WHERE {name} = ''")  # noqa: S608
                if count:
                    offenders.append(f"{table}.{name}: {count}")
    assert offenders == []


def test_landmark_decisions_present(db):
    # "ulloch" tolerates the archaic caption spelling M'culloch
    for fragment in ("Marbury", "ulloch", "Hallowell", "Hazlehurst", "Oswald"):
        assert (
            _one(
                db,
                f"SELECT count(*) FROM scotus_decisions WHERE case_name LIKE '%{fragment}%'",  # noqa: S608
            )
            >= 1
        ), f"missing {fragment}"


def test_houston_chimera_resolved(db):
    role, dup_of, method = db.execute(
        "SELECT dedup_role, dup_of, dup_method FROM clusters WHERE cluster_id = 1101126"
    ).fetchone()
    assert (role, dup_of, method) == ("duplicate", 85236, "human_review")


def test_fts_finds_mcculloch(db):
    names = [
        r[0]
        for r in db.execute(
            "SELECT c.case_name FROM opinions_fts f "
            "JOIN opinions o ON o.opinion_id = f.rowid "
            "JOIN clusters c USING (cluster_id) "
            "WHERE opinions_fts MATCH 'necessary proper'"
        )
    ]
    assert any("ulloch" in name for name in names)  # M'culloch v. State of Maryland
