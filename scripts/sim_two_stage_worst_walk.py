"""Per-decision-point worst-case walk between 'possible' and 'pessimistic' (NOT
'certain' -- see below), built 2026-08-21 (very late) at the user's direct request,
following the same-session correction that robust_alpha's MIN(possible, pessimistic,
certain) assumes the real trade sequence is globally consistent (every trade resolved
the same way), when a real sequence is genuinely mixed and path-dependent -- so MIN of
three pure totals isn't a proven lower bound on what a real mixed path could do.

Why 'certain' is excluded from this walk (not an oversight): backtester.py's own code
comment on the certain branch says it is "NOT uniformly a pessimistic-price bound,
unlike [possible/pessimistic]" -- certain sometimes credits a BETTER price than either
guess (the frozen-trigger case), and sometimes defers a fill entirely when the guesses
would have already filled. "Always pick whichever candidate is worst this bar" is not a
coherent operation against something that isn't reliably worse or better -- it needs its
own design, not a bolt-on to this walk. This script is scoped to `possible` vs
`pessimistic` only: genuine mirror-image guesses (Low-before-High vs High-before-Low),
so "which one is worse at this specific bar" is well-defined between the two.

Design: ONE shared bar-by-bar walk. At every bar where the position is `waiting`, BOTH
candidate rules (possible's Low-update-then-High-check; pessimistic's High-check-against-
prior-running_low-then-Low-update) are evaluated against the SAME running_low state each
maintains independently (they can diverge in running_low trajectory even on bars neither
fills). Whichever candidate FILLS this bar at the WORSE (higher, since buying) price is
taken; if only one fills, that one is taken; if neither fills, each candidate's own
running_low update rule is applied to ITS OWN tracked running_low (they can differ). Once
filled, exit-check logic (SL/TP/TRAIL/TIME) is IDENTICAL in structure across possible and
pessimistic (confirmed by reading backtester.py directly, 2026-08-21) -- the only
divergence point is entry price, so no separate "worst exit" choice is needed; the chosen
entry_price/stop_price/tp_price flow through the same, single exit-check logic from there.

Usage:
    .venv/bin/python scripts/sim_two_stage_worst_walk.py [--tickers T ...] [--dump]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategies
from backtester import prep_inputs, WIN, LOSS, TWIN, TLOSS, OPEN, run_backtest_v110
from scripts.export_trades import load_hourly
from scripts.sim_5min_whipsaw import NODES

TB_NODES = [n for n in NODES if n['strategy'] == 'TrailingBothZScoreBreakout']


def simulate_worst_walk(p, take_profit, stop_loss, trail_buy_pct, trail_pct,
                         max_hours_to_hold, target_h0, target_h1, z_thresh):
    prices, highs, lows, hours, opens = p['prices'], p['highs'], p['lows'], p['hours'], p['opens']
    daily_idx, sma_arr, std_arr = p['daily_idx'], p['sma_arr'], p['std_arr']
    trend_arr, has_trend = p['trend_arr'], p['has_trend']

    trades = []
    in_trade = waiting = trailing = False
    entry_price = stop_price = tp_price = peak = 0.0
    entry_bar = held = 0
    running_low_poss = running_low_pess = 0.0
    wait_bars = 0

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
            fill_poss = fill_pess = None  # candidate entry price, or None if no fill this bar

            # possible: Open-gap check first, else update running_low from this bar's
            # own Low BEFORE checking High against the freshly-updated trigger.
            trig_poss_gap = running_low_poss * (1.0 + trail_buy_pct)
            if op >= trig_poss_gap:
                fill_poss = op
            else:
                new_running_low_poss = low if low < running_low_poss else running_low_poss
                trig_poss = new_running_low_poss * (1.0 + trail_buy_pct)
                if high >= trig_poss:
                    fill_poss = trig_poss
                running_low_poss = new_running_low_poss

            # pessimistic: Open-gap check first, else check High against the PRIOR
            # (not-yet-updated) running_low; only update running_low with this bar's
            # Low afterward if it didn't fire.
            trig_pess_gap = running_low_pess * (1.0 + trail_buy_pct)
            if op >= trig_pess_gap:
                fill_pess = op
            else:
                trig_pess = running_low_pess * (1.0 + trail_buy_pct)
                if high >= trig_pess:
                    fill_pess = trig_pess
                else:
                    if low < running_low_pess:
                        running_low_pess = low

            candidates = [f for f in (fill_poss, fill_pess) if f is not None]
            if candidates:
                entry_price = max(candidates)  # worse for a buyer = higher price
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
            waiting = True; running_low_poss = op; running_low_pess = op; wait_bars = 0
            fired = True
        if not fired:
            signal_close = (cp <= lower_band) and (cp > trend_arr[di]) if has_trend else cp <= lower_band
            if signal_close:
                waiting = True; running_low_poss = cp; running_low_pess = cp; wait_bars = 0

    if in_trade:
        cp = prices[n - 1]
        pc = (cp - entry_price) / entry_price
        trades.append(dict(entry_i=entry_bar, exit_i=n - 1, entry_p=entry_price, exit_p=cp,
                            held=held, result=OPEN, ret=pc, exit_reason='OPEN'))

    return trades


def _summarize_local(trades):
    compounded = 1.0
    for t in trades:
        compounded *= (1.0 + t['ret'])
    return dict(n=len(trades), compounded_pct=round((compounded - 1.0) * 100, 2))


def _summarize_kernel(trades):
    compounded = 1.0
    for t in trades:
        compounded *= (1.0 + t['Return'])
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

        trades_possible, trades_pessimistic, trades_certain = run_backtest_v110(
            df_h, ind, ticker, take_profit=take_profit, stop_loss=fixed_sl,
            max_hours_to_hold=max_hold_hours, z_score_threshold=node['z'],
            trail_buy_pct=trail_buy_pct, trail_pct=trail_pct, entry_timing='open_check',
            return_bounds=True, prep=p)

        worst_walk = simulate_worst_walk(p, take_profit, fixed_sl, trail_buy_pct, trail_pct,
                                          max_hold_hours, 9, 14, node['z'])

        sp = _summarize_kernel(trades_possible)
        spess = _summarize_kernel(trades_pessimistic)
        sw = _summarize_local(worst_walk)
        robust_alpha_min = min(sp['compounded_pct'], spess['compounded_pct'])

        rows.append(dict(ticker=ticker, possible_n=sp['n'], possible_pct=sp['compounded_pct'],
                          pessimistic_n=spess['n'], pessimistic_pct=spess['compounded_pct'],
                          worst_walk_n=sw['n'], worst_walk_pct=sw['compounded_pct'],
                          robust_min=robust_alpha_min,
                          worse_than_min=sw['compounded_pct'] < robust_alpha_min))

    print(f"\n{'ticker':6} {'possible_n':>10} {'possible_%':>11} {'pessim_n':>9} {'pessim_%':>10} "
          f"{'walk_n':>7} {'walk_%':>10} {'min(p,ps)':>10} {'walk<min?':>10}")
    for r in rows:
        print(f"{r['ticker']:6} {r['possible_n']:>10} {r['possible_pct']:>11} {r['pessimistic_n']:>9} "
              f"{r['pessimistic_pct']:>10} {r['worst_walk_n']:>7} {r['worst_walk_pct']:>10} "
              f"{r['robust_min']:>10} {str(r['worse_than_min']):>10}")

    print("\nwalk = per-bar worst-of-{possible,pessimistic} walk-forward sim (this script). 'certain' excluded, see docstring.")
    print("min(p,ps) = MIN(possible, pessimistic) totals -- the 2-resolution analog of today's robust_alpha's MIN.")
    print("walk<min? = True means the path-dependent worst-walk is genuinely worse than the current MIN-of-pure-sims bound.")


if __name__ == '__main__':
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
