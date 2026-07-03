"""Tests for src.pipeline orchestration (no network; settings paths monkeypatched)."""

import json

from config import settings
from src import pipeline


def test_write_csv(tmp_path):
    p = tmp_path / "out.csv"
    pipeline._write_csv(str(p), ["a", "b"], [{"a": 1, "b": 2}, {"a": 3}])
    lines = p.read_text().splitlines()
    assert lines[0] == "a,b"
    assert lines[1] == "1,2"
    assert lines[2] == "3,"  # missing key -> empty cell


def test_validate_prints_total(capsys):
    pipeline._validate([{"dateFiled": "1805-02-01"}, {"dateFiled": "1805-03-01"}])
    assert "TOT" in capsys.readouterr().out


def test_stage_clusters_from_cache(tmp_path, monkeypatch, sample_raw_clusters):
    raw_path = tmp_path / "raw.json"
    raw_path.write_text(json.dumps(sample_raw_clusters))
    monkeypatch.setattr(settings, "ensure_dirs", lambda: None)
    monkeypatch.setattr(settings, "RAW_CLUSTERS", str(raw_path))
    for attr in ("ALL_CLUSTERS_CSV", "REVIEW_CSV", "DUPLICATES_CSV", "KEEP_CSV"):
        monkeypatch.setattr(settings, attr, str(tmp_path / f"{attr}.csv"))

    keep = pipeline.stage_clusters(from_cache=True, validate=True)

    assert [r["cluster_id"] for r in keep] == [10]  # dedup collapsed the Harvard copy
    assert (tmp_path / "KEEP_CSV.csv").exists()
    assert (tmp_path / "DUPLICATES_CSV.csv").exists()


def test_stage_load(monkeypatch, capsys):
    class FakeConn:
        def close(self):
            pass

    monkeypatch.setattr(settings, "ensure_dirs", lambda: None)
    monkeypatch.setattr(
        pipeline.load, "build_db", lambda *a, **k: (FakeConn(), {"n_keep_decisions": 663})
    )
    pipeline.stage_load()
    assert "663" in capsys.readouterr().out


def test_main_runs_all_stages(monkeypatch):
    called = []
    monkeypatch.setattr(pipeline, "stage_clusters", lambda **k: called.append("clusters"))
    monkeypatch.setattr(pipeline, "stage_text", lambda **k: called.append("text"))
    monkeypatch.setattr(pipeline, "stage_load", lambda: called.append("load"))
    monkeypatch.setattr(pipeline.sys, "argv", ["prog", "--stage", "all", "--from-cache"])
    pipeline.main()
    assert called == ["clusters", "text", "load"]
