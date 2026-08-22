"""Characterizes the trailing-stop peak-update-ordering gap found 2026-08-21 (very late)
-- see docs/backlog_cache.md's entry of the same name / docs/research_log.md.

The real kernel's trailing branch (backtester.py, both `possible` and `pessimistic`)
always credits this bar's High (raising `peak`, and therefore the trailing-stop level)
BEFORE checking whether the Low breaches anything -- i.e. it always assumes High-then-Low
ordering within the bar, with NO mirror-image treatment (Low-then-High: check the bar's
Low against the OLD, not-yet-raised stop FIRST) anywhere. Unlike entry (which gets a real
possible/pessimistic bracket), this is a single, always-favorable assumption.

This script builds the missing mirror -- a 'low_first' trailing variant that checks the
bar's Low against the OLD trail_stop BEFORE crediting the High -- and compares it against
the kernel's real ('high_first', reproducing run_backtest_v110's actual behavior) result,
entry-side logic UNCHANGED (this is scoped to the trailing-exit ordering question only).
Also builds a per-bar worst-case walk (pick whichever ordering is worse, mirroring
tonight's other worst-case-walk methodology) for the true floor.

Usage:
    .venv/bin/python scripts/sim_trailing_stop_ordering_walk.py [--tickers T ...] [--dump]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategies
from backtester import prep_inputs, run_backtest_v110, WIN, LOSS, TWIN, TLOSS, OPEN
from scripts.export_trades import load_hourly, simulate_trail_both_annotated
from scripts.sim_5min_whipsaw import NODES

TB_NODES = [n for n in NODES if n['strategy'] == 'TrailingBothZScoreBreakout']


def simulate_trailing_variant(p, take_profit, stop_loss, trail_buy_pct, trail_pct,
                               max_hours_to_hold, target_h0, target_h1, z_thresh, mode):
    """mode: 'high_first' (kernel's real, current behavior -- credit High before
    checking Low) | 'low_first' (the missing mirror -- check Low against the OLD
    stop BEFORE crediting High) | 'worst' (per-bar: pick whichever ordering exits
    worse this bar, mirroring the entry worst-walk methodology).
    Entry-side logic is BYTE-IDENTICAL to simulate_trail_both_annotated ('possible')
    -- only the trailing-exit branch differs, isolating this specific question."""
    prices, highs, lows, hours, opens = p['prices'], p['highs'], p['lows'], p['hours'], p['opens']
    daily_idx, sma_arr, std_arr = p['daily_idx'], p['sma_arr'], p['std_arr']
    trend_arr, has_trend = p['trend_arr'], p['has_trend']

    trades = []
    in_trade = waiting = trailing = False
    entry_price = stop_price = tp_price = peak = 0.0
    entry_bar = held = 0
    running_low = 0.0
    wait_bars = 0

    n = len(prices)
    for i in range(n):
        cp, high, low, op = prices[i], highs[i], lows[i], opens[i]

        if in_trade:
            held += 1
            if trailing:
                old_peak = peak
                old_trail_stop = old_peak * (1.0 - trail_pct)
                # gap-check (Open) is unambiguous regardless of mode -- Open is always first.
                if op <= old_trail_stop:
                    exit_px = op
                    pc = (exit_px - entry_price) / entry_price
                    trades.append(dict(entry_i=entry_bar, exit_i=i, entry_p=entry_price, exit_p=exit_px,
                                        held=held, result=WIN if pc > 0 else LOSS, ret=pc, exit_reason='TRAIL'))
                    in_trade = trailing = False
                    continue

                low_breaches_old = low <= old_trail_stop
                new_peak = high if high > old_peak else old_peak
                new_trail_stop = new_peak * (1.0 - trail_pct)
                low_breaches_new = low <= new_trail_stop

                if mode == 'high_first':
                    peak = new_peak
                    if low_breaches_new or held >= max_hours_to_hold:
                        exit_px = new_trail_stop if low_breaches_new else cp
                        pc = (exit_px - entry_price) / entry_price
                        reason = 'TRAIL' if low_breaches_new else 'TIME'
                        trades.append(dict(entry_i=entry_bar, exit_i=i, entry_p=entry_price, exit_p=exit_px,
                                            held=held, result=WIN if pc > 0 else LOSS, ret=pc, exit_reason=reason))
                        in_trade = trailing = False
                    continue

                if mode == 'low_first':
                    if low_breaches_old:
                        exit_px = old_trail_stop
                        pc = (exit_px - entry_price) / entry_price
                        trades.append(dict(entry_i=entry_bar, exit_i=i, entry_p=entry_price, exit_p=exit_px,
                                            held=held, result=WIN if pc > 0 else LOSS, ret=pc, exit_reason='TRAIL'))
                        in_trade = trailing = False
                        continue
                    peak = new_peak
                    if held >= max_hours_to_hold:
                        pc = (cp - entry_price) / entry_price
                        trades.append(dict(entry_i=entry_bar, exit_i=i, entry_p=entry_price, exit_p=cp,
                                            held=held, result=TWIN if pc > 0 else TLOSS, ret=pc, exit_reason='TIME'))
                        in_trade = False
                    continue

                # mode == 'worst': pick whichever candidate exit is worse (lower exit price)
                candidates = []
                if low_breaches_old:
                    candidates.append(old_trail_stop)
                if low_breaches_new:
                    candidates.append(new_trail_stop)
                if candidates:
                    exit_px = min(candidates)  # worse for a seller = lower price
                    pc = (exit_px - entry_price) / entry_price
                    trades.append(dict(entry_i=entry_bar, exit_i=i, entry_p=entry_price, exit_p=exit_px,
                                        held=held, result=WIN if pc > 0 else LOSS, ret=pc, exit_reason='TRAIL'))
                    in_trade = trailing = False
                    peak = new_peak
                    continue
                peak = new_peak
                if held >= max_hours_to_hold:
                    pc = (cp - entry_price) / entry_price
                    trades.append(dict(entry_i=entry_bar, exit_i=i, entry_p=entry_price, exit_p=cp,
                                        held=held, result=TWIN if pc > 0 else TLOSS, ret=pc, exit_reason='TIME'))
                    in_trade = False
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

    return trades


def _summarize(trades):
    compounded = 1.0
    trail_n = 0
    for t in trades:
        compounded *= (1.0 + t['ret'])
        if t.get('exit_reason') == 'TRAIL':
            trail_n += 1
    return dict(n=len(trades), compounded_pct=round((compounded - 1.0) * 100, 2), trail_n=trail_n)


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

        args_common = (take_profit, fixed_sl, trail_buy_pct, trail_pct, max_hold_hours, 9, 14, node['z'])
        high_first = simulate_trailing_variant(p, *args_common, mode='high_first')
        low_first = simulate_trailing_variant(p, *args_common, mode='low_first')
        worst = simulate_trailing_variant(p, *args_common, mode='worst')

        # Sanity: high_first should be byte-parity with the real kernel's 'possible'.
        strict = simulate_trail_both_annotated(p, take_profit, fixed_sl, max_hold_hours,
                                                trail_buy_pct, trail_pct, 9, 14, node['z'], open_check=True)

        sh, sl, sw, ss = _summarize(high_first), _summarize(low_first), _summarize(worst), _summarize(strict)
        affected = sh['trail_n'] - 0  # will refine below via direct diff count
        rows.append(dict(ticker=ticker, strict_n=ss['n'], strict_pct=ss['compounded_pct'],
                          hf_n=sh['n'], hf_pct=sh['compounded_pct'], hf_trail=sh['trail_n'],
                          lf_n=sl['n'], lf_pct=sl['compounded_pct'], lf_trail=sl['trail_n'],
                          w_n=sw['n'], w_pct=sw['compounded_pct'], w_trail=sw['trail_n'],
                          parity_ok=(ss['n'] == sh['n'] and abs(ss['compounded_pct'] - sh['compounded_pct']) < 0.5)))

    print(f"\n{'ticker':6} {'strict_n':>9} {'strict_%':>10} {'hf_n':>6} {'hf_%':>9} {'hf_trail':>9} "
          f"{'lf_n':>6} {'lf_%':>9} {'lf_trail':>9} {'w_n':>5} {'w_%':>9} {'w_trail':>8} {'parity':>7}")
    for r in rows:
        print(f"{r['ticker']:6} {r['strict_n']:>9} {r['strict_pct']:>10} {r['hf_n']:>6} {r['hf_pct']:>9} "
              f"{r['hf_trail']:>9} {r['lf_n']:>6} {r['lf_pct']:>9} {r['lf_trail']:>9} {r['w_n']:>5} "
              f"{r['w_pct']:>9} {r['w_trail']:>8} {str(r['parity_ok']):>7}")

    print("\nstrict  = real kernel ('possible' via run_backtest_v110 analog, simulate_trail_both_annotated)")
    print("hf      = this script's high_first (kernel's real assumption) -- should parity-match strict")
    print("lf      = low_first (the missing mirror: check Low against OLD stop before crediting High)")
    print("w       = worst-case per-bar (pick whichever ordering is worse this bar)")
    print("*_trail = count of TRAIL-exit trades in that variant")


if __name__ == '__main__':
    main()
