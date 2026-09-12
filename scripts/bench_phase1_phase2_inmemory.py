"""Benchmark: Phase1-coarse + Phase2-island run entirely in-process memory, zero DB writes,
for schema-v2 design discussion (docs/plans/backtest_schema_v2_phase_tables.md, "variant B").

Compares against today's real pipeline (Phase1 writes to backtest_cache/backtest_phase1,
Phase2 reads it back via SQL) by doing the exact same compute (same worker function,
run_single_backtest_node_ground_truth_isolated) but keeping Phase1's results as an
in-memory DataFrame and feeding that directly into island-center detection -- no SQL
write, no SQL read, for either phase. Writes NOTHING to any table; pure timing.

Scope: FULL real production Phase1-coarse grid (all windows x all z x full real
take_profit/stop_loss/hold/trail_pct axes, matching campaign_config.py + the real
WINDOWS/Z_THRESHOLDS used by scripts/run_ground_truth_phase1.py) at SOXL's real
live-node strategy/fixed_sl -- one full (ticker, strategy, fixed_sl) scope, same
size as one real production campaign slice, not a scaled-down sample. Real data
(data_source='massive'), same window already used for the schema-v2
backtest_phase1 test script, so results are directly comparable.

Uses ONE shared ProcessPoolExecutor across BOTH phases (2026-08-27 fix -- the first
version of this script opened a separate pool per phase, paying worker-spawn +
numba-JIT-warmup cost twice and understating in-memory throughput on a small sample).

Usage: .venv/bin/python scripts/bench_phase1_phase2_inmemory.py [--workers 8]
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor, FIRST_COMPLETED, as_completed, wait
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import pandas as pd
from tqdm import tqdm

from run_optimization_sweep import (
    compute_bh_returns, window_version_suffix, run_single_backtest_node_ground_truth_isolated,
    _trail_pcts_for_strategy, pick_island_centers, FINE_RADIUS, N_ISLANDS,
    CLIFF_RADIUS, PHASE25_ISLAND_CLIFFBOX_CAGR_MIN, GT_CANDIDATE_TIEBREAK,
    DB_PATH, _load_node_inputs_ground_truth, _load_minute_df, _load_second_df,
    _load_hourly_df_ground_truth,
)
from run_ground_truth_neighborhood import load_live_node
from backtester import run_backtest_ground_truth
from node_key import node_key, build_params_dict, GT_TRADES_KERNEL_VERSION
import campaign_config
import strategies
import db_cache

TICKER = "SOXL"
START, END = "2021-08-23", "2026-08-21"
DATA_SOURCE = "massive"

WINDOWS = [10, 20]
Z_THRESHOLDS = [1.0, 1.5, 2.0]
HOLD_TIME_CAPS = [7, 14, 21, 28, 35, 42, 49, 56, 63, 70, 77, 84, 91, 98, 105, 112, 119, 126, 133, 140]
ENTRY_TIMING = "open_check"

# N_GENERATIONS (2026-08-31, planner dispatch): legacy's multi-generation Phase2-island
# loop (config.json execution.max_generations, default 3 -- see run_optimization_sweep.py's
# run_phase2_island/run_phase2_island_ground_truth dispatch loop around line 4476) was
# never wired into this in-memory pipeline -- it only ever ran ONE Phase2-island pass
# (docs/plans/ground_truth_kernel_rebuild.md:290-295). Matches legacy's default exactly.
# No CLI override -- N_ISLANDS/WINDOWS/Z_THRESHOLDS all have one because they change the
# real scope being swept; this only changes how many extra island-reseeding passes run
# over the SAME scope, and a stuck/no-op generation is already free (see the loop itself).
N_GENERATIONS = 3

# Pipeline-version discriminator (2026-08-30, planner dispatch, item 3): the version
# string was previously derived PURELY from sweep parameters (z-thresholds, n-islands,
# date window) -- it had no component reflecting the PROMOTION ALGORITHM itself. A re-run
# of the exact same tickers/parameters after a real promotion-logic change (e.g. tonight's
# window/z backfill, SAFE/SAFE-gate persistence, Phase4 scope-detection fix) would
# otherwise silently produce the IDENTICAL version string as a prior, algorithmically
# different run -- making the two campaigns indistinguishable in candidate_nodes/
# sweep_run_log. Bump this integer whenever the promotion/backfill/gating logic changes
# materially (not for a sweep-parameter change -- those already get their own -z/-isl/
# -seed suffix). Starts at 2 (not 1) since tonight's window/z backfill two-stage fix +
# Phase1-insurance backfill + SAFE/SAFE-gate persistence + Phase4 scope-detection fix are
# collectively the first material promotion-algorithm change since this pipeline's
# original, undocumented-as-"v1" behavior. MUST stay in sync with run_inmemory_sweep_
# queue.sh's own PROMOTION_ALGO_VERSION shell variable -- same manual-sync convention the
# existing z/isl/seed suffixes already rely on (no shared single source of truth between
# the shell script and this module).
# Bumped 2 -> 3 (2026-08-30, paired-review HIGH finding, independent-cold review of the
# arm_pct backfill diff): the new arm_pct (take_profit-axis) backfill + the N_ISLANDS
# 10->3 revert are both material promotion-algorithm changes per this constant's own
# contract above -- confirmed as a REAL collision, not hypothetical: a --seed-watch-
# list-id 19 smoke test run under the old pv2 code and a second run under the new
# arm-backfill code both landed under the identical 'bench-inmemory-...-seed19-pv2'
# version string in the real research DB before this bump.
# Bumped 3 -> 4 (2026-08-31, planner dispatch, paired-review HIGH finding, both
# independent-cold AND contextual review converged on this independently): the new
# N_GENERATIONS multi-generation Phase2-island loop changes WHICH cells get explored
# and therefore which nodes get promoted -- at least as material as the arm_pct backfill
# that forced the pv2->pv3 bump. Confirmed as a REAL collision, not hypothetical: 3,231
# candidate_nodes rows + 55 finished sweep_run_log rows already exist under
# '...-isl3-pv3' from the single-pass code, and this session's own seed19 smoke test
# landed under the IDENTICAL '...-seed19-pv3' string as a pre-change run -- without this
# bump, run_one_fixed_sl's own dedup gate (keyed on ticker/strategy/fixed_sl/windows/
# version) would silently skip re-running any of those 55 already-"done" scopes under
# the new multi-generation code, so the new loop would never actually execute for them.
PROMOTION_ALGO_VERSION = 4

# Campaign name-reservation tag (2026-08-31, Task #8 -- was a bare "v6.5-" string literal
# inline in _build_version_string before this). Was reserved 2026-08-31 for the
# PROMOTION_ALGO_VERSION=3 resweep campaign in docs/plans/ground_truth_kernel_rebuild.md;
# stays "v6.5" for now even though PROMOTION_ALGO_VERSION has since moved to 4 -- the label
# names the CAMPAIGN (a resweep effort), not the algorithm version, which already has its
# own `-pv{N}` suffix. Bump this (and reserve the new name in that doc first) only when
# starting a genuinely new-named campaign, not on every PROMOTION_ALGO_VERSION change.
CAMPAIGN_LABEL = "v6.5"

# SECOND_RESOLUTION_MAX_CONCURRENT / _SECOND_RES_POOL (2026-09-06, paired-review MEDIUM
# finding, real empirical test): a naive in-flight-task throttle on the SHARED 8-worker
# pool bounds concurrent LOADS but not the total number of DISTINCT worker processes that
# ever end up holding a ticker's full second-resolution df in their per-process
# _SECOND_DF_CACHE over the life of a dispatch call -- confirmed empirically (2026-09-06):
# throttling in-flight submissions to 3 on the shared 8-worker pool still let 5+ distinct
# worker processes accumulate ~2.3GB each (the executor round-robins submissions across
# whichever of the 8 workers is idle, not a fixed subset), pushing this box's used memory
# to 11-12GB out of 15GB. Real fix: route ALL fill_resolution='second' work through a
# SEPARATE, small, persistent ProcessPoolExecutor -- at most SECOND_RESOLUTION_MAX_CONCURRENT
# processes EVER exist in it, for the whole run, so at most that many processes can ever
# load the data, not just be submitted-to-concurrently. Lazily created once and reused
# across every fill_resolution='second' _dispatch call in this process's lifetime
# (Phase2.5 + its completion pass) so a ticker's second-df, once loaded into one of these
# 3 workers' caches, stays warm for the rest of the run -- shut down implicitly at
# interpreter exit (concurrent.futures' own atexit hook), same as the main pool.
#
# Round-2 validation (2026-09-06, contextual-review-confirmed gap in the original test):
# the first empirical test used a SINGLE (window, z) throughout, so it never exercised
# _NODE_INPUT_CACHE_GT's per-process retention across MULTIPLE distinct (window, z)
# combos (a real ~0.9GB mprep per entry at SOXL scale, cap 6 at the time -- up to ~5.4GB/
# process) or the top-9 winner-trades loop's own in-MAIN-PROCESS second-resolution load
# running concurrently with this pool's 3 workers sitting resident (a 4th large-df
# holder never accounted for in the original 2.7GB x 3 = 8.1GB estimate). Fixed
# run_optimization_sweep.py's _load_node_inputs_ground_truth to cap second-resolution
# _NODE_INPUT_CACHE_GT retention at 2 entries/process (not the shared minute-resolution
# cap of 6), then re-ran a more realistic combined test: warmed this pool across 3
# distinct (window, z) cliffboxes, THEN ran 3 more distinct (window, z) loads in the
# MAIN process while the pool stayed resident -- system-wide used memory peaked at
# ~12Gi/15Gi (never below ~460MB free, no swap thrashing) -- safe with real margin, not
# just the single-(w,z) case.
#
# Round-3 correction (2026-09-06): a real end-to-end seed-mode smoke test (--workers 4,
# SOXL, the FULL pipeline -- Phase1+Phase2+Phase2.5+completion+winner-trades together,
# not an isolated component) was killed by a real low-memory intervention. The isolated
# tests above never combined the MAIN pool (4 workers, alive for the whole run) with this
# second-res pool (3 more) plus this session's own real overhead (~1.5GB across two
# concurrent Claude Code processes on this same box) -- 4+3=7 real OS processes at once
# is the actual worst case, not the second-res pool alone. Lowered to 2 as a direct
# result of this negative finding, not a guess -- re-validate under the SAME full
# end-to-end seed-mode invocation (not just an isolated _dispatch test) before trusting
# any value here again.
#
# Round-4 fix + re-validation (2026-09-11): root cause of the whole 2.3-2.85GB/worker
# problem diagnosed -- this pool (and the main pool) forked its workers BEFORE any
# ticker data was loaded anywhere, so each worker independently loaded (and kept) its
# own full private copy of the second-resolution df on first use; Linux fork's
# copy-on-write sharing only covers memory that existed in the PARENT before that
# worker was forked, and none did. Real fix (see main()'s preload-before-fork block,
# same pattern scripts/phase5_second_level_overlay_check.py already used for its own
# _DFH/_DF_1M/_DF_1S globals): main() now calls _load_hourly_df_ground_truth/
# _load_minute_df/_load_second_df for TICKER once, in the main process, before EITHER
# pool is created -- every worker forked afterward inherits the already-loaded frames
# via COW for free. Confirmed output-invariant first (pre-fix vs post-fix, same real
# AGQ/TrailingBoth node run through the actual kernel path: 147 trades, CAGR
# 15.576425954261165%, every payload field byte-identical).
#
# Also box RAM was upgraded 15Gi -> 23Gi since the 2026-09-06 incident above (+8GB,
# user-confirmed) -- a second, independent reason real margin is now larger than the
# numbers in Round-1/2/3 above assumed.
#
# Real re-validation (2026-09-11, under load harder than a normal solo run): ran the
# full end-to-end pipeline (--workers 4, AGQ, single window/z, Phase1+Phase2+Phase2.5+
# completion+winner-trades) WHILE a real live 8-worker v6.5.2 campaign job (its own
# still-uncapped-at-the-time second-res pool at 2 workers, running the OLD pre-fix
# code already resident in that process's memory) was also running -- a harsher
# combined-load test than this fix will ever see in normal solo operation. Measured via
# `ps`/`free` sampled every 15s for the whole run: this run's own second-res pool
# workers peaked at ~670-707MB RSS each (vs. the historical ~2.85GB/worker baseline --
# confirmed from the OTHER, still-unfixed job's second-res workers in the SAME sample
# set, which measured ~2.9GB each, matching the original incident numbers almost
# exactly) -- a ~75% per-worker reduction, consistent with COW-sharing actually working
# (residual ~700MB/worker, not near-zero, is real per-process interpreter/pandas/prep-
# cache overhead, not unshared ticker data). System-wide used memory peaked at ~12Gi/
# 23Gi (never below ~10Gi available) across the ENTIRE combined run (this fix's full
# pipeline run stacked on top of the live job's own 8+2=10 resident processes) -- real
# margin, not a near-miss. rc=0, no low-memory intervention.
#
# Round-5 (2026-09-11, same day, user follow-up): with the preload-before-fork fix
# above, a second-res worker's real marginal cost collapsed to roughly the MAIN pool's
# own per-worker footprint (~0.37-0.7GB observed, not ~2.85GB) -- the entire reason this
# pool needed an independent cap below --workers is gone. Removed the standalone cap:
# SECOND_RESOLUTION_MAX_CONCURRENT now defaults to None, meaning this pool matches
# whatever size the caller (the main pool, sized from --workers) already is -- the user
# controls concurrency once, for both pools, the same way, instead of a second
# independent knob nobody remembers exists. An explicit int override is still supported
# (`min(SECOND_RESOLUTION_MAX_CONCURRENT, max_workers)`) in case a manual cap is ever
# needed again -- e.g. for a box smaller than the one this was validated on. The
# existing `workers_budget`/`campaign_registry.get_workers_budget` dynamic throttle in
# `run_optimization_sweep._dispatch` (reads `dispatch_pool._max_workers` generically,
# not this constant) already applies uniformly to whichever pool is active regardless
# of this pool's OWN size at creation time -- unaffected by this change.
#
# Re-validated at the higher worker count this change actually enables (2026-09-11,
# --workers 8, AGQ, same full end-to-end method as Round-4, run concurrently with the
# SAME live 8-worker v6.5.2 campaign job (its own second-res pool still on the old
# pre-fix code, cap 2, ~2.8-2.85GB/worker -- reconfirmed AGAIN in this run's own `ps`
# samples) that Round-4 ran against): this run's 8 second-res workers peaked at
# ~669.6-669.7MB RSS EACH -- i.e. going from 2 workers (Round-4) to 8 workers (this
# round) cost about the SAME total second-res-pool memory (~5.4GB at 8x669MB vs.
# ~5.7GB at the OLD 2x2.85GB cap) because per-worker cost kept dropping as more workers
# shared the one COW-inherited copy. Main pool's own 8 workers peaked at
# ~471-473MB each (small increase over Round-4's 4-worker ~491MB, expected -- more
# workers means more distinct (window,z)-adjacent state resident at once, not a
# regression). System-wide used memory peaked at 13Gi/23Gi across the whole combined
# run (this run's own 8+8=16 workers stacked on the live job's own 8+2=10 -- 26 real OS
# processes touching this ticker's data at once, the hardest combined-load case tested
# so far) -- real RSS-sum-vs-actual-used gap here (naive sum of every process's own RSS
# would suggest far more than 13Gi) is itself further confirmation the fix's COW-sharing
# is real: RSS is charged per-process even for pages physically shared read-only across
# sibling forks, so actual physical usage tracks well below the naive per-process sum.
# rc=0, no low-memory intervention. Safe to leave SECOND_RESOLUTION_MAX_CONCURRENT
# uncapped (None) at this box's current 23Gi -- re-validate again on a smaller box or a
# meaningfully wider grid before trusting this at, say, --workers 16+.
SECOND_RESOLUTION_MAX_CONCURRENT = None
_SECOND_RES_POOL = None


def _get_second_resolution_pool(max_workers, ticker=None):
    """ticker (2026-09-11, real regression fix -- same underlying per-worker cache/state
    growth risk scripts/candidate_summary_report.py's run_gt_mode had, see
    db_cache.resolve_effective_gt_workers's own docstring for the full mechanism and the
    calibration data behind it): SECOND_RESOLUTION_MAX_CONCURRENT was set to None earlier
    tonight (removing the old hardcoded cap of 2, "follows --workers"), which reopened
    this pool to the exact same SOXL-scale sustained-load risk -- a fresh SOXL run
    exercises this exact pool. This pool is a module-level singleton created ONCE per
    process (first call wins, `size` is ignored on every later call) -- correct for the
    real one-ticker-per-process campaign model this file's callers actually use, but
    means `ticker` here must be the real ticker this process is running, not an arbitrary
    caller's own scope ticker. Only applies the cap when `ticker` is given (a caller that
    can't supply one falls back to the pre-fix, uncapped-by-row-count behavior)."""
    global _SECOND_RES_POOL
    if _SECOND_RES_POOL is None:
        size = (max_workers if SECOND_RESOLUTION_MAX_CONCURRENT is None
                 else min(SECOND_RESOLUTION_MAX_CONCURRENT, max_workers))
        if ticker is not None:
            size = db_cache.resolve_effective_gt_workers([ticker], size)
        _SECOND_RES_POOL = ProcessPoolExecutor(max_workers=size)
    return _SECOND_RES_POOL


def _dispatch(pool, tasks, ticker, strategy_name, version, fixed_sl, spy_bh, desc="dispatch",
              fill_resolution="minute"):
    """Same worker call the real pipeline uses -- returns list of result dicts, in memory only.

    fill_resolution='minute' (default, unchanged): every call site except Phase2.5's
    cliffbox dispatch. fill_resolution='second' (2026-09-06, one-shot-per-ticker design
    doc item 1): appends a 17th element to the worker's args tuple, requesting real
    1-second fill simulation (run_single_backtest_node_ground_truth_isolated falls back
    to minute resolution with a printed warning per-ticker if no massive_second_derived
    build is active) -- see run_optimization_sweep._load_node_inputs_ground_truth's own
    docstring for the full rationale. Deliberately NOT threaded through Phase1-coarse/
    Phase2-island's own _dispatch calls (a full coarse grid can be 100k+ cells --
    1s-resolution there would multiply real compute cost for no established benefit;
    the resolution-sensitivity PoC only tested a candidate's own cliffbox neighborhood,
    see docs/research_log.md's 2026-09-06 entries).

    workers_budget (2026-08-31, Task #8 follow-up -- real dynamic CPU control, folded in
    while the Task #8 commit was already on hold, per the user's own ask, superseding that
    task's original judgment call #2 which said only inter-job budget control would be
    built) is read ONCE per _dispatch call via campaign_registry.get_workers_budget(version)
    -- NOT polled mid-call. `pool` is still the ONE shared ProcessPoolExecutor created at a
    fixed size (main()'s ProcessPoolExecutor(max_workers=args.workers)); this only affects
    how many of its workers THIS call gives work to.

    If the budget is >= the pool's own max_workers (the common/default case -- the shell
    passes --workers-budget equal to --workers), this takes the ORIGINAL submit-everything-
    upfront path with zero throttling overhead. A real ProcessPoolExecutor benchmark (paired
    review, 2026-08-31) measured bounding submission at exactly max_workers as a 1.2-1.5x
    THROUGHPUT REGRESSION versus submit-all -- removing the executor's own internal call
    queue means a worker can idle between the parent waking from wait(), deserializing a
    result, and resubmitting. Only genuinely throttling (budget < max_workers) pays the
    bounded-submission cost, which is the actual point of asking for it.

    A single bench_phase1_phase2_inmemory.py process calls _dispatch MANY times (once per
    Phase1-coarse, once per Phase2-island generation x N_GENERATIONS, once per Phase2.5,
    per fixed_sl, per strategy) -- checking once per call, not mid-call, still gives real
    responsiveness to `campaign_registry.py set-workers-budget` on the timescale of the next
    dispatch (seconds to a few minutes typically), without the cost/complexity/DB-query-
    reliability risk of polling from inside a single 400K-cell dispatch loop (confirmed
    live, 2026-08-31: DFEN's own Phase1-coarse pass). A campaign with no workers_budget set
    (None), a non-positive value, or a DB read failure (a real risk -- this same DB is
    concurrently written by the sweep's own sweep_run_log/candidate_nodes writes plus any
    enqueue/claim-next/mark-finished/status call; get_workers_budget itself fails toward
    None on any exception) is all treated as unthrottled -- fails toward the ORIGINAL
    submit-everything-upfront behavior, never toward a silent hang or a crashed process."""
    from scripts import campaign_registry
    tasks = list(tasks)
    # dispatch_pool (2026-09-06, paired-review MEDIUM finding -- see _SECOND_RES_POOL's
    # module-level docstring for the real empirical measurement that ruled out an
    # in-flight-submission throttle on the SHARED pool as insufficient): fill_resolution=
    # 'second' work is routed to a dedicated, small, persistent pool instead of `pool` --
    # every other reference in this function to "the pool" below means dispatch_pool, not
    # the caller's shared one, so the throttle math (budget vs. max_workers) is scoped to
    # whichever pool is actually doing the work.
    dispatch_pool = (_get_second_resolution_pool(pool._max_workers, ticker=ticker)
                      if fill_resolution == "second" else pool)
    max_workers = dispatch_pool._max_workers

    b = campaign_registry.get_workers_budget(version)
    budget = max_workers if not isinstance(b, int) or b <= 0 else max(1, min(b, max_workers))

    rows = []
    fail_counts = {}

    def _record(task, future_or_none, res_or_exc):
        tp, sl, hold_hours, w, z_thresh, tpct = task
        if isinstance(res_or_exc, Exception):
            # Live error-rate visibility (2026-09-11, backlog item -- a suspiciously fast
            # stage completion was the only clue tonight to a majority of AGQ fixed_sl=3/4's
            # Phase2.5-cliffbox cells failing with status='ERROR' under memory pressure (the
            # 1s load raises, _record files it into fail_counts and emits no df25 row, so the
            # coordinate's cliff-safety neighbor falls through to the Phase1/Phase2 minute
            # row -- NOT a per-cell SUCCESS-with-downgraded-fill_resolution='minute', a
            # different, still-uninstrumented case caught downstream by the
            # worst_neighbor_cagr resolution check instead; comment corrected 2026-09-11,
            # paired-review MEDIUM finding, after an earlier version conflated the two).
            # fail_counts was already tracked here but only ever printed once, at the very
            # end. `progress` is resolved via closure -- _record is only ever called from
            # inside the loops below, after `progress` is assigned, in both the unthrottled
            # (as_completed) and throttled (bounded-submission) branches, so one copy of this
            # call covers both shapes.
            #
            # refresh=(key not in fail_counts BEFORE the increment) (2026-09-11, paired-
            # review HIGH finding, confirmed on rebuttal by both an independent-cold and a
            # contextual Opus review, verified directly against the real incident log): a
            # plain refresh=False is respectful of this bar's own mininterval=15.0 throttle
            # but is SELF-DEFEATING for exactly the mass-failure case this exists to catch --
            # an erroring cell short-circuits before simulation and returns ~250x faster than
            # a real one, so a stage failing at scale finishes in seconds, well under one
            # mininterval window, and the deferred postfix never gets a chance to render
            # before close() -- confirmed against logs/inmemory_sweep_queue_20260910_091832.log,
            # where the real AGQ fixed_sl=3 stage (1,771 ERRORs) ran end-to-end in 10.0s with
            # exactly two renders (0% and close()'s 100%), so a refresh=False postfix would
            # have landed at the SAME instant as the pre-existing end-of-stage print -- zero
            # net new visibility. Forcing a refresh only on each distinct status key's first
            # occurrence bounds forced redraws to the handful of real status values a worker
            # can return (well under 10), while still surfacing the very first failure within
            # milliseconds instead of only at stage completion.
            first = "CRASH" not in fail_counts
            fail_counts["CRASH"] = fail_counts.get("CRASH", 0) + 1
            progress.set_postfix(fail_counts, refresh=first)
            return
        status = str(res_or_exc.get("status"))
        if status != "SUCCESS":
            first = status not in fail_counts
            fail_counts[status] = fail_counts.get(status, 0) + 1
            progress.set_postfix(fail_counts, refresh=first)
            return
        alpha, num_trades, wr, comp_ret, wtw, node_cagr = res_or_exc["payload"]
        rows.append({
            "take_profit": int(tp), "stop_loss": int(sl), "max_hold_hours": hold_hours,
            "window": w, "z_score_threshold": z_thresh, "trail_sell_pct": tpct,
            "trades": num_trades, "win_rate": wr, "strategy_return": comp_ret,
            "alpha_vs_spy": alpha, "cagr": node_cagr,
            # Real per-cell ground truth (2026-09-06, paired-review HIGH finding), not
            # this call's fill_resolution request -- see run_single_backtest_node_
            # ground_truth_isolated's own comment on why the two can legitimately
            # differ. Falls back to the requested value only for a worker built before
            # this key existed (defensive, not expected to ever trigger in this process).
            "resolution": res_or_exc.get("fill_resolution", fill_resolution),
        })

    if budget >= max_workers:
        # Unthrottled -- original behavior, submit everything upfront and drain via
        # as_completed()'s own internal handling (no bounded-submission overhead).
        futures_map = {
            dispatch_pool.submit(run_single_backtest_node_ground_truth_isolated,
                        (ticker, strategy_name, version, int(tp), int(sl), hold, w, spy_bh, z,
                         fixed_sl, tpct, ENTRY_TIMING, True, START, END, DATA_SOURCE,
                         fill_resolution)): task
            for task in tasks
            for tp, sl, hold, w, z, tpct in [task]
        }
        progress = tqdm(as_completed(futures_map), total=len(futures_map), desc=desc,
                         unit="node", mininterval=15.0, maxinterval=30.0)
        for future in progress:
            task = futures_map[future]
            try:
                res = future.result()
            except Exception as e:
                _record(task, future, e)
                continue
            _record(task, future, res)
    else:
        # Genuinely throttled -- bounded submission, at most `budget` tasks in flight.
        task_iter = iter(tasks)
        in_flight = {}  # future -> task

        def _submit_next():
            try:
                task = next(task_iter)
            except StopIteration:
                return False
            tp, sl, hold, w, z, tpct = task
            future = dispatch_pool.submit(run_single_backtest_node_ground_truth_isolated,
                                  (ticker, strategy_name, version, int(tp), int(sl), hold, w,
                                   spy_bh, z, fixed_sl, tpct, ENTRY_TIMING, True, START, END,
                                   DATA_SOURCE, fill_resolution))
            in_flight[future] = task
            return True

        progress = tqdm(total=len(tasks), desc=desc, unit="node", mininterval=15.0, maxinterval=30.0)
        while len(in_flight) < budget and _submit_next():
            pass
        while in_flight:
            done, _pending = wait(in_flight.keys(), return_when=FIRST_COMPLETED)
            for future in done:
                task = in_flight.pop(future)
                progress.update(1)
                try:
                    res = future.result()
                except Exception as e:
                    _record(task, future, e)
                    continue
                _record(task, future, res)
            while len(in_flight) < budget and _submit_next():
                pass

    progress.close()
    if fail_counts:
        # Proportional, scrollback-durable severity signal (2026-09-11, paired-review HIGH
        # finding -- contextual Opus review, against the real incident log): the plain
        # {fail_counts} dict WAS already printed here the night of the real AGQ incident
        # (confirmed: logs/inmemory_sweep_queue_20260910_091832.log shows "non-SUCCESS
        # statuses: {'ERROR': 5183}" for a 6,920-cell stage) and was STILL missed -- the
        # gap that actually mattered wasn't visibility timing, it was that a 75%-failure
        # stage read no differently on the page than a handful of expected fringe misses.
        # A loud, greppable tag on a high failure fraction is the load-bearing half of this
        # fix; the live progress-bar postfix above is the complementary nice-to-have.
        total_failed = sum(fail_counts.values())
        frac = total_failed / len(tasks) if tasks else 0.0
        tag = "  *** HIGH FAILURE RATE ***" if frac >= 0.25 else ""
        print(f"  non-SUCCESS statuses: {fail_counts} ({total_failed:,}/{len(tasks):,} = {frac:.0%}){tag}")
    return rows


