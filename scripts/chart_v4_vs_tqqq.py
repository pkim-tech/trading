"""Visualize each watchlist ticker's real v4 winning node (stop_loss=1%,
entry_timing='open_check') compounded equity curve against TQQQ buy-and-hold
over the same date range -- one small-multiple subplot per ticker, both curves
indexed to 100 at the start of that ticker's own data range, log-scale y (the
strategy curves span orders of magnitude more than buy-and-hold, so a linear
scale would flatten everything but the biggest winner -- see dataviz skill's
"index to a common base" guidance for mismatched-scale series).

Usage: .venv/bin/python scripts/chart_v4_vs_tqqq.py [TICKER ...]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

import strategies
from backtester import run_backtest_v110
from run_optimization_sweep import _load_node_inputs
from candidate_checklist_report import best_v4_node, RESEARCH_DB

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "research"
STRATEGY_COLOR = "#2a78d6"   # categorical slot 1 (blue)
BENCHMARK_COLOR = "#52514e"  # text-secondary neutral, role = comparison baseline

DEFAULT_TICKERS = ["AGQ", "DPST", "EDC", "GDXD", "GDXU", "HIBL",
                    "KORU", "LABU", "NUGT", "SOXL", "TQQQ", "YANG"]


def equity_curve(trades):
    """Step function: 100 at first entry, compounds at each trade's exit time."""
    times, vals = [], []
    equity = 100.0
    for t in trades:
        if t["Result"] == "OPEN":
            continue
        times.append(t["Entry Time"])
        vals.append(equity)
        equity *= (1.0 + t["Return"])
        times.append(t["Exit Time"])
        vals.append(equity)
    return pd.Series(vals, index=pd.to_datetime(times))


def tqqq_bh_curve(start, end):
    df = pd.read_csv(CACHE_DIR / "TQQQ_1h.csv", index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df.sort_index()
    close_col = "Adj Close" if "Adj Close" in df.columns else "Close"
    sliced = df.loc[start:end, close_col]
    if sliced.empty:
        return None
    return (sliced / sliced.iloc[0]) * 100.0


def run(tickers):
    with sqlite3.connect(RESEARCH_DB) as conn:
        curves = {}
        for ticker in tickers:
            node = best_v4_node(conn, ticker)
            if node is None:
                print(f"[skip] {ticker}: no v4 SL=1/open_check node", file=sys.stderr)
                continue
            inputs = _load_node_inputs(ticker, strategies.TrailingBothZScoreBreakout,
                                        "TrailingBothZScoreBreakout", node["window"], node["z"])
            if inputs is None:
                continue
            _, _, prep = inputs
            trades = run_backtest_v110(
                None, None, ticker, take_profit=node["arm"] / 100.0, stop_loss=0.01,
                max_hours_to_hold=node["hold"], z_score_threshold=node["z"],
                trail_buy_pct=node["tb"] / 100.0, trail_pct=node["ts"] / 100.0,
                entry_timing="open_check", prep=prep)
            eq = equity_curve(trades)
            if eq.empty:
                continue
            bh = tqqq_bh_curve(eq.index.min(), eq.index.max())
            curves[ticker] = (eq, bh)
    return curves


def make_chart(curves, out_path):
    tickers = sorted(curves.keys())
    n = len(tickers)
    ncols = 4
    nrows = -(-n // ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(4.5 * ncols, 3.3 * nrows), squeeze=False)

    for idx, ticker in enumerate(tickers):
        ax = axes[idx // ncols][idx % ncols]
        eq, bh = curves[ticker]
        ax.plot(eq.index, eq.values, color=STRATEGY_COLOR, linewidth=2, label="v4 strategy")
        if bh is not None:
            ax.plot(bh.index, bh.values, color=BENCHMARK_COLOR, linewidth=1.5,
                     linestyle="--", label="TQQQ buy-hold")
        ax.set_yscale("log")
        ax.set_title(ticker, fontsize=10, fontweight="bold")
        ax.tick_params(labelsize=7)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%y-%m"))
        ax.grid(True, which="major", color="#E9ECEF", linewidth=0.6)
        for spine in ["top", "right"]:
            ax.spines[spine].set_visible(False)

    for idx in range(n, nrows * ncols):
        axes[idx // ncols][idx % ncols].set_visible(False)

    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=2, fontsize=10, frameon=False,
               bbox_to_anchor=(0.5, 1.03))
    fig.suptitle("v4 winning node (SL=1%, open_check) vs. TQQQ buy-hold, indexed to 100 (log scale)",
                 fontsize=12, y=1.07)
    fig.tight_layout()
    fig.savefig(out_path, dpi=140, bbox_inches="tight")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    tickers = sys.argv[1:] or DEFAULT_TICKERS
    curves = run(tickers)
    out_path = Path(__file__).resolve().parent.parent / "output" / "v4_vs_tqqq_chart.png"
    out_path.parent.mkdir(exist_ok=True)
    make_chart(curves, out_path)
