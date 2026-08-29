"""Task #4 (2026-08-29, planner dispatch): root-cause a real 1m-vs-1s core CAGR
divergence found by Phase5's kernel-direct trade generation (Task #2) for a specific
node -- SOXL/TrailingBothZScoreBreakout/window=15/fixed_sl=1 (island TP=23/24 SL=14):
core CAGR 60.55% @1m vs 52.99% @1s (7.56pp delta), same 118/118 trade count both
resolutions -- a per-trade divergence, not a missing-trade issue.

Generic diagnostic, not single-node-specific: dumps both 1m and 1s trade lists for
ANY given node directly via backtester.run_backtest_ground_truth (the real production
kernel, same as Phase3/5 now use post-Task #2), then diffs them trade-by-trade
(entry/exit time, entry/exit price, return, exit_reason) to find the driving trade(s).

Usage:
  .venv/bin/python scripts/diagnose_1m_vs_1s_trade_divergence.py --ticker SOXL \\
      --strategy TrailingBothZScoreBreakout --window 15 --z 1.0 --fixed-sl 1 \\
      --arm-pct 23 --trail-buy-pct 14 --trail-sell-pct 6 --max-hold-hours 91
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from backtester import run_backtest_ground_truth
from sim_1s_vs_1m_groundtruth_overlays import (
    load_hourly, load_seconds, resample_seconds_to_minutes, daily_indicators,
)


def dump_trades(ticker, strategy, window, z, fixed_sl, arm_pct, trail_buy_pct, trail_sell_pct,
                 max_hold_hours, entry_timing, dfh, minute_df, start, end):
    is_both = strategy == "TrailingBothZScoreBreakout"
    ind = daily_indicators(dfh, int(window))
    bars = dfh.loc[start:end + " 23:59:59"]
    return run_backtest_ground_truth(
        bars, ind, ticker, minute_df,
        fixed_sl=fixed_sl, arm_pct=arm_pct, trail_buy_pct=trail_buy_pct,
        trail_sell_pct=trail_sell_pct, max_hours_to_hold=max_hold_hours,
        z_score_threshold=z, is_both=is_both,
        open_check_entry_timing=(entry_timing == "open_check"), same_bar_reentry=True,
        need_times=True,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--window", type=int, required=True)
    ap.add_argument("--z", type=float, required=True)
    ap.add_argument("--fixed-sl", type=float, required=True)
    ap.add_argument("--arm-pct", type=float, required=True)
    ap.add_argument("--trail-buy-pct", type=float, required=True)
    ap.add_argument("--trail-sell-pct", type=float, required=True)
    ap.add_argument("--max-hold-hours", type=int, required=True)
    ap.add_argument("--entry-timing", default="open_check")
    ap.add_argument("--data-source", choices=["yahoo", "massive"], default="massive")
    ap.add_argument("--start", default="2021-08-23")
    ap.add_argument("--end", default="2026-08-21")
    args = ap.parse_args()

    print(f"Loading {args.ticker} hourly data...")
    dfh = load_hourly(args.ticker, data_source=args.data_source)
    print(f"Loading {args.ticker} 1-second data (slow step)...")
    df_1s = load_seconds(args.ticker)
    df_1m = resample_seconds_to_minutes(df_1s)
    print(f"  {len(df_1s):,} 1s rows, {len(df_1m):,} 1m rows")

    kwargs = dict(ticker=args.ticker, strategy=args.strategy, window=args.window, z=args.z,
                  fixed_sl=args.fixed_sl, arm_pct=args.arm_pct, trail_buy_pct=args.trail_buy_pct,
                  trail_sell_pct=args.trail_sell_pct, max_hold_hours=args.max_hold_hours,
                  entry_timing=args.entry_timing, dfh=dfh, start=args.start, end=args.end)

    print("\nRunning kernel @1m...")
    trades_1m = dump_trades(minute_df=df_1m, **kwargs)
    print("Running kernel @1s...")
    trades_1s = dump_trades(minute_df=df_1s, **kwargs)
    print(f"\n1m: {len(trades_1m)} trades  |  1s: {len(trades_1s)} trades")

    if len(trades_1m) != len(trades_1s):
        print("DIFFERENT TRADE COUNTS -- this is a missing/extra-trade issue, not a per-trade one. "
              "Diff entry timestamps to find the divergence point.")
        set_1m = {(t['Entry Time'], round(t['Entry Price'], 4)) for t in trades_1m}
        set_1s = {(t['Entry Time'], round(t['Entry Price'], 4)) for t in trades_1s}
        print("In 1m not in 1s:", sorted(set_1m - set_1s))
        print("In 1s not in 1m:", sorted(set_1s - set_1m))
        return

    print(f"\n{'idx':>4} {'entry_time':>20} {'entry_1m':>10} {'entry_1s':>10} {'d_entry':>9} "
          f"{'exit_1m':>10} {'exit_1s':>10} {'d_exit':>9} {'ret_1m':>9} {'ret_1s':>9} {'d_ret_pp':>9} "
          f"{'reason_1m':>6} {'reason_1s':>6}")
    total_diff = 0.0
    biggest = None
    for i, (a, b) in enumerate(zip(trades_1m, trades_1s)):
        d_entry = a['Entry Price'] - b['Entry Price']
        d_exit = a['Exit Price'] - b['Exit Price']
        d_ret = (a['Return'] - b['Return']) * 100
        total_diff += d_ret
        if abs(d_entry) > 1e-9 or abs(d_exit) > 1e-9 or a['exit_reason'] != b['exit_reason']:
            print(f"{i:>4} {str(a['Entry Time']):>20} {a['Entry Price']:>10.4f} {b['Entry Price']:>10.4f} "
                  f"{d_entry:>9.4f} {a['Exit Price']:>10.4f} {b['Exit Price']:>10.4f} {d_exit:>9.4f} "
                  f"{a['Return']*100:>9.3f} {b['Return']*100:>9.3f} {d_ret:>9.3f} "
                  f"{a['exit_reason']:>6} {b['exit_reason']:>6}")
        if biggest is None or abs(d_ret) > abs(biggest[1]):
            biggest = (i, d_ret, a, b)

    print(f"\nSum of per-trade return-pct diffs (1m - 1s): {total_diff:+.3f}pp across {len(trades_1m)} trades")
    if biggest:
        i, d_ret, a, b = biggest
        print(f"\nBiggest single-trade diff: trade {i}, d_ret={d_ret:+.3f}pp")
        print(f"  1m: entry={a['Entry Time']} @ {a['Entry Price']:.4f}  exit={a['Exit Time']} @ "
              f"{a['Exit Price']:.4f}  reason={a['exit_reason']}  armed={a['armed']}")
        print(f"  1s: entry={b['Entry Time']} @ {b['Entry Price']:.4f}  exit={b['Exit Time']} @ "
              f"{b['Exit Price']:.4f}  reason={b['exit_reason']}  armed={b['armed']}")


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
