# Design note: opinion text cleaning + reporter apparatus capture

Status: **draft / in progress.** Scope: add a deterministic, high-fidelity `clean_text` layer over
the opinion corpus, and (separately) capture the reporter apparatus (headmatter/summary) that the
current ETL leaves behind. This note is the canonical design record; the enforced sources remain the
code, `CONTRIBUTING.md`, and `db/README.md`.

## 1. Goals & constraints

`scotus_decisions` is a foundational dataset for search / RAG, model training, and legal
extraction. Cleaning must therefore be:

- **Deterministic** — no LLM/statistical passes; same input → same output.
- **High-fidelity & conservative** — preserve signal, avoid compounding errors.
- **Non-destructive** — never delete or overwrite; `raw_html` + `plain_text` stay untouched,
  additions are new columns/tables. Consistent with the project's data-lineage guarantees.

## 2. ETL audit (2026-07-06)

Checked the existing extract → transform → load against CourtListener's documented field semantics
and the live API. The pipeline does **not** strip captured opinion text, but it never *extracts* two
categories of meaningful data:

- **Reporter apparatus (material).** Cluster-level `summary` + `headmatter` are populated for ~36% of
  early cases at ~28–38K chars each — the reporter's syllabus, procedural account, and **arguments of
  counsel**. Verified (Ware v. Hylton) that this content is **not** duplicated in the opinion body,
  so it is genuinely absent from the corpus. `syllabus`/`arguments` fields are empty this era; the
  content lives in `summary`/`headmatter`. `extract.py` `CLUSTER_FIELDS` requests none of these.
- **Opinion metadata (minor).** `OPINION_FIELDS` omits `ordering_key` (canonical seriatim order — the
  13 multi-opinion cases), `per_curiam`, `joined_by_str`.
- **Verified OK.** `best_text` preferring `html_with_citations` loses no opinion text: for these
  Harvard-CAP cases that field *is* the Harvard XML (`<?xml…><opinion><author>…`) with citations
  annotated. **Consequence:** page breaks appear as `<page-number>` XML elements in those, not only
  `<span class="star-pagination" label>` in the HTML-flavored ones — the stripper must handle both.

### Apparatus field semantics (CourtListener model `help_text`, authoritative)

Quoted from `cl/search/models.py` (`OpinionCluster`) — the source of truth, not inference:

- **`headmatter`** — *"the content before an opinion in the Harvard CaseLaw import. This consists of
  summaries, headnotes, attorneys etc for the opinion."* → the **raw composite** pre-opinion blob.
- **`summary`** — *"A summary of what happened in the case. Appears at the beginning of the case just
  after the title of the case and court information."* → a **parsed component**.
- **`syllabus`** — *"A summary of the issues presented in the case and the outcome."*
- **`headnotes`** — *"summary descriptions of the legal issues… just after the summary and disposition."*
- **`arguments`** — *"The attorney(s) and legal arguments presented as HTML text. This is primarily
  seen in older opinions…"* (hence richest in our 1790–1820 range).

So `headmatter` is the raw container; `summary`/`syllabus`/`headnotes`/`arguments` are components
carved from the same pre-opinion matter — the container and its parsed pieces, not strict substrings.
Storing all fields keeps both representations for later reconciliation. Exact source→DB field/type
mapping: `dictionary.md`.

## 3. Plan: two PRs

**PR-A — reporter apparatus (this branch). BUILT.** Additive, non-destructive.
- New `src/apparatus.py` builds a standalone `scotus-apparatus.sqlite` (`cluster_text` long-form +
  `cluster_meta`), keyed on `cluster_id`; pipeline `--stage apparatus`. Apparatus stored **raw**.
- `cluster_text.canonical_cluster_id` resolves dedup'd duplicates → the decision, so apparatus joins
  straight to `scotus_decisions` (naive `cluster_id` join reaches 411 decisions; the resolved join
  reaches 608 — much apparatus arrived on the Harvard `U` *duplicate*). `cluster_meta` absents are
  NULL; asset `meta` carries a `git_commit`/`corpus_n_clusters` version pin against the core DB.
- Cheap cluster pull (31 year-requests) → **0 clusters skipped out of corpus** (the apparatus cluster
  set exactly matched the frozen 1,076), so the audited 663/690/1,076 corpus and `scotus.sqlite` are
  untouched.
- **Coverage:** 688/1,076 clusters carry apparatus — 1,838 rows, ~13.6M chars raw (headmatter 443 ·
  summary 660 · headnotes 598 · arguments 128 · history 6 · disposition 3; syllabus/procedural_history
  empty this era). Committed lineage snapshot: `dataset/apparatus_manifest.csv` (688 rows).
- Opinion-metadata backfill (663 paced requests) **deferred** — buys only the minor fields.

**PR-B — `clean_text` over opinion bodies.** The original cleaning plan:
- Add `clean_text`, `clean_version`, `ocr_suspect` to `opinions`; keep `raw_html` + `plain_text`.
- Structure-aware stripper over `raw_html` (both HTML and XML dialects): drop CL-inserted
  star-pagination markers (capture page boundaries), keep original document content; normalize
  `\r`→`\n`, collapse whitespace, strip control chars, Unicode **NFC** (no ASCII folding in the
  canonical column).
