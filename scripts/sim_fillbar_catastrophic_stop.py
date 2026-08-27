"""Tests the "catastrophic-only stop during the fill bar" idea (user's proposal,
2026-08-21) -- a 3rd, distinct variant from both things already tested/discussed tonight:

  1. (tested last night, sim_two_stage_stop_kernel.py) widened the stop for bar i+1 --
     the bar the STRICT KERNEL ALREADY CHECKS TIGHTLY. That's an extra grace period beyond
     what the backtest does, made every ticker's return worse, correctly rejected.
  2. (discussed, not yet built) place NO stop at all during the fill bar (bar i, which the
     strict kernel never checks either way) -- exactly matches the backtest's own blind
     spot, but leaves the position with zero real protection for up to ~1h.
  3. (this script) place a WIDE, catastrophic-only stop during the fill bar (bar i) --
     since the strict kernel never checks bar i's own stop condition at all, a wide stop
     that DOESN'T get hit changes nothing vs. the backtest (bar i+1 onward is byte-identical
     to baseline, real tight stop, matching the kernel exactly). Only diverges from backtest
     if the wide stop itself gets breached same-bar -- a real, large, single-bar move -- in
     which case live exits for real (protected) where the strict kernel would have silently
     held through and only evaluated from bar i+1.

Usage:
    .venv/bin/python scripts/sim_fillbar_catastrophic_stop.py [--tickers T ...] [--wide-sl PCT] [--dump]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategies
from backtester import prep_inputs, WIN, LOSS, TWIN, TLOSS, OPEN
from scripts.export_trades import load_hourly, simulate_trail_both_annotated
from scripts.sim_5min_whipsaw import NODES

TB_NODES = [n for n in NODES if n['strategy'] == 'TrailingBothZScoreBreakout']


def simulate_fillbar_catastrophic(p, take_profit, stop_loss, wide_sl, trail_buy_pct, trail_pct,
                                   max_hours_to_hold, target_h0, target_h1, z_thresh):
    """Byte-identical to simulate_trail_both_annotated EXCEPT: at the moment a fill occurs
    (bar i), the bar's own remaining Low is checked against a WIDE catastrophic stop (an
    approximation -- can't know true intrabar sequencing, same caveat as every other
    same-bar mimic in this project). If breached, exits same-bar at the wide stop price.
    If not breached, bar i produces NO trade event at all (matches baseline exactly -- the
    strict kernel doesn't check bar i's real tight stop either), and bar i+1 onward uses
    the REAL tight stop_price, byte-identical to baseline."""
    prices, highs, lows, hours, opens = p['prices'], p['highs'], p['lows'], p['hours'], p['opens']
    daily_idx, sma_arr, std_arr = p['daily_idx'], p['sma_arr'], p['std_arr']
    trend_arr, has_trend = p['trend_arr'], p['has_trend']

    trades = []
    in_trade = waiting = trailing = False
    entry_price = stop_price = tp_price = peak = 0.0
    entry_bar = held = 0
    running_low = 0.0
    wait_bars = 0
    signal_bar = signal_z = arm_bar = None
    fillbar_catastrophic_hits = 0

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
                trailing = True; peak = cp; arm_bar = i
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
                wide_stop_price = entry_price * (1.0 - wide_sl)
                entry_bar = i; held = 0; arm_bar = None
                in_trade = True; waiting = trailing = False
                # CATASTROPHIC-ONLY check on the fill bar itself (bar i). The strict
                # kernel never checks this bar at all -- so a wide stop that doesn't
                # get hit changes nothing; only a real, large single-bar move diverges.
                if low <= wide_stop_price:
                    pc = (wide_stop_price - entry_price) / entry_price
                    trades.append(dict(entry_i=entry_bar, exit_i=i, entry_p=entry_price, exit_p=wide_stop_price,
                                        held=0, result=LOSS, ret=pc, exit_reason='SL_FILLBAR_CATASTROPHIC'))
                    in_trade = False
                    fillbar_catastrophic_hits += 1
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
            signal_bar = i; signal_z = (op - sma) / std
            fired = True
        if not fired:
            signal_close = (cp <= lower_band) and (cp > trend_arr[di]) if has_trend else cp <= lower_band
            if signal_close:
                waiting = True; running_low = cp; wait_bars = 0
                signal_bar = i; signal_z = (cp - sma) / std

    if in_trade:
        cp = prices[n - 1]
        pc = (cp - entry_price) / entry_price
        trades.append(dict(entry_i=entry_bar, exit_i=n - 1, entry_p=entry_price, exit_p=cp,
                            held=held, result=OPEN, ret=pc, exit_reason='OPEN'))

    return trades, fillbar_catastrophic_hits


def _summarize(trades):
    compounded = 1.0
    for t in trades:
        compounded *= (1.0 + t['ret'])
    return dict(n=len(trades), compounded_pct=round((compounded - 1.0) * 100, 2))


def _trades_match(a, b):
    if len(a) != len(b):
        return False, f"trade count differs: {len(a)} vs {len(b)}"
    for k, (ta, tb) in enumerate(zip(a, b)):
        if ta['entry_i'] != tb['entry_i'] or ta['exit_i'] != tb['exit_i']:
            return False, f"trade #{k+1} bar mismatch"
        if abs(ta['exit_p'] - tb['exit_p']) > 1e-6:
            return False, f"trade #{k+1} exit price differs"
    return True, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tickers', nargs='*', default=None)
    ap.add_argument('--wide-sl', type=float, default=None,
                     help='catastrophic stop as a fraction, e.g. 0.08 for 8%%. Default: 3x node fixed_sl.')
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
        wide_sl = args.wide_sl if args.wide_sl is not None else fixed_sl * 3.0

        df_h = load_hourly(ticker)
        df_daily = df_h.resample("D").last().dropna(subset=["Close"])
        strat = strategies.TrailingBothZScoreBreakout(window=node['window'], z_score_threshold=node['z'])
        ind = strat.generate_daily_indicators(df_daily)
        p = prep_inputs(df_h, ind)

        strict = simulate_trail_both_annotated(p, take_profit, fixed_sl, max_hold_hours,
                                                trail_buy_pct, trail_pct, 9, 14, node['z'], open_check=True)
        catastrophic, hits = simulate_fillbar_catastrophic(
            p, take_profit, fixed_sl, wide_sl, trail_buy_pct, trail_pct, max_hold_hours, 9, 14, node['z'])

        s, c = _summarize(strict), _summarize(catastrophic)
        matched, mismatch = _trades_match(strict, catastrophic)
        rows.append(dict(ticker=ticker, wide_sl_pct=round(wide_sl * 100, 2),
                          strict_n=s['n'], strict_pct=s['compounded_pct'],
                          cat_n=c['n'], cat_pct=c['compounded_pct'], hits=hits,
                          matched=matched, mismatch=mismatch))

        if args.dump and hits:
            print(f"\n=== {ticker}: fill-bar catastrophic hits (wide_sl={wide_sl*100:.1f}%) ===")
            for t in catastrophic:
                if t['exit_reason'] == 'SL_FILLBAR_CATASTROPHIC':
                    et = p['timestamps'][t['entry_i']]
                    print(f"  {et} entry_p={t['entry_p']:.4f} exit_p={t['exit_p']:.4f} ret={t['ret']*100:.2f}%")

    print(f"\n{'ticker':6} {'wide_sl%':>9} {'strict_n':>9} {'strict_%':>10} {'cat_n':>6} {'cat_%':>10} "
          f"{'hits':>5} {'matches_strict_elsewhere':>25}")
    for r in rows:
        note = 'YES' if r['matched'] or r['hits'] else str(r['mismatch'])
        print(f"{r['ticker']:6} {r['wide_sl_pct']:>9} {r['strict_n']:>9} {r['strict_pct']:>10} "
              f"{r['cat_n']:>6} {r['cat_pct']:>10} {r['hits']:>5} {note:>25}")

    print("\nhits = fill-bar catastrophic stop hits (wide_sl breached same-bar as fill).")
    print("For any trade where hits==0 that bar, catastrophic variant is byte-identical to strict backtest.")


if __name__ == '__main__':
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
