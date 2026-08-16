"""Stage-3 (backtest-change-rollout skill) storage sizing forecast for a proposed
backtest_cache campaign -- built for the rolling-window SOXL quarterly-resweep
campaign (docs/deep_backlog.md's 2026-08-16 date-range entry), but reusable for
any future campaign since the skill's Stage 3 gate applies to every one.

Prints:
  - Current backtest_cache size on disk (table + its indexes, via the sqlite
    dbstat virtual table -- exact, not an estimate) and approximate row count
    (SELECT MAX(rowid), per the skill's guidance: COUNT(*) can time out on this
    table via a full scan).
  - Expected new-row count for a proposed campaign, via the Phase 1 formula the
    skill documents: z_thresholds x windows x take_profits x stop_losses x
    hold_time_caps x trail_pcts, per (ticker, fixed_sl, entry_timing) combo,
    multiplied by however many combos (tickers x windows x fixed_sl values) are
    in scope. Phase2/2.5 are NOT included in this formula -- the skill has no
    closed-form size for them (targeted, best-node-relative search, unlike
    Phase1's full mesh) -- flagged explicitly as an unquantified addition, not
    silently ignored.
  - Projected disk growth using the CURRENT real bytes/row (table+indexes),
    not a guessed constant -- so the forecast tracks actual index overhead
    (observed ~1.87x table-only size from the 5 covering indexes) rather than
    assuming it away.

Does NOT check `df -h` for available space and present it as ground truth --
per the skill: WSL2's vhdx free-space report can overstate real host headroom
by a large margin (confirmed 2026-07-20: df said 826GB free, real available was
113GB). Prints the raw df number as a data point only, with the caveat inline,
and expects the user to confirm real available space separately.

Usage: .venv/bin/python scripts/backtest_cache_storage_forecast.py \\
           --tickers SOXL --windows 9 --z 3 --w 2 --tp 14 --sl 14 --hold 20 --tpct 7 --fixed-sl-count 1
"""
import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from run_optimization_sweep import DB_PATH


def current_backtest_cache_size():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    cur = conn.cursor()
    cur.execute("SELECT MAX(rowid) FROM backtest_cache")
    row_count = cur.fetchone()[0] or 0
    cur.execute("""
        SELECT name, SUM(pgsize) FROM dbstat
        WHERE aggregate=TRUE AND (name='backtest_cache' OR name LIKE 'idx_bc_%' OR name='sqlite_autoindex_backtest_cache_1')
        GROUP BY name
    """)
    rows = cur.fetchall()
    conn.close()
    table_bytes = sum(b for n, b in rows if n == 'backtest_cache')
    total_bytes = sum(b for _, b in rows)
    return row_count, table_bytes, total_bytes, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tickers', type=int, default=1, help='number of tickers in scope')
    ap.add_argument('--windows-count', type=int, default=9, help='number of date-windows (quarterly steps etc.)')
    ap.add_argument('--fixed-sl-count', type=int, default=1, help='number of distinct fixed_sl values swept')
    ap.add_argument('--z', type=int, required=True, help='len(z_score_thresholds)')
    ap.add_argument('--w', type=int, required=True, help='len(windows) hyperparameter axis (indicator window, not date-window)')
    ap.add_argument('--tp', type=int, required=True, help='len(take_profits)')
    ap.add_argument('--sl', type=int, required=True, help='len(stop_losses)')
    ap.add_argument('--hold', type=int, required=True, help='len(hold_time_caps)')
    ap.add_argument('--tpct', type=int, default=1, help='len(trail_pcts), 1 if strategy has no 4th axis')
    args = ap.parse_args()

    row_count, table_bytes, total_bytes, rows = current_backtest_cache_size()
    bytes_per_row_table_only = table_bytes / row_count if row_count else 0
    bytes_per_row_incl_indexes = total_bytes / row_count if row_count else 0

    print("=== Current backtest_cache state ===")
    print(f"  Approx row count (MAX(rowid)): {row_count:,}")
    print(f"  Table-only size:               {table_bytes / 1e9:.3f} GB  ({bytes_per_row_table_only:.1f} bytes/row)")
    print(f"  Table + all indexes:           {total_bytes / 1e9:.3f} GB  ({bytes_per_row_incl_indexes:.1f} bytes/row)")
    print("  Breakdown:")
    for name, b in sorted(rows, key=lambda r: -r[1]):
        print(f"    {name:40s} {b / 1e9:.3f} GB")

    per_combo_phase1 = args.z * args.w * args.tp * args.sl * args.hold * args.tpct
    n_combos = args.tickers * args.windows_count * args.fixed_sl_count
    phase1_rows = per_combo_phase1 * n_combos

    print(f"\n=== Proposed campaign: Phase 1 formula ===")
    print(f"  Per-combo Phase1 rows = z({args.z}) x w({args.w}) x tp({args.tp}) x sl({args.sl}) "
          f"x hold({args.hold}) x tpct({args.tpct}) = {per_combo_phase1:,}")
    print(f"  Combos = tickers({args.tickers}) x date-windows({args.windows_count}) "
          f"x fixed_sl values({args.fixed_sl_count}) = {n_combos:,}")
    print(f"  Phase1 total new rows (upper bound, ignoring any pre-existing cache hits): {phase1_rows:,}")
    print(f"  NOTE: Phase2/Phase2.5 add MORE rows on top of this (targeted island/cliff-box search "
          f"around the best Phase1 node) -- no closed-form size for those in the skill; treat this "
          f"as a Phase1-only lower bound, not the full campaign total.")

    growth_table_only = phase1_rows * bytes_per_row_table_only
    growth_incl_indexes = phase1_rows * bytes_per_row_incl_indexes
    print(f"\n=== Projected disk growth (Phase1 rows only, at CURRENT observed bytes/row) ===")
    print(f"  Table-only:      {growth_table_only / 1e9:.3f} GB")
    print(f"  Table+indexes:   {growth_incl_indexes / 1e9:.3f} GB")

    total, used, free = shutil.disk_usage(str(DB_PATH.parent))
    print(f"\n=== df-reported free space on {DB_PATH.parent} (RAW, DO NOT TRUST AT FACE VALUE ON WSL2) ===")
    print(f"  Filesystem total: {total / 1e9:.1f} GB   Used: {used / 1e9:.1f} GB   'Free': {free / 1e9:.1f} GB")
    print("  CAVEAT (confirmed 2026-07-20): WSL2's vhdx free-space report can overstate real host\n"
          "  headroom by a large margin (df said 826GB free, real available was 113GB that time).\n"
          "  This number is NOT a substitute for asking the user their real available disk space.")


if __name__ == '__main__':
    main()
