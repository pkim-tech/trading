#!/usr/bin/env python3
"""
For each ticker, finds three candidate nodes -- best CLIFF-SAFE (current
selection convention: robust_alpha, MIN of possible/pessimistic/certain,
required to pass the neighbor-safety check), best NON-CLIFF-SAFE (top
robust_alpha row regardless of whether it clears the neighbor check), and
best POSSIBLE (top raw alpha_vs_spy row, ignoring robustness entirely) --
then runs the real 5-min-bar fill-accuracy replay (verify_fill_resolution_
accuracy.py) against each one's actual entry params, so node selection can
be informed by which resolution is empirically closest to real fills, not
just by the backtested alpha numbers.

Built 2026-08-08 (later) per the user's framing: rather than switching the
whole project to rank by "possible" (empirically the most accurate single
resolution, see docs/research_log.md's fill-accuracy entries), run the 5-min
check as part of candidate review itself, across all three candidate types,
and let the numbers -- not a blanket rule -- inform the final pick.

Usage:
    .venv/bin/python scripts/candidate_5min_report.py --tickers AGQ SOXL DPST
    .venv/bin/python scripts/candidate_5min_report.py --tickers AGQ --version v5
"""
import argparse
import json
import sqlite3
import time
from pathlib import Path

import pandas as pd
import yfinance as yf

from top_safe_nodes import best_safe_node
from verify_fill_resolution_accuracy import fill_accuracy_for_node, print_accuracy_summary

DB_PATH = Path("./cache/research/trading_universe.db")


def _node_from_row(row):
    return {
        'window': int(row['window']), 'z': row['z_score_threshold'],
        'trail_buy_pct': row['trail_buy_pct'], 'trail_sell_pct': row['trail_sell_pct'],
        'entry_timing': row['entry_timing'], 'arm_pct': row['take_profit'],
        'sl': int(row['stop_loss']), 'hold': int(row['max_hold_hours']),
        'trades': int(row['trades']),
        'alpha_raw': row['alpha_vs_spy'], 'alpha_pessimistic': row['alpha_vs_spy_pessimistic'],
        'alpha_certain': row['alpha_vs_spy_certain'], 'robust_alpha': row['robust_alpha'],
    }


def _node_key(n):
    return (n['window'], n['z'], n['trail_buy_pct'], n['trail_sell_pct'],
            n['entry_timing'], n['arm_pct'], n['sl'], n['hold'])


def find_candidates(df_t, min_alpha):
    """Returns {label: node_dict} for the three candidate types, deduped by
    identical params (a label is dropped if it's identical to one already found)."""
    candidates = {}

    cliff_safe = best_safe_node(df_t, min_alpha=min_alpha, metric="robust_alpha")
    if cliff_safe:
        cliff_safe['robust_alpha'] = cliff_safe.pop('alpha')  # normalize to match _node_from_row's key
        candidates['cliff-safe (current convention)'] = cliff_safe

    top_robust_row = df_t.sort_values("robust_alpha", ascending=False).iloc[0]
    top_robust = _node_from_row(top_robust_row)
    if not cliff_safe or _node_key(top_robust) != _node_key(cliff_safe):
        candidates['best robust_alpha (ignoring cliff-safety)'] = top_robust

    top_possible_row = df_t.sort_values("alpha_vs_spy", ascending=False).iloc[0]
    top_possible = _node_from_row(top_possible_row)
    existing_keys = {_node_key(n) for n in candidates.values()}
    if _node_key(top_possible) not in existing_keys:
        candidates['best possible (raw alpha_vs_spy)'] = top_possible

    return candidates


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="+", required=True)
    parser.add_argument("--version", default=None)
    parser.add_argument("--strategy", default=None)
    parser.add_argument("--min-alpha", type=float, default=200)
    args = parser.parse_args()

    with open("config.json") as f:
        config = json.load(f)
    version = args.version or config.get("version", "v1.8")
    strategy = args.strategy or config.get("active_strategies", ["ZScoreBreakout"])[0]

    print(f"Version: {version}  Strategy: {strategy}\n")

    t0 = time.time()
    placeholders = ",".join("?" * len(args.tickers))
    with sqlite3.connect(DB_PATH) as conn:
        df_all = pd.read_sql(f"""
            SELECT ticker, COALESCE(take_profit, arm_sell_pct) AS take_profit,
                   stop_loss, max_hold_hours, window,
                   z_score_threshold, trail_buy_pct, trail_sell_pct, entry_timing,
                   alpha_vs_spy, alpha_vs_spy_pessimistic, alpha_vs_spy_certain,
                   strategy_return, trades, win_rate
            FROM backtest_cache
            WHERE version=? AND strategy=? AND ticker IN ({placeholders}) AND trades > 0
        """, conn, params=(version, strategy, *args.tickers))

    pess = df_all["alpha_vs_spy_pessimistic"].fillna(df_all["alpha_vs_spy"])
    cert = df_all["alpha_vs_spy_certain"].fillna(df_all["alpha_vs_spy"])
    df_all["robust_alpha"] = pd.concat([df_all["alpha_vs_spy"], pess, cert], axis=1).min(axis=1)

    for ticker in args.tickers:
        df_t = df_all[df_all["ticker"] == ticker]
        if df_t.empty:
            print(f"{ticker}: no data\n")
            continue

        candidates = find_candidates(df_t, args.min_alpha)
        if not candidates:
            print(f"{ticker}: no candidates found\n")
            continue

        print(f"=== {ticker} ===")
        summary_rows = []
        for label, node in candidates.items():
            summary_rows.append({
                'candidate': label, 'window': node['window'], 'z': node['z'],
                'trail_buy_pct': node['trail_buy_pct'], 'arm': node['arm_pct'],
                'sl': node['sl'], 'hold': node['hold'], 'trades': node['trades'],
                'possible': f"{node['alpha_raw']:+.1f}%",
                'pessimistic': f"{node['alpha_pessimistic']:+.1f}%" if pd.notna(node['alpha_pessimistic']) else "-",
                'certain': f"{node['alpha_certain']:+.1f}%" if pd.notna(node['alpha_certain']) else "-",
                'robust_alpha': f"{node['robust_alpha']:+.1f}%",
            })
        pd.set_option('display.width', 160)
        print(pd.DataFrame(summary_rows).to_string(index=False))

        df_5m = yf.download(ticker, period="60d", interval="5m", multi_level_index=False, progress=False)
        df_5m.index = pd.to_datetime(df_5m.index).tz_localize(None)

        for label, node in candidates.items():
            print(f"\n--- 5-min fill-accuracy replay: {label} "
                  f"(window={node['window']}, z={node['z']}, trail_buy_pct={node['trail_buy_pct']}, "
                  f"hold={node['hold']}) ---")
            acc = fill_accuracy_for_node(ticker, node['window'], node['z'], node['trail_buy_pct'],
                                          node['hold'], df_5m=df_5m)
            if acc.empty:
                print(f"  no resolved signals in the last 58d")
                continue
            print_accuracy_summary(acc)
        print()

    print(f"{time.time()-t0:.2f}s")


if __name__ == "__main__":
    main()
