#!/usr/bin/env python3
"""
Find the highest-alpha cliff-safe node per ticker for a given version.

Usage:
    python scripts/top_safe_nodes.py --tickers UVIX QLD YINN TMV
    python scripts/top_safe_nodes.py --tickers UVIX QLD YINN TMV --version v1.8
"""
import argparse
import sqlite3
import json
import time
import pandas as pd
from pathlib import Path

DB_PATH      = Path("./cache/trading_universe.db")
CLIFF_RADIUS = 3


def best_safe_node(df_ticker):
    df = df_ticker.sort_values("alpha_vs_spy", ascending=False)
    candidates = df[df["alpha_vs_spy"] >= 200]
    for i, (_, row) in enumerate(candidates.iterrows()):
        mask = (
            (df["window"] == row["window"]) &
            (df["z_score_threshold"] == row["z_score_threshold"]) &
            (df["take_profit"].between(row["take_profit"] - CLIFF_RADIUS, row["take_profit"] + CLIFF_RADIUS)) &
            (df["stop_loss"].between(row["stop_loss"] - CLIFF_RADIUS, row["stop_loss"] + CLIFF_RADIUS)) &
            (df["max_hold_hours"].between(row["max_hold_hours"] - 7, row["max_hold_hours"] + 7))
        )
        worst = df.loc[mask, "alpha_vs_spy"].min()
        if worst >= 0:
            print(f"    found safe node at rank #{i+1}")
            return {
                'ticker': row["ticker"], 'tp': int(row["take_profit"]), 'sl': int(row["stop_loss"]),
                'hold': int(row["max_hold_hours"]), 'window': int(row["window"]),
                'z': row["z_score_threshold"], 'alpha': row["alpha_vs_spy"],
                'return': row["strategy_return"], 'trades': int(row["trades"]),
                'win_rate': row["win_rate"], 'worst_neighbor': worst
            }
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="+", required=True)
    parser.add_argument("--version", default=None)
    parser.add_argument("--strategy", default=None)
    args = parser.parse_args()

    with open("config.json") as f:
        config = json.load(f)

    version  = args.version or config.get("version", "v1.8")
    strategy = args.strategy or config.get("active_strategies", ["ZScoreBreakout"])[0]

    print(f"Version: {version}  Strategy: {strategy}")

    t0 = time.time()
    placeholders = ",".join("?" * len(args.tickers))
    with sqlite3.connect(DB_PATH) as conn:
        df_all = pd.read_sql(f"""
            SELECT ticker, take_profit, stop_loss, max_hold_hours, window,
                   z_score_threshold, alpha_vs_spy, strategy_return, trades, win_rate
            FROM backtest_cache
            WHERE version=? AND strategy=? AND ticker IN ({placeholders}) AND trades > 0
        """, conn, params=(version, strategy, *args.tickers))
    print(f"  DB load: {len(df_all):,} rows in {time.time()-t0:.2f}s\n")

    results = []
    for ticker in args.tickers:
        t1 = time.time()
        df_t = df_all[df_all["ticker"] == ticker]
        if df_t.empty:
            print(f"  {ticker}: no data")
            continue
        n_candidates = (df_t["alpha_vs_spy"] >= 200).sum()
        print(f"  {ticker}: {len(df_t):,} nodes, {n_candidates} above 200% alpha — cliff-checking...")
        node = best_safe_node(df_t)
        print(f"  {ticker}: done in {time.time()-t1:.2f}s")
        if node:
            results.append(node)
        else:
            print(f"  {ticker}: no safe node found")

    if not results:
        return

    print(f"\nTotal: {time.time()-t0:.2f}s\n")

    df = pd.DataFrame(results).sort_values("alpha", ascending=False)
    df["win_rate"] = df["win_rate"].map("{:.1f}%".format)
    df["alpha"]    = df["alpha"].map("{:+.1f}%".format)
    df["return"]   = df["return"].map("{:+.1f}%".format)
    df["worst_neighbor"] = df["worst_neighbor"].map("{:+.1f}%".format)

    print(df[["ticker","alpha","return","trades","win_rate","tp","sl","hold","window","z","worst_neighbor"]]
          .to_string(index=False))


if __name__ == "__main__":
    main()
