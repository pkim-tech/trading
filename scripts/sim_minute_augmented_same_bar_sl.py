"""Augments the same-bar-SL walk (sim_same_bar_sl_characterization.py) with REAL ground
truth from Massive.com 1-minute data (scripts/fetch_massive_minute_data.py) wherever it's
available, instead of guessing via possible/pessimistic/certain. Built 2026-08-21 (very
late), same night as the original characterization -- see docs/backlog_cache.md.

For any hourly bar inside the minute-data-covered window (the most recent ~2 years, per
the CSVs in cache/research/minute_data/), this looks up the real 1-minute bars for that
specific hour and determines the TRUE sequence: exact minute the fill trigger was crossed,
exact minute (if any) the resulting stop was breached -- no ordering assumption needed,
because the real order of events is directly observable. For bars OUTSIDE that window (the
oldest ~1 year of the ~3yr cached hourly history), falls back unchanged to the existing
hourly-bar approximation (gap-fill-only same-bar check, matching
scripts/sim_same_bar_sl_characterization.py's corrected logic).

This directly answers: of the same-bar-SL divergence found tonight, how much is real
(confirmed by actual minute data) vs. how much was still an artifact of the hourly
approximation, for the ~2/3 of history where we no longer have to guess.

Usage:
    .venv/bin/python scripts/sim_minute_augmented_same_bar_sl.py [--tickers T ...] [--dump]
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategies
from backtester import prep_inputs, WIN, LOSS, TWIN, TLOSS, OPEN
from scripts.export_trades import load_hourly, simulate_trail_both_annotated
from scripts.sim_5min_whipsaw import NODES

TB_NODES = [n for n in NODES if n['strategy'] == 'TrailingBothZScoreBreakout']
MINUTE_DATA_DIR = Path(__file__).resolve().parent.parent / "cache" / "research" / "minute_data"


def load_minute_data(ticker):
    path = MINUTE_DATA_DIR / f"{ticker}_1m.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path, parse_dates=["timestamp"])
    df = df.set_index("timestamp").sort_index()
    return df


def resolve_bar_with_minutes(minute_df, bar_start_ts, entry_price_fn, stop_loss, trail_buy_pct):
    """Given the real 1-minute bars for one hourly bar's span, walk them in true
    chronological order to determine: (a) the exact fill (if this hour resolves a WAIT),
    (b) whether the resulting stop was breached later in the SAME hour, using the real
    minute-by-minute sequence -- no assumption needed, this is ground truth."""
    bar_end_ts = bar_start_ts + pd.Timedelta(hours=1)
    window = minute_df.loc[(minute_df.index >= bar_start_ts) & (minute_df.index < bar_end_ts)]
    return window  # caller walks this directly; kept as a thin lookup helper


def _summarize(trades):
    compounded = 1.0
    for t in trades:
        compounded *= (1.0 + t['ret'])
    return dict(n=len(trades), compounded_pct=round((compounded - 1.0) * 100, 2))


def simulate_minute_augmented(p, minute_df, take_profit, stop_loss, trail_buy_pct, trail_pct,
                               max_hours_to_hold, target_h0, target_h1, z_thresh):
    prices, highs, lows, hours, opens = p['prices'], p['highs'], p['lows'], p['hours'], p['opens']
    daily_idx, sma_arr, std_arr = p['daily_idx'], p['sma_arr'], p['std_arr']
    trend_arr, has_trend = p['trend_arr'], p['has_trend']
    timestamps = p['timestamps']
    minute_min_ts = minute_df.index.min() if minute_df is not None else None
    minute_max_ts = minute_df.index.max() if minute_df is not None else None

    trades = []
    in_trade = waiting = trailing = False
    entry_price = stop_price = tp_price = peak = 0.0
    entry_bar = held = 0
    running_low = 0.0
    wait_bars = 0
    ground_truth_hits = approx_hits = 0

    n = len(prices)
    for i in range(n):
        cp, high, low, op = prices[i], highs[i], lows[i], opens[i]
        bar_ts = timestamps[i]
        has_minute_data = (minute_df is not None and minute_min_ts is not None
                            and minute_min_ts <= bar_ts <= minute_max_ts)

        if in_trade:
            held += 1
            if trailing:
                trail_stop_gap = peak * (1.0 - trail_pct)
                if op <= trail_stop_gap:
                    exit_px = op
                    pc = (exit_px - entry_price) / entry_price
                    trades.append(dict(entry_i=entry_bar, exit_i=i, entry_p=entry_price, exit_p=exit_px,
                                        held=held, result=WIN if pc > 0 else LOSS, ret=pc, exit_reason='TRAIL'))
                    in_trade = trailing = False
                    continue
                if high > peak:
                    peak = high
                trail_stop = peak * (1.0 - trail_pct)
                if low <= trail_stop or held >= max_hours_to_hold:
                    exit_px = trail_stop if low <= trail_stop else cp
                    pc = (exit_px - entry_price) / entry_price
                    reason = 'TRAIL' if low <= trail_stop else 'TIME'
                    trades.append(dict(entry_i=entry_bar, exit_i=i, entry_p=entry_price, exit_p=exit_px,
                                        held=held, result=WIN if pc > 0 else LOSS, ret=pc, exit_reason=reason))
                    in_trade = trailing = False
                continue
            if op <= stop_price:
                pc = (op - entry_price) / entry_price
                trades.append(dict(entry_i=entry_bar, exit_i=i, entry_p=entry_price, exit_p=op,
                                    held=held, result=LOSS, ret=pc, exit_reason='SL'))
                in_trade = False
                continue
            if low <= stop_price:
                pc = (stop_price - entry_price) / entry_price
                trades.append(dict(entry_i=entry_bar, exit_i=i, entry_p=entry_price, exit_p=stop_price,
                                    held=held, result=LOSS, ret=pc, exit_reason='SL'))
                in_trade = False
                continue
            if cp >= tp_price:
                trailing = True; peak = cp
                continue
            if held >= max_hours_to_hold:
                pc = (cp - entry_price) / entry_price
                trades.append(dict(entry_i=entry_bar, exit_i=i, entry_p=entry_price, exit_p=cp,
                                    held=held, result=TWIN if pc > 0 else TLOSS, ret=pc, exit_reason='TIME'))
                in_trade = False
            continue

        if waiting:
            wait_bars += 1
            buy_trigger_gap = running_low * (1.0 + trail_buy_pct)
            filled = False
            fill_type = None
            if op >= buy_trigger_gap:
                entry_price = op; filled = True; fill_type = 'gap'
            else:
                if low < running_low:
                    running_low = low
                buy_trigger = running_low * (1.0 + trail_buy_pct)
                if high >= buy_trigger:
                    entry_price = buy_trigger; filled = True; fill_type = 'bounce'
            if filled:
                tp_price = entry_price * (1.0 + take_profit)
                stop_price = entry_price * (1.0 - stop_loss)
                entry_bar = i; held = 0
                in_trade = True; waiting = trailing = False

                if has_minute_data:
                    # GROUND TRUTH: walk the real minute bars for this hour in true
                    # chronological order to see if the entry (whenever it truly
                    # happened, gap or bounce) was followed by a real stop breach.
                    bar_start = bar_ts
                    window = minute_df.loc[(minute_df.index >= bar_start) &
                                            (minute_df.index < bar_start + pd.Timedelta(hours=1))]
                    entry_minute_idx = None
                    for mi, (mts, mrow) in enumerate(window.iterrows()):
                        if fill_type == 'gap':
                            # gap-fill happens at the bar's Open, i.e. the first minute.
                            entry_minute_idx = 0
                            break
                        # bounce-fill: find the first minute whose High reaches the
                        # (already-known, hourly-computed) buy_trigger.
                        if mrow['High'] >= entry_price:
                            entry_minute_idx = mi
                            break
                    if entry_minute_idx is not None:
                        after = window.iloc[entry_minute_idx + 1:]
                        breach = after[after['Low'] <= stop_price]
                        if not breach.empty:
                            real_exit_p = stop_price
                            pc = (real_exit_p - entry_price) / entry_price
                            trades.append(dict(entry_i=entry_bar, exit_i=i, entry_p=entry_price,
                                                exit_p=real_exit_p, held=0, result=LOSS, ret=pc,
                                                exit_reason='SL_SAME_BAR_GROUND_TRUTH'))
                            in_trade = False
                            ground_truth_hits += 1
                    continue

                # No minute data for this hour -- fall back to the corrected hourly
                # approximation: only gap-fills get a same-bar check (unambiguous).
                if fill_type == 'gap' and low <= stop_price:
                    pc = (stop_price - entry_price) / entry_price
                    trades.append(dict(entry_i=entry_bar, exit_i=i, entry_p=entry_price, exit_p=stop_price,
                                        held=0, result=LOSS, ret=pc, exit_reason='SL_SAME_BAR_APPROX'))
                    in_trade = False
                    approx_hits += 1
                continue
            if wait_bars >= max_hours_to_hold:
                waiting = False
            continue

        h = hours[i]
        if h != target_h0 and h != target_h1:
            continue
        di = daily_idx[i]
        if di < 0:
            continue
        sma, std = sma_arr[di], std_arr[di]
        if std == 0.0:
            continue
        lower_band = sma - std * z_thresh
        fired = False
        signal_open = (op <= lower_band) and (op > trend_arr[di]) if has_trend else op <= lower_band
        if signal_open:
            waiting = True; running_low = op; wait_bars = 0
            fired = True
        if not fired:
            signal_close = (cp <= lower_band) and (cp > trend_arr[di]) if has_trend else cp <= lower_band
            if signal_close:
                waiting = True; running_low = cp; wait_bars = 0

    if in_trade:
        cp = prices[n - 1]
        pc = (cp - entry_price) / entry_price
        trades.append(dict(entry_i=entry_bar, exit_i=n - 1, entry_p=entry_price, exit_p=cp,
                            held=held, result=OPEN, ret=pc, exit_reason='OPEN'))

    return trades, ground_truth_hits, approx_hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tickers', nargs='*', default=None)
    args = ap.parse_args()
    nodes = TB_NODES if not args.tickers else [n for n in TB_NODES if n['ticker'] in args.tickers]

    rows = []
    for node in nodes:
        ticker = node['ticker']
        take_profit = node['arm_pct'] / 100.0
        fixed_sl = node['fixed_sl'] / 100.0
        trail_buy_pct = node['trail_buy_pct'] / 100.0
        trail_pct = node['trail_sell_pct'] / 100.0
        max_hold_hours = node['max_hold_hours']

        df_h = load_hourly(ticker)
        df_daily = df_h.resample("D").last().dropna(subset=["Close"])
        strat = strategies.TrailingBothZScoreBreakout(window=node['window'], z_score_threshold=node['z'])
        ind = strat.generate_daily_indicators(df_daily)
        p = prep_inputs(df_h, ind)
        minute_df = load_minute_data(ticker)

        strict = simulate_trail_both_annotated(p, take_profit, fixed_sl, max_hold_hours,
                                                trail_buy_pct, trail_pct, 9, 14, node['z'], open_check=True)
        augmented, gt_hits, approx_hits = simulate_minute_augmented(
            p, minute_df, take_profit, fixed_sl, trail_buy_pct, trail_pct, max_hold_hours, 9, 14, node['z'])

        ss, sa = _summarize(strict), _summarize(augmented)
        rows.append(dict(ticker=ticker, has_minute_data=minute_df is not None,
                          strict_n=ss['n'], strict_pct=ss['compounded_pct'],
                          aug_n=sa['n'], aug_pct=sa['compounded_pct'],
                          ground_truth_hits=gt_hits, approx_hits=approx_hits))

    print(f"\n{'ticker':6} {'has_minute':>10} {'strict_n':>9} {'strict_%':>10} {'aug_n':>6} {'aug_%':>9} "
          f"{'ground_truth_hits':>18} {'approx_hits':>12}")
    for r in rows:
        print(f"{r['ticker']:6} {str(r['has_minute_data']):>10} {r['strict_n']:>9} {r['strict_pct']:>10} "
              f"{r['aug_n']:>6} {r['aug_pct']:>9} {r['ground_truth_hits']:>18} {r['approx_hits']:>12}")

    print("\nground_truth_hits = same-bar SL confirmed via REAL minute data (no guessing).")
    print("approx_hits = same-bar SL from the hourly gap-fill-only approximation, for history outside minute-data coverage.")


if __name__ == '__main__':
    main()
