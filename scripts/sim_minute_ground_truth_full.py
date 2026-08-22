"""Full minute-resolution ground truth for BOTH the trailing-buy (entry/WAIT) and
trailing-sell (in-trade SL/TRAIL) state, wherever real 1-minute data covers it (the most
recent ~2yr of the ~3yr cached hourly history, via scripts/fetch_massive_minute_data.py).
Built 2026-08-21 (very late), same night as the original four findings -- see
docs/backlog_cache.md / docs/research_log.md.

Design: signal DETECTION stays anchored to the real 9:30/14:30 anchor-hour windows
(that's a genuine strategy design choice -- when live actually checks -- not a data-
resolution limitation, so it's untouched). Once a WAIT starts or a position is in_trade,
if the current hourly bar falls inside minute-data coverage, the WAIT-resolution and
in-trade (SL/TRAIL/TP/TIME) checks run against the REAL 1-minute bars in true
chronological order -- no possible/pessimistic/certain guessing, this is ground truth,
since we can observe the actual minute-by-minute path. Outside minute-data coverage
(the older ~1yr), falls back unchanged to the hourly hourly hourly kernel's own logic
(byte-identical to scripts/export_trades.py::simulate_trail_both_annotated, the
'possible' mirror already used everywhere else tonight).

held/wait_bars still count in HOURLY-bar units throughout (matching the kernel's own
convention -- max_hold_hours is an hourly-bar count, not a real-time duration), even
during minute-resolution sub-walks -- only incremented once per hourly bar, same as
always.

Usage:
    .venv/bin/python scripts/sim_minute_ground_truth_full.py [--tickers T ...] [--dump]
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
    df = pd.read_csv(path)
    # hourly kernel timestamps (p['timestamps']) are naive US/Eastern -- match that
    # convention here instead of staying tz-aware, same fix as sim_5min_backtest_mimic.py.
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("America/New_York").dt.tz_localize(None)
    df = df.set_index("timestamp").sort_index()
    # REGULAR SESSION ONLY (9:30-16:00 ET) -- the hourly kernel has zero visibility into
    # extended hours (its own hourly bars are regular-session-only), so overnight/
    # pre-market/after-hours minute data must never leak into running_low/peak tracking.
    # Enforced explicitly here, not just relied on via windowing, per the same-session
    # correction: "we do not use overnight data because the backtest doesn't know about it."
    t = df.index.time
    regular = (t >= pd.Timestamp("09:30").time()) & (t < pd.Timestamp("16:00").time())
    return df[regular]


def _minute_window(minute_df, bar_start_ts):
    return minute_df.loc[(minute_df.index >= bar_start_ts) &
                          (minute_df.index < bar_start_ts + pd.Timedelta(hours=1))]


def simulate_ground_truth(p, minute_df, take_profit, stop_loss, trail_buy_pct, trail_pct,
                           max_hours_to_hold, target_h0, target_h1, z_thresh):
    prices, highs, lows, hours, opens = p['prices'], p['highs'], p['lows'], p['hours'], p['opens']
    daily_idx, sma_arr, std_arr = p['daily_idx'], p['sma_arr'], p['std_arr']
    trend_arr, has_trend = p['trend_arr'], p['has_trend']
    timestamps = p['timestamps']
    minute_min = minute_df.index.min() if minute_df is not None else None
    minute_max = minute_df.index.max() if minute_df is not None else None

    trades = []
    in_trade = waiting = trailing = False
    entry_price = stop_price = tp_price = peak = 0.0
    entry_bar = held = 0
    running_low = 0.0
    wait_bars = 0
    gt_entry_events = gt_exit_events = 0

    n = len(prices)
    for i in range(n):
        cp, high, low, op = prices[i], highs[i], lows[i], opens[i]
        bar_ts = timestamps[i]
        has_minutes = (minute_df is not None and minute_min is not None
                        and minute_min <= bar_ts <= minute_max)
        window = _minute_window(minute_df, bar_ts) if has_minutes else None
        if has_minutes and len(window) < 55:
            # Sparse coverage (found 2026-08-21: DPST/KORU/JNUG/HIBL/LABU all have real,
            # substantial gaps -- lower-liquidity tickers don't trade every minute).
            # Trusting running_low/peak tracking against an incomplete minute record
            # would systematically miss real price extremes -- fall back to the hourly
            # approximation honestly rather than pretend we have ground truth here.
            has_minutes = False

        if in_trade:
            held += 1
            if has_minutes:
                exited = False
                for mts, mrow in window.iterrows():
                    mlow, mhigh, mclose = mrow['Low'], mrow['High'], mrow['Close']
                    if trailing:
                        mopen = mrow['Open']
                        old_trail_stop = peak * (1.0 - trail_pct)
                        if mopen <= old_trail_stop:
                            # gapped through the trailing-stop level before this minute's
                            # own High could even raise the peak -- credit the real,
                            # worse open price, not the nominal (possibly stale) stop.
                            real_exit_p = mopen
                        else:
                            if mhigh > peak:
                                peak = mhigh
                            real_trail_stop = peak * (1.0 - trail_pct)
                            real_exit_p = real_trail_stop if mlow <= real_trail_stop else None
                        if real_exit_p is not None:
                            pc = (real_exit_p - entry_price) / entry_price
                            trades.append(dict(entry_i=entry_bar, exit_i=i, entry_p=entry_price,
                                                exit_p=real_exit_p, held=held,
                                                result=WIN if pc > 0 else LOSS, ret=pc,
                                                exit_reason='TRAIL_GT'))
                            in_trade = trailing = False; exited = True; gt_exit_events += 1
                            break
                    else:
                        mopen = mrow['Open']
                        if mopen <= stop_price:
                            # gapped through before this minute even started -- credit the
                            # REAL, worse price (matches a real stop order's actual fill
                            # behavior), not the nominal trigger level.
                            real_exit_p = mopen
                        elif mlow <= stop_price:
                            real_exit_p = stop_price
                        else:
                            real_exit_p = None
                        if real_exit_p is not None:
                            pc = (real_exit_p - entry_price) / entry_price
                            trades.append(dict(entry_i=entry_bar, exit_i=i, entry_p=entry_price,
                                                exit_p=real_exit_p, held=held, result=LOSS, ret=pc,
                                                exit_reason='SL_GT'))
                            in_trade = False; exited = True; gt_exit_events += 1
                            break
                if exited:
                    continue
                # Arm check happens HOURLY only, matching the kernel exactly (cp>=tp_price
                # using the hourly bar's own Close) -- NOT per-minute. Fixed 2026-08-21:
                # was checking every real minute's Close, a genuine break from what the
                # backtest does (GDXU-style divergence), not a ground-truth refinement.
                if not trailing and cp >= tp_price:
                    trailing = True; peak = high
                if not trailing and held >= max_hours_to_hold:
                    pc = (cp - entry_price) / entry_price
                    trades.append(dict(entry_i=entry_bar, exit_i=i, entry_p=entry_price, exit_p=cp,
                                        held=held, result=TWIN if pc > 0 else TLOSS, ret=pc, exit_reason='TIME'))
                    in_trade = False
                elif trailing and held >= max_hours_to_hold:
                    trail_stop = peak * (1.0 - trail_pct)
                    exit_px = trail_stop if low <= trail_stop else cp
                    pc = (exit_px - entry_price) / entry_price
                    trades.append(dict(entry_i=entry_bar, exit_i=i, entry_p=entry_price, exit_p=exit_px,
                                        held=held, result=WIN if pc > 0 else LOSS, ret=pc, exit_reason='TIME'))
                    in_trade = trailing = False
                continue

            # No minute data -- hourly kernel logic, unchanged.
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
            if has_minutes:
                filled = False
                fill_pos = None
                for mpos, (mts, mrow) in enumerate(window.iterrows()):
                    mlow, mhigh, mopen = mrow['Low'], mrow['High'], mrow['Open']
                    trigger = running_low * (1.0 + trail_buy_pct)
                    if mopen >= trigger:
                        # gapped through the trigger before this minute even started --
                        # real fill is at the actual (worse, higher-for-a-buyer) open,
                        # not the nominal trigger level.
                        entry_price = mopen; filled = True; fill_pos = mpos
                        break
                    if mhigh >= trigger:
                        entry_price = trigger; filled = True; fill_pos = mpos
                        break
                    if mlow < running_low:
                        running_low = mlow
                if filled:
                    # NOTE (corrected, per this session's own Principle 1 conclusion):
                    # deliberately NOT checking for a same-bar SL breach here, even
                    # though real minute data could answer it. The validated kernel
                    # ('strict') structurally never evaluates the fill bar's own exit
                    # conditions -- that's not a data-resolution gap to fix, it's the
                    # validated decision process itself. Checking it here would be
                    # modeling live's CURRENT (unvalidated, diverges-from-kernel)
                    # same-bar-checking behavior, not a ground-truth refinement of the
                    # kernel's own logic. Real minute data is only used to resolve
                    # ambiguity WITHIN the kernel's existing rules (which fill price,
                    # which trailing-stop ordering), never to add a check the kernel
                    # itself never performs.
                    tp_price = entry_price * (1.0 + take_profit)
                    stop_price = entry_price * (1.0 - stop_loss)
                    entry_bar = i; held = 0
                    in_trade = True; waiting = trailing = False
                    gt_entry_events += 1
                    continue
                if wait_bars >= max_hours_to_hold:
                    waiting = False
                continue

            # No minute data -- hourly kernel WAIT logic, unchanged ('possible' resolution).
            buy_trigger_gap = running_low * (1.0 + trail_buy_pct)
            filled = False
            if op >= buy_trigger_gap:
                entry_price = op; filled = True
            else:
                if low < running_low:
                    running_low = low
                buy_trigger = running_low * (1.0 + trail_buy_pct)
                if high >= buy_trigger:
                    entry_price = buy_trigger; filled = True
            if filled:
                tp_price = entry_price * (1.0 + take_profit)
                stop_price = entry_price * (1.0 - stop_loss)
                entry_bar = i; held = 0
                in_trade = True; waiting = trailing = False
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

    return trades, gt_entry_events, gt_exit_events


def _summarize(trades):
    compounded = 1.0
    for t in trades:
        compounded *= (1.0 + t['ret'])
    return dict(n=len(trades), compounded_pct=round((compounded - 1.0) * 100, 2))


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
        gt, entries, exits = simulate_ground_truth(p, minute_df, take_profit, fixed_sl, trail_buy_pct,
                                                     trail_pct, max_hold_hours, 9, 14, node['z'])

        ss, sg = _summarize(strict), _summarize(gt)
        rows.append(dict(ticker=ticker, has_minute=minute_df is not None,
                          strict_n=ss['n'], strict_pct=ss['compounded_pct'],
                          gt_n=sg['n'], gt_pct=sg['compounded_pct'],
                          gt_entries=entries, gt_exits=exits))

    print(f"\n{'ticker':6} {'has_minute':>10} {'strict_n':>9} {'strict_%':>10} {'gt_n':>6} {'gt_%':>9} "
          f"{'gt_entry_events':>15} {'gt_exit_events':>14}")
    for r in rows:
        print(f"{r['ticker']:6} {str(r['has_minute']):>10} {r['strict_n']:>9} {r['strict_pct']:>10} "
              f"{r['gt_n']:>6} {r['gt_pct']:>9} {r['gt_entries']:>15} {r['gt_exits']:>14}")

    print("\ngt = ground-truth minute-resolution walk (real data where covered, hourly fallback otherwise).")
    print("gt_entry_events/gt_exit_events = how many WAIT-fills / in-trade-exits were resolved via real minute data.")


if __name__ == '__main__':
    main()
