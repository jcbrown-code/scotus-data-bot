"""Extract stage: fetch from the CourtListener REST API (authenticated).

Two endpoints, two access patterns:
- clusters: filtered by docket__court=scotus, fetched one YEAR at a time (the full-range
  join times out server-side) with cursor pagination. Returns structured citations.
- opinions: filtered by exact cluster=<id> only (no batch/era filter exists), so text is
  pulled one cluster at a time with adaptive pacing to stay under the rate limit.
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CLUSTERS = "https://www.courtlistener.com/api/rest/v4/clusters/"
CLUSTER_FIELDS = (
    "id,case_name,date_filed,scdb_id,source,citations,citation_count,precedential_status"
)

OPINIONS = "https://www.courtlistener.com/api/rest/v4/opinions/"
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


def _get(url, headers, timeout=60, pace=False):
    """GET one page with retry on 429 / transient network errors."""
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", "30"))
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
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            w = 5 * (attempt + 1)
            print(f"  network error ({e}); retry in {w}s...", file=sys.stderr)
            time.sleep(w)
    raise RuntimeError(f"failed to fetch after retries: {url}")


def fetch_clusters(after, before, token, pause=0.3, fields=CLUSTER_FIELDS):
    """Fetch SCOTUS clusters one year at a time (cursor-paginated within each year).

    `fields` selects the payload: the default `CLUSTER_FIELDS` drives the frozen core corpus;
    pass `APPARATUS_FIELDS` to pull the (much larger) reporter-apparatus payload instead."""
    headers = build_headers(token)
    y0, y1 = int(after[:4]), int(before[:4])
    rows, seen = [], set()
    for year in range(y0, y1 + 1):
        lo = max(after, f"{year}-01-01")
        hi = min(before, f"{year}-12-31")
        url = (
            CLUSTERS
            + "?"
            + urllib.parse.urlencode(
                {
                    "docket__court": "scotus",
                    "date_filed__gte": lo,
                    "date_filed__lte": hi,
                    "fields": fields,
                }
            )
        )
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
        print(f"  {year}: +{len(rows) - n0}  (total {len(rows)})", file=sys.stderr)
    return rows


def fetch_opinions(cluster_id, headers):
    """Return the raw opinion API objects for one cluster (adaptively paced)."""
    url = (
        OPINIONS + "?" + urllib.parse.urlencode({"cluster": cluster_id, "fields": OPINION_FIELDS})
    )
    data = _get(url, headers, timeout=90, pace=True)
    return data.get("results", [])
