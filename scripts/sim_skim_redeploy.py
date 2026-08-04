"""Skim-and-redeploy overlay test: does periodically taking strategy profits into SPY,
then feeding that reserve back in as the strategy draws down, beat just staying 100%
in the strategy through a crash+recovery cycle?

Reuses the daily-bar synthetic leveraged-ETF reconstruction and strategy simulation
from sim_bear_market_stress.py -- same caveats apply (daily-bar approximation, not the
live hourly kernel; see that script's docstring).

Rule (mirrors the "skim on the way up, redeploy little by little on the way down" idea):
  - Track the strategy's own equity curve (flat/cash between trades, compounding only
    on realized closed-trade returns -- matches how capital actually sits idle between
    signal windows live).
  - SKIM: whenever strategy equity makes a new high that's >= skim_step above the level
    at the last skim (or start), move skim_fraction of the strategy allocation into a
    SPY reserve.
  - REDEPLOY: whenever strategy equity drops >= redeploy_step below the level at the
    last redeploy (starting from the running peak), move redeploy_fraction of the
    current SPY reserve back into the strategy. Each trigger re-arms at the new lower
    level, so a deep drawdown fires the ladder multiple times ("little by little").
  - Compared against a 100%-always-in-strategy baseline over the same window.

Usage: .venv/bin/python scripts/sim_skim_redeploy.py [--tickers T...]
       [--skim-step 0.10] [--skim-frac 0.20] [--redeploy-step 0.15] [--redeploy-frac 0.25]
"""
import argparse

import numpy as np
import pandas as pd

from sim_bear_market_stress import (
    CRASHES, PROXIES, fetch_underlying, get_node_params, run_strategy_daily,
    synthesize_leveraged,
)


def build_equity_curve(trades, n):
    """Day-indexed equity, flat/cash between trades, compounding on realized returns."""
    equity = np.ones(n)
    level = 1.0
    trades_by_exit = {t["exit_day"]: t for t in trades}
    for i in range(n):
        if i in trades_by_exit:
            level *= (1.0 + trades_by_exit[i]["return"])
        equity[i] = level
    return equity


def skim_redeploy_overlay(strategy_equity, spy_equity, skim_step, skim_frac,
                           redeploy_step, redeploy_frac, w_strategy0=1.0):
    n = len(strategy_equity)
    w_strategy, w_spy = w_strategy0, 1.0 - w_strategy0
    total = 1.0
    skim_ref = strategy_equity[0]
    redeploy_ref = strategy_equity[0]
    peak = strategy_equity[0]

    total_curve = np.ones(n)
    reserve_frac_curve = np.zeros(n)

    for i in range(1, n):
        r_strat = strategy_equity[i] / strategy_equity[i - 1] - 1.0
        r_spy = spy_equity[i] / spy_equity[i - 1] - 1.0
        val_strategy = total * w_strategy * (1 + r_strat)
        val_spy = total * w_spy * (1 + r_spy)
        total = val_strategy + val_spy
        w_strategy, w_spy = val_strategy / total, val_spy / total

        peak = max(peak, strategy_equity[i])

        if strategy_equity[i] >= skim_ref * (1 + skim_step):
            moved = w_strategy * skim_frac
            w_strategy -= moved
            w_spy += moved
            skim_ref = strategy_equity[i]
            redeploy_ref = strategy_equity[i]  # re-arm redeploy ladder from the new high

        if strategy_equity[i] <= redeploy_ref * (1 - redeploy_step) and w_spy > 0:
            moved = w_spy * redeploy_frac
            w_spy -= moved
            w_strategy += moved
            redeploy_ref = strategy_equity[i]

        total_curve[i] = total
        reserve_frac_curve[i] = w_spy

    return total_curve, reserve_frac_curve


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*", default=list(PROXIES.keys()))
    ap.add_argument("--skim-step", type=float, default=0.10)
    ap.add_argument("--skim-frac", type=float, default=0.20)
    ap.add_argument("--redeploy-step", type=float, default=0.15)
    ap.add_argument("--redeploy-frac", type=float, default=0.25)
    args = ap.parse_args()

    underlying_cache = {}
    spy_underlying = fetch_underlying("SPY")
    rows = []

    for ticker in args.tickers:
        if ticker not in PROXIES or ticker == "SPY":
            continue
        proxy, leverage, note = PROXIES[ticker]
        params = get_node_params(ticker)
        if params is None:
            continue
        if proxy not in underlying_cache:
            underlying_cache[proxy] = fetch_underlying(proxy)
        synth = synthesize_leveraged(underlying_cache[proxy], leverage)

        for crash, (start, bottom, recov_end) in CRASHES.items():
            start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(recov_end)
            lookback_start = synth.index.searchsorted(start_ts) - params["window"] - 1
            sim_bars = synth.iloc[max(0, lookback_start):synth.index.searchsorted(end_ts) + 1]
            if len(sim_bars) < params["window"] + 5:
                continue
            win_start_i = sim_bars.index.searchsorted(start_ts)

            trades = run_strategy_daily(sim_bars, params)
            trades = [t for t in trades if t["entry_day"] >= win_start_i]
            strat_eq_full = build_equity_curve(trades, len(sim_bars))
            strat_eq = strat_eq_full[win_start_i:]
            strat_eq = strat_eq / strat_eq[0]

            spy_window = spy_underlying.reindex(sim_bars.index, method="ffill")["Close"].to_numpy()
            spy_eq = spy_window[win_start_i:]
            spy_eq = spy_eq / spy_eq[0]

            n = min(len(strat_eq), len(spy_eq))
            strat_eq, spy_eq = strat_eq[:n], spy_eq[:n]

            overlay_curve, reserve_curve = skim_redeploy_overlay(
                strat_eq, spy_eq, args.skim_step, args.skim_frac,
                args.redeploy_step, args.redeploy_frac)

            baseline_final = strat_eq[-1] - 1.0
            overlay_final = overlay_curve[-1] - 1.0
            baseline_dd = float(np.min(strat_eq / np.maximum.accumulate(strat_eq)) - 1.0)
            overlay_dd = float(np.min(overlay_curve / np.maximum.accumulate(overlay_curve)) - 1.0)

            rows.append({
                "ticker": ticker, "crash": crash,
                "baseline_return": baseline_final, "overlay_return": overlay_final,
                "baseline_max_dd": baseline_dd, "overlay_max_dd": overlay_dd,
                "max_reserve_frac": float(np.max(reserve_curve)),
                "overlay_wins": overlay_final > baseline_final,
            })

    df = pd.DataFrame(rows)
    pd.set_option("display.width", 160)
    for crash in CRASHES:
        sub = df[df["crash"] == crash]
        print(f"\n=== {crash} ===")
        print(sub[["ticker", "baseline_return", "overlay_return", "baseline_max_dd",
                    "overlay_max_dd", "max_reserve_frac"]].to_string(index=False))

    print(f"\nOverlay beats baseline: {df['overlay_wins'].sum()} / {len(df)}")
    print(f"Overlay always has a shallower or equal max drawdown: "
          f"{(df['overlay_max_dd'] >= df['baseline_max_dd']).sum()} / {len(df)}")
    df.to_csv("output/skim_redeploy_stress.csv", index=False)
    print("Wrote output/skim_redeploy_stress.csv")


if __name__ == "__main__":
    main()
