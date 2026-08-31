"""One-time migration: backfill phase4_results from the OLD candidate_nodes.core_safe/
addon_safe TEXT columns -- 2026-08-30, paired-review HIGH finding.

Real gap this closes: the first version of tonight's Phase4-verdict-persistence feature
wrote core_safe/addon_safe as two new TEXT "True"/"False"/NULL columns directly on
candidate_nodes. A same-day follow-up consolidated that onto the PRE-EXISTING
phase4_results table (candidate_verification_store.py) instead, to avoid a second,
unsynced source of truth -- but candidate_nodes.core_safe/addon_safe are no longer read
by anything after that consolidation, so every real verdict persisted there BEFORE the
consolidation landed (confirmed real: 729 rows, 636 of them WEBL under
'bench-inmemory-...-isl10') would otherwise silently go invisible to the Phase5
SAFE/SAFE gate -- which reads phase4_results exclusively now, sees no row at all for
those candidate_ids, and treats them as "unverified," defeating the entire point of the
gate for real, already-completed work.

Only migrates a candidate_id that has NO existing phase4_results row yet (never
overwrites a real phase4_results row that already exists, e.g. from
run_candidate_nodes_campaign_verification.py's own SOXL rows) -- upsert_phase4's own
merge-with-existing-row semantics (see that function's own docstring) make this safe to
re-run idempotently even if some candidate_ids already have a phase4_results row: a
second run just re-merges the same core_safe/addon_safe values in, changing nothing.

Every OTHER phase4_results column (check4/8/11/13, addon_cagr, drought) is left NULL for
a migrated row -- the old TEXT-column design never tracked them, so there's nothing to
migrate for those fields. This is a real, acceptable gap for a migrated row (not silently
wrong): if a future report wants those fields for a pre-consolidation candidate, it can
still find them in candidate_nodes.phase4_checklist_json, which the same original run
also wrote (see candidate_summary_report._persist_phase4_verdicts_and_checklist).

Usage:
  .venv/bin/python scripts/migrate_core_safe_to_phase4_results.py           # real run
  .venv/bin/python scripts/migrate_core_safe_to_phase4_results.py --dry-run # report only
"""
import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

DB_PATH = "cache/research/trading_universe.db"


def find_rows_to_migrate(conn):
    """Every candidate_nodes row with a real (non-NULL) old-style TEXT core_safe/
    addon_safe value that has no phase4_results row yet. Returns list of
    (id, core_safe_text, addon_safe_text)."""
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(candidate_nodes)")}
    if "core_safe" not in existing_cols:
        return []
    has_phase4_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='phase4_results'").fetchone()
    not_in_sql = ("AND cn.id NOT IN (SELECT candidate_id FROM phase4_results)"
                  if has_phase4_table else "")
    return conn.execute(f"""
        SELECT cn.id, cn.core_safe, cn.addon_safe
        FROM candidate_nodes cn
        WHERE (cn.core_safe IS NOT NULL OR cn.addon_safe IS NOT NULL)
        {not_in_sql}
    """).fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report what would migrate, write nothing")
    ap.add_argument("--db", default=DB_PATH)
    args = ap.parse_args()

    from candidate_verification_store import ensure_phase4_table, upsert_phase4

    conn = sqlite3.connect(args.db)
    try:
        ensure_phase4_table(conn)
        rows = find_rows_to_migrate(conn)
        print(f"Found {len(rows)} candidate_nodes row(s) with an old-style core_safe/"
              f"addon_safe TEXT verdict and no phase4_results row yet.")
        if args.dry_run:
            for cid, core_safe, addon_safe in rows[:10]:
                print(f"  would migrate id={cid}: core_safe={core_safe!r} addon_safe={addon_safe!r}")
            if len(rows) > 10:
                print(f"  ... and {len(rows) - 10} more")
            return

        n_migrated = 0
        for cid, core_safe, addon_safe in rows:
            fields = {
                "core_safe": None if core_safe is None else (core_safe == "True"),
                "addon_safe": None if addon_safe is None else (addon_safe == "True"),
            }
            upsert_phase4(conn, cid, fields)
            n_migrated += 1
        print(f"Migrated {n_migrated} row(s) into phase4_results.")
    finally:
        conn.close()


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
