import os

import pytest

from config import settings
from src import load


@pytest.fixture
def sample_raw_clusters():
    """Three raw clusters (clusters-endpoint shape): a canonical KEEP, its unmerged
    Harvard 'U' duplicate (same case), and a REVIEW case. Shared across tests."""
    return [
        {
            "id": 10,
            "case_name": "Lindo v. Gardner",
            "date_filed": "1803-02-28",
            "citations": [{"reporter": "U.S.", "volume": "5", "page": "343"}],
            "scdb_id": "1803-014",
            "source": "L",
            "citation_count": 5,
        },
        {
            "id": 8403137,
            "case_name": "Lindo v. Gardner",
            "date_filed": "1803-02-15",
            "citations": [{"reporter": "U.S.", "volume": "5", "page": "343"}],
            "scdb_id": "",
            "source": "U",
            "citation_count": 0,
        },
        {
            "id": 3,
            "case_name": "Respublica v. X",
            "date_filed": "1790-08-01",
            "citations": [{"reporter": "U.S.", "volume": "2", "page": "55"}],
            "scdb_id": "",
            "source": "L",
            "citation_count": 0,
        },
    ]


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
