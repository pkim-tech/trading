"""Checklist checks 4 (win-rate stability, 70/30 split), 9 (same-day-block
sensitivity) and 10 (same-day-block stability, 70/30 split) for a list of
tickers -- the two deeper checks candidate_checklist_report.py deliberately
leaves out (needs a full trade replay per ticker, not just cached backtest_cache
rows). Reuses run_backtest_v110/_summarize_trades/compute_bh_returns directly
rather than reimplementing the kernel or the alpha math.

Pulls the same real v4 winning node (stop_loss=1%, entry_timing='open_check')
that candidate_checklist_report.py uses, via best_v4_node from that module.

See docs/watchlist_candidate_checklist.md checks 4/9/10 for what these numbers
mean.

Usage: .venv/bin/python scripts/checklist_deep_checks.py TICKER [TICKER ...] [out.csv]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3
import pandas as pd

import strategies
from backtester import run_backtest_v110
from run_optimization_sweep import _load_node_inputs, _summarize_trades, compute_bh_returns
from candidate_checklist_report import best_v4_node, RESEARCH_DB

CLOSED = ["WIN", "LOSS", "TWIN", "TLOSS"]
STRATEGY_NAME = "TrailingBothZScoreBreakout"


def _robust(trades, trades_pess, trades_cert, spy_bh):
    closed = [t for t in trades if t["Result"] in CLOSED]
    if not closed:
        return None
    alpha, n, win_rate, compounded, win_twin_rate = _summarize_trades(closed, spy_bh)
    alphas = [alpha]
    for tl in (trades_pess, trades_cert):
        c = [t for t in tl if t["Result"] in CLOSED] if tl else None
        if c:
            a, *_ = _summarize_trades(c, spy_bh)
            alphas.append(a)
    return dict(closed=closed, alpha=alpha, robust_alpha=min(alphas), n=n,
                win_rate=win_rate, win_twin_rate=win_twin_rate, compounded=compounded)


def _split_7030(closed_sorted):
    n = len(closed_sorted)
    cut = int(n * 0.7)
    return closed_sorted[:cut], closed_sorted[cut:]


def check4(closed_baseline, spy_bh):
    df = pd.DataFrame(closed_baseline).sort_values("Entry Time")
    early, late = _split_7030(df.to_dict("records"))
    if not early or not late:
        return None
    _, _, early_wr, _, early_wtr = _summarize_trades(early, spy_bh)
    _, _, late_wr, _, late_wtr = _summarize_trades(late, spy_bh)
    return dict(early_n=len(early), early_win_rate=early_wr, early_win_twin_rate=early_wtr,
                late_n=len(late), late_win_rate=late_wr, late_win_twin_rate=late_wtr)


def check9(base, blocked):
    if base is None or blocked is None or base["robust_alpha"] == 0:
        return None
    retention = blocked["robust_alpha"] / base["robust_alpha"] if base["robust_alpha"] else None
    return dict(base_trades=base["n"], blocked_trades=blocked["n"],
                base_robust_alpha=base["robust_alpha"], blocked_robust_alpha=blocked["robust_alpha"],
                retention_pct=retention * 100 if retention is not None else None)


def check10(base, blocked, spy_bh):
    """Split baseline+blocked trade lists by the SAME cutoff date (baseline's 70th-
    percentile entry time), then compute each half's own retention ratio."""
    if base is None or blocked is None:
        return None
    df_base = pd.DataFrame(base["closed"]).sort_values("Entry Time")
    n = len(df_base)
    cut_time = df_base["Entry Time"].iloc[int(n * 0.7)] if n >= 2 else None
    if cut_time is None:
        return None

    df_blocked = pd.DataFrame(blocked["closed"]).sort_values("Entry Time")

    def half_alpha(df, before):
        sub = df[df["Entry Time"] < cut_time] if before else df[df["Entry Time"] >= cut_time]
        if sub.empty:
            return None
        alpha, n_t, *_ = _summarize_trades(sub.to_dict("records"), spy_bh)
        return alpha, n_t

    early_base, early_blocked = half_alpha(df_base, True), half_alpha(df_blocked, True)
    late_base, late_blocked = half_alpha(df_base, False), half_alpha(df_blocked, False)

    def retention(b, blk):
        if not b or not blk or b[0] == 0:
            return None, (b[1] if b else 0), (blk[1] if blk else 0)
        return blk[0] / b[0] * 100, b[1], blk[1]

    early_ret, early_bn, early_kn = retention(early_base, early_blocked)
    late_ret, late_bn, late_kn = retention(late_base, late_blocked)
    return dict(cut_time=cut_time, early_base_trades=early_bn, early_blocked_trades=early_kn,
                early_retention_pct=early_ret, late_base_trades=late_bn,
                late_blocked_trades=late_kn, late_retention_pct=late_ret)


def run_ticker(conn, ticker):
    node = best_v4_node(conn, ticker)
    if node is None:
        return dict(ticker=ticker, note="no v4 SL=1/open_check node found")

    strategy_class = strategies.TrailingBothZScoreBreakout
    inputs = _load_node_inputs(ticker, strategy_class, STRATEGY_NAME, node["window"], node["z"])
    if inputs is None:
        return dict(ticker=ticker, note="no cached hourly data")
    _, _, prep = inputs
    asset_bh, spy_bh = compute_bh_returns(ticker)
    if spy_bh is None:
        return dict(ticker=ticker, note="no SPY buy-hold reference")

    common = dict(
        df_hourly=None, df_daily_indicators=None, ticker=ticker,
        take_profit=node["arm"] / 100.0, stop_loss=0.01,
        max_hours_to_hold=node["hold"], z_score_threshold=node["z"],
        trail_buy_pct=node["tb"] / 100.0, trail_pct=node["ts"] / 100.0,
        entry_timing="open_check", return_bounds=True, prep=prep,
    )

    t_base, p_base, c_base = run_backtest_v110(same_day_block=False, **common)
    t_blk, p_blk, c_blk = run_backtest_v110(same_day_block=True, **common)

    base = _robust(t_base, p_base, c_base, spy_bh)
    blocked = _robust(t_blk, p_blk, c_blk, spy_bh)

    row = dict(ticker=ticker, window=node["window"], hold=node["hold"], z=node["z"],
               tb=node["tb"], ts=node["ts"], arm=node["arm"])
    c4 = check4(base["closed"], spy_bh) if base else None
    c9 = check9(base, blocked)
    c10 = check10(base, blocked, spy_bh)
    if c4:
        row.update({f"c4_{k}": v for k, v in c4.items()})
    if c9:
        row.update({f"c9_{k}": v for k, v in c9.items()})
    if c10:
        row.update({f"c10_{k}": v for k, v in c10.items()})
    return row


def run(tickers):
    rows = []
    with sqlite3.connect(RESEARCH_DB, timeout=60) as conn:
        for ticker in tickers:
            print(f"[{ticker}] running checks 4/9/10...", file=sys.stderr)
            try:
                rows.append(run_ticker(conn, ticker))
            except Exception as e:
                rows.append(dict(ticker=ticker, note=f"error: {e!r}"))
    return pd.DataFrame(rows)


if __name__ == "__main__":
    args = sys.argv[1:]
    out_path = "logs/checklist_deep_checks.csv"
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
