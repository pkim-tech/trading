"""
Research script: out-of-sample check for the entry-timing-seasonality finding
(docs/research_log.md's 2026-08-03 entries) -- GDXD, UVIX, and DPST (v4 config) showed a
statistically significant morning-vs-afternoon return difference across their full ~3-year history.
Does that hold up if the history is split chronologically into two halves and each half is tested
independently? A pattern that only shows up on the full combined sample and vanishes/reverses in
either half is much more likely to be an artifact of one unusual stretch than a real, stable effect
-- same rationale as scripts/walk_forward_check.py's existing N-fold convention (calendar-time folds,
not trade-count folds).

Usage: .venv/bin/python scripts/entry_timing_walkforward_check.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import mannwhitneyu

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtester import prep_inputs
import strategies
from scripts.export_trades import load_hourly, simulate_trail_both_annotated
from scripts.entry_timing_seasonality import load_nodes, TARGET_H0, TARGET_H1

TICKERS = ["GDXD", "UVIX", "DPST"]
N_FOLDS = 2


def get_trades_with_time(node):
    df_h = load_hourly(node["ticker"])
    df_daily = df_h.resample("D").last().dropna(subset=["Close"])
    strat_cls = getattr(strategies, node["strategy"])
    ind = strat_cls(window=node["window"]).generate_daily_indicators(df_daily)
    p = prep_inputs(df_h, ind)

    trades = simulate_trail_both_annotated(
        p, node["arm_pct"] / 100.0, node["fixed_sl"] / 100.0, node["max_hold_hours"],
        node["trail_buy_pct"] / 100.0, node["trail_sell_pct"] / 100.0,
        TARGET_H0, TARGET_H1, node["z"],
    )
    timestamps = p["timestamps"]
    out = []
    for t in trades:
        if t["signal_i"] is None:
            continue
        out.append({
            "hour": timestamps[t["signal_i"]].hour,
            "time": timestamps[t["signal_i"]],
            "ret": t["ret"],
        })
    return out


def fold_stats(trades):
    g0 = [t["ret"] for t in trades if t["hour"] == TARGET_H0]
    g1 = [t["ret"] for t in trades if t["hour"] == TARGET_H1]
    if len(g0) < 5 or len(g1) < 5:
        return {"n0": len(g0), "n1": len(g1), "mean0": None, "mean1": None, "p": None}
    _, p = mannwhitneyu(g0, g1, alternative="two-sided")
    return {
        "n0": len(g0), "mean0": round(np.mean(g0), 4),
        "n1": len(g1), "mean1": round(np.mean(g1), 4),
        "p": round(p, 4),
    }


def main():
    nodes = {n["ticker"]: n for n in load_nodes(57)}
    rows = []
    for ticker in TICKERS:
        node = nodes[ticker]
        trades = get_trades_with_time(node)
        times = [t["time"] for t in trades]
        start, end = min(times), max(times)
        boundaries = pd.date_range(start, end, periods=N_FOLDS + 1)

        full = fold_stats(trades)
        rows.append({"ticker": ticker, "fold": "FULL", "period": f"{start.date()}..{end.date()}", **full})

        for i in range(N_FOLDS):
            fstart, fend = boundaries[i], boundaries[i + 1]
            fold_trades = [t for t in trades if fstart <= t["time"] <= fend]
            stats = fold_stats(fold_trades)
            rows.append({"ticker": ticker, "fold": f"{i+1}/{N_FOLDS}",
                         "period": f"{fstart.date()}..{fend.date()}", **stats})

    df = pd.DataFrame(rows)
    pd.set_option("display.width", 160)
    print(df.to_string(index=False))
    print("\nmean0/mean1 = mean return for 9:30-bar / 14:30-bar signals in that period. "
          "p<0.05 = significant difference within that fold alone.")


if __name__ == "__main__":
    main()
