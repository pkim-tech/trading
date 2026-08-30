""""v6.2" parity check: does bench_phase1_phase2_inmemory.py's (new, in-memory)
Phase1/2/2.5-GT candidate selection reproduce the same top-9 candidates as the OLD
orchestration (scripts/run_ground_truth_phase1.py -> run_optimization_sweep.py's real
dispatch_parallel_grid_ground_truth / run_phase2_island_ground_truth /
run_phase25_cliff_box_ground_truth, writing to backtest_cache) for the identical
real scope?

Naming: "v6.1" is already taken (docs/plans/ground_truth_kernel_rebuild.md, a parked
strategy-timing variant). This is "v6.2" -- an old-vs-new PIPELINE comparison, not a
kernel/strategy change. See docs/deep_backlog.md's 2026-08-29 entry.

Scope (confirmed via scripts/candidate_nodes_status.py --ticker SOXL): ticker=SOXL,
strategy=TrailingBothZScoreBreakout, fixed_sl=2, window=10, data_source=massive,
[2021-08-23, 2026-08-21]. The NEW pipeline already has 9 candidate_nodes rows for this
exact scope under version='bench-inmemory-v6-massive-w2021-08-23_2026-08-21'
(created_at='2026-08-27T22:46:38'). Checked directly (2026-08-29): backtest_cache
ALREADY has a real OLD-orchestration run for this exact ticker/strategy/fixed_sl/
data_source/date-window under version='v6-massive-w2021-08-23_2026-08-21'
(kernel_version='ground_truth_v6', run_timestamp 2026-08-23 05:59-13:42, 149,160 rows
at window=10 alone -- well beyond the 82,320-cell Phase1-coarse grid, i.e. a real
Phase1->2->2.5-GT chain already ran) -- so NO new sweep was launched for this check;
this script is a pure read-only re-derivation against data that already exists.

This does NOT write to backtest_cache, candidate_nodes, or config.json. Read-only.

Usage: .venv/bin/python scripts/compare_old_new_pipeline_parity_v62.py
"""
import os
import sys
import sqlite3

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from run_optimization_sweep import (
    DB_PATH, FINE_RADIUS, GT_CANDIDATE_TIEBREAK, _campaign_scope_sql,
    _phase1_coarse_gt_status, _sl_axis_real_column, ROBUST_ALPHA_SQL,
    pick_island_centers,
)
from node_key import GT_TRADES_KERNEL_VERSION
import campaign_config
import strategies

TICKER = "SOXL"
STRATEGY = "TrailingBothZScoreBreakout"
FIXED_SL = 2
WINDOW = 10
ENTRY_TIMING = "open_check"
START, END = "2021-08-23", "2026-08-21"

OLD_VERSION = "v6-massive-w2021-08-23_2026-08-21"
NEW_VERSION = "bench-inmemory-v6-massive-w2021-08-23_2026-08-21"

HOLD_TIME_CAPS = [7, 14, 21, 28, 35, 42, 49, 56, 63, 70, 77, 84, 91, 98, 105, 112, 119,
                   126, 133, 140]
Z_THRESHOLDS = [1.0, 1.5, 2.0]


def _hp_window10():
    grid = campaign_config.STRATEGIES[STRATEGY]
    return {
        "windows": [WINDOW],
        "z_score_thresholds": Z_THRESHOLDS,
        "take_profits": grid["take_profits"],
        "stop_losses": grid["stop_losses"],
        "hold_time_caps": HOLD_TIME_CAPS,
        "trail_pcts": grid["trail_pcts"],
    }


def derive_old_top9_window10():
    """Same selection logic as run_optimization_sweep.derive_phase25_candidates_
    ground_truth (island-center detection + top-3-per-island on cagr), but restricted
    to window=10 only (that function has no window filter -- it would otherwise mix
    window=10 and window=20 rows into the same island region, which is not what the
    new pipeline's --window 10 run did), and with the same cross-island-convergence
    fallthrough as bench_phase1_phase2_inmemory.py's final_candidates loop (an island
    whose top pick was already claimed by another island falls through to its own next
    distinct pick, rather than silently losing a slot)."""
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(STRATEGY)
    scope_sql, scope_params = _campaign_scope_sql(STRATEGY, FIXED_SL, ENTRY_TIMING)
    tpct_col = "trail_sell_pct" if fourth_axis_col == "trail_pct" else "0"
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql(f"""
            SELECT axis_tp AS take_profit, {_sl_axis_real_column(sl_axis_col)} AS stop_loss,
                   max_hold_hours, window, z_score_threshold,
                   {tpct_col} AS tpct,
                   {ROBUST_ALPHA_SQL} AS robust_alpha, cagr, trades
            FROM backtest_cache
            WHERE version=? AND ticker=? AND strategy=? AND trades > 0
              AND kernel_version='ground_truth_v6' AND window=? {scope_sql}
        """, conn, params=(OLD_VERSION, TICKER, STRATEGY, WINDOW, *scope_params))
    if df.empty:
        return []

    centers = pick_island_centers(df, rank_col="cagr")
    tb_cols = ["cagr"] + [c for c, _ in GT_CANDIDATE_TIEBREAK]
    tb_asc = [False] + [asc for _, asc in GT_CANDIDATE_TIEBREAK]

    candidates = []
    claimed = {}
    for tp_c, sl_c in centers:
        region = df[(df["take_profit"] - tp_c).abs().le(FINE_RADIUS)
                    & (df["stop_loss"] - sl_c).abs().le(FINE_RADIUS)]
        region = region[region["cagr"].notna()]
        if region.empty:
            continue
        region = region.sort_values(tb_cols, ascending=tb_asc)
        picked = 0
        for _, cand in region.iterrows():
            if picked >= 3:
                break
            key = (int(cand["take_profit"]), int(cand["stop_loss"]), int(cand["max_hold_hours"]),
                   int(cand["window"]), float(cand["z_score_threshold"]), float(cand["tpct"]))
            if key in claimed:
                claimed[key]["converged_from_islands"].append((tp_c, sl_c))
                continue
            c = {
                "island": (tp_c, sl_c), "take_profit": key[0], "stop_loss": key[1],
                "max_hold_hours": key[2], "window": key[3], "z_score_threshold": key[4],
                "trail_sell_pct": key[5], "cagr": float(cand["cagr"]),
                "robust_alpha": float(cand["robust_alpha"]), "trades": int(cand["trades"]),
                "converged_from_islands": [(tp_c, sl_c)],
            }
            claimed[key] = c
            candidates.append(c)
            picked += 1
    return candidates


