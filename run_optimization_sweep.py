import sys
import os
import logging
import json
import argparse
import re
import sqlite3
import time
from pathlib import Path
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor, as_completed
import numpy as np
import pandas as pd
from tqdm import tqdm

from backtester import (run_backtest_dispatch,
                        prep_inputs, _simulate, _simulate_limit, _simulate_trail, _simulate_trail_buy,
                        _simulate_trail_both, _simulate_limit_trail, _simulate_close_limitexit,
                        run_backtest_ground_truth, prep_minute_inputs,
                        apply_addon_overlay_ground_truth, simulate_drought_overlay_ground_truth,
                        drought_included_excluded_ground_truth)
import strategies
from db_cache import refresh_dropdown_cache, refresh_pivot_cache, refresh_cliff_grid_cache

CACHE_DIR    = Path("./cache/research")
OPTO_LOG_DIR = Path("./logs")
OPTO_LOG_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = CACHE_DIR / "trading_universe.db"

FINE_RADIUS    = 4
N_ISLANDS      = 3
ISLAND_MIN_SEP = 6
CLIFF_RADIUS   = 2
# Same threshold as idx_bc_cagr_candidates' partial index (rebuild_indexes()) -- gates
# Phase4 (drought/add-on overlay) eligibility in derive_phase25_candidates_ground_truth,
# NOT whether an island gets cliff-boxed at all (2026-08-23 redesign: the original
# 2026-08-22 version of this gate blocked cliff-boxing itself, which had no v5 precedent
# and risked dropping a real near-miss -- e.g. a coarse 33 sitting next to a genuine
# cliff-safe 55 -- before refinement ever got a chance to find it).
PHASE25_ISLAND_CAGR_MIN = 30
# Separate, much more conservative gate (2026-08-23): skips cliff-boxing ONLY for an
# island whose coarse top cell is already negative -- unlike PHASE25_ISLAND_CAGR_MIN's
# 50% bar, a negative coarse cell is unlikely to be hiding a genuinely better neighbor
# (it already lost to its own island's own top rank), so this is a real compute-saving
# measure without the near-miss risk the 50% cliff-boxing gate had.
PHASE25_ISLAND_CLIFFBOX_CAGR_MIN = 0

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(OPTO_LOG_DIR / "matrix_execution.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("MatrixSweepEngine")


def init_idempotent_db():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS backtest_cache (
            strategy TEXT, version TEXT, ticker TEXT, window INTEGER,
            max_hold_hours INTEGER, take_profit INTEGER, stop_loss INTEGER,
            trades INTEGER, win_rate REAL, strategy_return REAL,
            alpha_vs_spy REAL, asset_bh REAL, spy_bh REAL, run_timestamp TEXT,
            z_score_threshold REAL DEFAULT 2.0,
            PRIMARY KEY (strategy, version, ticker, window, max_hold_hours, take_profit, stop_loss, z_score_threshold)
        )
    """)
    try:
        cursor.execute("ALTER TABLE backtest_cache ADD COLUMN z_score_threshold REAL DEFAULT 2.0")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE backtest_cache ADD COLUMN fixed_sl REAL DEFAULT 0")
    except Exception:
        pass
    try:
        # win_rate only counts Result=='WIN' (excludes profitable TIME-exit 'TWIN'
        # trades) — win_twin_rate = (WIN+TWIN)/trades is the real "did this trade
        # make money" rate. Added 2026-07-05 after finding a KORU node whose 21%
        # win_rate was misleading (71% of its trades were actually profitable, just
        # via TWIN). Old rows keep win_twin_rate=0 (not recomputed retroactively —
        # would require re-simulating all historical trades).
        cursor.execute("ALTER TABLE backtest_cache ADD COLUMN win_twin_rate REAL DEFAULT 0")
    except Exception:
        pass
    # v3.x reparameterization (2026-07-05): stop_loss now always means real SL;
    # trail_buy_pct/trail_pct get real columns instead of overloading stop_loss.
    # PK must be rebuilt to add them (SQLite can't ALTER a PRIMARY KEY in place) —
    # old v1.x/v2.x rows are copied over untouched (trail_buy_pct/trail_pct=0 for
    # them; their stop_loss keeps its old, overloaded meaning, still correctly
    # interpreted via docs/design.md's lookup table). No value transformation,
    # straight copy — only new writes use the corrected semantics.
    bc_cols = {r[1] for r in cursor.execute("PRAGMA table_info(backtest_cache)").fetchall()}
    if 'trail_buy_pct' not in bc_cols:
        logger.info("Migrating backtest_cache schema: adding trail_buy_pct/trail_sell_pct, rebuilding PK...")
        before_count = cursor.execute("SELECT COUNT(*) FROM backtest_cache").fetchone()[0]
        cursor.executescript("""
            CREATE TABLE backtest_cache_new (
                strategy TEXT, version TEXT, ticker TEXT, window INTEGER,
                max_hold_hours INTEGER, take_profit INTEGER, stop_loss INTEGER,
                trades INTEGER, win_rate REAL, strategy_return REAL,
                alpha_vs_spy REAL, asset_bh REAL, spy_bh REAL, run_timestamp TEXT,
                z_score_threshold REAL DEFAULT 2.0, fixed_sl REAL DEFAULT 0,
                trail_buy_pct REAL DEFAULT 0, trail_sell_pct REAL DEFAULT 0,
                PRIMARY KEY (strategy, version, ticker, window, max_hold_hours,
                             take_profit, stop_loss, z_score_threshold,
                             trail_buy_pct, trail_sell_pct)
            );
            INSERT INTO backtest_cache_new
                (strategy, version, ticker, window, max_hold_hours, take_profit, stop_loss,
                 trades, win_rate, strategy_return, alpha_vs_spy, asset_bh, spy_bh,
                 run_timestamp, z_score_threshold, fixed_sl, trail_buy_pct, trail_sell_pct)
            SELECT strategy, version, ticker, window, max_hold_hours, take_profit, stop_loss,
                   trades, win_rate, strategy_return, alpha_vs_spy, asset_bh, spy_bh,
                   run_timestamp, z_score_threshold, fixed_sl, 0, 0
            FROM backtest_cache;
            DROP TABLE backtest_cache;
            ALTER TABLE backtest_cache_new RENAME TO backtest_cache;
        """)
        after_count = cursor.execute("SELECT COUNT(*) FROM backtest_cache").fetchone()[0]
        if after_count != before_count:
            raise RuntimeError(f"backtest_cache migration row count mismatch: {before_count} -> {after_count}")
        logger.info(f"Migration complete: {after_count:,} rows carried over unchanged.")
        bc_cols = {r[1] for r in cursor.execute("PRAGMA table_info(backtest_cache)").fetchall()}

    # take_profit split (2026-07-07): NULL for TrailingBothZScoreBreakout (real value
    # lives in arm_sell_pct instead — mirrors active_signals.py's live-side split, see
    # _tp_or_arm_pct there). axis_tp is a write-time-computed, always-non-NULL mirror of
    # whichever of the two actually holds the swept grid value — SQLite's composite PK
    # can't dedupe on take_profit once it's NULL (NULL never equals NULL), so axis_tp
    # (not take_profit) is what the PK and all internal island/cliff-box queries use.
    if 'arm_sell_pct' not in bc_cols or 'axis_tp' not in bc_cols:
        logger.info("Migrating backtest_cache schema: adding arm_sell_pct/axis_tp, rebuilding PK...")
        before_count = cursor.execute("SELECT COUNT(*) FROM backtest_cache").fetchone()[0]
        # arm_sell_pct may already exist from an earlier ad-hoc migration (with
        # take_profit already NULLed for TrailingBoth rows) — read it if present,
        # else treat it as never having existed (NULL for every row).
        src_arm_sell_pct = "arm_sell_pct" if 'arm_sell_pct' in bc_cols else "NULL"
        cursor.executescript(f"""
            CREATE TABLE backtest_cache_new (
                strategy TEXT, version TEXT, ticker TEXT, window INTEGER,
                max_hold_hours INTEGER, take_profit INTEGER, stop_loss INTEGER,
                trades INTEGER, win_rate REAL, strategy_return REAL,
                alpha_vs_spy REAL, asset_bh REAL, spy_bh REAL, run_timestamp TEXT,
                z_score_threshold REAL DEFAULT 2.0, fixed_sl REAL DEFAULT 0,
                trail_buy_pct REAL DEFAULT 0, trail_sell_pct REAL DEFAULT 0,
                win_twin_rate REAL DEFAULT 0, arm_sell_pct REAL, axis_tp REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (strategy, version, ticker, window, max_hold_hours,
                             axis_tp, stop_loss, z_score_threshold,
                             trail_buy_pct, trail_sell_pct)
            );
            INSERT INTO backtest_cache_new
                (strategy, version, ticker, window, max_hold_hours, take_profit, stop_loss,
                 trades, win_rate, strategy_return, alpha_vs_spy, asset_bh, spy_bh,
                 run_timestamp, z_score_threshold, fixed_sl, trail_buy_pct, trail_sell_pct,
                 win_twin_rate, arm_sell_pct, axis_tp)
            SELECT strategy, version, ticker, window, max_hold_hours,
                   CASE WHEN strategy = 'TrailingBothZScoreBreakout' THEN NULL ELSE take_profit END,
                   stop_loss, trades, win_rate, strategy_return, alpha_vs_spy, asset_bh, spy_bh,
                   run_timestamp, z_score_threshold, fixed_sl, trail_buy_pct, trail_sell_pct,
                   COALESCE(win_twin_rate, 0),
                   CASE WHEN strategy = 'TrailingBothZScoreBreakout' THEN COALESCE({src_arm_sell_pct}, take_profit) ELSE NULL END,
                   COALESCE(take_profit, {src_arm_sell_pct})
            FROM backtest_cache;
            DROP TABLE backtest_cache;
            ALTER TABLE backtest_cache_new RENAME TO backtest_cache;
        """)
        after_count = cursor.execute("SELECT COUNT(*) FROM backtest_cache").fetchone()[0]
        if after_count != before_count:
            raise RuntimeError(f"backtest_cache axis_tp migration row count mismatch: {before_count} -> {after_count}")
        logger.info(f"Migration complete: {after_count:,} rows carried over, axis_tp backfilled from take_profit.")
        bc_cols = {r[1] for r in cursor.execute("PRAGMA table_info(backtest_cache)").fetchall()}

    # v4 (2026-07-14): fill-optimism resolution bounds + entry_timing axis — see
    # docs/backlog_cache.md fill-optimism item, plan at
    # /home/pkim/.claude/plans/rustling-bubbling-hennessy.md. entry_timing is a
    # campaign-level constant (like fixed_sl/stop_loss, not swept within a run) so
    # it goes in the PK. 'possible' is the existing alpha_vs_spy/strategy_return
    # (Low-before-High assumption, unchanged) — 'pessimistic' (High-before-Low
    # assumption) and 'certain' (no-guessing, only provable fills) are new,
    # plain data columns riding alongside, not PK members. None of the three is a
    # rigorous bound on the others (see _simulate_trail_both docstring) — island
    # search ranks by MIN across all three, not 'possible' alone, so a node only
    # gets picked when it's robust under every resolution, not just the
    # optimistic default.
    if 'entry_timing' not in bc_cols:
        logger.info("Migrating backtest_cache schema: adding entry_timing/pessimistic+certain bound columns, rebuilding PK...")
        before_count = cursor.execute("SELECT COUNT(*) FROM backtest_cache").fetchone()[0]
        cursor.executescript("""
            CREATE TABLE backtest_cache_new (
                strategy TEXT, version TEXT, ticker TEXT, window INTEGER,
                max_hold_hours INTEGER, take_profit INTEGER, stop_loss INTEGER,
                trades INTEGER, win_rate REAL, strategy_return REAL,
                alpha_vs_spy REAL, asset_bh REAL, spy_bh REAL, run_timestamp TEXT,
                z_score_threshold REAL DEFAULT 2.0, fixed_sl REAL DEFAULT 0,
                trail_buy_pct REAL DEFAULT 0, trail_sell_pct REAL DEFAULT 0,
                win_twin_rate REAL DEFAULT 0, arm_sell_pct REAL, axis_tp REAL NOT NULL DEFAULT 0,
                entry_timing TEXT NOT NULL DEFAULT 'close',
                strategy_return_pessimistic REAL, alpha_vs_spy_pessimistic REAL,
                strategy_return_certain REAL, alpha_vs_spy_certain REAL,
                PRIMARY KEY (strategy, version, ticker, window, max_hold_hours,
                             axis_tp, stop_loss, z_score_threshold,
                             trail_buy_pct, trail_sell_pct, entry_timing)
            );
            INSERT INTO backtest_cache_new
                (strategy, version, ticker, window, max_hold_hours, take_profit, stop_loss,
                 trades, win_rate, strategy_return, alpha_vs_spy, asset_bh, spy_bh,
                 run_timestamp, z_score_threshold, fixed_sl, trail_buy_pct, trail_sell_pct,
                 win_twin_rate, arm_sell_pct, axis_tp, entry_timing,
                 strategy_return_pessimistic, alpha_vs_spy_pessimistic,
                 strategy_return_certain, alpha_vs_spy_certain)
            SELECT strategy, version, ticker, window, max_hold_hours, take_profit, stop_loss,
                   trades, win_rate, strategy_return, alpha_vs_spy, asset_bh, spy_bh,
                   run_timestamp, z_score_threshold, fixed_sl, trail_buy_pct, trail_sell_pct,
                   win_twin_rate, arm_sell_pct, axis_tp, 'close', NULL, NULL, NULL, NULL
            FROM backtest_cache;
            DROP TABLE backtest_cache;
            ALTER TABLE backtest_cache_new RENAME TO backtest_cache;
        """)
        after_count = cursor.execute("SELECT COUNT(*) FROM backtest_cache").fetchone()[0]
        if after_count != before_count:
            raise RuntimeError(f"backtest_cache v4 migration row count mismatch: {before_count} -> {after_count}")
        logger.info(f"Migration complete: {after_count:,} rows carried over, entry_timing backfilled to 'close'.")

    # phase (2026-07-15): tags each row with whichever phase (Phase1-Coarse/
    # Phase2-Island/Phase2.5-CliffBox/Phase3-Full) first computed it, so a
    # "does a later phase ever actually improve on an earlier one" analysis is a
    # simple query. Plain data column, no PK rebuild. Originally added by hand
    # against the live DB — codified here so a fresh DB doesn't fail on INSERT.
    try:
        cursor.execute("ALTER TABLE backtest_cache ADD COLUMN phase TEXT")
    except Exception:
        pass
    # generation (2026-07-15): for Phase2-Island rows only, which island-search
    # generation (1-indexed, config.execution.max_generations) produced this row —
    # lets the same "does it earn its cost" question be asked of the generation
    # loop, not just the phase pipeline. NULL for all other phases (single-pass).
    try:
        cursor.execute("ALTER TABLE backtest_cache ADD COLUMN generation INTEGER")
    except Exception:
        pass
    # sweep_run_id (2026-08-11): links a row back to the sweep_runs invocation
    # that computed it -- the real gap behind "which kernel/git-commit produced
    # this candidate_nodes pick," raised 2026-08-11 after the watch_list_
    # candidate_link table made the wl_id<->candidate_nodes link real but left
    # candidate_nodes<->sweep-run untraceable. Nullable, no PK change, NO
    # BACKFILL of existing rows (same convention as phase/generation above) --
    # historical rows predate this column and stay NULL; only rows written by
    # a sweep run that passes run_id through dispatch_parallel_grid going
    # forward get stamped.
    try:
        cursor.execute("ALTER TABLE backtest_cache ADD COLUMN sweep_run_id INTEGER")
    except Exception:
        pass
    # kernel_version (2026-08-21): self-documents which kernel logic produced a row --
    # the already-designed-but-unbuilt idea from project_kernel_versioning_idea memory,
    # triggered now by docs/plans/ground_truth_kernel_rebuild.md's v6 campaign (real
    # minute-resolution kernel, backtester.run_backtest_ground_truth) landing alongside
    # the existing hourly kernel (backtester.run_backtest_dispatch). Nullable, no PK
    # change, NO BACKFILL (same convention as phase/generation/sweep_run_id above) --
    # every pre-2026-08-21 row predates this column and stays NULL (implicitly "hourly",
    # the only kernel that existed then); only a v6 campaign's writes get stamped
    # 'ground_truth_v6' explicitly. Plain data column, not part of version scoping --
    # 'v6' as a version string already isolates ground-truth rows from v4/v5 hourly
    # rows at the (strategy, version, ticker, ...) cache-key level; this column is the
    # self-documentation the plan asked for, not a second isolation mechanism.
    try:
        cursor.execute("ALTER TABLE backtest_cache ADD COLUMN kernel_version TEXT")
    except Exception:
        pass
    try:
        # cagr (2026-08-22): real, properly-annualized CAGR -- alpha_vs_spy/
        # strategy_return are raw over-the-window returns, not annualized, so they
        # aren't comparable across campaigns with different window lengths and
        # overstate a multi-year window's apparent edge relative to a 1yr one.
        # v6-only (NULL for pre-v6 rows, not backfilled -- see feedback_backtest_
        # cache_axis_column_remapping memory / the 2026-08-07 deferred schema item
        # for why this project treats v6 as the clean cutover point rather than
        # migrating historical rows). Computed in Python at write time (same
        # formula as scripts/annualized_alpha_report.py::cagr()), not a SQL
        # expression -- ordinary stored column, ordinary index, no generated-
        # column/JSON complexity needed for this one. Placed here (after all PK
        # rebuild migrations, alongside phase/generation/sweep_run_id/kernel_version)
        # rather than earlier in this function, since those rebuilds' explicit
        # column lists would otherwise silently drop a plain ALTER-added column
        # added before them on any DB that still needs to run them.
        #
        # NULL cagr means one of three things, not just "pre-v6 row": (1) a row
        # written before this column existed (pre-v6 AND early v6, until a
        # cache-hit self-heal or fresh recompute backfills it -- see the
        # cached_map read path in dispatch_parallel_grid_ground_truth), (2) a v6
        # NO_TRADES/error node (span_days/years never computed), or (3) a v6 node
        # with a degenerate zero-or-negative-span window (years<=0) or an
        # unrepresentable (OverflowError) annualization. Don't use `cagr IS NULL`
        # as a clean "is this pre-v6" discriminator -- check kernel_version too.
        cursor.execute("ALTER TABLE backtest_cache ADD COLUMN cagr REAL")
    except Exception:
        pass

    # backtest_phase1 (2026-08-25): ephemeral scratch table for the schema-v2 experiment
    # (docs/plans/backtest_schema_v2_phase_tables.md) -- a PARALLEL destination for
    # Phase1-Coarse-GT data, written/read only by the new *_phase1() functions below
    # (dispatch_parallel_grid_ground_truth_phase1, _phase1_coarse_gt_status_phase1,
    # _phase2_island_gt_tasks_phase1). Does NOT touch backtest_cache or any existing
    # function/call site -- the real (currently-swept) GT pipeline is byte-for-byte
    # unchanged; this exists so the new table/read/write shape can be tested side-by-side
    # against the real pipeline's output before anything is ever cut over. Same column
    # set/PK shape as backtest_cache's GT-relevant columns, since the parallel functions
    # are near-verbatim copies of their backtest_cache counterparts -- easiest to diff
    # output when the shapes match exactly. Explicitly ephemeral/droppable per campaign
    # (not a permanent growing table like backtest_cache) -- nothing here is precious.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS backtest_phase1 (
            strategy TEXT, version TEXT, ticker TEXT, window INTEGER,
            max_hold_hours INTEGER, take_profit INTEGER, stop_loss INTEGER,
            trades INTEGER, win_rate REAL, strategy_return REAL,
            alpha_vs_spy REAL, asset_bh REAL, spy_bh REAL, run_timestamp TEXT,
            z_score_threshold REAL DEFAULT 2.0, fixed_sl REAL DEFAULT 0,
            trail_buy_pct REAL DEFAULT 0, trail_sell_pct REAL DEFAULT 0,
            win_twin_rate REAL DEFAULT 0, arm_sell_pct REAL, axis_tp REAL NOT NULL DEFAULT 0,
            entry_timing TEXT NOT NULL DEFAULT 'open_check',
            strategy_return_pessimistic REAL, alpha_vs_spy_pessimistic REAL,
            strategy_return_certain REAL, alpha_vs_spy_certain REAL,
            phase TEXT, generation INTEGER, sweep_run_id INTEGER,
            kernel_version TEXT, cagr REAL,
            PRIMARY KEY (strategy, version, ticker, window, max_hold_hours,
                         axis_tp, stop_loss, z_score_threshold,
                         trail_buy_pct, trail_sell_pct, entry_timing)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sl_sweep_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL, strategy TEXT NOT NULL, version TEXT NOT NULL,
            stop_loss REAL NOT NULL, trail_sell_pct REAL NOT NULL, entry_timing TEXT NOT NULL,
            best_alpha REAL, best_alpha_pessimistic REAL, best_alpha_certain REAL,
            worst_neighbor_alpha REAL,
            best_node_tp REAL, best_node_hold INTEGER, best_node_trail_buy_pct REAL,
            n_islands INTEGER, any_cliff_safe INTEGER, run_timestamp TEXT NOT NULL,
            UNIQUE(ticker, strategy, version, stop_loss, trail_sell_pct, entry_timing, run_timestamp)
        )
    """)
    # worst_neighbor_alpha_i3 (2026-08-08): this table existed with a
    # worst_neighbor_alpha column (i2, matching this module's own
    # CLIFF_RADIUS=2) but was never actually populated -- Checkpoint 2 computed
    # the value live every campaign and only logged it, never persisted it,
    # so every downstream consumer (top_safe_nodes.py etc, CLIFF_RADIUS=3) had
    # to slowly re-derive it from scratch. Fixed same day: Checkpoint 2 now
    # inserts here (scoped to best_alpha > 100%, "should be fewer nodes" --
    # user's call, keeps this cheap rather than storing for every candidate),
    # with i3 added alongside the existing i2 column so both radii are on hand
    # without a second slow re-scan.
    try:
        cursor.execute("ALTER TABLE sl_sweep_summary ADD COLUMN worst_neighbor_alpha_i3 REAL")
    except Exception:
        pass
    # window/z_score_threshold/fixed_sl (2026-08-0x): without these, a row can't be
    # mapped back to which campaign/node it describes -- the CRITICAL gap that led
    # to disabling this table's persistence 2026-08-08 (confirmed: JNUG/TrailingExit/
    # stop_loss=1 produced 3 indistinguishable rows from 3 different fixed_sl
    # campaigns before this fix).
    for col, coltype in (('window', 'INTEGER'), ('z_score_threshold', 'REAL'), ('fixed_sl', 'REAL')):
        try:
            cursor.execute(f"ALTER TABLE sl_sweep_summary ADD COLUMN {col} {coltype}")
        except Exception:
            pass

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sweep_runs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            version       TEXT NOT NULL,
            started_at    TEXT NOT NULL,
            finished_at   TEXT,
            status        TEXT NOT NULL DEFAULT 'RUNNING',
            strategies    TEXT NOT NULL,
            tickers       TEXT NOT NULL,
            phase_reached TEXT,
            config_json   TEXT NOT NULL,
            notes         TEXT,
            log_file      TEXT
        )
    """)
    # git_commit/kernel_dirty (2026-08-11): the actual "identifiable sweep run"
    # piece -- version (e.g. 'v5') is a data-versioning tag the user chooses,
    # not a code identity, so two runs tagged the same version can still have
    # computed results with different backtester.py/strategies.py code (this
    # has happened for real -- see CLAUDE.md's kernel-fix-mid-campaign
    # history). git_commit is HEAD's real sha at start_sweep_run time;
    # kernel_dirty flags whether backtester.py/strategies.py had uncommitted
    # changes when the run started (a real run against not-yet-committed
    # kernel code is a genuine traceability gap worth flagging, not silently
    # trusting the commit hash as the whole story).
    # try/except per column (2026-08-13 paired review, LOW-MEDIUM): the bare
    # if-not-in-cols-then-ALTER pattern below is check-then-act, not atomic across
    # processes -- two invocations starting simultaneously on the first run after
    # a schema change (e.g. the Streamlit UI and a queued sweep step) could both
    # see the column missing and the loser crashes on a real, uncaught
    # "duplicate column name" error. Matches the already-safe backtest_cache
    # ALTER pattern above.
    sr_cols = {r[1] for r in cursor.execute("PRAGMA table_info(sweep_runs)").fetchall()}
    if "git_commit" not in sr_cols:
        try:
            cursor.execute("ALTER TABLE sweep_runs ADD COLUMN git_commit TEXT")
        except Exception:
            pass
    if "kernel_dirty" not in sr_cols:
        try:
            cursor.execute("ALTER TABLE sweep_runs ADD COLUMN kernel_dirty INTEGER")
        except Exception:
            pass
    # data_start/data_end (2026-08-12): git_commit/kernel_dirty above answer "what code
    # produced this run" -- these answer "what data did it see." Found needed for real:
    # JNUG's backtest_cache rows nearly tripled trade count between two runs 3 days apart
    # (2026-08-08 vs 2026-08-11) with no bad tick, no split-guard rescale logged, and no
    # way to tell what the raw hourly CSV actually contained at either run's moment --
    # only *when* the run happened, not what data window it loaded. This doesn't solve
    # that (the CSV itself still has zero retention/versioning -- a snapshot would be
    # needed for that), but it stops the next occurrence from being unexplainable in the
    # same way: at least the loaded date range is on file per run.
    #
    # NEITHER column is backfilled onto pre-2026-08-13 rows (2026-08-13 paired review,
    # CRITICAL, corrected same day): data_start WAS backfilled from each ticker's current
    # CSV once, on the "a ticker's earliest cached bar never moves" assumption -- false.
    # scripts/backfill_hourly_history.py (built 2026-08-11, one day earlier) explicitly
    # re-extends a ticker's history backward on demand and was run across 218 tickers that
    # day; JNUG's own earliest bar moved from 2025-06-27 to 2023-09-12 as a result. The
    # backfill silently stamped that 2023-09-12 date onto JNUG's 2026-08-08 sweep_runs rows
    # -- runs that, per docs/deep_backlog.md's own 2026-08-08 entry, had zero data before
    # 2025-06-27 at the time they ran. That's not an approximation, it's a fabricated
    # provenance record on exactly the row the JNUG investigation would have queried.
    # Reverted: any row with data_start set but data_end still NULL was backfilled, not
    # genuinely captured (data_end is never backfilled, only set together with data_start
    # by a real start_sweep_run() call) -- all 1,128 such rows nulled back out. Every
    # pre-2026-08-13 row is now honestly NULL unless it was a real live capture.
    if "data_start" not in sr_cols:
        try:
            cursor.execute("ALTER TABLE sweep_runs ADD COLUMN data_start TEXT")
        except Exception:
            pass
    if "data_end" not in sr_cols:
        try:
            cursor.execute("ALTER TABLE sweep_runs ADD COLUMN data_end TEXT")
        except Exception:
            pass
    conn.commit()
    conn.close()


def _current_kernel_git_state():
    """Returns (git_commit, kernel_dirty) for the running process's checkout --
    best-effort: any failure (git missing, not a repo, etc.) returns (None, None)
    rather than blocking a real sweep run over a provenance nicety."""
    import subprocess
    repo_dir = str(Path(__file__).resolve().parent)
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True,
            text=True, timeout=5, check=True
        ).stdout.strip()
        dirty_out = subprocess.run(
            ["git", "status", "--porcelain", "--", "backtester.py", "strategies.py"],
            cwd=repo_dir, capture_output=True, text=True, timeout=5, check=True
        ).stdout
        return commit, int(bool(dirty_out.strip()))
    except Exception as e:
        logger.warning(f"Could not determine kernel git state: {e}")
        return None, None


def rebuild_indexes():
    """Index maintenance costs every insert during the sweep -- deferred out of
    init_idempotent_db (which used to run this at the start of every
    invocation) to here, called once at the very end of a run/queue, so bulk
    inserts across Phase1-3 aren't paying index-update overhead throughout."""
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    cursor = conn.cursor()
    cursor.execute("DROP INDEX IF EXISTS idx_bc_version_ticker")
    cursor.execute("DROP INDEX IF EXISTS idx_bc_version_ticker_z_return")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bc_version_window ON backtest_cache(version, window)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bc_version_ticker_strategy ON backtest_cache(version, ticker, strategy)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bc_version_return ON backtest_cache(version, strategy_return)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bc_ticker ON backtest_cache(ticker)")
    # cagr partial index (2026-08-22): candidate-selection real value is filtering
    # signal from noise, not raw query speed (candidate selection runs post-sweep,
    # not on any daily/nightly cadence -- see feedback_backtest_cache_axis_column_
    # remapping-adjacent 2026-08-22 conversation). Real data: only ~0.94% of a real
    # 164,640-cell SOXL campaign clears CAGR>50 -- a partial index only over rows
    # that clear the bar is a cheap, low-write-cost way to make "show me real
    # candidates" queries fast as a side effect, without needing a full-table
    # index. v6-only (cagr is NULL for pre-v6 rows, so they're naturally excluded).
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bc_cagr_candidates "
                   "ON backtest_cache(version, strategy, ticker, cagr) WHERE cagr > 50")
    conn.commit()
    conn.close()


