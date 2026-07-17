"""Quantifies the cost of intentionally deferring a same-day exit to the next
calendar day, instead of letting the real exit trigger (SL / trailing-stop
breach / TIME) fire immediately -- the opposite leg of the existing
same_day_block kernel feature (which defers a same-day *entry* after a prior
exit). Repeatable across tickers/nodes; only requires a ticker's cached hourly
CSV plus the node's real params, same inputs any other v4-era script uses.

Reuses scripts/export_trades.py's simulate_trail_both_annotated (baseline) and
the new simulate_trail_both_deferred_sell (deferred) -- both pure-Python
mirrors of backtester._simulate_trail_both, not reimplementations of the exit
logic from scratch. Entry-side logic between the two is byte-identical, so any
divergence in the results is purely from deferring same-day exits.

Caveat: these pure-Python mirrors only support entry_timing='close' (no
open_check branch) -- fine for most nodes, but GDXD's real live node uses
open_check, so results for GDXD here are indicative only, not its exact
production behavior.

Usage:
    .venv/bin/python scripts/sim_delayed_sell.py TICKER --window 20 --z 1.0 \\
        --tb 1.0 --arm 7.0 --sl 1.0 --ts 1.0 --max-hours 7
"""
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

import strategies
from backtester import prep_inputs
from export_trades import simulate_trail_both_annotated, simulate_trail_both_deferred_sell

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "research"


def _load(ticker):
    df_hourly = pd.read_csv(CACHE_DIR / f"{ticker}_1h.csv", index_col=0, parse_dates=True)
    df_hourly.index = pd.to_datetime(df_hourly.index).tz_localize(None)
    df_hourly = df_hourly.sort_index()
    close_col = 'Adj Close' if 'Adj Close' in df_hourly.columns else 'Close'
    df_daily = df_hourly.resample('D').last().dropna(subset=[close_col])
    return df_hourly, df_daily


def _compounded(trades):
    ret = 1.0
    for t in trades:
        ret *= (1.0 + t['ret'])
    return (ret - 1.0) * 100.0


def run(ticker, window, z, tb, arm, sl, ts, max_hours, target_hours=(9, 14)):
    df_hourly, df_daily = _load(ticker)
    strat = strategies.TrailingBothZScoreBreakout(window=window, z_score_threshold=z)
    df_daily_ind = strat.generate_daily_indicators(df_daily)
    p = prep_inputs(df_hourly, df_daily_ind)

    kwargs = dict(take_profit=arm / 100, stop_loss=sl / 100, max_hours_to_hold=max_hours,
                  trail_buy_pct=tb / 100, trail_pct=ts / 100,
                  target_h0=target_hours[0], target_h1=target_hours[1], z_thresh=z)

    baseline = simulate_trail_both_annotated(p, **kwargs)
    deferred = simulate_trail_both_deferred_sell(p, **kwargs)

    n_deferred = sum(1 for t in deferred if t.get('deferred'))
    print(f"{ticker}: baseline trades={len(baseline)}  deferred-sim trades={len(deferred)}  "
          f"({n_deferred} trades had their exit pushed to a later day)")
    print(f"  baseline compounded return:      {_compounded(baseline):+.1f}%")
    print(f"  deferred-exit compounded return: {_compounded(deferred):+.1f}%")

    # Once a trade is actually deferred, it holds longer, which delays the next
    # signal check -- baseline trade #N and deferred trade #N stop being the same
    # real entry after that point (deferred's whole subsequent timeline shifts).
    # Only entry_i values shared by both sequences are a valid apples-to-apples
    # per-trade comparison; report those up to the first divergence, then say so.
    base_by_entry = {t['entry_i']: t for t in baseline}
    def_by_entry = {t['entry_i']: t for t in deferred}
    shared_entries = sorted(set(base_by_entry) & set(def_by_entry))
    diverged_at = None
    timestamps = p['timestamps']
    print(f"\n  Per-trade comparison (entries present in both timelines, i.e. before any divergence):")
    for ei in shared_entries:
        bt, dt = base_by_entry[ei], def_by_entry[ei]
        if not dt.get('deferred'):
            continue
        entry_t = timestamps[ei]
        base_exit_t = timestamps[bt['exit_i']]
        def_exit_t = timestamps[dt['exit_i']]
        print(f"    entry {entry_t}  base_exit {base_exit_t} ({bt['ret']*100:+.2f}%)  "
              f"-> deferred_exit {def_exit_t} ({dt['ret']*100:+.2f}%)  "
              f"drift {(dt['ret']-bt['ret'])*100:+.2f}pp")
    all_entries_sorted = sorted(set(base_by_entry) | set(def_by_entry))
    for ei in all_entries_sorted:
        if ei not in base_by_entry or ei not in def_by_entry:
            diverged_at = ei
            break
    if diverged_at is not None:
        print(f"\n  Timelines diverge after entry {timestamps[diverged_at]} (a deferred exit shifted "
              f"when the next signal could be caught) -- trades after this point aren't the same "
              f"real entry in both sequences, so only the aggregate compounded-return comparison "
              f"above is meaningful past this, not a per-trade one.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('ticker')
    parser.add_argument('--window', type=int, required=True)
    parser.add_argument('--z', type=float, required=True)
    parser.add_argument('--tb', type=float, required=True, help='trail_buy_pct, %%')
    parser.add_argument('--arm', type=float, required=True, help='arm_sell_pct (take_profit), %%')
    parser.add_argument('--sl', type=float, required=True, help='stop_loss, %%')
    parser.add_argument('--ts', type=float, required=True, help='trail_sell_pct, %%')
    parser.add_argument('--max-hours', type=int, required=True, dest='max_hours')
    args = parser.parse_args()
    run(args.ticker, args.window, args.z, args.tb, args.arm, args.sl, args.ts, args.max_hours)
