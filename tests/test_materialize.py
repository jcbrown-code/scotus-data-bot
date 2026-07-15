"""Tests for src.materialize — offline, deterministic; tmp raw-mirror fixtures + SQLite staging.

Covers the materialize_hierarchy contract: non-lossy count conservation, referential integrity,
coverage + n_opinions cascade, source-field retention (anti-best_text), normalization correctness,
determinism, decision-independence, round-trip, citation-timestamp stripping, xml_scan exclusion.
"""

import json
import sqlite3

import pytest

from src import materialize

CL = "https://www.courtlistener.com/api/rest/v4"


def _raw_cluster(cluster_id, sub_opinion_ids=(), citations=None, **over):
    rec = {
        "id": cluster_id,
        "case_name": f"Case {cluster_id}",
        "case_name_full": "",
        "date_filed": "1815-01-01",
        "citations": citations
        if citations is not None
        else [{"reporter": "U.S.", "volume": "12", "page": "100"}],
        "scdb_id": "",
        "source": "L",
        "citation_count": 0,
        "precedential_status": "Published",
        "sub_opinions": [f"{CL}/opinions/{i}/" for i in sub_opinion_ids],
    }
    rec.update(over)
    return rec


def _raw_opinion(opinion_id, cluster_id, sources=None, **over):
    rec = {
        "id": opinion_id,
        "cluster_id": cluster_id,
        "cluster": f"{CL}/clusters/{cluster_id}/",
        "type": "010combined",
        "author_str": "",
        "extracted_by_ocr": False,
        "ordering_key": None,
    }
    rec.update(sources or {})
    rec.update(over)
    return rec


def _write_mirror(tmp_path, clusters, opinions):
    cdir = tmp_path / "clusters"
    odir = tmp_path / "opinions"
    cdir.mkdir(parents=True)
    odir.mkdir(parents=True)
    for cluster in clusters:
        (cdir / f"{cluster['id']}.json").write_text(json.dumps(cluster))
    for opinion in opinions:
        (odir / f"{opinion['id']}.json").write_text(json.dumps(opinion))
    return str(cdir), str(odir)


def _run(tmp_path, clusters, opinions):
    cdir, odir = _write_mirror(tmp_path, clusters, opinions)
    db = str(tmp_path / "scotus-staging.sqlite")
    stg_clusters, stg_opinions = materialize.materialize_hierarchy(cdir, odir, db)
    return stg_clusters, stg_opinions, db


# ---- 1. count conservation -------------------------------------------------


def test_count_conservation(tmp_path):
    clusters = [_raw_cluster(1, [10]), _raw_cluster(2, [20, 21])]
    opinions = [_raw_opinion(10, 1), _raw_opinion(20, 2), _raw_opinion(21, 2)]
    stg_clusters, stg_opinions, db = _run(tmp_path, clusters, opinions)
    assert len(stg_clusters) == 2
    assert len(stg_opinions) == 3
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT count(*) FROM stg_clusters").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM stg_opinions").fetchone()[0] == 3
    conn.close()


# ---- 2. referential integrity ----------------------------------------------


def test_orphan_opinion_raises(tmp_path):
    clusters = [_raw_cluster(1, [10])]
    opinions = [_raw_opinion(10, 1), _raw_opinion(99, 777)]  # 777 has no cluster
    with pytest.raises(RuntimeError, match="orphan opinion 99"):
        _run(tmp_path, clusters, opinions)


# ---- 3. coverage + n_opinions cascade --------------------------------------


def test_coverage_and_n_opinions(tmp_path):
    clusters = [_raw_cluster(1, [10]), _raw_cluster(2, [20, 21])]  # 2 is seriatim
    opinions = [
        _raw_opinion(10, 1),
        _raw_opinion(20, 2, type="020lead"),
        _raw_opinion(21, 2, type="030concurrence"),
    ]
    stg_clusters, _, _ = _run(tmp_path, clusters, opinions)
    by_id = {c["cluster_id"]: c for c in stg_clusters}
    assert by_id[1]["n_opinions"] == 1
    assert by_id[2]["n_opinions"] == 2  # seriatim: >1


def test_declared_but_unmaterialized_raises(tmp_path):
    clusters = [_raw_cluster(1, [10, 11])]  # declares 11 but we only provide 10
    opinions = [_raw_opinion(10, 1)]
    with pytest.raises(RuntimeError, match="unmaterialized sub_opinions"):
        _run(tmp_path, clusters, opinions)


# ---- 4. source-field retention (anti-best_text) ----------------------------


def test_all_source_fields_retained(tmp_path):
    sources = {
        "html_lawbox": "<p>L</p>",
        "xml_harvard": "<x>H</x>",
        "html_with_citations": "<p>C</p>",
    }
    clusters = [_raw_cluster(1, [10])]
    opinions = [_raw_opinion(10, 1, sources=sources)]
    _, _, db = _run(tmp_path, clusters, opinions)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM stg_opinions WHERE opinion_id=10").fetchone()
    conn.close()
    assert row["source_html_lawbox"] == "<p>L</p>"
    assert row["source_xml_harvard"] == "<x>H</x>"
    assert row["source_html_with_citations"] == "<p>C</p>"
    for absent in (
        "source_html",
        "source_html_columbia",
        "source_html_anon_2020",
        "source_plain_text",
    ):
        assert row[absent] is None


def test_blank_source_field_not_retained(tmp_path):
    clusters = [_raw_cluster(1, [10])]
    opinions = [_raw_opinion(10, 1, sources={"html_lawbox": "  ", "xml_harvard": "real"})]
    stg_clusters, stg_opinions, _ = _run(tmp_path, clusters, opinions)
    assert stg_opinions[0]["sources"] == {"xml_harvard": "real"}


