import sys
import os
import logging
import json
import argparse
import sqlite3
import time
from pathlib import Path
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor, as_completed
import numpy as np
import pandas as pd
from tqdm import tqdm

from backtester import (run_backtest_dispatch,
                        prep_inputs, _simulate, _simulate_limit, _simulate_trail, _simulate_trail_buy,
                        _simulate_trail_both, _simulate_limit_trail, _simulate_close_limitexit)
import strategies
from db_cache import refresh_dropdown_cache, refresh_pivot_cache, refresh_cliff_grid_cache

CACHE_DIR    = Path("./cache/research")
OPTO_LOG_DIR = Path("./logs")
OPTO_LOG_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = CACHE_DIR / "trading_universe.db"

FINE_RADIUS    = 4
N_ISLANDS      = 3
ISLAND_MIN_SEP = 6
CLIFF_RADIUS   = 2

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(OPTO_LOG_DIR / "matrix_execution.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("MatrixSweepEngine")


def init_idempotent_db():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS backtest_cache (
            strategy TEXT, version TEXT, ticker TEXT, window INTEGER,
            max_hold_hours INTEGER, take_profit INTEGER, stop_loss INTEGER,
            trades INTEGER, win_rate REAL, strategy_return REAL,
            alpha_vs_spy REAL, asset_bh REAL, spy_bh REAL, run_timestamp TEXT,
            z_score_threshold REAL DEFAULT 2.0,
            PRIMARY KEY (strategy, version, ticker, window, max_hold_hours, take_profit, stop_loss, z_score_threshold)
        )
    """)
    try:
        cursor.execute("ALTER TABLE backtest_cache ADD COLUMN z_score_threshold REAL DEFAULT 2.0")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE backtest_cache ADD COLUMN fixed_sl REAL DEFAULT 0")
    except Exception:
        pass
    try:
        # win_rate only counts Result=='WIN' (excludes profitable TIME-exit 'TWIN'
        # trades) — win_twin_rate = (WIN+TWIN)/trades is the real "did this trade
        # make money" rate. Added 2026-07-05 after finding a KORU node whose 21%
        # win_rate was misleading (71% of its trades were actually profitable, just
        # via TWIN). Old rows keep win_twin_rate=0 (not recomputed retroactively —
        # would require re-simulating all historical trades).
        cursor.execute("ALTER TABLE backtest_cache ADD COLUMN win_twin_rate REAL DEFAULT 0")
    except Exception:
        pass

    # v3.x reparameterization (2026-07-05): stop_loss now always means real SL;
    # trail_buy_pct/trail_pct get real columns instead of overloading stop_loss.
    # PK must be rebuilt to add them (SQLite can't ALTER a PRIMARY KEY in place) —
    # old v1.x/v2.x rows are copied over untouched (trail_buy_pct/trail_pct=0 for
    # them; their stop_loss keeps its old, overloaded meaning, still correctly
    # interpreted via docs/design.md's lookup table). No value transformation,
    # straight copy — only new writes use the corrected semantics.
    bc_cols = {r[1] for r in cursor.execute("PRAGMA table_info(backtest_cache)").fetchall()}
    if 'trail_buy_pct' not in bc_cols:
        logger.info("Migrating backtest_cache schema: adding trail_buy_pct/trail_sell_pct, rebuilding PK...")
        before_count = cursor.execute("SELECT COUNT(*) FROM backtest_cache").fetchone()[0]
        cursor.executescript("""
            CREATE TABLE backtest_cache_new (
                strategy TEXT, version TEXT, ticker TEXT, window INTEGER,
                max_hold_hours INTEGER, take_profit INTEGER, stop_loss INTEGER,
                trades INTEGER, win_rate REAL, strategy_return REAL,
                alpha_vs_spy REAL, asset_bh REAL, spy_bh REAL, run_timestamp TEXT,
                z_score_threshold REAL DEFAULT 2.0, fixed_sl REAL DEFAULT 0,
                trail_buy_pct REAL DEFAULT 0, trail_sell_pct REAL DEFAULT 0,
                PRIMARY KEY (strategy, version, ticker, window, max_hold_hours,
                             take_profit, stop_loss, z_score_threshold,
                             trail_buy_pct, trail_sell_pct)
            );
            INSERT INTO backtest_cache_new
                (strategy, version, ticker, window, max_hold_hours, take_profit, stop_loss,
                 trades, win_rate, strategy_return, alpha_vs_spy, asset_bh, spy_bh,
                 run_timestamp, z_score_threshold, fixed_sl, trail_buy_pct, trail_sell_pct)
            SELECT strategy, version, ticker, window, max_hold_hours, take_profit, stop_loss,
                   trades, win_rate, strategy_return, alpha_vs_spy, asset_bh, spy_bh,
                   run_timestamp, z_score_threshold, fixed_sl, 0, 0
            FROM backtest_cache;
            DROP TABLE backtest_cache;
            ALTER TABLE backtest_cache_new RENAME TO backtest_cache;
        """)
        after_count = cursor.execute("SELECT COUNT(*) FROM backtest_cache").fetchone()[0]
        if after_count != before_count:
            raise RuntimeError(f"backtest_cache migration row count mismatch: {before_count} -> {after_count}")
        logger.info(f"Migration complete: {after_count:,} rows carried over unchanged.")
        bc_cols = {r[1] for r in cursor.execute("PRAGMA table_info(backtest_cache)").fetchall()}

    # take_profit split (2026-07-07): NULL for TrailingBothZScoreBreakout (real value
    # lives in arm_sell_pct instead — mirrors active_signals.py's live-side split, see
    # _tp_or_arm_pct there). axis_tp is a write-time-computed, always-non-NULL mirror of
    # whichever of the two actually holds the swept grid value — SQLite's composite PK
    # can't dedupe on take_profit once it's NULL (NULL never equals NULL), so axis_tp
    # (not take_profit) is what the PK and all internal island/cliff-box queries use.
    if 'arm_sell_pct' not in bc_cols or 'axis_tp' not in bc_cols:
        logger.info("Migrating backtest_cache schema: adding arm_sell_pct/axis_tp, rebuilding PK...")
        before_count = cursor.execute("SELECT COUNT(*) FROM backtest_cache").fetchone()[0]
        # arm_sell_pct may already exist from an earlier ad-hoc migration (with
        # take_profit already NULLed for TrailingBoth rows) — read it if present,
        # else treat it as never having existed (NULL for every row).
        src_arm_sell_pct = "arm_sell_pct" if 'arm_sell_pct' in bc_cols else "NULL"
        cursor.executescript(f"""
            CREATE TABLE backtest_cache_new (
                strategy TEXT, version TEXT, ticker TEXT, window INTEGER,
                max_hold_hours INTEGER, take_profit INTEGER, stop_loss INTEGER,
                trades INTEGER, win_rate REAL, strategy_return REAL,
                alpha_vs_spy REAL, asset_bh REAL, spy_bh REAL, run_timestamp TEXT,
                z_score_threshold REAL DEFAULT 2.0, fixed_sl REAL DEFAULT 0,
                trail_buy_pct REAL DEFAULT 0, trail_sell_pct REAL DEFAULT 0,
                win_twin_rate REAL DEFAULT 0, arm_sell_pct REAL, axis_tp REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (strategy, version, ticker, window, max_hold_hours,
                             axis_tp, stop_loss, z_score_threshold,
                             trail_buy_pct, trail_sell_pct)
            );
            INSERT INTO backtest_cache_new
                (strategy, version, ticker, window, max_hold_hours, take_profit, stop_loss,
                 trades, win_rate, strategy_return, alpha_vs_spy, asset_bh, spy_bh,
                 run_timestamp, z_score_threshold, fixed_sl, trail_buy_pct, trail_sell_pct,
                 win_twin_rate, arm_sell_pct, axis_tp)
            SELECT strategy, version, ticker, window, max_hold_hours,
                   CASE WHEN strategy = 'TrailingBothZScoreBreakout' THEN NULL ELSE take_profit END,
                   stop_loss, trades, win_rate, strategy_return, alpha_vs_spy, asset_bh, spy_bh,
                   run_timestamp, z_score_threshold, fixed_sl, trail_buy_pct, trail_sell_pct,
                   COALESCE(win_twin_rate, 0),
                   CASE WHEN strategy = 'TrailingBothZScoreBreakout' THEN COALESCE({src_arm_sell_pct}, take_profit) ELSE NULL END,
                   COALESCE(take_profit, {src_arm_sell_pct})
            FROM backtest_cache;
            DROP TABLE backtest_cache;
            ALTER TABLE backtest_cache_new RENAME TO backtest_cache;
        """)
        after_count = cursor.execute("SELECT COUNT(*) FROM backtest_cache").fetchone()[0]
        if after_count != before_count:
            raise RuntimeError(f"backtest_cache axis_tp migration row count mismatch: {before_count} -> {after_count}")
        logger.info(f"Migration complete: {after_count:,} rows carried over, axis_tp backfilled from take_profit.")
        bc_cols = {r[1] for r in cursor.execute("PRAGMA table_info(backtest_cache)").fetchall()}

    # v4 (2026-07-14): fill-optimism resolution bounds + entry_timing axis — see
    # docs/backlog_cache.md fill-optimism item, plan at
    # /home/pkim/.claude/plans/rustling-bubbling-hennessy.md. entry_timing is a
    # campaign-level constant (like fixed_sl/stop_loss, not swept within a run) so
    # it goes in the PK. 'possible' is the existing alpha_vs_spy/strategy_return
    # (Low-before-High assumption, unchanged) — 'pessimistic' (High-before-Low
    # assumption) and 'certain' (no-guessing, only provable fills) are new,
    # plain data columns riding alongside, not PK members. None of the three is a
    # rigorous bound on the others (see _simulate_trail_both docstring) — island
    # search ranks by MIN across all three, not 'possible' alone, so a node only
    # gets picked when it's robust under every resolution, not just the
    # optimistic default.
    if 'entry_timing' not in bc_cols:
        logger.info("Migrating backtest_cache schema: adding entry_timing/pessimistic+certain bound columns, rebuilding PK...")
        before_count = cursor.execute("SELECT COUNT(*) FROM backtest_cache").fetchone()[0]
        cursor.executescript("""
            CREATE TABLE backtest_cache_new (
                strategy TEXT, version TEXT, ticker TEXT, window INTEGER,
                max_hold_hours INTEGER, take_profit INTEGER, stop_loss INTEGER,
                trades INTEGER, win_rate REAL, strategy_return REAL,
                alpha_vs_spy REAL, asset_bh REAL, spy_bh REAL, run_timestamp TEXT,
                z_score_threshold REAL DEFAULT 2.0, fixed_sl REAL DEFAULT 0,
                trail_buy_pct REAL DEFAULT 0, trail_sell_pct REAL DEFAULT 0,
                win_twin_rate REAL DEFAULT 0, arm_sell_pct REAL, axis_tp REAL NOT NULL DEFAULT 0,
                entry_timing TEXT NOT NULL DEFAULT 'close',
                strategy_return_pessimistic REAL, alpha_vs_spy_pessimistic REAL,
                strategy_return_certain REAL, alpha_vs_spy_certain REAL,
                PRIMARY KEY (strategy, version, ticker, window, max_hold_hours,
                             axis_tp, stop_loss, z_score_threshold,
                             trail_buy_pct, trail_sell_pct, entry_timing)
            );
            INSERT INTO backtest_cache_new
                (strategy, version, ticker, window, max_hold_hours, take_profit, stop_loss,
                 trades, win_rate, strategy_return, alpha_vs_spy, asset_bh, spy_bh,
                 run_timestamp, z_score_threshold, fixed_sl, trail_buy_pct, trail_sell_pct,
                 win_twin_rate, arm_sell_pct, axis_tp, entry_timing,
                 strategy_return_pessimistic, alpha_vs_spy_pessimistic,
                 strategy_return_certain, alpha_vs_spy_certain)
            SELECT strategy, version, ticker, window, max_hold_hours, take_profit, stop_loss,
                   trades, win_rate, strategy_return, alpha_vs_spy, asset_bh, spy_bh,
                   run_timestamp, z_score_threshold, fixed_sl, trail_buy_pct, trail_sell_pct,
                   win_twin_rate, arm_sell_pct, axis_tp, 'close', NULL, NULL, NULL, NULL
            FROM backtest_cache;
            DROP TABLE backtest_cache;
            ALTER TABLE backtest_cache_new RENAME TO backtest_cache;
        """)
        after_count = cursor.execute("SELECT COUNT(*) FROM backtest_cache").fetchone()[0]
        if after_count != before_count:
            raise RuntimeError(f"backtest_cache v4 migration row count mismatch: {before_count} -> {after_count}")
        logger.info(f"Migration complete: {after_count:,} rows carried over, entry_timing backfilled to 'close'.")

    # phase (2026-07-15): tags each row with whichever phase (Phase1-Coarse/
    # Phase2-Island/Phase2.5-CliffBox/Phase3-Full) first computed it, so a
    # "does a later phase ever actually improve on an earlier one" analysis is a
    # simple query. Plain data column, no PK rebuild. Originally added by hand
    # against the live DB — codified here so a fresh DB doesn't fail on INSERT.
    try:
        cursor.execute("ALTER TABLE backtest_cache ADD COLUMN phase TEXT")
    except Exception:
        pass
    # generation (2026-07-15): for Phase2-Island rows only, which island-search
    # generation (1-indexed, config.execution.max_generations) produced this row —
    # lets the same "does it earn its cost" question be asked of the generation
    # loop, not just the phase pipeline. NULL for all other phases (single-pass).
    try:
        cursor.execute("ALTER TABLE backtest_cache ADD COLUMN generation INTEGER")
    except Exception:
        pass

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sl_sweep_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL, strategy TEXT NOT NULL, version TEXT NOT NULL,
            stop_loss REAL NOT NULL, trail_sell_pct REAL NOT NULL, entry_timing TEXT NOT NULL,
            best_alpha REAL, best_alpha_pessimistic REAL, best_alpha_certain REAL,
            worst_neighbor_alpha REAL,
            best_node_tp REAL, best_node_hold INTEGER, best_node_trail_buy_pct REAL,
            n_islands INTEGER, any_cliff_safe INTEGER, run_timestamp TEXT NOT NULL,
            UNIQUE(ticker, strategy, version, stop_loss, trail_sell_pct, entry_timing, run_timestamp)
        )
    """)

    cursor.execute("DROP INDEX IF EXISTS idx_bc_version_ticker")
    cursor.execute("DROP INDEX IF EXISTS idx_bc_version_ticker_z_return")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bc_version_window ON backtest_cache(version, window)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bc_version_ticker_strategy ON backtest_cache(version, ticker, strategy)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bc_version_return ON backtest_cache(version, strategy_return)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bc_ticker ON backtest_cache(ticker)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sweep_runs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            version       TEXT NOT NULL,
            started_at    TEXT NOT NULL,
            finished_at   TEXT,
            status        TEXT NOT NULL DEFAULT 'RUNNING',
            strategies    TEXT NOT NULL,
            tickers       TEXT NOT NULL,
            phase_reached TEXT,
            config_json   TEXT NOT NULL,
            notes         TEXT,
            log_file      TEXT
        )
    """)
    conn.commit()
    conn.close()


def start_sweep_run(config_version, strategy_names, tickers, config, log_file):
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sweep_runs (version, started_at, status, strategies, tickers, config_json, log_file)
        VALUES (?, ?, 'RUNNING', ?, ?, ?, ?)
    """, (config_version, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
          json.dumps(strategy_names), json.dumps(tickers), json.dumps(config), str(log_file)))
    conn.commit()
    run_id = cur.lastrowid
    conn.close()
    return run_id


