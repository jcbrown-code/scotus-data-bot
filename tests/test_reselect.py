"""Tests for src.transform.reselect — choose the best source-text field per opinion.

Pure unit tests for the priority + dirtiness signals, plus data-quality tests over
the real staging DB (skipped when absent).
"""

import os
import sqlite3

import pytest

from config import settings
from src.transform import reselect


def _opinion(oid=1, cid=1, type="010combined", **sources):
    row = {"opinion_id": oid, "cluster_id": cid, "type": type}
    for field in reselect.SOURCE_PRIORITY:
        row[field] = sources.get(field)
    return row


# ---- is_ocr_dirty ------------------------------------------------------------


@pytest.mark.parametrize(
    "text, dirty",
    [
        ("a clean modern opinion of the court", False),
        ("the ■ glyph is a replacement char", True),  # replacement glyph
        ("the juftice delivered the opinion", True),  # long-s token
        ("2d and 3d are correct period ordinals", False),  # must NOT flag legit ordinals
    ],
)
def test_is_ocr_dirty(text, dirty):
    assert reselect.is_ocr_dirty(text) is dirty


# ---- select_source (priority) ------------------------------------------------


def test_prefers_lawbox_over_everything():
    op = _opinion(
        source_html_lawbox="clean opinion text",
        source_xml_harvard="dirty juftice text",
        source_html="apparatus bundled",
    )
    assert reselect.select_source(op) == ("source_html_lawbox", False)


def test_prefers_harvard_over_html_when_no_lawbox():
    # opinion-only-first: dirty-but-opinion-only harvard beats clean-but-apparatus html
    op = _opinion(source_xml_harvard="the juftice faid", source_html="clean apparatus text")
    field, dirty = reselect.select_source(op)
    assert field == "source_xml_harvard" and dirty is True


def test_falls_back_to_html_then_citations():
    assert reselect.select_source(_opinion(source_html="x"))[0] == "source_html"
    assert reselect.select_source(_opinion(source_html_with_citations="x"))[0] == (
        "source_html_with_citations"
    )


def test_no_source_returns_none():
    assert reselect.select_source(_opinion()) == (None, False)


def test_build_selections_carries_type():
    ops = [
        _opinion(1, 1, "010combined", source_html_lawbox="clean"),
        _opinion(2, 1, "020lead", source_xml_harvard="juftice"),
    ]
    by_id = {s.opinion_id: s for s in reselect.build_selections(ops)}
    assert by_id[1].chosen_source == "source_html_lawbox" and by_id[1].type == "010combined"
    assert by_id[2].chosen_source == "source_xml_harvard" and by_id[2].is_ocr_dirty is True


# ---- round trip --------------------------------------------------------------


def test_run_reselect_writes_table(tmp_path):
    db = str(tmp_path / "staging.sqlite")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE stg_cluster_dedup (cluster_id INTEGER, us_volume INTEGER, dedup_role TEXT)"
    )
    source_cols = ", ".join(f"{f} TEXT" for f in reselect.SOURCE_PRIORITY)
    conn.execute(
        f"CREATE TABLE stg_opinions (opinion_id INTEGER PRIMARY KEY, cluster_id INTEGER, "
        f"type TEXT, {source_cols})"
    )
    conn.execute("INSERT INTO stg_cluster_dedup VALUES (1, 6, 'canonical')")
    conn.execute("INSERT INTO stg_cluster_dedup VALUES (2, 6, 'duplicate')")  # excluded
    conn.execute("INSERT INTO stg_cluster_dedup VALUES (3, 19, 'canonical')")  # vol 19 excluded
    conn.execute(
        "INSERT INTO stg_opinions (opinion_id, cluster_id, type, source_html_lawbox) "
        "VALUES (10, 1, '010combined', 'clean')"
    )
    conn.execute(
        "INSERT INTO stg_opinions (opinion_id, cluster_id, type, source_xml_harvard) "
        "VALUES (20, 2, '010combined', 'x')"
    )
    conn.execute(
        "INSERT INTO stg_opinions (opinion_id, cluster_id, type, source_html) "
        "VALUES (30, 3, '010combined', 'x')"
    )
    conn.commit()
    conn.close()

    selections = reselect.run_reselect(db)
    assert [s.opinion_id for s in selections] == [10]  # only canonical, vols 2-18
    conn = sqlite3.connect(db)
    stored = conn.execute("SELECT chosen_source FROM stg_opinion_source").fetchall()
    conn.close()
    assert stored == [("source_html_lawbox",)]


# ---- data-quality against real staging ---------------------------------------


def _real_selections():
    if not os.path.exists(settings.STAGING_DB_PATH):
        pytest.skip("staging DB missing; run materialize + scope + dedup first")
    conn = sqlite3.connect(settings.STAGING_DB_PATH)
    has_dedup = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='stg_cluster_dedup'"
    ).fetchone()[0]
    conn.close()
    if not has_dedup:
        pytest.skip("stg_cluster_dedup missing; run the dedup stage first")
    return reselect.build_selections(reselect.read_corpus_opinions(settings.STAGING_DB_PATH))


def test_every_corpus_opinion_gets_a_source():
    """No corpus opinion is left without a source-text field (0 textless)."""
    sourceless = [s.opinion_id for s in _real_selections() if s.chosen_source is None]
    assert sourceless == []


def test_split_rows_are_harvard():
    """Per-justice split rows exist only as Harvard, so they must select xml_harvard."""
    wrong = [
        s.opinion_id
        for s in _real_selections()
        if s.type in ("020lead", "030concurrence", "040dissent")
        and s.chosen_source != "source_xml_harvard"
    ]
    assert wrong == []
