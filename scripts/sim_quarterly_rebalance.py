"""Quarterly-rebalance overlay test, run head-to-head against the existing skim-and-
redeploy overlay (sim_skim_redeploy.py) and a 100%-in-strategy baseline -- at the
PORTFOLIO level (all real v5 watchlist tickers combined, equal-weighted), not just
per-ticker. This is the alternative floated in a separate conversation: instead of an
event-triggered skim (new-high threshold) / redeploy (drawdown threshold) ladder,
periodically (e.g. every ~63 trading days = 1 quarter) rebalance strategy-vs-SPY-
reserve weights back to a fixed target split, regardless of price level.

Reuses the same daily-bar synthetic leveraged-ETF harness as sim_bear_market_stress.py
and the same real crash windows -- same caveats apply (daily-bar approximation of the
live hourly kernel, not exact; see that script's docstring for why).

Why portfolio-level: the real account shares capital across tickers, and a single-
ticker view can't show whether a calendar rebalance's fixed trigger dates happen to
land badly for one ticker but well for another -- pooling smooths idiosyncratic timing
and is the actually-relevant comparison for a real shared-capital decision.

Usage: .venv/bin/python scripts/sim_quarterly_rebalance.py
       [--rebalance-days 63] [--target-weight 0.80]
       [--skim-step 0.10] [--skim-frac 0.20] [--redeploy-step 0.15] [--redeploy-frac 0.25]
"""
import argparse

import numpy as np
import pandas as pd

from sim_bear_market_stress import (
    CRASHES, PROXIES, fetch_underlying, get_node_params, run_strategy_daily,
    synthesize_leveraged,
)
from sim_skim_redeploy import build_equity_curve, skim_redeploy_overlay

# Real v5 watchlist tickers (watchlist_id=65) -- the actual live/paper portfolio.
# SPY/SH/QQQ/SOXS excluded: SPY is the reserve asset itself, the other 3 are extra
# proxies in PROXIES not part of the real watchlist.
PORTFOLIO_TICKERS = ["AGQ", "DPST", "GDXU", "HIBL", "KORU", "NUGT", "SOXL", "UDOW", "USD", "YANG"]


def quarterly_rebalance_overlay(strategy_equity, spy_equity, target_strategy_weight,
                                 rebalance_every_days, w_strategy0=1.0):
    """Fixed calendar-date rebalance: every `rebalance_every_days` trading days, reset
    strategy/SPY-reserve weights back to (target_strategy_weight, 1-target). Unlike
    skim/redeploy, this fires on a schedule regardless of price level -- trims winners
    and tops up from the reserve on losers alike, whatever the calendar says."""
    n = len(strategy_equity)
    w_strategy, w_spy = w_strategy0, 1.0 - w_strategy0
    total = 1.0

    total_curve = np.ones(n)
    reserve_frac_curve = np.zeros(n)
    reserve_frac_curve[0] = w_spy

    for i in range(1, n):
        r_strat = strategy_equity[i] / strategy_equity[i - 1] - 1.0
        r_spy = spy_equity[i] / spy_equity[i - 1] - 1.0
        val_strategy = total * w_strategy * (1 + r_strat)
        val_spy = total * w_spy * (1 + r_spy)
        total = val_strategy + val_spy
        w_strategy, w_spy = val_strategy / total, val_spy / total

        if i % rebalance_every_days == 0:
            w_strategy, w_spy = target_strategy_weight, 1.0 - target_strategy_weight

        total_curve[i] = total
        reserve_frac_curve[i] = w_spy

    return total_curve, reserve_frac_curve


def build_portfolio_curves(tickers, crash, window):
    """For one crash window, builds each ticker's own strategy equity curve, reindexes
    all of them onto SPY's own trading-day calendar for that window (ffill for any
    per-proxy calendar gaps), and equal-weight-averages the tickers that have data.
    Returns (master_dates, portfolio_strategy_eq, spy_eq) or None if <2 tickers have
    coverage for this crash (matches the existing per-ticker script's silent-skip
    pattern for crashes that predate a given proxy's history, e.g. 2000_dotcom/SLV)."""
    start, bottom, recov_end = window
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(recov_end)

    spy_underlying = fetch_underlying("SPY")
    master = spy_underlying.loc[start_ts:end_ts]
    if len(master) < 20:
        return None
    master_dates = master.index
    spy_eq = (master["Close"] / master["Close"].iloc[0]).to_numpy()

    per_ticker_eq = {}
    for ticker in tickers:
        proxy, leverage, _note = PROXIES[ticker]
        params = get_node_params(ticker)
        if params is None:
            continue
        underlying = fetch_underlying(proxy)
        synth = synthesize_leveraged(underlying, leverage)
        if synth.index.min() > start_ts - pd.Timedelta(days=params["window"] * 3):
            continue  # proxy's history doesn't reach far enough back for this crash

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
        strat_series = pd.Series(strat_eq, index=sim_bars.index[win_start_i:])

        # Reindex onto the shared SPY calendar; ffill covers any per-proxy holiday
        # mismatch, bfill covers a proxy whose own index starts a day or two late.
        reindexed = strat_series.reindex(master_dates, method="ffill").bfill()
        per_ticker_eq[ticker] = reindexed.to_numpy()

    if len(per_ticker_eq) < 2:
        return None

    portfolio_eq = np.mean(np.vstack(list(per_ticker_eq.values())), axis=0)
    portfolio_eq = portfolio_eq / portfolio_eq[0]
    return master_dates, portfolio_eq, spy_eq, list(per_ticker_eq.keys())