# ── cliff_addon_cache (2026-08-23, same_bar_reentry True-flip fix) ──────────────
# Persistence for run_addon_cliff_safety_ground_truth's per-cell (core+addon) results.
# Deliberately its own table, not new columns on backtest_cache -- backtest_cache's PK
# and every downstream query in this file assume "one row per grid coordinate, core
# metrics only"; addon_alpha/addon_cagr/addon_return_floor_breached have no home there
# without touching that shared, load-bearing schema for a small (~9 candidates x their
# cliff-box neighborhoods per campaign) bounded write pattern that's structurally
# different from the full-sweep grid. kernel_version is part of the PK (not a plain
# column checked after the fact) so a stale row from an older kernel_version simply
# never matches a lookup under the current one -- same "no backfill, old rows just go
# unread" convention backtest_cache itself uses for its own kernel_version column.
#
# WHY this table exists instead of reading core numbers straight out of backtest_cache
# (the framing this fix was originally scoped under): checked and rejected -- addon_
# alpha/addon_cagr require the per-trade `trades` list from run_backtest_ground_truth
# (apply_addon_overlay_ground_truth needs real per-trade Return/exit data), and
# backtest_cache only ever stores aggregate columns (win_rate, strategy_return, cagr,
# ...), never trades. A backtest_cache hit for a neighbor coordinate's CORE row could
# never skip the run_backtest_ground_truth call this function needs anyway to get
# trades for the addon overlay -- so it would buy zero simulation-avoidance, only (now
# redundant, since same_bar_reentry=True makes the two independently-computed core
# numbers deterministic-equal anyway) cross-checking. The only real compute-avoidance
# lever for this workload is caching the FULL per-cell (core+addon) result across
# separate report runs for the same candidate/scope -- this table is that cache.
#
# data_source/date-window ARE real PK columns here (2026-08-23, paired-review
# independent-cold HIGH finding, fixed same day) -- unlike backtest_cache, which relies
# entirely on config_version encoding both (the '-massive' marker / window_version_
# suffix convention) with NO enforcement on THIS function's own call path. That gap was
# not hypothetical: verifying this same fix found a real, already-shipped caller bug
# (scripts/candidate_summary_report.py's gt_rows_for_scope calls this function via
# build_candidate_report_ground_truth with no data_source and start_date=end_date=None,
# silently evaluating a massive-tagged, windowed config_version against full-history
# yahoo data). Before this table existed, that produced wrong output for one run, once.
# With a config_version-only cache key, it would have done something worse: PERSISTED
# the wrong-source/wrong-window numbers under the correctly-tagged version string,
# where a later, correctly-invoked call would silently receive them as a "hit." Making
# data_source/start_date/end_date real key columns closes that regardless of whether
# every caller remembers to pass them consistently -- the guards below in
# run_addon_cliff_safety_ground_truth catch the loud/detectable half (data_source=
# 'massive' with no marker); this key design catches the quiet half (two calls that
# legitimately differ in source/window never collide in the cache, full stop).
def _ensure_cliff_addon_cache_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cliff_addon_cache (
            ticker TEXT, strategy TEXT, version TEXT, entry_timing TEXT, fixed_sl REAL,
            same_bar_reentry INTEGER, data_source TEXT, start_date TEXT, end_date TEXT,
            take_profit REAL, stop_loss REAL,
            max_hold_hours INTEGER, window INTEGER, z_score_threshold REAL, tpct REAL,
            kernel_version TEXT,
            n_trades INTEGER, n_armed INTEGER,
            core_alpha REAL, core_return REAL, core_cagr REAL,
            addon_alpha REAL, addon_return REAL, addon_cagr REAL,
            addon_return_floor_breached INTEGER,
            computed_at TEXT,
            PRIMARY KEY (ticker, strategy, version, entry_timing, fixed_sl, same_bar_reentry,
                         data_source, start_date, end_date,
                         take_profit, stop_loss, max_hold_hours, window, z_score_threshold,
                         tpct, kernel_version)
        )
    """)


def _load_cliff_addon_cache_map(conn, ticker, strategy_name, config_version, entry_timing,
                                 fixed_sl, data_source, start_date, end_date,
                                 kernel_version='ground_truth_v6'):
    """One bulk SELECT for this (ticker, strategy, version, entry_timing, fixed_sl,
    data_source, start_date, end_date, kernel_version) scope -- covers every
    candidate's own cell AND full cliff-box neighborhood in this campaign, not just one
    coordinate, so a repeat report run for the same ticker/scope needs exactly one
    query total, not one per cell. Keyed on same_bar_reentry=1 only -- this file's
    caller always evaluates under True now (see run_addon_cliff_safety_ground_truth's
    docstring); a row can only exist under a different value if written by code that
    predates this fix, and won't match here.

    'addon_eligible' is deliberately NOT read from the cached row into the returned map
    (2026-08-23, paired-review independent-cold HIGH finding) -- it's a pure
    account-status ANNOTATION with zero effect on the computed core/addon numbers, and
    the ticker's real watch_list account can change between when a row was written and
    when it's read back. The caller (_evaluate_cell_ground_truth_with_addon) always
    overwrites this field with the CURRENT call's addon_eligible before returning a
    cache-hit result, so a stale cached value is never actually the one that goes to
    a report."""
    start_key = pd.Timestamp(start_date).strftime('%Y-%m-%d') if start_date is not None else ''
    end_key = pd.Timestamp(end_date).strftime('%Y-%m-%d') if end_date is not None else ''
    rows = conn.execute("""
        SELECT take_profit, stop_loss, max_hold_hours, window, z_score_threshold, tpct,
               n_trades, n_armed, core_alpha, core_return, core_cagr,
               addon_alpha, addon_return, addon_cagr, addon_return_floor_breached
        FROM cliff_addon_cache
        WHERE ticker=? AND strategy=? AND version=? AND entry_timing=? AND fixed_sl=?
          AND same_bar_reentry=1 AND data_source=? AND start_date=? AND end_date=?
          AND kernel_version=?
    """, (ticker, strategy_name, config_version, entry_timing, round(float(fixed_sl), 4),
          data_source, start_key, end_key, kernel_version)).fetchall()
    cache_map = {}
    for r in rows:
        tp, sl, hold, w, z, tpct = r[0], r[1], r[2], r[3], r[4], r[5]
        key = (int(tp), round(float(sl), 4), int(hold), int(w), round(float(z), 4), round(float(tpct), 4))
        cache_map[key] = {
            'coords': (tp, sl, hold, w, z, tpct),
            'n_trades': r[6], 'n_armed': r[7],
            'core_alpha': r[8], 'core_return': r[9], 'core_cagr': r[10],
            'addon_alpha': r[11], 'addon_return': r[12], 'addon_cagr': r[13],
            'addon_return_floor_breached': bool(r[14]),
        }
    return cache_map


def _flush_cliff_addon_cache_rows(conn, ticker, strategy_name, config_version, entry_timing,
                                   fixed_sl, data_source, start_date, end_date, new_rows,
                                   kernel_version='ground_truth_v6'):
    """new_rows: list of (coord_key, result_dict) pairs collected during a
    run_addon_cliff_safety_ground_truth pass (own cell across all candidates + every
    evaluated neighbor) -- persisted in one executemany + commit at the end of the
    pass, not per-cell, to keep the write path cheap relative to the simulation cost
    it's saving on the NEXT run."""
    if not new_rows:
        return
    start_key = pd.Timestamp(start_date).strftime('%Y-%m-%d') if start_date is not None else ''
    end_key = pd.Timestamp(end_date).strftime('%Y-%m-%d') if end_date is not None else ''
    now = datetime.now().isoformat()
    params = []
    for key, result in new_rows:
        tp, sl, hold, w, z, tpct = key
        params.append((
            ticker, strategy_name, config_version, entry_timing, round(float(fixed_sl), 4),
            1, data_source, start_key, end_key, tp, sl, hold, w, z, tpct, kernel_version,
            result['n_trades'], result['n_armed'],
            result['core_alpha'], result['core_return'], result['core_cagr'],
            result['addon_alpha'], result['addon_return'], result['addon_cagr'],
            int(bool(result['addon_return_floor_breached'])),
            now,
        ))
    conn.executemany("""
        INSERT OR REPLACE INTO cliff_addon_cache
            (ticker, strategy, version, entry_timing, fixed_sl, same_bar_reentry,
             data_source, start_date, end_date,
             take_profit, stop_loss, max_hold_hours, window, z_score_threshold, tpct,
             kernel_version, n_trades, n_armed, core_alpha, core_return, core_cagr,
             addon_alpha, addon_return, addon_cagr, addon_return_floor_breached,
             computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, params)
    conn.commit()


def _tickers_data_date_range(tickers):
    """Real min/max Datetime actually present across these tickers' cached hourly CSVs,
    right now, at sweep-start time -- the "what data did this run load" counterpart to
    git_commit/kernel_dirty's "what code ran it." Best-effort per ticker (a missing/
    unreadable CSV is skipped, not fatal to starting the sweep); returns (None, None)
    if no ticker's CSV was readable at all."""
    # SPY is always included below (2026-08-13 paired review): alpha_vs_spy/spy_bh for every
    # ticker in a run comes from SPY_1h.csv via compute_bh_returns, loaded independently of
    # `tickers` -- a SPY-only data revision would otherwise be invisible to this function even
    # though it changes every alpha number the run produces.
    #
    # Everything below is inside the try (2026-08-13 paired review, HIGH): a tz-aware or
    # malformed CSV (e.g. a bootstrap fetch that skipped tz_localize(None), or a torn read from
    # data_collector.py's background sync overlapping this read) used to leave the comparison/
    # strftime calls unguarded -- a TypeError/AttributeError there would crash the whole sweep
    # before status='FAILED' is ever recorded (this runs before sys.excepthook is installed in
    # main()). Now: tz-aware timestamps are localized to naive (matching every other CSV reader
    # in this file), and a non-DatetimeIndex result (garbled first column) is caught by the same
    # try instead of reaching the comparison.
    lo, hi = None, None
    for ticker in set(tickers) | {"SPY"}:
        cache_path = CACHE_DIR / f"{ticker}_1h.csv"
        try:
            df = pd.read_csv(cache_path, index_col=0, usecols=[0], parse_dates=True)
            if len(df.index) == 0:
                continue
            idx = pd.DatetimeIndex(df.index)
            if idx.tz is not None:
                idx = idx.tz_localize(None)
            t_lo, t_hi = idx.min(), idx.max()
            if lo is None or t_lo < lo:
                lo = t_lo
            if hi is None or t_hi > hi:
                hi = t_hi
        except Exception:
            continue
    fmt = lambda ts: ts.strftime("%Y-%m-%d %H:%M:%S") if ts is not None else None
    return fmt(lo), fmt(hi)


def start_sweep_run(config_version, strategy_names, tickers, config, log_file):
    git_commit, kernel_dirty = _current_kernel_git_state()
    data_start, data_end = _tickers_data_date_range(tickers)
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sweep_runs (version, started_at, status, strategies, tickers, config_json, log_file,
                                 git_commit, kernel_dirty, data_start, data_end)
        VALUES (?, ?, 'RUNNING', ?, ?, ?, ?, ?, ?, ?, ?)
    """, (config_version, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
          json.dumps(strategy_names), json.dumps(tickers), json.dumps(config), str(log_file),
          git_commit, kernel_dirty, data_start, data_end))
    conn.commit()
    run_id = cur.lastrowid
    conn.close()
    if kernel_dirty:
        logger.warning(f"sweep_runs id={run_id}: backtester.py/strategies.py have UNCOMMITTED changes -- "
                        f"this run's results won't be reproducible from git_commit={git_commit} alone.")
    return run_id


def update_sweep_run(run_id, **fields):
    if not fields:
        return
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    cur = conn.cursor()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    cur.execute(f"UPDATE sweep_runs SET {set_clause} WHERE id = ?", (*fields.values(), run_id))
    conn.commit()
    conn.close()


def window_version_suffix(start_date, end_date):
    """Single source of truth for encoding a date window into a backtest_cache
    `version` string -- e.g. window_version_suffix('2025-06-01', '2026-06-01')
    -> '-w2025-06-01_2026-06-01'.

    backtest_cache has no date-window columns (see dispatch_parallel_grid's guard
    below) and every real downstream consumer (_campaign_scope_sql,
    identify_island_candidates, identify_full_mesh_candidates, db_cache.py's
    refresh_pivot_cache/refresh_dropdown_cache/refresh_cliff_grid_cache,
    scripts/candidate_5min_report.py, scripts/candidate_summary_report.py::
    best_node_strategy) already scopes strictly by `version` alone -- confirmed
    empirically before adopting this design. So a windowed campaign gets full,
    collision-proof isolation from every other version for free just by suffixing
    the version string, matching this project's existing v4->v5 "distinct version
    string per incompatible campaign" precedent (no schema/PK migration needed).
    Both dispatch_parallel_grid's guard and main()'s version construction must
    build the suffix through this one function -- never duplicate the format
    string, or a mismatch between the two silently defeats the guard's whole
    purpose.

    Dates are normalized to YYYY-MM-DD via pd.Timestamp before formatting (paired
    Opus review finding, 2026-08-17): without this, '2025-6-1' and '2025-06-01' --
    the identical window to _window_prep/compute_bh_returns, both of which already
    go through pd.Timestamp -- would silently produce two different version
    strings and split one campaign's cache across two disjoint, half-populated
    namespaces with no error raised anywhere. Also gives free format validation
    (raises loudly on an unparseable date) and normalizes a datetime.date/
    pd.Timestamp object caller to the same string a CLI caller would produce.

    Both-or-neither validated HERE, not just in the CLI arg parser (paired Opus
    review finding, 2026-08-17): the CLI is not the only realistic caller -- the
    planned rolling-window orchestration script (a separate peer session's work)
    will call this programmatically per window, and a partial call (e.g. a typo
    dropping one kwarg) should fail loudly at the one shared source of truth
    rather than produce a nonsense suffix like '-wNone_2026-06-01' that only
    surfaces confusingly, several calls later, inside dispatch_parallel_grid's
    guard."""
    if (start_date is None) != (end_date is None):
        raise ValueError(
            f"window_version_suffix: start_date and end_date must both be given, "
            f"or neither (got start_date={start_date!r}, end_date={end_date!r})."
        )
    return f"-w{pd.Timestamp(start_date).strftime('%Y-%m-%d')}_{pd.Timestamp(end_date).strftime('%Y-%m-%d')}"


def min_hold_version_suffix(min_hold_hours):
    """Single source of truth for encoding a min_hold_hours compliance-hold floor
    into a backtest_cache `version` string -- e.g. min_hold_version_suffix(112)
    -> '-minhold112'. Same rationale/precedent as window_version_suffix() above
    (2026-08-16/17): min_hold_hours is a campaign-level constant (fixed for the
    whole run, not swept within it, same as fixed_sl/entry_timing), and every real
    downstream consumer of backtest_cache already scopes by `version` alone -- so
    a distinct version suffix gives full, collision-proof isolation from every
    non-floored campaign for free, without a schema/PK migration. min_hold_hours=0
    (no floor, the default) gets NO suffix -- it must stay byte-identical to every
    pre-existing campaign's version string, since 0 reproduces prior kernel
    behavior exactly (see backtester.py::_simulate_trail_both's docstring) and a
    0-floor row is not a new/distinct thing that needs its own namespace.
    Both dispatch_parallel_grid's guard and main()'s version construction must
    build the suffix through this one function, mirroring window_version_suffix's
    own warning about not duplicating the format string."""
    n = int(min_hold_hours)
    if n < 0:
        raise ValueError(f"min_hold_version_suffix: min_hold_hours must be >= 0, got {n}.")
    if n == 0:
        return ""
    return f"-minhold{n}"


# Per-worker-process memo: workers are long-lived across grid nodes, and dispatch
# is one ticker at a time — without this every node re-parses the CSV and recomputes
# indicators (~18k times per ticker in Phase 3). Indicators depend only on
# (ticker, strategy, window): no strategy's generate_daily_indicators uses z.
_NODE_INPUT_CACHE = {}
_NODE_INPUT_CACHE_MAX = 6


def _load_node_inputs(ticker, strategy_class, strategy_name, w, z_thresh, start_date=None, end_date=None):
    # start_date/end_date fold into the memo key deliberately -- a differently-windowed
    # rerun must never silently serve another window's cached arrays back out (same
    # bug shape as the disabled run_phase1_coarse row-count check and the removed
    # campaign-level 'done' check, docs/deep_backlog.md). Indicators are still computed
    # off the FULL cached history below regardless of window (never truncated
    # pre-indicator) -- only the per-bar arrays get trimmed, after computation.
    key = (ticker, strategy_name, int(w), start_date, end_date)
    hit = _NODE_INPUT_CACHE.get(key)
    if hit is not None:
        return hit

    cache_path = CACHE_DIR / f"{ticker}_1h.csv"
    df_hourly_raw = pd.read_csv(cache_path, index_col=0, parse_dates=True)
    df_hourly_raw.index = pd.to_datetime(df_hourly_raw.index).tz_localize(None)
    df_hourly_raw = df_hourly_raw.sort_index()
    if df_hourly_raw.empty:
        entry = None
    else:
        close_col = 'Adj Close' if 'Adj Close' in df_hourly_raw.columns else 'Close'
        df_daily = df_hourly_raw.resample('D').last().dropna(subset=[close_col])
        strat_instance = strategy_class(window=w, z_score_threshold=z_thresh)
        df_daily_processed = strat_instance.generate_daily_indicators(df_daily)
        prep = prep_inputs(df_hourly_raw, df_daily_processed)
        if start_date is not None or end_date is not None:
            prep = _window_prep(prep, start_date, end_date)
        # df_hourly_raw/df_daily_processed are ALWAYS full-history, even when prep (3rd
        # element) is windowed -- currently safe because every run_backtest_dispatch
        # branch takes prep when supplied and ignores these first two entirely (see
        # scripts/audit_date_window_soxl.py's call pattern). A future caller reading
        # df_hourly_raw/df_daily_processed directly off this tuple must NOT assume they
        # reflect start_date/end_date -- only prep does.
        entry = (df_hourly_raw, df_daily_processed, prep)

    if len(_NODE_INPUT_CACHE) >= _NODE_INPUT_CACHE_MAX:
        _NODE_INPUT_CACHE.clear()
    _NODE_INPUT_CACHE[key] = entry
    return entry


def _window_prep(prep, start_date, end_date):
    """Trims prep_inputs()'s per-hourly-bar arrays to [start_date, end_date], leaving
    sma_arr/std_arr/trend_arr untouched -- same technique as
    scripts/paper_vs_backtest_reconcile.py::get_trades_and_bars_since (confirmed
    correct there against real KORU/YANG divergence, 2026-08-12). Those three arrays
    are indexed per calendar day via daily_idx, not by array position, so slicing them
    too would misalign every lookup -- this is what lets indicators keep using the full
    real prior history for warm-up while only the tradeable window shrinks.

    end_date is a YYYY-MM-DD date; pd.Timestamp(end_date) is midnight, so extend by a
    day or a trade signaling ON end_date is silently excluded (same truncation trap
    noted in get_backtest_trades_in_window).

    Cold-start bias (confirmed empirically against real SOXL data, both paired-review
    Opus passes, 2026-08-16): the windowed run starts FLAT at start_date, so it can take
    a trade the full-history run couldn't (the full-history run may still be holding an
    earlier position spanning the boundary) -- produced a real, correctly-explained
    37-vs-36 trade discrepancy in the Stage 1 audit (scripts/audit_date_window_soxl.py).
    This is not a bug -- it's the same property get_trades_and_bars_since's reused
    slicing pattern has by design -- but it IS an uncontrolled bias for a rolling-window
    drift study: each window's first trade may be a flat-start artifact, and with
    quarterly-stepped overlapping windows every window gets one.

    Mirror-image bias at the OTHER boundary (found by review, 2026-08-16, undocumented
    in the first pass of this note): a position still open when the sim runs out of bars
    at end_date never closes within the window and drops out of the `closed` trade set
    entirely -- the full-history run, continuing past end_date, would report it. Same
    class of uncontrolled bias (a window can lose its real last trade, not just gain a
    spurious first one), and it skews `compounded`/win-rate the same way.

    Left unhandled deliberately (Phase 1 scope is data-loading only, see
    docs/deep_backlog.md's 2026-08-16 entry) -- a future rolling-window campaign built on
    this needs to account for both ends (e.g. discard/flag each window's first AND last
    trade, or seed/carry position state across window boundaries) rather than treat
    window-to-window trade-count deltas as pure signal."""
    if start_date is not None and end_date is not None and pd.Timestamp(start_date) > pd.Timestamp(end_date):
        raise ValueError(f"_window_prep: start_date {start_date} is after end_date {end_date}")
    ts = prep['timestamps']
    start_pos = ts.searchsorted(pd.Timestamp(start_date)) if start_date is not None else 0
    end_pos = (ts.searchsorted(pd.Timestamp(end_date) + pd.Timedelta(days=1))
               if end_date is not None else len(ts))
    if start_pos >= end_pos:
        span = f"{ts[0]} to {ts[-1]}" if len(ts) else "empty"
        raise ValueError(
            f"_window_prep: window [{start_date}, {end_date}] produced zero bars "
            f"(cached data spans {span}) -- check the window is inside the cached "
            f"data's range and start_date <= end_date")
    windowed = dict(prep)
    for k in ("prices", "highs", "lows", "opens", "hours", "daily_idx", "timestamps"):
        windowed[k] = prep[k][start_pos:end_pos]
    return windowed


def _warmup_worker():
    """ProcessPoolExecutor initializer: pays each numba kernel's one-time JIT
    compile cost at worker startup instead of on a random real grid node."""
    prices    = np.array([100.0, 99.0, 101.0], dtype=np.float64)
    hilo      = prices
    hours     = np.array([9, 14, 9], dtype=np.int64)
    daily_idx = np.array([0, 0, 0], dtype=np.int64)
    sma_arr   = np.array([100.0], dtype=np.float64)
    std_arr   = np.array([1.0], dtype=np.float64)
    trend_arr = np.array([0.0], dtype=np.float64)

    _simulate(prices, hours, daily_idx, sma_arr, std_arr, trend_arr, False,
              0.05, 0.05, 1, 9, 14, 2.0)
    _simulate_limit(prices, hilo, hours, daily_idx, sma_arr, std_arr, trend_arr, False,
                     0.05, 0.05, 1, 9, 14, 2.0)
    _simulate_trail(prices, hilo, hilo, prices, hours, daily_idx, sma_arr, std_arr, trend_arr, False,
                     0.05, 0.05, 1, 0.03, 9, 14, 2.0, False)
    _simulate_trail_buy(prices, hilo, hilo, prices, hours, daily_idx, sma_arr, std_arr, trend_arr, False,
                         0.05, 0.05, 1, 0.03, 9, 14, 2.0)
    _simulate_trail_both(prices, hilo, hilo, hours, daily_idx, sma_arr, std_arr, trend_arr, False,
                          0.05, 0.05, 1, 0.03, 0.03, 9, 14, 2.0, prices, False)
    _simulate_limit_trail(prices, hilo, hilo, hours, daily_idx, sma_arr, std_arr, trend_arr, False,
                           0.05, 0.05, 1, 0.03, 9, 14, 2.0)
    _simulate_close_limitexit(prices, hilo, hilo, hours, daily_idx, sma_arr, std_arr, trend_arr, False,
                               0.05, 0.05, 1, 9, 14, 2.0)


def _config_trail_pct():
    """Legacy single-value fallback for TrailingBothZScoreBreakout's trail_pct (exit
    trailing %) when config.json's hyperparameters.trail_pcts isn't set — matches the
    old v2.10/v2.13-17 behavior of one fixed value per whole backfill run."""
    try:
        with open("config.json") as f:
            return float(json.load(f).get("execution", {}).get("trail_pct", 3)) / 100.0
    except Exception:
        return 0.03


def _sl_axis_real_column(sl_axis_col):
    """Real backtest_cache column for a strategy's conceptual sl_axis (see
    strategies.resolve_axis_columns) -- trail_pct's real column is trail_sell_pct,
    everything else (stop_loss, trail_buy_pct) already matches its conceptual name."""
    return 'trail_sell_pct' if sl_axis_col == 'trail_pct' else sl_axis_col


def _trail_pcts_for_strategy(strategy_name, hp):
    """TrailingBothZScoreBreakout is the only strategy with a real, swept 4th axis
    (trail_pct). Everything else doesn't use this axis (single dummy value)."""
    _, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)
    if fourth_axis_col != 'trail_pct':
        return [0.0]
    return [float(v) for v in hp.get('trail_pcts', [_config_trail_pct() * 100])]


def _summarize_trades(closed, spy_bh):
    """(alpha, n_trades, win_rate, compounded, win_twin_rate) for a closed-trade list."""
    df_tr = pd.DataFrame(closed)
    win_rate      = float((len(df_tr[df_tr['Result'] == 'WIN']) / len(df_tr)) * 100)
    win_twin_rate = float((len(df_tr[df_tr['Result'].isin(['WIN', 'TWIN'])]) / len(df_tr)) * 100)
    compounded = float(((df_tr['Return'] + 1).prod() - 1) * 100)
    alpha_calc = float(compounded - spy_bh)
    return alpha_calc, len(df_tr), win_rate, compounded, win_twin_rate


# ═══════════════ v6 ground-truth kernel wiring (docs/plans/ground_truth_kernel_rebuild.md
# Step 3) — mirrors the hourly-kernel machinery above (_load_node_inputs/
# run_single_backtest_node_isolated/dispatch_parallel_grid) but calls
# backtester.run_backtest_ground_truth (real per-minute resolution) instead of
# run_backtest_dispatch. Deliberately a SEPARATE, self-contained path rather than
# threading a `kernel` flag through the existing heavily-guarded functions (the
# start_date/end_date/min_hold_hours version-suffix assertions in particular) — smaller,
# more reviewable diff, and this scope doesn't need windowing/min_hold_hours yet. Reuses
# identify_island_candidates/identify_full_mesh_candidates/pick_island_centers UNCHANGED
# (they're kernel-agnostic, working off backtest_cache columns generically) and
# ROBUST_ALPHA_SQL UNCHANGED (v6 rows leave alpha_vs_spy_pessimistic/_certain NULL, so
# MIN(alpha_vs_spy, COALESCE(NULL,alpha_vs_spy), COALESCE(NULL,alpha_vs_spy)) collapses
# to plain alpha_vs_spy automatically — exactly the plan's "job (2) disappears" cliff-
# safety redefinition, no extra code needed). Scope: TrailingBothZScoreBreakout /
# TrailingExitZScoreBreakout only, matching backtester.run_backtest_ground_truth's own
# scope (see its docstring's Step-3 interface-convention warning before extending this).

MINUTE_DIR = CACHE_DIR / "minute_data"
_MINUTE_DF_CACHE = {}
_MINUTE_DF_CACHE_MAX = 3  # minute CSVs are large (~500k-600k rows); cap tighter than
                          # _NODE_INPUT_CACHE_MAX since a worker process holds both.
_NODE_INPUT_CACHE_GT = {}


def _load_minute_df(ticker, data_source="yahoo"):
    """data_source='yahoo' (default, unchanged): reads the raw, UNADJUSTED minute CSV,
    same as before this param existed. data_source='massive': reads db_cache.
    get_massive_minute_ohlcv(ticker) instead -- dividend-adjusted minute bars,
    consistent with the data_source='massive' hourly leg (see db_cache.py's
    massive_minute_derived table docstring; fixed 2026-08-22, previously the minute
    leg stayed on the raw unadjusted CSV even under --data-source massive, a real
    ~1.6%+ inconsistency vs. the adjusted hourly bars that grows further back in time).
    Folded into the memo key so a mixed-source run never reuses the other source's
    cached minute frame."""
    key = (ticker, data_source)
    hit = _MINUTE_DF_CACHE.get(key)
    if hit is not None:
        return hit
    if data_source == "massive":
        import db_cache
        df = db_cache.get_massive_minute_ohlcv(ticker)
    else:
        df = pd.read_csv(MINUTE_DIR / f"{ticker}_1m.csv")
        ts = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("US/Eastern").dt.tz_localize(None)
        df = df.set_index(ts).sort_index()
        t = df.index.time
        keep = (t >= pd.Timestamp("09:30").time()) & (t < pd.Timestamp("16:00").time())
        df = df.loc[keep, ["Open", "High", "Low", "Close"]]
    if len(_MINUTE_DF_CACHE) >= _MINUTE_DF_CACHE_MAX:
        _MINUTE_DF_CACHE.clear()
    _MINUTE_DF_CACHE[key] = df
    return df


def _load_node_inputs_ground_truth(ticker, strategy_class, strategy_name, w, z_thresh,
                                    start_date=None, end_date=None, data_source="yahoo"):
    """Same per-worker-process memo pattern as _load_node_inputs. Indicators depend only
    on (ticker, strategy, window) — z_thresh is a kernel arg, not baked into df_daily_
    processed, so it's not part of the cache key (matches _load_node_inputs's own key,
    which also omits z_thresh for the same reason). start_date/end_date DO fold into the
    key (mirroring _load_node_inputs) — required below.

    data_source='yahoo' (default, unchanged): reads cache/research/{ticker}_1h.csv, same
    as before this param existed. data_source='massive': reads db_cache.
    get_massive_hourly_ohlcv(ticker) instead — dividend/split-adjusted hourly bars
    derived from Massive.com minute data, back to ~2021-08-23 for most tickers vs.
    yahoo's ~2023-07-24 floor (see db_cache.py's massive_hourly_derived table
    docstring). Folded into the memo key so a mixed-source campaign never reuses the
    other source's cached prep/mprep.

    Also caches the derived prep/mprep arrays (prep_inputs()/prep_minute_inputs() output)
    for the ACTUAL (possibly windowed) hourly bars fed to the kernel, same as
    _load_node_inputs caches prep — otherwise run_backtest_ground_truth recomputes both
    from scratch on every single grid cell, dominated by prep_minute_inputs' pure-Python/
    pandas groupby (measured ~1.0-1.3s/call on SOXL even windowed, ~3.65s/call full-
    history, vs ~9.6ms/call for the hourly-only prep_inputs) even though neither depends
    on the tp/sl/hold axes being swept. Found 2026-08-22 diagnosing the v6 GT sweep's ~4
    nodes/s rate.

    First cut of this fix (2026-08-22, paired-review contextual pass) only cached the
    full-history case and fell back to recomputing per-cell whenever start_date/end_date
    was set — missed that BOTH real GT campaign entry points (run_ground_truth_phase1.py,
    run_ground_truth_neighborhood.py) always call windowed, so the fast path
    never fired in production. Slicing df_hourly to the window BEFORE computing prep/
    mprep (below) — rather than nulling them out — restores the cache hit for the actual
    workload: every cell in a windowed campaign shares the same (ticker, strategy, w,
    start_date, end_date) key and reuses one sliced prep/mprep pair."""
    key = (ticker, strategy_name, int(w), start_date, end_date, data_source)
    hit = _NODE_INPUT_CACHE_GT.get(key)
    if hit is not None:
        return hit

    if data_source == "massive":
        import db_cache
        df_hourly_raw = db_cache.get_massive_hourly_ohlcv(ticker)
    else:
        cache_path = CACHE_DIR / f"{ticker}_1h.csv"
        df_hourly_raw = pd.read_csv(cache_path, index_col=0, parse_dates=True)
        df_hourly_raw.index = pd.to_datetime(df_hourly_raw.index).tz_localize(None)
        df_hourly_raw = df_hourly_raw.sort_index()
    if df_hourly_raw.empty:
        entry = None
    else:
        close_col = 'Adj Close' if 'Adj Close' in df_hourly_raw.columns else 'Close'
        df_daily = df_hourly_raw.resample('D').last().dropna(subset=[close_col])
        strat_instance = strategy_class(window=w, z_score_threshold=z_thresh)
        df_daily_processed = strat_instance.generate_daily_indicators(df_daily)
        minute_df = _load_minute_df(ticker, data_source=data_source)

        df_hourly_windowed = df_hourly_raw
        if start_date is not None or end_date is not None:
            # Same pd.Timestamp-slicing convention as the (now-removed) inline windowing
            # this replaced — tolerates None on either side, unlike
            # window_version_suffix's stricter contract.
            lo = pd.Timestamp(start_date) if start_date is not None else None
            hi = (pd.Timestamp(end_date) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)) if end_date is not None else None
            df_hourly_windowed = df_hourly_raw.loc[lo:hi]

        if df_hourly_windowed.empty:
            entry = (df_hourly_raw, df_daily_processed, minute_df, df_hourly_windowed, None, None)
        else:
            prep = prep_inputs(df_hourly_windowed, df_daily_processed)
            mprep = prep_minute_inputs(minute_df, df_hourly_windowed)
            entry = (df_hourly_raw, df_daily_processed, minute_df, df_hourly_windowed, prep, mprep)

    if len(_NODE_INPUT_CACHE_GT) >= _NODE_INPUT_CACHE_MAX:
        _NODE_INPUT_CACHE_GT.clear()
    _NODE_INPUT_CACHE_GT[key] = entry
    return entry


def _summarize_trades_ground_truth(trades, spy_bh, years):
    """Ground-truth trades carry 'Return' directly, no Result/WIN-LOSS/TWIN-TLOSS code
    (backtester.run_backtest_ground_truth's exit_reason is SL/TRAIL/TIME, a mechanism
    label, not a profitability label) — win_twin_rate exists in _summarize_trades
    specifically to fold TWIN (profitable TIME exit) back into "did this trade make
    money," which Return>0 already answers directly here with no split to correct for.

    CAUTION when comparing a v6 row's win_rate against an hourly (v4/v5) row's win_rate
    (flagged by the paired-review independent-cold pass, 2026-08-21): the hourly kernel's
    win_rate (_summarize_trades, Result=='WIN') EXCLUDES profitable TIME exits (TWIN) --
    this function's win_rate does not make that distinction (Return>0 regardless of exit
    mechanism), so it's definitionally the hourly path's win_twin_rate, not its win_rate.
    A side-by-side v5-vs-v6 comparison should read v6's win_rate against v5's
    win_twin_rate column, not v5's win_rate."""
    df_tr = pd.DataFrame(trades)
    win_rate = float((df_tr['Return'] > 0).mean() * 100)
    compounded = float(((df_tr['Return'] + 1).prod() - 1) * 100)
    alpha_calc = float(compounded - spy_bh)
    node_cagr = _cagr_from_total_return(compounded, years)
    return alpha_calc, len(df_tr), win_rate, compounded, win_rate, node_cagr


def _cagr_from_total_return(total_return_pct, years):
    """Real, properly-annualized CAGR -- same formula as scripts/annualized_alpha_
    report.py::cagr(), duplicated here (not imported) since that's a script, not a
    shared module, and this is a small, pure, one-line formula. Returns None if
    years<=0 (can't annualize a zero-span window), so callers can distinguish
    "not computed" from a real 0% CAGR. Also returns None on OverflowError -- a
    very short window (years<<1) combined with a large total return raises
    OverflowError on the fractional power (confirmed reproducible, e.g.
    total_return_pct=700, years=1/365.25) -- the node's other stats (alpha,
    trades, compounded return) are still real and shouldn't be discarded just
    because CAGR can't be represented for this specific window.

    Also returns None for total_return_pct <= -100 (2026-08-22, paired-review finding
    on the new GT add-on overlay: unlike a core-only compounded return -- always > -100%
    since every individual core trade's Return is bounded below by -fixed_sl%, so the
    running product of (1+r) factors stays positive -- an add-on-blended trade's Return
    has no such floor (the add-on leg has no independent stop-loss; a severe gap-down
    past half the arm-to-entry price move can push a single trade's blended Return below
    -100%), which can drive the overall compounded product to <= 0. `(1+x)**(1/years)`
    for x <= -1 and a non-integer exponent silently returns a COMPLEX number in Python,
    not an exception -- undetected by the existing `except OverflowError` and liable to
    crash the first place that later `.2f`-formats it. Guarded here, not at the caller,
    so every caller (core and add-on alike) gets the same protection for free."""
    if years is None or years <= 0:
        return None
    if total_return_pct <= -100.0:
        return None
    try:
        return ((1.0 + total_return_pct / 100.0) ** (1.0 / years) - 1.0) * 100.0
    except OverflowError:
        return None


