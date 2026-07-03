"""Tests for config.settings helpers (no network)."""

import os

import pytest

from config import settings


def test_get_token_present(monkeypatch):
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", "abc123")
    assert settings.get_token() == "abc123"


def test_get_token_absent_exits(monkeypatch):
    monkeypatch.delenv("COURTLISTENER_API_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        settings.get_token()


def test_build_timestamp_override(monkeypatch):
    monkeypatch.setenv("SCOTUS_BUILD_TIMESTAMP", "2020-01-01T00:00:00Z")
    assert settings.build_timestamp() == "2020-01-01T00:00:00Z"


def test_build_timestamp_default(monkeypatch):
    monkeypatch.delenv("SCOTUS_BUILD_TIMESTAMP", raising=False)
    assert settings.build_timestamp()  # some non-empty ISO string


def test_ensure_dirs(tmp_path, monkeypatch):
    for attr in ("RAW_DIR", "PROCESSED_DIR", "DATASET_DIR", "FULLTEXT_DIR"):
        monkeypatch.setattr(settings, attr, str(tmp_path / attr))
    settings.ensure_dirs()
    for attr in ("RAW_DIR", "PROCESSED_DIR", "DATASET_DIR", "FULLTEXT_DIR"):
        assert os.path.isdir(getattr(settings, attr))