def update_sweep_run(run_id, **fields):
    if not fields:
        return
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    cur = conn.cursor()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    cur.execute(f"UPDATE sweep_runs SET {set_clause} WHERE id = ?", (*fields.values(), run_id))
    conn.commit()
    conn.close()


# Per-worker-process memo: workers are long-lived across grid nodes, and dispatch
# is one ticker at a time — without this every node re-parses the CSV and recomputes
# indicators (~18k times per ticker in Phase 3). Indicators depend only on
# (ticker, strategy, window): no strategy's generate_daily_indicators uses z.
_NODE_INPUT_CACHE = {}
_NODE_INPUT_CACHE_MAX = 6


def _load_node_inputs(ticker, strategy_class, strategy_name, w, z_thresh):
    key = (ticker, strategy_name, int(w))
    hit = _NODE_INPUT_CACHE.get(key)
    if hit is not None:
        return hit

    cache_path = CACHE_DIR / f"{ticker}_1h.csv"
    df_hourly_raw = pd.read_csv(cache_path, index_col=0, parse_dates=True)
    df_hourly_raw.index = pd.to_datetime(df_hourly_raw.index).tz_localize(None)
    df_hourly_raw = df_hourly_raw.sort_index()
    if df_hourly_raw.empty:
        entry = None
    else:
        close_col = 'Adj Close' if 'Adj Close' in df_hourly_raw.columns else 'Close'
        df_daily = df_hourly_raw.resample('D').last().dropna(subset=[close_col])
        strat_instance = strategy_class(window=w, z_score_threshold=z_thresh)
        df_daily_processed = strat_instance.generate_daily_indicators(df_daily)
        entry = (df_hourly_raw, df_daily_processed, prep_inputs(df_hourly_raw, df_daily_processed))

    if len(_NODE_INPUT_CACHE) >= _NODE_INPUT_CACHE_MAX:
        _NODE_INPUT_CACHE.clear()
    _NODE_INPUT_CACHE[key] = entry
    return entry