def _campaign_years_for_window(ticker, start_date, end_date, data_source="yahoo"):
    """Real simulated-window span in years, for a whole (ticker, start_date, end_date)
    campaign -- same slicing convention as _load_node_inputs_ground_truth's
    df_hourly_windowed (real bar range, not the raw requested dates), duplicated here
    rather than reusing that function since df_hourly_windowed's min/max doesn't depend
    on w/z_thresh/strategy (only on the date-range slice), so this can be computed once
    per campaign instead of once per node. Used only to backfill `cagr` for cache-hit
    rows written before this column existed (or before a since-fixed bug prevented it
    from being computed) -- see dispatch_parallel_grid_ground_truth's cached_map read.

    data_source must match the campaign's own data_source (paired review, 2026-08-22) --
    reading the yahoo CSV's span to backfill cagr for massive-sourced rows would compute
    the wrong window span (yahoo's ~2023-07-24 floor vs massive's ~2021-08-23)."""
    if data_source == "massive":
        import db_cache
        try:
            df_hourly_raw = db_cache.get_massive_hourly_ohlcv(ticker)
        except ValueError:
            return None
    else:
        cache_path = CACHE_DIR / f"{ticker}_1h.csv"
        if not cache_path.exists():
            return None
        df_hourly_raw = pd.read_csv(cache_path, index_col=0, parse_dates=True)
        df_hourly_raw.index = pd.to_datetime(df_hourly_raw.index).tz_localize(None)
        df_hourly_raw = df_hourly_raw.sort_index()
    df_hourly_windowed = df_hourly_raw
    if start_date is not None or end_date is not None:
        lo = pd.Timestamp(start_date) if start_date is not None else None
        hi = (pd.Timestamp(end_date) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)) if end_date is not None else None
        df_hourly_windowed = df_hourly_raw.loc[lo:hi]
    if df_hourly_windowed.empty:
        return None
    span_days = (df_hourly_windowed.index.max() - df_hourly_windowed.index.min()).days
    return span_days / 365.25 if span_days > 0 else None


def run_single_backtest_node_ground_truth_isolated(args):
    (ticker, strategy_name, config_version, tp, sl, hold_hours, w, spy_bh, z_thresh, fixed_sl,
     trail_pct_pct, entry_timing, same_bar_reentry, start_date, end_date, data_source) = args

    strategy_class = getattr(strategies, strategy_name, None)
    if strategy_name not in ('TrailingBothZScoreBreakout', 'TrailingExitZScoreBreakout') or not strategy_class:
        return {"coords": (tp, sl, hold_hours), "payload": (0.0, 0, 0.0), "window": w, "z_thresh": z_thresh, "status": "UNKNOWN_STRAT"}

    try:
        inputs = _load_node_inputs_ground_truth(ticker, strategy_class, strategy_name, w, z_thresh,
                                                 start_date, end_date, data_source=data_source)
    except Exception as e:
        return {"coords": (tp, sl, hold_hours), "payload": (0.0, 0, 0.0), "window": w, "z_thresh": z_thresh, "status": "ERROR", "error": repr(e)}

    if inputs is None:
        return {"coords": (tp, sl, hold_hours), "payload": (0.0, 0, 0.0), "window": w, "z_thresh": z_thresh, "status": "EMPTY"}
    df_hourly_raw, df_daily_processed, minute_df, df_hourly_windowed, prep, mprep = inputs
    # df_daily_processed (indicators) ALWAYS stays full-history, even when windowed --
    # matches _load_node_inputs/_window_prep's convention (never truncate pre-indicator).
    # Only the hourly bars actually fed to the kernel (df_hourly_windowed) get sliced,
    # mirroring the parity test's own fix (tests/test_ground_truth_kernel_parity.py) for
    # the identical window-boundary state-leak risk. prep/mprep above are already sliced
    # to match df_hourly_windowed (computed inside the loader, cached per (ticker,
    # strategy, w, start_date, end_date)) -- both real GT campaign entry points
    # (run_ground_truth_phase1.py, run_ground_truth_neighborhood.py) call windowed,
    # so this is the actual hot path, not a fallback.
    if df_hourly_windowed.empty:
        return {"coords": (tp, sl, hold_hours), "payload": (0.0, 0, 0.0), "window": w, "z_thresh": z_thresh, "status": "EMPTY"}

    is_both = strategy_name == 'TrailingBothZScoreBreakout'
    # Axis meaning mirrors strategies.resolve_axis_columns for these two strategies:
    # TrailingBoth sweeps trail_buy_pct(=sl)/arm(=tp)/trail_sell_pct(=tpct);
    # TrailingExit sweeps trail_sell_pct(=sl)/arm-or-tp(=tp), no trail_buy_pct.
    if is_both:
        trail_buy_pct_arg, trail_sell_pct_arg, arm_pct_arg = float(sl), float(trail_pct_pct), float(tp)
    else:
        trail_buy_pct_arg, trail_sell_pct_arg, arm_pct_arg = 0.0, float(sl), float(tp)

    try:
        trades = run_backtest_ground_truth(
            df_hourly_windowed, df_daily_processed, ticker, minute_df,
            fixed_sl=fixed_sl, arm_pct=arm_pct_arg, trail_buy_pct=trail_buy_pct_arg,
            trail_sell_pct=trail_sell_pct_arg, max_hours_to_hold=hold_hours,
            z_score_threshold=z_thresh, is_both=is_both,
            open_check_entry_timing=(entry_timing == 'open_check'),
            same_bar_reentry=same_bar_reentry,
            prep=prep, mprep=mprep, need_times=False,
        )
    except Exception as e:
        return {"coords": (tp, sl, hold_hours), "payload": (0.0, 0, 0.0), "window": w, "z_thresh": z_thresh, "status": "SIM_ERROR", "error": repr(e)}

    if not trades:
        return {"coords": (tp, sl, hold_hours), "payload": (0.0, 0, 0.0), "window": w, "z_thresh": z_thresh, "status": "NO_TRADES"}

    # CAGR needs the real window span in years -- df_hourly_windowed's own actual bar
    # range (not the requested start_date/end_date, which can exceed the real cached
    # data's coverage) is the honest source of truth for what was actually simulated.
    span_days = (df_hourly_windowed.index.max() - df_hourly_windowed.index.min()).days
    years = span_days / 365.25 if span_days > 0 else None
    alpha_calc, n_trades, win_rate, compounded, win_twin_rate, node_cagr = _summarize_trades_ground_truth(
        trades, spy_bh, years)
    return {
        "coords":  (tp, sl, hold_hours),
        "payload": (alpha_calc, n_trades, win_rate, compounded, win_twin_rate, node_cagr),
        "window":  w, "z_thresh": z_thresh, "status": "SUCCESS"
    }


def dispatch_parallel_grid_ground_truth(shared_pool, tasks, ticker, strategy_name, config_version,
                                         phase_label, spy_bh, asset_bh, run_timestamp, fixed_sl=0,
                                         entry_timing='close', same_bar_reentry=True, generation=None,
                                         run_id=None, start_date=None, end_date=None, data_source="yahoo"):
    """v6 counterpart to dispatch_parallel_grid — same cache-lookup/dispatch/write shape,
    calling run_single_backtest_node_ground_truth_isolated instead. No min_hold_hours
    support (not needed for the v6 Tranche-1 scope). Writes alpha_vs_spy_pessimistic/
    _certain as NULL always (no resolution-ambiguity hedging with real minute data — see

    data_source='yahoo' (default, unchanged): every existing caller (including
    run_phase2_island_ground_truth/run_phase25_cliff_box_ground_truth) keeps reading
    cache/research/{ticker}_1h.csv exactly as before this param existed.
    data_source='massive' is opt-in, currently wired only from
    scripts/run_ground_truth_phase1.py's --data-source flag — see
    module docstring) and kernel_version='ground_truth_v6'.

    start_date/end_date reuse dispatch_parallel_grid's own window_version_suffix guard —
    a windowed call whose config_version doesn't carry the matching suffix is REJECTED,
    same rationale as the hourly path: a windowed result must never share a cache key
    with a full-history one (found live in this session: SOXL_1h.csv's actual full-
    history window has grown since older v4/v5 campaigns ran, so 'full history' is not a
    stable, comparable window across time — an explicit window is what makes a ground-
    truth-vs-hourly-kernel comparison apples-to-apples).

    CAUTION (paired-review independent-cold pass, 2026-08-21): same_bar_reentry is a real
    behavioral kernel arg with no backtest_cache column and no version-suffix guard of its
    own -- flipping it under an unchanged config_version would silently serve rows computed
    at the OTHER setting back out of the cache (same failure shape window_version_suffix/
    min_hold_version_suffix exist to prevent for their own axes). Do not vary
    same_bar_reentry within a single version string; every caller today passes True."""
    if start_date is not None or end_date is not None:
        required_suffix = window_version_suffix(start_date, end_date)
        if not config_version.endswith(required_suffix):
            raise ValueError(
                f"dispatch_parallel_grid_ground_truth: windowed call (start_date={start_date!r}, "
                f"end_date={end_date!r}) but config_version={config_version!r} does not end with "
                f"the required suffix {required_suffix!r}. Build config_version via "
                f"window_version_suffix(start_date, end_date) before calling this."
            )

    # data_source is NOT its own backtest_cache column (paired review, 2026-08-22, same
    # failure shape as the same_bar_reentry CAUTION above) -- a massive-sourced run over a
    # config_version already swept on yahoo (or vice versa) would silently serve/overwrite
    # the other source's rows as cache hits. Require the marker in config_version itself
    # (mirroring window_version_suffix's own enforced-suffix convention) rather than trust
    # every caller to remember -- cheaper than a real schema column for how rarely
    # data_source varies today.
    if data_source == "massive" and "-massive" not in config_version:
        raise ValueError(
            f"dispatch_parallel_grid_ground_truth: data_source='massive' but "
            f"config_version={config_version!r} carries no '-massive' marker -- append "
            f"'-massive' to config_version so massive-sourced rows can never collide with "
            f"yahoo-sourced rows under the same version string."
        )

    conn   = sqlite3.connect(DB_PATH, timeout=60.0)
    cursor = conn.cursor()
    matrix_results  = []
    unvisited_tasks = []

    uses_fixed_sl = strategies.uses_fixed_sl(strategy_name)
    stored_fsl = float(fixed_sl) if uses_fixed_sl else 0.0
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)

    cached_map = {}
    _missing_cagr_rows = []  # cache-hit rows with strategy_return but no cagr -- backfilled below
    cursor.execute("""
        SELECT window, max_hold_hours, axis_tp, stop_loss, z_score_threshold, fixed_sl,
               trail_buy_pct, trail_sell_pct, trades, win_rate, strategy_return, alpha_vs_spy, win_twin_rate,
               cagr
        FROM backtest_cache
        WHERE strategy=? AND version=? AND ticker=? AND entry_timing=? AND kernel_version='ground_truth_v6'
    """, (strategy_name, config_version, ticker, entry_timing))
    for r in cursor.fetchall():
        if r[4] is None:
            continue
        row_fsl = float(r[5]) if (uses_fixed_sl and r[5] is not None) else 0.0
        if sl_axis_col == 'trail_buy_pct':
            row_sl_raw = float(r[6])
        elif sl_axis_col == 'trail_pct':
            row_sl_raw = float(r[7])
        else:
            row_sl_raw = float(r[3])
        row_tpct_raw = float(r[7]) if fourth_axis_col == 'trail_pct' else 0.0
        cached_map[(int(r[2]), row_sl_raw, int(r[1]), int(r[0]), float(r[4]), row_fsl, row_tpct_raw)] = \
            (r[8], r[9], r[10], r[11], r[12] if r[12] is not None else 0.0)
        if r[13] is None and r[10] is not None:
            _missing_cagr_rows.append(r)

    if _missing_cagr_rows:
        # cagr is cheaply derivable from the already-cached strategy_return + the
        # campaign's real window span -- no re-simulation needed. Self-heals rows
        # written before the cagr column existed (or before this backfill was added)
        # every time this ticker/version/strategy combo is dispatched again.
        campaign_years = _campaign_years_for_window(ticker, start_date, end_date, data_source=data_source)
        if campaign_years is not None:
            backfill_params = []
            for r in _missing_cagr_rows:
                node_cagr = _cagr_from_total_return(r[10], campaign_years)
                if node_cagr is not None:
                    backfill_params.append((node_cagr, strategy_name, config_version, ticker,
                                             r[0], r[1], r[2], r[3], r[4], r[6], r[7], entry_timing))
            if backfill_params:
                cursor.executemany(
                    """UPDATE backtest_cache SET cagr=?
                       WHERE strategy=? AND version=? AND ticker=? AND window=? AND max_hold_hours=?
                         AND axis_tp=? AND stop_loss=? AND z_score_threshold=?
                         AND trail_buy_pct=? AND trail_sell_pct=? AND entry_timing=?""",
                    backfill_params
                )
                conn.commit()
                logger.info(f"[{ticker}] {phase_label}: backfilled cagr for "
                            f"{len(backfill_params)} pre-existing cache rows")

    for t in tasks:
        tp, sl, hold_hours, w, z_thresh, tpct = t
        cached_row = cached_map.get((int(tp), float(sl), int(hold_hours), int(w), float(z_thresh), stored_fsl, float(tpct)))
        if cached_row:
            matrix_results.append({
                "Strategy": strategy_name, "Version": config_version, "Ticker": ticker, "Window": w,
                "Take Profit %": int(tp), "Stop Loss %": int(sl), "Max Hold Hours": hold_hours,
                "Z Threshold": z_thresh,
                "Trades": cached_row[0], "Win Rate %": cached_row[1], "Return %": cached_row[2],
                "Alpha vs SPY %": cached_row[3], "Win+TWin Rate %": cached_row[4],
                "Asset B&H %": asset_bh, "SPY B&H %": spy_bh
            })
        else:
            unvisited_tasks.append(t)

    logger.info(f"[{ticker}] {phase_label} (ground_truth_v6): {len(matrix_results):,} cached, "
                f"{len(unvisited_tasks):,} to compute (of {len(tasks):,} total)")

    if not unvisited_tasks:
        conn.close()
        return pd.DataFrame(matrix_results)

    futures_map = {
        shared_pool.submit(run_single_backtest_node_ground_truth_isolated,
                           (ticker, strategy_name, config_version, int(tp), int(sl), hold, w, spy_bh, z,
                            fixed_sl, tpct, entry_timing, same_bar_reentry, start_date, end_date,
                            data_source)): task
        for task in unvisited_tasks
        for tp, sl, hold, w, z, tpct in [task]
    }

    progress_bar = tqdm(
        as_completed(futures_map), total=len(futures_map),
        desc=f"[{ticker}] {phase_label} (gt)", unit="node",
        mininterval=15.0, maxinterval=30.0
    )

    fail_counts = {}
    buffer = []
    batch_size = 5000

    for future in progress_bar:
        tp, sl, hold_hours, w, z_thresh, tpct = futures_map[future]
        try:
            res = future.result()
            status = res.get("status")
            if status not in ("SUCCESS", "NO_TRADES"):
                fail_counts[status] = fail_counts.get(status, 0) + 1
                if sum(fail_counts.values()) == 1:
                    logger.warning(f"[{ticker}] {phase_label} first failed node TP={tp} SL={sl}: "
                                   f"{status} {res.get('error', '')}")
                continue

            if status == "SUCCESS":
                alpha, num_trades, wr, comp_ret, wtw, node_cagr = res["payload"]
            else:
                alpha, num_trades, wr, comp_ret, wtw, node_cagr = 0.0, 0, 0.0, 0.0, 0.0, None

            progress_bar.set_postfix({"Alpha": f"{alpha:+.1f}%", "Trades": num_trades})

            if status == "SUCCESS":
                matrix_results.append({
                    "Strategy": strategy_name, "Version": config_version, "Ticker": ticker, "Window": w,
                    "Take Profit %": int(tp), "Stop Loss %": int(sl), "Max Hold Hours": hold_hours,
                    "Z Threshold": z_thresh,
                    "Trades": num_trades, "Win Rate %": wr, "Return %": comp_ret,
                    "Alpha vs SPY %": alpha, "Win+TWin Rate %": wtw,
                    "Asset B&H %": asset_bh, "SPY B&H %": spy_bh
                })

            if sl_axis_col == 'trail_buy_pct':
                row_stop_loss, row_trail_buy_pct = int(round(stored_fsl)), float(sl)
                row_trail_pct = float(tpct) if fourth_axis_col == 'trail_pct' else 0.0
            elif sl_axis_col == 'trail_pct':
                row_stop_loss, row_trail_buy_pct, row_trail_pct = int(round(stored_fsl)), 0.0, float(sl)
            else:
                row_stop_loss, row_trail_buy_pct, row_trail_pct = int(sl), 0.0, 0.0

            if strategy_name == 'TrailingBothZScoreBreakout':
                row_take_profit, row_arm_sell_pct = None, float(tp)
            else:
                row_take_profit, row_arm_sell_pct = int(tp), None

            buffer.append((strategy_name, config_version, ticker, w, hold_hours, row_take_profit, row_stop_loss,
                           num_trades, wr, comp_ret, alpha, asset_bh, spy_bh, run_timestamp, z_thresh,
                           stored_fsl, row_trail_buy_pct, row_trail_pct, wtw, row_arm_sell_pct, float(tp),
                           entry_timing, None, None, None, None, phase_label, generation, run_id,
                           'ground_truth_v6', node_cagr))

            if len(buffer) >= batch_size:
                cursor.executemany(
                    """INSERT OR REPLACE INTO backtest_cache
                       (strategy, version, ticker, window, max_hold_hours, take_profit, stop_loss,
                        trades, win_rate, strategy_return, alpha_vs_spy, asset_bh, spy_bh,
                        run_timestamp, z_score_threshold, fixed_sl, trail_buy_pct, trail_sell_pct,
                        win_twin_rate, arm_sell_pct, axis_tp, entry_timing,
                        strategy_return_pessimistic, alpha_vs_spy_pessimistic,
                        strategy_return_certain, alpha_vs_spy_certain, phase, generation, sweep_run_id,
                        kernel_version, cagr)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    buffer
                )
                buffer = []
                conn.commit()

        except Exception as e:
            logger.error(f"Worker crashed TP={tp} SL={sl}: {e}")

    if buffer:
        cursor.executemany(
            """INSERT OR REPLACE INTO backtest_cache
               (strategy, version, ticker, window, max_hold_hours, take_profit, stop_loss,
                trades, win_rate, strategy_return, alpha_vs_spy, asset_bh, spy_bh,
                run_timestamp, z_score_threshold, fixed_sl, trail_buy_pct, trail_sell_pct,
                win_twin_rate, arm_sell_pct, axis_tp, entry_timing,
                strategy_return_pessimistic, alpha_vs_spy_pessimistic,
                strategy_return_certain, alpha_vs_spy_certain, phase, generation, sweep_run_id,
                kernel_version, cagr)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            buffer
        )
        conn.commit()

    conn.close()
    return pd.DataFrame(matrix_results)


# ── Phase1 schema-v2 experiment (parallel, backtest_phase1) ──────────────────
# Added 2026-08-25, docs/plans/backtest_schema_v2_phase_tables.md. These 3 functions
# are near-verbatim copies of dispatch_parallel_grid_ground_truth/_phase1_coarse_gt_status/
# _phase2_island_gt_tasks above, retargeted at the new backtest_phase1 scratch table
# instead of backtest_cache. NOT wired into any real call site (scripts/run_ground_truth_
# phase1.py, run_phase2_island_ground_truth, run_phase25_cliff_box_ground_truth all still
# call the ORIGINAL backtest_cache-based functions, completely unchanged) -- this exists
# purely so the new table/read/write shape can be run side-by-side against the real
# pipeline's output on a real ticker and diffed, per the plan doc's test-plan item #3/#6,
# before anything is ever cut over. Kernel-adjacent (touches run_optimization_sweep.py) --
# CLAUDE.md's Review-Gate Persistence Rule applies once this is wired into any real script
# or call site, same as any other change here. Deliberately duplicated rather than
# parameterizing the existing functions with a table-name arg -- the point is to be able to
# run both and diff, not to change what's live; once validated, per the plan doc's own "one
# definition of winner, not one per script" requirement, this should collapse back into a
# single implementation rather than persist as two.
def dispatch_parallel_grid_ground_truth_phase1(shared_pool, tasks, ticker, strategy_name, config_version,
                                                phase_label, spy_bh, asset_bh, run_timestamp, fixed_sl=0,
                                                entry_timing='close', same_bar_reentry=True, generation=None,
                                                run_id=None, start_date=None, end_date=None, data_source="yahoo"):
    """Parallel twin of dispatch_parallel_grid_ground_truth, writing/reading
    backtest_phase1 instead of backtest_cache. Same guards, same dispatch/write shape.
    Additionally calls strategies.validate_row_axis_mapping() on every row before
    buffering it -- the new schema-enforcement check requested alongside this table,
    not (yet) added to the original backtest_cache-writing function since that's live
    production code this experiment is deliberately not touching."""
    if start_date is not None or end_date is not None:
        required_suffix = window_version_suffix(start_date, end_date)
        if not config_version.endswith(required_suffix):
            raise ValueError(
                f"dispatch_parallel_grid_ground_truth_phase1: windowed call (start_date={start_date!r}, "
                f"end_date={end_date!r}) but config_version={config_version!r} does not end with "
                f"the required suffix {required_suffix!r}. Build config_version via "
                f"window_version_suffix(start_date, end_date) before calling this."
            )

    if data_source == "massive" and "-massive" not in config_version:
        raise ValueError(
            f"dispatch_parallel_grid_ground_truth_phase1: data_source='massive' but "
            f"config_version={config_version!r} carries no '-massive' marker -- append "
            f"'-massive' to config_version so massive-sourced rows can never collide with "
            f"yahoo-sourced rows under the same version string."
        )

    conn   = sqlite3.connect(DB_PATH, timeout=60.0)
    cursor = conn.cursor()
    matrix_results  = []
    unvisited_tasks = []

    uses_fixed_sl = strategies.uses_fixed_sl(strategy_name)
    stored_fsl = float(fixed_sl) if uses_fixed_sl else 0.0
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)

    cached_map = {}
    _missing_cagr_rows = []
    cursor.execute("""
        SELECT window, max_hold_hours, axis_tp, stop_loss, z_score_threshold, fixed_sl,
               trail_buy_pct, trail_sell_pct, trades, win_rate, strategy_return, alpha_vs_spy, win_twin_rate,
               cagr
        FROM backtest_phase1
        WHERE strategy=? AND version=? AND ticker=? AND entry_timing=?
    """, (strategy_name, config_version, ticker, entry_timing))
    for r in cursor.fetchall():
        if r[4] is None:
            continue
        row_fsl = float(r[5]) if (uses_fixed_sl and r[5] is not None) else 0.0
        if sl_axis_col == 'trail_buy_pct':
            row_sl_raw = float(r[6])
        elif sl_axis_col == 'trail_pct':
            row_sl_raw = float(r[7])
        else:
            row_sl_raw = float(r[3])
        row_tpct_raw = float(r[7]) if fourth_axis_col == 'trail_pct' else 0.0
        cached_map[(int(r[2]), row_sl_raw, int(r[1]), int(r[0]), float(r[4]), row_fsl, row_tpct_raw)] = \
            (r[8], r[9], r[10], r[11], r[12] if r[12] is not None else 0.0)
        if r[13] is None and r[10] is not None:
            _missing_cagr_rows.append(r)

    if _missing_cagr_rows:
        campaign_years = _campaign_years_for_window(ticker, start_date, end_date, data_source=data_source)
        if campaign_years is not None:
            backfill_params = []
            for r in _missing_cagr_rows:
                node_cagr = _cagr_from_total_return(r[10], campaign_years)
                if node_cagr is not None:
                    backfill_params.append((node_cagr, strategy_name, config_version, ticker,
                                             r[0], r[1], r[2], r[3], r[4], r[6], r[7], entry_timing))
            if backfill_params:
                cursor.executemany(
                    """UPDATE backtest_phase1 SET cagr=?
                       WHERE strategy=? AND version=? AND ticker=? AND window=? AND max_hold_hours=?
                         AND axis_tp=? AND stop_loss=? AND z_score_threshold=?
                         AND trail_buy_pct=? AND trail_sell_pct=? AND entry_timing=?""",
                    backfill_params
                )
                conn.commit()
                logger.info(f"[{ticker}] {phase_label} (phase1-experiment): backfilled cagr for "
                            f"{len(backfill_params)} pre-existing rows")

    for t in tasks:
        tp, sl, hold_hours, w, z_thresh, tpct = t
        cached_row = cached_map.get((int(tp), float(sl), int(hold_hours), int(w), float(z_thresh), stored_fsl, float(tpct)))
        if cached_row:
            matrix_results.append({
                "Strategy": strategy_name, "Version": config_version, "Ticker": ticker, "Window": w,
                "Take Profit %": int(tp), "Stop Loss %": int(sl), "Max Hold Hours": hold_hours,
                "Z Threshold": z_thresh,
                "Trades": cached_row[0], "Win Rate %": cached_row[1], "Return %": cached_row[2],
                "Alpha vs SPY %": cached_row[3], "Win+TWin Rate %": cached_row[4],
                "Asset B&H %": asset_bh, "SPY B&H %": spy_bh
            })
        else:
            unvisited_tasks.append(t)

    logger.info(f"[{ticker}] {phase_label} (phase1-experiment): {len(matrix_results):,} cached, "
                f"{len(unvisited_tasks):,} to compute (of {len(tasks):,} total)")

    if not unvisited_tasks:
        conn.close()
        return pd.DataFrame(matrix_results)

    futures_map = {
        shared_pool.submit(run_single_backtest_node_ground_truth_isolated,
                           (ticker, strategy_name, config_version, int(tp), int(sl), hold, w, spy_bh, z,
                            fixed_sl, tpct, entry_timing, same_bar_reentry, start_date, end_date,
                            data_source)): task
        for task in unvisited_tasks
        for tp, sl, hold, w, z, tpct in [task]
    }

    progress_bar = tqdm(
        as_completed(futures_map), total=len(futures_map),
        desc=f"[{ticker}] {phase_label} (phase1-experiment)", unit="node",
        mininterval=15.0, maxinterval=30.0
    )

    fail_counts = {}
    buffer = []
    batch_size = 5000

    for future in progress_bar:
        tp, sl, hold_hours, w, z_thresh, tpct = futures_map[future]
        try:
            res = future.result()
            status = res.get("status")
            if status not in ("SUCCESS", "NO_TRADES"):
                fail_counts[status] = fail_counts.get(status, 0) + 1
                if sum(fail_counts.values()) == 1:
                    logger.warning(f"[{ticker}] {phase_label} first failed node TP={tp} SL={sl}: "
                                   f"{status} {res.get('error', '')}")
                continue

            if status == "SUCCESS":
                alpha, num_trades, wr, comp_ret, wtw, node_cagr = res["payload"]
            else:
                alpha, num_trades, wr, comp_ret, wtw, node_cagr = 0.0, 0, 0.0, 0.0, 0.0, None

            progress_bar.set_postfix({"Alpha": f"{alpha:+.1f}%", "Trades": num_trades})

            if status == "SUCCESS":
                matrix_results.append({
                    "Strategy": strategy_name, "Version": config_version, "Ticker": ticker, "Window": w,
                    "Take Profit %": int(tp), "Stop Loss %": int(sl), "Max Hold Hours": hold_hours,
                    "Z Threshold": z_thresh,
                    "Trades": num_trades, "Win Rate %": wr, "Return %": comp_ret,
                    "Alpha vs SPY %": alpha, "Win+TWin Rate %": wtw,
                    "Asset B&H %": asset_bh, "SPY B&H %": spy_bh
                })

            if sl_axis_col == 'trail_buy_pct':
                row_stop_loss, row_trail_buy_pct = int(round(stored_fsl)), float(sl)
                row_trail_pct = float(tpct) if fourth_axis_col == 'trail_pct' else 0.0
            elif sl_axis_col == 'trail_pct':
                row_stop_loss, row_trail_buy_pct, row_trail_pct = int(round(stored_fsl)), 0.0, float(sl)
            else:
                row_stop_loss, row_trail_buy_pct, row_trail_pct = int(sl), 0.0, 0.0

            if strategy_name == 'TrailingBothZScoreBreakout':
                row_take_profit, row_arm_sell_pct = None, float(tp)
            else:
                row_take_profit, row_arm_sell_pct = int(tp), None

            strategies.validate_row_axis_mapping(strategy_name, row_take_profit, row_stop_loss,
                                                  row_trail_buy_pct, row_trail_pct, row_arm_sell_pct)

            buffer.append((strategy_name, config_version, ticker, w, hold_hours, row_take_profit, row_stop_loss,
                           num_trades, wr, comp_ret, alpha, asset_bh, spy_bh, run_timestamp, z_thresh,
                           stored_fsl, row_trail_buy_pct, row_trail_pct, wtw, row_arm_sell_pct, float(tp),
                           entry_timing, None, None, None, None, phase_label, generation, run_id,
                           'ground_truth_v6', node_cagr))

            if len(buffer) >= batch_size:
                cursor.executemany(
                    """INSERT OR REPLACE INTO backtest_phase1
                       (strategy, version, ticker, window, max_hold_hours, take_profit, stop_loss,
                        trades, win_rate, strategy_return, alpha_vs_spy, asset_bh, spy_bh,
                        run_timestamp, z_score_threshold, fixed_sl, trail_buy_pct, trail_sell_pct,
                        win_twin_rate, arm_sell_pct, axis_tp, entry_timing,
                        strategy_return_pessimistic, alpha_vs_spy_pessimistic,
                        strategy_return_certain, alpha_vs_spy_certain, phase, generation, sweep_run_id,
                        kernel_version, cagr)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    buffer
                )
                buffer = []
                conn.commit()

        except Exception as e:
            logger.error(f"Worker crashed TP={tp} SL={sl}: {e}")

    if buffer:
        cursor.executemany(
            """INSERT OR REPLACE INTO backtest_phase1
               (strategy, version, ticker, window, max_hold_hours, take_profit, stop_loss,
                trades, win_rate, strategy_return, alpha_vs_spy, asset_bh, spy_bh,
                run_timestamp, z_score_threshold, fixed_sl, trail_buy_pct, trail_sell_pct,
                win_twin_rate, arm_sell_pct, axis_tp, entry_timing,
                strategy_return_pessimistic, alpha_vs_spy_pessimistic,
                strategy_return_certain, alpha_vs_spy_certain, phase, generation, sweep_run_id,
                kernel_version, cagr)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            buffer
        )
        conn.commit()

    conn.close()
    return pd.DataFrame(matrix_results)


def _phase1_coarse_gt_status_phase1(ticker, strategy_name, config_version, hp, entry_timing, fixed_sl):
    """Parallel twin of _phase1_coarse_gt_status, reading backtest_phase1 instead of
    backtest_cache. No kernel_version filter needed -- every row in backtest_phase1 is
    GT by construction (only dispatch_parallel_grid_ground_truth_phase1 writes here).
    See that function's module-level comment for why this duplicates rather than
    parameterizes the original."""
    trail_pcts = _trail_pcts_for_strategy(strategy_name, hp)
    expected = (len(hp['z_score_thresholds']) * len(hp['windows']) * len(hp['take_profits'])
                * len(hp['stop_losses']) * len(hp['hold_time_caps']) * len(trail_pcts))
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)
    scope_sql, scope_params = _campaign_scope_sql(strategy_name, fixed_sl, entry_timing)
    tpct_filter, tpct_params = "", []
    if fourth_axis_col == 'trail_pct':
        tpct_filter = f" AND trail_sell_pct IN ({','.join('?' * len(trail_pcts))})"
        tpct_params = [float(v) for v in trail_pcts]
    with sqlite3.connect(DB_PATH, timeout=60.0) as conn:
        z_ph = ','.join('?' * len(hp['z_score_thresholds']))
        w_ph = ','.join('?' * len(hp['windows']))
        tp_ph = ','.join('?' * len(hp['take_profits']))
        sl_ph = ','.join('?' * len(hp['stop_losses']))
        hold_ph = ','.join('?' * len(hp['hold_time_caps']))
        done = conn.execute(
            f"SELECT COUNT(*) FROM backtest_phase1 WHERE strategy=? AND version=? AND ticker=?"
            f" AND z_score_threshold IN ({z_ph}) AND window IN ({w_ph})"
            f" AND axis_tp IN ({tp_ph}) AND {_sl_axis_real_column(sl_axis_col)} IN ({sl_ph})"
            f" AND max_hold_hours IN ({hold_ph}) {scope_sql} {tpct_filter}",
            (strategy_name, config_version, ticker, *hp['z_score_thresholds'], *hp['windows'],
             *hp['take_profits'], *hp['stop_losses'], *hp['hold_time_caps'], *scope_params, *tpct_params)
        ).fetchone()[0]
    return done, expected


