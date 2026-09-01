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
    DB_PATH, _load_node_inputs_ground_truth,
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


def _dispatch(pool, tasks, ticker, strategy_name, version, fixed_sl, spy_bh, desc="dispatch"):
    """Same worker call the real pipeline uses -- returns list of result dicts, in memory only.

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
    max_workers = pool._max_workers

    b = campaign_registry.get_workers_budget(version)
    budget = max_workers if not isinstance(b, int) or b <= 0 else max(1, min(b, max_workers))

    rows = []
    fail_counts = {}

    def _record(task, future_or_none, res_or_exc):
        tp, sl, hold_hours, w, z_thresh, tpct = task
        if isinstance(res_or_exc, Exception):
            fail_counts["CRASH"] = fail_counts.get("CRASH", 0) + 1
            return
        status = res_or_exc.get("status")
        if status != "SUCCESS":
            fail_counts[status] = fail_counts.get(status, 0) + 1
            return
        alpha, num_trades, wr, comp_ret, wtw, node_cagr = res_or_exc["payload"]
        rows.append({
            "take_profit": int(tp), "stop_loss": int(sl), "max_hold_hours": hold_hours,
            "window": w, "z_score_threshold": z_thresh, "trail_sell_pct": tpct,
            "trades": num_trades, "win_rate": wr, "strategy_return": comp_ret,
            "alpha_vs_spy": alpha, "cagr": node_cagr,
        })

    if budget >= max_workers:
        # Unthrottled -- original behavior, submit everything upfront and drain via
        # as_completed()'s own internal handling (no bounded-submission overhead).
        futures_map = {
            pool.submit(run_single_backtest_node_ground_truth_isolated,
                        (ticker, strategy_name, version, int(tp), int(sl), hold, w, spy_bh, z,
                         fixed_sl, tpct, ENTRY_TIMING, True, START, END, DATA_SOURCE)): task
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
            future = pool.submit(run_single_backtest_node_ground_truth_isolated,
                                  (ticker, strategy_name, version, int(tp), int(sl), hold, w,
                                   spy_bh, z, fixed_sl, tpct, ENTRY_TIMING, True, START, END,
                                   DATA_SOURCE))
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
        print(f"  non-SUCCESS statuses: {fail_counts}")
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
                                hourly_build_id=None, minute_build_id=None):
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
        for col in ("kernel_version TEXT", "hourly_build_id INTEGER", "minute_build_id INTEGER"):
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
            for i, t in enumerate(trades):
                buffer.append((nk, config_version, ticker, strategy_name, float(fixed_sl), i,
                               str(t['Entry Time']), t['Entry Price'], str(t['Exit Time']),
                               t['Exit Price'], t['exit_reason'], t['Return'], int(t['armed']),
                               str(t['Arm Time']) if t['armed'] else None, t['Arm Price'], now_iso,
                               kernel_version, hourly_build_id, minute_build_id))
        conn.executemany("""
            INSERT INTO backtest_winner_trades
                (node_key, version, ticker, strategy, fixed_sl, trade_idx, entry_time,
                 entry_price, exit_time, exit_price, exit_reason, return_pct, armed,
                 arm_time, arm_price, created_at, kernel_version, hourly_build_id, minute_build_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, buffer)
        conn.commit()
    return len(buffer)


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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sweep_run_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL, finished_at TEXT,
                script TEXT, pid INTEGER, ticker TEXT, strategy TEXT, fixed_sl REAL,
                windows TEXT, version TEXT,
                n_final_candidates INTEGER, n_candidate_nodes_written INTEGER,
                n_trade_rows_written INTEGER, elapsed_s REAL
            )""")
        cur = conn.execute("""
            INSERT INTO sweep_run_log (started_at, script, pid, ticker, strategy, fixed_sl,
                windows, version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (time.strftime("%Y-%m-%dT%H:%M:%S"), os.path.basename(__file__), os.getpid(),
              ticker, strategy_name, float(fixed_sl), ",".join(str(w) for w in windows), version))
        conn.commit()
        return cur.lastrowid


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

    raw_generic_tp = row["arm_sell_pct"] if strategy_name == 'TrailingBothZScoreBreakout' \
        else row["take_profit"]
    if raw_generic_tp is None:
        raise SystemExit(
            f"--seed-watch-list-id {watch_list_id}: tp-axis value is NULL (strategy="
            f"{strategy_name!r}, looked in "
            f"{'arm_sell_pct' if strategy_name == 'TrailingBothZScoreBreakout' else 'take_profit'}"
            f") -- can't seed from this row.")
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
            raise SystemExit(
                f"--seed-watch-list-id {watch_list_id}: {axis_name} axis value {value} is "
                f"fractional -- Phase1/Phase2/Phase2.5's tp/sl mesh is integer-only by design "
                f"(range()-walked boxes, integer-only campaign_config grids); seeding from "
                f"this value would silently round it to a DIFFERENT node. Not supported.")
        return rounded

    task = (_require_integer(generic_tp, "tp"), _require_integer(generic_sl, "sl"),
            int(row["max_hold_hours"]), int(row["window"]), float(row["z_score_threshold"]),
            float(generic_tpct))
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
    as build_phase1_tasks_grid above), and used at TWO call sites in run_one_fixed_sl -- see
    each call site's own comment for why one function serves both.

    Real gap this closes: a pooled top-N_ISLANDS x top-3 selection over ALL (window, z) combos
    at once -- confirmed empirically (2026-08-30) that this leaves an average 11.0 of 16
    combos with ZERO representation, not a rare tail case (concrete real example: ETHU
    window=10 z=1.0 fixed_sl=1 had a previously-validated winner, robust_alpha ~540-555%, but
    produced zero top-30 candidates in the widened sweep).

    Pure/read-only: does NOT mutate `df_source` or append anywhere -- callers decide what to
    do with the returned rows (seed a cliffbox sweep, or promote directly to candidate_nodes).
    For every (window, z) combo not in `present_combos`, returns its own top-`top_n` rows (by
    cagr, `tb_cols`/`tb_asc` tiebreak, `cagr` not null) from `df_source`, regardless of how
    weak they look -- no CAGR floor, deliberately: filtering risks hiding a real region the
    same way the pooled cut already does.

    Returns (missing_combos, rows_by_combo) -- `rows_by_combo[(w, z)]` is a list of up to
    `top_n` pandas Series (empty list if `df_source` has zero evidence at all for that combo,
    e.g. every cell had trades=0 -- distinguishable from "found some, took top_n" so a caller
    can report the two cases differently instead of treating them the same)."""
    all_combos = {(w, z) for w in windows for z in z_thresholds}
    missing_combos = sorted(all_combos - present_combos)
    rows_by_combo = {}
    for w, z in missing_combos:
        pool_wz = df_source[(df_source["window"] == w) & (df_source["z_score_threshold"] == z)
                             & df_source["cagr"].notna()]
        if pool_wz.empty:
            rows_by_combo[(w, z)] = []
            continue
        pool_wz = pool_wz.sort_values(tb_cols, ascending=tb_asc)
        rows_by_combo[(w, z)] = [row for _, row in pool_wz.head(top_n).iterrows()]
    return missing_combos, rows_by_combo


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
                          "artifact) for Phase1+Phase2's combined df_full. If it exists, "
                          "skips Phase1 AND Phase2 entirely and loads df_full from it -- "
                          "for iterating on Phase2.5 logic without repaying the ~7min "
                          "Phase1+Phase2 cost each time. If missing, computes normally "
                          "and saves it after Phase2 finishes. Default: "
                          "<job-tmp>/bench_phase12_checkpoint_<ticker>_<strategy>_<fixed_sl>_"
                          "w<windows>_<date-range-suffix>.parquet (mutually exclusive with "
                          "--resume-from-top100 AND with --seed-watch-list-id).")
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

    _windows_str = ",".join(str(w) for w in WINDOWS)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for fixed_sl in fixed_sl_list:
            with sqlite3.connect(DB_PATH, timeout=60.0) as _conn:
                _conn.execute("""
                    CREATE TABLE IF NOT EXISTS sweep_run_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        started_at TEXT NOT NULL, finished_at TEXT,
                        script TEXT, pid INTEGER, ticker TEXT, strategy TEXT, fixed_sl REAL,
                        windows TEXT, version TEXT,
                        n_final_candidates INTEGER, n_candidate_nodes_written INTEGER,
                        n_trade_rows_written INTEGER, elapsed_s REAL
                    )""")
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


def _build_checkpoint_filename(strategy_name, fixed_sl, args):
    """Builds the dev-iteration checkpoint filename, extracted (2026-08-29, paired-review
    fixup) out of run_one_fixed_sl so it's directly unit-testable without a real DB/backtest
    run. Depends on module-level TICKER/WINDOWS/Z_THRESHOLDS/START/END plus the override
    flags on args."""
    # Keyed on WINDOWS too (not just strategy/fixed_sl) -- found live 2026-08-29: a
    # --window override run silently loaded a stale checkpoint from an earlier
    # standard-grid ([10,20]) run under the same strategy/fixed_sl, skipping Phase1+2
    # entirely and never actually computing the overridden window at all. The checkpoint
    # itself is explicitly documented as a "dev-iteration" convenience, not a production
    # artifact -- this key just makes that convenience safe to use across different grids.
    _windows_key = "-".join(str(w) for w in WINDOWS)
    # Keyed on Z_THRESHOLDS too -- same bug class as the WINDOWS key: a --z-thresholds
    # override run must not silently load a checkpoint from an earlier run under the
    # standard z grid (or a different z grid) with the same strategy/fixed_sl/windows.
    _z_key = "z" + "-".join(str(z) for z in Z_THRESHOLDS) if args.z_thresholds is not None else ""
    # Also keyed on the date range (not just WINDOWS) -- same bug class as the WINDOWS key
    # above: a full-range run's checkpoint must not get silently loaded by a later
    # short-range --start-date/--end-date smoke-test run (or vice versa).
    _range_key = window_version_suffix(START, END)
    # Also keyed on TICKER + the seed watch_list id (2026-08-29, paired review): seed mode
    # mutates the module-level TICKER global (previously immutable across a whole run), so
    # two different seed nodes sharing (strategy, fixed_sl, windows, date-range) but
    # different tickers would otherwise collide on the same checkpoint path -- silently
    # loading one ticker's df_full and promoting it under another ticker's version.
    _seed_key = f"_seed{args.seed_watch_list_id}" if getattr(args, "seed_watch_list_id", None) \
        is not None else ""
    # Also keyed on N_ISLANDS -- same bug class as the WINDOWS/Z_THRESHOLDS keys above: an
    # --n-islands override run must not silently load a checkpoint from an earlier run under
    # a different island count with the same strategy/fixed_sl/windows/date-range.
    _isl_key = f"_isl{args.n_islands}" if args.n_islands is not None else ""
    # Also keyed on PROMOTION_ALGO_VERSION (2026-08-31, planner dispatch, paired-review
    # HIGH finding): this checkpoint stores df_full -- the full Phase1+Phase2 evidence
    # pool -- and loading it SKIPS Phase1 AND Phase2 entirely, including the whole
    # N_GENERATIONS multi-generation loop. Without this key, a stale pre-pv4 checkpoint
    # (single Phase2-island pass) would silently load under the new pv4 code and get
    # promoted as if it were a real multi-generation result -- confirmed live: 153 stale
    # checkpoints from the single-pass code were sitting in the checkpoint dir with
    # otherwise-identical keys at the time this fix landed. Same bug class, same fix
    # shape, as the WINDOWS/Z_THRESHOLDS/N_ISLANDS keys above -- this one auto-resolves
    # every FUTURE PROMOTION_ALGO_VERSION bump too, not just this one.
    _pv_key = f"_pv{PROMOTION_ALGO_VERSION}"
    return (f"bench_phase12_checkpoint_{TICKER}_{strategy_name}_{fixed_sl}_w{_windows_key}"
            f"{_z_key}{_range_key}{_seed_key}{_isl_key}{_pv_key}.parquet")


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
        _job_tmp, _build_checkpoint_filename(strategy_name, fixed_sl, args))

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
    if _seed_task is None and not args.resume_from_top100 and os.path.exists(checkpoint_path):
      t0 = time.time()
      df_full = pd.read_parquet(checkpoint_path)
      t1 = t2 = t3 = time.time()
      phase1_rows, phase2_rows = [], []  # total-cells-computed count below stays honest
      print(f"CHECKPOINT: loaded df_full ({len(df_full):,} rows, deduped Phase1+Phase2) "
            f"from {checkpoint_path} in {t1 - t0:.2f}s -- skipping Phase1 AND Phase2 "
            f"dispatch entirely (dev-iteration checkpoint, NOT a production artifact).")
    else:
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
      if _seed_task is None:
          os.makedirs(os.path.dirname(checkpoint_path), exist_ok=True)
          df_full.to_parquet(checkpoint_path)
          print(f"Checkpoint saved: {checkpoint_path} ({len(df_full):,} rows)")
      else:
          print("Seed mode: checkpoint save skipped (always re-runs Phase1+Phase2 for real).")

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
    seed_present_combos = {(t[3], t[4]) for t in phase25_tasks}
    missing_combos_seed, backfill_seed_rows = find_missing_window_z_top_n(
        seed_present_combos, WINDOWS, Z_THRESHOLDS, df_full, tb_cols, tb_asc, top_n=2)
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
    if missing_combos_seed:
        zero_evidence_seed = [c for c, rows in backfill_seed_rows.items() if not rows]
        print(f"Window/z backfill (seed stage): {len(missing_combos_seed)} combo(s) with zero "
              f"representation among the normal Phase2.5 seed picks -- adding "
              f"{backfill_seed_count} extra seed cell(s) (top-2 each, from raw Phase1+Phase2 "
              f"data) into the SAME cliffbox sweep so they get real refinement + cliff-safety "
              f"verification, not a bypass: {missing_combos_seed}")
        if zero_evidence_seed:
            print(f"  {len(zero_evidence_seed)} of those had NO evidence at all (every cell "
                  f"had trades=0) -- 0 seed cells added: {zero_evidence_seed}")
        if backfill_seed_skipped_low_cagr:
            print(f"  {len(backfill_seed_skipped_low_cagr)} seed cell(s) skipped cliffbox "
                  f"expansion (top cagr <= {PHASE25_ISLAND_CLIFFBOX_CAGR_MIN} or NaN, same floor "
                  f"the normal per-island loop uses) -- still eligible for final-stage "
                  f"promotion from unrefined data, just not densified: {backfill_seed_skipped_low_cagr}")
        print(f"  Cliff-box cells to verify after backfill: {len(phase25_tasks):,} total")

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
    phase25_rows = _dispatch(pool, phase25_tasks, TICKER, strategy_name, version, fixed_sl, spy_bh,
                              desc="Phase2.5-cliffbox (in-memory)")
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
    df25 = pd.DataFrame(phase25_rows)
    df_final = pd.concat([df_full, df25], ignore_index=True)
    df_final = df_final[df_final["trades"] > 0]
    df_final = df_final.drop_duplicates(
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
    present_combos_final = {(c["window"], c["z_score_threshold"]) for c in final_candidates}
    missing_combos, backfill_final_rows = find_missing_window_z_top_n(
        present_combos_final, WINDOWS, Z_THRESHOLDS, df_final, tb_cols, tb_asc, top_n=2)
    backfilled_count = 0
    for combo, rows in backfill_final_rows.items():
        for cand in rows:
            key = (int(cand["take_profit"]), int(cand["stop_loss"]), int(cand["max_hold_hours"]),
                   int(cand["window"]), float(cand["z_score_threshold"]), float(cand["trail_sell_pct"]))
            if key in claimed:
                continue  # already promoted via island selection -- shouldn't happen for a
                          # combo we just confirmed has zero representation, but safe either way
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
    if missing_combos:
        combos_with_zero_evidence = [c for c, rows in backfill_final_rows.items() if not rows]
        print(f"\nBackfilled {backfilled_count} candidate(s) across {len(missing_combos)} "
              f"window/z combo(s) with zero island representation (top-2 each, where evidence "
              f"existed): {missing_combos}")
        if combos_with_zero_evidence:
            print(f"  {len(combos_with_zero_evidence)} of those had NO evidence at all (every "
                  f"cell had trades=0) -- contributed 0, not 2: {combos_with_zero_evidence}")

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

    # Cliff-safety verdict per candidate: worst_neighbor_cagr, min(cagr) among cells
    # within +-CLIFF_RADIUS tp/sl of the candidate (same hold/window/z/trail_pct),
    # sourced from df_final (Phase1+Phase2+Phase2.5 combined -- the full evidence pool
    # already in memory). Persisted for real now (2026-08-31, planner dispatch) --
    # _insert_candidate_nodes_rows writes it to a real candidate_nodes.worst_neighbor_cagr
    # column, ALTER-guarded same as params_json/selection_source/core_safe. Previously
    # print-only ("compute the full box, persist only the verdict" was the intent from
    # the start, just not implemented until now).
    for c in final_candidates:
        neighbors = df_final[
            (df_final["take_profit"] - c["take_profit"]).abs().le(CLIFF_RADIUS)
            & (df_final["stop_loss"] - c["stop_loss"]).abs().le(CLIFF_RADIUS)
            & (df_final["max_hold_hours"] == c["max_hold_hours"])
            & (df_final["window"] == c["window"])
            & (df_final["z_score_threshold"] == c["z_score_threshold"])
            & (df_final["trail_sell_pct"] == c["trail_sell_pct"])
        ]
        c["worst_neighbor_cagr"] = float(neighbors["cagr"].min()) if not neighbors.empty else None
        c["n_neighbors_checked"] = len(neighbors)

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
    for c in final_candidates:
        key = (c["take_profit"], c["stop_loss"], c["max_hold_hours"], c["window"],
               c["z_score_threshold"], c["trail_sell_pct"])
        node_keys_by_key[key] = node_key(
            strategy_name, TICKER, fixed_sl, c["window"], c["z_score_threshold"],
            c["max_hold_hours"], c["take_profit"], c["stop_loss"], c["trail_sell_pct"],
            ENTRY_TIMING, strategies.resolve_axis_columns)
        if key in winner_trades:
            continue  # duplicate candidate (shared across islands) -- don't re-simulate
        inputs = _load_node_inputs_ground_truth(TICKER, strategy_class, strategy_name,
                                                 c["window"], c["z_score_threshold"],
                                                 START, END, data_source=DATA_SOURCE)
        _, df_daily_processed, minute_df, df_hourly_windowed, prep, mprep = inputs
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
        hourly_build_id=_hourly_build_id, minute_build_id=_minute_build_id)
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
