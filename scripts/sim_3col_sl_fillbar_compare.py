"""3-way comparison: strict backtest kernel vs. 5-min-resolution live-mimic sim, with SL
tested under two modes -- 'current' (SL enforced continuously from the moment of fill,
matching live's real today-behavior) and 'fixed' (SL suppressed until the position crosses
into the next real hourly bar boundary after its own fill, matching the backtest kernel's
actual gating exactly -- the decided-but-unbuilt live fix). Built 2026-08-21, forked from
scripts/sim_5min_backtest_mimic.py -- that script's arm/TP check (`cp >= tp_price`) ran on
EVERY 5-min bar with no bar-close gating, an unintended second variable (real live/kernel
both only evaluate arm at the hourly bar-close checkpoint) that invalidated its GDXU/arm
finding (see docs/backlog_cache.md's 2026-08-21 (later still) entry -- withdrawn as a SIM
bug, not a real finding). This script isolates SL-only: arm-check here is gated to the same
real HH:25/HH:30 checkpoints as entry-signal detection, held IDENTICAL across all three
columns -- only the SL enforcement axis varies.

Original docstring (5-minute-bar mirror of backtester.py's _simulate_trail_both /
_simulate_trail state machines, built 2026-08-20 for the real-money SOXL double-SL-loss
investigation -- two real SL exits same day, $371 combined, 2026-08-20) -- see
docs/research_log.md's 2026-08-20 entry for the original writeup this script forked from.

Real question: does the strategy, fed genuinely finer-grained (5-min, not hourly) real
price data -- much closer to what live's continuous polling/broker-side trailing order
actually sees -- produce the same-day multiple-entry-after-SL pattern seen live for SOXL,
and how common/costly is it across the other real capital-at-stake nodes?

Design (deliberately NOT a full re-derivation of the kernel from scratch -- ported
directly from backtester.py's _simulate_trail_both (TrailingBothZScoreBreakout, real
trailing-buy bounce-fill entry) and _simulate_trail (TrailingExitZScoreBreakout,
immediate-entry) 'possible' resolution only):
  - Entry SIGNAL detection stays anchored to the real two-checkpoint clock windows the
    live daemon actually polls (open_check @ HH:30, close_check @ HH:25 next slot, hours
    9/14 -- see CLAUDE.md's Signal windows note), using DAILY-bar SMA/Std (window days,
    prior days only, matching signals_compute.compute_buy_signal's df_daily_prior slice)
    for the lower_band -- exactly the real live logic, at real precision (daily bars,
    not 5-min).
  - Once a signal fires (WAITING for TrailingBoth, or immediately in_trade for
    TrailingExit), all subsequent running_low/bounce-fill/SL/trailing-stop/TIME
    monitoring happens bar-by-bar over the 5-min series (continuous), not once per
    hour -- this is the actual point of the exercise. held/wait_bars count 5-min bars;
    max_hold_hours is converted to a bar-count cap (`* 12`, 12 five-min bars/hour) so
    real elapsed-time semantics are preserved.
  - All 12 real capital-at-stake nodes use entry_timing='open_check' as of 2026-08-20 --
    only that path is implemented (open check first, close check same real 2-checkpoint
    design as a fallback later in the same window).
  - same_day_block: confirmed live (accounts table, 2026-08-20) that brokerage/roth/ira
    are ALL cash_settlement_type='margin' now, so same_day_block never applies to any
    real capital-at-stake node -- hardcoded off here, not re-derived from a stale doc.
  - No trend filter (has_trend=False) -- neither live strategy class uses one.

Not a claimed drop-in replacement for the real numba kernel or a sweep-grade backtest --
a single-resolution ('possible': Low-before-High) pure-Python research tool, matching the
precedent of scripts/sim_chaos_monkey.py / scripts/sim_gap_policy.py (both single-
resolution export_trades mirrors built for one specific research question).

Usage:
    .venv/bin/python scripts/sim_5min_whipsaw.py [--tickers T ...] [--days N]
"""
import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

LIVE_DB = 'cache/live/trading_live.db'

