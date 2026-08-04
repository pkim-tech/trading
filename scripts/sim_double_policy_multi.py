"""Tests the "double" skim policy (hold reserve untouched through a decline, then
move it ALL back into the strategy the moment the strategy reclaims its own
pre-decline level, then resume skimming fresh from there) across multiple real
strategy tickers and reserve vehicles.

For each (strategy ticker, reserve ticker) pair:
  1. Compute today's REAL strategy/reserve split from the ticker's actual 3-year
     backtested trade history (pure skim only, no ladder redeploy -- see
     sim_real_skim_reserve.py / the "hold" policy from prior sessions).
  2. Starting from that real split, run a synthetic crash+recovery (see
     sim_bear_market_stress.py for the daily-bar-approximation caveats) under
     "hold" vs "double" and report both.

Usage: .venv/bin/python scripts/sim_double_policy_multi.py
       [--tickers SOXL AGQ KORU ...] [--reserves SPY QQQ]
"""
import argparse

import numpy as np
import pandas as pd

from sim_bear_market_stress import CRASHES, PROXIES, fetch_underlying, run_strategy_daily, synthesize_leveraged
from sim_real_skim_reserve import build_real_trades, daily_equity_from_trades, get_node, load_hourly

DEFAULT_TICKERS = ["SOXL", "AGQ", "KORU", "GDXU", "NUGT", "HIBL", "USD", "YANG", "UDOW", "DPST"]
DEFAULT_RESERVES = ["SPY", "QQQ"]
# UPRO/TQQQ's real data starts after the GFC began -- reconstruct synthetically from
# their 1x underlying instead so the GFC window has full coverage, same pattern as PROXIES.
RESERVE_SYNTH_OVERRIDE = {"UPRO": ("SPY", 3), "TQQQ": ("QQQ", 3)}


def overlay(strategy_equity, reserve_equity, skim_step, skim_frac, mode, w_strategy0,
            recovery_level=1.0, signal_equity=None):
    """signal_equity: the equity curve used to decide WHEN to redeploy in
    'double_reserve' mode -- defaults to reserve_equity itself, but can be a
    different series (e.g. always SPY) so the trigger and the actual parked
    vehicle are independent."""
    if signal_equity is None:
        signal_equity = reserve_equity
    n = len(strategy_equity)
    w_strategy, w_reserve = w_strategy0, 1.0 - w_strategy0
    total = 1.0
    skim_ref = strategy_equity[0]
    recovered = False
    has_declined = False  # only arm the recovery trigger AFTER a real decline below recovery_level
    signal_peak = signal_equity[0]
    total_curve = np.ones(n)
    for i in range(1, n):
        r_strat = strategy_equity[i] / strategy_equity[i - 1] - 1.0
        r_res = reserve_equity[i] / reserve_equity[i - 1] - 1.0
        val_strategy = total * w_strategy * (1 + r_strat)
        val_reserve = total * w_reserve * (1 + r_res)
        total = val_strategy + val_reserve
        w_strategy, w_reserve = val_strategy / total, val_reserve / total
        signal_peak = max(signal_peak, signal_equity[i])

        if mode == "double":
            trigger_level = strategy_equity[i]
            armed = strategy_equity[i] < recovery_level
        else:  # double_reserve: trigger off signal_equity's OWN drawdown-from-peak
            trigger_level = signal_equity[i] / signal_peak
            armed = trigger_level < recovery_level

        if armed:
            has_declined = True

        if mode in ("double", "double_reserve") and not recovered and has_declined and trigger_level >= recovery_level:
            w_strategy += w_reserve
            w_reserve = 0.0
            recovered = True
            skim_ref = strategy_equity[i]

        if strategy_equity[i] >= skim_ref * (1 + skim_step):
            moved = w_strategy * skim_frac
            w_strategy -= moved
            w_reserve += moved
            skim_ref = strategy_equity[i]

        total_curve[i] = total
    return total_curve


