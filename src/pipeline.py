"""Pipeline orchestrator: one stage per invocation (stages are never chained in one job).

Extract / raw mirror:
  extract        : mirror clusters + opinions VERBATIM into data/raw/ (needs the API token).
  package-mirror : build the raw-mirror release archive + committed CHECKSUMS ledger.
  fetch-mirror   : download + verify the raw-mirror Release asset, unpack into data/raw/.

Transform (each reads/writes the staging DB; run in order, separately):
  materialize -> scope -> dedup -> validate -> reselect -> clean

Load:
  load : build the shipped scotus.sqlite from the staging DB (blank-slate rebuild).

  apparatus : build the optional reporter-apparatus asset (scotus-apparatus.sqlite); separate
              pull, keyed on cluster_id, leaves the core DB untouched.

Network stages need COURTLISTENER_API_TOKEN; run via:
    agentsecrets env -- python -m src.pipeline --stage extract
"""

import argparse
import csv
import json
import os
import sys
from collections import Counter

from config import settings
from src import apparatus, extract, load, mirror
from src.transform import clean_opinions, dedup, materialize, reselect, scope, validate


def _write_csv(path, cols, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows([{k: r.get(k, "") for k in cols} for r in rows])


def stage_extract():
    """Extract: mirror clusters + opinions VERBATIM into data/raw/{clusters,opinions}/.

    Decision-independent — scope is `docket__court=scotus` only, every cluster, full API fields,
    no reshaping. Opinions are fetched for EVERY cluster, kept or not. Writes the run manifest and
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
    # Cross-check: n_clusters counts the whole mirror; clusters_by_year is this run's window.
    # A mismatch means the mirror is wider than this run's window; flag it (do not fail).
    windowed = sum(year["stored"] for year in manifest["clusters_by_year"])
    if manifest["n_clusters"] != windowed:
        print(
            f"  NOTE: n_clusters={manifest['n_clusters']} counts the whole mirror, but this run's "
            f"window stored {windowed} — the mirror is wider than the run window.",
            file=sys.stderr,
        )
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


def stage_materialize():
    """Transform stage 1: normalize the raw mirror into the cluster -> opinion staging DB.

    Decision-independent — preserves the 1:many hierarchy with referential integrity, retains every
    candidate source field, and makes no scope/dedup/clean decision."""
    settings.ensure_dirs()
    stg_clusters, stg_opinions = materialize.materialize_hierarchy(
        settings.RAW_CLUSTERS_DIR,
        settings.RAW_OPINIONS_DIR,
        settings.STAGING_DB_PATH,
    )
    print(
        f"materialize: {len(stg_clusters)} clusters, {len(stg_opinions)} opinions "
        f"-> {settings.STAGING_DB_PATH}",
        file=sys.stderr,
    )
    return stg_clusters, stg_opinions


def stage_scope():
    """Transform stage 2: propose, per cluster, whether it is a SCOTUS decision.

    Reads the staging DB and the human-review ledger (dataset/scope_review.csv, authoritative),
    adjudicates the rest via reporter authority (Cranch/Wheaton were SCOTUS-only) and, within
    Dallas, scdb_id, and writes the derived stg_cluster_scope table (is_scotus + evidence +
    proposed disposition). Propose-only and non-destructive: nothing is dropped here; a later
    stage executes the dispositions."""
    proposals = scope.run_scope()
    counts = Counter(proposal.is_scotus for proposal in proposals)
    print(
        f"scope: {len(proposals)} clusters -> "
        f"{counts.get('true', 0)} scotus / {counts.get('false', 0)} not -> stg_cluster_scope",
        file=sys.stderr,
    )
    return proposals


def stage_dedup():
    """Transform stage 3: collapse duplicate records of the same decision.

    Reads the scope keep-candidates and writes stg_cluster_dedup labeling each cluster
    canonical or duplicate (dup_of), using scdb identity + caption + opinion-text overlap,
    plus the human-review ledger (dataset/dedup_review.csv) for adjudicated pairs the
    automated gates cannot reach. Label-only and non-destructive: stg_opinions is
    untouched, the 1:many hierarchy intact."""
    records = dedup.run_dedup()
    canonical = sum(1 for record in records if record.dedup_role == "canonical")
    reviewed = sum(1 for record in records if record.dup_method == "human_review")
    print(
        f"dedup: {len(records)} keep-candidates -> {canonical} canonical / "
        f"{len(records) - canonical} duplicate ({reviewed} via review ledger) "
        f"-> stg_cluster_dedup",
        file=sys.stderr,
    )
    return records


def stage_validate():
    """Transform stage 4: reconcile the deduplicated KEEP set against the reference.

    Per-volume acceptance check over the final corpus (U.S. vols 2-18; vol 19 is buffer,
    excluded) against dataset/case_name_reference.csv. Writes stg_validate_volume / _detail
    and a committed dataset/validate_report.csv, and prints the report. Read-only on corpus
    data."""
    results = validate.run_validate()
    print(validate.format_report(results), file=sys.stderr)
    return results


def stage_reselect():
    """Transform stage 5: choose the best source-text field per corpus opinion.

    Writes stg_opinion_source (chosen_source + is_ocr_dirty + type) by priority
    html_lawbox -> xml_harvard -> html. Non-destructive: records the choice only, leaves
    all source fields in stg_opinions; per opinion row, so combined + split both kept."""
    selections = reselect.run_reselect()
    by_src = Counter(s.chosen_source for s in selections)
    dirty = sum(1 for s in selections if s.is_ocr_dirty)
    print(
        f"reselect: {len(selections)} opinions -> "
        f"lawbox {by_src.get('source_html_lawbox', 0)} / "
        f"harvard {by_src.get('source_xml_harvard', 0)} / "
        f"html {by_src.get('source_html', 0)}; {dirty} ocr-dirty -> stg_opinion_source",
        file=sys.stderr,
    )
    return selections


def stage_clean():
    """Transform stage 6: clean_text per corpus opinion from its chosen source.

    Runs each opinion's reselect-chosen source through the shared deterministic cleaner
    (src/clean.py) and writes stg_opinion_clean (clean_text + version + ocr_suspect) and
    stg_page_break. Non-destructive; reuses the tested cleaner (star-pagination -> page
    breaks, both dialects, NFC, no OCR correction, keeps captions/headers)."""
    cleaned = clean_opinions.run_clean()
    n_breaks = sum(len(c.page_breaks) for c in cleaned)
    n_suspect = sum(1 for c in cleaned if c.ocr_suspect)
    print(
        f"clean: {len(cleaned)} opinions -> stg_opinion_clean "
        f"({n_breaks} page-breaks, {n_suspect} with ocr_suspect)",
        file=sys.stderr,
    )
    return cleaned


def stage_load():
    """Load: build the shipped scotus.sqlite from the staging DB.

    Requires every Transform stage to have run (fails loudly naming the missing
    stage otherwise). Blank-slate rebuild; ships all clusters + all opinion rows
    fully labeled, corpus text + offset spans, FTS, and the scotus_decisions view."""
    settings.ensure_dirs()
    counts = load.build_db()
    print(f"load: built {settings.DB_PATH}", file=sys.stderr)
    for key, value in counts.items():
        print(f"  {key:26} {value}", file=sys.stderr)
    return counts


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
        required=True,
        choices=[
            "extract",
            "package-mirror",
            "fetch-mirror",
            "materialize",
            "scope",
            "dedup",
            "validate",
            "reselect",
            "clean",
            "load",
            "apparatus",
        ],
    )
    ap.add_argument(
        "--from-cache",
        action="store_true",
        help="apparatus stage: use the cached pull, no network",
    )
    args = ap.parse_args()

    if args.stage == "extract":
        stage_extract()
    if args.stage == "package-mirror":
        stage_package_mirror()
    if args.stage == "fetch-mirror":
        stage_fetch_mirror()
    if args.stage == "materialize":
        stage_materialize()
    if args.stage == "scope":
        stage_scope()
    if args.stage == "dedup":
        stage_dedup()
    if args.stage == "validate":
        stage_validate()
    if args.stage == "reselect":
        stage_reselect()
    if args.stage == "clean":
        stage_clean()
    if args.stage == "load":
        stage_load()
    if args.stage == "apparatus":
        stage_apparatus(from_cache=args.from_cache)


if __name__ == "__main__":
    main()
