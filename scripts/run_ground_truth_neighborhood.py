"""Step 4/5 scoped check: a small parameter-neighborhood ground-truth (v6) sweep around a
real live watch_list node's own config -- NOT a full production grid campaign.

Built for docs/plans/ground_truth_kernel_rebuild.md Steps 3-5, deliberately scoped down
from a full Phase1-coarse campaign after benchmarking showed ~1.6s/cell warm (vs sub-ms
for the hourly kernel) -- a full hp grid (thousands of cells, all Tranche-1 tickers) is not
sized/approved yet (see the plan's "Performance -- corrected" section). This mirrors
Phase2.5's cliff-box shape (+-CLIFF_RADIUS around the live config's own TP/SL, +-two weeks
of hold) at the node's ACTUAL live parameters, which is what Step 4 needs (is ground
truth's own value robust across the parameter neighborhood?) without launching an unsized
campaign. Generic over ticker -- run once per Tranche-1 ticker as each is authorized,
rather than hardcoding one ticker's params as module constants (a real live config for a
given ticker/strategy pulled fresh from cache/live/trading_live.db every run, not copied
into this file, so it can't silently drift stale against a config edit).

WINDOW: defaults to 2024-08-21..2026-08-20, the SAME window tonight's validated Python
prototype (scripts/sim_minute_groundtruth_independent.py) and the Step 2a parity test use
-- found live this session that "full history" is NOT a stable, comparable window across
different sweep campaigns (SOXL_1h.csv's cached range has grown since older v4/v5 rows
were written, so an unwindowed v6 run's spy_bh doesn't match an unwindowed v5 row's
spy_bh even though both claim "full history"). An explicit shared window is what makes a
ground-truth-vs-hourly-kernel comparison actually apples-to-apples.

Usage: .venv/bin/python scripts/run_ground_truth_neighborhood.py --ticker SOXL
       .venv/bin/python scripts/run_ground_truth_neighborhood.py --ticker AGQ --start 2024-08-21 --end 2026-08-20
Writes to cache/research/trading_universe.db, backtest_cache, version='v6-w<start>_<end>',
kernel_version='ground_truth_v6'. Real compute -- each ticker run requires its own
explicit user authorization per the standing "never launch a sweep campaign yourself"
rule (2026-08-21 exception granted for SOXL specifically, not a blanket authorization).
"""
import argparse
import os
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from run_optimization_sweep import (
    init_idempotent_db, dispatch_parallel_grid_ground_truth, compute_bh_returns,
    window_version_suffix, CLIFF_RADIUS, DB_PATH,
)

DEFAULT_START, DEFAULT_END = "2024-08-21", "2026-08-20"
LIVE_DB = os.path.join(ROOT, "cache", "live", "trading_live.db")


