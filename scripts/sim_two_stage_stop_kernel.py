"""Two-stage-stop kernel sim, built 2026-08-20 (night) toward Live-State/Principle-1 —
"live execution should match what the backtest validates" (see docs/session_cache.md's
2026-08-20 (late) entry for the SOXL divergence this thread started from).

IMPORTANT — corrected design (earlier draft wrongly replaced the real trailing-buy
bounce-wait entry mechanic with immediate entry; that's NOT what this tests): the real
TrailingBothZScoreBreakout entry logic (wait for price to bounce back up above a running
low before filling — `_simulate_trail_both`'s `waiting` state) stays completely untouched
here, byte-for-byte identical to scripts/export_trades.py::simulate_trail_both_annotated
(the project's verified 'possible'-resolution mirror). This script's ONLY change vs that
baseline: for exactly the first bar evaluated after a fill, the SL check uses a
deliberately WIDE stop instead of the real fixed_sl, then flips to the real (tight)
fixed_sl from the following bar onward. Idea (user's framing): the bar immediately after a
fill is the one window the hourly kernel can't really resolve (no proof of what happened
between the fill and that bar's own Open/Low other than the two endpoints) — smoothing
that one ambiguous bar's stop width is an attempt at a more faithful kernel simulation of
that ambiguity, not a proposal to change the real entry mechanic or drop trailing-buy.
Whether this materially changes results vs baseline (it should be small, since baseline
already applies no SL check at all on the fill bar itself — see below) is exactly the
question this script exists to answer.

Bar-index detail (matches simulate_trail_both_annotated's control flow exactly): a fill
transition (from `waiting` to `in_trade`) happens INSIDE bar i's iteration but consumes
that iteration -- no exit check ever runs against bar i itself, baseline or here. The
first bar an exit check runs against is bar i+1. So "the first bar evaluated after a
fill" = bar i+1, not bar i -- this script sets a `settling` flag at fill time and uses the
wide stop for exactly that one evaluation, then reverts to the real tight fixed_sl
(already the same stop_price the baseline would compute) for every bar after.

Deliberately NOT touching backtester.py/strategies.py — a standalone research script,
same convention as scripts/export_trades.py / scripts/sim_5min_whipsaw.py.

Usage:
    .venv/bin/python scripts/sim_two_stage_stop_kernel.py [--tickers T ...] [--wide-sl PCT] [--dump]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategies
from backtester import prep_inputs, WIN, LOSS, TWIN, TLOSS, OPEN, _RESULT_NAMES
from scripts.export_trades import load_hourly, simulate_trail_both_annotated
from scripts.sim_5min_whipsaw import NODES

# Only the real TrailingBoth (trailing-buy bounce-wait) nodes are in scope -- the
# divergence/ambiguity this targets is specific to that mechanic's WAIT/fill transition.
TB_NODES = [n for n in NODES if n['strategy'] == 'TrailingBothZScoreBreakout']


def simulate_two_stage(p, take_profit, stop_loss, wide_sl, trail_buy_pct, trail_pct,
                        max_hours_to_hold, target_h0, target_h1, z_thresh, open_check=True):
    """Identical to simulate_trail_both_annotated except: at the moment `waiting` resolves
    into `in_trade` (either fill branch — gap-through-trigger at Open, or the normal
    bounce-fill at High), a `settling` flag is set; the very next bar's SL check (both the
    op<=stop gap-check and the low<=stop check) uses `wide_stop_price` instead of the real
    `stop_price`, then `settling` clears so every subsequent bar uses the real tight stop
    exactly as baseline does. Trailing-arm/exit and TIME logic are untouched."""
    prices, highs, lows, hours, opens = p['prices'], p['highs'], p['lows'], p['hours'], p['opens']
    daily_idx, sma_arr, std_arr = p['daily_idx'], p['sma_arr'], p['std_arr']
    trend_arr, has_trend = p['trend_arr'], p['has_trend']

    trades = []
    in_trade = waiting = trailing = settling = False
    entry_price = stop_price = wide_stop_price = tp_price = peak = 0.0
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
            check_stop = wide_stop_price if settling else stop_price
            settling = False
            if trailing:
                trail_stop_gap = peak * (1.0 - trail_pct)
                if op <= trail_stop_gap:
                    exit_px = op
                    pc = (exit_px - entry_price) / entry_price
                    trades.append(dict(signal_i=signal_bar, signal_z=signal_z, entry_i=entry_bar,
                                        arm_i=arm_bar, exit_i=i, entry_p=entry_price, exit_p=exit_px,
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
                    trades.append(dict(signal_i=signal_bar, signal_z=signal_z, entry_i=entry_bar,
                                        arm_i=arm_bar, exit_i=i, entry_p=entry_price, exit_p=exit_px,
                                        held=held, result=WIN if pc > 0 else LOSS, ret=pc, exit_reason=reason))
                    in_trade = trailing = False
                continue
            if op <= check_stop:
                pc = (op - entry_price) / entry_price
                trades.append(dict(signal_i=signal_bar, signal_z=signal_z, entry_i=entry_bar,
                                    arm_i=arm_bar, exit_i=i, entry_p=entry_price, exit_p=op,
                                    held=held, result=LOSS, ret=pc,
                                    exit_reason='SL_WIDE' if check_stop == wide_stop_price else 'SL'))
                in_trade = False
                continue
            if low <= check_stop:
                pc = (check_stop - entry_price) / entry_price
                trades.append(dict(signal_i=signal_bar, signal_z=signal_z, entry_i=entry_bar,
                                    arm_i=arm_bar, exit_i=i, entry_p=entry_price, exit_p=check_stop,
                                    held=held, result=LOSS, ret=pc,
                                    exit_reason='SL_WIDE' if check_stop == wide_stop_price else 'SL'))
                in_trade = False
                continue
            if cp >= tp_price:
                trailing = True; peak = cp; arm_bar = i
                continue
            if held >= max_hours_to_hold:
                pc = (cp - entry_price) / entry_price
                trades.append(dict(signal_i=signal_bar, signal_z=signal_z, entry_i=entry_bar,
                                    arm_i=arm_bar, exit_i=i, entry_p=entry_price, exit_p=cp,
                                    held=held, result=TWIN if pc > 0 else TLOSS, ret=pc, exit_reason='TIME'))
                in_trade = False
            continue

        if waiting:
            wait_bars += 1
            buy_trigger_gap = running_low * (1.0 + trail_buy_pct)
            if op >= buy_trigger_gap:
                entry_price = op
                tp_price = entry_price * (1.0 + take_profit)
                stop_price = entry_price * (1.0 - stop_loss)
                wide_stop_price = entry_price * (1.0 - wide_sl)
                entry_bar = i; held = 0; arm_bar = None
                in_trade = True; waiting = trailing = False; settling = True
                continue
            if low < running_low:
                running_low = low
            buy_trigger = running_low * (1.0 + trail_buy_pct)
            if high >= buy_trigger:
                entry_price = buy_trigger
                tp_price = entry_price * (1.0 + take_profit)
                stop_price = entry_price * (1.0 - stop_loss)
                wide_stop_price = entry_price * (1.0 - wide_sl)
                entry_bar = i; held = 0; arm_bar = None
                in_trade = True; waiting = trailing = False; settling = True
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
        if open_check:
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
        trades.append(dict(signal_i=signal_bar, signal_z=signal_z, entry_i=entry_bar, arm_i=arm_bar,
                            exit_i=n - 1, entry_p=entry_price, exit_p=cp, held=held, result=OPEN, ret=pc,
                            exit_reason='OPEN'))

    return trades


def _summarize(trades):
    compounded = 1.0
    reasons = {}
    for t in trades:
        compounded *= (1.0 + t['ret'])
        reasons[t.get('exit_reason', '?')] = reasons.get(t.get('exit_reason', '?'), 0) + 1
    return dict(n=len(trades), compounded_pct=round((compounded - 1.0) * 100, 2), reasons=reasons)


def _trades_match(a, b):
    """Exact-match check: same entry/exit bar indices and prices (up to fp noise). Used
    to prove the two sims agree everywhere the wide stop never gets exercised."""
    if len(a) != len(b):
        return False, f"trade count differs: {len(a)} vs {len(b)}"
    for k, (ta, tb) in enumerate(zip(a, b)):
        if ta['entry_i'] != tb['entry_i'] or ta['exit_i'] != tb['exit_i']:
            return False, f"trade #{k+1} bar mismatch: entry {ta['entry_i']}/{tb['entry_i']} exit {ta['exit_i']}/{tb['exit_i']}"
        if abs(ta['exit_p'] - tb['exit_p']) > 1e-6:
            return False, f"trade #{k+1} exit price differs: {ta['exit_p']} vs {tb['exit_p']} ({ta.get('exit_reason')})"
    return True, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tickers', nargs='*', default=None)
    ap.add_argument('--wide-sl', type=float, default=None,
                    help='wide stop as a fraction, e.g. 0.10 for 10%%. Default: 3x the node fixed_sl.')
    ap.add_argument('--dump', action='store_true', help='print every trade for manual review')
    args = ap.parse_args()

    nodes = TB_NODES if not args.tickers else [n for n in TB_NODES if n['ticker'] in args.tickers]
    if not nodes:
        print("No matching TrailingBoth nodes.", file=sys.stderr)
        return

    rows = []
    for node in nodes:
        ticker = node['ticker']
        window, z = node['window'], node['z']
        take_profit = node['arm_pct'] / 100.0
        fixed_sl = node['fixed_sl'] / 100.0
        trail_buy_pct = node['trail_buy_pct'] / 100.0
        trail_pct = node['trail_sell_pct'] / 100.0
        max_hold_hours = node['max_hold_hours']
        wide_sl = args.wide_sl if args.wide_sl is not None else fixed_sl * 3.0

        df_h = load_hourly(ticker)
        df_daily = df_h.resample("D").last().dropna(subset=["Close"])
        strat = strategies.TrailingBothZScoreBreakout(window=window, z_score_threshold=z)
        ind = strat.generate_daily_indicators(df_daily)
        p = prep_inputs(df_h, ind)

        baseline_trades = simulate_trail_both_annotated(
            p, take_profit, fixed_sl, max_hold_hours, trail_buy_pct, trail_pct, 9, 14, z,
            open_check=True)
        two_stage_trades = simulate_two_stage(
            p, take_profit, fixed_sl, wide_sl, trail_buy_pct, trail_pct, max_hold_hours, 9, 14, z,
            open_check=True)

        base_sum = _summarize(baseline_trades)
        two_sum = _summarize(two_stage_trades)
        matched, mismatch_reason = _trades_match(baseline_trades, two_stage_trades)

        rows.append(dict(ticker=ticker, wide_sl_pct=round(wide_sl * 100, 2),
                          base_n=base_sum['n'], base_compounded=base_sum['compounded_pct'],
                          two_n=two_sum['n'], two_compounded=two_sum['compounded_pct'],
                          matched=matched, mismatch=mismatch_reason,
                          wide_hits=two_sum['reasons'].get('SL_WIDE', 0)))

        if args.dump:
            print(f"\n=== {ticker} — BASELINE — {base_sum['n']} trades, {base_sum['compounded_pct']}% ===")
            timestamps = p['timestamps']
            for k, t in enumerate(baseline_trades):
                print(f"  [{k+1}] {timestamps[t['entry_i']]} -> {timestamps[t['exit_i']]}  "
                      f"entry={t['entry_p']:.2f} exit={t['exit_p']:.2f} held={t['held']}h "
                      f"ret={t['ret']*100:.2f}%  {_RESULT_NAMES[t['result']]}")
            print(f"\n=== {ticker} — TWO-STAGE-STOP (wide_sl={wide_sl*100:.1f}% for the fill+1 bar only) — "
                  f"{two_sum['n']} trades, {two_sum['compounded_pct']}% ===")
            for k, t in enumerate(two_stage_trades):
                print(f"  [{k+1}] {timestamps[t['entry_i']]} -> {timestamps[t['exit_i']]}  "
                      f"entry={t['entry_p']:.2f} exit={t['exit_p']:.2f} held={t['held']}h "
                      f"ret={t['ret']*100:.2f}%  {t['exit_reason']}")

    print()
    print(f"{'ticker':6} {'wide_sl%':>9} {'base_n':>7} {'base_%':>10} {'two_n':>6} {'two_%':>10} "
          f"{'matched':>8} {'wide_hits':>10}  note")
    for r in rows:
        note = '' if r['matched'] else r['mismatch']
        print(f"{r['ticker']:6} {r['wide_sl_pct']:>9} {r['base_n']:>7} {r['base_compounded']:>10} "
              f"{r['two_n']:>6} {r['two_compounded']:>10} {str(r['matched']):>8} {r['wide_hits']:>10}  {note}")


if __name__ == '__main__':
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
