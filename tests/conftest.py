"""Shared pytest fixtures for the test suite."""

import os
import sqlite3

import pytest

from config import settings
from src import load


@pytest.fixture(scope="session")
def db(tmp_path_factory):
    """Build the shipped SQLite DB once from the on-disk staging DB; yield a connection.

    Skips (rather than fails) when the staging DB or a required derived table is
    absent — the data-quality suite requires a full pipeline run; unit tests do not."""
    if not os.path.exists(settings.STAGING_DB_PATH):
        pytest.skip("staging DB missing; run the pipeline stages first")
    path = str(tmp_path_factory.mktemp("db") / "scotus.sqlite")
    try:
        load.build_db(settings.STAGING_DB_PATH, path)
    except RuntimeError as error:
        pytest.skip(str(error))
    conn = sqlite3.connect(path)
    yield conn
    conn.close()
