"""Extends sim_bear_market_stress.py's real historical-crash reconstruction (2008 GFC,
2020 COVID, 2022 bear, 2000 dotcom synthetic daily-bar leveraged-ETF paths) to apply the
v5-stacked overlay modules -- NOT the full stack, see limitation below.

Uses scripts/stacked_model/trade_schema.from_daily_stress to normalize
run_strategy_daily's day-indexed trades into the canonical shape, then
scripts/stacked_model/put_hedge.hedge_pnl (the SAME function v5_stacked_backtest.py uses
on real hourly trades) for the hedge piece -- verified 2026-08-07 to reproduce
sim_bear_market_stress_hedged.py's original inline SOXL/KORU 15%-OTM numbers exactly
before this script was trusted. Skim-and-reserve applies cleanly too (it only needs an
equity curve, not bar-level trade structure).

KNOWN LIMITATION, not silently scoped around: add-on-at-arm and the drought overlay do
NOT apply here. Both need bar-level state the daily-bar synthetic model doesn't expose
-- add-on needs a real arm-day index (run_strategy_daily doesn't track/return one), and
drought-overlay needs a real signal-gap detector against the synthetic series (a
meaningfully separate build, not a generalization of the existing pattern). This script
is core+puthedge+skim only; core+drought+addon's crash resilience is untested here.

Usage: .venv/bin/python scripts/v5_stacked_crash_stress.py [--tickers SOXL AGQ KORU]
       [--otm-pcts 15 25 50]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.sim_bear_market_stress import (
    CRASHES, PROXIES, fetch_underlying, get_node_params, run_strategy_daily, synthesize_leveraged,
)
from scripts.stacked_model.trade_schema import from_daily_stress
from scripts.stacked_model import put_hedge
from scripts.stacked_model import skim_reserve

DB_PATH = Path(__file__).resolve().parent.parent / "cache" / "research" / "trading_universe.db"


def compounded_and_dd(rets):
    if not rets:
        return 0.0, 0.0
    # eq must start at the real 1.0 starting point, not the first trade's own outcome --
    # see scripts/v5_stacked_backtest.py's identical fix (2026-08-08). Most likely to
    # bite here specifically, since this script filters to decline-only trades, whose
    # first entry is often already a loser.
    eq = np.cumprod(np.r_[1.0, [1 + r for r in rets]])
    peak = np.maximum.accumulate(eq)
    return float(eq[-1] - 1.0), float(((eq - peak) / peak).min())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*", default=["SOXL", "AGQ", "KORU"])
    ap.add_argument("--otm-pcts", nargs="*", type=float, default=[15, 25, 50])
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    underlying_cache = {}
    spy_underlying = fetch_underlying("SPY")
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
        if proxy not in underlying_cache:
            underlying_cache[proxy] = fetch_underlying(proxy)
        synth = synthesize_leveraged(underlying_cache[proxy], leverage)

        for crash, (start, bottom, recov_end) in CRASHES.items():
            start_ts, bottom_ts, end_ts = pd.Timestamp(start), pd.Timestamp(bottom), pd.Timestamp(recov_end)
            window = synth.loc[start:recov_end]
            if len(window) < params["window"] + 5:
                continue
            lookback_start = synth.index.searchsorted(start_ts) - params["window"] - 1
            sim_bars = synth.iloc[max(0, lookback_start):synth.index.searchsorted(end_ts) + 1]
            all_trades = run_strategy_daily(sim_bars, params)
            win_start_i = sim_bars.index.searchsorted(start_ts)
            bottom_i = sim_bars.index.searchsorted(bottom_ts)
            decline_trades = [t for t in all_trades if win_start_i <= t["entry_day"] < bottom_i]
            if not decline_trades:
                continue

            canonical = [from_daily_stress(t) for t in decline_trades]
            unhedged_comp, unhedged_dd = compounded_and_dd([t["ret"] for t in canonical])
            row = {"ticker": ticker, "crash": crash, "trades": len(canonical),
                   "core_compounded_pct": unhedged_comp * 100, "core_max_dd_pct": unhedged_dd * 100}

            # Crash-stress trades are synthetic/historical (e.g. 2008) -- there's no way
            # to know historical IV, so (matching the already-validated
            # sim_bear_market_stress_hedged.py precedent) every trade is priced with
            # TODAY's real IV as the best available forward-looking proxy.
            for otm in args.otm_pcts:
                hedged = put_hedge.apply_hedge(canonical, ticker, otm, conn, prefer_real_iv=True)
                comp, dd = compounded_and_dd([t["ret"] for t in hedged])
                row[f"puthedge{int(otm)}_compounded_pct"] = comp * 100
                row[f"puthedge{int(otm)}_max_dd_pct"] = dd * 100

            spy_window = spy_underlying.reindex(sim_bars.index, method="ffill")["Close"].to_numpy()
            strat_eq = np.cumprod([1.0] + [1 + t["ret"] for t in canonical])
            spy_eq_at_exits = np.array([spy_window[win_start_i]] +
                                        [spy_window[t["exit_i"]] for t in canonical])
            spy_eq_at_exits = spy_eq_at_exits / spy_eq_at_exits[0]
            skim_curve, _ = skim_reserve.manual_redeploy_overlay(strat_eq, spy_eq_at_exits, latency_days=0)
            skim_comp, skim_dd = compounded_and_dd(list(np.diff(skim_curve) / skim_curve[:-1]))
            row["core+skim_compounded_pct"] = (skim_curve[-1] - 1.0) * 100
            row["core+skim_max_dd_pct"] = float(
                np.min(skim_curve / np.maximum.accumulate(skim_curve)) - 1.0) * 100

            rows.append(row)

    df = pd.DataFrame(rows)
    pd.set_option("display.width", 220)
    print(df.round(1).to_string(index=False))
    df.to_csv("output/v5_stacked_crash_stress.csv", index=False)
    print("\nWrote output/v5_stacked_crash_stress.csv")
    print("\nNOTE: add-on-at-arm and drought-overlay are NOT included in this crash test "
          "(see module docstring) -- their crash resilience remains untested.")


if __name__ == "__main__":
    main()
