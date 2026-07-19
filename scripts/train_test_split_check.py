"""Out-of-sample train/test check for v4 winning nodes (watchlist candidate
checklist check 13): chronologically splits each ticker's already-computed
trade list into a 70% train slice and 30% test slice (per-ticker 70th-
percentile entry-time cutoff, same pattern as checklist_deep_checks.py's
_split_7030/check10), then reports robust-alpha (MIN of possible/pessimistic/
certain) and compounded return separately for each half.

No re-sweep, no backtest_cache schema change: the strategy's SMA/std
indicators are already backward-looking (rolling window over strictly prior
days), so running the backtest once over full history and splitting the
resulting trade list by entry time is numerically equivalent to running two
separate backtests on split date ranges (aside from a negligible boundary
artifact from any position still open at the cutoff).

A node whose test-slice robust-alpha collapses relative to train-slice
robust-alpha is a real overfitting flag -- the whole point of this check.

Benchmark note: compute_bh_returns() returns a single SPY buy-hold return over
a ticker's *entire* cached history -- using that one number as the benchmark
for both the train slice and the test slice would be wrong (the test slice's
alpha needs to be measured against SPY's return during just the test period,
not the whole history). period_spy_bh() below slices SPY's own cached CSV to
the same [start, cut) / [cut, end) date ranges as the trade split instead.

Usage: .venv/bin/python scripts/train_test_split_check.py TICKER [TICKER ...] [out.csv]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3
import pandas as pd

import strategies
from backtester import run_backtest_v110
from run_optimization_sweep import _load_node_inputs, _summarize_trades, CACHE_DIR
from candidate_checklist_report import best_v4_node, RESEARCH_DB

CLOSED = ["WIN", "LOSS", "TWIN", "TLOSS"]
STRATEGY_NAME = "TrailingBothZScoreBreakout"

_SPY_DF = None


def _spy_df():
    global _SPY_DF
    if _SPY_DF is None:
        path = CACHE_DIR / "SPY_1h.csv"
        df = pd.read_csv(path, index_col=0, parse_dates=True).sort_index()
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        _SPY_DF = df
    return _SPY_DF


def period_spy_bh(start, end):
    """SPY buy-hold %% return over [start, end), sliced from SPY's own cached CSV."""
    df = _spy_df()
    sliced = df.loc[start:end]
    if len(sliced) < 2:
        return 0.0
    col = 'Adj Close' if 'Adj Close' in sliced.columns else 'Close'
    return float(((sliced[col].iloc[-1] - sliced[col].iloc[0]) / sliced[col].iloc[0]) * 100)


def _half_metrics(closed_list, cut_time, before, spy_bh):
    if not closed_list:
        return None
    df = pd.DataFrame(closed_list)
    sub = df[df["Entry Time"] < cut_time] if before else df[df["Entry Time"] >= cut_time]
    if sub.empty:
        return None
    alpha, n, win_rate, compounded, win_twin_rate = _summarize_trades(sub.to_dict("records"), spy_bh)
    return dict(alpha=alpha, n=n, win_rate=win_rate, compounded=compounded, win_twin_rate=win_twin_rate)


def split_check(t_base, p_base, c_base, data_start, data_end):
    closed_base = [t for t in t_base if t["Result"] in CLOSED]
    if not closed_base:
        return None
    df_base = pd.DataFrame(closed_base).sort_values("Entry Time")
    n = len(df_base)
    if n < 2:
        return None
    cut_time = df_base["Entry Time"].iloc[int(n * 0.7)]

    train_spy_bh = period_spy_bh(data_start, cut_time)
    test_spy_bh = period_spy_bh(cut_time, data_end)

    closed_pess = [t for t in p_base if t["Result"] in CLOSED] if p_base else None
    closed_cert = [t for t in c_base if t["Result"] in CLOSED] if c_base else None

    def robust_half(before, spy_bh):
        base_h = _half_metrics(closed_base, cut_time, before, spy_bh)
        pess_h = _half_metrics(closed_pess, cut_time, before, spy_bh) if closed_pess else None
        cert_h = _half_metrics(closed_cert, cut_time, before, spy_bh) if closed_cert else None
        if base_h is None:
            return None
        alphas = [base_h["alpha"]]
        if pess_h:
            alphas.append(pess_h["alpha"])
        if cert_h:
            alphas.append(cert_h["alpha"])
        base_h["robust_alpha"] = min(alphas)
        return base_h

    train = robust_half(True, train_spy_bh)
    test = robust_half(False, test_spy_bh)
    if train is None or test is None:
        return None

    retention = (test["robust_alpha"] / train["robust_alpha"] * 100
                 if train["robust_alpha"] not in (0, None) else None)

    return dict(
        cut_time=cut_time,
        train_spy_bh=train_spy_bh, test_spy_bh=test_spy_bh,
        train_n=train["n"], train_robust_alpha=train["robust_alpha"],
        train_compounded=train["compounded"], train_win_rate=train["win_rate"],
        test_n=test["n"], test_robust_alpha=test["robust_alpha"],
        test_compounded=test["compounded"], test_win_rate=test["win_rate"],
        oos_retention_pct=retention,
    )


def run_ticker(conn, ticker):
    node = best_v4_node(conn, ticker)
    if node is None:
        return dict(ticker=ticker, note="no v4 SL=1/open_check node found")

    strategy_class = strategies.TrailingBothZScoreBreakout
    inputs = _load_node_inputs(ticker, strategy_class, STRATEGY_NAME, node["window"], node["z"])
    if inputs is None:
        return dict(ticker=ticker, note="no cached hourly data")
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

    row = dict(ticker=ticker, window=node["window"], hold=node["hold"], z=node["z"],
               tb=node["tb"], ts=node["ts"], arm=node["arm"],
               full_robust_alpha=node["robust_alpha"])
    split = split_check(t_base, p_base, c_base, data_start, data_end)
    if split:
        row.update(split)
    else:
        row["note"] = "not enough closed trades to split"
    return row


def run(tickers):
    rows = []
    with sqlite3.connect(RESEARCH_DB, timeout=60) as conn:
        for ticker in tickers:
            print(f"[{ticker}] running train/test split check...", file=sys.stderr)
            try:
                rows.append(run_ticker(conn, ticker))
            except Exception as e:
                rows.append(dict(ticker=ticker, note=f"error: {e!r}"))
    return pd.DataFrame(rows)


if __name__ == "__main__":
    args = sys.argv[1:]
    out_path = "logs/train_test_split_check.csv"
    if args and args[-1].endswith(".csv"):
        out_path = args.pop()
    tickers = args
    if not tickers:
        print(__doc__)
        sys.exit(1)
    df = run(tickers)
    df.to_csv(out_path, index=False)
    print(f"\nWrote {len(df)} rows to {out_path}")
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 50)
    print(df.to_string(index=False))