def _phase2_island_gt_tasks_phase1(ticker, strategy_name, config_version, hp, entry_timing, fixed_sl):
    """Parallel twin of _phase2_island_gt_tasks, reading island-center source data from
    backtest_phase1 instead of backtest_cache. See dispatch_parallel_grid_ground_truth_phase1's
    module-level comment for why this duplicates rather than parameterizes the original."""
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)
    trail_pcts = _trail_pcts_for_strategy(strategy_name, hp)
    scope_sql, scope_params = _campaign_scope_sql(strategy_name, fixed_sl, entry_timing)
    tasks = set()
    with sqlite3.connect(DB_PATH) as conn:
        for z in hp['z_score_thresholds']:
            for w in hp['windows']:
                for tpct in trail_pcts:
                    params = [config_version, ticker, strategy_name, float(z), int(w), *scope_params]
                    tpct_filter = ""
                    if fourth_axis_col == 'trail_pct':
                        tpct_filter = "AND trail_sell_pct=?"
                        params.append(float(tpct))
                    df_wz = pd.read_sql(f"""
                        SELECT axis_tp AS take_profit, {_sl_axis_real_column(sl_axis_col)} AS stop_loss, max_hold_hours, alpha_vs_spy,
                               {ROBUST_ALPHA_SQL} AS robust_alpha, cagr
                        FROM backtest_phase1
                        WHERE version=? AND ticker=? AND strategy=?
                          AND z_score_threshold=? AND window=? AND trades > 0 {scope_sql} {tpct_filter}
                    """, conn, params=params)

                    if df_wz.empty:
                        continue

                    centers = pick_island_centers(df_wz, rank_col='cagr')
                    for (tp_c, sl_c) in centers:
                        for tp in range(max(1, tp_c - FINE_RADIUS), min(30, tp_c + FINE_RADIUS) + 1):
                            for sl in range(max(1, sl_c - FINE_RADIUS), min(30, sl_c + FINE_RADIUS) + 1):
                                for hold in hp['hold_time_caps']:
                                    tasks.add((tp, sl, int(hold), int(w), float(z), float(tpct)))
    return tasks


def run_single_backtest_node_isolated(args):
    (ticker, strategy_name, config_version, tp, sl, hold_hours, w, spy_bh, z_thresh, fixed_sl,
     trail_pct_pct, entry_timing, start_date, end_date, min_hold_hours) = args

    strategy_class = getattr(strategies, strategy_name, None)
    if not strategy_class:
        return {"coords": (tp, sl, hold_hours), "payload": (0.0, 0, 0.0), "window": w, "z_thresh": z_thresh, "status": "UNKNOWN_STRAT"}

    try:
        inputs = _load_node_inputs(ticker, strategy_class, strategy_name, w, z_thresh, start_date, end_date)
    except Exception as e:
        return {"coords": (tp, sl, hold_hours), "payload": (0.0, 0, 0.0), "window": w, "z_thresh": z_thresh, "status": "ERROR", "error": repr(e)}

    if inputs is None:
        return {"coords": (tp, sl, hold_hours), "payload": (0.0, 0, 0.0), "window": w, "z_thresh": z_thresh, "status": "EMPTY"}
    df_hourly_raw, df_daily_processed, prep = inputs

    try:
        result = run_backtest_dispatch(
            strategy_class, df_hourly_raw, df_daily_processed, ticker,
            take_profit=tp, sl_raw=sl, max_hours_to_hold=hold_hours, z_score_threshold=z_thresh,
            fixed_sl=fixed_sl, trail_pct_pct=trail_pct_pct, entry_timing=entry_timing,
            return_bounds=True, prep=prep, min_hold_hours=min_hold_hours
        )
        # pessimistic/certain bounds only exist for TrailingBothZScoreBreakout —
        # every other strategy's dispatch branch ignores return_bounds and returns
        # a plain list.
        if isinstance(result, tuple):
            trades, trades_pess, trades_cert = result
        else:
            trades, trades_pess, trades_cert = result, None, None
        closed_codes = ["WIN", "LOSS", "TWIN", "TLOSS"]
        closed = [t for t in trades if t["Result"] in closed_codes]
        closed_pess = [t for t in trades_pess if t["Result"] in closed_codes] if trades_pess is not None else None
        closed_cert = [t for t in trades_cert if t["Result"] in closed_codes] if trades_cert is not None else None
    except Exception as e:
        return {"coords": (tp, sl, hold_hours), "payload": (0.0, 0, 0.0), "window": w, "z_thresh": z_thresh, "status": "SIM_ERROR", "error": repr(e)}

    if not closed:
        return {"coords": (tp, sl, hold_hours), "payload": (0.0, 0, 0.0), "window": w, "z_thresh": z_thresh, "status": "NO_TRADES"}

    alpha_calc, n_trades, win_rate, compounded, win_twin_rate = _summarize_trades(closed, spy_bh)
    if closed_pess:
        alpha_pess, _, _, compounded_pess, _ = _summarize_trades(closed_pess, spy_bh)
    else:
        alpha_pess, compounded_pess = None, None
    if closed_cert:
        alpha_cert, _, _, compounded_cert, _ = _summarize_trades(closed_cert, spy_bh)
    else:
        alpha_cert, compounded_cert = None, None

    return {
        "coords":  (tp, sl, hold_hours),
        "payload": (alpha_calc, n_trades, win_rate, compounded, win_twin_rate,
                    alpha_pess, compounded_pess, alpha_cert, compounded_cert),
        "window":  w, "z_thresh": z_thresh, "status": "SUCCESS"
    }


def dispatch_parallel_grid(shared_pool, tasks, ticker, strategy_name, config_version, phase_label, spy_bh, asset_bh, run_timestamp, fixed_sl=0, entry_timing='close', generation=None, run_id=None, start_date=None, end_date=None, min_hold_hours=0):
    # backtest_cache has no start_date/end_date columns -- cached_map below keys purely
    # on (tp, sl, hold, w, z, fsl, tpct) within a given (strategy, version, ticker,
    # entry_timing) scope. A windowed call would either read back a stale full-history
    # row under an identical key, or write a windowed result that silently corrupts/
    # collides with the full-history row at that same key -- UNLESS the window itself is
    # encoded into `config_version` (see window_version_suffix() above), which gives full
    # collision-proof isolation for free since every real consumer already scopes by
    # version alone. Rather than a schema/PK migration (seriously considered, rejected as
    # unnecessarily risky), this assertion is the actual guard: it doesn't block a
    # windowed call, it blocks a windowed call whose caller forgot to window the VERSION
    # too -- the one real corruption risk left. See docs/deep_backlog.md's 2026-08-16
    # date-range entry for the full history.
    # WHOEVER CALLS THIS WINDOWED: the version suffix is not the only requirement -- the
    # caller's own compute_bh_returns(ticker) call must ALSO be windowed to the same
    # start_date/end_date, or a windowed trade set gets scored against a full-history
    # spy_bh/asset_bh benchmark, silently reintroducing the exact HIGH bug fixed
    # 2026-08-16 (see run_single_backtest_node_isolated's own spy_bh param, which is
    # honored correctly when windowed -- the gap is only in what the caller passes).
    if start_date is not None or end_date is not None:
        required_suffix = window_version_suffix(start_date, end_date)
        # endswith, not `in` (paired Opus review finding, 2026-08-17): a substring
        # check is a weaker guarantee than the guard's own docstring implies -- this
        # is a fixed-width, unambiguous suffix by construction, so anchoring to the
        # end of the string is free and closes any future false-negative shape
        # (e.g. an already-suffixed --version with the CLI's suffix appended again
        # would still legitimately endswith() the required suffix, so this doesn't
        # change behavior for the real cases, it only removes the substring risk).
        if not config_version.endswith(required_suffix):
            raise ValueError(
                f"dispatch_parallel_grid: windowed call (start_date={start_date!r}, "
                f"end_date={end_date!r}) but config_version={config_version!r} does not "
                f"contain the required suffix {required_suffix!r}. Build config_version "
                f"via window_version_suffix(start_date, end_date) before calling this -- "
                f"a windowed sweep must never share a version string with a full-history "
                f"one, or cached rows from one window silently corrupt/collide with "
                f"another's."
            )
    # min_hold_hours (2026-08-17): same rationale as the start_date/end_date guard
    # above, deliberately NOT combined-and-tested with date-windowing in the same
    # call -- if a future campaign ever needs both, verify the ordering/interaction
    # of the two suffixes before trusting it, don't assume this guard covers that.
    if int(min_hold_hours) != 0:
        required_minhold_suffix = min_hold_version_suffix(min_hold_hours)
        if not config_version.endswith(required_minhold_suffix):
            raise ValueError(
                f"dispatch_parallel_grid: min_hold_hours={min_hold_hours} but "
                f"config_version={config_version!r} does not end with the required "
                f"suffix {required_minhold_suffix!r}. Build config_version via "
                f"min_hold_version_suffix(min_hold_hours) before calling this -- a "
                f"floored campaign must never share a version string with a "
                f"non-floored one, or cached rows silently corrupt/collide."
            )
    conn   = sqlite3.connect(DB_PATH, timeout=60.0)
    cursor = conn.cursor()
    matrix_results  = []
    unvisited_tasks = []

    # v1.8/v1.9/v1.10/v2.11 use fixed_sl as the real stop loss (the swept 'stop_loss'
    # column holds trail_pct/trail_buy_pct for these) — a cache row is only valid for
    # the fixed_sl it was computed with, else re-running with a different fixed_stop_loss
    # would silently serve stale results under the same (tp, sl, hold, w, z) key.
    uses_fixed_sl = strategies.uses_fixed_sl(strategy_name)
    stored_fsl = float(fixed_sl) if uses_fixed_sl else 0.0

    # Which real column a task's raw sl/tpct grid values land in for this strategy —
    # see docs/design.md "Grid axis meaning by strategy".
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)

    # One query for all cached nodes of this (strategy, version, ticker) instead of
    # one SELECT per task (Phase 3 = ~18k queries per ticker).
    cached_map = {}
    cursor.execute("""
        SELECT window, max_hold_hours, axis_tp, stop_loss, z_score_threshold, fixed_sl,
               trail_buy_pct, trail_sell_pct, trades, win_rate, strategy_return, alpha_vs_spy, win_twin_rate
        FROM backtest_cache
        WHERE strategy=? AND version=? AND ticker=? AND entry_timing=?
          AND (kernel_version IS NULL OR kernel_version<>'ground_truth_v6')
    """, (strategy_name, config_version, ticker, entry_timing))
    for r in cursor.fetchall():
        if r[4] is None:
            continue  # legacy NULL-z rows never matched the old equality check either
        row_fsl = float(r[5]) if (uses_fixed_sl and r[5] is not None) else 0.0
        # Recover the raw sl/tpct grid values this row was computed with, from
        # whichever real column holds them for this strategy.
        if sl_axis_col == 'trail_buy_pct':
            row_sl_raw = float(r[6])
        elif sl_axis_col == 'trail_pct':
            row_sl_raw = float(r[7])
        else:
            row_sl_raw = float(r[3])
        row_tpct_raw = float(r[7]) if fourth_axis_col == 'trail_pct' else 0.0
        # axis_tp (not take_profit — NULL for TrailingBothZScoreBreakout) is the raw
        # swept 'tp' grid value regardless of strategy.
        cached_map[(int(r[2]), row_sl_raw, int(r[1]), int(r[0]), float(r[4]), row_fsl, row_tpct_raw)] = \
            (r[8], r[9], r[10], r[11], r[12] if r[12] is not None else 0.0)

    for t in tasks:
        tp, sl, hold_hours, w, z_thresh, tpct = t
        cached_row = cached_map.get((int(tp), float(sl), int(hold_hours), int(w), float(z_thresh), stored_fsl, float(tpct)))
        if cached_row:
            matrix_results.append({
                "Strategy": strategy_name, "Version": config_version, "Ticker": ticker, "Window": w,
                "Take Profit %": int(tp), "Stop Loss %": int(sl), "Max Hold Hours": hold_hours,
                "Z Threshold": z_thresh,
                "Trades": cached_row[0], "Win Rate %": cached_row[1], "Return %": cached_row[2],
                "Alpha vs SPY %": cached_row[3], "Win+TWin Rate %": cached_row[4],
                "Asset B&H %": asset_bh, "SPY B&H %": spy_bh
            })
        else:
            unvisited_tasks.append(t)

    logger.info(f"[{ticker}] {phase_label}: {len(matrix_results):,} cached, "
                f"{len(unvisited_tasks):,} to compute (of {len(tasks):,} total)")

    if not unvisited_tasks:
        conn.close()
        return pd.DataFrame(matrix_results)

    try:
        with open("active_phase_grid.json", "w") as gf:
            json.dump({"phase": phase_label, "nodes": [
                {"take_profit": int(t[0]), "stop_loss": int(t[1]), "max_hold_hours": int(t[2])}
                for t in unvisited_tasks
            ]}, gf)
    except Exception:
        pass

    futures_map = {
        shared_pool.submit(run_single_backtest_node_isolated,
                           (ticker, strategy_name, config_version, int(tp), int(sl), hold, w, spy_bh, z, fixed_sl,
                            tpct, entry_timing, start_date, end_date, min_hold_hours)): task
        for task in unvisited_tasks
        for tp, sl, hold, w, z, tpct in [task]
    }

    progress_bar = tqdm(
        as_completed(futures_map), total=len(futures_map),
        desc=f"[{ticker}] {phase_label}", unit="node",
        mininterval=15.0, maxinterval=30.0
    )

    node_counter      = 0
    last_postfix_time = 0.0
    fail_counts       = {}
    buffer            = []
    batch_size        = 5000

    for future in progress_bar:
        tp, sl, hold_hours, w, z_thresh, tpct = futures_map[future]
        try:
            res    = future.result()
            status = res.get("status")
            if status not in ("SUCCESS", "NO_TRADES"):
                fail_counts[status] = fail_counts.get(status, 0) + 1
                if sum(fail_counts.values()) == 1:
                    logger.warning(f"[{ticker}] {phase_label} first failed node TP={tp} SL={sl}: "
                                   f"{status} {res.get('error', '')}")
            if status in ("SUCCESS", "NO_TRADES"):
                if status == "SUCCESS":
                    (alpha, num_trades, wr, comp_ret, wtw,
                     alpha_pess, comp_ret_pess, alpha_cert, comp_ret_cert) = res["payload"]
                else:
                    alpha, num_trades, wr, comp_ret, wtw = 0.0, 0, 0.0, 0.0, 0.0
                    alpha_pess, comp_ret_pess, alpha_cert, comp_ret_cert = None, None, None, None

                now = time.time()
                if now - last_postfix_time >= 2.0:
                    progress_bar.set_postfix({"Alpha": f"{alpha:+.1f}%", "Trades": num_trades})
                    last_postfix_time = now

                node_counter += 1
                if node_counter % 50 == 0:
                    try:
                        with open("current_test.json", "w") as tf:
                            json.dump({"phase": phase_label, "ticker": ticker, "strategy": strategy_name,
                                       "version": config_version, "take_profit": int(tp),
                                       "stop_loss": int(sl), "max_hold_hours": int(hold_hours)}, tf)
                    except Exception:
                        pass

                if status == "SUCCESS":
                    matrix_results.append({
                        "Strategy": strategy_name, "Version": config_version, "Ticker": ticker, "Window": w,
                        "Take Profit %": int(tp), "Stop Loss %": int(sl), "Max Hold Hours": hold_hours,
                        "Z Threshold": z_thresh,
                        "Trades": num_trades, "Win Rate %": wr, "Return %": comp_ret,
                        "Alpha vs SPY %": alpha, "Win+TWin Rate %": wtw,
                        "Asset B&H %": asset_bh, "SPY B&H %": spy_bh
                    })

                # Map this task's raw sl/tpct grid values onto the real named columns.
                if sl_axis_col == 'trail_buy_pct':
                    row_stop_loss, row_trail_buy_pct = int(round(stored_fsl)), float(sl)
                    row_trail_pct = float(tpct) if fourth_axis_col == 'trail_pct' else 0.0
                elif sl_axis_col == 'trail_pct':
                    row_stop_loss, row_trail_buy_pct, row_trail_pct = int(round(stored_fsl)), 0.0, float(sl)
                else:
                    row_stop_loss, row_trail_buy_pct, row_trail_pct = int(sl), 0.0, 0.0

                # take_profit is NULL for TrailingBothZScoreBreakout (real value lives in
                # arm_sell_pct instead — mirrors active_signals.py's live-side split).
                # axis_tp always holds the raw 'tp' value regardless of strategy, since
                # the PK can't dedupe on a column that's sometimes NULL.
                if strategy_name == 'TrailingBothZScoreBreakout':
                    row_take_profit, row_arm_sell_pct = None, float(tp)
                else:
                    row_take_profit, row_arm_sell_pct = int(tp), None

                buffer.append((strategy_name, config_version, ticker, w, hold_hours, row_take_profit, row_stop_loss,
                               num_trades, wr, comp_ret, alpha, asset_bh, spy_bh, run_timestamp, z_thresh,
                               stored_fsl, row_trail_buy_pct, row_trail_pct, wtw, row_arm_sell_pct, float(tp),
                               entry_timing, comp_ret_pess, alpha_pess, comp_ret_cert, alpha_cert, phase_label,
                               generation, run_id))

                if len(buffer) >= batch_size:
                    cursor.executemany(
                        """INSERT OR REPLACE INTO backtest_cache
                           (strategy, version, ticker, window, max_hold_hours, take_profit, stop_loss,
                            trades, win_rate, strategy_return, alpha_vs_spy, asset_bh, spy_bh,
                            run_timestamp, z_score_threshold, fixed_sl, trail_buy_pct, trail_sell_pct,
                            win_twin_rate, arm_sell_pct, axis_tp, entry_timing,
                            strategy_return_pessimistic, alpha_vs_spy_pessimistic,
                            strategy_return_certain, alpha_vs_spy_certain, phase, generation, sweep_run_id)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        buffer
                    )
                    buffer = []
                    conn.commit()

        except Exception as e:
            logger.error(f"Worker crashed TP={tp} SL={sl}: {e}")

    if buffer:
        cursor.executemany(
            """INSERT OR REPLACE INTO backtest_cache
               (strategy, version, ticker, window, max_hold_hours, take_profit, stop_loss,
                trades, win_rate, strategy_return, alpha_vs_spy, asset_bh, spy_bh,
                run_timestamp, z_score_threshold, fixed_sl, trail_buy_pct, trail_sell_pct,
                win_twin_rate, arm_sell_pct, axis_tp, entry_timing,
                strategy_return_pessimistic, alpha_vs_spy_pessimistic,
                strategy_return_certain, alpha_vs_spy_certain, phase, generation, sweep_run_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            buffer
        )
    conn.commit()
    progress_bar.close()
    conn.close()
    if fail_counts:
        logger.warning(f"[{ticker}] {phase_label}: {sum(fail_counts.values())} nodes failed {fail_counts}")
    return pd.DataFrame(matrix_results)


# ── B&H helper ────────────────────────────────────────────────────────────────

def compute_bh_returns(ticker, start_date=None, end_date=None, data_source="yahoo"):
    # start_date/end_date default to None -- the non-windowed call path (every existing
    # caller) is byte-identical to before this param was added. When windowed, both legs
    # (asset_bh AND spy_bh) get sliced to the SAME [start_date, end_date] range the trade
    # set itself was windowed to -- same technique as
    # scripts/train_test_split_check.py::period_spy_bh, reused rather than reinvented.
    # Without this, a windowed trade set's alpha_calc (_summarize_trades: compounded -
    # spy_bh) would silently diff against a full-history benchmark, which is wrong for
    # exactly the cross-window comparison date-windowing exists to support.
    #
    # data_source='yahoo' (default, unchanged): both legs read cache/research/*_1h.csv,
    # exactly as before this param existed. data_source='massive': both legs (asset AND
    # SPY benchmark) read db_cache.get_massive_hourly_ohlcv instead, so a
    # massive-sourced trade set is benchmarked apples-to-apples against a
    # massive-sourced SPY B&H, not a Yahoo one.
    if data_source == "massive":
        import db_cache
        try:
            df = db_cache.get_massive_hourly_ohlcv(ticker)
        except ValueError:
            return None, None
    else:
        cache_path = CACHE_DIR / f"{ticker}_1h.csv"
        if not cache_path.exists():
            return None, None
        df = pd.read_csv(cache_path, index_col=0, parse_dates=True).sort_index()
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    if start_date is not None or end_date is not None:
        # end_date is inclusive-through-end-of-day, matching _window_prep's convention
        # exactly (pd.Timestamp(end_date) is midnight -- extend by a day or a bar ON
        # end_date is silently excluded).
        # Fail loudly on a degenerate window, matching _window_prep's ValueError
        # convention (found by review, 2026-08-16: this used to return (None, None)
        # here, which a caller could confuse with "no SPY cache file" instead of
        # "you gave a bad date range").
        if start_date is not None and end_date is not None and pd.Timestamp(start_date) > pd.Timestamp(end_date):
            raise ValueError(f"compute_bh_returns: start_date {start_date} is after end_date {end_date}")
        full_span = f"{df.index.min()} to {df.index.max()}" if len(df) else "empty"
        lo = pd.Timestamp(start_date) if start_date is not None else df.index.min()
        hi = (pd.Timestamp(end_date) + pd.Timedelta(days=1)) if end_date is not None else df.index.max() + pd.Timedelta(days=1)
        df = df.loc[(df.index >= lo) & (df.index < hi)]
        if len(df) < 2:
            raise ValueError(
                f"compute_bh_returns: window [{start_date}, {end_date}] produced too few "
                f"bars to compute a return (cached data spans {full_span}) -- check the "
                f"window is inside the cached data's range and start_date <= end_date")
    close_col = 'Adj Close' if 'Adj Close' in df.columns else 'Close'
    asset_bh  = ((df[close_col].iloc[-1] - df[close_col].iloc[0]) / df[close_col].iloc[0]) * 100

    spy_bh = 0.0
    if data_source == "massive":
        import db_cache
        try:
            spy_df = db_cache.get_massive_hourly_ohlcv("SPY")
        except ValueError:
            spy_df = None
    else:
        spy_cache = CACHE_DIR / "SPY_1h.csv"
        spy_df = pd.read_csv(spy_cache, index_col=0, parse_dates=True).sort_index() if spy_cache.exists() else None
    if spy_df is not None:
        if spy_df.index.tz is not None:
            spy_df.index = spy_df.index.tz_localize(None)
        sliced = spy_df.loc[df.index.min():df.index.max()]
        if not sliced.empty:
            spy_col = 'Adj Close' if 'Adj Close' in spy_df.columns else 'Close'
            spy_bh  = ((sliced[spy_col].iloc[-1] - sliced[spy_col].iloc[0]) / sliced[spy_col].iloc[0]) * 100
    return asset_bh, spy_bh


# ── Island selection ──────────────────────────────────────────────────────────

# A node's "robust" alpha is the worst of its three fill-optimism resolutions
# (possible/pessimistic/certain — see backtester._simulate_trail_both), not just
# the optimistic 'possible' number — island search and cliff-safety rank/filter
# on this everywhere so a node is only ever selected when it holds up under all
# three, not just the default optimistic one. COALESCE falls back to alpha_vs_spy
# itself for strategies that don't produce pessimistic/certain bounds (NULL).
ROBUST_ALPHA_SQL = ("MIN(alpha_vs_spy, COALESCE(alpha_vs_spy_pessimistic, alpha_vs_spy), "
                     "COALESCE(alpha_vs_spy_certain, alpha_vs_spy))")

# Deterministic tiebreak for GT top-3-per-island candidate selection (derive_phase25_
# candidates_ground_truth / run_phase25_cliff_box_ground_truth's `region.sort_values(
# 'robust_alpha', ...)` step) -- found 2026-08-22 via the GT-kernel prune-validation
# gate's first real-data run: a real SOXL campaign (v6-w2026-06-01_2026-08-01,
# TrailingBoth) had multiple tpct values tied at the exact same robust_alpha within one
# island, and neither this function's pandas sort nor the validator's independent SQL
# re-implementation applied any tiebreak, so each picked an arbitrary (and different)
# top-3 subset among the tied cells -- the same bug class already fixed once in
# prune_backtest_cache.py's own TIEBREAK_SQL (found 2026-08-07, 5,800/11,288 groups tied
# on max robust_alpha there). Each column here is a genuine secondary preference, not
# merely a determinism nonce:
#   trades DESC          -- more trades backing the same alpha is a more statistically
#                            reliable sample, not a coincidence from a thin sample.
#   stop_loss ASC         -- among configs tied on alpha/trades, the tighter stop achieved
#                            the same return while risking less per trade (genuinely
#                            better risk-adjusted).
#   max_hold_hours ASC    -- among configs additionally tied on stop_loss, the shorter
#                            hold achieved the same return with less capital tied up per
#                            trade (better capital efficiency).
# window/z_score_threshold/tpct/take_profit ASC are appended only as a final catch-all in
# case all of the above still tie -- no risk/capital-efficiency claim is made for those,
# they just guarantee the exact same winner every run against the same data.
GT_CANDIDATE_TIEBREAK = [
    ('trades', False), ('stop_loss', True), ('max_hold_hours', True),
    ('window', True), ('z_score_threshold', True), ('tpct', True), ('take_profit', True),
]


def pick_island_centers(df, n=N_ISLANDS, min_sep=ISLAND_MIN_SEP, rank_col=None):
    """Greedily picks up to n island centers by descending rank, skipping any coordinate
    within min_sep of an already-picked center.

    Deterministic tiebreak on (take_profit, stop_loss) added 2026-08-23 -- found by the
    GT-kernel prune-validation gate's first real-data end-to-end run: a real SOXL/
    TrailingBoth scope (v6-w2026-06-01_2026-08-01) has 15 rows spanning 3 distinct
    coordinates -- (21,3), (24,3), (27,3) -- tied at the exact same bit-identical
    top-of-scope robust_alpha. Without a tiebreak, `df.sort_values(rank_col,
    ascending=False)` alone leaves the winner among exact ties decided by incidental
    DataFrame row order, which is NOT invariant under an operation that changes which
    physical rows are present (e.g. re-deriving centers post-prune, or re-running a sweep
    after new unrelated rows land in the same scope) even though every candidate row's
    OWN robust_alpha value is unchanged. This produced a real, reproducible divergence:
    `pick_island_centers` returned `[(24,3),(15,2),(30,3)]` against the live DB and
    `[(27,3),(21,3),(15,2)]` against the exact same scope's pruned copy. Same bug class as
    GT_CANDIDATE_TIEBREAK (top-3-within-island pick, fixed 2026-08-22) and the legacy
    prune_backtest_cache.py's TIEBREAK_SQL (2026-08-07) -- all three are "pandas/SQL sort
    on a column with real ties and no secondary key" instances found via this same
    project's real campaign data. take_profit/stop_loss ASC (rather than reusing the full
    GT_CANDIDATE_TIEBREAK column list) is deliberately minimal here: this function ranks
    COORDINATES across the whole scope (window/z/hold/tpct not yet chosen), so only the
    axes it actually receives are available to break a tie on; once a center coordinate is
    picked, GT_CANDIDATE_TIEBREAK's fuller column set resolves the *within-island* pick.
    Verified 2026-08-23: with this tiebreak, `pick_island_centers` (and therefore
    `derive_phase25_candidates_ground_truth`'s full top-9-per-scope candidate list)
    reproduces identically pre- and post-prune across all 16 real ground_truth_v6 scopes
    in the production DB, including the previously-divergent SOXL scope above.

    rank_col (2026-08-23, ground_truth_kernel_rebuild.md Step 4): explicit override for
    which column to rank centers by. GT callers pass rank_col='cagr' -- CAGR (bounded,
    real compounded return) is the sole GT selection metric now, alpha is diagnostic-only
    for GT. Legacy (non-GT) callers never pass this, so they keep the original
    robust_alpha/alpha_vs_spy fallback unchanged below -- this is a pure additive
    override, no legacy behavior change.
    """
    if rank_col is None:
        rank_col = 'robust_alpha' if 'robust_alpha' in df.columns else 'alpha_vs_spy'
    centers = []
    df_sorted = df.sort_values([rank_col, 'take_profit', 'stop_loss'],
                                ascending=[False, True, True])
    for _, row in df_sorted.iterrows():
        tp, sl = int(row['take_profit']), int(row['stop_loss'])
        if all(abs(tp - c[0]) >= min_sep or abs(sl - c[1]) >= min_sep for c in centers):
            centers.append((tp, sl))
        if len(centers) == n:
            break
    return centers


# ── Phase 1: Coarse scan ──────────────────────────────────────────────────────

def _campaign_scope_sql(strategy_name, fixed_sl, entry_timing):
    """Extra WHERE-clause fragment + params to keep a phase2/2.5/checkpoint query
    scoped to one (stop_loss, entry_timing) campaign when multiple v4-style
    campaigns share a single version string — see docs/design.md's 3-axis island
    cap and the v4 plan's separate-campaign-per-(stop_loss,entry_timing) design.
    For strategies whose real 'stop_loss' column IS the swept grid axis (not
    fixed_sl), only entry_timing needs scoping."""
    if strategies.uses_fixed_sl(strategy_name):
        return " AND stop_loss=? AND entry_timing=?", [int(round(float(fixed_sl))), entry_timing]
    return " AND entry_timing=?", [entry_timing]


def run_phase1_coarse(shared_pool, ticker, strategy_name, config_version, hp, spy_bh, asset_bh, run_timestamp, fixed_sl=0, entry_timing='close', run_id=None, start_date=None, end_date=None, min_hold_hours=0):
    z_thresholds = hp['z_score_thresholds']
    trail_pcts = _trail_pcts_for_strategy(strategy_name, hp)
    expected = (len(z_thresholds) * len(hp['windows']) * len(hp['take_profits'])
                * len(hp['stop_losses']) * len(hp['hold_time_caps']) * len(trail_pcts))

    with sqlite3.connect(DB_PATH, timeout=60.0) as chk:
        z_ph = ','.join('?' * len(z_thresholds))
        w_ph = ','.join('?' * len(hp['windows']))
        scope_sql, scope_params = _campaign_scope_sql(strategy_name, fixed_sl, entry_timing)
        cached = chk.execute(
            f"SELECT COUNT(*) FROM backtest_cache WHERE strategy=? AND version=? AND ticker=?"
            f" AND z_score_threshold IN ({z_ph}) AND window IN ({w_ph}) {scope_sql}",
            (strategy_name, config_version, ticker, *z_thresholds, *hp['windows'], *scope_params)
        ).fetchone()[0]

    # Disabled 2026-07-20: this was a row-count comparison, not a verification
    # that the cached rows were computed with current code -- it silently
    # skipped Phase 1 (and let a kernel fix go unexercised) after the
    # backtester.py gap-fix. dispatch_parallel_grid's own per-task cache
    # lookup below still avoids recomputing genuinely-unchanged nodes.
    # if cached >= expected:
    #     logger.info(f"[{ticker}] Phase1 fully cached ({cached}/{expected}). Skipping.")
    #     return

    tasks = [(int(tp), int(sl), int(hold), int(w), float(z), float(tpct))
             for z    in z_thresholds
             for w    in hp['windows']
             for tp   in hp['take_profits']
             for sl   in hp['stop_losses']
             for hold in hp['hold_time_caps']
             for tpct in trail_pcts]

    dispatch_parallel_grid(shared_pool, tasks, ticker, strategy_name, config_version,
                           "Phase1-Coarse", spy_bh, asset_bh, run_timestamp, fixed_sl, entry_timing, run_id=run_id,
                           start_date=start_date, end_date=end_date, min_hold_hours=min_hold_hours)


# ── Checkpoint 1: rank by coarse alpha, return island candidates ──────────────

def identify_island_candidates(config_version, strategy_name, n_index, n_stock, allowed_tickers=None, fixed_sl=0, entry_timing='close'):
    scope_sql, scope_params = _campaign_scope_sql(strategy_name, fixed_sl, entry_timing)
    with sqlite3.connect(DB_PATH) as conn:
        query = f"""
            SELECT b.ticker, MAX({ROBUST_ALPHA_SQL}) as best_alpha,
                   t.index_underlier, t.stock_underlier
            FROM backtest_cache b
            LEFT JOIN tickers t ON t.symbol = b.ticker
            WHERE b.version=? AND b.strategy=? AND b.trades > 0
              AND (b.kernel_version IS NULL OR b.kernel_version<>'ground_truth_v6') {scope_sql}
        """
        params = [config_version, strategy_name, *scope_params]
        if allowed_tickers:
            query += f" AND b.ticker IN ({','.join('?' * len(allowed_tickers))})"
            params += list(allowed_tickers)
        query += " GROUP BY b.ticker ORDER BY best_alpha DESC"
        df = pd.read_sql(query, conn, params=params)

    def utype(row):
        if pd.notna(row.get('index_underlier')) and row['index_underlier']:
            return 'index'
        return 'other'

    df['underlier'] = df.apply(utype, axis=1)
    top_index = df[df['underlier'] == 'index'].head(n_index)['ticker'].tolist()
    top_other  = df[df['underlier'] != 'index'].head(n_stock)['ticker'].tolist()

    logger.info(f"Checkpoint1 — top index ({n_index}): {top_index}")
    logger.info(f"Checkpoint1 — top other ({n_stock}): {top_other}")
    return top_index, top_other


# ── Phase 2: Island mesh ──────────────────────────────────────────────────────

def run_phase2_island(shared_pool, ticker, strategy_name, config_version, hp, spy_bh, asset_bh, run_timestamp, fixed_sl=0, entry_timing='close', generation=None, run_id=None, start_date=None, end_date=None, min_hold_hours=0):
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)
    trail_pcts = _trail_pcts_for_strategy(strategy_name, hp)
    scope_sql, scope_params = _campaign_scope_sql(strategy_name, fixed_sl, entry_timing)
    tasks = set()
    with sqlite3.connect(DB_PATH) as conn:
        for z in hp['z_score_thresholds']:
            for w in hp['windows']:
                for tpct in trail_pcts:
                    params = [config_version, ticker, strategy_name, float(z), int(w), *scope_params]
                    tpct_filter = ""
                    if fourth_axis_col == 'trail_pct':
                        tpct_filter = "AND trail_sell_pct=?"
                        params.append(float(tpct))
                    df_wz = pd.read_sql(f"""
                        SELECT axis_tp AS take_profit, {_sl_axis_real_column(sl_axis_col)} AS stop_loss, max_hold_hours, alpha_vs_spy,
                               {ROBUST_ALPHA_SQL} AS robust_alpha
                        FROM backtest_cache
                        WHERE version=? AND ticker=? AND strategy=?
                          AND z_score_threshold=? AND window=? AND trades > 0
                          AND (kernel_version IS NULL OR kernel_version<>'ground_truth_v6') {scope_sql} {tpct_filter}
                    """, conn, params=params)

                    if df_wz.empty:
                        continue

                    centers = pick_island_centers(df_wz)
                    for (tp_c, sl_c) in centers:
                        for tp in range(max(1, tp_c - FINE_RADIUS), min(30, tp_c + FINE_RADIUS) + 1):
                            for sl in range(max(1, sl_c - FINE_RADIUS), min(30, sl_c + FINE_RADIUS) + 1):
                                for hold in hp['hold_time_caps']:
                                    tasks.add((tp, sl, int(hold), int(w), float(z), float(tpct)))

    if not tasks:
        logger.warning(f"[{ticker}] Phase2: no island tasks generated.")
        return

    logger.info(f"[{ticker}] Phase2 island mesh: {len(tasks)} tasks ({N_ISLANDS} islands ±{FINE_RADIUS})")
    dispatch_parallel_grid(shared_pool, list(tasks), ticker, strategy_name, config_version,
                           "Phase2-Island", spy_bh, asset_bh, run_timestamp, fixed_sl, entry_timing,
                           generation=generation, run_id=run_id, start_date=start_date, end_date=end_date,
                           min_hold_hours=min_hold_hours)


