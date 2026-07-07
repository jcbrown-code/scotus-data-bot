# Data dictionary

Exact mapping from source data to the SQLite fields this project builds. Two assets:

- **Core corpus** — `scotus.sqlite`, built by [`src/load.py`](src/load.py) from the committed
  `dataset/` staging + the cached opinion pull.
- **Optional apparatus** — `scotus-apparatus.sqlite`, built by [`src/apparatus.py`](src/apparatus.py)
  (`--stage apparatus`); ATTACH-able, keyed on `cluster_id`.

Sources: CourtListener REST **v4** `clusters` and `opinions` endpoints (field names/types per each
endpoint's `OPTIONS` schema); the hand-authored `dataset/review_dispositions.csv`; and values derived
by the pipeline. SQLite is dynamically typed — the "DB type" column is the *declared* affinity.

**Origin legend:** `direct` = copied verbatim · `stringified` = coerced to TEXT via `str()` ·
`mapped` = value-transformed · `derived` = computed from other fields (no single source) ·
`human` = hand-authored · `object` = SQLite view/index over other columns.

---

## Core corpus — `scotus.sqlite`

### `clusters` (1,076 rows)

Loaded from `dataset/all_clusters.csv`, which the pipeline writes from `clusters` API records via
`transform.classify` + `transform.assign_dedup`.

| DB column | DB type | Origin | Source field | Source type | Notes |
|---|---|---|---|---|---|
| `cluster_id` | INTEGER PK | direct | `clusters.id` | integer | |
| `case_name` | TEXT | direct | `clusters.case_name` | string | |
| `us_cite` | TEXT | derived | `clusters.citations[]` | array | `"{vol} U.S. {page}"` from the U.S.-reporter entry (`transform.us_cite`); `""` if none |
| `volume` | INTEGER | derived | ← `us_cite` | — | int parsed from `us_cite` (`transform.parse_us_cite`); NULL if unparseable |
| `page` | TEXT | derived | ← `us_cite` | — | page token from `us_cite` |
| `date_filed` | TEXT | direct | `clusters.date_filed` | date (ISO string) | |
| `scdb_id` | TEXT | direct | `clusters.scdb_id` | string | `""` if absent |
| `source` | TEXT | direct | `clusters.source` | string (enum code) | provenance code, e.g. `L`, `U`, `LU` (`U` = Harvard CAP) |
| `citation_count` | INTEGER | direct | `clusters.citation_count` | integer | |
| `precedential_status` | TEXT | direct | `clusters.precedential_status` | string (enum) | |
| `bucket` | TEXT | derived | — | — | `KEEP`/`REVIEW` filter (`transform.classify`) |
| `dedup_role` | TEXT | derived | — | — | `canonical`/`duplicate` (`transform.assign_dedup`) |
| `dup_of` | INTEGER | derived | — | — | → `clusters.cluster_id`, or NULL for canonical |

### `citations` (3,426 rows)

Loaded from the raw `clusters` pull (`clusters.citations[]`).

| DB column | DB type | Origin | Source field | Source type | Notes |
|---|---|---|---|---|---|
| `cluster_id` | INTEGER | direct | `clusters.id` | integer | → `clusters.cluster_id` |
| `reporter` | TEXT | direct | `clusters.citations[].reporter` | string | e.g. `U.S.`, `Dall.`, `Cranch`, `Wheat.` |
| `volume` | TEXT | stringified | `clusters.citations[].volume` | string | `str()` applied |
| `page` | TEXT | stringified | `clusters.citations[].page` | string | `str()` applied |
| `type` | INTEGER | direct | `clusters.citations[].type` | integer | CourtListener citation-type enum (e.g. `1` federal, `5` early-SCOTUS reporters) |

Primary key `(cluster_id, reporter, volume, page)`; exact-duplicate tuples are collapsed and the
dropped count recorded in `meta.n_citation_dupes_dropped`.

### `opinions` (690 rows)

Loaded from `data/raw/fulltext/<cluster>.json`, which store `transform.opinion_record` output built
from `opinions` API objects.

| DB column | DB type | Origin | Source field | Source type | Notes |
|---|---|---|---|---|---|
| `opinion_id` | INTEGER PK | direct | `opinions.id` | integer | |
| `cluster_id` | INTEGER | direct | `clusters.id` (parent) | integer | → `clusters.cluster_id` |
| `type` | TEXT | direct | `opinions.type` | string (enum code) | e.g. `010combined`, `020lead`, `030concurrence`, `040dissent` |
| `author` | TEXT | direct | `opinions.author_str` | string | `""` if absent |
| `extracted_by_ocr` | INTEGER | mapped | `opinions.extracted_by_ocr` | boolean | `True`→1, `False`→0, missing→NULL |
| `text_source` | TEXT | derived | — | — | which field `transform.best_text` chose; `html_with_citations` for all 690 |
| `char_count` | INTEGER | derived | — | — | `len(raw_html)` |
| `raw_html` | TEXT | direct | `opinions.html_with_citations` | string (HTML/XML) | the `best_text` winner; preference order `html_with_citations` → `plain_text` → `xml_harvard` → `html` |
| `plain_text` | TEXT | derived | ← `raw_html` | — | `transform.strip_html`: tags removed, entities unescaped, whitespace collapsed |

*Requested from the API as `best_text` fallbacks but stored only if chosen:* `opinions.plain_text`,
`opinions.xml_harvard`, `opinions.html`. *Requested by the audit but not (yet) stored — deferred
per design doc:* `opinions.ordering_key`, `opinions.per_curiam`, `opinions.joined_by_str`.

### `review_dispositions` (206 rows)

Hand-authored adjudication of the non-SCOTUS `REVIEW` bucket. Loaded from
`dataset/review_dispositions.csv`; only the four columns below enter the table (the CSV's
`caseName`/`us_cite`/`volume`/`dateFiled` columns are context, not loaded).

> **206 = REVIEW candidates**, one disposition each. Dedup later collapsed 1 into another REVIEW
> cluster, leaving **205 canonical** REVIEW records (`meta.n_review`). The table keeps all 206 (the
> disposition for the dedup'd duplicate is retained, not deleted — consistent with the
> non-destructive ethos). Don't conflate the two counts.

| DB column | DB type | Origin | Notes |
|---|---|---|---|
| `cluster_id` | INTEGER | human | → `clusters.cluster_id` |
| `disposition` | TEXT | human | |
| `confidence` | TEXT | human | |
| `rationale` | TEXT | human | |

### `meta`, `scotus_decisions`, `opinions_fts`

| Object | Kind | Definition |
|---|---|---|
| `meta` | table | `(key, value)` build provenance — derived: `pipeline_version`, `build_timestamp`, `date_range`, `source`, `git_commit`, and `n_*` counts |
| `scotus_decisions` | view | `SELECT * FROM clusters WHERE bucket='KEEP' AND dedup_role='canonical'` (the canonical 663) |
| `opinions_fts` | FTS5 index | over `opinions.plain_text` (`content='opinions'`, `content_rowid='opinion_id'`) |

---

## Optional apparatus — `scotus-apparatus.sqlite`

Built from the `clusters` pull with `extract.APPARATUS_FIELDS`, restricted to the frozen corpus.
Stored **raw** (uncleaned). Both `cluster_id` and `canonical_cluster_id` join to `scotus.sqlite`'s
`clusters.cluster_id` (separate file → no enforced FK).

### `cluster_text` (1,838 rows)

One row per (`cluster_id`, `kind`) for each **non-empty** apparatus field.

| DB column | DB type | Origin | Source field | Source type | Notes |
|---|---|---|---|---|---|
| `cluster_id` | INTEGER | direct | `clusters.id` | integer | **true source** of the apparatus (canonical OR a dedup'd duplicate); part of PK |
| `canonical_cluster_id` | INTEGER | derived | ← `clusters.dup_of` | — | the canonical cluster it resolves to; **join on this** to reach the decision (indexed) |
| `kind` | TEXT | derived | (the source field name) | — | one of the 8 kinds below; part of PK |
| `char_count` | INTEGER | derived | — | — | `len(raw_text)` |
| `raw_text` | TEXT | direct | `clusters.<kind>` | string (HTML/XML) | verbatim source value |

`kind` ∈ `clusters.{syllabus, headnotes, summary, headmatter, arguments, disposition, history,
procedural_history}` (all `string`). Observed coverage in the 1790–1820 corpus: `summary` 660,
`headnotes` 598, `headmatter` 443, `arguments` 128, `history` 6, `disposition` 3; `syllabus` and
`procedural_history` empty this era. See `docs/clean-text-design.md` for each field's CourtListener
`help_text` (headmatter = raw composite; the others = parsed components).

> **Why `canonical_cluster_id` exists.** Much apparatus arrived on the Harvard `U` *duplicate*
> cluster, not the canonical record we keep as the decision. Of the 688 clusters with apparatus:
> 411 KEEP-canonical, 202 KEEP-duplicate, 74 REVIEW-canonical, 1 REVIEW-duplicate. So a **naive**
> `JOIN … ON cluster_id = scotus_decisions.cluster_id` reaches only **411** decisions; joining on
> **`canonical_cluster_id`** reaches **608** (the other 197 have apparatus only on a duplicate).
> 55 of the 663 decisions have no apparatus anywhere.

### `cluster_meta` (744 rows)

One row per cluster where ≥1 field below is non-empty. Absent fields are **`NULL`** (the source's
mix of `""`/`null` is normalized to `NULL` at this boundary), not empty strings.

| DB column | DB type | Origin | Source field | Source type |
|---|---|---|---|---|
| `cluster_id` | INTEGER PK | direct | `clusters.id` | integer |
| `case_name_full` | TEXT | direct | `clusters.case_name_full` | string |
| `attorneys` | TEXT | direct | `clusters.attorneys` | string |
| `judges` | TEXT | direct | `clusters.judges` | string |

> **Note (placement).** `case_name_full` is case *identity*, not reporter apparatus, so it sits a
> little oddly here rather than beside `case_name` in the core `clusters` table. It lives in this
> asset only because moving it to core would break the frozen-corpus guarantee and re-touch the
> committed `all_clusters.csv`; revisit if/when the core corpus is next rebuilt.

### `meta`

`(key, value)` build provenance — derived: `asset`, `pipeline_version`, `build_timestamp`, coverage
counts, and a **version pin** (`git_commit`, `corpus_n_clusters`) that must match `scotus.sqlite`'s
`meta.git_commit` for an `ATTACH` join to be valid. Committed lineage snapshot of coverage lives at
`dataset/apparatus_manifest.csv` (`cluster_id, bucket, dedup_role, canonical_cluster_id, kinds,
total_chars`).
