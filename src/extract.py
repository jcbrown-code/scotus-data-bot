"""Extract stage: fetch from the CourtListener REST API (authenticated).

Two endpoints, two access patterns:
- clusters: filtered by docket__court=scotus, fetched one YEAR at a time (the full-range
  join times out server-side) with cursor pagination. Returns structured citations.
- opinions: filtered by exact cluster=<id> only (no batch/era filter exists), so text is
  pulled one cluster at a time with adaptive pacing to stay under the rate limit.
"""

import glob
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CLUSTERS_URL = "https://www.courtlistener.com/api/rest/v4/clusters/"
CLUSTER_FIELDS = (
    "id,case_name,date_filed,scdb_id,source,citations,citation_count,precedential_status"
)

OPINIONS_URL = "https://www.courtlistener.com/api/rest/v4/opinions/"
OPINION_FIELDS = (
    "id,type,author_str,extracted_by_ocr,html_with_citations,plain_text,xml_harvard,html"
)

# Reporter apparatus (front matter the opinion body omits): the Harvard-CAP headmatter and the
# reporter's summary / syllabus / arguments of counsel, plus small cluster-level metadata. Pulled
# into a SEPARATE, optional asset (see src/apparatus.py) so the audited core corpus stays frozen.
APPARATUS_FIELDS = (
    "id,case_name_full,syllabus,headnotes,summary,headmatter,arguments,"
    "disposition,history,procedural_history,attorneys,judges"
)

# Adaptive pacing for the per-cluster opinion fetch: a steady delay between requests,
# auto-raised on each 429 so the run settles just under CourtListener's short-window
# throttle instead of bursting into repeated back-offs.
PACE = {"delay": 1.0}


def build_headers(token):
    return {"User-Agent": "scotus-data-bot/2.0", "Authorization": f"Token {token}"}


def _request(url, headers, timeout=60, pace=False):
    """GET one page with retry on 429 / 5xx / transient network errors.

    Returns (body, meta) where meta records the reliability trace for the run log:
    {"attempts": n, "retry_after": [secs...], "server_errors": [codes...]}. The public `_get`
    wraps this and returns just the body for back-compat with the legacy stages."""
    req = urllib.request.Request(url, headers=headers)
    meta = {"attempts": 0, "retry_after": [], "server_errors": []}
    for attempt in range(6):
        meta["attempts"] = attempt + 1
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp), meta
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", "30"))
                meta["retry_after"].append(wait)
                if pace:
                    PACE["delay"] = min(PACE["delay"] + 0.5, 4.0)
                    print(
                        f"    throttled (429), waiting {wait}s; pace now {PACE['delay']}s",
                        file=sys.stderr,
                    )
                else:
                    print(f"  throttled (429), waiting {wait}s...", file=sys.stderr)
                time.sleep(wait + 1)
                continue
            if 500 <= e.code < 600:  # transient server error — back off and retry
                meta["server_errors"].append(e.code)
                backoff = 5 * (attempt + 1)
                print(f"  server error {e.code}; retry in {backoff}s...", file=sys.stderr)
                time.sleep(backoff)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            backoff = 5 * (attempt + 1)
            print(f"  network error ({e}); retry in {backoff}s...", file=sys.stderr)
            time.sleep(backoff)
    raise RuntimeError(f"failed to fetch after retries: {url}")


def _get(url, headers, timeout=60, pace=False):
    """Back-compat shim: return just the response body (legacy stages call this)."""
    body, _meta = _request(url, headers, timeout=timeout, pace=pace)
    return body


def fetch_clusters(after, before, token, pause=0.3, fields=CLUSTER_FIELDS):
    """Fetch SCOTUS clusters one year at a time (cursor-paginated within each year).

    `fields` selects the payload: the default `CLUSTER_FIELDS` drives the frozen core corpus;
    pass `APPARATUS_FIELDS` to pull the (much larger) reporter-apparatus payload instead."""
    headers = build_headers(token)
    start_year, end_year = int(after[:4]), int(before[:4])
    rows, seen = [], set()
    for year in range(start_year, end_year + 1):
        window_start = max(after, f"{year}-01-01")
        window_end = min(before, f"{year}-12-31")
        url = (
            CLUSTERS_URL
            + "?"
            + urllib.parse.urlencode(
                {
                    "docket__court": "scotus",
                    "date_filed__gte": window_start,
                    "date_filed__lte": window_end,
                    "fields": fields,
                }
            )
        )
        start_count = len(rows)
        while url:
            body = _get(url, headers)
            for rec in body.get("results", []):
                if rec["id"] in seen:
                    continue
                seen.add(rec["id"])
                rows.append(rec)
            url = body.get("next")
            if url:
                time.sleep(pause)
        print(f"  {year}: +{len(rows) - start_count}  (total {len(rows)})", file=sys.stderr)
    return rows


def fetch_opinions(cluster_id, headers):
    """Return the raw opinion API objects for one cluster (adaptively paced)."""
    url = (
        OPINIONS_URL
        + "?"
        + urllib.parse.urlencode({"cluster": cluster_id, "fields": OPINION_FIELDS})
    )
    body = _get(url, headers, timeout=90, pace=True)
    return body.get("results", [])


