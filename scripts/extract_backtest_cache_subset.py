"""Extract a WHERE-clause-defined subset of backtest_cache into its own SQLite DB file.

Usage:
    .venv/bin/python scripts/extract_backtest_cache_subset.py <output_path> <where_clause> [source_db]

Example:
    .venv/bin/python scripts/extract_backtest_cache_subset.py \\
        output/backtest_cache_soxl_v6.db "ticker='SOXL' AND kernel_version='ground_truth_v6'"
"""
import sqlite3
import sys
import time

DEFAULT_SOURCE_DB = "cache/research/trading_universe.db"


def extract(output_path: str, where_clause: str, source_db: str = DEFAULT_SOURCE_DB) -> None:
    t0 = time.time()
    conn = sqlite3.connect(source_db, timeout=120)
    conn.execute("ATTACH DATABASE ? AS dest", (output_path,))
    schema_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='backtest_cache'"
    ).fetchone()[0]
    schema_sql = schema_sql.replace('"backtest_cache"', "backtest_cache", 1)
    conn.execute(schema_sql.replace("CREATE TABLE backtest_cache", "CREATE TABLE dest.backtest_cache", 1))
    conn.execute(f"INSERT INTO dest.backtest_cache SELECT * FROM backtest_cache WHERE {where_clause}")
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM dest.backtest_cache").fetchone()[0]
    conn.execute("DETACH DATABASE dest")
    conn.close()
    print(f"{output_path}: {n} rows ({time.time() - t0:.1f}s)")


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    extract(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else DEFAULT_SOURCE_DB)
