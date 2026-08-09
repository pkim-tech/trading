"""Which of backtester.py's three trailing-buy bounce-fill resolutions
(possible/pessimistic/certain -- see _simulate_trail_both's docstring) is
actually closest to what really happens? Extends
verify_trailing_buy_resolution.py (which only replays 'possible') to also
replay 'pessimistic' and 'certain' for the same real historical signals,
using the exact same entry-side logic as the numba kernel
(backtester.py:884-1005), then compares all three against the real 5-min-bar
ground truth already used to validate 'possible'.

Built 2026-08-08 in response to the actual research question: not "is the
hourly kernel roughly right" (the existing tool's question) but "when the
three resolutions disagree, which one usually turns out to be right" -- so
future work can lean on whichever resolution is empirically most reliable
instead of always taking the conservative MIN of all three.

Usage:
  .venv/bin/python scripts/verify_fill_resolution_accuracy.py --adhoc "TNA:20:1.5:1.0:126,DRIP:20:1.0:8.0:84"
"""
import argparse
import sys
from pathlib import Path
from datetime import timedelta

import numpy as np
import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtester import prep_inputs
import strategies

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_trailing_buy_resolution import _load_hourly, replay_five_min, FIVE_MIN_LOOKBACK_DAYS


def find_all_resolution_signals(ticker, window, z_thresh, trail_buy_pct, max_hold_hours, cutoff):
    """Replays entry-only logic for all three resolutions in one pass, mirroring
    backtester.py's _simulate_trail_both exactly (possible: lines ~884 waiting
    branch unmodified single-guess; pessimistic: lines 884-905; certain:
    lines 984-1005). All three share the SAME signal-detection trigger (the
    z-score band check) -- they only differ in how the bounce-fill within the
    waiting state resolves."""
    df_hourly, df_daily = _load_hourly(ticker)
    strat = strategies.TrailingBothZScoreBreakout(window=window, z_score_threshold=z_thresh)
    indicators = strat.generate_daily_indicators(df_daily)
    p = prep_inputs(df_hourly, indicators)

    prices, highs, lows, hours = p['prices'], p['highs'], p['lows'], p['hours']
    opens = p['opens'] if 'opens' in p else df_hourly['Open'].values
    daily_idx, sma_arr, std_arr = p['daily_idx'], p['sma_arr'], p['std_arr']
    timestamps = p['timestamps']

    # possible
    waiting = False
    running_low = 0.0
    wait_bars = 0

    # pessimistic
    waiting_p = False
    running_low_p = 0.0
    wait_bars_p = 0

    # certain
    waiting_c = False
    running_low_c = 0.0
    wait_bars_c = 0

    signal_bar_i = 0
    signal_ts = signal_cp = None
    results = {}  # signal_bar_i -> dict of resolution -> (entry_time, entry_price)
    order = []

    for i in range(len(prices)):
        cp, high, low, op = prices[i], highs[i], lows[i], opens[i]

        any_waiting = waiting or waiting_p or waiting_c
        if any_waiting:
            rec = results.setdefault(signal_bar_i, {})

            if waiting:
                wait_bars += 1
                if low < running_low:
                    running_low = low
                buy_trigger = running_low * (1.0 + trail_buy_pct)
                if high >= buy_trigger:
                    rec['possible'] = (timestamps[i], buy_trigger)
                    waiting = False
                elif wait_bars >= max_hold_hours:
                    waiting = False

            if waiting_p:
                wait_bars_p += 1
                buy_trigger_p = running_low_p * (1.0 + trail_buy_pct)
                if op >= buy_trigger_p:
                    rec['pessimistic'] = (timestamps[i], op)
                    waiting_p = False
                elif high >= buy_trigger_p:
                    rec['pessimistic'] = (timestamps[i], buy_trigger_p)
                    waiting_p = False
                else:
                    if low < running_low_p:
                        running_low_p = low
                    if wait_bars_p >= max_hold_hours:
                        waiting_p = False

            if waiting_c:
                wait_bars_c += 1
                buy_trigger_prior = running_low_c * (1.0 + trail_buy_pct)
                if op >= buy_trigger_prior:
                    rec['certain'] = (timestamps[i], op)
                    waiting_c = False
                else:
                    updated_low_c = low if low < running_low_c else running_low_c
                    buy_trigger_updated = updated_low_c * (1.0 + trail_buy_pct)
                    if cp >= buy_trigger_updated:
                        rec['certain'] = (timestamps[i], buy_trigger_updated)
                        waiting_c = False
                    else:
                        running_low_c = updated_low_c
                        if wait_bars_c >= max_hold_hours:
                            waiting_c = False
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
            waiting = waiting_p = waiting_c = True
            running_low = running_low_p = running_low_c = cp
            wait_bars = wait_bars_p = wait_bars_c = 0
            signal_bar_i = i
            signal_ts, signal_cp = timestamps[i], cp
            cutoff_i = min(i + max_hold_hours, len(timestamps) - 1)
            results[i] = {'signal_time': signal_ts, 'signal_close': signal_cp,
                          'cutoff_time': timestamps[cutoff_i]}
            order.append(i)

    return [results[i] for i in order if results[i]['signal_time'] >= cutoff]


