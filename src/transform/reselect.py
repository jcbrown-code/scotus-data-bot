"""Transform · reselect: choose the best source-text field per opinion.

materialize retained every candidate transcription field per opinion; CourtListener
documents no precedence and its ``html_with_citations`` pick is not fidelity-safe
(often the OCR-dirty Harvard text). This stage records, per opinion, which field to
use downstream -- a pointer, not a copy, so it is non-destructive: every source field
stays in stg_opinions.

The choice is by priority, cleanest-and-opinion-only first (measured on the corpus):
- ``html_lawbox``: clean and opinion-only -- ideal when present (435/693, 0% dirty).
- ``xml_harvard``: opinion-only, but OCR-dirty for Dallas (long-s); preferred over html
  anyway, because correct scope beats cleanliness -- apparatus contamination is worse
  and harder to strip than localized, flaggable OCR noise.
- ``html`` (resource.org): clean words but BUNDLES the reporter apparatus (syllabus +
  arguments + other opinions) into the body -- wrong scope, so a last resort.
- ``html_with_citations``: CL's derived pick; a final universal fallback.

It works per opinion row, so it is neutral to the combined-vs-split representation: a
cluster's combined row and its per-justice split rows each get a source, and ``type``
is carried through so the distinction stays queryable (the "keep both, typed" decision
for the ~12 dual-representation clusters). Segmenting inline seriatim is a later stage.

``is_ocr_dirty`` flags an opinion whose chosen text carries OCR markers -- input to a
future OCR pass, not a correction here.
"""

import re
import sqlite3
from typing import NamedTuple

from config import settings

# Source-text fields in preference order (see module docstring): opinion-only + clean
# first, apparatus-bundled html last, CL's derived pick as the final fallback.
SOURCE_PRIORITY = (
    "source_html_lawbox",
    "source_xml_harvard",
    "source_html",
    "source_html_with_citations",
)

# OCR-dirtiness markers: the U+25A0 replacement glyph, and long-s tokens (f-for-s), which
# in this corpus are confined to Dallas-era Harvard OCR. A conservative flag, not a fix.
_REPLACEMENT_GLYPH = "■"
_LONG_S_TOKENS = re.compile(
    r"\b(?:juftice|faid|fuch|thofe|fhall|prefent|caufe|becaufe|firft|conftitution"
    r"|congrefs|houfe|cafe|fuit|purpofe|reafon|perfon|againft|fubject|prefident)\b"
)


def is_ocr_dirty(text: str) -> bool:
    """Whether opinion text carries OCR markers (replacement glyph or long-s tokens)."""
    lowered = (text or "").lower()
    return _REPLACEMENT_GLYPH in (text or "") or bool(_LONG_S_TOKENS.search(lowered))


def select_source(opinion: dict) -> tuple[str | None, bool]:
    """Choose one opinion's source field by priority; report if its text is OCR-dirty."""
    for field in SOURCE_PRIORITY:
        if opinion.get(field):
            return field, is_ocr_dirty(opinion[field])
    return None, False  # no source text at all (should not occur in the corpus)


class OpinionSource(NamedTuple):
    opinion_id: int
    cluster_id: int
    type: str
    chosen_source: str | None
    is_ocr_dirty: bool


def build_selections(opinions: list[dict]) -> list[OpinionSource]:
    """Apply the source choice to every opinion (pure; no I/O)."""
    selections = []
    for opinion in opinions:
        chosen, dirty = select_source(opinion)
        selections.append(
            OpinionSource(
                opinion_id=opinion["opinion_id"],
                cluster_id=opinion["cluster_id"],
                type=opinion.get("type") or "",
                chosen_source=chosen,
                is_ocr_dirty=dirty,
            )
        )
    return selections


def read_corpus_opinions(staging_db_path: str) -> list[dict]:
    """Opinions in the final corpus (canonical KEEP clusters, vols 2-18), as dicts.

    Includes both the combined and per-justice split rows of the dual-representation
    clusters -- the choice is per row, nothing is dropped here."""
    columns = "o.opinion_id, o.cluster_id, o.type, " + ", ".join(f"o.{f}" for f in SOURCE_PRIORITY)
    conn = sqlite3.connect(staging_db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT {columns} FROM stg_opinions o "
            "JOIN stg_cluster_dedup d USING (cluster_id) "
            "WHERE d.dedup_role = 'canonical' AND d.us_volume BETWEEN 2 AND 18 "
            "ORDER BY o.opinion_id"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


_SOURCE_TABLE_COLUMNS = ("opinion_id", "cluster_id", "type", "chosen_source", "is_ocr_dirty")


def write_source_table(staging_db_path: str, selections: list[OpinionSource]) -> None:
    """Write the derived stg_opinion_source table (clean rebuild; idempotent)."""
    conn = sqlite3.connect(staging_db_path)
    try:
        conn.execute("DROP TABLE IF EXISTS stg_opinion_source")
        conn.execute(
            "CREATE TABLE stg_opinion_source ("
            "opinion_id INTEGER PRIMARY KEY, cluster_id INTEGER, type TEXT, "
            "chosen_source TEXT, is_ocr_dirty INTEGER NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO stg_opinion_source "
            f"({', '.join(_SOURCE_TABLE_COLUMNS)}) VALUES (?, ?, ?, ?, ?)",
            [
                (s.opinion_id, s.cluster_id, s.type, s.chosen_source, int(s.is_ocr_dirty))
                for s in selections
            ],
        )
        conn.commit()
    finally:
        conn.close()


def run_reselect(staging_db_path: str = settings.STAGING_DB_PATH) -> list[OpinionSource]:
    """Read corpus opinions, choose a source for each, write stg_opinion_source."""
    opinions = read_corpus_opinions(staging_db_path)
    selections = build_selections(opinions)
    write_source_table(staging_db_path, selections)
    return selections
