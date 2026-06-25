# Human review of the 206 REVIEW candidates (1790–1820)

The automated filter routed 206 clusters to REVIEW (Dallas vols 1–4, no `scdb_id`).
This is the manual adjudication of that bucket. **Goal of the pass: catch false
negatives — genuine SCOTUS decisions wrongly excluded.**

> **Note (post-dedup):** the dataset was later regenerated from the `clusters`
> endpoint with duplicate-cluster removal; REVIEW is now **205** canonical records
> (one was a duplicate). The adjudication below is unchanged — all REVIEW records
> are non-SCOTUS, and no genuine decision was wrongly excluded.

## Result

| Disposition | Count | Meaning |
|-------------|------:|---------|
| `DROP-not-scotus` | 168 | Pennsylvania state-court + U.S. Circuit Court (PA) cases reported by Dallas |
| `DROP-duplicate`  | 35  | CourtListener stub/placeholder records (duplicate fragments of real cases, or broken records) |
| `KEEP?-admin`     | 3   | SCOTUS administrative orders — Court *actions*, not *decisions* (judgment call) |

**No genuine SCOTUS decided opinion was found in the REVIEW bucket.** The
`scdb_id`-based filter already captured the early Court's decisions completely
(the Supreme Court Database Legacy is comprehensive for 1791+). Per-disposition
detail is in `review_dispositions.csv`.

## Why these are not SCOTUS

Dallas reported three different courts in vols 2–4; CourtListener tags them all
`court=scotus`. The non-SCOTUS ones are distinguishable by content:

- **Pennsylvania prosecutions** — captioned *Respublica v. …* or *Commonwealth v. …*.
- **PA civil / Philadelphia commercial** — incl. marine-insurance cases; these are
  **jury trials**, which the Supreme Court does not hold (decisive tell).
- **Federal circuit cases** — *United States v. …* criminal matters (e.g. the 1795
  Whiskey Rebellion treason trials, *U.S. v. Worrall*, *U.S. v. Fries*) heard by
  Justices riding circuit in the U.S. Circuit Court for Pennsylvania.
- **Ejectment / land cases** — *X's Lessee v. Y* (e.g. *Huidekoper's Lessee v.
  Douglass*, explicitly labeled "Circuit Court, Pennsylvania District").

### Spot-checks (read in full text, not just metadata)

The CourtListener HTML header stamps "Supreme Court of United States" on *every*
Dallas reprint, so the header is unreliable. These were confirmed from opinion **content**:

| Case | Cite | Evidence | Verdict |
|------|------|----------|---------|
| U.S. v. Worrall | 2 U.S. 384 | federal criminal indictment tried in PA | circuit |
| Russel v. Union Insurance | 4 U.S. 421 | "Washington, Justice" charging a **jury** | circuit |
| Huidekoper's Lessee v. Douglass | 4 U.S. 392 | header "Circuit Court, Pennsylvania District" | circuit |
| Hollingsworth v. Adams | 2 U.S. 396 | writ quashed; p.396 < 401 (2 Dall. SCOTUS starts at 401) | PA/circuit |
| Ex parte Hallowell | 3 U.S. 410 | text is a modern **9th-Circuit cert denial** mis-attached to an 1799 cite | broken record |

## The one judgment call: 3 administrative orders

`84677` Appointment of Justices · `84678` Qualification of Counsellors & Attorneys ·
`84682` Appointment of Paterson.

These are genuine acts of the Supreme Court but are administrative minutes, not
adjudicated decisions, which is why SCDB does not catalog them. Whether to include
them depends on the corpus definition:
- **Corpus = "SCOTUS decisions/opinions"** → exclude (keep them dropped). Clean set = **870**.
- **Corpus = "everything the Court did"** → add them to KEEP. Clean set = **873**.

Default applied: **excluded** (the 870-case KEEP set is unchanged). Flip by moving
the 3 cluster_ids if the broader definition is wanted.
