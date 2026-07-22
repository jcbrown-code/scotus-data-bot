"""Transform · clean_opinions: derive clean_text from each opinion's chosen source.

reselect chose one source-text field per opinion (stg_opinion_source). This stage
runs that chosen text through the shared deterministic cleaner (src/clean.py,
``clean_opinion``) and writes the results: stg_opinion_clean (clean_text +
clean_version + ocr_suspect) and stg_page_break (the star-pagination map).

The cleaner is reused unchanged -- it renders both the HTML and Harvard-XML dialects,
strips star-pagination into page breaks, normalizes to NFC, and does NOT correct OCR
or drop prose. Two properties matter for this corpus and are preserved by reusing it:
inline justice headers survive (the seriatim segmentation signal for a later stage),
and captions/footnote bodies are kept. Non-destructive: raw sources stay in
stg_opinions; this stage only derives.
"""

import sqlite3
from typing import NamedTuple

from config import settings
from src import clean
from src.transform import reselect


class OpinionClean(NamedTuple):
    opinion_id: int
    cluster_id: int
    clean_text: str
    ocr_suspect: str | None
    page_breaks: list


def read_chosen_texts(staging_db_path: str) -> list[tuple]:
    """Return (opinion_id, cluster_id, chosen_text) for each corpus opinion.

    The text is the single field reselect chose (stg_opinion_source.chosen_source)."""
    source_cols = ", ".join(f"o.{field}" for field in reselect.SOURCE_PRIORITY)
    conn = sqlite3.connect(staging_db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT s.opinion_id, s.cluster_id, s.chosen_source, {source_cols} "
            "FROM stg_opinion_source s JOIN stg_opinions o USING (opinion_id) "
            "ORDER BY s.opinion_id"
        ).fetchall()
    finally:
        conn.close()
    return [
        (
            row["opinion_id"],
            row["cluster_id"],
            (row[row["chosen_source"]] or "") if row["chosen_source"] else "",
        )
        for row in rows
    ]


def build_clean_opinions(chosen_texts: list[tuple]) -> list[OpinionClean]:
    """Clean each opinion's chosen text (pure apart from the shared cleaner)."""
    results = []
    for opinion_id, cluster_id, text in chosen_texts:
        clean_text, page_breaks, ocr_suspect = clean.clean_opinion(text)
        results.append(
            OpinionClean(
                opinion_id=opinion_id,
                cluster_id=cluster_id,
                clean_text=clean_text,
                ocr_suspect=clean.ocr_suspect_json(ocr_suspect),
                page_breaks=page_breaks,
            )
        )
    return results


def write_clean_tables(staging_db_path: str, cleaned: list[OpinionClean]) -> None:
    """Write stg_opinion_clean + stg_page_break (clean rebuild; idempotent)."""
    conn = sqlite3.connect(staging_db_path)
    try:
        conn.execute("DROP TABLE IF EXISTS stg_opinion_clean")
        conn.execute("DROP TABLE IF EXISTS stg_page_break")
        conn.execute(
            "CREATE TABLE stg_opinion_clean ("
            "opinion_id INTEGER PRIMARY KEY, cluster_id INTEGER, clean_text TEXT NOT NULL, "
            "clean_version INTEGER NOT NULL, ocr_suspect TEXT)"
        )
        conn.execute(
            "CREATE TABLE stg_page_break ("
            "opinion_id INTEGER, ordinal INTEGER, page_label TEXT, char_offset INTEGER, "
            "anchor TEXT, PRIMARY KEY (opinion_id, ordinal))"
        )
        conn.executemany(
            "INSERT INTO stg_opinion_clean VALUES (?, ?, ?, ?, ?)",
            [
                (c.opinion_id, c.cluster_id, c.clean_text, clean.CLEAN_VERSION, c.ocr_suspect)
                for c in cleaned
            ],
        )
        conn.executemany(
            "INSERT INTO stg_page_break VALUES (?, ?, ?, ?, ?)",
            [
                (c.opinion_id, pb["ordinal"], pb["page_label"], pb["char_offset"], pb["anchor"])
                for c in cleaned
                for pb in c.page_breaks
            ],
        )
        conn.commit()
    finally:
        conn.close()


def run_clean(staging_db_path: str = settings.STAGING_DB_PATH) -> list[OpinionClean]:
    """Clean every corpus opinion's chosen source; write the derived tables."""
    cleaned = build_clean_opinions(read_chosen_texts(staging_db_path))
    write_clean_tables(staging_db_path, cleaned)
    return cleaned
