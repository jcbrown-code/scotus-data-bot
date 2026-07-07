"""OCR correction suggester — stage 2 of the suggest->review->apply pipeline (design doc §7).

Deterministic candidate generation (OCR-inverse transforms) ranked/gated by a frequency lexicon.
Nothing is corrected here: this only *proposes* fixes for human review (`ocr_corrections.csv`).

Correction-time tool: the frequency lexicon (`wordfreq`) is an optional dependency (the
`[correction]` extra) and is lazy-imported, so importing this module never requires it. `suggest`
takes an injectable `freq` function, so tests are deterministic and need no dependency.
"""

import itertools
import re

# (char_to_replace, replacement, rule_name) — the OCR-inverse transforms for v1. Both are
# single-char, same-length substitutions, so applying them preserves page_breaks char offsets.
TRANSFORMS = [("f", "s", "long-s"), ("b", "h", "h->b")]

# A candidate correction qualifies only when it is much MORE COMMON than the original word:
# >= GAIN zipf points higher AND at least MIN_CAND in absolute terms. This is what lets the
# frequency lexicon (unlike a binary wordlist) leave British/archaic spellings and inflections
# alone — favour(4.3)->savour(2.8) is rejected because the original is already common.
GAIN = 2.0
MIN_CAND = 3.0
MAX_SUBS = 5  # cap transform combinatorics on pathological tokens

_TOKEN_RE = re.compile(r"[A-Za-z]{3,}")


def _wordfreq_zipf(word):
    """Default frequency source: English zipf frequency from wordfreq (lazy-imported)."""
    from wordfreq import zipf_frequency

    return zipf_frequency(word, "en")


def _candidates(token):
    """Yield (candidate_lower, rule) from OCR-inverse transforms over subsets of char positions.

    Subsets (not just replace-all) because a word can mix a long-s error with a real 'f':
    'himfelf' -> 'himself' replaces only the first f."""
    low = token.lower()
    seen = set()
    for a, b, rule in TRANSFORMS:
        pos = [i for i, c in enumerate(low) if c == a]
        if not pos or len(pos) > MAX_SUBS:
            continue
        for r in range(1, len(pos) + 1):
            for combo in itertools.combinations(pos, r):
                chars = list(low)
                for i in combo:
                    chars[i] = b
                cand = "".join(chars)
                if cand != low and cand not in seen:
                    seen.add(cand)
                    yield cand, rule


def suggest(token, freq=_wordfreq_zipf):
    """Return {suggestion, rule, n_candidates, alternatives} for one token, or None if there is no
    confident correction. `suggestion` is lowercase; apply restores each occurrence's case."""
    f0 = freq(token.lower())
    quals = []
    for cand, rule in _candidates(token):
        fc = freq(cand)
        if fc >= MIN_CAND and fc - f0 >= GAIN:
            quals.append((cand, rule, fc))
    if not quals:
        return None
    quals.sort(key=lambda x: (-x[2], x[0]))  # most common first (deterministic tiebreak)
    best, rule, _ = quals[0]
    return {
        "suggestion": best,
        "rule": rule,
        "n_candidates": len(quals),
        "alternatives": ";".join(c for c, _, _ in quals[1:]),
    }


def suggest_text(clean_text, freq=_wordfreq_zipf):
    """Yield {char_offset, original, suggestion, rule, n_candidates, alternatives} for each
    correctable token in `clean_text`, in document order."""
    for m in _TOKEN_RE.finditer(clean_text):
        s = suggest(m.group(0), freq=freq)
        if s:
            yield {"char_offset": m.start(), "original": m.group(0), **s}
