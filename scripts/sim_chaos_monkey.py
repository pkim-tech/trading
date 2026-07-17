"""Execution-adherence robustness ("chaos monkey") simulation — quantifies how
much of a node's backtested compounded return depends on catching every single
entry/exit signal exactly on time, the real risk during manual-execution
trading. See docs/backlog_cache.md's 2026-07-16 "chaos monkey" item.

Model (see export_trades.simulate_trail_both_chaos / _resolve_miss for the
exact mechanics):
- Entry signals are only checkable at the two daily signal windows
  (target_h0/target_h1, matching the real Slack alert cadence); exit triggers
  (SL / trailing-stop breach / TIME) are checked continuously, every hourly
  bar, matching the live daemon's continuous position monitoring. TP-arming
  (switching into trailing-sell mode) is never missable -- it's an internal
  state change, not a discrete action a human has to click.
- Two independently-configurable miss modes, applied to both entry and exit
  checks at the same rate per run:
    drop  -- each check is an independent coin flip at `miss_rate`; a missed
             check is simply not acted on, no memory. If the underlying
             condition disappears before it's ever caught (entry setup moves
             away, exit price recovers), that opportunity is gone for good --
             an unbounded "the alert just never got noticed in time" model.
    delay -- same per-check coin flip, but capped: once the SAME
             still-qualifying condition has been missed `max_delay_checks - 1`
             times in a row, the next check forces action regardless of the
             coin flip. Models "I'm bad about it but I always catch it within
             N checks" rather than an unbounded miss.
- miss_rate in {1%, 5%, 10%, 20%}, max_delay_checks=3, 1000 Monte Carlo trials
  per (ticker, mode, miss_rate), using each ticker's real current live
  watch_list node (watchlist_id=9).
- Baseline ("perfect adherence") is simulate_trail_both_annotated's normal
  compounded return -- matches every other number already on file.

Caveat: this pure-Python mirror only supports entry_timing='close' (same
limitation as sim_delayed_sell.py) -- GDXD's real live node uses open_check,
so its numbers here are indicative only, not its exact production behavior.

Usage:
    .venv/bin/python scripts/sim_chaos_monkey.py                    # all watchlist_id=9 tickers
    .venv/bin/python scripts/sim_chaos_monkey.py --tickers SOXL KORU
    .venv/bin/python scripts/sim_chaos_monkey.py --trials 200        # faster, coarser
"""
import argparse
import random
import sqlite3
import time
from pathlib import Path

import numpy as np
import pandas as pd

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategies
from backtester import prep_inputs
from export_trades import simulate_trail_both_annotated, simulate_trail_both_chaos

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / "cache" / "research"
LIVE_DIR = REPO_ROOT / "cache" / "live"
OUTPUT_DIR = REPO_ROOT / "output"

MISS_RATES = [0.01, 0.05, 0.10, 0.20]
MODES = ["drop", "delay"]
MAX_DELAY_CHECKS = 3


def _load(ticker):
    df_hourly = pd.read_csv(CACHE_DIR / f"{ticker}_1h.csv", index_col=0, parse_dates=True)
    df_hourly.index = pd.to_datetime(df_hourly.index).tz_localize(None)
    df_hourly = df_hourly.sort_index()
    close_col = 'Adj Close' if 'Adj Close' in df_hourly.columns else 'Close'
    df_daily = df_hourly.resample('D').last().dropna(subset=[close_col])
    return df_hourly, df_daily


def _compounded(trades):
    ret = 1.0
    for t in trades:
        ret *= (1.0 + t['ret'])
    return (ret - 1.0) * 100.0


def get_watchlist_nodes(watchlist_id=9):
    conn = sqlite3.connect(LIVE_DIR / "trading_live.db")
    c = conn.cursor()
    c.execute(
        "SELECT ticker, window, z_score_threshold, trail_buy_pct, arm_sell_pct, "
        "trail_sell_pct, fixed_sl, max_hold_hours, entry_timing "
        "FROM watch_list WHERE watchlist_id=? ORDER BY ticker",
        (watchlist_id,),
    )
    rows = c.fetchall()
    conn.close()
    cols = ["ticker", "window", "z", "trail_buy_pct", "arm_sell_pct",
            "trail_sell_pct", "fixed_sl", "max_hold_hours", "entry_timing"]
    return [dict(zip(cols, r)) for r in rows]


