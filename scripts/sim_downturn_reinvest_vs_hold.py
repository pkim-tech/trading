"""Starting from today's REAL skim-reserve split (computed from a ticker's actual real
trade history, see sim_real_skim_reserve.py), simulate a synthetic crash+recovery
forward from here under two policies:
  - REINVEST: keep laddering the reserve back into the strategy as it draws down
    (the same skim_redeploy_overlay rule already used, redeploy_frac active).
  - HOLD: leave the reserve fully parked in the reserve ticker for the whole
    crash+recovery cycle, untouched (redeploy_frac=0) -- only the strategy-allocated
    capital is exposed to the crash.

Same daily-bar synthetic-leveraged-ETF caveats as sim_bear_market_stress.py apply.
Strategy tickers must have a proxy/leverage entry in sim_bear_market_stress.PROXIES.
Reserve tickers either have an entry in RESERVE_PROXY_LEVERAGE (leveraged, needs
synthesis) or are fetched directly at 1x (e.g. SPY, USO -- both have enough real
yfinance history to cover all 3 crash windows without synthesis).

Usage: .venv/bin/python scripts/sim_downturn_reinvest_vs_hold.py [--strategy-ticker SOXL]
       [--reserve-ticker SPY|SSO|USO|...]
"""
import argparse

import numpy as np
import pandas as pd

from sim_bear_market_stress import CRASHES, PROXIES, fetch_underlying, run_strategy_daily, synthesize_leveraged
from sim_real_skim_reserve import build_real_trades, daily_equity_from_trades, get_node, load_hourly
from sim_skim_redeploy import skim_redeploy_overlay

RESERVE_PROXY_LEVERAGE = {"SPY": ("SPY", 1), "SSO": ("SPY", 2)}


def real_current_split(strategy_ticker, reserve_ticker):
    """Today's real w_strategy/w_reserve split from strategy_ticker's actual real history."""
    node = get_node(strategy_ticker, 65)
    trades, timestamps = build_real_trades(strategy_ticker, node)
    strat_equity = daily_equity_from_trades(trades, timestamps)

    reserve_h = load_hourly(reserve_ticker)
    reserve_daily = reserve_h["Close"].resample("D").last().dropna()
    reserve_equity = reserve_daily.reindex(strat_equity.index, method="ffill").bfill()
    reserve_equity = reserve_equity / reserve_equity.iloc[0]

    overlay_curve, reserve_curve = skim_redeploy_overlay(
        strat_equity.to_numpy(), reserve_equity.to_numpy(),
        skim_step=0.10, skim_frac=0.20, redeploy_step=0.15, redeploy_frac=0.25)
    w_reserve = float(reserve_curve[-1])
    real_total_value = node["starting_notional"] * overlay_curve[-1]
    return 1.0 - w_reserve, w_reserve, real_total_value


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy-ticker", default="SOXL")
    ap.add_argument("--reserve-ticker", default="SPY")
    ap.add_argument("--skim-step", type=float, default=0.10)
    ap.add_argument("--skim-frac", type=float, default=0.20)
    ap.add_argument("--redeploy-step", type=float, default=0.15)
    ap.add_argument("--redeploy-frac", type=float, default=0.25)
    args = ap.parse_args()

    if args.strategy_ticker not in PROXIES:
        raise SystemExit(f"no crash-proxy mapping for {args.strategy_ticker} in sim_bear_market_stress.PROXIES")
    strat_proxy, strat_leverage, _ = PROXIES[args.strategy_ticker]

    w_strategy0, w_reserve0, current_total_naive = real_current_split(args.strategy_ticker, args.reserve_ticker)
    print(f"Starting split (today, real): {w_strategy0:.1%} in {args.strategy_ticker}, {w_reserve0:.1%} in "
          f"{args.reserve_ticker} reserve")

    strat_underlying = fetch_underlying(strat_proxy)
    strat_synth = synthesize_leveraged(strat_underlying, strat_leverage)
    if args.reserve_ticker in RESERVE_PROXY_LEVERAGE:
        res_proxy, res_leverage = RESERVE_PROXY_LEVERAGE[args.reserve_ticker]
        res_underlying = strat_underlying if res_proxy == strat_proxy else fetch_underlying(res_proxy)
        res_synth = synthesize_leveraged(res_underlying, res_leverage)
    else:
        # 1x reserve ticker with its own real long history (e.g. USO) -- fetch directly, no synthesis
        res_synth = fetch_underlying(args.reserve_ticker)

    node = get_node(args.strategy_ticker, 65)
    rows = []
    for crash, (start, bottom, recov_end) in CRASHES.items():
        start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(recov_end)
        lookback_start = strat_synth.index.searchsorted(start_ts) - node["window"] - 1
        sim_bars = strat_synth.iloc[max(0, lookback_start):strat_synth.index.searchsorted(end_ts) + 1]
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

        res_window = res_synth.reindex(sim_bars.index, method="ffill")["Close"].to_numpy()
        res_eq = res_window[win_start_i:]
        res_eq = res_eq / res_eq[0]

        n = min(len(strat_eq), len(res_eq))
        strat_eq, res_eq = strat_eq[:n], res_eq[:n]

        reinvest_curve, _ = skim_redeploy_overlay(
            strat_eq, res_eq, args.skim_step, args.skim_frac,
            args.redeploy_step, args.redeploy_frac, w_strategy0=w_strategy0)
        hold_curve, _ = skim_redeploy_overlay(
            strat_eq, res_eq, args.skim_step, args.skim_frac,
            redeploy_step=999.0, redeploy_frac=0.0, w_strategy0=w_strategy0)

        rows.append({
            "crash": crash,
            "reinvest_return": reinvest_curve[-1] - 1.0,
            "hold_return": hold_curve[-1] - 1.0,
            "reinvest_final_value": current_total_naive * reinvest_curve[-1],
            "hold_final_value": current_total_naive * hold_curve[-1],
        })

    df = pd.DataFrame(rows)
    pd.set_option("display.width", 160)
    print(f"\nStarting total account value (real, today): ${current_total_naive:,.0f}\n")
    print(df.to_string(index=False))
    df.to_csv(f"output/downturn_reinvest_vs_hold_{args.reserve_ticker}.csv", index=False)
    print(f"\nWrote output/downturn_reinvest_vs_hold_{args.reserve_ticker}.csv")


if __name__ == "__main__":
    main()
