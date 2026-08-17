"""Stage-1 manual trade audit (see .claude/skills/backtest-change-rollout) for
the new min_hold_hours compliance-floor parameter added to
backtester.py::_simulate_trail_both/run_backtest_v110 (2026-08-17, real
backlog item relayed via peer session "planner": a firm compliance policy
mandates a 15-trading-day minimum hold, blocking ALL exits including SL
during that window -- research/backtest-only, no live-automation path ever,
since real execution requires manual compliance sign-off on both entry and
exit per trade).

Runs the real, fixed numba kernel (run_backtest_v110, all three resolutions)
twice on real historical bars for one ticker/node -- once with
min_hold_hours=0 (baseline, byte-identical to no-floor behavior) and once
with the real floor -- and prints every trade where the floor actually
changed the outcome (delayed exit, or an exit that never re-fires and ends
OPEN) next to the raw OHLC that justifies it, so a human can follow the
logic by hand instead of trusting an aggregate before/after stat.

Usage: .venv/bin/python scripts/audit_min_hold_hours.py TICKER --window W
  --z Z --tp TP --sl SL --tb TB --ts TS --max-hours H --min-hold-hours N
  [--entry-timing close|open_check]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from backtester import prep_inputs, run_backtest_v110
import strategies

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "research"


def _load(ticker):
    df = pd.read_csv(CACHE_DIR / f"{ticker}_1h.csv", index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df


def _key(tr):
    return (pd.Timestamp(tr['Entry Time']), tr['Entry Price'])


def audit(ticker, window, z, tp, sl, tb, ts, max_hours, min_hold_hours, entry_timing):
    df = _load(ticker)
    strat = strategies.TrailingBothZScoreBreakout(window=window, z_score_threshold=z)
    df_daily = df.resample('D').last().dropna(subset=['Close'])
    df_ind = strat.generate_daily_indicators(df_daily)
    prep = prep_inputs(df, df_ind)

    kwargs = dict(
        take_profit=tp / 100.0, stop_loss=sl / 100.0, max_hours_to_hold=max_hours,
        z_score_threshold=z, trail_buy_pct=tb / 100.0, trail_pct=ts / 100.0,
        entry_timing=entry_timing, return_bounds=True, prep=prep,
    )
    baseline = run_backtest_v110(df, df_ind, ticker, min_hold_hours=0, **kwargs)
    floored = run_backtest_v110(df, df_ind, ticker, min_hold_hours=min_hold_hours, **kwargs)

    prices, highs, lows, opens = prep['prices'], prep['highs'], prep['lows'], prep['opens']
    timestamps = prep['timestamps']

    print(f"{ticker}  window={window} z={z} tp={tp}% sl={sl}% tb={tb}% ts={ts}% "
          f"max_hours={max_hours} min_hold_hours={min_hold_hours} entry_timing={entry_timing}\n")

    changed = 0
    for label, base_trades, floor_trades in [
        ('possible', baseline[0], floored[0]),
        ('pessimistic', baseline[1], floored[1]),
        ('certain', baseline[2], floored[2]),
    ]:
        base_by_entry = {_key(tr): tr for tr in base_trades}
        floor_by_entry = {_key(tr): tr for tr in floor_trades}
        common_entries = set(base_by_entry) & set(floor_by_entry)

        for entry_key in sorted(common_entries):
            b = base_by_entry[entry_key]
            f = floor_by_entry[entry_key]
            if b['Exit Time'] == f['Exit Time'] and b['Result'] == f['Result']:
                continue  # floor had no effect on this trade

            changed += 1
            entry_time, entry_price = entry_key
            print(f"[{label}] entry={entry_time} @ {entry_price:.4f}")
            print(f"  BASELINE (min_hold=0): exit={b['Exit Time']} @ {b['Exit Price']:.4f} "
                  f"held={b['hours_held']}h result={b['Result']} ret={b['Return']*100:+.2f}%")
            print(f"  FLOORED  (min_hold={min_hold_hours}): exit={f['Exit Time']} @ {f['Exit Price']:.4f} "
                  f"held={f['hours_held']}h result={f['Result']} ret={f['Return']*100:+.2f}%")
            print(f"  held-delta: {f['hours_held'] - b['hours_held']}h  "
                  f"return-delta: {(f['Return'] - b['Return'])*100:+.2f}pp")

            # print the raw bars spanning the divergence window, for hand-verification
            b_exit_i = list(timestamps).index(pd.Timestamp(b['Exit Time']))
            f_exit_i = list(timestamps).index(pd.Timestamp(f['Exit Time']))
            lo_i, hi_i = min(b_exit_i, f_exit_i) - 1, max(b_exit_i, f_exit_i)
            lo_i = max(lo_i, 0)
            print("  bars around both exits:")
            for i in range(lo_i, hi_i + 1):
                marker = []
                if i == b_exit_i:
                    marker.append("<-- baseline exit")
                if i == f_exit_i:
                    marker.append("<-- floored exit")
                print(f"    {timestamps[i]}  O={opens[i]:.4f} H={highs[i]:.4f} "
                      f"L={lows[i]:.4f} C={prices[i]:.4f}  {' '.join(marker)}")
            print()

        # entries present in only one side (shouldn't normally happen since
        # min_hold_hours never changes entry logic -- flag it if it does)
        only_base = set(base_by_entry) - set(floor_by_entry)
        only_floor = set(floor_by_entry) - set(base_by_entry)
        if only_base or only_floor:
            print(f"[{label}] WARNING: entry-set mismatch between baseline and floored runs "
                  f"(only_base={len(only_base)}, only_floor={len(only_floor)}) -- "
                  f"min_hold_hours should never change entry timing, investigate.")

    print(f"\n{changed} trade(s) where min_hold_hours changed the outcome, "
          f"across possible/pessimistic/certain.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--window", type=int, required=True)
    ap.add_argument("--z", type=float, default=1.0)
    ap.add_argument("--tp", type=float, required=True, help="arm_sell_pct %%")
    ap.add_argument("--sl", type=float, required=True, help="fixed_sl %%")
    ap.add_argument("--tb", type=float, required=True, help="trail_buy_pct %%")
    ap.add_argument("--ts", type=float, required=True, help="trail_sell_pct %%")
    ap.add_argument("--max-hours", type=int, required=True)
    ap.add_argument("--min-hold-hours", type=int, required=True)
    ap.add_argument("--entry-timing", default="open_check", choices=["close", "open_check"])
    args = ap.parse_args()

    audit(args.ticker, args.window, args.z, args.tp, args.sl, args.tb, args.ts,
          args.max_hours, args.min_hold_hours, args.entry_timing)
