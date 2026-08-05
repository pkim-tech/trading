"""Applies the validated 15%-OTM put hedge (see docs/research_log.md's 2026-08-06 put-hedge
entries -- real ATM IV, BS pricing, hard floor at the strike distance) to
sim_bear_market_stress.py's real synthetic crash windows (2008 GFC, 2020 COVID, 2022 bear,
2000 dotcom), so the hedge can be judged against genuine historical crash severity instead
of the calm 2023-2026 real backtest window (which never contained a real crash).

Per-trade hedge P&L: BS_put_price(entry_price, strike=entry_price*0.85, T=20d, iv) at entry,
minus BS_put_price(exit_price, strike, T=max(20-held_days,0)/365, iv) at exit -- single-shot
valuation (not a day-by-day walk, since these are daily-bar trades with discrete entry/exit
rows already) using the same real ATM IV pulled from options_snapshot as the live analysis.
Hedge applied to every trade in a crash's DECLINE phase only (the actual risk period) --
recovery-phase trades are left unhedged, matching the "hedge only when it's actually needed"
framing already established.

Usage: .venv/bin/python scripts/sim_bear_market_stress_hedged.py [--tickers SOXL AGQ KORU]
"""
import argparse
import sqlite3
import sys

import numpy as np
import pandas as pd

from sim_bear_market_stress import CRASHES, PROXIES, fetch_underlying, get_node_params, run_strategy_daily, synthesize_leveraged
sys.path.insert(0, "..")
from scripts.put_decay_forecast import bs_put_price, DB_PATH

OTM_PCT = 0.15
ROLL_T_DAYS = 20


def get_real_iv(ticker):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    rows = c.execute("""SELECT underlying_price, strike, implied_volatility FROM options_snapshot
                         WHERE ticker=? AND snapshot_ts=(SELECT MAX(snapshot_ts) FROM options_snapshot WHERE ticker=?)
                         AND implied_volatility IS NOT NULL AND implied_volatility>0
                         ORDER BY expiration LIMIT 500""", (ticker, ticker)).fetchall()
    if not rows:
        return None
    px = rows[0][0]
    atm = min(rows, key=lambda r: abs(r[1] - px))
    return atm[2]


def hedge_pnl(entry_price, exit_price, held_days, iv):
    strike = entry_price * (1 - OTM_PCT)
    v_entry = bs_put_price(entry_price, strike, ROLL_T_DAYS / 365.0, iv)
    t_exit = max(ROLL_T_DAYS - held_days, 0) / 365.0
    v_exit = bs_put_price(exit_price, strike, t_exit, iv)
    return (v_exit - v_entry) / entry_price


def compounded_and_dd(rets):
    if not rets:
        return 0.0, 0.0
    eq = np.cumprod([1 + r for r in rets])
    peak = np.maximum.accumulate(eq)
    dd = ((eq - peak) / peak).min()
    return eq[-1] - 1.0, dd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*", default=["SOXL", "AGQ", "KORU"])
    args = ap.parse_args()

    underlying_cache = {}
    rows = []
    for ticker in args.tickers:
        if ticker not in PROXIES:
            print(f"{ticker}: no proxy mapping, skipping", file=sys.stderr)
            continue
        proxy, leverage, note = PROXIES[ticker]
        params = get_node_params(ticker)
        if params is None:
            print(f"{ticker}: no watch_list node found, skipping", file=sys.stderr)
            continue
        iv = get_real_iv(ticker)
        if iv is None:
            print(f"{ticker}: no real options IV found, skipping", file=sys.stderr)
            continue

        if proxy not in underlying_cache:
            underlying_cache[proxy] = fetch_underlying(proxy)
        synth = synthesize_leveraged(underlying_cache[proxy], leverage)

        for crash, (start, bottom, recov_end) in CRASHES.items():
            start_ts, bottom_ts = pd.Timestamp(start), pd.Timestamp(bottom)
            window = synth.loc[start:recov_end]
            if len(window) < params["window"] + 5:
                continue
            lookback_start = synth.index.searchsorted(start_ts) - params["window"] - 1
            sim_bars = synth.iloc[max(0, lookback_start):synth.index.searchsorted(pd.Timestamp(recov_end)) + 1]
            all_trades = run_strategy_daily(sim_bars, params)
            win_start_i = sim_bars.index.searchsorted(start_ts)
            bottom_i = sim_bars.index.searchsorted(bottom_ts)
            decline_trades = [t for t in all_trades if win_start_i <= t["entry_day"] < bottom_i]
            if not decline_trades:
                continue

            unhedged_rets = [t["return"] for t in decline_trades]
            hedged_rets = []
            for t in decline_trades:
                pnl = hedge_pnl(t["entry_price"], t["exit_price"], t["held_days"], iv)
                hedged_rets.append(t["return"] + pnl)

            u_comp, u_dd = compounded_and_dd(unhedged_rets)
            h_comp, h_dd = compounded_and_dd(hedged_rets)
            rows.append({
                "ticker": ticker, "crash": crash, "trades": len(decline_trades),
                "unhedged_compounded_pct": u_comp * 100, "unhedged_max_dd_pct": u_dd * 100,
                "hedged_compounded_pct": h_comp * 100, "hedged_max_dd_pct": h_dd * 100,
            })

    df = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    print(df.round(1).to_string(index=False))
    df.to_csv("output/bear_market_stress_hedged.csv", index=False)
    print("\nWrote output/bear_market_stress_hedged.csv")


if __name__ == "__main__":
    main()
