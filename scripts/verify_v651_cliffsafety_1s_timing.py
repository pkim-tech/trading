"""Real timing measurement + tool (2026-09-07): re-verify cliff-safety (worst_neighbor_
cagr) at 1s resolution for a ticker's real v6.5.1 final candidate population, reusing the
already-committed kernel wiring (bench_phase1_phase2_inmemory.py's Phase2.5 cliffbox
dispatch, fill_resolution='second') rather than the standalone PoC script -- this is what
a real production re-verification run would actually use. Read-only by default: reports
timing/throughput and whether any candidate's worst_neighbor_cagr verdict would change,
does NOT write to candidate_nodes unless --persist is passed.

IMPORTANT, corrected 2026-09-07 (round 2, real bug found running this against 12 more
tickers): `candidate_nodes.generation` is NOT a "is this a final candidate" flag -- it
records which Phase2-island mesh-refinement pass FIRST discovered a winning cell's own
coordinates (1-indexed; None for a Phase1-only/backfilled/pre-generation-column cell).
Every row candidate_nodes ever holds for a (ticker, version, strategy, fixed_sl) scope
comes from exactly ONE real `_insert_candidate_nodes_rows` write (confirmed by a single
shared `created_at` per scope) -- ALL of them are real final candidates, not just the
ones stamped generation=3. Filtering to generation=3 alone (the first version of this
script did this, and so did the request that led to it) silently keeps only ~1-17% of
the real population per ticker (checked across all 14 v6.5.1 tickers, 2026-09-07:
generation=3-only counts ranged 0-168 per ticker vs real totals of 855-1031) -- UGL's
real population is a real, complete 855-row final set; it just happens that NONE of its
winning cells were first-discovered specifically in generation 3 (its Phase2 mesh DID
reach generation 3 for several scopes per backtest_phase2_insurance, the final winners
just all trace back to generation 1/2 discovery -- a benign coincidence of this
attribute, not a stalled/incomplete campaign). Default behavior now tests the REAL full
population (no generation filter) -- pass --generation to reproduce the cheaper,
intentionally-partial original scope if that's ever actually what's wanted.

Groups a ticker's real candidate_nodes rows by (strategy, fixed_sl, entry_timing) -- each
such group is one real Phase2.5 "scope" (own TRAIL_PCTS grid, own ENTRY_TIMING) -- reverse-
maps each row's strategy-specific arm_pct/trail_buy_pct/trail_sell_pct columns back to the
generic (take_profit, stop_loss, max_hold_hours, window, z_score_threshold, trail_sell_pct)
axis tuple bench's own cliffbox_tasks_for_cell/worst_neighbor_cagr logic expects (same
sl_axis_col/fourth_axis_col branches as _insert_candidate_nodes_rows' forward mapping --
see that function's own docstring), further sub-groups each group by (window, z) and
dispatches ONE (window, z) at a time (2026-09-07 fix -- a flat dispatch over a group's
full cliffbox union interleaves cells across every distinct (window, z) pair in that
group, defeating _NODE_INPUT_CACHE_GT's tightened 2-entry second-resolution cap and
forcing constant cache-miss reloads; measured effect on a real 24-candidate/11-(window,z)
GDXU group: throughput collapsed from 65-180 cells/sec to 1.35 cells/sec and the
resulting disk I/O correlated with two real low-memory kills before this fix), via the
real bench._dispatch(..., fill_resolution='second').

Usage:
  .venv/bin/python scripts/verify_v651_cliffsafety_1s_timing.py --ticker GDXU --workers 8
  .venv/bin/python scripts/verify_v651_cliffsafety_1s_timing.py --ticker SOXL --workers 8 --persist
  .venv/bin/python scripts/verify_v651_cliffsafety_1s_timing.py --ticker UGL --generation 3
"""
import argparse
import os
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import pandas as pd

import strategies
import campaign_config
from run_optimization_sweep import DB_PATH, CLIFF_RADIUS
import bench_phase1_phase2_inmemory as bench


def _reverse_map_candidate_row(row, strategy_name):
    """arm_pct = generic tp always. sl_axis_col branch mirrors
    _insert_candidate_nodes_rows' forward mapping, read backwards (same logic
    bench._reverse_map_generic_task uses for watch_list rows -- this is the
    candidate_nodes-column-naming equivalent, since candidate_nodes always stores
    arm_pct/trail_buy_pct/trail_sell_pct directly, unlike watch_list's flat columns)."""
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)
    generic_tp = float(row["arm_pct"])
    if sl_axis_col == 'trail_buy_pct':
        generic_sl = float(row["trail_buy_pct"])
        generic_tpct = float(row["trail_sell_pct"]) if fourth_axis_col == 'trail_pct' else 0.0
    elif sl_axis_col == 'trail_pct':
        generic_sl = float(row["trail_sell_pct"])
        generic_tpct = 0.0
    else:
        generic_sl = float(row["stop_loss"]) if "stop_loss" in row.keys() else 0.0
        generic_tpct = 0.0
    return {
        "id": row["id"], "take_profit": int(round(generic_tp)),
        "stop_loss": int(round(generic_sl)), "max_hold_hours": int(row["max_hold_hours"]),
        "window": int(row["window"]), "z_score_threshold": float(row["z"]),
        "trail_sell_pct": generic_tpct,
        "stored_worst_neighbor_cagr": row["worst_neighbor_cagr"],
    }


