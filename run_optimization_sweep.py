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

from backtester import run_backtest, run_backtest_v17, run_backtest_v18, run_backtest_v19, run_backtest_v110, prep_inputs
import strategies
from db_cache import refresh_dropdown_cache, refresh_pivot_cache, refresh_cliff_grid_cache

CACHE_DIR    = Path("./cache")
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
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bc_version_ticker ON backtest_cache(version, ticker)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bc_version_window ON backtest_cache(version, window)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bc_version_ticker_strategy ON backtest_cache(version, ticker, strategy)")

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


def run_single_backtest_node_isolated(args):
    ticker, strategy_name, config_version, tp, sl, hold_hours, w, spy_bh, z_thresh, fixed_sl = args

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
        if issubclass(strategy_class, strategies.TrailingBothZScoreBreakout):
            trades = run_backtest_v110(
                df_hourly_raw, df_daily_processed, ticker,
                take_profit=float(tp / 100.0), stop_loss=float(fixed_sl / 100.0),
                max_hours_to_hold=int(hold_hours), z_score_threshold=float(z_thresh),
                trail_buy_pct=float(sl / 100.0), trail_pct=0.03, prep=prep
            )
        elif issubclass(strategy_class, strategies.TrailingBuyZScoreBreakout):
            trades = run_backtest_v19(
                df_hourly_raw, df_daily_processed, ticker,
                take_profit=float(tp / 100.0), stop_loss=float(fixed_sl / 100.0),
                max_hours_to_hold=int(hold_hours), z_score_threshold=float(z_thresh),
                trail_buy_pct=float(sl / 100.0), prep=prep
            )
        elif issubclass(strategy_class, strategies.TrailingExitZScoreBreakout):
            trades = run_backtest_v18(
                df_hourly_raw, df_daily_processed, ticker,
                take_profit=float(tp / 100.0), stop_loss=float(fixed_sl / 100.0),
                max_hours_to_hold=int(hold_hours), z_score_threshold=float(z_thresh),
                trail_pct=float(sl / 100.0), prep=prep
            )
        elif issubclass(strategy_class, strategies.LimitOrderZScoreBreakout):
            trades = run_backtest_v17(
                df_hourly_raw, df_daily_processed, ticker,
                take_profit=float(tp / 100.0), stop_loss=float(sl / 100.0),
                max_hours_to_hold=int(hold_hours), z_score_threshold=float(z_thresh), prep=prep
            )
        else:
            trades = run_backtest(
                df_hourly_raw, df_daily_processed, ticker,
                take_profit=float(tp / 100.0), stop_loss=float(sl / 100.0),
                max_hours_to_hold=int(hold_hours), z_score_threshold=float(z_thresh), prep=prep
            )
        closed = [t for t in trades if t["Result"] in ["WIN", "LOSS", "TWIN", "TLOSS"]]
    except Exception as e:
        return {"coords": (tp, sl, hold_hours), "payload": (0.0, 0, 0.0), "window": w, "z_thresh": z_thresh, "status": "SIM_ERROR", "error": repr(e)}

    if not closed:
        return {"coords": (tp, sl, hold_hours), "payload": (0.0, 0, 0.0), "window": w, "z_thresh": z_thresh, "status": "NO_TRADES"}

    df_tr = pd.DataFrame(closed)
    win_rate   = float((len(df_tr[df_tr['Result'] == 'WIN']) / len(df_tr)) * 100)
    compounded = float(((df_tr['Return'] + 1).prod() - 1) * 100)
    alpha_calc = float(compounded - spy_bh)

    return {
        "coords":  (tp, sl, hold_hours),
        "payload": (alpha_calc, len(df_tr), win_rate, compounded),
        "window":  w, "z_thresh": z_thresh, "status": "SUCCESS"
    }


