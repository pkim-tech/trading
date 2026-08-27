"""Live-mimic baseline, built 2026-08-21 (night) as shared ground truth for comparing
3 candidate fixes (see docs/backlog_cache.md / docs/session_cache.md's 2026-08-20 (late)
and 2026-08-21 entries) for the confirmed SOXL live-vs-backtest divergence.

Real confirmed incident (checked directly against trade_log, not assumed):
  SOXL/ira, 2026-08-20: entry 09:32:25 (from a GTC trailing-buy order that rested overnight
  from 2026-08-19 14:30's signal, filled 09:31:43 per broker records) -> exit 09:45:50, SL,
  entry_price=122.985, exit_price=120.52. SECOND trade: entry 11:05:04 -> exit 11:11:30, SL,
  entry_price=124.3911, exit_price=121.87.
The strict hourly kernel (scripts/export_trades.py::simulate_trail_both_annotated, the
verified 'possible'-resolution mirror) CANNOT produce a second trade here: its per-bar loop
processes bar i=9:30 (spanning 09:30-10:30) via if-in_trade/elif-waiting/else exclusively --
resolving the carried-over WAIT consumes that bar's iteration; the bar's OWN independent
Open/Close-crossing-lower-band condition (real: Open $119.99 z=-1.795, Close $121.39
z=-1.647, both independently below threshold -- confirmed 2026-08-20) is never separately
evaluated once the carry-resolution claims that bar.

Live's REAL bookkeeping (active_signals.py::_scan_buy_signals, read directly 2026-08-21)
doesn't have this same-bar exclusivity: `buy_alerted` is cleared once `closed_today` is
true, and a node becomes eligible for a completely fresh signal check the next time
compute_buy_signal is evaluated (open-check ~9:31-9:40 / close-check ~10:25-10:40, both of
which read the SAME 9:30 hourly bar, just at different real wall-clock times ~1 hour apart).
Trade 1 opened+closed entirely within that ~13-minute span (09:32-09:45), well inside the
9:30-10:30 bar -- by the time the close-check window runs (10:25-10:40), the node is flat
again and the SAME 9:30 bar's own Close condition can independently re-fire, placing a
second (real, resting) trailing-buy order that filled later (11:05) once price bounced.

This script approximates that real mechanism at HOURLY-bar granularity (an approximation,
not a tick-perfect live replica -- flagged explicitly, calibrate against the real SOXL
2026-08-20 trade_log rows above before trusting this for any other ticker/day):
  - Entry/exit mechanics for the CURRENT trade are byte-identical to
    simulate_trail_both_annotated (same WAIT/bounce-fill, same SL/TP/trailing/TIME).
  - The one addition: when a WAIT resolves into in_trade on bar i, if that WAIT's ORIGINAL
    signal bar's calendar date is EARLIER than bar i's own date (a genuine overnight/
    multi-day carry -- the exact condition that created tonight's ambiguity), an INLINE
    same-bar SL check runs immediately (using bar i's own Low, entry from bar i's Open --
    the strict kernel never checks the fill bar itself; this is the deliberate deviation
    modeling live's continuous intrabar monitoring). If breached, the position closes on
    bar i itself (not bar i+1), and the normal signal-detection branch (open_check then
    close_check) is evaluated AGAIN on that SAME bar i, as a completely independent fresh
    check -- exactly modeling live's separate open-check/close-check re-derivation of the
    same anchor bar once the node goes flat again mid-bar.
  - Only SL is checked inline (not TP/trailing/TIME) -- matches the real incident's shape
    and keeps the approximation narrow/falsifiable rather than overfit to make numbers move.

Usage:
    .venv/bin/python scripts/sim_live_mimic_baseline.py [--tickers T ...] [--dump]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategies
from backtester import prep_inputs, WIN, LOSS, TWIN, TLOSS, OPEN, _RESULT_NAMES
from scripts.export_trades import load_hourly, simulate_trail_both_annotated
from scripts.sim_5min_whipsaw import NODES

TB_NODES = [n for n in NODES if n['strategy'] == 'TrailingBothZScoreBreakout']


def simulate_live_mimic(p, take_profit, stop_loss, trail_buy_pct, trail_pct,
                         max_hours_to_hold, target_h0, target_h1, z_thresh):
    prices, highs, lows, hours, opens = p['prices'], p['highs'], p['lows'], p['hours'], p['opens']
    daily_idx, sma_arr, std_arr = p['daily_idx'], p['sma_arr'], p['std_arr']
    trend_arr, has_trend = p['trend_arr'], p['has_trend']
    dates = p['timestamps'].date

    trades = []
    in_trade = waiting = trailing = False
    entry_price = stop_price = tp_price = peak = 0.0
    entry_bar = held = 0
    running_low = 0.0
    wait_bars = 0
    signal_bar = None
    signal_z = None
    signal_date = None
    arm_bar = None
    carried_reentry_count = 0

    def check_signal(i, op, cp, allow_open=True):
        """Returns True if a fresh WAIT was started on bar i (mutates outer state).
        Mirrors simulate_trail_both_annotated's signal-detection branch exactly."""
        nonlocal waiting, running_low, wait_bars, signal_bar, signal_z, signal_date
        di = daily_idx[i]
        if di < 0:
            return False
        sma, std = sma_arr[di], std_arr[di]
        if std == 0.0:
            return False
        lower_band = sma - std * z_thresh
        if allow_open:
            signal_open = (op <= lower_band) and (op > trend_arr[di]) if has_trend else op <= lower_band
            if signal_open:
                waiting = True; running_low = op; wait_bars = 0
                signal_bar = i; signal_z = (op - sma) / std; signal_date = dates[i]
                return True
        signal_close = (cp <= lower_band) and (cp > trend_arr[di]) if has_trend else cp <= lower_band
        if signal_close:
            waiting = True; running_low = cp; wait_bars = 0
            signal_bar = i; signal_z = (cp - sma) / std; signal_date = dates[i]
            return True
        return False

    n = len(prices)
    for i in range(n):
        cp, high, low, op = prices[i], highs[i], lows[i], opens[i]

        if in_trade:
            held += 1
            if trailing:
                trail_stop_gap = peak * (1.0 - trail_pct)
                if op <= trail_stop_gap:
                    exit_px = op
                    pc = (exit_px - entry_price) / entry_price
                    trades.append(dict(signal_i=signal_bar, entry_i=entry_bar, arm_i=arm_bar, exit_i=i,
                                        entry_p=entry_price, exit_p=exit_px, held=held,
                                        result=WIN if pc > 0 else LOSS, ret=pc, exit_reason='TRAIL'))
                    in_trade = trailing = False
                    continue
                if high > peak:
                    peak = high
                trail_stop = peak * (1.0 - trail_pct)
                if low <= trail_stop or held >= max_hours_to_hold:
                    exit_px = trail_stop if low <= trail_stop else cp
                    pc = (exit_px - entry_price) / entry_price
                    reason = 'TRAIL' if low <= trail_stop else 'TIME'
                    trades.append(dict(signal_i=signal_bar, entry_i=entry_bar, arm_i=arm_bar, exit_i=i,
                                        entry_p=entry_price, exit_p=exit_px, held=held,
                                        result=WIN if pc > 0 else LOSS, ret=pc, exit_reason=reason))
                    in_trade = trailing = False
                continue
            if op <= stop_price:
                pc = (op - entry_price) / entry_price
                trades.append(dict(signal_i=signal_bar, entry_i=entry_bar, arm_i=arm_bar, exit_i=i,
                                    entry_p=entry_price, exit_p=op, held=held, result=LOSS, ret=pc, exit_reason='SL'))
                in_trade = False
                continue
            if low <= stop_price:
                pc = (stop_price - entry_price) / entry_price
                trades.append(dict(signal_i=signal_bar, entry_i=entry_bar, arm_i=arm_bar, exit_i=i,
                                    entry_p=entry_price, exit_p=stop_price, held=held, result=LOSS, ret=pc, exit_reason='SL'))
                in_trade = False
                continue
            if cp >= tp_price:
                trailing = True; peak = cp; arm_bar = i
                continue
            if held >= max_hours_to_hold:
                pc = (cp - entry_price) / entry_price
                trades.append(dict(signal_i=signal_bar, entry_i=entry_bar, arm_i=arm_bar, exit_i=i,
                                    entry_p=entry_price, exit_p=cp, held=held,
                                    result=TWIN if pc > 0 else TLOSS, ret=pc, exit_reason='TIME'))
                in_trade = False
            continue

        if waiting:
            wait_bars += 1
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
                is_carried = signal_date is not None and signal_date < dates[i]
                tp_price = entry_price * (1.0 + take_profit)
                stop_price = entry_price * (1.0 - stop_loss)
                entry_bar = i; held = 0; arm_bar = None
                in_trade = True; waiting = trailing = False
                if is_carried:
                    # LIVE-MIMIC DEVIATION: inline same-bar SL check + independent
                    # re-derivation of this same bar's own signal (see module docstring).
                    if low <= stop_price:
                        pc = (stop_price - entry_price) / entry_price
                        trades.append(dict(signal_i=signal_bar, entry_i=entry_bar, arm_i=None, exit_i=i,
                                            entry_p=entry_price, exit_p=stop_price, held=0, result=LOSS,
                                            ret=pc, exit_reason='SL_SAME_BAR_CARRIED'))
                        in_trade = False
                        carried_reentry_count += 1
                        check_signal(i, op, cp, allow_open=False)  # close-check re-derivation only
                continue
            if wait_bars >= max_hours_to_hold:
                waiting = False
            continue

        h = hours[i]
        if h != target_h0 and h != target_h1:
            continue
        check_signal(i, op, cp, allow_open=True)

    if in_trade:
        cp = prices[n - 1]
        pc = (cp - entry_price) / entry_price
        trades.append(dict(signal_i=signal_bar, entry_i=entry_bar, arm_i=arm_bar, exit_i=n - 1,
                            entry_p=entry_price, exit_p=cp, held=held, result=OPEN, ret=pc, exit_reason='OPEN'))

    return trades, carried_reentry_count


