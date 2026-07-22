"""Tests for src.pipeline orchestration (no network)."""

from src import pipeline


def test_write_csv(tmp_path):
    p = tmp_path / "out.csv"
    pipeline._write_csv(str(p), ["a", "b"], [{"a": 1, "b": 2}, {"a": 3}])
    lines = p.read_text().splitlines()
    assert lines[0] == "a,b"
    assert lines[1] == "1,2"
    assert lines[2] == "3,"  # missing key -> empty cell
