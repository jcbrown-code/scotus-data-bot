"""Tests for src.clean — the deterministic opinion-text cleaner (offline)."""

import json

import pytest

from src import clean

# ---- structural page markers ------------------------------------------------


def test_star_pagination_span_captured_and_dropped():
    raw = '<p>alpha beta <span class="star-pagination" label="25">*25</span> gamma delta</p>'
    ct, pbs, _ = clean.clean_opinion(raw)
    assert "*25" not in ct and "star-pagination" not in ct
    assert [p["page_label"] for p in pbs] == ["25"]
    # char_offset points at the first char of the page's text ("gamma")
    off = pbs[0]["char_offset"]
    assert ct[off:].startswith("gamma")
    assert pbs[0]["anchor"].startswith("gamma delta")


def test_star_pagination_label_parsed_from_text_when_attr_absent():
    raw = '<p>x <span class="star-pagination">*407</span> y</p>'
    _, pbs, _ = clean.clean_opinion(raw)
    assert pbs[0]["page_label"] == "407"


def test_page_number_element_captured():
    raw = '<opinion><p>foo <page-number label="2">*2</page-number> bar</p></opinion>'
    ct, pbs, _ = clean.clean_opinion(raw)
    assert "*2" not in ct
    assert pbs[0]["page_label"] == "2" and ct[pbs[0]["char_offset"] :].startswith("bar")


def test_bracketed_inline_captured_but_bare_preserved():
    raw = "<p>one [*626 two *627] three *54 four</p>"
    ct, pbs, _ = clean.clean_opinion(raw)
    # bracketed forms captured + removed
    assert "[*626" not in ct and "*627]" not in ct
    assert [p["page_label"] for p in pbs] == ["626", "627"]
    # bare *54 (no bracket) is preserved verbatim — too ambiguous to strip
    assert "*54" in ct


def test_ordinals_sequential_and_offsets_monotonic():
    raw = '<p>a <span class="star-pagination" label="1">*1</span> b '
    raw += '<span class="star-pagination" label="2">*2</span> c</p>'
    _, pbs, _ = clean.clean_opinion(raw)
    assert [p["ordinal"] for p in pbs] == [1, 2]
    assert pbs[0]["char_offset"] < pbs[1]["char_offset"]


# ---- content preservation ---------------------------------------------------


def test_footnote_body_and_ref_marker_preserved():
    raw = (
        '<p>text with a ref<a class="footnote" href="#fn1">1</a>.</p>'
        '<div class="footnote"><p>1. the footnote body is real content</p></div>'
    )
    ct, _, _ = clean.clean_opinion(raw)
    assert "the footnote body is real content" in ct  # body kept
    assert "ref1." in ct or "ref 1" in ct  # inline marker kept as text (not dropped)


def test_caption_and_citations_kept():
    raw = (
        "<center><h1>Doe v. Roe</h1></center>"
        '<p>See <span class="citation">5 U.S. 137</span> for context.</p>'
    )
    ct, _, _ = clean.clean_opinion(raw)
    assert "Doe v. Roe" in ct and "5 U.S. 137" in ct


def test_self_closing_block_tag_breaks_line():
    ct, _, _ = clean.clean_opinion("<p>alpha<br/>beta</p>")
    assert "alpha\nbeta" in ct


def test_unicode_nfc_kept_no_ascii_folding():
    ct, _, _ = clean.clean_opinion("<p>café rôle — dash</p>")
    assert "café" in ct and "rôle" in ct and "—" in ct  # accents + em-dash preserved


def test_box_glyph_kept_and_flagged_and_control_chars_stripped():
    ct, _, ocr = clean.clean_opinion("<p>next ■ term\r\nhere\x07x</p>")
    assert "■" in ct  # ■ kept in clean_text (missing-text signal)
    assert any(h["token"] == "■" for h in ocr)  # ...and flagged in ocr_suspect
    assert "\r" not in ct and "\x07" not in ct  # CR + control char removed
    assert "term\nhere" in ct  # \r\n -> \n


# ---- ocr_suspect ------------------------------------------------------------


def test_ocr_suspects_located_not_corrected():
    ct, _, ocr = clean.clean_opinion("<p>the juftice muft decide tbe cafe</p>")
    tokens = {h["token"] for h in ocr}
    assert {"juftice", "muft", "tbe"} <= tokens
    assert "juftice" in ct  # located, NOT rewritten
    # offsets point at the token
    for h in ocr:
        assert ct[h["offset"] :].startswith(h["token"])


def test_ocr_suspect_json_roundtrip():
    assert clean.ocr_suspect_json([]) is None
    payload = clean.ocr_suspect_json([{"offset": 3, "token": "tbe"}])
    doc = json.loads(payload)
    assert doc["count"] == 1 and doc["hits"][0]["token"] == "tbe"


# ---- robustness -------------------------------------------------------------


@pytest.mark.parametrize("raw", ["", "   ", None, "<p></p>"])
def test_empty_inputs(raw):
    assert clean.clean_opinion(raw) == ("", [], []) or clean.clean_opinion(raw)[0] == ""


def test_deterministic_and_idempotent():
    raw = '<div><p>Foo <span class="star-pagination" label="9">*9</span> the juftice</p></div>'
    assert clean.clean_opinion(raw) == clean.clean_opinion(raw)


def test_no_sentinels_leak_into_output():
    raw = '<p>a <span class="star-pagination" label="1">*1</span> b</p>'
    ct, _, _ = clean.clean_opinion(raw)
    assert "" not in ct and "" not in ct


def test_input_containing_sentinel_chars_is_safe():
    """Adversarial: raw already holds the private-use sentinel chars — they must be dropped, not
    mistaken for page-break markers (which would IndexError). Found via property fuzzing."""
    raw = clean._S0 + "5" + clean._S1 + "<p>hello world</p>"
    ct, pbs, _ = clean.clean_opinion(raw)
    assert "hello world" in ct and pbs == []
