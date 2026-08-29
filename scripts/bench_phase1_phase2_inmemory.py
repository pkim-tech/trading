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
from node_key import node_key
import campaign_config
import strategies

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
        buffer.append((now_iso, ticker, strategy_name, config_version, c["window"],
                       c["z_score_threshold"], float(fixed_sl), arm_pct, trail_buy_pct,
                       trail_sell_pct, c["max_hold_hours"], entry_timing,
                       c["alpha_vs_spy"], c["trades"], now_iso))

    with sqlite3.connect(DB_PATH, timeout=60.0) as conn:
        before = conn.execute(
            "SELECT COUNT(*) FROM candidate_nodes WHERE version=? AND ticker=? AND strategy=?",
            (config_version, ticker, strategy_name)).fetchone()[0]
        conn.executemany("""
            INSERT OR IGNORE INTO candidate_nodes
                (created_at, ticker, strategy, version, window, z, fixed_sl, arm_pct,
                 trail_buy_pct, trail_sell_pct, max_hold_hours, entry_timing,
                 robust_alpha, trades, robust_alpha_computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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


def _insert_winner_trades_rows(winner_trades_by_key, node_keys_by_key, strategy_name,
                                config_version, ticker, fixed_sl):
    """New table (not yet in real production) -- one row per real trade, keyed by
    node_key (stable across re-sweeps) + version (disambiguates which data window this
    specific trade sequence came from, since node_key deliberately does NOT encode that
    -- same reasoning candidate_nodes already uses its own separate version column
    rather than baking it into any identity)."""
    with sqlite3.connect(DB_PATH, timeout=60.0) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS backtest_winner_trades (
                node_key TEXT, version TEXT, ticker TEXT, strategy TEXT, fixed_sl REAL,
                trade_idx INTEGER, entry_time TEXT, entry_price REAL, exit_time TEXT,
                exit_price REAL, exit_reason TEXT, return_pct REAL, armed INTEGER,
                arm_time TEXT, arm_price REAL, created_at TEXT,
                UNIQUE(node_key, version, trade_idx)
            )""")
        before = conn.execute(
            "SELECT COUNT(*) FROM backtest_winner_trades WHERE version=? AND ticker=? AND strategy=?",
            (config_version, ticker, strategy_name)).fetchone()[0]
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
        buffer = []
        for key, trades in winner_trades_by_key.items():
            nk = node_keys_by_key[key]
            for i, t in enumerate(trades):
                buffer.append((nk, config_version, ticker, strategy_name, float(fixed_sl), i,
                               str(t['Entry Time']), t['Entry Price'], str(t['Exit Time']),
                               t['Exit Price'], t['exit_reason'], t['Return'], int(t['armed']),
                               str(t['Arm Time']) if t['armed'] else None, t['Arm Price'], now_iso))
        conn.executemany("""
            INSERT OR IGNORE INTO backtest_winner_trades
                (node_key, version, ticker, strategy, fixed_sl, trade_idx, entry_time,
                 entry_price, exit_time, exit_price, exit_reason, return_pct, armed,
                 arm_time, arm_price, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, buffer)
        conn.commit()
        after = conn.execute(
            "SELECT COUNT(*) FROM backtest_winner_trades WHERE version=? AND ticker=? AND strategy=?",
            (config_version, ticker, strategy_name)).fetchone()[0]
    actually_inserted = after - before
    if actually_inserted < len(buffer):
        print(f"  ({len(buffer) - actually_inserted} of {len(buffer)} trade rows already existed "
              f"-- skipped via INSERT OR IGNORE, not overwritten)")
    return actually_inserted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--strategy", choices=sorted(campaign_config.STRATEGIES),
                     help="explicit strategy, used INSTEAD of load_live_node(TICKER)'s")
    ap.add_argument("--fixed-sl", dest="fixed_sl", type=int,
                     help="explicit fixed_sl, used INSTEAD of load_live_node(TICKER)'s")
    ap.add_argument("--window", type=int, default=None,
                     help="run this single window value in ISOLATION instead of the "
                          "real production grid (module-level WINDOWS=[10,20]) -- e.g. "
                          "--window 15 to explore a midpoint value not otherwise swept. "
                          "Does not add to the standard grid, replaces it for this run.")
    ap.add_argument("--resume-from-top100", action="store_true",
                     help="skip Phase1 dispatch entirely; load df1 from the persisted "
                          "top-100 Phase1-Coarse-GT snapshot in backtest_cache instead. "
                          "Deliberately tests the correctness risk already flagged in "
                          "design discussion: pick_island_centers walks the FULL ranked "
                          "grid by design (a pre-filtered top-100 subset can miss a real "
                          "island entirely) -- this compares against the full-grid run's "
                          "own 9 final candidates to see how much it actually diverges.")
    ap.add_argument("--checkpoint-file", default=None,
                     help="local dev-iteration checkpoint (parquet, NOT a production "
                          "artifact) for Phase1+Phase2's combined df_full. If it exists, "
                          "skips Phase1 AND Phase2 entirely and loads df_full from it -- "
                          "for iterating on Phase2.5 logic without repaying the ~7min "
                          "Phase1+Phase2 cost each time. If missing, computes normally "
                          "and saves it after Phase2 finishes. Default: "
                          "<job-tmp>/bench_phase12_checkpoint_<strategy>_<fixed_sl>.parquet "
                          "(mutually exclusive with --resume-from-top100).")
    args = ap.parse_args()
    if args.resume_from_top100 and args.checkpoint_file:
        raise SystemExit("--resume-from-top100 and --checkpoint-file are mutually exclusive "
                          "(one tests a narrow top-100-only dataset, the other a full "
                          "Phase1+Phase2 checkpoint) -- pick one.")

    if args.window is not None:
        global WINDOWS
        WINDOWS = [args.window]
        print(f"Window override: running window={args.window} in ISOLATION "
              f"(replaces standard grid {[10, 20]})")

    if args.strategy is not None:
        strategy_name = args.strategy
        fixed_sl = args.fixed_sl
        if fixed_sl is None:
            raise SystemExit("--strategy requires --fixed-sl too (no live-node lookup in this mode)")
        print(f"Override mode: strategy={strategy_name}, fixed_sl={fixed_sl} (no live-node lookup)")
    else:
        node = load_live_node(TICKER)
        strategy_name = node["strategy"]
        fixed_sl = node["fixed_sl"]
        print(f"Live node: strategy={strategy_name}, fixed_sl={fixed_sl}")

    grid = campaign_config.STRATEGIES[strategy_name]
    TAKE_PROFITS = grid["take_profits"]
    STOP_LOSSES = grid["stop_losses"]
    TRAIL_PCTS = _trail_pcts_for_strategy(strategy_name, grid)

    version = "bench-inmemory-v6" + ("-massive" if DATA_SOURCE == "massive" else "") + window_version_suffix(START, END)

    _job_tmp = os.path.join(os.environ["CLAUDE_JOB_DIR"], "tmp") if "CLAUDE_JOB_DIR" in os.environ else "/tmp"
    # Keyed on WINDOWS too (not just strategy/fixed_sl) -- found live 2026-08-29: a
    # --window override run silently loaded a stale checkpoint from an earlier
    # standard-grid ([10,20]) run under the same strategy/fixed_sl, skipping Phase1+2
    # entirely and never actually computing the overridden window at all. The checkpoint
    # itself is explicitly documented as a "dev-iteration" convenience, not a production
    # artifact -- this key just makes that convenience safe to use across different grids.
    _windows_key = "-".join(str(w) for w in WINDOWS)
    checkpoint_path = args.checkpoint_file or os.path.join(
        _job_tmp, f"bench_phase12_checkpoint_{strategy_name}_{fixed_sl}_w{_windows_key}.parquet")

    asset_bh, spy_bh = compute_bh_returns(TICKER, start_date=START, end_date=END, data_source=DATA_SOURCE)
    if spy_bh is None:
        raise SystemExit(f"compute_bh_returns returned None for {TICKER}/{DATA_SOURCE} -- no derived build.")

    phase1_tasks = [(int(tp), int(sl), int(hold), int(w), float(z), float(tpct))
                     for z in Z_THRESHOLDS for w in WINDOWS
                     for tp in TAKE_PROFITS for sl in STOP_LOSSES
                     for hold in HOLD_TIME_CAPS for tpct in TRAIL_PCTS]

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
      if not args.resume_from_top100 and os.path.exists(checkpoint_path):
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

            df1 = pd.DataFrame(phase1_rows)
            df1 = df1[df1["trades"] > 0]

        # Sanity check: fewer than 100 successful Phase1 cells out of 164,640 means
        # something is badly wrong upstream (data load failure, wrong ticker/window,
        # near-total SIM_ERROR/EMPTY rate) -- not a legitimate sparse-grid outcome.
        if len(df1) < 100:
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
        os.makedirs(os.path.dirname(checkpoint_path), exist_ok=True)
        df_full.to_parquet(checkpoint_path)
        print(f"Checkpoint saved: {checkpoint_path} ({len(df_full):,} rows)")

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
    n_trade_rows_written = _insert_winner_trades_rows(
        winner_trades, node_keys_by_key, strategy_name, version, TICKER, fixed_sl)
    t_ins7 = time.time()
    print(f"Trade rows written to backtest_winner_trades: {n_trade_rows_written} "
          f"in {t_ins7 - t_ins6:.2f}s")


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
