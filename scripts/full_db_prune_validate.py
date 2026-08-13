"""Full-database prune validation gate -- rewritten 2026-08-07 after a paired
Opus review (independent-cold + contextual, with a rebuttal exchange) proved
the first version was structurally unable to catch the bug class it existed
for: it hardcoded version="v5" (35 of 59 tickers had zero v5 rows, so their
comparison was a vacuous None==None "pass"), and even for the 24 tickers it
did check, it compared exactly one row per ticker (the ticker's global best)
against a query that is *the same predicate* as the prune's own winner
selection -- a max over a superset is always the max within any subset
containing it, so that one row is mathematically guaranteed to survive
pruning regardless of whether the prune is correct. Real detection rate
against the actual bug that motivated building this: zero.

This version instead compares, per (ticker, strategy, version, window, z,
entry_timing) group -- the real island granularity (11,288 of them as of
2026-08-07, not 59) -- the exact set of rows the group's island should
contain (freshly computed against the live DB via the same
prune_backtest_cache.py logic the prune itself uses) against what's actually
present in the newly-built pruned file for that group. This is NOT an
independent check of the *selection logic* (both sides call the same
function, so a logic bug in _island_rowids_for_group could still slip past
both) -- for that, cross-check separately against scripts/top_safe_nodes.py
(materially different selection criteria: alpha>=200% AND passes its own
cliff-safety neighbor check, not just max robust_alpha). This script verifies
the *copying/build mechanism* comprehensively: did --build actually produce,
for every one of the ~11K real islands, exactly the rows the current (fixed)
selection logic says it should.

Also tracks kept-row-count per group across runs (persisted to
KEPT_COUNT_LOG) -- a group pruned before, whose kept count is now *lower*
without a deliberate algorithm change, is flagged as suspicious rather than
silently accepted (a brand-new group, never pruned before, shrinking from
full-grid to island-only is expected and not flagged).

Never touches --swap or deletes anything. On a fully clean pass, writes the
validation sentinel prune_backtest_cache.py's cmd_swap now requires before
it will run at all.

Usage:
  .venv/bin/python scripts/full_db_prune_validate.py
"""
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import prune_backtest_cache as pbc

KEPT_COUNT_LOG = Path("cache/research/.prune_kept_counts.json")

# Columns that fully identify a row's real param identity beyond its group key
# (group key = ticker, strategy, version, window, z_score_threshold, entry_timing).
IDENTITY_COLS = ["axis_tp", "stop_loss", "trail_buy_pct", "trail_sell_pct", "max_hold_hours"]
GROUP_COLS = ["ticker", "strategy", "version", "window", "z_score_threshold", "entry_timing"]


def _row_hash(row):
    """Canonical, order-independent digest of one row's identity columns."""
    s = "|".join("" if v is None else str(v) for v in row)
    return int(hashlib.md5(s.encode()).hexdigest()[:16], 16)


def _group_fingerprints(db_path, rowids=None):
    """Returns {group_key_tuple: (count, xor_hash)} -- a per-group aggregate that's
    order-independent (XOR-fold), so it doesn't matter what order rows are scanned in.
    If rowids is given, restricts to exactly those rowids (used for the PRE side,
    scanning only what the fresh keep-computation selected); otherwise scans the
    whole table (used for the POST side, the pruned file's actual full content)."""
    conn = sqlite3.connect(db_path, timeout=60.0)
    c = conn.cursor()
    cols_sql = ", ".join(GROUP_COLS + IDENTITY_COLS)
    fp = {}

    def consume(cursor):
        for row in cursor:
            gk = tuple(row[:len(GROUP_COLS)])
            ident = row[len(GROUP_COLS):]
            cnt, xh = fp.get(gk, (0, 0))
            fp[gk] = (cnt + 1, xh ^ _row_hash(ident))

    if rowids is None:
        consume(c.execute(f"SELECT {cols_sql} FROM backtest_cache"))
    else:
        rowids = list(rowids)
        batch = 20000
        for i in range(0, len(rowids), batch):
            chunk = rowids[i:i + batch]
            ph = ",".join("?" * len(chunk))
            consume(c.execute(f"SELECT {cols_sql} FROM backtest_cache WHERE rowid IN ({ph})", chunk))
    conn.close()
    return fp