def dispatch_parallel_grid(shared_pool, tasks, ticker, strategy_name, config_version, phase_label, spy_bh, asset_bh, run_timestamp, fixed_sl=0):
    conn   = sqlite3.connect(DB_PATH, timeout=60.0)
    cursor = conn.cursor()
    matrix_results  = []
    unvisited_tasks = []

    # One query for all cached nodes of this (strategy, version, ticker) instead of
    # one SELECT per task (Phase 3 = ~18k queries per ticker).
    cached_map = {}
    cursor.execute("""
        SELECT window, max_hold_hours, take_profit, stop_loss, z_score_threshold,
               trades, win_rate, strategy_return, alpha_vs_spy
        FROM backtest_cache
        WHERE strategy=? AND version=? AND ticker=?
    """, (strategy_name, config_version, ticker))
    for r in cursor.fetchall():
        if r[4] is None:
            continue  # legacy NULL-z rows never matched the old equality check either
        cached_map[(int(r[2]), int(r[3]), int(r[1]), int(r[0]), float(r[4]))] = (r[5], r[6], r[7], r[8])

    for t in tasks:
        tp, sl, hold_hours, w, z_thresh = t
        cached_row = cached_map.get((int(tp), int(sl), int(hold_hours), int(w), float(z_thresh)))
        if cached_row:
            matrix_results.append({
                "Strategy": strategy_name, "Version": config_version, "Ticker": ticker, "Window": w,
                "Take Profit %": int(tp), "Stop Loss %": int(sl), "Max Hold Hours": hold_hours,
                "Z Threshold": z_thresh,
                "Trades": cached_row[0], "Win Rate %": cached_row[1], "Return %": cached_row[2],
                "Alpha vs SPY %": cached_row[3], "Asset B&H %": asset_bh, "SPY B&H %": spy_bh
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
                           (ticker, strategy_name, config_version, int(tp), int(sl), hold, w, spy_bh, z, fixed_sl)): task
        for task in unvisited_tasks
        for tp, sl, hold, w, z in [task]
    }

    progress_bar = tqdm(
        as_completed(futures_map), total=len(futures_map),
        desc=f"[{ticker}] {phase_label}", unit="node",
        mininterval=15.0, maxinterval=30.0
    )

    node_counter      = 0
    last_postfix_time = 0.0
    fail_counts       = {}
    for future in progress_bar:
        tp, sl, hold_hours, w, z_thresh = futures_map[future]
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
                    alpha, num_trades, wr, comp_ret = res["payload"]
                else:
                    alpha, num_trades, wr, comp_ret = 0.0, 0, 0.0, 0.0

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
                        "Alpha vs SPY %": alpha, "Asset B&H %": asset_bh, "SPY B&H %": spy_bh
                    })

                cursor.execute(
                    "INSERT OR REPLACE INTO backtest_cache VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (strategy_name, config_version, ticker, w, hold_hours, int(tp), int(sl),
                     num_trades, wr, comp_ret, alpha, asset_bh, spy_bh, run_timestamp, z_thresh)
                )
                if node_counter % 100 == 0:
                    conn.commit()

        except Exception as e:
            logger.error(f"Worker crashed TP={tp} SL={sl}: {e}")

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

def pick_island_centers(df, n=N_ISLANDS, min_sep=ISLAND_MIN_SEP):
    centers = []
    for _, row in df.sort_values('alpha_vs_spy', ascending=False).iterrows():
        tp, sl = int(row['take_profit']), int(row['stop_loss'])
        if all(abs(tp - c[0]) >= min_sep or abs(sl - c[1]) >= min_sep for c in centers):
            centers.append((tp, sl))
        if len(centers) == n:
            break
    return centers


# ── Phase 1: Coarse scan ──────────────────────────────────────────────────────

