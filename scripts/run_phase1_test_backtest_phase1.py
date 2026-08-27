"""Test script for the backtest_phase1 schema-v2 experiment
(docs/plans/backtest_schema_v2_phase_tables.md) -- runs the SAME real Phase1-Coarse-GT
grid as scripts/run_ground_truth_phase1.py, for the same ticker/window, but through the
NEW dispatch_parallel_grid_ground_truth_phase1() function (writes to backtest_phase1,
not backtest_cache). Does not touch backtest_cache at all, does not call Phase2/2.5 --
Phase1 only, so the result can be diffed against SOXL's real existing
v6-massive-w2021-08-23_2026-08-21 Phase1-Coarse-GT rows in backtest_cache for a real
end-to-end comparison (per the plan doc's test-plan item #3/#6).

Reuses SOXL's real live-node strategy/fixed_sl (load_live_node) and the SAME date
window/version-base already used for the granularity work (2021-08-23..2026-08-21,
data_source='massive') so this is directly comparable to real existing data, not a
fresh/incomparable window.

Per this project's standing convention, this script is meant to be run by the user in
their own shell -- not launched by the agent.

Usage: .venv/bin/python scripts/run_phase1_test_backtest_phase1.py [--workers 8]
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
    init_idempotent_db, compute_bh_returns, window_version_suffix,
    dispatch_parallel_grid_ground_truth_phase1, _trail_pcts_for_strategy,
)
from run_ground_truth_neighborhood import load_live_node
import campaign_config

TICKER = "SOXL"
START, END = "2021-08-23", "2026-08-21"
DATA_SOURCE = "massive"

WINDOWS = [10, 20]
Z_THRESHOLDS = [1.0, 1.5, 2.0]
HOLD_TIME_CAPS = [7, 14, 21, 28, 35, 42, 49, 56, 63, 70, 77, 84, 91, 98, 105, 112, 119, 126, 133, 140]
ENTRY_TIMING = "open_check"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--strategy", choices=sorted(campaign_config.STRATEGIES),
                     help="explicit strategy, used INSTEAD of load_live_node(TICKER)'s -- "
                          "same override pattern as run_ground_truth_phase1.py")
    ap.add_argument("--fixed-sl", dest="fixed_sl", type=int,
                     help="explicit fixed_sl, used INSTEAD of load_live_node(TICKER)'s")
    args = ap.parse_args()

    if args.strategy is not None:
        strategy_name = args.strategy
        fixed_sl = args.fixed_sl
        if fixed_sl is None:
            raise SystemExit("--strategy requires --fixed-sl too (no live-node lookup in this mode)")
        print(f"Permutation mode: strategy={strategy_name}, fixed_sl={fixed_sl} (no live-node lookup)")
    else:
        n = load_live_node(TICKER)
        print(f"Live node: {n}")
        strategy_name = n["strategy"]
        fixed_sl = n["fixed_sl"]
    if strategy_name not in campaign_config.STRATEGIES:
        raise SystemExit(
            f"{TICKER}'s resolved strategy is {strategy_name!r}, which has no grid entry "
            f"in campaign_config.STRATEGIES ({sorted(campaign_config.STRATEGIES)})."
        )
    grid = campaign_config.STRATEGIES[strategy_name]
    TAKE_PROFITS = grid["take_profits"]
    STOP_LOSSES = grid["stop_losses"]
    TRAIL_PCTS = _trail_pcts_for_strategy(strategy_name, grid)

    init_idempotent_db()  # creates backtest_phase1 if it doesn't exist yet

    version = "v6" + ("-massive" if DATA_SOURCE == "massive" else "") + window_version_suffix(START, END)
    run_timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    tasks = [(int(tp), int(sl), int(hold), int(w), float(z), float(tpct))
             for z in Z_THRESHOLDS
             for w in WINDOWS
             for tp in TAKE_PROFITS
             for sl in STOP_LOSSES
             for hold in HOLD_TIME_CAPS
             for tpct in TRAIL_PCTS]
    print(f"{len(tasks):,} Phase1-coarse cells, version={version}, workers={args.workers} "
          f"(writing to backtest_phase1, NOT backtest_cache)")

    asset_bh, spy_bh = compute_bh_returns(TICKER, start_date=START, end_date=END, data_source=DATA_SOURCE)
    if asset_bh is None:
        raise SystemExit(
            f"compute_bh_returns returned None for {TICKER} under data_source={DATA_SOURCE!r} -- "
            f"no massive_hourly_derived build exists for this ticker (or SPY)."
        )

    t0 = time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        dispatch_parallel_grid_ground_truth_phase1(
            pool, tasks, TICKER, strategy_name, version, "Phase1-Coarse-GT",
            spy_bh, asset_bh, run_timestamp, fixed_sl=fixed_sl, entry_timing=ENTRY_TIMING,
            same_bar_reentry=True, start_date=START, end_date=END, data_source=DATA_SOURCE,
        )
    print(f"Phase1-coarse-GT (backtest_phase1 test) done in {(time.time()-t0)/3600:.2f}h")
    print("Next step: diff backtest_phase1 rows against backtest_cache's real "
          f"kernel_version='ground_truth_v6' rows for the same (ticker={TICKER}, "
          f"strategy={strategy_name}, version={version}) scope.")


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
