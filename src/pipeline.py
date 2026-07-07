"""Pipeline orchestrator: extract -> transform -> load.

Stages (run one or `all`):
  clusters  : fetch SCOTUS clusters (or --from-cache), filter + dedup, write staging CSVs.
  text      : fetch opinion text for the KEEP set (resumable, paced), write fulltext + manifest.
  load      : build the SQLite database from the staging files.
  all       : clusters -> text -> load.
  apparatus : build the optional reporter-apparatus asset (scotus-apparatus.sqlite); separate
              pull, keyed on cluster_id, leaves the core DB untouched. NOT part of `all`.

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
from src import apparatus, extract, load, ocr_suggest, transform


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
    _write_csv(settings.KEEP_CSV, cols, keep)  # committed snapshot

    print(
        f"clusters={len(recs)} keep={len(keep)} review={len(review)} "
        f"duplicates={len(dupes)} (Harvard-U dupes "
        f"{sum(1 for d in dupes if d['source'] == 'U')})"
    )
    if validate:
        _validate(keep)
    return keep


def _validate(keep):
    yk = Counter(int(r["dateFiled"][:4]) for r in keep if r["dateFiled"])
    print("\nyear | keep | wiki |  Δ")
    tc = tw = 0
    for y in range(1791, 1821):
        c, w = yk.get(y, 0), settings.WIKI_ANNUAL[y]
        tc += c
        tw += w
        print(f"{y} | {c:>4} | {w:>4} | {c - w:+d}")
    print(f"TOT  | {tc:>4} | {tw:>4} | {tc - tw:+d}")


def stage_text(limit=0):
    settings.ensure_dirs()
    if not os.path.exists(settings.KEEP_CSV):
        sys.exit("ERROR: keep.csv missing — run the 'clusters' stage first.")
    headers = None  # fetched lazily, so a fully-cached rebuild needs no token
    rows = list(csv.DictReader(open(settings.KEEP_CSV)))
    if limit:
        rows = rows[:limit]
    manifest, failures, done, skip, fail = [], [], 0, 0, 0

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
            failures.append({"cluster_id": cid, "caseName": r["caseName"], "error": str(e)})
            fail += 1
            continue
        ops = [transform.opinion_record(o) for o in api_ops]
        total = sum(o["char_count"] for o in ops)
        rec = {
            "cluster_id": cid,
            "caseName": r["caseName"],
            "us_cite": r["us_cite"],
            "dateFiled": r["dateFiled"],
            "scdb_id": r.get("scdb_id", ""),
            "source": r.get("source", ""),
            "n_opinions": len(ops),
            "total_chars": total,
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
    # Durable per-run failure log — a KEEP decision missing text is a data gap, so it must
    # be recorded, not left in stderr only.
    _write_csv(settings.FAILURES_CSV, ["cluster_id", "caseName", "error"], failures)
    empty = sum(1 for m in manifest if (m["total_chars"] or 0) == 0)
    print(f"text: fetched={done} skipped={skip} failed={fail} | textless={empty}")
    if failures:
        print(f"  {len(failures)} text-fetch failures logged -> {settings.FAILURES_CSV}")


def stage_load():
    settings.ensure_dirs()
    conn, counts = load.build_db("sqlite", path=settings.DB_PATH)
    print(f"loaded sqlite database at {settings.DB_PATH}")
    for k, v in counts.items():
        print(f"  {k:24} {v}")
    conn.close()


def stage_apparatus(from_cache=False):
    """Build the optional reporter-apparatus asset (scotus-apparatus.sqlite).

    Pulls cluster-level apparatus (headmatter/summary/…) for the frozen corpus and builds a
    standalone SQLite keyed on cluster_id. The core scotus.sqlite is never touched. Needs the
    'clusters' stage first (for the corpus's cluster IDs). Network unless --from-cache."""
    settings.ensure_dirs()
    if not os.path.exists(settings.ALL_CLUSTERS_CSV):
        sys.exit("ERROR: all_clusters.csv missing — run the 'clusters' stage first.")
    clu = {int(r["cluster_id"]): r for r in csv.DictReader(open(settings.ALL_CLUSTERS_CSV))}
    # canonical resolution: a duplicate resolves to its dup_of; a canonical resolves to itself.
    corpus = {
        cid: (int(r["dup_of"]) if r["dedup_role"] == "duplicate" and r["dup_of"] else cid)
        for cid, r in clu.items()
    }

    if from_cache and os.path.exists(settings.RAW_APPARATUS):
        raw = json.load(open(settings.RAW_APPARATUS))
        print(f"loaded {len(raw)} raw apparatus clusters from cache", file=sys.stderr)
    else:
        token = settings.get_token()
        print(f"fetching apparatus {settings.AFTER}..{settings.BEFORE}", file=sys.stderr)
        raw = extract.fetch_clusters(
            settings.AFTER, settings.BEFORE, token, fields=extract.APPARATUS_FIELDS
        )
        json.dump(raw, open(settings.RAW_APPARATUS, "w"))
        print(f"cached {len(raw)} raw apparatus clusters", file=sys.stderr)

    conn, counts = apparatus.build_apparatus_db(
        path=settings.APPARATUS_DB_PATH,
        raw_apparatus=settings.RAW_APPARATUS,
        corpus=corpus,
    )
    # Committed coverage snapshot (lineage): which corpus clusters carry which apparatus kinds,
    # with bucket/dedup provenance so the coverage breakdown is auditable without a join.
    manifest = {}
    for rc in raw:
        cid = int(rc["id"])
        if cid not in clu:
            continue
        rows = apparatus.apparatus_rows(rc)
        if rows:
            manifest[cid] = {
                "cluster_id": cid,
                "bucket": clu[cid]["bucket"],
                "dedup_role": clu[cid]["dedup_role"],
                "canonical_cluster_id": corpus[cid],
                "kinds": ";".join(k for k, _, _ in rows),
                "total_chars": sum(cc for _, cc, _ in rows),
            }
    _write_csv(
        settings.APPARATUS_MANIFEST_CSV,
        ["cluster_id", "bucket", "dedup_role", "canonical_cluster_id", "kinds", "total_chars"],
        [manifest[k] for k in sorted(manifest)],
    )
    conn.close()
    print(f"built apparatus asset at {settings.APPARATUS_DB_PATH}")
    for k, v in counts.items():
        print(f"  {k:28} {v}")


def _page_label_for(breaks, offset):
    """Reporter page_label for a char_offset: the last page break at or before it ('' if none)."""
    label = ""
    for off, lb in breaks:
        if off <= offset:
            label = lb
        else:
            break
    return label


def stage_ocr_suggest():
    """Generate the OCR-correction review artifact (dataset/ocr_corrections.csv) from the built DB.

    One row per DISTINCT (original -> suggestion) mapping so review stays tractable; the apply
    stage expands approved mappings to all occurrences. Needs `scotus.sqlite` (run --stage load
    first) and the [correction] extra (wordfreq). Nothing is corrected here — only proposed."""
    import sqlite3
    from collections import defaultdict

    settings.ensure_dirs()
    if not os.path.exists(settings.DB_PATH):
        sys.exit("ERROR: scotus.sqlite missing — run `--stage load` first.")
    conn = sqlite3.connect(settings.DB_PATH)

    breaks = defaultdict(list)
    for oid, off, label in conn.execute(
        "SELECT opinion_id, char_offset, page_label FROM page_breaks "
        "ORDER BY opinion_id, char_offset"
    ):
        breaks[oid].append((off, label))

    # aggregate per distinct (original_lower -> suggestion): count + first example location
    agg = {}
    for oid, clean_text in conn.execute("SELECT opinion_id, clean_text FROM opinions"):
        for s in ocr_suggest.suggest_text(clean_text):
            key = (s["original"].lower(), s["suggestion"])
            row = agg.get(key)
            if row is None:
                agg[key] = {
                    "original": s["original"].lower(),
                    "suggestion": s["suggestion"],
                    "rule": s["rule"],
                    "n_candidates": s["n_candidates"],
                    "alternatives": s["alternatives"],
                    "count": 1,
                    "example_opinion_id": oid,
                    "example_page": _page_label_for(breaks.get(oid, []), s["char_offset"]),
                    "status": "pending",
                    "corrected": "",
                }
            else:
                row["count"] += 1
    conn.close()

    rows = sorted(agg.values(), key=lambda r: (-r["count"], r["original"]))
    cols = [
        "original",
        "suggestion",
        "rule",
        "n_candidates",
        "alternatives",
        "count",
        "example_opinion_id",
        "example_page",
        "status",
        "corrected",
    ]
    _write_csv(settings.OCR_CORRECTIONS_CSV, cols, rows)
    total = sum(r["count"] for r in rows)
    print(
        f"ocr-suggest: {len(rows)} distinct corrections ({total} occurrences) "
        f"-> {settings.OCR_CORRECTIONS_CSV}"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--stage",
        choices=["clusters", "text", "load", "apparatus", "ocr-suggest", "all"],
        default="all",
    )
    ap.add_argument(
        "--from-cache", action="store_true", help="reprocess cached clusters, no network"
    )
    ap.add_argument("--validate", action="store_true", help="compare per-year KEEP vs Wikipedia")
    ap.add_argument("--limit", type=int, default=0, help="text stage: only first N clusters")
    args = ap.parse_args()

    if args.stage in ("clusters", "all"):
        stage_clusters(from_cache=args.from_cache, validate=args.validate)
    if args.stage in ("text", "all"):
        stage_text(limit=args.limit)
    if args.stage in ("load", "all"):
        stage_load()
    # 'apparatus' and 'ocr-suggest' are separate, optional stages — deliberately NOT part of 'all'.
    if args.stage == "apparatus":
        stage_apparatus(from_cache=args.from_cache)
    if args.stage == "ocr-suggest":
        stage_ocr_suggest()


if __name__ == "__main__":
    main()
