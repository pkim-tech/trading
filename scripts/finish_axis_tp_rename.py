#!/usr/bin/env python3
"""
Final step of the axis_tp migration recovery (see scripts/recover_migration_wal.py) —
renames the already-verified-complete backtest_cache_new to backtest_cache and
rebuilds the indexes that were dropped along with the old table.

Usage:
    python scripts/finish_axis_tp_rename.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path("./cache/trading_universe.db")


def main():
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if 'backtest_cache' in tables:
        raise SystemExit("backtest_cache already exists — nothing to rename, aborting.")
    if 'backtest_cache_new' not in tables:
        raise SystemExit("backtest_cache_new not found — nothing to rename, aborting.")

    conn.execute("ALTER TABLE backtest_cache_new RENAME TO backtest_cache")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bc_version_window ON backtest_cache(version, window)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bc_version_ticker_strategy ON backtest_cache(version, ticker, strategy)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bc_version_return ON backtest_cache(version, strategy_return)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bc_ticker ON backtest_cache(ticker)")
    conn.commit()

    n = conn.execute("SELECT COUNT(*) FROM backtest_cache").fetchone()[0]
    print(f"Renamed backtest_cache_new -> backtest_cache. Row count: {n:,}. Indexes rebuilt.")
    conn.close()


if __name__ == '__main__':
    main()