def _dispatch_grouped_by_wz(pool, tasks, ticker, strategy_name, version, fixed_sl, spy_bh,
                             desc="dispatch", fill_resolution="second"):
    """fill_resolution='second' wrapper around `_dispatch` that sub-groups `tasks` by
    (window, z_score_threshold) -- task[3]/task[4], see `cliffbox_tasks_for_cell`'s own
    tuple-shape docstring -- and dispatches ONE (window, z) group fully (submitted to
    `pool`, drained via `_dispatch`'s own as_completed/throttled-submission logic) before
    moving to the next, instead of a single flat call over the tasks' union.

    Why this matters specifically for fill_resolution='second' (2026-09-11, real
    production gap -- this exact fix already existed in scripts/verify_v651_
    cliffsafety_1s_timing.py, commits dc8f78e/a4e1408, 2026-09-07, but was never ported
    into the real Phase2.5-cliffbox call sites below, which is what every actual
    production campaign run uses): `_load_node_inputs_ground_truth`'s second-resolution
    `_NODE_INPUT_CACHE_GT` entries are capped at 2/process (run_optimization_sweep.py,
    deliberately tighter than the minute-resolution cap -- a real memory-safety fix, NOT
    to be loosened here). A flat dispatch over a task set spanning many distinct
    (window, z) pairs (a real production Phase2.5 seed set easily spans 4 windows x 4 z
    = 16 pairs) interleaves cells across all of them in whatever order the pool's shared
    call queue happens to hand them to workers -- once a worker's 2-entry cache holds 2
    DIFFERENT (window, z) mpreps, every subsequent cell for a THIRD (window, z) evicts
    both and forces a full ~0.9GB mprep rebuild (indicator + second-bucketing arrays)
    from scratch, repeated over and over as the interleaving continues. This is
    PER-PROCESS, so more workers means more independent tiny caches thrashing
    independently, not less -- verify_v651's own real measurement: throughput collapsed
    from 65-180 cells/sec (grouped) to 1.35 cells/sec (interleaved) on one real 11-(w,z)
    GDXU group, a 50-130x difference, and the resulting sustained I/O correlated with a
    real low-memory kill. Grouping so every cell for one (window, z) pair dispatches
    before the next pair starts means each worker's cache, once warmed for that pair,
    stays warm for the whole group instead of being evicted every few cells.

    Dispatch ORDER changes only efficiency, never any computed VALUE -- `_dispatch`'s own
    per-cell kernel call takes a fully self-contained args tuple and is unaffected by
    which other cells ran before it; the only cross-task state is the worker-side loader
    caches (`_NODE_INPUT_CACHE_GT`/`_SECOND_DF_CACHE`), which are pure memoization
    (performance only, verified read-only downstream, see run_optimization_sweep.py's
    own preload-fix docstring). Confirmed two ways, not assumed: a pure grouping/
    reassembly unit test (no pool/DB) proves this function's own task-partitioning is
    exact -- every input task lands in exactly one group and every group's rows are
    concatenated back, nothing dropped/duplicated -- and independent-cold + contextual
    Opus review (2026-09-11) both traced `_record`'s row construction and confirmed it
    depends only on the task's own params, never on what else was dispatched
    alongside/before it. Sorts groups by (window, z) purely for deterministic/readable
    logging, matching
    verify_v651's own convention -- correctness doesn't depend on group order, only on
    each group being fully drained before the next one starts.

    Per-group dispatch (rather than one flat call) does NOT weaken `_dispatch`'s own
    2026-09-11 aggregate "HIGH FAILURE RATE" tag: that tag fires per-call when a call's
    own failure fraction reaches 25%. The whole-stage aggregate fraction this replaces
    is a weighted mean of the per-group fractions (weighted by group size) -- if the
    aggregate were >= 25%, at least one group's own fraction must ALSO be >= 25%
    (a weighted mean of values all below a threshold cannot itself reach that
    threshold), so the tag still fires whenever it would have, just attached to the
    specific (window, z) group actually responsible instead of the whole stage --
    strictly more diagnosable, not less.

    Real throughput validation (2026-09-11, --workers 8, AGQ/TrailingBoth fixed_sl=4,
    full real grid window=[5,10,15,20] z=[0.5,1.0,1.5,2.0] -- 16 distinct (window,z)
    pairs, same grid width as a real production campaign -- run under a scratch
    campaign-label so it didn't touch/collide with the live v6.5.2 campaign job running
    concurrently at the time): Phase2.5-cliffbox (7,563 real cells, including window/z
    and arm_pct backfill) completed in 50.2s -- 151 cells/sec. The live campaign job's
    own real, unfixed fixed_sl=2 cliffbox stage that same night measured 6,234 cells in
    3,215.6s -- 1.94 cells/sec. ~78x throughput improvement, consistent with
    verify_v651_cliffsafety_1s_timing.py's own documented 50-130x range for this same
    fix. rc=0, no errors, system memory stayed safe (9.8-11Gi used / 11-13Gi available)
    throughout, run concurrently alongside the live job's own resident processes."""
    tasks_by_wz = {}
    for t in tasks:
        key = (t[3], t[4])
        tasks_by_wz.setdefault(key, set()).add(t)

    rows = []
    for (w, z), wz_tasks in sorted(tasks_by_wz.items()):
        wz_rows = _dispatch(pool, wz_tasks, ticker, strategy_name, version, fixed_sl, spy_bh,
                             desc=f"{desc} w={w} z={z}", fill_resolution=fill_resolution)
        rows.extend(wz_rows)
    return rows


def _insert_phase1_insurance_rows(rows, strategy_name, config_version, ticker, fixed_sl, entry_timing):
    """Dedicated insurance-snapshot table, separate from backtest_cache on purpose
    (2026-08-27, per design discussion) -- this data is a write-once debug aid for
    re-deriving pick_island_centers later, never a queryable result a future sweep
    expects a cache-hit against, so it shouldn't share backtest_cache's real production
    schema/UNIQUE-key space (that's what caused the earlier top-100/top-9 collision).
    Generic axis-agnostic columns (take_profit/stop_loss/trail_sell_pct = raw grid axis
    values, NOT strategy-remapped column meanings) -- no need to pretend to be
    backtest_cache-compatible since nothing else reads this table."""
    with sqlite3.connect(DB_PATH, timeout=60.0) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS backtest_phase1_insurance (
                strategy TEXT, version TEXT, ticker TEXT, fixed_sl REAL, entry_timing TEXT,
                window INTEGER, z_score_threshold REAL, max_hold_hours INTEGER,
                take_profit REAL, stop_loss REAL, trail_sell_pct REAL,
                trades INTEGER, cagr REAL, created_at TEXT,
                UNIQUE(strategy, version, ticker, fixed_sl, entry_timing, window,
                       z_score_threshold, max_hold_hours, take_profit, stop_loss, trail_sell_pct)
            )""")
        before = conn.execute(
            "SELECT COUNT(*) FROM backtest_phase1_insurance WHERE version=? AND ticker=? AND strategy=?",
            (config_version, ticker, strategy_name)).fetchone()[0]
        conn.executemany(
            """INSERT OR IGNORE INTO backtest_phase1_insurance
               (strategy, version, ticker, fixed_sl, entry_timing, window, z_score_threshold,
                max_hold_hours, take_profit, stop_loss, trail_sell_pct, trades, cagr, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(strategy_name, config_version, ticker, fixed_sl, entry_timing, r["window"],
              r["z_score_threshold"], r["max_hold_hours"], r["take_profit"], r["stop_loss"],
              r["trail_sell_pct"], r["trades"], r["cagr"], time.strftime("%Y-%m-%d %H:%M:%S"))
             for r in rows])
        conn.commit()
        after = conn.execute(
            "SELECT COUNT(*) FROM backtest_phase1_insurance WHERE version=? AND ticker=? AND strategy=?",
            (config_version, ticker, strategy_name)).fetchone()[0]
    actually_inserted = after - before
    if actually_inserted < len(rows):
        print(f"  ({len(rows) - actually_inserted} of {len(rows)} rows already existed "
              f"at this coordinate -- skipped via INSERT OR IGNORE, not overwritten)")
    return actually_inserted


def _insert_phase2_insurance_rows(rows, strategy_name, config_version, ticker, fixed_sl, entry_timing):
    """Same purpose/shape as _insert_phase1_insurance_rows above, one phase later:
    Phase2's mesh-generation output (phase2_rows) is already correctly scoped per
    (window, z, trail_pct) -- confirmed, not the pooling bug (see docs/backlog_cache.md's
    "Root cause, corrected/completed same night" entry) -- but it gets discarded the
    moment centers25/final_centers pool the combined result set down to N_ISLANDS global
    regions. backtest_phase1_insurance alone isn't enough to re-debug a lost region after
    the fact (confirmed 2026-09-02/03: only 3 rows existed for HIBL's whole window=10/
    z=1.0/fixed_sl=3 scope) -- this table exists to persist the richer, correctly-scoped
    Phase2 data BEFORE that pooling happens, same "write-once debug aid, not a
    backtest_cache-compatible production table" posture as Phase1's insurance table.
    One extra `generation` column vs Phase1's schema -- Phase2 rows genuinely carry this
    (which Phase2-island generation first computed the cell, 1-indexed), Phase1 rows
    don't have an equivalent concept."""
    with sqlite3.connect(DB_PATH, timeout=60.0) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS backtest_phase2_insurance (
                strategy TEXT, version TEXT, ticker TEXT, fixed_sl REAL, entry_timing TEXT,
                window INTEGER, z_score_threshold REAL, max_hold_hours INTEGER,
                take_profit REAL, stop_loss REAL, trail_sell_pct REAL, generation INTEGER,
                trades INTEGER, cagr REAL, created_at TEXT,
                UNIQUE(strategy, version, ticker, fixed_sl, entry_timing, window,
                       z_score_threshold, max_hold_hours, take_profit, stop_loss, trail_sell_pct)
            )""")
        before = conn.execute(
            "SELECT COUNT(*) FROM backtest_phase2_insurance WHERE version=? AND ticker=? AND strategy=?",
            (config_version, ticker, strategy_name)).fetchone()[0]
        conn.executemany(
            """INSERT OR IGNORE INTO backtest_phase2_insurance
               (strategy, version, ticker, fixed_sl, entry_timing, window, z_score_threshold,
                max_hold_hours, take_profit, stop_loss, trail_sell_pct, generation, trades,
                cagr, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(strategy_name, config_version, ticker, fixed_sl, entry_timing, r["window"],
              r["z_score_threshold"], r["max_hold_hours"], r["take_profit"], r["stop_loss"],
              r["trail_sell_pct"], r.get("generation"), r["trades"], r["cagr"],
              time.strftime("%Y-%m-%d %H:%M:%S"))
             for r in rows])
        conn.commit()
        after = conn.execute(
            "SELECT COUNT(*) FROM backtest_phase2_insurance WHERE version=? AND ticker=? AND strategy=?",
            (config_version, ticker, strategy_name)).fetchone()[0]
    actually_inserted = after - before
    if actually_inserted < len(rows):
        print(f"  ({len(rows) - actually_inserted} of {len(rows)} rows already existed "
              f"at this coordinate -- skipped via INSERT OR IGNORE, not overwritten)")
    return actually_inserted


def _insert_candidate_nodes_rows(candidates, strategy_name, config_version, ticker, fixed_sl, entry_timing):
    """Promotion step -- mirrors scripts/locate_best_node.py's real INSERT INTO
    candidate_nodes exactly (same key_cols, same UNIQUE constraint, same column
    meanings: arm_pct is the universal tp-axis slot regardless of strategy -- there's
    no separate take_profit column -- trail_buy_pct/trail_sell_pct follow
    strategies.resolve_axis_columns' sl_axis/fourth_axis mapping same as backtest_cache).
    `robust_alpha` stores alpha_vs_spy for a GT row -- ROBUST_ALPHA_SQL's
    MIN(alpha_vs_spy, COALESCE(alpha_vs_spy_pessimistic, alpha_vs_spy), ...) collapses to
    plain alpha_vs_spy when the pessimistic/certain columns are NULL (always true for GT
    rows, which have no possible/pessimistic/certain resolution split)."""
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")

    buffer = []
    for c in candidates:
        arm_pct = float(c["take_profit"])
        if sl_axis_col == 'trail_buy_pct':
            trail_buy_pct = float(c["stop_loss"])
            trail_sell_pct = float(c["trail_sell_pct"]) if fourth_axis_col == 'trail_pct' else 0.0
        elif sl_axis_col == 'trail_pct':
            trail_buy_pct, trail_sell_pct = 0.0, float(c["stop_loss"])
        else:
            trail_buy_pct, trail_sell_pct = 0.0, 0.0
        # params_json (2026-08-29): built from the RAW pre-remap axis values (c["take_profit"]/
        # c["stop_loss"]/c["trail_sell_pct"]) -- same raw inputs node_key() itself takes
        # elsewhere in this file (see the node_key(...) call above in the winner-trades
        # block), NOT the strategy-remapped trail_buy_pct/arm_pct columns just computed
        # above for the candidate_nodes row itself. See feedback_backtest_cache_axis_
        # column_remapping memory -- mixing these two up is a real prior bug class.
        params_json = json.dumps(
            build_params_dict(strategy_name, ticker, fixed_sl, c["window"], c["z_score_threshold"],
                               c["max_hold_hours"], c["take_profit"], c["stop_loss"],
                               c["trail_sell_pct"], entry_timing, strategies.resolve_axis_columns),
            sort_keys=True)
        # selection_source (2026-08-30, planner dispatch, paired-review fix): tags a
        # window/z-backfilled candidate distinctly from one that won its slot via real
        # island selection -- see find_missing_window_z_top_n for the full reasoning.
        # A DEDICATED column, not `comment` (the original plan) -- paired review (both
        # independent-cold and contextual, converging independently) found `comment` is
        # already the human's own free-text review note (locate_best_node.set_pick_
        # comment/candidate_full_review.py render it as such), so reusing it here would
        # (a) show machine text in the column the review report presents as the
        # reviewer's note and (b) get silently overwritten the first time anyone actually
        # comments on a backfilled node. None (-> NULL) for every normal island-selected
        # candidate.
        # backfill_reason (2026-08-30, planner dispatch item 2): distinguishes which axis
        # triggered the backfill -- "window_z" vs "arm_pct" -- rather than one generic
        # tag, so a later report can tell the two evidenced gaps apart. Defaults to
        # "unknown" (2026-08-30, paired-review LOW finding: defaulting to "window_z"
        # would silently MISLABEL a future third backfill mechanism that forgets to set
        # this key as window_z, rather than flagging it) for a backfilled candidate dict
        # that somehow lacks the key -- shouldn't happen, both current backfill call
        # sites always set it, but a missing-key case should announce itself, not guess.
        selection_source = (f"backfill_missing_{c.get('backfill_reason', 'unknown')}"
                             if c.get("backfilled") else None)
        # worst_neighbor_cagr (2026-08-31, planner dispatch): the real scalar the
        # "Cliff-safety verdict per candidate" block above already computes (min(cagr)
        # among cells within +-CLIFF_RADIUS tp/sl, same hold/window/z/trail_pct, from
        # df_final -- the full in-memory Phase1+Phase2+Phase2.5 evidence pool) -- was
        # PRINT-ONLY before this (see that block's own now-stale "not stored... printed
        # here only as the benchmark's proof-of-concept" comment, predating candidate_
        # nodes promotion entirely). Persisted as the RAW REAL value (not a pre-derived
        # boolean) so a reader applies whatever threshold it needs -- candidate_summary_
        # report.py's own pre-Phase4 filter uses the exact same `< 0` bar Phase4's own
        # run_addon_cliff_safety_ground_truth already uses for core_cliff (see that
        # function's own docstring), so the two stay conceptually aligned even though
        # they're two independent computations (real equivalence measured separately,
        # not structurally coupled -- see the pre-filter's own docstring for why).
        # None when no real neighbor existed (c["n_neighbors_checked"] == 0) -- fails
        # CLOSED to "unknown," never silently treated as either safe or unsafe.
        # generation (2026-08-31, planner dispatch): which Phase2-island generation
        # (1-indexed) first computed this candidate's underlying cell -- None for a
        # Phase1-only or Phase2.5-cliffbox-only cell, same convention as legacy's own
        # backtest_cache.generation column (see run_optimization_sweep.py's ALTER TABLE
        # comment, 2026-07-15). `candidate_nodes` never had this column before (33 cols,
        # confirmed via a direct schema query) -- added below via the same ALTER-guard
        # pattern as params_json/selection_source/worst_neighbor_cagr.
        buffer.append((now_iso, ticker, strategy_name, config_version, c["window"],
                       c["z_score_threshold"], float(fixed_sl), arm_pct, trail_buy_pct,
                       trail_sell_pct, c["max_hold_hours"], entry_timing,
                       c["alpha_vs_spy"], c["trades"], now_iso, params_json, selection_source,
                       c["worst_neighbor_cagr"], c.get("generation")))

    with sqlite3.connect(DB_PATH, timeout=60.0) as conn:
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(candidate_nodes)")}
        if 'params_json' not in existing_cols:
            conn.execute("ALTER TABLE candidate_nodes ADD COLUMN params_json TEXT")
        if 'selection_source' not in existing_cols:
            conn.execute("ALTER TABLE candidate_nodes ADD COLUMN selection_source TEXT")
        if 'worst_neighbor_cagr' not in existing_cols:
            conn.execute("ALTER TABLE candidate_nodes ADD COLUMN worst_neighbor_cagr REAL")
        if 'generation' not in existing_cols:
            conn.execute("ALTER TABLE candidate_nodes ADD COLUMN generation INTEGER")
        before = conn.execute(
            "SELECT COUNT(*) FROM candidate_nodes WHERE version=? AND ticker=? AND strategy=?",
            (config_version, ticker, strategy_name)).fetchone()[0]
        conn.executemany("""
            INSERT OR IGNORE INTO candidate_nodes
                (created_at, ticker, strategy, version, window, z, fixed_sl, arm_pct,
                 trail_buy_pct, trail_sell_pct, max_hold_hours, entry_timing,
                 robust_alpha, trades, robust_alpha_computed_at, params_json, selection_source,
                 worst_neighbor_cagr, generation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, buffer)
        conn.commit()
        after = conn.execute(
            "SELECT COUNT(*) FROM candidate_nodes WHERE version=? AND ticker=? AND strategy=?",
            (config_version, ticker, strategy_name)).fetchone()[0]
    actually_inserted = after - before
    if actually_inserted < len(buffer):
        print(f"  ({len(buffer) - actually_inserted} of {len(buffer)} rows already existed "
              f"at this coordinate -- skipped via INSERT OR IGNORE, not overwritten)")
    return actually_inserted


def _check_live_node_regression(df_final, final_candidates, ticker, strategy_name, fixed_sl,
                                  entry_timing):
    """Live-node regression guard (2026-09-03, added per Opus paired-review challenge on
    the window/z island-quota-diversity fix above -- this is "the actual guarantee
    mechanism, not the search-method fix itself"). The diversity fix reduces the RISK of a
    real live node's own config getting silently dropped from final_candidates, but
    doesn't GUARANTEE it -- there could always be more than top_n distinct islands in a
    combo, or an entirely different bug. Real motivating case this whole investigation
    started from: HIBL's arm=28/trail_buy=3 live-promoted node (candidate_nodes id=907)
    had real evidence (cagr=52.40%) but silently never appeared in v6.5's final_candidates
    for that ticker -- nothing printed, nothing flagged, discovered only via a separate
    manual investigation days later.

    Before promotion, checks every CURRENTLY-LIVE watch_list node matching this exact
    (ticker, strategy, fixed_sl, entry_timing) scope against df_final (the full
    Phase1+Phase2+Phase2.5 evidence pool this run computed) and final_candidates (what's
    about to be promoted). Reuses _reverse_map_generic_task -- the SAME mapping seed mode
    uses -- rather than a second, possibly-diverging implementation. Three outcomes per
    live node: (1) its exact coordinate IS in final_candidates -- silent, nothing to flag;
    (2) it has real evidence in df_final but ISN'T in final_candidates -- loud WARNING with
    its real cagr + full config, since that's a live node whose backtest performance this
    campaign run silently disagrees should be a candidate at all; (3) no evidence in
    df_final at all (off-grid, or genuinely never computed this run) -- a quieter notice,
    since this function can't verify a coordinate with zero evidence either way.

    Read-only: never blocks promotion, never mutates final_candidates -- flags a real gap
    for a human to notice and investigate, matching this module's existing "flag loudly,
    don't silently drop" convention (see the WARNING prints throughout this file for
    island-count shortfalls).

    Hardened (2026-09-03, cold-review HIGH finding): this runs right before the ONLY
    candidate_nodes write, after potentially hours of Phase1/2/2.5 compute -- a diagnostic
    guard must never be able to discard a run's real promotion. Wrapped in a blanket
    try/except (a bug in the guard itself must degrade to a loud skip notice, not an
    unhandled exception that loses the whole run's output) and opens the live DB read-only
    via a `file:...?mode=ro` URI (default sqlite3.connect silently CREATES an empty DB file
    if trading_live.db is somehow missing, then fails with a confusing "no such table"
    instead of a clear "file not found or no read access") with the connection explicitly
    closed in a finally block (`with sqlite3.connect(...)` commits on exit but does NOT
    close the connection -- a real, easy-to-miss quirk of that context manager -- this
    function runs once per fixed_sl scope in a campaign, so a real per-scope fd leak on the
    live trading DB otherwise).

    Scope accounting (2026-09-03, contextual-review HIGH finding): a live node whose
    strategy/fixed_sl/entry_timing doesn't match THIS run's scope literally can't be
    checked against this run's own df_final (nonsensical to compare across scopes), but
    silently saying nothing about it is the same silent-absence failure this guard exists
    to prevent -- prints an explicit "N live node(s) for this ticker are outside this run's
    scope, not checked here" notice instead, so "guard ran and found nothing" and "guard
    silently skipped everything" are never confused for one another."""
    try:
        live_db_path = os.path.join(ROOT, "cache", "live", "trading_live.db")
        conn = sqlite3.connect(f"file:{live_db_path}?mode=ro", uri=True, timeout=60.0)
        try:
            conn.row_factory = sqlite3.Row
            all_ticker_rows = conn.execute(
                "SELECT * FROM watch_list WHERE ticker=? AND state='live' AND archived_at IS NULL",
                (ticker,)).fetchall()
        finally:
            conn.close()
    except Exception as e:
        print(f"  LIVE-NODE GUARD: skipped for ticker={ticker} -- could not read "
              f"trading_live.db ({type(e).__name__}: {e}).")
        return
    if not all_ticker_rows:
        return

    in_scope_ids = set()
    in_scope_rows = []
    for r in all_ticker_rows:
        if (r["strategy"] == strategy_name and float(r["fixed_sl"]) == float(fixed_sl)
                and r["entry_timing"] == entry_timing):
            in_scope_ids.add(r["id"])
            in_scope_rows.append(r)
    out_of_scope_ids = [r["id"] for r in all_ticker_rows if r["id"] not in in_scope_ids]
    if out_of_scope_ids:
        print(f"  LIVE-NODE GUARD: {len(out_of_scope_ids)} live node(s) for {ticker} "
              f"(watch_list id(s) {out_of_scope_ids}) are outside this run's scope "
              f"(strategy={strategy_name!r} fixed_sl={fixed_sl} entry_timing={entry_timing!r}) "
              f"-- not checked here, only checkable by a run covering their own scope.")
    if not in_scope_rows:
        return

    try:
        claimed_keys = {
            (int(c["take_profit"]), int(c["stop_loss"]), int(c["max_hold_hours"]), int(c["window"]),
             float(c["z_score_threshold"]), float(c["trail_sell_pct"]))
            for c in final_candidates
        }
        for row in in_scope_rows:
            row = dict(row)
            try:
                task = _reverse_map_generic_task(row, strategy_name)
            except (ValueError, TypeError) as e:
                print(f"  LIVE-NODE GUARD: watch_list id={row['id']} ticker={ticker} -- could not "
                      f"reverse-map for regression check ({e}), skipped.")
                continue
            tp, sl, hold, w, z, tpct = task
            if (tp, sl, hold, w, z, tpct) in claimed_keys:
                continue
            evidence = df_final[
                (df_final["take_profit"] == tp) & (df_final["stop_loss"] == sl)
                & (df_final["max_hold_hours"] == hold) & (df_final["window"] == w)
                & (df_final["z_score_threshold"] == z) & (df_final["trail_sell_pct"] == tpct)
                & df_final["cagr"].notna()
            ]
            if evidence.empty:
                print(f"  LIVE-NODE GUARD: watch_list id={row['id']} ticker={ticker} config "
                      f"(TP={tp} SL={sl} hold={hold}h w={w} z={z} tpct={tpct}) has NO evidence in "
                      f"this run's df_final -- off-grid or never computed, can't verify.")
                continue
            cagr = float(evidence.iloc[0]["cagr"])
            print(f"  LIVE-NODE GUARD WARNING: watch_list id={row['id']} ticker={ticker} live node "
                  f"config (TP={tp} SL={sl} hold={hold}h w={w} z={z} tpct={tpct}, cagr={cagr:.2f}%) "
                  f"has real evidence in df_final but is NOT in final_candidates -- dropped by "
                  f"selection. Investigate before treating this campaign's output as "
                  f"authoritative for this ticker.")
    except Exception as e:
        print(f"  LIVE-NODE GUARD: aborted mid-check for ticker={ticker} "
              f"({type(e).__name__}: {e}) -- not all live nodes for this scope were verified.")


def _clear_prior_seed_mode_table_rows(table_name, strategy_name, config_version, ticker, fixed_sl):
    """Seed mode only (2026-08-29, paired review rounds 4-5): deletes THIS version's
    PRIOR rows in ONE table before a new seed run writes its own output to that same
    table. Needed because the repeatability fix (seed mode always bypasses
    sweep_run_log's dedup skip, and skips the checkpoint entirely) makes every repeat
    invocation of the same --seed-watch-list-id genuinely re-run for real -- but the
    version string is still deterministic (stable "-seed<id>" suffix, not
    timestamped), so without this, repeat runs would UNION their writes under the
    identical version via INSERT OR IGNORE with no way to tell which run produced
    which row, and a row written by an older/buggier code revision would stay there
    indistinguishably forever.

    Deliberately DELETE-and-replace rather than a timestamp-suffixed version per
    invocation: seed mode is a disposable smoke test meant to reflect the CURRENT
    code's behavior against one real node, not an audit trail of every historical
    invocation -- a stable, greppable "-seed<id>" version that always reflects the
    latest run is more useful here than an ever-growing pile of past runs a consumer
    would have to filter by recency to find the real answer. Never called for a
    non-seed run (full-grid campaigns keep their real accumulate-forever semantics).

    Called separately, once per table, immediately before that table's own write site
    (round-5 fix, contextual review) -- NOT all bundled at one call site before
    candidate_nodes' write. backtest_phase1_insurance is written ~230 lines before
    candidate_nodes/backtest_winner_trades in run_one_fixed_sl, and clearing it that
    early left the SAME version-union staleness this whole mechanism was built to
    close, just on a third table (verified real: all 4 existing seed versions each
    had exactly 1 stale backtest_phase1_insurance row from their first run only).
    backtest_winner_trades' clear is similarly deferred to immediately before ITS OWN
    write (rather than bundled with candidate_nodes' clear ~65 lines earlier) --
    there's real per-candidate kernel work (real backtests, need_times=True) between
    the two writes that can raise; clearing early would leave backtest_winner_trades
    at zero rows for this version if that work crashed, while candidate_nodes already
    has the new run's rows -- the "worse than stale" state this feature exists to
    prevent, just moved to a different table/timing.

    Deliberately WHERE version=? ALONE (round-5 REVERTED the fixed_sl/ticker/strategy
    predicates a same-round change had added -- both independent-cold and contextual
    Opus review, round 5, independently converged on this): the "-seed<id>" version
    namespace is written by nothing else, so it's already sufficient to select exactly
    this seed's rows and nothing else. Adding fixed_sl/ticker/strategy to the WHERE
    re-opens the exact staleness class this function exists to close: those columns
    are read from the seed's CURRENT (mutable) watch_list row, not fixed at version-
    creation time, so if a seed node is retuned between two runs of the identical
    --seed-watch-list-id (real, plausible -- staged/canary nodes get re-roled often),
    the second run's DELETE would silently fail to match the first run's rows, leaving
    two vintages coexisting under one version with no way to tell which run produced
    which -- precisely the union-under-identical-version failure this mechanism was
    built to prevent."""
    with sqlite3.connect(DB_PATH, timeout=60.0) as conn:
        existing_tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))}
        if not existing_tables:
            return 0
        deleted = conn.execute(
            f"DELETE FROM {table_name} WHERE version=?", (config_version,)).rowcount
        conn.commit()
    if deleted:
        print(f"Seed mode: cleared {deleted} prior {table_name} row(s) for "
              f"version={config_version} before writing this run's output "
              f"(repeat-invocation replace, not union).")
    return deleted


