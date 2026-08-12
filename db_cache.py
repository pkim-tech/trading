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


def _ensure_mutation_log_table(conn):
    # data_mutation_log: traceability (not full immutability/versioning, per the
    # user's 2026-07-22 call -- see docs/research_log.md's 2026-07-22 entry) for
    # every split-guard rescale of a cached *_1h.csv file. The rescale itself is
    # scale-invariant to the %-based signals every strategy trades on, so re-running
    # the same code against today's cache should reproduce a past backtest_cache
    # number without needing the exact old bytes -- this table exists so the *fact*
    # that a rescale happened (when/why/how) is never silently lost, and the actual
    # pre-rescale data is still recoverable via pre_mutation_snapshot if ever needed.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS data_mutation_log (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker               TEXT NOT NULL,
            factor               REAL NOT NULL,
            detected_at          TEXT NOT NULL DEFAULT (datetime('now')),
            overlap_bar_time     TEXT,
            price_before         REAL,
            price_after          REAL,
            notes                TEXT,
            pre_mutation_snapshot TEXT
        )
    """)


def log_data_mutation(ticker, factor, overlap_bar_time, price_before, price_after,
                       notes, pre_mutation_df):
    """Records one split-guard rescale event, with the full pre-rescale
    DataFrame (CSV-serialized) so the actual old data is recoverable, not
    just the fact that it changed. Called only from data_manager.py's rescale
    branch, right before it overwrites df_local in place."""
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_mutation_log_table(conn)
        conn.execute("""
            INSERT INTO data_mutation_log
                (ticker, factor, overlap_bar_time, price_before, price_after, notes, pre_mutation_snapshot)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (ticker, factor, overlap_bar_time, price_before, price_after, notes,
              pre_mutation_df.to_csv()))


def get_data_mutations(ticker=None, limit=200):
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_mutation_log_table(conn)
        conn.row_factory = sqlite3.Row
        q = "SELECT id, ticker, factor, detected_at, overlap_bar_time, price_before, price_after, notes FROM data_mutation_log"
        params = ()
        if ticker:
            q += " WHERE ticker = ?"
            params = (ticker,)
        q += " ORDER BY id DESC LIMIT ?"
        params = params + (limit,)
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def _ensure_bad_tick_scan_table(conn):
    # bad_tick_scan_log: one row per ticker per scan_bad_ticks.py run, recording
    # the actual date range/row count scanned -- not just the hits found. Built
    # 2026-08-11 after the DFEN bad-tick investigation: without recording what
    # window was actually scanned, a rerun with a shifted cache window (new bars
    # appended, or a ticker's data refetched) produces different results with no
    # way to tell "the data changed" from "the scan logic changed" from "a new
    # bad tick appeared." hits_json is the full hit list for that ticker (usually
    # empty), so a scan's findings are permanent even if the underlying CSV is
    # later patched.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bad_tick_scan_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at       TEXT NOT NULL DEFAULT (datetime('now')),
            ticker       TEXT NOT NULL,
            date_start   TEXT,
            date_end     TEXT,
            row_count    INTEGER,
            threshold    REAL,
            recovery_frac REAL,
            hits_count   INTEGER,
            hits_json    TEXT
        )
    """)


def log_bad_tick_scan(ticker, date_start, date_end, row_count, threshold,
                       recovery_frac, hits):
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_bad_tick_scan_table(conn)
        conn.execute("""
            INSERT INTO bad_tick_scan_log
                (ticker, date_start, date_end, row_count, threshold, recovery_frac,
                 hits_count, hits_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (ticker, date_start, date_end, row_count, threshold, recovery_frac,
              len(hits), json.dumps(hits, default=str)))