def _warmup_worker():
    """ProcessPoolExecutor initializer: pays each numba kernel's one-time JIT
    compile cost at worker startup instead of on a random real grid node."""
    prices    = np.array([100.0, 99.0, 101.0], dtype=np.float64)
    hilo      = prices
    hours     = np.array([9, 14, 9], dtype=np.int64)
    daily_idx = np.array([0, 0, 0], dtype=np.int64)
    sma_arr   = np.array([100.0], dtype=np.float64)
    std_arr   = np.array([1.0], dtype=np.float64)
    trend_arr = np.array([0.0], dtype=np.float64)

    _simulate(prices, hours, daily_idx, sma_arr, std_arr, trend_arr, False,
              0.05, 0.05, 1, 9, 14, 2.0)
    _simulate_limit(prices, hilo, hours, daily_idx, sma_arr, std_arr, trend_arr, False,
                     0.05, 0.05, 1, 9, 14, 2.0)
    _simulate_trail(prices, hilo, hilo, hours, daily_idx, sma_arr, std_arr, trend_arr, False,
                     0.05, 0.05, 1, 0.03, 9, 14, 2.0)
    _simulate_trail_buy(prices, hilo, hilo, hours, daily_idx, sma_arr, std_arr, trend_arr, False,
                         0.05, 0.05, 1, 0.03, 9, 14, 2.0)
    _simulate_trail_both(prices, hilo, hilo, hours, daily_idx, sma_arr, std_arr, trend_arr, False,
                          0.05, 0.05, 1, 0.03, 0.03, 9, 14, 2.0, prices, False)
    _simulate_limit_trail(prices, hilo, hilo, hours, daily_idx, sma_arr, std_arr, trend_arr, False,
                           0.05, 0.05, 1, 0.03, 9, 14, 2.0)
    _simulate_close_limitexit(prices, hilo, hilo, hours, daily_idx, sma_arr, std_arr, trend_arr, False,
                               0.05, 0.05, 1, 9, 14, 2.0)


def _config_trail_pct():
    """Legacy single-value fallback for TrailingBothZScoreBreakout's trail_pct (exit
    trailing %) when config.json's hyperparameters.trail_pcts isn't set — matches the
    old v2.10/v2.13-17 behavior of one fixed value per whole backfill run."""
    try:
        with open("config.json") as f:
            return float(json.load(f).get("execution", {}).get("trail_pct", 3)) / 100.0
    except Exception:
        return 0.03


def _trail_pcts_for_strategy(strategy_name, hp):
    """TrailingBothZScoreBreakout is the only strategy with a real, swept 4th axis
    (trail_pct). Everything else doesn't use this axis (single dummy value)."""
    _, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)
    if fourth_axis_col != 'trail_pct':
        return [0.0]
    return [float(v) for v in hp.get('trail_pcts', [_config_trail_pct() * 100])]


def _summarize_trades(closed, spy_bh):
    """(alpha, n_trades, win_rate, compounded, win_twin_rate) for a closed-trade list."""
    df_tr = pd.DataFrame(closed)
    win_rate      = float((len(df_tr[df_tr['Result'] == 'WIN']) / len(df_tr)) * 100)
    win_twin_rate = float((len(df_tr[df_tr['Result'].isin(['WIN', 'TWIN'])]) / len(df_tr)) * 100)
    compounded = float(((df_tr['Return'] + 1).prod() - 1) * 100)
    alpha_calc = float(compounded - spy_bh)
    return alpha_calc, len(df_tr), win_rate, compounded, win_twin_rate


def run_single_backtest_node_isolated(args):
    ticker, strategy_name, config_version, tp, sl, hold_hours, w, spy_bh, z_thresh, fixed_sl, trail_pct_pct, entry_timing = args

    strategy_class = getattr(strategies, strategy_name, None)
    if not strategy_class:
        return {"coords": (tp, sl, hold_hours), "payload": (0.0, 0, 0.0), "window": w, "z_thresh": z_thresh, "status": "UNKNOWN_STRAT"}

    try:
        inputs = _load_node_inputs(ticker, strategy_class, strategy_name, w, z_thresh)
    except Exception as e:
        return {"coords": (tp, sl, hold_hours), "payload": (0.0, 0, 0.0), "window": w, "z_thresh": z_thresh, "status": "ERROR", "error": repr(e)}

    if inputs is None:
        return {"coords": (tp, sl, hold_hours), "payload": (0.0, 0, 0.0), "window": w, "z_thresh": z_thresh, "status": "EMPTY"}
    df_hourly_raw, df_daily_processed, prep = inputs

    try:
        result = run_backtest_dispatch(
            strategy_class, df_hourly_raw, df_daily_processed, ticker,
            take_profit=tp, sl_raw=sl, max_hours_to_hold=hold_hours, z_score_threshold=z_thresh,
            fixed_sl=fixed_sl, trail_pct_pct=trail_pct_pct, entry_timing=entry_timing,
            return_bounds=True, prep=prep
        )
        # pessimistic/certain bounds only exist for TrailingBothZScoreBreakout —
        # every other strategy's dispatch branch ignores return_bounds and returns
        # a plain list.
        if isinstance(result, tuple):
            trades, trades_pess, trades_cert = result
        else:
            trades, trades_pess, trades_cert = result, None, None
        closed_codes = ["WIN", "LOSS", "TWIN", "TLOSS"]
        closed = [t for t in trades if t["Result"] in closed_codes]
        closed_pess = [t for t in trades_pess if t["Result"] in closed_codes] if trades_pess is not None else None
        closed_cert = [t for t in trades_cert if t["Result"] in closed_codes] if trades_cert is not None else None
    except Exception as e:
        return {"coords": (tp, sl, hold_hours), "payload": (0.0, 0, 0.0), "window": w, "z_thresh": z_thresh, "status": "SIM_ERROR", "error": repr(e)}

    if not closed:
        return {"coords": (tp, sl, hold_hours), "payload": (0.0, 0, 0.0), "window": w, "z_thresh": z_thresh, "status": "NO_TRADES"}

    alpha_calc, n_trades, win_rate, compounded, win_twin_rate = _summarize_trades(closed, spy_bh)
    if closed_pess:
        alpha_pess, _, _, compounded_pess, _ = _summarize_trades(closed_pess, spy_bh)
    else:
        alpha_pess, compounded_pess = None, None
    if closed_cert:
        alpha_cert, _, _, compounded_cert, _ = _summarize_trades(closed_cert, spy_bh)
    else:
        alpha_cert, compounded_cert = None, None

    return {
        "coords":  (tp, sl, hold_hours),
        "payload": (alpha_calc, n_trades, win_rate, compounded, win_twin_rate,
                    alpha_pess, compounded_pess, alpha_cert, compounded_cert),
        "window":  w, "z_thresh": z_thresh, "status": "SUCCESS"
    }