def _insert_winner_trades_rows(winner_trades_by_key, node_keys_by_key, strategy_name,
                                config_version, ticker, fixed_sl,
                                kernel_version=GT_TRADES_KERNEL_VERSION,
                                hourly_build_id=None, minute_build_id=None,
                                fill_resolution_by_key=None):
    """New table (not yet in real production) -- one row per real trade, keyed by
    node_key (stable across re-sweeps) + version (disambiguates which data window this
    specific trade sequence came from, since node_key deliberately does NOT encode that
    -- same reasoning candidate_nodes already uses its own separate version column
    rather than baking it into any identity).

    kernel_version/hourly_build_id/minute_build_id (2026-08-29, staleness-invalidation
    fix): stamps which GT kernel logic version and which db_cache.active_builds
    hourly/minute build_id produced this trade list, so run_optimization_sweep.
    get_cached_trades can detect two real stale-cache scenarios neither node_key nor
    version alone catches: (1) a future backtester.py fix changing what
    run_backtest_ground_truth computes without a matching data/version change, (2) a
    scripts/promote_derived_build.py promotion changing the real underlying bars with
    ZERO change to `version` (real precedent: the 2026-08-27 SOXL/DPST/DFEN minute-
    archive narrowing incident). NULL when the caller passes no build_id (a ticker/
    table with nothing ever promoted via active_builds) -- get_cached_trades treats a
    NULL stored value as "unknown, don't trust" too, same as a value mismatch, never as
    an automatic match.

    DELETE-then-INSERT, not INSERT OR IGNORE (fixed 2026-08-29, paired-review HIGH
    finding, round 2, both independent-cold and contextual Opus review independently
    confirmed): the OLD INSERT OR IGNORE behavior meant a re-run over an already-
    populated (node_key, version) silently kept the OLD rows (with their OLD, possibly
    now-stale-or-NULL kernel_version/build_id stamps) and dropped every freshly-stamped
    replacement -- so a re-run could NEVER refresh a stale cache entry, which defeats
    this entire staleness-invalidation fix's purpose (confirmed empirically: the real
    DB's ~73k pre-existing rows would have stayed permanently NULL-stamped forever,
    0% real benefit on all existing data). Worse, a plain per-row INSERT OR REPLACE
    (the other option the review considered) has its own real correctness bug if a
    re-run's new trade list has a DIFFERENT length than what's already stored: it would
    only overwrite the overlapping trade_idx range and leave any extra OLD trade_idx
    rows in place, producing a "Frankenstein" trade list mixing old and new vintages at
    ONE node_key/version -- get_cached_trades' own "trade_idx sequence is contiguous
    0..len-1" gap check would not catch this (both halves are individually contiguous).
    DELETE-then-INSERT avoids both failure modes: every (node_key, version) pair this
    call is about to write gets its ENTIRE old row set removed first, so a re-run is a
    genuine full replacement, never a partial merge."""
    with sqlite3.connect(DB_PATH, timeout=60.0) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS backtest_winner_trades (
                node_key TEXT, version TEXT, ticker TEXT, strategy TEXT, fixed_sl REAL,
                trade_idx INTEGER, entry_time TEXT, entry_price REAL, exit_time TEXT,
                exit_price REAL, exit_reason TEXT, return_pct REAL, armed INTEGER,
                arm_time TEXT, arm_price REAL, created_at TEXT,
                UNIQUE(node_key, version, trade_idx)
            )""")
        # sqlite has no "ADD COLUMN IF NOT EXISTS" (pre-3.35) -- probe-first pattern,
        # same convention scripts/candidate_verification_store.py's ensure_table() uses.
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(backtest_winner_trades)")}
        # fill_resolution (2026-09-06, paired-review HIGH finding, round 2 -- corrected
        # after both independent-cold and contextual review flagged the original comment
        # here as factually wrong): records whether THIS row's trade sequence came from a
        # minute- or second-resolution resim. get_cached_trades (candidate_verification_
        # store.py) now READS this column and stamps it onto every returned trade dict,
        # so run_optimization_sweep.py's build_candidate_report_ground_truth can label
        # trades_resolution correctly ('second_cache' vs 'minute_cache') instead of
        # unconditionally claiming 'minute_cache' -- that mislabeling was a real,
        # confirmed bug, not a hypothetical one, since scripts/persist_addon_overlay_
        # trades.py is a REAL existing consumer of this table (the original comment's
        # "no live consumer relying on it yet" was wrong -- checked directly, that
        # script has queried backtest_winner_trades since 2026-08-29).
        #
        # STILL a real, deliberately-deferred gap: no second_build_id column exists here
        # (unlike hourly_build_id/minute_build_id), so get_cached_trades' staleness check
        # cannot detect a superseded massive_second_derived build promoting mid-campaign
        # the way the 2026-08-27 minute-archive incident is caught on the minute leg --
        # a stale second-resolution row could still be served as fresh. Not fixed here:
        # closing it needs a new column on this table, plumbing get_active_build_id(...,
        # 'second') through both writer and reader, and a real test -- deferred as a
        # separate, scoped follow-up rather than expanding this fix further.
        for col in ("kernel_version TEXT", "hourly_build_id INTEGER", "minute_build_id INTEGER",
                    "fill_resolution TEXT"):
            name = col.split()[0]
            if name not in existing_cols:
                conn.execute(f"ALTER TABLE backtest_winner_trades ADD COLUMN {col}")
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
        # DELETE every (node_key, version) pair this call is about to (re)write -- see
        # this function's own docstring for why this must be a full delete, not a
        # per-row REPLACE. One DELETE per distinct node_key (there are at most 9 -- the
        # top-9-per-scope population), not one big DELETE over the whole ticker/version,
        # so an UNRELATED node_key's existing rows (e.g. a different fixed_sl's earlier
        # run sharing this same version string) are never touched.
        distinct_node_keys = sorted(set(node_keys_by_key.values()))
        n_deleted = 0
        for nk in distinct_node_keys:
            cur = conn.execute(
                "DELETE FROM backtest_winner_trades WHERE node_key=? AND version=?",
                (nk, config_version))
            n_deleted += cur.rowcount
        if n_deleted:
            print(f"  (deleted {n_deleted} pre-existing trade row(s) across "
                  f"{len(distinct_node_keys)} node_key(s) about to be rewritten -- "
                  f"real refresh, not a stale-stamp-preserving skip)")
        buffer = []
        for key, trades in winner_trades_by_key.items():
            nk = node_keys_by_key[key]
            # Per-key resolution (2026-09-06, round 2 fix -- contextual-review-confirmed
            # gap): a single scalar applied to every row was itself a mislabel risk --
            # one candidate in the batch can fall back to minute while its siblings get
            # real 1s. Defaults to "minute" for a caller that doesn't pass the map
            # (defensive; every real call site in this file now does).
            row_fill_resolution = (fill_resolution_by_key or {}).get(key, "minute")
            for i, t in enumerate(trades):
                buffer.append((nk, config_version, ticker, strategy_name, float(fixed_sl), i,
                               str(t['Entry Time']), t['Entry Price'], str(t['Exit Time']),
                               t['Exit Price'], t['exit_reason'], t['Return'], int(t['armed']),
                               str(t['Arm Time']) if t['armed'] else None, t['Arm Price'], now_iso,
                               kernel_version, hourly_build_id, minute_build_id,
                               row_fill_resolution))
        conn.executemany("""
            INSERT INTO backtest_winner_trades
                (node_key, version, ticker, strategy, fixed_sl, trade_idx, entry_time,
                 entry_price, exit_time, exit_price, exit_reason, return_pct, armed,
                 arm_time, arm_price, created_at, kernel_version, hourly_build_id, minute_build_id,
                 fill_resolution)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, buffer)
        conn.commit()
    return len(buffer)


