"""
Research script: does the entry signal's time-of-day matter? Live trading only checks two fixed
daily windows (10:25-10:40 ET, catching the 9:30 bar close; 15:25-15:40 ET, catching the 14:30 bar
close) -- never tested whether one is actually better than the other, just always used both.

For each of the 10 real v5 watchlist tickers, runs the same trade-simulation code the live sweep
engine trusts (scripts/export_trades.py's annotated/chaos-with-zero-miss-rate mirrors of the real
numba kernel, not a new simulator), tags every trade by which of the two daily windows fired its
entry signal, and compares outcomes (win rate, mean return) between the two groups per ticker and
pooled -- using a Mann-Whitney U test on returns and a Fisher exact test on win/loss counts, not
just eyeballing the numbers.

Usage: .venv/bin/python scripts/entry_timing_seasonality.py
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import fisher_exact, mannwhitneyu

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtester import prep_inputs, OPEN
import strategies
from scripts.export_trades import (
    load_hourly,
    simulate_trail_both_annotated,
    simulate_trail_exit_chaos,
)

LIVE_DB = Path("cache/live/trading_live.db")
TARGET_H0, TARGET_H1 = 9, 14  # the two live signal-window bar anchors


def load_nodes(watchlist_id=65):
    con = sqlite3.connect(LIVE_DB)
    rows = con.execute("""
        SELECT MIN(id), ticker, strategy, window, z_score_threshold, arm_sell_pct, take_profit,
               fixed_sl, trail_buy_pct, trail_sell_pct, max_hold_hours, entry_timing
        FROM watch_list WHERE watchlist_id=? AND state='paper'
        GROUP BY ticker
    """, (watchlist_id,)).fetchall()
    con.close()
    cols = ["id", "ticker", "strategy", "window", "z", "arm_sell_pct", "take_profit", "fixed_sl",
            "trail_buy_pct", "trail_sell_pct", "max_hold_hours", "entry_timing"]
    nodes = [dict(zip(cols, r)) for r in rows]
    # take_profit holds the arm-sell threshold for TrailingExitZScoreBreakout nodes;
    # arm_sell_pct holds it for TrailingBothZScoreBreakout (never both populated on
    # the same row -- signals_db.py:983-988). Corrected 2026-08-04 -- this script
    # previously hardcoded TP=disabled for every TrailingExit node and never passed
    # open_check, both silently wrong for every real v5 node. See
    # docs/research_log.md's 2026-08-04 correction entry -- this script's own
    # 2026-08-03 published finding needs re-verification under the fix.
    for n in nodes:
        n["arm_pct"] = n["arm_sell_pct"] if n["strategy"] == "TrailingBothZScoreBreakout" else n["take_profit"]
    return nodes


def build_indicators(strategy_name, df_daily, window):
    strat_cls = getattr(strategies, strategy_name)
    strat = strat_cls(window=window)
    return strat.generate_daily_indicators(df_daily)


def get_trades(node):
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

    timestamps = p["timestamps"]
    out = []
    for t in trades:
        if t["signal_i"] is None or t["result"] == OPEN:
            continue
        hour = timestamps[t["signal_i"]].hour
        out.append({"hour": hour, "ret": t["ret"], "win": t["ret"] > 0})
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watchlist-id", type=int, default=65)
    args = parser.parse_args()
    nodes = load_nodes(args.watchlist_id)
    per_ticker_rows = []
    pooled = {TARGET_H0: [], TARGET_H1: []}

    for node in nodes:
        try:
            trades = get_trades(node)
        except Exception as e:
            print(f"{node['ticker']}: failed ({e})")
            continue

        g0 = [t for t in trades if t["hour"] == TARGET_H0]
        g1 = [t for t in trades if t["hour"] == TARGET_H1]
        if not g0 or not g1:
            print(f"{node['ticker']}: only one signal-hour group present, skipping ({len(g0)} vs {len(g1)})")
            continue

        r0, r1 = [t["ret"] for t in g0], [t["ret"] for t in g1]
        w0, w1 = sum(t["win"] for t in g0), sum(t["win"] for t in g1)

        _, mw_p = mannwhitneyu(r0, r1, alternative="two-sided")
        _, fisher_p = fisher_exact([[w0, len(g0) - w0], [w1, len(g1) - w1]])

        pooled[TARGET_H0].extend(r0)
        pooled[TARGET_H1].extend(r1)

        per_ticker_rows.append({
            "ticker": node["ticker"],
            "n_h0": len(g0), "winrate_h0": round(w0 / len(g0), 3), "mean_ret_h0": round(np.mean(r0), 4),
            "n_h1": len(g1), "winrate_h1": round(w1 / len(g1), 3), "mean_ret_h1": round(np.mean(r1), 4),
            "mannwhitney_p": round(mw_p, 3), "fisher_p": round(fisher_p, 3),
        })

    df = pd.DataFrame(per_ticker_rows)
    pd.set_option("display.width", 160)
    print(df.to_string(index=False))

    r0, r1 = pooled[TARGET_H0], pooled[TARGET_H1]
    w0, w1 = sum(x > 0 for x in r0), sum(x > 0 for x in r1)
    _, mw_p = mannwhitneyu(r0, r1, alternative="two-sided")
    _, fisher_p = fisher_exact([[w0, len(r0) - w0], [w1, len(r1) - w1]])
    print(f"\nPooled across all tickers: {TARGET_H0}:30 bar (n={len(r0)}, winrate={w0/len(r0):.3f}, "
          f"mean_ret={np.mean(r0):.4f}) vs {TARGET_H1}:30 bar (n={len(r1)}, winrate={w1/len(r1):.3f}, "
          f"mean_ret={np.mean(r1):.4f})")
    print(f"Mann-Whitney p={mw_p:.4f}, Fisher exact p={fisher_p:.4f} (p<0.05 = likely real difference)")


if __name__ == "__main__":
    main()
