"""Monte Carlo estimate for the pre-coverage (older ~1yr, no real minute data) portion of
each ticker's history. Built 2026-08-21, same session as the ground-truth work -- see
docs/backlog_cache.md / docs/research_log.md.

Rationale (user's framing): rather than assuming the pre-coverage portion is either always
'possible' (today's actual convention -- optimistic) or always the worst-of-two guess (a
different, also-arbitrary single assumption), use the REAL measured ordering rate from the
covered portion (~49.5%-51.8% low_before_high across all 6 tickers, essentially a coin
flip -- see docs/research_log.md's 2026-08-21 (very late) entry) to randomly resolve each
ambiguous bar in the pre-coverage period, many times, and look at the resulting
distribution instead of a single point estimate.

At each WAIT-resolution bar, flips a coin: heads = 'possible'-style (Low-before-High,
update running_low from this bar's own Low before checking the High), tails =
'pessimistic'-style (check High against the prior running_low first). Same idea for the
trailing-stop peak-update ordering once in a trade.

Usage:
    .venv/bin/python scripts/sim_monte_carlo_precoverage.py [--tickers T ...] [--trials N]
"""
import argparse
import random
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategies
from backtester import prep_inputs, WIN, LOSS, TWIN, TLOSS, OPEN
from scripts.export_trades import load_hourly
from scripts.sim_5min_whipsaw import NODES
from scripts.sim_minute_ground_truth_full import load_minute_data, simulate_ground_truth

TB_NODES = [n for n in NODES if n['strategy'] == 'TrailingBothZScoreBreakout']


def simulate_coinflip(p, take_profit, stop_loss, trail_buy_pct, trail_pct,
                       max_hours_to_hold, target_h0, target_h1, z_thresh, cutoff_bar, rng):
    """Only simulates up to (not including) cutoff_bar -- the pre-coverage portion.
    At each WAIT-resolution and trailing-stop-ordering decision, flips a coin (rng,
    p=0.5, matching the measured real rate) between possible-style and pessimistic-style
    resolution."""
    prices, highs, lows, hours, opens = p['prices'], p['highs'], p['lows'], p['hours'], p['opens']
    daily_idx, sma_arr, std_arr = p['daily_idx'], p['sma_arr'], p['std_arr']
    trend_arr, has_trend = p['trend_arr'], p['has_trend']

    trades = []
    in_trade = waiting = trailing = False
    entry_price = stop_price = tp_price = peak = 0.0
    entry_bar = held = 0
    running_low = 0.0
    wait_bars = 0

    for i in range(min(cutoff_bar, len(prices))):
        cp, high, low, op = prices[i], highs[i], lows[i], opens[i]

        if in_trade:
            held += 1
            if trailing:
                # coin flip: possible-style (credit High first) vs pessimistic-style
                # (check Low against the OLD stop before crediting High)
                low_first = rng.random() < 0.5
                old_trail_stop = peak * (1.0 - trail_pct)
                if op <= old_trail_stop:
                    exit_px = op
                    pc = (exit_px - entry_price) / entry_price
                    trades.append(dict(entry_i=entry_bar, exit_i=i, ret=pc, exit_reason='TRAIL'))
                    in_trade = trailing = False
                    continue
                if low_first and low <= old_trail_stop:
                    exit_px = old_trail_stop
                    pc = (exit_px - entry_price) / entry_price
                    trades.append(dict(entry_i=entry_bar, exit_i=i, ret=pc, exit_reason='TRAIL'))
                    in_trade = trailing = False
                    continue
                if high > peak:
                    peak = high
                new_trail_stop = peak * (1.0 - trail_pct)
                if low <= new_trail_stop or held >= max_hours_to_hold:
                    exit_px = new_trail_stop if low <= new_trail_stop else cp
                    pc = (exit_px - entry_price) / entry_price
                    reason = 'TRAIL' if low <= new_trail_stop else 'TIME'
                    trades.append(dict(entry_i=entry_bar, exit_i=i, ret=pc, exit_reason=reason))
                    in_trade = trailing = False
                continue
            if op <= stop_price:
                pc = (op - entry_price) / entry_price
                trades.append(dict(entry_i=entry_bar, exit_i=i, ret=pc, exit_reason='SL'))
                in_trade = False
                continue
            if low <= stop_price:
                pc = (stop_price - entry_price) / entry_price
                trades.append(dict(entry_i=entry_bar, exit_i=i, ret=pc, exit_reason='SL'))
                in_trade = False
                continue
            if cp >= tp_price:
                trailing = True; peak = cp
                continue
            if held >= max_hours_to_hold:
                pc = (cp - entry_price) / entry_price
                trades.append(dict(entry_i=entry_bar, exit_i=i, ret=pc, exit_reason='TIME'))
                in_trade = False
            continue

        if waiting:
            wait_bars += 1
            low_first = rng.random() < 0.5
            buy_trigger_gap = running_low * (1.0 + trail_buy_pct)
            filled = False
            if op >= buy_trigger_gap:
                entry_price = op; filled = True
            elif low_first:
                # possible-style: update running_low from this bar's Low first.
                new_running_low = low if low < running_low else running_low
                trig = new_running_low * (1.0 + trail_buy_pct)
                if high >= trig:
                    entry_price = trig; filled = True
                running_low = new_running_low
            else:
                # pessimistic-style: check High against the OLD running_low first.
                trig = running_low * (1.0 + trail_buy_pct)
                if high >= trig:
                    entry_price = trig; filled = True
                else:
                    if low < running_low:
                        running_low = low
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

    return trades


