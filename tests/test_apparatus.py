"""Tests for src.apparatus (deterministic, offline).

Unit tests use small in-memory builds; the data-quality test builds from the real apparatus pull
and auto-skips when it hasn't been generated yet (same pattern as the `db` fixture)."""

import csv
import json
import os

import pytest

from config import settings
from src import apparatus


@pytest.fixture
def sample_raw_apparatus():
    """Three clusters: a canonical rich in apparatus + metadata; its Harvard duplicate that carries
    the headmatter (and only a full name); and an entirely empty cluster."""
    return [
        {
            "id": 100,
            "case_name_full": "Ware, Administrator of Jones v. Hylton et al.",
            "syllabus": "",
            "headnotes": "  ",  # whitespace-only -> skipped
            "summary": "ERROR from the Circuit Court for the District of Virginia.",
            "headmatter": "<parties>Ware v. Hylton</parties>",
            "arguments": "",
            "disposition": "Affirmed.",
            "history": None,
            "procedural_history": None,
            "attorneys": "E. Tilghman, for the plaintiff in error.",
            "judges": "Chase, Paterson, Iredell, Wilson, Cushing",
        },
        {
            # Harvard 'U' duplicate of 100 — apparatus landed here, not on the canonical
            "id": 101,
            "case_name_full": "Ware v. Hylton",
            "headmatter": "<parties>Ware v. Hylton (Harvard)</parties>",
            "attorneys": "",  # empty -> NULL in cluster_meta
            "judges": None,
        },
        {
            "id": 200,
            "case_name_full": "",
            "summary": "",
            "headmatter": "",
            "attorneys": None,
            "judges": None,
        },
    ]


def test_apparatus_rows_skips_empty(sample_raw_apparatus):
    rows = apparatus.apparatus_rows(sample_raw_apparatus[0])
    kinds = {k for k, _, _ in rows}
    # only non-empty, non-whitespace kinds survive
    assert kinds == {"summary", "headmatter", "disposition"}
    # char_count is len(raw_text) for each
    for _, cc, txt in rows:
        assert cc == len(txt)
    assert apparatus.apparatus_rows(sample_raw_apparatus[2]) == []


def test_meta_row_normalizes_empty_to_none(sample_raw_apparatus):
    assert apparatus.meta_row(sample_raw_apparatus[0]) == (
        "Ware, Administrator of Jones v. Hylton et al.",
        "E. Tilghman, for the plaintiff in error.",
        "Chase, Paterson, Iredell, Wilson, Cushing",
    )
    # partial: empty attorneys/judges become None (not ""), full name kept
    assert apparatus.meta_row(sample_raw_apparatus[1]) == ("Ware v. Hylton", None, None)
    # all empty -> no row
    assert apparatus.meta_row(sample_raw_apparatus[2]) is None


def test_build_filters_to_corpus(tmp_path, sample_raw_apparatus):
    """Only clusters in the corpus map are included; out-of-corpus rows are skipped AND counted."""
    raw_path = tmp_path / "raw_apparatus.json"
    raw_path.write_text(json.dumps(sample_raw_apparatus))
    db_path = str(tmp_path / "apparatus.sqlite")

    # corpus contains cluster 100 only (101, 200 out-of-corpus)
    conn, counts = apparatus.build_apparatus_db(
        path=db_path, raw_apparatus=str(raw_path), corpus={100: 100}
    )
    assert counts["n_clusters_with_apparatus"] == 1
    assert counts["n_text_rows"] == 3
    assert counts["n_meta_rows"] == 1
    assert counts["n_skipped_out_of_corpus"] == 2
    ids = {r[0] for r in conn.execute("SELECT DISTINCT cluster_id FROM cluster_text")}
    assert ids == {100}
    conn.close()


