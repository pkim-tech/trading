import json
import sqlite3
import pandas as pd
from pathlib import Path

DB_PATH = str(Path(__file__).parent / "cache" / "research" / "trading_universe.db")


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


def refresh_pivot_cache(versions=None):
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_table(conn)
        if versions is None:
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


# Qualified 3x index tickers, holds collapsed to best alpha per (tp, sl) node.
# Shared by the Top Pivot cliff-safe section (live fallback) and the sweep-end refresh.
CLIFF_GRID_SQL = """
    SELECT b.ticker, b.strategy, b.version, b.window,
           COALESCE(b.z_score_threshold, 2.0) AS z,
           b.axis_tp AS take_profit, b.stop_loss,
           MAX(b.alpha_vs_spy) AS max_alpha,
           MAX(b.asset_bh)     AS bh
    FROM backtest_cache b
    JOIN (
        SELECT symbol FROM tickers
        WHERE leverage = 3
          AND (inverse IS NULL OR inverse = 0)
          AND index_underlier IS NOT NULL AND index_underlier != ''
          AND (dupe_direxion IS NULL OR dupe_direxion = '')
          AND avg_vol_10d IS NOT NULL AND last_price IS NOT NULL
          AND avg_vol_10d * last_price >= 5000000
    ) q ON q.symbol = b.ticker
    WHERE b.trades >= ?
    GROUP BY b.ticker, b.strategy, b.version, b.window,
             COALESCE(b.z_score_threshold, 2.0), b.axis_tp, b.stop_loss
"""


def load_cliff_grid(min_trades=5):
    """kv-cached at sweep completion; falls back to the heavy live query (~2 min)."""
    cached = get_kv(f"cliff_grid_mt{min_trades}")
    if cached is not None:
        return pd.DataFrame(cached)
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql_query(CLIFF_GRID_SQL, conn, params=(min_trades,))


def refresh_cliff_grid_cache(min_trades=5):
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_table(conn)
        df = pd.read_sql_query(CLIFF_GRID_SQL, conn, params=(min_trades,))
    set_kv(f"cliff_grid_mt{min_trades}", df.to_dict(orient="records"))
    print(f"Cliff grid cache refreshed ({len(df):,} nodes, min_trades={min_trades})")


def refresh_best_nodes_cache():
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_table(conn)
        versions = [r[0] for r in conn.execute(
            "SELECT DISTINCT version FROM backtest_cache ORDER BY version DESC"
        ).fetchall()]

        for v in versions:
            print(f"  best_nodes cache: {v}...")
            rows = conn.execute("""
                WITH best AS (
                    SELECT ticker, window, COALESCE(z_score_threshold, 2.0) AS z,
                           axis_tp, stop_loss, max_hold_hours,
                           ROW_NUMBER() OVER (
                               PARTITION BY ticker, window, COALESCE(z_score_threshold, 2.0)
                               ORDER BY alpha_vs_spy DESC
                           ) AS rn
                    FROM backtest_cache WHERE version = ?
                )
                SELECT ticker, window, z, axis_tp, stop_loss, max_hold_hours
                FROM best WHERE rn = 1
            """, (v,)).fetchall()
            data = {f"{r[0]}|{int(r[1])}|{float(r[2])}": [int(r[3]), int(r[4]), int(r[5])] for r in rows}
            set_kv(f"best_nodes_{v}", data)

    print(f"Best nodes cache refreshed for {len(versions)} versions")


if __name__ == "__main__":
    refresh_dropdown_cache()
    refresh_pivot_cache()
    refresh_best_nodes_cache()
