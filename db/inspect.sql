-- Human-readable completeness report for the SCOTUS corpus database.
-- Run: sqlite3 data/processed/scotus.sqlite < db/inspect.sql   (or `make inspect`)
.mode box
.headers on

SELECT '== BUILD PROVENANCE ==' AS section;
SELECT key, value FROM meta ORDER BY key;

SELECT '== TOTALS ==' AS section;
SELECT
  (SELECT count(*) FROM clusters)                                              AS clusters,
  (SELECT count(*) FROM scotus_decisions)                                      AS keep_decisions,
  (SELECT count(*) FROM clusters WHERE bucket='REVIEW' AND dedup_role='canonical') AS review,
  (SELECT count(*) FROM clusters WHERE dedup_role='duplicate')                 AS duplicates,
  (SELECT count(*) FROM opinions)                                              AS opinions,
  (SELECT count(*) FROM citations)                                             AS citations;
-- keep_decisions is case-level (scotus_decisions); opinions is document-level — seriatim
-- cases carry several opinions that link to one decision, so opinions >= keep_decisions.
SELECT 'keep_decisions = distinct decisions (cases); opinions = opinion documents '
    || '(seriatim cases have several)' AS note;

SELECT '== COMPLETENESS (should be 0 textless) ==' AS section;
SELECT count(*) AS textless_decisions
FROM scotus_decisions d
WHERE NOT EXISTS (SELECT 1 FROM opinions o
                  WHERE o.cluster_id=d.cluster_id AND length(trim(o.plain_text))>0);

SELECT '== REPORTER COVERAGE ==' AS section;
SELECT CASE
         WHEN volume BETWEEN 2 AND 4   THEN 'Dallas (2-4 U.S.)'
         WHEN volume BETWEEN 5 AND 13  THEN 'Cranch (5-13 U.S.)'
         WHEN volume BETWEEN 14 AND 18 THEN 'Wheaton (14-18 U.S.)'
         ELSE 'other' END AS reporter_era,
       count(*) AS decisions
FROM scotus_decisions GROUP BY 1 ORDER BY min(volume);

SELECT '== LONGEST & SHORTEST OPINIONS ==' AS section;
SELECT c.case_name, c.us_cite, o.char_count
FROM opinions o JOIN clusters c ON c.cluster_id=o.cluster_id
ORDER BY o.char_count DESC LIMIT 5;
SELECT c.case_name, c.us_cite, o.char_count
FROM opinions o JOIN clusters c ON c.cluster_id=o.cluster_id
WHERE o.char_count>0 ORDER BY o.char_count ASC LIMIT 5;

SELECT '== FTS SAMPLE: "necessary proper" ==' AS section;
SELECT DISTINCT c.case_name, c.us_cite
FROM opinions_fts f JOIN opinions o ON o.opinion_id=f.rowid
JOIN clusters c ON c.cluster_id=o.cluster_id
WHERE opinions_fts MATCH 'necessary proper';

SELECT '== EVERY DECISION (date, citation, chars) ==' AS section;
SELECT d.date_filed, d.case_name, d.us_cite,
       (SELECT sum(char_count) FROM opinions o WHERE o.cluster_id=d.cluster_id) AS chars
FROM scotus_decisions d ORDER BY d.date_filed, d.cluster_id;