def run_phase1_coarse(shared_pool, ticker, strategy_name, config_version, hp, spy_bh, asset_bh, run_timestamp, fixed_sl=0):
    z_thresholds = hp['z_score_thresholds']
    expected = (len(z_thresholds) * len(hp['windows']) * len(hp['take_profits'])
                * len(hp['stop_losses']) * len(hp['hold_time_caps']))

    with sqlite3.connect(DB_PATH, timeout=60.0) as chk:
        z_ph = ','.join('?' * len(z_thresholds))
        w_ph = ','.join('?' * len(hp['windows']))
        cached = chk.execute(
            f"SELECT COUNT(*) FROM backtest_cache WHERE strategy=? AND version=? AND ticker=?"
            f" AND z_score_threshold IN ({z_ph}) AND window IN ({w_ph})",
            (strategy_name, config_version, ticker, *z_thresholds, *hp['windows'])
        ).fetchone()[0]

    if cached >= expected:
        logger.info(f"[{ticker}] Phase1 fully cached ({cached}/{expected}). Skipping.")
        return

    tasks = [(int(tp), int(sl), int(hold), int(w), float(z))
             for z    in z_thresholds
             for w    in hp['windows']
             for tp   in hp['take_profits']
             for sl   in hp['stop_losses']
             for hold in hp['hold_time_caps']]

    dispatch_parallel_grid(shared_pool, tasks, ticker, strategy_name, config_version,
                           "Phase1-Coarse", spy_bh, asset_bh, run_timestamp, fixed_sl)


# ── Checkpoint 1: rank by coarse alpha, return island candidates ──────────────

def identify_island_candidates(config_version, strategy_name, n_index, n_stock, allowed_tickers=None):
    with sqlite3.connect(DB_PATH) as conn:
        query = """
            SELECT b.ticker, MAX(b.alpha_vs_spy) as best_alpha,
                   t.index_underlier, t.stock_underlier
            FROM backtest_cache b
            LEFT JOIN tickers t ON t.symbol = b.ticker
            WHERE b.version=? AND b.strategy=? AND b.trades > 0
        """
        params = [config_version, strategy_name]
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

def run_phase2_island(shared_pool, ticker, strategy_name, config_version, hp, spy_bh, asset_bh, run_timestamp, fixed_sl=0):
    tasks = set()
    with sqlite3.connect(DB_PATH) as conn:
        for z in hp['z_score_thresholds']:
            for w in hp['windows']:
                df_wz = pd.read_sql("""
                    SELECT take_profit, stop_loss, max_hold_hours, alpha_vs_spy
                    FROM backtest_cache
                    WHERE version=? AND ticker=? AND strategy=?
                      AND z_score_threshold=? AND window=? AND trades > 0
                """, conn, params=(config_version, ticker, strategy_name, float(z), int(w)))

                if df_wz.empty:
                    continue

                centers = pick_island_centers(df_wz)
                for (tp_c, sl_c) in centers:
                    for tp in range(max(1, tp_c - FINE_RADIUS), min(30, tp_c + FINE_RADIUS) + 1):
                        for sl in range(max(1, sl_c - FINE_RADIUS), min(30, sl_c + FINE_RADIUS) + 1):
                            for hold in hp['hold_time_caps']:
                                tasks.add((tp, sl, int(hold), int(w), float(z)))

    if not tasks:
        logger.warning(f"[{ticker}] Phase2: no island tasks generated.")
        return

    logger.info(f"[{ticker}] Phase2 island mesh: {len(tasks)} tasks ({N_ISLANDS} islands ±{FINE_RADIUS})")
    dispatch_parallel_grid(shared_pool, list(tasks), ticker, strategy_name, config_version,
                           "Phase2-Island", spy_bh, asset_bh, run_timestamp, fixed_sl)


# ── Phase 2.5: targeted cliff-box sweep around true best node ────────────────

