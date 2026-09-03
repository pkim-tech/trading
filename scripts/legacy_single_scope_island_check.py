"""Diagnostic tool (2026-09-03): run the LEGACY ground-truth pipeline (run_optimization_
sweep.py -- NOT bench_phase1_phase2_inmemory.py) for exactly ONE (window, z_score_threshold)
scope, against a ticker's CURRENT data, and report whether legacy's real final-candidate
selection (derive_phase25_candidates_ground_truth) still surfaces a given (take_profit,
stop_loss) coordinate in its top-3-per-island pooled output.

Built to answer a specific research question: HIBL's real v6 promotion (candidate_nodes
id=907, arm_pct=28/trail_buy_pct=3, window=10, z=1.0, fixed_sl=3, entry_timing=open_check)
doesn't survive under today's v6.5/bench_phase1_phase2_inmemory.py pooling -- is that a
REAL regression in the bench rewrite, or does the identical global-pooling design already
present in LEGACY's own derive_phase25_candidates_ground_truth (confirmed via code read,
2026-09-03 -- see run_optimization_sweep.py:2863, which pools ALL window/z/tpct combos into
one N_ISLANDS-wide pick_island_centers call, same shape as bench's centers25/final_centers)
produce the same miss when run fresh against CURRENT data? Scoped to a single (window, z)
slice (not the real full 2x3 grid) purely for speed -- legacy's Phase1-Coarse-GT/Phase2-
Island-GT/derive_phase25_candidates_ground_truth are otherwise called exactly as
run_ground_truth_phase1.py's own real production entrypoint would, same functions, same
kernel, same cache table -- this is not a re-implementation.

Reusable for any future "does legacy still find X" single-scope check -- not a one-off
throwaway (see feedback_repeatable_scripts_not_oneoffs in agent memory).

Usage:
    .venv/bin/python scripts/legacy_single_scope_island_check.py --ticker HIBL \
        --strategy TrailingBothZScoreBreakout --fixed-sl 3 --entry-timing open_check \
        --window 10 --z 1.0 --start 2021-08-23 --end 2026-08-21 \
        --watch-arm 28 --watch-sl 3 --workers 8
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
    window_version_suffix, run_phase2_island_ground_truth, derive_phase25_candidates_ground_truth,
    _trail_pcts_for_strategy,
)
import campaign_config

HOLD_TIME_CAPS = [7, 14, 21, 28, 35, 42, 49, 56, 63, 70, 77, 84, 91, 98, 105, 112, 119, 126, 133, 140]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--strategy", required=True, choices=sorted(campaign_config.STRATEGIES))
    ap.add_argument("--fixed-sl", dest="fixed_sl", type=int, required=True)
    ap.add_argument("--entry-timing", dest="entry_timing", default="open_check")
    ap.add_argument("--window", type=int, nargs="+", required=True,
                     help="one or more window values -- multiple values stay ONE pooled "
                          "Phase1/Phase2.5 scope (shared island centers / top-3-per-island "
                          "ranking across all of them), matching the real production grid's "
                          "pooling shape, not N separate single-window runs.")
    ap.add_argument("--z", dest="z_thresholds", type=float, nargs="+", required=True,
                     help="one or more z-score threshold values, same pooled-scope semantics as --window.")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--watch-arm", dest="watch_arm", type=int, required=True,
                     help="the take_profit/arm value to check for in the final top-3-per-island output")
    ap.add_argument("--watch-sl", dest="watch_sl", type=int, required=True,
                     help="the stop_loss-axis value (trail_buy_pct for TrailingBoth) paired with --watch-arm")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--generations", type=int, default=3)
    ap.add_argument("--version-tag", dest="version_tag", default="hiblcmp",
                     help="distinguishing marker in the version string so this diagnostic run "
                          "can never collide with a real production version under the same "
                          "ticker/window/date-range (default 'hiblcmp' -- change per ticker/scope)")
    args = ap.parse_args()

    grid = campaign_config.STRATEGIES[args.strategy]
    TAKE_PROFITS = grid["take_profits"]
    STOP_LOSSES = grid["stop_losses"]
    TRAIL_PCTS = _trail_pcts_for_strategy(args.strategy, grid)

    init_idempotent_db()
    rebuild_indexes()

    version = f"v6-{args.version_tag}-massive" + window_version_suffix(args.start, args.end)
    run_timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    tasks = [(int(tp), int(sl), int(hold), w, z, float(tpct))
             for z in args.z_thresholds for w in args.window
             for tp in TAKE_PROFITS for sl in STOP_LOSSES
             for hold in HOLD_TIME_CAPS for tpct in TRAIL_PCTS]
    print(f"{len(tasks):,} Phase1-coarse-GT cells, scope window={args.window} z={args.z_thresholds}, "
          f"version={version}, workers={args.workers}")

    asset_bh, spy_bh = compute_bh_returns(args.ticker, start_date=args.start, end_date=args.end,
                                           data_source="massive")
    if asset_bh is None:
        raise SystemExit(f"compute_bh_returns returned None for {args.ticker} -- no "
                          f"massive_hourly_derived build exists for this ticker (or SPY).")

    hp = {
        "windows": args.window,
        "z_score_thresholds": args.z_thresholds,
        "take_profits": TAKE_PROFITS,
        "stop_losses": STOP_LOSSES,
        "hold_time_caps": HOLD_TIME_CAPS,
        "trail_pcts": grid["trail_pcts"],
    }

    t0 = time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        dispatch_parallel_grid_ground_truth(
            pool, tasks, args.ticker, args.strategy, version, "Phase1-Coarse-GT",
            spy_bh, asset_bh, run_timestamp, fixed_sl=args.fixed_sl, entry_timing=args.entry_timing,
            same_bar_reentry=True, start_date=args.start, end_date=args.end, data_source="massive",
        )
        print(f"Phase1-Coarse-GT done in {time.time()-t0:.1f}s")

        t1 = time.time()
        for gen in range(args.generations):
            print(f"Phase2-Island-GT generation {gen+1}/{args.generations}...")
            run_phase2_island_ground_truth(
                pool, args.ticker, args.strategy, version, hp, spy_bh, asset_bh, run_timestamp,
                fixed_sl=args.fixed_sl, entry_timing=args.entry_timing, same_bar_reentry=True,
                start_date=args.start, end_date=args.end, data_source="massive", generation=gen + 1,
            )
        print(f"Phase2-Island-GT done in {time.time()-t1:.1f}s")

    candidates = derive_phase25_candidates_ground_truth(
        args.ticker, args.strategy, version, hp, fixed_sl=args.fixed_sl, entry_timing=args.entry_timing)

    print(f"\n=== derive_phase25_candidates_ground_truth: {len(candidates)} candidates "
          f"(pooled top-3-per-island, real legacy final-selection logic) ===")
    found = False
    for c in sorted(candidates, key=lambda r: -r["cagr"]):
        hit = (c["take_profit"] == args.watch_arm and c["stop_loss"] == args.watch_sl)
        if hit:
            found = True
        marker = "  <-- WATCHED CELL" if hit else ""
        print(f"  island({c['island_tp']},{c['island_sl']}): TP={c['take_profit']} "
              f"SL={c['stop_loss']} hold={c['max_hold_hours']}h w={c['window']} "
              f"z={c['z_score_threshold']} tpct={c['tpct']} -> cagr={c['cagr']:.2f}% "
              f"robust_alpha={c['robust_alpha']:.2f}%{marker}")

    print(f"\nWatched cell (TP={args.watch_arm}, SL={args.watch_sl}) "
          f"{'FOUND' if found else 'NOT FOUND'} in legacy's real top-3-per-island pooled output.")


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
