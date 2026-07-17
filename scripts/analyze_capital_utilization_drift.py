"""Quantifies how much budgeted capital a trailing-buy order actually deploys,
given the current worst-case sizing formula (signals_helpers.buy_order_sizing:
shares = floor(target_notional / (signal_price * (1 + trail_buy_pct%)))).

Uses scripts/export_trades.simulate_trail_both_annotated (the same read-only
bar-by-bar mirror used for the fill-optimism checklist item) to get real
historical entry fills across every live trailing-buy node's full backtest
history -- far more trades than the live trade_log alone (11 rows total,
most without a recorded trail_buy_pct at fill time).

For each historical trade:
    worst_case_trigger = signal_price * (1 + trail_buy_pct)   -- what shares were sized off
    actual_fill        = running_low * (1 + trail_buy_pct) at whichever bar triggered
                          (always <= worst_case_trigger, since running_low only falls)
    utilization_pct     = actual_fill / worst_case_trigger * 100
    idle_capital_pct    = 100 - utilization_pct

Also reports the separate integer-rounding contributor (whole-share truncation)
and, for the "leave a buffer" idea, what a no-padding (raw signal_price)
sizing formula's overspend distribution looks like -- i.e. how much buffer
cash would need to be reserved to cover the worst historical overspend if
sizing were made more aggressive instead of worst-case.

Usage: .venv/bin/python scripts/analyze_capital_utilization_drift.py
"""
import sys
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import strategies
from backtester import prep_inputs
from export_trades import simulate_trail_both_annotated, load_hourly

LIVE_DIR = Path(__file__).resolve().parent.parent / "cache" / "live"