def _require_full_gt_hp(hp, strategy_name, caller_name):
    """Validates hp carries every axis Phase1's own `expected` formula needs, each as a
    genuinely NON-EMPTY sequence (not just present) — found by the paired-review
    independent-cold pass on this guard itself (2026-08-22): an empty list (e.g.
    take_profits=[]) satisfies a bare `key in hp` check while making `expected` come out
    0, so `done(0) < expected(0)` is False and the completeness guard silently passes
    against a Phase1 that never ran. Also found: `trail_pcts` was missing from the
    original required-key list entirely -- the exact same silent-fallback failure shape
    that produced 9ab807a's bogus smoke-test numbers (_trail_pcts_for_strategy defaults
    to config.json's value when the key is absent, understating `expected`)."""
    required = ['take_profits', 'stop_losses', 'hold_time_caps', 'windows', 'z_score_thresholds']
    _, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)
    if fourth_axis_col == 'trail_pct':
        required.append('trail_pcts')
    for key in required:
        val = hp.get(key)
        if not val:
            raise ValueError(
                f"{caller_name}: hp['{key}'] is missing or empty -- pass the FULL, non-empty "
                f"campaign hp dict (same shape as run_phase1_coarse's), not a reduced one, so "
                f"Phase1-Coarse-GT completeness can actually be verified before this reads "
                f"island centers from it."
            )


def _phase1_coarse_gt_status(ticker, strategy_name, config_version, hp, entry_timing, fixed_sl):
    """(done_count, expected_count) of real-minute-kernel cells computed for this exact
    campaign scope, regardless of which phase wrote them. Caller must have already
    validated hp via _require_full_gt_hp (expected can't come out 0 as a result).

    Deliberately NOT filtered to phase='Phase1-Coarse-GT' (found by the paired-review
    independent-cold pass, 2026-08-22): backtest_cache's PK excludes both `phase` and
    `kernel_version`, and every dispatch write is INSERT OR REPLACE using whatever
    phase_label the caller passed -- so a cell Phase2-GT computes that happens to fall
    inside Phase1's own task grid gets silently RELABELED if Phase1 (re)computes that
    same coordinate later, and if Phase1 restarts and treats that coordinate as already-
    cached (its own cached_map lookup, keyed on the cell's values not its phase), it never
    relabels it back -- permanently capping `done` below `expected` with no way to clear
    it short of deleting rows. Counting ANY ground_truth_v6 row at these coordinates
    (whichever phase last wrote it) is what actually answers "has every cell in this
    scope been computed," which is the real question -- not "which phase's name is
    currently stamped on it." Mirrors run_phase1_coarse's own pre-check query
    (run_optimization_sweep.py:~1485) for the WHERE-scoping shape.

    IMPORTANT: `done` is also constrained to axis_tp/sl-axis-column/max_hold_hours/4th-axis
    (trail_sell_pct, when the strategy has one) values that are literally IN Phase1's own
    hp lists (not just z/window/scope-scoped) — without this, Phase2/2.5's fine mesh
    (which explores full-integer tp/sl neighborhoods, not just Phase1's sparse coarse-grid
    values) would get counted as generic "done" cells too, letting `done` climb past
    `expected` from off-grid cells while genuine Phase1 grid coordinates are still
    unfilled -- a false-complete pass. The 4th-axis constraint was originally missing from
    this fix (paired-review verification pass, 2026-08-22, found it as a live-demonstrable
    gap: narrowing trail_pcts to a subset already covered by broader real data made `done`
    count rows outside the narrowed set, again risking a false-complete pass) -- not yet
    live-triggerable in practice (every real trail_sell_pct value on file is already
    inside the campaign's own trail_pcts list), but fixed defensively before it could be."""
    trail_pcts = _trail_pcts_for_strategy(strategy_name, hp)
    expected = (len(hp['z_score_thresholds']) * len(hp['windows']) * len(hp['take_profits'])
                * len(hp['stop_losses']) * len(hp['hold_time_caps']) * len(trail_pcts))
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)
    scope_sql, scope_params = _campaign_scope_sql(strategy_name, fixed_sl, entry_timing)
    tpct_filter, tpct_params = "", []
    if fourth_axis_col == 'trail_pct':
        tpct_filter = f" AND trail_sell_pct IN ({','.join('?' * len(trail_pcts))})"
        tpct_params = [float(v) for v in trail_pcts]
    with sqlite3.connect(DB_PATH, timeout=60.0) as conn:
        z_ph = ','.join('?' * len(hp['z_score_thresholds']))
        w_ph = ','.join('?' * len(hp['windows']))
        tp_ph = ','.join('?' * len(hp['take_profits']))
        sl_ph = ','.join('?' * len(hp['stop_losses']))
        hold_ph = ','.join('?' * len(hp['hold_time_caps']))
        done = conn.execute(
            f"SELECT COUNT(*) FROM backtest_cache WHERE strategy=? AND version=? AND ticker=?"
            f" AND kernel_version='ground_truth_v6'"
            f" AND z_score_threshold IN ({z_ph}) AND window IN ({w_ph})"
            f" AND axis_tp IN ({tp_ph}) AND {_sl_axis_real_column(sl_axis_col)} IN ({sl_ph})"
            f" AND max_hold_hours IN ({hold_ph}) {scope_sql} {tpct_filter}",
            (strategy_name, config_version, ticker, *hp['z_score_thresholds'], *hp['windows'],
             *hp['take_profits'], *hp['stop_losses'], *hp['hold_time_caps'], *scope_params, *tpct_params)
        ).fetchone()[0]
    return done, expected


def _phase2_island_gt_tasks(ticker, strategy_name, config_version, hp, entry_timing, fixed_sl):
    """Task set (tp, sl, hold, w, z, tpct) that Phase2-Island-GT's own island-mesh
    generation builds for this exact campaign scope. Phase2's mesh size is data-dependent
    (unlike Phase1's fixed grid), so there's no formula for `expected`, only rebuilding
    the actual mesh.

    Unrestricted -- sees the full current scope, including any prior generation's own
    fine-mesh rows, matching legacy's run_phase2_island/run_phase25_cliff_box exactly
    (neither has ever had any grid restriction). This function used to also support a
    restrict_to_phase1_grid=True mode for _phase2_island_gt_status's completeness check
    (added 2026-08-22, preemptively, for a hypothetical mid-Phase2 call that never
    actually occurs in the real call chain) -- removed 2026-08-23 after confirming
    against real data that the restricted set is NOT reliably a subset of what
    unrestricted dispatch actually computes (different pick_island_centers picks on
    different underlying data), which could false-block a genuinely complete run. Both
    real callers of the completeness check only ever run after Phase2's entire
    generation loop finishes (data is static), so matching dispatch's own query exactly
    is both simpler and correct, not just tolerable."""
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)
    trail_pcts = _trail_pcts_for_strategy(strategy_name, hp)
    scope_sql, scope_params = _campaign_scope_sql(strategy_name, fixed_sl, entry_timing)
    tasks = set()
    with sqlite3.connect(DB_PATH) as conn:
        for z in hp['z_score_thresholds']:
            for w in hp['windows']:
                for tpct in trail_pcts:
                    params = [config_version, ticker, strategy_name, float(z), int(w), *scope_params]
                    tpct_filter = ""
                    if fourth_axis_col == 'trail_pct':
                        tpct_filter = "AND trail_sell_pct=?"
                        params.append(float(tpct))
                    df_wz = pd.read_sql(f"""
                        SELECT axis_tp AS take_profit, {_sl_axis_real_column(sl_axis_col)} AS stop_loss, max_hold_hours, alpha_vs_spy,
                               {ROBUST_ALPHA_SQL} AS robust_alpha, cagr
                        FROM backtest_cache
                        WHERE version=? AND ticker=? AND strategy=?
                          AND z_score_threshold=? AND window=? AND trades > 0
                          AND kernel_version='ground_truth_v6' {scope_sql} {tpct_filter}
                    """, conn, params=params)

                    if df_wz.empty:
                        continue

                    # rank_col='cagr' (2026-08-23, ground_truth_kernel_rebuild.md Step 4):
                    # this is real GT task-generation logic (matches actual Phase2-GT
                    # dispatch), so it must rank centers the same way the rest of the GT
                    # pipeline now does -- CAGR, not alpha.
                    centers = pick_island_centers(df_wz, rank_col='cagr')
                    for (tp_c, sl_c) in centers:
                        for tp in range(max(1, tp_c - FINE_RADIUS), min(30, tp_c + FINE_RADIUS) + 1):
                            for sl in range(max(1, sl_c - FINE_RADIUS), min(30, sl_c + FINE_RADIUS) + 1):
                                for hold in hp['hold_time_caps']:
                                    tasks.add((tp, sl, int(hold), int(w), float(z), float(tpct)))
    return tasks


def run_phase2_island_ground_truth(shared_pool, ticker, strategy_name, config_version, hp, spy_bh,
                                    asset_bh, run_timestamp, fixed_sl=0, entry_timing='open_check',
                                    same_bar_reentry=True, generation=None, run_id=None,
                                    start_date=None, end_date=None, data_source="yahoo"):
    """v6 counterpart to run_phase2_island. Task-GENERATION logic (island-center detection
    off backtest_cache, ±FINE_RADIUS mesh) is IDENTICAL to the hourly version, copied
    rather than shared, because it's genuinely kernel-agnostic (queries backtest_cache
    generically by version/strategy/ticker — ROBUST_ALPHA_SQL/pick_island_centers don't
    know or care which kernel produced a row). Only the final dispatch call differs:
    dispatch_parallel_grid_ground_truth instead of dispatch_parallel_grid. Requires
    Phase1-coarse-GT to have already been run for the same (ticker, strategy,
    config_version) — reads its rows to find islands.

    HARD-BLOCKS if Phase1-Coarse-GT isn't complete for this scope (found by the paired-
    review pass on 9ab807a: running this against a still-growing Phase1 grid would
    silently mesh around provisional/wrong centers, with no signal anything was wrong —
    the fix is to fail loudly, not to warn-and-continue). `hp` MUST be the full campaign
    hp dict (take_profits/stop_losses included, matching run_phase1_coarse's own shape) —
    passing a reduced hp (as an earlier smoke test did) will raise here rather than
    silently under-checking completeness.

    Callable multiple times with an increasing `generation` number (see
    run_ground_truth_phase1.py's --generations loop) to mirror the legacy engine's own
    multi-generation Phase2 -- each call re-derives centers off whatever's currently in
    backtest_cache (including any prior generation's own fine-mesh rows), same as
    run_phase2_island (legacy) always did. No exclusion/forcing mechanism -- a
    generation that finds nothing new simply re-picks the same centers and cache-hits
    its way to a fast no-op, matching legacy's real behavior exactly (2026-08-23: an
    earlier attempt to force new territory via an exclude-prior-centers mechanism had a
    real bug -- excluded radius exceeded the mesh radius, making genuine edge-walking
    structurally impossible -- reverted in favor of this simpler, legacy-faithful
    approach)."""
    _require_full_gt_hp(hp, strategy_name, "run_phase2_island_ground_truth")
    done, expected = _phase1_coarse_gt_status(ticker, strategy_name, config_version, hp, entry_timing, fixed_sl)
    if done < expected:
        raise RuntimeError(
            f"[{ticker}] Phase2-GT blocked: Phase1-Coarse-GT is incomplete for this campaign "
            f"scope ({done:,}/{expected:,} cells). Reading island centers from a still-growing "
            f"Phase1 grid would silently mesh around provisional/wrong centers -- wait for "
            f"Phase1-Coarse-GT to actually finish before calling this."
        )

    tasks = _phase2_island_gt_tasks(ticker, strategy_name, config_version, hp, entry_timing, fixed_sl)
    if not tasks:
        logger.warning(f"[{ticker}] Phase2-GT: no island tasks generated.")
        return

    logger.info(f"[{ticker}] Phase2-GT island mesh: {len(tasks)} tasks ({N_ISLANDS} islands ±{FINE_RADIUS})")
    dispatch_parallel_grid_ground_truth(shared_pool, list(tasks), ticker, strategy_name, config_version,
                                        "Phase2-Island-GT", spy_bh, asset_bh, run_timestamp, fixed_sl,
                                        entry_timing, same_bar_reentry=same_bar_reentry, generation=generation,
                                        run_id=run_id, start_date=start_date, end_date=end_date,
                                        data_source=data_source)


# ── Phase 2.5: targeted cliff-box sweep around true best node ────────────────

def run_phase25_cliff_box(shared_pool, ticker, strategy_name, config_version, hp, spy_bh, asset_bh, run_timestamp, fixed_sl=0, entry_timing='close', run_id=None, start_date=None, end_date=None, min_hold_hours=0):
    """Sweep ±CLIFF_RADIUS in TP/SL and ±7h in hold around the true best node from Phase 2.
    Guarantees cliff check has complete neighborhood data regardless of where the peak landed.
    For TrailingBothZScoreBreakout, also sweeps the trail_pct axis's immediate neighbors
    (±1 in the configured trail_pcts list) so Checkpoint 2's cliff check has data there too."""
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)
    trail_pcts = _trail_pcts_for_strategy(strategy_name, hp)
    scope_sql, scope_params = _campaign_scope_sql(strategy_name, fixed_sl, entry_timing)
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(f"""
            SELECT axis_tp, {_sl_axis_real_column(sl_axis_col)} AS stop_loss, max_hold_hours, window, z_score_threshold,
                   {'trail_sell_pct' if fourth_axis_col == 'trail_pct' else '0'} AS tpct
            FROM backtest_cache
            WHERE version=? AND ticker=? AND strategy=? AND trades > 0
              AND (kernel_version IS NULL OR kernel_version<>'ground_truth_v6') {scope_sql}
            ORDER BY {ROBUST_ALPHA_SQL} DESC LIMIT 1
        """, (config_version, ticker, strategy_name, *scope_params)).fetchone()
    if not row:
        return
    tp_c, sl_c, hold_c, w_c, z_c, tpct_c = int(row[0]), int(row[1]), int(row[2]), int(row[3]), float(row[4]), float(row[5])

    if fourth_axis_col == 'trail_pct' and tpct_c in trail_pcts:
        idx = trail_pcts.index(tpct_c)
        tpct_neighbors = trail_pcts[max(0, idx - 1): idx + 2]
    else:
        tpct_neighbors = [tpct_c]

    tasks = set()
    for tp in range(max(1, tp_c - CLIFF_RADIUS), min(30, tp_c + CLIFF_RADIUS) + 1):
        for sl in range(max(1, sl_c - CLIFF_RADIUS), min(30, sl_c + CLIFF_RADIUS) + 1):
            for hold in [h for h in hp['hold_time_caps'] if abs(h - hold_c) <= 7]:
                for tpct in tpct_neighbors:
                    tasks.add((tp, sl, hold, w_c, z_c, float(tpct)))

    logger.info(f"[{ticker}] Phase2.5 cliff-box: {len(tasks)} tasks around TP={tp_c} SL={sl_c} hold={hold_c}h")
    dispatch_parallel_grid(shared_pool, list(tasks), ticker, strategy_name, config_version,
                           "Phase2.5-CliffBox", spy_bh, asset_bh, run_timestamp, fixed_sl, entry_timing, run_id=run_id,
                           start_date=start_date, end_date=end_date, min_hold_hours=min_hold_hours)


def run_phase25_cliff_box_ground_truth(shared_pool, ticker, strategy_name, config_version, hp, spy_bh,
                                        asset_bh, run_timestamp, fixed_sl=0, entry_timing='open_check',
                                        same_bar_reentry=True, run_id=None, start_date=None, end_date=None,
                                        data_source="yahoo"):
    """v6 counterpart to run_phase25_cliff_box — same per-candidate box shape
    (±CLIFF_RADIUS in TP/SL, ±7h in hold, ±1 trail_pct neighbor), but unlike the legacy
    function (deliberately untouched, see isolate-new-code-from-settled-paths convention),
    this GT version does NOT collapse to a single global-best cell before cliff-checking.

    Real-candidate-selection rationale (2026-08-22 design decision): a node that's #1 by
    raw robust-alpha/CAGR across the whole campaign scope might not be the best real
    candidate once factors this pipeline doesn't sweep (overlay effects like drought/
    add-on) are considered later. Collapsing to one winner before cliff-checking throws
    away that information. Instead: re-derive up to N_ISLANDS distinct (tp, sl) island
    centers across the FULL scope (all z/window/trail_pct combos flattened together, via
    the same pick_island_centers min-separation logic Phase2 uses -- see module docstring
    on pick_island_centers), then cliff-box each island's own top-3 robust-alpha-ranked
    cells within that island's own ±FINE_RADIUS region (not top-3 overall across islands).
    Up to N_ISLANDS * 3 cliff-boxes total, merged into one deduped task set and dispatched
    in a single call (same mechanism, phase_label unchanged, so downstream consumers
    filtering on "Phase2.5-CliffBox-GT" are unaffected).

    Center detection is unrestricted (2026-08-23 redesign) -- sees the full current
    scope, not just Phase1's coarse grid, matching legacy's run_phase25_cliff_box
    exactly (no restriction, ever). Without this, a genuinely-discovered fine-mesh or
    multi-generation island would be invisible to Phase2.5/candidate selection (found
    2026-08-23: this was the actual behavior before this fix, silently making the whole
    generation loop's work unreachable).

    HARD-BLOCKS on Phase1-Coarse-GT's completeness only (same _require_full_gt_hp/
    _phase1_coarse_gt_status check as run_phase2_island_ground_truth) -- Phase1 has a
    real, external race to guard against (Massive data can still be mid-build) and a
    fixed, formula-computable expected size, so this gate is sound. A second gate on
    Phase2-Island-GT's own completeness (_phase2_island_gt_status) existed here from
    2026-08-22 to 2026-08-23 and was REMOVED (not just re-tuned) after being measured
    against real production data: it re-derives centers from whatever's in the cache
    RIGHT NOW, but a genuinely-progressing multi-generation walk's own last-generation
    writes always shift what a fresh re-derivation would want next -- the check can only
    pass in the degenerate case where the walk made zero progress. Applied as a hard
    gate, it blocked 5 of 6 real production scopes tested, including scopes with
    already-working Phase2.5-CliffBox-GT data from before this redesign. No v5 analog
    ever existed for this gate; v5's run_phase25_cliff_box has never had ANY completeness
    check between Phase2 and Phase2.5."""
    _require_full_gt_hp(hp, strategy_name, "run_phase25_cliff_box_ground_truth")
    p1_done, p1_expected = _phase1_coarse_gt_status(ticker, strategy_name, config_version, hp, entry_timing, fixed_sl)
    if p1_done < p1_expected:
        raise RuntimeError(
            f"[{ticker}] Phase2.5-GT blocked: Phase1-Coarse-GT is incomplete for this campaign "
            f"scope ({p1_done:,}/{p1_expected:,} cells) -- Phase2.5 refines around Phase2's best "
            f"point, which itself depends on Phase1 having actually finished. Wait for "
            f"Phase1-Coarse-GT to finish before calling this."
        )
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)
    trail_pcts = _trail_pcts_for_strategy(strategy_name, hp)
    scope_sql, scope_params = _campaign_scope_sql(strategy_name, fixed_sl, entry_timing)
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql(f"""
            SELECT axis_tp AS take_profit, {_sl_axis_real_column(sl_axis_col)} AS stop_loss,
                   max_hold_hours, window, z_score_threshold,
                   {'trail_sell_pct' if fourth_axis_col == 'trail_pct' else '0'} AS tpct,
                   {ROBUST_ALPHA_SQL} AS robust_alpha, cagr, trades
            FROM backtest_cache
            WHERE version=? AND ticker=? AND strategy=? AND trades > 0
              AND kernel_version='ground_truth_v6' {scope_sql}
        """, conn, params=(config_version, ticker, strategy_name, *scope_params))
    if df.empty:
        logger.warning(f"[{ticker}] Phase2.5-GT: no ground_truth_v6 rows in scope -- nothing to cliff-box.")
        return

    # Center detection matches v5's actual behavior (2026-08-23 redesign): unrestricted
    # over the FULL current scope, not just Phase1's original coarse-grid values. Safe
    # here specifically because run_phase25_cliff_box_ground_truth only ever runs AFTER
    # Phase2's entire generation loop has finished, and dispatch_parallel_grid_ground_
    # truth is fully synchronous (blocks on its whole task batch, no early return) -- so
    # whatever centers the last generation picked are guaranteed fully ATTEMPTED by the
    # time this runs (a per-cell failure still just logs and skips, same as any dispatch
    # call, not a new risk this introduces), not just "static" in the sense of no
    # concurrent writer. The
    # rerun-instability concern that motivated the earlier Phase1-grid-only restriction
    # only applied to re-deriving centers WHILE Phase2 is still actively writing, which
    # can't happen here either way. This lets Phase2.5 actually see
    # whatever the generation loop's own walk discovered -- v5's own Phase2.5
    # (run_phase25_cliff_box) queries its single global-best row with no
    # tp/sl restriction at all, so this restores real v5 parity rather than the
    # GT-only restriction that made the generation loop's discoveries unreachable.
    # rank_col='cagr' (2026-08-23, ground_truth_kernel_rebuild.md Step 4): CAGR is the
    # sole GT selection metric now -- see pick_island_centers' own docstring.
    centers = pick_island_centers(df, rank_col='cagr')

    tasks = set()
    for tp_c, sl_c in centers:
        region = df[(df['take_profit'] - tp_c).abs().le(FINE_RADIUS) &
                    (df['stop_loss'] - sl_c).abs().le(FINE_RADIUS)]
        if region.empty:
            continue
        # GT_CANDIDATE_TIEBREAK (module-level, see its own comment) resolves ties on
        # cagr deterministically. Note: pandas ignores `kind` for a multi-column
        # sort_values (it always uses a stable lexsort internally regardless), so no
        # `kind=` argument is needed here for stability -- residual ties past the full
        # tiebreak column set are already impossible in practice since that column set
        # fully identifies a cache cell within a scope.
        # Ranked on cagr, not robust_alpha (2026-08-23, ground_truth_kernel_rebuild.md
        # Step 4 -- CAGR is the sole GT selection metric now; robust_alpha stays in the
        # dataframe/output as a diagnostic column only, never the sort key).
        _tb_cols = ['cagr'] + [c for c, _ in GT_CANDIDATE_TIEBREAK]
        _tb_asc = [False] + [asc for _, asc in GT_CANDIDATE_TIEBREAK]
        region = region.sort_values(_tb_cols, ascending=_tb_asc)

        top_cagr = region.iloc[0]['cagr']
        if pd.isna(top_cagr):
            # NULL cagr means "not yet computed" (pre-column row, or a cache-hit that
            # hasn't been backfilled -- see the schema-migration comment near
            # PHASE25_ISLAND_CAGR_MIN's definition), NOT "fails the quality bar". Safe
            # today only because the standard Phase1->2->2.5 call path always backfills
            # cagr for the whole scope before Phase2.5 runs -- flagged loud in case a
            # future standalone/resume-from-phase call skips that backfill.
            logger.error(f"[{ticker}] Phase2.5-GT: island(TP={tp_c} SL={sl_c})'s top cell has "
                         f"NULL cagr (unknown, not sub-{PHASE25_ISLAND_CLIFFBOX_CAGR_MIN}) -- "
                         f"skipping this island rather than risk cliff-boxing an unranked cell. "
                         f"If this fires outside the standard Phase1->2->2.5 chain, run Phase1 "
                         f"for this scope first to backfill cagr.")
            continue
        if top_cagr <= PHASE25_ISLAND_CLIFFBOX_CAGR_MIN:
            logger.info(f"[{ticker}] Phase2.5-GT: skipping island(TP={tp_c} SL={sl_c}) -- "
                        f"top cell cagr={top_cagr:.2f} is negative, not worth cliff-boxing.")
            continue
        # No quality gate beyond the negative-CAGR skip above (2026-08-23 redesign): v5's
        # own Phase2.5 (run_phase25_cliff_box) cliff-boxes its single best row
        # unconditionally. PHASE25_ISLAND_CAGR_MIN still applies, but only downstream at
        # derive_phase25_candidates_ground_truth, where it decides which candidates are
        # eligible for Phase4 (drought/add-on overlay), not whether an island gets
        # refined at all.

        for _, cand in region.head(3).iterrows():
            tp_c2, sl_c2, hold_c, w_c, z_c = (int(cand['take_profit']), int(cand['stop_loss']),
                                              int(cand['max_hold_hours']), int(cand['window']),
                                              float(cand['z_score_threshold']))
            tpct_c = float(cand['tpct'])
            if fourth_axis_col == 'trail_pct' and tpct_c in trail_pcts:
                idx = trail_pcts.index(tpct_c)
                tpct_neighbors = trail_pcts[max(0, idx - 1): idx + 2]
            else:
                tpct_neighbors = [tpct_c]

            logger.info(f"[{ticker}] Phase2.5-GT cliff-box candidate: island(TP={tp_c} SL={sl_c}) "
                        f"cell TP={tp_c2} SL={sl_c2} hold={hold_c}h w={w_c} z={z_c} tpct={tpct_c} "
                        f"robust_alpha={cand['robust_alpha']:.4f} cagr={cand['cagr']}")

            for tp in range(max(1, tp_c2 - CLIFF_RADIUS), min(30, tp_c2 + CLIFF_RADIUS) + 1):
                for sl in range(max(1, sl_c2 - CLIFF_RADIUS), min(30, sl_c2 + CLIFF_RADIUS) + 1):
                    for hold in [h for h in hp['hold_time_caps'] if abs(h - hold_c) <= 7]:
                        for tpct in tpct_neighbors:
                            tasks.add((tp, sl, hold, w_c, z_c, float(tpct)))

    if not tasks:
        logger.warning(f"[{ticker}] Phase2.5-GT: no cliff-box tasks generated from {len(centers)} island center(s).")
        return

    logger.info(f"[{ticker}] Phase2.5-GT cliff-box: {len(tasks)} tasks across {len(centers)} island(s)")
    dispatch_parallel_grid_ground_truth(shared_pool, list(tasks), ticker, strategy_name, config_version,
                                        "Phase2.5-CliffBox-GT", spy_bh, asset_bh, run_timestamp, fixed_sl,
                                        entry_timing, same_bar_reentry=same_bar_reentry, run_id=run_id,
                                        start_date=start_date, end_date=end_date, data_source=data_source)


# ── Add-on overlay evaluation for Phase2.5-GT candidates (2026-08-22) ────────
# Purely additive / read-only against already-populated backtest_cache core data --
# does NOT dispatch anything through dispatch_parallel_grid_ground_truth (no
# ProcessPoolExecutor sweep, no backtest_cache writes), and does NOT change how
# core-only candidate selection works. Every cell evaluated here is computed
# in-process via a direct run_backtest_ground_truth call (reusing
# _load_node_inputs_ground_truth's per-(ticker,strategy,window) prep/mprep memo), then
# passed through backtester.apply_addon_overlay_ground_truth for the blended number.
# same_bar_reentry=True (2026-08-23 fix -- this comment used to read "always False here,
# user's explicit compute-halving direction," left unreconciled against
# dispatch_parallel_grid_ground_truth's own CAUTION comment for a full day; see
# run_addon_cliff_safety_ground_truth's docstring for the full reasoning and why leaving
# a stale comment like this one unfixed is exactly the failure shape being fixed).
# Core candidate selection itself is unaffected either way -- this section only covers
# the add-on-safety cliff-box pass.