def real_current_split(ticker, reserve_ticker, watchlist_id=65):
    node = get_node(ticker, watchlist_id)
    if node is None:
        return None
    trades, timestamps = build_real_trades(ticker, node)
    strat_equity = daily_equity_from_trades(trades, timestamps)
    reserve_h = load_hourly(reserve_ticker)
    reserve_daily = reserve_h["Close"].resample("D").last().dropna()
    reserve_equity = reserve_daily.reindex(strat_equity.index, method="ffill").bfill()
    reserve_equity = reserve_equity / reserve_equity.iloc[0]

    curve = overlay(strat_equity.to_numpy(), reserve_equity.to_numpy(),
                     skim_step=0.10, skim_frac=0.20, mode="hold", w_strategy0=1.0)
    # infer w_reserve from a matching reserve-only run since overlay() doesn't return it directly
    w_reserve = 0.0
    w_strategy, w_r = 1.0, 0.0
    tot = 1.0
    skim_ref = strat_equity.to_numpy()[0]
    for i in range(1, len(strat_equity)):
        r_strat = strat_equity.iloc[i] / strat_equity.iloc[i - 1] - 1.0
        r_res = reserve_equity.iloc[i] / reserve_equity.iloc[i - 1] - 1.0
        val_s = tot * w_strategy * (1 + r_strat)
        val_r = tot * w_r * (1 + r_res)
        tot = val_s + val_r
        w_strategy, w_r = val_s / tot, val_r / tot
        if strat_equity.iloc[i] >= skim_ref * 1.10:
            moved = w_strategy * 0.20
            w_strategy -= moved
            w_r += moved
            skim_ref = strat_equity.iloc[i]
    real_total_value = node["starting_notional"] * tot
    return 1.0 - w_r, w_r, real_total_value, node


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*", default=DEFAULT_TICKERS)
    ap.add_argument("--reserves", nargs="*", default=DEFAULT_RESERVES)
    args = ap.parse_args()

    underlying_cache = {}
    reserve_underlying_cache = {}
    rows = []
    spy_synth = synthesize_leveraged(fetch_underlying("SPY"), 1)

    for ticker in args.tickers:
        if ticker not in PROXIES:
            print(f"{ticker}: no proxy mapping, skipping")
            continue
        proxy, leverage, note = PROXIES[ticker]

        for reserve_ticker in args.reserves:
            split = real_current_split(ticker, reserve_ticker)
            if split is None:
                print(f"{ticker}: no watch_list node, skipping")
                continue
            w_strategy0, w_reserve0, current_total, node = split

            if proxy not in underlying_cache:
                underlying_cache[proxy] = fetch_underlying(proxy)
            synth = synthesize_leveraged(underlying_cache[proxy], leverage)
            res_proxy, res_leverage = RESERVE_SYNTH_OVERRIDE.get(reserve_ticker, (reserve_ticker, 1))
            if res_proxy not in reserve_underlying_cache:
                reserve_underlying_cache[res_proxy] = fetch_underlying(res_proxy)
            reserve_synth = synthesize_leveraged(reserve_underlying_cache[res_proxy], res_leverage)

            for crash, (start, bottom, recov_end) in CRASHES.items():
                start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(recov_end)
                lookback_start = synth.index.searchsorted(start_ts) - node["window"] - 1
                sim_bars = synth.iloc[max(0, lookback_start):synth.index.searchsorted(end_ts) + 1]
                if len(sim_bars) < node["window"] + 5:
                    continue
                win_start_i = sim_bars.index.searchsorted(start_ts)

                params = {"window": node["window"], "z_score_threshold": node["z_score_threshold"],
                          "fixed_sl": node["fixed_sl"], "trail_buy_pct": node["trail_buy_pct"],
                          "trail_sell_pct": node["trail_sell_pct"], "arm_sell_pct": node["arm_sell_pct"],
                          "take_profit": None, "max_hold_hours": node["max_hold_hours"]}
                trades = [t for t in run_strategy_daily(sim_bars, params) if t["entry_day"] >= win_start_i]

                equity = np.ones(len(sim_bars))
                level = 1.0
                by_exit = {t["exit_day"]: t for t in trades}
                for i in range(len(sim_bars)):
                    if i in by_exit:
                        level *= (1.0 + by_exit[i]["return"])
                    equity[i] = level
                strat_eq = equity[win_start_i:]
                strat_eq = strat_eq / strat_eq[0]

                res_window = reserve_synth.reindex(sim_bars.index, method="ffill")["Close"].to_numpy()
                res_eq = res_window[win_start_i:]
                res_eq = res_eq / res_eq[0]

                spy_signal_window = spy_synth.reindex(sim_bars.index, method="ffill")["Close"].to_numpy()
                spy_signal_eq = spy_signal_window[win_start_i:]
                spy_signal_eq = spy_signal_eq / spy_signal_eq[0]

                n = min(len(strat_eq), len(res_eq), len(spy_signal_eq))
                strat_eq, res_eq, spy_signal_eq = strat_eq[:n], res_eq[:n], spy_signal_eq[:n]

                hold_curve = overlay(strat_eq, res_eq, 0.10, 0.20, "hold", w_strategy0)
                double_curve = overlay(strat_eq, res_eq, 0.10, 0.20, "double", w_strategy0,
                                        recovery_level=1.0)
                double80_curve = overlay(strat_eq, res_eq, 0.10, 0.20, "double", w_strategy0,
                                          recovery_level=0.8)
                spy100_curve = overlay(strat_eq, res_eq, 0.10, 0.20, "double_reserve", w_strategy0,
                                        signal_equity=spy_signal_eq,
                                        recovery_level=1.0)
                spy80_curve = overlay(strat_eq, res_eq, 0.10, 0.20, "double_reserve", w_strategy0,
                                       signal_equity=spy_signal_eq,
                                       recovery_level=0.8)

                rows.append({
                    "ticker": ticker, "reserve": reserve_ticker, "crash": crash,
                    "start_value": current_total,
                    "hold_final": current_total * hold_curve[-1],
                    "double100_final": current_total * double_curve[-1],
                    "double80_final": current_total * double80_curve[-1],
                    "reserve100_final": current_total * spy100_curve[-1],
                    "reserve80_final": current_total * spy80_curve[-1],
                    "double100_vs_hold_pct": (double_curve[-1] / hold_curve[-1] - 1.0) * 100,
                    "double80_vs_hold_pct": (double80_curve[-1] / hold_curve[-1] - 1.0) * 100,
                    "reserve100_vs_hold_pct": (spy100_curve[-1] / hold_curve[-1] - 1.0) * 100,
                    "reserve80_vs_hold_pct": (spy80_curve[-1] / hold_curve[-1] - 1.0) * 100,
                })

    df = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_rows", 100)
    print(df.to_string(index=False))
    df.to_csv("output/double_policy_multi.csv", index=False)
    print("\nWrote output/double_policy_multi.csv")

    print("\n--- Win counts (out of", len(df), ") ---")
    print(f"double[strategy]@100% beats hold: {(df['double100_vs_hold_pct'] > 0).sum()}")
    print(f"double[strategy]@80% beats hold:  {(df['double80_vs_hold_pct'] > 0).sum()}")
    print(f"double[reserve]@100% beats hold:  {(df['reserve100_vs_hold_pct'] > 0).sum()}")
    print(f"double[reserve]@80% beats hold:   {(df['reserve80_vs_hold_pct'] > 0).sum()}")
    print(f"double[reserve]@100% beats double[strategy]@100%: {(df['reserve100_final'] > df['double100_final']).sum()}")


if __name__ == "__main__":
    main()
