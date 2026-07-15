"""Transform stage: filter, de-duplicate, parse citations, strip opinion HTML.

Pure functions (stdlib only) — the validated cleaning logic, importable by tests.

SCOTUS filter:  KEEP if scdb_id present OR U.S. reporter volume >= 5 (Cranch/Wheaton
                onward = pure SCOTUS); else REVIEW (PA / circuit reprints).
De-duplication: CourtListener's 2025 Harvard CAP import (source "U") left many early
                cases with an unmerged duplicate cluster. Collapse same-case clusters
                (union-find) by (a) identical normalized-name + year, or (b) identical
                U.S. citation + name-token overlap >= 0.5. Keep the best record.
"""

import html
import re
from collections import defaultdict

# ---- citation helpers ------------------------------------------------------


def us_cite(citations):
    """Return (volume:int|None, 'V U.S. P') from the U.S. reporter citation object."""
    for c in citations or []:
        if (c.get("reporter") or "").strip() == "U.S.":
            try:
                vol = int(str(c.get("volume")).strip())
            except (TypeError, ValueError):
                vol = None
            return vol, f"{c.get('volume')} U.S. {c.get('page')}"
    return None, ""


_US_CITE_RE = re.compile(r"^\s*(\d+)\s+U\.S\.\s+(\S+)")


def parse_us_cite(us_cite_str):
    """Split a 'V U.S. P' string into (volume:int|None, page:str|None)."""
    m = _US_CITE_RE.match(us_cite_str or "")
    if not m:
        return None, None
    try:
        return int(m.group(1)), m.group(2)
    except ValueError:
        return None, m.group(2)


# ---- case-identity helpers (de-dup) ----------------------------------------


def norm_name(n):
    """Normalize a case name so a Harvard duplicate matches its canonical record."""
    n = (n or "").lower().replace("the ", " ").replace("trustees of ", " ")
    n = n.replace("m'", "mc").replace("'", "")
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    return re.sub(r"\s+", " ", n).strip()[:22]


_STOP = {"v", "the", "of", "a", "and", "et", "al"}


def _toks(n):
    return set(w for w in re.sub(r"[^a-z0-9 ]", " ", (n or "").lower()).split() if w not in _STOP)


def quality(x):
    """Rank records of the same case; the max is the canonical one to keep."""
    return (
        1 if x["scdb_id"] else 0,  # prefer SCDB-catalogued record
        0 if x["source"] == "U" else 1,  # prefer merged / non-Harvard-only
        x["citation_count"],  # prefer more-cited
        -int(x["cluster_id"]),
    )  # prefer oldest (lowest) id


def dedup(records):
    """Collapse duplicate clusters via two signals, transitively (union-find):
       (a) identical (normalized name, year);
       (b) identical U.S. citation AND name-token overlap >= 0.5.
    Returns (canonical_id_set, {duplicate_id: canonical_id})."""
    parent = {r["cluster_id"]: r["cluster_id"] for r in records}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    by_ny = defaultdict(list)
    for r in records:
        by_ny[(norm_name(r["caseName"]), r["dateFiled"][:4])].append(r)
    for g in by_ny.values():
        for r in g[1:]:
            union(g[0]["cluster_id"], r["cluster_id"])

    by_cite = defaultdict(list)
    for r in records:
        if r["us_cite"]:
            by_cite[r["us_cite"]].append(r)
    for g in by_cite.values():
        for i in range(len(g)):
            ti = _toks(g[i]["caseName"])
            for j in range(i + 1, len(g)):
                tj = _toks(g[j]["caseName"])
                if ti and tj and len(ti & tj) / len(ti | tj) >= 0.5:
                    union(g[i]["cluster_id"], g[j]["cluster_id"])

    comp = defaultdict(list)
    for r in records:
        comp[find(r["cluster_id"])].append(r)
    canonical, dup_of = set(), {}
    for members in comp.values():
        members.sort(key=quality, reverse=True)
        canonical.add(members[0]["cluster_id"])
        for d in members[1:]:
            dup_of[d["cluster_id"]] = members[0]["cluster_id"]
    return canonical, dup_of


# ---- record builders -------------------------------------------------------


def classify(raw_rows):
    """Raw cluster dicts (clusters endpoint) -> records with the KEEP/REVIEW bucket."""
    recs = []
    for r in raw_rows:
        vol, cite = us_cite(r.get("citations"))
        scdb = (r.get("scdb_id") or "").strip()
        recs.append(
            {
                "cluster_id": r["id"],
                "caseName": r.get("case_name", ""),
                "us_cite": cite,
                "volume": vol if vol is not None else "",
                "scdb_id": scdb,
                "source": r.get("source", ""),
                "citation_count": r.get("citation_count") or 0,
                "precedential_status": r.get("precedential_status", ""),
                "dateFiled": r.get("date_filed", "") or "",
                "bucket": "KEEP" if (scdb or (vol is not None and vol >= 5)) else "REVIEW",
            }
        )
    return recs


def assign_dedup(recs):
    """Annotate each record with dedup_role + dup_of, de-duping within each bucket
    (so a KEEP case is never merged into a REVIEW one)."""
    canonical, dup_of = set(), {}
    for bucket in ("KEEP", "REVIEW"):
        c, d = dedup([x for x in recs if x["bucket"] == bucket])
        canonical |= c
        dup_of.update(d)
    for x in recs:
        x["dedup_role"] = "canonical" if x["cluster_id"] in canonical else "duplicate"
        x["dup_of"] = dup_of.get(x["cluster_id"], "")
    return recs


# ---- opinion text ----------------------------------------------------------

# Preference order: html_with_citations is the most complete (e.g. McCulloch has text
# ONLY there), then plain_text, xml_harvard, html.
TEXT_FIELDS = ["html_with_citations", "plain_text", "xml_harvard", "html"]


def best_text(op):
    """Pick the richest populated text field from an opinion API object."""
    for f in TEXT_FIELDS:
        v = op.get(f)
        if v and v.strip():
            return f, v
    return None, ""


def strip_html(s):
    """Render HTML/XML opinion markup to readable plain text."""
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", s).strip()


def opinion_record(api_op):
    """Build a stored opinion record (raw + stripped text) from an opinion API object."""
    src, raw = best_text(api_op)
    return {
        "opinion_id": api_op["id"],
        "type": api_op.get("type"),
        "author": api_op.get("author_str") or "",
        "ocr": api_op.get("extracted_by_ocr"),
        "text_source": src,
        "char_count": len(raw),
        "raw": raw,
        "text": strip_html(raw) if src else "",
    }
