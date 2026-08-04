"""Honest out-of-sample check for a single ticker's real backtested CAGR: split the
real cached hourly history chronologically in half, grid-search parameters using ONLY
the first half's compounded return, then report how that exact (unseen-until-now)
combo performs on the untouched second half. Answers "how much of the backtested
edge is real vs curve-fit to the specific window it was picked on."

Distinct from the island/cliff-safety sweep machinery in run_optimization_sweep.py --
this is a small, self-contained research grid (not written to backtest_cache), same
pattern as sim_gap_policy.py / sim_chaos_monkey.py.

Usage: .venv/bin/python scripts/sim_out_of_sample_check.py TICKER [--split 0.5]
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtester import prep_inputs, run_backtest_dispatch
import strategies

CACHE_DIR = Path("cache/research")

WINDOWS = [10, 15, 20]
Z_THRESHOLDS = [1.0, 1.5, 2.0]
FIXED_SLS = [1.0, 2.0, 3.0]
TRAIL_BUY_PCTS = [1.0, 3.0, 5.0]
# held fixed at SOXL's current live values -- full 5-axis grid is out of scope here
TRAIL_SELL_PCT = 1.0
ARM_SELL_PCT = 30.0
MAX_HOLD_HOURS = 70


def load_hourly(ticker):
    df = pd.read_csv(CACHE_DIR / f"{ticker}_1h.csv", index_col=0, parse_dates=True)
    close_col = "Adj Close" if "Adj Close" in df.columns else "Close"
    if close_col != "Close":
        df["Close"] = df[close_col]
    return df


def compounded_return(trades):
    equity = 1.0
    for t in trades:
        equity *= (1.0 + t["Return"])
    return equity - 1.0


def run_full_grid(ticker, df_h, df_daily):
    """One backtest per grid combo over the FULL period; returns list of
    (params_dict, trades) so any fold split can be applied by re-slicing on
    Entry Time without re-running the kernel."""
    out = []
    for window in WINDOWS:
        strat = strategies.TrailingBothZScoreBreakout(window=window, z_score_threshold=2.0)
        ind = strat.generate_daily_indicators(df_daily)
        p = prep_inputs(df_h, ind)
        for z in Z_THRESHOLDS:
            for fixed_sl in FIXED_SLS:
                for trail_buy in TRAIL_BUY_PCTS:
                    trades = run_backtest_dispatch(
                        strategies.TrailingBothZScoreBreakout, df_h, ind, ticker,
                        take_profit=ARM_SELL_PCT, sl_raw=trail_buy, max_hours_to_hold=MAX_HOLD_HOURS,
                        z_score_threshold=z, fixed_sl=fixed_sl, trail_pct_pct=TRAIL_SELL_PCT,
                        entry_timing="open_check", prep=p,
                    )
                    out.append({
                        "params": {"window": window, "z": z, "fixed_sl": fixed_sl, "trail_buy": trail_buy},
                        "trades": trades,
                    })
    return out


def single_split(args, df_h, grid):
    split_ts = df_h.index[0] + (df_h.index[-1] - df_h.index[0]) * args.split
    print(f"Data range: {df_h.index[0].date()} -> {df_h.index[-1].date()}")
    print(f"Train (in-sample): {df_h.index[0].date()} -> {split_ts.date()}")
    print(f"Test (out-of-sample, never used for selection): {split_ts.date()} -> {df_h.index[-1].date()}\n")

    results = []
    for entry in grid:
        train_trades = [t for t in entry["trades"] if t["Entry Time"] < split_ts]
        test_trades = [t for t in entry["trades"] if t["Entry Time"] >= split_ts]
        if len(train_trades) < 5:
            continue
        results.append({
            **entry["params"],
            "train_return": compounded_return(train_trades), "train_trades": len(train_trades),
            "test_return": compounded_return(test_trades), "test_trades": len(test_trades),
        })

    df = pd.DataFrame(results).sort_values("train_return", ascending=False)
    pd.set_option("display.width", 160)
    print("Top 10 by TRAIN (in-sample) compounded return:")
    print(df.head(10).to_string(index=False))

    best = df.iloc[0]
    print(f"\nBest in-sample combo: window={best['window']}, z={best['z']}, "
          f"fixed_sl={best['fixed_sl']}, trail_buy={best['trail_buy']}")
    print(f"  Train (in-sample) return:      {best['train_return']:+.1%} ({best['train_trades']} trades)")
    print(f"  Test (out-of-sample) return:   {best['test_return']:+.1%} ({best['test_trades']} trades)")
    df.to_csv(f"output/oos_check_{args.ticker}.csv", index=False)
    print(f"\nWrote output/oos_check_{args.ticker}.csv")


def walk_forward_folds(args, df_h, grid, n_folds=5):
    """Rolling walk-forward: for each fold k=1..n_folds-1, select the best combo
    using ONLY trades whose entry falls before fold k's start (all history so far,
    not just the prior fold), then report that combo's return restricted to fold k
    itself (never used for selection)."""
    t0, t1 = df_h.index[0], df_h.index[-1]
    bounds = [t0 + (t1 - t0) * (i / n_folds) for i in range(n_folds + 1)]
    print(f"Data range: {t0.date()} -> {t1.date()}, {n_folds} folds, walk-forward "
          f"(train = all history before the fold, test = the fold itself, never seen)\n")

    fold_results = []
    for k in range(1, n_folds):
        train_end, test_start, test_end = bounds[k], bounds[k], bounds[k + 1]
        best_combo, best_train_ret = None, -float("inf")
        for entry in grid:
            train_trades = [t for t in entry["trades"] if t["Entry Time"] < train_end]
            if len(train_trades) < 5:
                continue
            r = compounded_return(train_trades)
            if r > best_train_ret:
                best_train_ret, best_combo = r, entry

        test_trades = [t for t in best_combo["trades"]
                        if test_start <= t["Entry Time"] < test_end]
        test_ret = compounded_return(test_trades)
        fold_results.append({
            "fold": k, "test_window": f"{test_start.date()}..{test_end.date()}",
            **best_combo["params"], "train_return": best_train_ret,
            "test_return": test_ret, "test_trades": len(test_trades),
        })
        print(f"Fold {k} ({test_start.date()} -> {test_end.date()}): selected "
              f"window={best_combo['params']['window']} z={best_combo['params']['z']} "
              f"fixed_sl={best_combo['params']['fixed_sl']} trail_buy={best_combo['params']['trail_buy']} "
              f"(train return {best_train_ret:+.1%}) -> held-out test return {test_ret:+.1%} "
              f"({len(test_trades)} trades)")

    df = pd.DataFrame(fold_results)
    avg_test = df["test_return"].mean()
    total_test_trades = df["test_trades"].sum()
    print(f"\nAverage held-out test return across {n_folds - 1} folds: {avg_test:+.1%} "
          f"({total_test_trades} total out-of-sample trades)")
    df.to_csv(f"output/oos_walkforward_{args.ticker}.csv", index=False)
    print(f"Wrote output/oos_walkforward_{args.ticker}.csv")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--split", type=float, default=0.5, help="fraction of the period used as train")
    ap.add_argument("--folds", type=int, default=None, help="run rolling walk-forward with N folds instead of a single split")
    args = ap.parse_args()

    df_h = load_hourly(args.ticker)
    df_daily = df_h.resample("D").last().dropna(subset=["Close"])
    grid = run_full_grid(args.ticker, df_h, df_daily)

    if args.folds:
        walk_forward_folds(args, df_h, grid, n_folds=args.folds)
    else:
        single_split(args, df_h, grid)


if __name__ == "__main__":
    main()
