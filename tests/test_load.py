"""Tests for src.load internals (small in-memory builds; no network)."""

import json
import sqlite3

from src import load


def _fresh_db():
    conn = sqlite3.connect(":memory:")
    for stmt in load.DDL:
        conn.execute(stmt)
    return conn


def test_load_citations_reports_dropped_dupes(tmp_path):
    """Exact-duplicate (cluster, reporter, volume, page) tuples are collapsed AND counted."""
    raw = [
        {
            "id": 1,
            "citations": [
                {"reporter": "U.S.", "volume": "2", "page": "1", "type": 1},
                {"reporter": "U.S.", "volume": "2", "page": "1", "type": 1},  # exact dupe
                {"reporter": "Dall.", "volume": "2", "page": "1", "type": 5},
            ],
        }
    ]
    path = tmp_path / "raw.json"
    path.write_text(json.dumps(raw))

    conn = _fresh_db()
    inserted, dropped = load._load_citations(conn, "?", "sqlite", str(path))
    assert inserted == 2
    assert dropped == 1
    assert conn.execute("SELECT count(*) FROM citations").fetchone()[0] == 2
