#!/usr/bin/env python3
"""Retrieve full opinion text for the de-duplicated KEEP set (the 663 SCOTUS decisions).

For each KEEP cluster, fetch its opinion(s) from the CourtListener `opinions` endpoint
and store the best available text. CourtListener spreads text across several fields and
which one is populated varies by source; we prefer html_with_citations (the most
complete — e.g. McCulloch has text ONLY there), then plain_text, xml_harvard, html.
Each saved record keeps the raw field plus a tag-stripped plain-text rendering.

Resumable (skips clusters already on disk) and throttle-resilient. A cluster may hold
several opinions (seriatim concurrences/dissents in the early era) — all are kept.

Requires COURTLISTENER_API_TOKEN (opinions endpoint needs auth):
    agentsecrets env -- python3 fetch_text.py             # all KEEP clusters
    agentsecrets env -- python3 fetch_text.py --limit 5   # smoke-test on first 5
"""
import argparse
import csv
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

OPINIONS = "https://www.courtlistener.com/api/rest/v4/opinions/"
TEXT_FIELDS = ["html_with_citations", "plain_text", "xml_harvard", "html"]  # preference
FIELDS = "id,type,author_str,extracted_by_ocr," + ",".join(TEXT_FIELDS)
HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST_COLS = ["cluster_id", "caseName", "us_cite", "dateFiled",
                 "n_opinions", "total_chars", "text_sources"]


# Adaptive pacing: steady delay between requests, auto-raised whenever we hit a 429
# so the run settles just under CourtListener's short-window throttle instead of
# bursting into repeated 35s back-offs. (Batching isn't possible — the opinions
# endpoint only filters by exact cluster=<id> — so smooth pacing is the only lever.)
PACE = {"delay": 1.0}

def get(url, headers):
    for attempt in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=90) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                w = int(e.headers.get("Retry-After", "30"))
                PACE["delay"] = min(PACE["delay"] + 0.5, 4.0)   # self-tune slower
                print(f"    throttled (429), waiting {w}s; pace now {PACE['delay']}s", file=sys.stderr)
                time.sleep(w + 1)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            w = 5 * (attempt + 1)
            print(f"    network error ({e}); retry in {w}s...", file=sys.stderr)
            time.sleep(w)
    raise RuntimeError("failed after retries: " + url)


def strip_html(s):
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", s).strip()


def best_text(op):
    for f in TEXT_FIELDS:
        v = op.get(f)
        if v and v.strip():
            return f, v
    return None, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", default=os.path.join(HERE, "keep.csv"))
    ap.add_argument("--outdir", default=os.path.join(HERE, "fulltext"))
    ap.add_argument("--limit", type=int, default=0, help="only first N clusters (testing)")
    args = ap.parse_args()

    token = os.environ.get("COURTLISTENER_API_TOKEN")
    if not token:
        sys.exit("ERROR: COURTLISTENER_API_TOKEN required.\n"
                 "Run: agentsecrets env -- python3 fetch_text.py")
    headers = {"User-Agent": "scotus-data-bot/2.0", "Authorization": f"Token {token}"}
    os.makedirs(args.outdir, exist_ok=True)

    rows = list(csv.DictReader(open(args.keep)))
    if args.limit:
        rows = rows[:args.limit]
    manifest, done, skip, fail = [], 0, 0, 0

    for i, r in enumerate(rows, 1):
        cid = r["cluster_id"]
        out = os.path.join(args.outdir, f"{cid}.json")
        if os.path.exists(out):
            j = json.load(open(out))
            manifest.append({k: j.get(k) for k in MANIFEST_COLS})
            skip += 1
            continue
        url = OPINIONS + "?" + urllib.parse.urlencode({"cluster": cid, "fields": FIELDS})
        try:
            data = get(url, headers)
        except Exception as e:
            print(f"  [{i}] cluster {cid} FAILED: {e}", file=sys.stderr)
            fail += 1
            continue
        ops = []
        for o in data.get("results", []):
            src, raw = best_text(o)
            ops.append({
                "opinion_id": o["id"], "type": o.get("type"),
                "author": o.get("author_str") or "", "ocr": o.get("extracted_by_ocr"),
                "text_source": src, "char_count": len(raw),
                "raw": raw, "text": strip_html(raw) if src else "",
            })
        total = sum(o["char_count"] for o in ops)
        rec = {
            "cluster_id": cid, "caseName": r["caseName"], "us_cite": r["us_cite"],
            "dateFiled": r["dateFiled"], "scdb_id": r.get("scdb_id", ""),
            "source": r.get("source", ""), "n_opinions": len(ops),
            "total_chars": total,
            "text_sources": ";".join(sorted({o["text_source"] for o in ops if o["text_source"]})),
            "opinions": ops,
        }
        json.dump(rec, open(out, "w"))
        manifest.append({k: rec[k] for k in MANIFEST_COLS})
        done += 1
        if i % 25 == 0 or args.limit:
            print(f"  [{i}/{len(rows)}] {cid} {r['caseName'][:32]} "
                  f"ops={len(ops)} chars={total}", file=sys.stderr)
        time.sleep(PACE["delay"])

    with open(os.path.join(HERE, "fulltext_manifest.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_COLS)
        w.writeheader()
        w.writerows(manifest)

    empty = sum(1 for m in manifest if (m["total_chars"] or 0) == 0)
    print(f"\nfetched={done} skipped={skip} failed={fail}", file=sys.stderr)
    print(f"manifest rows={len(manifest)} | clusters with NO text={empty}", file=sys.stderr)


if __name__ == "__main__":
    main()