def get_bad_tick_scans(ticker=None, limit=500):
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_bad_tick_scan_table(conn)
        conn.row_factory = sqlite3.Row
        q = "SELECT * FROM bad_tick_scan_log"
        params = ()
        if ticker:
            q += " WHERE ticker = ?"
            params = (ticker,)
        q += " ORDER BY id DESC LIMIT ?"
        params = params + (limit,)
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def _ensure_sweep_tranches_table(conn):
    # sweep_tranches: DB-backed replacement for scripts/liquidity_tranches.txt's
    # hand-edited ticker-membership list, same rationale as the accounts table
    # replacing hardcoded account config (2026-08-11) and SCHWAB_AUTOMATION_TICKERS
    # moving off a Python literal -- a flat file that gets hand-edited repeatedly
    # (disqualified-ticker removals, priority reshuffles) has no audit trail of
    # who/when/why. This table is the source of truth; scripts/
    # render_liquidity_tranches.py regenerates the .txt file from it in the exact
    # format run_liquidity_tranches.sh already parses, so that script's tested
    # bash parsing logic doesn't need to change at all. Soft-delete (active=0),
    # never a hard DELETE, per standing convention -- a disqualification is itself
    # a real fact worth keeping, not just an absence.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sweep_tranches (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign     TEXT NOT NULL DEFAULT 'liquidity_screen',
            tranche_num  INTEGER NOT NULL,
            ticker       TEXT NOT NULL,
            active       INTEGER NOT NULL DEFAULT 1,
            added_at     TEXT NOT NULL DEFAULT (datetime('now')),
            removed_at   TEXT,
            reason       TEXT,
            UNIQUE(campaign, tranche_num, ticker)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sweep_campaign_config (
            campaign     TEXT PRIMARY KEY,
            version      TEXT NOT NULL,
            fixed_sls    TEXT NOT NULL,
            strategies   TEXT NOT NULL,
            entry_timing TEXT NOT NULL,
            updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)


def set_sweep_campaign_config(campaign, version, fixed_sls, strategies, entry_timing):
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_sweep_tranches_table(conn)
        conn.execute("""
            INSERT INTO sweep_campaign_config (campaign, version, fixed_sls, strategies, entry_timing, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(campaign) DO UPDATE SET
                version=excluded.version, fixed_sls=excluded.fixed_sls,
                strategies=excluded.strategies, entry_timing=excluded.entry_timing,
                updated_at=excluded.updated_at
        """, (campaign, version, fixed_sls, strategies, entry_timing))


def get_sweep_campaign_config(campaign):
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_sweep_tranches_table(conn)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM sweep_campaign_config WHERE campaign=?", (campaign,)).fetchone()
        return dict(row) if row else None


def add_tranche_ticker(campaign, tranche_num, ticker, reason=None):
    """Adds a ticker to a tranche, or reactivates it (clears removed_at) if it
    was previously soft-removed under the same (campaign, tranche_num, ticker)
    -- a ticker moving back into scope (e.g. a disqualification reversed) keeps
    its original added_at rather than looking like a brand-new row."""
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_sweep_tranches_table(conn)
        conn.execute("""
            INSERT INTO sweep_tranches (campaign, tranche_num, ticker, active, reason)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(campaign, tranche_num, ticker) DO UPDATE SET
                active=1, removed_at=NULL, reason=excluded.reason
        """, (campaign, tranche_num, ticker, reason))


def remove_tranche_ticker(campaign, tranche_num, ticker, reason):
    """Soft-removes a ticker from a tranche -- reason is required (not optional)
    since a removal with no recorded why is exactly the discrepancy-tracking gap
    this table exists to close."""
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_sweep_tranches_table(conn)
        conn.execute("""
            UPDATE sweep_tranches SET active=0, removed_at=datetime('now'), reason=?
            WHERE campaign=? AND tranche_num=? AND ticker=?
        """, (reason, campaign, tranche_num, ticker))


def get_tranches(campaign='liquidity_screen', active_only=True):
    """Returns {tranche_num: [ticker, ...]}, tickers in insertion (added_at) order
    within each tranche."""
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_sweep_tranches_table(conn)
        conn.row_factory = sqlite3.Row
        q = "SELECT tranche_num, ticker FROM sweep_tranches"
        if active_only:
            q += " WHERE active=1"
        q += " ORDER BY tranche_num, added_at, id"
        rows = conn.execute(q).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r['tranche_num'], []).append(r['ticker'])
    return out


def get_tranche_audit(campaign='liquidity_screen'):
    """Full history including inactive (removed) rows, for review -- every
    removal carries its reason, never silently dropped."""
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_sweep_tranches_table(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM sweep_tranches WHERE campaign=?
            ORDER BY tranche_num, added_at, id
        """, (campaign,)).fetchall()
    return [dict(r) for r in rows]


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
