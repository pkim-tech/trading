import sys
import os
import logging
import json
import sqlite3
import time
from pathlib import Path
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor, as_completed
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from tqdm import tqdm

# --- Dynamic Core Strategy Imports ---
from backtester import run_backtest
import strategies

# --- Global Workspace Environments ---
CACHE_DIR = Path("./cache")
OPTO_LOG_DIR = Path("./logs")
OPTO_LOG_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = CACHE_DIR / "trading_universe.db"

# --- Structural Log Level Framing Configuration ---
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
    """Validates and enforces target SQL relational table architecture schemas."""
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
            PRIMARY KEY (strategy, version, ticker, window, max_hold_hours, take_profit, stop_loss)
        )
    """)
    conn.commit()
    conn.close()


def run_single_backtest_node_isolated(args):
    """Pure mathematical worker node running inside the isolated subprocess."""
    ticker, strategy_name, config_version, tp, sl, hold_hours, w, spy_bh = args

    try:
        cache_path = CACHE_DIR / f"{ticker}_1h.csv"
        df_hourly_raw = pd.read_csv(cache_path, index_col=0, parse_dates=True).sort_index()
    except Exception:
        return {"coords": (tp, sl, hold_hours), "payload": (0.0, 0, 0.0), "window": w, "status": "ERROR"}

    if df_hourly_raw.empty:
        return {"coords": (tp, sl, hold_hours), "payload": (0.0, 0, 0.0), "window": w, "status": "EMPTY"}

    # 🔄 Dynamically mappings class references to prevent missing strategy errors in processes
    strategy_mapping = {
        "ZScore_Original": strategies.ZScoreBreakout,
        "ZScore_TrendFiltered": strategies.TrendFilteredZScore
    }
    strategy_class = strategy_mapping.get(strategy_name)
    if not strategy_class:
        return {"coords": (tp, sl, hold_hours), "payload": (0.0, 0, 0.0), "window": w, "status": "UNKNOWN_STRAT"}

    close_col = 'Adj Close' if 'Adj Close' in df_hourly_raw.columns else 'Close'
    df_daily = df_hourly_raw.resample('D').last().dropna(subset=[close_col])
    
    strat_instance = strategy_class(window=w)
    df_daily_processed = strat_instance.generate_daily_indicators(df_daily)

    try:
        trades = run_backtest(
            df_hourly_raw, df_daily_processed, ticker, 
            take_profit=float(tp / 100.0), stop_loss=float(sl / 100.0), max_hours_to_hold=int(hold_hours)
        )
        closed = [t for t in trades if t["Result"] in ["WIN", "LOSS", "TWIN", "TLOSS"]]
    except Exception:
        return {"coords": (tp, sl, hold_hours), "payload": (0.0, 0, 0.0), "window": w, "status": "SIM_ERROR"}

    if not closed:
        return {"coords": (tp, sl, hold_hours), "payload": (0.0, 0, 0.0), "window": w, "status": "NO_TRADES"}

    df_tr = pd.DataFrame(closed)
    win_rate = float((len(df_tr[df_tr['Result'] == 'WIN']) / len(df_tr)) * 100)
    compounded = float(((df_tr['Return'] + 1).prod() - 1) * 100)
    alpha_calc = float(compounded - spy_bh)

    return {
        "coords": (tp, sl, hold_hours),
        "payload": (alpha_calc, len(df_tr), win_rate, compounded),
        "window": w,
        "status": "SUCCESS"
    }


def dispatch_parallel_grid(shared_pool, tasks, ticker, strategy_name, config_version, phase_label, spy_bh, asset_bh, run_timestamp):
    """Dispatches processing frames to the warmed up shared pool and gathers metrics securely."""
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    cursor = conn.cursor()
    matrix_results = []

    unvisited_tasks = []
    for t in tasks:
        tp, sl, hold_hours, w = t
        cursor.execute("""
            SELECT trades, win_rate, strategy_return, alpha_vs_spy 
            FROM backtest_cache 
            WHERE strategy=? AND version=? AND ticker=? AND window=? AND max_hold_hours=? AND take_profit=? AND stop_loss=?
        """, (strategy_name, config_version, ticker, w, hold_hours, int(tp), int(sl)))
        cached_row = cursor.fetchone()
        
        if cached_row:
            matrix_results.append({
                "Strategy": strategy_name, "Version": config_version, "Ticker": ticker, "Window": w,
                "Take Profit %": int(tp), "Stop Loss %": int(sl), "Max Hold Hours": hold_hours,
                "Trades": cached_row[0], "Win Rate %": cached_row[1], "Return %": cached_row[2],
                "Alpha vs SPY %": cached_row[3], "Asset B&H %": asset_bh, "SPY B&H %": spy_bh
            })
        else:
            unvisited_tasks.append(t)

    if not unvisited_tasks:
        conn.close()
        return pd.DataFrame(matrix_results)

    planned_nodes = [{"take_profit": int(t[0]), "stop_loss": int(t[1]), "max_hold_hours": int(t[2])} for t in unvisited_tasks]
    try:
        with open("active_phase_grid.json", "w") as gf:
            json.dump({"phase": phase_label, "nodes": planned_nodes}, gf)
    except Exception:
        pass

    futures_map = {}
    for task in unvisited_tasks:
        tp, sl, hold_hours, w = task
        args = (ticker, strategy_name, config_version, int(tp), int(sl), hold_hours, w, spy_bh)
        futures_map[shared_pool.submit(run_single_backtest_node_isolated, args)] = task

    logger.info(f"🚀 Processing Grid: {len(unvisited_tasks)} unvisited execution nodes sent to worker pool...")

    progress_bar = tqdm(
        as_completed(futures_map), 
        total=len(futures_map), 
        desc=f"📊 [{ticker}] {phase_label}", 
        unit="node",
        mininterval=15.0,
        maxinterval=30.0
    )
    
    node_counter = 0
    COMMIT_BATCH = 100
    last_postfix_time = 0.0
    for future in progress_bar:
        tp, sl, hold_hours, w = futures_map[future]
        try:
            res = future.result()
            if res.get("status") == "SUCCESS":
                alpha, num_trades, wr, comp_ret = res["payload"]
                now = time.time()
                if now - last_postfix_time >= 2.0:
                    progress_bar.set_postfix({"Alpha Peak": f"{alpha:+.1f}%", "Trades": num_trades})
                    last_postfix_time = now

                node_counter += 1
                if node_counter % 50 == 0:
                    try:
                        with open("current_test.json", "w") as tf:
                            json.dump({"phase": phase_label, "ticker": ticker, "strategy": strategy_name, "version": config_version, "take_profit": int(tp), "stop_loss": int(sl), "max_hold_hours": int(hold_hours)}, tf)
                    except Exception:
                        pass

                matrix_results.append({
                    "Strategy": strategy_name, "Version": config_version, "Ticker": ticker, "Window": w,
                    "Take Profit %": int(tp), "Stop Loss %": int(sl), "Max Hold Hours": hold_hours,
                    "Trades": num_trades, "Win Rate %": wr, "Return %": comp_ret,
                    "Alpha vs SPY %": alpha, "Asset B&H %": asset_bh, "SPY B&H %": spy_bh
                })

                cursor.execute("INSERT OR REPLACE INTO backtest_cache VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (strategy_name, config_version, ticker, w, hold_hours, int(tp), int(sl), num_trades, wr, comp_ret, alpha, asset_bh, spy_bh, run_timestamp))

                if node_counter % COMMIT_BATCH == 0:
                    conn.commit()

        except Exception as e:
            logger.error(f"Worker process crashed evaluating node TP={tp}, SL={sl}: {e}")

    conn.commit()  # flush remaining
    progress_bar.close()
    conn.close()
    return pd.DataFrame(matrix_results)


def run_master_evolutionary_suite(shared_pool, ticker, strategy_class, strategy_name, run_timestamp):
    """Executes multi-generation mesh scans using the clean shared execution thread context."""
    init_idempotent_db()
    
    try:
        with open("config.json", "r") as f: 
            config = json.load(f)
    except Exception:
        logger.error("Configuration file missing. Halting execution pipeline.")
        return None
        
    config_version = config.get("version", "v1.2")
    hp = config["hyperparameters"]
    max_generations = config.get("execution", {}).get("max_generations", 1)
    
    cache_path = CACHE_DIR / f"{ticker}_1h.csv"
    if not cache_path.exists():
        logger.error(f"Critical Ingestion Error: Base cache token not found at {cache_path}")
        return None
        
    df_hourly_raw = pd.read_csv(cache_path, index_col=0, parse_dates=True).sort_index()
    close_col = 'Adj Close' if 'Adj Close' in df_hourly_raw.columns else 'Close'
    asset_bh = ((df_hourly_raw[close_col].iloc[-1] - df_hourly_raw[close_col].iloc[0]) / df_hourly_raw[close_col].iloc[0]) * 100
    
    spy_cache, spy_bh = CACHE_DIR / "SPY_1h.csv", 0.0
    if spy_cache.exists():
        spy_df = pd.read_csv(spy_cache, index_col=0, parse_dates=True).sort_index()
        spy_sliced = spy_df.loc[df_hourly_raw.index.min():df_hourly_raw.index.max()]
        if not spy_sliced.empty:
            spy_col = 'Adj Close' if 'Adj Close' in spy_df.columns else 'Close'
            spy_bh = ((spy_sliced[spy_col].iloc[-1] - spy_sliced[spy_col].iloc[0]) / spy_sliced[spy_col].iloc[0]) * 100

    macro_tasks = []
    for w in hp["windows"]:
        for tp in hp["take_profits"]:
            for sl in hp["stop_losses"]:
                for hold in hp["hold_time_caps"]:
                    macro_tasks.append((int(tp), int(sl), int(hold), int(w)))

    logger.info(f"Generated {len(macro_tasks)} total structural grid nodes for the brute-force sweep.")
    df_global = dispatch_parallel_grid(shared_pool, macro_tasks, ticker, strategy_name, config_version, "Macro Scan", spy_bh, asset_bh, run_timestamp)
    if df_global.empty: 
        logger.warning("Initial structural baseline empty. Skipping optimization sequences.")
        return None
        
    frontier = [{"tp": int(r['Take Profit %']), "sl": int(r['Stop Loss %']), "hold": int(r['Max Hold Hours']), "w": int(r['Window'])} 
                for _, r in df_global.nlargest(2, "Alpha vs SPY %").iterrows()]
    all_fine_data = []
    
    for generation in range(1, max_generations + 1):
        logger.info(f"🧬 Generation {generation}/{max_generations} | Refining Local Grid Boundaries...")
        generation_tasks = []
        
        for agent in frontier:
            fine_tps = [int(agent["tp"] - 2), int(agent["tp"] - 1), int(agent["tp"]), int(agent["tp"] + 1), int(agent["tp"] + 2)]
            fine_sls = [int(agent["sl"] - 2), int(agent["sl"] - 1), int(agent["sl"]), int(agent["sl"] + 1), int(agent["sl"] + 2)]
            
            fine_tps = sorted(list(set([max(1, tp) for tp in fine_tps])))
            fine_sls = sorted(list(set([max(1, sl) for sl in fine_sls])))
            fine_holds = sorted(list(set([max(24, agent["hold"] - 24), agent["hold"], min(120, agent["hold"] + 24)])))
                    
            for tp_mod in fine_tps:
                for sl_mod in fine_sls:
                    for hold_mod in fine_holds:
                        generation_tasks.append((tp_mod, sl_mod, hold_mod, agent["w"]))

        generation_tasks = list(set(generation_tasks))
        
        df_gen_mesh = dispatch_parallel_grid(shared_pool, generation_tasks, ticker, strategy_name, config_version, f"Gen {generation} Mesh", spy_bh, asset_bh, run_timestamp)
        if df_gen_mesh.empty:
            break
        all_fine_data.append(df_gen_mesh)
            
        df_current_universe = pd.concat([df_global] + all_fine_data, ignore_index=True).drop_duplicates(subset=["Max Hold Hours", "Take Profit %", "Stop Loss %"])
        df_current_universe["Safety_Score"] = 0.0
        
        for idx, row in df_current_universe.iterrows():
            tp, sl, h = row["Take Profit %"], row["Stop Loss %"], row["Max Hold Hours"]
            surrounding = df_current_universe[(df_current_universe["Take Profit %"].between(tp - 1, tp + 1)) & 
                                              (df_current_universe["Stop Loss %"].between(sl - 1, sl + 1)) & 
                                              (df_current_universe["Max Hold Hours"].between(h - 24, h + 24))]
            
            worst_neighbor = surrounding["Alpha vs SPY %"].min()
            df_current_universe.at[idx, "Safety_Score"] = worst_neighbor if worst_neighbor <= 0 else surrounding["Alpha vs SPY %"].mean() + 100.0

        top_survivors = df_current_universe.nlargest(2, "Safety_Score")
        current_alpha_peak = df_current_universe["Alpha vs SPY %"].max()
        logger.info(f"📊 Gen {generation} Complete | Frontier Alpha Peak: {current_alpha_peak:+.2f}%")
        
        if top_survivors["Alpha vs SPY %"].max() <= 0.0:
            break
            
        frontier = [{"tp": int(s["Take Profit %"]), "sl": int(s["Stop Loss %"]), "hold": int(s["Max Hold Hours"]), "w": int(s["Window"])} for _, s in top_survivors.iterrows()]

    df_master = df_global
    if all_fine_data:
        valid_dfs = [df for df in all_fine_data if not df.empty]
        if valid_dfs:
            df_master = pd.concat([df_global] + valid_dfs, ignore_index=True).drop_duplicates(
                subset=["Strategy", "Version", "Ticker", "Window", "Max Hold Hours", "Take Profit %", "Stop Loss %"]
            )
    
    if not df_master.empty:
        best_hold = int(df_master.nlargest(1, "Alpha vs SPY %")["Max Hold Hours"].values[0])
        df_plane = df_master[df_master["Max Hold Hours"] == best_hold]
        
        if len(df_plane) > 1:
            try:
                plt.figure(figsize=(12, 10))
                pivot_table = df_plane.pivot(index="Stop Loss %", columns="Take Profit %", values="Alpha vs SPY %")
                sns.heatmap(pivot_table, annot=False, cmap="RdYlGn", cbar_kws={'label': 'Alpha vs SPY %'}, linewidths=0.5)
                plt.title(f"Safety Perimeter Matrix ({ticker} - {strategy_name} @ {best_hold}h)")
                
                output_img = OPTO_LOG_DIR / f"safety_perimeter_{ticker}_{strategy_name}.png"
                plt.savefig(output_img, bbox_inches='tight')
                plt.close()
                logger.info(f"🖼️ Successfully exported final topological map asset layer to: {output_img}")
            except Exception as e:
                logger.warning(f"Topological heatmap file generation bypassed: {e}")

    return df_master


if __name__ == "__main__":
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    run_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    logger.info("============================================")
    logger.info("🖥️  DEPLOYING AUTONOMOUS PARALLEL SEARCH WRAPPER ")
    logger.info("============================================")
    
    try:
        with open("config.json", "r") as f: 
            config = json.load(f)
        filtered_tickers = config.get("target_tickers", [])
        # 🌟 NEW: Pull configuration target lists straight out of workspace JSON fields
        configured_strategies = config.get("active_strategies", ["ZScore_Original"])
    except Exception as conf_err:
        logger.critical(f"Failed to extract dynamic runtime inputs from config.json: {conf_err}")
        filtered_tickers = []
        configured_strategies = ["ZScore_Original"]
        
    strategy_class_references = {
        "ZScore_Original": strategies.ZScoreBreakout,
        "ZScore_TrendFiltered": strategies.TrendFilteredZScore
    }
    
    if filtered_tickers:
        with ProcessPoolExecutor(max_workers=10) as shared_pool:
            for ticker in filtered_tickers:
                for name in configured_strategies:
                    strat_class = strategy_class_references.get(name)
                    if not strat_class:
                        logger.warning(f"Skip request: Strategy key '{name}' lacks structural mapper index.")
                        continue
                        
                    logger.info(f"Target verification clear. Booting processing pipelines for [{ticker}] - [{name}]")
                    run_master_evolutionary_suite(shared_pool, ticker, strat_class, name, run_timestamp)
                        
    for p in ["current_test.json", "active_phase_grid.json"]:
        if os.path.exists(p): 
            try: os.remove(p)
            except Exception: pass
            
    logger.info("=============================================")
    logger.info("🏁 PIPELINE OPERATIONS SUCCESSFULLY CLOSED   ")
    logger.info("=============================================")