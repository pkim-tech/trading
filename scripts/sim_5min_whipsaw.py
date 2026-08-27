"""5-minute-bar mirror of backtester.py's _simulate_trail_both / _simulate_trail state
machines, built 2026-08-20 for the real-money SOXL double-SL-loss investigation (two real
SL exits same day, $371 combined, 2026-08-20) -- see docs/research_log.md's 2026-08-20
entry for the writeup this script produced.

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


def simulate(node, bars5, lower_band_by_day, hourly_index=None):
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

    trades = []
    state = 'idle'  # idle -> waiting (TrailingBoth only) -> in_trade -> (trailing sub-state)
    running_low = None
    wait_bars = 0
    wait_cutoff = None
    entry_price = entry_time = None
    stop_price = tp_price = None
    trailing = False
    peak = None
    held = 0
    cutoff = None
    last_exit_day = None

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
            # not yet armed: SL check (open-first gap, then intrabar low)
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
            if cp >= tp_price:
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
                held = 0; cutoff = time_cutoff(ts); state = 'in_trade'; trailing = False
                continue
            if low < running_low:
                running_low = low
            buy_trigger = running_low * (1.0 + trail_buy)
            if high >= buy_trigger:
                entry_price = buy_trigger; entry_time = ts
                tp_price = entry_price * (1.0 + arm_pct)
                stop_price = entry_price * (1.0 - fixed_sl)
                held = 0; cutoff = time_cutoff(ts); state = 'in_trade'; trailing = False
                continue
            if wait_cutoff is not None and ts >= wait_cutoff:
                state = 'idle'
            continue

        # idle: only check the signal at the real live-daemon checkpoints --
        # open_check @ HH:30 (hour 9 or 14), close_check @ HH:25 (the last bar of
        # that hour, i.e. true hourly close), both hours 9/14 per target_hours=(9,14).
        is_open_ckpt = minute == 30 and hour in (9, 14)
        is_close_ckpt = minute == 25 and hour in (10, 15)
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
            held = 0; cutoff = time_cutoff(ts); state = 'in_trade'; trailing = False

    return trades


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tickers', nargs='*', default=None)
    ap.add_argument('--days', type=int, default=60)
    args = ap.parse_args()

    nodes = NODES if not args.tickers else [n for n in NODES if n['ticker'] in args.tickers]

    summary = []
    all_sim_trades = {}
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

        sim_trades = simulate(node, bars5, lower_band_by_day, hourly_index=hourly_index)
        all_sim_trades[ticker] = sim_trades

        real_trades = load_real_trades(ticker)
        min_d, max_d = bars5.index[0].date(), bars5.index[-1].date()
        real_in_window = [t for t in real_trades
                           if min_d <= pd.Timestamp(t['entry_time']).date() <= max_d]

        # same-day multi-entry + net cost, from the SIMULATION itself
        by_day = {}
        for t in sim_trades:
            by_day.setdefault(t['entry_time'].date(), []).append(t)
        multi_days = {d: ts for d, ts in by_day.items() if len(ts) >= 2}
        sim_multi_sl_cost = 0.0
        cost_day_detail = []
        for d, ts in multi_days.items():
            sl_trades = [t for t in ts if t['exit_reason'] == 'SL']
            if len(sl_trades) >= 2:
                dollar = sum(t['pnl_pct'] for t in sl_trades) / 100.0 * node['notional']
                sim_multi_sl_cost += dollar
                cost_day_detail.append((str(d), len(sl_trades),
                                         round(sum(t['pnl_pct'] for t in sl_trades), 2), round(dollar, 2)))

        total_pnl_dollar = sum(t['pnl_pct'] for t in sim_trades) / 100.0 * node['notional']

        summary.append(dict(
            ticker=ticker, sim_trades=len(sim_trades), sim_days_multi_entry=len(multi_days),
            sim_days_multi_sl_loss=len(cost_day_detail), sim_multi_sl_cost=round(sim_multi_sl_cost, 2),
            sim_total_pnl=round(total_pnl_dollar, 2),
            real_trades_in_window=len(real_in_window),
        ))
        for row in cost_day_detail:
            print(f"    {ticker} {row}", file=sys.stderr)

    print()
    print(f"{'ticker':6} {'sim_trades':>10} {'multi_entry_days':>17} {'multi_SL_days':>14} "
          f"{'multi_SL_cost($)':>17} {'sim_total_pnl($)':>17} {'real_trades':>12}")
    for r in summary:
        print(f"{r['ticker']:6} {r['sim_trades']:>10} {r['sim_days_multi_entry']:>17} "
              f"{r['sim_days_multi_sl_loss']:>14} {r['sim_multi_sl_cost']:>17} "
              f"{r['sim_total_pnl']:>17} {r['real_trades_in_window']:>12}")
    print()
    print(f"TOTAL multi-SL-day cost across nodes: {sum(r['sim_multi_sl_cost'] for r in summary):.2f}")

    return all_sim_trades, summary


if __name__ == '__main__':
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
