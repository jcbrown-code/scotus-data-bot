#!/usr/bin/env python3
"""Pull a de-duplicated set of genuine SCOTUS decisions for a date range from CourtListener.

Uses the database-backed CLUSTERS endpoint (not the search endpoint, which the docs
describe as the relevance-ranked search-engine view, "not canonical"). The clusters
endpoint returns structured `citations` ({volume, reporter, page}) so we never have to
regex citation strings.

Pipeline
--------
1. FETCH  : clusters where docket__court=scotus within [after, before]  (auth required).
2. FILTER : KEEP if scdb_id present OR U.S. reporter volume >= 5 (Cranch/Wheaton onward
            = pure SCOTUS); else REVIEW (Pennsylvania / circuit reprints — see REVIEW_NOTES.md).
3. DEDUP  : CourtListener's 2025-09 Harvard CAP import (source "U") left many early cases
            with an unmerged DUPLICATE cluster. Collapse records that share a
            (normalized case name, year) identity, keeping the best one:
            prefer scdb_id, then a merged / non-"U" source, then citation_count, then
            the lowest (oldest) cluster id. Dropped duplicates are recorded, never silently lost.

Requires COURTLISTENER_API_TOKEN (the clusters endpoint needs authentication). Run via:
    agentsecrets env -- python3 scotus_ingest.py            # full pull + dedup
    agentsecrets env -- python3 scotus_ingest.py --validate # also compare to Wikipedia
    python3 scotus_ingest.py --from-cache --validate        # reprocess cache, no network

Outputs: all_clusters.csv (every cluster + bucket + dedup role), keep.csv (canonical
SCOTUS decisions), review.csv (canonical non-SCOTUS), duplicates.csv (dropped dupes).
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, Counter

CLUSTERS = "https://www.courtlistener.com/api/rest/v4/clusters/"
FIELDS = "id,case_name,date_filed,scdb_id,source,citations,citation_count,precedential_status"

# Wikipedia "Number of U.S. Supreme Court cases decided by year", 1791-1820.
WIKI = {1791:4,1792:3,1793:2,1794:1,1795:6,1796:16,1797:8,1798:5,1799:9,1800:10,
        1801:5,1802:0,1803:19,1804:14,1805:24,1806:28,1807:19,1808:32,1809:46,1810:39,
        1811:0,1812:40,1813:46,1814:48,1815:40,1816:43,1817:42,1818:38,1819:33,1820:27}


# ---- citation / identity helpers -------------------------------------------

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
    return (1 if x["scdb_id"] else 0,           # prefer SCDB-catalogued record
            0 if x["source"] == "U" else 1,      # prefer merged / non-Harvard-only
            x["citation_count"],                 # prefer more-cited
            -int(x["cluster_id"]))               # prefer oldest (lowest) id


def dedup(records):
    """Collapse duplicate clusters via two signals, transitively (union-find):
       (a) identical (normalized name, year);
       (b) identical U.S. citation AND name-token overlap >= 0.5.
    Signal (b) catches cross-source spelling variants (e.g. 'United States v. More'
    vs 'United States v. Benjamin More') without merging genuine companion cases that
    merely share a starting page (those have ~zero name overlap).
    Returns (canonical_id_set, {duplicate_id: canonical_id})."""
    parent = {r["cluster_id"]: r["cluster_id"] for r in records}
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
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


# ---- fetch (clusters endpoint, cursor pagination, resilient) ---------------

def _get(url, headers):
    """GET one page with retry on 429 / transient network errors."""
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", "30"))
                print(f"  throttled (429), waiting {wait}s...", file=sys.stderr)
                time.sleep(wait + 1)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            w = 5 * (attempt + 1)
            print(f"  network error ({e}); retry in {w}s...", file=sys.stderr)
            time.sleep(w)
    raise RuntimeError(f"failed to fetch after retries: {url}")


def fetch_all(after, before, token, pause=0.3):
    """Fetch SCOTUS clusters one YEAR at a time — the full-range docket__court join
    times out server-side, but a single-year window returns quickly."""
    headers = {"User-Agent": "scotus-data-bot/2.0", "Authorization": f"Token {token}"}
    y0, y1 = int(after[:4]), int(before[:4])
    rows, seen = [], set()
    for year in range(y0, y1 + 1):
        lo = max(after, f"{year}-01-01")
        hi = min(before, f"{year}-12-31")
        url = CLUSTERS + "?" + urllib.parse.urlencode({
            "docket__court": "scotus", "date_filed__gte": lo,
            "date_filed__lte": hi, "fields": FIELDS,
        })
        n0 = len(rows)
        while url:
            data = _get(url, headers)
            for r in data.get("results", []):
                if r["id"] in seen:
                    continue
                seen.add(r["id"])
                rows.append(r)
            url = data.get("next")
            if url:
                time.sleep(pause)
        print(f"  {year}: +{len(rows)-n0}  (total {len(rows)})", file=sys.stderr)
    return rows


# ---- main ------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--after", default="1790-01-01")
    ap.add_argument("--before", default="1820-12-31")
    ap.add_argument("--outdir", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--from-cache", action="store_true",
                    help="reprocess raw_clusters.json instead of refetching")
    ap.add_argument("--validate", action="store_true",
                    help="print per-year KEEP vs Wikipedia (1791-1820)")
    args = ap.parse_args()

    cache = os.path.join(args.outdir, "raw_clusters.json")
    if args.from_cache and os.path.exists(cache):
        rows = json.load(open(cache))
        print(f"loaded {len(rows)} raw clusters from cache", file=sys.stderr)
    else:
        token = os.environ.get("COURTLISTENER_API_TOKEN")
        if not token:
            sys.exit("ERROR: COURTLISTENER_API_TOKEN required (clusters endpoint needs auth).\n"
                     "Run: agentsecrets env -- python3 scotus_ingest.py")
        print(f"Fetching clusters docket__court=scotus {args.after}..{args.before}", file=sys.stderr)
        rows = fetch_all(args.after, args.before, token)
        json.dump(rows, open(cache, "w"))
        print(f"cached {len(rows)} raw clusters -> {cache}", file=sys.stderr)

    # 1) SCOTUS filter
    recs = []
    for r in rows:
        vol, cite = us_cite(r.get("citations"))
        scdb = (r.get("scdb_id") or "").strip()
        recs.append({
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
        })

    # 2) de-dup within each bucket (keep buckets separate so a KEEP case is never
    #    merged into a REVIEW one)
    canonical, dup_of = set(), {}
    for bucket in ("KEEP", "REVIEW"):
        c, d = dedup([x for x in recs if x["bucket"] == bucket])
        canonical |= c
        dup_of.update(d)
    for x in recs:
        x["dedup_role"] = "canonical" if x["cluster_id"] in canonical else "duplicate"
        x["dup_of"] = dup_of.get(x["cluster_id"], "")
    recs.sort(key=lambda x: (x["dateFiled"], int(x["cluster_id"])))

    cols = ["cluster_id", "caseName", "us_cite", "volume", "scdb_id", "source",
            "citation_count", "precedential_status", "dateFiled", "bucket",
            "dedup_role", "dup_of"]

    def write(name, data):
        with open(os.path.join(args.outdir, name), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(data)

    keep = [r for r in recs if r["bucket"] == "KEEP" and r["dedup_role"] == "canonical"]
    review = [r for r in recs if r["bucket"] == "REVIEW" and r["dedup_role"] == "canonical"]
    dupes = [r for r in recs if r["dedup_role"] == "duplicate"]
    write("all_clusters.csv", recs)
    write("keep.csv", keep)
    write("review.csv", review)
    write("duplicates.csv", dupes)

    print(f"\nraw clusters         : {len(recs)}")
    print(f"  duplicates removed : {len(dupes)}  (source=='U' Harvard-only: "
          f"{sum(1 for d in dupes if d['source']=='U')})")
    print(f"KEEP  (canonical)    : {len(keep)}")
    print(f"REVIEW (canonical)   : {len(review)}")

    if args.validate:
        yk = Counter(int(r["dateFiled"][:4]) for r in keep if r["dateFiled"])
        print("\nyear | keep | wiki |  Δ")
        tc = tw = 0
        for y in range(1791, 1821):
            c, w = yk.get(y, 0), WIKI[y]
            tc += c; tw += w
            print(f"{y} | {c:>4} | {w:>4} | {c-w:+d}")
        print(f"TOT  | {tc:>4} | {tw:>4} | {tc-tw:+d}")


if __name__ == "__main__":
    main()