_SWEEP_RUN_LOG_CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS sweep_run_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL, finished_at TEXT,
        script TEXT, pid INTEGER, ticker TEXT, strategy TEXT, fixed_sl REAL,
        windows TEXT, version TEXT,
        n_final_candidates INTEGER, n_candidate_nodes_written INTEGER,
        n_trade_rows_written INTEGER, elapsed_s REAL,
        checkpoint_source TEXT, checkpoint_hash TEXT, checkpoint_path TEXT
    )"""

# checkpoint_source/checkpoint_hash/checkpoint_path added 2026-09-10 (checkpoint
# entry_timing incident structural fix, recommendation #3) -- the real sweep_run_log
# table on disk predates these columns (created under the old CREATE TABLE body, which
# is a no-op against an already-existing table), so a probe-first ALTER is needed on
# top of the CREATE TABLE above for any pre-existing DB. Mirrors campaign_registry.py's
# own probe-first migration convention.
def _ensure_sweep_run_log_checkpoint_columns(conn):
    # Paired-review MEDIUM finding (2026-09-10): PRAGMA table_info + ALTER TABLE is a
    # real TOCTOU race the first time this runs against a pre-existing DB -- two
    # concurrent sweep processes (a real supported mode, see run_inmemory_sweep_queue.sh's
    # own concurrent-claimer design) can both see a column missing and both ALTER; the
    # loser hits sqlite3.OperationalError: duplicate column name and crashes at startup.
    # Narrow, one-time window (only until the columns exist everywhere), but real --
    # swallow the race instead of assuming it can't happen.
    existing = {r[1] for r in conn.execute("PRAGMA table_info(sweep_run_log)").fetchall()}
    for col in ("checkpoint_source", "checkpoint_hash", "checkpoint_path"):
        if col not in existing:
            try:
                conn.execute(f"ALTER TABLE sweep_run_log ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e):
                    raise


def _log_sweep_run_start(ticker, strategy_name, fixed_sl, windows, version):
    """Real invocation log -- new table, 2026-08-29 (Task #6 piece #1, planner dispatch).
    Real gap found the same night: nobody could tell, after the fact, which sweep
    invocations of THIS script actually ran (ticker/strategy/fixed_sl/windows/version,
    when, whether they finished) -- run_optimization_sweep.py's legacy backtest_cache
    pipeline has its own completeness-checking functions (_phase1_coarse_gt_status
    etc.); this in-memory pipeline has no equivalent, since it writes nothing to
    backtest_cache at all (see phase4_candidate_nodes_resolver.py's own module
    docstring for the downstream consequence this already caused). Deliberately no
    try/finally around the start/finish pair -- if the process crashes or is killed
    before _log_sweep_run_finish runs, the row simply stays with finished_at=NULL
    forever, and THAT incompleteness is the "this run never finished" signal. No
    separate status column, no exception handling -- keep it this simple.
    Returns the new row's id."""
    with sqlite3.connect(DB_PATH, timeout=60.0) as conn:
        conn.execute(_SWEEP_RUN_LOG_CREATE_SQL)
        _ensure_sweep_run_log_checkpoint_columns(conn)
        cur = conn.execute("""
            INSERT INTO sweep_run_log (started_at, script, pid, ticker, strategy, fixed_sl,
                windows, version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (time.strftime("%Y-%m-%dT%H:%M:%S"), os.path.basename(__file__), os.getpid(),
              ticker, strategy_name, float(fixed_sl), ",".join(str(w) for w in windows), version))
        conn.commit()
        return cur.lastrowid


def _log_sweep_run_checkpoint(run_id, source, checkpoint_hash, checkpoint_path):
    """Records checkpoint provenance on the sweep_run_log row _log_sweep_run_start
    created (2026-09-10, checkpoint entry_timing incident structural fix, recommendation
    #3) -- called immediately after run_one_fixed_sl's load/compute decision, NOT
    deferred to _log_sweep_run_finish, so a crash anywhere after this point still leaves
    the real computed-vs-loaded provenance visible in sweep_run_log. Previously a
    contaminated/reused-checkpoint run was undetectable from the DB after the fact, only
    inferable from an anomalously small elapsed_s. `source` is 'computed' or 'loaded'."""
    with sqlite3.connect(DB_PATH, timeout=60.0) as conn:
        _ensure_sweep_run_log_checkpoint_columns(conn)
        conn.execute("""
            UPDATE sweep_run_log SET checkpoint_source=?, checkpoint_hash=?, checkpoint_path=?
            WHERE id=?
        """, (source, checkpoint_hash, checkpoint_path, run_id))
        conn.commit()


def _log_sweep_run_finish(run_id, n_final_candidates, n_candidate_nodes_written,
                           n_trade_rows_written, elapsed_s):
    """Completes the row _log_sweep_run_start created. See that function's own
    docstring for why there's deliberately no try/finally guaranteeing this runs."""
    with sqlite3.connect(DB_PATH, timeout=60.0) as conn:
        conn.execute("""
            UPDATE sweep_run_log SET finished_at=?, n_final_candidates=?,
                n_candidate_nodes_written=?, n_trade_rows_written=?, elapsed_s=?
            WHERE id=?
        """, (time.strftime("%Y-%m-%dT%H:%M:%S"), n_final_candidates, n_candidate_nodes_written,
              n_trade_rows_written, elapsed_s, run_id))
        conn.commit()


def _reverse_map_generic_task(row, strategy_name):
    """Reverse-maps a watch_list row's flat strategy-specific SL/trail columns back into
    the generic (tp, sl, hold, w, z, tpct) axis-value tuple phase1_tasks/df_final's own
    take_profit/stop_loss/trail_sell_pct columns expect -- the exact INVERSE of
    _insert_candidate_nodes_rows' forward mapping (same sl_axis_col/fourth_axis_col
    branches, read backwards). Extracted (2026-09-03) out of _load_seed_node so
    _check_live_node_regression (the new live-node-dropped-from-final-selection guard)
    reuses the IDENTICAL mapping instead of a second, hand-copied implementation --
    see feedback_backtest_cache_axis_column_remapping in agent memory for why that's a
    real, previously-hit bug class in this codebase, not a hypothetical risk.

    Forward mapping (for reference, from _insert_candidate_nodes_rows):
        arm_pct = generic tp always (direct passthrough, no strategy-dependent case)
        sl_axis_col == 'trail_buy_pct': trail_buy_pct = generic sl;
            trail_sell_pct = generic tpct (4th axis) if fourth_axis_col == 'trail_pct' else 0.0
        sl_axis_col == 'trail_pct':     trail_buy_pct = 0.0; trail_sell_pct = generic sl
        else (sl_axis_col == 'stop_loss'): trail_buy_pct = trail_sell_pct = 0.0

    Raises ValueError (not SystemExit -- callers decide whether a bad row is fatal or
    just skippable) on a NULL tp-axis value or a non-integer tp/sl axis value (Phase1/
    Phase2/Phase2.5's tp/sl mesh is integer-only by design -- range()-walked boxes,
    integer-only campaign_config grids; a fractional value would silently round to a
    DIFFERENT node, e.g. a canary node's arm_sell_pct=0.1 silently becoming node 0)."""
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)
    raw_generic_tp = row["arm_sell_pct"] if strategy_name == 'TrailingBothZScoreBreakout' \
        else row["take_profit"]
    if raw_generic_tp is None:
        raise ValueError(
            f"tp-axis value is NULL (strategy={strategy_name!r}, looked in "
            f"{'arm_sell_pct' if strategy_name == 'TrailingBothZScoreBreakout' else 'take_profit'})")
    generic_tp = float(raw_generic_tp)
    if sl_axis_col == 'trail_buy_pct':
        generic_sl = float(row["trail_buy_pct"])
        generic_tpct = float(row["trail_sell_pct"]) if fourth_axis_col == 'trail_pct' else 0.0
    elif sl_axis_col == 'trail_pct':
        generic_sl = float(row["trail_sell_pct"])
        generic_tpct = 0.0
    else:
        generic_sl = float(row["stop_loss"])
        generic_tpct = 0.0

    def _require_integer(value, axis_name):
        rounded = int(round(value))
        if abs(rounded - value) > 1e-9:
            raise ValueError(
                f"{axis_name} axis value {value} is fractional -- Phase1/Phase2/Phase2.5's "
                f"tp/sl mesh is integer-only by design; not supported.")
        return rounded

    return (_require_integer(generic_tp, "tp"), _require_integer(generic_sl, "sl"),
            int(row["max_hold_hours"]), int(row["window"]), float(row["z_score_threshold"]),
            float(generic_tpct))


def _load_seed_node(watch_list_id):
    """Smoke-test seed mode (2026-08-29): loads a real live watch_list row and
    reverse-maps its flat strategy-specific SL/trail columns back into the generic
    (tp, sl, tpct) axis-value triple phase1_tasks expects -- the exact INVERSE of
    _insert_candidate_nodes_rows' forward mapping above (same sl_axis_col/
    fourth_axis_col branches, read backwards). watch_list lives in
    cache/live/trading_live.db, a DIFFERENT sqlite file than this script's own
    DB_PATH (trading_universe.db) -- needs its own connection, never DB_PATH.

    Forward mapping (for reference, from _insert_candidate_nodes_rows):
        arm_pct = generic tp always (direct passthrough, no strategy-dependent case)
        sl_axis_col == 'trail_buy_pct': trail_buy_pct = generic sl;
            trail_sell_pct = generic tpct (4th axis) if fourth_axis_col == 'trail_pct' else 0.0
        sl_axis_col == 'trail_pct':     trail_buy_pct = 0.0; trail_sell_pct = generic sl
        else (sl_axis_col == 'stop_loss'): trail_buy_pct = trail_sell_pct = 0.0

    So the reverse, given the row's real flat columns:
        generic tp    = row['take_profit'], EXCEPT for TrailingBothZScoreBreakout, whose
            real tp-axis value lives in row['arm_sell_pct'] instead (take_profit is always
            NULL on those rows -- see signals_db._tp_or_arm_pct/add_node's own "take_profit
            never means two different things" convention. Found by paired review 2026-08-29:
            the naive row['take_profit'] passthrough is NULL for 105 of 154 real non-archived
            watch_list rows, since TrailingBoth is the majority strategy in the live watchlist)
        sl_axis_col == 'trail_buy_pct': generic sl = row['trail_buy_pct'];
            generic tpct = row['trail_sell_pct'] if fourth_axis_col == 'trail_pct' else 0.0
        sl_axis_col == 'trail_pct':     generic sl = row['trail_sell_pct']; generic tpct = 0.0
        else:                            generic sl = row['stop_loss']; generic tpct = 0.0

    Rejects (SystemExit, not a silent workaround) rather than mutate the seed node:
    - a strategy not in campaign_config.STRATEGIES (no real hyperparameter grid to build
      Phase2's mesh from -- currently only TrailingBoth/TrailingExit are defined there)
    - a strategy that doesn't use a fixed_sl (uses_fixed_sl=False) -- seed mode assumes one
      canonical fixed_sl the same way the live-node/--strategy override paths already do
    - a NULL tp-axis/fixed_sl value on the row (shouldn't happen for a real state='live'/
      'dry_run' node, but a stale/malformed row shouldn't silently produce nonsense)
    - a tp or sl axis value that isn't a whole number -- Phase1/Phase2/Phase2.5's fine-mesh/
      cliffbox boxes are built with Python range() over tp/sl (see FINE_RADIUS/CLIFF_RADIUS
      usage below), and campaign_config.STRATEGIES' real grids (COMBINED/TRAIL_PCTS) are
      integer-only too -- there's no way to represent e.g. a canary node's arm_sell_pct=0.1
      on that axis without silently rounding it to a DIFFERENT node (0 != 0.1). tpct (the
      4th axis, trail_sell_pct) is NOT range()-walked anywhere in this file (matched by exact
      equality against TRAIL_PCTS instead) so it's exempt from this restriction and stays a
      real float -- found by paired review 2026-08-29 after a rounding bug silently turned
      arm_sell_pct=0.1 into node 0 with no warning.
    """
    live_db_path = os.path.join(ROOT, "cache", "live", "trading_live.db")
    with sqlite3.connect(live_db_path, timeout=60.0) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM watch_list WHERE id=?", (watch_list_id,)).fetchone()
    if row is None:
        raise SystemExit(f"--seed-watch-list-id {watch_list_id}: no such row in watch_list "
                          f"({live_db_path}).")
    row = dict(row)
    strategy_name = row["strategy"]

    if strategy_name not in campaign_config.STRATEGIES:
        raise SystemExit(
            f"--seed-watch-list-id {watch_list_id}: strategy {strategy_name!r} isn't in "
            f"campaign_config.STRATEGIES ({sorted(campaign_config.STRATEGIES)}) -- seed mode "
            f"needs a real hyperparameter grid (take_profits/stop_losses/trail_pcts) to build "
            f"Phase2's fine-mesh around the seed point, only defined for these strategies.")
    if not strategies.uses_fixed_sl(strategy_name):
        raise SystemExit(
            f"--seed-watch-list-id {watch_list_id}: strategy {strategy_name!r} doesn't use a "
            f"fixed_sl (uses_fixed_sl=False) -- seed mode assumes one canonical fixed_sl per "
            f"run, same as the live-node/--strategy override paths; not supported for this "
            f"strategy yet.")
    if row["fixed_sl"] is None:
        raise SystemExit(f"--seed-watch-list-id {watch_list_id}: fixed_sl is NULL on this row.")

    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)
    try:
        task = _reverse_map_generic_task(row, strategy_name)
    except ValueError as e:
        raise SystemExit(f"--seed-watch-list-id {watch_list_id}: {e}")
    seed = {
        "ticker": row["ticker"],
        "strategy_name": strategy_name,
        "fixed_sl": float(row["fixed_sl"]),
        "window": int(row["window"]),
        "z_score_threshold": float(row["z_score_threshold"]),
        "max_hold_hours": int(row["max_hold_hours"]),
        "entry_timing": row["entry_timing"],
        "task": task,
    }
    print(f"Seed node loaded: watch_list id={watch_list_id} ticker={seed['ticker']} "
          f"strategy={strategy_name} label={row.get('label')!r} entry_timing={row['entry_timing']!r} "
          f"-- raw flat columns take_profit={row['take_profit']} arm_sell_pct={row['arm_sell_pct']} "
          f"stop_loss={row['stop_loss']} trail_buy_pct={row['trail_buy_pct']} "
          f"trail_sell_pct={row['trail_sell_pct']} fixed_sl={row['fixed_sl']} "
          f"(sl_axis_col={sl_axis_col!r}, fourth_axis_col={fourth_axis_col!r}) -> "
          f"reverse-mapped generic Phase1 task (tp,sl,hold,w,z,tpct)={task}")
    return seed


def build_phase1_tasks_grid(z_thresholds, windows, take_profits, stop_losses, hold_time_caps,
                             trail_pcts):
    """The REAL Phase1 task cross-product (extracted verbatim from run_one_fixed_sl's
    non-seed-mode branch, 2026-08-29 paired-review MEDIUM fix, so a test can assert
    against the actual production code path instead of a re-implementation). z and
    window are pooled into the SAME flat list -- this is what makes a multi-value
    --window/--z-thresholds run ONE campaign (shared island centers / top-9 ranking
    downstream) instead of N separate ones."""
    return [(int(tp), int(sl), int(hold), int(w), float(z), float(tpct))
            for z in z_thresholds for w in windows
            for tp in take_profits for sl in stop_losses
            for hold in hold_time_caps for tpct in trail_pcts]


def find_missing_window_z_top_n(present_combos, windows, z_thresholds, df_source, tb_cols, tb_asc,
                                  top_n=2):
    """Extracted (2026-08-30, planner dispatch, paired-review fix -- same "so a test can
    assert against the actual production code path instead of a re-implementation" reasoning
    as build_phase1_tasks_grid above). Used only at the Phase1-insurance-snapshot call site
    in run_one_fixed_sl as of 2026-09-03 -- the Phase2.5-seed and final-stage backfill call
    sites were switched to top_up_window_z_island_quota (see that function's own docstring),
    a stricter unconditional-per-combo-quota generalization of this one; kept here
    unchanged for the insurance snapshot, which doesn't need that generalization (it's a
    debug-only raw union, not a promotion decision).

    Real gap this closes: a pooled top-N_ISLANDS x top-3 selection over ALL (window, z) combos
    at once -- confirmed empirically (2026-08-30) that this leaves an average 11.0 of 16
    combos with ZERO representation, not a rare tail case (concrete real example: ETHU
    window=10 z=1.0 fixed_sl=1 had a previously-validated winner, robust_alpha ~540-555%, but
    produced zero top-30 candidates in the widened sweep).

    Pure/read-only: does NOT mutate `df_source` or append anywhere -- callers decide what to
    do with the returned rows (seed a cliffbox sweep, or promote directly to candidate_nodes).
    For every (window, z) combo not in `present_combos`, returns up to `top_n` rows from
    `df_source` (cagr not null), regardless of how weak they look -- no CAGR floor,
    deliberately: filtering risks hiding a real region the same way the pooled cut already does.

    Diversified by distinct island, not a flat cagr sort (2026-09-03, real research finding --
    see docs/research_log.md's 2026-09-03 HIBL entry): HIBL window=10/z=1.0 has TWO real,
    distinct islands in this combo's own pool -- TP=2 (cagr ~60%) and TP=28 (cagr ~52%, the
    real live-promoted node's own region) -- but the ORIGINAL flat `pool_wz.sort_values(...)
    .head(top_n)` let TP=2's stronger cells consume BOTH of this combo's 2 backfill slots,
    leaving TP=28's real island with ZERO representation even though its own (window, z) combo
    WAS correctly identified as missing and in scope for backfill. Fixed by picking up to
    `top_n` DISTINCT island centers within the missing combo's own pool via pick_island_centers
    (same function/min_sep every other island-picking call site in this codebase already uses)
    and taking each island's own single best cell (`tb_cols`/`tb_asc` tiebreak) -- not top_n
    rows off one flat sort. A combo with only one real island still returns just one row
    (pick_island_centers naturally returns fewer than n if fewer islands exist), same as
    before.

    Returns (missing_combos, rows_by_combo) -- `rows_by_combo[(w, z)]` is a list of up to
    `top_n` pandas Series, one per distinct island found (empty list if `df_source` has zero
    evidence at all for that combo, e.g. every cell had trades=0 -- distinguishable from
    "found some, took up to top_n" so a caller can report the two cases differently instead of
    treating them the same)."""
    all_combos = {(w, z) for w in windows for z in z_thresholds}
    missing_combos = sorted(all_combos - present_combos)
    rows_by_combo = {}
    for w, z in missing_combos:
        pool_wz = df_source[(df_source["window"] == w) & (df_source["z_score_threshold"] == z)
                             & df_source["cagr"].notna()]
        if pool_wz.empty:
            rows_by_combo[(w, z)] = []
            continue
        centers_wz = pick_island_centers(pool_wz, n=top_n, rank_col="cagr")
        picked = []
        for tp_c, sl_c in centers_wz:
            region = pool_wz[(pool_wz["take_profit"] - tp_c).abs().le(FINE_RADIUS)
                              & (pool_wz["stop_loss"] - sl_c).abs().le(FINE_RADIUS)]
            if region.empty:
                continue
            region = region.sort_values(tb_cols, ascending=tb_asc)
            picked.append(region.iloc[0])
        rows_by_combo[(w, z)] = picked
    return missing_combos, rows_by_combo


def top_up_window_z_island_quota(present_df, windows, z_thresholds, df_source, tb_cols, tb_asc,
                                   top_n=2):
    """Unconditional generalization of find_missing_window_z_top_n above (2026-09-03, real
    gap found via Opus paired-review challenge on the diversify-by-island fix above): that
    function only backfills a (window, z) combo that is ENTIRELY absent from the caller's
    current selection -- a combo with even ONE candidate already present (e.g. one of its
    own real islands happened to win a global N_ISLANDS=3 slot via normal pooled selection)
    is treated as "covered" and never checked again, so a SECOND real, distinct island in
    that same combo can still be silently lost with no backfill mechanism to catch it. This
    function instead checks EVERY (window, z) combo in the full grid, unconditionally,
    against a real per-combo quota: up to `top_n` distinct islands guaranteed represented,
    topping up whichever of a combo's own real islands aren't already covered by an
    existing `present_df` row -- regardless of whether that combo already had some other
    presence. Estimated cost is roughly an ADDITIONAL ~1.2x multiplier ON TOP OF (not
    instead of, and not "well under") the ~1.73x downstream Phase2.5/Phase4/5 cost already
    accepted 2026-08-30 for the original window/z backfill -- worst case (every combo
    needs a full extra island) is bounded by 16 combos x top_n=2 = 32 rows vs the original
    mechanism's ~22, and in practice most combos need at most one additional row (this
    only tops up the shortfall, never re-adds an already-covered island) -- corrected
    2026-09-03, contextual-review MEDIUM finding: the ORIGINAL version of this comment
    justified the 1.2x by claiming "most combos are already fully covered," which
    contradicts this codebase's own empirical finding (an average 11.0 of 16 combos have
    ZERO representation pre-backfill) -- the 1.2x estimate itself was never wrong, just
    its stated reason, which risked a future session re-deriving a wrong cost model from it.

    `present_df` -- a DataFrame of whatever candidates the caller currently has (any
    source: island selection, an earlier backfill pass, etc), with at least take_profit/
    stop_loss/window/z_score_threshold columns. May be empty (pd.DataFrame()) -- every
    combo is then treated as fully uncovered, equivalent to find_missing_window_z_top_n's
    original all-combos-missing case.

    Coverage assigned by NEAREST center, not "within FINE_RADIUS of ANY center" (2026-09-03,
    fixed a real bug both independent-cold and contextual review converged on
    independently): pick_island_centers' own separation test is an OR across axes --
    `abs(tp-c0) >= min_sep OR abs(sl-c1) >= min_sep` -- so two genuinely distinct centers
    CAN have overlapping +-FINE_RADIUS boxes (e.g. centers 6 apart in stop_loss but only 2
    apart in take_profit). The original "is any present row within FINE_RADIUS of this
    center" check would then let ONE present row satisfy TWO centers' coverage
    simultaneously, silently starving the second real island of any top-up -- reproduced
    concretely by the cold reviewer with an overlapping-centers pool. Fixed by assigning
    each present row to its OWN single nearest center (Chebyshev distance, matching the
    box-radius shape) BEFORE checking coverage -- a present row can satisfy at most one
    center's quota slot, exactly like the per-island top-3 selection elsewhere in this
    file already does per-island, not per-arbitrary-box.

    Returns (combos_topped_up, rows_by_combo, combos_zero_evidence) -- combos_topped_up is
    the sorted list of (w, z) combos that needed at least one new row; a combo needing zero
    top-up (already fully covered) is absent from both, not present with an empty list --
    this is a coverage-repair pass, not a presence report, so "needed nothing" and "found
    nothing to add despite a gap" are NOT the same case here (unlike find_missing_window_z_
    top_n's own empty-list-vs-absent-key contract, which answers a different question).
    combos_zero_evidence (2026-09-03, restored a real diagnostic both reviews found
    silently dropped -- see find_missing_window_z_top_n's own "zero evidence" distinction)
    is the sorted list of (w, z) combos with literally zero computed cells (every cell had
    trades=0, or no cell was ever computed) -- these can't be topped up at all, and were
    previously invisible in the run log, contrary to this module's "flag loudly, don't
    silently drop" convention."""
    all_combos = {(w, z) for w in windows for z in z_thresholds}
    rows_by_combo = {}
    combos_topped_up = []
    combos_zero_evidence = []
    for w, z in sorted(all_combos):
        pool_wz = df_source[(df_source["window"] == w) & (df_source["z_score_threshold"] == z)
                             & df_source["cagr"].notna()]
        if pool_wz.empty:
            combos_zero_evidence.append((w, z))
            continue
        centers_wz = pick_island_centers(pool_wz, n=top_n, rank_col="cagr")
        if present_df.empty:
            present_wz = present_df
        else:
            present_wz = present_df[(present_df["window"] == w)
                                     & (present_df["z_score_threshold"] == z)]
        covered_centers = set()
        if not present_wz.empty and centers_wz:
            for _, prow in present_wz.iterrows():
                nearest = min(
                    centers_wz,
                    key=lambda c: max(abs(prow["take_profit"] - c[0]), abs(prow["stop_loss"] - c[1])))
                if max(abs(prow["take_profit"] - nearest[0]),
                       abs(prow["stop_loss"] - nearest[1])) <= FINE_RADIUS:
                    covered_centers.add(nearest)
        new_rows = []
        for tp_c, sl_c in centers_wz:
            if (tp_c, sl_c) in covered_centers:
                continue
            region = pool_wz[(pool_wz["take_profit"] - tp_c).abs().le(FINE_RADIUS)
                              & (pool_wz["stop_loss"] - sl_c).abs().le(FINE_RADIUS)]
            if region.empty:
                continue
            region = region.sort_values(tb_cols, ascending=tb_asc)
            new_rows.append(region.iloc[0])
        if new_rows:
            rows_by_combo[(w, z)] = new_rows
            combos_topped_up.append((w, z))
    return combos_topped_up, rows_by_combo, combos_zero_evidence


def find_missing_arm_top_n(present_arms, all_arms, df_source, tb_cols, tb_asc, top_n=3):
    """Single-axis sibling of find_missing_window_z_top_n above (2026-08-30, planner
    dispatch) -- same missing-key-then-top-N-backfill mechanism, but keyed on a single
    `take_profit` value (the 'arm_pct' axis in candidate_nodes' own storage naming --
    take_profit is ALWAYS the generic tp/arm axis regardless of strategy, see
    _insert_candidate_nodes_rows' own forward-mapping docstring) rather than a
    (window, z) pair.

    Real gap this closes: ETHU's real true best SAFE overlay winner (candidate_nodes
    id=3144, 514.3% overlay -- the actual maximum, once a CLIFF-rejected higher number is
    correctly excluded) was discovered via island rank #3's own +-CLIFF_RADIUS neighbor
    search, NOT as an independently-detected island center -- meaning a real, distinct
    take_profit region can exist that never becomes its own TP/SL island under a
    low-N_ISLANDS run, the identical failure mode the window/z backfill above already
    protects against, just on a different axis.

    A separate, twin function rather than generalizing find_missing_window_z_top_n's own
    signature to a variable-arity key -- that function is already shipped, tested, and
    paired-reviewed with 3 real call sites; risking a signature change there for a
    marginal code-sharing gain isn't worth it given how small this logic is.

    Deliberately NOT extended to trail_buy_pct/trail_sell_pct -- no concrete evidence of
    missed structure was found on those axes, only window/z (handled above) and arm/TP
    (here). Scope stays tight to what's actually evidenced, not applied "on principle."

    Returns (missing_arms, rows_by_arm) -- same empty-list-vs-absent-key contract as
    find_missing_window_z_top_n: `rows_by_arm[arm]` is a list of up to `top_n` pandas
    Series, or [] if df_source has zero evidence at all for that arm value."""
    missing_arms = sorted(set(all_arms) - set(present_arms))
    rows_by_arm = {}
    for arm in missing_arms:
        pool_arm = df_source[(df_source["take_profit"] == arm) & df_source["cagr"].notna()]
        if pool_arm.empty:
            rows_by_arm[arm] = []
            continue
        pool_arm = pool_arm.sort_values(tb_cols, ascending=tb_asc)
        rows_by_arm[arm] = [row for _, row in pool_arm.head(top_n).iterrows()]
    return missing_arms, rows_by_arm


def find_missing_window_z_tpct_top_n(present_combos, windows, z_thresholds, trail_pcts,
                                       df_source, tb_cols, tb_asc, top_n=2):
    """Insurance-snapshot-ONLY sibling of find_missing_window_z_top_n above -- keyed on
    the full (window, z, trail_sell_pct) triple instead of just (window, z). NOT a
    replacement for find_missing_window_z_top_n and NOT wired into either of the
    Phase2.5/final-stage production backfill call sites -- those stay exactly as they
    are, per find_missing_arm_top_n's own docstring note above ("Deliberately NOT
    extended to trail_buy_pct/trail_sell_pct -- no concrete evidence of missed
    structure was found on those axes"), which was true for the PROMOTION pipeline at
    the time it was written.

    Real gap THIS function closes (2026-09-03, paired-review HIGH finding against the
    backtest_phase2_insurance snapshot diff, confirmed by both independent-cold and
    contextual review): the insurance snapshot's own top-1000-by-cagr + n=30-wide-
    island union pools across ALL (window, z, trail_sell_pct) scopes combined, same as
    the promotion pipeline's pre-backfill pooling did -- for a real TrailingBoth
    campaign (7 trail_pcts x 2 windows x 3 z = 42 scopes, phase2_rows routinely in the
    hundreds of thousands of cells) a whole trail_pct scope can legitimately end up
    with zero rows in the ~1300-row retained union, even though find_missing_window_z_
    top_n's own (window, z)-only key would call that scope's (w, z) pair "present"
    (some OTHER trail_pct at that same (w, z) survived) and never backfill it. This
    table's whole purpose is re-debugging a lost region after the fact, so leaving a
    trail_pct-scope gap in the snapshot itself defeats that purpose the same way the
    production pooling bug does -- unlike the production pipeline, there's no
    downstream promotion-cost concern here (this is a raw top-N union, not a cliffbox
    seed), so extending the key to all three axes is cheap and directly closes the gap.

    Same empty-list-vs-absent-key contract as its siblings above."""
    all_combos = {(w, z, tp) for w in windows for z in z_thresholds for tp in trail_pcts}
    missing_combos = sorted(all_combos - present_combos)
    rows_by_combo = {}
    for w, z, tp in missing_combos:
        pool_wztp = df_source[(df_source["window"] == w) & (df_source["z_score_threshold"] == z)
                               & (df_source["trail_sell_pct"] == tp) & df_source["cagr"].notna()]
        if pool_wztp.empty:
            rows_by_combo[(w, z, tp)] = []
            continue
        pool_wztp = pool_wztp.sort_values(tb_cols, ascending=tb_asc)
        rows_by_combo[(w, z, tp)] = [row for _, row in pool_wztp.head(top_n).iterrows()]
    return missing_combos, rows_by_combo


def cliffbox_tasks_for_cell(cand, trail_pcts, cliff_radius=None, hold_time_caps=None):
    """The +-cliff_radius tp/sl x +-7h hold x adjacent-trail_pct neighborhood-expansion
    around one raw candidate cell -- factored out (2026-08-30, planner dispatch) from the
    per-island Phase2.5 seed loop so the window/z backfill seed step (added same day, see
    its own call site comment) can reuse the IDENTICAL expansion instead of a second
    hand-copied implementation that could silently diverge from it. `cliff_radius`/
    `hold_time_caps` default to the module globals CLIFF_RADIUS/HOLD_TIME_CAPS (unchanged
    behavior at the original call site); only `trail_pcts` must be passed explicitly, since
    TRAIL_PCTS is strategy-dependent and function-local, not a module global."""
    if cliff_radius is None:
        cliff_radius = CLIFF_RADIUS
    if hold_time_caps is None:
        hold_time_caps = HOLD_TIME_CAPS
    tp_c2, sl_c2 = int(cand["take_profit"]), int(cand["stop_loss"])
    hold_c, w_c, z_c = int(cand["max_hold_hours"]), int(cand["window"]), float(cand["z_score_threshold"])
    tpct_c = float(cand["trail_sell_pct"])
    if tpct_c in trail_pcts:
        idx = trail_pcts.index(tpct_c)
        tpct_neighbors = trail_pcts[max(0, idx - 1): idx + 2]
    else:
        tpct_neighbors = [tpct_c]
    tasks = set()
    for tp in range(max(1, tp_c2 - cliff_radius), min(30, tp_c2 + cliff_radius) + 1):
        for sl in range(max(1, sl_c2 - cliff_radius), min(30, sl_c2 + cliff_radius) + 1):
            for hold in [h for h in hold_time_caps if abs(h - hold_c) <= 7]:
                for tpct in tpct_neighbors:
                    tasks.add((tp, sl, hold, w_c, z_c, float(tpct)))
    return tasks


def apply_grid_overrides(args):
    """Replace the module-level Phase1 grid axes WINDOWS / Z_THRESHOLDS with explicit
    CLI values, if given. Each is a PLAIN REPLACE (not an add) and stays ONE pooled
    Phase1 axis -- pick_island_centers / the final top-9 ranking pool across it, exactly
    as if the module default had been that list. Extracted from main() 2026-08-29 so the
    override is unit-testable without a real backtest run. No-op for any axis whose flag
    was omitted (None). Seed mode sets both from the seed node's own params in main()
    instead and never calls this."""
    global WINDOWS, Z_THRESHOLDS, N_ISLANDS
    if args.window is not None:
        _prev = list(WINDOWS)
        WINDOWS = list(args.window)
        print(f"Window override: WINDOWS={WINDOWS} as ONE pooled Phase1 axis "
              f"(replaces standard grid {_prev})")
    if args.z_thresholds is not None:
        _prev = list(Z_THRESHOLDS)
        Z_THRESHOLDS = list(args.z_thresholds)
        print(f"Z-threshold override: Z_THRESHOLDS={Z_THRESHOLDS} as ONE pooled Phase1 "
              f"axis (replaces standard grid {_prev})")
    if args.n_islands is not None:
        _prev = N_ISLANDS
        N_ISLANDS = args.n_islands
        print(f"Island-count override: N_ISLANDS={N_ISLANDS} (replaces default {_prev}) -- "
              f"widens Phase2's fine-mesh dispatch to this many centers per scope. Only "
              f"affects THIS module's own N_ISLANDS (imported copy) -- run_optimization_"
              f"sweep.py's own module-level N_ISLANDS=3 default is untouched, so its legacy "
              f"callers/pick_island_centers()'s own default parameter value are unaffected.")


def build_arg_parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--strategy", choices=sorted(campaign_config.STRATEGIES),
                     help="explicit strategy, used INSTEAD of load_live_node(TICKER)'s")
    ap.add_argument("--fixed-sl", dest="fixed_sl", type=int,
                     help="explicit SINGLE fixed_sl, used INSTEAD of load_live_node(TICKER)'s. "
                          "Takes precedence over --fixed-sl-values when both are given.")
    ap.add_argument("--fixed-sl-values", type=int, nargs="+", default=None,
                     help="2026-08-29 addition (packaging only, see main()'s own docstring-"
                          "adjacent comment below): loop over each of these fixed_sl values "
                          "within ONE process/pool instead of requiring a separate CLI "
                          "invocation per value -- only used in --strategy override mode "
                          "(ignored when --fixed-sl is explicitly given, and ignored entirely "
                          "in live-node mode, which already has one canonical fixed_sl). "
                          "Default (when omitted) is [1..8], matching the real production "
                          "fixed_sl grid -- default is None here (not [1..8] directly) purely "
                          "so --seed-watch-list-id can detect whether this was EXPLICITLY "
                          "passed for its own mutual-exclusivity check below.")
    ap.add_argument("--window", type=int, nargs="+", default=None,
                     help="run these window value(s) instead of the real production grid "
                          f"(module-level WINDOWS={WINDOWS}) -- e.g. --window 15 for a single "
                          "midpoint value, or --window 5 10 15 20 for a widened grid. "
                          "Multi-value stays ONE pooled Phase1 axis (shared island centers / "
                          "top-9 ranking), NOT N separate campaigns. Replaces the standard "
                          "grid for this run, does not add to it.")
    ap.add_argument("--z-thresholds", dest="z_thresholds", type=float, nargs="+", default=None,
                     help="run these z-score threshold value(s) instead of the real "
                          f"production grid (module-level Z_THRESHOLDS={Z_THRESHOLDS}) -- e.g. "
                          "--z-thresholds 0.5 1 1.5 2 for the AGQ widened pooled grid (z=0.5 "
                          "is otherwise unreachable from the CLI). Multi-value stays ONE "
                          "pooled Phase1 axis, same semantics as --window. Replaces the "
                          "standard grid for this run, does not add to it. Mutually exclusive "
                          "with --seed-watch-list-id (seed mode derives z from the seed node "
                          "itself). Default None leaves Z_THRESHOLDS unchanged.")
    ap.add_argument("--n-islands", dest="n_islands", type=int, default=None,
                     help="override module-level N_ISLANDS (default 3) -- widens Phase2's "
                          "fine-mesh dispatch to this many centers per scope, e.g. --n-islands "
                          "10 so a mid-CAGR core config with an explosive overlay isn't "
                          "discarded before Phase4 ever sees it. Only affects THIS module's "
                          "own copy of N_ISLANDS -- run_optimization_sweep.py's own N_ISLANDS=3 "
                          "default and its own legacy callers are untouched. Default None "
                          "leaves N_ISLANDS unchanged.")
    ap.add_argument("--entry-timing", dest="entry_timing", choices=["open_check", "close"],
                     default=None,
                     help="override module-level ENTRY_TIMING (default 'open_check', matching "
                          "every real live watch_list node today -- 0 live nodes use 'close'). "
                          "The GT kernel (_simulate_trail_ground_truth) already supports "
                          "close_check; this flag is the CLI path to actually sweep it for a "
                          "ticker with no live close-entry node to seed from. Mutually "
                          "exclusive with --seed-watch-list-id (seed mode derives entry_timing "
                          "from the seed node itself). A non-default value gets a -close "
                          "version-string suffix (campaign_registry.build_version_string) so "
                          "it can never collide with an open_check campaign sharing the same "
                          "label/z/window/n_islands. Default None leaves ENTRY_TIMING "
                          "unchanged.")
    ap.add_argument("--campaign-label", dest="campaign_label", default=None,
                     help="override module-level CAMPAIGN_LABEL (default 'v6.5') -- e.g. "
                          "--campaign-label v6.6 for a new campaign generation distinct from "
                          "the standing v6.5 one. Default None leaves CAMPAIGN_LABEL "
                          "unchanged.")
    ap.add_argument("--ticker", type=str, default=None,
                     help="override module-level TICKER (default 'SOXL') -- e.g. --ticker "
                          "AGQ to run a different ticker's campaign. Mutually exclusive with "
                          "--seed-watch-list-id, which derives TICKER from the real live "
                          "watch_list row it seeds from -- reproducing that exact node, "
                          "including its real ticker, IS seed mode's whole point, so an "
                          "explicit --ticker there would be a silent, likely-wrong override "
                          "rather than a deliberate choice. Default None leaves TICKER at "
                          "its module default unchanged.")
    ap.add_argument("--seed-watch-list-id", dest="seed_watch_list_id", type=int, default=None,
                     help="Smoke-test mode (2026-08-29): seed Phase1 with exactly ONE real "
                          "live watch_list row's params (reverse-mapped via "
                          "strategies.resolve_axis_columns back to the generic tp/sl/tpct "
                          "axis-value triple phase1_tasks expects) instead of the full "
                          "campaign_config grid -- Phase2's real fine-mesh/island logic then "
                          "builds the neighborhood around that single seed point completely "
                          "unchanged. Reads cache/live/trading_live.db (a DIFFERENT sqlite "
                          "file than this script's own DB_PATH), NOT DB_PATH. Mutually "
                          "exclusive with --strategy/--fixed-sl/--fixed-sl-values (the full "
                          "campaign grid path), --window and --z-thresholds (both come from "
                          "the seed node itself, not a separate override), and --resume-from-top100/"
                          "--checkpoint-file (both bypass phase1_tasks entirely, which would "
                          "let the relaxed seed-mode sanity check trivially pass on unrelated "
                          "rows).")
    ap.add_argument("--resume-from-top100", action="store_true",
                     help="skip Phase1 dispatch entirely; load df1 from the persisted "
                          "top-100 Phase1-Coarse-GT snapshot in backtest_cache instead. "
                          "Deliberately tests the correctness risk already flagged in "
                          "design discussion: pick_island_centers walks the FULL ranked "
                          "grid by design (a pre-filtered top-100 subset can miss a real "
                          "island entirely) -- this compares against the full-grid run's "
                          "own 9 final candidates to see how much it actually diverges.")
    ap.add_argument("--start-date", type=str, default=None,
                     help="override module-level START (format YYYY-MM-DD, matching "
                          "START/END module default '2021-08-23'/'2026-08-21') -- use a "
                          "short range for a fast full-pipeline smoke test. Optional; "
                          "default None leaves START at its module default unchanged.")
    ap.add_argument("--end-date", type=str, default=None,
                     help="override module-level END (format YYYY-MM-DD) -- paired with "
                          "--start-date for a short-range smoke test. Optional; default "
                          "None leaves END at its module default unchanged.")
    ap.add_argument("--checkpoint-file", default=None,
                     help="local dev-iteration checkpoint (parquet, NOT a production "
                          "artifact) for Phase1+Phase2's combined df_full, PLUS a "
                          "<path>.manifest.json sidecar with the full resolved param dict "
                          "(2026-09-10: content-hashed identity, hard-refuses to load on a "
                          "mismatch or missing manifest -- see _checkpoint_identity_params). "
                          "If it exists and validates, skips Phase1 AND Phase2 entirely and "
                          "loads df_full from it -- for iterating on Phase2.5 logic without "
                          "repaying the ~7min Phase1+Phase2 cost each time. If missing, "
                          "computes normally and saves both files after Phase2 finishes. "
                          "Default (see --use-checkpoint below): "
                          "<job-tmp>/bench_phase12_checkpoint_<ticker>_<strategy>_<fixed_sl>_"
                          "<content_hash16>.parquet -- ONE SHARED PATH ACROSS --fixed-sl-"
                          "values (unlike the auto-computed default, which is naturally "
                          "distinct per fixed_sl since fixed_sl is part of the hash), so "
                          "this flag is rejected outright at parse time for a run covering "
                          "more than one fixed_sl (mutually exclusive with --resume-from-"
                          "top100 AND with --seed-watch-list-id too). Explicitly passing "
                          "this flag is itself the opt-in this run needs to load a "
                          "checkpoint at all -- see --use-checkpoint below for the "
                          "auto-computed-default-path equivalent.")
    ap.add_argument("--use-checkpoint", action="store_true",
                     help="opt-in (2026-09-03, real live incident fix -- checkpoint entry_"
                          "timing incident, docs/backlog_cache.md): without this flag (and "
                          "without an explicit --checkpoint-file), the auto-computed default "
                          "checkpoint path is NEVER loaded, even if a stale file happens to "
                          "exist there -- only explicitly saved to, for a LATER run that "
                          "passes this flag to opt back in. Closes a real live incident: "
                          "every invocation of this script registers a real campaign row "
                          "(main()'s unconditional campaign_registry.register_campaign call) "
                          "-- there is no separate 'just a dev test' mode this script can "
                          "detect on its own, so 'opt-in for any run registering a real "
                          "campaign' means opt-in for every default-path load, full stop. "
                          "A genuine local dev-iteration session (the ONLY real reason this "
                          "mechanism exists) passes this explicitly; a real campaign run "
                          "never should.")
    return ap


