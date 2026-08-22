"""Option 3 for the 2026-08-20 SOXL live-vs-backtest divergence: cap the trailing-buy
WAIT state's max carry to the calendar day it started on, IN THE KERNEL.

Root cause being addressed (confirmed 2026-08-20): backtester._simulate_trail_both's
per-bar loop is mutually exclusive (`if in_trade / elif waiting / else signal-check`) --
exactly one branch runs per bar. When a WAIT carried over from a PRIOR trading day
resolves into a position on the next day's 9:30 bar, that bar's iteration is consumed and
the kernel never checks whether that same bar independently fired a fresh entry signal.
Live has no such coupling, so live took a 2nd real trade the kernel can't represent.

Option 3 removes the possibility of an overnight WAIT-carry entirely: on the first bar
whose calendar date is later than the date of the bar that started the WAIT, the WAIT is
cancelled (running_low / wait_bars reset) BEFORE the bounce-fill trigger is evaluated, and
that bar then falls through to the normal fresh-signal check (so the bar is not consumed
-- consuming it would recreate the very ambiguity this is meant to remove). A `--strict`
mode instead consumes the bar (cancel + continue) for comparison.

This is a REAL backtest behavior change, unlike the live-side Options 2/4: trades that
only exist because a WAIT survived overnight simply do not happen. Quantifying that cost
honestly is the point of this script.

Usage:
    .venv/bin/python scripts/sim_option3_same_day_wait_cap.py [--tickers T ...] [--strict]
                                                              [--detail TICKER]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import strategies
from backtester import prep_inputs, WIN, LOSS, TWIN, TLOSS, OPEN, _RESULT_NAMES
from export_trades import (load_hourly, simulate_trail_both_annotated,
                           _simulate_exit_from_entry)

# Real live TrailingBothZScoreBreakout nodes (subset of scripts/sim_5min_whipsaw.py NODES).
NODES = [
    dict(id=92,  ticker='SOXL', window=10, z=1.0, trail_buy_pct=3.0, fixed_sl=2.0,
         arm_pct=30.0, trail_sell_pct=1.0, max_hold_hours=70),
    dict(id=197, ticker='DPST', window=20, z=1.0, trail_buy_pct=2.0, fixed_sl=3.0,
         arm_pct=30.0, trail_sell_pct=2.0, max_hold_hours=112),
    dict(id=202, ticker='KORU', window=20, z=1.5, trail_buy_pct=1.0, fixed_sl=3.0,
         arm_pct=14.0, trail_sell_pct=6.0, max_hold_hours=126),
    dict(id=231, ticker='JNUG', window=10, z=1.0, trail_buy_pct=1.0, fixed_sl=2.0,
         arm_pct=29.0, trail_sell_pct=1.0, max_hold_hours=112),
    dict(id=235, ticker='HIBL', window=20, z=1.0, trail_buy_pct=2.0, fixed_sl=3.0,
         arm_pct=12.0, trail_sell_pct=1.0, max_hold_hours=77),
    dict(id=236, ticker='LABU', window=20, z=1.0, trail_buy_pct=9.0, fixed_sl=2.0,
         arm_pct=30.0, trail_sell_pct=1.0, max_hold_hours=98),
]


def simulate_option3(p, take_profit, stop_loss, max_hours_to_hold,
                     trail_buy_pct, trail_pct, target_h0, target_h1, z_thresh,
                     open_check=False, strict=False, cap=True):
    """Byte-for-byte copy of export_trades.simulate_trail_both_annotated except for the
    same-day WAIT cap in the `waiting` branch. Returns (trades, cancels)."""
    prices, highs, lows, hours, opens = p['prices'], p['highs'], p['lows'], p['hours'], p['opens']
    daily_idx, sma_arr, std_arr = p['daily_idx'], p['sma_arr'], p['std_arr']
    trend_arr, has_trend = p['trend_arr'], p['has_trend']
    dates = p['timestamps'].strftime('%Y-%m-%d').to_numpy()

    trades = []
    cancels = []
    in_trade = waiting = trailing = False
    entry_price = stop_price = tp_price = peak = 0.0
    entry_bar = held = 0
    running_low = 0.0
    wait_bars = 0
    signal_bar = None
    signal_z = None
    arm_bar = None

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
                    trades.append(dict(signal_i=signal_bar, signal_z=signal_z, entry_i=entry_bar,
                                       arm_i=arm_bar, exit_i=i, entry_p=entry_price, exit_p=exit_px,
                                       held=held, result=WIN if pc > 0 else LOSS, ret=pc))
                    in_trade = trailing = False
                    continue
                if high > peak:
                    peak = high
                trail_stop = peak * (1.0 - trail_pct)
                if low <= trail_stop or held >= max_hours_to_hold:
                    exit_px = trail_stop if low <= trail_stop else cp
                    pc = (exit_px - entry_price) / entry_price
                    trades.append(dict(signal_i=signal_bar, signal_z=signal_z, entry_i=entry_bar,
                                       arm_i=arm_bar, exit_i=i, entry_p=entry_price, exit_p=exit_px,
                                       held=held, result=WIN if pc > 0 else LOSS, ret=pc))
                    in_trade = trailing = False
                continue
            if op <= stop_price:
                pc = (op - entry_price) / entry_price
                trades.append(dict(signal_i=signal_bar, signal_z=signal_z, entry_i=entry_bar,
                                   arm_i=arm_bar, exit_i=i, entry_p=entry_price, exit_p=op,
                                   held=held, result=LOSS, ret=pc))
                in_trade = False
                continue
            if low <= stop_price:
                pc = (stop_price - entry_price) / entry_price
                trades.append(dict(signal_i=signal_bar, signal_z=signal_z, entry_i=entry_bar,
                                   arm_i=arm_bar, exit_i=i, entry_p=entry_price, exit_p=stop_price,
                                   held=held, result=LOSS, ret=pc))
                in_trade = False
                continue
            if cp >= tp_price:
                trailing = True; peak = cp; arm_bar = i
                continue
            if held >= max_hours_to_hold:
                pc = (cp - entry_price) / entry_price
                trades.append(dict(signal_i=signal_bar, signal_z=signal_z, entry_i=entry_bar,
                                   arm_i=arm_bar, exit_i=i, entry_p=entry_price, exit_p=cp,
                                   held=held, result=TWIN if pc > 0 else TLOSS, ret=pc))
                in_trade = False
                continue
            continue

        if waiting:
            # ---- OPTION 3: same-day WAIT cap -------------------------------------
            # `dates` comes from the real bar timestamps, so "a later date" is
            # naturally "the next TRADING day" -- weekends/holidays have no bars.
            if cap and dates[i] != dates[signal_bar]:
                cancels.append(dict(signal_i=signal_bar, signal_date=dates[signal_bar],
                                    cancel_i=i, cancel_date=dates[i],
                                    running_low=running_low, wait_bars=wait_bars))
                waiting = False
                running_low = 0.0
                wait_bars = 0
                if strict:
                    continue
                # fall through to the fresh-signal check for this same bar
            else:
                wait_bars += 1
                buy_trigger_gap = running_low * (1.0 + trail_buy_pct)
                if op >= buy_trigger_gap:
                    entry_price = op
                    tp_price = entry_price * (1.0 + take_profit)
                    stop_price = entry_price * (1.0 - stop_loss)
                    entry_bar = i; held = 0; arm_bar = None
                    in_trade = True; waiting = trailing = False
                    continue
                if low < running_low:
                    running_low = low
                buy_trigger = running_low * (1.0 + trail_buy_pct)
                if high >= buy_trigger:
                    entry_price = buy_trigger
                    tp_price = entry_price * (1.0 + take_profit)
                    stop_price = entry_price * (1.0 - stop_loss)
                    entry_bar = i; held = 0; arm_bar = None
                    in_trade = True; waiting = trailing = False
                    continue
                if wait_bars >= max_hours_to_hold:
                    waiting = False
                    running_low = 0.0
                    wait_bars = 0
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
        if open_check:
            op = opens[i]
            signal_open = (op <= lower_band) and (op > trend_arr[di]) if has_trend else op <= lower_band
            if signal_open:
                waiting = True; running_low = op; wait_bars = 0
                signal_bar = i; signal_z = (op - sma) / std
                fired = True
        if not fired:
            signal = (cp <= lower_band) and (cp > trend_arr[di]) if has_trend else cp <= lower_band
            if signal:
                waiting = True; running_low = cp; wait_bars = 0
                signal_bar = i; signal_z = (cp - sma) / std

    if in_trade:
        cp = prices[n - 1]
        pc = (cp - entry_price) / entry_price
        trades.append(dict(signal_i=signal_bar, signal_z=signal_z, entry_i=entry_bar,
                           arm_i=arm_bar, exit_i=n - 1, entry_p=entry_price, exit_p=cp,
                           held=held, result=OPEN, ret=pc))

    return trades, cancels


def counterfactual_wait(p, cancel, take_profit, stop_loss, max_hours_to_hold,
                        trail_buy_pct, trail_pct):
    """What the UNMODIFIED kernel's WAIT would have done from the cancellation bar on
    (fill or wait_bars timeout), and the full trade it would have produced."""
    prices, highs, lows, opens = p['prices'], p['highs'], p['lows'], p['opens']
    running_low = cancel['running_low']
    wait_bars = cancel['wait_bars']
    n = len(prices)
    for i in range(cancel['cancel_i'], n):
        cp, high, low, op = prices[i], highs[i], lows[i], opens[i]
        wait_bars += 1
        entry_price = None
        if op >= running_low * (1.0 + trail_buy_pct):
            entry_price = op
        else:
            if low < running_low:
                running_low = low
            trig = running_low * (1.0 + trail_buy_pct)
            if high >= trig:
                entry_price = trig
        if entry_price is not None:
            ex = _simulate_exit_from_entry(prices, highs, lows, opens, i, entry_price,
                                           take_profit, stop_loss, trail_pct, max_hours_to_hold)
            return dict(filled=True, entry_i=i, entry_p=entry_price, **ex)
        if wait_bars >= max_hours_to_hold:
            return dict(filled=False)
    return dict(filled=False)


def compounded(trades):
    r = 1.0
    for t in trades:
        r *= (1.0 + t['ret'])
    return (r - 1.0) * 100.0


def build(node):
    df_h = load_hourly(node['ticker'])
    df_daily = df_h.resample("D").last().dropna(subset=["Close"])
    strat = strategies.TrailingBothZScoreBreakout(window=node['window'],
                                                  z_score_threshold=node['z'])
    ind = strat.generate_daily_indicators(df_daily)
    return prep_inputs(df_h, ind)


def run(node, strict=False, detail=False):
    p = build(node)
    ts = p['timestamps']
    args = dict(take_profit=node['arm_pct'] / 100.0, stop_loss=node['fixed_sl'] / 100.0,
                max_hours_to_hold=node['max_hold_hours'],
                trail_buy_pct=node['trail_buy_pct'] / 100.0,
                trail_pct=node['trail_sell_pct'] / 100.0)

    base = simulate_trail_both_annotated(p, args['take_profit'], args['stop_loss'],
                                         args['max_hours_to_hold'], args['trail_buy_pct'],
                                         args['trail_pct'], 9, 14, node['z'], open_check=True)
    o3, cancels = simulate_option3(p, args['take_profit'], args['stop_loss'],
                                   args['max_hours_to_hold'], args['trail_buy_pct'],
                                   args['trail_pct'], 9, 14, node['z'],
                                   open_check=True, strict=strict)

    # Baseline trades whose entry landed on a LATER calendar day than their signal --
    # exactly the population Option 3 makes impossible.
    dates = ts.strftime('%Y-%m-%d')
    carried = [t for t in base
               if t['signal_i'] is not None and dates[t['entry_i']] != dates[t['signal_i']]]

    cf = []
    for c in cancels:
        r = counterfactual_wait(p, c, args['take_profit'], args['stop_loss'],
                                args['max_hours_to_hold'], args['trail_buy_pct'],
                                args['trail_pct'])
        cf.append((c, r))
    cf_filled = [(c, r) for c, r in cf if r['filled']]

    res = dict(
        ticker=node['ticker'],
        base_n=len(base), base_pct=compounded(base),
        o3_n=len(o3), o3_pct=compounded(o3),
        cancels=len(cancels),
        cf_filled=len(cf_filled),
        cf_mean=(sum(r['ret'] for _, r in cf_filled) / len(cf_filled) * 100.0) if cf_filled else 0.0,
        cf_wins=sum(1 for _, r in cf_filled if r['ret'] > 0),
        carried_n=len(carried),
        carried_mean=(sum(t['ret'] for t in carried) / len(carried) * 100.0) if carried else 0.0,
        carried_wins=sum(1 for t in carried if t['ret'] > 0),
    )

    if detail:
        print(f"\n=== {node['ticker']} detail ===")
        print(f"bars {len(ts)}  {ts[0]} .. {ts[-1]}")
        print(f"\n-- baseline overnight-carried entries ({len(carried)}) --")
        for t in carried[-15:]:
            print(f"  signal {ts[t['signal_i']]}  entry {ts[t['entry_i']]} @ {t['entry_p']:.4f}"
                  f"  exit {ts[t['exit_i']]} @ {t['exit_p']:.4f}  {_RESULT_NAMES[t['result']]}"
                  f" {t['ret']*100:+.2f}%")
        print(f"\n-- option3 cancellations ({len(cancels)}) --")
        for c, r in cf[-15:]:
            note = (f"would fill {ts[r['entry_i']]} @ {r['entry_p']:.4f} -> {r['ret']*100:+.2f}%"
                    if r['filled'] else "would never fill (wait_bars timeout)")
            print(f"  wait started {ts[c['signal_i']]} ({c['signal_date']}), cancelled at "
                  f"{ts[c['cancel_i']]} ({c['cancel_date']}, wait_bars={c['wait_bars']}, "
                  f"running_low={c['running_low']:.4f}) | {note}")
    return res


def parity(node):
    """Regression check on this file's copy of the kernel: with the day-cap disabled it
    must produce a byte-identical trade list to export_trades.simulate_trail_both_annotated,
    so any measured delta is attributable to the cap alone and nothing else."""
    p = build(node)
    tp, sl = node['arm_pct'] / 100.0, node['fixed_sl'] / 100.0
    tb, tr = node['trail_buy_pct'] / 100.0, node['trail_sell_pct'] / 100.0
    mh = node['max_hold_hours']
    base = simulate_trail_both_annotated(p, tp, sl, mh, tb, tr, 9, 14, node['z'], open_check=True)
    mine, c = simulate_option3(p, tp, sl, mh, tb, tr, 9, 14, node['z'], open_check=True, cap=False)
    ok = base == mine
    print(f"  {node['ticker']:<6} parity={'OK' if ok else 'MISMATCH'}  trades base={len(base)} "
          f"copy={len(mine)}  cancels_when_uncapped={len(c)} (must be 0)")
    return ok


def verify(node, trades_after=None):
    """Adversarial self-checks: is the cancel firing exactly on genuine day-boundary
    crossings, at the FIRST bar of the new day, never on a same-day wait?"""
    p = build(node)
    ts = p['timestamps']
    dates = ts.strftime('%Y-%m-%d')
    tp, sl = node['arm_pct'] / 100.0, node['fixed_sl'] / 100.0
    tb, tr = node['trail_buy_pct'] / 100.0, node['trail_sell_pct'] / 100.0
    mh = node['max_hold_hours']
    o3, cancels = simulate_option3(p, tp, sl, mh, tb, tr, 9, 14, node['z'], open_check=True)

    bad = []
    for c in cancels:
        si, ci = c['signal_i'], c['cancel_i']
        if not (dates[ci] > dates[si]):
            bad.append(('not-a-later-date', c))
        if ci <= si:
            bad.append(('cancel-before-signal', c))
        # must be the FIRST bar of the new day: every bar strictly between is same-day
        for j in range(si + 1, ci):
            if dates[j] != dates[si]:
                bad.append(('late-cancel', c)); break
        # and the bar before the cancel must be the signal day (contiguity)
        if dates[ci - 1] != dates[si]:
            bad.append(('discontiguous', c))
        if c['wait_bars'] < 0:
            bad.append(('neg-wait', c))

    same_day_fills = [t for t in o3
                      if t['signal_i'] is not None and dates[t['entry_i']] == dates[t['signal_i']]]
    later_day_fills = [t for t in o3
                       if t['signal_i'] is not None and dates[t['entry_i']] != dates[t['signal_i']]]
    # weekend/holiday-spanning cancels (Fri -> Mon): calendar gap > 1 day
    import datetime as _dt
    span = [c for c in cancels
            if (_dt.date.fromisoformat(c['cancel_date']) - _dt.date.fromisoformat(c['signal_date'])).days > 1]
    late_start = [c for c in cancels if ts[c['signal_i']].hour == 14]
    early_start = [c for c in cancels if ts[c['signal_i']].hour == 9]

    print(f"\n=== {node['ticker']} verify ===")
    print(f"  cancels={len(cancels)}  bad_checks={len(bad)}")
    for tag, c in bad[:10]:
        print(f"    !! {tag} {c}")
    print(f"  option3 trades: same-day entry={len(same_day_fills)}  later-day entry={len(later_day_fills)}"
          f"  (later-day MUST be 0)")
    print(f"  cancels from a 14:30 signal={len(early_start) and len(late_start) or len(late_start)}"
          f"  from a 09:30 signal={len(early_start)}")
    print(f"  cancels spanning >1 calendar day (weekend/holiday)={len(span)}")
    if trades_after:
        print(f"  -- option3 trades on/after {trades_after} --")
        for t in o3:
            if dates[t['entry_i']] >= trades_after:
                print(f"    signal {ts[t['signal_i']]} entry {ts[t['entry_i']]} @ {t['entry_p']:.4f}"
                      f" exit {ts[t['exit_i']]} @ {t['exit_p']:.4f} {_RESULT_NAMES[t['result']]}"
                      f" {t['ret']*100:+.2f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tickers', nargs='*')
    ap.add_argument('--strict', action='store_true',
                    help='cancel consumes the bar (no fresh-signal fallthrough)')
    ap.add_argument('--detail', nargs='*', default=[])
    ap.add_argument('--parity', action='store_true', help='regression: cap off must equal the baseline kernel')
    ap.add_argument('--verify', action='store_true', help='adversarial self-checks on the cancel rule')
    ap.add_argument('--trades-after', default=None, help='print trades on/after YYYY-MM-DD')
    a = ap.parse_args()

    nodes = [n for n in NODES if not a.tickers or n['ticker'] in a.tickers]
    if a.parity:
        print("parity (cap disabled -> must match simulate_trail_both_annotated exactly):")
        print("ALL OK" if all([parity(n) for n in nodes]) else "FAILED")
        return
    if a.verify or a.trades_after:
        for n in nodes:
            verify(n, a.trades_after)
        return
    rows = [run(n, strict=a.strict, detail=n['ticker'] in a.detail) for n in nodes]

    print(f"\nmode: {'STRICT (cancel consumes bar)' if a.strict else 'FALLTHROUGH (cancel, then fresh-signal check same bar)'}")
    hdr = (f"{'ticker':<7}{'base_n':>7}{'base_%':>12}{'o3_n':>6}{'o3_%':>12}"
           f"{'cancels':>9}{'cf_fill':>8}{'cf_mean%':>10}{'cf_w/l':>9}"
           f"{'carried':>8}{'carr_mean%':>11}{'carr_w/l':>10}")
    print(hdr)
    print('-' * len(hdr))
    for r in rows:
        print(f"{r['ticker']:<7}{r['base_n']:>7}{r['base_pct']:>12.1f}{r['o3_n']:>6}"
              f"{r['o3_pct']:>12.1f}{r['cancels']:>9}{r['cf_filled']:>8}{r['cf_mean']:>10.2f}"
              f"{str(r['cf_wins'])+'/'+str(r['cf_filled']-r['cf_wins']):>9}"
              f"{r['carried_n']:>8}{r['carried_mean']:>11.2f}"
              f"{str(r['carried_wins'])+'/'+str(r['carried_n']-r['carried_wins']):>10}")


if __name__ == '__main__':
    main()
