"""Max drawdown (true peak-to-trough, not just consecutive-loss streaks) for
every watchlist ticker's real v4 winning node (stop_loss=1%, entry_timing=
'open_check'). Walks the full compounded equity curve trade-by-trade and
tracks the running peak vs. every subsequent point -- the standard max-
drawdown definition, not an approximation from streak length alone (a
drawdown can also be built from a mix of wins-that-don't-recover-the-prior-
peak and losses, not just an unbroken loss streak).

Usage: .venv/bin/python scripts/v4_max_drawdown.py [TICKER ...]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3
import pandas as pd

import strategies
from backtester import run_backtest_v110
from run_optimization_sweep import _load_node_inputs
from candidate_checklist_report import best_v4_node, RESEARCH_DB

DEFAULT_TICKERS = ["AGQ", "DPST", "EDC", "GDXD", "GDXU", "HIBL",
                    "KORU", "LABU", "NUGT", "SOXL", "TQQQ", "YANG"]


def max_drawdown(trades):
    equity = 100.0
    peak = 100.0
    peak_time = None
    max_dd = 0.0
    dd_peak_time = dd_trough_time = None
    for t in trades:
        if t["Result"] == "OPEN":
            continue
        if peak_time is None:
            peak_time = t["Entry Time"]
        equity *= (1.0 + t["Return"])
        if equity >= peak:
            peak = equity
            peak_time = t["Exit Time"]
        else:
            dd = (equity - peak) / peak
            if dd < max_dd:
                max_dd = dd
                dd_peak_time = peak_time
                dd_trough_time = t["Exit Time"]
    return max_dd * 100.0, dd_peak_time, dd_trough_time


def run(tickers):
    rows = []
    with sqlite3.connect(RESEARCH_DB) as conn:
        for ticker in tickers:
            node = best_v4_node(conn, ticker)
            if node is None:
                rows.append(dict(ticker=ticker, note="no v4 SL=1/open_check node"))
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
            dd_pct, dd_start, dd_end = max_drawdown(trades)
            rows.append(dict(ticker=ticker, trades=len([t for t in trades if t["Result"] != "OPEN"]),
                              max_drawdown_pct=round(dd_pct, 2), dd_start=dd_start, dd_end=dd_end))
    return pd.DataFrame(rows)


if __name__ == "__main__":
    tickers = sys.argv[1:] or DEFAULT_TICKERS
    df = run(tickers)
    df = df.sort_values("max_drawdown_pct")
    pd.set_option("display.width", 200)
    print(df.to_string(index=False))
