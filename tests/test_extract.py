"""Tests for src.extract — offline; network is mocked via monkeypatching urlopen / `_get`.

Covers the cluster fetch (apparatus pull) AND the raw-mirror reliability guarantees: rate-limit/
backoff, schema validation, verbatim+deterministic storage, pagination continuity, coverage,
idempotency."""

import email.message
import io
import json
import urllib.error

import pytest

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


# ===========================================================================
# Raw-mirror stage (fetch_clusters_raw / fetch_opinions_raw + reliability)
# ===========================================================================


class _FakeResp:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode()

    def read(self, *a):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, retry_after=None):
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError("http://x", code, "err", hdrs, None)


def _patch_urlopen(monkeypatch, actions):
    """Feed a queue of actions to urlopen: a dict -> success page; an Exception -> raised."""
    it = iter(actions)

    def fake(req, timeout=None):
        action = next(it)
        if isinstance(action, Exception):
            raise action
        return _FakeResp(action)

    monkeypatch.setattr(extract.urllib.request, "urlopen", fake)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(extract.time, "sleep", lambda *a, **k: None)


def _cluster(i, subs=()):
    return {
        "id": i,
        "case_name": f"C{i}",
        "date_filed": "1815-01-01",
        "citations": [],
        "source": "R",
        "sub_opinions": list(subs),
    }


# ---- rate limit / backoff --------------------------------------------------


def test_backoff_respects_retry_after(monkeypatch):
    slept = []
    monkeypatch.setattr(extract.time, "sleep", lambda s: slept.append(s))
    _patch_urlopen(monkeypatch, [_http_error(429, retry_after=7), {"ok": True}])
    body, meta = extract._request("http://x", {})
    assert body == {"ok": True}
    assert meta["retry_after"] == [7]
    assert 8 in slept  # slept Retry-After + 1


def test_backoff_handles_5xx(monkeypatch):
    _patch_urlopen(monkeypatch, [_http_error(502), {"ok": True}])
    body, meta = extract._request("http://x", {})
    assert body == {"ok": True}
    assert meta["server_errors"] == [502]


def test_backoff_gives_up(monkeypatch):
    _patch_urlopen(monkeypatch, [_http_error(429, 1)] * 6)
    with pytest.raises(RuntimeError):
        extract._request("http://x", {})


def test_non_transient_http_error_is_raised(monkeypatch):
    _patch_urlopen(monkeypatch, [_http_error(404)])
    with pytest.raises(urllib.error.HTTPError):
        extract._request("http://x", {})


def test_pace_adaptive_on_429(monkeypatch):
    extract.PACE["delay"] = 1.0
    _patch_urlopen(monkeypatch, [_http_error(429, 1), {"ok": True}])
    extract._request("http://x", {}, pace=True)
    assert extract.PACE["delay"] > 1.0


# ---- schema validation -----------------------------------------------------


def test_validate_schema_ok():
    rec = _cluster(1)
    assert extract.validate_schema(rec, "cluster") is rec


def test_validate_schema_missing_required():
    with pytest.raises(extract.SchemaError):
        extract.validate_schema({"id": 1}, "cluster")


def test_validate_schema_wrong_type():
    with pytest.raises(extract.SchemaError):
        extract.validate_schema({"id": "x", "cluster": 5, "type": "t"}, "opinion")


# ---- verbatim + deterministic storage --------------------------------------


def test_store_raw_verbatim_and_deterministic(tmp_path):
    rec = {"id": 42, "b": 2, "a": 1, "text": "café"}
    d = tmp_path / "clusters"
    d.mkdir()
    extract.store_raw(str(d), rec)
    first = (d / "42.json").read_bytes()
    extract.store_raw(str(d), rec)
    assert (d / "42.json").read_bytes() == first  # re-store is byte-identical
    assert json.loads(first) == rec  # no field dropped or reshaped


# ---- clusters: pagination continuity ---------------------------------------


def test_fetch_clusters_pagination_and_count(tmp_path, monkeypatch):
    cdir = tmp_path / "clusters"
    cdir.mkdir()
    page1 = {"results": [_cluster(1), _cluster(2)], "next": "http://next", "count": 3}
    page2 = {"results": [_cluster(3)], "next": None, "count": 3}
    _patch_urlopen(monkeypatch, [page1, page2])
    log = extract.fetch_clusters_raw("1815-01-01", "1815-12-31", "tok", str(cdir))
    assert log[0]["stored"] == 3 and log[0]["api_count"] == 3
    assert extract._read_ids(str(cdir)) == {1, 2, 3}


def test_fetch_clusters_pagination_gap_raises(tmp_path, monkeypatch):
    cdir = tmp_path / "clusters"
    cdir.mkdir()
    _patch_urlopen(monkeypatch, [{"results": [_cluster(1)], "next": None, "count": 5}])
    with pytest.raises(RuntimeError, match="pagination gap"):
        extract.fetch_clusters_raw("1815-01-01", "1815-12-31", "tok", str(cdir))