# ---------------------------------------------------------------------------
# Raw mirror (Extract stage): faithful, decision-independent, validated.
#
# Scope = docket__court=scotus only. Every record is stored VERBATIM (no field filter/reshape)
# as one JSON per record; opinions fetched for EVERY cluster (all buckets). The pull is proven by
# schema validation + pagination-continuity + coverage + idempotency. Apparatus rides on the full
# cluster record, so there is no separate apparatus fetch.
# ---------------------------------------------------------------------------

SCOPE = {
    "docket__court": "scotus"
}  # the ONLY server-side filter (period is the date window below)


class SchemaError(ValueError):
    """A fetched record is missing a required field or has a wrong type."""


# Minimal contracts: fields the mirror's invariants (identity + linkage) depend on. Full records
# are stored verbatim regardless; this only guards those invariants, not the whole payload.
CLUSTER_SCHEMA = {
    "required": ("id", "case_name", "date_filed", "citations", "source", "sub_opinions"),
    "types": {"id": int, "citations": list, "sub_opinions": list},
}
OPINION_SCHEMA = {
    "required": ("id", "cluster", "type"),
    "types": {"id": int},
}
SCHEMAS = {"cluster": CLUSTER_SCHEMA, "opinion": OPINION_SCHEMA}


def validate_schema(record, entity):
    """Raise SchemaError if `record` violates the schema for `entity` ('cluster' | 'opinion')."""
    schema = SCHEMAS[entity]
    record_id = record.get("id", "?")
    for field in schema["required"]:
        if field not in record:
            raise SchemaError(f"{entity} {record_id}: missing required field '{field}'")
    for field, typ in schema["types"].items():
        val = record.get(field)
        if val is not None and not isinstance(val, typ):
            raise SchemaError(
                f"{entity} {record_id}: '{field}' expected {typ.__name__}, "
                f"got {type(val).__name__}"
            )
    return record


def store_raw(entity_dir, record):
    """Write one API record VERBATIM to entity_dir/<id>.json.

    Keys are sorted so the mirror is deterministic — a re-fetch of unchanged data yields a
    byte-identical file (the basis of the idempotency check). No field is dropped or reshaped."""
    path = os.path.join(entity_dir, f"{record['id']}.json")
    with open(path, "w") as f:
        json.dump(record, f, sort_keys=True, indent=2, ensure_ascii=False)


def _resolve_count(list_body, headers):
    """Resolve the v4 list response's deferred `count` URL to an int, or None if absent."""
    count = list_body.get("count")
    if isinstance(count, int):
        return count
    if isinstance(count, str) and count.startswith("http"):
        body, _meta = _request(count, headers)
        return body.get("count") if isinstance(body, dict) else body
    return None


def _parse_sub_opinion_ids(cluster_record):
    """The opinion ids a cluster declares via its `sub_opinions` URL list."""
    ids = set()
    for url in cluster_record.get("sub_opinions") or []:
        ids.add(int(str(url).rstrip("/").rsplit("/", 1)[-1]))
    return ids


def fetch_clusters_raw(after, before, token, clusters_dir):
    """Mirror every SCOTUS cluster filed in [after, before] verbatim, one JSON per record.

    Full records (no `fields=` — apparatus rides along). Year-by-year cursor pagination (the
    full-range join times out). Asserts continuity (stored == API count) and no duplicate
    ids across pages, per year. Returns a per-year run log."""
    headers = build_headers(token)
    start_year, end_year = int(after[:4]), int(before[:4])
    log = []
    for year in range(start_year, end_year + 1):
        window_start = max(after, f"{year}-01-01")
        window_end = min(before, f"{year}-12-31")
        url = (
            CLUSTERS_URL
            + "?"
            + urllib.parse.urlencode(
                {**SCOPE, "date_filed__gte": window_start, "date_filed__lte": window_end}
            )
        )
        seen, pages, api_count = set(), 0, None
        while url:
            body, _meta = _request(url, headers, timeout=60)
            if api_count is None:
                api_count = _resolve_count(body, headers)
            for rec in body.get("results", []):
                validate_schema(rec, "cluster")
                if rec["id"] in seen:
                    raise RuntimeError(f"duplicate cluster {rec['id']} across pages ({year})")
                seen.add(rec["id"])
                store_raw(clusters_dir, rec)
            pages += 1
            url = body.get("next")
            if url:
                time.sleep(0.3)
        stored = len(seen)
        if api_count is not None and stored != api_count:
            raise RuntimeError(f"pagination gap {year}: stored {stored} != API count {api_count}")
        log.append({"year": year, "api_count": api_count, "stored": stored, "pages": pages})
        print(f"  {year}: {stored} clusters (api={api_count}, {pages} pages)", file=sys.stderr)
    return log