def fill_accuracy_for_node(ticker, window, z_score_threshold, trail_buy_pct_pct, max_hold_hours, df_5m=None):
    """Real-signal fill-accuracy diffs for one node, as a DataFrame (columns:
    signal_time, possible_diff_pct, pessimistic_diff_pct, certain_diff_pct).
    Pass a pre-downloaded df_5m to avoid re-fetching yfinance per node when
    comparing several candidate nodes for the same ticker (candidate_5min_report.py).
    Extracted from main() 2026-08-08 (later) so callers other than the CLI
    (e.g. a multi-candidate comparison report) can reuse this without a
    subprocess/CLI round trip."""
    trail_buy_pct = trail_buy_pct_pct / 100.0
    cutoff = pd.Timestamp.now().normalize() - timedelta(days=FIVE_MIN_LOOKBACK_DAYS)
    signals = find_all_resolution_signals(
        ticker, window, z_score_threshold, trail_buy_pct, max_hold_hours, cutoff)
    real_signals = [s for s in signals if any(k in s for k in ('possible', 'pessimistic', 'certain'))]
    if not real_signals:
        return pd.DataFrame(columns=['signal_time', 'possible_diff_pct', 'pessimistic_diff_pct', 'certain_diff_pct'])

    if df_5m is None:
        df_5m = yf.download(ticker, period="60d", interval="5m", multi_level_index=False, progress=False)
        df_5m.index = pd.to_datetime(df_5m.index).tz_localize(None)

    rows = []
    for s in real_signals:
        real = replay_five_min(ticker, df_5m, s['signal_time'], s['signal_close'],
                                trail_buy_pct, s['cutoff_time'])
        if real is None:
            continue
        row = {'signal_time': s['signal_time']}
        for res in ('possible', 'pessimistic', 'certain'):
            if res in s:
                _, px = s[res]
                row[f'{res}_diff_pct'] = (real['five_min_entry_price'] - px) / px * 100
            else:
                # np.nan, not None -- a column that's missing for EVERY real
                # signal (e.g. no 'certain' resolution ever computed) would
                # otherwise infer as object dtype, and pandas' .abs() raises
                # TypeError on an object-dtype column containing None (found
                # 2026-08-09 running candidate_full_review.py at real scale,
                # 82 tickers -- first caller to hit this edge case).
                row[f'{res}_diff_pct'] = np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def print_accuracy_summary(df):
    diff_cols = ['possible_diff_pct', 'pessimistic_diff_pct', 'certain_diff_pct']
    print("\n--- Which resolution is closest to the real 5-min fill, per signal ---")
    abs_diffs = df[diff_cols].abs()
    closest = abs_diffs.idxmin(axis=1).str.replace('_diff_pct', '', regex=False)
    print(closest.value_counts())

    print("\n--- Mean absolute price diff vs real 5-min fill, per resolution ---")
    for col in diff_cols:
        vals = df[col].dropna()
        if len(vals):
            print(f"  {col.replace('_diff_pct', ''):12}: mean_abs={vals.abs().mean():.3f}%  "
                  f"mean_signed={vals.mean():+.3f}%  n={len(vals)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adhoc", required=True,
                     help="comma-separated ticker:window:z:trail_buy_pct:max_hold_hours entries")
    args = ap.parse_args()

    nodes = []
    for spec in args.adhoc.split(','):
        ticker, window, z, tb, hold = spec.strip().split(':')
        nodes.append({'ticker': ticker.upper(), 'window': int(window),
                       'z_score_threshold': float(z), 'trail_buy_pct': float(tb),
                       'max_hold_hours': int(hold)})

    all_rows = []
    for n in nodes:
        df = fill_accuracy_for_node(n['ticker'], n['window'], n['z_score_threshold'],
                                     n['trail_buy_pct'], n['max_hold_hours'])
        if df.empty:
            print(f"{n['ticker']}: no resolved signals in the last {FIVE_MIN_LOOKBACK_DAYS}d")
            continue
        df.insert(0, 'ticker', n['ticker'])
        all_rows.append(df)

    if not all_rows:
        print("No comparable signals found.")
        return

    df = pd.concat(all_rows, ignore_index=True)
    pd.set_option('display.width', 160)
    print(df.to_string(index=False))
    print_accuracy_summary(df)


if __name__ == "__main__":
    main()