def run_ticker(node, trials, seed):
    ticker = node["ticker"]
    df_hourly, df_daily = _load(ticker)
    strat = strategies.TrailingBothZScoreBreakout(window=node["window"],
                                                    z_score_threshold=node["z"])
    df_daily_ind = strat.generate_daily_indicators(df_daily)
    p = prep_inputs(df_hourly, df_daily_ind)

    kwargs = dict(take_profit=node["arm_sell_pct"] / 100, stop_loss=node["fixed_sl"] / 100,
                  max_hours_to_hold=node["max_hold_hours"], trail_buy_pct=node["trail_buy_pct"] / 100,
                  trail_pct=node["trail_sell_pct"] / 100, target_h0=9, target_h1=14, z_thresh=node["z"])

    baseline_trades = simulate_trail_both_annotated(p, **kwargs)
    baseline = _compounded(baseline_trades)

    rows = []
    rng = random.Random(seed)
    for mode in MODES:
        for miss_rate in MISS_RATES:
            returns = np.empty(trials)
            for t in range(trials):
                trades = simulate_trail_both_chaos(
                    p, rng=rng, entry_miss_mode=mode, entry_miss_rate=miss_rate,
                    exit_miss_mode=mode, exit_miss_rate=miss_rate,
                    max_delay_checks=MAX_DELAY_CHECKS, **kwargs)
                returns[t] = _compounded(trades)
            rows.append({
                "ticker": ticker, "mode": mode, "miss_rate": miss_rate,
                "baseline_pct": baseline,
                "mean_pct": returns.mean(), "median_pct": np.median(returns),
                "p10_pct": np.percentile(returns, 10), "p90_pct": np.percentile(returns, 90),
                "mean_vs_baseline_ratio": (returns.mean() + 100) / (baseline + 100) if baseline > -100 else float("nan"),
                "entry_timing": node["entry_timing"],
            })
    return rows, baseline_trades


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="+", default=None)
    parser.add_argument("--trials", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--watchlist-id", type=int, default=9)
    args = parser.parse_args()

    nodes = get_watchlist_nodes(args.watchlist_id)
    if args.tickers:
        wanted = set(args.tickers)
        nodes = [n for n in nodes if n["ticker"] in wanted]

    all_rows = []
    t_start = time.time()
    for i, node in enumerate(nodes):
        ticker = node["ticker"]
        t0 = time.time()
        try:
            rows, baseline_trades = run_ticker(node, args.trials, seed=args.seed + i)
        except FileNotFoundError:
            print(f"  [skip] {ticker}: no cached hourly data")
            continue
        all_rows.extend(rows)
        elapsed = time.time() - t0
        caveat = " (entry_timing=open_check -- indicative only, mirror is close-only)" if node["entry_timing"] != "close" else ""
        print(f"{ticker}: baseline={rows[0]['baseline_pct']:+.1f}%  "
              f"({len(baseline_trades)} trades, {elapsed:.1f}s){caveat}")
        for r in rows:
            print(f"    {r['mode']:5s} miss={r['miss_rate']*100:4.0f}%  "
                  f"mean={r['mean_pct']:+9.1f}%  median={r['median_pct']:+9.1f}%  "
                  f"p10={r['p10_pct']:+9.1f}%  p90={r['p90_pct']:+9.1f}%  "
                  f"ratio_vs_baseline={r['mean_vs_baseline_ratio']:.3f}")

    print(f"\nTotal wall time: {time.time() - t_start:.1f}s")

    out = pd.DataFrame(all_rows)
    OUTPUT_DIR.mkdir(exist_ok=True)
    csv_path = OUTPUT_DIR / "chaos_monkey_summary.csv"
    out.to_csv(csv_path, index=False)
    print(f"Wrote {csv_path}")

    if not out.empty:
        make_chart(out, OUTPUT_DIR / "chaos_monkey_chart.png")


def make_chart(df, out_path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    tickers = sorted(df["ticker"].unique())
    n = len(tickers)
    ncols = 4
    nrows = -(-n // ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(4.2 * ncols, 3.2 * nrows), squeeze=False)

    colors = {"drop": "#4C6EF5", "delay": "#F08C00"}  # categorical, fixed order

    for idx, ticker in enumerate(tickers):
        ax = axes[idx // ncols][idx % ncols]
        sub = df[df["ticker"] == ticker].sort_values("miss_rate")
        baseline = sub["baseline_pct"].iloc[0]
        ax.axhline(baseline, color="#868E96", linestyle="--", linewidth=1.5, label="baseline (perfect)")
        for mode in MODES:
            m = sub[sub["mode"] == mode]
            ax.plot(m["miss_rate"] * 100, m["mean_pct"], marker="o", markersize=4,
                    linewidth=2, color=colors[mode], label=mode)
            ax.fill_between(m["miss_rate"] * 100, m["p10_pct"], m["p90_pct"],
                             color=colors[mode], alpha=0.15, linewidth=0)
        ax.set_title(ticker, fontsize=10, fontweight="bold")
        ax.set_xlabel("miss rate %", fontsize=8)
        ax.set_ylabel("compounded return %", fontsize=8)
        ax.tick_params(labelsize=7)
        ax.grid(True, color="#E9ECEF", linewidth=0.6)
        for spine in ["top", "right"]:
            ax.spines[spine].set_visible(False)

    for idx in range(n, nrows * ncols):
        axes[idx // ncols][idx % ncols].set_visible(False)

    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=3, fontsize=9, frameon=False,
               bbox_to_anchor=(0.5, 1.02))
    fig.suptitle("Chaos monkey: compounded return vs. signal miss rate (shaded = p10-p90)",
                 fontsize=12, y=1.06)
    fig.tight_layout()
    fig.savefig(out_path, dpi=140, bbox_inches="tight")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