def _build_version_string(args):
    """Builds the sweep_run_log/candidate_nodes 'version' discriminator string, extracted
    (2026-08-29, paired-review fixup) out of main() so it's directly unit-testable without
    a real DB/backtest run. Depends only on module-level DATA_SOURCE/START/END plus the
    override flags on args -- NOT on fixed_sl (one version string legitimately covers every
    (strategy, fixed_sl) combo from a single campaign's real invocations).

    PURE -- delegates the actual string construction to campaign_registry.build_version_
    string (2026-08-31, Task #8) so this module and run_inmemory_sweep_queue.sh resolve the
    exact same version string from ONE shared function instead of independently
    reconstructing it -- closes the 2026-08-31 pv3/pv4 split-brain incident class (see
    docs/plans/campaign_registry_design.md). Deliberately does NOT call campaign_registry.
    register_campaign here (that's a real DB write) -- this function must stay side-effect-
    free since existing tests call it directly with no DB fixture. The one real call site
    (main(), below) does the registration separately, exactly once per process."""
    from scripts import campaign_registry
    return campaign_registry.build_version_string(
        label=CAMPAIGN_LABEL, promotion_algo_version=PROMOTION_ALGO_VERSION,
        data_source=DATA_SOURCE, window_start=START, window_end=END,
        z_thresholds=(Z_THRESHOLDS if args.z_thresholds is not None else None),
        n_islands=args.n_islands, seed_watch_list_id=args.seed_watch_list_id,
        # Gated on args.entry_timing (the explicit CLI override), NOT the module-
        # global ENTRY_TIMING -- same reasoning as the z_thresholds gate above.
        # In seed mode, ENTRY_TIMING is set from the seed node's own real value
        # (main()'s seed block), and the pre-existing -seed<id> suffix already
        # makes that campaign collision-safe; args.entry_timing stays None there
        # (mutually exclusive with --seed-watch-list-id), so this must NOT read
        # the global or every close-entry seed campaign's version string would
        # silently change, breaking sweep_run_log's finished-run dedup and
        # orphaning existing candidate_nodes rows under the old string.
        entry_timing=args.entry_timing)


def main():
    ap = build_arg_parser()
    args = ap.parse_args()
    if args.resume_from_top100 and args.checkpoint_file:
        raise SystemExit("--resume-from-top100 and --checkpoint-file are mutually exclusive "
                          "(one tests a narrow top-100-only dataset, the other a full "
                          "Phase1+Phase2 checkpoint) -- pick one.")
    if args.resume_from_top100 and args.use_checkpoint:
        # paired-review LOW finding (2026-09-03, Runlist Step 2): --use-checkpoint has the
        # identical conflict --checkpoint-file already hard-errors on above -- both
        # predicates (_should_load_checkpoint/_should_save_checkpoint) already correctly
        # exclude --resume-from-top100 regardless of the opt-in flags, so this combination
        # was never UNSAFE, just silently ignored instead of rejected like its sibling flag.
        raise SystemExit("--resume-from-top100 and --use-checkpoint are mutually exclusive "
                          "(resume-from-top100 never loads/saves a checkpoint regardless of "
                          "this flag) -- pick one.")

    if args.n_islands is not None and args.n_islands < 1:
        # (2026-08-29, paired review, CONFIRMED MEDIUM): pick_island_centers's loop-exit
        # condition (`if len(centers) == n: break`) is unreachable for n<=0, so an
        # unvalidated --n-islands 0 or negative would silently return an unbounded (all
        # min-sep-separated) set of island centers instead of erroring.
        raise SystemExit(f"--n-islands {args.n_islands}: must be >= 1.")

    if args.seed_watch_list_id is not None:
        _seed_conflicts = []
        if args.strategy is not None:
            _seed_conflicts.append("--strategy")
        if args.fixed_sl is not None:
            _seed_conflicts.append("--fixed-sl")
        if args.fixed_sl_values is not None:
            _seed_conflicts.append("--fixed-sl-values")
        if args.window is not None:
            _seed_conflicts.append("--window")
        if args.z_thresholds is not None:
            _seed_conflicts.append("--z-thresholds")
        if args.resume_from_top100:
            _seed_conflicts.append("--resume-from-top100")
        if args.checkpoint_file:
            _seed_conflicts.append("--checkpoint-file")
        if args.use_checkpoint:
            _seed_conflicts.append("--use-checkpoint")
        if args.ticker is not None:
            _seed_conflicts.append("--ticker")
        if args.entry_timing is not None:
            _seed_conflicts.append("--entry-timing")
        if _seed_conflicts:
            raise SystemExit(
                f"--seed-watch-list-id is mutually exclusive with {', '.join(_seed_conflicts)} "
                f"-- seed mode derives strategy/fixed_sl/window/z directly from the real live "
                f"watch_list row, it doesn't take a separate grid-override or axis-override "
                f"path. Pick one.")

    apply_grid_overrides(args)

    if args.start_date is not None:
        global START
        START = args.start_date
        print(f"Start-date override: START={START} (module default '2021-08-23')")
    if args.end_date is not None:
        global END
        END = args.end_date
        print(f"End-date override: END={END} (module default '2026-08-21')")

    if args.ticker is not None:
        # Mutually exclusive with --seed-watch-list-id (enforced above) -- seed mode
        # derives TICKER from the real live watch_list row instead, so this branch and
        # that one never both apply to the same run. Must run before load_live_node(TICKER)
        # below (live-node-mode default branch) and before run_one_fixed_sl's TICKER usage.
        global TICKER
        TICKER = args.ticker
        print(f"Ticker override: TICKER={TICKER} (module default 'SOXL')")

    if args.entry_timing is not None:
        # Mutually exclusive with --seed-watch-list-id (enforced above) -- seed mode
        # derives ENTRY_TIMING from the real live watch_list row instead (see that
        # block's own comment for why silently leaving it at 'open_check' there would
        # be wrong). Must run before the live-node-mode default branch below, which
        # would otherwise silently overwrite this with whatever the live node's own
        # entry_timing is.
        global ENTRY_TIMING
        ENTRY_TIMING = args.entry_timing
        print(f"Entry-timing override: ENTRY_TIMING={ENTRY_TIMING!r} (module default "
              f"'open_check')")

    if args.campaign_label is not None:
        global CAMPAIGN_LABEL
        CAMPAIGN_LABEL = args.campaign_label
        print(f"Campaign-label override: CAMPAIGN_LABEL={CAMPAIGN_LABEL!r} (module default "
              f"'v6.5')")

    seed = None
    if args.seed_watch_list_id is not None:
        seed = _load_seed_node(args.seed_watch_list_id)
        strategy_name = seed["strategy_name"]
        fixed_sl_list = [seed["fixed_sl"]]
        # TICKER and ENTRY_TIMING are already declared global above (--ticker and
        # --entry-timing override blocks) -- a second `global` here after either
        # block's assignment raises SyntaxError ("assigned to before global
        # declaration"), a real Python quirk confirmed while building this: once a
        # name is globalled+assigned in one place in a function, a later `global`
        # statement for the SAME name is illegal, even in a mutually-exclusive
        # branch that can never run in the same call. TICKER/ENTRY_TIMING stay
        # covered by their earlier declarations.
        global WINDOWS, Z_THRESHOLDS, HOLD_TIME_CAPS
        WINDOWS = [seed["window"]]
        # Z_THRESHOLDS/ENTRY_TIMING/HOLD_TIME_CAPS overridden the same way WINDOWS already
        # is above -- found by paired review 2026-08-29 (round 2 and round 3):
        # - Z_THRESHOLDS: without this, a seed z not in the default [1.0, 1.5, 2.0] grid
        #   (true for every real z=0.1 canary node) makes every Phase2 w/z/tpct slice come
        #   up empty with no explicit warning, silently degrading to seed-alone.
        # - ENTRY_TIMING: left at its "open_check" default, silently backtests/promotes a
        #   real close-entry_timing seed node under the WRONG entry_timing, defeating
        #   "reproduce this exact live node."
        # - HOLD_TIME_CAPS: without this, Phase2's mesh only ever explores the STANDARD
        #   grid's hold values (7,14,21,...) around the seed's tp/sl, never the seed's own
        #   real hold (real examples: RETL live hold=11, TMF live hold=48, hold=100 paper
        #   node) -- and since the cliff-safety neighbor check filters strictly on
        #   max_hold_hours == candidate's own hold, a candidate at the seed's off-grid hold
        #   would only ever match itself (n_neighbors_checked=1), producing a
        #   "cliff-safe"-looking verdict backed by nothing.
        Z_THRESHOLDS = [seed["z_score_threshold"]]
        ENTRY_TIMING = seed["entry_timing"]
        HOLD_TIME_CAPS = [seed["max_hold_hours"]]
        print(f"Seed mode: strategy={strategy_name}, fixed_sl={fixed_sl_list[0]}, "
              f"WINDOWS override -> {WINDOWS}, Z_THRESHOLDS override -> {Z_THRESHOLDS}, "
              f"ENTRY_TIMING override -> {ENTRY_TIMING!r}, HOLD_TIME_CAPS override -> "
              f"{HOLD_TIME_CAPS} (all derived from watch_list id={args.seed_watch_list_id}, "
              f"no grid/live-node lookup)")
        if seed["ticker"] != TICKER:
            print(f"Seed mode: ticker override {TICKER} -> {seed['ticker']} "
                  f"(from watch_list id={args.seed_watch_list_id})")
            TICKER = seed["ticker"]
    elif args.strategy is not None:
        strategy_name = args.strategy
        # --fixed-sl (single, explicit) takes precedence over --fixed-sl-values (the new
        # looping default) -- an explicit single value is an unambiguous "just this one"
        # request, not something the new default-[1..8] behavior should override.
        fixed_sl_list = [args.fixed_sl] if args.fixed_sl is not None else (
            args.fixed_sl_values if args.fixed_sl_values is not None else [1, 2, 3, 4, 5, 6, 7, 8])
        print(f"Override mode: strategy={strategy_name}, fixed_sl values={fixed_sl_list} "
              f"(no live-node lookup)")
    else:
        node = load_live_node(TICKER)
        strategy_name = node["strategy"]
        fixed_sl_list = [node["fixed_sl"]]
        print(f"Live node: strategy={strategy_name}, fixed_sl={fixed_sl_list[0]}")

    if args.checkpoint_file and len(fixed_sl_list) > 1:
        # Paired-review HIGH finding (2026-09-10, checkpoint content-hash structural fix):
        # an explicit --checkpoint-file path does NOT vary per fixed_sl (unlike the
        # auto-computed default path, which is hash-derived and therefore naturally
        # distinct per fixed_sl) -- but fixed_sl IS part of the checkpoint identity hash
        # (_checkpoint_identity_params). Without this guard, fixed_sl_list[0] would
        # compute+save under the shared path, then fixed_sl_list[1] would find that file,
        # see a hash mismatch, and _validate_checkpoint_manifest would hard-raise,
        # killing the whole multi-fixed_sl run. That hard-raise is the gate working as
        # designed (the OLD code silently loaded fixed_sl[0]'s df_full for every
        # subsequent fixed_sl instead) -- but failing at argv-parse time with a clear
        # message is much better than failing mid-run after fixed_sl[0]'s candidates are
        # already written. Real invocation path: run_inmemory_sweep_queue.sh can pass
        # --checkpoint-file alongside a multi-value --fixed-sl-values CSV if an operator
        # sets CHECKPOINT_FILE explicitly (the default USE_CHECKPOINT=1 form is unaffected
        # -- its auto-computed path already varies per fixed_sl).
        raise SystemExit(
            f"--checkpoint-file is a single shared path but this run covers {len(fixed_sl_list)} "
            f"fixed_sl values ({fixed_sl_list}) -- each needs its own checkpoint identity. Use "
            f"--use-checkpoint instead (auto-computed, hash-derived path, one per fixed_sl), or "
            f"invoke this script once per fixed_sl with its own --checkpoint-file each time.")

    # version depends only on DATA_SOURCE/START/END (not fixed_sl) -- computed ONCE and
    # reused across every fixed_sl in the loop below, matching the real established
    # convention already confirmed against live data (scripts/candidate_nodes_status.py):
    # one version string legitimately covers every (strategy, fixed_sl) combo from a
    # single campaign's real invocations.
    version = _build_version_string(args)
    # Real DB registration (2026-08-31, Task #8) -- the ONE place this process registers
    # its campaign row, exactly once per invocation. _build_version_string above stays
    # pure (no I/O) specifically so existing tests can call it directly with no DB
    # fixture; this is the real side-effecting call, using the identical inputs.
    from scripts import campaign_registry
    campaign_registry.register_campaign(
        version, CAMPAIGN_LABEL, PROMOTION_ALGO_VERSION, DATA_SOURCE, START, END,
        z_thresholds=(Z_THRESHOLDS if args.z_thresholds is not None else None),
        n_islands=args.n_islands, seed_watch_list_id=args.seed_watch_list_id,
        # args.entry_timing (not the module-global ENTRY_TIMING) -- must match
        # _build_version_string's own gating exactly, or this call's stored
        # `entry_timing` column and `version` string (computed above, already
        # gated correctly) would disagree for a seed-mode close campaign.
        entry_timing=args.entry_timing, created_by="bench_phase1_phase2_inmemory.py")

    # fixed_sl packaging (2026-08-29, Task #6 follow-up, planner dispatch): loops the
    # existing per-fixed_sl Phase1+Phase2+Phase2.5+candidate-write logic (now
    # run_one_fixed_sl below) over multiple fixed_sl values within ONE process/pool,
    # instead of requiring 8 separate CLI invocations. PACKAGING ONLY, NOT a selection-
    # algorithm change: fixed_sl stays OUTSIDE the pooled/flattened window/z/tpct
    # ranking -- each fixed_sl value still gets its own fully separate Phase1/2/2.5 run
    # and its own separate top-9 candidate_nodes rows, exactly what a separate
    # --fixed-sl N invocation would produce today. Deliberately NOT pooling fixed_sl in
    # with window/z/tpct for island detection -- Task #6's OAT sensitivity work
    # (commit 3390eeb) found fixed_sl is genuinely cliff-prone (real cliffs even at
    # 0.25%-step sub-integer resolution), so pooling it into the existing ranking would
    # be wrong given that finding.
    #
    # Resumability (real requirement): before each fixed_sl iteration, check sweep_run_log
    # for an already-finished row at this exact (ticker, strategy, fixed_sl, windows,
    # version) -- if found, skip it entirely (its candidates are already in
    # candidate_nodes from a prior run). Makes a killed-partway multi-value run
    # resumable: rerunning the same command only redoes the fixed_sl values that never
    # finished.
    # Seed mode's single reverse-mapped Phase1 task, stashed on args so
    # run_one_fixed_sl can bypass the full grid cross-product with it -- None in
    # every non-seed mode, leaving that function's existing behavior untouched.
    args._seed_task = seed["task"] if seed is not None else None

    # Preload-before-fork (2026-09-11, real memory-scaling bug fix): this process is
    # scoped to exactly one TICKER for its whole lifetime (finalized above by this
    # point -- --ticker override, seed mode, or load_live_node all resolve before
    # here). Both the main 8-worker pool (Phase1-coarse/Phase2-island, fill_resolution
    # ='minute') and the separate second-resolution pool (Phase2.5-cliffbox,
    # fill_resolution='second') call into run_optimization_sweep's worker-side loaders
    # (_load_hourly_df_ground_truth/_load_minute_df/_load_second_df) FROM INSIDE the
    # worker task body -- each loader's cache is a plain module-level dict, so a worker
    # process that never inherited an already-populated entry loads (and keeps) its own
    # full copy for the rest of its life, once per distinct worker that happens to pick
    # up a task needing it. Confirmed empirically: second-res workers held ~2.85GB RSS
    # each (independent copies, not shared) capping SECOND_RESOLUTION_MAX_CONCURRENT at
    # 2; main-pool workers separately held ~0.37GB each x 8 = ~2.96GB of the same
    # duplication at smaller scale. Populating all three caches HERE, in the main
    # process, before either ProcessPoolExecutor is created, means every worker forked
    # afterward inherits the already-loaded data via Linux fork's copy-on-write sharing
    # (same pattern scripts/phase5_second_level_overlay_check.py's main() already uses
    # for _DFH/_DF_1M/_DF_1S) -- one real copy total instead of one per worker that
    # touches this ticker. Second-res preload is wrapped in try/except ValueError,
    # matching _load_node_inputs_ground_truth's own existing fallback for a ticker with
    # no active massive_second_derived build -- Phase2.5 still falls back to minute
    # resolution per-cell in that case, same as before this fix, just without a free
    # preload to lean on.
    _load_hourly_df_ground_truth(TICKER, data_source=DATA_SOURCE)
    _load_minute_df(TICKER, data_source=DATA_SOURCE)
    try:
        _load_second_df(TICKER, data_source=DATA_SOURCE)
    except ValueError as e:
        print(f"[preload] {TICKER}: {e} -- second-resolution pool workers will fall back "
              f"to minute resolution per-cell, same as _load_node_inputs_ground_truth's "
              f"existing fallback.")

    _windows_str = ",".join(str(w) for w in WINDOWS)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for fixed_sl in fixed_sl_list:
            with sqlite3.connect(DB_PATH, timeout=60.0) as _conn:
                _conn.execute(_SWEEP_RUN_LOG_CREATE_SQL)
                _ensure_sweep_run_log_checkpoint_columns(_conn)
                # Seed mode bypasses this dedup check entirely (2026-08-29, paired review
                # round 3, contextual): the "-seed<id>" version suffix alone only solved
                # cross-contamination with real full-grid campaigns, it did NOT achieve
                # repeatability -- every invocation of the same --seed-watch-list-id
                # produces the identical version string, so a second identical run of the
                # exact same seed command would otherwise print "already done" and do zero
                # work, contradicting the whole point ("fast, REPEATABLE full-pipeline
                # smoke test"). Seed mode is explicitly a cheap, intentionally-repeatable
                # smoke test, not a real campaign that needs resumability protection from
                # a killed-partway run -- so it just always re-runs.
                already_done = None if args.seed_watch_list_id is not None else _conn.execute("""
                    SELECT 1 FROM sweep_run_log
                    WHERE ticker=? AND strategy=? AND fixed_sl=? AND windows=? AND version=?
                          AND finished_at IS NOT NULL
                    LIMIT 1
                """, (TICKER, strategy_name, float(fixed_sl), _windows_str, version)).fetchone()
            if already_done:
                print(f"\nfixed_sl={fixed_sl}: already done (finished sweep_run_log row found "
                      f"for this exact ticker/strategy/fixed_sl/windows/version) -- skipping.")
                continue
            run_one_fixed_sl(pool, strategy_name, fixed_sl, version, args)


def _resolve_checkpoint_grid(strategy_name, take_profits, stop_losses, trail_pcts, hold_time_caps):
    """Fills in any grid axis left as None by resolving it from
    campaign_config.STRATEGIES[strategy_name] / module HOLD_TIME_CAPS -- the same
    source run_one_fixed_sl itself uses (see its own TAKE_PROFITS/STOP_LOSSES/
    TRAIL_PCTS local vars). Lets the real call site pass its already-resolved
    values in directly (no recomputation, no drift risk) while other callers
    (tests, ad hoc introspection) can omit them and still get a correct default."""
    grid = campaign_config.STRATEGIES[strategy_name]
    if take_profits is None:
        take_profits = grid["take_profits"]
    if stop_losses is None:
        stop_losses = grid["stop_losses"]
    if trail_pcts is None:
        trail_pcts = _trail_pcts_for_strategy(strategy_name, grid)
    if hold_time_caps is None:
        hold_time_caps = HOLD_TIME_CAPS
    return take_profits, stop_losses, trail_pcts, hold_time_caps


def _checkpoint_identity_params(strategy_name, fixed_sl, args, take_profits=None,
                                 stop_losses=None, trail_pcts=None, hold_time_caps=None):
    """The full resolved param set that determines Phase1/Phase2's real output for this
    run -- see docs/backlog_cache.md's 'checkpoint entry_timing incident' architectural
    item (2026-09-01, HIGH). Replaces the old hand-maintained filename-key allowlist
    (_build_checkpoint_filename previously grew a new hand-written "_<axis>_key" string
    fragment after each of 6 separate real live incidents: WINDOWS, Z_THRESHOLDS, date
    range, TICKER/seed_watch_list_id, N_ISLANDS, PROMOTION_ALGO_VERSION, ENTRY_TIMING)
    with a single param dict that gets content-hashed for the checkpoint's real identity
    (_checkpoint_identity_hash) -- both recommendations #1/#2 from that review. Adding a
    new axis that affects Phase1/Phase2's output in the future means adding one line to
    this dict, not inventing a new filename fragment and hoping every future reviewer
    remembers the pattern (the review's own words: "the pattern guarantees a seventh").

    Every value is read from the ACTUAL resolved state, not from args alone -- module
    globals (TICKER/WINDOWS/Z_THRESHOLDS/ENTRY_TIMING/N_ISLANDS) may already reflect a
    seed-mode or CLI-override value by the time this runs (main()'s apply_grid_overrides()
    and the --seed-watch-list-id block both mutate these globals before run_one_fixed_sl
    is ever called for real work) -- so this can't drift from what Phase1 tasks are
    actually built from. take_profits/stop_losses/trail_pcts/hold_time_caps also close
    the separate, related gap flagged in the same paired review (docs/backlog_cache.md's
    "checkpoint filename doesn't key on campaign_config.py's grid contents" entry):
    editing COMBINED/TRAIL_PCTS in campaign_config.py now changes the hash too, since the
    resolved grid tuple is part of what's hashed, not just strategy_name.

    n_generations/fine_radius added by independent-cold paired review (2026-09-10): both
    change which real Phase2 cells get computed into df_full (N_GENERATIONS controls the
    island-reseeding pass count; FINE_RADIUS sets the Phase2 mesh box half-width), and
    PROMOTION_ALGO_VERSION's own contract explicitly says NOT to bump for a sweep-scope
    parameter change like this (pointing instead at "its own -z/-isl/-seed suffix" -- the
    exact per-axis mechanism this hash replaces), so relying on a human remembering to
    bump PROMOTION_ALGO_VERSION here would NOT have covered them -- neither is a CLI
    override today, but hashing them now closes the gap unconditionally rather than by
    convention."""
    take_profits, stop_losses, trail_pcts, hold_time_caps = _resolve_checkpoint_grid(
        strategy_name, take_profits, stop_losses, trail_pcts, hold_time_caps)
    return {
        "strategy_name": strategy_name,
        "fixed_sl": float(fixed_sl),
        "ticker": TICKER,
        "data_source": DATA_SOURCE,
        "start": START,
        "end": END,
        "windows": sorted(WINDOWS),
        "z_thresholds": sorted(Z_THRESHOLDS),
        "hold_time_caps": sorted(hold_time_caps),
        "take_profits": sorted(take_profits),
        "stop_losses": sorted(stop_losses),
        "trail_pcts": sorted(trail_pcts),
        "seed_watch_list_id": getattr(args, "seed_watch_list_id", None),
        "n_islands": N_ISLANDS,
        "n_generations": N_GENERATIONS,
        "fine_radius": FINE_RADIUS,
        "promotion_algo_version": PROMOTION_ALGO_VERSION,
        "entry_timing": ENTRY_TIMING,
    }


