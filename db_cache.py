import json
import sqlite3
import pandas as pd
from pathlib import Path

DB_PATH = str(Path(__file__).parent / "cache" / "trading_universe.db")


def _ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kv_cache (
            key        TEXT PRIMARY KEY,
            value      TEXT,
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)


def get_kv(key):
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_table(conn)
        row = conn.execute("SELECT value FROM kv_cache WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def set_kv(key, value):
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_table(conn)
        conn.execute(
            "INSERT OR REPLACE INTO kv_cache(key, value, updated_at) VALUES (?, ?, datetime('now'))",
            (key, json.dumps(value))
        )


def refresh_dropdown_cache():
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_table(conn)

        versions = [r[0] for r in conn.execute(
            "SELECT DISTINCT version FROM backtest_cache ORDER BY version DESC"
        ).fetchall()]
        set_kv("versions", versions)

        for v in versions:
            tickers = [r[0] for r in conn.execute(
                "SELECT DISTINCT ticker FROM backtest_cache WHERE version = ? ORDER BY ticker", (v,)
            ).fetchall()]
            strategies = [r[0] for r in conn.execute(
                "SELECT DISTINCT strategy FROM backtest_cache WHERE version = ? ORDER BY strategy", (v,)
            ).fetchall()]
            set_kv(f"tickers_{v}", tickers)
            set_kv(f"strategies_{v}", strategies)

            # strats_by_ticker for Spatial Topology
            strats_by_ticker = {}
            for t in tickers:
                strats_by_ticker[t] = strategies  # same strategies available for all tickers
            set_kv(f"strats_by_ticker_{v}", strats_by_ticker)

    print(f"Cached {len(versions)} versions: {versions}")


def refresh_pivot_cache():
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_table(conn)
        versions = [r[0] for r in conn.execute(
            "SELECT DISTINCT version FROM backtest_cache ORDER BY version DESC"
        ).fetchall()]

        for v in versions:
            print(f"  pivot cache: {v}...")

            df_cells = pd.read_sql_query("""
                SELECT ticker, window, COALESCE(z_score_threshold, 2.0) AS z,
                       trades, MAX(strategy_return) AS strategy_return
                FROM backtest_cache
                WHERE version = ? AND window IN (10, 20, 30)
                GROUP BY ticker, window, z_score_threshold, trades
            """, conn, params=(v,))

            # Best node per ticker for alpha/bh metadata
            df_meta = pd.read_sql_query("""
                WITH best AS (
                    SELECT ticker, strategy_return, alpha_vs_spy, asset_bh,
                           CASE WHEN asset_bh > 0 THEN strategy_return / asset_bh ELSE NULL END AS bh_mult,
                           ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY strategy_return DESC) AS rn
                    FROM backtest_cache
                    WHERE version = ? AND window IN (10, 20, 30)
                )
                SELECT ticker, alpha_vs_spy, asset_bh, bh_mult FROM best WHERE rn = 1
            """, conn, params=(v,))

            set_kv(f"pivot_cells_{v}", df_cells.to_dict(orient="records"))
            set_kv(f"pivot_meta_{v}", df_meta.to_dict(orient="records"))

    print(f"Pivot cache refreshed for {len(versions)} versions")


if __name__ == "__main__":
    refresh_dropdown_cache()
    refresh_pivot_cache()
