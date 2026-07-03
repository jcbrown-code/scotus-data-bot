import os

import pytest

from config import settings
from src import load


@pytest.fixture(scope="session")
def db(tmp_path_factory):
    """Build the SQLite DB once from the on-disk staging files; yield a connection.

    Skips (rather than fails) if the staging data hasn't been generated yet — the
    data-quality suite requires a prior pipeline run; the unit tests do not."""
    for p in (settings.ALL_CLUSTERS_CSV, settings.RAW_CLUSTERS, settings.FULLTEXT_DIR):
        if not os.path.exists(p):
            pytest.skip(f"staging data missing ({p}); run `python -m src.pipeline` first")
    path = str(tmp_path_factory.mktemp("db") / "test.sqlite")
    conn, _ = load.build_db("sqlite", path=path)
    yield conn
    conn.close()
