"""Checks whether backtester.py's hourly-bar bounce-detection for TrailingBothZScoreBreakout's
trailing-buy entry (_simulate_trail_both's 'waiting' state: track running_low, fire when High
clears running_low * (1 + trail_buy_pct)) is a reasonable proxy for what a continuously-tracking
broker trailing-buy order would actually do -- without needing real broker fills. Re-detects the
same bounce using 5-min bars (yfinance, ~60 days back) for every recent live-watchlist signal and
compares entry price/time against what the hourly kernel predicts for the same signal.

Usage: .venv/bin/python scripts/verify_trailing_buy_resolution.py [--tickers AGQ,SOXL]
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
from backtester import prep_inputs
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


def find_hourly_signals(ticker, window, z_thresh, trail_buy_pct, max_hold_hours, cutoff):
    """Replays _simulate_trail_both's entry-only logic in Python (not numba) so intermediate
    signal/waiting state can be inspected. Mirrors backtester.py:601-627 exactly."""
    df_hourly, df_daily = _load_hourly(ticker)
    strat = strategies.TrailingBothZScoreBreakout(window=window, z_score_threshold=z_thresh)
    indicators = strat.generate_daily_indicators(df_daily)
    p = prep_inputs(df_hourly, indicators)

    prices, highs, lows, hours = p['prices'], p['highs'], p['lows'], p['hours']
    daily_idx, sma_arr, std_arr = p['daily_idx'], p['sma_arr'], p['std_arr']
    timestamps = p['timestamps']

    waiting = False
    running_low = 0.0
    wait_bars = 0
    signal_bar_i = 0
    signals = []

    for i in range(len(prices)):
        cp, high, low = prices[i], highs[i], lows[i]

        if waiting:
            wait_bars += 1
            if low < running_low:
                running_low = low
            buy_trigger = running_low * (1.0 + trail_buy_pct)
            if high >= buy_trigger:
                # max_hold_hours counts hourly *bars* (only ~7/day exist during the
                # trading session), not calendar hours -- look up the real bar
                # timestamp at wait_bars==max_hold_hours rather than adding
                # timedelta(hours=max_hold_hours).
                cutoff_i = min(signal_bar_i + max_hold_hours, len(timestamps) - 1)
                signals.append({
                    'signal_time': signal_ts, 'signal_close': signal_cp,
                    'cutoff_time': timestamps[cutoff_i],
                    'hourly_entry_time': timestamps[i], 'hourly_entry_price': buy_trigger,
                })
                waiting = False
                continue
            if wait_bars >= max_hold_hours:
                waiting = False
            continue

        h = hours[i]
        if h != 9 and h != 14:
            continue
        di = daily_idx[i]
        if di < 0:
            continue
        sma, std = sma_arr[di], std_arr[di]
        if std == 0.0:
            continue
        lower_band = sma - std * z_thresh
        if cp <= lower_band:
            waiting = True
            running_low = cp
            wait_bars = 0
            signal_bar_i = i
            signal_ts, signal_cp = timestamps[i], cp

    return [s for s in signals if s['signal_time'] >= cutoff]


def replay_five_min(ticker, df_5m, signal_time, signal_close, trail_buy_pct, cutoff_time):
    """From the bar immediately after the signal hour's close, walk 5-min bars tracking the
    same running_low/buy_trigger logic as the hourly kernel. Returns None if the trade
    needs more 5-min history than yfinance's 60-day window actually has (rather than
    silently reporting the last available bar as a fabricated result)."""
    start = signal_time + timedelta(hours=1)
    window = df_5m[(df_5m.index >= start) & (df_5m.index <= cutoff_time)]
    if window.empty:
        return None
    running_low = signal_close
    buy_trigger = running_low * (1.0 + trail_buy_pct)
    for ts, row in window.iterrows():
        if row['Low'] < running_low:
            running_low = row['Low']
            buy_trigger = running_low * (1.0 + trail_buy_pct)
        if row['High'] >= buy_trigger:
            return {'five_min_entry_time': ts, 'five_min_entry_price': buy_trigger}
    return None


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
        "SELECT DISTINCT ticker, version, window, z_score_threshold, trail_buy_pct, max_hold_hours "
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

        df_hourly_recent, _ = _load_hourly(ticker)
        df_hourly_recent = df_hourly_recent[df_hourly_recent.index >= cutoff]
        intrahour_pct = (df_hourly_recent['High'] - df_hourly_recent['Low']) / df_hourly_recent['Close'] * 100
        vol_ratios[ticker] = intrahour_pct.median() / (trail_buy_pct * 100)

        signals = find_hourly_signals(
            ticker, n['window'], n['z_score_threshold'], trail_buy_pct, n['max_hold_hours'], cutoff)
        if not signals:
            print(f"{ticker}: no signals in the last {FIVE_MIN_LOOKBACK_DAYS}d")
            continue

        df_5m = yf.download(ticker, period="60d", interval="5m", multi_level_index=False, progress=False)
        df_5m.index = pd.to_datetime(df_5m.index).tz_localize(None)

        for s in signals:
            r = replay_five_min(ticker, df_5m, s['signal_time'], s['signal_close'],
                                 trail_buy_pct, s['cutoff_time'])
            row = {'ticker': ticker, **s}
            if r is None:
                row['five_min_entry_time'] = None
                row['five_min_entry_price'] = None
                row['price_diff_pct'] = None
            else:
                row.update(r)
                row['price_diff_pct'] = (r['five_min_entry_price'] - s['hourly_entry_price']) / s['hourly_entry_price'] * 100
            all_rows.append(row)

    if not all_rows:
        print("No comparable signals found in the 5-min data window.")
        return

    df = pd.DataFrame(all_rows)
    pd.set_option('display.width', 160)
    print(df.to_string(index=False))

    matched = df.dropna(subset=['five_min_entry_time'])
    if len(matched):
        print(f"\n{len(matched)}/{len(df)} signals matched within the 5-min data window.")
        print(f"Mean price diff (5-min fill vs hourly kernel): {matched['price_diff_pct'].mean():+.3f}%")
        print(f"Max abs price diff: {matched['price_diff_pct'].abs().max():.3f}%")
        time_diff_hours = (matched['five_min_entry_time'] - matched['hourly_entry_time']).dt.total_seconds() / 3600
        print(f"Mean entry-time diff (5-min earlier than hourly bar close, hours): {(-time_diff_hours).mean():+.2f}h")
        summary = matched.groupby('ticker')['price_diff_pct'].agg(['mean', 'count'])
        summary['median_intrahour_range_pct_of_trigger'] = summary.index.map(vol_ratios)
        print("\nPer-ticker mean price diff (%) and median-intrahour-range / trail_buy_pct ratio")
        print("(ratio >> 1 flags tickers likely to see premature-fill drift):")
        print(summary.to_string())


if __name__ == '__main__':
    main()
