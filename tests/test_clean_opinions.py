"""Tests for src.transform.clean_opinions — derive clean_text from the chosen source.

The cleaner itself (src/clean.py) is tested separately; these cover the stage wiring
(reading the reselect choice, writing the tables) plus data-quality over real staging.
"""

import os
import sqlite3

import pytest

from config import settings
from src import clean
from src.transform import clean_opinions, reselect


def test_build_uses_chosen_text_and_extracts_page_breaks():
    raw = (
        '<p>Alpha opinion text</p><span class="star-pagination" label="401">*401</span><p>beta</p>'
    )
    cleaned = clean_opinions.build_clean_opinions([(10, 1, raw)])
    assert len(cleaned) == 1
    c = cleaned[0]
    assert "Alpha opinion text" in c.clean_text
    assert 'class="star-pagination"' not in c.clean_text  # markup stripped
    assert [pb["page_label"] for pb in c.page_breaks] == ["401"]


def test_empty_source_yields_empty_clean():
    cleaned = clean_opinions.build_clean_opinions([(10, 1, "")])
    assert cleaned[0].clean_text == "" and cleaned[0].page_breaks == []


def test_run_clean_writes_both_tables(tmp_path):
    db = str(tmp_path / "staging.sqlite")
    conn = sqlite3.connect(db)
    source_cols = ", ".join(f"{f} TEXT" for f in reselect.SOURCE_PRIORITY)
    conn.execute(
        "CREATE TABLE stg_opinions (opinion_id INTEGER PRIMARY KEY, cluster_id INTEGER, "
        f"{source_cols})"
    )
    conn.execute(
        "CREATE TABLE stg_opinion_source (opinion_id INTEGER PRIMARY KEY, cluster_id INTEGER, "
        "type TEXT, chosen_source TEXT, is_ocr_dirty INTEGER)"
    )
    conn.execute(
        "INSERT INTO stg_opinions (opinion_id, cluster_id, source_html_lawbox) "
        "VALUES (10, 1, '<p>Opinion of the court.</p>"
        '<span class="star-pagination" label="5">*5</span><p>more</p>\')'
    )
    conn.execute(
        "INSERT INTO stg_opinion_source VALUES (10, 1, '010combined', 'source_html_lawbox', 0)"
    )
    conn.commit()
    conn.close()

    clean_opinions.run_clean(db)
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT clean_text, clean_version FROM stg_opinion_clean WHERE opinion_id=10"
    ).fetchone()
    breaks = conn.execute("SELECT page_label FROM stg_page_break WHERE opinion_id=10").fetchall()
    conn.close()
    assert "Opinion of the court." in row[0] and row[1] == clean.CLEAN_VERSION
    assert breaks == [("5",)]


# ---- data-quality against real staging ---------------------------------------


def _real_cleaned():
    if not os.path.exists(settings.STAGING_DB_PATH):
        pytest.skip("staging DB missing; run the earlier stages first")
    conn = sqlite3.connect(settings.STAGING_DB_PATH)
    has = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='stg_opinion_source'"
    ).fetchone()[0]
    conn.close()
    if not has:
        pytest.skip("stg_opinion_source missing; run the reselect stage first")
    return clean_opinions.build_clean_opinions(
        clean_opinions.read_chosen_texts(settings.STAGING_DB_PATH)
    )


def test_every_corpus_opinion_has_clean_text():
    empty = [c.opinion_id for c in _real_cleaned() if not c.clean_text.strip()]
    assert empty == []


def test_no_star_markers_or_sentinels_leak():
    import re

    offenders = [
        c.opinion_id
        for c in _real_cleaned()
        if clean._S0 in c.clean_text
        or clean._S1 in c.clean_text
        or re.search(r'class="star-pagination"|<page-number', c.clean_text)
    ]
    assert offenders == []


def test_page_break_offsets_are_in_range():
    bad = [
        c.opinion_id
        for c in _real_cleaned()
        for pb in c.page_breaks
        if not (0 <= pb["char_offset"] <= len(c.clean_text))
    ]
    assert bad == []
