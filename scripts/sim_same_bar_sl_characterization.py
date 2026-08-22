"""Characterizes the same-bar-intrabar-SL divergence found 2026-08-21 (night) while
testing 3 candidate live-vs-backtest fixes (see docs/research_log.md's 2026-08-21 (night)
entry / docs/backlog_cache.md). Next real step from that entry, not started until now.

Finding being characterized: the strict hourly kernel (simulate_trail_both_annotated)
never evaluates a fill bar's OWN exit conditions -- `held` starts at 0 and the fill
branch does `continue` immediately after filling, so SL/TP/TRAIL/TIME are first checked
on the NEXT bar at the earliest. Live monitors continuously and can catch a real SL
breach within the same bar it just filled on. scripts/sim_live_mimic_baseline.py already
models this, but ONLY for carried-over (multi-day) WAIT resolutions (`is_carried`), and
its own falsification test showed that's NOT the dominant remaining SOXL divergence (186
mimic trades vs 160 strict, even with the carried-only fix landed).

Real gating bug found and fixed here, in BOTH the carried and new full variant: after an
inline same-bar SL exit, the original script (sim_live_mimic_baseline.py) re-derives a
fresh signal via check_signal(i, ...) unconditionally at whatever bar `i` currently is --
but live only ever checks BUY signals in the two real daily windows (10:25-10:40 ET /
15:25-15:40 ET, which read the 9:30/14:30 anchor bars specifically, per CLAUDE.md's
"Signal windows" note). A same-bar SL hit landing on a non-anchor bar (11:30, 12:30, etc,
which happens routinely for a WAIT that resolves mid-day) should NOT immediately re-derive
a new signal at that same off-window bar -- live wouldn't check again until the next real
window. An UNGATED first version of this script's "full" (all-fills) variant produced a
runaway same-day re-entry cascade (SOXL: 464 trades, -97.7% compounded, vs strict's 160
trades/+1157.5%) traced directly to this bug: an off-window same-bar SL exit re-derived
a signal, which then filled and SL'd again a bar or two later, repeating several times
within one trending-down day. This script gates re-derivation to real anchor-hour bars
(target_h0/target_h1) only, matching live's actual signal-check cadence, and reports both
the gated (trustworthy) and ungated (for-reference, shows the bug's own size) results.

Four variants compared per ticker, full cached history, 6 real live TrailingBoth tickers
(SOXL/DPST/KORU/JNUG/HIBL/LABU):
  - strict         : simulate_trail_both_annotated, unmodified (the current backtest kernel)
  - carried_gated  : carried-only same-bar SL check, re-derivation gated to real windows
  - full_gated     : all-fills same-bar SL check, re-derivation gated to real windows
  - full_ungated   : all-fills same-bar SL check, NO window gate (shows the bug's size)

Usage:
    .venv/bin/python scripts/sim_same_bar_sl_characterization.py [--tickers T ...] [--dump]
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


def simulate_same_bar_sl(p, take_profit, stop_loss, trail_buy_pct, trail_pct,
                          max_hours_to_hold, target_h0, target_h1, z_thresh,
                          apply_to='carried', gate_rederivation=True):
    """apply_to: 'carried' (only multi-day-WAIT fills get the inline same-bar SL check,
    matching sim_live_mimic_baseline.py's original scope) or 'all' (every fill does).
    gate_rederivation: if True, a post-same-bar-SL-exit signal re-check only fires when
    the current bar's hour is a real anchor hour (target_h0/target_h1) -- matches live's
    actual two-window-per-day signal cadence. If False, reproduces the original (buggy)
    unconditional re-derivation, kept only to show the bug's own size."""
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
    signal_bar = signal_z = signal_date = arm_bar = None
    same_bar_hits_carried = same_bar_hits_fresh = 0

    def check_signal(i, op, cp, allow_open=True):
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
                is_carried = signal_date is not None and signal_date < dates[i]
                tp_price = entry_price * (1.0 + take_profit)
                stop_price = entry_price * (1.0 - stop_loss)
                entry_bar = i; held = 0; arm_bar = None
                in_trade = True; waiting = trailing = False
                do_inline_check = is_carried if apply_to == 'carried' else True
                if do_inline_check and low <= stop_price:
                    pc = (stop_price - entry_price) / entry_price
                    trades.append(dict(entry_i=entry_bar, exit_i=i, entry_p=entry_price, exit_p=stop_price,
                                        held=0, result=LOSS, ret=pc,
                                        exit_reason='SL_SAME_BAR_CARRIED' if is_carried else 'SL_SAME_BAR_FRESH'))
                    in_trade = False
                    if is_carried:
                        same_bar_hits_carried += 1
                    else:
                        same_bar_hits_fresh += 1
                    h_i = hours[i]
                    if not gate_rederivation or h_i == target_h0 or h_i == target_h1:
                        check_signal(i, op, cp, allow_open=False)
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
        trades.append(dict(entry_i=entry_bar, exit_i=n - 1, entry_p=entry_price, exit_p=cp,
                            held=held, result=OPEN, ret=pc, exit_reason='OPEN'))

    return trades, same_bar_hits_carried, same_bar_hits_fresh


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

        args_common = (take_profit, fixed_sl, trail_buy_pct, trail_pct, max_hold_hours, 9, 14, node['z'])

        strict = simulate_trail_both_annotated(p, take_profit, fixed_sl, max_hold_hours,
                                                trail_buy_pct, trail_pct, 9, 14, node['z'], open_check=True)
        carried_gated, cg_c, cg_f = simulate_same_bar_sl(p, *args_common, apply_to='carried', gate_rederivation=True)
        full_gated, fg_c, fg_f = simulate_same_bar_sl(p, *args_common, apply_to='all', gate_rederivation=True)
        full_ungated, fu_c, fu_f = simulate_same_bar_sl(p, *args_common, apply_to='all', gate_rederivation=False)

        s = _summarize(strict)
        cg = _summarize(carried_gated)
        fg = _summarize(full_gated)
        fu = _summarize(full_ungated)
        rows.append(dict(ticker=ticker, strict_n=s['n'], strict_pct=s['compounded_pct'],
                          cg_n=cg['n'], cg_pct=cg['compounded_pct'],
                          fg_n=fg['n'], fg_pct=fg['compounded_pct'], fg_fresh=fg_f,
                          fu_n=fu['n'], fu_pct=fu['compounded_pct'], fu_fresh=fu_f))

        if args.dump:
            print(f"\n=== {ticker}: fresh same-bar SL hits (window-gated) ===")
            for t in full_gated:
                if t['exit_reason'] == 'SL_SAME_BAR_FRESH':
                    et = p['timestamps'][t['entry_i']]
                    print(f"  {et} entry_p={t['entry_p']:.4f} exit_p={t['exit_p']:.4f} ret={t['ret']*100:.2f}%")

    hdr = (f"{'ticker':6} {'strict_n':>9} {'strict_%':>10} {'cg_n':>6} {'cg_%':>9} "
           f"{'fg_n':>6} {'fg_%':>9} {'fg_fresh':>9} {'fu_n':>6} {'fu_%':>10} {'fu_fresh':>9}")
    print(f"\n{hdr}")
    for r in rows:
        print(f"{r['ticker']:6} {r['strict_n']:>9} {r['strict_pct']:>10} {r['cg_n']:>6} {r['cg_pct']:>9} "
              f"{r['fg_n']:>6} {r['fg_pct']:>9} {r['fg_fresh']:>9} {r['fu_n']:>6} {r['fu_pct']:>10} {r['fu_fresh']:>9}")

    print("\nstrict   = current backtest kernel (unmodified)")
    print("cg       = carried-only same-bar SL check, re-derivation GATED to real 9:30/14:30 anchor windows")
    print("fg       = all-fills same-bar SL check (the new channel under test), re-derivation GATED (trustworthy)")
    print("fg_fresh = fg's same-bar SL hits specifically on a FRESH (same-day) fill -- the new channel's own count")
    print("fu/fu_fresh = same as fg but re-derivation UNGATED (the bug) -- shown only to size the bug, not a real finding")


if __name__ == '__main__':
    main()
