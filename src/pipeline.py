"""Pipeline orchestrator: extract -> transform -> load.

Stages (run one or `all`):
  clusters : fetch SCOTUS clusters (or --from-cache), filter + dedup, write staging CSVs.
  text     : fetch opinion text for the KEEP set (resumable, paced), write fulltext + manifest.
  load     : build the SQLite database from the staging files.
  all      : clusters -> text -> load.

Network stages need COURTLISTENER_API_TOKEN; run via:
    agentsecrets env -- python -m src.pipeline --stage all --validate
Reprocess without network (data already cached on disk):
    python -m src.pipeline --stage all --from-cache --validate
"""
import argparse
import csv
import json
import os
import sys
from collections import Counter

from config import settings
from src import extract, transform, load


def _write_csv(path, cols, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows([{k: r.get(k, "") for k in cols} for r in rows])


def stage_clusters(from_cache=False, validate=False):
    settings.ensure_dirs()
    if from_cache and os.path.exists(settings.RAW_CLUSTERS):
        raw = json.load(open(settings.RAW_CLUSTERS))
        print(f"loaded {len(raw)} raw clusters from cache", file=sys.stderr)
    else:
        token = settings.get_token()
        print(f"fetching clusters {settings.AFTER}..{settings.BEFORE}", file=sys.stderr)
        raw = extract.fetch_clusters(settings.AFTER, settings.BEFORE, token)
        json.dump(raw, open(settings.RAW_CLUSTERS, "w"))
        print(f"cached {len(raw)} raw clusters", file=sys.stderr)

    recs = transform.assign_dedup(transform.classify(raw))
    recs.sort(key=lambda x: (x["dateFiled"], int(x["cluster_id"])))
    keep = [r for r in recs if r["bucket"] == "KEEP" and r["dedup_role"] == "canonical"]
    review = [r for r in recs if r["bucket"] == "REVIEW" and r["dedup_role"] == "canonical"]
    dupes = [r for r in recs if r["dedup_role"] == "duplicate"]

    cols = settings.CLUSTER_COLS
    _write_csv(settings.ALL_CLUSTERS_CSV, cols, recs)
    _write_csv(settings.REVIEW_CSV, cols, review)
    _write_csv(settings.DUPLICATES_CSV, cols, dupes)
    _write_csv(settings.KEEP_CSV, cols, keep)          # committed snapshot

    print(f"clusters={len(recs)} keep={len(keep)} review={len(review)} "
          f"duplicates={len(dupes)} (Harvard-U dupes "
          f"{sum(1 for d in dupes if d['source']=='U')})")
    if validate:
        _validate(keep)
    return keep


def _validate(keep):
    yk = Counter(int(r["dateFiled"][:4]) for r in keep if r["dateFiled"])
    print("\nyear | keep | wiki |  Δ")
    tc = tw = 0
    for y in range(1791, 1821):
        c, w = yk.get(y, 0), settings.WIKI_ANNUAL[y]
        tc += c; tw += w
        print(f"{y} | {c:>4} | {w:>4} | {c-w:+d}")
    print(f"TOT  | {tc:>4} | {tw:>4} | {tc-tw:+d}")


def stage_text(limit=0):
    settings.ensure_dirs()
    if not os.path.exists(settings.KEEP_CSV):
        sys.exit("ERROR: keep.csv missing — run the 'clusters' stage first.")
    headers = None  # fetched lazily, so a fully-cached rebuild needs no token
    rows = list(csv.DictReader(open(settings.KEEP_CSV)))
    if limit:
        rows = rows[:limit]
    manifest, done, skip, fail = [], 0, 0, 0

    for i, r in enumerate(rows, 1):
        cid = r["cluster_id"]
        out = os.path.join(settings.FULLTEXT_DIR, f"{cid}.json")
        if os.path.exists(out):
            j = json.load(open(out))
            manifest.append({k: j.get(k) for k in settings.MANIFEST_COLS})
            skip += 1
            continue
        if headers is None:
            headers = extract.build_headers(settings.get_token())
        try:
            api_ops = extract.fetch_opinions(cid, headers)
        except Exception as e:
            print(f"  [{i}] cluster {cid} FAILED: {e}", file=sys.stderr)
            fail += 1
            continue
        ops = [transform.opinion_record(o) for o in api_ops]
        total = sum(o["char_count"] for o in ops)
        rec = {
            "cluster_id": cid, "caseName": r["caseName"], "us_cite": r["us_cite"],
            "dateFiled": r["dateFiled"], "scdb_id": r.get("scdb_id", ""),
            "source": r.get("source", ""), "n_opinions": len(ops), "total_chars": total,
            "text_sources": ";".join(sorted({o["text_source"] for o in ops if o["text_source"]})),
            "opinions": ops,
        }
        json.dump(rec, open(out, "w"))
        manifest.append({k: rec[k] for k in settings.MANIFEST_COLS})
        done += 1
        if i % 25 == 0 or limit:
            print(f"  [{i}/{len(rows)}] {cid} {r['caseName'][:32]} chars={total}", file=sys.stderr)
        import time
        time.sleep(extract.PACE["delay"])

    _write_csv(settings.MANIFEST_CSV, settings.MANIFEST_COLS, manifest)  # committed snapshot
    empty = sum(1 for m in manifest if (m["total_chars"] or 0) == 0)
    print(f"text: fetched={done} skipped={skip} failed={fail} | textless={empty}")


def stage_load():
    settings.ensure_dirs()
    conn, counts = load.build_db("sqlite", path=settings.DB_PATH)
    print(f"loaded sqlite database at {settings.DB_PATH}")
    for k, v in counts.items():
        print(f"  {k:24} {v}")
    conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["clusters", "text", "load", "all"], default="all")
    ap.add_argument("--from-cache", action="store_true", help="reprocess cached clusters, no network")
    ap.add_argument("--validate", action="store_true", help="compare per-year KEEP vs Wikipedia")
    ap.add_argument("--limit", type=int, default=0, help="text stage: only first N clusters")
    args = ap.parse_args()

    if args.stage in ("clusters", "all"):
        stage_clusters(from_cache=args.from_cache, validate=args.validate)
    if args.stage in ("text", "all"):
        stage_text(limit=args.limit)
    if args.stage in ("load", "all"):
        stage_load()


if __name__ == "__main__":
    main()
