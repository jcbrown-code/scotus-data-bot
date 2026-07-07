# Database

A single SQLite file (`data/processed/scotus.sqlite`) built by `src/load.py` from the
staging files. FTS5 full-text search over opinion text. The same schema loads into
Postgres via `--target postgres --dsn …` (tsvector + GIN instead of FTS5).

## Build & inspect

```bash
python -m src.load --target sqlite --db data/processed/scotus.sqlite   # or: make db
make inspect                              # human-readable completeness report
datasette data/processed/scotus.sqlite    # browse/query/visualize in the browser
sqlite3 data/processed/scotus.sqlite      # ad-hoc SQL
```

## Schema

| Table / view | Rows | Notes |
|---|---|---|
| `clusters` | 1,076 | every cluster, with `bucket` (KEEP/REVIEW), `dedup_role`, `dup_of` |
| `citations` | many per cluster | structured parallel cites (`reporter, volume, page, type`) |
| `opinions` | 690 | per opinion: `raw_html` + `plain_text` + `clean_text` (+ `clean_version`, `ocr_suspect`), `type`, `author`, `char_count` |
| `page_breaks` | 3,633 | reporter page boundaries within `clean_text`: `ordinal, page_label, char_offset, anchor` |
| `review_dispositions` | 206 | human adjudication of every REVIEW **candidate** (205 canonical + 1 later dedup'd as a duplicate) |
| `meta` | — | build provenance (version, timestamp, date range, counts, git commit) |
| `scotus_decisions` (view) | **663** | canonical decisions: `bucket='KEEP' AND dedup_role='canonical'` |
| `opinions_fts` | — | FTS5 index over `opinions.clean_text` (diacritic-folded tokenizer for recall) |

`clusters.dup_of` and `opinions.cluster_id`/`citations.cluster_id` reference `clusters.cluster_id`;
`page_breaks.opinion_id` references `opinions.opinion_id`.

### Cleaned text

`clean_text` is a deterministic, high-fidelity render of `raw_html` (`src/clean.py`): star-pagination
page markers are removed (captured in `page_breaks` instead), whitespace/Unicode is normalized (NFC),
but original content — footnote bodies, captions, citations — is preserved and **no OCR is
corrected**. OCR-suspect tokens are located (not fixed) in `opinions.ocr_suspect` (JSON). `raw_html`
and `plain_text` are untouched. `clean_version` tracks the cleaning logic (rebuild → offsets stay
valid). `page_breaks.char_offset` indexes into `clean_text` where each reporter page begins.

```sql
-- reconstruct which reporter page a search hit falls on
SELECT o.opinion_id, max(pb.page_label) AS page
FROM opinions o JOIN page_breaks pb ON pb.opinion_id = o.opinion_id
WHERE pb.char_offset <= instr(o.clean_text, 'commerce among the') GROUP BY o.opinion_id;
```

## Reporter apparatus (optional separate asset)

The early reporters (Dallas, Cranch, Wheaton) printed substantial front matter that is **not** part
of any opinion — the reporter's syllabus, procedural summary, and arguments of counsel. CourtListener
exposes this at the cluster level; it lives in a **separate, optional** database so the core corpus
above stays byte-for-byte frozen (see `docs/clean-text-design.md`). Coverage: **688 of 1,076
clusters** carry apparatus (1,838 rows, ~13.6M chars raw — larger than the opinion corpus itself).

```bash
python -m src.pipeline --stage apparatus   # pull + build data/processed/scotus-apparatus.sqlite
```

| Table | Notes |
|---|---|
| `cluster_text` | one row per (`cluster_id`, `kind`), `kind` ∈ {syllabus, headnotes, summary, headmatter, arguments, disposition, history, procedural_history}; `raw_text` stored **raw** (uncleaned), with `char_count`; `canonical_cluster_id` resolves duplicates → the decision |
| `cluster_meta` | per cluster: `case_name_full`, `attorneys`, `judges` (absent = NULL) |
| `meta` | build provenance + version pin (`git_commit` must match the core DB's) |

Both `cluster_id` and `canonical_cluster_id` join to `clusters.cluster_id` in the core `scotus.sqlite`
(separate file, so no enforced FK). **Join on `canonical_cluster_id`** to reach a decision's apparatus
— much of it arrived on the Harvard `U` *duplicate*, so a naive `cluster_id` join reaches only 411 of
the 663 decisions, vs **608** via `canonical_cluster_id` (55 decisions have no apparatus at all).

```sql
ATTACH 'data/processed/scotus-apparatus.sqlite' AS app;

-- all reporter apparatus for a decision (resolves duplicates automatically)
SELECT a.kind, a.raw_text
FROM scotus_decisions d
JOIN app.cluster_text a ON a.canonical_cluster_id = d.cluster_id
WHERE d.case_name LIKE 'Ware%' AND a.kind IN ('summary', 'headmatter');
```

## Example queries

```sql
-- every decision, oldest first
SELECT date_filed, case_name, us_cite FROM scotus_decisions ORDER BY date_filed;

-- full-text search (FTS5)
SELECT c.case_name, c.us_cite
FROM opinions_fts f
JOIN opinions o ON o.opinion_id = f.rowid
JOIN clusters c ON c.cluster_id = o.cluster_id
WHERE opinions_fts MATCH 'commerce clause';

-- read an opinion's text
SELECT plain_text FROM opinions o JOIN clusters c USING (cluster_id)
WHERE c.case_name LIKE 'McCulloch%';

-- trace a dropped duplicate to its canonical record
SELECT d.cluster_id, d.case_name, k.case_name AS canonical
FROM clusters d JOIN clusters k ON k.cluster_id = d.dup_of
WHERE d.dedup_role = 'duplicate' LIMIT 10;
```
