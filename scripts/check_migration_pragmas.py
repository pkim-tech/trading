#!/usr/bin/env python3
"""
Report SQLite pragma/connection state for trading_universe.db — used to judge
whether a bigger cache_size would speed up the in-progress axis_tp migration.

Usage:
    python scripts/check_migration_pragmas.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path("./cache/trading_universe.db")


def main():
    conn = sqlite3.connect(DB_PATH)
    cache_pages = conn.execute("PRAGMA cache_size").fetchone()[0]
    page_size   = conn.execute("PRAGMA page_size").fetchone()[0]
    journal     = conn.execute("PRAGMA journal_mode").fetchone()[0]
    synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]

    cache_bytes = abs(cache_pages) * (page_size if cache_pages > 0 else 1024)
    print(f"cache_size:  {cache_pages} ({'pages' if cache_pages > 0 else 'KB'}) "
          f"≈ {cache_bytes / 1e6:.1f} MB" if cache_pages > 0 else
          f"cache_size:  {cache_pages} KB ≈ {abs(cache_pages) / 1e3:.1f} MB")
    print(f"page_size:   {page_size} bytes")
    print(f"journal_mode:{journal}")
    print(f"synchronous: {synchronous}")

    try:
        n = conn.execute("SELECT COUNT(*) FROM backtest_cache").fetchone()[0]
        print(f"backtest_cache row count (whichever table has that name right now): {n:,}")
    except Exception as e:
        print(f"backtest_cache: {e}")

    try:
        n_new = conn.execute("SELECT COUNT(*) FROM backtest_cache_new").fetchone()[0]
        print(f"backtest_cache_new row count (migration target, in progress): {n_new:,}")
    except Exception as e:
        print(f"backtest_cache_new: {e}")


if __name__ == '__main__':
    main()
