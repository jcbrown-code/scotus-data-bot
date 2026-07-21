"""Unit tests for the pure cleaning/transform logic (no network, no DB)."""

import pytest

from src import transform_legacy as t


@pytest.mark.parametrize(
    "citations, expected",
    [
        # the structured U.S. cite is chosen; a 'U.S. LEXIS' entry never yields a volume
        (
            [
                {"reporter": "U.S. LEXIS", "volume": "1810", "page": "350"},
                {"reporter": "L. Ed.", "volume": "3", "page": "240"},
                {"reporter": "U.S.", "volume": "10", "page": "332"},
            ],
            (10, "10 U.S. 332"),
        ),
        ([{"reporter": "L. Ed.", "volume": "1", "page": "1"}], (None, "")),  # no U.S. reporter
        ([], (None, "")),  # no citations at all
    ],
)
def test_us_cite(citations, expected):
    assert t.us_cite(citations) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2 U.S. 112", (2, "112")),
        ("17 U.S. 316", (17, "316")),
        ("", (None, None)),
        ("not a cite", (None, None)),
    ],
)
def test_parse_us_cite(text, expected):
    assert t.parse_us_cite(text) == expected


@pytest.mark.parametrize(
    "a, b",
    [
        ("The New-York", "The New York"),  # hyphen + article
        ("M'Culloch v. Maryland", "McCulloch v. Maryland"),  # m' -> mc
    ],
)
def test_norm_name_variants_match(a, b):
    assert t.norm_name(a) == t.norm_name(b)


@pytest.mark.parametrize(
    "raw_html, expected",
    [
        ("<p>Hello &amp; <b>world</b></p>", "Hello & world"),
        ("<div>a<br>b</div>", "a b"),
    ],
)
def test_strip_html(raw_html, expected):
    assert t.strip_html(raw_html) == expected


@pytest.mark.parametrize(
    "volume, scdb_id, expected_bucket",
    [
        ("6", "", "KEEP"),  # vol >= 5 (Cranch)
        ("2", "1793-001", "KEEP"),  # Dallas w/ scdb id
        ("2", "", "REVIEW"),  # vol < 5, no scdb id
    ],
)
def test_classify_filter_rule(volume, scdb_id, expected_bucket):
    raw = [
        {
            "id": 1,
            "case_name": "Some Case",
            "date_filed": "1800-01-01",
            "citations": [{"reporter": "U.S.", "volume": volume, "page": "1"}],
            "scdb_id": scdb_id,
            "source": "L",
            "citation_count": 0,
        }
    ]
    assert t.classify(raw)[0]["bucket"] == expected_bucket


def test_best_text_prefers_html_with_citations():
    src, raw = t.best_text({"html_with_citations": "<p>rich</p>", "plain_text": "plain"})
    assert (src, raw) == ("html_with_citations", "<p>rich</p>")


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


def test_assign_dedup_marks_roles(sample_raw_clusters):
    """assign_dedup dedups within each bucket and tags canonical/duplicate + dup_of."""
    roles = {
        r["cluster_id"]: (r["bucket"], r["dedup_role"], r["dup_of"])
        for r in t.assign_dedup(t.classify(sample_raw_clusters))
    }
    assert roles[10] == ("KEEP", "canonical", "")
    assert roles[8403137] == ("KEEP", "duplicate", 10)  # Harvard copy collapsed
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
