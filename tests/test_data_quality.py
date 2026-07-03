"""Data-quality assertions against the loaded SQLite database.

These confirm the corpus is complete and internally consistent — the machine-checked
counterpart to eyeballing the data in Datasette / `make inspect`."""

from config import settings
from src import transform as t


def _scalar(db, sql, params=()):
    return db.execute(sql, params).fetchone()[0]


def test_counts(db):
    assert _scalar(db, "SELECT count(*) FROM clusters") == 1076
    assert _scalar(db, "SELECT count(*) FROM scotus_decisions") == 663
    assert (
        _scalar(
            db, "SELECT count(*) FROM clusters WHERE bucket='REVIEW' AND dedup_role='canonical'"
        )
        == 205
    )
    assert _scalar(db, "SELECT count(*) FROM clusters WHERE dedup_role='duplicate'") == 208


def test_referential_integrity(db):
    assert (
        _scalar(
            db,
            "SELECT count(*) FROM opinions o "
            "LEFT JOIN clusters c ON c.cluster_id=o.cluster_id "
            "WHERE c.cluster_id IS NULL",
        )
        == 0
    )
    assert (
        _scalar(
            db,
            "SELECT count(*) FROM clusters x "
            "WHERE x.dup_of IS NOT NULL AND x.dup_of NOT IN "
            "(SELECT cluster_id FROM clusters)",
        )
        == 0
    )
    assert (
        _scalar(
            db,
            "SELECT count(*) FROM citations ci "
            "LEFT JOIN clusters c ON c.cluster_id=ci.cluster_id "
            "WHERE c.cluster_id IS NULL",
        )
        == 0
    )


def test_every_decision_has_text(db):
    """Every canonical SCOTUS decision has at least one opinion with non-empty text."""
    textless = _scalar(
        db,
        "SELECT count(*) FROM scotus_decisions d WHERE NOT EXISTS ("
        "  SELECT 1 FROM opinions o WHERE o.cluster_id=d.cluster_id "
        "  AND length(trim(o.plain_text)) > 0)",
    )
    assert textless == 0


def test_filter_rule_invariant(db):
    """No canonical KEEP decision violates the rule (vol<5 must have an scdb_id)."""
    bad = _scalar(
        db,
        "SELECT count(*) FROM scotus_decisions "
        "WHERE (volume IS NULL OR volume < 5) "
        "AND (scdb_id IS NULL OR scdb_id='')",
    )
    assert bad == 0


def test_no_residual_duplicates(db):
    """Among canonical decisions, no two share a U.S. citation with high name overlap."""
    rows = db.execute(
        "SELECT us_cite, case_name FROM scotus_decisions WHERE us_cite <> ''"
    ).fetchall()
    by_cite = {}
    for cite, name in rows:
        by_cite.setdefault(cite, []).append(name)
    for cite, names in by_cite.items():
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = t._toks(names[i]), t._toks(names[j])
                if a and b:
                    assert len(a & b) / len(a | b) < 0.5, (
                        f"residual dup at {cite}: {names[i]} ~ {names[j]}"
                    )


def test_per_year_tracks_wikipedia(db):
    rows = db.execute(
        "SELECT substr(date_filed,1,4) y, count(*) FROM scotus_decisions GROUP BY y"
    ).fetchall()
    yk = {int(y): n for y, n in rows if y}
    total = sum(yk.values())
    assert total == 663
    # every year within a small tolerance of the historical annual count
    for y, wiki in settings.WIKI_ANNUAL.items():
        assert abs(yk.get(y, 0) - wiki) <= 8, f"{y}: keep={yk.get(y, 0)} wiki={wiki}"


def test_landmark_cases_present(db):
    for name in ["McCulloch", "Marbury", "Martin v. Hunter", "Fletcher", "Gibbons"]:
        assert (
            _scalar(
                db, "SELECT count(*) FROM scotus_decisions WHERE case_name LIKE ?", (f"%{name}%",)
            )
            >= 1
        ), f"missing {name}"


def test_fts_finds_mcculloch(db):
    names = [
        r[0]
        for r in db.execute(
            "SELECT c.case_name FROM opinions_fts f "
            "JOIN opinions o ON o.opinion_id=f.rowid "
            "JOIN clusters c ON c.cluster_id=o.cluster_id "
            "WHERE opinions_fts MATCH 'necessary proper'"
        ).fetchall()
    ]
    assert any("McCulloch" in n for n in names)
