"""Read-only/compute-only: dumps the real GT-kernel (backtester.run_backtest_ground_truth)
trade list for ONE specific param set, no watch_list lookup required -- for candidate_nodes
rows that aren't live yet (e.g. SOXL's v6 pick, id=851, held back from tonight's promotion
batch1 only because of a real open position, not because the pick itself is in question).

Same call pattern as scripts/compare_all_live_5yr_massive.py (read-only, no sweep dispatch,
no backtest_cache writes) -- just parameterized directly instead of via load_nodes(), since
this node isn't in watch_list.

Usage:
    .venv/bin/python scripts/dump_gt_trades_single_node.py
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__)) + "/.."
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from backtester import run_backtest_ground_truth  # noqa: E402
from sim_minute_groundtruth_independent import load_hourly, load_minutes, daily_indicators  # noqa: E402

# candidate_nodes id=851, SOXL's real v6 pick
TICKER = "SOXL"
WINDOW = 10
Z_THRESH = 1.5
FIXED_SL = 2.0
ARM_PCT = 29.0
TRAIL_BUY_PCT = 9.0
TRAIL_SELL_PCT = 7.0
MAX_HOLD_HOURS = 84
IS_BOTH = True
OPEN_CHECK = True
START = "2021-08-23"
END = "2026-08-21"
DATA_SOURCE = "massive"


def main():
    t0 = time.time()
    print(f"[1/5] loading hourly ({DATA_SOURCE})...", flush=True)
    dfh = load_hourly(TICKER, data_source=DATA_SOURCE)
    print(f"      {len(dfh):,} hourly rows, elapsed={time.time()-t0:.1f}s", flush=True)

    print(f"[2/5] loading minute data ({DATA_SOURCE}) -- this is usually the slow step...", flush=True)
    t1 = time.time()
    mdf = load_minutes(TICKER, data_source=DATA_SOURCE)
    print(f"      {len(mdf):,} minute rows, elapsed={time.time()-t1:.1f}s (total {time.time()-t0:.1f}s)", flush=True)

    print("[3/5] computing daily indicators...", flush=True)
    t1 = time.time()
    ind = daily_indicators(dfh, WINDOW)
    bars = dfh.loc[START:END + " 23:59:59"]
    print(f"      done, elapsed={time.time()-t1:.1f}s (total {time.time()-t0:.1f}s)", flush=True)

    print("[4/5] running GT kernel (numba JIT compile on first call can take a "
          "noticeable pause, then it's fast)...", flush=True)
    t1 = time.time()
    trades = run_backtest_ground_truth(
        bars, ind, TICKER, mdf,
        fixed_sl=FIXED_SL, arm_pct=ARM_PCT, trail_buy_pct=TRAIL_BUY_PCT,
        trail_sell_pct=TRAIL_SELL_PCT, max_hours_to_hold=MAX_HOLD_HOURS,
        z_score_threshold=Z_THRESH, is_both=IS_BOTH,
        open_check_entry_timing=OPEN_CHECK, same_bar_reentry=True,
    )
    print(f"      {len(trades)} trades, elapsed={time.time()-t1:.1f}s (total {time.time()-t0:.1f}s)", flush=True)

    print("[5/5] saving + computing CAGR...", flush=True)
    import pandas as pd
    df = pd.DataFrame(trades)
    out_path = os.path.join(ROOT, "output", "gt_kernel_trades_soxl_851.csv")
    df.to_csv(out_path, index=False)
    print(f"      saved to {out_path}")
    print(df.head(10).to_string())

    bal = 1.0
    for t in trades:
        bal *= (1 + t["Return"])
    years = (bars.index.max() - bars.index.min()).total_seconds() / (365.25 * 86400)
    cagr = (bal ** (1 / years) - 1) * 100
    print(f"\ncompounded CAGR from this trade list: {cagr:.2f}% over {years:.2f}y "
          f"(total elapsed {time.time()-t0:.1f}s)")


if __name__ == "__main__":
    main()