- `page_breaks(opinion_id, ordinal, page_label, char_offset, anchor)` relational table; `char_offset`
  into the versioned `clean_text`, `anchor` = following words for human/cross-version relocation.
- **No OCR correction.** `ocr_suspect` records *where* OCR bigrams hit (rich `[{offset, token}, …]`),
  turning a future manual pass from "re-scan 690 docs" into "jump to these spans."
- FTS5 keeps the canonical column NFC and gets recall via a folded tokenizer
  (`tokenize="unicode61 remove_diacritics 2"`) — no `unaccent`/shadow column.

## 4. Decisions log (settled)

- clean_text/plain_text/raw_html all stay **in the SQLite file** (no blob/Parquet offload — the
  single self-contained `.sqlite` Release asset is a core design goal; ~21→~35MB is fine at this scale).
- **Footnote bodies are KEPT.** Resolved by CourtListener docs + data: `<div class="footnote">` bodies
  are original casebody content (e.g. op 1299599 carries a 14K-char footnote reproducing the Circuit
  judges' reasoning in *Hayburn's Case*), not CL annotations. Only CL-inserted star-pagination is
  dropped (→ `page_breaks`).
- **Apparatus stored separately**, not folded into opinions — matches CAP's native `head_matter`
  vs `opinions[]` split and avoids 1-to-many duplication across seriatim opinions.
- Corpus counts frozen; apparatus attached by `cluster_id`. Opinion-metadata backfill deferred.

## 5. Decisions & outstanding

PR-A decisions **ruled** (2026-07-06):
- **#4 Distribution — separate optional asset.** Apparatus ships as a standalone
  `scotus-apparatus.sqlite(.gz)`, `ATTACH`-able and keyed on `cluster_id`. The core `scotus.sqlite`
  is untouched by PR-A (opinion-metadata backfill is deferred), so PR-A is a self-contained new build
  target and the audited corpus stays fully frozen.
- **#3 Apparatus fields — all of them.** Store every non-empty apparatus field (`syllabus`,
  `headnotes`, `summary`, `headmatter`, `arguments`, `disposition`, `history`,
  `procedural_history`) raw. Maximal completeness; cleaning deferred.
- **#6 — yes**, also capture `case_name_full` / `attorneys` / `judges` (into the apparatus asset, not
  the frozen core).

Outstanding (PR-B; defaults are conservative/max-fidelity, taken unless overridden):

| # | Decision | Default (lean) |
|---|---|---|
| 1 | Inline footnote **ref markers** (`†`/`*`/digit superscripts) | keep verbatim |
| 2 | `■` OCR "unreadable" glyph | keep + flag in `ocr_suspect` (it *is* missing-text signal) |
| 5 | `ocr_suspect` detector scope | curated, unambiguous whole-word set (long-s + `h→b`); precision over recall |

## 6. Prior art & best practices

Recent efforts that clean this exact data (CAP + CourtListener) independently confirm the approach;
no surveyed rule contradicts it.

- **Structured star-pagination, not display-parsed markers.** The Common Pile / Eventual pipeline hit
  bugs "handling these star paginations" and had to fetch "a corrected revision… with adjusted
  extraction directly from the source API." Our `label`-attribute → `page_breaks` approach sidesteps
  that pitfall by design.
- **Structure-aware HTML parsing** targeting specific classes/tags (they used Selectolax). We use
  stdlib `html.parser` — same approach, no dependency (stdlib-only runtime rule).
- **Keep headmatter separate from the opinion body** (CAP's native `casebody` schema; COLD Cases).
  → our `cluster_text` table.
- **"Minimize preprocessing; correct only obvious OCR errors"** (CAP/free-law; Pile of Law). We go
  *further* — flag, don't fix. Note: the field norm would *permit* correcting obvious long-s OCR; our
  stricter flag-only stance is deliberate for a foundational dataset.
- **Unicode + whitespace normalization** — adopted (NFC + collapse).

**Non-goal: text-level minhash near-dedup.** The Common Pile applies exact + minhash dedup, which
matters for web-scale corpora full of reprints. Our corpus is already cluster-deduped, curated, and
small (663/690); text-level minhash would be overkill. Documented as an explicit non-goal.

Sources: Eventual, "Processing 99% of U.S. Caselaw for Under $1 in the Common Pile"
(https://www.eventual.ai/blog/processing-99-of-us-caselaw-for-under-1-in-the-common-pile) ·
COLD Cases, Harvard LIL (https://lil.law.harvard.edu/our-work/cold-cases/) ·
free-law/Caselaw_Access_Project (https://huggingface.co/datasets/free-law/Caselaw_Access_Project) ·
Pile of Law, Henderson et al. 2022 (https://arxiv.org/abs/2207.00220) ·
CourtListener Case Law API (https://www.courtlistener.com/help/api/rest/case-law/).
