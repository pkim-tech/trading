"""Checks whether backtester.py's hourly-bar trailing-sell modeling for
TrailingBothZScoreBreakout (once trailing is armed: track peak, exit when Low crosses
peak * (1 - trail_sell_pct)) is a reasonable proxy for what continuous price tracking
would actually catch -- mirrors verify_trailing_buy_resolution.py but for the exit side.
Re-detects every recent live-watchlist trailing-sell exit using 5-min bars (yfinance,
~60 days back) and compares exit price/time against what the hourly kernel predicts for
the same trade.

Usage: .venv/bin/python scripts/verify_trailing_sell_resolution.py [--tickers AGQ,SOXL]
"""
import argparse
import sqlite3
import sys
from pathlib import Path
from datetime import timedelta

import numpy as np
import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtester import prep_inputs
from export_trades import simulate_trail_both_annotated
import strategies

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "research"
LIVE_DB = Path(__file__).resolve().parent.parent / "cache" / "live" / "trading_live.db"
FIVE_MIN_LOOKBACK_DAYS = 58  # yfinance caps 5m history at 60d; leave margin


def _load_hourly(ticker):
    df = pd.read_csv(CACHE_DIR / f"{ticker}_1h.csv", index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df.sort_index()
    df_daily = df.resample('D').last().dropna()
    return df, df_daily


def find_hourly_trailing_exits(ticker, window, z_thresh, trail_buy_pct, trail_sell_pct,
                                arm_pct, max_hold_hours, cutoff):
    """Replays the full trailing-buy/trailing-sell state machine (via the read-only
    annotated mirror in export_trades.py) and keeps only trades where trailing actually
    armed -- those trades' exits are governed entirely by the trailing branch."""
    df_hourly, df_daily = _load_hourly(ticker)
    strat = strategies.TrailingBothZScoreBreakout(window=window, z_score_threshold=z_thresh)
    indicators = strat.generate_daily_indicators(df_daily)
    p = prep_inputs(df_hourly, indicators)
    timestamps = p['timestamps']

    trades = simulate_trail_both_annotated(
        p, take_profit=arm_pct, stop_loss=1.0, max_hours_to_hold=max_hold_hours,
        trail_buy_pct=trail_buy_pct, trail_pct=trail_sell_pct, target_h0=9, target_h1=14,
        z_thresh=z_thresh)

    events = []
    for t in trades:
        if t['arm_i'] is None:
            continue
        arm_time = timestamps[t['arm_i']]
        if arm_time < cutoff:
            continue
        # max_hold_hours counts hourly *bars* (only ~7/day exist during the trading
        # session), not calendar hours -- look up the real bar timestamp at
        # held==max_hold_hours rather than adding timedelta(hours=max_hold_hours).
        cutoff_i = min(t['entry_i'] + max_hold_hours, len(timestamps) - 1)
        events.append({
            'arm_time': arm_time,
            'peak_at_arm': p['prices'][t['arm_i']],
            'entry_time': timestamps[t['entry_i']],
            'cutoff_time': timestamps[cutoff_i],
            'hourly_exit_time': timestamps[t['exit_i']],
            'hourly_exit_price': t['exit_p'],
            'hourly_result': t['result'],
        })
    return events


def replay_five_min(df_5m, arm_time, peak_at_arm, trail_sell_pct, cutoff_time):
    """From the bar immediately after the arm hour's close, walk 5-min bars tracking the
    same peak/trail_stop logic as the hourly kernel's trailing branch. Returns None if the
    trade needs more 5-min history than yfinance's 60-day window actually has (rather than
    silently reporting the last available bar as if it were a real time-based exit)."""
    start = arm_time + timedelta(hours=1)
    window = df_5m[(df_5m.index >= start) & (df_5m.index <= cutoff_time)]
    if window.empty:
        return None
    peak = peak_at_arm
    trail_stop = peak * (1.0 - trail_sell_pct)
    for ts, row in window.iterrows():
        if row['High'] > peak:
            peak = row['High']
            trail_stop = peak * (1.0 - trail_sell_pct)
        if row['Low'] <= trail_stop:
            return {'five_min_exit_time': ts, 'five_min_exit_price': trail_stop, 'five_min_reason': 'trail'}
    if df_5m.index.max() < cutoff_time:
        return None  # ran out of 5-min history before the real max-hold cutoff
    last_row = window.iloc[-1]
    return {'five_min_exit_time': window.index[-1], 'five_min_exit_price': last_row['Close'], 'five_min_reason': 'time'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tickers', help="comma-separated ticker subset, e.g. AGQ,SOXL "
                         "(default: full active watchlist)")
    args = parser.parse_args()
    ticker_filter = {t.strip().upper() for t in args.tickers.split(',')} if args.tickers else None

    conn = sqlite3.connect(LIVE_DB)
    conn.row_factory = sqlite3.Row
    active_wl = conn.execute(
        "SELECT id FROM watchlists WHERE is_active=1"
    ).fetchone()[0]
    nodes = conn.execute(
        "SELECT DISTINCT ticker, version, window, z_score_threshold, trail_buy_pct, "
        "trail_sell_pct, arm_sell_pct, max_hold_hours "
        "FROM watch_list WHERE strategy='TrailingBothZScoreBreakout' AND watchlist_id=?",
        (active_wl,)
    ).fetchall()
    if ticker_filter:
        nodes = [n for n in nodes if n['ticker'] in ticker_filter]

    cutoff = pd.Timestamp.now().normalize() - timedelta(days=FIVE_MIN_LOOKBACK_DAYS)
    all_rows = []

    vol_ratios = {}
    for n in nodes:
        ticker = n['ticker']
        trail_buy_pct = n['trail_buy_pct'] / 100.0
        trail_sell_pct = n['trail_sell_pct'] / 100.0
        arm_pct = n['arm_sell_pct'] / 100.0

        df_hourly_recent, _ = _load_hourly(ticker)
        df_hourly_recent = df_hourly_recent[df_hourly_recent.index >= cutoff]
        intrahour_pct = (df_hourly_recent['High'] - df_hourly_recent['Low']) / df_hourly_recent['Close'] * 100
        vol_ratios[ticker] = intrahour_pct.median() / (trail_sell_pct * 100)

        events = find_hourly_trailing_exits(
            ticker, n['window'], n['z_score_threshold'], trail_buy_pct, trail_sell_pct,
            arm_pct, n['max_hold_hours'], cutoff)
        if not events:
            print(f"{ticker}: no trailing-sell exits in the last {FIVE_MIN_LOOKBACK_DAYS}d")
            continue

        df_5m = yf.download(ticker, period="60d", interval="5m", multi_level_index=False, progress=False)
        df_5m.index = pd.to_datetime(df_5m.index).tz_localize(None)

        for ev in events:
            r = replay_five_min(df_5m, ev['arm_time'], ev['peak_at_arm'], trail_sell_pct,
                                 ev['cutoff_time'])
            row = {'ticker': ticker, **ev}
            if r is None:
                row['five_min_exit_time'] = None
                row['five_min_exit_price'] = None
                row['price_diff_pct'] = None
            else:
                row.update(r)
                row['price_diff_pct'] = (r['five_min_exit_price'] - ev['hourly_exit_price']) / ev['hourly_exit_price'] * 100
            all_rows.append(row)

    if not all_rows:
        print("No comparable trailing-sell exits found in the 5-min data window.")
        return

    df = pd.DataFrame(all_rows)
    pd.set_option('display.width', 160)
    print(df.to_string(index=False))

    matched = df.dropna(subset=['five_min_exit_time'])
    if len(matched):
        print(f"\n{len(matched)}/{len(df)} exits matched within the 5-min data window.")
        print(f"Mean price diff (5-min exit vs hourly kernel): {matched['price_diff_pct'].mean():+.3f}%")
        print(f"Max abs price diff: {matched['price_diff_pct'].abs().max():.3f}%")
        time_diff_hours = (matched['five_min_exit_time'] - matched['hourly_exit_time']).dt.total_seconds() / 3600
        print(f"Mean exit-time diff (5-min earlier than hourly bar close, hours): {(-time_diff_hours).mean():+.2f}h")
        summary = matched.groupby('ticker')['price_diff_pct'].agg(['mean', 'count'])
        summary['median_intrahour_range_pct_of_trigger'] = summary.index.map(vol_ratios)
        print("\nPer-ticker mean price diff (%) and median-intrahour-range / trail_sell_pct ratio")
        print("(ratio >> 1 flags tickers likely to see premature-exit drift):")
        print(summary.to_string())


if __name__ == '__main__':
    main()
