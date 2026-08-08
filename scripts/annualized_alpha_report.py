#!/usr/bin/env python3
"""
Annualizes each ticker's selected node alpha so cross-ticker comparison is
fair regardless of how much cached history exists per ticker -- found
2026-08-08 that raw alpha_vs_spy numbers span wildly different real
calendar windows (SOXL/AGQ/DPST ~3.0y of cached hourly data, UGL/JNUG
~1.1y, SPCL only ~0.31y), since asset_bh/spy_bh (run_optimization_sweep.
compute_bh_returns) are computed over each ticker's own full cached-data
date range, not a shared window. A raw "+184.7% alpha" over 4 months and
a raw "+1212.1% alpha" over 3 years were never actually comparable numbers.

Deliberately does NOT touch ROBUST_ALPHA_SQL, backtest_cache's schema, or
the real selection convention (robust_alpha = MIN(possible,pessimistic,
certain) stays the selection criterion) -- this is purely a reporting-
layer annualization, using the same calendar window already used for
asset_bh/spy_bh, computed fresh from the cached CSV's own date range
(compute_bh_returns doesn't persist the day count, only the % returns).

User's explicit call (2026-08-08): annualize over the FULL calendar span of
cached data, not the strategy's actual invested-only time -- cheaper (no
trade-list replay needed) and directly fixes the horizon-mismatch problem;
does not account for a strategy only being in the market some of that time.

Usage:
    .venv/bin/python scripts/annualized_alpha_report.py --tickers SOXL SPCL AGQ UGL JNUG
"""
import argparse
import json
import sqlite3
import time
from pathlib import Path

import pandas as pd

from top_safe_nodes import best_safe_node

DB_PATH = Path("./cache/research/trading_universe.db")
CACHE_DIR = Path("./cache/research")


def calendar_days(ticker):
    """Real calendar day span of this ticker's cached hourly data -- the same
    window compute_bh_returns uses for asset_bh/spy_bh, just not persisted
    anywhere, so recomputed fresh from the CSV."""
    path = CACHE_DIR / f"{ticker}_1h.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    return (df.index.max() - df.index.min()).days


def cagr(total_return_pct, days):
    """Annualized compounded rate from a total-return percentage over `days`
    calendar days. Returns None if days<=0 (can't annualize a zero-span window)."""
    if days is None or days <= 0:
        return None
    years = days / 365.25
    return ((1.0 + total_return_pct / 100.0) ** (1.0 / years) - 1.0) * 100.0


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
                   strategy_return, asset_bh, spy_bh, trades, win_rate
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
        node = best_safe_node(df_t, min_alpha=args.min_alpha, metric="robust_alpha")
        if node is None:
            print(f"{ticker}: no cliff-safe node found")
            continue

        days = calendar_days(ticker)
        strat_cagr = cagr(node['return'], days)
        spy_row = df_t.iloc[0]
        spy_cagr = cagr(spy_row['spy_bh'], days)
        annualized_excess = (strat_cagr - spy_cagr) if (strat_cagr is not None and spy_cagr is not None) else None

        rows.append({
            'ticker': ticker, 'days': days, 'years': round(days / 365.25, 2) if days else None,
            'trades': node['trades'], 'raw_return': node['return'], 'raw_alpha': node['alpha'],
            'spy_bh_full_window': spy_row['spy_bh'],
            'strategy_cagr': strat_cagr, 'spy_cagr': spy_cagr, 'annualized_excess': annualized_excess,
        })

    if not rows:
        return

    df = pd.DataFrame(rows).sort_values('annualized_excess', ascending=False)
    for col in ('raw_return', 'raw_alpha', 'spy_bh_full_window', 'strategy_cagr', 'spy_cagr', 'annualized_excess'):
        df[col] = df[col].map(lambda v: f"{v:+.1f}%" if pd.notna(v) else "-")
    pd.set_option('display.width', 160)
    print(df.to_string(index=False))
    print(f"\n{time.time()-t0:.2f}s")


if __name__ == "__main__":
    main()
