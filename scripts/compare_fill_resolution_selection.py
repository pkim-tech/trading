#!/usr/bin/env python3
"""
Compares node selection under two metrics: robust_alpha (MIN of possible/
pessimistic/certain, the project's standing convention) vs alpha_vs_spy alone
("possible" only, empirically the most accurate single resolution per
verify_fill_resolution_accuracy.py). Reuses top_safe_nodes.best_safe_node()
for both selections instead of re-deriving the cliff-safety logic.

Built 2026-08-08 after checking SOXL by hand (inline python, not committed)
found that node's "possible" alpha (+1212.1%) was actually the LOWEST of the
three -- pessimistic +1415.0%, certain +8871.5% -- opposite of what the fill-
accuracy check's per-signal result would suggest. Real point: per-signal fill
accuracy (which resolution's PRICE is closest to a real fill) and per-node
aggregate return (which resolution's cumulative RETURN is highest) are
different questions -- a resolution can be the more accurate one on most
individual fills and still not be the one driving the highest node-level
number, since MIN() is dominated by whichever resolution is worst overall,
not by average per-fill error.

Usage:
    .venv/bin/python scripts/compare_fill_resolution_selection.py --tickers SOXL AGQ DPST
    .venv/bin/python scripts/compare_fill_resolution_selection.py --tickers SOXL --version v5
"""
import argparse
import json
import sqlite3
import time
from pathlib import Path

import pandas as pd

from top_safe_nodes import best_safe_node

DB_PATH = Path("./cache/research/trading_universe.db")


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

    rows = []
    for ticker in args.tickers:
        df_t = df_all[df_all["ticker"] == ticker]
        if df_t.empty:
            print(f"{ticker}: no data")
            continue

        robust_node = best_safe_node(df_t, min_alpha=args.min_alpha, metric="robust_alpha")
        possible_node = best_safe_node(df_t, min_alpha=args.min_alpha, metric="alpha_vs_spy")

        if robust_node is None and possible_node is None:
            print(f"{ticker}: no safe node under either metric")
            continue

        def _key(n):
            return (n['window'], n['z'], n['trail_buy_pct'], n['trail_sell_pct'],
                    n['entry_timing'], n['arm_pct'], n['sl'], n['hold']) if n else None

        same_node = _key(robust_node) == _key(possible_node)

        for label, node in (("robust_alpha (current convention)", robust_node),
                             ("alpha_vs_spy / possible-only", possible_node)):
            if node is None:
                rows.append({'ticker': ticker, 'selected_by': label, 'possible': None,
                              'pessimistic': None, 'certain': None, 'robust_alpha': None,
                              'trades': None, 'same_node': None})
                continue
            rows.append({
                'ticker': ticker, 'selected_by': label,
                'possible': node['alpha_raw'], 'pessimistic': node['alpha_pessimistic'],
                'certain': node['alpha_certain'], 'robust_alpha': node['alpha'],
                'trades': node['trades'], 'same_node': same_node,
            })

    if not rows:
        return

    df = pd.DataFrame(rows)
    for col in ('possible', 'pessimistic', 'certain', 'robust_alpha'):
        df[col] = df[col].map(lambda v: f"{v:+.1f}%" if pd.notna(v) else "-")
    pd.set_option('display.width', 160)
    print(df.to_string(index=False))
    print(f"\n{time.time()-t0:.2f}s")


if __name__ == "__main__":
    main()