def dispatch_parallel_grid(shared_pool, tasks, ticker, strategy_name, config_version, phase_label, spy_bh, asset_bh, run_timestamp, fixed_sl=0, entry_timing='close', generation=None):
    conn   = sqlite3.connect(DB_PATH, timeout=60.0)
    cursor = conn.cursor()
    matrix_results  = []
    unvisited_tasks = []

    # v1.8/v1.9/v1.10/v2.11 use fixed_sl as the real stop loss (the swept 'stop_loss'
    # column holds trail_pct/trail_buy_pct for these) — a cache row is only valid for
    # the fixed_sl it was computed with, else re-running with a different fixed_stop_loss
    # would silently serve stale results under the same (tp, sl, hold, w, z) key.
    uses_fixed_sl = strategies.uses_fixed_sl(strategy_name)
    stored_fsl = float(fixed_sl) if uses_fixed_sl else 0.0

    # Which real column a task's raw sl/tpct grid values land in for this strategy —
    # see docs/design.md "Grid axis meaning by strategy".
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)

    # One query for all cached nodes of this (strategy, version, ticker) instead of
    # one SELECT per task (Phase 3 = ~18k queries per ticker).
    cached_map = {}
    cursor.execute("""
        SELECT window, max_hold_hours, axis_tp, stop_loss, z_score_threshold, fixed_sl,
               trail_buy_pct, trail_sell_pct, trades, win_rate, strategy_return, alpha_vs_spy, win_twin_rate
        FROM backtest_cache
        WHERE strategy=? AND version=? AND ticker=? AND entry_timing=?
    """, (strategy_name, config_version, ticker, entry_timing))
    for r in cursor.fetchall():
        if r[4] is None:
            continue  # legacy NULL-z rows never matched the old equality check either
        row_fsl = float(r[5]) if (uses_fixed_sl and r[5] is not None) else 0.0
        # Recover the raw sl/tpct grid values this row was computed with, from
        # whichever real column holds them for this strategy.
        if sl_axis_col == 'trail_buy_pct':
            row_sl_raw = float(r[6])
        elif sl_axis_col == 'trail_pct':
            row_sl_raw = float(r[7])
        else:
            row_sl_raw = float(r[3])
        row_tpct_raw = float(r[7]) if fourth_axis_col == 'trail_pct' else 0.0
        # axis_tp (not take_profit — NULL for TrailingBothZScoreBreakout) is the raw
        # swept 'tp' grid value regardless of strategy.
        cached_map[(int(r[2]), row_sl_raw, int(r[1]), int(r[0]), float(r[4]), row_fsl, row_tpct_raw)] = \
            (r[8], r[9], r[10], r[11], r[12] if r[12] is not None else 0.0)

    for t in tasks:
        tp, sl, hold_hours, w, z_thresh, tpct = t
        cached_row = cached_map.get((int(tp), float(sl), int(hold_hours), int(w), float(z_thresh), stored_fsl, float(tpct)))
        if cached_row:
            matrix_results.append({
                "Strategy": strategy_name, "Version": config_version, "Ticker": ticker, "Window": w,
                "Take Profit %": int(tp), "Stop Loss %": int(sl), "Max Hold Hours": hold_hours,
                "Z Threshold": z_thresh,
                "Trades": cached_row[0], "Win Rate %": cached_row[1], "Return %": cached_row[2],
                "Alpha vs SPY %": cached_row[3], "Win+TWin Rate %": cached_row[4],
                "Asset B&H %": asset_bh, "SPY B&H %": spy_bh
            })
        else:
            unvisited_tasks.append(t)

    if not unvisited_tasks:
        conn.close()
        return pd.DataFrame(matrix_results)

    try:
        with open("active_phase_grid.json", "w") as gf:
            json.dump({"phase": phase_label, "nodes": [
                {"take_profit": int(t[0]), "stop_loss": int(t[1]), "max_hold_hours": int(t[2])}
                for t in unvisited_tasks
            ]}, gf)
    except Exception:
        pass

    futures_map = {
        shared_pool.submit(run_single_backtest_node_isolated,
                           (ticker, strategy_name, config_version, int(tp), int(sl), hold, w, spy_bh, z, fixed_sl, tpct, entry_timing)): task
        for task in unvisited_tasks
        for tp, sl, hold, w, z, tpct in [task]
    }

    progress_bar = tqdm(
        as_completed(futures_map), total=len(futures_map),
        desc=f"[{ticker}] {phase_label}", unit="node",
        mininterval=15.0, maxinterval=30.0
    )

    node_counter      = 0
    last_postfix_time = 0.0
    fail_counts       = {}
    buffer            = []
    batch_size        = 5000

    for future in progress_bar:
        tp, sl, hold_hours, w, z_thresh, tpct = futures_map[future]
        try:
            res    = future.result()
            status = res.get("status")
            if status not in ("SUCCESS", "NO_TRADES"):
                fail_counts[status] = fail_counts.get(status, 0) + 1
                if sum(fail_counts.values()) == 1:
                    logger.warning(f"[{ticker}] {phase_label} first failed node TP={tp} SL={sl}: "
                                   f"{status} {res.get('error', '')}")
            if status in ("SUCCESS", "NO_TRADES"):
                if status == "SUCCESS":
                    (alpha, num_trades, wr, comp_ret, wtw,
                     alpha_pess, comp_ret_pess, alpha_cert, comp_ret_cert) = res["payload"]
                else:
                    alpha, num_trades, wr, comp_ret, wtw = 0.0, 0, 0.0, 0.0, 0.0
                    alpha_pess, comp_ret_pess, alpha_cert, comp_ret_cert = None, None, None, None

                now = time.time()
                if now - last_postfix_time >= 2.0:
                    progress_bar.set_postfix({"Alpha": f"{alpha:+.1f}%", "Trades": num_trades})
                    last_postfix_time = now

                node_counter += 1
                if node_counter % 50 == 0:
                    try:
                        with open("current_test.json", "w") as tf:
                            json.dump({"phase": phase_label, "ticker": ticker, "strategy": strategy_name,
                                       "version": config_version, "take_profit": int(tp),
                                       "stop_loss": int(sl), "max_hold_hours": int(hold_hours)}, tf)
                    except Exception:
                        pass

                if status == "SUCCESS":
                    matrix_results.append({
                        "Strategy": strategy_name, "Version": config_version, "Ticker": ticker, "Window": w,
                        "Take Profit %": int(tp), "Stop Loss %": int(sl), "Max Hold Hours": hold_hours,
                        "Z Threshold": z_thresh,
                        "Trades": num_trades, "Win Rate %": wr, "Return %": comp_ret,
                        "Alpha vs SPY %": alpha, "Win+TWin Rate %": wtw,
                        "Asset B&H %": asset_bh, "SPY B&H %": spy_bh
                    })

                # Map this task's raw sl/tpct grid values onto the real named columns.
                if sl_axis_col == 'trail_buy_pct':
                    row_stop_loss, row_trail_buy_pct = int(round(stored_fsl)), float(sl)
                    row_trail_pct = float(tpct) if fourth_axis_col == 'trail_pct' else 0.0
                elif sl_axis_col == 'trail_pct':
                    row_stop_loss, row_trail_buy_pct, row_trail_pct = int(round(stored_fsl)), 0.0, float(sl)
                else:
                    row_stop_loss, row_trail_buy_pct, row_trail_pct = int(sl), 0.0, 0.0

                # take_profit is NULL for TrailingBothZScoreBreakout (real value lives in
                # arm_sell_pct instead — mirrors active_signals.py's live-side split).
                # axis_tp always holds the raw 'tp' value regardless of strategy, since
                # the PK can't dedupe on a column that's sometimes NULL.
                if strategy_name == 'TrailingBothZScoreBreakout':
                    row_take_profit, row_arm_sell_pct = None, float(tp)
                else:
                    row_take_profit, row_arm_sell_pct = int(tp), None

                buffer.append((strategy_name, config_version, ticker, w, hold_hours, row_take_profit, row_stop_loss,
                               num_trades, wr, comp_ret, alpha, asset_bh, spy_bh, run_timestamp, z_thresh,
                               stored_fsl, row_trail_buy_pct, row_trail_pct, wtw, row_arm_sell_pct, float(tp),
                               entry_timing, comp_ret_pess, alpha_pess, comp_ret_cert, alpha_cert, phase_label,
                               generation))

                if len(buffer) >= batch_size:
                    cursor.executemany(
                        """INSERT OR REPLACE INTO backtest_cache
                           (strategy, version, ticker, window, max_hold_hours, take_profit, stop_loss,
                            trades, win_rate, strategy_return, alpha_vs_spy, asset_bh, spy_bh,
                            run_timestamp, z_score_threshold, fixed_sl, trail_buy_pct, trail_sell_pct,
                            win_twin_rate, arm_sell_pct, axis_tp, entry_timing,
                            strategy_return_pessimistic, alpha_vs_spy_pessimistic,
                            strategy_return_certain, alpha_vs_spy_certain, phase, generation)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        buffer
                    )
                    buffer = []
                    conn.commit()

        except Exception as e:
            logger.error(f"Worker crashed TP={tp} SL={sl}: {e}")

    if buffer:
        cursor.executemany(
            """INSERT OR REPLACE INTO backtest_cache
               (strategy, version, ticker, window, max_hold_hours, take_profit, stop_loss,
                trades, win_rate, strategy_return, alpha_vs_spy, asset_bh, spy_bh,
                run_timestamp, z_score_threshold, fixed_sl, trail_buy_pct, trail_sell_pct,
                win_twin_rate, arm_sell_pct, axis_tp, entry_timing,
                strategy_return_pessimistic, alpha_vs_spy_pessimistic,
                strategy_return_certain, alpha_vs_spy_certain, phase, generation)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            buffer
        )
    conn.commit()
    progress_bar.close()
    conn.close()
    if fail_counts:
        logger.warning(f"[{ticker}] {phase_label}: {sum(fail_counts.values())} nodes failed {fail_counts}")
    return pd.DataFrame(matrix_results)


