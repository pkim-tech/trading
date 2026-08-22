"""Real Phase1-coarse v6 (ground-truth) sweep for a single ticker — 164,640 cells, full
standard grid (windows/z/tp/sl/hold/trail_pct, entry_timing='open_check' only per
campaign_config.py's established scope for TrailingBothZScoreBreakout). NOT a
neighborhood check — this is the real coarse discovery pass.

Built for docs/plans/ground_truth_kernel_rebuild.md Step 3, under explicit user
authorization (2026-08-21/22), and an explicit one-time exception to run it directly
(not user-run) given this is bespoke tooling with no config.json race condition (unlike
the standard run_sweep_queue.sh). Generic over ticker via --ticker (mirrors
run_ground_truth_neighborhood.py's own --ticker convention) -- the campaign grid
constants below (WINDOWS/Z_THRESHOLDS/COMBINED/TRAIL_PCTS/HOLD_TIME_CAPS) are a fixed
standard grid, not ticker-specific; only the live node config (strategy, fixed_sl, etc.,
via load_live_node) varies per ticker.

Default window matches the validated Step 2a parity test and the earlier neighborhood
check (2024-08-21..2026-08-20) — kept consistent for apples-to-apples comparison, since
"full history" was found this session to be an unstable, non-comparable window across
different campaigns (each ticker's cached hourly CSV range has grown over time).

--start/--end (added 2026-08-22) override the window explicitly -- e.g. for the full
real hourly-data overlap for a given ticker. A different window gets its own version
string automatically (window_version_suffix), so it writes fresh backtest_cache rows
rather than colliding with the 2024-08-21..2026-08-20 campaign's results.

**Phase2/2.5-GT auto-chain (added 2026-08-22, per the plan's Step 3 "search strategy:
unchanged" decision)**: after Phase1-coarse-GT finishes, this script automatically calls
run_phase2_island_ground_truth (fine mesh around Phase1's own island centers) and then
run_phase25_cliff_box_ground_truth (tight refinement around Phase2's true best node) --
same phased engine as the legacy hourly pipeline's __main__ block, just the GT function
counterparts. identify_island_candidates (the legacy pipeline's Checkpoint 1) is
deliberately NOT used here -- that function selects which TICKERS proceed past Phase 1
in a multi-ticker campaign; this script always runs one ticker at a time (per --ticker
invocation), and run_phase2_island_ground_truth already does its own per-(window,z,
trail_pct) island-center selection internally (via pick_island_centers, reading whatever
Phase1-Coarse-GT rows this run just wrote) -- there is no cross-ticker ranking step
needed here. Both GT phase functions already hard-block on their own completeness
checks for the exact campaign scope -- run_phase2_island_ground_truth on
Phase1-Coarse-GT completeness, run_phase25_cliff_box_ground_truth on BOTH
Phase1-Coarse-GT AND Phase2-Island-GT completeness (see their docstrings) -- so no
separate completeness check is needed in this script.
--max-phase lets a run stop after Phase1 or Phase2 without running the rest, mirroring the
legacy pipeline's own --max-phase convention.

Usage: .venv/bin/python scripts/run_ground_truth_phase1.py --ticker SOXL [--workers 8] [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--max-phase {1,2,2.5}]
Writes to cache/research/trading_universe.db, version='v6-w<start>_<end>'.
"""
import argparse
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from run_optimization_sweep import (
    init_idempotent_db, rebuild_indexes, dispatch_parallel_grid_ground_truth, compute_bh_returns,
    window_version_suffix, run_phase2_island_ground_truth, run_phase25_cliff_box_ground_truth,
)
from run_ground_truth_neighborhood import load_live_node

DEFAULT_START, DEFAULT_END = "2024-08-21", "2026-08-20"

