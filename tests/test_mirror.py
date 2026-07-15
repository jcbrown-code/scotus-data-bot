"""Tests for src.mirror — offline; the deterministic archive + checksum ledger + fetch/verify.

These guard the hybrid storage's immutability (per-record + archive hashes catch any change) and
the reproducible packaging (byte-identical archive -> stable hash)."""

import gzip
import io
import os
import shutil
import tarfile

import pytest

from src import mirror


def _mirror(root, clusters=None, opinions=None):
    """Create a tiny raw mirror under root and return its path."""
    clusters = clusters or {1: '{"id": 1}'}
    opinions = opinions or {10: '{"id": 10}'}
    os.makedirs(os.path.join(root, "clusters"))
    os.makedirs(os.path.join(root, "opinions"))
    for i, body in clusters.items():
        with open(os.path.join(root, "clusters", f"{i}.json"), "w") as f:
            f.write(body)
    for i, body in opinions.items():
        with open(os.path.join(root, "opinions", f"{i}.json"), "w") as f:
            f.write(body)
    return root


def test_build_archive_deterministic(tmp_path):
    raw = _mirror(str(tmp_path / "raw"))
    a1, a2 = str(tmp_path / "a1.tgz"), str(tmp_path / "a2.tgz")
    h1 = mirror.build_archive(raw, a1)
    h2 = mirror.build_archive(raw, a2)
    assert h1 == h2  # stable hash
    with open(a1, "rb") as f1, open(a2, "rb") as f2:
        assert f1.read() == f2.read()  # byte-identical


def test_checksums_written_and_verify_ok(tmp_path):
    raw = _mirror(str(tmp_path / "raw"))
    archive = str(tmp_path / "m.tgz")
    mirror.build_archive(raw, archive)
    ck = str(tmp_path / "CHECKSUMS.sha256")
    mirror.write_checksums(raw, archive, ck)
    ledger = mirror.read_checksums(ck)
    assert "clusters/1.json" in ledger and "opinions/10.json" in ledger
    assert mirror.ARCHIVE_NAME in ledger
    assert mirror.verify_records(raw, ck) == []


def test_verify_detects_change_missing_and_extra(tmp_path):
    raw = _mirror(str(tmp_path / "raw"))
    archive = str(tmp_path / "m.tgz")
    mirror.build_archive(raw, archive)
    ck = str(tmp_path / "CHECKSUMS.sha256")
    mirror.write_checksums(raw, archive, ck)

    with open(os.path.join(raw, "clusters", "1.json"), "w") as f:
        f.write('{"id": 1, "tampered": true}')
    assert dict(mirror.verify_records(raw, ck)).get("clusters/1.json") == "hash mismatch"

    with open(os.path.join(raw, "opinions", "11.json"), "w") as f:
        f.write("{}")
    assert dict(mirror.verify_records(raw, ck)).get("opinions/11.json") == "not in ledger"

    os.remove(os.path.join(raw, "opinions", "10.json"))
    assert dict(mirror.verify_records(raw, ck)).get("opinions/10.json") == "missing"


def test_fetch_mirror_verifies_and_rejects_tamper(tmp_path, monkeypatch):
    src = _mirror(str(tmp_path / "src"))
    asset = str(tmp_path / "asset.tgz")
    mirror.build_archive(src, asset)
    ck = str(tmp_path / "CHECKSUMS.sha256")
    mirror.write_checksums(src, asset, ck)

    monkeypatch.setattr(mirror, "download_file", lambda url, dest: shutil.copy(asset, dest))
    dst = str(tmp_path / "dst")
    os.makedirs(dst)
    n = mirror.fetch_mirror("http://x", dst, ck, str(tmp_path / "dl.tgz"))
    assert n == 2  # 1 cluster + 1 opinion
    assert mirror.verify_records(dst, ck) == []

    bad = str(tmp_path / "bad.tgz")
    with open(bad, "wb") as f:
        f.write(b"corrupt")
    monkeypatch.setattr(mirror, "download_file", lambda url, dest: shutil.copy(bad, dest))
    dst2 = str(tmp_path / "dst2")
    os.makedirs(dst2)
    with pytest.raises(RuntimeError, match="archive hash mismatch"):
        mirror.fetch_mirror("http://x", dst2, ck, str(tmp_path / "dl2.tgz"))


def test_unpack_skips_unsafe_members(tmp_path):
    evil = str(tmp_path / "evil.tgz")
    with open(evil, "wb") as out:
        with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w") as tar:
                for name in ("clusters/1.json", "../evil.json", "/abs.json"):
                    data = b"{}"
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
    dst = str(tmp_path / "dst")
    os.makedirs(dst)
    mirror.unpack_archive(evil, dst)
    assert os.path.exists(os.path.join(dst, "clusters", "1.json"))
    assert not os.path.exists(os.path.join(str(tmp_path), "evil.json"))
    assert not os.path.exists("/abs.json")