def load_new_top9_window10():
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT arm_pct AS take_profit, trail_buy_pct AS stop_loss, max_hold_hours,
                   window, z AS z_score_threshold, trail_sell_pct, robust_alpha, trades
            FROM candidate_nodes
            WHERE ticker=? AND version=? AND strategy=? AND fixed_sl=? AND window=?
            ORDER BY robust_alpha DESC
        """, (TICKER, NEW_VERSION, STRATEGY, FIXED_SL, WINDOW)).fetchall()
    cols = ["take_profit", "stop_loss", "max_hold_hours", "window", "z_score_threshold",
            "trail_sell_pct", "robust_alpha", "trades"]
    return [dict(zip(cols, r)) for r in rows]


def lookup_old_cell(key):
    """key = (take_profit, stop_loss, max_hold_hours, window, z_score_threshold, trail_sell_pct).
    Look up this exact cell's OLD-orchestration cagr/trades/alpha_vs_spy from
    backtest_cache (regardless of whether it made the old pipeline's own top-9) -- this
    answers "does the underlying per-cell computation agree", independent of whether
    the two pipelines' SELECTION algorithms agree."""
    tp, sl, hold, w, z, tpct = key
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(STRATEGY)
    tpct_col = "trail_sell_pct" if fourth_axis_col == "trail_pct" else None
    scope_sql, scope_params = _campaign_scope_sql(STRATEGY, FIXED_SL, ENTRY_TIMING)
    with sqlite3.connect(DB_PATH) as conn:
        sql = f"""
            SELECT cagr, trades, alpha_vs_spy, {ROBUST_ALPHA_SQL} AS robust_alpha
            FROM backtest_cache
            WHERE version=? AND ticker=? AND strategy=? AND kernel_version='ground_truth_v6'
              AND axis_tp=? AND {_sl_axis_real_column(sl_axis_col)}=? AND max_hold_hours=?
              AND window=? AND z_score_threshold=? {scope_sql}
        """
        params = [OLD_VERSION, TICKER, STRATEGY, tp, sl, hold, w, z, *scope_params]
        if tpct_col:
            sql += f" AND {tpct_col}=?"
            params.append(tpct)
        rows = conn.execute(sql, params).fetchall()
        if len(rows) > 1:
            print(f"  WARNING: {len(rows)} backtest_cache rows for key={key} under this exact "
                  f"scope (expected exactly 1) -- PK collision or duplicate data, using first.")
        row = rows[0] if rows else None
    if row is None:
        return None
    return {"cagr": row[0], "trades": row[1], "alpha_vs_spy": row[2], "robust_alpha": row[3]}


def key_of(c):
    return (c["take_profit"], c["stop_loss"], c["max_hold_hours"], c["window"],
            c["z_score_threshold"], c["trail_sell_pct"])


def main():
    print("=" * 100)
    print(f"v6.2 parity check: OLD orchestration vs NEW in-memory pipeline")
    print(f"Scope: {TICKER} / {STRATEGY} / fixed_sl={FIXED_SL} / window={WINDOW} / "
          f"data_source=massive / [{START}, {END}]")
    print(f"OLD version={OLD_VERSION!r}  NEW version={NEW_VERSION!r}")
    print(f"GT_TRADES_KERNEL_VERSION (current code) = {GT_TRADES_KERNEL_VERSION!r}")
    print("=" * 100)

    hp = _hp_window10()
    done, expected = _phase1_coarse_gt_status(TICKER, STRATEGY, OLD_VERSION, hp, ENTRY_TIMING, FIXED_SL)
    print(f"\nOLD-orchestration Phase1-Coarse-GT completeness (window=10 subgrid): "
          f"{done:,}/{expected:,} ({100*done/max(expected,1):.1f}%)")
    if done < expected:
        print("WARNING: incomplete Phase1 grid for this scope -- island detection below "
              "may be working off a partial search space. Proceeding anyway (read-only).")

    old_candidates = derive_old_top9_window10()
    new_candidates = load_new_top9_window10()

    print(f"\nOLD orchestration derived {len(old_candidates)} candidates "
          f"(re-derived read-only via derive_phase25_candidates_ground_truth's own logic, "
          f"window-restricted)")
    print(f"NEW pipeline has {len(new_candidates)} candidate_nodes rows on file")

    old_sorted = sorted(old_candidates, key=lambda c: -c["cagr"])
    print("\n--- OLD orchestration top candidates (ranked by cagr) ---")
    for i, c in enumerate(old_sorted, 1):
        print(f"  #{i} TP={c['take_profit']} SL={c['stop_loss']} hold={c['max_hold_hours']}h "
              f"z={c['z_score_threshold']} tpct={c['trail_sell_pct']} "
              f"-> cagr={c['cagr']:.2f}% trades={c['trades']} robust_alpha={c['robust_alpha']:.1f}")

    new_sorted = sorted(new_candidates, key=lambda c: -c["robust_alpha"])
    print("\n--- NEW pipeline top candidates (ranked by robust_alpha, no cagr stored "
          "in candidate_nodes) ---")
    for i, c in enumerate(new_sorted, 1):
        print(f"  #{i} TP={c['take_profit']} SL={c['stop_loss']} hold={c['max_hold_hours']}h "
              f"z={c['z_score_threshold']} tpct={c['trail_sell_pct']} "
              f"robust_alpha={c['robust_alpha']:.1f} trades={c['trades']}")

    old_keys = {key_of(c) for c in old_candidates}
    new_keys = {key_of(c) for c in new_candidates}
    common = old_keys & new_keys
    only_old = old_keys - new_keys
    only_new = new_keys - old_keys

    print(f"\n--- Param-set diff ---")
    print(f"Common to both top-N sets: {len(common)}/{max(len(old_keys), len(new_keys))}")
    if only_old:
        print(f"Only in OLD top-{len(old_candidates)}: {sorted(only_old)}")
    if only_new:
        print(f"Only in NEW top-{len(new_candidates)}: {sorted(only_new)}")

    print(f"\n--- Per-cell cross-check: for each NEW-pipeline candidate, does OLD "
          f"orchestration's OWN backtest_cache row for that exact cell agree on cagr? ---")
    mismatches = []
    for c in new_sorted:
        k = key_of(c)
        old_cell = lookup_old_cell(k)
        if old_cell is None:
            print(f"  TP={k[0]} SL={k[1]} hold={k[2]}h z={k[4]} tpct={k[5]}: "
                  f"NOT FOUND in OLD backtest_cache at all (off-grid or off-scope cell)")
            mismatches.append((k, "missing"))
            continue
        alpha_diff = abs(old_cell["robust_alpha"] - c["robust_alpha"])
        alpha_rel_diff = alpha_diff / max(abs(old_cell["robust_alpha"]), 1.0)
        trades_match = old_cell["trades"] == c["trades"]
        # Reasonable float tolerance (not exact-bitwise): 0.5% relative OR 1.0 absolute,
        # whichever is looser -- these are large robust_alpha magnitudes (hundreds to
        # thousands), so a fixed small absolute threshold produces false MISMATCHes on
        # genuinely-immaterial float noise.
        status = "OK" if (trades_match and (alpha_rel_diff < 0.005 or alpha_diff < 1.0)) else "MISMATCH"
        print(f"  TP={k[0]} SL={k[1]} hold={k[2]}h z={k[4]} tpct={k[5]}: "
              f"OLD cagr={old_cell['cagr']:.2f}% trades={old_cell['trades']} "
              f"robust_alpha={old_cell['robust_alpha']:.1f}  |  "
              f"NEW robust_alpha={c['robust_alpha']:.1f} trades={c['trades']}  -> {status}")
        if status == "MISMATCH":
            mismatches.append((k, "value_mismatch"))

    print("\n" + "=" * 100)
    if not only_old and not only_new and not mismatches:
        print("VERDICT: FULL PARITY -- same param set, same per-cell values.")
    elif not mismatches and (only_old or only_new):
        print("VERDICT: per-cell VALUES agree everywhere they overlap, but the two "
              "pipelines' SELECTION (which 9 cells make the cut) diverges -- see "
              "param-set diff above. Likely a selection-algorithm difference (island "
              "centers/tiebreak/cross-island convergence handling), not a kernel/data bug.")
    else:
        print("VERDICT: REAL per-cell divergence found -- see mismatches above. This "
              "would need investigation in whichever pipeline computed the disagreeing "
              "cell (do NOT patch bench_phase1_phase2_inmemory.py / "
              "run_optimization_sweep.py without a paired review, per CLAUDE.md's "
              "Review-Gate Persistence Rule).")
    print("=" * 100)


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
