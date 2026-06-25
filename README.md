# scotus-data-bot

Tools and data for building a clean corpus of **U.S. Supreme Court decisions, 1790–1820**, from the [CourtListener](https://www.courtlistener.com/) API.

## The problem

Building a clean SCOTUS census from CourtListener has **two** pitfalls:

1. **Non-SCOTUS cases.** The early *U.S. Reports* (Dallas reporters, vols 2–4) reprinted Pennsylvania state-court and lower federal (circuit) cases alongside genuine SCOTUS decisions; CourtListener tags them all `scotus`.
2. **Duplicate clusters.** CourtListener's 2025 Harvard Caselaw Access Project import (`source = "U"`) was only partially merged, leaving ~200 early cases with an unmerged duplicate cluster (often a wrong page number and no `scdb_id`).

Together these inflate a naïve `docket__court=scotus` pull for 1790–1820 to **1,076 clusters**, of which only ~660 are distinct Supreme Court decisions.

## Method

**Source:** the database-backed `clusters` endpoint (not `search`, which the docs call the relevance-ranked, non-canonical view), fetched one year at a time with structured `citations`.

**1. SCOTUS filter** — validated against known cases (*Chisholm*, *Ware v. Hylton*, *Calder v. Bull* carry SCDB IDs; PA/circuit reprints do not):

> **KEEP** if U.S. reporter volume **≥ 5** (Cranch/Wheaton onward = exclusively SCOTUS) **OR** the cluster has a non-empty **`scdb_id`**. **REVIEW** otherwise — see [REVIEW_NOTES.md](scotus_filter/REVIEW_NOTES.md) (all non-SCOTUS; 0 genuine decisions wrongly excluded).

**2. De-duplication** — collapse clusters of the same case (transitively) by: identical *(normalized name, year)*, **or** identical U.S. citation + ≥0.5 name-token overlap. Keep the best record (prefer `scdb_id`, then a merged / non-`U` source, then citation count). Companion cases that merely share a starting page have ~zero name overlap and are correctly kept distinct.

## Results (1790–1820)

| Bucket | Count |
|--------|------:|
| **KEEP** (distinct SCOTUS decisions) | **663** |
| REVIEW (non-SCOTUS) | 205 |
| duplicates removed | 208 |
| **Total clusters** | **1,076** |

**Validation:** the 663 per-year counts track [Wikipedia's annual SCOTUS decision totals](https://en.wikipedia.org/wiki/Number_of_U.S._Supreme_Court_cases_decided_by_year) closely — 647 total (+16), most years exact or ±1. The residual is the 1791 term-vs-calendar attribution (−4) and genuine companion-case granularity. Run `--validate` to reproduce the comparison. All landmark cases retained (Marbury, McCulloch, Martin v. Hunter, Dartmouth, Gibbons, Fletcher).

## Files

| File | Description |
|------|-------------|
| `scotus_filter/scotus_ingest.py` | Reusable ingestion: clusters endpoint → SCOTUS filter → dedup → CSVs (+`--validate`). |
| `scotus_filter/all_clusters.csv` | All 1,076 clusters with `bucket`, `source`, `dedup_role`, `dup_of`. |
| `scotus_filter/keep.csv` | The 663 distinct SCOTUS decisions. |
| `scotus_filter/review.csv` | The 205 non-SCOTUS canonical records. |
| `scotus_filter/duplicates.csv` | The 208 dropped duplicates, each with its `dup_of` canonical id. |
| `scotus_filter/REVIEW_NOTES.md`, `review_dispositions.csv` | Human adjudication of the REVIEW bucket. |
| `scotus_filter/bucket.py`, `scotus_filter/page_*.json` | Superseded bootstrap (manual sample used to derive the rule); kept for provenance. |

## Usage

```bash
cd scotus_filter
python3 scotus_ingest.py --validate               # default 1790-01-01 .. 1820-12-31
python3 scotus_ingest.py --after 1790-01-01 --before 1835-12-31
python3 scotus_ingest.py --from-cache --validate  # reprocess cached raw data, no refetch
```

The `clusters` endpoint **requires authentication**. The script reads a CourtListener
API token from the `COURTLISTENER_API_TOKEN` environment variable and sends it as
`Authorization: Token <key>`.

This project manages that token with [agentsecrets](https://github.com/The-17/agentsecrets)
(zero-knowledge: the value is injected into the child process and never printed).
The directory is linked to the `dev-secret-agent` project; run the ingest with:

```bash
agentsecrets env -- python3 scotus_ingest.py
```

The first successful run writes `scotus_filter/raw_clusters.json` (gitignored),
after which `--from-cache` reprocesses instantly without any network calls.

## Status / next steps

- [x] Filter rule derived and validated
- [x] Full 1790–1820 enumeration and classification
- [x] Human review pass over the REVIEW candidates (all non-SCOTUS; see REVIEW_NOTES.md)
- [x] De-duplication + validation against the historical annual record
- [ ] Full-text retrieval for the 663 confirmed-SCOTUS decisions