def derive_phase25_candidates_ground_truth(ticker, strategy_name, config_version, hp,
                                            fixed_sl=0, entry_timing='open_check'):
    """Read-only re-derivation of run_phase25_cliff_box_ground_truth's own up-to-
    (N_ISLANDS * 3) candidate list, against whatever Phase1/Phase2-GT rows already exist
    in backtest_cache -- does NOT dispatch anything (no Phase2.5-CliffBox-GT rows are
    required or produced). Same Phase1-completeness guard, same center/top-3-per-island
    selection logic as that function (no Phase2-completeness guard -- removed 2026-08-23,
    see that function's own docstring for why) -- but the CAGR gates deliberately differ now
    (2026-08-23 redesign), not kept in lockstep: run_phase25_cliff_box_ground_truth uses
    the narrower PHASE25_ISLAND_CLIFFBOX_CAGR_MIN (skips cliff-boxing only a negative-
    coarse-cell island, real compute savings with low near-miss risk); this function
    uses PHASE25_ISLAND_CAGR_MIN to set each returned candidate's `phase4_eligible` flag
    (whether it's worth the expensive drought/add-on overlay), never to drop a candidate
    outright -- every island this function finds a center for returns real candidates.

    Returns a list of dicts: {island_tp, island_sl, take_profit, stop_loss,
    max_hold_hours, window, z_score_threshold, tpct, robust_alpha, cagr}."""
    _require_full_gt_hp(hp, strategy_name, "derive_phase25_candidates_ground_truth")
    p1_done, p1_expected = _phase1_coarse_gt_status(ticker, strategy_name, config_version, hp, entry_timing, fixed_sl)
    if p1_done < p1_expected:
        raise RuntimeError(f"[{ticker}] derive_phase25_candidates_ground_truth blocked: "
                            f"Phase1-Coarse-GT incomplete ({p1_done:,}/{p1_expected:,}).")
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)
    trail_pcts = _trail_pcts_for_strategy(strategy_name, hp)
    scope_sql, scope_params = _campaign_scope_sql(strategy_name, fixed_sl, entry_timing)
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql(f"""
            SELECT axis_tp AS take_profit, {_sl_axis_real_column(sl_axis_col)} AS stop_loss,
                   max_hold_hours, window, z_score_threshold,
                   {'trail_sell_pct' if fourth_axis_col == 'trail_pct' else '0'} AS tpct,
                   {ROBUST_ALPHA_SQL} AS robust_alpha, cagr, trades
            FROM backtest_cache
            WHERE version=? AND ticker=? AND strategy=? AND trades > 0
              AND kernel_version='ground_truth_v6' {scope_sql}
        """, conn, params=(config_version, ticker, strategy_name, *scope_params))
    if df.empty:
        return []

    # Center detection unrestricted (2026-08-23 redesign, matches run_phase25_cliff_box_
    # ground_truth's own fix) -- sees whatever any Phase2 generation actually wrote,
    # not just Phase1's original coarse grid.
    # rank_col='cagr' (2026-08-23, ground_truth_kernel_rebuild.md Step 4): CAGR is the
    # sole GT selection metric now -- see pick_island_centers' own docstring.
    centers = pick_island_centers(df, rank_col='cagr')
    candidates = []
    for tp_c, sl_c in centers:
        region = df[(df['take_profit'] - tp_c).abs().le(FINE_RADIUS) &
                    (df['stop_loss'] - sl_c).abs().le(FINE_RADIUS)]
        if region.empty:
            continue
        # Drop NULL-cagr rows before ranking (2026-08-23, ground_truth_kernel_rebuild.md
        # Step 4, paired-review HIGH finding): a NULL cagr means "not yet computed", not
        # "worst possible" -- a rankable-by-alpha row that happens to have NULL cagr must
        # NOT silently enter the cagr sort/head(3)/winner_index selection (robust_alpha
        # was never NULL, so this NaN path is new with the cagr switch). Matches
        # prune_backtest_cache_ground_truth_validate.py's independent Method B, which
        # already does this same explicit skip.
        region = region[region['cagr'].notna()]
        if region.empty:
            continue
        # GT_CANDIDATE_TIEBREAK (module-level, see its own comment) resolves ties on
        # cagr deterministically. Note: pandas ignores `kind` for a multi-column
        # sort_values (it always uses a stable lexsort internally regardless), so no
        # `kind=` argument is needed here for stability -- residual ties past the full
        # tiebreak column set are already impossible in practice since that column set
        # fully identifies a cache cell within a scope.
        # Ranked on cagr, not robust_alpha (2026-08-23, ground_truth_kernel_rebuild.md
        # Step 4 -- CAGR is the sole GT selection metric now; robust_alpha stays in the
        # dataframe/output as a diagnostic column only, never the sort key).
        _tb_cols = ['cagr'] + [c for c, _ in GT_CANDIDATE_TIEBREAK]
        _tb_asc = [False] + [asc for _, asc in GT_CANDIDATE_TIEBREAK]
        region = region.sort_values(_tb_cols, ascending=_tb_asc)
        top_cagr = region.iloc[0]['cagr']
        # PHASE25_ISLAND_CAGR_MIN (2026-08-23 redesign): no longer drops the island's
        # candidates outright -- they're still real candidates the report should show.
        # It only decides Phase4 (drought/add-on overlay) eligibility, since overlay
        # compute isn't worth spending on a candidate whose own core CAGR is already
        # sub-threshold (e.g. negative) -- that decision belongs at this reporting/
        # selection layer, not inside Phase2.5's execution (which cliff-boxes every
        # island unconditionally now, matching v5).
        phase4_eligible = not pd.isna(top_cagr) and top_cagr > PHASE25_ISLAND_CAGR_MIN
        for _, cand in region.head(3).iterrows():
            candidates.append({
                'island_tp': tp_c, 'island_sl': sl_c,
                'take_profit': int(cand['take_profit']), 'stop_loss': int(cand['stop_loss']),
                'max_hold_hours': int(cand['max_hold_hours']), 'window': int(cand['window']),
                'z_score_threshold': float(cand['z_score_threshold']), 'tpct': float(cand['tpct']),
                'robust_alpha': float(cand['robust_alpha']), 'cagr': float(cand['cagr']),
                'phase4_eligible': phase4_eligible,
            })
    return candidates


def addon_margin_eligible_ticker(ticker):
    """Real add-on-leg DEPLOYABILITY check for a ticker, 2026-08-23 (GT Phase 4) --
    checks the ACTUAL live enforcement gate (schwab_safety.py's approve_and_record,
    is_addon_leg branch, ~line 1551: `if not limits.margin_capable: raise
    SafetyViolation(...)`), NOT a literal "brokerage account" check. Corrected
    mid-build: the original dispatch for this task claimed gt_addon_winner_drought_
    eval.py already reasoned about a brokerage-only constraint -- verified false, that
    file has zero brokerage/margin/account references. There was no existing gate to
    carry forward; this is new.

    Also corrected against CLAUDE.md's own "Account type: brokerage=margin; sep/roth/ira=
    cash" framing in its Live Trading section -- that line is stale (predates the
    2026-08-11 roth cash->margin correction documented in signals_db.py's `accounts`
    table seed comment). The real `accounts` table has margin_capable=True for
    brokerage/roth/ira/soxl_ira and False only for sep -- confirmed directly against
    signals_db.py's ensure_tables() seed rows and schwab_safety.py's own real BUY-time
    check before writing this, not assumed from the doc.

    IMPORTANT (corrected same day per direct user instruction, after both paired-review
    passes converged on flagging the original version's behavior): this function is
    ANNOTATION-ONLY -- it does NOT gate whether the add-on backtest number gets
    computed anywhere in this file. The add-on CAGR is a real INPUT to a future
    account-assignment decision (strong add-on upside argues for promoting a new
    candidate into a margin-capable account, not just describing an account it's
    already in) -- gating the computation on today's account would hide exactly the
    number that decision needs for every not-yet-promoted candidate. Every
    _evaluate_cell_ground_truth_with_addon call always computes addon_alpha/addon_cagr
    regardless of this function's result.

    Looks up the ticker's real watch_list account(s) (any watchlist, any mode/state --
    excludes only `paper_role` clone rows, e.g. the 'daily_sync' daily-track clones per
    signals_db.get_watch_list_node's own established convention, matching that
    function's own broader-than-the-single-named-case exclusion -- and archived rows)
    and cross-references schwab_safety.ACCOUNTS[account].margin_capable for each.
    Reports NOT eligible (with a reason string distinguishing "no watch_list node yet"
    from "known non-margin account(s)" from "ambiguous across accounts") if no node is
    found for the ticker, the DB lookup fails, or the ticker's own nodes disagree across
    accounts -- "not eligible today" is explicitly NOT the same claim as "this number
    isn't real" (see the IMPORTANT note above); a not-yet-promoted candidate with a
    strong add-on CAGR is exactly the case this reason string exists to label, not hide.

    Returns (eligible: bool, reason: str)."""
    import signals_db
    import schwab_safety
    # sqlite3.Connection's own context manager only commits/rolls back on exit, it does
    # NOT close (stdlib gotcha) -- explicit close in finally instead (connection-leak
    # finding from paired review). NOT nested inside a `with conn:` block -- that
    # would call conn.commit() on an already-closed connection in its own __exit__,
    # raising "Cannot operate on a closed database" (caught while fixing this).
    c = None
    try:
        c = signals_db._conn()
        rows = c.execute(
            "SELECT DISTINCT account FROM watch_list WHERE ticker = ? "
            "AND account IS NOT NULL AND (paper_role IS NULL) "
            "AND (archived_at IS NULL)", (ticker,)).fetchall()
    except Exception as e:
        return False, f"watch_list lookup failed: {e}"
    finally:
        if c is not None:
            c.close()
    accounts = sorted({r['account'] for r in rows})
    if not accounts:
        return False, f"no watch_list node found for {ticker!r} yet -- not a real ineligibility, just not yet promoted/assigned an account"
    capable = [bool(schwab_safety.ACCOUNTS.get(a) and schwab_safety.ACCOUNTS[a].margin_capable)
               for a in accounts]
    if all(capable):
        return True, f"account(s) {accounts} all margin_capable"
    if not any(capable):
        return False, f"account(s) {accounts} not margin_capable"
    return False, f"ambiguous margin capability across accounts {accounts}"


def _evaluate_cell_ground_truth_with_addon(ticker, strategy_name, tp, sl, hold_hours, w, z_thresh,
                                            fixed_sl, tpct, entry_timing, start_date, end_date,
                                            spy_bh, years, data_source="yahoo", addon_eligible=True,
                                            config_version=None, addon_cache_map=None,
                                            new_cache_rows=None):
    """One (tp, sl, hold, w, z, tpct) cell, evaluated in-process (no ProcessPoolExecutor,
    no backtest_cache write) with same_bar_reentry=True -- returns core AND add-on-
    adjusted alpha/CAGR side by side. Reuses run_single_backtest_node_ground_truth_
    isolated's exact axis-mapping convention (trail_buy_pct/trail_sell_pct/arm_pct from
    tp/sl/tpct) so a cell computed here matches what the real sweep would have computed
    for the same coordinates.

    same_bar_reentry=True (2026-08-23, fixes a same-week regression -- see
    run_addon_cliff_safety_ground_truth's own docstring for the full reasoning): this
    function used to hardcode False as an explicit "compute-halving" choice for add-on
    safety specifically. That directly contradicted dispatch_parallel_grid_ground_truth's
    own CAUTION comment ("every caller today passes True") one day after that comment was
    written, with no reconciliation on file -- confirmed via `git log -S` as a same-week
    unreconciled reversal, not a deliberate considered exception. Fixed here: every
    real GT campaign call uses True, and a candidate's cliff-box neighborhood must be
    evaluated under the SAME same_bar_reentry setting that produced the island it was
    selected from, or the neighbor check is scoring a different landscape shape than the
    one that actually produced the candidate.

    addon_cache_map/new_cache_rows (2026-08-23, same fix): optional cliff_addon_cache
    read/write plumbing -- see run_addon_cliff_safety_ground_truth's docstring and the
    cliff_addon_cache table comment above _ensure_cliff_addon_cache_table for the design.
    When config_version and addon_cache_map are both given and this exact coordinate is
    already in the map, the cached result is returned immediately with ZERO simulation.
    On a cache miss, if new_cache_rows is given, the freshly computed result is appended
    to it (as (coord_key, result)) for the caller to persist after the whole pass.

    addon_eligible (2026-08-23, GT Phase 4; corrected same day -- see below): purely an
    ANNOTATION carried through to the return dict, never a gate on whether the add-on
    blended computation runs. The add-on backtest number (what CAGR it WOULD produce)
    has no dependency on which account a ticker currently lives in -- it's pure
    simulation off the same core `trades` list, no watch_list/account lookup needed for
    the number itself. Real reason this field exists: the add-on CAGR is a real INPUT
    to a future account-assignment decision (a strong add-on number is an argument for
    promoting a new candidate into a margin-capable account rather than ira/roth/sep,
    not just a property of an account already assigned) -- gating the computation on
    today's account would hide exactly the number a promotion decision needs for every
    not-yet-promoted candidate. (Earlier same-day version of this function DID gate on
    addon_eligible and skipped the computation entirely for a non-eligible/unknown
    account -- corrected per direct user instruction before this shipped.)"""
    cache_key = None
    if config_version is not None and addon_cache_map is not None:
        cache_key = (int(tp), round(float(sl), 4), int(hold_hours), int(w),
                     round(float(z_thresh), 4), round(float(tpct), 4))
        cached = addon_cache_map.get(cache_key)
        if cached is not None:
            # addon_eligible is a pure account-status ANNOTATION (paired-review
            # independent-cold HIGH finding, 2026-08-23) -- it has zero effect on the
            # cached core/addon numbers, and the ticker's real watch_list account can
            # change between when a row was cached and when it's read back. Always
            # stamped with the CURRENT call's value on a cache hit (never persisted/
            # read from cliff_addon_cache itself -- see _load_cliff_addon_cache_map's
            # docstring) so a stale cached account status can never leak into a report.
            return {**cached, 'addon_eligible': addon_eligible}

    strategy_class = getattr(strategies, strategy_name)
    is_both = strategy_name == 'TrailingBothZScoreBreakout'
    inputs = _load_node_inputs_ground_truth(ticker, strategy_class, strategy_name, w, z_thresh,
                                             start_date, end_date, data_source=data_source)
    if inputs is None:
        return None
    _, df_daily_processed, minute_df, df_hourly_windowed, prep, mprep = inputs
    if df_hourly_windowed.empty:
        return None
    if is_both:
        trail_buy_pct_arg, trail_sell_pct_arg, arm_pct_arg = float(sl), float(tpct), float(tp)
    else:
        trail_buy_pct_arg, trail_sell_pct_arg, arm_pct_arg = 0.0, float(sl), float(tp)

    trades = run_backtest_ground_truth(
        df_hourly_windowed, df_daily_processed, ticker, minute_df,
        fixed_sl=fixed_sl, arm_pct=arm_pct_arg, trail_buy_pct=trail_buy_pct_arg,
        trail_sell_pct=trail_sell_pct_arg, max_hours_to_hold=hold_hours,
        z_score_threshold=z_thresh, is_both=is_both,
        open_check_entry_timing=(entry_timing == 'open_check'),
        same_bar_reentry=True, prep=prep, mprep=mprep, need_times=False,
    )
    if not trades:
        return None
    core_alpha, n_trades, _, core_ret, _, core_cagr = _summarize_trades_ground_truth(trades, spy_bh, years)
    n_armed = sum(1 for t in trades if t.get('armed'))
    # Always computed regardless of addon_eligible (see docstring correction above) --
    # the number itself has no account dependency.
    addon_trades = apply_addon_overlay_ground_truth(trades)
    # Paired-review CRITICAL finding (2026-08-22, both Sonnet- and Opus-independent
    # passes): a blended add-on Return can go below -100% (no independent stop on the
    # add-on leg, unlike core), which flips the sign of the compounded product and can
    # silently corrupt addon_alpha/addon_cagr into a garbage number (nan/complex) with
    # no exception. Detected here via apply_addon_overlay_ground_truth's own
    # 'return_below_floor' flag -- when ANY trade in the cell breaches it, this cell's
    # addon stats are reported as None/flagged rather than aggregated, so a poisoned
    # cell can never silently look like a real number to a caller.
    floor_breached = any(t.get('return_below_floor') for t in addon_trades)
    if floor_breached:
        addon_alpha, addon_ret, addon_cagr = None, None, None
    else:
        addon_alpha, _, _, addon_ret, _, addon_cagr = _summarize_trades_ground_truth(addon_trades, spy_bh, years)
    result = {
        'coords': (tp, sl, hold_hours, w, z_thresh, tpct),
        'n_trades': n_trades, 'n_armed': n_armed,
        'core_alpha': core_alpha, 'core_return': core_ret, 'core_cagr': core_cagr,
        'addon_alpha': addon_alpha, 'addon_return': addon_ret, 'addon_cagr': addon_cagr,
        'addon_return_floor_breached': floor_breached,
        'addon_eligible': addon_eligible,
    }
    if cache_key is not None and new_cache_rows is not None:
        new_cache_rows.append((cache_key, result))
    return result


def run_addon_cliff_safety_ground_truth(ticker, strategy_name, config_version, hp, candidates,
                                         spy_bh, fixed_sl=0, entry_timing='open_check',
                                         start_date=None, end_date=None, years=None,
                                         data_source="yahoo", cliff_radius=None,
                                         addon_eligible=True):
    """For each candidate from derive_phase25_candidates_ground_truth, computes the
    add-on-adjusted alpha/CAGR at the candidate's own cell AND across the same cliff-box
    neighborhood shape Phase2.5-GT's own dispatch would generate (±cliff_radius in
    TP/SL, ±7h hold neighbors from hp['hold_time_caps'], adjacent trail_pct for
    TrailingBoth) -- then applies the SAME cliff-safety verdict convention this codebase
    already uses (identify_full_mesh_candidates: cliff iff worst-neighbor robust-alpha-
    equivalent < 0), computed on the add-on-adjusted alpha instead of the core one.

    cliff_radius defaults to CLIFF_RADIUS (the same radius core's own Phase2.5-GT cliff-
    box uses) -- overridable to a smaller radius for a cheaper smoke-test pass; this
    function does zero backtest_cache writes and never touches
    dispatch_parallel_grid_ground_truth, so there is no risk of colliding with a real
    sweep's data regardless of radius chosen.

    Real compute: each cell here is one direct run_backtest_ground_truth call
    (~seconds warm, per docs/plans/ground_truth_kernel_rebuild.md's own benchmark note)
    -- a full 9-candidate x full cliff-box pass is real, possibly slow work, same
    caution as any other GT campaign call in this file. See cliff_addon_cache (2026-08-23
    fix, below) for the persistence layer that avoids paying this cost again on a repeat
    report run for the same ticker/scope.

    same_bar_reentry=True (2026-08-23, fixes a same-week regression): this pass used to
    hardcode same_bar_reentry=False for every cell, an explicit "compute-halving"
    decision made 2026-08-22. That directly contradicted dispatch_parallel_grid_ground_
    truth's own CAUTION comment ("Do not vary same_bar_reentry within a single version
    string; every caller today passes True"), written ONE DAY EARLIER (2026-08-21) with
    no reconciliation between the two on file -- confirmed via `git log -S
    same_bar_reentry` as a same-week unreconciled reversal, not a deliberate considered
    exception (user's own assessment, 2026-08-23). Fixed here for two reasons, not just
    consistency: (1) every real GT campaign call (Phase1/Phase2/Phase2.5-GT,
    dispatch_parallel_grid_ground_truth) uses True, so a candidate's own cached core row
    was always computed under True; (2) structurally, a candidate's "island" (the local
    good-performing region Phase2.5 selected it from) was found under True -- checking
    its neighbors under False evaluates a DIFFERENT landscape shape than the one that
    actually produced the candidate, not just a cheaper approximation of the same one.
    `core_alpha`/`core_cliff` in each result now DO match the candidate's own cached
    `candidate['robust_alpha']` (mod float determinism) -- the "NOT guaranteed to match"
    caveat this docstring used to carry for that comparison no longer applies.

    Persistence / cliff_addon_cache (2026-08-23, same fix): the flip to True means every
    neighbor coordinate is now evaluated under the exact setting Phase1/Phase2/Phase2.5-GT
    already swept -- so many neighbor coordinates for a Phase2.5-derived candidate were
    already computed by the real campaign that produced it. A backtest_cache-read
    shortcut for those coordinates' CORE numbers was considered and rejected (see the
    cliff_addon_cache table comment above _ensure_cliff_addon_cache_table): addon_alpha/
    addon_cagr need the per-trade `trades` list, which backtest_cache never stores, so a
    backtest_cache hit could never skip the run_backtest_ground_truth call this function
    needs anyway for the addon overlay -- it would buy zero simulation-avoidance. The
    real, verifiable compute-avoidance lever is cliff_addon_cache, this function's own
    small table: on the FIRST report run for a ticker/scope, every cell is still
    simulated (no shortcut exists for a coordinate whose addon result has never been
    computed before) and persisted; on a REPEAT run for the same
    (ticker, strategy, version, entry_timing, fixed_sl, kernel_version) scope, every cell
    is a cache hit and the pass runs with zero simulation. Loaded once per call (one bulk
    SELECT covering every candidate's own cell and full neighborhood, not one query per
    cell) and flushed once at the end (one executemany), not per-cell, to keep the
    caching overhead itself cheap relative to what it's saving.

    KNOWN LIMITATIONS (paired-review findings, 2026-08-22, not fixed -- read before
    treating `addon_cliff`/`addon_cagr` as a real-money go/no-go signal on their own):
    (1) `addon_cliff` reuses the SAME absolute worst-neighbor<0 bar the core (unlevered)
    metric uses, applied to a series that carries ~2x exposure through the armed window
    -- a node whose armed trades are net positive will structurally look "safer" with
    add-on than without, by construction, not because the extra margin exposure is
    actually safer. (2) `addon_cagr` compounds the add-on leg's borrowed capital as if
    financing were free (no margin interest, no maintenance requirement, no ceiling as
    the account grows) -- an upper bound on real add-on CAGR, not a realizable one.
    (3) The simulation assumes the add-on leg always exits at the exact same time/price
    as core; the REAL leg carries its own independent stop (signals_notify.py's
    _place_stop_loss_for_addon_leg, anchored to the PARENT's entry price, not the arm
    price) that can fire first, and can also fail to open at all (non-margin account,
    ticker/node automation gates, a SafetyViolation, or an entry-timeout ABANDONED leg)
    -- this pass assumes a 100% fill rate. (4) [RESOLVED 2026-08-23, see above --
    same_bar_reentry is now True, matching every real campaign dispatch; kept as (4) so
    the numbering doesn't shift under any external reference to (1)-(3).] None of (1)-(3)
    make the add-on-vs-core COMPARISON invalid (both sides of that comparison share the
    same same_bar_reentry/fill-rate/financing assumptions) -- they matter if this output
    is used for an absolute, not relative, verdict.

    CAGR-based worst-neighbor (2026-08-23, ground_truth_kernel_rebuild.md Step 4,
    paired-review HIGH finding): `worst_neighbor`/`worst_neighbor_core` are now computed
    from `core_cagr`/`addon_cagr` (bounded at -100%) instead of `core_alpha`/`addon_alpha`
    (unbounded raw alpha differences -- the KORU sub-(-100%) worst_neighbor_pct bug).
    This is a real, DELIBERATE threshold-semantics change, not just a units swap: the
    `< 0` cliff/safe boundary used to mean "a neighbor underperforms SPY" (alpha units);
    it now means "a neighbor loses money outright" (CAGR units) -- a materially LOOSER
    safety bar (a neighbor at, say, +5% CAGR while SPY does +60% was CLIFF before, is SAFE
    now). This diff intentionally does NOT introduce a nonzero CAGR floor to compensate
    (e.g. the plan doc's own separately-stated "worst-neighbor ground-truth CAGR > 20%"
    selection-bar framing, ground_truth_kernel_rebuild.md's "Cliff-safety redefinition"
    section) -- that's a real open design question (what should the safe/cliff CAGR
    threshold actually be for THIS specific check) left for explicit user decision, not
    something to default silently here. See docs/backlog_cache.md.

    addon_eligible (2026-08-23, GT Phase 4; corrected same day): threaded straight
    through to every _evaluate_cell_ground_truth_with_addon call (own cell and every
    neighbor) as a pure ANNOTATION -- it does NOT gate whether addon_alpha/addon_cliff
    get computed (corrected per direct user instruction: the add-on number has no
    account dependency and is a real input to a FUTURE account-assignment decision for
    not-yet-promoted candidates, so gating it on TODAY's account would hide exactly the
    number that decision needs). Every result in the returned list carries the same
    'addon_eligible' value (whatever the ticker's real watch_list account status was at
    call time) purely so a caller/report can label the number's current deployability
    without having to re-derive it.

    data_source/config_version (2026-08-23, found LIVE while verifying this fix, then
    hardened after BOTH paired-review passes independently flagged it HIGH): real,
    reproduced bug found verifying this fix -- scripts/candidate_summary_report.py's
    gt_rows_for_scope (and scripts/candidate_full_review.py has the identical
    omission) calls build_candidate_report_ground_truth without passing data_source
    through at all, so a massive-tagged config_version (e.g.
    'v6-massive-w2021-08-23_2026-08-21') silently gets its own_cell/neighbor cells
    evaluated against YAHOO data instead -- confirmed via direct comparison (DPST
    TP=17/SL=3/hold=112 massive: 394 trades/36.6% core CAGR; same coords, yahoo
    default: 200 trades/27.9% core CAGR -- same coordinates, different data, silently
    different numbers). Those caller bugs are out of THIS fix's scope (different
    files, pre-existing, affect both same_bar_reentry settings equally) -- see
    docs/backlog_cache.md.

    What IS in scope: cliff_addon_cache itself must never silently serve/persist one
    source's numbers as another's. First cut of this fix (same day) tried to handle
    this with ONLY a ValueError guard below (mirroring dispatch_parallel_grid_ground_
    truth's identical one-directional check) -- both the independent-cold and
    contextual paired reviews independently flagged this as insufficient: the guard
    only fires when data_source=='massive' with no '-massive' marker; the bug actually
    found above is the OPPOSITE direction (data_source silently defaulting to 'yahoo'
    under an already-massive-tagged version), which no caller-side omission can trigger
    this check to catch. Real fix: data_source/start_date/end_date are now genuine
    PK columns on cliff_addon_cache itself (see _ensure_cliff_addon_cache_table's own
    comment) -- a yahoo-sourced call and a massive-sourced call for the same
    coordinates now simply never collide in the cache, regardless of whether every
    caller remembers to pass data_source correctly. The ValueError guard below is kept
    for parity with dispatch_parallel_grid_ground_truth and because a loud failure is
    still better than a silent one where it CAN fire, but it is not what makes the
    cache safe -- the PK design is."""
    if data_source == "massive" and "-massive" not in config_version:
        raise ValueError(
            f"run_addon_cliff_safety_ground_truth: data_source='massive' but "
            f"config_version={config_version!r} carries no '-massive' marker -- append "
            f"'-massive' to config_version so massive-sourced cliff_addon_cache rows can "
            f"never collide with yahoo-sourced rows under the same version string "
            f"(same guard as dispatch_parallel_grid_ground_truth's own data_source check)."
        )
    radius = CLIFF_RADIUS if cliff_radius is None else cliff_radius
    trail_pcts = _trail_pcts_for_strategy(strategy_name, hp)
    # Same fourth-axis test run_phase25_cliff_box_ground_truth itself uses (NOT a
    # hand-inlined strategy-name check) -- paired-review finding (2026-08-22, Opus
    # independent-cold): re-deriving "does this strategy have a trail_pct 4th axis" by
    # comparing strategy_name directly is the exact pattern that produced a documented
    # CRITICAL elsewhere in this file (identify_full_mesh_candidates' trail_sell_pct
    # mix-up). resolve_axis_columns is the single source of truth for this.
    _, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)

    cache_conn = sqlite3.connect(DB_PATH, timeout=60.0)
    _ensure_cliff_addon_cache_table(cache_conn)
    addon_cache_map = _load_cliff_addon_cache_map(
        cache_conn, ticker, strategy_name, config_version, entry_timing, fixed_sl,
        data_source, start_date, end_date)
    new_cache_rows = []

    results = []
    for cand in candidates:
        tp_c, sl_c, hold_c, w_c, z_c, tpct_c = (cand['take_profit'], cand['stop_loss'],
                                                 cand['max_hold_hours'], cand['window'],
                                                 cand['z_score_threshold'], cand['tpct'])
        if fourth_axis_col == 'trail_pct' and tpct_c in trail_pcts:
            idx = trail_pcts.index(tpct_c)
            tpct_neighbors = trail_pcts[max(0, idx - 1): idx + 2]
        else:
            tpct_neighbors = [tpct_c]

        own = _evaluate_cell_ground_truth_with_addon(
            ticker, strategy_name, tp_c, sl_c, hold_c, w_c, z_c, fixed_sl, tpct_c,
            entry_timing, start_date, end_date, spy_bh, years, data_source=data_source,
            addon_eligible=addon_eligible, config_version=config_version,
            addon_cache_map=addon_cache_map, new_cache_rows=new_cache_rows)

        neighbor_addon_cagrs = []
        neighbor_core_cagrs = []
        for tp in range(max(1, tp_c - radius), min(30, tp_c + radius) + 1):
            for sl in range(max(1, sl_c - radius), min(30, sl_c + radius) + 1):
                for hold in [h for h in hp['hold_time_caps'] if abs(h - hold_c) <= 7]:
                    for tpct in tpct_neighbors:
                        cell = _evaluate_cell_ground_truth_with_addon(
                            ticker, strategy_name, tp, sl, hold, w_c, z_c, fixed_sl, tpct,
                            entry_timing, start_date, end_date, spy_bh, years, data_source=data_source,
                            addon_eligible=addon_eligible, config_version=config_version,
                            addon_cache_map=addon_cache_map, new_cache_rows=new_cache_rows)
                        if cell is not None:
                            # cagr, not alpha (2026-08-23, ground_truth_kernel_rebuild.md
                            # Step 4): raw alpha differences are unbounded below -100%
                            # (confirmed root cause of KORU's confusing sub-(-100%)
                            # worst_neighbor_pct reading) -- cagr is the bounded,
                            # real-compounded-return figure already computed on the same
                            # cell dict, just unused for this purpose until now.
                            # addon_cagr is None when apply_addon_overlay_ground_truth
                            # flagged a return_below_floor breach for this cell (see
                            # _evaluate_cell_ground_truth_with_addon) -- excluded from the
                            # worst-neighbor min() rather than crashing on a None/float
                            # comparison or, worse, being silently treated as the most
                            # negative value. core_cagr is never None (core Return is
                            # always >= -1, so core aggregation can't hit this failure
                            # mode) but included in the same None-guard for symmetry.
                            if cell['addon_cagr'] is not None:
                                neighbor_addon_cagrs.append(cell['addon_cagr'])
                            if cell['core_cagr'] is not None:
                                neighbor_core_cagrs.append(cell['core_cagr'])

        # Fail CLOSED (None/"unknown"), not open to "safe", when nothing was evaluated --
        # paired-review finding (2026-08-22, all 4 review passes converged on this
        # independently): this is the exact failure shape identify_full_mesh_candidates'
        # own comments (run_optimization_sweep.py, its `else 0.0` fix) already document
        # as having fabricated a "safe" verdict for every TrailingExit row historically.
        worst_neighbor = min(neighbor_addon_cagrs) if neighbor_addon_cagrs else None
        worst_neighbor_core = min(neighbor_core_cagrs) if neighbor_core_cagrs else None
        addon_cliff = None if worst_neighbor is None else (worst_neighbor < 0)
        core_cliff = None if worst_neighbor_core is None else (worst_neighbor_core < 0)
        results.append({
            'candidate': cand, 'own_cell': own,
            'n_neighbors_evaluated': len(neighbor_addon_cagrs),
            # NOTE: these two dict keys keep their original "_alpha" suffix (not renamed,
            # 2026-08-23 CAGR-ranking fix -- kept the diff minimal/reviewable per task
            # scope) but now hold CAGR values, not alpha. worst_neighbor/worst_neighbor_core
            # above are themselves CAGR-based (min of neighbor_*_cagrs).
            'worst_neighbor_addon_alpha': worst_neighbor,
            'worst_neighbor_core_alpha': worst_neighbor_core,
            'core_cliff': core_cliff,
            'addon_cliff': addon_cliff,
            # same_bar_reentry=True (2026-08-23 fix, see this function's own docstring):
            # this pass now evaluates under the SAME setting every real campaign dispatch
            # uses, so `core_alpha`/`core_cliff` above match the candidate's own cached
            # `candidate['robust_alpha']` (mod float determinism) -- kept as an explicit
            # field (rather than dropped now that it's a constant) so a consumer reading
            # an older result dict (or one persisted before this fix) can still tell
            # which convention it was computed under.
            'same_bar_reentry': True,
            'addon_eligible': addon_eligible,
        })

    _flush_cliff_addon_cache_rows(cache_conn, ticker, strategy_name, config_version,
                                   entry_timing, fixed_sl, data_source, start_date, end_date,
                                   new_cache_rows)
    cache_conn.close()
    return results


# ── GT candidate-report enrichment (2026-08-22) ──────────────────────────────
# Checks 1/4/8/9/11/13 of docs/watchlist_candidate_checklist.md, ported from
# scripts/checklist_v65.py to GT's Return-based trade format, built ONLY for the up-to-9
# already-selected Phase2.5-GT candidates per campaign (NOT the full grid -- see
# build_candidate_report_ground_truth's own docstring). Two robustness bars are used
# throughout this codebase for two different purposes and are NOT interchangeable:
# PHASE25_ISLAND_CAGR_MIN (30%) gates Phase4 (drought/add-on overlay) eligibility for an
# already-selected candidate (candidate-quality bar; does NOT block cliff-boxing itself
# -- see PHASE25_ISLAND_CLIFFBOX_CAGR_MIN for that); GT_ROBUSTNESS_CAGR_MIN (20%) is the
# established "does this survive a perturbation" bar applied to checks 9/13 below
# (robustness bar).
GT_ROBUSTNESS_CAGR_MIN = 20
GT_FLUKE_MIN_TRADES = 10  # same "too few to trust" threshold as checklist_v65.check4_stability
GT_WALK_FORWARD_FOLDS = 5  # same fold count as checklist_v65.FOLDS
GT_DROUGHT_IE_VOL_GATE = 0.4  # same "one real validated value" as candidate_full_review.DEFAULT_VOL_GATE


def _check1_macro_gt(ticker, start_date, end_date, data_source="yahoo"):
    """Check 1 (macro/trend) -- ticker's own 30d/90d return off its already-loaded hourly
    bars resampled to daily closes, within the campaign's own window. Same loading
    convention as _campaign_years_for_window (duplicated rather than shared -- see that
    function's own docstring for why -- and the same 21/63-trading-day approximation for
    30/90 calendar days checklist_v65.check1_macro uses)."""
    if data_source == "massive":
        import db_cache
        try:
            df_hourly_raw = db_cache.get_massive_hourly_ohlcv(ticker)
        except ValueError:
            return None, None
    else:
        cache_path = CACHE_DIR / f"{ticker}_1h.csv"
        if not cache_path.exists():
            return None, None
        df_hourly_raw = pd.read_csv(cache_path, index_col=0, parse_dates=True)
        df_hourly_raw.index = pd.to_datetime(df_hourly_raw.index).tz_localize(None)
        df_hourly_raw = df_hourly_raw.sort_index()
    df_windowed = df_hourly_raw
    if start_date is not None or end_date is not None:
        lo = pd.Timestamp(start_date) if start_date is not None else None
        hi = (pd.Timestamp(end_date) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)) if end_date is not None else None
        df_windowed = df_hourly_raw.loc[lo:hi]
    if df_windowed.empty:
        return None, None
    close_col = 'Adj Close' if 'Adj Close' in df_windowed.columns else 'Close'
    df_daily = df_windowed.resample('D').last().dropna(subset=[close_col])
    close = df_daily[close_col]
    r30 = float((close.iloc[-1] / close.iloc[-21] - 1) * 100) if len(close) > 21 else None
    r90 = float((close.iloc[-1] / close.iloc[-63] - 1) * 100) if len(close) > 63 else None
    return r30, r90


