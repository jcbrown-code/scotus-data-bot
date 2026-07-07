"""Deterministic, high-fidelity cleaning of opinion text (see docs/clean-text-design.md).

`clean_opinion(raw_html)` renders an opinion's stored `raw_html` (CourtListener
`html_with_citations`, in either the div-HTML or the Harvard-XML dialect) into a canonical
`clean_text`, and returns alongside it a page-break map and an OCR-suspect locator. It is:

- deterministic — pure functions, same input -> same output; no LLM/statistical passes;
- conservative — the ONLY content dropped is star-pagination page markers (captured instead as page
  breaks): the structural `<span class="star-pagination">` / `<page-number>` forms, plus the
  *bracketed* inline text form (`[*626`, `*625]`). Bare unbracketed `*54` and all other original
  content — footnote bodies and their inline ref markers, the case caption, citations — is kept;
- non-destructive — `raw_html` + `plain_text` are untouched; this is a derived column;
- no OCR correction — OCR-suspect tokens are LOCATED (`ocr_suspect`), never rewritten.

Normalization: `\r`->`\n`, control chars stripped (except `\n`/`\t`), whitespace collapsed, Unicode
NFC. No ASCII folding in the canonical column (that lives in the FTS tokenizer instead). The `■`
OCR "unreadable character" glyph is KEPT (it marks missing text) and flagged in `ocr_suspect`.
"""

import json
import re
import unicodedata
from html.parser import HTMLParser

# Bump when the cleaning logic changes: stored in opinions.clean_version so a rebuild is detectable
# and char_offsets in page_breaks are always interpreted against the matching text.
CLEAN_VERSION = 1

# Block-level tags that should produce a line break in the rendered text.
_BLOCK = {
    "p",
    "div",
    "br",
    "center",
    "blockquote",
    "li",
    "tr",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "opinion",
    "author",
    "parties",
    "footnote",
    "headnotes",
    "syllabus",
    "page",
}

# Private-use sentinel wrapping the index of a structural page-break marker in the render buffer.
# PUA chars survive whitespace/NFC normalization untouched, then are resolved to offsets at the end
_S0, _S1 = "", ""

# A page break is EITHER a structural sentinel (star-pagination span / page-number element) OR a
# *bracketed* inline marker (`[*626`, `*625]`). Bare unbracketed `*54` is deliberately NOT matched:
# too ambiguous (footnote asterisk vs. real content) — so it is preserved verbatim.
_BREAK_RE = re.compile(_S0 + r"(?P<sidx>\d+)" + _S1 + r"|\[\*(?P<opn>\d+)\]?|\*(?P<cls>\d+)\]")

# Curated, whole-word OCR-suspect tokens (precision over recall — see design doc §5). Two profiled
# classes: long-s mis-OCR'd as 'f' (juſtice -> juftice) and h->b (the -> tbe). Deliberately a
# high-confidence starter set; the goal is to LOCATE representative spans, not catch every error.
_OCR_TOKENS = frozenset(
    {
        # long-s ('f' where 's' belongs) — unambiguous non-words
        "juftice",
        "juftices",
        "firft",
        "muft",
        "fhall",
        "fuch",
        "thefe",
        "thofe",
        "becaufe",
        "caufe",
        "conftitution",
        "prefent",
        "reafon",
        "reafons",
        "perfon",
        "perfons",
        "fervice",
        "againft",
        "moft",
        "juft",
        "laft",
        "poffeffion",
        "poffeffed",
        "courfe",
        "confent",
        "defendant",
        "defendants",
        "faid",
        "fame",
        "fome",
        "fubject",
        "fuit",
        "ftate",
        "ftates",
        "fupreme",
        "houfe",
        "ufe",
        "ufed",
        "purfuant",
        "purpofe",
        "increafe",
        "expreffed",
        # h->b confusion — unambiguous non-words
        "tbe",
        "tbat",
        "tbis",
        "tbey",
        "tbem",
        "wbich",
        "wben",
        "wbere",
        "witb",
        "bad",
        "bave",
        "bim",
        "bis",
        "ber",
        "bere",
        "tbeir",
        "tbere",
        "otber",
        "wbo",
    }
)
_TOKEN_RE = re.compile(r"[A-Za-z]+")


