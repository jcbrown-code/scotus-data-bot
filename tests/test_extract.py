"""Tests for src.extract — network is mocked via monkeypatching `_get`."""

import io
import urllib.error

from src import extract


def test_build_headers():
    h = extract.build_headers("tok")
    assert h["Authorization"] == "Token tok"
    assert "User-Agent" in h


def test_fetch_clusters_paginates_and_dedupes(monkeypatch):
    pages = [
        {"results": [{"id": 1}, {"id": 2}], "next": "cursor2"},
        {"results": [{"id": 2}, {"id": 3}], "next": None},  # id 2 repeats -> deduped
    ]
    calls = iter(pages)
    monkeypatch.setattr(extract, "_get", lambda url, headers: next(calls))
    monkeypatch.setattr(extract.time, "sleep", lambda *_: None)
    rows = extract.fetch_clusters("1805-01-01", "1805-12-31", "tok")
    assert [r["id"] for r in rows] == [1, 2, 3]


def test_fetch_opinions_returns_results(monkeypatch):
    monkeypatch.setattr(
        extract, "_get", lambda url, headers, **kw: {"results": [{"id": 9, "type": "010combined"}]}
    )
    ops = extract.fetch_opinions(9, {"Authorization": "Token tok"})
    assert ops == [{"id": 9, "type": "010combined"}]


def test_get_retries_then_succeeds(monkeypatch):
    """A transient network error is retried, then the parsed JSON is returned."""
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.URLError("transient")
        return io.BytesIO(b'{"results": []}')  # BytesIO is a context manager

    monkeypatch.setattr(extract.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(extract.time, "sleep", lambda *_: None)
    assert extract._get("http://x", {}) == {"results": []}
    assert calls["n"] == 2