def _check4_stability_gt(trades):
    """Check 4 (70/30 win-rate stability), ported from checklist_v65.check4_stability --
    same split convention: trades sorted chronologically by Entry Time, then cut by
    COUNT at the 70th percentile index (`cut = int(n * 0.7)`), not by an equal-time-span
    cut -- matching the original function exactly (corrected 2026-08-22 paired-review
    docstring finding: an earlier version of this docstring said "by TIME not count",
    which described check13's fold-slicing, not this function's own split). Adapted from
    the hourly kernel's Result-label wins (Result in WIN/TWIN) to GT's Return-based
    win/loss (Return > 0), matching how _summarize_trades_ground_truth's own win_rate is
    defined. Returns (None, None) below the same too-few-to-trust threshold
    check4_stability itself uses."""
    if len(trades) < GT_FLUKE_MIN_TRADES:
        return None, None
    df = pd.DataFrame(trades).sort_values("Entry Time")
    n = len(df)
    cut = int(n * 0.7)
    early, late = df.iloc[:cut], df.iloc[cut:]
    early_wr = float((early["Return"] > 0).mean() * 100) if len(early) else None
    late_wr = float((late["Return"] > 0).mean() * 100) if len(late) else None
    return early_wr, late_wr


def _check8_fluke_gt(trades):
    """Check 8 (trade-count fluke), ported from checklist_v65.check8_fluke -- single-
    biggest-trade-removal compounded-return comparison, plus a too_few_trades flag using
    the same GT_FLUKE_MIN_TRADES threshold check4_stability's own trades<10 check uses
    (the task's "reuse the same number" instruction)."""
    n = len(trades)
    if n == 0:
        return {'n_trades': 0, 'too_few_trades': True, 'compounded_pct': None,
                'compounded_without_best_pct': None, 'best_trade_share_pct': None}
    df = pd.DataFrame(trades)
    compounded_total = float(((df["Return"] + 1).prod() - 1) * 100)
    best_i = df["Return"].idxmax()
    without_best = df.drop(best_i)
    compounded_wo = float(((without_best["Return"] + 1).prod() - 1) * 100) if len(without_best) else 0.0
    return {
        'n_trades': n,
        'too_few_trades': n < GT_FLUKE_MIN_TRADES,
        'compounded_pct': compounded_total,
        'compounded_without_best_pct': compounded_wo,
        'best_trade_share_pct': compounded_total - compounded_wo,
    }


def _check11_max_drawdown_gt(trades):
    """Check 11 (max drawdown), same convention as scripts/v4_max_drawdown.max_drawdown --
    pure function of the compounded equity curve (cumulative product of 1+Return per
    trade in chronological order). Returns (max_dd_pct [<=0], peak_time, trough_time)."""
    df = pd.DataFrame(trades).sort_values("Entry Time")
    equity, peak, peak_time = 100.0, 100.0, None
    max_dd, dd_peak_time, dd_trough_time = 0.0, None, None
    for _, t in df.iterrows():
        if peak_time is None:
            peak_time = t["Entry Time"]
        equity *= (1.0 + t["Return"])
        if equity >= peak:
            peak = equity
            peak_time = t["Exit Time"]
        else:
            dd = (equity - peak) / peak
            if dd < max_dd:
                max_dd, dd_peak_time, dd_trough_time = dd, peak_time, t["Exit Time"]
    return max_dd * 100.0, dd_peak_time, dd_trough_time


def _check13_walk_forward_gt(trades, robustness_cagr_min=GT_ROBUSTNESS_CAGR_MIN):
    """Check 13 (walk-forward N-fold), ported from checklist_v65.check13_walk_forward --
    same equal-TIME-span 5-fold slicing. Reports each fold's own CAGR (GT trades carry
    Return directly, no alpha-vs-SPY split needed the way the hourly path's possible/
    pessimistic/certain triple required) and flags a fold 'fragile' at CAGR<=20%
    (GT_ROBUSTNESS_CAGR_MIN, the project's established robustness bar) -- NOT the 30%
    PHASE25_ISLAND_CAGR_MIN candidate-quality bar used elsewhere in this pipeline; the
    two are deliberately different thresholds for two different purposes."""
    if len(trades) < GT_WALK_FORWARD_FOLDS:
        return []
    df = pd.DataFrame(trades).sort_values("Entry Time")
    dates_min, dates_max = df["Entry Time"].min(), df["Exit Time"].max()
    span = dates_max - dates_min
    edges = [dates_min + span * i / GT_WALK_FORWARD_FOLDS for i in range(GT_WALK_FORWARD_FOLDS + 1)]
    rows = []
    for i in range(GT_WALK_FORWARD_FOLDS):
        start, end = edges[i], edges[i + 1]
        sub = df[(df["Entry Time"] >= start) & (df["Entry Time"] < end)]
        if sub.empty:
            rows.append({'fold': i + 1, 'n': 0, 'compounded_pct': None, 'cagr': None, 'fragile': None})
            continue
        compounded = float(((sub["Return"] + 1).prod() - 1) * 100)
        fold_years = (end - start).total_seconds() / (365.25 * 86400)
        cagr = _cagr_from_total_return(compounded, fold_years)
        # A fold with real trades but cagr=None means _cagr_from_total_return hit its
        # total_return<=-100% guard (a fold that's fully wiped out) -- that is at least
        # as fragile as a low-but-computable CAGR, not "unknown"/unflagged (paired-review
        # finding, 2026-08-22: the worst fold rendering as unflagged 'n/a' was worse than
        # silent, since a reader would read no marker as "this fold was fine"). Only a
        # genuinely empty fold (n=0, no trades at all) stays 'unknown'.
        fragile = True if cagr is None else (cagr <= robustness_cagr_min)
        rows.append({'fold': i + 1, 'n': len(sub), 'compounded_pct': compounded, 'cagr': cagr, 'fragile': fragile})
    return rows


def build_candidate_report_ground_truth(ticker, strategy_name, config_version, hp,
                                         start_date, end_date, fixed_sl=0,
                                         entry_timing='open_check', data_source="yahoo",
                                         cliff_radius=None, candidates_override=None):
    """candidates_override (2026-08-29, Task #3, additive -- see Review-Gate Persistence
    Rule outcome recorded in this commit's message): when provided, use this candidate
    list directly instead of calling derive_phase25_candidates_ground_truth -- lets a
    caller feed a candidate_nodes-sourced list (scripts/phase4_candidate_nodes_resolver.py)
    for a campaign the in-memory sweep pipeline ran, which writes zero backtest_cache
    rows and so derive_phase25_candidates_ground_truth can never see. Every existing
    caller passes nothing (default None) and gets byte-identical behavior to before this
    param existed -- pure additive override, no existing call site changes. A candidate_
    nodes-sourced list has real robust_alpha but cagr=None for every candidate (that
    table doesn't persist a cagr column) -- winner_index selection below falls back to
    ranking by robust_alpha ONLY when every candidate's cagr is None (a real backtest_
    cache-sourced list always has real cagr on every candidate, so the default path's
    ranking is unaffected); a report can never mix the two sources within one call.

    Full enriched candidate report for a completed GT campaign's up-to-9 Phase2.5-GT
    candidates (2026-08-22 candidate-report build-out). Reuses derive_phase25_candidates_
    ground_truth (candidate derivation -- NOT re-derived here) and run_addon_cliff_safety_
    ground_truth (core_cliff/addon_cliff verdicts -- NOT re-derived here) rather than
    duplicating either. Adds checks 1/4/8/11/13 (see module comment above check1) computed
    against each candidate's OWN real trade list (same_bar_reentry=True, matching the real
    Phase1/2/2.5-GT dispatch convention every cached row in this campaign was computed
    under -- run_addon_cliff_safety_ground_truth's own cells now also use same_bar_
    reentry=True as of 2026-08-23, see that function's docstring; this comment used to
    call out a same_bar_reentry mismatch between the two that no longer exists).

    Check 9 (same-day-block sensitivity) is NOT built: `run_backtest_ground_truth`/
    `_simulate_trail_ground_truth` (backtester.py) have no same_day_block parameter at
    all (same finding checklist_v65.py already documented for the legacy TE kernel) --
    inventing new kernel behavior to support it is out of scope for this report, so this
    is reported as a plain finding via the 'check9_same_day_block' key (None) rather than silently
    dropped.

    Only computes checks 1/4/8/11/13 for the up-to-9 selected candidates, NOT the full
    grid (~164k+ cells) -- computing these across the whole mesh would be prohibitively
    expensive and isn't what they're for (this pipeline's other pieces already rank the
    full grid; this report explains the finalists).

    Returns a dict: {ticker, strategy_name, config_version, macro_r30_pct, macro_r90_pct,
    same_day_block_check9: None (see above), addon_eligible, addon_eligibility_reason,
    candidates: [ {candidate, n_trades, check4_early_wr/late_wr, check8,
    check11_max_drawdown_pct/peak/trough, check13_folds, core_cliff, addon_cliff,
    core_addon_disagreement, drought: {...} or None}, ... ], winner_index}.

    Drought/add-on architecture (2026-08-23, GT Phase 4 -- replaces the old post-hoc
    single-fixed-confirm_days bolt-on, scripts/gt_addon_winner_drought_eval.py, which is
    no longer called from here): each candidate's own core `trades` list (computed once
    below, same_bar_reentry=True) now feeds BOTH the checks 4/8/11/13 AND a per-candidate
    drought confirm_days x vol_gate sweep (backtester.simulate_drought_overlay_ground_
    truth) in the same pass -- one run_backtest_ground_truth call per candidate instead
    of a second one just to re-derive the winner's own trade list a second time. Drought
    is computed for EVERY shortlisted candidate now, not just the overall winner. Add-on
    (run_addon_cliff_safety_ground_truth, unchanged mechanism) is ALWAYS computed for
    every candidate regardless of the ticker's current account -- the add-on number has
    no account dependency and is a real input to a FUTURE account-assignment decision
    (a strong add-on CAGR argues for promoting a candidate into a margin-capable account,
    not just describing one it's already in). addon_margin_eligible_ticker(ticker) is
    computed ONCE for the whole campaign purely to ANNOTATE the number with its current
    real-world deployability ('addon_eligible'/'addon_eligibility_reason' at the report
    level) -- it never gates or blanks the computation itself."""
    if candidates_override is not None:
        candidates = candidates_override
    else:
        candidates = derive_phase25_candidates_ground_truth(
            ticker, strategy_name, config_version, hp, fixed_sl=fixed_sl, entry_timing=entry_timing)
    if not candidates:
        return {'ticker': ticker, 'strategy_name': strategy_name, 'config_version': config_version,
                'candidates': [], 'error': 'No Phase2.5-GT candidates found for this campaign scope.'}

    asset_bh, spy_bh = compute_bh_returns(ticker, start_date=start_date, end_date=end_date, data_source=data_source)
    years = _campaign_years_for_window(ticker, start_date, end_date, data_source=data_source)
    r30, r90 = _check1_macro_gt(ticker, start_date, end_date, data_source=data_source)

    addon_eligible, addon_eligibility_reason = addon_margin_eligible_ticker(ticker)

    # Phase4 (drought/add-on overlay) is real, possibly-slow compute (one
    # run_backtest_ground_truth call per cell, times up to 9 candidates times the full
    # cliff-box neighborhood -- see this function's own docstring) -- skip it for any
    # candidate PHASE25_ISLAND_CAGR_MIN already marked ineligible (2026-08-23 redesign),
    # rather than computing it for everyone and only using the CAGR flag informationally.
    phase4_candidates = [c for c in candidates if c.get('phase4_eligible', True)]
    if phase4_candidates:
        eligible_addon_results = run_addon_cliff_safety_ground_truth(
            ticker, strategy_name, config_version, hp, phase4_candidates, spy_bh, fixed_sl=fixed_sl,
            entry_timing=entry_timing, start_date=start_date, end_date=end_date, years=years,
            data_source=data_source, cliff_radius=cliff_radius, addon_eligible=addon_eligible)
    else:
        eligible_addon_results = []
    _addon_by_id = dict(zip((id(c) for c in phase4_candidates), eligible_addon_results))
    addon_results = [
        _addon_by_id[id(c)] if id(c) in _addon_by_id
        else {'core_cliff': None, 'addon_cliff': None, 'addon_cagr': None, 'own_cell': None}
        for c in candidates
    ]

    strategy_class = getattr(strategies, strategy_name)
    is_both = strategy_name == 'TrailingBothZScoreBreakout'
    # Drought's SL/arm/trail overlay mechanism was only ever validated against
    # TrailingBoth's own arm-then-trail risk parameters (scripts/drought_overlay_test.py
    # -- the legacy script this replaces made the identical "is_both assumed True"
    # restriction, carried forward unchanged rather than newly generalizing an
    # unvalidated TrailingExit drought overlay in this same change).
    drought_skip_reason = None if is_both else (
        f"strategy is {strategy_name!r}, not TrailingBothZScoreBreakout -- the drought "
        f"overlay mechanism is only validated for TrailingBoth's arm-then-trail shape.")

    # Trades-cache read-back (2026-08-29, Task #1, planner dispatch): Phase2.5
    # (bench_phase1_phase2_inmemory.py's _insert_winner_trades_rows) already persists
    # this exact same_bar_reentry=True/need_times=True trade list for its own top-9-
    # per-scope population, under the identical real inputs (same node_key params,
    # same version -- which encodes data_source + start/end window, see
    # candidate_summary_report._window_dates_from_version) this loop re-derives from
    # scratch below. Reading it back saves a full run_backtest_ground_truth call per
    # candidate whenever it's already there. NOT every Phase4 candidate is guaranteed
    # to have a cached row -- Phase2.5 only ever writes its own top-9-per-scope
    # population (confirmed empirically at dispatch time, see this task's own real-DB
    # coverage check) -- get_cached_trades returning None is the real, expected
    # fallback-to-resimulate signal for any candidate outside that population, not a bug.
    from scripts.node_key import node_key as _node_key
    from scripts.candidate_verification_store import get_cached_trades as _get_cached_trades
    _trades_conn = sqlite3.connect(DB_PATH)

    rows = []
    try:
        for cand, addon in zip(candidates, addon_results):
            node_key_val = _node_key(
                strategy_name, ticker, fixed_sl, cand['window'], cand['z_score_threshold'],
                cand['max_hold_hours'], cand['take_profit'], cand['stop_loss'], cand['tpct'],
                entry_timing, strategies.resolve_axis_columns)
            cached_trades = _get_cached_trades(_trades_conn, node_key_val, config_version)
            need_resim = cached_trades is None
            # is_both scopes still need df_hourly_windowed for the drought overlay below
            # even when trades came from cache -- non-is_both scopes with a cache hit
            # never need the (possibly slow) hourly/minute input load at all.
            need_inputs = need_resim or is_both

            inputs = _load_node_inputs_ground_truth(
                ticker, strategy_class, strategy_name, cand['window'], cand['z_score_threshold'],
                start_date, end_date, data_source=data_source) if need_inputs else None
            trades = cached_trades if cached_trades is not None else []
            df_hourly_windowed = None
            if inputs is not None:
                _, df_daily_processed, minute_df, df_hourly_windowed, prep, mprep = inputs
                if not df_hourly_windowed.empty and need_resim:
                    if is_both:
                        trail_buy_pct_arg, trail_sell_pct_arg, arm_pct_arg = (
                            float(cand['stop_loss']), float(cand['tpct']), float(cand['take_profit']))
                    else:
                        trail_buy_pct_arg, trail_sell_pct_arg, arm_pct_arg = (
                            0.0, float(cand['stop_loss']), float(cand['take_profit']))
                    trades = run_backtest_ground_truth(
                        df_hourly_windowed, df_daily_processed, ticker, minute_df,
                        fixed_sl=fixed_sl, arm_pct=arm_pct_arg, trail_buy_pct=trail_buy_pct_arg,
                        trail_sell_pct=trail_sell_pct_arg, max_hours_to_hold=cand['max_hold_hours'],
                        z_score_threshold=cand['z_score_threshold'], is_both=is_both,
                        open_check_entry_timing=(entry_timing == 'open_check'), same_bar_reentry=True,
                        prep=prep, mprep=mprep, need_times=True,
                    )

            c4_early_wr, c4_late_wr = _check4_stability_gt(trades) if trades else (None, None)
            c8 = _check8_fluke_gt(trades)
            dd_pct, dd_peak, dd_trough = _check11_max_drawdown_gt(trades) if trades else (None, None, None)
            c13_folds = _check13_walk_forward_gt(trades) if trades else []

            # Drought is Phase4 overlay work same as add-on -- skip for phase4_eligible=False
            # candidates too (2026-08-23 fix: this was still computing unconditionally,
            # only add-on had been wired to respect the flag).
            drought = None
            drought_ie = None
            if cand.get('phase4_eligible', True) and is_both and trades and df_hourly_windowed is not None:
                drought = simulate_drought_overlay_ground_truth(
                    trades, df_hourly_windowed, ticker, fixed_sl,
                    arm_pct=float(cand['take_profit']), trail_sell_pct=float(cand['tpct']))
                # Included-vs-excluded vol-gate challenge (2026-08-23, candidate_full_review.py
                # GT full-review port -- docs/overlay_parameter_robustness_process.md step 4),
                # only meaningful once a real winning confirm_days exists (drought['best_confirm_days']
                # is None when the sweep found zero real drought windows at every grid cell).
                if drought is not None and drought.get('best_confirm_days') is not None:
                    drought_ie = drought_included_excluded_ground_truth(
                        trades, df_hourly_windowed, ticker, fixed_sl,
                        arm_pct=float(cand['take_profit']), trail_sell_pct=float(cand['tpct']),
                        confirm_days=drought['best_confirm_days'], vol_gate=GT_DROUGHT_IE_VOL_GATE)

            core_cliff = addon['core_cliff']
            addon_cliff = addon['addon_cliff']
            disagreement = (core_cliff is not None and addon_cliff is not None and core_cliff != addon_cliff)

            rows.append({
                'candidate': cand,
                'phase4_eligible': cand.get('phase4_eligible', True),
                'n_trades': len(trades),
                # Raw per-trade closed-trade list (need_times=True -- Entry/Exit Time/Price,
                # Return, armed/Arm Time/Arm Price), added 2026-08-23 for candidate_full_
                # review.py's --kernel gt full-review port: lets that report compute real
                # win-rate/tranche/addon-leg numbers directly off this SAME trade list (via
                # apply_addon_overlay_ground_truth) instead of a second run_backtest_ground_
                # truth call. [] when this candidate had no cached hourly inputs.
                'trades': trades,
                'check4_early_wr_pct': c4_early_wr, 'check4_late_wr_pct': c4_late_wr,
                'check8_fluke': c8,
                'check11_max_drawdown_pct': dd_pct, 'check11_dd_peak_time': dd_peak, 'check11_dd_trough_time': dd_trough,
                'check13_folds': c13_folds,
                'core_safe': None if core_cliff is None else (not core_cliff),
                'addon_safe': None if addon_cliff is None else (not addon_cliff),
                'core_addon_disagreement': disagreement,
                'addon_detail': addon,
                'drought': drought,
                'drought_ie': drought_ie,
            })
    finally:
        _trades_conn.close()

    # cagr, not robust_alpha (2026-08-23, ground_truth_kernel_rebuild.md Step 4): CAGR is
    # the sole GT selection metric now -- this is the single most consequential ranking
    # line in the pipeline (decides which candidate gets labeled the overall winner).
    # Falls back to robust_alpha ONLY for a candidates_override list whose cagr is
    # entirely None (candidate_nodes doesn't persist cagr, see this function's own
    # docstring) -- never triggers for the default backtest_cache-sourced path, which
    # always has real cagr on every candidate.
    if all(c['cagr'] is None for c in candidates):
        winner_index = max(range(len(candidates)), key=lambda i: candidates[i]['robust_alpha'])
        winner_metric = 'robust_alpha'
    else:
        winner_index = max(range(len(candidates)), key=lambda i: candidates[i]['cagr'])
        winner_metric = 'cagr'

    return {
        'ticker': ticker, 'strategy_name': strategy_name, 'config_version': config_version,
        'macro_r30_pct': r30, 'macro_r90_pct': r90,
        'check9_same_day_block': None,  # see docstring -- no same_day_block param exists in the GT kernel
        'addon_eligible': addon_eligible,
        'addon_eligibility_reason': addon_eligibility_reason,
        'candidates': rows,
        'winner_index': winner_index,
        'winner_metric': winner_metric,
        'drought_skip_reason': drought_skip_reason,
    }


def print_candidate_report_ground_truth(report):
    """Renders build_candidate_report_ground_truth's dict as a readable text report --
    separate from the builder so a caller can also consume the dict programmatically
    (e.g. a future Streamlit page) without re-parsing printed text."""
    print(f"\n{'='*100}\nGT Candidate Report -- {report['ticker']} / {report['strategy_name']} / {report['config_version']}\n{'='*100}")
    if report.get('error'):
        print(report['error'])
        return
    r30, r90 = report['macro_r30_pct'], report['macro_r90_pct']
    print(f"Check 1 (macro/trend): 30d={r30:+.1f}%  90d={r90:+.1f}%" if r30 is not None and r90 is not None
          else "Check 1 (macro/trend): insufficient daily history")
    print("Check 9 (same-day-block sensitivity): SKIPPED -- run_backtest_ground_truth has no "
          "same_day_block parameter (matches checklist_v65.py's own finding for the legacy TE kernel).")
    print(f"{len(report['candidates'])} candidate(s):\n")

    for i, row in enumerate(report['candidates']):
        c = row['candidate']
        marker = f" <-- OVERALL WINNER (by {report['winner_metric']})" if i == report['winner_index'] else ""
        print(f"--- Candidate {i+1}{marker} ---")
        # cagr can be None for a candidates_override list (candidate_nodes doesn't
        # persist cagr, see this function's own docstring) -- never None for the
        # default backtest_cache-sourced path.
        cagr_str = f"{c['cagr']:.1f}%" if c['cagr'] is not None else "N/A (candidate_nodes-sourced)"
        print(f"  island(TP={c['island_tp']} SL={c['island_sl']})  cell TP={c['take_profit']} "
              f"SL={c['stop_loss']} hold={c['max_hold_hours']}h w={c['window']} z={c['z_score_threshold']} "
              f"tpct={c['tpct']}  robust_alpha={c['robust_alpha']:.2f}  cagr={cagr_str}  n_trades={row['n_trades']}")

        phase4_eligible = row.get('phase4_eligible', True)
        skip_note = " (skipped -- below PHASE25_ISLAND_CAGR_MIN)" if not phase4_eligible else ""
        core_s = "SAFE" if row['core_safe'] else ("CLIFF" if row['core_safe'] is False else
                 ("SKIPPED" if not phase4_eligible else "UNKNOWN"))
        addon_s = "SAFE" if row['addon_safe'] else ("CLIFF" if row['addon_safe'] is False else
                  ("SKIPPED" if not phase4_eligible else "UNKNOWN"))
        flag = "  *** CORE/ADD-ON DISAGREEMENT ***" if row['core_addon_disagreement'] else ""
        print(f"  Verdicts: core-safe={core_s}  add-on-safe={addon_s}{skip_note}{flag}")

        # Add-on CAGR (2026-08-23, GT Phase 4; corrected same day per direct user
        # instruction): ALWAYS computed and surfaced prominently here, regardless of
        # whether the ticker is currently on a margin-capable account -- this number is
        # a real INPUT to a future account-assignment decision for a not-yet-promoted
        # candidate (a strong add-on CAGR argues FOR assigning it a margin-capable
        # account), not just a property of an account it already happens to be in.
        # Deliberately not buried as a footnote -- earlier same-day versions of this
        # report gated the computation itself on today's account (fixed) and then
        # buried the label as a single campaign-wide footnote (also fixed here: the
        # deployability label is now per-candidate, right next to the number it
        # qualifies, since 'addon_eligible' is the same for every candidate in a
        # campaign but a reader shouldn't have to scroll to the bottom to connect them).
        own = row['addon_detail']['own_cell']
        addon_cagr = own['addon_cagr'] if own else None
        core_cagr_own = own['core_cagr'] if own else None
        if report['addon_eligible']:
            deploy_note = "deployable now -- already on a margin-capable account"
        else:
            deploy_note = f"NOT YET DEPLOYABLE -- {report['addon_eligibility_reason']}"
        if addon_cagr is not None:
            delta = (addon_cagr - core_cagr_own) if core_cagr_own is not None else None
            delta_str = f", {delta:+.1f}pp vs core's own {core_cagr_own:.1f}%" if delta is not None else ""
            print(f"  Add-on CAGR if placed in a margin-capable account: {addon_cagr:+.1f}%{delta_str}  [{deploy_note}]")
        elif not phase4_eligible:
            print("  Add-on CAGR: skipped (below PHASE25_ISLAND_CAGR_MIN, not worth the compute)")
        elif own is None:
            print("  Add-on CAGR: unavailable (no cell data for this candidate)")
        else:
            print("  Add-on CAGR: unavailable (return_below_floor breach -- see addon_return_floor_breached)")

        if row['check4_early_wr_pct'] is not None:
            print(f"  Check 4 (70/30 win-rate stability): early={row['check4_early_wr_pct']:.1f}%  "
                  f"late={row['check4_late_wr_pct']:.1f}%")
        else:
            print(f"  Check 4 (70/30 win-rate stability): skipped (< {GT_FLUKE_MIN_TRADES} trades)")

        c8 = row['check8_fluke']
        if c8['compounded_pct'] is not None:
            # "(same_bar_reentry=True)" label -- this candidate's own drought block below
            # now reuses this SAME same_bar_reentry=True trades list (2026-08-23 GT Phase
            # 4 change), so its 'core=' figure matches this compounded_pct exactly, unlike
            # the old post-hoc drought script which recomputed core trades with
            # same_bar_reentry=False. add-on's own cliff-safety pass (Verdicts line above)
            # is computed separately (its own run_backtest_ground_truth call, own trades
            # list) and can still legitimately differ from this number in magnitude (e.g.
            # candidate-report-scoped date window vs cliff-box campaign scope) -- but as
            # of 2026-08-23 it uses the SAME same_bar_reentry=True setting, not a
            # different one, so any difference is no longer a same_bar_reentry mismatch;
            # see run_addon_cliff_safety_ground_truth's own docstring.
            print(f"  Check 8 (trade-count fluke, same_bar_reentry=True): n={c8['n_trades']}"
                  f"{' [TOO FEW]' if c8['too_few_trades'] else ''}  "
                  f"compounded={c8['compounded_pct']:+.1f}%  w/o best trade={c8['compounded_without_best_pct']:+.1f}%  "
                  f"best-trade share={c8['best_trade_share_pct']:+.1f}pp")
        else:
            print("  Check 8 (trade-count fluke): no trades")

        if row['check11_max_drawdown_pct'] is not None:
            print(f"  Check 11 (max drawdown): {row['check11_max_drawdown_pct']:.1f}%")

        folds = row['check13_folds']
        if folds:
            fold_parts = []
            for f in folds:
                if f['cagr'] is None and f['n'] > 0:
                    cagr_str, frag_str = 'n/a', '[WIPED OUT]'
                elif f['cagr'] is None:
                    cagr_str, frag_str = 'n/a', ''
                else:
                    cagr_str = f"{f['cagr']:.0f}%"
                    frag_str = '[FRAGILE]' if f['fragile'] else ''
                fold_parts.append(f"F{f['fold']}:n={f['n']},cagr={cagr_str}{frag_str}")
            fold_str = "  ".join(fold_parts)
            print(f"  Check 13 (walk-forward {GT_WALK_FORWARD_FOLDS}-fold, robustness bar <= {GT_ROBUSTNESS_CAGR_MIN}% CAGR): {fold_str}")

        d = row.get('drought')
        if d is not None:
            drought_only_str = 'n/a' if d['drought_compounded_pct'] is None else f"{d['drought_compounded_pct']:+.1f}%"
            combined_str = 'n/a' if d['combined_compounded_pct'] is None else f"{d['combined_compounded_pct']:+.1f}%"
            print(f"  Drought overlay (confirm_days x vol_gate swept, same_bar_reentry=True): "
                  f"core={d['core_compounded_pct']:+.1f}%  "
                  f"best(confirm_days={d['best_confirm_days']}, vol_gate={d['best_vol_gate']})  "
                  f"windows={d['n_drought_windows']} (simulated={d['n_drought_simulated']}, "
                  f"grid_cells={d['n_grid_cells_evaluated']})  "
                  f"drought_only={drought_only_str}  combined={combined_str}")
        elif report['strategy_name'] == 'TrailingBothZScoreBreakout':
            print(f"  Drought overlay: no result (< 2 core trades, or none mapped onto "
                  f"df_hourly_windowed's index).")
        elif report.get('drought_skip_reason'):
            print(f"  Drought overlay: skipped ({report['drought_skip_reason']})")
        print()


# ── Checkpoint 2: cliff check, return full-mesh candidates ───────────────────

