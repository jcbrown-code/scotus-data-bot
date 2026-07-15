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
from src import apparatus, extract, load, mirror, transform


def _write_csv(path, cols, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows([{k: r.get(k, "") for k in cols} for r in rows])


def stage_extract():
    """Extract: mirror clusters + opinions VERBATIM into data/raw/{clusters,opinions}/.

    Decision-independent — scope is `docket__court=scotus` only, all buckets, full API fields, no
    reshaping. Opinions are fetched for EVERY cluster (not just KEEP). Writes the run manifest and
    runs cheap coverage/orphan integrity checks over the mirror."""
    settings.ensure_dirs()
    manifest = extract.extract(
        settings.AFTER,
        settings.BEFORE,
        settings.get_token(),
        settings.RAW_CLUSTERS_DIR,
        settings.RAW_OPINIONS_DIR,
        settings.EXTRACT_MANIFEST,
        timestamp=settings.build_timestamp(),
        git_commit=settings.git_commit(),
    )
    print(
        f"extract: {manifest['n_clusters']} clusters, {manifest['n_opinions']} opinions mirrored",
        file=sys.stderr,
    )
    gaps = extract.verify_coverage(settings.RAW_CLUSTERS_DIR, settings.RAW_OPINIONS_DIR)
    orphans = extract.verify_no_orphans(settings.RAW_CLUSTERS_DIR, settings.RAW_OPINIONS_DIR)
    if gaps:
        print(f"  WARNING: {len(gaps)} clusters missing declared opinions", file=sys.stderr)
    if orphans:
        print(f"  WARNING: {len(orphans)} orphan opinions", file=sys.stderr)
    return manifest


def stage_package_mirror():
    """Build the deterministic raw-mirror release archive + write the committed CHECKSUMS ledger.

    Run before cutting the GitHub Release: produces the tarball asset and the committed
    CHECKSUMS.sha256 (per-record + archive hashes; the immutability/tracing anchor)."""
    archive = os.path.join(settings.RAW_DIR, mirror.ARCHIVE_NAME)
    digest = mirror.build_archive(settings.RAW_DIR, archive)
    mirror.write_checksums(settings.RAW_DIR, archive, settings.CHECKSUMS_PATH)
    print(
        f"package-mirror: {archive} sha256={digest[:12]} -> {settings.CHECKSUMS_PATH}",
        file=sys.stderr,
    )


def stage_fetch_mirror():
    """Download the raw-mirror Release asset, verify it against CHECKSUMS, unpack into data/raw/.

    Reproduces the mirror from the Release instead of re-hitting CourtListener; raises on any hash
    mismatch (immutability check)."""
    settings.ensure_dirs()
    archive = os.path.join(settings.RAW_DIR, mirror.ARCHIVE_NAME)
    url = (
        f"https://github.com/{settings.GITHUB_REPO}/releases/download/"
        f"{settings.RAW_MIRROR_TAG}/{mirror.ARCHIVE_NAME}"
    )
    n = mirror.fetch_mirror(url, settings.RAW_DIR, settings.CHECKSUMS_PATH, archive)
    print(f"fetch-mirror: verified + unpacked {n} records from {url}", file=sys.stderr)


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--stage",
        choices=[
            "extract",
            "package-mirror",
            "fetch-mirror",
            "clusters",
            "text",
            "load",
            "apparatus",
            "all",
        ],
        default="all",
    )
    ap.add_argument(
        "--from-cache", action="store_true", help="reprocess cached clusters, no network"
    )
    ap.add_argument("--validate", action="store_true", help="compare per-year KEEP vs Wikipedia")
    ap.add_argument("--limit", type=int, default=0, help="text stage: only first N clusters")
    args = ap.parse_args()

    # 'extract' is the new decision-independent raw mirror; separate from the legacy 'clusters'/
    # 'text' fetch path, which will be migrated to read from the mirror. NOT part of 'all' yet.
    if args.stage == "extract":
        stage_extract()
    if args.stage == "package-mirror":
        stage_package_mirror()
    if args.stage == "fetch-mirror":
        stage_fetch_mirror()
    if args.stage in ("clusters", "all"):
        stage_clusters(from_cache=args.from_cache, validate=args.validate)
    if args.stage in ("text", "all"):
        stage_text(limit=args.limit)
    if args.stage in ("load", "all"):
        stage_load()
    # 'apparatus' is a separate, optional asset — deliberately NOT part of 'all'.
    if args.stage == "apparatus":
        stage_apparatus(from_cache=args.from_cache)


if __name__ == "__main__":
    main()
