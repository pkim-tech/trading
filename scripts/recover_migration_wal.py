#!/usr/bin/env python3
"""
Recover trading_universe.db after the axis_tp migration was killed mid-script
(CREATE/INSERT/DROP already committed, RENAME never ran — see docs/session_cache.md).

Checkpoints the un-truncated WAL (grown to ~32GB from the kill) with a busy
timeout, then reports whether backtest_cache_new is a complete copy safe to
rename, or whether a restore from backup is needed instead.

Usage:
    python scripts/recover_migration_wal.py
"""

import sqlite3
from pathlib import Path

DB_PATH     = Path("./cache/trading_universe.db")
BACKUP_PATH = Path("./cache/trading_universe_pre_axis_tp.db.bak")
EXPECTED_TRAILINGBOTH = 75_658_063


def main():
    conn = sqlite3.connect(DB_PATH, timeout=120.0)
    print("Checkpointing WAL (this replays committed pages into the main file, may take a while)...")
    result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    print(f"wal_checkpoint(TRUNCATE) -> busy={result[0]} log_frames={result[1]} checkpointed={result[2]}")

    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    print("tables:", tables)

    if 'backtest_cache_new' not in tables:
        print("backtest_cache_new missing — restore from backup required.")
        conn.close()
        return

    n_total = conn.execute("SELECT COUNT(*) FROM backtest_cache_new").fetchone()[0]
    n_tb = conn.execute(
        "SELECT COUNT(*) FROM backtest_cache_new WHERE strategy='TrailingBothZScoreBreakout'"
    ).fetchone()[0]
    n_null_tp_tb = conn.execute(
        "SELECT COUNT(*) FROM backtest_cache_new WHERE strategy='TrailingBothZScoreBreakout' AND take_profit IS NULL"
    ).fetchone()[0]
    n_axis_tp_null = conn.execute("SELECT COUNT(*) FROM backtest_cache_new WHERE axis_tp IS NULL").fetchone()[0]
    conn.close()

    print(f"backtest_cache_new total rows: {n_total:,}")
    print(f"TrailingBothZScoreBreakout rows: {n_tb:,} (expected {EXPECTED_TRAILINGBOTH:,})")
    print(f"TrailingBoth rows with take_profit NULL: {n_null_tp_tb:,} (should equal {n_tb:,})")
    print(f"rows with axis_tp NULL: {n_axis_tp_null:,} (should be 0)")

    bak_conn = sqlite3.connect(f"file:{BACKUP_PATH}?mode=ro", uri=True)
    n_backup = bak_conn.execute("SELECT COUNT(*) FROM backtest_cache").fetchone()[0]
    bak_conn.close()
    print(f"backup backtest_cache row count: {n_backup:,}")

    complete = (n_tb == EXPECTED_TRAILINGBOTH and n_null_tp_tb == n_tb
                and n_axis_tp_null == 0 and n_total == n_backup)
    print()
    if complete:
        print("COMPLETE — safe to run: ALTER TABLE backtest_cache_new RENAME TO backtest_cache;")
    else:
        print("INCOMPLETE — do not rename. Restore trading_universe.db from the backup instead.")


if __name__ == '__main__':
    main()
