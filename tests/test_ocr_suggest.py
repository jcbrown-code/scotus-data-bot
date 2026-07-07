"""Tests for src.ocr_suggest (deterministic, offline).

The frequency lexicon is injected as a fake, so these need neither wordfreq nor the network."""

from src import ocr_suggest

# A tiny fake frequency table (zipf-like). Everything else is treated as frequency 0 (unknown).
_FREQ = {
    "the": 7.5,
    "justice": 5.0,
    "such": 5.5,
    "said": 6.0,
    "side": 5.2,
    "constitution": 5.0,
    "favour": 4.3,  # common British spelling — must be left alone
    "savour": 2.8,  # ...even though a transform reaches it
    "bah": 4.0,  # crafted ambiguity below
    "hah": 3.5,
}


def freq(word):
    return _FREQ.get(word, 0.0)


def test_candidates_cover_both_transforms():
    cands = dict(ocr_suggest._candidates("juftice"))
    assert cands.get("justice") == "long-s"
    cands = dict(ocr_suggest._candidates("tbe"))
    assert cands.get("the") == "h->b"


def test_candidates_use_position_subsets():
    # 'himfelf' -> 'himself' requires replacing only the FIRST f, not both
    assert "himself" in dict(ocr_suggest._candidates("himfelf"))


def test_suggest_confident_long_s():
    s = ocr_suggest.suggest("juftice", freq=freq)
    assert s == {"suggestion": "justice", "rule": "long-s", "n_candidates": 1, "alternatives": ""}


def test_suggest_rejects_common_original():
    # favour(4.3) -> savour(2.8): original is MORE common, so no suggestion (the key guard)
    assert ocr_suggest.suggest("favour", freq=freq) is None


def test_suggest_none_when_no_transform_helps():
    assert ocr_suggest.suggest("hello", freq=freq) is None
    assert ocr_suggest.suggest("the", freq=freq) is None  # already common


def test_suggest_ranks_and_lists_alternatives():
    # 'bab' -> {hab(0), bah(4.0), hah(3.5)}; two qualify, best is the most common
    s = ocr_suggest.suggest("bab", freq=freq)
    assert s["suggestion"] == "bah" and s["n_candidates"] == 2
    assert s["alternatives"] == "hah"


def test_suggest_text_yields_offsets_in_order():
    out = list(ocr_suggest.suggest_text("the juftice conftitution favour", freq=freq))
    # 'the' common, 'favour' common -> both skipped; only the two long-s errors surface
    assert [o["original"] for o in out] == ["juftice", "conftitution"]
    assert out[0]["char_offset"] == 4  # position of 'juftice'
    assert out[0]["char_offset"] < out[1]["char_offset"]
