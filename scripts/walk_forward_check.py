"""Walk-forward / N-fold out-of-time consistency check for v4 winning nodes --
generalizes train_test_split_check.py's single 70/30 split into N equal
chronological (calendar-time, not trade-count) windows across each ticker's
full cached history, and reports robust-alpha (MIN of possible/pessimistic/
certain) separately per window.

Why this exists: a single 70/30 split can't distinguish "real robust edge" from
"got lucky on whichever window landed after the cut" -- KORU's 70/30 result
(2026-07-18) showed a >5x out-of-sample improvement driven almost entirely by
one outlier trade, which a single split has no way to flag as fragile. Slicing
into N windows instead surfaces the full distribution: a node with a real edge
should show broadly consistent (mostly positive, no wild single-window swings)
robust-alpha across windows; a node that's overfit or got lucky in one window
will show high dispersion or negative windows.

No re-sweep, no backtest_cache schema change -- same "run once, slice the
already-computed trade list" approach as train_test_split_check.py, extended
from 2 windows to N. Each window's SPY benchmark is computed over that
window's own calendar date range (period_spy_bh), not the full-history number.

Usage: .venv/bin/python scripts/walk_forward_check.py TICKER [TICKER ...] [--folds N] [out.csv]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3
import numpy as np
import pandas as pd

import strategies
from backtester import run_backtest_v110
from run_optimization_sweep import _load_node_inputs, _summarize_trades, CACHE_DIR
from candidate_checklist_report import best_v4_node, RESEARCH_DB
from train_test_split_check import period_spy_bh

CLOSED = ["WIN", "LOSS", "TWIN", "TLOSS"]
STRATEGY_NAME = "TrailingBothZScoreBreakout"
DEFAULT_FOLDS = 5


def _fold_metrics(closed_list, start, end, spy_bh):
    if not closed_list:
        return None
    df = pd.DataFrame(closed_list)
    sub = df[(df["Entry Time"] >= start) & (df["Entry Time"] < end)]
    if sub.empty:
        return dict(n=0, alpha=None, compounded=None)
    alpha, n, win_rate, compounded, win_twin_rate = _summarize_trades(sub.to_dict("records"), spy_bh)
    return dict(n=n, alpha=alpha, compounded=compounded, win_rate=win_rate)


def walk_forward(t_base, p_base, c_base, data_start, data_end, n_folds):
    closed_base = [t for t in t_base if t["Result"] in CLOSED]
    closed_pess = [t for t in p_base if t["Result"] in CLOSED] if p_base else []
    closed_cert = [t for t in c_base if t["Result"] in CLOSED] if c_base else []
    if not closed_base:
        return None

    boundaries = pd.date_range(data_start, data_end, periods=n_folds + 1)
    rows = []
    for i in range(n_folds):
        start, end = boundaries[i], boundaries[i + 1]
        spy_bh = period_spy_bh(start, end)
        base_m = _fold_metrics(closed_base, start, end, spy_bh)
        pess_m = _fold_metrics(closed_pess, start, end, spy_bh)
        cert_m = _fold_metrics(closed_cert, start, end, spy_bh)
        if base_m is None or base_m["n"] == 0:
            rows.append(dict(fold=i + 1, start=start, end=end, n=0,
                              robust_alpha=None, compounded=None))
            continue
        alphas = [base_m["alpha"]]
        if pess_m and pess_m["n"] > 0:
            alphas.append(pess_m["alpha"])
        if cert_m and cert_m["n"] > 0:
            alphas.append(cert_m["alpha"])
        rows.append(dict(fold=i + 1, start=start, end=end, n=base_m["n"],
                          robust_alpha=min(alphas), compounded=base_m["compounded"]))
    return rows


def summarize(rows):
    valid = [r["robust_alpha"] for r in rows if r["robust_alpha"] is not None]
    if not valid:
        return dict(folds_with_trades=0)
    return dict(
        folds_with_trades=len(valid),
        min_fold_alpha=min(valid),
        max_fold_alpha=max(valid),
        mean_fold_alpha=float(np.mean(valid)),
        std_fold_alpha=float(np.std(valid)),
        negative_folds=sum(1 for a in valid if a < 0),
    )


def run_ticker(conn, ticker, n_folds):
    node = best_v4_node(conn, ticker)
    if node is None:
        return [dict(ticker=ticker, note="no v4 SL=1/open_check node found")]

    strategy_class = strategies.TrailingBothZScoreBreakout
    inputs = _load_node_inputs(ticker, strategy_class, STRATEGY_NAME, node["window"], node["z"])
    if inputs is None:
        return [dict(ticker=ticker, note="no cached hourly data")]
    _, _, prep = inputs

    hourly_path = CACHE_DIR / f"{ticker}_1h.csv"
    df_t = pd.read_csv(hourly_path, index_col=0, parse_dates=True).sort_index()
    if df_t.index.tz is not None:
        df_t.index = df_t.index.tz_localize(None)
    data_start, data_end = df_t.index.min(), df_t.index.max()

    t_base, p_base, c_base = run_backtest_v110(
        df_hourly=None, df_daily_indicators=None, ticker=ticker,
        take_profit=node["arm"] / 100.0, stop_loss=0.01,
        max_hours_to_hold=node["hold"], z_score_threshold=node["z"],
        trail_buy_pct=node["tb"] / 100.0, trail_pct=node["ts"] / 100.0,
        entry_timing="open_check", return_bounds=True, prep=prep, same_day_block=False,
    )

    fold_rows = walk_forward(t_base, p_base, c_base, data_start, data_end, n_folds)
    if fold_rows is None:
        return [dict(ticker=ticker, note="no closed trades")]

    summary = summarize(fold_rows)
    out = []
    for r in fold_rows:
        row = dict(ticker=ticker, **r)
        row.update(summary)
        out.append(row)
    return out


def run(tickers, n_folds):
    rows = []
    with sqlite3.connect(RESEARCH_DB, timeout=60) as conn:
        for ticker in tickers:
            print(f"[{ticker}] running {n_folds}-fold walk-forward check...", file=sys.stderr)
            try:
                rows.extend(run_ticker(conn, ticker, n_folds))
            except Exception as e:
                rows.append(dict(ticker=ticker, note=f"error: {e!r}"))
    return pd.DataFrame(rows)


if __name__ == "__main__":
    args = sys.argv[1:]
    n_folds = DEFAULT_FOLDS
    if "--folds" in args:
        idx = args.index("--folds")
        n_folds = int(args[idx + 1])
        del args[idx:idx + 2]
    out_path = "logs/walk_forward_check.csv"
    if args and args[-1].endswith(".csv"):
        out_path = args.pop()
    tickers = args
    if not tickers:
        print(__doc__)
        sys.exit(1)
    df = run(tickers, n_folds)
    df.to_csv(out_path, index=False)
    print(f"\nWrote {len(df)} rows to {out_path}")
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 50)
    print(df.to_string(index=False))