def _summarize(trades):
    compounded = 1.0
    for t in trades:
        compounded *= (1.0 + t['ret'])
    return dict(n=len(trades), compounded_pct=round((compounded - 1.0) * 100, 2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tickers', nargs='*', default=None)
    ap.add_argument('--dump', action='store_true')
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

        baseline = simulate_trail_both_annotated(p, take_profit, fixed_sl, max_hold_hours,
                                                   trail_buy_pct, trail_pct, 9, 14, node['z'], open_check=True)
        mimic, carried_hits = simulate_live_mimic(p, take_profit, fixed_sl, trail_buy_pct, trail_pct,
                                                    max_hold_hours, 9, 14, node['z'])
        b, m = _summarize(baseline), _summarize(mimic)
        rows.append(dict(ticker=ticker, base_n=b['n'], base_pct=b['compounded_pct'],
                          mimic_n=m['n'], mimic_pct=m['compounded_pct'], carried_hits=carried_hits))

        if args.dump and ticker == 'SOXL':
            timestamps = p['timestamps']
            print(f"\n=== SOXL live-mimic trades on/around 2026-08-20 ===")
            for t in mimic:
                et = timestamps[t['entry_i']]
                if et.date().isoformat() in ('2026-08-19', '2026-08-20'):
                    print(f"  entry={et} exit={timestamps[t['exit_i']]} "
                          f"entry_p={t['entry_p']:.3f} exit_p={t['exit_p']:.3f} "
                          f"ret={t['ret']*100:.2f}% {t['exit_reason']}")

    print(f"\n{'ticker':6} {'base_n':>7} {'base_%':>10} {'mimic_n':>8} {'mimic_%':>10} {'carried_hits':>13}")
    for r in rows:
        print(f"{r['ticker']:6} {r['base_n']:>7} {r['base_pct']:>10} {r['mimic_n']:>8} {r['mimic_pct']:>10} {r['carried_hits']:>13}")


if __name__ == '__main__':
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
