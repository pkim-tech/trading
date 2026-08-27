"""Option 2 -- "kernel slot-consumption rule enforced literally in live", built 2026-08-21
for the confirmed SOXL live-vs-backtest divergence (see docs/session_cache.md's 2026-08-20
(late)/2026-08-21 entries and scripts/sim_live_mimic_baseline.py's module docstring for the
full root-cause writeup).

Premise: backtester._simulate_trail_both's per-bar loop is if-in_trade/elif-waiting/else --
exactly ONE branch runs per bar, so a bar whose iteration is consumed resolving a
carried-over WAIT can never ALSO produce that same bar's own independent fresh signal.
Live has no such exclusivity (`buy_alerted` clears on `closed_today`, and the close-check
window re-derives the SAME anchor bar ~1h after the open-check window), which is exactly how
the real 2026-08-20 SOXL second trade happened.

Option 2 = suppress that re-derivation in live: once an anchor bar's slot has been consumed
by a carried-over WAIT resolution, no fresh signal may be taken against that same anchor bar,
even though live legitimately detects one. Cost: forgo a real trade. Benefit: live's trade
sequence stays reproducible by the kernel.

This script is the OFFLINE evaluation of that rule. It re-implements
sim_live_mimic_baseline.simulate_live_mimic verbatim with one change, gated by
`suppress_carried_reentry`: the post-carried-SL `check_signal(...)` call is skipped (and the
event recorded instead). The inline same-bar SL check on the carried resolution itself is
UNCHANGED -- it models live's real continuous intrabar monitoring and is independent of the
suppression question.

Usage:
    .venv/bin/python scripts/sim_option2_slot_suppression.py [--tickers T ...] [--dump TICKER]
                                                             [--dump-dates YYYY-MM-DD ...]
                                                             [--diff-trades]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategies
from backtester import prep_inputs, WIN, LOSS, TWIN, TLOSS, OPEN, _RESULT_NAMES
from scripts.export_trades import load_hourly, simulate_trail_both_annotated
from scripts.sim_live_mimic_baseline import simulate_live_mimic, TB_NODES


def simulate_option2(p, take_profit, stop_loss, trail_buy_pct, trail_pct,
                     max_hours_to_hold, target_h0, target_h1, z_thresh,
                     suppress_carried_reentry=True, inline_carried_sl=True):
    """Verbatim copy of sim_live_mimic_baseline.simulate_live_mimic with the single
    Option-2 change at the marked line. With suppress_carried_reentry=False this MUST
    reproduce the live-mimic exactly (asserted in main())."""
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
    suppressed = []   # bars where Option 2 actually vetoed a real fresh signal

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

    def would_signal(i, op, cp, allow_open=True):
        """Read-only twin of check_signal -- reports whether a signal WOULD have fired,
        without mutating state. Used only for recording what Option 2 vetoed."""
        di = daily_idx[i]
        if di < 0:
            return None
        sma, std = sma_arr[di], std_arr[di]
        if std == 0.0:
            return None
        lower_band = sma - std * z_thresh
        if allow_open:
            sig = (op <= lower_band) and (op > trend_arr[di]) if has_trend else op <= lower_band
            if sig:
                return ('open', op, lower_band, (op - sma) / std)
        sig = (cp <= lower_band) and (cp > trend_arr[di]) if has_trend else cp <= lower_band
        if sig:
            return ('close', cp, lower_band, (cp - sma) / std)
        return None

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
                                   entry_p=entry_price, exit_p=op, held=held, result=LOSS, ret=pc,
                                   exit_reason='SL'))
                in_trade = False
                continue
            if low <= stop_price:
                pc = (stop_price - entry_price) / entry_price
                trades.append(dict(signal_i=signal_bar, entry_i=entry_bar, arm_i=arm_bar, exit_i=i,
                                   entry_p=entry_price, exit_p=stop_price, held=held, result=LOSS,
                                   ret=pc, exit_reason='SL'))
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
                if is_carried and inline_carried_sl:
                    if low <= stop_price:
                        pc = (stop_price - entry_price) / entry_price
                        trades.append(dict(signal_i=signal_bar, entry_i=entry_bar, arm_i=None, exit_i=i,
                                           entry_p=entry_price, exit_p=stop_price, held=0, result=LOSS,
                                           ret=pc, exit_reason='SL_SAME_BAR_CARRIED'))
                        in_trade = False
                        carried_reentry_count += 1
                        # ---- OPTION 2 CHANGE ----
                        if suppress_carried_reentry:
                            w = would_signal(i, op, cp, allow_open=False)
                            if w is not None:
                                suppressed.append(dict(bar=i, date=dates[i], via=w[0], px=w[1],
                                                       lower_band=w[2], z=w[3]))
                        else:
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
        trades.append(dict(signal_i=signal_bar, entry_i=entry_bar, arm_i=arm_bar, exit_i=n - 1,
                           entry_p=entry_price, exit_p=cp, held=held, result=OPEN, ret=pc,
                           exit_reason='OPEN'))

    return trades, carried_reentry_count, suppressed


def summarize(trades):
    compounded = 1.0
    for t in trades:
        compounded *= (1.0 + t['ret'])
    return dict(n=len(trades), compounded_pct=round((compounded - 1.0) * 100, 2))


def build_inputs(node):
    df_h = load_hourly(node['ticker'])
    df_daily = df_h.resample("D").last().dropna(subset=["Close"])
    strat = strategies.TrailingBothZScoreBreakout(window=node['window'], z_score_threshold=node['z'])
    ind = strat.generate_daily_indicators(df_daily)
    return prep_inputs(df_h, ind)


def _key(t):
    return (t['entry_i'], t['exit_i'], round(t['entry_p'], 6), round(t['exit_p'], 6))


def fmt_trades(trades, timestamps, dates_filter=None):
    out = []
    for t in trades:
        et, xt = timestamps[t['entry_i']], timestamps[t['exit_i']]
        if dates_filter and et.date().isoformat() not in dates_filter \
                and xt.date().isoformat() not in dates_filter:
            continue
        out.append(f"    sig={timestamps[t['signal_i']]} entry={et} exit={xt} "
                   f"entry_p={t['entry_p']:.4f} exit_p={t['exit_p']:.4f} "
                   f"ret={t['ret']*100:+.2f}% {t.get('exit_reason', _RESULT_NAMES.get(t['result'], '?'))}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tickers', nargs='*', default=None)
    ap.add_argument('--dump', default=None, help='ticker to dump trade lists for')
    ap.add_argument('--dump-dates', nargs='*', default=None)
    ap.add_argument('--diff-trades', action='store_true',
                    help='print every trade where option2 differs from live-mimic')
    args = ap.parse_args()

    nodes = TB_NODES if not args.tickers else [n for n in TB_NODES if n['ticker'] in args.tickers]
    rows = []
    for node in nodes:
        ticker = node['ticker']
        take_profit = node['arm_pct'] / 100.0
        fixed_sl = node['fixed_sl'] / 100.0
        tb = node['trail_buy_pct'] / 100.0
        ts = node['trail_sell_pct'] / 100.0
        mh = node['max_hold_hours']
        p = build_inputs(node)
        timestamps = p['timestamps']

        base = simulate_trail_both_annotated(p, take_profit, fixed_sl, mh, tb, ts, 9, 14,
                                             node['z'], open_check=True)
        mimic, mimic_carried = simulate_live_mimic(p, take_profit, fixed_sl, tb, ts, mh, 9, 14, node['z'])
        opt2, carried, suppressed = simulate_option2(p, take_profit, fixed_sl, tb, ts, mh, 9, 14, node['z'])

        # equivalence guard: flag off => must be byte-identical to the shared live-mimic
        ctl, _, ctl_sup = simulate_option2(p, take_profit, fixed_sl, tb, ts, mh, 9, 14, node['z'],
                                           suppress_carried_reentry=False)
        assert [_key(t) for t in ctl] == [_key(t) for t in mimic], f"{ticker}: control copy diverges from live-mimic"
        assert ctl_sup == [], f"{ticker}: control run recorded suppressions"

        # FALSIFICATION TEST: suppression ON + the shared live-mimic's inline same-bar SL
        # deviation OFF must collapse exactly onto the strict baseline. If it doesn't, this
        # copy is not a faithful mirror and every number below is suspect.
        iso, _, _ = simulate_option2(p, take_profit, fixed_sl, tb, ts, mh, 9, 14, node['z'],
                                     inline_carried_sl=False)
        iso_ok = [_key(t) for t in iso] == [_key(t) for t in base]
        print(f"[falsification] {ticker}: suppress+no-inline-SL == strict baseline? "
              f"{'YES' if iso_ok else 'NO'}  ({len(iso)} vs {len(base)} trades)")

        b, m, o = summarize(base), summarize(mimic), summarize(opt2)
        rows.append(dict(ticker=ticker, base_n=b['n'], base_pct=b['compounded_pct'],
                         mimic_n=m['n'], mimic_pct=m['compounded_pct'],
                         opt2_n=o['n'], opt2_pct=o['compounded_pct'],
                         carried=carried, suppressed=len(suppressed),
                         bars=len(p['prices']),
                         first=str(timestamps[0].date()), last=str(timestamps[-1].date())))

        if suppressed:
            print(f"\n--- {ticker}: {len(suppressed)} Option-2 suppression(s) ---")
            for s in suppressed:
                print(f"    bar={s['bar']} {timestamps[s['bar']]} via={s['via']} "
                      f"px={s['px']:.4f} lower_band={s['lower_band']:.4f} z={s['z']:.3f}")

        if args.diff_trades:
            mk, ok = {_key(t) for t in mimic}, {_key(t) for t in opt2}
            only_m = [t for t in mimic if _key(t) not in ok]
            only_o = [t for t in opt2 if _key(t) not in mk]
            if only_m or only_o:
                print(f"\n--- {ticker}: trades in live-mimic but NOT option2 ({len(only_m)}) ---")
                for line in fmt_trades(only_m, timestamps):
                    print(line)
                print(f"--- {ticker}: trades in option2 but NOT live-mimic ({len(only_o)}) ---")
                for line in fmt_trades(only_o, timestamps):
                    print(line)

        if args.dump == ticker:
            df = set(args.dump_dates) if args.dump_dates else None
            for label, tl in (('STRICT BASELINE', base), ('LIVE-MIMIC', mimic), ('OPTION 2', opt2)):
                print(f"\n=== {ticker} {label} ===")
                for line in fmt_trades(tl, timestamps, df):
                    print(line)

    print(f"\n{'ticker':6} {'bars':>6} {'base_n':>7} {'base_%':>11} {'mimic_n':>8} {'mimic_%':>11} "
          f"{'opt2_n':>7} {'opt2_%':>11} {'carried':>8} {'suppr':>6}  history")
    for r in rows:
        print(f"{r['ticker']:6} {r['bars']:>6} {r['base_n']:>7} {r['base_pct']:>11} {r['mimic_n']:>8} "
              f"{r['mimic_pct']:>11} {r['opt2_n']:>7} {r['opt2_pct']:>11} {r['carried']:>8} "
              f"{r['suppressed']:>6}  {r['first']}..{r['last']}")


if __name__ == '__main__':
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
