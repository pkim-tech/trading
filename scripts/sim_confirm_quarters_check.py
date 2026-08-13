"""Does requiring N consecutive quarters of a positive trailing-1yr return
actually predict better forward (next-quarter) performance? Built 2026-08-13
to test the "require 2 consecutive quarterly confirmations before promoting
a candidate" debounce idea empirically, instead of arguing about it --
raised directly: the "require it twice" reasoning doesn't bottom out on its
own logic (you could always ask "but what if THAT confirmation is noise
too"), so this checks whether it actually has predictive value at all,
against real data.

Method, per real live-node config (the 10 real brokerage/ira/roth tickers):
1. Replay full trade history (drought_overlay_test.get_trades_and_bars).
2. At every real calendar quarter-end in the ticker's data range, compute the
   trailing-1yr compounded return (trades whose exit falls in the preceding
   365 days) -- "pass" if positive, "fail" otherwise.
3. Track the current consecutive-pass streak length at each quarter-end.
4. Look at the ACTUAL forward return in the next calendar quarter (trades
   whose exit falls in that next quarter alone), bucketed by the streak
   length that preceded it.
5. Report mean/median forward return per streak-length bucket, with N
   prominently shown -- this project's real ticker/history count means every
   bucket is small, and any finding here is indicative, not conclusive.

Usage: .venv/bin/python scripts/sim_confirm_quarters_check.py
"""
import sqlite3
import sys
from pathlib import Path
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drought_overlay_test import get_trades_and_bars

LIVE_DB = Path(__file__).resolve().parent.parent / "cache/live/trading_live.db"


def _real_nodes():
    con = sqlite3.connect(LIVE_DB)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("""SELECT w.id, w.ticker, w.strategy, w.window, w.z_score_threshold as z, w.arm_sell_pct,
                   w.take_profit, w.fixed_sl, w.trail_buy_pct, w.trail_sell_pct, w.max_hold_hours, w.entry_timing
                   FROM watch_list w JOIN accounts a ON w.account = a.alias
                   WHERE w.state='live' AND a.trading_enabled=1 AND w.account IN ('brokerage', 'ira', 'roth')
                   ORDER BY w.ticker""")
    nodes = [dict(r) for r in cur.fetchall()]
    con.close()
    for n in nodes:
        n["arm_pct"] = n["arm_sell_pct"] if n["strategy"] == "TrailingBothZScoreBreakout" else n["take_profit"]
    return nodes


def _quarter_ends(start, end):
    return list(pd.date_range(start=start, end=end, freq="QE"))


def _compounded(rets):
    return float(np.prod([1.0 + r for r in rets]) - 1.0) if rets else None


def analyze_ticker(node):
    trades, df_h = get_trades_and_bars(node)
    exits = [(df_h.index[t["exit_i"]], t["ret"]) for t in trades if t.get("exit_i") is not None]
    if len(exits) < 8:
        return []  # too little history to say anything

    exits.sort(key=lambda x: x[0])
    start, end = exits[0][0], exits[-1][0]
    q_ends = _quarter_ends(start, end)
    if len(q_ends) < 4:
        return []

    pass_streak = 0
    fail_streak = 0
    records = []
    for i, q_end in enumerate(q_ends):
        window_start = q_end - pd.Timedelta(days=365)
        trailing_rets = [r for ts, r in exits if window_start < ts <= q_end]
        trailing_comp = _compounded(trailing_rets)
        passed = trailing_comp is not None and trailing_comp > 0
        pass_streak = pass_streak + 1 if passed else 0
        fail_streak = 0 if passed else fail_streak + 1

        if i + 1 < len(q_ends):
            next_q_start, next_q_end = q_end, q_ends[i + 1]
            fwd_rets = [r for ts, r in exits if next_q_start < ts <= next_q_end]
            fwd_comp = _compounded(fwd_rets)
            if fwd_comp is not None:
                records.append({"ticker": node["ticker"], "streak": pass_streak, "fail_streak": fail_streak,
                                 "forward_return_pct": fwd_comp * 100})
    return records


def main():
    all_records = []
    for node in _real_nodes():
        try:
            recs = analyze_ticker(node)
            all_records.extend(recs)
            print(f"{node['ticker']:6s}: {len(recs)} quarter-transitions")
        except Exception as e:
            print(f"{node['ticker']:6s}: FAILED ({e})", file=sys.stderr)

    def _report(title, key_fn, labels):
        by_bucket = defaultdict(list)
        for r in all_records:
            bucket = min(r[key_fn], 3)
            by_bucket[bucket].append(r["forward_return_pct"])
        print(f"\n=== {title} ===")
        print(f"{'Streak':>8s} {'N':>5s} {'Mean fwd%':>12s} {'Median fwd%':>13s} {'Win rate':>10s}")
        for bucket in sorted(by_bucket):
            vals = by_bucket[bucket]
            label = labels.get(bucket, f"{bucket}" if bucket < 3 else "3+")
            wr = 100 * sum(1 for v in vals if v > 0) / len(vals)
            print(f"{label:>8s} {len(vals):>5d} {np.mean(vals):>12.1f} {np.median(vals):>13.1f} {wr:>9.1f}%")

    print(f"\n({len(all_records)} total quarter-transitions across all tickers -- "
          f"~{len(set(r['ticker'] for r in all_records))} independent ticker-histories, heavily "
          f"autocorrelated within each, not {len(all_records)} independent trials)")
    _report("By preceding consecutive PASS streak (does sustained strength predict continuation?)",
            "streak", {0: "0 (fail)"})
    _report("By preceding consecutive FAIL streak (does a persistent loser stay a loser?)",
            "fail_streak", {0: "0 (pass)"})


if __name__ == "__main__":
    main()