def max_drawdown(curve):
    return float(np.min(curve / np.maximum.accumulate(curve)) - 1.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebalance-days", type=int, default=63, help="trading days between rebalances (~1 quarter)")
    ap.add_argument("--target-weight", type=float, default=0.80, help="target strategy weight after each rebalance")
    ap.add_argument("--skim-step", type=float, default=0.10)
    ap.add_argument("--skim-frac", type=float, default=0.20)
    ap.add_argument("--redeploy-step", type=float, default=0.15)
    ap.add_argument("--redeploy-frac", type=float, default=0.25)
    ap.add_argument("--sweep", action="store_true",
                     help="also run target-weight {0.70,0.80,0.90} x rebalance-days {21,63,126} grid")
    args = ap.parse_args()

    rows = []
    detail_rows = []

    for crash, window in CRASHES.items():
        built = build_portfolio_curves(PORTFOLIO_TICKERS, crash, window)
        if built is None:
            print(f"[{crash}] skipped -- insufficient ticker coverage for this window")
            continue
        master_dates, port_eq, spy_eq, covered = built

        baseline_return = port_eq[-1] - 1.0
        baseline_dd = max_drawdown(port_eq)

        skim_curve, skim_reserve = skim_redeploy_overlay(
            port_eq, spy_eq, args.skim_step, args.skim_frac, args.redeploy_step, args.redeploy_frac)
        skim_return = skim_curve[-1] - 1.0
        skim_dd = max_drawdown(skim_curve)

        qr_curve, qr_reserve = quarterly_rebalance_overlay(
            port_eq, spy_eq, args.target_weight, args.rebalance_days)
        qr_return = qr_curve[-1] - 1.0
        qr_dd = max_drawdown(qr_curve)

        row = {
            "crash": crash, "tickers_covered": len(covered), "days": len(master_dates),
            "baseline_return": baseline_return, "baseline_max_dd": baseline_dd,
            "skim_return": skim_return, "skim_max_dd": skim_dd, "skim_max_reserve": float(np.max(skim_reserve)),
            "qrebal_return": qr_return, "qrebal_max_dd": qr_dd, "qrebal_max_reserve": float(np.max(qr_reserve)),
        }
        rows.append(row)
        detail_rows.append({"crash": crash, "covered_tickers": ",".join(covered)})

    df = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    print("\n=== Portfolio-level comparison (equal-weighted, real v5 watchlist tickers) ===")
    print(df.to_string(index=False))

    print(f"\nBaseline (100% strategy):   mean return={df['baseline_return'].mean():+.1%}  "
          f"mean max_dd={df['baseline_max_dd'].mean():+.1%}")
    print(f"Skim-and-redeploy overlay:  mean return={df['skim_return'].mean():+.1%}  "
          f"mean max_dd={df['skim_max_dd'].mean():+.1%}  mean max_reserve={df['skim_max_reserve'].mean():.1%}")
    print(f"Quarterly-rebalance overlay: mean return={df['qrebal_return'].mean():+.1%}  "
          f"mean max_dd={df['qrebal_max_dd'].mean():+.1%}  mean max_reserve={df['qrebal_max_reserve'].mean():.1%}")

    for pretty, detail in [("Detail", detail_rows)]:
        print(f"\n-- {pretty} --")
        for d in detail:
            print(f"  {d['crash']}: {d['covered_tickers']}")

    df.to_csv("output/quarterly_rebalance_portfolio_stress.csv", index=False)
    print("\nWrote output/quarterly_rebalance_portfolio_stress.csv")

    if args.sweep:
        print("\n\n=== Parameter sweep: target_weight x rebalance_days ===")
        sweep_rows = []
        cached = {}
        for crash, window in CRASHES.items():
            built = build_portfolio_curves(PORTFOLIO_TICKERS, crash, window)
            if built is not None:
                cached[crash] = built

        for target_weight in (0.70, 0.80, 0.90):
            for rebalance_days in (21, 63, 126):  # ~monthly, quarterly, semi-annual
                returns, dds, reserves = [], [], []
                for crash, (master_dates, port_eq, spy_eq, covered) in cached.items():
                    curve, reserve = quarterly_rebalance_overlay(port_eq, spy_eq, target_weight, rebalance_days)
                    returns.append(curve[-1] - 1.0)
                    dds.append(max_drawdown(curve))
                    reserves.append(float(np.max(reserve)))
                sweep_rows.append({
                    "target_weight": target_weight, "rebalance_days": rebalance_days,
                    "mean_return": np.mean(returns), "mean_max_dd": np.mean(dds),
                    "mean_max_reserve": np.mean(reserves),
                })
        sweep_df = pd.DataFrame(sweep_rows)
        print(sweep_df.to_string(index=False))
        sweep_df.to_csv("output/quarterly_rebalance_sweep.csv", index=False)
        print("\nWrote output/quarterly_rebalance_sweep.csv")


if __name__ == "__main__":
    main()