def identify_full_mesh_candidates(config_version, strategy_name, island_tickers, n_index, n_stock, fixed_sl=0, entry_timing='close'):
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)
    scope_sql, scope_params = _campaign_scope_sql(strategy_name, fixed_sl, entry_timing)
    results = []
    with sqlite3.connect(DB_PATH) as conn:
        for ticker in island_tickers:
            row = conn.execute(f"""
                SELECT axis_tp, {_sl_axis_real_column(sl_axis_col)} AS stop_loss, max_hold_hours, window, z_score_threshold,
                       {ROBUST_ALPHA_SQL} AS robust_alpha,
                       {'trail_sell_pct' if fourth_axis_col == 'trail_pct' else '0'} AS tpct,
                       stop_loss AS literal_stop_loss, trail_buy_pct AS literal_trail_buy_pct,
                       trail_sell_pct AS literal_trail_sell_pct
                FROM backtest_cache
                WHERE version=? AND ticker=? AND strategy=? AND trades > 0
                  AND (kernel_version IS NULL OR kernel_version<>'ground_truth_v6') {scope_sql}
                ORDER BY robust_alpha DESC LIMIT 1
            """, (config_version, ticker, strategy_name, *scope_params)).fetchone()
            if not row:
                continue

            tp_c, sl_c, hold_c, win_c, z_c, best_alpha, tpct_c = (
                int(row[0]), int(row[1]), int(row[2]), int(row[3]), float(row[4]), float(row[5]), float(row[6]))
            # literal_stop_loss/literal_trail_buy_pct/literal_trail_sell_pct: the
            # LITERAL backtest_cache columns of that name (distinct from sl_c above,
            # which is the axis _sl_axis_real_column resolves to -- for TrailingBoth
            # that's trail_buy_pct itself, and for TrailingExit it's trail_sell_pct,
            # not the literal stop_loss column). Fetched directly, not derived from
            # tpct_c/fourth_axis_col, because a prior version of this fix pinned
            # trail_sell_pct=0.0 whenever it was the strategy's sl_axis rather than
            # its fourth_axis (TrailingExit's real case) -- 0.0 matches zero rows for
            # that strategy, so the neighbor search silently returned empty and
            # failed open to a fabricated "safe" 0.0 (paired Opus review, both
            # independent-cold and contextual, converged on this same CRITICAL
            # finding). Only needed for the i3 neighbor query below, which must
            # mirror top_safe_nodes.py's own convention exactly.
            literal_sl_c, literal_trail_buy_pct_c, literal_trail_sell_pct_c = (
                float(row[7]), float(row[8]), float(row[9]))
            # axis_tp is a REAL column -- tp_c's int() truncation is harmless today
            # (every real v5 axis_tp value is currently a whole number) but would
            # silently shift the i3 neighborhood off top_safe_nodes.py's exact-float
            # convention for a future fractional TP/arm grid (paired review finding).
            # Only the i3 block below uses this; i2 above is untouched, out of scope.
            axis_tp_f = float(row[0])

            tpct_filter = ""
            tpct_params = []
            if fourth_axis_col == 'trail_pct':
                tpct_filter = "AND trail_sell_pct BETWEEN ? AND ?"
                tpct_params = [tpct_c - 1, tpct_c + 1]

            worst = conn.execute(f"""
                SELECT MIN({ROBUST_ALPHA_SQL}) FROM backtest_cache
                WHERE version=? AND ticker=? AND strategy=?
                  AND window=? AND z_score_threshold=?
                  AND axis_tp        BETWEEN ? AND ?
                  AND {_sl_axis_real_column(sl_axis_col)}  BETWEEN ? AND ?
                  AND max_hold_hours BETWEEN ? AND ?
                  {scope_sql} {tpct_filter}
                  AND trades > 0
                  AND (kernel_version IS NULL OR kernel_version<>'ground_truth_v6')
            """, (config_version, ticker, strategy_name,
                  win_c, z_c,
                  tp_c - CLIFF_RADIUS, tp_c + CLIFF_RADIUS,
                  sl_c - CLIFF_RADIUS, sl_c + CLIFF_RADIUS,
                  hold_c - 7, hold_c + 7,
                  *scope_params, *tpct_params)).fetchone()[0]

            worst_neighbor = float(worst) if worst is not None else 0.0
            cliff = worst_neighbor < 0
            logger.info(f"  [{ticker}] best={best_alpha:+.1f}%  worst_neighbor={worst_neighbor:+.1f}%  {'CLIFF' if cliff else 'safe'}")
            results.append({'ticker': ticker, 'best_alpha': best_alpha, 'worst_neighbor': worst_neighbor})

            # Persist i2 (above) and i3 (this project's other standing cliff-safety
            # radius, see scripts/top_safe_nodes.py's CLIFF_RADIUS=3) for the group's
            # winner, so future consumers don't have to slowly re-derive this from
            # scratch. Scoped to best_alpha > 100% ("should be fewer nodes" -- user's
            # call, 2026-08-08) rather than every candidate. Best-effort: a failure
            # here must never break the sweep itself.
            #
            # RE-ENABLED 2026-08-0x, then fixed AGAIN same session after a paired
            # Opus review (independent-cold + contextual, both converged
            # independently with matching real-data measurements) found the first
            # re-enable attempt was itself broken in two ways:
            # (1) CRITICAL -- pinning trail_sell_pct via
            # `tpct_c if fourth_axis_col == 'trail_pct' else 0.0` is wrong for
            # TrailingExitZScoreBreakout: its sl_axis IS 'trail_pct' (real column
            # trail_sell_pct), not its fourth_axis (which is None), so this pinned
            # 0.0 -- a value matching zero real rows for that strategy (30 distinct
            # real values, none 0.0). The neighbor search came back empty every
            # time, and `else 0.0` on the MIN() result then persisted a fabricated
            # "safe" 0.0 for every TrailingExit row. Fixed: pin against
            # literal_trail_sell_pct_c (the literal column, fetched directly above)
            # instead of re-deriving from tpct_c/fourth_axis_col.
            # (2) HIGH -- the original comment's "harmless no-op" claim about
            # {scope_sql} was correct as a statement (it does pin stop_loss=fixed_sl
            # exactly for uses_fixed_sl strategies) but wrong about the
            # consequence: it meant the "stop_loss BETWEEN" radius never actually
            # searched neighboring fixed_sl campaigns, while top_safe_nodes.py's
            # own CLIFF_RADIUS=3 (scripts/top_safe_nodes.py:35-55) is NOT
            # campaign-scoped -- it pools the whole ticker/strategy/version's
            # backtest_cache and only 4 distinct fixed_sl values typically exist,
            # so its +/-3 window genuinely spans nearly all of them. Measured on
            # real SOXL/v5/TrailingBoth data: this function's campaign-scoped i3
            # read 666.3% while the true top_safe_nodes-equivalent number was 5.6%
            # -- up to ~119x more optimistic than the real convention it claimed to
            # match, a worse failure direction than the original 18x-pessimistic
            # bug this whole fix was written to close. Fixed: the i3 query now
            # drops the stop_loss half of {scope_sql} and only holds entry_timing
            # fixed (matching top_safe_nodes.py's own scoping exactly), letting
            # "stop_loss BETWEEN" genuinely search neighboring fixed_sl campaigns
            # for every strategy, not just non-uses_fixed_sl ones.
            # Residual, accepted difference (LOW, both reviews flagged, not fixed):
            # top_safe_nodes.best_safe_node() ranks unscoped across ALL campaigns
            # with a min_alpha=200 floor and walks down ranks until a safe one is
            # found -- it can pick a different anchor node than this function's
            # single campaign-scoped top-1. The two numbers are now comparable in
            # NEIGHBORHOOD (same radius convention, same held-fixed axes) but can
            # still legitimately describe different winning nodes.
            if best_alpha > 100:
                CLIFF_RADIUS_I3 = 3
                try:
                    worst_i3 = conn.execute(f"""
                        SELECT MIN({ROBUST_ALPHA_SQL}) FROM backtest_cache
                        WHERE version=? AND ticker=? AND strategy=?
                          AND window=? AND z_score_threshold=?
                          AND axis_tp        BETWEEN ? AND ?
                          AND stop_loss      BETWEEN ? AND ?
                          AND max_hold_hours BETWEEN ? AND ?
                          AND trail_buy_pct  = ?
                          AND trail_sell_pct = ?
                          AND entry_timing   = ?
                          AND trades > 0
                          AND (kernel_version IS NULL OR kernel_version<>'ground_truth_v6')
                    """, (config_version, ticker, strategy_name,
                          win_c, z_c,
                          axis_tp_f - CLIFF_RADIUS_I3, axis_tp_f + CLIFF_RADIUS_I3,
                          literal_sl_c - CLIFF_RADIUS_I3, literal_sl_c + CLIFF_RADIUS_I3,
                          hold_c - 7, hold_c + 7,
                          literal_trail_buy_pct_c,
                          literal_trail_sell_pct_c,
                          entry_timing)).fetchone()[0]
                    # None (not 0.0) on an empty neighbor set -- matches
                    # top_safe_nodes.py's own fail-closed handling (its
                    # pd.notna(worst) guard); a fabricated 0.0 here would read as
                    # "exactly non-cliff" for a case that was never actually
                    # checked (the same CRITICAL-adjacent failure mode as bug (1)
                    # above, caught by the same paired review).
                    worst_neighbor_i3 = float(worst_i3) if worst_i3 is not None else None
                    # Persist the LITERAL columns (fetched above), not tpct_c/sl_c --
                    # a 2nd paired review round (independent-cold + contextual, both
                    # converged) found the first fix only corrected the i3 QUERY, not
                    # this INSERT: tpct_c is hardcoded 0 whenever fourth_axis_col !=
                    # 'trail_pct' (TrailingExit's real case, since trail_sell_pct is
                    # its sl_axis, not its fourth_axis), so every TrailingExit row
                    # still persisted a fabricated trail_sell_pct=0.0/NULL
                    # best_node_trail_buy_pct -- the exact "row can't be mapped back
                    # to its node" CRITICAL this whole re-enable exists to close,
                    # still open on the write side. Also: stop_loss/fixed_sl are only
                    # meaningful for uses_fixed_sl strategies (TrailingBoth/
                    # TrailingExit, where fixed_sl really is a real per-campaign
                    # constant) -- for a future non-uses_fixed_sl strategy through
                    # this path (e.g. plain ZScoreBreakout), fixed_sl doesn't exist as
                    # a real campaign concept and the real SL is literal_sl_c instead
                    # (2nd paired review round, MEDIUM, confirmed latent -- no such
                    # campaign has run through here yet).
                    _uses_fixed_sl = strategies.uses_fixed_sl(strategy_name)
                    conn.execute("""
                        INSERT OR REPLACE INTO sl_sweep_summary
                            (ticker, strategy, version, stop_loss, trail_sell_pct, entry_timing,
                             window, z_score_threshold, fixed_sl,
                             best_alpha, worst_neighbor_alpha, worst_neighbor_alpha_i3,
                             best_node_tp, best_node_hold, best_node_trail_buy_pct,
                             any_cliff_safe, run_timestamp)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (ticker, strategy_name, config_version,
                          fixed_sl if _uses_fixed_sl else literal_sl_c,
                          literal_trail_sell_pct_c, entry_timing,
                          win_c, z_c, fixed_sl if _uses_fixed_sl else None,
                          best_alpha, worst_neighbor, worst_neighbor_i3,
                          axis_tp_f, hold_c,
                          literal_trail_buy_pct_c if sl_axis_col == 'trail_buy_pct' else None,
                          0 if cliff else 1, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                    conn.commit()
                except Exception as e:
                    logger.warning(f"  [{ticker}] failed to persist sl_sweep_summary row: {e}")

    if not results:
        return [], []

    df = pd.DataFrame(results)
    safe = df[df['worst_neighbor'] >= 0].sort_values('best_alpha', ascending=False)

    with sqlite3.connect(DB_PATH) as conn:
        t_df = pd.read_sql("SELECT symbol, index_underlier, avg_vol_10d, last_price FROM tickers", conn)
    t_df = t_df.rename(columns={'symbol': 'ticker'})
    safe = safe.merge(t_df, on='ticker', how='left')
    safe['is_index'] = safe['index_underlier'].notna() & (safe['index_underlier'].astype(str).str.strip() != '')
    safe['max_notional'] = safe['avg_vol_10d'] * safe['last_price'] * 0.01

    before = len(safe)
    safe = safe[safe['best_alpha'] >= 200]
    safe = safe[safe['max_notional'].notna() & (safe['max_notional'] >= 50_000)]
    logger.info(f"Checkpoint2 — filters: {before} cliff-free → {len(safe)} after alpha>=200% and liq>=$50k")

    all_index = safe[safe['is_index']]['ticker'].tolist()
    all_other  = safe[~safe['is_index']]['ticker'].tolist()
    top_other  = all_other[:n_stock]
    rest_other = all_other[n_stock:]

    logger.info(f"Checkpoint2 — Phase3 order: top {n_stock} non-index → all index ({len(all_index)}) → remaining non-index ({len(rest_other)})")
    logger.info(f"  Top non-index: {top_other}")
    logger.info(f"  Index: {all_index}")
    logger.info(f"  Remaining non-index: {rest_other}")
    return top_other, all_index, rest_other


# ── Phase 3: Full mesh ────────────────────────────────────────────────────────

def run_phase3_full(shared_pool, ticker, strategy_name, config_version, hp, spy_bh, asset_bh, run_timestamp, fixed_sl=0, entry_timing='close', run_id=None, start_date=None, end_date=None, min_hold_hours=0):
    trail_pcts = _trail_pcts_for_strategy(strategy_name, hp)
    tasks = [(tp, sl, int(hold), int(w), float(z), float(tpct))
             for z    in hp['z_score_thresholds']
             for w    in hp['windows']
             for tp   in range(1, 31)
             for sl   in range(1, 31)
             for hold in hp['hold_time_caps']
             for tpct in trail_pcts]

    logger.info(f"[{ticker}] Phase3 full mesh: {len(tasks)} tasks (cache skips coarse+island already done)")
    dispatch_parallel_grid(shared_pool, tasks, ticker, strategy_name, config_version,
                           "Phase3-Full", spy_bh, asset_bh, run_timestamp, fixed_sl, entry_timing, run_id=run_id,
                           start_date=start_date, end_date=end_date, min_hold_hours=min_hold_hours)

    scope_sql, scope_params = _campaign_scope_sql(strategy_name, fixed_sl, entry_timing)
    with sqlite3.connect(DB_PATH) as conn:
        pre_phase3 = conn.execute(f"""
            SELECT MAX({ROBUST_ALPHA_SQL}) FROM backtest_cache
            WHERE version=? AND ticker=? AND strategy=? AND trades > 0
              AND phase IN ('Phase1-Coarse','Phase2-Island','Phase2.5-CliffBox') {scope_sql}
        """, (config_version, ticker, strategy_name, *scope_params)).fetchone()[0]
        overall = conn.execute(f"""
            SELECT MAX({ROBUST_ALPHA_SQL}) FROM backtest_cache
            WHERE version=? AND ticker=? AND strategy=? AND trades > 0
              AND (kernel_version IS NULL OR kernel_version<>'ground_truth_v6') {scope_sql}
        """, (config_version, ticker, strategy_name, *scope_params)).fetchone()[0]
    pre_phase3 = float(pre_phase3) if pre_phase3 is not None else None
    overall    = float(overall) if overall is not None else None
    if overall is not None:
        if pre_phase3 is not None:
            improved = overall > pre_phase3
            logger.info(f"  [{ticker}] Phase3 best={overall:+.1f}%  (pre-Phase3 best={pre_phase3:+.1f}%)  "
                        f"{'IMPROVED' if improved else 'no improvement'}")
        else:
            logger.info(f"  [{ticker}] Phase3 best={overall:+.1f}%  (no pre-Phase3 data to compare)")

    # Static heatmap PNG disabled — superseded by the interactive Spatial Topology page,
    # nothing read these files back. Left commented instead of deleted in case that changes.
    # with sqlite3.connect(DB_PATH) as conn:
    #     df = pd.read_sql("""
    #         SELECT take_profit, stop_loss, max_hold_hours, alpha_vs_spy
    #         FROM backtest_cache
    #         WHERE version=? AND ticker=? AND strategy=? AND trades > 0
    #     """, conn, params=(config_version, ticker, strategy_name))
    # if df.empty:
    #     return
    # best_hold = int(df.nlargest(1, 'alpha_vs_spy')['max_hold_hours'].iloc[0])
    # df_plane  = df[df['max_hold_hours'] == best_hold]
    # if len(df_plane) < 2:
    #     return
    # try:
    #     pivot = df_plane.groupby(['stop_loss', 'take_profit'])['alpha_vs_spy'].mean().unstack('take_profit')
    #     plt.figure(figsize=(12, 10))
    #     sns.heatmap(pivot, annot=False, cmap='RdYlGn', cbar_kws={'label': 'Alpha vs SPY %'}, linewidths=0.5)
    #     plt.title(f"{ticker} — {strategy_name} @ {best_hold}h ({config_version})")
    #     out = OPTO_LOG_DIR / f"topology_{ticker}_{strategy_name}.png"
    #     plt.savefig(out, bbox_inches='tight')
    #     plt.close()
    #     logger.info(f"[{ticker}] Heatmap saved: {out}")
    # except Exception as e:
    #     logger.warning(f"[{ticker}] Heatmap failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Three-phase sweep engine")
    parser.add_argument("--phase", type=int, choices=[1, 2, 3], default=None,
                        help="Run only this phase (default: all phases)")
    parser.add_argument("--max-phase", dest="max_phase", default="3", choices=["1", "2", "2.5", "3"],
                        help="Run phases up through this one, then stop (default: 3, full pipeline). "
                             "Island/cliff-safety selection only needs data through Phase 2.5 -- "
                             "'--max-phase 2.5' skips the expensive full-mesh Phase 3 pass entirely.")
    parser.add_argument("--tickers", nargs="+", default=None,
                        help="Override tickers to sweep")
    parser.add_argument("--version", default=None,
                        help="Override version from config.json")
    parser.add_argument("--entry-timing", dest="entry_timing", default=None, choices=["close", "open_check"],
                        help="Override entry_timing from config.json (close=bar-close only, "
                             "open_check=also check bar Open before falling through to Close)")
    parser.add_argument("--skip-cache-refresh", action="store_true",
                        help="Skip dropdown/pivot/cliff-grid cache refresh at end of run "
                             "(useful when chaining multiple versions and refreshing once at the end)")
    parser.add_argument("--start-date", dest="start_date", default=None,
                        help="YYYY-MM-DD -- window the sweep to [start-date, end-date]. Must be given "
                             "together with --end-date. The window is encoded into the version string "
                             "via window_version_suffix() so windowed results never collide with a "
                             "full-history campaign under the same base version.")
    parser.add_argument("--end-date", dest="end_date", default=None,
                        help="YYYY-MM-DD -- see --start-date (both-or-neither).")
    parser.add_argument("--min-hold-hours", dest="min_hold_hours", type=int, default=0,
                        help="Research/backtest-only compliance-hold floor (see backtester.py::"
                             "_simulate_trail_both) -- blocks ALL exits (incl. SL) until this many "
                             "hourly bars have elapsed since entry. Only TrailingBothZScoreBreakout "
                             "supports it (run_backtest_dispatch raises for any other strategy). "
                             "0 (default) reproduces prior kernel behavior exactly and gets no "
                             "version suffix. A nonzero value is encoded into the version string via "
                             "min_hold_version_suffix() so floored results never collide with a "
                             "non-floored campaign under the same base version.")
    args = parser.parse_args()

    if args.min_hold_hours < 0:
        logger.critical("--min-hold-hours must be >= 0.")
        sys.exit(1)

    # Paired session-wrap review (2026-08-17, both independent-cold and
    # contextual) found combining --min-hold-hours with --start-date/--end-date
    # has real gaps: the window suffix gets appended before the minhold suffix,
    # so (a) a bare --start-date/--end-date run against an already-minhold-
    # suffixed --version silently bypasses the minhold resume guard (it only
    # checks when min_hold_hours != 0), and (b) the two flags together hard-
    # crash deep inside Phase 1 with a misleading "you forgot the suffix"
    # message, AND the window resume guard's own $-anchored regex fails to
    # match a version carrying a trailing minhold suffix, defeating ITS resume
    # guard too. Neither suffix helper is designed to compose safely with the
    # other yet -- reject the combination outright rather than let either
    # bypass reach a real sweep.
    if args.min_hold_hours != 0 and (args.start_date is not None or args.end_date is not None):
        logger.critical(
            "--min-hold-hours and --start-date/--end-date cannot be combined -- "
            "window_version_suffix() and min_hold_version_suffix() are not "
            "designed to compose safely yet (see run_optimization_sweep.py's "
            "2026-08-17 comments). Run them as separate campaigns."
        )
        sys.exit(1)

    if (args.start_date is None) != (args.end_date is None):
        logger.critical("--start-date and --end-date must both be given, or neither.")
        sys.exit(1)

    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    run_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    logger.info("=" * 52)
    logger.info("  THREE-PHASE SWEEP ENGINE")
    logger.info("  Phase1: Coarse  |  Phase2: Island  |  Phase3: Full")
    logger.info("=" * 52)

    try:
        with open("config.json") as f:
            config = json.load(f)
    except Exception as e:
        logger.critical(f"Failed to load config.json: {e}")
        sys.exit(1)

    config_version    = args.version or config.get("version", "v1.6")
    start_date        = args.start_date
    end_date          = args.end_date
    # Guard against the mirror-image corruption direction (paired Opus review
    # finding, 2026-08-17): dispatch_parallel_grid's own guard only fires when
    # start_date/end_date are non-None, so running with an already-windowed-looking
    # --version but WITHOUT --start-date/--end-date (a very plausible resume/extend
    # command -- the suffixed version is exactly what shows up in log filenames,
    # sweep_runs rows, and every phase banner, so it's the natural string to copy)
    # would write full-history results straight into that windowed version's cache
    # rows, silently mixing the two -- nothing downstream can tell them apart since
    # every consumer scopes by version alone. Regex anchored to window_version_
    # suffix's exact fixed-width format so an unrelated version containing "-w"
    # for some other reason doesn't false-positive.
    _WINDOW_SUFFIX_RE = re.compile(r'-w\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}$')
    if start_date is None and _WINDOW_SUFFIX_RE.search(config_version):
        logger.critical(
            f"--version {config_version!r} already looks like a windowed version "
            f"(matches window_version_suffix's format) but --start-date/--end-date "
            f"were not given. Running like this would write FULL-HISTORY results "
            f"into what looks like a windowed campaign's cache rows, with nothing "
            f"downstream able to tell them apart. Pass the matching --start-date/"
            f"--end-date, or use a different --version if a full-history run was "
            f"actually intended."
        )
        sys.exit(1)
    if start_date is not None:
        config_version += window_version_suffix(start_date, end_date)
        logger.info(f"Windowed run: [{start_date}, {end_date}] -- version suffixed to {config_version!r}")

    min_hold_hours = args.min_hold_hours
    # Same resume-safety guard as the window regex above, mirrored for min_hold_hours.
    _MINHOLD_SUFFIX_RE = re.compile(r'-minhold\d+$')
    if min_hold_hours == 0 and _MINHOLD_SUFFIX_RE.search(config_version):
        logger.critical(
            f"--version {config_version!r} already looks like a min-hold-floored version "
            f"(matches min_hold_version_suffix's format) but --min-hold-hours was not given "
            f"(or given as 0). Running like this would write NON-FLOORED results into what "
            f"looks like a floored campaign's cache rows, with nothing downstream able to "
            f"tell them apart. Pass the matching --min-hold-hours, or use a different "
            f"--version if a non-floored run was actually intended."
        )
        sys.exit(1)
    if min_hold_hours != 0:
        config_version += min_hold_version_suffix(min_hold_hours)
        logger.info(f"Compliance-hold-floored run: min_hold_hours={min_hold_hours} -- "
                    f"version suffixed to {config_version!r}")

    hp                = config["hyperparameters"]
    max_workers       = config.get("execution", {}).get("max_workers", 6)
    fixed_sl          = config.get("execution", {}).get("fixed_stop_loss", 0)
    entry_timing      = args.entry_timing or config.get("execution", {}).get("entry_timing", "close")
    tickers           = args.tickers or config.get("target_tickers", [])
    strategy_names    = config.get("active_strategies", ["ZScoreBreakout"])
    phase_only        = args.phase
    max_phase         = args.max_phase

    if not tickers:
        logger.error("No tickers in config.")
        sys.exit(1)

    # Paired session-wrap review (2026-08-17, contextual) found this reachable
    # TODAY, not theoretical: run_backtest_dispatch's min_hold_hours guard
    # raises ValueError inside a pool worker, which run_single_backtest_node_
    # isolated's blanket except turns into a SIM_ERROR status per-node -- the
    # whole campaign then completes "successfully" with zero rows written, no
    # non-zero exit code, nothing but a buried per-node warning. Fail fast at
    # startup instead, before any worker is even spun up.
    if args.min_hold_hours != 0:
        for name in strategy_names:
            cls = getattr(strategies, name, None)
            if cls is None or not issubclass(cls, strategies.TrailingBothZScoreBreakout):
                logger.critical(
                    f"--min-hold-hours={args.min_hold_hours} was given but active_strategies "
                    f"includes {name!r}, which doesn't support the compliance-hold floor "
                    f"(only TrailingBothZScoreBreakout does). Without this check the campaign "
                    f"would silently fail every node and complete with zero rows written -- "
                    f"fix config.json's active_strategies or drop --min-hold-hours."
                )
                sys.exit(1)

    # Real methodology bug this session fell into and had to diagnose by hand,
    # 2026-08-17: whenever a swept max_hours_to_hold value is below
    # min_hold_hours, the TIME-exit condition fires early and then sits
    # blocked until the floor -- every such hold_time_caps value collapses
    # into byte-identical trade sets (confirmed empirically: 16 of 20 default
    # grid values tied under a 112h floor). Not a hard error (a campaign
    # deliberately probing this collapse is a legitimate, if unusual, thing to
    # run) -- just a loud warning so it can't happen silently again.
    if args.min_hold_hours != 0:
        low_caps = [h for h in hp.get('hold_time_caps', []) if h < args.min_hold_hours]
        if low_caps:
            logger.warning(
                f"--min-hold-hours={args.min_hold_hours} but hold_time_caps includes "
                f"{len(low_caps)} value(s) below it ({sorted(low_caps)}) -- every one of "
                f"these will collapse into the same degenerate 'wait exactly until the "
                f"floor' behavior (see docs/research_log.md's 2026-08-17 entry). Consider "
                f"restricting hold_time_caps to values >= min_hold_hours."
            )

    init_idempotent_db()

    run_id_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_log_path = OPTO_LOG_DIR / f"sweep_{config_version}_{run_id_tag}.log"

    # tqdm progress bars write directly to stderr, bypassing the logging module —
    # a logging.FileHandler alone misses all of it. Tee raw stdout/stderr into the
    # run log file too so it has everything, not just structured logger.info() calls.
    class _Tee:
        def __init__(self, *streams):
            self._streams = streams
        def write(self, data):
            for s in self._streams:
                s.write(data)
        def flush(self):
            for s in self._streams:
                s.flush()

    run_log_file = open(run_log_path, "a")
    sys.stdout = _Tee(sys.stdout, run_log_file)
    sys.stderr = _Tee(sys.stderr, run_log_file)

    # logging.StreamHandler captured the original sys.stdout object at basicConfig()
    # time (module import) — reassigning sys.stdout above doesn't retarget it, so
    # logger.info() calls still need their own handler to reach the run log file.
    run_file_handler = logging.FileHandler(run_log_path)
    run_file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logging.getLogger().addHandler(run_file_handler)

    run_id = start_sweep_run(config_version, strategy_names, tickers, config, run_log_path)

    def _mark_failed_on_crash(exc_type, exc_value, exc_tb):
        logger.critical("Sweep crashed", exc_info=(exc_type, exc_value, exc_tb))
        update_sweep_run(run_id, status='FAILED',
                          finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                          notes=str(exc_value)[:500])
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    sys.excepthook = _mark_failed_on_crash

    logger.info(f"Version: {config_version} | Tickers: {len(tickers)} | Workers: {max_workers}")
    logger.info(f"Coarse grid: TP/SL {hp['take_profits']} | Hold: {len(hp['hold_time_caps'])} values | Z: {hp['z_score_thresholds']}")

    # Precompute B&H returns once — reused across all phases
    logger.info("Precomputing B&H returns for all tickers...")
    bh_cache = {}
    for ticker in tickers:
        # Windowed compute_bh_returns raises ValueError (not the missing-CSV None,None
        # path) when the window falls outside this ticker's cached span -- e.g. a
        # ticker whose data starts later than an early rolling-window step, or a stale
        # cache. Unguarded, that ValueError propagates out of this loop and aborts the
        # ENTIRE sweep before any phase runs (paired Opus review finding, 2026-08-17,
        # confirmed by both independent reviewers -- real risk for the planned 9-window
        # rolling campaign). Catch specifically ValueError (not a bare except -- a
        # genuinely broken/corrupt CSV should still be loud) and drop the ticker into
        # the same "not in valid_tickers" bucket the missing-cache-file path already
        # uses, instead of crashing the run for every other ticker in scope. The
        # exception message already includes the ticker's real cached span (see
        # compute_bh_returns's own ValueError text), so the warning below carries
        # enough to distinguish "window outside history" from "cache looks stale"
        # without needing to re-derive it here.
        try:
            asset_bh, spy_bh = compute_bh_returns(ticker, start_date, end_date)
        except ValueError as e:
            logger.warning(f"[{ticker}] Skipping (compute_bh_returns): {e}")
            continue
        if asset_bh is not None:
            bh_cache[ticker] = (asset_bh, spy_bh)
    valid_tickers = [t for t in tickers if t in bh_cache]
    logger.info(f"Valid tickers with cache data: {len(valid_tickers)}/{len(tickers)}")

    with ProcessPoolExecutor(max_workers=max_workers, initializer=_warmup_worker) as shared_pool:

        if phase_only != 3:
            # ── Phase 1 ───────────────────────────────────────────────────────
            logger.info(f"\n{'='*52}")
            logger.info(f"PHASE 1 — COARSE SCAN ({len(valid_tickers)} tickers) [{config_version}]")
            logger.info(f"{'='*52}")
            for ticker in valid_tickers:
                for name in strategy_names:
                    if not getattr(strategies, name, None):
                        logger.warning(f"Unknown strategy: {name}")
                        continue
                    asset_bh, spy_bh = bh_cache[ticker]
                    run_phase1_coarse(shared_pool, ticker, name, config_version, hp, spy_bh, asset_bh, run_timestamp, fixed_sl, entry_timing, run_id=run_id, start_date=start_date, end_date=end_date, min_hold_hours=min_hold_hours)

            logger.info("Phase 1 complete.")

        max_generations = config.get("execution", {}).get("max_generations", 1)
        max_generations = max(1, max_generations)

        for name in strategy_names:
            if not getattr(strategies, name, None):
                continue

            if phase_only == 3:
                # Skip directly to Phase 3 using provided tickers
                full_tickers = [t for t in valid_tickers if t in bh_cache]
                logger.info(f"\n{'='*52}")
                logger.info(f"PHASE 3 — FULL MESH ({len(full_tickers)} tickers) [direct] [{config_version}]")
                logger.info(f"{'='*52}")
                for ticker in full_tickers:
                    if ticker not in bh_cache:
                        logger.warning(f"[{ticker}] No B&H data, skipping Phase 3.")
                        continue
                    asset_bh, spy_bh = bh_cache[ticker]
                    run_phase3_full(shared_pool, ticker, name, config_version, hp, spy_bh, asset_bh, run_timestamp, fixed_sl, entry_timing, run_id=run_id, start_date=start_date, end_date=end_date, min_hold_hours=min_hold_hours)
                continue

            island_tickers = []
            for gen in range(max_generations):
                # ── Checkpoint 1 (re-run each generation) ────────────────
                logger.info(f"\nCheckpoint 1 (gen {gen+1}/{max_generations}): ranking results for {name} [{config_version}]...")
                top_index, top_other = identify_island_candidates(config_version, name, 25, 5, allowed_tickers=valid_tickers, fixed_sl=fixed_sl, entry_timing=entry_timing)
                island_tickers = top_index + top_other

                if not island_tickers:
                    logger.warning("No island candidates. Skipping phases 2 & 3.")
                    break

                if phase_only == 1 or max_phase == "1":
                    continue

                # ── Phase 2 ───────────────────────────────────────────────
                logger.info(f"\n{'='*52}")
                logger.info(f"PHASE 2 — ISLAND MESH gen {gen+1}/{max_generations} ({len(island_tickers)} tickers) [{config_version}]")
                logger.info(f"{'='*52}")
                for ticker in island_tickers:
                    if ticker not in bh_cache:
                        logger.warning(f"[{ticker}] No B&H data, skipping Phase 2.")
                        continue
                    asset_bh, spy_bh = bh_cache[ticker]
                    run_phase2_island(shared_pool, ticker, name, config_version, hp, spy_bh, asset_bh, run_timestamp, fixed_sl, entry_timing, generation=gen + 1, run_id=run_id, start_date=start_date, end_date=end_date, min_hold_hours=min_hold_hours)

                logger.info(f"Phase 2 gen {gen+1} complete.")

            if not island_tickers or phase_only in (1, 2) or max_phase in ("1", "2"):
                continue

            # ── Phase 2.5 ─────────────────────────────────────────────────
            logger.info(f"\n{'='*52}")
            logger.info(f"PHASE 2.5 — CLIFF-BOX SWEEP ({len(island_tickers)} tickers) [{config_version}]")
            logger.info(f"{'='*52}")
            for ticker in island_tickers:
                if ticker not in bh_cache:
                    continue
                asset_bh, spy_bh = bh_cache[ticker]
                run_phase25_cliff_box(shared_pool, ticker, name, config_version, hp, spy_bh, asset_bh, run_timestamp, fixed_sl, entry_timing, run_id=run_id, start_date=start_date, end_date=end_date, min_hold_hours=min_hold_hours)

            # ── Checkpoint 2 ─────────────────────────────────────────────
            logger.info(f"\nCheckpoint 2: cliff check on {len(island_tickers)} island tickers [{config_version}]...")
            top_other, all_index, rest_other = identify_full_mesh_candidates(
                config_version, name, island_tickers, 5, 5, fixed_sl=fixed_sl, entry_timing=entry_timing
            )
            # Order: top 5 non-index → all index → remaining non-index
            full_tickers = top_other + all_index + rest_other

            if not full_tickers:
                logger.warning("No cliff-free candidates for Phase 3.")
                continue

            if max_phase in ("1", "2", "2.5"):
                logger.info(f"--max-phase {max_phase}: skipping Phase 3 for {name}.")
                continue

            # ── Phase 3 ───────────────────────────────────────────────────
            logger.info(f"\n{'='*52}")
            logger.info(f"PHASE 3 — FULL MESH ({len(full_tickers)} tickers) [{config_version}]")
            logger.info(f"{'='*52}")
            for ticker in full_tickers:
                if ticker not in bh_cache:
                    logger.warning(f"[{ticker}] No B&H data, skipping Phase 3.")
                    continue
                asset_bh, spy_bh = bh_cache[ticker]
                run_phase3_full(shared_pool, ticker, name, config_version, hp, spy_bh, asset_bh, run_timestamp, fixed_sl, entry_timing, run_id=run_id, start_date=start_date, end_date=end_date, min_hold_hours=min_hold_hours)

    if args.skip_cache_refresh:
        logger.info("\nSkipping cache refresh and index rebuild (--skip-cache-refresh).")
    else:
        logger.info("\nRebuilding indexes...")
        rebuild_indexes()
        logger.info("\nFinal cache refresh...")
        refresh_dropdown_cache()
        refresh_pivot_cache(versions=[config_version])
        try:
            refresh_cliff_grid_cache()
        except Exception as e:
            logger.warning(f"Cliff grid cache refresh failed (page will fall back to live query): {e}")

    for p in ["current_test.json", "active_phase_grid.json"]:
        if os.path.exists(p):
            try: os.remove(p)
            except Exception: pass

    logger.info("=" * 52)
    logger.info("  THREE-PHASE SWEEP COMPLETE")
    logger.info("=" * 52)

    update_sweep_run(run_id, status='COMPLETE',
                      finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                      phase_reached=f"phase{phase_only}" if phase_only else "all")
