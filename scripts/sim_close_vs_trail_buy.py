"""Research script: for real historical signals firing in the LAST daily
signal window (the 14:30-labeled bar checked in the 15:25-15:40 window --
no bars left same-day to catch a trailing-buy bounce before the overnight
gap), compares the actual trailing-buy (TB) fill against a market-on-close
(MOC) counterfactual entry at that same bar's Close. See
docs/backlog_cache.md's 'last-bar market-on-close vs trailing-buy' research
item and export_trades.collect_last_window_comparisons for the exact
mechanics (all three fill resolutions -- possible/pessimistic/certain --
per the standing robust-alpha convention).

Usage:
    .venv/bin/python scripts/sim_close_vs_trail_buy.py --tickers SOXL
    .venv/bin/python scripts/sim_close_vs_trail_buy.py                # all watchlist_id=65 tickers
"""
import argparse
import sqlite3
from pathlib import Path

import pandas as pd

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategies
from backtester import prep_inputs
from export_trades import collect_last_window_comparisons

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / "cache" / "research"
LIVE_DIR = REPO_ROOT / "cache" / "live"
OUTPUT_DIR = REPO_ROOT / "output"


def _load(ticker):
    df_hourly = pd.read_csv(CACHE_DIR / f"{ticker}_1h.csv", index_col=0, parse_dates=True)
    df_hourly.index = pd.to_datetime(df_hourly.index).tz_localize(None)
    df_hourly = df_hourly.sort_index()
    close_col = 'Adj Close' if 'Adj Close' in df_hourly.columns else 'Close'
    df_daily = df_hourly.resample('D').last().dropna(subset=[close_col])
    return df_hourly, df_daily


def get_watchlist_nodes(watchlist_id=65, strategy="TrailingBothZScoreBreakout"):
    conn = sqlite3.connect(LIVE_DIR / "trading_live.db")
    c = conn.cursor()
    c.execute(
        "SELECT ticker, window, z_score_threshold, trail_buy_pct, arm_sell_pct, "
        "trail_sell_pct, fixed_sl, max_hold_hours, entry_timing "
        "FROM watch_list WHERE watchlist_id=? AND strategy=? ORDER BY ticker",
        (watchlist_id, strategy),
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

    rows = collect_last_window_comparisons(
        p, take_profit=node["arm_sell_pct"] / 100, stop_loss=node["fixed_sl"] / 100,
        max_hours_to_hold=node["max_hold_hours"], trail_buy_pct=node["trail_buy_pct"] / 100,
        trail_pct=node["trail_sell_pct"] / 100, target_h0=9, target_h1=14, z_thresh=node["z"],
        open_check=(node["entry_timing"] == "open_check"))
    for r in rows:
        r["ticker"] = ticker
    return rows


def summarize(df):
    if df.empty:
        return pd.DataFrame()
    out = []
    for (ticker, res), g in df.groupby(["ticker", "resolution"]):
        n = len(g)
        tb_wins = (g["tb_ret"] > g["moc_ret"]).sum()
        moc_wins = (g["moc_ret"] > g["tb_ret"]).sum()
        tb_compounded = (g["tb_ret"] + 1).prod()
        moc_compounded = (g["moc_ret"] + 1).prod()
        out.append(dict(
            ticker=ticker, resolution=res, last_window_signals=n,
            tb_wins=tb_wins, moc_wins=moc_wins, ties=n - tb_wins - moc_wins,
            tb_win_rate_pct=(g["tb_ret"] > 0).mean() * 100,
            moc_win_rate_pct=(g["moc_ret"] > 0).mean() * 100,
            tb_mean_ret_pct=g["tb_ret"].mean() * 100,
            moc_mean_ret_pct=g["moc_ret"].mean() * 100,
            tb_compounded_pct=(tb_compounded - 1) * 100,
            moc_compounded_pct=(moc_compounded - 1) * 100,
        ))
    return pd.DataFrame(out)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="+", default=None)
    parser.add_argument("--watchlist-id", type=int, default=65)
    args = parser.parse_args()

    nodes = get_watchlist_nodes(args.watchlist_id)
    if args.tickers:
        wanted = set(args.tickers)
        nodes = [n for n in nodes if n["ticker"] in wanted]

    all_rows = []
    for node in nodes:
        ticker = node["ticker"]
        try:
            rows = run_ticker(node)
        except FileNotFoundError:
            print(f"  [skip] {ticker}: no cached hourly data")
            continue
        all_rows.extend(rows)

    df = pd.DataFrame(all_rows)
    OUTPUT_DIR.mkdir(exist_ok=True)
    detail_path = OUTPUT_DIR / "close_vs_trail_buy_detail.csv"
    df.to_csv(detail_path, index=False)
    print(f"Wrote {detail_path} ({len(df)} rows)\n")

    summary = summarize(df)
    summary_path = OUTPUT_DIR / "close_vs_trail_buy_summary.csv"
    summary.to_csv(summary_path, index=False)
    print(f"Wrote {summary_path}\n")

    if not summary.empty:
        print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