class _Renderer(HTMLParser):
    """Render opinion markup to text, suppressing structural star-pagination / page-number markers
    and recording a sentinel (+ its page label) at each one's position."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.buf = []
        self.labels = []  # page_label per structural break, in document order
        self._pb_tag = None  # tag name of the page-break element currently open (else None)
        self._pb_text = ""  # its text content, to parse a label from when the attr is absent

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if (tag == "span" and "star-pagination" in classes) or tag == "page-number":
            self.buf.append(f"{_S0}{len(self.labels)}{_S1}")
            self.labels.append(a.get("label"))  # may be None -> filled from text at end
            self._pb_tag = tag
            self._pb_text = ""
            return
        if tag in _BLOCK:
            self.buf.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCK:
            self.buf.append("\n")

    def handle_endtag(self, tag):
        if self._pb_tag is not None and tag == self._pb_tag:
            if self.labels[-1] is None:  # no label attr: parse from the '*NNN' text
                self.labels[-1] = self._pb_text.lstrip("*").strip() or None
            self._pb_tag = None
            return
        if tag in _BLOCK:
            self.buf.append("\n")

    def handle_data(self, data):
        if self._pb_tag is not None:
            self._pb_text += data  # captured for the label, not emitted
        else:
            self.buf.append(data)


def _normalize(s):
    """Whitespace/control/Unicode normalization that preserves the page-break sentinels."""
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # strip control chars except \n and \t (keeps the private-use sentinels, which aren't controls)
    s = "".join(ch for ch in s if ch in "\n\t" or unicodedata.category(ch) != "Cc")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)  # trim spaces around newlines
    s = re.sub(r"\n{3,}", "\n\n", s)  # collapse blank runs to one blank line
    s = unicodedata.normalize("NFC", s)
    return s.strip()


def _find_ocr_suspects(text):
    """Locate OCR-suspect spots: curated whole-word tokens plus every ``■`` unreadable-char glyph.
    Returns [{offset, token}] in document order (``■`` kept in clean_text; also flagged here)."""
    hits = [
        {"offset": m.start(), "token": m.group(0)}
        for m in _TOKEN_RE.finditer(text)
        if m.group(0).lower() in _OCR_TOKENS
    ]
    hits += [{"offset": m.start(), "token": "\u25a0"} for m in re.finditer("\u25a0", text)]
    hits.sort(key=lambda h: h["offset"])
    return hits


def clean_opinion(raw_html):
    """Return (clean_text, page_breaks, ocr_suspect) for one opinion's raw_html.

    page_breaks: list of {ordinal, page_label, char_offset, anchor}; char_offset indexes into the
    returned clean_text (where the reporter's page begins). ocr_suspect: list of {offset, token}.
    Both are ordered by position."""
    if not raw_html or not raw_html.strip():
        return "", [], []

    r = _Renderer()
    r.feed(raw_html)
    r.close()
    normalized = _normalize("".join(r.buf))

    # Single pass over both marker kinds: build clean_text with markers removed, noting each
    # break's position (= where the following page text begins).
    out, raw_breaks, last = [], [], 0
    grown = 0
    for m in _BREAK_RE.finditer(normalized):
        seg = normalized[last : m.start()]
        out.append(seg)
        grown += len(seg)
        if m.group("sidx") is not None:
            label = r.labels[int(m.group("sidx"))]
        else:
            label = m.group("opn") or m.group("cls")
        raw_breaks.append((label, grown))
        last = m.end()
    out.append(normalized[last:])
    clean = "".join(out)

    breaks = []
    for ordinal, (label, off) in enumerate(raw_breaks, 1):
        while off < len(clean) and clean[off] in " \n\t":  # advance to the page's first real char
            off += 1
        anchor = " ".join(clean[off : off + 80].split()[:6])
        breaks.append(
            {"ordinal": ordinal, "page_label": label, "char_offset": off, "anchor": anchor}
        )

    return clean, breaks, _find_ocr_suspects(clean)


def ocr_suspect_json(hits):
    """Serialize ocr_suspect hits for the opinions.ocr_suspect column; None if empty."""
    if not hits:
        return None
    return json.dumps({"count": len(hits), "hits": hits}, separators=(",", ":"))