def test_canonical_pointer_resolves_duplicate(tmp_path, sample_raw_apparatus):
    """Apparatus on a dedup'd duplicate (101) is stamped with the canonical decision (100), so it
    joins straight to the decision — while cluster_id preserves the true source location."""
    raw_path = tmp_path / "raw_apparatus.json"
    raw_path.write_text(json.dumps(sample_raw_apparatus))
    db_path = str(tmp_path / "apparatus.sqlite")

    conn, _ = apparatus.build_apparatus_db(
        path=db_path, raw_apparatus=str(raw_path), corpus={100: 100, 101: 100}
    )
    # the duplicate's apparatus resolves to canonical 100 but keeps its own source id
    dup = conn.execute(
        "SELECT cluster_id, canonical_cluster_id FROM cluster_text WHERE cluster_id=101"
    ).fetchall()
    assert dup and all(src == 101 and canon == 100 for src, canon in dup)
    # a query 'apparatus for decision 100' finds both 100's own and 101's rows
    reachable = {
        r[0]
        for r in conn.execute("SELECT cluster_id FROM cluster_text WHERE canonical_cluster_id=100")
    }
    assert reachable == {100, 101}
    # NULL for the duplicate's absent attorneys/judges
    assert conn.execute(
        "SELECT attorneys, judges FROM cluster_meta WHERE cluster_id=101"
    ).fetchone() == (None, None)
    conn.close()


def test_build_is_deterministic(tmp_path, sample_raw_apparatus):
    raw_path = tmp_path / "raw_apparatus.json"
    raw_path.write_text(json.dumps(sample_raw_apparatus))

    def build(p):
        conn, _ = apparatus.build_apparatus_db(
            path=str(tmp_path / p),
            raw_apparatus=str(raw_path),
            corpus={100: 100, 101: 100, 200: 200},
        )
        rows = conn.execute(
            "SELECT cluster_id, canonical_cluster_id, kind, char_count, raw_text "
            "FROM cluster_text ORDER BY cluster_id, kind"
        ).fetchall()
        conn.close()
        return rows

    assert build("a.sqlite") == build("b.sqlite")


# ---- data-quality (real pull) ----------------------------------------------


@pytest.fixture(scope="session")
def apparatus_db(tmp_path_factory):
    """Build the real apparatus asset from the on-disk pull; skip if it hasn't been generated."""
    if not os.path.exists(settings.RAW_APPARATUS) or not os.path.exists(settings.ALL_CLUSTERS_CSV):
        pytest.skip("apparatus pull missing; run `python -m src.pipeline --stage apparatus` first")
    clu = {int(r["cluster_id"]): r for r in csv.DictReader(open(settings.ALL_CLUSTERS_CSV))}
    corpus = {
        cid: (int(r["dup_of"]) if r["dedup_role"] == "duplicate" and r["dup_of"] else cid)
        for cid, r in clu.items()
    }
    path = str(tmp_path_factory.mktemp("app") / "apparatus.sqlite")
    conn, _ = apparatus.build_apparatus_db(
        path=path, raw_apparatus=settings.RAW_APPARATUS, corpus=corpus
    )
    yield conn
    conn.close()


def test_apparatus_invariants(apparatus_db):
    conn = apparatus_db
    q = lambda s: conn.execute(s).fetchone()[0]  # noqa: E731
    # char_count always matches the stored text length
    assert q("SELECT count(*) FROM cluster_text WHERE char_count != length(raw_text)") == 0
    # no empty rows leaked in
    assert q("SELECT count(*) FROM cluster_text WHERE raw_text IS NULL OR trim(raw_text)=''") == 0
    # every kind is one we intended to capture
    kinds = {r[0] for r in conn.execute("SELECT DISTINCT kind FROM cluster_text")}
    assert kinds <= set(apparatus.APPARATUS_KINDS)
    # the resolved pointer is always populated (the whole point of it)
    assert q("SELECT count(*) FROM cluster_text WHERE canonical_cluster_id IS NULL") == 0
    # there is actually apparatus to show (guards against a silently-empty pull)
    assert q("SELECT count(*) FROM cluster_text") > 0
