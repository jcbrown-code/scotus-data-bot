"""Property-based tests (Hypothesis) for the pure, deterministic functions.

These compensate for the untyped codebase: they assert *invariants* across thousands of generated
inputs — things a type checker cannot express (idempotence, offset bounds, dedup partitioning) and
that enumerated example tests miss. Everything here is offline and deterministic."""

import re

from hypothesis import given, settings
from hypothesis import strategies as st

from src import clean
from src import transform as t

# ---- clean.clean_opinion -----------------------------------------------------

# Fragments that assemble opinion-like markup, so generated inputs exercise the marker-capture
# paths (structural spans, page-number elements, bracketed inline markers) as well as plain text.
_word = st.text(alphabet="abcdefghijklmnop STU", min_size=1, max_size=6)
_fragment = st.one_of(
    _word,
    st.builds(
        lambda n: f'<span class="star-pagination" label="{n}">*{n}</span>', st.integers(1, 999)
    ),
    st.builds(lambda n: f'<page-number label="{n}">*{n}</page-number>', st.integers(1, 999)),
    st.builds(lambda n: f"[*{n}", st.integers(1, 999)),
    st.builds(lambda n: f"*{n}]", st.integers(1, 999)),
    st.sampled_from(["<p>", "</p>", "<br/>", "the juftice", "tbe", "■", "\r\n", "café", "&amp;"]),
)
_opinion_html = st.lists(_fragment, max_size=40).map(
    lambda parts: "<div>" + " ".join(parts) + "</div>"
)


def _assert_invariants(raw):
    ct, pbs, ocr = clean.clean_opinion(raw)
    assert isinstance(ct, str)
    # no page-break sentinels leak into the canonical text
    assert clean._S0 not in ct and clean._S1 not in ct
    # ordinals are 1..n with no gaps; offsets are valid indices, in order
    assert [p["ordinal"] for p in pbs] == list(range(1, len(pbs) + 1))
    prev = -1
    for p in pbs:
        assert 0 <= p["char_offset"] <= len(ct)
        assert p["char_offset"] >= prev  # non-decreasing (document order)
        prev = p["char_offset"]
    # every ocr-suspect hit points at exactly its token in the text
    for h in ocr:
        assert ct[h["offset"] : h["offset"] + len(h["token"])] == h["token"]
    # deterministic: identical output on a second run
    assert clean.clean_opinion(raw) == (ct, pbs, ocr)
    return ct, pbs


@given(st.text(max_size=1500))
@settings(max_examples=300, deadline=None)
def test_clean_opinion_never_crashes_and_holds_invariants(raw):
    """Arbitrary text (incl. control/PUA/unicode) must not crash and must hold the invariants."""
    _assert_invariants(raw)


@given(_opinion_html)
@settings(max_examples=300, deadline=None)
def test_clean_opinion_on_structured_markup(raw):
    ct, pbs = _assert_invariants(raw)
    # no captured page-marker form survives in the text
    assert clean._S0 not in ct
    assert not re.search(r"\[\*\d+|\*\d+\]", ct)  # bracketed inline forms fully removed
    assert 'class="star-pagination"' not in ct and "<page-number" not in ct


# ---- transform.parse_us_cite -------------------------------------------------


@given(
    st.integers(min_value=1, max_value=999999), st.from_regex(r"[0-9]{1,4}[a-z]?", fullmatch=True)
)
def test_parse_us_cite_roundtrips_volume_and_page(vol, page):
    v, p = t.parse_us_cite(f"{vol} U.S. {page}")
    assert v == vol and p == page


# ---- transform.dedup ---------------------------------------------------------

_record = st.fixed_dictionaries(
    {
        "cluster_id": st.integers(min_value=1, max_value=10_000),
        "caseName": st.text(min_size=0, max_size=18),
        "dateFiled": st.from_regex(r"1[78][0-9]{2}-[01][0-9]-[0-3][0-9]", fullmatch=True),
        "us_cite": st.sampled_from(["", "5 U.S. 1", "6 U.S. 2", "5 U.S. 137", "7 U.S. 9"]),
        "scdb_id": st.sampled_from(["", "1803-001"]),
        "source": st.sampled_from(["L", "U", "LU", "R"]),
        "citation_count": st.integers(min_value=0, max_value=50),
    }
)


@given(st.lists(_record, max_size=10, unique_by=lambda r: r["cluster_id"]))
@settings(max_examples=200, deadline=None)
def test_dedup_partitions_and_every_duplicate_resolves(recs):
    canonical, dup_of = t.dedup(recs)
    ids = {r["cluster_id"] for r in recs}
    # canonical set and duplicate keys partition all ids exactly
    assert canonical | set(dup_of) == ids
    assert canonical.isdisjoint(set(dup_of))
    # every duplicate points at a canonical record, never at itself
    for dup, canon in dup_of.items():
        assert canon in canonical and canon != dup


# ---- transform.norm_name / strip_html ---------------------------------------


@given(st.text(max_size=60))
def test_norm_name_is_bounded_stripped_and_deterministic(name):
    out = t.norm_name(name)
    assert out == t.norm_name(name)
    assert len(out) <= 22 and out == out.strip()


@given(st.text(max_size=800))
@settings(max_examples=200, deadline=None)
def test_strip_html_removes_all_tags_without_crashing(s):
    out = t.strip_html(s)
    assert isinstance(out, str)
    assert not re.search(r"<[^>]+>", out)  # no complete tag survives