# ticker -> (id, strategy, window, z, trail_buy_pct%, fixed_sl%, arm_sell_pct%/take_profit%,
#            trail_sell_pct%, max_hold_hours, account, starting_notional)
NODES = [
    dict(id=92,  ticker='SOXL', strategy='TrailingBothZScoreBreakout', window=10, z=1.0,
         trail_buy_pct=3.0, fixed_sl=2.0, arm_pct=30.0, trail_sell_pct=1.0, max_hold_hours=70,
         account='ira', notional=10000),
    dict(id=197, ticker='DPST', strategy='TrailingBothZScoreBreakout', window=20, z=1.0,
         trail_buy_pct=2.0, fixed_sl=3.0, arm_pct=30.0, trail_sell_pct=2.0, max_hold_hours=112,
         account='ira', notional=10000),
    dict(id=202, ticker='KORU', strategy='TrailingBothZScoreBreakout', window=20, z=1.5,
         trail_buy_pct=1.0, fixed_sl=3.0, arm_pct=14.0, trail_sell_pct=6.0, max_hold_hours=126,
         account='roth', notional=10000),
    dict(id=231, ticker='JNUG', strategy='TrailingBothZScoreBreakout', window=10, z=1.0,
         trail_buy_pct=1.0, fixed_sl=2.0, arm_pct=29.0, trail_sell_pct=1.0, max_hold_hours=112,
         account='roth', notional=10000),
    dict(id=235, ticker='HIBL', strategy='TrailingBothZScoreBreakout', window=20, z=1.0,
         trail_buy_pct=2.0, fixed_sl=3.0, arm_pct=12.0, trail_sell_pct=1.0, max_hold_hours=77,
         account='ira', notional=10000),
    dict(id=236, ticker='LABU', strategy='TrailingBothZScoreBreakout', window=20, z=1.0,
         trail_buy_pct=9.0, fixed_sl=2.0, arm_pct=30.0, trail_sell_pct=1.0, max_hold_hours=98,
         account='ira', notional=10000),
    dict(id=203, ticker='AGQ',  strategy='TrailingExitZScoreBreakout', window=10, z=1.0,
         trail_buy_pct=0.0, fixed_sl=2.0, arm_pct=8.0, trail_sell_pct=7.0, max_hold_hours=84,
         account='brokerage', notional=6000),
    dict(id=229, ticker='GDXU', strategy='TrailingExitZScoreBreakout', window=10, z=1.0,
         trail_buy_pct=0.0, fixed_sl=3.0, arm_pct=30.0, trail_sell_pct=10.0, max_hold_hours=112,
         account='brokerage', notional=5000),
    dict(id=230, ticker='NUGT', strategy='TrailingExitZScoreBreakout', window=10, z=1.0,
         trail_buy_pct=0.0, fixed_sl=3.0, arm_pct=26.0, trail_sell_pct=2.0, max_hold_hours=140,
         account='ira', notional=10000),
    dict(id=232, ticker='DFEN', strategy='TrailingExitZScoreBreakout', window=10, z=1.0,
         trail_buy_pct=0.0, fixed_sl=3.0, arm_pct=28.0, trail_sell_pct=1.0, max_hold_hours=140,
         account='roth', notional=10000),
    dict(id=233, ticker='UGL',  strategy='TrailingExitZScoreBreakout', window=10, z=1.0,
         trail_buy_pct=0.0, fixed_sl=1.0, arm_pct=2.0, trail_sell_pct=5.0, max_hold_hours=140,
         account='brokerage', notional=5000),
    dict(id=234, ticker='WEBL', strategy='TrailingExitZScoreBreakout', window=10, z=1.0,
         trail_buy_pct=0.0, fixed_sl=1.0, arm_pct=30.0, trail_sell_pct=10.0, max_hold_hours=91,
         account='brokerage', notional=5000),
]

BARS_PER_HOUR = 12  # 5-min bars


def load_real_trades(ticker):
    con = sqlite3.connect(LIVE_DB)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM trade_log WHERE ticker=? AND is_dry_run_sim=0 ORDER BY entry_time", (ticker,))]
    con.close()
    return rows


