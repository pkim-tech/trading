"""Real Phase1-coarse v6 (ground-truth) sweep for a single ticker — full standard grid
(windows/z/tp/sl/hold[/trail_pct], entry_timing='open_check' only per campaign_config.py's
established scope), generic over BOTH live strategies (TrailingBothZScoreBreakout and
TrailingExitZScoreBreakout). NOT a neighborhood check — this is the real coarse discovery
pass.

Built for docs/plans/ground_truth_kernel_rebuild.md Step 3, under explicit user
authorization (2026-08-21/22), and an explicit one-time exception to run it directly
(not user-run) given this is bespoke tooling with no config.json race condition (unlike
the standard run_sweep_queue.sh). Generic over ticker via --ticker (mirrors
run_ground_truth_neighborhood.py's own --ticker convention). The per-strategy grid shape
(take_profits/stop_losses/whether a real 4th trail_pct axis exists) is looked up from
campaign_config.STRATEGIES[strategy_name], keyed off strategies.resolve_axis_columns --
NOT hardcoded here, since TrailingBothZScoreBreakout and TrailingExitZScoreBreakout sweep
physically different columns under the same "stop_losses" grid name (see
strategies.resolve_axis_columns / campaign_config.py's own per-strategy comments:
TrailingBoth's stop_losses is the COMBINED 1-30 grid mapped to trail_buy_pct, with a real
swept trail_sell_pct 4th axis; TrailingExit's stop_losses is actually the 1-7 TRAIL_PCTS
grid mapped to trail_pct, with NO real 4th axis -- collapsed to a single dummy value via
run_optimization_sweep._trail_pcts_for_strategy). WINDOWS/Z_THRESHOLDS/HOLD_TIME_CAPS are
genuinely strategy-agnostic (confirmed against campaign_config.py, which only varies
take_profits/stop_losses/trail_pcts/entry_timings per strategy) and stay as fixed
module-level constants, not ticker- or strategy-specific.

Default window matches the validated Step 2a parity test and the earlier neighborhood
check (2024-08-21..2026-08-20) — kept consistent for apples-to-apples comparison, since
"full history" was found this session to be an unstable, non-comparable window across
different campaigns (each ticker's cached hourly CSV range has grown over time).

--start/--end (added 2026-08-22) override the window explicitly -- e.g. for the full
real hourly-data overlap for a given ticker. A different window gets its own version
string automatically (window_version_suffix), so it writes fresh backtest_cache rows
rather than colliding with the 2024-08-21..2026-08-20 campaign's results.

--strategy/--fixed-sl (added 2026-08-22, for candidate-discovery/permutation campaigns
-- see scripts/run_ground_truth_sweep_queue_permutation.sh): when BOTH are passed
explicitly, they're used INSTEAD of load_live_node(TICKER)'s strategy/fixed_sl --
load_live_node() is not even called in that case, so this works for any ticker
regardless of what's currently live, under either strategy, at any fixed_sl value
(exactly the STRATEGY x FIXED_SL x TICKER permutation the legacy run_sweep_queue.sh
sweeps for the hourly kernel). This is deliberately NOT the same question the
existing --ticker-only mode answers ("does the kernel faithfully reproduce what's
already live") -- it's unconstrained candidate discovery. Passing only one of the two
is an error (ambiguous: which source does the other value come from?). Passing
neither preserves the original live-config-derived behavior unchanged, so the
already-working run_ground_truth_sweep_queue.sh / Step 4-5 use case keeps working
exactly as before.

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
       .venv/bin/python scripts/run_ground_truth_phase1.py --ticker SOXL --strategy TrailingExitZScoreBreakout --fixed-sl 2   # permutation mode, no live-node dependency
Writes to cache/research/trading_universe.db, version='v6-w<start>_<end>' (yahoo) or
'v6-massive-w<start>_<end>' (--data-source massive).
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
    _trail_pcts_for_strategy,
)
from run_ground_truth_neighborhood import load_live_node
import campaign_config

DEFAULT_START, DEFAULT_END = "2024-08-21", "2026-08-20"

WINDOWS = [10, 20]
Z_THRESHOLDS = [1.0, 1.5, 2.0]
HOLD_TIME_CAPS = [7, 14, 21, 28, 35, 42, 49, 56, 63, 70, 77, 84, 91, 98, 105, 112, 119, 126, 133, 140]
ENTRY_TIMING = "open_check"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--strategy", choices=sorted(campaign_config.STRATEGIES),
                     help="explicit strategy, used INSTEAD of load_live_node(TICKER)'s "
                          "strategy (permutation/candidate-discovery mode). Must be paired "
                          "with --fixed-sl. Omit both to keep the original live-node-derived "
                          "behavior.")
    ap.add_argument("--fixed-sl", dest="fixed_sl", type=int,
                     help="explicit fixed_sl, used INSTEAD of load_live_node(TICKER)'s "
                          "fixed_sl. Must be paired with --strategy. Integer only (paired "
                          "review, 2026-08-22): backtest_cache's PK stores "
                          "int(round(fixed_sl)) while the cache-key/campaign-scope lookups "
                          "use the raw value -- a fractional fixed_sl (e.g. 1.5) would get a "
                          "distinct cache key but collide on PK with fixed_sl=2, so "
                          "INSERT OR REPLACE would silently clobber another campaign's rows.")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=DEFAULT_END)
    ap.add_argument("--max-phase", dest="max_phase", default="2.5", choices=["1", "2", "2.5"],
                     help="Run phases up through this one, then stop (default: 2.5, full "
                          "Phase1-Coarse-GT -> Phase2-Island-GT -> Phase2.5-CliffBox-GT chain). "
                          "'1' stops after the coarse grid; '2' stops after the island mesh.")
    ap.add_argument("--skip-cache-refresh", action="store_true",
                     help="skip rebuild_indexes() -- pass this on EVERY ticker in a "
                          "multi-ticker loop (see scripts/run_ground_truth_sweep_queue.sh), "
                          "then call rebuild_indexes() once separately after the whole "
                          "batch finishes, matching run_sweep_queue.sh's convention.")
    ap.add_argument("--data-source", choices=["yahoo", "massive"], default="yahoo",
                     help="hourly+minute data source (default: yahoo, unchanged behavior). "
                          "'massive' reads db_cache.get_massive_hourly_ohlcv/"
                          "get_massive_minute_ohlcv (dividend-adjusted, back to ~2021-08-23 "
                          "vs yahoo's ~2023-07-24 floor) -- fully wired through Phase1/2/2.5 "
                          "as of 2026-08-22, no --max-phase restriction.")
    args = ap.parse_args()
    TICKER = args.ticker
    START, END = args.start, args.end
    max_phase = args.max_phase
    data_source = args.data_source
    # Both blockers below (max_phase restriction + minute/hourly adjustment-basis
    # mismatch warning) were real as of the original --data-source wiring diff, but
    # both are now resolved: run_phase2_island_ground_truth/run_phase25_cliff_box_
    # ground_truth accept and thread data_source through to dispatch_parallel_grid_
    # ground_truth (2026-08-22), and _load_minute_df reads db_cache.get_massive_
    # minute_ohlcv (dividend-adjusted, consistent with the hourly leg) under
    # data_source='massive' (fixed 2026-08-22, verified via a real 24/24 byte-identical
    # 5yr comparison across all 12 real live tickers -- see scripts/compare_all_live_
    # 5yr_massive.py). No remaining restriction on --max-phase for massive runs.

    if (args.strategy is None) != (args.fixed_sl is None):
        raise SystemExit(
            "--strategy and --fixed-sl must be passed together (permutation mode) or not "
            "at all (live-node-derived mode) -- got only one of the two."
        )
    if args.strategy is not None:
        strategy_name = args.strategy
        fixed_sl = args.fixed_sl
        print(f"Permutation mode: strategy={strategy_name}, fixed_sl={fixed_sl} (no live-node lookup)")
    else:
        n = load_live_node(TICKER)
        print(f"Live node: {n}")
        strategy_name = n["strategy"]
        fixed_sl = n["fixed_sl"]
    if strategy_name not in campaign_config.STRATEGIES:
        raise SystemExit(
            f"{TICKER}'s resolved strategy is {strategy_name!r} (from "
            f"{'--strategy' if args.strategy is not None else 'load_live_node()'}), which "
            f"has no grid entry in campaign_config.STRATEGIES "
            f"({sorted(campaign_config.STRATEGIES)}). This script only supports strategies "
            f"with a defined grid there."
        )
    grid = campaign_config.STRATEGIES[strategy_name]
    # Per-strategy axis remapping (paired review, 2026-08-22): campaign_config.STRATEGIES'
    # "stop_losses" key holds a DIFFERENT physical grid depending on strategy --
    # TrailingBothZScoreBreakout's is the COMBINED 1-30 grid (-> trail_buy_pct column, with
    # a real swept 4th trail_sell_pct axis); TrailingExitZScoreBreakout's is actually the
    # 1-7 TRAIL_PCTS grid (-> trail_pct column, no real 4th axis). Never assume COMBINED for
    # both -- see strategies.resolve_axis_columns, the single source of truth this reads.
    TAKE_PROFITS = grid["take_profits"]
    STOP_LOSSES = grid["stop_losses"]
    # TRAIL_PCTS is only a real, swept 4th axis for strategies where
    # strategies.resolve_axis_columns(strategy_name)'s fourth_axis == 'trail_pct'
    # (TrailingBothZScoreBreakout today) -- _trail_pcts_for_strategy below re-derives this
    # itself. For everything else (e.g.
    # TrailingExitZScoreBreakout, whose 4th axis collapses entirely -- its own trail_pct
    # concept already lives in the sl_axis/STOP_LOSSES grid above), looping over the full
    # TRAIL_PCTS list here would submit real duplicate-cell tasks that only differ in an
    # ignored column, and produce zero cache hits on re-run (cache key uses the real 0.0
    # tpct, tasks would carry 1.0-7.0). run_optimization_sweep._trail_pcts_for_strategy is
    # the same helper Phase2/2.5-GT already use for this -- reused here for task generation
    # too, not just neighbor-selection, so Phase1's own tasks/hp match that logic exactly.
    TRAIL_PCTS = _trail_pcts_for_strategy(strategy_name, grid)

    init_idempotent_db()
    if args.skip_cache_refresh:
        print("Skipping rebuild_indexes() (--skip-cache-refresh).")
    else:
        rebuild_indexes()
    # '-massive' marker (paired review, 2026-08-22): dispatch_parallel_grid_ground_truth
    # hard-requires this in config_version whenever data_source='massive', so a massive run
    # can never collide with/overwrite yahoo-sourced rows sharing the same window suffix.
    # '-massive' marker must come BEFORE the window suffix, not after --
    # dispatch_parallel_grid_ground_truth's own guard requires config_version to END
    # WITH window_version_suffix(...) exactly (real, confirmed-by-review bug: appending
    # '-massive' after the suffix broke that check and made every massive dispatch call
    # raise ValueError immediately, before writing a single row).
    version = "v6" + ("-massive" if data_source == "massive" else "") + window_version_suffix(START, END)
    run_timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    tasks = [(int(tp), int(sl), int(hold), int(w), float(z), float(tpct))
             for z in Z_THRESHOLDS
             for w in WINDOWS
             for tp in TAKE_PROFITS
             for sl in STOP_LOSSES
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
    # lists (via their own _trail_pcts_for_strategy(strategy_name, hp) call) and compare
    # against real backtest_cache rows. hp['trail_pcts'] is always the raw
    # campaign_config.STRATEGIES[...]["trail_pcts"] value (not the possibly-collapsed
    # [0.0] TRAIL_PCTS used for task generation above) -- callers re-derive whether it's a
    # real axis themselves via resolve_axis_columns, same as this script does.
    hp = {
        "windows": WINDOWS,
        "z_score_thresholds": Z_THRESHOLDS,
        "take_profits": TAKE_PROFITS,
        "stop_losses": STOP_LOSSES,
        "hold_time_caps": HOLD_TIME_CAPS,
        "trail_pcts": grid["trail_pcts"],
    }

    t0 = time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        dispatch_parallel_grid_ground_truth(
            pool, tasks, TICKER, strategy_name, version, "Phase1-Coarse-GT",
            spy_bh, asset_bh, run_timestamp, fixed_sl=fixed_sl, entry_timing=ENTRY_TIMING,
            same_bar_reentry=True, start_date=START, end_date=END, data_source=data_source,
        )
        print(f"Phase1-coarse-GT done in {(time.time()-t0)/3600:.2f}h")

        if max_phase == "1":
            print("--max-phase 1: stopping after Phase1-Coarse-GT.")
            return

        t1 = time.time()
        run_phase2_island_ground_truth(
            pool, TICKER, strategy_name, version, hp, spy_bh, asset_bh, run_timestamp,
            fixed_sl=fixed_sl, entry_timing=ENTRY_TIMING, same_bar_reentry=True,
            start_date=START, end_date=END, data_source=data_source,
        )
        print(f"Phase2-Island-GT done in {(time.time()-t1)/3600:.2f}h")

        if max_phase == "2":
            print("--max-phase 2: stopping after Phase2-Island-GT.")
            return

        t2 = time.time()
        run_phase25_cliff_box_ground_truth(
            pool, TICKER, strategy_name, version, hp, spy_bh, asset_bh, run_timestamp,
            fixed_sl=fixed_sl, entry_timing=ENTRY_TIMING, same_bar_reentry=True,
            start_date=START, end_date=END, data_source=data_source,
        )
        print(f"Phase2.5-CliffBox-GT done in {(time.time()-t2)/3600:.2f}h")


if __name__ == "__main__":
    main()