WINDOWS = [10, 20]
Z_THRESHOLDS = [1.0, 1.5, 2.0]
COMBINED = [1, 2, 3, 4, 5, 6, 9, 12, 15, 18, 21, 24, 27, 30]
TRAIL_PCTS = [1, 2, 3, 4, 5, 6, 7]
HOLD_TIME_CAPS = [7, 14, 21, 28, 35, 42, 49, 56, 63, 70, 77, 84, 91, 98, 105, 112, 119, 126, 133, 140]
ENTRY_TIMING = "open_check"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=DEFAULT_END)
    ap.add_argument("--max-phase", dest="max_phase", default="2.5", choices=["1", "2", "2.5"],
                     help="Run phases up through this one, then stop (default: 2.5, full "
                          "Phase1-Coarse-GT -> Phase2-Island-GT -> Phase2.5-CliffBox-GT chain). "
                          "'1' stops after the coarse grid; '2' stops after the island mesh.")
    ap.add_argument("--data-source", choices=["yahoo", "massive"], default="yahoo",
                     help="hourly data source (default: yahoo, unchanged behavior). 'massive' "
                          "reads db_cache.get_massive_hourly_ohlcv (back to ~2021-08-23 vs "
                          "yahoo's ~2023-07-24 floor), wired only into Phase1-Coarse-GT/"
                          "compute_bh_returns so far -- run_phase2_island_ground_truth/"
                          "run_phase25_cliff_box_ground_truth aren't data_source-aware yet, "
                          "so 'massive' requires --max-phase 1.")
    args = ap.parse_args()
    TICKER = args.ticker
    START, END = args.start, args.end
    max_phase = args.max_phase
    data_source = args.data_source
    if data_source == "massive" and max_phase != "1":
        raise SystemExit(
            "--data-source massive currently requires --max-phase 1: "
            "run_phase2_island_ground_truth/run_phase25_cliff_box_ground_truth "
            "aren't wired for an alternate data source yet, so chaining into them "
            "here would silently mix a massive-sourced Phase1 with a yahoo-sourced "
            "Phase2/2.5."
        )
    if data_source == "massive":
        # CONFIRMED BLOCKER (paired review, 2026-08-22): massive_hourly_derived is
        # dividend/split-adjusted, but the minute feed run_backtest_ground_truth resolves
        # SL/TP/TRAIL intrabar triggers against (_load_minute_df -> {ticker}_1m.csv) is
        # NOT adjusted -- measured on SOXL: raw-minute-vs-massive-hourly price ratio drifts
        # from ~1.03 at 2021-08-23 to ~1.00 at 2026-08-21 (dividend depth grows going back
        # in time). Entry prices come from the adjusted hourly frame while exits are
        # resolved against unadjusted minute bars -- a systematic, silent price mismatch
        # bigger than most swept SL/TP thresholds (1-6%). A massive-sourced GT result is
        # NOT yet trustworthy for a real go/no-go decision until the minute feed gets the
        # same dividend adjustment applied. Fine for wiring/shape smoke-tests; not fine for
        # a real 5yr comparison campaign.
        print("WARNING: --data-source massive uses an unadjusted minute feed against "
              "adjusted hourly bars -- SL/TP/TRAIL exit prices will be systematically off. "
              "Do not trust these results for a live-trading decision yet.")

    n = load_live_node(TICKER)
    print(f"Live node: {n}")
    # Flagged by independent-cold review (2026-08-22): the grid/hp shape below (TRAIL_PCTS
    # swept as the 4th axis) is only valid for TrailingBothZScoreBreakout. load_live_node
    # also accepts TrailingExitZScoreBreakout (whose 4th axis collapses to [0.0]), which
    # would silently submit 7x redundant tpct duplicates per cell and, worse, produce zero
    # cache hits on any re-run (cache key uses the real 0.0 tpct, tasks carry 1.0-7.0).
    # Asserted so a ticker whose live node isn't TrailingBoth can't go live wrong.
    assert n["strategy"] == "TrailingBothZScoreBreakout", (
        f"This script's grid (TRAIL_PCTS swept as a real axis) is only valid for "
        f"TrailingBothZScoreBreakout, but {TICKER}'s live node is {n['strategy']!r}."
    )

    init_idempotent_db()
    rebuild_indexes()
    # '-massive' marker (paired review, 2026-08-22): dispatch_parallel_grid_ground_truth
    # hard-requires this in config_version whenever data_source='massive', so a massive run
    # can never collide with/overwrite yahoo-sourced rows sharing the same window suffix.
    version = "v6" + window_version_suffix(START, END) + ("-massive" if data_source == "massive" else "")
    run_timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    tasks = [(int(tp), int(sl), int(hold), int(w), float(z), float(tpct))
             for z in Z_THRESHOLDS
             for w in WINDOWS
             for tp in COMBINED
             for sl in COMBINED
             for hold in HOLD_TIME_CAPS
             for tpct in TRAIL_PCTS]
    print(f"{len(tasks):,} Phase1-coarse cells, version={version}, workers={args.workers}")

    asset_bh, spy_bh = compute_bh_returns(TICKER, start_date=START, end_date=END, data_source=data_source)
    if asset_bh is None:
        raise SystemExit(
            f"compute_bh_returns returned None for {TICKER} under data_source={data_source!r} -- "
            f"no massive_hourly_derived build exists for this ticker (or SPY). Run "
            f"scripts/build_massive_hourly_derived.py first."
        )

    # Full campaign hp dict -- MUST match the exact grid `tasks` above was built from,
    # since run_phase2_island_ground_truth/run_phase25_cliff_box_ground_truth's
    # completeness guards (_phase1_coarse_gt_status) recompute `expected` from these same
    # lists and compare against real backtest_cache rows. COMBINED is deliberately reused
    # for both take_profits and stop_losses -- Phase1's own task list above sweeps the
    # same COMBINED values across both the tp and sl loops.
    hp = {
        "windows": WINDOWS,
        "z_score_thresholds": Z_THRESHOLDS,
        "take_profits": COMBINED,
        "stop_losses": COMBINED,
        "hold_time_caps": HOLD_TIME_CAPS,
        "trail_pcts": TRAIL_PCTS,
    }

    t0 = time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        dispatch_parallel_grid_ground_truth(
            pool, tasks, TICKER, n["strategy"], version, "Phase1-Coarse-GT",
            spy_bh, asset_bh, run_timestamp, fixed_sl=n["fixed_sl"], entry_timing=ENTRY_TIMING,
            same_bar_reentry=True, start_date=START, end_date=END, data_source=data_source,
        )
        print(f"Phase1-coarse-GT done in {(time.time()-t0)/3600:.2f}h")

        if max_phase == "1":
            print("--max-phase 1: stopping after Phase1-Coarse-GT.")
            return

        t1 = time.time()
        run_phase2_island_ground_truth(
            pool, TICKER, n["strategy"], version, hp, spy_bh, asset_bh, run_timestamp,
            fixed_sl=n["fixed_sl"], entry_timing=ENTRY_TIMING, same_bar_reentry=True,
            start_date=START, end_date=END,
        )
        print(f"Phase2-Island-GT done in {(time.time()-t1)/3600:.2f}h")

        if max_phase == "2":
            print("--max-phase 2: stopping after Phase2-Island-GT.")
            return

        t2 = time.time()
        run_phase25_cliff_box_ground_truth(
            pool, TICKER, n["strategy"], version, hp, spy_bh, asset_bh, run_timestamp,
            fixed_sl=n["fixed_sl"], entry_timing=ENTRY_TIMING, same_bar_reentry=True,
            start_date=START, end_date=END,
        )
        print(f"Phase2.5-CliffBox-GT done in {(time.time()-t2)/3600:.2f}h")


if __name__ == "__main__":
    main()
