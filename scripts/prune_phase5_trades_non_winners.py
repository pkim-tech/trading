"""Delete-non-winners cleanup for phase5_trades/phase5_drought_windows (2026-09-13,
docs/watchlist_candidate_checklist.md check 19). candidate_full_review.gt_full_review_rows
now writes trades for the WHOLE curated population it processes during Phase 9 (~160
candidates/campaign) -- phase5_trades doubles as a temporary cache for that population
until a campaign's promotion decisions are finalized, at which point this script drops
the non-promoted candidates' rows, keeping only real winners long-term (see the checklist
entry for the full design rationale).

Same verify-then-delete convention as the prune-validation skill (docs: .claude/skills/
prune-validation/SKILL.md), scaled down to this table's much smaller real risk (a
row here is a re-derivable resimulation result, not backtest_cache's irreplaceable sweep
output) -- still: print exactly what will be deleted BEFORE deleting anything, require an
explicit --execute flag to actually delete (default is a dry-run report only), and print
before/after row counts so the caller can confirm the result matches expectation.

Winner resolution: a candidate_id counts as a winner if it appears in ANY
watch_list.label (live DB, archived or not -- a superseded/archived node's trade history
stays real trading-relevant history, not something to prune) via the real
"candidate_nodes id=<N>" substring promote_candidate.py (and its ad hoc-script
predecessors) always embed in the label -- see that script's own `label = f"sweep pick
(candidate_nodes id={...})"` construction. This is a real, load-bearing substring match,
not a guess: confirmed against the live DB it also matches promote_candidate.py's
predecessor scripts' differently-worded labels ("v5 promotion from candidate_nodes
id=...", "v5 resync from candidate_nodes id=..."), all of which share the same
"candidate_nodes id=<N>" substring. --keep-ids lets a caller pass an explicit additional
keep-list (e.g. a campaign's real promoted-candidate list from a planner dispatch)
instead of/in addition to relying on this pattern match -- useful since not every
historical promotion may have gone through label-embedding tooling.

Scope: always scoped to one candidate_nodes.version (a campaign) at a time -- deleting
across multiple campaigns in one pass would make the before/after counts harder to sanity-
check by eye, and there's no real need to prune more than one campaign at once.

Usage:
    # dry run (default) -- prints what would be deleted, deletes nothing
    .venv/bin/python scripts/prune_phase5_trades_non_winners.py --version "v6.5.2-..."

    # add explicit keep-ids on top of the watch_list-label auto-detected set
    .venv/bin/python scripts/prune_phase5_trades_non_winners.py --version "v6.5.2-..." \\
        --keep-ids 56298,55138,58312

    # actually delete
    .venv/bin/python scripts/prune_phase5_trades_non_winners.py --version "v6.5.2-..." --execute
"""
import argparse
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__)) + "/.."
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from run_optimization_sweep import DB_PATH  # noqa: E402

LIVE_DB_PATH = os.path.join(ROOT, "cache", "live", "trading_live.db")


def winners_from_watch_list_labels():
    """Every candidate_id referenced by ANY watch_list.label (any state, archived or
    not) via a real "candidate_nodes id=<N>" substring. Returns a set of ints."""
    conn = sqlite3.connect(LIVE_DB_PATH)
    rows = conn.execute(
        "SELECT label FROM watch_list WHERE label LIKE '%candidate_nodes id=%'").fetchall()
    conn.close()
    ids = set()
    for (label,) in rows:
        for m in re.finditer(r"candidate_nodes id=(\d+)", label or ""):
            ids.add(int(m.group(1)))
    return ids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True,
                     help="candidate_nodes.version string to scope the prune to (one campaign).")
    ap.add_argument("--keep-ids", default=None,
                     help="comma-separated candidate_ids to keep IN ADDITION to the "
                          "watch_list-label auto-detected winner set.")
    ap.add_argument("--no-auto-detect", action="store_true",
                     help="skip the watch_list-label auto-detect entirely -- keep ONLY "
                          "--keep-ids. Use when you want an explicit, fully-specified "
                          "keep-list rather than relying on label-parsing.")
    ap.add_argument("--execute", action="store_true",
                     help="actually delete. Default is a dry-run report only.")
    args = ap.parse_args()

    keep_ids = set()
    if not args.no_auto_detect:
        keep_ids |= winners_from_watch_list_labels()
    if args.keep_ids:
        keep_ids |= {int(x) for x in args.keep_ids.split(",") if x.strip()}

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    scope_ids = [r[0] for r in conn.execute(
        "SELECT id FROM candidate_nodes WHERE version=?", (args.version,)).fetchall()]
    scope_id_set = set(scope_ids)
    keep_in_scope = keep_ids & scope_id_set
    delete_ids = sorted(scope_id_set - keep_ids)

    print(f"Version: {args.version}")
    print(f"  candidate_nodes rows in scope: {len(scope_id_set)}")
    print(f"  keep-ids (winners, in scope):  {len(keep_in_scope)} -- {sorted(keep_in_scope)}")
    print(f"  candidates to delete:          {len(delete_ids)}")

    if not delete_ids:
        print("Nothing to delete.")
        return

    placeholders = ",".join("?" * len(delete_ids))
    pre_trades = conn.execute(
        f"SELECT COUNT(*) FROM phase5_trades WHERE candidate_id IN ({placeholders})",
        delete_ids).fetchone()[0]
    pre_windows = conn.execute(
        f"SELECT COUNT(*) FROM phase5_drought_windows WHERE candidate_id IN ({placeholders})",
        delete_ids).fetchone()[0]
    print(f"  phase5_trades rows to delete:          {pre_trades}")
    print(f"  phase5_drought_windows rows to delete: {pre_windows}")

    kept_trades_before = conn.execute(
        "SELECT COUNT(*) FROM phase5_trades WHERE candidate_id IN "
        f"(SELECT id FROM candidate_nodes WHERE version=?)", (args.version,)).fetchone()[0]

    if not args.execute:
        print("\nDRY RUN -- nothing deleted. Re-run with --execute to actually delete.")
        return

    conn.execute(f"DELETE FROM phase5_trades WHERE candidate_id IN ({placeholders})", delete_ids)
    conn.execute(f"DELETE FROM phase5_drought_windows WHERE candidate_id IN ({placeholders})", delete_ids)
    conn.commit()

    kept_trades_after = conn.execute(
        "SELECT COUNT(*) FROM phase5_trades WHERE candidate_id IN "
        f"(SELECT id FROM candidate_nodes WHERE version=?)", (args.version,)).fetchone()[0]
    remaining_ids = sorted({r[0] for r in conn.execute(
        f"SELECT DISTINCT candidate_id FROM phase5_trades WHERE candidate_id IN "
        f"(SELECT id FROM candidate_nodes WHERE version=?)", (args.version,)).fetchall()})

    print(f"\nDeleted. phase5_trades rows for this version: {kept_trades_before} -> {kept_trades_after}")
    print(f"Remaining distinct candidate_ids for this version: {remaining_ids}")
    unexpected = set(remaining_ids) - keep_in_scope
    if unexpected:
        print(f"WARNING: {len(unexpected)} candidate_id(s) remain that were NOT in the keep set "
              f"(should be empty): {sorted(unexpected)}")
    conn.close()


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
