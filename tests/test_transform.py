"""Unit tests for the pure cleaning/transform logic (no network, no DB)."""

from src import transform as t


def test_us_cite_ignores_lexis():
    """The structured U.S. cite is chosen; a 'U.S. LEXIS' entry never yields a volume."""
    cites = [
        {"reporter": "U.S. LEXIS", "volume": "1810", "page": "350"},
        {"reporter": "L. Ed.", "volume": "3", "page": "240"},
        {"reporter": "U.S.", "volume": "10", "page": "332"},
    ]
    vol, cite = t.us_cite(cites)
    assert vol == 10
    assert cite == "10 U.S. 332"


def test_parse_us_cite():
    assert t.parse_us_cite("2 U.S. 112") == (2, "112")
    assert t.parse_us_cite("17 U.S. 316") == (17, "316")
    assert t.parse_us_cite("") == (None, None)


def test_norm_name_variants_match():
    assert t.norm_name("The New-York") == t.norm_name("The New York")
    assert t.norm_name("M'Culloch v. Maryland").startswith("mcculloch")


def test_dedup_collapses_harvard_duplicate():
    """A canonical (Lawbox, has scdb) + an unmerged Harvard 'U' copy collapse to one."""
    recs = [
        {
            "cluster_id": 100,
            "caseName": "Lindo v. Gardner",
            "dateFiled": "1803-02-28",
            "us_cite": "5 U.S. 343",
            "scdb_id": "1803-014",
            "source": "L",
            "citation_count": 5,
        },
        {
            "cluster_id": 8403137,
            "caseName": "Lindo v. Gardner",
            "dateFiled": "1803-02-15",
            "us_cite": "5 U.S. 343",
            "scdb_id": "",
            "source": "U",
            "citation_count": 0,
        },
    ]
    canonical, dup_of = t.dedup(recs)
    assert canonical == {100}  # the scdb-bearing record wins
    assert dup_of == {8403137: 100}


def test_dedup_keeps_companion_cases():
    """Distinct cases that merely share a starting page (zero name overlap) stay separate."""
    recs = [
        {
            "cluster_id": 1,
            "caseName": "West v. Barnes",
            "dateFiled": "1792-02-14",
            "us_cite": "2 U.S. 401",
            "scdb_id": "1791-001",
            "source": "LR",
            "citation_count": 1,
        },
        {
            "cluster_id": 2,
            "caseName": "Oswald v. New York",
            "dateFiled": "1792-02-14",
            "us_cite": "2 U.S. 401",
            "scdb_id": "1792-001",
            "source": "LR",
            "citation_count": 1,
        },
    ]
    canonical, dup_of = t.dedup(recs)
    assert canonical == {1, 2}
    assert dup_of == {}


def test_strip_html():
    assert t.strip_html("<p>Hello &amp; <b>world</b></p>") == "Hello & world"


def test_best_text_prefers_html_with_citations():
    op = {"html_with_citations": "<p>rich</p>", "plain_text": "plain"}
    src, raw = t.best_text(op)
    assert src == "html_with_citations"
    assert raw == "<p>rich</p>"


def test_classify_filter_rule():
    raw = [
        {
            "id": 1,
            "case_name": "Cranch case",
            "date_filed": "1805-02-01",
            "citations": [{"reporter": "U.S.", "volume": "6", "page": "1"}],
            "scdb_id": "",
            "source": "L",
            "citation_count": 0,
        },  # vol>=5 -> KEEP
        {
            "id": 2,
            "case_name": "Dallas SCOTUS",
            "date_filed": "1793-02-19",
            "citations": [{"reporter": "U.S.", "volume": "2", "page": "419"}],
            "scdb_id": "1793-001",
            "source": "L",
            "citation_count": 0,
        },  # scdb -> KEEP
        {
            "id": 3,
            "case_name": "Respublica v. X",
            "date_filed": "1790-08-01",
            "citations": [{"reporter": "U.S.", "volume": "2", "page": "55"}],
            "scdb_id": "",
            "source": "L",
            "citation_count": 0,
        },  # vol<5, no scdb -> REVIEW
    ]
    by_id = {r["cluster_id"]: r["bucket"] for r in t.classify(raw)}
    assert by_id == {1: "KEEP", 2: "KEEP", 3: "REVIEW"}


def test_us_cite_none_when_no_us_reporter():
    assert t.us_cite([]) == (None, "")
    assert t.us_cite([{"reporter": "L. Ed.", "volume": "1", "page": "1"}]) == (None, "")


def test_assign_dedup_marks_roles():
    """assign_dedup dedups within each bucket and tags canonical/duplicate + dup_of."""
    raw = [
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
    roles = {
        r["cluster_id"]: (r["bucket"], r["dedup_role"], r["dup_of"])
        for r in t.assign_dedup(t.classify(raw))
    }
    assert roles[10] == ("KEEP", "canonical", "")
    assert roles[8403137] == ("KEEP", "duplicate", 10)
    assert roles[3] == ("REVIEW", "canonical", "")


def test_opinion_record():
    op = {
        "id": 5,
        "type": "010combined",
        "author_str": "",
        "extracted_by_ocr": False,
        "html_with_citations": "<p>Hi &amp; bye</p>",
    }
    rec = t.opinion_record(op)
    assert rec["opinion_id"] == 5
    assert rec["text_source"] == "html_with_citations"
    assert rec["char_count"] == len("<p>Hi &amp; bye</p>")
    assert rec["text"] == "Hi & bye"