def _comp(trades):
    c = 1.0
    for t in trades:
        c *= (1.0 + t['ret'])
    return (c - 1.0) * 100.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tickers', nargs='*', default=None)
    ap.add_argument('--trials', type=int, default=100)
    args = ap.parse_args()
    nodes = TB_NODES if not args.tickers else [n for n in TB_NODES if n['ticker'] in args.tickers]

    rows = []
    for node in nodes:
        ticker = node['ticker']
        tp, sl = node['arm_pct'] / 100.0, node['fixed_sl'] / 100.0
        tbp, tsp = node['trail_buy_pct'] / 100.0, node['trail_sell_pct'] / 100.0
        mh = node['max_hold_hours']

        df_h = load_hourly(ticker)
        df_daily = df_h.resample("D").last().dropna(subset=["Close"])
        strat = strategies.TrailingBothZScoreBreakout(window=node['window'], z_score_threshold=node['z'])
        ind = strat.generate_daily_indicators(df_daily)
        p = prep_inputs(df_h, ind)
        minute_df = load_minute_data(ticker)
        ts = p['timestamps']

        minute_min = minute_df.index.min()
        cutoff_bar = int(np.searchsorted(ts.values, np.datetime64(minute_min)))

        gt_full, _, _ = simulate_ground_truth(p, minute_df, tp, sl, tbp, tsp, mh, 9, 14, node['z'])
        post_trades = [t for t in gt_full if ts[t['entry_i']] >= minute_min]
        post_pct = _comp(post_trades)

        rng = random.Random(42)
        pre_results = []
        for trial in range(args.trials):
            trades = simulate_coinflip(p, tp, sl, tbp, tsp, mh, 9, 14, node['z'], cutoff_bar, rng)
            pre_results.append(_comp(trades))
        pre_arr = np.array(pre_results)

        combined = (1 + pre_arr / 100.0) * (1 + post_pct / 100.0) * 100 - 100

        rows.append(dict(ticker=ticker, pre_mean=pre_arr.mean(), pre_p10=np.percentile(pre_arr, 10),
                          pre_p90=np.percentile(pre_arr, 90), post_pct=post_pct,
                          combined_mean=combined.mean(), combined_p10=np.percentile(combined, 10),
                          combined_p90=np.percentile(combined, 90)))

    print(f"\n{'ticker':6} {'pre_mean':>10} {'pre_p10':>9} {'pre_p90':>9} {'post_%':>9} "
          f"{'combined_mean':>14} {'combined_p10':>13} {'combined_p90':>13}")
    for r in rows:
        print(f"{r['ticker']:6} {r['pre_mean']:>10.1f} {r['pre_p10']:>9.1f} {r['pre_p90']:>9.1f} "
              f"{r['post_pct']:>9.1f} {r['combined_mean']:>14.1f} {r['combined_p10']:>13.1f} {r['combined_p90']:>13.1f}")


if __name__ == '__main__':
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