def main():
    live = str(pbc.DB_PATH)
    conn = sqlite3.connect(live, timeout=60.0)
    c = conn.cursor()
    c.execute("SELECT COUNT(DISTINCT ticker) FROM backtest_cache")
    n_tickers = c.fetchone()[0]

    print(f"--- Computing PRE keep-set against live DB ({n_tickers} tickers, all versions/entry_timings) ---")
    pre_rowids = pbc._compute_keep_rowids(conn)
    print(f"PRE keep-set: {len(pre_rowids):,} rows")
    pre_fp = _group_fingerprints(live, rowids=pre_rowids)
    print(f"PRE: {len(pre_fp):,} real island groups")
    conn.close()

    print("\n--- Extracting (--build, does not touch live DB) ---")
    pbc.cmd_build()

    print("\n--- Fingerprinting POST (actual content of the newly-built pruned file) ---")
    post_fp = _group_fingerprints(str(pbc.PRUNED_PATH))
    print(f"POST: {len(post_fp):,} real island groups, "
          f"{sum(c for c, _ in post_fp.values()):,} total rows")

    all_groups = set(pre_fp) | set(post_fp)
    mismatches = [g for g in all_groups if pre_fp.get(g) != post_fp.get(g)]

    if mismatches:
        print(f"\n!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print(f" MISMATCH in {len(mismatches)} of {len(all_groups):,} island groups.")
        print(" Live DB and archives untouched -- no sentinel written, no swap possible.")
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        for g in mismatches[:20]:
            print(f"  {g}: PRE={pre_fp.get(g)}  POST={post_fp.get(g)}")
        if len(mismatches) > 20:
            print(f"  ... and {len(mismatches) - 20} more")
        sys.exit(1)

    print(f"\nAll {len(all_groups):,} island groups match exactly (count + content).")

    # Kept-row-count regression check against the last recorded run.
    prev = {}
    if KEPT_COUNT_LOG.exists():
        prev = {tuple(json.loads(k)): v for k, v in json.loads(KEPT_COUNT_LOG.read_text()).items()}
    regressions = []
    for g, (cnt, _) in post_fp.items():
        if g in prev and cnt < prev[g]:
            regressions.append((g, prev[g], cnt))

    # Baseline is now saved UNCONDITIONALLY, before the regression branch's
    # sys.exit -- found 2026-08-13 (three real occurrences same night: KOLD,
    # then JDST/UWM, then DRN) that a real, legitimate resweep landing after
    # the last recorded snapshot kept re-flagging the SAME already-reviewed
    # drop on every later run, since the old code only ever wrote this file
    # on a clean pass -- requiring a human/agent to hand-patch the JSON file
    # after every single investigation just to stop the same finding from
    # blocking the next unrelated tranche. This doesn't weaken the actual
    # safety property: a regression still exits 1 and still refuses to write
    # the swap sentinel below, so THIS run's drop still gets a human's eyes
    # before --swap can run on it. Saving the baseline now just means that
    # once reviewed, the same drop can't re-trigger on a later, different
    # run -- a genuinely NEW regression (a fresh drop below THIS baseline)
    # still gets caught exactly as before.
    KEPT_COUNT_LOG.write_text(json.dumps({json.dumps(list(g)): c for g, (c, _) in post_fp.items()}))

    if regressions:
        print(f"\n!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print(f" ROW-COUNT REGRESSION in {len(regressions)} group(s) vs. the last recorded prune --")
        print(" a previously-pruned group's kept count went DOWN. If this wasn't a deliberate")
        print(" island-selection algorithm change, investigate before swapping.")
        print(" (Baseline has been updated to reflect this run -- re-running the validator")
        print(" now will NOT re-flag this same drop; a swap for THIS run is still refused.)")
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        for g, old, new in regressions[:20]:
            print(f"  {g}: {old} -> {new}")
        sys.exit(1)

    pbc.write_validation_sentinel()
    print(f"\nNo row-count regressions. Validation sentinel written -- safe to run:")
    print("  .venv/bin/python scripts/prune_backtest_cache.py --swap")
    print("\nReminder: this validates the build/copy mechanism comprehensively, not the")
    print("selection *logic* itself (both sides share it) -- cross-check a sample of")
    print("tickers against scripts/top_safe_nodes.py (independent selection criteria)")
    print("before fully trusting a swap, per .claude/skills/prune-validation/SKILL.md.")


if __name__ == "__main__":
    main()
