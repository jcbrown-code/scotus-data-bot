-- Human-readable completeness report for the SCOTUS corpus database (V2 schema).
-- Run: sqlite3 data/processed/scotus.sqlite < db/inspect.sql   (or `make inspect`)
.mode box
.headers on

SELECT '== BUILD PROVENANCE ==' AS section;
SELECT key, value FROM meta ORDER BY key;

SELECT '== TOTALS ==' AS section;
SELECT
  (SELECT count(*) FROM clusters)                                             AS clusters,
  (SELECT count(*) FROM scotus_decisions)                                     AS decisions,
  (SELECT count(*) FROM clusters WHERE is_scotus='false')                     AS scope_dropped,
  (SELECT count(*) FROM clusters WHERE dedup_role='duplicate')                AS duplicates,
  (SELECT count(*) FROM opinions)                                             AS opinions,
  (SELECT count(*) FROM opinions WHERE clean_text IS NOT NULL)                AS corpus_opinions,
  (SELECT count(*) FROM citations)                                            AS citations;
-- decisions is case-level (the scotus_decisions view); corpus_opinions is document-level —
-- seriatim cases carry several opinions per decision, so corpus_opinions >= decisions.
SELECT 'decisions = distinct cases (the view); corpus_opinions = opinion documents '
    || 'with derived text (seriatim cases have several)' AS note;

SELECT '== COMPLETENESS (should be 0 textless) ==' AS section;
SELECT count(*) AS textless_decisions
FROM scotus_decisions d
WHERE NOT EXISTS (SELECT 1 FROM opinions o
                  WHERE o.cluster_id=d.cluster_id AND o.clean_text IS NOT NULL);

SELECT '== REPORTER COVERAGE ==' AS section;
SELECT CASE
         WHEN us_volume BETWEEN 2 AND 4   THEN 'Dallas (2-4 U.S.)'
         WHEN us_volume BETWEEN 5 AND 13  THEN 'Cranch (5-13 U.S.)'
         WHEN us_volume BETWEEN 14 AND 18 THEN 'Wheaton (14-18 U.S.)'
         ELSE 'other' END AS reporter_era,
       count(*) AS decisions
FROM scotus_decisions GROUP BY 1 ORDER BY min(us_volume);

SELECT '== OCR-SUSPECT SURFACE (input to the future OCR stage) ==' AS section;
SELECT
  (SELECT count(DISTINCT opinion_id) FROM ocr_suspects)                       AS flagged_opinions,
  (SELECT count(*) FROM ocr_suspects)                                         AS flagged_spots,
  (SELECT count(*) FROM opinions WHERE is_ocr_dirty=1)                        AS ocr_dirty_opinions;

SELECT '== LONGEST & SHORTEST CORPUS OPINIONS ==' AS section;
SELECT c.case_name, c.us_cite, length(o.clean_text) AS chars
FROM opinions o JOIN clusters c ON c.cluster_id=o.cluster_id
WHERE o.clean_text IS NOT NULL
ORDER BY length(o.clean_text) DESC LIMIT 5;
SELECT c.case_name, c.us_cite, length(o.clean_text) AS chars
FROM opinions o JOIN clusters c ON c.cluster_id=o.cluster_id
WHERE o.clean_text IS NOT NULL
ORDER BY length(o.clean_text) ASC LIMIT 5;

SELECT '== FTS SAMPLE: "necessary proper" ==' AS section;
SELECT DISTINCT c.case_name, c.us_cite
FROM opinions_fts f JOIN opinions o ON o.opinion_id=f.rowid
JOIN clusters c ON c.cluster_id=o.cluster_id
WHERE opinions_fts MATCH 'necessary proper';

SELECT '== EVERY DECISION (date, citation, chars) ==' AS section;
SELECT d.date_filed, d.case_name, d.us_cite,
       (SELECT sum(length(o.clean_text)) FROM opinions o
        WHERE o.cluster_id=d.cluster_id AND o.clean_text IS NOT NULL) AS chars
FROM scotus_decisions d ORDER BY d.date_filed, d.cluster_id;