# ── B&H helper ────────────────────────────────────────────────────────────────

def compute_bh_returns(ticker):
    cache_path = CACHE_DIR / f"{ticker}_1h.csv"
    if not cache_path.exists():
        return None, None
    df = pd.read_csv(cache_path, index_col=0, parse_dates=True).sort_index()
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    close_col = 'Adj Close' if 'Adj Close' in df.columns else 'Close'
    asset_bh  = ((df[close_col].iloc[-1] - df[close_col].iloc[0]) / df[close_col].iloc[0]) * 100

    spy_bh    = 0.0
    spy_cache = CACHE_DIR / "SPY_1h.csv"
    if spy_cache.exists():
        spy_df = pd.read_csv(spy_cache, index_col=0, parse_dates=True).sort_index()
        if spy_df.index.tz is not None:
            spy_df.index = spy_df.index.tz_localize(None)
        sliced = spy_df.loc[df.index.min():df.index.max()]
        if not sliced.empty:
            spy_col = 'Adj Close' if 'Adj Close' in spy_df.columns else 'Close'
            spy_bh  = ((sliced[spy_col].iloc[-1] - sliced[spy_col].iloc[0]) / sliced[spy_col].iloc[0]) * 100
    return asset_bh, spy_bh


# ── Island selection ──────────────────────────────────────────────────────────

# A node's "robust" alpha is the worst of its three fill-optimism resolutions
# (possible/pessimistic/certain — see backtester._simulate_trail_both), not just
# the optimistic 'possible' number — island search and cliff-safety rank/filter
# on this everywhere so a node is only ever selected when it holds up under all
# three, not just the default optimistic one. COALESCE falls back to alpha_vs_spy
# itself for strategies that don't produce pessimistic/certain bounds (NULL).
ROBUST_ALPHA_SQL = ("MIN(alpha_vs_spy, COALESCE(alpha_vs_spy_pessimistic, alpha_vs_spy), "
                     "COALESCE(alpha_vs_spy_certain, alpha_vs_spy))")


def pick_island_centers(df, n=N_ISLANDS, min_sep=ISLAND_MIN_SEP):
    rank_col = 'robust_alpha' if 'robust_alpha' in df.columns else 'alpha_vs_spy'
    centers = []
    for _, row in df.sort_values(rank_col, ascending=False).iterrows():
        tp, sl = int(row['take_profit']), int(row['stop_loss'])
        if all(abs(tp - c[0]) >= min_sep or abs(sl - c[1]) >= min_sep for c in centers):
            centers.append((tp, sl))
        if len(centers) == n:
            break
    return centers


# ── Phase 1: Coarse scan ──────────────────────────────────────────────────────

def _campaign_scope_sql(strategy_name, fixed_sl, entry_timing):
    """Extra WHERE-clause fragment + params to keep a phase2/2.5/checkpoint query
    scoped to one (stop_loss, entry_timing) campaign when multiple v4-style
    campaigns share a single version string — see docs/design.md's 3-axis island
    cap and the v4 plan's separate-campaign-per-(stop_loss,entry_timing) design.
    For strategies whose real 'stop_loss' column IS the swept grid axis (not
    fixed_sl), only entry_timing needs scoping."""
    if strategies.uses_fixed_sl(strategy_name):
        return " AND stop_loss=? AND entry_timing=?", [int(round(float(fixed_sl))), entry_timing]
    return " AND entry_timing=?", [entry_timing]


def run_phase1_coarse(shared_pool, ticker, strategy_name, config_version, hp, spy_bh, asset_bh, run_timestamp, fixed_sl=0, entry_timing='close'):
    z_thresholds = hp['z_score_thresholds']
    trail_pcts = _trail_pcts_for_strategy(strategy_name, hp)
    expected = (len(z_thresholds) * len(hp['windows']) * len(hp['take_profits'])
                * len(hp['stop_losses']) * len(hp['hold_time_caps']) * len(trail_pcts))

    with sqlite3.connect(DB_PATH, timeout=60.0) as chk:
        z_ph = ','.join('?' * len(z_thresholds))
        w_ph = ','.join('?' * len(hp['windows']))
        scope_sql, scope_params = _campaign_scope_sql(strategy_name, fixed_sl, entry_timing)
        cached = chk.execute(
            f"SELECT COUNT(*) FROM backtest_cache WHERE strategy=? AND version=? AND ticker=?"
            f" AND z_score_threshold IN ({z_ph}) AND window IN ({w_ph}) {scope_sql}",
            (strategy_name, config_version, ticker, *z_thresholds, *hp['windows'], *scope_params)
        ).fetchone()[0]

    if cached >= expected:
        logger.info(f"[{ticker}] Phase1 fully cached ({cached}/{expected}). Skipping.")
        return

    tasks = [(int(tp), int(sl), int(hold), int(w), float(z), float(tpct))
             for z    in z_thresholds
             for w    in hp['windows']
             for tp   in hp['take_profits']
             for sl   in hp['stop_losses']
             for hold in hp['hold_time_caps']
             for tpct in trail_pcts]

    dispatch_parallel_grid(shared_pool, tasks, ticker, strategy_name, config_version,
                           "Phase1-Coarse", spy_bh, asset_bh, run_timestamp, fixed_sl, entry_timing)


# ── Checkpoint 1: rank by coarse alpha, return island candidates ──────────────

def identify_island_candidates(config_version, strategy_name, n_index, n_stock, allowed_tickers=None, fixed_sl=0, entry_timing='close'):
    scope_sql, scope_params = _campaign_scope_sql(strategy_name, fixed_sl, entry_timing)
    with sqlite3.connect(DB_PATH) as conn:
        query = f"""
            SELECT b.ticker, MAX({ROBUST_ALPHA_SQL}) as best_alpha,
                   t.index_underlier, t.stock_underlier
            FROM backtest_cache b
            LEFT JOIN tickers t ON t.symbol = b.ticker
            WHERE b.version=? AND b.strategy=? AND b.trades > 0 {scope_sql}
        """
        params = [config_version, strategy_name, *scope_params]
        if allowed_tickers:
            query += f" AND b.ticker IN ({','.join('?' * len(allowed_tickers))})"
            params += list(allowed_tickers)
        query += " GROUP BY b.ticker ORDER BY best_alpha DESC"
        df = pd.read_sql(query, conn, params=params)

    def utype(row):
        if pd.notna(row.get('index_underlier')) and row['index_underlier']:
            return 'index'
        return 'other'

    df['underlier'] = df.apply(utype, axis=1)
    top_index = df[df['underlier'] == 'index'].head(n_index)['ticker'].tolist()
    top_other  = df[df['underlier'] != 'index'].head(n_stock)['ticker'].tolist()

    logger.info(f"Checkpoint1 — top index ({n_index}): {top_index}")
    logger.info(f"Checkpoint1 — top other ({n_stock}): {top_other}")
    return top_index, top_other