def test_fetch_clusters_duplicate_id_raises(tmp_path, monkeypatch):
    cdir = tmp_path / "clusters"
    cdir.mkdir()
    page1 = {"results": [_cluster(1)], "next": "http://next", "count": 1}
    page2 = {"results": [_cluster(1)], "next": None, "count": 1}
    _patch_urlopen(monkeypatch, [page1, page2])
    with pytest.raises(RuntimeError, match="duplicate cluster"):
        extract.fetch_clusters_raw("1815-01-01", "1815-12-31", "tok", str(cdir))


# ---- opinions: coverage + resume -------------------------------------------


def test_fetch_opinions_coverage_and_resume(tmp_path, monkeypatch):
    cdir = tmp_path / "clusters"
    odir = tmp_path / "opinions"
    cdir.mkdir()
    odir.mkdir()
    extract.store_raw(str(cdir), _cluster(1, subs=["/o/10/", "/o/11/"]))
    body = {
        "results": [
            {"id": 10, "cluster": "/c/1/", "type": "010combined"},
            {"id": 11, "cluster": "/c/1/", "type": "030concurrence"},
        ],
        "next": None,
    }
    _patch_urlopen(monkeypatch, [body])
    log = extract.fetch_opinions_raw("tok", str(cdir), str(odir))
    assert log == [{"cluster_id": 1, "opinions": 2}]
    assert extract._read_ids(str(odir)) == {10, 11}
    _patch_urlopen(monkeypatch, [])  # resume: all present -> no fetch (empty queue)
    assert extract.fetch_opinions_raw("tok", str(cdir), str(odir)) == []


def test_fetch_opinions_missing_declared_raises(tmp_path, monkeypatch):
    cdir = tmp_path / "clusters"
    odir = tmp_path / "opinions"
    cdir.mkdir()
    odir.mkdir()
    extract.store_raw(str(cdir), _cluster(1, subs=["/o/10/", "/o/11/"]))
    body = {"results": [{"id": 10, "cluster": "/c/1/", "type": "t"}], "next": None}
    _patch_urlopen(monkeypatch, [body])
    with pytest.raises(RuntimeError, match="not returned"):
        extract.fetch_opinions_raw("tok", str(cdir), str(odir))


def test_fetch_opinions_unexpected_pagination_raises(tmp_path, monkeypatch):
    cdir = tmp_path / "clusters"
    odir = tmp_path / "opinions"
    cdir.mkdir()
    odir.mkdir()
    extract.store_raw(str(cdir), _cluster(1, subs=["/o/10/"]))
    body = {"results": [{"id": 10, "cluster": "/c/1/", "type": "t"}], "next": "http://more"}
    _patch_urlopen(monkeypatch, [body])
    with pytest.raises(RuntimeError, match="unexpected opinion pagination"):
        extract.fetch_opinions_raw("tok", str(cdir), str(odir))


# ---- coverage / orphan / idempotency helpers -------------------------------


def test_verify_coverage_and_orphans(tmp_path):
    cdir = tmp_path / "clusters"
    odir = tmp_path / "opinions"
    cdir.mkdir()
    odir.mkdir()
    extract.store_raw(str(cdir), _cluster(1, subs=["/o/10/", "/o/11/"]))
    extract.store_raw(str(odir), {"id": 10, "cluster": "/c/1/"})
    extract.store_raw(str(odir), {"id": 99, "cluster": "/c/77/"})  # orphan
    assert extract.verify_coverage(str(cdir), str(odir)) == [(1, [11])]
    orphans = extract.verify_no_orphans(str(cdir), str(odir))
    assert 99 in orphans and 10 not in orphans


def test_idempotency_volatile_vs_substantive(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    extract.store_raw(str(a), {"id": 1, "case_name": "X", "date_modified": "T1"})
    extract.store_raw(str(b), {"id": 1, "case_name": "X", "date_modified": "T2"})  # volatile only
    extract.store_raw(str(a), {"id": 2, "case_name": "Y"})
    extract.store_raw(str(b), {"id": 2, "case_name": "Z"})  # substantive
    d = extract.diff_stores(str(a), str(b))
    assert d["volatile"] == ["1.json"]
    assert d["substantive"] == ["2.json"]
    assert d["only_a"] == [] and d["only_b"] == []


def test_extract_manifest_counts_full_mirror_not_run(tmp_path, monkeypatch):
    """Manifest n_clusters/n_opinions describe the MIRROR, not just this run's fetches, so a
    resumed run (which skips already-stored opinions) still reports the true totals."""
    cdir = tmp_path / "clusters"
    odir = tmp_path / "opinions"
    cdir.mkdir()
    odir.mkdir()
    # a prior run already stored cluster 1 + its only opinion
    extract.store_raw(str(cdir), _cluster(1, subs=["/o/10/"]))
    extract.store_raw(str(odir), {"id": 10, "cluster": "/c/1/", "type": "t"})
    # this run: clusters re-fetched (1 page, count=1); opinion 10 present -> resume skips it
    page = {"results": [_cluster(1, subs=["/o/10/"])], "next": None, "count": 1}
    _patch_urlopen(monkeypatch, [page])
    manifest = extract.extract(
        "1815-01-01", "1815-12-31", "tok", str(cdir), str(odir), str(tmp_path / "m.json")
    )
    assert manifest["n_clusters"] == 1
    assert manifest["n_opinions"] == 1  # from the mirror, not the (empty) resume fetch
    assert manifest["opinions_fetched_this_run"] == 0
