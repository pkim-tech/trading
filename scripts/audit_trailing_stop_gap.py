"""Stage-1 manual trade audit (see .claude/skills/backtest-change-rollout) for
the 2026-07-20 exit-side gap-through-trigger fix on the trailing-STOP path
(SL is audited separately -- tests/test_trailing_exit_gap.py already covers
SL with a synthetic case). Runs the real, fixed numba kernel
(run_backtest_v110, all three resolutions) on real historical bars for one
ticker/node, finds every trade whose trailing-stop exit was actually decided
by the gap fix (bar's Open already breached the trail-stop level confirmed
through the prior bar), and prints the raw OHLC + old-vs-new fill decision
for each one so a human can follow the logic by hand -- not just an
aggregate before/after number.

"Old code would have" is reconstructed by replaying forward from the
divergence bar under the pre-fix rule (Low-only check, ignore Open) until it
would have actually exited -- this can land on a materially different bar,
not just a different price on the same bar.

Usage: .venv/bin/python scripts/audit_trailing_stop_gap.py TICKER --window W
  --z Z --tp TP --sl SL --tb TB --ts TS --max-hours H [--entry-timing close|open_check]
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


def _replay_old_exit(opens, highs, lows, prices, start_i, peak, trail_pct,
                      entry_price, held, max_hours_to_hold):
    """Pre-fix rule from start_i onward: ignore Open, only check Low against a
    peak updated from High. Returns (exit_i, exit_price, held_at_exit)."""
    n = len(prices)
    for i in range(start_i, n):
        held += 1
        high, low, cp = highs[i], lows[i], prices[i]
        if high > peak:
            peak = high
        trail_stop = peak * (1.0 - trail_pct)
        if low <= trail_stop or held >= max_hours_to_hold:
            exit_px = trail_stop if low <= trail_stop else cp
            return i, exit_px, held
    return n - 1, prices[n - 1], held


def audit(ticker, window, z, tp, sl, tb, ts, max_hours, entry_timing):
    df = _load(ticker)
    strat = strategies.TrailingBothZScoreBreakout(window=window, z_score_threshold=z)
    df_daily = df.resample('D').last().dropna(subset=['Close'])
    df_ind = strat.generate_daily_indicators(df_daily)
    prep = prep_inputs(df, df_ind)

    possible, pessimistic, certain = run_backtest_v110(
        df, df_ind, ticker,
        take_profit=tp / 100.0, stop_loss=sl / 100.0, max_hours_to_hold=max_hours,
        z_score_threshold=z, trail_buy_pct=tb / 100.0, trail_pct=ts / 100.0,
        entry_timing=entry_timing, return_bounds=True, prep=prep,
    )

    prices, highs, lows, opens = prep['prices'], prep['highs'], prep['lows'], prep['opens']
    timestamps = prep['timestamps']
    idx_of_ts = {t: i for i, t in enumerate(timestamps)}

    trail_pct = ts / 100.0
    found = 0
    for label, trades in [('possible', possible), ('pessimistic', pessimistic), ('certain', certain)]:
        for tr in trades:
            if tr['Result'] not in ('WIN', 'LOSS'):
                continue
            entry_i = idx_of_ts[pd.Timestamp(tr['Entry Time'])]
            exit_i = idx_of_ts[pd.Timestamp(tr['Exit Time'])]
            entry_price = tr['Entry Price']
            tp_price = entry_price * (1.0 + tp / 100.0)

            # Replay forward from entry to find the real arm bar (Close first
            # clears tp_price) and the peak as of the bar BEFORE exit_i --
            # mirrors the kernel's own trailing state exactly.
            arm_i = None
            peak = 0.0
            i = entry_i
            while i < exit_i:
                i += 1
                if arm_i is None:
                    if prices[i] >= tp_price:
                        arm_i = i
                        peak = prices[i]
                    continue
                if i == exit_i:
                    break
                if highs[i] > peak:
                    peak = highs[i]
            if arm_i is None:
                continue  # exit wasn't a trailing-stop exit (SL/TIME instead)

            trail_stop_gap = peak * (1.0 - trail_pct)
            op = opens[exit_i]
            if op > trail_stop_gap:
                continue  # normal fill, not a gap-fix case

            # This is a real gap-fix divergence -- reconstruct what the old
            # (Low-only) rule would have done from this same bar forward.
            old_exit_i, old_exit_px, old_held = _replay_old_exit(
                opens, highs, lows, prices, exit_i, peak, trail_pct,
                entry_price, tr['hours_held'] - 1, max_hours
            )

            found += 1
            print(f"\n[{label}] {ticker} entry={tr['Entry Time']} @ {entry_price:.4f}  "
                  f"arm_bar={timestamps[arm_i]}")
            print(f"  Divergence bar {timestamps[exit_i]}: prior peak={peak:.4f}  "
                  f"trail_stop={trail_stop_gap:.4f}  "
                  f"O={opens[exit_i]:.4f} H={highs[exit_i]:.4f} L={lows[exit_i]:.4f} C={prices[exit_i]:.4f}")
            print(f"  NEW (fixed):  exit @ Open = {tr['Exit Price']:.4f}  "
                  f"({timestamps[exit_i]}, held={tr['hours_held']}h)  ret={tr['Return']*100:.2f}%")
            old_ret = (old_exit_px - entry_price) / entry_price
            print(f"  OLD (pre-fix): would exit @ {old_exit_px:.4f}  "
                  f"({timestamps[old_exit_i]}, held={old_held}h)  ret={old_ret*100:.2f}%")
            print(f"  Delta: new fill is {(tr['Exit Price'] - old_exit_px):+.4f} vs. old "
                  f"({(tr['Exit Price']/old_exit_px - 1)*100:+.2f}%), "
                  f"{old_exit_i - exit_i} bar(s) earlier than old exit would have landed")

    print(f"\n{found} real gap-fix divergence(s) found across possible/pessimistic/certain.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--window", type=int, required=True)
    ap.add_argument("--z", type=float, default=2.0)
    ap.add_argument("--tp", type=float, required=True, help="axis_tp / arm_sell_pct %%")
    ap.add_argument("--sl", type=float, required=True, help="fixed_sl %%")
    ap.add_argument("--tb", type=float, required=True, help="trail_buy_pct %%")
    ap.add_argument("--ts", type=float, required=True, help="trail_sell_pct %%")
    ap.add_argument("--max-hours", type=int, required=True)
    ap.add_argument("--entry-timing", default="close", choices=["close", "open_check"])
    args = ap.parse_args()
    audit(args.ticker, args.window, args.z, args.tp, args.sl, args.tb, args.ts,
          args.max_hours, args.entry_timing)