# ── Phase 2: Island mesh ──────────────────────────────────────────────────────

def run_phase2_island(shared_pool, ticker, strategy_name, config_version, hp, spy_bh, asset_bh, run_timestamp, fixed_sl=0, entry_timing='close', generation=None):
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)
    trail_pcts = _trail_pcts_for_strategy(strategy_name, hp)
    scope_sql, scope_params = _campaign_scope_sql(strategy_name, fixed_sl, entry_timing)
    tasks = set()
    with sqlite3.connect(DB_PATH) as conn:
        for z in hp['z_score_thresholds']:
            for w in hp['windows']:
                for tpct in trail_pcts:
                    params = [config_version, ticker, strategy_name, float(z), int(w), *scope_params]
                    tpct_filter = ""
                    if fourth_axis_col == 'trail_pct':
                        tpct_filter = "AND trail_sell_pct=?"
                        params.append(float(tpct))
                    df_wz = pd.read_sql(f"""
                        SELECT axis_tp AS take_profit, {sl_axis_col} AS stop_loss, max_hold_hours, alpha_vs_spy,
                               {ROBUST_ALPHA_SQL} AS robust_alpha
                        FROM backtest_cache
                        WHERE version=? AND ticker=? AND strategy=?
                          AND z_score_threshold=? AND window=? AND trades > 0 {scope_sql} {tpct_filter}
                    """, conn, params=params)

                    if df_wz.empty:
                        continue

                    centers = pick_island_centers(df_wz)
                    for (tp_c, sl_c) in centers:
                        for tp in range(max(1, tp_c - FINE_RADIUS), min(30, tp_c + FINE_RADIUS) + 1):
                            for sl in range(max(1, sl_c - FINE_RADIUS), min(30, sl_c + FINE_RADIUS) + 1):
                                for hold in hp['hold_time_caps']:
                                    tasks.add((tp, sl, int(hold), int(w), float(z), float(tpct)))

    if not tasks:
        logger.warning(f"[{ticker}] Phase2: no island tasks generated.")
        return

    logger.info(f"[{ticker}] Phase2 island mesh: {len(tasks)} tasks ({N_ISLANDS} islands ±{FINE_RADIUS})")
    dispatch_parallel_grid(shared_pool, list(tasks), ticker, strategy_name, config_version,
                           "Phase2-Island", spy_bh, asset_bh, run_timestamp, fixed_sl, entry_timing,
                           generation=generation)


# ── Phase 2.5: targeted cliff-box sweep around true best node ────────────────

def run_phase25_cliff_box(shared_pool, ticker, strategy_name, config_version, hp, spy_bh, asset_bh, run_timestamp, fixed_sl=0, entry_timing='close'):
    """Sweep ±CLIFF_RADIUS in TP/SL and ±7h in hold around the true best node from Phase 2.
    Guarantees cliff check has complete neighborhood data regardless of where the peak landed.
    For TrailingBothZScoreBreakout, also sweeps the trail_pct axis's immediate neighbors
    (±1 in the configured trail_pcts list) so Checkpoint 2's cliff check has data there too."""
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)
    trail_pcts = _trail_pcts_for_strategy(strategy_name, hp)
    scope_sql, scope_params = _campaign_scope_sql(strategy_name, fixed_sl, entry_timing)
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(f"""
            SELECT axis_tp, {sl_axis_col} AS stop_loss, max_hold_hours, window, z_score_threshold,
                   {'trail_sell_pct' if fourth_axis_col == 'trail_pct' else '0'} AS tpct
            FROM backtest_cache
            WHERE version=? AND ticker=? AND strategy=? AND trades > 0 {scope_sql}
            ORDER BY {ROBUST_ALPHA_SQL} DESC LIMIT 1
        """, (config_version, ticker, strategy_name, *scope_params)).fetchone()
    if not row:
        return
    tp_c, sl_c, hold_c, w_c, z_c, tpct_c = int(row[0]), int(row[1]), int(row[2]), int(row[3]), float(row[4]), float(row[5])

    if fourth_axis_col == 'trail_pct' and tpct_c in trail_pcts:
        idx = trail_pcts.index(tpct_c)
        tpct_neighbors = trail_pcts[max(0, idx - 1): idx + 2]
    else:
        tpct_neighbors = [tpct_c]

    tasks = set()
    for tp in range(max(1, tp_c - CLIFF_RADIUS), min(30, tp_c + CLIFF_RADIUS) + 1):
        for sl in range(max(1, sl_c - CLIFF_RADIUS), min(30, sl_c + CLIFF_RADIUS) + 1):
            for hold in [h for h in hp['hold_time_caps'] if abs(h - hold_c) <= 7]:
                for tpct in tpct_neighbors:
                    tasks.add((tp, sl, hold, w_c, z_c, float(tpct)))

    logger.info(f"[{ticker}] Phase2.5 cliff-box: {len(tasks)} tasks around TP={tp_c} SL={sl_c} hold={hold_c}h")
    dispatch_parallel_grid(shared_pool, list(tasks), ticker, strategy_name, config_version,
                           "Phase2.5-CliffBox", spy_bh, asset_bh, run_timestamp, fixed_sl, entry_timing)


# ── Checkpoint 2: cliff check, return full-mesh candidates ───────────────────

def identify_full_mesh_candidates(config_version, strategy_name, island_tickers, n_index, n_stock, fixed_sl=0, entry_timing='close'):
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)
    scope_sql, scope_params = _campaign_scope_sql(strategy_name, fixed_sl, entry_timing)
    results = []
    with sqlite3.connect(DB_PATH) as conn:
        for ticker in island_tickers:
            row = conn.execute(f"""
                SELECT axis_tp, {sl_axis_col} AS stop_loss, max_hold_hours, window, z_score_threshold,
                       {ROBUST_ALPHA_SQL} AS robust_alpha,
                       {'trail_sell_pct' if fourth_axis_col == 'trail_pct' else '0'} AS tpct
                FROM backtest_cache
                WHERE version=? AND ticker=? AND strategy=? AND trades > 0 {scope_sql}
                ORDER BY robust_alpha DESC LIMIT 1
            """, (config_version, ticker, strategy_name, *scope_params)).fetchone()
            if not row:
                continue

            tp_c, sl_c, hold_c, win_c, z_c, best_alpha, tpct_c = (
                int(row[0]), int(row[1]), int(row[2]), int(row[3]), float(row[4]), float(row[5]), float(row[6]))

            tpct_filter = ""
            tpct_params = []
            if fourth_axis_col == 'trail_pct':
                tpct_filter = "AND trail_sell_pct BETWEEN ? AND ?"
                tpct_params = [tpct_c - 1, tpct_c + 1]

            worst = conn.execute(f"""
                SELECT MIN({ROBUST_ALPHA_SQL}) FROM backtest_cache
                WHERE version=? AND ticker=? AND strategy=?
                  AND window=? AND z_score_threshold=?
                  AND axis_tp        BETWEEN ? AND ?
                  AND {sl_axis_col}  BETWEEN ? AND ?
                  AND max_hold_hours BETWEEN ? AND ?
                  {scope_sql} {tpct_filter}
                  AND trades > 0
            """, (config_version, ticker, strategy_name,
                  win_c, z_c,
                  tp_c - CLIFF_RADIUS, tp_c + CLIFF_RADIUS,
                  sl_c - CLIFF_RADIUS, sl_c + CLIFF_RADIUS,
                  hold_c - 7, hold_c + 7,
                  *scope_params, *tpct_params)).fetchone()[0]

            worst_neighbor = float(worst) if worst is not None else 0.0
            cliff = worst_neighbor < 0
            logger.info(f"  [{ticker}] best={best_alpha:+.1f}%  worst_neighbor={worst_neighbor:+.1f}%  {'CLIFF' if cliff else 'safe'}")
            results.append({'ticker': ticker, 'best_alpha': best_alpha, 'worst_neighbor': worst_neighbor})

    if not results:
        return [], []

    df = pd.DataFrame(results)
    safe = df[df['worst_neighbor'] >= 0].sort_values('best_alpha', ascending=False)

    with sqlite3.connect(DB_PATH) as conn:
        t_df = pd.read_sql("SELECT symbol, index_underlier, avg_vol_10d, last_price FROM tickers", conn)
    t_df = t_df.rename(columns={'symbol': 'ticker'})
    safe = safe.merge(t_df, on='ticker', how='left')
    safe['is_index'] = safe['index_underlier'].notna() & (safe['index_underlier'].astype(str).str.strip() != '')
    safe['max_notional'] = safe['avg_vol_10d'] * safe['last_price'] * 0.01

    before = len(safe)
    safe = safe[safe['best_alpha'] >= 200]
    safe = safe[safe['max_notional'].notna() & (safe['max_notional'] >= 50_000)]
    logger.info(f"Checkpoint2 — filters: {before} cliff-free → {len(safe)} after alpha>=200% and liq>=$50k")

    all_index = safe[safe['is_index']]['ticker'].tolist()
    all_other  = safe[~safe['is_index']]['ticker'].tolist()
    top_other  = all_other[:n_stock]
    rest_other = all_other[n_stock:]

    logger.info(f"Checkpoint2 — Phase3 order: top {n_stock} non-index → all index ({len(all_index)}) → remaining non-index ({len(rest_other)})")
    logger.info(f"  Top non-index: {top_other}")
    logger.info(f"  Index: {all_index}")
    logger.info(f"  Remaining non-index: {rest_other}")
    return top_other, all_index, rest_other


