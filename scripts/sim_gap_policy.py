"""Gap-through-trigger entry policy comparison -- decides, empirically, whether
a real overnight/intraday gap past a trailing-buy's trigger price should
always be taken (resize and enter at the real Open) or skipped above some
gap-size threshold. See docs/backlog_cache.md's trailing-buy re-sizing item
and this session's finding: real gaps exceed a node's trail_buy_pct on
19-44% of trading days across the v4 watchlist.

Model (see export_trades.simulate_trail_both_gap_policy for the exact
mechanics): the entry-side gap fix (backtester.py, fixed this session) always
fills at the real Open once it's proven to have crossed the trigger --
skip_threshold=None reproduces that default ("always resize and enter").
skip_threshold=X abandons the setup instead (no trade this signal) whenever
the Open overshoots the trigger by more than X (relative), so the node keeps
waiting for the next clean signal rather than chasing an entry the backtest
never validated at that size.

Sweeps skip_threshold in {None, 3%, 5%, 10%, 15%} per ticker, using each
ticker's real active v4 node (watchlist_id=57), and reports compounded
return plus gap-trade-specific stats (count, win rate, contribution to total
return) for each variant.

Usage:
    .venv/bin/python scripts/sim_gap_policy.py                    # all watchlist_id=57 tickers
    .venv/bin/python scripts/sim_gap_policy.py --tickers SOXL KORU
"""
import argparse
import sqlite3
import time
from pathlib import Path

import pandas as pd

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategies
from backtester import prep_inputs
from export_trades import simulate_trail_both_gap_policy

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / "cache" / "research"
LIVE_DIR = REPO_ROOT / "cache" / "live"
OUTPUT_DIR = REPO_ROOT / "output"

THRESHOLDS = [None, 0.03, 0.05, 0.10, 0.15]


def _label(t):
    return "always" if t is None else f"skip>{t*100:.0f}%"


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


def get_watchlist_nodes(watchlist_id=57):
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


def run_ticker(node):
    ticker = node["ticker"]
    df_hourly, df_daily = _load(ticker)
    strat = strategies.TrailingBothZScoreBreakout(window=node["window"],
                                                    z_score_threshold=node["z"])
    df_daily_ind = strat.generate_daily_indicators(df_daily)
    p = prep_inputs(df_hourly, df_daily_ind)

    kwargs = dict(take_profit=node["arm_sell_pct"] / 100, stop_loss=node["fixed_sl"] / 100,
                  max_hours_to_hold=node["max_hold_hours"], trail_buy_pct=node["trail_buy_pct"] / 100,
                  trail_pct=node["trail_sell_pct"] / 100, target_h0=9, target_h1=14, z_thresh=node["z"],
                  open_check=(node["entry_timing"] == "open_check"))

    rows = []
    for threshold in THRESHOLDS:
        trades, gap_events = simulate_trail_both_gap_policy(p, skip_threshold=threshold, **kwargs)
        total_compounded = _compounded(trades)

        entered_bars = {g["bar_i"] for g in gap_events if g["entered"]}
        gap_trades = [t for t in trades if t["entry_i"] in entered_bars]
        non_gap_trades = [t for t in trades if t["entry_i"] not in entered_bars]
        gap_wins = sum(1 for t in gap_trades if t["ret"] > 0)
        skipped = sum(1 for g in gap_events if not g["entered"])

        rows.append({
            "ticker": ticker,
            "policy": _label(threshold),
            "threshold": threshold,
            "total_trades": len(trades),
            "compounded_pct": total_compounded,
            "gap_trades": len(gap_trades),
            "gap_trades_skipped": skipped,
            "gap_win_rate_pct": (gap_wins / len(gap_trades) * 100) if gap_trades else float("nan"),
            "non_gap_trades": len(non_gap_trades),
            "non_gap_compounded_pct": _compounded(non_gap_trades),
        })
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="+", default=None)
    parser.add_argument("--watchlist-id", type=int, default=57)
    args = parser.parse_args()

    nodes = get_watchlist_nodes(args.watchlist_id)
    if args.tickers:
        wanted = set(args.tickers)
        nodes = [n for n in nodes if n["ticker"] in wanted]

    all_rows = []
    t_start = time.time()
    for node in nodes:
        ticker = node["ticker"]
        try:
            rows = run_ticker(node)
        except FileNotFoundError:
            print(f"  [skip] {ticker}: no cached hourly data")
            continue
        all_rows.extend(rows)

    print(f"Total wall time: {time.time() - t_start:.1f}s\n")

    out = pd.DataFrame(all_rows)
    OUTPUT_DIR.mkdir(exist_ok=True)
    csv_path = OUTPUT_DIR / "gap_policy_summary.csv"
    out.to_csv(csv_path, index=False)
    print(f"Wrote {csv_path}\n")

    if out.empty:
        return

    print_table(out)


def print_table(out):
    always = out[out["policy"] == "always"].set_index("ticker")
    print(f"{'ticker':<6} {'always%':>9} {'gapN':>5} {'gapWin%':>8} | " +
          " | ".join(f"{_label(t):>10}" for t in THRESHOLDS if t is not None))
    print("-" * (6 + 9 + 5 + 8 + 3 + len(THRESHOLDS[1:]) * 13))
    for ticker in sorted(out["ticker"].unique()):
        sub = out[out["ticker"] == ticker].set_index("threshold")
        a = always.loc[ticker]
        cells = []
        for t in THRESHOLDS:
            if t is None:
                continue
            r = sub.loc[t]
            delta = r["compounded_pct"] - a["compounded_pct"]
            cells.append(f"{r['compounded_pct']:+8.0f}%{'+' if delta >= 0 else '-'}")
        print(f"{ticker:<6} {a['compounded_pct']:+8.0f}% {int(a['gap_trades']):5d} "
              f"{a['gap_win_rate_pct']:7.1f}% | " + " | ".join(f"{c:>10}" for c in cells))

    print("\n(each skip-threshold column shows that policy's compounded %, "
          "'+' if it beats 'always', '-' if it's worse)")


if __name__ == "__main__":
    main()