def _checkpoint_identity_hash(params):
    """Deterministic hash over the resolved param dict -- sort_keys means key
    order/insertion order never affects the hash. sha256 truncated to 16 hex chars
    (collision risk is irrelevant here: this is an identity check against a
    human-readable sidecar manifest, not a security boundary, and the manifest's own
    full params dict is the real source of truth _validate_checkpoint_manifest checks
    against, not the hash alone)."""
    blob = json.dumps(params, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _build_checkpoint_filename(strategy_name, fixed_sl, args, take_profits=None,
                                stop_losses=None, trail_pcts=None, hold_time_caps=None):
    """Builds the dev-iteration checkpoint filename, extracted (2026-08-29, paired-review
    fixup) out of run_one_fixed_sl so it's directly unit-testable without a real DB/backtest
    run. Content-hashed (2026-09-10 structural fix) rather than built from a hand-maintained
    key-list -- see _checkpoint_identity_params' docstring. The TICKER/strategy_name/fixed_sl
    prefix is kept purely for human skimmability of a checkpoint directory listing; the hash
    suffix is what actually determines whether two runs' checkpoints collide or not."""
    params = _checkpoint_identity_params(strategy_name, fixed_sl, args, take_profits,
                                          stop_losses, trail_pcts, hold_time_caps)
    content_hash = _checkpoint_identity_hash(params)
    return f"bench_phase12_checkpoint_{TICKER}_{strategy_name}_{fixed_sl}_{content_hash}.parquet"


def _checkpoint_manifest_path(checkpoint_path):
    return checkpoint_path + ".manifest.json"


def _write_checkpoint_manifest(checkpoint_path, params, content_hash):
    """Human-readable sidecar written alongside every saved checkpoint (2026-09-10
    structural fix, recommendation #2) -- a bare hash in the filename is opaque; this
    manifest is what an operator or a future debugging session actually reads to see
    WHY a given checkpoint file is considered valid for its filename. It's also what
    _validate_checkpoint_manifest checks on load, as defense-in-depth beyond the
    filename's own self-encoded hash -- an explicit --checkpoint-file path is a
    human-supplied filename that does NOT self-encode a hash at all, so this manifest
    is the ONLY identity check that path ever gets."""
    manifest = {"content_hash": content_hash, "params": params,
                "written_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with open(_checkpoint_manifest_path(checkpoint_path), "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True, default=str)


def _validate_checkpoint_manifest(checkpoint_path, params, content_hash):
    """Hard-refuse-on-mismatch gate (2026-09-10 structural fix, recommendation #2 from
    the checkpoint entry_timing incident review: "replace the filename allowlist with a
    content hash... validated on load with a hard refuse-on-mismatch"). A checkpoint
    file existing under the expected content-hashed default filename is already strong
    evidence of a match (the filename IS derived from the hash), but this revalidates
    against a written manifest as defense-in-depth -- e.g. an explicit --checkpoint-file
    path is a human-supplied filename that does not self-encode a hash, so this is the
    only identity check that path ever gets. Raises RuntimeError -- never silently
    recomputes or falls through -- on a missing manifest (unverifiable, treated the same
    as a real mismatch) or a hash mismatch."""
    manifest_path = _checkpoint_manifest_path(checkpoint_path)
    if not os.path.exists(manifest_path):
        raise RuntimeError(
            f"Checkpoint manifest missing: {manifest_path!r} -- refusing to load "
            f"{checkpoint_path!r} without provenance. Delete the checkpoint file and "
            f"re-run (fresh compute + a real manifest), or restore the manifest if it "
            f"was deleted by mistake.")
    with open(manifest_path) as f:
        manifest = json.load(f)
    if manifest.get("content_hash") != content_hash:
        raise RuntimeError(
            f"Checkpoint identity mismatch for {checkpoint_path!r}: manifest hash "
            f"{manifest.get('content_hash')!r} != this run's resolved hash {content_hash!r}. "
            f"manifest params={manifest.get('params')} vs this run's params={params}. "
            f"Refusing to load a checkpoint that doesn't match this run's real resolved "
            f"parameters -- see docs/backlog_cache.md's 'checkpoint entry_timing incident' "
            f"architectural item.")


def _should_load_checkpoint(args, seed_task, checkpoint_path):
    """Extracted 2026-09-03 (Runlist Step 2, real live incident fix -- checkpoint
    entry_timing incident, docs/backlog_cache.md) so the load gate is a small, directly
    testable predicate instead of inline logic buried in run_one_fixed_sl's own large
    body. Opt-in: an explicit --checkpoint-file or --use-checkpoint is now REQUIRED before
    the auto-computed default path's mere on-disk existence can trigger a load -- every
    real invocation of this script registers a real campaign row (main()'s unconditional
    register_campaign call), so there is no separate 'just testing' mode to detect other
    than an explicit flag. Seed mode and --resume-from-top100 never load a checkpoint
    regardless of the opt-in flags (see their own callers' docstrings for why).

    `bool(args.checkpoint_file)` (truthy, not `is not None`) -- paired-review LOW finding:
    an `is not None` check would let `--checkpoint-file ""` fall through to the SAME
    default-path load this opt-in exists to gate (checkpoint_path's own resolution,
    `args.checkpoint_file or os.path.join(...)`, already falls back to the default path
    for an empty string) -- matching that same truthy convention closes the gap instead of
    reopening it via a deliberately-odd empty-string invocation."""
    return (bool(args.checkpoint_file or args.use_checkpoint)
            and seed_task is None and not args.resume_from_top100
            and os.path.exists(checkpoint_path))


def _should_save_checkpoint(args, seed_task):
    """Extracted alongside _should_load_checkpoint for the same testability reason.
    Real fix, 2026-09-03 (Runlist Step 3): previously only checked `seed_task is None` --
    a --resume-from-top100 run's own deliberately-narrowed df_full (built from a pre-
    filtered top-100 snapshot, documented as able to miss a real island entirely) could
    overwrite the SHARED default checkpoint path a later real full-campaign run would
    load, silently promoting candidates derived from the crippled pool. Mirrors the load
    gate's own --resume-from-top100 exclusion (must never diverge from it)."""
    return seed_task is None and not args.resume_from_top100


def run_one_fixed_sl(pool, strategy_name, fixed_sl, version, args):
    """Real per-fixed_sl Phase1+Phase2+Phase2.5+candidate-write body -- extracted
    2026-08-29 (Task #6 follow-up, planner dispatch) from what used to be main()'s
    own single-fixed_sl body, so main() can loop this over multiple fixed_sl values
    within one shared pool. Also owns this fixed_sl's own sweep_run_log start/finish
    logging (piece #1) -- main()'s own resumability check (already-finished row ==
    skip) happens BEFORE this function is even called, so every real call here is a
    genuine new unit of work."""
    _run_log_t0 = time.time()
    _run_log_id = _log_sweep_run_start(TICKER, strategy_name, fixed_sl, WINDOWS, version)

    grid = campaign_config.STRATEGIES[strategy_name]
    TAKE_PROFITS = grid["take_profits"]
    STOP_LOSSES = grid["stop_losses"]
    TRAIL_PCTS = _trail_pcts_for_strategy(strategy_name, grid)



    _job_tmp = os.path.join(os.environ["CLAUDE_JOB_DIR"], "tmp") if "CLAUDE_JOB_DIR" in os.environ else "/tmp"
    checkpoint_path = args.checkpoint_file or os.path.join(
        _job_tmp, _build_checkpoint_filename(strategy_name, fixed_sl, args,
                                              TAKE_PROFITS, STOP_LOSSES, TRAIL_PCTS))
    # Resolved once here (not re-derived at load/save time) so the load-time validation
    # and the save-time manifest are guaranteed to agree on what this run's real identity
    # is, even though seed mode later locally reassigns TAKE_PROFITS/TRAIL_PCTS below --
    # moot for seed mode specifically since it never loads/saves a checkpoint at all (see
    # _should_load_checkpoint/_should_save_checkpoint), but keeping one resolved value
    # avoids a second, potentially-diverging recomputation entirely.
    _checkpoint_params = _checkpoint_identity_params(strategy_name, fixed_sl, args,
                                                      TAKE_PROFITS, STOP_LOSSES, TRAIL_PCTS)
    _checkpoint_hash = _checkpoint_identity_hash(_checkpoint_params)

    asset_bh, spy_bh = compute_bh_returns(TICKER, start_date=START, end_date=END, data_source=DATA_SOURCE)
    if spy_bh is None:
        raise SystemExit(f"compute_bh_returns returned None for {TICKER}/{DATA_SOURCE} -- no derived build.")

    _seed_task = getattr(args, "_seed_task", None)
    if _seed_task is not None:
        # Seed-mode TRAIL_PCTS override (2026-08-29, paired review round 4, CONFIRMED by
        # both reviewers -- fixes a real regression round 3 itself introduced): REPLACE
        # TRAIL_PCTS entirely with the seed's own tpct, same pattern as WINDOWS/
        # Z_THRESHOLDS/HOLD_TIME_CAPS, rather than appending it to the end of the
        # standard grid. Round 3's `TRAIL_PCTS = TRAIL_PCTS + [_seed_task[5]]` fixed
        # Phase2's island-detection empty-slice bug but broke Phase2.5's neighbor-slicing
        # (`idx = TRAIL_PCTS.index(tpct_c); tpct_neighbors = TRAIL_PCTS[idx-1:idx+2]`),
        # which assumes LIST-POSITION adjacency == NUMERIC adjacency -- an appended value
        # at the end of the list has a numerically-distant list neighbor. Concrete
        # example: DPST id=53 (tpct=13.0) made TRAIL_PCTS=[1,2,3,4,5,6,7,13], idx=7,
        # neighbors=[7.0, 13.0] -- Phase2.5 built a whole cliffbox at tpct=7.0 (6 grid-
        # steps away, zero real Phase1/Phase2 support), and those rows could outrank and
        # get PROMOTED into candidate_nodes instead of the seed's own tpct=13.0 rows.
        # REAL affected population (corrected 2026-08-29, round-5 relay correction --
        # round 4 mischaracterized this): TrailingBothZScoreBreakout off-grid-tpct seeds
        # ONLY, 32 of 105 non-archived TB rows, all state='paper'/'dry_run'/'research',
        # ZERO live nodes -- DPST id=53 above is the real representative example.
        # TrailingExitZScoreBreakout was NEVER actually affected by round 3's append
        # bug despite the earlier writeup claiming otherwise: `_trail_pcts_for_strategy`
        # short-circuits to the literal list `[0.0]` for that strategy (never reads a
        # real multi-value grid at all, since TrailingExit has no real fourth axis), so
        # `0.0 not in [0.0]` was always False and the append branch never fired for any
        # TrailingExit seed, live or otherwise.
        #
        # REPLACING (not appending) makes idx always 0, tpct_neighbors always
        # [seed_tpct], correct for both the off-grid case above and the always-on-grid
        # TrailingExit case. Also note (round-5, contextual review): for the 73 TB seeds
        # that WERE already on-grid before this fix (including 11 real live nodes --
        # RETL, TMF, ETHU, OILU, HIBL, JNUG, KORU, LABU, NUGT, DFEN, WEBL), this override
        # is a deliberate, real behavior change vs the ORIGINAL (pre-round-3) code: it
        # narrows Phase2.5's explored tpct neighborhood from the two numerically-adjacent
        # standard-grid values down to just the seed's own single value -- an intentional
        # trade-off consistent with WINDOWS/Z_THRESHOLDS/HOLD_TIME_CAPS all being pinned
        # to their seed's exact value too (seed mode's whole point is reproducing ONE
        # real node's exact config, not exploring a neighborhood around it on every
        # axis), not something needing its own separate fix.
        TRAIL_PCTS = [_seed_task[5]]
        print(f"Seed mode: TRAIL_PCTS override -> {TRAIL_PCTS} (replaces standard grid "
              f"{_trail_pcts_for_strategy(strategy_name, grid)}, not appended -- round-4 fix)")

        # TAKE_PROFITS override (2026-08-30, paired-review MEDIUM finding against the new
        # arm_pct backfill): same "reproduce ONE real node's exact config" pin every other
        # axis already gets above -- TAKE_PROFITS was the one axis seed mode left
        # un-pinned, which the window/z backfill never noticed (it's naturally inert in
        # seed mode once window/z are both pinned to one value -- there's nothing left to
        # be "missing") but the new arm_pct backfill is NOT inert against: with the full
        # 14-value grid still in play, a seed run would see ~11 of 14 grid arms as
        # "missing" and backfill-promote real, non-seed candidates under the seed's own
        # `-seed<id>` version -- silently defeating this mode's whole point. Confirmed via
        # a real seed-mode smoke test before this fix: 6 extra arm-backfilled candidates
        # got promoted alongside the seed's own real 6 island candidates.
        TAKE_PROFITS = [_seed_task[0]]
        print(f"Seed mode: TAKE_PROFITS override -> {TAKE_PROFITS} (replaces standard grid "
              f"{grid['take_profits']}, not appended)")

    if _seed_task is not None:
        # Seed-mode smoke test (2026-08-29): Phase1 reduced to exactly the one real
        # live node's reverse-mapped task tuple, bypassing the full grid cross-product
        # entirely. Everything downstream (Phase2 mesh, Phase2.5-cliffbox, promotion)
        # is untouched -- it already just consumes whatever ends up in df1.
        phase1_tasks = [_seed_task]
        print(f"Seed mode: Phase1 task list reduced to ONE cell: {_seed_task} "
              f"(bypasses the {len(TAKE_PROFITS) * len(STOP_LOSSES) * len(HOLD_TIME_CAPS) * len(TRAIL_PCTS) * len(Z_THRESHOLDS) * len(WINDOWS):,}-cell full grid)")
    else:
        phase1_tasks = build_phase1_tasks_grid(
            Z_THRESHOLDS, WINDOWS, TAKE_PROFITS, STOP_LOSSES, HOLD_TIME_CAPS, TRAIL_PCTS)

    # Seed mode never uses the checkpoint at all (2026-08-29, paired review round 4,
    # CONFIRMED by both reviewers): the round-3 repeatability fix bypassed sweep_run_log's
    # dedup check, but the default checkpoint path is STABLE across identical seed
    # invocations (keyed on _seed<id>), so it still silently short-circuited a repeat run
    # -- loading the PRIOR run's df_full and only re-running Phase2.5+promotion against
    # stale upstream data. If the kernel/strategy changed between runs (the actual reason
    # to rerun a smoke test), this would silently validate new code against old Phase1/
    # Phase2 output. Seed mode's whole cost profile (Phase1=1 cell, Phase2=~81 cells) is
    # cheap enough that there's no expensive cost to amortize -- the checkpoint's entire
    # justification for existing -- so it's skipped (both load AND save) entirely.
    #
    # Opt-in load gate (2026-09-03, real live incident fix -- checkpoint entry_timing
    # incident, docs/backlog_cache.md's Runlist Step 2): `args.checkpoint_file is not None
    # or args.use_checkpoint` added -- previously this branch fired on the auto-computed
    # DEFAULT path's mere existence, with no signal the caller actually wanted checkpoint
    # behavior at all. Real incident: the first v6.6 (close-entry) job silently loaded an
    # `open_check` checkpoint left over from the prior evening's real v6.5 run, skipping
    # Phase1+Phase2 entirely for what was meant to be a genuine close-entry sweep --
    # caught live, before any candidate_nodes/backtest_phase1_insurance rows were written,
    # but only because a human was reading stdout in real time. Every real invocation of
    # this script registers a real campaign row (main()'s unconditional register_campaign
    # call, no separate 'just testing' mode exists) -- so 'opt-in for any run registering a
    # real campaign' means every default-path load now requires an explicit ask.
    _checkpoint_saved = False  # overridden True only if the compute branch's save fires
    if _should_load_checkpoint(args, _seed_task, checkpoint_path):
      # Hard-refuse-on-mismatch gate (2026-09-10 structural fix) -- raises before any
      # read if the manifest is missing or its hash doesn't match this run's real
      # resolved params. Replaces the old "filename existing is proof enough" trust
      # model that caused the entry_timing incident.
      _validate_checkpoint_manifest(checkpoint_path, _checkpoint_params, _checkpoint_hash)
      _checkpoint_source = "loaded"
      t0 = time.time()
      df_full = pd.read_parquet(checkpoint_path)
      t1 = t2 = t3 = time.time()
      phase1_rows, phase2_rows = [], []  # total-cells-computed count below stays honest
      print(f"CHECKPOINT: loaded df_full ({len(df_full):,} rows, deduped Phase1+Phase2) "
            f"from {checkpoint_path} in {t1 - t0:.2f}s -- skipping Phase1 AND Phase2 "
            f"dispatch entirely (dev-iteration checkpoint, NOT a production artifact). "
            f"Manifest hash validated: {_checkpoint_hash}.")
    else:
      _checkpoint_source = "computed"
      if args.resume_from_top100:
          t0 = time.time()
          with sqlite3.connect(DB_PATH) as conn:
              df1 = pd.read_sql("""
                  SELECT take_profit, stop_loss, max_hold_hours, window,
                         z_score_threshold, trail_sell_pct, cagr, trades
                  FROM backtest_phase1_insurance
                  WHERE version=? AND ticker=? AND strategy=?
              """, conn, params=(version, TICKER, strategy_name))
          t1 = time.time()
          print(f"RESUME MODE: loaded {len(df1):,} rows from persisted top-100 snapshot "
                f"in {t1 - t0:.2f}s -- skipping Phase1 dispatch entirely "
                f"({len(phase1_tasks):,} cells NOT recomputed). "
                f"pick_island_centers will run against only these {len(df1):,} rows, "
                f"not the full grid -- real correctness risk, that's the point of this test.")
          phase1_rows = []  # for the final total-cells-computed count below
      else:
          print(f"Phase1-coarse (in-memory): {len(phase1_tasks):,} cells, "
                f"windows={WINDOWS}, z={Z_THRESHOLDS}, version={version} (not written anywhere)")
          t0 = time.time()
          phase1_rows = _dispatch(pool, phase1_tasks, TICKER, strategy_name, version, fixed_sl, spy_bh,
                                   desc="Phase1-coarse (in-memory)")
          t1 = time.time()
          print(f"[{datetime.now().strftime('%H:%M:%S')}] PROGRESS: Phase1 done ticker={TICKER} strategy={strategy_name} fixed_sl={fixed_sl}: "
                f"{len(phase1_rows):,} rows in {t1 - t0:.1f}s "
                f"({len(phase1_rows) / max(t1 - t0, 0.001):.0f} nodes/sec)")

          # Guard BEFORE building df1 (2026-08-29, paired review): if every Phase1 cell
          # failed (non-SUCCESS status), phase1_rows is [] and pd.DataFrame([]) has no
          # "trades" column at all -- df1["trades"] below would raise a raw KeyError
          # instead of the friendly sanity-check SystemExit further down, which is
          # especially unreachable in seed mode's own 1-cell case (this is exactly the
          # case that check exists to catch).
          if not phase1_rows:
              raise SystemExit(f"Phase1 dispatch returned ZERO rows (no SUCCESS statuses "
                                f"among {len(phase1_tasks):,} cells) -- check _dispatch's "
                                f"non-SUCCESS status counts above for the failure reason.")

          df1 = pd.DataFrame(phase1_rows)
          df1 = df1[df1["trades"] > 0]

      # Sanity check: fewer than 100 successful Phase1 cells out of 164,640 means
      # something is badly wrong upstream (data load failure, wrong ticker/window,
      # near-total SIM_ERROR/EMPTY rate) -- not a legitimate sparse-grid outcome.
      # Seed mode is a deliberate exception: phase1_tasks is exactly 1 cell by design,
      # so the real bar there is just "did that one cell succeed at all" (trades>0),
      # not >=100.
      if _seed_task is not None:
          if len(df1) < 1:
              raise SystemExit(f"Seed mode Phase1 sanity check FAILED: the seed cell "
                                f"{_seed_task} produced zero successful rows (trades>0) -- "
                                f"the real live node itself failed to backtest. Check "
                                f"_dispatch's non-SUCCESS status counts above.")
      elif len(df1) < 100:
          raise SystemExit(f"Phase1 sanity check FAILED: only {len(df1)} successful cells "
                            f"(need >=100) -- something is wrong upstream, not a real "
                            f"sparse-data outcome. Check _dispatch's non-SUCCESS status counts above.")

      # Insurance snapshot: written for real into backtest_phase1_insurance under the
      # bench- version prefix. Matters more here than in the disk-based backtest_phase1
      # design -- Phase1's raw grid is never written anywhere else in this script, so
      # once the process exits nothing survives unless something is explicitly saved.
      #
      # NOT a naive global top-N (2026-08-27 fix, found in design discussion): a plain
      # top-N-by-cagr sort can silently omit an entire real island if the global top-N
      # happens to cluster around one strong region -- pick_island_centers (which sees
      # the FULL grid) would still find that other island correctly for Phase2, but the
      # insurance snapshot -- whose whole purpose is to let a future session re-debug
      # pick_island_centers's choices -- would have zero evidence it existed. Fixed by
      # unioning global top-1000-by-cagr with a WIDE island-detection pass (n=30, well
      # above the real N_ISLANDS=3) so up to 30 distinct regions are represented, not
      # just whichever one cell ranks highest overall. Still capped/lossy in principle
      # (an unlikely 31st+ region could still be missed), but size was never the
      # constraint here (union stays well under ~2000 rows, still negligible) -- this
      # is about actually satisfying the snapshot's stated purpose, not about a byte
      # budget. Skipped in resume mode -- df1 IS already the snapshot, re-snapshotting
      # a subset of itself is a no-op (INSERT OR IGNORE would just skip every row).
      if args.resume_from_top100:
          print("Phase1 insurance snapshot: skipped (resume mode -- df1 already IS "
                "the persisted snapshot, nothing new to write)")
      else:
          t_ins0 = time.time()
          global_top = df1.sort_values("cagr", ascending=False).head(1000)
          wide_centers = pick_island_centers(df1, n=30, rank_col="cagr")
          region_rows = []
          for tp_c, sl_c in wide_centers:
              region = df1[(df1["take_profit"] - tp_c).abs().le(FINE_RADIUS)
                           & (df1["stop_loss"] - sl_c).abs().le(FINE_RADIUS)]
              region_rows.append(region.sort_values("cagr", ascending=False).head(10))
          insurance_df = pd.concat([global_top] + region_rows, ignore_index=True)

          # Window/z backfill (2026-08-30, planner dispatch): same blind-spot class the
          # candidate_nodes promotion pool had before find_missing_window_z_top_n was built
          # for it (see that function's docstring) -- global_top/wide_centers above both pool
          # every (window, z) combo together, so a combo can end up with zero representation
          # in the insurance snapshot even though pick_island_centers itself (run separately,
          # against the FULL grid, for the real Phase2 seeding) would still find it fine. Only
          # the insurance snapshot's OWN stated purpose -- letting a future session re-debug
          # island detection after the fact -- would have zero evidence such a combo ever
          # existed. Unlike the promotion-pool fix, no cliffbox/Phase2.5 concern applies here:
          # insurance is explicitly a raw, unrefined Phase1-only snapshot by design already
          # (see the "NOT a naive global top-N" comment above), so a plain top-2-per-missing-
          # combo union straight from df1 is the full fix, no seed/final two-stage split
          # needed. `tb_cols=["cagr"]` (not the fuller GT_CANDIDATE_TIEBREAK-based `tb_cols`
          # used elsewhere in this function) matches this block's OWN existing sort
          # convention -- global_top/region_rows above both sort purely on cagr too.
          present_combos_ins = {(int(w), float(z)) for w, z in insurance_df[
              ["window", "z_score_threshold"]].drop_duplicates().itertuples(index=False)}
          missing_combos_ins, backfill_ins_rows = find_missing_window_z_top_n(
              present_combos_ins, WINDOWS, Z_THRESHOLDS, df1, tb_cols=["cagr"], tb_asc=[False],
              top_n=2)
          backfill_ins_flat = [row for rows in backfill_ins_rows.values() for row in rows]
          if backfill_ins_flat:
              insurance_df = pd.concat([insurance_df, pd.DataFrame(backfill_ins_flat)],
                                        ignore_index=True)
          if missing_combos_ins:
              zero_evidence_ins = [c for c, rows in backfill_ins_rows.items() if not rows]
              print(f"Phase1 insurance backfill: {len(missing_combos_ins)} window/z combo(s) "
                    f"with zero representation in the top-1000/region union -- adding "
                    f"{len(backfill_ins_flat)} extra row(s) (top-2 each, where evidence "
                    f"existed): {missing_combos_ins}")
              if zero_evidence_ins:
                  print(f"  {len(zero_evidence_ins)} of those had NO evidence at all (every "
                        f"cell had trades=0): {zero_evidence_ins}")

          insurance_df = insurance_df.drop_duplicates(
              subset=["take_profit", "stop_loss", "max_hold_hours", "window",
                      "z_score_threshold", "trail_sell_pct"])
          insurance_rows = insurance_df.to_dict("records")
          if _seed_task is not None:
              _clear_prior_seed_mode_table_rows(
                  "backtest_phase1_insurance", strategy_name, version, TICKER, fixed_sl)
          n_written = _insert_phase1_insurance_rows(
              insurance_rows, strategy_name, version, TICKER, fixed_sl, ENTRY_TIMING)
          t_ins1 = time.time()
          print(f"Phase1 insurance snapshot: {len(insurance_rows)} rows "
                f"(top-1000 + {len(wide_centers)}-region coverage), {n_written} written to "
                f"backtest_phase1_insurance in {t_ins1 - t_ins0:.2f}s (version={version})")

      # Island-center detection + fine mesh, run for N_GENERATIONS passes (2026-08-31,
      # planner dispatch -- closes the gap docs/plans/ground_truth_kernel_rebuild.md:
      # 290-295 flagged: this pipeline previously ran exactly ONE Phase2-island pass,
      # missing a true peak that sits just outside the first pass's +-FINE_RADIUS window
      # but is reachable by hopping generation to generation, the same failure mode
      # legacy's multi-generation Phase2 (config.json execution.max_generations,
      # run_phase2_island_ground_truth's own docstring) already mitigates on the disk-
      # based path. Each generation re-derives pick_island_centers off df_gen_pool --
      # df1 (raw Phase1) PLUS every prior generation's own Phase2 rows, accumulated
      # in-memory exactly like Phase1->Phase2->Phase2.5 already accumulate today (no mid-
      # sweep DB write, matching this module's whole in-memory design) -- so a later
      # generation can walk toward a peak the first pass's mesh didn't reach.
      #
      # `explored_tasks` is this in-memory pipeline's equivalent of legacy's cache-lookup-
      # before-write: only cells not already dispatched in an earlier generation get
      # re-dispatched. A generation whose freshly-picked centers mesh entirely inside
      # `explored_tasks` (island centers have converged -- same picks as before) simply
      # dispatches zero new cells and no-ops. That IS the stopping signal by design --
      # no separate hardcoded early-exit/break is layered on top, matching legacy's own
      # "a generation that finds nothing new simply re-picks the same centers and cache-
      # hits its way to a fast no-op" behavior (run_phase2_island_ground_truth docstring).
      t2 = time.time()
      phase2_rows = []
      explored_tasks = set()
      df_gen_pool = df1
      for gen in range(1, N_GENERATIONS + 1):
          phase2_tasks = set()
          for z in Z_THRESHOLDS:
              for w in WINDOWS:
                  for tpct in TRAIL_PCTS:
                      df_wz = df_gen_pool[(df_gen_pool["window"] == w)
                                           & (df_gen_pool["z_score_threshold"] == z)
                                           & (df_gen_pool["trail_sell_pct"] == tpct)]
                      if df_wz.empty:
                          continue
                      centers = pick_island_centers(df_wz, n=N_ISLANDS, rank_col="cagr")
                      if len(centers) < N_ISLANDS and gen == 1:
                          print(f"  WARNING: (w={w} z={z} tpct={tpct}) only found {len(centers)} "
                                f"island(s), expected {N_ISLANDS} -- check for a data gap in "
                                f"this slice, not necessarily fatal (a real scope can "
                                f"legitimately have fewer distinct islands than N_ISLANDS).")
                      for (tp_c, sl_c) in centers:
                          for tp in range(max(1, tp_c - FINE_RADIUS), min(30, tp_c + FINE_RADIUS) + 1):
                              for sl in range(max(1, sl_c - FINE_RADIUS), min(30, sl_c + FINE_RADIUS) + 1):
                                  for hold in HOLD_TIME_CAPS:
                                      phase2_tasks.add((tp, sl, int(hold), int(w), float(z), float(tpct)))
          new_tasks = phase2_tasks - explored_tasks
          print(f"Phase2-island mesh gen {gen}/{N_GENERATIONS} (in-memory): {len(phase2_tasks):,} cells "
                f"picked, {len(new_tasks):,} new (not already explored in a prior generation) "
                f"({N_ISLANDS} islands x {len(WINDOWS)}w x {len(Z_THRESHOLDS)}z x {len(TRAIL_PCTS)} trail_pcts, +-{FINE_RADIUS} box)")
          if not new_tasks:
              print(f"  gen {gen}/{N_GENERATIONS}: 0 new cells -- island centers converged to "
                    f"already-explored territory, no-op (this is the real stopping signal, "
                    f"not treated as an error).")
              continue
          gen_rows = _dispatch(pool, new_tasks, TICKER, strategy_name, version, fixed_sl, spy_bh,
                                desc=f"Phase2-island gen{gen} (in-memory)")
          for row in gen_rows:
              row["generation"] = gen
          phase2_rows.extend(gen_rows)
          explored_tasks |= new_tasks
          if gen_rows:
              # Feeds the NEXT generation's pick_island_centers call -- accumulated
              # in-memory, never written to any table mid-sweep.
              df_gen_pool = pd.concat([df_gen_pool, pd.DataFrame(gen_rows)], ignore_index=True)
      t3 = time.time()
      print(f"[{datetime.now().strftime('%H:%M:%S')}] PROGRESS: Phase2 done ticker={TICKER} strategy={strategy_name} fixed_sl={fixed_sl}: "
            f"{len(phase2_rows):,} rows across {N_GENERATIONS} generation(s) in {t3 - t2:.1f}s "
            f"({len(phase2_rows) / max(t3 - t2, 0.001):.0f} nodes/sec)")

      # Phase2 insurance snapshot (2026-09-03): same "write-once debug aid" posture as
      # the Phase1 insurance snapshot above, applied one phase later -- persists
      # phase2_rows (Phase2's own mesh-generation output, already correctly scoped per
      # (window, z, trail_pct)) BEFORE centers25/final_centers pool it down to N_ISLANDS
      # global regions below. Same top-1000-by-cagr + wide-island-union (n=30, well
      # above the real N_ISLANDS=3) + backfill pattern as Phase1's snapshot -- see
      # _insert_phase2_insurance_rows' docstring for why this table exists at all.
      #
      # _clear_prior_seed_mode_table_rows runs UNCONDITIONALLY here (2026-09-03,
      # paired-review MEDIUM finding), not just inside the "phase2_rows non-empty"
      # branch below -- a repeat --seed-watch-list-id run that produces zero Phase2
      # rows (all generations converge to already-explored territory) would otherwise
      # leave the PRIOR run's rows in place under the identical deterministic
      # "-seed<id>" version, the exact staleness this helper exists to prevent.
      if _seed_task is not None:
          _clear_prior_seed_mode_table_rows(
              "backtest_phase2_insurance", strategy_name, version, TICKER, fixed_sl)
      if not phase2_rows:
          print("Phase2 insurance snapshot: skipped (zero Phase2 rows this run -- "
                "every generation converged to already-explored territory with no new "
                "cells, or every dispatched cell returned non-SUCCESS -- check "
                "_dispatch's status counts above to tell which)")
      else:
          t_ins2_0 = time.time()
          df2_ins = pd.DataFrame(phase2_rows)
          df2_ins = df2_ins[df2_ins["trades"] > 0]
          global_top2 = df2_ins.sort_values("cagr", ascending=False).head(1000)
          wide_centers2 = pick_island_centers(df2_ins, n=30, rank_col="cagr")
          region_rows2 = []
          for tp_c, sl_c in wide_centers2:
              region = df2_ins[(df2_ins["take_profit"] - tp_c).abs().le(FINE_RADIUS)
                                & (df2_ins["stop_loss"] - sl_c).abs().le(FINE_RADIUS)]
              region_rows2.append(region.sort_values("cagr", ascending=False).head(10))
          insurance_df2 = pd.concat([global_top2] + region_rows2, ignore_index=True)

          # (window, z, trail_sell_pct)-keyed backfill (2026-09-03, paired-review HIGH
          # finding, confirmed by both independent-cold and contextual review +
          # rebuttal): find_missing_window_z_top_n's own (window, z)-only key would
          # mark a whole (w, z) pair "present" as soon as ANY one of its trail_pct
          # values survived the top-1000/wide-island union, silently missing the other
          # trail_pct scopes at that same (w, z) -- see find_missing_window_z_tpct_
          # top_n's own docstring for the full real-scale numbers (TrailingBoth: 42
          # (w,z,tpct) scopes, phase2_rows in the hundreds of thousands, union capped
          # at ~1300 rows). Insurance-snapshot-only -- does NOT touch the production
          # Phase2.5/final-stage backfill call sites below, which stay on the
          # (window, z)-only key exactly as before.
          present_combos_ins2 = {(int(w), float(z), float(tp)) for w, z, tp in insurance_df2[
              ["window", "z_score_threshold", "trail_sell_pct"]
          ].drop_duplicates().itertuples(index=False)}
          missing_combos_ins2, backfill_ins2_rows = find_missing_window_z_tpct_top_n(
              present_combos_ins2, WINDOWS, Z_THRESHOLDS, TRAIL_PCTS, df2_ins,
              tb_cols=["cagr"], tb_asc=[False], top_n=2)
          backfill_ins2_flat = [row for rows in backfill_ins2_rows.values() for row in rows]
          if backfill_ins2_flat:
              insurance_df2 = pd.concat([insurance_df2, pd.DataFrame(backfill_ins2_flat)],
                                         ignore_index=True)
          if missing_combos_ins2:
              zero_evidence_ins2 = [c for c, rows in backfill_ins2_rows.items() if not rows]
              print(f"Phase2 insurance backfill: {len(missing_combos_ins2)} window/z/tpct "
                    f"combo(s) with zero representation in the top-1000/region union -- "
                    f"adding {len(backfill_ins2_flat)} extra row(s) (top-2 each, where "
                    f"evidence existed): {missing_combos_ins2}")
              if zero_evidence_ins2:
                  print(f"  {len(zero_evidence_ins2)} of those had NO evidence at all (every "
                        f"cell had trades=0): {zero_evidence_ins2}")

          insurance_df2 = insurance_df2.drop_duplicates(
              subset=["take_profit", "stop_loss", "max_hold_hours", "window",
                      "z_score_threshold", "trail_sell_pct"])
          insurance_rows2 = insurance_df2.to_dict("records")
          n_written2 = _insert_phase2_insurance_rows(
              insurance_rows2, strategy_name, version, TICKER, fixed_sl, ENTRY_TIMING)
          t_ins2_1 = time.time()
          print(f"Phase2 insurance snapshot: {len(insurance_rows2)} rows "
                f"(top-1000 + {len(wide_centers2)}-region coverage), {n_written2} written to "
                f"backtest_phase2_insurance in {t_ins2_1 - t_ins2_0:.2f}s (version={version})")

      # Phase2.5-CliffBox-GT (in-memory): center detection off the FULL scope
      # (Phase1 + Phase2 combined), matching run_phase25_cliff_box_ground_truth's own
      # "unrestricted, sees whatever the generation loop found" behavior -- not just
      # Phase2's mesh. Top-3-per-island candidates (same GT_CANDIDATE_TIEBREAK order),
      # cliff-boxed +-CLIFF_RADIUS around each.
      df2 = pd.DataFrame(phase2_rows)
      df_full = pd.concat([df1, df2], ignore_index=True)
      df_full = df_full[df_full["trades"] > 0]
      # dedupe on the real grid coordinate -- the same (tp,sl,hold,w,z,tpct) cell can
      # legitimately get recomputed in more than one phase (e.g. a Phase1 grid point
      # that also falls inside Phase2's mesh); real backtest_cache never double-stores
      # a cell (cache-lookup-before-write), this in-memory concat needs the same guard
      # or top-N/top-3-per-island ranking will double-count identical cells as if they
      # were distinct.
      df_full = df_full.drop_duplicates(
          subset=["take_profit", "stop_loss", "max_hold_hours", "window",
                  "z_score_threshold", "trail_sell_pct"])

      # Dev-iteration checkpoint save (not a production artifact) -- lets the NEXT
      # run skip straight to Phase2.5 instead of repaying Phase1+Phase2's ~7min.
      # Skipped entirely in seed mode (2026-08-29, round 4) -- see the load-side skip's
      # own comment above for why: seed mode is cheap enough that there's no cost to
      # amortize, and saving one would make repeat seed runs silently stale.
      #
      # `not args.resume_from_top100` added (2026-09-03, Runlist Step 3, paired-review
      # finding from the checkpoint entry_timing incident review): the LOAD guard above
      # already excludes --resume-from-top100 (`_seed_task is None and not args.
      # resume_from_top100 and os.path.exists(...)`), but this SAVE guard previously only
      # checked `_seed_task is None` -- nothing kept a --resume-from-top100 run's own
      # deliberately-narrowed df_full (built from a pre-filtered top-100 snapshot,
      # documented as able to miss a real island entirely) from overwriting the SHARED
      # default checkpoint path. Real failure scenario this closes: run --resume-from-
      # top100 once as a divergence experiment for some (ticker, strategy, fixed_sl,
      # windows, z, ...) tuple, then run the real full campaign for the identical tuple
      # the same day -- the full run would find the file, load it (this file's own load
      # guard already correctly prevented THAT specific load, matching the file's real
      # narrowed provenance), print "skipping Phase1 AND Phase2," and promote candidates
      # derived from the deliberately-crippled top-100-only pool into candidate_nodes.
      # resume-from-top100's own df_full has no legitimate reason to ever be cached.
      if _should_save_checkpoint(args, _seed_task):
          # Manifest written BEFORE the parquet (paired-review LOW finding: the reverse
          # order risks a kill leaving an orphaned parquet with no manifest, which every
          # future run would then hard-refuse to load until manually deleted). This order
          # is self-healing instead: a kill between the two leaves a manifest but no
          # parquet, so _should_load_checkpoint's os.path.exists(checkpoint_path) check on
          # the PARQUET still correctly reports "nothing to load" and the next run just
          # recomputes and overwrites both files cleanly.
          os.makedirs(os.path.dirname(checkpoint_path), exist_ok=True)
          _write_checkpoint_manifest(checkpoint_path, _checkpoint_params, _checkpoint_hash)
          df_full.to_parquet(checkpoint_path)
          _checkpoint_saved = True
          print(f"Checkpoint saved: {checkpoint_path} ({len(df_full):,} rows, "
                f"manifest hash {_checkpoint_hash}).")
      elif _seed_task is not None:
          print("Seed mode: checkpoint save skipped (always re-runs Phase1+Phase2 for real).")
      else:
          print("--resume-from-top100: checkpoint save skipped (df_full built from a "
                "pre-filtered top-100 snapshot, not the full grid -- must never overwrite "
                "the shared default checkpoint path a real full-campaign run would load).")

    # Checkpoint provenance record (2026-09-10 structural fix, recommendation #3 from the
    # checkpoint entry_timing incident review): recorded immediately here, not deferred to
    # _log_sweep_run_finish, so a crash anywhere after this point still leaves the real
    # computed-vs-loaded provenance visible in sweep_run_log -- a contaminated/reused-
    # checkpoint run was previously undetectable from the DB after the fact, only
    # inferable from an anomalously small elapsed_s.
    #
    # checkpoint_hash/checkpoint_path are only recorded when a checkpoint was ACTUALLY
    # loaded or saved (paired-review MEDIUM finding: seed mode and --resume-from-top100
    # both skip load AND save entirely, and in seed mode the hash would additionally
    # reflect the pre-seed-override TAKE_PROFITS/TRAIL_PCTS, not the seed's real pinned
    # values -- logging it anyway would falsely group a seed/narrowed-pool run with an
    # unrelated real full-grid run under the same checkpoint_hash in a future forensic
    # query). checkpoint_source stays 'computed' either way -- that part is literally
    # accurate (Phase1/Phase2 genuinely ran) regardless of whether the result got cached.
    _checkpoint_used = _checkpoint_source == "loaded" or _checkpoint_saved
    _log_sweep_run_checkpoint(
        _run_log_id, _checkpoint_source,
        _checkpoint_hash if _checkpoint_used else None,
        checkpoint_path if _checkpoint_used else None)

    # --- Everything below runs regardless of which branch built df_full ---

    # Centers/seed cells for WHERE to cliff-box: pre-2.5 data only (Phase1+Phase2),
    # matching run_phase25_cliff_box_ground_truth's own input at the point it runs
    # (Phase2.5-CliffBox-GT rows don't exist yet). This part does NOT decide the
    # final 9 candidates -- see below.
    centers25 = pick_island_centers(df_full, n=N_ISLANDS, rank_col="cagr")
    if len(centers25) < N_ISLANDS:
        print(f"  WARNING (Phase2.5 seed detection): only found {len(centers25)} island(s) "
              f"across the full scope, expected {N_ISLANDS} -- check df_full for a real gap.")
    tb_cols = ["cagr"] + [("trail_sell_pct" if c == "tpct" else c) for c, _ in GT_CANDIDATE_TIEBREAK]
    tb_asc = [False] + [asc for _, asc in GT_CANDIDATE_TIEBREAK]

    phase25_tasks = set()
    seed_count = 0
    # seed_candidate_rows (2026-09-03): the actual seed candidate rows themselves (not
    # phase25_tasks' own expanded cliffbox contents -- same "expansion inflates coverage"
    # trap seed_arms_seeded below already avoids on the take_profit axis) -- feeds
    # top_up_window_z_island_quota's per-(window,z) coverage check below.
    seed_candidate_rows = []
    # seed_arms_seeded (2026-08-30, paired-review MEDIUM finding, both independent-cold
    # and contextual review of the arm_pct backfill diff): tracks the ACTUAL take_profit
    # value of every real seed cell fed into cliffbox_tasks_for_cell -- NOT derived from
    # phase25_tasks's own expanded contents (see the arm-axis seed-stage backfill below
    # for why that shortcut, which the window/z backfill safely uses for window/z, is
    # WRONG on the take_profit axis: cliffbox_tasks_for_cell varies tp over
    # tp_c +-CLIFF_RADIUS, so one seed at tp=4 would make {t[0] for t in phase25_tasks}
    # falsely report arms 2,3,4,5,6 as all "already seeded," suppressing real backfill
    # seeding for a genuinely under-covered arm that happens to fall in another seed's
    # cliffbox neighbor band).
    seed_arms_seeded = set()
    for tp_c, sl_c in centers25:
        region = df_full[(df_full["take_profit"] - tp_c).abs().le(FINE_RADIUS)
                          & (df_full["stop_loss"] - sl_c).abs().le(FINE_RADIUS)]
        if region.empty:
            continue
        region = region.sort_values(tb_cols, ascending=tb_asc)
        top_cagr = region.iloc[0]["cagr"]
        if pd.isna(top_cagr) or top_cagr <= PHASE25_ISLAND_CLIFFBOX_CAGR_MIN:
            print(f"  island(TP={tp_c} SL={sl_c}): top cagr={top_cagr} -- skipped (<= "
                  f"{PHASE25_ISLAND_CLIFFBOX_CAGR_MIN} or NaN)")
            continue
        for _, cand in region.head(3).iterrows():
            seed_count += 1
            seed_arms_seeded.add(int(cand["take_profit"]))
            seed_candidate_rows.append(cand)
            phase25_tasks |= cliffbox_tasks_for_cell(cand, TRAIL_PCTS)

    print(f"\nPhase2.5-cliffbox (in-memory): {seed_count} seed cells across "
          f"{len(centers25)} island(s), {len(phase25_tasks):,} cliff-box cells to verify "
          f"(pre-backfill)")

    # Window/z backfill -- SEED stage (2026-08-30, planner dispatch, corrected same day after
    # catching a real gap in the first version of this fix): the normal per-island loop above
    # only seeds phase25_tasks for (window, z) combos that happen to be represented among the
    # top-N_ISLANDS islands' top-3 picks. A combo with none is invisible to Phase2.5's cliffbox
    # refinement entirely -- if backfilling only happened AFTER Phase2.5 (the original, wrong
    # version of this fix), a backfilled candidate would be promoted straight from the raw,
    # unrefined Phase1/Phase2 grid with NO real cliff-safety verification (worst_neighbor_cagr
    # would degenerate to `== cagr` off a single self-referential neighbor) -- exactly the kind
    # of unverified node the whole cliff-safety mechanism exists to prevent, and worse than not
    # backfilling at all, since every OTHER promoted candidate DOES carry a real verdict.
    #
    # Fix: detect missing combos HERE (from `phase25_tasks`'s own (window, z) coverage, i.e.
    # whatever actually got seeded above -- cheaper and exactly equivalent to tracking it
    # separately during the loop), then for each missing combo pull its own top-2 raw cells
    # from df_full (same source the normal loop uses) and feed them into the SAME cliffbox
    # expansion (`cliffbox_tasks_for_cell`) as ordinary seeds -- no separate bypass path. This
    # densifies df_final around them before the final candidate-selection stage below ever
    # runs, so their eventual cliff-safety verdict is real, not degenerate.
    # Cost note (2026-08-30, paired-review MEDIUM finding, contextual review of this diff):
    # the ~1.73x downstream-Phase4/5 cost estimate the user already approved was framed
    # against the ORIGINAL final-stage-only design; this seed-stage correction adds a
    # second, larger, previously-uncosted expense -- each backfill seed gets a full
    # cliffbox expansion (~225 cells at CLIFF_RADIUS=2), so ~11 missing combos x 2 seeds
    # can roughly double-to-triple Phase2.5's own dispatch size on top of its normal
    # ~9-seed baseline, before any Phase4/5 work. Applying the SAME
    # PHASE25_ISLAND_CLIFFBOX_CAGR_MIN skip the normal per-island loop above already uses
    # (line ~1298) caps this: a missing combo whose own top cell is <= that floor (or NaN)
    # skips the expensive cliffbox expansion here, but is NOT excluded from final-stage
    # promotion below -- that stays floor-free by design (backfill promotion is
    # deliberately unfiltered; this is a COMPUTE-cost guard on Phase2.5, not a promotion
    # filter). A combo skipped here still gets promoted at final stage from whatever
    # (possibly coarser, unrefined) data df_final already has for it -- same posture the
    # ORIGINAL per-island path already accepts for a below-floor island region.
    # top_up_window_z_island_quota (2026-09-03, real gap found via Opus paired-review
    # challenge on the original diversify-by-island fix -- see that function's own
    # docstring): UNCONDITIONAL per-(window,z) quota, not gated on "this combo has zero
    # representation" -- a combo with ONE seed already present (via normal N_ISLANDS=3
    # pooled selection) can still be missing a SECOND real, distinct island in that same
    # combo, which find_missing_window_z_top_n's presence-only gate would never catch.
    # present_df built from the actual seed candidate rows themselves (seed_candidate_
    # rows), not phase25_tasks' own expanded cliffbox contents -- same "expansion
    # inflates coverage" trap seed_arms_seeded already avoids on the take_profit axis
    # (see that variable's own comment above).
    seed_present_df = pd.DataFrame(seed_candidate_rows) if seed_candidate_rows else pd.DataFrame()
    combos_topped_up_seed, backfill_seed_rows, combos_zero_evidence_seed = top_up_window_z_island_quota(
        seed_present_df, WINDOWS, Z_THRESHOLDS, df_full, tb_cols, tb_asc, top_n=2)
    backfill_seed_count = 0
    backfill_seed_skipped_low_cagr = []
    for combo, rows in backfill_seed_rows.items():
        for cand in rows:
            if pd.isna(cand["cagr"]) or cand["cagr"] <= PHASE25_ISLAND_CLIFFBOX_CAGR_MIN:
                backfill_seed_skipped_low_cagr.append(combo)
                continue
            seed_arms_seeded.add(int(cand["take_profit"]))
            phase25_tasks |= cliffbox_tasks_for_cell(cand, TRAIL_PCTS)
            backfill_seed_count += 1
    if combos_topped_up_seed:
        print(f"Window/z island-quota top-up (seed stage): {len(combos_topped_up_seed)} combo(s) "
              f"had at least one of their own real islands under-represented (unconditional "
              f"per-combo quota check, not just zero-representation combos) -- adding "
              f"{backfill_seed_count} extra seed cell(s) (from raw Phase1+Phase2 data) into "
              f"the SAME cliffbox sweep so they get real refinement + cliff-safety "
              f"verification, not a bypass: {combos_topped_up_seed}")
        if backfill_seed_skipped_low_cagr:
            print(f"  {len(backfill_seed_skipped_low_cagr)} seed cell(s) skipped cliffbox "
                  f"expansion (top cagr <= {PHASE25_ISLAND_CLIFFBOX_CAGR_MIN} or NaN, same floor "
                  f"the normal per-island loop uses) -- still eligible for final-stage "
                  f"promotion from unrefined data, just not densified: {backfill_seed_skipped_low_cagr}")
        print(f"  Cliff-box cells to verify after backfill: {len(phase25_tasks):,} total")
    if combos_zero_evidence_seed:
        print(f"  {len(combos_zero_evidence_seed)} window/z combo(s) have NO evidence at all "
              f"(every cell had trades=0, or none computed) -- can't be topped up: "
              f"{combos_zero_evidence_seed}")

    # arm_pct (take_profit) backfill -- SEED stage (2026-08-30, planner dispatch): same
    # two-stage requirement as the window/z backfill above, on a different axis -- a
    # missing take_profit value must ALSO get real Phase2.5 cliffbox refinement before
    # final-stage promotion, not a bypass straight from raw Phase1/Phase2 data (identical
    # reasoning to the window/z seed-stage block above; see that block's own comment for
    # the full "why seed stage, not just final stage" writeup -- not repeated here).
    # Real gap this closes: see find_missing_arm_top_n's own docstring (ETHU's real true
    # best SAFE overlay winner was only found via an island's neighbor search, never as
    # its own independently-detected island center). Same PHASE25_ISLAND_CLIFFBOX_CAGR_MIN
    # compute-cost floor applies here too, for the same reason (promotion itself stays
    # floor-free; this only guards the expensive cliffbox expansion).
    #
    # seed_arms_seeded (NOT `{t[0] for t in phase25_tasks}`) -- 2026-08-30, paired-review
    # MEDIUM finding (both independent-cold and contextual review, converging
    # independently): the window/z backfill's own `seed_present_combos = {(t[3], t[4])
    # for t in phase25_tasks}` a few lines up is safe because cliffbox_tasks_for_cell
    # never varies window/z within one seed's expansion -- but it DOES vary take_profit
    # over tp_c +-CLIFF_RADIUS, so the same shortcut on this axis would falsely count a
    # neighbor-band arm as "already seeded" even though it was never its own seed,
    # suppressing real backfill seeding for it. `seed_arms_seeded` (built directly from
    # each seed loop's own `cand["take_profit"]`, above) has no such radius-inflation.
    missing_arms_seed, backfill_arm_seed_rows = find_missing_arm_top_n(
        seed_arms_seeded, TAKE_PROFITS, df_full, tb_cols, tb_asc, top_n=3)
    backfill_arm_seed_count = 0
    backfill_arm_seed_skipped_low_cagr = []
    for arm, rows in backfill_arm_seed_rows.items():
        for cand in rows:
            if pd.isna(cand["cagr"]) or cand["cagr"] <= PHASE25_ISLAND_CLIFFBOX_CAGR_MIN:
                backfill_arm_seed_skipped_low_cagr.append(arm)
                continue
            phase25_tasks |= cliffbox_tasks_for_cell(cand, TRAIL_PCTS)
            backfill_arm_seed_count += 1
    if missing_arms_seed:
        zero_evidence_arm_seed = [a for a, rows in backfill_arm_seed_rows.items() if not rows]
        print(f"arm_pct backfill (seed stage): {len(missing_arms_seed)} take_profit value(s) "
              f"with zero representation among the normal Phase2.5 seed picks -- adding "
              f"{backfill_arm_seed_count} extra seed cell(s) (top-3 each, from raw "
              f"Phase1+Phase2 data) into the SAME cliffbox sweep: {missing_arms_seed}")
        if zero_evidence_arm_seed:
            print(f"  {len(zero_evidence_arm_seed)} of those had NO evidence at all (every "
                  f"cell had trades=0) -- 0 seed cells added: {zero_evidence_arm_seed}")
        if backfill_arm_seed_skipped_low_cagr:
            print(f"  {len(backfill_arm_seed_skipped_low_cagr)} seed cell(s) skipped cliffbox "
                  f"expansion (top cagr <= {PHASE25_ISLAND_CLIFFBOX_CAGR_MIN} or NaN) -- still "
                  f"eligible for final-stage promotion from unrefined data, just not "
                  f"densified: {backfill_arm_seed_skipped_low_cagr}")
        print(f"  Cliff-box cells to verify after arm_pct backfill: {len(phase25_tasks):,} total")

    # Real overlap check: how many of Phase2.5's cells were ALREADY computed in
    # Phase1 and/or Phase2? Real production would skip these via cache-lookup;
    # this in-memory version has no equivalent, so it's pure redundant compute.
    already_computed = set(
        (int(r.take_profit), int(r.stop_loss), int(r.max_hold_hours), int(r.window),
         float(r.z_score_threshold), float(r.trail_sell_pct))
        for r in df_full[["take_profit", "stop_loss", "max_hold_hours", "window",
                           "z_score_threshold", "trail_sell_pct"]].itertuples(index=False))
    overlap = phase25_tasks & already_computed
    print(f"Phase2.5-cliffbox overlap check: {len(overlap):,} of {len(phase25_tasks):,} "
          f"cliff-box cells ({100 * len(overlap) / max(len(phase25_tasks), 1):.1f}%) "
          f"were already computed in Phase1/Phase2 -- redundant recompute.")

    t4 = time.time()
    phase25_rows = _dispatch_grouped_by_wz(pool, phase25_tasks, TICKER, strategy_name, version,
                                            fixed_sl, spy_bh, desc="Phase2.5-cliffbox (in-memory)",
                                            fill_resolution="second")
    t5 = time.time()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] PROGRESS: Phase2.5 done ticker={TICKER} strategy={strategy_name} fixed_sl={fixed_sl}: "
          f"{len(phase25_rows):,} rows in {t5 - t4:.1f}s "
          f"({len(phase25_rows) / max(t5 - t4, 0.001):.0f} nodes/sec)")

    print(f"\nTotal: Phase1={t1 - t0:.1f}s + Phase2={t3 - t2:.1f}s + Phase2.5={t5 - t4:.1f}s "
          f"= {t5 - t0:.1f}s wall-clock, zero DB writes "
          f"({len(phase1_rows) + len(phase2_rows) + len(phase25_rows):,} total cells computed).")

    # Final 9 candidates: re-derived from Phase1+Phase2+Phase2.5 COMBINED, matching
    # derive_phase25_candidates_ground_truth's real query (no phase filter -- it reads
    # whatever's in backtest_cache for the scope, which after a real Phase2.5 run
    # includes its own denser cliff-box rows too, not just the pre-2.5 seed data).
    # _second_build_active: used below to decide whether to even ATTEMPT second-
    # resolution work (completion pass, top-9 winner-trades capture) -- NOT to tag
    # resolution, since 2026-09-06 round 2 (paired-review HIGH finding): each row's real
    # `resolution` now comes straight from the worker itself (run_single_backtest_node_
    # ground_truth_isolated's own `fill_resolution` return key, threaded through
    # _dispatch's `_record`), so df25/phase25_rows already carry the TRUE per-cell value
    # -- including the per-cell-fallback case (a build vanishing mid-run, a transient
    # read error) this per-run boolean cannot see. Overwriting it with this coarse
    # per-run assumption would have silently undone that fix.
    _second_build_active = db_cache.get_active_build_id(TICKER, 'second') is not None
    df25 = pd.DataFrame(phase25_rows)
    if not df_full.empty and "resolution" not in df_full.columns:
        # Only a resumed run reading an OLD checkpoint parquet (predates this column)
        # can land here -- Phase1/Phase2 are always minute-resolution by construction
        # (see _dispatch's own docstring: deliberately not threaded through their
        # dispatch calls), so backfilling "minute" here is always correct, never a guess.
        df_full["resolution"] = "minute"
    # df_full FIRST (2026-09-06, paired-review HIGH finding, round 2 -- REVERSED from
    # this fix's original df25-first version after both independent-cold and contextual
    # review independently confirmed a real selection-bias bug): `df_final` is what
    # pick_island_centers/region.sort_values below rank candidates on, and what a
    # selected candidate's own persisted cagr/trades/alpha_vs_spy come from. Phase2.5 now
    # runs some cells at 1s while Phase1/Phase2 stay minute -- a df25-first dedup would
    # make WHICH CELL WINS the ranking depend on whether it happened to fall in a seed's
    # cliffbox, purely a resolution artifact (1s vs minute CAGR diverges by a real,
    # ticker-dependent, sometimes-large amount -- confirmed empirically: SOXL +2.7pp,
    # ETHU +41.6pp). That changes the SELECTION itself, not just a diagnostic about it.
    # Fix: keep `df_final`'s own ranking/selection basis minute-only-consistent (matching
    # every campaign before Phase2.5 started requesting 1s fill), by preferring df_full's
    # minute value on any coordinate overlap. The 1s data isn't discarded -- see
    # `df_cliffsafety` below, a SEPARATE pool built second-first, used only for the
    # worst_neighbor_cagr robustness verdict (which is genuinely supposed to answer "is
    # this candidate's neighborhood safe," a real use for 1s data) and the top-9
    # winner-trades capture (a direct resim, not a df_final lookup) -- never for deciding
    # which cell IS a final candidate.
    df_final = pd.concat([df_full, df25], ignore_index=True)
    df_final = df_final[df_final["trades"] > 0]
    df_final = df_final.drop_duplicates(
        subset=["take_profit", "stop_loss", "max_hold_hours", "window",
                "z_score_threshold", "trail_sell_pct"])
    # df_cliffsafety: df25 (1s) FIRST -- the ORIGINAL df25-first fix, now scoped to ONLY
    # this separate pool so it can't bias df_final's own selection. Used exclusively by
    # the "Final-candidate cliffbox completion pass" and the worst_neighbor_cagr loop
    # below.
    df_cliffsafety = pd.concat([df25, df_full], ignore_index=True)
    df_cliffsafety = df_cliffsafety[df_cliffsafety["trades"] > 0]
    df_cliffsafety = df_cliffsafety.drop_duplicates(
        subset=["take_profit", "stop_loss", "max_hold_hours", "window",
                "z_score_threshold", "trail_sell_pct"])

    # Cross-island convergence handling (2026-08-27, design discussion): two islands'
    # +-FINE_RADIUS regions can overlap when they're close (min_sep=6, FINE_RADIUS=4),
    # so the SAME literal cell can legitimately rank in both islands' top-3 -- not a
    # coincidental tie, the identical (tp,sl,hold,w,z,tpct) row. Rather than silently
    # losing that slot (INSERT OR IGNORE just drops the duplicate, shrinking 9->8),
    # each island falls through to its own next-best genuinely-distinct pick instead,
    # so a real 9th candidate still gets a chance -- and the convergence itself is
    # recorded (`converged_from_islands`) as a real robustness signal (a cell two
    # independent neighborhoods both rank highly is stronger evidence than one alone),
    # not silently discarded.
    final_centers = pick_island_centers(df_final, n=N_ISLANDS, rank_col="cagr")
    final_candidates = []
    claimed = {}  # coordinate key -> candidate dict already added (tracks convergence)

    def _row_generation(row):
        """Which Phase2-island generation (1-indexed) first computed this df_final row --
        NaN/absent for a Phase1-only or Phase2.5-cliffbox-only cell (matches legacy's own
        generation-column convention: only Phase2-Island rows carry a real value, see
        run_optimization_sweep.py's ALTER TABLE backtest_cache ADD COLUMN generation
        comment) or a pre-generation-loop checkpoint that predates this column."""
        gen = row.get("generation") if hasattr(row, "get") else None
        return int(gen) if gen is not None and pd.notna(gen) else None
    for tp_c, sl_c in final_centers:
        region = df_final[(df_final["take_profit"] - tp_c).abs().le(FINE_RADIUS)
                           & (df_final["stop_loss"] - sl_c).abs().le(FINE_RADIUS)]
        region = region[region["cagr"].notna()]
        if region.empty:
            continue
        region = region.sort_values(tb_cols, ascending=tb_asc)
        picked = 0
        for _, cand in region.iterrows():
            if picked >= 3:
                break
            key = (int(cand["take_profit"]), int(cand["stop_loss"]), int(cand["max_hold_hours"]),
                   int(cand["window"]), float(cand["z_score_threshold"]), float(cand["trail_sell_pct"]))
            if key in claimed:
                claimed[key]["converged_from_islands"].append((tp_c, sl_c))
                continue  # already claimed by an earlier island -- fall through, don't consume this slot
            c = {
                "island": (tp_c, sl_c), "take_profit": key[0], "stop_loss": key[1],
                "max_hold_hours": key[2], "window": key[3], "z_score_threshold": key[4],
                "trail_sell_pct": key[5], "cagr": float(cand["cagr"]),
                "trades": int(cand["trades"]), "alpha_vs_spy": float(cand["alpha_vs_spy"]),
                "converged_from_islands": [(tp_c, sl_c)], "generation": _row_generation(cand),
            }
            claimed[key] = c
            final_candidates.append(c)
            picked += 1

    n_converged = sum(1 for c in final_candidates if len(c["converged_from_islands"]) > 1)
    if n_converged:
        print(f"\n{n_converged} candidate(s) converged from multiple islands (real robustness "
              f"signal, not a duplicate):")
        for c in final_candidates:
            if len(c["converged_from_islands"]) > 1:
                print(f"  TP={c['take_profit']} SL={c['stop_loss']} hold={c['max_hold_hours']}h "
                      f"-> found via islands {c['converged_from_islands']}")

    # Window/z backfill -- FINAL stage (2026-08-30, planner dispatch; corrected same day --
    # see the seed-stage backfill block above for the full history/reasoning). Recomputed
    # HERE from `final_candidates`' own (window, z) coverage rather than reusing
    # `missing_combos_seed` from the seed stage, since the two can legitimately differ:
    # Phase2.5's own refinement/convergence-dedup can shift which combos actually land a
    # final top-N_ISLANDS slot. `df_final` at this point already includes the seed-stage
    # backfill's cliffbox rows (added to phase25_tasks above, before Phase2.5 ran), so
    # unlike the original (wrong) version of this fix, a backfilled candidate here has a
    # real, densely-computed neighborhood behind it -- the cliff-safety verdict loop right
    # below this treats it exactly like every island-selected candidate, no special case.
    #
    # No CAGR floor (deliberate: filtering "how weak is too weak" risks hiding a real
    # region exactly the same way the island cut already does) and top-2 per missing combo
    # (not top-1, upgraded after discussing the real cost: ~11 missing combos x 2 = ~22
    # extra candidates/scope, ~2,112 extra across a full 6-ticker campaign, ~1.73x today's
    # downstream Phase4/5 compute -- confirmed acceptable: "I am ok with the extra hours...
    # we get to pick a better node"). Tagged via `selection_source` (a new, dedicated
    # candidate_nodes column, NOT `comment` -- see _insert_candidate_nodes_rows for why the
    # original `comment`-reuse plan was dropped after paired review) so a later report can
    # separate "won via island selection" from "backfilled for window/z coverage".
    # top_up_window_z_island_quota, not find_missing_window_z_top_n (2026-09-03, real gap
    # found via Opus paired-review challenge -- see that function's own docstring and the
    # seed-stage call site's matching comment above): UNCONDITIONAL per-(window,z) quota,
    # not gated on "this combo has zero representation" -- final_candidates may already
    # contain ONE real candidate for a combo (via island selection or the seed-stage
    # top-up above) while a SECOND real, distinct island in that same combo is still
    # missing. present_df built straight from final_candidates itself (a list of dicts ->
    # DataFrame, already carries take_profit/stop_loss/window/z_score_threshold).
    final_present_df = pd.DataFrame(final_candidates) if final_candidates else pd.DataFrame()
    combos_topped_up, backfill_final_rows, combos_zero_evidence_final = top_up_window_z_island_quota(
        final_present_df, WINDOWS, Z_THRESHOLDS, df_final, tb_cols, tb_asc, top_n=2)
    backfilled_count = 0
    for combo, rows in backfill_final_rows.items():
        for cand in rows:
            key = (int(cand["take_profit"]), int(cand["stop_loss"]), int(cand["max_hold_hours"]),
                   int(cand["window"]), float(cand["z_score_threshold"]), float(cand["trail_sell_pct"]))
            if key in claimed:
                continue  # already promoted via island selection or an earlier top-up --
                          # shouldn't happen for a coordinate we just confirmed was
                          # under-represented, but safe either way
            c = {
                "island": None, "take_profit": key[0], "stop_loss": key[1],
                "max_hold_hours": key[2], "window": key[3], "z_score_threshold": key[4],
                "trail_sell_pct": key[5], "cagr": float(cand["cagr"]),
                "trades": int(cand["trades"]), "alpha_vs_spy": float(cand["alpha_vs_spy"]),
                "converged_from_islands": [], "backfilled": True,
                "backfill_reason": "window_z", "generation": _row_generation(cand),
            }
            claimed[key] = c
            final_candidates.append(c)
            backfilled_count += 1
    if combos_topped_up:
        print(f"\nBackfilled {backfilled_count} candidate(s) across {len(combos_topped_up)} "
              f"window/z combo(s) with at least one under-represented island (unconditional "
              f"per-combo quota check, not just zero-representation combos): {combos_topped_up}")
    if combos_zero_evidence_final:
        print(f"  {len(combos_zero_evidence_final)} window/z combo(s) have NO evidence at all "
              f"(every cell had trades=0, or none computed) -- can't be topped up: "
              f"{combos_zero_evidence_final}")

    # arm_pct (take_profit) backfill -- FINAL stage (2026-08-30, planner dispatch): same
    # shape as the window/z final-stage block above, on the take_profit axis instead --
    # re-detected fresh from final_candidates' own coverage (not reused from the seed
    # stage, same reasoning as the window/z final-stage block: Phase2.5/convergence-dedup
    # can legitimately shift which arm values actually land a final slot). df_final at
    # this point already includes the seed-stage arm backfill's own cliffbox rows, so a
    # backfilled-here candidate still gets a real cliff-safety verdict, not a degenerate
    # one.
    present_arms_final = {c["take_profit"] for c in final_candidates}
    missing_arms, backfill_arm_final_rows = find_missing_arm_top_n(
        present_arms_final, TAKE_PROFITS, df_final, tb_cols, tb_asc, top_n=3)
    backfilled_arm_count = 0
    for arm, rows in backfill_arm_final_rows.items():
        for cand in rows:
            key = (int(cand["take_profit"]), int(cand["stop_loss"]), int(cand["max_hold_hours"]),
                   int(cand["window"]), float(cand["z_score_threshold"]), float(cand["trail_sell_pct"]))
            if key in claimed:
                continue  # already promoted via island selection or the window/z backfill
                          # above -- shouldn't happen for an arm value we just confirmed
                          # has zero representation, but safe either way
            c = {
                "island": None, "take_profit": key[0], "stop_loss": key[1],
                "max_hold_hours": key[2], "window": key[3], "z_score_threshold": key[4],
                "trail_sell_pct": key[5], "cagr": float(cand["cagr"]),
                "trades": int(cand["trades"]), "alpha_vs_spy": float(cand["alpha_vs_spy"]),
                "converged_from_islands": [], "backfilled": True,
                "backfill_reason": "arm_pct", "generation": _row_generation(cand),
            }
            claimed[key] = c
            final_candidates.append(c)
            backfilled_arm_count += 1
    if missing_arms:
        arms_with_zero_evidence = [a for a, rows in backfill_arm_final_rows.items() if not rows]
        print(f"\nBackfilled {backfilled_arm_count} candidate(s) across {len(missing_arms)} "
              f"take_profit value(s) with zero island representation (top-3 each, where "
              f"evidence existed): {missing_arms}")
        if arms_with_zero_evidence:
            print(f"  {len(arms_with_zero_evidence)} of those had NO evidence at all (every "
                  f"cell had trades=0) -- contributed 0, not 3: {arms_with_zero_evidence}")

    # Final-candidate cliffbox completion pass (2026-09-06, paired-review HIGH finding):
    # final_centers/final_candidates above are re-derived from df_final AFTER Phase2.5,
    # so a final candidate's own (tp, sl) is not guaranteed to be one of the seed
    # candidates cliffbox_tasks_for_cell was originally called on (those seeds came from
    # PRE-Phase2.5 island selection, minute-resolution only) -- e.g. a final island center
    # can shift to a cell whose neighborhood was never dispatched at 1s at all. Without
    # this pass, worst_neighbor_cagr's min() below silently mixes 1s cells (wherever a
    # final candidate happens to fall inside an earlier seed's cliffbox) with leftover
    # minute-only cells for the rest of its neighborhood -- only exact-coordinate overlaps
    # were fixed by df_cliffsafety's df25-first concat order, not the full box. Fixed by
    # dispatching cliffbox_tasks_for_cell for EVERY final candidate (backfilled ones
    # included) at the same fill_resolution Phase2.5 used, same as the seed-stage pattern,
    # then merging any genuinely new cells into df_cliffsafety ONLY (df25-first, matching
    # its own concat order) -- NEVER into df_final, which stays the minute-priority
    # selection/ranking pool untouched by this pass (see df_final/df_cliffsafety's own
    # comment above for why the two must not be conflated).
    if _second_build_active:
        final_cliffbox_needed = set()
        for c in final_candidates:
            final_cliffbox_needed |= cliffbox_tasks_for_cell(c, TRAIL_PCTS)
        already_second = set(
            (int(r.take_profit), int(r.stop_loss), int(r.max_hold_hours), int(r.window),
             float(r.z_score_threshold), float(r.trail_sell_pct))
            for r in df25[["take_profit", "stop_loss", "max_hold_hours", "window",
                           "z_score_threshold", "trail_sell_pct"]].itertuples(index=False)) if not df25.empty else set()
        missing_for_final = final_cliffbox_needed - already_second
        if missing_for_final:
            print(f"\nFinal-candidate cliffbox completion: {len(missing_for_final):,} cell(s) in "
                  f"final candidates' own +-{CLIFF_RADIUS} neighborhoods were never computed at "
                  f"1s resolution during Phase2.5 -- dispatching now so worst_neighbor_cagr never "
                  f"mixes resolutions.")
            completion_rows = _dispatch_grouped_by_wz(pool, missing_for_final, TICKER, strategy_name,
                                                       version, fixed_sl, spy_bh,
                                                       desc="Phase2.5-cliffbox-completion (in-memory)",
                                                       fill_resolution="second")
            # No manual "resolution" stamp here (2026-09-06, round 2 fix) -- completion_rows
            # already carries each cell's REAL resolution via _record (see _second_build_
            # active's own comment above), including the per-cell-fallback case this
            # completion pass is specifically meant to protect against.
            df_completion = pd.DataFrame(completion_rows)
            if not df_completion.empty:
                df_cliffsafety = pd.concat([df_completion, df_cliffsafety], ignore_index=True)
                df_cliffsafety = df_cliffsafety[df_cliffsafety["trades"] > 0]
                df_cliffsafety = df_cliffsafety.drop_duplicates(
                    subset=["take_profit", "stop_loss", "max_hold_hours", "window",
                            "z_score_threshold", "trail_sell_pct"])

    # Cliff-safety verdict per candidate: worst_neighbor_cagr, min(cagr) among cells
    # within +-CLIFF_RADIUS tp/sl of the candidate (same hold/window/z/trail_pct),
    # sourced from df_cliffsafety (Phase1+Phase2+Phase2.5+completion combined, 1s-
    # preferring -- the full evidence pool already in memory, deliberately SEPARATE from
    # df_final so this robustness check can use 1s data without biasing which cell won
    # selection). Persisted for real now (2026-08-31, planner dispatch) --
    # _insert_candidate_nodes_rows writes it to a real candidate_nodes.worst_neighbor_cagr
    # column, ALTER-guarded same as params_json/selection_source/core_safe. Previously
    # print-only ("compute the full box, persist only the verdict" was the intent from
    # the start, just not implemented until now).
    for c in final_candidates:
        neighbors = df_cliffsafety[
            (df_cliffsafety["take_profit"] - c["take_profit"]).abs().le(CLIFF_RADIUS)
            & (df_cliffsafety["stop_loss"] - c["stop_loss"]).abs().le(CLIFF_RADIUS)
            & (df_cliffsafety["max_hold_hours"] == c["max_hold_hours"])
            & (df_cliffsafety["window"] == c["window"])
            & (df_cliffsafety["z_score_threshold"] == c["z_score_threshold"])
            & (df_cliffsafety["trail_sell_pct"] == c["trail_sell_pct"])
        ]
        c["worst_neighbor_cagr"] = float(neighbors["cagr"].min()) if not neighbors.empty else None
        c["n_neighbors_checked"] = len(neighbors)
        # Defense-in-depth resolution check (paired-review HIGH finding): the completion
        # pass above should make every neighbor 'second' whenever _second_build_active,
        # but flag loudly rather than silently trust it -- e.g. a completion dispatch that
        # itself fell back per-cell for a reason unrelated to the build (a transient read
        # error) would otherwise mix resolutions with no visible trace.
        if _second_build_active and not neighbors.empty and (neighbors["resolution"] != "second").any():
            n_minute = int((neighbors["resolution"] != "second").sum())
            print(f"  [worst_neighbor_cagr WARNING] TP={c['take_profit']} SL={c['stop_loss']}: "
                  f"{n_minute} of {len(neighbors)} neighbor cell(s) are minute-resolution despite "
                  f"an active second build -- cliff-safety verdict may mix resolutions.")

    _backfill_bits = []
    if backfilled_count:
        _backfill_bits.append(f"{backfilled_count} backfilled (missing window/z coverage)")
    if backfilled_arm_count:
        _backfill_bits.append(f"{backfilled_arm_count} backfilled (missing arm_pct coverage)")
    _backfill_note = f" + {' + '.join(_backfill_bits)}" if _backfill_bits else ""
    print(f"\n=== Final {len(final_candidates)} candidates (post-Phase2.5, {N_ISLANDS} islands x "
          f"top-3{_backfill_note}) ===")
    # sl_axis_col: what the 'stop_loss' column actually means for THIS strategy (see
    # strategies.resolve_axis_columns()) -- e.g. trail_buy_pct (entry-trigger trail%)
    # for TrailingBothZScoreBreakout, trail_pct (exit trail%) for TrailingExitZScoreBreakout.
    # fixed_sl is the REAL protective stop, held constant for this whole run, and was
    # previously missing from this line entirely.
    sl_axis_col, _ = strategies.resolve_axis_columns(strategy_name)
    for c in sorted(final_candidates, key=lambda r: -r["cagr"]):
        tag = (f"island{c['island']}" if c.get("island") is not None
               else f"backfill({c.get('backfill_reason', 'unknown')})")
        # wnc_str (2026-08-31, paired-review LOW finding): worst_neighbor_cagr is
        # documented as a real None case (n_neighbors_checked == 0) -- unreachable in
        # practice today (a candidate's own df_final row always satisfies its own
        # neighbor predicate) but the print must not silently assume that forever.
        wnc_str = (f"{c['worst_neighbor_cagr']:.2f}%" if c['worst_neighbor_cagr'] is not None
                   else "N/A")
        print(f"  {tag}: TP={c['take_profit']} {sl_axis_col}={c['stop_loss']} "
              f"fixed_sl={fixed_sl} hold={c['max_hold_hours']}h w={c['window']} "
              f"z={c['z_score_threshold']} trail_pct={c['trail_sell_pct']} -> "
              f"cagr={c['cagr']:.2f}% trades={c['trades']} "
              f"| worst_neighbor_cagr={wnc_str} "
              f"(n={c['n_neighbors_checked']})")

    # Live-node regression guard (2026-09-03) -- runs BEFORE promotion, read-only, never
    # blocks the write below. See _check_live_node_regression's own docstring.
    _check_live_node_regression(df_final, final_candidates, TICKER, strategy_name, fixed_sl,
                                 ENTRY_TIMING)

    # Top-9 write: promotion into candidate_nodes (NOT backtest_cache -- per the
    # "neither top-100 nor top-9 belongs in backtest_cache" design conclusion, the
    # winners are the campaign's real OUTPUT, not a cache row).
    if _seed_task is not None:
        _clear_prior_seed_mode_table_rows("candidate_nodes", strategy_name, version, TICKER, fixed_sl)
    t_ins2 = time.time()
    n_written9 = _insert_candidate_nodes_rows(
        final_candidates, strategy_name, version, TICKER, fixed_sl, ENTRY_TIMING)
    t_ins3 = time.time()
    print(f"\nTop-{len(final_candidates)} candidates: {n_written9} rows promoted to "
          f"candidate_nodes in {t_ins3 - t_ins2:.2f}s")

    # Real trade sequences for the final 9 -- single-process, direct kernel calls
    # (need_times=True, unlike the worker function's need_times=False aggregate-only
    # path) since it's only 9 backtests, not worth a pool. In-memory only for now --
    # no backtest_winner_trades table exists yet to persist these into.
    t_ins4 = time.time()
    strategy_class = getattr(strategies, strategy_name)
    is_both = strategy_name == 'TrailingBothZScoreBreakout'
    winner_trades = {}
    node_keys_by_key = {}
    fill_resolution_by_key = {}
    for c in final_candidates:
        key = (c["take_profit"], c["stop_loss"], c["max_hold_hours"], c["window"],
               c["z_score_threshold"], c["trail_sell_pct"])
        node_keys_by_key[key] = node_key(
            strategy_name, TICKER, fixed_sl, c["window"], c["z_score_threshold"],
            c["max_hold_hours"], c["take_profit"], c["stop_loss"], c["trail_sell_pct"],
            ENTRY_TIMING, strategies.resolve_axis_columns)
        if key in winner_trades:
            continue  # duplicate candidate (shared across islands) -- don't re-simulate
        # fill_resolution='second' when available (2026-09-06, paired-review MEDIUM
        # finding): this loop captures the REAL final-9 candidate population's own trade
        # sequences -- the actual thing Phase4/backtest_winner_trades/downstream reports
        # consume -- not just the cliffbox neighbor check above. Leaving this at the
        # default 'minute' meant Phase2.5's whole 1s-resolution effort never reached the
        # candidate population that matters; only the cliff-safety verdict (a robustness
        # check ABOUT the population) ever saw 1s data.
        inputs = _load_node_inputs_ground_truth(
            TICKER, strategy_class, strategy_name, c["window"], c["z_score_threshold"],
            START, END, data_source=DATA_SOURCE,
            fill_resolution="second" if _second_build_active else "minute")
        _, df_daily_processed, minute_df, df_hourly_windowed, prep, mprep, _actual_fill_res = inputs
        if _second_build_active and _actual_fill_res != "second":
            print(f"  [winner-trades WARNING] TP={c['take_profit']} SL={c['stop_loss']}: "
                  f"requested second-resolution fill but got {_actual_fill_res!r} -- top-9 "
                  f"trade sequence for this candidate is minute-resolution.")
        if is_both:
            trail_buy_pct_arg, trail_sell_pct_arg, arm_pct_arg = (
                float(c["stop_loss"]), float(c["trail_sell_pct"]), float(c["take_profit"]))
        else:
            trail_buy_pct_arg, trail_sell_pct_arg, arm_pct_arg = (
                0.0, float(c["stop_loss"]), float(c["take_profit"]))
        trades = run_backtest_ground_truth(
            df_hourly_windowed, df_daily_processed, TICKER, minute_df,
            fixed_sl=fixed_sl, arm_pct=arm_pct_arg, trail_buy_pct=trail_buy_pct_arg,
            trail_sell_pct=trail_sell_pct_arg, max_hours_to_hold=c["max_hold_hours"],
            z_score_threshold=c["z_score_threshold"], is_both=is_both,
            open_check_entry_timing=(ENTRY_TIMING == 'open_check'),
            same_bar_reentry=True, prep=prep, mprep=mprep, need_times=True,
        )
        winner_trades[key] = trades
        # Per-key, not per-run (2026-09-06, round 2 fix -- same class of bug as the
        # resolution-column fixes above): _actual_fill_res is THIS candidate's real
        # outcome, which can legitimately differ from _second_build_active's per-run
        # assumption (the exact case the WARNING two lines up detects and prints).
        fill_resolution_by_key[key] = _actual_fill_res
    t_ins5 = time.time()
    total_trade_rows = sum(len(t) for t in winner_trades.values())
    print(f"Top-9 real trade sequences: {len(winner_trades)} distinct candidates, "
          f"{total_trade_rows:,} total trade rows, captured in {t_ins5 - t_ins4:.2f}s")

    t_ins6 = time.time()
    if _seed_task is not None:
        _clear_prior_seed_mode_table_rows(
            "backtest_winner_trades", strategy_name, version, TICKER, fixed_sl)
    # Real resolved build_ids (staleness-invalidation fix, 2026-08-29, paired-review HIGH
    # finding) -- one lookup per ticker/table for this whole run, not per candidate (a
    # promotion mid-run is not a case this pipeline defends against anywhere else either).
    # None when nothing has ever been promoted for this ticker/table (get_active_build_id's
    # own documented "no build at all" case) -- stored as NULL, which get_cached_trades
    # treats as "unknown, don't trust" on read-back, same as a real mismatch.
    _hourly_build_id = db_cache.get_active_build_id(TICKER, 'hourly')
    _minute_build_id = db_cache.get_active_build_id(TICKER, 'minute')
    n_trade_rows_written = _insert_winner_trades_rows(
        winner_trades, node_keys_by_key, strategy_name, version, TICKER, fixed_sl,
        kernel_version=GT_TRADES_KERNEL_VERSION,
        hourly_build_id=_hourly_build_id, minute_build_id=_minute_build_id,
        fill_resolution_by_key=fill_resolution_by_key)
    t_ins7 = time.time()
    print(f"Trade rows written to backtest_winner_trades: {n_trade_rows_written} "
          f"in {t_ins7 - t_ins6:.2f}s")

    _log_sweep_run_finish(_run_log_id, len(final_candidates), n_written9, n_trade_rows_written,
                           time.time() - _run_log_t0)

    # PROGRESS line moved here (2026-08-29, paired review, CONFIRMED MEDIUM): the old
    # "PROGRESS: fixed_sl DONE" print fired right after the final-candidates list was
    # built, BEFORE the candidate_nodes write, the winner-trades ground-truth backtests,
    # and _log_sweep_run_finish actually ran -- a crash after that point would leave a
    # misleadingly "DONE"-looking log line while sweep_run_log.finished_at was still NULL.
    # Now fires after the real last step, with the final candidate count on the line
    # itself so a `grep "PROGRESS:"` log monitor gets real information.
    print(f"[{datetime.now().strftime('%H:%M:%S')}] PROGRESS: fixed_sl DONE ticker={TICKER} strategy={strategy_name} fixed_sl={fixed_sl}: "
          f"{len(final_candidates)} final candidates, {n_written9} candidate_nodes rows, "
          f"{n_trade_rows_written} trade rows written")


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