def fetch_opinions_raw(token, clusters_dir, opinions_dir):
    """Mirror the opinions of EVERY stored cluster verbatim (all buckets — decoupled from KEEP).

    Resumable: skips a cluster whose sub_opinions are all stored. Asserts single-page
    and coverage (every declared sub_opinion is returned). Returns a per-cluster run log."""
    headers = build_headers(token)
    log = []
    for cluster_file in sorted(glob.glob(os.path.join(clusters_dir, "*.json"))):
        cluster = json.load(open(cluster_file))
        cluster_id = cluster["id"]
        expected = _parse_sub_opinion_ids(cluster)
        if expected and all(
            os.path.exists(os.path.join(opinions_dir, f"{opinion_id}.json"))
            for opinion_id in expected
        ):
            continue
        url = OPINIONS_URL + "?" + urllib.parse.urlencode({"cluster": cluster_id})
        body, _meta = _request(url, headers, timeout=90, pace=True)
        if body.get("next"):
            raise RuntimeError(f"unexpected opinion pagination for cluster {cluster_id}")
        got = set()
        for opinion in body.get("results", []):
            validate_schema(opinion, "opinion")
            store_raw(opinions_dir, opinion)
            got.add(opinion["id"])
        missing = expected - got
        if missing:
            raise RuntimeError(
                f"cluster {cluster_id}: declared sub_opinions not returned by cluster filter: "
                f"{sorted(missing)}"
            )
        log.append({"cluster_id": cluster_id, "opinions": len(got)})
        time.sleep(PACE["delay"])
    return log


def extract(
    after, before, token, clusters_dir, opinions_dir, manifest_path, timestamp="", git_commit=""
):
    """Run the Extract stage: mirror clusters + opinions verbatim, then write the run manifest.

    Paths are passed in (not read from settings) so the stage is decoupled and testable."""
    cluster_log = fetch_clusters_raw(after, before, token, clusters_dir)
    opinion_log = fetch_opinions_raw(token, clusters_dir, opinions_dir)
    manifest = {
        "after": after,
        "before": before,
        "scope": SCOPE,
        "timestamp": timestamp,
        "git_commit": git_commit,
        "n_clusters": len(_read_ids(clusters_dir)),  # actual mirror size (resume-independent)
        "n_opinions": len(_read_ids(opinions_dir)),  # actual mirror size (resume-independent)
        "clusters_by_year": cluster_log,
        "opinions_fetched_this_run": sum(entry["opinions"] for entry in opinion_log),
    }
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, sort_keys=True, indent=2)
    return manifest


# ---- verification helpers (pure; reused by tests and the run) --------------


def _read_ids(directory):
    return {int(os.path.basename(p)[:-5]) for p in glob.glob(os.path.join(directory, "*.json"))}


def verify_coverage(clusters_dir, opinions_dir):
    """Return [(cluster_id, sorted(missing_opinion_ids)), ...] — clusters whose declared
    sub_opinions are not all present in the opinion mirror. Empty == complete coverage."""
    stored_ids = _read_ids(opinions_dir)
    gaps = []
    for cluster_file in glob.glob(os.path.join(clusters_dir, "*.json")):
        cluster = json.load(open(cluster_file))
        missing = _parse_sub_opinion_ids(cluster) - stored_ids
        if missing:
            gaps.append((cluster["id"], sorted(missing)))
    return gaps


def verify_no_orphans(clusters_dir, opinions_dir):
    """Return opinion ids whose parent cluster is not in the cluster mirror."""
    cluster_ids = _read_ids(clusters_dir)
    orphans = []
    for opinion_path in glob.glob(os.path.join(opinions_dir, "*.json")):
        opinion = json.load(open(opinion_path))
        parent = opinion.get("cluster")
        parent_id = int(str(parent).rstrip("/").rsplit("/", 1)[-1]) if parent else None
        if parent_id not in cluster_ids:
            orphans.append(opinion["id"])
    return orphans


VOLATILE_FIELDS = {"date_created", "date_modified", "resource_uri", "absolute_url"}


def diff_stores(dir_a, dir_b, volatile=VOLATILE_FIELDS):
    """Compare two raw stores of the same entity for idempotency.

    Returns {"only_a", "only_b", "substantive", "volatile"} (filenames). A record differing only
    in server-set `volatile` fields is 'volatile' (expected on a re-fetch); anything else is
    'substantive' (a real idempotency failure)."""
    names_a = {os.path.basename(p) for p in glob.glob(os.path.join(dir_a, "*.json"))}
    names_b = {os.path.basename(p) for p in glob.glob(os.path.join(dir_b, "*.json"))}
    out = {
        "only_a": sorted(names_a - names_b),
        "only_b": sorted(names_b - names_a),
        "substantive": [],
        "volatile": [],
    }

    def strip_volatile(rec):
        return {k: v for k, v in rec.items() if k not in volatile}

    for name in sorted(names_a & names_b):
        rec_a = json.load(open(os.path.join(dir_a, name)))
        rec_b = json.load(open(os.path.join(dir_b, name)))
        if rec_a == rec_b:
            continue
        bucket = "substantive" if strip_volatile(rec_a) != strip_volatile(rec_b) else "volatile"
        out[bucket].append(name)
    return out
