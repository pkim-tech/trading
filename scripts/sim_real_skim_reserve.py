"""Applies the skim-and-redeploy overlay (see sim_skim_redeploy.py) to a ticker's REAL
backtested trade history over our real cached hourly data (not the synthetic crash
reconstruction) -- answers "if I'd been skimming into SPY since real data started, how
big would the reserve be today?"

Usage: .venv/bin/python scripts/sim_real_skim_reserve.py TICKER [--watchlist-id 65]
       [--skim-step 0.10] [--skim-frac 0.20] [--redeploy-step 0.15] [--redeploy-frac 0.25]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtester import prep_inputs
from export_trades import simulate_trail_both_annotated
from sim_skim_redeploy import skim_redeploy_overlay
import strategies

CACHE_DIR = Path("cache/research")
LIVE_DB = Path("cache/live/trading_live.db")


def load_hourly(ticker):
    df = pd.read_csv(CACHE_DIR / f"{ticker}_1h.csv", index_col=0, parse_dates=True)
    close_col = "Adj Close" if "Adj Close" in df.columns else "Close"
    if close_col != "Close":
        df["Close"] = df[close_col]
    return df


def get_node(ticker, watchlist_id):
    conn = sqlite3.connect(LIVE_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """SELECT window, arm_sell_pct, trail_buy_pct, trail_sell_pct, fixed_sl,
                  max_hold_hours, z_score_threshold, starting_notional, mode
           FROM watch_list WHERE ticker=? AND watchlist_id=?
           ORDER BY (mode='live') DESC LIMIT 1""",
        (ticker, watchlist_id),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def build_real_trades(ticker, node):
    df_h = load_hourly(ticker)
    df_daily = df_h.resample("D").last().dropna(subset=["Close"])
    strat = strategies.TrailingBothZScoreBreakout(
        window=node["window"], z_score_threshold=node["z_score_threshold"])
    ind = strat.generate_daily_indicators(df_daily)
    p = prep_inputs(df_h, ind)

    take_profit = (node["arm_sell_pct"] or 0.0) / 100.0
    stop_loss = node["fixed_sl"] / 100.0
    trail_buy_pct = node["trail_buy_pct"] / 100.0
    trail_pct = node["trail_sell_pct"] / 100.0

    trades = simulate_trail_both_annotated(
        p, take_profit, stop_loss, node["max_hold_hours"],
        trail_buy_pct, trail_pct, 9, 14, node["z_score_threshold"],
    )
    timestamps = p["timestamps"]
    return trades, timestamps


def daily_equity_from_trades(trades, timestamps):
    """Compound realized trade returns onto a real calendar-day index; flat between
    trades (cash), matching how the real strategy sits idle between signal windows."""
    days = pd.DatetimeIndex(pd.Series(timestamps).dt.normalize().unique()).sort_values()
    equity = pd.Series(1.0, index=days)
    level = 1.0
    for t in sorted(trades, key=lambda x: x["exit_i"]):
        exit_day = pd.Timestamp(timestamps[t["exit_i"]]).normalize()
        level *= (1.0 + t["ret"])
        equity.loc[equity.index >= exit_day] = level
    return equity


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--watchlist-id", type=int, default=65)
    ap.add_argument("--reserve-ticker", default="SPY", help="vehicle to skim profits into")
    ap.add_argument("--skim-step", type=float, default=0.10)
    ap.add_argument("--skim-frac", type=float, default=0.20)
    ap.add_argument("--redeploy-step", type=float, default=0.15)
    ap.add_argument("--redeploy-frac", type=float, default=0.25)
    args = ap.parse_args()

    node = get_node(args.ticker, args.watchlist_id)
    if node is None:
        raise SystemExit(f"no watch_list node for {args.ticker} on watchlist {args.watchlist_id}")
    print(f"Node params: {node}")

    trades, timestamps = build_real_trades(args.ticker, node)
    print(f"Real trades: {len(trades)}, data range "
          f"{pd.Timestamp(timestamps[0]).date()} -> {pd.Timestamp(timestamps[-1]).date()}")

    strat_equity = daily_equity_from_trades(trades, timestamps)

    reserve_h = load_hourly(args.reserve_ticker)
    reserve_daily = reserve_h["Close"].resample("D").last().dropna()
    reserve_equity = reserve_daily.reindex(strat_equity.index, method="ffill").bfill()
    reserve_equity = reserve_equity / reserve_equity.iloc[0]

    overlay_curve, reserve_curve = skim_redeploy_overlay(
        strat_equity.to_numpy(), reserve_equity.to_numpy(),
        args.skim_step, args.skim_frac, args.redeploy_step, args.redeploy_frac)

    baseline_final = strat_equity.iloc[-1] - 1.0
    overlay_final = overlay_curve[-1] - 1.0
    current_reserve_frac = reserve_curve[-1]
    max_reserve_frac = float(np.max(reserve_curve))

    print(f"\nBaseline (100% in strategy) return since {strat_equity.index[0].date()}: {baseline_final:+.1%}")
    print(f"Overlay (skim/redeploy into {args.reserve_ticker}) return: {overlay_final:+.1%}")
    print(f"Current {args.reserve_ticker} reserve fraction of account: {current_reserve_frac:.1%}")
    print(f"Peak {args.reserve_ticker} reserve fraction reached: {max_reserve_frac:.1%}")
    if node["starting_notional"]:
        total_value = node["starting_notional"] * (1.0 + overlay_final)
        print(f"\nIf started with ${node['starting_notional']:,.0f}: "
              f"total account value now ${total_value:,.0f}, "
              f"of which ${total_value * current_reserve_frac:,.0f} sitting in {args.reserve_ticker} reserve")


if __name__ == "__main__":
    main()