# ---- 5. normalization correctness ------------------------------------------


@pytest.mark.parametrize(
    "citations, expected",
    [
        ([{"reporter": "U.S.", "volume": "12", "page": "100"}], (12, "100", "12 U.S. 100")),
        (
            [
                {"reporter": "U.S. LEXIS", "volume": "9", "page": "z"},
                {"reporter": "Dall.", "volume": "2", "page": "1"},
                {"reporter": "U.S.", "volume": "2", "page": "300"},
            ],
            (2, "300", "2 U.S. 300"),
        ),
        ([{"reporter": "Dall.", "volume": "2", "page": "1"}], (None, None, None)),
        ([], (None, None, None)),
    ],
)
def test_parse_us_cite(citations, expected):
    assert materialize.parse_us_cite(citations) == expected


def test_resolve_cluster_id_prefers_int_then_url():
    assert (
        materialize.resolve_cluster_id({"cluster_id": 55, "cluster": f"{CL}/clusters/999/"}) == 55
    )
    assert materialize.resolve_cluster_id({"cluster": f"{CL}/clusters/777/"}) == 777


def test_normalize_cluster_name_and_scdb(tmp_path):
    clusters = [_raw_cluster(1, [10], case_name_full="", scdb_id="")]
    opinions = [_raw_opinion(10, 1)]
    stg_clusters, _, _ = _run(tmp_path, clusters, opinions)
    assert stg_clusters[0]["case_name"] == "Case 1"
    assert stg_clusters[0]["case_name_full"] is None  # "" -> None
    assert stg_clusters[0]["scdb_id"] == ""


# ---- 6. determinism --------------------------------------------------------


def test_determinism(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOTUS_BUILD_TIMESTAMP", "2026-01-01T00:00:00+00:00")
    clusters = [_raw_cluster(2, [20]), _raw_cluster(1, [10])]  # unsorted input
    opinions = [_raw_opinion(20, 2), _raw_opinion(10, 1)]
    a_clusters, a_opinions, a_db = _run(tmp_path / "a", clusters, opinions)
    b_clusters, b_opinions, b_db = _run(tmp_path / "b", clusters, opinions)
    assert a_clusters == b_clusters and a_opinions == b_opinions
    assert [c["cluster_id"] for c in a_clusters] == [1, 2]  # numeric order imposed

    def rows(db, table):
        conn = sqlite3.connect(db)
        out = conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        conn.close()
        return out

    assert rows(a_db, "stg_clusters") == rows(b_db, "stg_clusters")
    assert rows(a_db, "stg_opinions") == rows(b_db, "stg_opinions")


# ---- 7. decision-independence ----------------------------------------------


def test_no_decision_columns(tmp_path):
    _, _, db = _run(tmp_path, [_raw_cluster(1, [10])], [_raw_opinion(10, 1)])
    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(stg_clusters)")}
    cols |= {r[1] for r in conn.execute("PRAGMA table_info(stg_opinions)")}
    conn.close()
    forbidden = {
        "bucket",
        "disposition",
        "dedup_role",
        "dup_of",
        "is_scotus",
        "is_in_scope",
        "clean_text",
    }
    assert cols.isdisjoint(forbidden)


# ---- 8. round-trip ---------------------------------------------------------


def test_round_trip(tmp_path):
    clusters = [_raw_cluster(1, [10])]
    opinions = [_raw_opinion(10, 1, sources={"html_lawbox": "L"})]
    stg_clusters, stg_opinions, db = _run(tmp_path, clusters, opinions)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    crow = conn.execute("SELECT * FROM stg_clusters WHERE cluster_id=1").fetchone()
    orow = conn.execute("SELECT * FROM stg_opinions WHERE opinion_id=10").fetchone()
    conn.close()
    # citations + sub_opinion_ids round-trip through JSON
    assert json.loads(crow["sub_opinion_ids_json"]) == stg_clusters[0]["sub_opinion_ids"]
    assert json.loads(crow["citations_json"]) == stg_clusters[0]["citations"]
    assert crow["n_opinions"] == stg_clusters[0]["n_opinions"]
    # source_* columns reconstruct the in-memory sources dict
    reconstructed = {
        f: orow[f"source_{f}"] for f in materialize.CANDIDATE_SOURCE_FIELDS if orow[f"source_{f}"]
    }
    assert reconstructed == stg_opinions[0]["sources"]


# ---- 9. citation-timestamp stripping + xml_scan exclusion ------------------


def test_citation_timestamps_stripped(tmp_path):
    citations = [
        {
            "reporter": "U.S.",
            "volume": "12",
            "page": "100",
            "date_created": "2025-09-25T00:00:00Z",
            "date_modified": "2025-09-25T00:00:00Z",
        }
    ]
    clusters = [_raw_cluster(1, [10], citations=citations)]
    opinions = [_raw_opinion(10, 1)]
    stg_clusters, _, _ = _run(tmp_path, clusters, opinions)
    stored = stg_clusters[0]["citations"][0]
    assert "date_created" not in stored and "date_modified" not in stored
    assert stored == {"reporter": "U.S.", "volume": "12", "page": "100"}  # rest retained


def test_xml_scan_excluded(tmp_path):
    opinions = [_raw_opinion(10, 1, sources={"html_lawbox": "L"}, xml_scan="<scan>noise</scan>")]
    stg_clusters, stg_opinions, db = _run(tmp_path, [_raw_cluster(1, [10])], opinions)
    assert "xml_scan" not in stg_opinions[0]["sources"]
    assert "source_xml_scan" not in materialize.OPINION_COLUMNS
    conn = sqlite3.connect(db)
    schema_cols = {r[1] for r in conn.execute("PRAGMA table_info(stg_opinions)")}
    conn.close()
    assert "source_xml_scan" not in schema_cols