def build_daily_bands(ticker, window, z):
    """Bug fix 2026-08-20 (found via DPST's missed 7/23 trade): MUST source daily
    closes the same way the real kernel does -- df_h.resample('D').last() from the
    cached hourly CSV (scripts/drought_overlay_test.load_hourly) -- not an
    independently-fetched yfinance daily() series, which can diverge materially
    (confirmed: $134.84 vs the kernel's real $137.87 lower_band for DPST/7-23,
    a ~$3 gap that silently caused this sim to miss a real, kernel-confirmed
    trade entirely)."""
    from scripts.drought_overlay_test import load_hourly
    df_h = load_hourly(ticker)
    daily = df_h.resample('D').last().dropna(subset=['Close'])
    daily['SMA'] = daily['Close'].rolling(window=window).mean()
    daily['Std'] = daily['Close'].rolling(window=window).std()
    # shift(1): today's check uses PRIOR days' SMA/Std only, matching
    # signals_compute.compute_buy_signal's df_daily_prior slice.
    daily['SMA_prior'] = daily['SMA'].shift(1)
    daily['Std_prior'] = daily['Std'].shift(1)
    daily['lower_band'] = daily['SMA_prior'] - z * daily['Std_prior']
    return daily['lower_band']


def simulate(node, bars5, lower_band_by_day, hourly_index=None, sl_mode='current'):
    """Bar-by-bar simulation over 5-min bars. Returns list of trade dicts.

    Bug fix 2026-08-20 (found via DPST's TIME-exit date mismatch): max_hold_hours
    counts real KERNEL HOURLY BARS (7/trading day, 9:30-15:30 anchors), not a literal
    conversion to calendar hours -- backtester.py's `held` increments once per hourly
    bar regardless of the 6.5h real session length, so 112 "hours" is really 112
    hourly-bar-slots (~16 trading days), NOT 112*(60/5)=1344 five-min bars (~17.2
    trading days, since 5-min bars ARE literal calendar-time-accurate). The naive
    bars*12 conversion held DPST's real 7/23 position ~5 real days longer than the
    kernel would, landing its TIME exit on a materially different (worse) price.
    Fix: precompute the exact real cutoff timestamp by walking `hourly_index`
    (the same real hourly bar series the kernel itself uses) forward
    max_hold_hours bars from the entry's own hourly bar -- exact parity with the
    kernel's counting convention, not an approximation."""
    strategy = node['strategy']
    trail_buy = node['trail_buy_pct'] / 100.0
    fixed_sl = node['fixed_sl'] / 100.0
    arm_pct = node['arm_pct'] / 100.0
    trail_sell = node['trail_sell_pct'] / 100.0
    max_hold_hours = node['max_hold_hours']

    def time_cutoff(entry_ts):
        if hourly_index is None:
            return None
        # hourly_index (from the cached CSV) is naive US/Eastern; bars5's index may be
        # tz-aware -- strip tz before comparing, same convention as the rest of the project.
        naive_ts = entry_ts.tz_localize(None) if entry_ts.tzinfo is not None else entry_ts
        pos = hourly_index.searchsorted(naive_ts, side='right')
        cutoff_pos = pos - 1 + max_hold_hours
        if cutoff_pos >= len(hourly_index):
            return None  # never times out within the available data
        cutoff_naive = hourly_index[cutoff_pos]
        return cutoff_naive.tz_localize(entry_ts.tzinfo) if entry_ts.tzinfo is not None else cutoff_naive

    def next_bar_boundary(entry_ts):
        """Start timestamp of the real hourly bar immediately AFTER entry_ts's own
        hourly bar -- the kernel's exit-check never reaches the fill bar's own
        iteration (mutually-exclusive if/elif/else, entry consumes that bar), so
        the earliest bar the kernel's SL check ever runs against is this one.
        sl_mode='fixed' suppresses SL until real clock time reaches this boundary."""
        if hourly_index is None:
            return None
        naive_ts = entry_ts.tz_localize(None) if entry_ts.tzinfo is not None else entry_ts
        pos = hourly_index.searchsorted(naive_ts, side='right')
        if pos >= len(hourly_index):
            return None
        boundary_naive = hourly_index[pos]
        return boundary_naive.tz_localize(entry_ts.tzinfo) if entry_ts.tzinfo is not None else boundary_naive

    def anchor_span_id(ts):
        """Which real anchor-hour bar (the kernel's h==target_h0/target_h1 row)
        `ts` falls within, or None. The kernel's hourly bar labeled '9:30' spans
        real clock time 9:30:00-10:29:59 (bars labeled by start time); '14:30'
        spans 14:30:00-15:29:59."""
        d = ts.date()
        t = (ts.hour, ts.minute)
        if (9, 30) <= t < (10, 30):
            return (d, 'AM')
        if (14, 30) <= t < (15, 30):
            return (d, 'PM')
        return None

    trades = []
    state = 'idle'  # idle -> waiting (TrailingBoth only) -> in_trade -> (trailing sub-state)
    running_low = None
    wait_bars = 0
    wait_cutoff = None
    entry_price = entry_time = None
    stop_price = tp_price = None
    fill_boundary = None
    trailing = False
    peak = None
    held = 0
    cutoff = None
    last_exit_day = None
    # Mechanical backtest-mimic rule (2026-08-20): the kernel evaluates each
    # anchor-hour bar's entry check EXACTLY ONCE (Open-then-Close of that same
    # bar, sequential per-bar processing) -- if the node is occupied (waiting or
    # in_trade) at ANY point during that bar's real 1-hour span, no fresh entry
    # check ever happens for that span, even if the position exits partway
    # through and the node goes idle again before the span ends. consumed_span
    # tracks the (date, 'AM'/'PM') span already used up this way.
    consumed_span = None

    idx = bars5.index
    opens = bars5['Open'].values
    highs = bars5['High'].values
    lows = bars5['Low'].values
    closes = bars5['Close'].values

    for i in range(len(bars5)):
        ts = idx[i]
        op, high, low, cp = opens[i], highs[i], lows[i], closes[i]
        day = ts.date()
        hour, minute = ts.hour, ts.minute
        span = anchor_span_id(ts)
        if span is not None and state != 'idle':
            consumed_span = span

        if state == 'in_trade':
            held += 1
            if trailing:
                trail_stop_gap = peak * (1.0 - trail_sell)
                if op <= trail_stop_gap:
                    exit_px = op
                    pc = (exit_px - entry_price) / entry_price
                    trades.append(dict(ticker=node['ticker'], entry_time=entry_time, exit_time=ts,
                                        entry_price=entry_price, exit_price=exit_px,
                                        exit_reason='TRAIL', pnl_pct=pc * 100, held_bars=held))
                    state = 'idle'; trailing = False; last_exit_day = day
                    continue
                if high > peak:
                    peak = high
                trail_stop = peak * (1.0 - trail_sell)
                timed_out = cutoff is not None and ts >= cutoff
                if low <= trail_stop or timed_out:
                    exit_px = trail_stop if low <= trail_stop else cp
                    pc = (exit_px - entry_price) / entry_price
                    reason = 'TRAIL' if low <= trail_stop else 'TIME'
                    trades.append(dict(ticker=node['ticker'], entry_time=entry_time, exit_time=ts,
                                        entry_price=entry_price, exit_price=exit_px,
                                        exit_reason=reason, pnl_pct=pc * 100, held_bars=held))
                    state = 'idle'; trailing = False; last_exit_day = day
                continue
            # not yet armed: SL check (open-first gap, then intrabar low).
            # sl_mode='fixed' suppresses this until real clock time reaches the
            # start of the hourly bar AFTER the fill's own hourly bar -- matching
            # the kernel's mutually-exclusive-branch gap exactly (decided fix,
            # see docs/backlog_cache.md's SOXL/LABU same-bar-SL entry).
            sl_active = sl_mode == 'current' or fill_boundary is None or ts >= fill_boundary
            if sl_active:
                if op <= stop_price:
                    pc = (op - entry_price) / entry_price
                    trades.append(dict(ticker=node['ticker'], entry_time=entry_time, exit_time=ts,
                                        entry_price=entry_price, exit_price=op,
                                        exit_reason='SL', pnl_pct=pc * 100, held_bars=held))
                    state = 'idle'; last_exit_day = day
                    continue
                if low <= stop_price:
                    pc = (stop_price - entry_price) / entry_price
                    trades.append(dict(ticker=node['ticker'], entry_time=entry_time, exit_time=ts,
                                        entry_price=entry_price, exit_price=stop_price,
                                        exit_reason='SL', pnl_pct=pc * 100, held_bars=held))
                    state = 'idle'; last_exit_day = day
                    continue
            # Arm/TP check gated to a real hourly bar CLOSE -- the kernel's
            # `elif cp >= tp_price:` (backtester.py) runs on EVERY hourly bar
            # while in_trade, unconditional on target_h0/target_h1 (that gate
            # only applies to the idle->waiting entry-signal check). Live's
            # `check_exit` mirrors this via `at_bar_close`, also unconditional
            # on which hour. A 5-min bar is a real hourly close when its own
            # close time lands on the hour (minute==25 -> the bar spanning
            # HH:25-HH:30 is the last slice of the HH:30-labeled hourly bar),
            # for ALL hours 10 through 16 (the 9:30-10:30 hourly bar's close is
            # 10:30, i.e. minute==25/hour==10; the 15:30-16:00 hourly bar's
            # close is minute==25/hour==16). Confirmed bug found by review,
            # 2026-08-21: an earlier version of this gate copied the
            # entry-signal checkpoints (HH:30/HH:25, hours 9/14 only) here --
            # wrong axis, under-fired arm (missed 5 of 7 real hourly closes)
            # and over-fired it twice (9:30/14:30 aren't hourly closes at all).
            # Held IDENTICAL across sl_mode='current'/'fixed' -- arm is not the
            # axis under test here.
            # Confirmed bug found by adversarial review, 2026-08-21: minute==25 was
            # correct for 5-min bars only (that bar spans HH:25-HH:30, so its Close
            # IS the real hourly close). Fed 1-minute bars, minute==25 is 4 minutes
            # early and the wrong price. The real hourly close is represented by the
            # LAST minute bar before the hour boundary: minute==29 for the 6
            # bars that close cleanly on the hour (10:30 through 15:30), plus a
            # session-end special case for the 15:30-labeled hourly bar (spans
            # 15:30-16:30 in theory, but the market closes at 16:00 -- its real
            # close is represented by the session's last minute bar, 15:59, since
            # load_minute_data filters to t < 16:00).
            is_hourly_close = (minute == 29 and 10 <= hour <= 15) or (minute == 59 and hour == 15)
            if is_hourly_close and cp >= tp_price:
                trailing = True; peak = cp
                continue
            if cutoff is not None and ts >= cutoff:
                pc = (cp - entry_price) / entry_price
                trades.append(dict(ticker=node['ticker'], entry_time=entry_time, exit_time=ts,
                                    entry_price=entry_price, exit_price=cp,
                                    exit_reason='TIME', pnl_pct=pc * 100, held_bars=held))
                state = 'idle'; last_exit_day = day
            continue

        if state == 'waiting':
            wait_bars += 1
            buy_trigger_gap = running_low * (1.0 + trail_buy)
            if op >= buy_trigger_gap:
                entry_price = op; entry_time = ts
                tp_price = entry_price * (1.0 + arm_pct)
                stop_price = entry_price * (1.0 - fixed_sl)
                held = 0; cutoff = time_cutoff(ts); fill_boundary = next_bar_boundary(ts); state = 'in_trade'; trailing = False
                continue
            if low < running_low:
                running_low = low
            buy_trigger = running_low * (1.0 + trail_buy)
            if high >= buy_trigger:
                entry_price = buy_trigger; entry_time = ts
                tp_price = entry_price * (1.0 + arm_pct)
                stop_price = entry_price * (1.0 - fixed_sl)
                held = 0; cutoff = time_cutoff(ts); fill_boundary = next_bar_boundary(ts); state = 'in_trade'; trailing = False
                continue
            if wait_cutoff is not None and ts >= wait_cutoff:
                state = 'idle'
            continue

        # idle: only check the signal at the real live-daemon checkpoints --
        # open_check @ HH:30 (hour 9 or 14), close_check @ HH:25 (the last bar of
        # that hour, i.e. true hourly close), both hours 9/14 per target_hours=(9,14).
        if span is None or span == consumed_span:
            continue  # this anchor-hour bar's one entry opportunity is already used up
        is_open_ckpt = minute == 30 and hour in (9, 14)
        # Same 1-minute-bar fix as the arm-check above: the 9:30 bar's close is
        # represented by its own last minute (10:29), the 14:30 bar's close by 15:29.
        is_close_ckpt = minute == 29 and hour in (10, 15)
        if not (is_open_ckpt or is_close_ckpt):
            continue
        lb = lower_band_by_day.get(day)
        if lb is None or pd.isna(lb):
            continue
        price_to_check = op if is_open_ckpt else cp
        if price_to_check > lb:
            continue
        # signal fires
        if strategy == 'TrailingBothZScoreBreakout':
            state = 'waiting'; running_low = price_to_check; wait_bars = 0
            wait_cutoff = time_cutoff(ts)
        else:  # TrailingExitZScoreBreakout -- immediate entry, no waiting
            entry_price = price_to_check; entry_time = ts
            tp_price = entry_price * (1.0 + arm_pct)
            stop_price = entry_price * (1.0 - fixed_sl)
            held = 0; cutoff = time_cutoff(ts); fill_boundary = next_bar_boundary(ts); state = 'in_trade'; trailing = False

    # Mark-to-market OPEN trade if still in a position at window end -- matches
    # the kernel's own behavior (backtester.py's OPEN=4 result, emitted by
    # _simulate_trail_both/_simulate_trail when the loop ends still in_trade,
    # see backtester.py:1117/472). Without this the sim silently drops any
    # position still open at the data's last bar, while `strict` counts it --
    # confirmed bug found by review, 2026-08-21 (biases the sim's total toward
    # looking better than strict, not worse, but breaks apples-to-apples either way).
    if state == 'in_trade' and entry_price is not None:
        last_cp = closes[-1]
        pc = (last_cp - entry_price) / entry_price
        trades.append(dict(ticker=node['ticker'], entry_time=entry_time, exit_time=idx[-1],
                            entry_price=entry_price, exit_price=last_cp,
                            exit_reason='OPEN', pnl_pct=pc * 100, held_bars=held))

    return trades