def get_live_trailing_buy_nodes():
    conn = sqlite3.connect(LIVE_DIR / "trading_live.db")
    conn.row_factory = sqlite3.Row
    wl_id = conn.execute("SELECT id FROM watchlists WHERE is_active=1").fetchone()[0]
    rows = conn.execute(
        "SELECT ticker, window, arm_sell_pct, trail_buy_pct, trail_sell_pct, fixed_sl, "
        "max_hold_hours, z_score_threshold, starting_notional, account FROM watch_list "
        "WHERE watchlist_id=? AND mode='live' AND strategy='TrailingBothZScoreBreakout' "
        "ORDER BY ticker", (wl_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def analyze_ticker(node):
    ticker = node['ticker']
    df_h = load_hourly(ticker)
    df_daily = df_h.resample("D").last().dropna(subset=["Close"])

    strat = strategies.TrailingBothZScoreBreakout(window=node['window'],
                                                    z_score_threshold=node['z_score_threshold'])
    ind = strat.generate_daily_indicators(df_daily)
    p = prep_inputs(df_h, ind)

    take_profit = node['arm_sell_pct'] / 100.0
    stop_loss = node['fixed_sl'] / 100.0
    trail_buy_pct = node['trail_buy_pct'] / 100.0
    trail_pct = node['trail_sell_pct'] / 100.0
    z_thresh = node['z_score_threshold']

    trades = simulate_trail_both_annotated(
        p, take_profit, stop_loss, node['max_hold_hours'],
        trail_buy_pct, trail_pct, 9, 14, z_thresh,
    )
    prices = p['prices']
    target_notional = node['starting_notional']

    rows = []
    for t in trades:
        signal_price = float(prices[t['signal_i']])
        worst_case_trigger = signal_price * (1 + trail_buy_pct)
        actual_fill = t['entry_p']  # running_low*(1+trail_buy_pct) at the bar it triggered
        utilization_pct = actual_fill / worst_case_trigger * 100
        shares = int(target_notional // worst_case_trigger)
        deployed = shares * actual_fill
        rounding_idle = target_notional - shares * worst_case_trigger  # $ left over even at worst-case price
        total_idle = target_notional - deployed
        rows.append({
            'signal_price': signal_price, 'worst_case_trigger': worst_case_trigger,
            'actual_fill': actual_fill, 'utilization_pct': utilization_pct,
            'shares': shares, 'deployed': deployed,
            'rounding_idle_$': rounding_idle, 'total_idle_$': total_idle,
            'total_idle_pct': total_idle / target_notional * 100,
        })

    full_df = pd.DataFrame(rows)
    zero_fill = int((full_df['shares'] == 0).sum())

    # Buffer-cash question: if sizing were done off raw signal_price (no worst-case
    # padding at all -- the pre-2026-07-17 formula), how far over target_notional
    # would the worst historical trade have gone? That overspend is the buffer a
    # more-aggressive sizing scheme would need to keep in reserve. Computed from the
    # full (unfiltered) trade list, row-aligned with `trades` -- must not use the
    # shares>0-filtered df below, which would silently misalign trades vs. prices.
    overspend_rows = []
    for t, sp in zip(trades, full_df['signal_price']):
        raw_sh = int(target_notional // sp)
        actual_cost = raw_sh * t['entry_p']
        overspend_rows.append(actual_cost - target_notional)
    overspend = pd.Series(overspend_rows)

    # Trades where worst_case_trigger alone exceeds target_notional (shares=0) are a
    # different problem (starting_notional too small for the share price at that point
    # in history, e.g. GDXD traded at $1000s/share before its 2024-2026 reverse splits)
    # -- not sizing-formula drift. Excluded from the utilization stats below, reported
    # separately, so they don't drown out the real per-trade drift signal.
    df = full_df[full_df['shares'] > 0].reset_index(drop=True)

    return ticker, target_notional, df, overspend, zero_fill


def main():
    nodes = get_live_trailing_buy_nodes()
    summary = []
    print(f"{'Ticker':<6} {'trades':>6} {'target$':>9}  {'util% mean':>10} {'util% p10':>10} {'util% p50':>10} "
          f"{'idle$ mean':>10} {'idle$ p90':>10}  {'round$ mean':>11}  {'overspend$ max (no-pad)':>24}  {'zero-fill':>9}")
    for node in nodes:
        try:
            ticker, target_notional, df, overspend, zero_fill = analyze_ticker(node)
        except Exception as e:
            print(f"{node['ticker']:<6}  ERROR: {e}")
            continue
        if df.empty:
            print(f"{node['ticker']:<6}  no affordable trades")
            continue
        summary.append((ticker, target_notional, df, overspend))
        print(f"{ticker:<6} {len(df):>6} {target_notional:>9,.0f}  "
              f"{df['utilization_pct'].mean():>9.1f}% {df['utilization_pct'].quantile(.10):>9.1f}% "
              f"{df['utilization_pct'].median():>9.1f}%  "
              f"{df['total_idle_$'].mean():>9,.0f}  {df['total_idle_$'].quantile(.90):>9,.0f}  "
              f"{df['rounding_idle_$'].mean():>10,.0f}  {overspend.max():>23,.0f}  {zero_fill:>9}")

    print()
    print("Watchlist-wide (pooled across all trades, all tickers):")
    all_df = pd.concat([s[2] for s in summary], ignore_index=True)
    all_overspend = pd.concat([s[3] for s in summary], ignore_index=True)
    print(f"  mean utilization: {all_df['utilization_pct'].mean():.1f}%   "
          f"median: {all_df['utilization_pct'].median():.1f}%   "
          f"p10: {all_df['utilization_pct'].quantile(.10):.1f}%")
    print(f"  mean idle $ per trade: ${all_df['total_idle_$'].mean():,.0f}   "
          f"of which rounding-only: ${all_df['rounding_idle_$'].mean():,.0f}")
    print(f"  worst-case overspend if sized off raw signal_price (no padding): "
          f"max ${all_overspend.max():,.0f}, p99 ${all_overspend.quantile(.99):,.0f}, "
          f"mean ${all_overspend.mean():,.0f}")


if __name__ == '__main__':
    main()