def load_live_node(ticker):
    con = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT id, ticker, strategy, window, z_score_threshold, fixed_sl, trail_buy_pct, "
        "trail_sell_pct, arm_sell_pct, take_profit, max_hold_hours, entry_timing "
        "FROM watch_list WHERE ticker=? AND state='live' AND strategy IN "
        "('TrailingBothZScoreBreakout','TrailingExitZScoreBreakout') LIMIT 1",
        (ticker,)
    ).fetchone()
    con.close()
    if row is None:
        raise ValueError(f"No real state='live' TrailingBoth/TrailingExit node found for {ticker}")
    n = dict(row)
    n["arm_pct"] = (n["arm_sell_pct"] if n["strategy"] == "TrailingBothZScoreBreakout"
                    else n["take_profit"]) or 0.0
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=DEFAULT_END)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    n = load_live_node(args.ticker)
    is_both = n["strategy"] == "TrailingBothZScoreBreakout"
    tp_c = int(round(n["arm_pct"]))
    sl_c = int(round(n["trail_buy_pct"] if is_both else n["trail_sell_pct"]))
    tpct_c = n["trail_sell_pct"] if is_both else 0.0
    hold_c = int(n["max_hold_hours"])

    print(f"Live node: id={n['id']} {n['ticker']} {n['strategy']} window={n['window']} "
          f"z={n['z_score_threshold']} fixed_sl={n['fixed_sl']} arm/tp={tp_c}% sl_axis={sl_c}% "
          f"4th_axis={tpct_c}% hold={hold_c}h entry_timing={n['entry_timing']}")

    init_idempotent_db()
    version = "v6" + window_version_suffix(args.start, args.end)
    run_timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    tasks = set()
    for tp in range(max(1, tp_c - CLIFF_RADIUS), min(30, tp_c + CLIFF_RADIUS) + 1):
        for sl in range(max(1, sl_c - CLIFF_RADIUS), min(30, sl_c + CLIFF_RADIUS) + 1):
            for hold in [h for h in (hold_c - 14, hold_c - 7, hold_c, hold_c + 7, hold_c + 14) if h > 0]:
                tasks.add((tp, sl, hold, int(n["window"]), float(n["z_score_threshold"]), tpct_c))
    tasks = list(tasks)
    print(f"{len(tasks)} neighborhood cells around arm/tp={tp_c} sl_axis={sl_c} hold={hold_c}h, "
          f"window=[{args.start}, {args.end}]")

    spy_bh, asset_bh = compute_bh_returns(args.ticker, start_date=args.start, end_date=args.end)

    t0 = time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        dispatch_parallel_grid_ground_truth(
            pool, tasks, args.ticker, n["strategy"], version, f"GT-{args.ticker}-Neighborhood",
            spy_bh, asset_bh, run_timestamp, fixed_sl=n["fixed_sl"], entry_timing=n["entry_timing"],
            same_bar_reentry=True, start_date=args.start, end_date=args.end,
        )
    print(f"done in {time.time()-t0:.1f}s")

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT axis_tp, trail_buy_pct, trail_sell_pct, max_hold_hours, alpha_vs_spy, strategy_return, trades
            FROM backtest_cache
            WHERE version=? AND ticker=? AND strategy=? AND kernel_version='ground_truth_v6'
              AND window=? AND z_score_threshold=?
            ORDER BY axis_tp, trail_buy_pct, max_hold_hours
        """, (version, args.ticker, n["strategy"], int(n["window"]), float(n["z_score_threshold"]))).fetchall()

    print(f"\n{len(rows)} rows in backtest_cache for this neighborhood (spy_bh={spy_bh:.1f}%):")
    for r in rows:
        print(f"  arm/tp={r[0]:.0f}% trail_buy={r[1]:.0f}% trail_sell={r[2]:.0f}% hold={r[3]}h -> "
              f"alpha={r[4]:+.1f}% return={r[5]:+.1f}% trades={r[6]}")

    # Found by paired-review independent-cold pass (2026-08-21): for TrailingExit
    # (is_both=False), the real sl axis is trail_sell_pct (r[2]), not trail_buy_pct
    # (r[1], always 0 for this strategy) -- the old filter (r[1]==0) matched every row
    # equally and silently picked whichever sorted first, not the actual live config.
    live_row = [r for r in rows if r[0] == tp_c
                and r[1] == (sl_c if is_both else 0)
                and r[2] == (tpct_c if is_both else sl_c)
                and r[3] == hold_c]
    if live_row:
        print(f"\n{args.ticker}'s actual live config: ground-truth alpha={live_row[0][4]:+.1f}%, "
              f"return={live_row[0][5]:+.1f}%, trades={live_row[0][6]}")
    if rows:
        worst = min(r[4] for r in rows)
        print(f"Worst-neighbor ground-truth alpha-vs-SPY in this box: {worst:+.1f}%")
        print(f"Plan's new selection bar is worst-neighbor CAGR > 20% (a raw-return bar, not "
              f"alpha-vs-SPY) -- this prints alpha-vs-SPY for now; re-derive CAGR from "
              f"strategy_return/trades/years directly before treating this as a pass/fail verdict.")


if __name__ == "__main__":
    main()