# ── Phase 3: Full mesh ────────────────────────────────────────────────────────

def run_phase3_full(shared_pool, ticker, strategy_name, config_version, hp, spy_bh, asset_bh, run_timestamp, fixed_sl=0, entry_timing='close'):
    trail_pcts = _trail_pcts_for_strategy(strategy_name, hp)
    tasks = [(tp, sl, int(hold), int(w), float(z), float(tpct))
             for z    in hp['z_score_thresholds']
             for w    in hp['windows']
             for tp   in range(1, 31)
             for sl   in range(1, 31)
             for hold in hp['hold_time_caps']
             for tpct in trail_pcts]

    logger.info(f"[{ticker}] Phase3 full mesh: {len(tasks)} tasks (cache skips coarse+island already done)")
    dispatch_parallel_grid(shared_pool, tasks, ticker, strategy_name, config_version,
                           "Phase3-Full", spy_bh, asset_bh, run_timestamp, fixed_sl, entry_timing)

    scope_sql, scope_params = _campaign_scope_sql(strategy_name, fixed_sl, entry_timing)
    with sqlite3.connect(DB_PATH) as conn:
        pre_phase3 = conn.execute(f"""
            SELECT MAX({ROBUST_ALPHA_SQL}) FROM backtest_cache
            WHERE version=? AND ticker=? AND strategy=? AND trades > 0
              AND phase IN ('Phase1-Coarse','Phase2-Island','Phase2.5-CliffBox') {scope_sql}
        """, (config_version, ticker, strategy_name, *scope_params)).fetchone()[0]
        overall = conn.execute(f"""
            SELECT MAX({ROBUST_ALPHA_SQL}) FROM backtest_cache
            WHERE version=? AND ticker=? AND strategy=? AND trades > 0 {scope_sql}
        """, (config_version, ticker, strategy_name, *scope_params)).fetchone()[0]
    pre_phase3 = float(pre_phase3) if pre_phase3 is not None else None
    overall    = float(overall) if overall is not None else None
    if overall is not None:
        if pre_phase3 is not None:
            improved = overall > pre_phase3
            logger.info(f"  [{ticker}] Phase3 best={overall:+.1f}%  (pre-Phase3 best={pre_phase3:+.1f}%)  "
                        f"{'IMPROVED' if improved else 'no improvement'}")
        else:
            logger.info(f"  [{ticker}] Phase3 best={overall:+.1f}%  (no pre-Phase3 data to compare)")

    # Static heatmap PNG disabled — superseded by the interactive Spatial Topology page,
    # nothing read these files back. Left commented instead of deleted in case that changes.
    # with sqlite3.connect(DB_PATH) as conn:
    #     df = pd.read_sql("""
    #         SELECT take_profit, stop_loss, max_hold_hours, alpha_vs_spy
    #         FROM backtest_cache
    #         WHERE version=? AND ticker=? AND strategy=? AND trades > 0
    #     """, conn, params=(config_version, ticker, strategy_name))
    # if df.empty:
    #     return
    # best_hold = int(df.nlargest(1, 'alpha_vs_spy')['max_hold_hours'].iloc[0])
    # df_plane  = df[df['max_hold_hours'] == best_hold]
    # if len(df_plane) < 2:
    #     return
    # try:
    #     pivot = df_plane.groupby(['stop_loss', 'take_profit'])['alpha_vs_spy'].mean().unstack('take_profit')
    #     plt.figure(figsize=(12, 10))
    #     sns.heatmap(pivot, annot=False, cmap='RdYlGn', cbar_kws={'label': 'Alpha vs SPY %'}, linewidths=0.5)
    #     plt.title(f"{ticker} — {strategy_name} @ {best_hold}h ({config_version})")
    #     out = OPTO_LOG_DIR / f"topology_{ticker}_{strategy_name}.png"
    #     plt.savefig(out, bbox_inches='tight')
    #     plt.close()
    #     logger.info(f"[{ticker}] Heatmap saved: {out}")
    # except Exception as e:
    #     logger.warning(f"[{ticker}] Heatmap failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Three-phase sweep engine")
    parser.add_argument("--phase", type=int, choices=[1, 2, 3], default=None,
                        help="Run only this phase (default: all phases)")
    parser.add_argument("--max-phase", dest="max_phase", default="3", choices=["1", "2", "2.5", "3"],
                        help="Run phases up through this one, then stop (default: 3, full pipeline). "
                             "Island/cliff-safety selection only needs data through Phase 2.5 -- "
                             "'--max-phase 2.5' skips the expensive full-mesh Phase 3 pass entirely.")
    parser.add_argument("--tickers", nargs="+", default=None,
                        help="Override tickers to sweep")
    parser.add_argument("--version", default=None,
                        help="Override version from config.json")
    parser.add_argument("--entry-timing", dest="entry_timing", default=None, choices=["close", "open_check"],
                        help="Override entry_timing from config.json (close=bar-close only, "
                             "open_check=also check bar Open before falling through to Close)")
    parser.add_argument("--skip-cache-refresh", action="store_true",
                        help="Skip dropdown/pivot/cliff-grid cache refresh at end of run "
                             "(useful when chaining multiple versions and refreshing once at the end)")
    args = parser.parse_args()

    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    run_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    logger.info("=" * 52)
    logger.info("  THREE-PHASE SWEEP ENGINE")
    logger.info("  Phase1: Coarse  |  Phase2: Island  |  Phase3: Full")
    logger.info("=" * 52)

    try:
        with open("config.json") as f:
            config = json.load(f)
    except Exception as e:
        logger.critical(f"Failed to load config.json: {e}")
        sys.exit(1)

    config_version    = args.version or config.get("version", "v1.6")
    hp                = config["hyperparameters"]
    max_workers       = config.get("execution", {}).get("max_workers", 6)
    fixed_sl          = config.get("execution", {}).get("fixed_stop_loss", 0)
    entry_timing      = args.entry_timing or config.get("execution", {}).get("entry_timing", "close")
    tickers           = args.tickers or config.get("target_tickers", [])
    strategy_names    = config.get("active_strategies", ["ZScoreBreakout"])
    phase_only        = args.phase
    max_phase         = args.max_phase

    if not tickers:
        logger.error("No tickers in config.")
        sys.exit(1)

    init_idempotent_db()

    run_id_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_log_path = OPTO_LOG_DIR / f"sweep_{config_version}_{run_id_tag}.log"

    # tqdm progress bars write directly to stderr, bypassing the logging module —
    # a logging.FileHandler alone misses all of it. Tee raw stdout/stderr into the
    # run log file too so it has everything, not just structured logger.info() calls.
    class _Tee:
        def __init__(self, *streams):
            self._streams = streams
        def write(self, data):
            for s in self._streams:
                s.write(data)
        def flush(self):
            for s in self._streams:
                s.flush()

    run_log_file = open(run_log_path, "a")
    sys.stdout = _Tee(sys.stdout, run_log_file)
    sys.stderr = _Tee(sys.stderr, run_log_file)

    # logging.StreamHandler captured the original sys.stdout object at basicConfig()
    # time (module import) — reassigning sys.stdout above doesn't retarget it, so
    # logger.info() calls still need their own handler to reach the run log file.
    run_file_handler = logging.FileHandler(run_log_path)
    run_file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logging.getLogger().addHandler(run_file_handler)

    run_id = start_sweep_run(config_version, strategy_names, tickers, config, run_log_path)

    def _mark_failed_on_crash(exc_type, exc_value, exc_tb):
        logger.critical("Sweep crashed", exc_info=(exc_type, exc_value, exc_tb))
        update_sweep_run(run_id, status='FAILED',
                          finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                          notes=str(exc_value)[:500])
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    sys.excepthook = _mark_failed_on_crash

    logger.info(f"Version: {config_version} | Tickers: {len(tickers)} | Workers: {max_workers}")
    logger.info(f"Coarse grid: TP/SL {hp['take_profits']} | Hold: {len(hp['hold_time_caps'])} values | Z: {hp['z_score_thresholds']}")

    # Precompute B&H returns once — reused across all phases
    logger.info("Precomputing B&H returns for all tickers...")
    bh_cache = {}
    for ticker in tickers:
        asset_bh, spy_bh = compute_bh_returns(ticker)
        if asset_bh is not None:
            bh_cache[ticker] = (asset_bh, spy_bh)
    valid_tickers = [t for t in tickers if t in bh_cache]
    logger.info(f"Valid tickers with cache data: {len(valid_tickers)}/{len(tickers)}")

    with ProcessPoolExecutor(max_workers=max_workers, initializer=_warmup_worker) as shared_pool:

        if phase_only != 3:
            # ── Phase 1 ───────────────────────────────────────────────────────
            logger.info(f"\n{'='*52}")
            logger.info(f"PHASE 1 — COARSE SCAN ({len(valid_tickers)} tickers) [{config_version}]")
            logger.info(f"{'='*52}")
            for ticker in valid_tickers:
                for name in strategy_names:
                    if not getattr(strategies, name, None):
                        logger.warning(f"Unknown strategy: {name}")
                        continue
                    asset_bh, spy_bh = bh_cache[ticker]
                    run_phase1_coarse(shared_pool, ticker, name, config_version, hp, spy_bh, asset_bh, run_timestamp, fixed_sl, entry_timing)

            logger.info("Phase 1 complete.")

        max_generations = config.get("execution", {}).get("max_generations", 1)
        max_generations = max(1, max_generations)

        for name in strategy_names:
            if not getattr(strategies, name, None):
                continue

            if phase_only == 3:
                # Skip directly to Phase 3 using provided tickers
                full_tickers = [t for t in valid_tickers if t in bh_cache]
                logger.info(f"\n{'='*52}")
                logger.info(f"PHASE 3 — FULL MESH ({len(full_tickers)} tickers) [direct] [{config_version}]")
                logger.info(f"{'='*52}")
                for ticker in full_tickers:
                    if ticker not in bh_cache:
                        logger.warning(f"[{ticker}] No B&H data, skipping Phase 3.")
                        continue
                    asset_bh, spy_bh = bh_cache[ticker]
                    run_phase3_full(shared_pool, ticker, name, config_version, hp, spy_bh, asset_bh, run_timestamp, fixed_sl, entry_timing)
                continue

            island_tickers = []
            for gen in range(max_generations):
                # ── Checkpoint 1 (re-run each generation) ────────────────
                logger.info(f"\nCheckpoint 1 (gen {gen+1}/{max_generations}): ranking results for {name} [{config_version}]...")
                top_index, top_other = identify_island_candidates(config_version, name, 25, 5, allowed_tickers=valid_tickers, fixed_sl=fixed_sl, entry_timing=entry_timing)
                island_tickers = top_index + top_other

                if not island_tickers:
                    logger.warning("No island candidates. Skipping phases 2 & 3.")
                    break

                if phase_only == 1 or max_phase == "1":
                    continue

                # ── Phase 2 ───────────────────────────────────────────────
                logger.info(f"\n{'='*52}")
                logger.info(f"PHASE 2 — ISLAND MESH gen {gen+1}/{max_generations} ({len(island_tickers)} tickers) [{config_version}]")
                logger.info(f"{'='*52}")
                for ticker in island_tickers:
                    if ticker not in bh_cache:
                        logger.warning(f"[{ticker}] No B&H data, skipping Phase 2.")
                        continue
                    asset_bh, spy_bh = bh_cache[ticker]
                    run_phase2_island(shared_pool, ticker, name, config_version, hp, spy_bh, asset_bh, run_timestamp, fixed_sl, entry_timing, generation=gen + 1)

                logger.info(f"Phase 2 gen {gen+1} complete.")

            if not island_tickers or phase_only in (1, 2) or max_phase in ("1", "2"):
                continue

            # ── Phase 2.5 ─────────────────────────────────────────────────
            logger.info(f"\n{'='*52}")
            logger.info(f"PHASE 2.5 — CLIFF-BOX SWEEP ({len(island_tickers)} tickers) [{config_version}]")
            logger.info(f"{'='*52}")
            for ticker in island_tickers:
                if ticker not in bh_cache:
                    continue
                asset_bh, spy_bh = bh_cache[ticker]
                run_phase25_cliff_box(shared_pool, ticker, name, config_version, hp, spy_bh, asset_bh, run_timestamp, fixed_sl, entry_timing)

            # ── Checkpoint 2 ─────────────────────────────────────────────
            logger.info(f"\nCheckpoint 2: cliff check on {len(island_tickers)} island tickers [{config_version}]...")
            top_other, all_index, rest_other = identify_full_mesh_candidates(
                config_version, name, island_tickers, 5, 5, fixed_sl=fixed_sl, entry_timing=entry_timing
            )
            # Order: top 5 non-index → all index → remaining non-index
            full_tickers = top_other + all_index + rest_other

            if not full_tickers:
                logger.warning("No cliff-free candidates for Phase 3.")
                continue

            if max_phase in ("1", "2", "2.5"):
                logger.info(f"--max-phase {max_phase}: skipping Phase 3 for {name}.")
                continue

            # ── Phase 3 ───────────────────────────────────────────────────
            logger.info(f"\n{'='*52}")
            logger.info(f"PHASE 3 — FULL MESH ({len(full_tickers)} tickers) [{config_version}]")
            logger.info(f"{'='*52}")
            for ticker in full_tickers:
                if ticker not in bh_cache:
                    logger.warning(f"[{ticker}] No B&H data, skipping Phase 3.")
                    continue
                asset_bh, spy_bh = bh_cache[ticker]
                run_phase3_full(shared_pool, ticker, name, config_version, hp, spy_bh, asset_bh, run_timestamp, fixed_sl, entry_timing)

    if args.skip_cache_refresh:
        logger.info("\nSkipping cache refresh (--skip-cache-refresh).")
    else:
        logger.info("\nFinal cache refresh...")
        refresh_dropdown_cache()
        refresh_pivot_cache(versions=[config_version])
        try:
            refresh_cliff_grid_cache()
        except Exception as e:
            logger.warning(f"Cliff grid cache refresh failed (page will fall back to live query): {e}")

    for p in ["current_test.json", "active_phase_grid.json"]:
        if os.path.exists(p):
            try: os.remove(p)
            except Exception: pass

    logger.info("=" * 52)
    logger.info("  THREE-PHASE SWEEP COMPLETE")
    logger.info("=" * 52)

    update_sweep_run(run_id, status='COMPLETE',
                      finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                      phase_reached=f"phase{phase_only}" if phase_only else "all")
