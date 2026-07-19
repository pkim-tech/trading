"""One-off investigation: why do DPST/SOXL's chaos-monkey mean compounded return
hold up (or improve) under missed signals while KORU/HIBL/most of the watchlist
decay? (docs/backlog_cache.md 2026-07-16 chaos-monkey item, flagged unexplained).

Decomposes miss_rate into entry-only vs exit-only to isolate which side drives
each ticker's direction. Hypothesis: simulate_trail_both_chaos's SL check
(export_trades.py:341, `if low <= stop_price`) re-evaluates fresh every bar with
no memory of a prior touch -- if a miss lets a spike-down bar pass without
exiting, and price recovers above stop_price by the next bar, the SL condition
never re-fires and the position rides out what would have been a stop-out in
the perfect-adherence baseline. If that "SL forgiveness" effect is more common
for DPST/SOXL than KORU/HIBL, exit-only misses should show the same
improve-vs-decay divergence as the original combined-miss run, while
entry-only misses should decay for everyone (missing a real entry is pure lost
opportunity, no equivalent escape hatch).

Usage: .venv/bin/python scripts/investigate_chaos_divergence.py
"""
import random
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategies
from backtester import prep_inputs
from export_trades import simulate_trail_both_annotated, simulate_trail_both_chaos
from sim_chaos_monkey import _load, _compounded, get_watchlist_nodes

TICKERS = ["DPST", "SOXL", "KORU", "HIBL"]
MISS_RATE = 0.20
TRIALS = 2000
SEED = 7


def run(node, trials, seed):
    ticker = node["ticker"]
    df_hourly, df_daily = _load(ticker)
    strat = strategies.TrailingBothZScoreBreakout(window=node["window"], z_score_threshold=node["z"])
    df_daily_ind = strat.generate_daily_indicators(df_daily)
    p = prep_inputs(df_hourly, df_daily_ind)
    kwargs = dict(take_profit=node["arm_sell_pct"] / 100, stop_loss=node["fixed_sl"] / 100,
                  max_hours_to_hold=node["max_hold_hours"], trail_buy_pct=node["trail_buy_pct"] / 100,
                  trail_pct=node["trail_sell_pct"] / 100, target_h0=9, target_h1=14, z_thresh=node["z"])

    baseline = _compounded(simulate_trail_both_annotated(p, **kwargs))

    rng = random.Random(seed)
    results = {}
    for label, entry_rate, exit_rate in [
        ("entry_only", MISS_RATE, 0.0),
        ("exit_only", 0.0, MISS_RATE),
        ("both", MISS_RATE, MISS_RATE),
    ]:
        returns = np.empty(trials)
        for t in range(trials):
            trades = simulate_trail_both_chaos(
                p, rng=rng, entry_miss_mode="drop", entry_miss_rate=entry_rate,
                exit_miss_mode="drop", exit_miss_rate=exit_rate,
                max_delay_checks=3, **kwargs)
            returns[t] = _compounded(trades)
        results[label] = dict(mean=returns.mean(), median=np.median(returns),
                               ratio=(returns.mean() + 100) / (baseline + 100) if baseline > -100 else float("nan"))
    return baseline, results


def main():
    nodes = {n["ticker"]: n for n in get_watchlist_nodes()}
    rows = []
    for ticker in TICKERS:
        node = nodes[ticker]
        baseline, results = run(node, TRIALS, SEED)
        row = dict(ticker=ticker, baseline_pct=baseline)
        for label, r in results.items():
            row[f"{label}_mean_pct"] = r["mean"]
            row[f"{label}_ratio"] = r["ratio"]
        rows.append(row)
        print(f"{ticker}: baseline={baseline:+.1f}%")
        for label, r in results.items():
            print(f"    {label:10s} mean={r['mean']:+9.1f}%  ratio={r['ratio']:.3f}")

    df = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    print()
    print(df.to_string(index=False))


if __name__ == "__main__":
    main()
