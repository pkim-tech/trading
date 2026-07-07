#!/usr/bin/env python3
"""
Post-kill diagnostic for the axis_tp backtest_cache migration.

Checks whether backtest_cache_new has the full row count (INSERT...SELECT
completed before DROP TABLE backtest_cache ran, just missing the final
RENAME) or is a partial/incomplete copy.

Usage:
    python scripts/check_migration_kill_state.py
"""

import sqlite3
from pathlib import Path

DB_PATH      = Path("./cache/trading_universe.db")
BACKUP_PATH  = Path("./cache/trading_universe_pre_axis_tp.db.bak")
EXPECTED_TRAILINGBOTH = 75_658_063


def main():
    conn = sqlite3.connect(DB_PATH)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    print("tables in trading_universe.db:", tables)

    if 'backtest_cache_new' not in tables:
        print("backtest_cache_new does not exist — nothing to recover, must restore from backup.")
        return

    n_total = conn.execute("SELECT COUNT(*) FROM backtest_cache_new").fetchone()[0]
    print(f"backtest_cache_new total rows: {n_total:,}")

    n_tb = conn.execute(
        "SELECT COUNT(*) FROM backtest_cache_new WHERE strategy='TrailingBothZScoreBreakout'"
    ).fetchone()[0]
    print(f"backtest_cache_new TrailingBothZScoreBreakout rows: {n_tb:,} (expected {EXPECTED_TRAILINGBOTH:,})")

    n_null_take_profit_tb = conn.execute(
        "SELECT COUNT(*) FROM backtest_cache_new WHERE strategy='TrailingBothZScoreBreakout' AND take_profit IS NULL"
    ).fetchone()[0]
    n_axis_tp_null = conn.execute(
        "SELECT COUNT(*) FROM backtest_cache_new WHERE axis_tp IS NULL"
    ).fetchone()[0]
    print(f"TrailingBoth rows with take_profit NULL: {n_null_take_profit_tb:,} (should equal {n_tb:,})")
    print(f"rows with axis_tp NULL: {n_axis_tp_null:,} (should be 0)")

    print()
    bak_conn = sqlite3.connect(f"file:{BACKUP_PATH}?mode=ro", uri=True)
    n_backup = bak_conn.execute("SELECT COUNT(*) FROM backtest_cache").fetchone()[0]
    print(f"backup (pre-migration) backtest_cache row count: {n_backup:,}")

    complete = (n_tb == EXPECTED_TRAILINGBOTH and n_null_take_profit_tb == n_tb
                and n_axis_tp_null == 0 and n_total == n_backup)
    print()
    print("COMPLETE — safe to rename backtest_cache_new -> backtest_cache" if complete
          else "INCOMPLETE — do not rename, restore from backup instead")


if __name__ == '__main__':
    main()