def run_backtest_strict(node, start_date, end_date):
    """Strict column: the actual numba kernel, dispatched to the correct one per
    strategy via backtester.run_backtest_dispatch (the same single-source-of-truth
    dispatcher run_optimization_sweep.py itself uses) -- windowed to
    [start_date, end_date] via run_optimization_sweep's own
    _load_node_inputs/_window_prep. Confirmed bug found by review, 2026-08-21: an
    earlier version of this function hardcoded run_backtest_v110 (the TrailingBoth
    kernel) for ALL strategies, silently fabricating a nonexistent hybrid strategy
    for the 6 real TrailingExitZScoreBreakout nodes (their trail_buy_pct=0.0
    placeholder fed straight into _simulate_trail_both's bounce-fill logic)."""
    from run_optimization_sweep import _load_node_inputs, _window_prep
    from backtester import run_backtest_dispatch
    import strategies as strat_mod

    strat_cls = getattr(strat_mod, node['strategy'])
    df_hourly, df_daily, prep = _load_node_inputs(
        node['ticker'], strat_cls, node['strategy'], node['window'], node['z'])
    prep = _window_prep(prep, start_date, end_date)
    # run_backtest_dispatch is percent-scale (e.g. 2.0, not 0.02) -- NODES dicts
    # already store percent values, passed through as-is, matching
    # run_optimization_sweep.py's own convention (docs/design.md 'Grid axis
    # meaning by strategy').
    if strat_cls is strat_mod.TrailingBothZScoreBreakout:
        sl_raw = node['trail_buy_pct']
    else:  # TrailingExitZScoreBreakout -- trail_pct_pct axis unused by this kernel,
           # sl_raw carries the trailing-exit's own trail_sell_pct instead.
        sl_raw = node['trail_sell_pct']
    trades = run_backtest_dispatch(
        strat_cls, df_hourly, df_daily, node['ticker'],
        take_profit=node['arm_pct'], sl_raw=sl_raw, max_hours_to_hold=node['max_hold_hours'],
        z_score_threshold=node['z'], fixed_sl=node['fixed_sl'], trail_pct_pct=node['trail_sell_pct'],
        entry_timing='open_check', prep=prep)
    # Flat-notional (non-compounded) dollar sum -- same convention as the sim's own
    # total_pnl_dollar calc (sum of pct*notional per trade), for an apples-to-apples
    # comparison across all three columns.
    total_pnl = sum(((t['Exit Price'] - t['Entry Price']) / t['Entry Price']) for t in trades) * node['notional']
    return trades, round(total_pnl, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tickers', nargs='*', default=None)
    ap.add_argument('--days', type=int, default=60)
    args = ap.parse_args()

    nodes = NODES if not args.tickers else [n for n in NODES if n['ticker'] in args.tickers]

    summary = []
    for node in nodes:
        ticker = node['ticker']
        print(f"=== {ticker} ({node['strategy']}) ===", file=sys.stderr)
        try:
            bars5 = yf.download(ticker, period=f'{args.days}d', interval='5m',
                                 multi_level_index=False, progress=False)
        except Exception as e:
            print(f"  5m fetch failed: {e}", file=sys.stderr)
            continue
        if bars5.empty:
            print("  no 5m data", file=sys.stderr)
            continue
        if bars5.index.tz is not None:
            bars5.index = bars5.index.tz_convert('America/New_York')

        lower_band = build_daily_bands(ticker, node['window'], node['z'])
        lower_band_by_day = {d.date(): v for d, v in lower_band.items()}

        from scripts.export_trades import load_hourly
        df_h = load_hourly(ticker)
        hourly_index = df_h.index

        min_d, max_d = bars5.index[0].date(), bars5.index[-1].date()

        sim_current = simulate(node, bars5, lower_band_by_day, hourly_index=hourly_index, sl_mode='current')
        sim_fixed = simulate(node, bars5, lower_band_by_day, hourly_index=hourly_index, sl_mode='fixed')
        bt_trades, bt_pnl = run_backtest_strict(node, str(min_d), str(max_d))

        pnl_current = round(sum(t['pnl_pct'] for t in sim_current) / 100.0 * node['notional'], 2)
        pnl_fixed = round(sum(t['pnl_pct'] for t in sim_fixed) / 100.0 * node['notional'], 2)

        # Matched-pair diff (confirmed methodological gap found by review, 2026-08-21):
        # suppressing SL on the fill bar changes how long a position stays open, which
        # changes consumed_span, which changes which LATER trades even get a chance to
        # fire -- current vs fixed run genuinely different trade sequences after the
        # first divergence, so summing each run's full trade list and diffing the totals
        # blends the SL-timing effect with an unrelated sequence effect. This walks both
        # trade lists in parallel, matching on identical (entry_time, entry_price) --
        # trades before the first divergence are the ONLY ones that isolate the SL
        # effect alone; everything from the first mismatch on is a sequence effect, not
        # an SL-timing effect, and is reported separately.
        matched_pnl_delta = 0.0
        matched_n = 0
        first_divergence = None
        for tc, tf in zip(sim_current, sim_fixed):
            if tc['entry_time'] == tf['entry_time'] and abs(tc['entry_price'] - tf['entry_price']) < 1e-6:
                matched_pnl_delta += (tf['pnl_pct'] - tc['pnl_pct']) / 100.0 * node['notional']
                matched_n += 1
            else:
                first_divergence = str(tc['entry_time'])
                break
        else:
            if len(sim_current) != len(sim_fixed):
                first_divergence = f"trade-count mismatch after {matched_n} matched"

        summary.append(dict(
            ticker=ticker, strategy=node['strategy'],
            backtest_trades=len(bt_trades), backtest_pnl=bt_pnl,
            live_current_trades=len(sim_current), live_current_pnl=pnl_current,
            live_fixed_trades=len(sim_fixed), live_fixed_pnl=pnl_fixed,
            matched_n=matched_n, matched_sl_delta=round(matched_pnl_delta, 2),
            first_divergence=first_divergence,
        ))

    print()
    print(f"{'ticker':6} {'strict_trades':>13} {'strict_pnl($)':>14} "
          f"{'live_cur_trades':>16} {'live_cur_pnl($)':>16} "
          f"{'live_fix_trades':>16} {'live_fix_pnl($)':>16}")
    for r in summary:
        print(f"{r['ticker']:6} {r['backtest_trades']:>13} {r['backtest_pnl']:>14} "
              f"{r['live_current_trades']:>16} {r['live_current_pnl']:>16} "
              f"{r['live_fixed_trades']:>16} {r['live_fixed_pnl']:>16}")
    print()
    print(f"TOTAL strict:      {sum(r['backtest_pnl'] for r in summary):.2f}")
    print(f"TOTAL live-current: {sum(r['live_current_pnl'] for r in summary):.2f}")
    print(f"TOTAL live-fixed:   {sum(r['live_fixed_pnl'] for r in summary):.2f}")
    print()
    print("--- Matched-pair SL-timing effect only (trades identical in both runs, before first divergence) ---")
    print(f"{'ticker':6} {'matched_n':>10} {'sl_delta($)':>12} {'diverges_at':>22}")
    for r in summary:
        print(f"{r['ticker']:6} {r['matched_n']:>10} {r['matched_sl_delta']:>12} "
              f"{str(r['first_divergence']):>22}")
    print(f"TOTAL matched-pair SL-timing delta (fixed - current, isolated): "
          f"{sum(r['matched_sl_delta'] for r in summary):.2f}")

    return summary


if __name__ == '__main__':
    main()
