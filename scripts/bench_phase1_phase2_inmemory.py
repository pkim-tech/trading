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
from concurrent.futures import ProcessPoolExecutor, as_completed

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


def _dispatch(pool, tasks, ticker, strategy_name, version, fixed_sl, spy_bh, desc="dispatch"):
    """Same worker call the real pipeline uses -- returns list of result dicts, in memory only."""
    futures_map = {
        pool.submit(run_single_backtest_node_ground_truth_isolated,
                    (ticker, strategy_name, version, int(tp), int(sl), hold, w, spy_bh, z,
                     fixed_sl, tpct, ENTRY_TIMING, True, START, END, DATA_SOURCE)): task
        for task in tasks
        for tp, sl, hold, w, z, tpct in [task]
    }
    rows = []
    fail_counts = {}
    progress = tqdm(as_completed(futures_map), total=len(futures_map), desc=desc,
                     unit="node", mininterval=15.0, maxinterval=30.0)
    for future in progress:
        tp, sl, hold_hours, w, z_thresh, tpct = futures_map[future]
        try:
            res = future.result()
        except Exception as e:
            fail_counts["CRASH"] = fail_counts.get("CRASH", 0) + 1
            continue
        status = res.get("status")
        if status != "SUCCESS":
            fail_counts[status] = fail_counts.get(status, 0) + 1
            continue
        alpha, num_trades, wr, comp_ret, wtw, node_cagr = res["payload"]
        rows.append({
            "take_profit": int(tp), "stop_loss": int(sl), "max_hold_hours": hold_hours,
            "window": w, "z_score_threshold": z_thresh, "trail_sell_pct": tpct,
            "trades": num_trades, "win_rate": wr, "strategy_return": comp_ret,
            "alpha_vs_spy": alpha, "cagr": node_cagr,
        })
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
        buffer.append((now_iso, ticker, strategy_name, config_version, c["window"],
                       c["z_score_threshold"], float(fixed_sl), arm_pct, trail_buy_pct,
                       trail_sell_pct, c["max_hold_hours"], entry_timing,
                       c["alpha_vs_spy"], c["trades"], now_iso, params_json))

    with sqlite3.connect(DB_PATH, timeout=60.0) as conn:
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(candidate_nodes)")}
        if 'params_json' not in existing_cols:
            conn.execute("ALTER TABLE candidate_nodes ADD COLUMN params_json TEXT")
        before = conn.execute(
            "SELECT COUNT(*) FROM candidate_nodes WHERE version=? AND ticker=? AND strategy=?",
            (config_version, ticker, strategy_name)).fetchone()[0]
        conn.executemany("""
            INSERT OR IGNORE INTO candidate_nodes
                (created_at, ticker, strategy, version, window, z, fixed_sl, arm_pct,
                 trail_buy_pct, trail_sell_pct, max_hold_hours, entry_timing,
                 robust_alpha, trades, robust_alpha_computed_at, params_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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


def main():
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
    ap.add_argument("--window", type=int, default=None,
                     help="run this single window value in ISOLATION instead of the "
                          "real production grid (module-level WINDOWS=[10,20]) -- e.g. "
                          "--window 15 to explore a midpoint value not otherwise swept. "
                          "Does not add to the standard grid, replaces it for this run.")
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
                          "campaign grid path), --window (window comes from the seed node "
                          "itself, not a separate override), and --resume-from-top100/"
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
    args = ap.parse_args()
    if args.resume_from_top100 and args.checkpoint_file:
        raise SystemExit("--resume-from-top100 and --checkpoint-file are mutually exclusive "
                          "(one tests a narrow top-100-only dataset, the other a full "
                          "Phase1+Phase2 checkpoint) -- pick one.")

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
        if args.resume_from_top100:
            _seed_conflicts.append("--resume-from-top100")
        if args.checkpoint_file:
            _seed_conflicts.append("--checkpoint-file")
        if args.ticker is not None:
            _seed_conflicts.append("--ticker")
        if _seed_conflicts:
            raise SystemExit(
                f"--seed-watch-list-id is mutually exclusive with {', '.join(_seed_conflicts)} "
                f"-- seed mode derives strategy/fixed_sl/window directly from the real live "
                f"watch_list row, it doesn't take a separate grid-override or window-override "
                f"path. Pick one.")

    if args.window is not None:
        global WINDOWS
        WINDOWS = [args.window]
        print(f"Window override: running window={args.window} in ISOLATION "
              f"(replaces standard grid {[10, 20]})")

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

    seed = None
    if args.seed_watch_list_id is not None:
        seed = _load_seed_node(args.seed_watch_list_id)
        strategy_name = seed["strategy_name"]
        fixed_sl_list = [seed["fixed_sl"]]
        # TICKER already declared global above (--ticker override block) -- a second
        # `global TICKER` here after that block's assignment raises SyntaxError ("assigned
        # to before global declaration"), a real Python quirk confirmed while building this:
        # once a name is globalled+assigned in one place in a function, a later `global`
        # statement for the SAME name is illegal, even in a mutually-exclusive branch that
        # can never run in the same call. TICKER stays covered by the earlier declaration.
        global ENTRY_TIMING, Z_THRESHOLDS, HOLD_TIME_CAPS
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
    version = "bench-inmemory-v6" + ("-massive" if DATA_SOURCE == "massive" else "") + window_version_suffix(START, END)
    if args.seed_watch_list_id is not None:
        # Seed-mode discriminator (2026-08-29, paired review): without this, sweep_run_log's
        # dedup key (ticker/strategy/fixed_sl/windows/version) can't tell a seed-mode smoke
        # test apart from a real full-grid campaign at the same coordinates -- a seed run
        # could silently no-op a genuine full-grid run's "already done" check (or vice versa),
        # and the resulting candidate_nodes rows would be indistinguishable from real
        # full-grid campaign output under the default date range.
        version += f"-seed{args.seed_watch_list_id}"

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
    # Keyed on WINDOWS too (not just strategy/fixed_sl) -- found live 2026-08-29: a
    # --window override run silently loaded a stale checkpoint from an earlier
    # standard-grid ([10,20]) run under the same strategy/fixed_sl, skipping Phase1+2
    # entirely and never actually computing the overridden window at all. The checkpoint
    # itself is explicitly documented as a "dev-iteration" convenience, not a production
    # artifact -- this key just makes that convenience safe to use across different grids.
    _windows_key = "-".join(str(w) for w in WINDOWS)
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
    checkpoint_path = args.checkpoint_file or os.path.join(
        _job_tmp, f"bench_phase12_checkpoint_{TICKER}_{strategy_name}_{fixed_sl}_w{_windows_key}"
                  f"{_range_key}{_seed_key}.parquet")

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

    if _seed_task is not None:
        # Seed-mode smoke test (2026-08-29): Phase1 reduced to exactly the one real
        # live node's reverse-mapped task tuple, bypassing the full grid cross-product
        # entirely. Everything downstream (Phase2 mesh, Phase2.5-cliffbox, promotion)
        # is untouched -- it already just consumes whatever ends up in df1.
        phase1_tasks = [_seed_task]
        print(f"Seed mode: Phase1 task list reduced to ONE cell: {_seed_task} "
              f"(bypasses the {len(TAKE_PROFITS) * len(STOP_LOSSES) * len(HOLD_TIME_CAPS) * len(TRAIL_PCTS) * len(Z_THRESHOLDS) * len(WINDOWS):,}-cell full grid)")
    else:
        phase1_tasks = [(int(tp), int(sl), int(hold), int(w), float(z), float(tpct))
                         for z in Z_THRESHOLDS for w in WINDOWS
                         for tp in TAKE_PROFITS for sl in STOP_LOSSES
                         for hold in HOLD_TIME_CAPS for tpct in TRAIL_PCTS]

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
          print(f"Phase1-coarse (in-memory) done: {len(phase1_rows):,} rows in {t1 - t0:.1f}s "
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
          insurance_df = pd.concat([global_top] + region_rows, ignore_index=True).drop_duplicates(
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

      # Island-center detection, straight off the in-memory DataFrame -- same shape as
      # _phase2_island_gt_tasks's per-(w,z,tpct) loop, minus the SQL read.
      phase2_tasks = set()
      for z in Z_THRESHOLDS:
          for w in WINDOWS:
              for tpct in TRAIL_PCTS:
                  df_wz = df1[(df1["window"] == w) & (df1["z_score_threshold"] == z)
                              & (df1["trail_sell_pct"] == tpct)]
                  if df_wz.empty:
                      continue
                  centers = pick_island_centers(df_wz, rank_col="cagr")
                  if len(centers) < N_ISLANDS:
                      print(f"  WARNING: (w={w} z={z} tpct={tpct}) only found {len(centers)} "
                            f"island(s), expected {N_ISLANDS} -- check for a data gap in "
                            f"this slice, not necessarily fatal (a real scope can "
                            f"legitimately have fewer distinct islands than N_ISLANDS).")
                  for (tp_c, sl_c) in centers:
                      for tp in range(max(1, tp_c - FINE_RADIUS), min(30, tp_c + FINE_RADIUS) + 1):
                          for sl in range(max(1, sl_c - FINE_RADIUS), min(30, sl_c + FINE_RADIUS) + 1):
                              for hold in HOLD_TIME_CAPS:
                                  phase2_tasks.add((tp, sl, int(hold), int(w), float(z), float(tpct)))
      print(f"Phase2-island mesh (in-memory): {len(phase2_tasks):,} cells "
            f"({N_ISLANDS} islands x {len(WINDOWS)}w x {len(Z_THRESHOLDS)}z x {len(TRAIL_PCTS)} trail_pcts, +-{FINE_RADIUS} box)")

      t2 = time.time()
      phase2_rows = _dispatch(pool, phase2_tasks, TICKER, strategy_name, version, fixed_sl, spy_bh,
                               desc="Phase2-island (in-memory)")
      t3 = time.time()
      print(f"Phase2-island (in-memory) done: {len(phase2_rows):,} rows in {t3 - t2:.1f}s "
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
    centers25 = pick_island_centers(df_full, rank_col="cagr")
    if len(centers25) < N_ISLANDS:
        print(f"  WARNING (Phase2.5 seed detection): only found {len(centers25)} island(s) "
              f"across the full scope, expected {N_ISLANDS} -- check df_full for a real gap.")
    tb_cols = ["cagr"] + [("trail_sell_pct" if c == "tpct" else c) for c, _ in GT_CANDIDATE_TIEBREAK]
    tb_asc = [False] + [asc for _, asc in GT_CANDIDATE_TIEBREAK]

    phase25_tasks = set()
    seed_count = 0
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
            tp_c2, sl_c2 = int(cand["take_profit"]), int(cand["stop_loss"])
            hold_c, w_c, z_c = int(cand["max_hold_hours"]), int(cand["window"]), float(cand["z_score_threshold"])
            tpct_c = float(cand["trail_sell_pct"])
            if tpct_c in TRAIL_PCTS:
                idx = TRAIL_PCTS.index(tpct_c)
                tpct_neighbors = TRAIL_PCTS[max(0, idx - 1): idx + 2]
            else:
                tpct_neighbors = [tpct_c]
            for tp in range(max(1, tp_c2 - CLIFF_RADIUS), min(30, tp_c2 + CLIFF_RADIUS) + 1):
                for sl in range(max(1, sl_c2 - CLIFF_RADIUS), min(30, sl_c2 + CLIFF_RADIUS) + 1):
                    for hold in [h for h in HOLD_TIME_CAPS if abs(h - hold_c) <= 7]:
                        for tpct in tpct_neighbors:
                            phase25_tasks.add((tp, sl, hold, w_c, z_c, float(tpct)))

    print(f"\nPhase2.5-cliffbox (in-memory): {seed_count} seed cells across "
          f"{len(centers25)} island(s), {len(phase25_tasks):,} cliff-box cells to verify")

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
    print(f"Phase2.5-cliffbox (in-memory) done: {len(phase25_rows):,} rows in {t5 - t4:.1f}s "
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
    final_centers = pick_island_centers(df_final, rank_col="cagr")
    final_candidates = []
    claimed = {}  # coordinate key -> candidate dict already added (tracks convergence)
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
                "converged_from_islands": [(tp_c, sl_c)],
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

    # Cliff-safety verdict per candidate: worst_neighbor_cagr, min(cagr) among cells
    # within +-CLIFF_RADIUS tp/sl of the candidate (same hold/window/z/trail_pct),
    # sourced from df_final (Phase1+Phase2+Phase2.5 combined -- the full evidence pool
    # already in memory). This is the scalar we actually persist, per the "compute the
    # full box, persist only the verdict" design -- NOT stored in backtest_cache today
    # (no such column exists there; would need a real schema addition to land for
    # real, printed here only as the benchmark's proof-of-concept).
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

    print(f"\n=== Final {len(final_candidates)} candidates (post-Phase2.5, {N_ISLANDS} islands x top-3) ===")
    for c in sorted(final_candidates, key=lambda r: -r["cagr"]):
        print(f"  island{c['island']}: TP={c['take_profit']} SL={c['stop_loss']} "
              f"hold={c['max_hold_hours']}h w={c['window']} z={c['z_score_threshold']} "
              f"trail_pct={c['trail_sell_pct']} -> cagr={c['cagr']:.2f}% trades={c['trades']} "
              f"| worst_neighbor_cagr={c['worst_neighbor_cagr']:.2f}% "
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


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