def run_phase25_cliff_box(shared_pool, ticker, strategy_name, config_version, hp, spy_bh, asset_bh, run_timestamp, fixed_sl=0):
    """Sweep ±CLIFF_RADIUS in TP/SL and ±7h in hold around the true best node from Phase 2.
    Guarantees cliff check has complete neighborhood data regardless of where the peak landed."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("""
            SELECT take_profit, stop_loss, max_hold_hours, window, z_score_threshold FROM backtest_cache
            WHERE version=? AND ticker=? AND strategy=? AND trades > 0
            ORDER BY alpha_vs_spy DESC LIMIT 1
        """, (config_version, ticker, strategy_name)).fetchone()
    if not row:
        return
    tp_c, sl_c, hold_c, w_c, z_c = int(row[0]), int(row[1]), int(row[2]), int(row[3]), float(row[4])

    tasks = set()
    for tp in range(max(1, tp_c - CLIFF_RADIUS), min(30, tp_c + CLIFF_RADIUS) + 1):
        for sl in range(max(1, sl_c - CLIFF_RADIUS), min(30, sl_c + CLIFF_RADIUS) + 1):
            for hold in [h for h in hp['hold_time_caps'] if abs(h - hold_c) <= 7]:
                tasks.add((tp, sl, hold, w_c, z_c))

    logger.info(f"[{ticker}] Phase2.5 cliff-box: {len(tasks)} tasks around TP={tp_c} SL={sl_c} hold={hold_c}h")
    dispatch_parallel_grid(shared_pool, list(tasks), ticker, strategy_name, config_version,
                           "Phase2.5-CliffBox", spy_bh, asset_bh, run_timestamp, fixed_sl)


# ── Checkpoint 2: cliff check, return full-mesh candidates ───────────────────

def identify_full_mesh_candidates(config_version, strategy_name, island_tickers, n_index, n_stock):
    results = []
    with sqlite3.connect(DB_PATH) as conn:
        for ticker in island_tickers:
            row = conn.execute("""
                SELECT take_profit, stop_loss, max_hold_hours, window, z_score_threshold, alpha_vs_spy FROM backtest_cache
                WHERE version=? AND ticker=? AND strategy=? AND trades > 0
                ORDER BY alpha_vs_spy DESC LIMIT 1
            """, (config_version, ticker, strategy_name)).fetchone()
            if not row:
                continue

            tp_c, sl_c, hold_c, win_c, z_c, best_alpha = int(row[0]), int(row[1]), int(row[2]), int(row[3]), float(row[4]), float(row[5])

            worst = conn.execute("""
                SELECT MIN(alpha_vs_spy) FROM backtest_cache
                WHERE version=? AND ticker=? AND strategy=?
                  AND window=? AND z_score_threshold=?
                  AND take_profit    BETWEEN ? AND ?
                  AND stop_loss      BETWEEN ? AND ?
                  AND max_hold_hours BETWEEN ? AND ?
                  AND trades > 0
            """, (config_version, ticker, strategy_name,
                  win_c, z_c,
                  tp_c - CLIFF_RADIUS, tp_c + CLIFF_RADIUS,
                  sl_c - CLIFF_RADIUS, sl_c + CLIFF_RADIUS,
                  hold_c - 7, hold_c + 7)).fetchone()[0]

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

def run_phase3_full(shared_pool, ticker, strategy_name, config_version, hp, spy_bh, asset_bh, run_timestamp, fixed_sl=0):
    tasks = [(tp, sl, int(hold), int(w), float(z))
             for z    in hp['z_score_thresholds']
             for w    in hp['windows']
             for tp   in range(1, 31)
             for sl   in range(1, 31)
             for hold in hp['hold_time_caps']]

    logger.info(f"[{ticker}] Phase3 full mesh: {len(tasks)} tasks (cache skips coarse+island already done)")
    dispatch_parallel_grid(shared_pool, tasks, ticker, strategy_name, config_version,
                           "Phase3-Full", spy_bh, asset_bh, run_timestamp, fixed_sl)

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
    parser.add_argument("--tickers", nargs="+", default=None,
                        help="Override tickers to sweep")
    parser.add_argument("--version", default=None,
                        help="Override version from config.json")
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
    tickers           = args.tickers or config.get("target_tickers", [])
    strategy_names    = config.get("active_strategies", ["ZScoreBreakout"])
    phase_only        = args.phase

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

    with ProcessPoolExecutor(max_workers=max_workers) as shared_pool:

        if phase_only != 3:
            # ── Phase 1 ───────────────────────────────────────────────────────
            logger.info(f"\n{'='*52}")
            logger.info(f"PHASE 1 — COARSE SCAN ({len(valid_tickers)} tickers)")
            logger.info(f"{'='*52}")
            for ticker in valid_tickers:
                for name in strategy_names:
                    if not getattr(strategies, name, None):
                        logger.warning(f"Unknown strategy: {name}")
                        continue
                    asset_bh, spy_bh = bh_cache[ticker]
                    run_phase1_coarse(shared_pool, ticker, name, config_version, hp, spy_bh, asset_bh, run_timestamp, fixed_sl)

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
                logger.info(f"PHASE 3 — FULL MESH ({len(full_tickers)} tickers) [direct]")
                logger.info(f"{'='*52}")
                for ticker in full_tickers:
                    if ticker not in bh_cache:
                        logger.warning(f"[{ticker}] No B&H data, skipping Phase 3.")
                        continue
                    asset_bh, spy_bh = bh_cache[ticker]
                    run_phase3_full(shared_pool, ticker, name, config_version, hp, spy_bh, asset_bh, run_timestamp, fixed_sl)
                continue

            island_tickers = []
            for gen in range(max_generations):
                # ── Checkpoint 1 (re-run each generation) ────────────────
                logger.info(f"\nCheckpoint 1 (gen {gen+1}/{max_generations}): ranking results for {name}...")
                top_index, top_other = identify_island_candidates(config_version, name, 25, 5, allowed_tickers=valid_tickers)
                island_tickers = top_index + top_other

                if not island_tickers:
                    logger.warning("No island candidates. Skipping phases 2 & 3.")
                    break

                if phase_only == 1:
                    continue

                # ── Phase 2 ───────────────────────────────────────────────
                logger.info(f"\n{'='*52}")
                logger.info(f"PHASE 2 — ISLAND MESH gen {gen+1}/{max_generations} ({len(island_tickers)} tickers)")
                logger.info(f"{'='*52}")
                for ticker in island_tickers:
                    if ticker not in bh_cache:
                        logger.warning(f"[{ticker}] No B&H data, skipping Phase 2.")
                        continue
                    asset_bh, spy_bh = bh_cache[ticker]
                    run_phase2_island(shared_pool, ticker, name, config_version, hp, spy_bh, asset_bh, run_timestamp, fixed_sl)

                logger.info(f"Phase 2 gen {gen+1} complete.")

            if not island_tickers or phase_only in (1, 2):
                continue

            # ── Phase 2.5 ─────────────────────────────────────────────────
            logger.info(f"\n{'='*52}")
            logger.info(f"PHASE 2.5 — CLIFF-BOX SWEEP ({len(island_tickers)} tickers)")
            logger.info(f"{'='*52}")
            for ticker in island_tickers:
                if ticker not in bh_cache:
                    continue
                asset_bh, spy_bh = bh_cache[ticker]
                run_phase25_cliff_box(shared_pool, ticker, name, config_version, hp, spy_bh, asset_bh, run_timestamp, fixed_sl)

            # ── Checkpoint 2 ─────────────────────────────────────────────
            logger.info(f"\nCheckpoint 2: cliff check on {len(island_tickers)} island tickers...")
            top_other, all_index, rest_other = identify_full_mesh_candidates(
                config_version, name, island_tickers, 5, 5
            )
            # Order: top 5 non-index → all index → remaining non-index
            full_tickers = top_other + all_index + rest_other

            if not full_tickers:
                logger.warning("No cliff-free candidates for Phase 3.")
                continue

            # ── Phase 3 ───────────────────────────────────────────────────
            logger.info(f"\n{'='*52}")
            logger.info(f"PHASE 3 — FULL MESH ({len(full_tickers)} tickers)")
            logger.info(f"{'='*52}")
            for ticker in full_tickers:
                if ticker not in bh_cache:
                    logger.warning(f"[{ticker}] No B&H data, skipping Phase 3.")
                    continue
                asset_bh, spy_bh = bh_cache[ticker]
                run_phase3_full(shared_pool, ticker, name, config_version, hp, spy_bh, asset_bh, run_timestamp, fixed_sl)

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