def run_ticker(ticker, workers, persist, generation=None):
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    conn.row_factory = sqlite3.Row
    query = """
        SELECT id, strategy, fixed_sl, entry_timing, window, z, arm_pct, trail_buy_pct,
               trail_sell_pct, max_hold_hours, worst_neighbor_cagr
        FROM candidate_nodes
        WHERE ticker=? AND version LIKE 'v6.5.1%'
    """
    params = [ticker]
    if generation is not None:
        query += " AND generation=?"
        params.append(generation)
    rows = conn.execute(query, params).fetchall()
    scope_desc = f"generation={generation}" if generation is not None else "ALL generations (real full population)"
    if not rows:
        print(f"{ticker}: 0 real v6.5.1 candidates found ({scope_desc}) -- nothing to verify.")
        return

    groups = {}
    for r in rows:
        key = (r["strategy"], float(r["fixed_sl"]), r["entry_timing"])
        groups.setdefault(key, []).append(_reverse_map_candidate_row(r, r["strategy"]))

    print(f"{ticker}: {len(rows)} real candidates ({scope_desc}) across {len(groups)} "
          f"(strategy, fixed_sl, entry_timing) group(s).")

    bench.TICKER = ticker
    bench.DATA_SOURCE = "massive"

    total_t0 = time.time()
    total_cells = 0
    all_verdicts = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for (strategy_name, fixed_sl, entry_timing), cands in groups.items():
            bench.ENTRY_TIMING = entry_timing
            trail_pcts_grid = campaign_config.STRATEGIES[strategy_name]["trail_pcts"]
            trail_pcts = bench._trail_pcts_for_strategy(strategy_name, {"trail_pcts": trail_pcts_grid})

            # Sub-grouped by (window, z), dispatched ONE (window, z) at a time (2026-09-07,
            # real finding from GDXU's TrailingExitZScoreBreakout/fixed_sl=1.0 group: 24
            # candidates spanned 11 distinct (window, z) pairs -- a single flat dispatch
            # over the UNION of all candidates' cliffboxes interleaves cells across all 11
            # keys, and _NODE_INPUT_CACHE_GT's second-resolution cap of 2 (a real,
            # deliberate memory-safety fix, NOT to be loosened here -- see run_optimization_
            # sweep.py's own comment) means most cells miss the cache and force a fresh
            # db_cache.get_massive_hourly_ohlcv() + prep_minute_inputs() recompute. Measured
            # real effect: throughput collapsed from 65-180 cells/sec (the earlier groups,
            # each a single (window, z)) to 1.35 cells/sec on this group, and the resulting
            # sustained heavy disk I/O correlated with a real low-memory kill mid-run (page
            # cache growth the harness's monitor treated as a crisis). This is a calling-
            # script fix, not a kernel-wiring fix -- the committed dispatch/cache code is
            # unchanged; this script just stops asking it to thrash.
            tasks_by_wz = {}
            for c in cands:
                key = (c["window"], c["z_score_threshold"])
                tasks_by_wz.setdefault(key, set())
                tasks_by_wz[key] |= bench.cliffbox_tasks_for_cell(c, trail_pcts)

            group_rows = []
            t0 = time.time()
            for (w, z), wz_tasks in sorted(tasks_by_wz.items()):
                wz_t0 = time.time()
                wz_rows = bench._dispatch(
                    pool, wz_tasks, ticker, strategy_name, "verify-v651-1s-timing", fixed_sl,
                    0.0, desc=f"{ticker} {strategy_name} sl={fixed_sl} w={w} z={z}",
                    fill_resolution="second")
                wz_t1 = time.time()
                group_rows.extend(wz_rows)
                print(f"    (window={w}, z={z}): {len(wz_tasks)} cell(s), {len(wz_rows)} "
                      f"succeeded in {wz_t1 - wz_t0:.1f}s "
                      f"({len(wz_tasks) / max(wz_t1 - wz_t0, 0.001):.2f} cells/sec)")
            t1 = time.time()
            tasks = set().union(*tasks_by_wz.values()) if tasks_by_wz else set()
            total_cells += len(tasks)
            print(f"  group strategy={strategy_name} fixed_sl={fixed_sl} entry_timing={entry_timing}: "
                  f"{len(cands)} candidate(s), {len(tasks_by_wz)} distinct (window,z) pair(s), "
                  f"{len(tasks)} cliffbox cell(s) dispatched, {len(group_rows)} succeeded in "
                  f"{t1 - t0:.1f}s ({len(tasks) / max(t1 - t0, 0.001):.2f} cells/sec)")

            df = pd.DataFrame(group_rows)
            df = df[df["trades"] > 0] if not df.empty else df
            for c in cands:
                if df.empty:
                    new_wnc, n_neighbors = None, 0
                else:
                    neighbors = df[
                        (df["take_profit"] - c["take_profit"]).abs().le(CLIFF_RADIUS)
                        & (df["stop_loss"] - c["stop_loss"]).abs().le(CLIFF_RADIUS)
                        & (df["max_hold_hours"] == c["max_hold_hours"])
                        & (df["window"] == c["window"])
                        & (df["z_score_threshold"] == c["z_score_threshold"])
                        & (df["trail_sell_pct"] == c["trail_sell_pct"])
                    ]
                    new_wnc = float(neighbors["cagr"].min()) if not neighbors.empty else None
                    n_neighbors = len(neighbors)
                # Sign-crossing check, NOT a core_safe recompute: candidate_nodes.core_safe
                # is a separate Phase4-derived column (phase4_results.core_safe, a fuller
                # checklist verdict, not just this neighbor check) and is NULL for every
                # real gen=3 v6.5.1 row checked here -- inventing a core_safe formula from
                # worst_neighbor_cagr alone would not match Phase4's real derivation, so
                # this only reports whether worst_neighbor_cagr itself crosses the same
                # 0-threshold PHASE25_ISLAND_CLIFFBOX_CAGR_MIN already uses elsewhere in
                # this file for cliff detection -- a real signal, not the full core_safe verdict.
                stored = c["stored_worst_neighbor_cagr"]
                sign_crossed = (stored is not None and new_wnc is not None
                                and (stored > 0) != (new_wnc > 0))
                changed = (stored is None or new_wnc is None
                           or abs(new_wnc - stored) > 0.01)
                all_verdicts.append({
                    "id": c["id"], "strategy": strategy_name, "fixed_sl": fixed_sl,
                    "stored_wnc": stored, "new_wnc": new_wnc,
                    "n_neighbors": n_neighbors, "changed": changed, "sign_crossed": sign_crossed,
                })
    total_t1 = time.time()

    print(f"\n{ticker} TOTAL: {total_t1 - total_t0:.1f}s wall-clock, {total_cells:,} cliffbox "
          f"cell(s) dispatched across {len(groups)} group(s) "
          f"({total_cells / max(total_t1 - total_t0, 0.001):.2f} cells/sec, "
          f"{(total_t1 - total_t0) / len(rows):.2f}s/candidate averaged over {len(rows)} candidates)")

    n_changed = sum(1 for v in all_verdicts if v["changed"])
    n_sign_crossed = sum(1 for v in all_verdicts if v["sign_crossed"])
    print(f"{ticker}: {n_changed} of {len(all_verdicts)} candidate(s) show a numeric "
          f"worst_neighbor_cagr change (>0.01pp) vs stored value; {n_sign_crossed} cross "
          f"the 0-threshold (a real cliff-safety verdict change):")
    for v in all_verdicts:
        if v["changed"]:
            flag = " *** SIGN CROSSED ***" if v["sign_crossed"] else ""
            print(f"  id={v['id']} strategy={v['strategy']} fixed_sl={v['fixed_sl']}: "
                  f"stored_wnc={v['stored_wnc']} -> new_wnc={v['new_wnc']} (n={v['n_neighbors']}){flag}")

    if persist:
        with sqlite3.connect(DB_PATH, timeout=60.0) as wconn:
            for v in all_verdicts:
                wconn.execute(
                    "UPDATE candidate_nodes SET worst_neighbor_cagr=? WHERE id=?",
                    (v["new_wnc"], v["id"]))
            wconn.commit()
        print(f"{ticker}: PERSISTED refreshed worst_neighbor_cagr for "
              f"{len(all_verdicts)} candidate_nodes row(s) -- this was a real data write, "
              f"not just a timing test.")
    else:
        print(f"{ticker}: read-only run, nothing written to candidate_nodes.")


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--persist", action="store_true",
                     help="write refreshed worst_neighbor_cagr back to candidate_nodes")
    ap.add_argument("--generation", type=int, default=None,
                     help="restrict to this generation value only (see module docstring's "
                          "2026-09-07 correction for why this is NOT 'final candidates only' "
                          "-- default is the real full population, no generation filter)")
    args = ap.parse_args()
    run_ticker(args.ticker, args.workers, args.persist, generation=args.generation)
