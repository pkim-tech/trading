"""
Research script: does the SHAPE of a trade's z-score approach into the entry trigger matter,
not just that it crossed the threshold? Raised 2026-08-04 evening while reasoning about SOXL's
weak uptrend performance vs. AGQ's strength in AGQ's downturns -- the working hypothesis is that
the strategy's edge tracks volatility/churn (oscillation around the mean) more than raw price
direction, and that a *sharp*, fast breach (one violent bar blowing through the trigger, more
crash-like) may behave differently than a *gradual* multi-bar grind across it (more chop-like).

For each real trade (using the same trade-simulation mirrors of the live numba kernel that
scripts/entry_timing_seasonality.py already trusts -- simulate_trail_both_annotated /
simulate_trail_exit_chaos at zero miss rate), reconstructs the full z-score series for the
ticker and measures, at the entry signal bar:
  - overshoot: how far past the trigger threshold the signal fired (-z_thresh - signal_z;
    positive = breached harder than the minimum needed)
  - velocity_3bar: z[signal] - z[signal-3], i.e. how much z moved in the 3 bars immediately
    before the signal (more negative = sharper/faster drop into the trigger)
tags each trade sharp vs. gradual by velocity tercile, and reports win rate / mean return per
group (Mann-Whitney on returns, Fisher exact on win/loss) alongside Spearman correlations of
both overshoot and velocity against trade return -- plus prints the full trade-by-trade table
for manual eyeballing (the "look carefully at AGQ" ask), not just the aggregate stats.

This is a first pass, meant to be iterated on -- see docs/backlog_cache.md.

Usage: .venv/bin/python scripts/z_entry_velocity_audit.py [--tickers AGQ ...] [--watchlist-id 65]
       [--velocity-lag 3] [--csv]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import fisher_exact, mannwhitneyu, spearmanr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtester import prep_inputs, OPEN
import strategies
from scripts.export_trades import (
    load_hourly,
    simulate_trail_both_annotated,
    simulate_trail_exit_chaos,
)

LIVE_DB = Path("cache/live/trading_live.db")
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
TARGET_H0, TARGET_H1 = 9, 14


def load_nodes(watchlist_id, tickers=None):
    con = sqlite3.connect(LIVE_DB)
    q = """
        SELECT MIN(id), ticker, strategy, window, z_score_threshold, arm_sell_pct, take_profit,
               fixed_sl, trail_buy_pct, trail_sell_pct, max_hold_hours, entry_timing
        FROM watch_list WHERE watchlist_id=? AND mode='research'
    """
    params = [watchlist_id]
    if tickers:
        q += f" AND ticker IN ({','.join('?' * len(tickers))})"
        params += tickers
    q += " GROUP BY ticker"
    rows = con.execute(q, params).fetchall()
    con.close()
    cols = ["id", "ticker", "strategy", "window", "z", "arm_sell_pct", "take_profit", "fixed_sl",
            "trail_buy_pct", "trail_sell_pct", "max_hold_hours", "entry_timing"]
    nodes = [dict(zip(cols, r)) for r in rows]
    # take_profit holds the arm-sell threshold for TrailingExitZScoreBreakout nodes;
    # arm_sell_pct holds it for TrailingBothZScoreBreakout -- the two are never both
    # populated on the same row (signals_db.py:983-988). Unify into one field so
    # downstream code doesn't have to special-case it (the earlier bug: assuming
    # arm_sell_pct alone covered both strategies, silently disabling TP for every
    # TrailingExit node -- see docs/research_log.md's 2026-08-04 correction entry).
    for n in nodes:
        n["arm_pct"] = n["arm_sell_pct"] if n["strategy"] == "TrailingBothZScoreBreakout" else n["take_profit"]
    return nodes


def build_indicators(strategy_name, df_daily, window):
    strat_cls = getattr(strategies, strategy_name)
    strat = strat_cls(window=window)
    return strat.generate_daily_indicators(df_daily)


def get_trades_and_zseries(node):
    df_h = load_hourly(node["ticker"])
    df_daily = df_h.resample("D").last().dropna(subset=["Close"])
    ind = build_indicators(node["strategy"], df_daily, node["window"])
    p = prep_inputs(df_h, ind)

    open_check = node["entry_timing"] == "open_check"
    if node["strategy"] == "TrailingBothZScoreBreakout":
        trades = simulate_trail_both_annotated(
            p, node["arm_pct"] / 100.0, node["fixed_sl"] / 100.0, node["max_hold_hours"],
            node["trail_buy_pct"] / 100.0, node["trail_sell_pct"] / 100.0,
            TARGET_H0, TARGET_H1, node["z"], open_check=open_check,
        )
    elif node["strategy"] == "TrailingExitZScoreBreakout":
        rng = np.random.default_rng(0)
        trades = simulate_trail_exit_chaos(
            p, node["arm_pct"] / 100.0, node["fixed_sl"] / 100.0, node["max_hold_hours"],
            node["trail_sell_pct"] / 100.0, TARGET_H0, TARGET_H1, node["z"],
            rng, "drop", 0.0, "drop", 0.0, open_check=open_check,
        )
    else:
        raise ValueError(f"unhandled strategy {node['strategy']}")

    # z-score at every bar, matching the kernel's own (price - sma) / std definition
    # (backtester.prep_inputs' daily_idx already maps each hourly bar to the most
    # recently *completed* day's SMA/Std -- see that function's docstring).
    daily_idx, sma_arr, std_arr, prices = p["daily_idx"], p["sma_arr"], p["std_arr"], p["prices"]
    valid = daily_idx >= 0
    z = np.full(len(prices), np.nan)
    z[valid] = (prices[valid] - sma_arr[daily_idx[valid]]) / std_arr[daily_idx[valid]]

    return trades, z, p["timestamps"], node["z"]


def annotate(trades, z, timestamps, z_thresh, velocity_lag, ticker):
    rows = []
    for t in trades:
        si = t["signal_i"]
        if si is None or si < velocity_lag or t["result"] == OPEN:
            continue
        # Use the trade's own recorded signal_z (the exact fired value -- Open-based
        # when open_check fired on the Open, Close-based otherwise), not a recomputed
        # Close-only z[si], which would be wrong for open_check fires.
        signal_z = t["signal_z"]
        lag_z = z[si - velocity_lag]
        if signal_z is None or np.isnan(signal_z) or np.isnan(lag_z):
            continue
        rows.append({
            "ticker": ticker,
            "signal_time": timestamps[si],
            "exit_time": timestamps[t["exit_i"]],
            "signal_z": round(float(signal_z), 3),
            "overshoot": round(float(-z_thresh - signal_z), 3),
            "velocity": round(float(signal_z - lag_z), 3),
            "held_hours": t["held"],
            "result": "WIN" if t["ret"] > 0 else "LOSS",
            "ret": round(float(t["ret"]), 4),
        })
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="*", default=["AGQ"])
    parser.add_argument("--watchlist-id", type=int, default=65)
    parser.add_argument("--velocity-lag", type=int, default=3)
    parser.add_argument("--csv", action="store_true")
    args = parser.parse_args()

    nodes = load_nodes(args.watchlist_id, args.tickers)
    all_rows = []
    for node in nodes:
        try:
            trades, z, timestamps, z_thresh = get_trades_and_zseries(node)
        except Exception as e:
            print(f"{node['ticker']}: failed ({e})")
            continue
        rows = annotate(trades, z, timestamps, z_thresh, args.velocity_lag, node["ticker"])
        all_rows.extend(rows)

    if not all_rows:
        print("No trades found.")
        return

    df = pd.DataFrame(all_rows)
    pd.set_option("display.width", 160)
    pd.set_option("display.max_rows", None)
    print(df.to_string(index=False))

    if args.csv:
        OUTPUT_DIR.mkdir(exist_ok=True)
        out_path = OUTPUT_DIR / "z_entry_velocity_audit.csv"
        df.to_csv(out_path, index=False)
        print(f"\nWrote {out_path}")

    print("\n--- Correlations (velocity/overshoot vs. return) ---")
    for ticker, g in df.groupby("ticker"):
        if len(g) < 5:
            print(f"{ticker}: n={len(g)}, too few trades to correlate")
            continue
        rho_v, p_v = spearmanr(g["velocity"], g["ret"])
        rho_o, p_o = spearmanr(g["overshoot"], g["ret"])
        print(f"{ticker}: n={len(g)}  velocity vs ret: rho={rho_v:.3f} p={p_v:.3f}  "
              f"overshoot vs ret: rho={rho_o:.3f} p={p_o:.3f}")

    if len(df["ticker"].unique()) > 1:
        rho_v, p_v = spearmanr(df["velocity"], df["ret"])
        rho_o, p_o = spearmanr(df["overshoot"], df["ret"])
        print(f"Pooled: n={len(df)}  velocity vs ret: rho={rho_v:.3f} p={p_v:.3f}  "
              f"overshoot vs ret: rho={rho_o:.3f} p={p_o:.3f}")

    print("\n--- Sharp vs. gradual (velocity tercile split) ---")
    for ticker, g in df.groupby("ticker"):
        if len(g) < 9:
            print(f"{ticker}: n={len(g)}, too few trades to tercile-split")
            continue
        lo, hi = g["velocity"].quantile([1 / 3, 2 / 3])
        sharp = g[g["velocity"] <= lo]       # steepest drop into the trigger
        gradual = g[g["velocity"] >= hi]     # calmest approach
        if len(sharp) < 2 or len(gradual) < 2:
            continue
        w_s, w_g = (sharp["ret"] > 0).sum(), (gradual["ret"] > 0).sum()
        _, mw_p = mannwhitneyu(sharp["ret"], gradual["ret"], alternative="two-sided")
        _, fisher_p = fisher_exact([[w_s, len(sharp) - w_s], [w_g, len(gradual) - w_g]])
        print(f"{ticker}: sharp n={len(sharp)} winrate={w_s/len(sharp):.3f} mean_ret={sharp['ret'].mean():.4f}"
              f"  |  gradual n={len(gradual)} winrate={w_g/len(gradual):.3f} mean_ret={gradual['ret'].mean():.4f}"
              f"  |  Mann-Whitney p={mw_p:.3f}  Fisher p={fisher_p:.3f}")


if __name__ == "__main__":
    main()
