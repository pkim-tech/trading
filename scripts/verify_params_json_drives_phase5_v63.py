"""v6.3 thread, Phase5 leg (2026-08-29): proves candidate_nodes.params_json can drive
Phase5 (scripts/phase5_second_level_overlay_check.py -- the second-level 1s-vs-1m
overlay precision check) to an IDENTICAL result as the existing flat-column path, same
method as the already-done Phase4 leg (scripts/verify_params_json_drives_report_v64.py).

Method: pick one real scope with multiple candidate_nodes rows carrying non-null
params_json (default: AGQ/TrailingBothZScoreBreakout/fixed_sl=3/window=10, version
bench-inmemory-v6-massive-w2026-06-01_2026-08-21, 9 candidates -- picked over the SOXL
alternative purely for 1-second-CSV size: AGQ's is ~194MB vs SOXL's ~2.7GB, same proof
either way since both are real rows with non-null params_json). Build the candidates
list TWICE:

  1. "flat" -- phase4_candidate_nodes_resolver.derive_phase25_candidates_from_candidate_
     nodes(..., window=W, full_population=True) -- the exact call Phase5's own run_scope()
     makes for a candidate_nodes-fallback scope (see that file's main(), the `window is
     not None` branch). full_population=True is used deliberately (not the island-capped
     default) because that's what Phase5's real call path actually uses now (2026-08-29
     Phase3-retirement widening, see that function's own docstring).
  2. "json" -- this script's own derive_json_candidates_full_population, sourcing
     take_profit/stop_loss/tpct/window/z_score_threshold/max_hold_hours from
     json.loads(row.params_json) instead of flat columns, same field mapping as
     verify_params_json_drives_report_v64.derive_candidates_from_params_json (copied,
     not imported -- that script's version does island-clustering, this one mirrors the
     full_population branch instead: no clustering, sorted-only, each row is its own
     island).

Then for EACH candidate in both lists: build the Phase5 node dict via phase5_second_
level_overlay_check.node_from_candidate (same axis-column mapping Phase5's real run_scope
uses), run the real GT kernel at both 1m and 1s resolution via phase5_second_level_
overlay_check._run_gt_kernel + .overlay_cagrs (same functions run_scope/_check_candidate_
core call), and diff every output field (core/addon/drought/core_both CAGR at both
resolutions + deltas) between the flat-driven and json-driven run for the same candidate.
A match proves params_json is sufficient to drive Phase5 to an identical result; any
mismatch or KeyError building the JSON-sourced list is a real, reportable gap.

Deliberately does NOT call phase5_second_level_overlay_check.run_scope/main (which
persists to candidate_verification_results/phase5_trades) -- this script never writes to
the DB, read-only proof-of-concept only, same posture as verify_params_json_drives_
report_v64.py. Does NOT touch run_optimization_sweep.py or any hard-gated file for real,
only imports from it (read-only).

Usage:
  .venv/bin/python scripts/verify_params_json_drives_phase5_v63.py [--db PATH]
      [--ticker AGQ --strategy TrailingBothZScoreBreakout
       --version bench-inmemory-v6-massive-w2026-06-01_2026-08-21
       --fixed-sl 3.0 --entry-timing open_check --window 10]
"""
import argparse
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import strategies
from prune_backtest_cache_ground_truth import _hp_for_strategy
from candidate_summary_report import _window_dates_from_version
from phase4_candidate_nodes_resolver import derive_phase25_candidates_from_candidate_nodes
import run_optimization_sweep as ros
from phase5_second_level_overlay_check import (
    node_from_candidate, _run_gt_kernel, overlay_cagrs,
)
from sim_1s_vs_1m_groundtruth_overlays import load_hourly, load_seconds, resample_seconds_to_minutes

DB_PATH = "cache/research/trading_universe.db"

DEFAULT_SCOPE = dict(
    ticker="AGQ", strategy="TrailingBothZScoreBreakout",
    version="bench-inmemory-v6-massive-w2026-06-01_2026-08-21",
    fixed_sl=3.0, entry_timing="open_check", window=10,
)


def derive_json_candidates_full_population(db_path, ticker, strategy_name, config_version,
                                             fixed_sl, entry_timing, window):
    """JSON-sourced twin of phase4_candidate_nodes_resolver.derive_phase25_candidates_
    from_candidate_nodes(..., full_population=True) -- same field mapping as
    verify_params_json_drives_report_v64.derive_candidates_from_params_json, but no
    island-clustering (mirrors the full_population branch: every raw row is its own
    "island" of one, sorted by the same tiebreak). Raises KeyError (uncaught,
    deliberately) if a row's params_json is missing a field this needs."""
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)
    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT id, robust_alpha, trades, params_json FROM candidate_nodes
        WHERE ticker=? AND strategy=? AND version=? AND fixed_sl=? AND entry_timing=? AND window=?
              AND params_json IS NOT NULL
    """, (ticker, strategy_name, config_version, float(fixed_sl), entry_timing, int(window))).fetchall()
    conn.close()
    if not rows:
        return []

    decoded_rows = []
    for row_id, robust_alpha, trades, params_json in rows:
        p = json.loads(params_json)
        take_profit = p["arm_pct"] if strategy_name == "TrailingBothZScoreBreakout" else p["take_profit"]
        stop_loss = p[sl_axis_col]
        tpct = p[fourth_axis_col] if fourth_axis_col else 0.0
        decoded_rows.append({
            "id": row_id, "robust_alpha": float(robust_alpha), "trades": trades,
            "take_profit": float(take_profit), "stop_loss": float(stop_loss), "tpct": float(tpct),
            "max_hold_hours": int(p["max_hold_hours"]), "window": int(p["window"]),
            "z_score_threshold": float(p["z_score_threshold"]),
        })

    _tb_cols = ["robust_alpha"] + [c for c, _ in ros.GT_CANDIDATE_TIEBREAK]
    _tb_asc = [False] + [asc for _, asc in ros.GT_CANDIDATE_TIEBREAK]
    for col, asc in zip(reversed(_tb_cols), reversed(_tb_asc)):
        decoded_rows.sort(key=lambda r: r[col], reverse=not asc)

    candidates = []
    for row in decoded_rows:
        tp, sl = int(row["take_profit"]), int(row["stop_loss"])
        candidates.append({
            "id": row["id"],
            "island_tp": tp, "island_sl": sl,
            "take_profit": tp, "stop_loss": sl,
            "max_hold_hours": row["max_hold_hours"], "window": row["window"],
            "z_score_threshold": row["z_score_threshold"], "tpct": row["tpct"],
            "robust_alpha": row["robust_alpha"], "cagr": None,
            "phase4_eligible": True,
        })
    return candidates


def diff_row(flat_row, json_row):
    diffs = {}
    keys = set(flat_row) | set(json_row)
    for k in keys:
        fv, jv = flat_row.get(k), json_row.get(k)
        if fv != jv:
            diffs[k] = (fv, jv)
    return diffs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--ticker", default=DEFAULT_SCOPE["ticker"])
    ap.add_argument("--strategy", default=DEFAULT_SCOPE["strategy"])
    ap.add_argument("--version", default=DEFAULT_SCOPE["version"])
    ap.add_argument("--fixed-sl", type=float, default=DEFAULT_SCOPE["fixed_sl"])
    ap.add_argument("--entry-timing", default=DEFAULT_SCOPE["entry_timing"])
    ap.add_argument("--window", type=int, default=DEFAULT_SCOPE["window"])
    ap.add_argument("--data-source", choices=["yahoo", "massive"], default="massive")
    args = ap.parse_args()

    ros.DB_PATH = args.db
    ticker, strategy_name, version = args.ticker, args.strategy, args.version
    fixed_sl, entry_timing, window = args.fixed_sl, args.entry_timing, args.window

    print(f"Scope: ticker={ticker} strategy={strategy_name} version={version!r} "
          f"fixed_sl={fixed_sl} entry_timing={entry_timing} window={window}")

    flat_candidates = derive_phase25_candidates_from_candidate_nodes(
        ticker, strategy_name, version, fixed_sl=fixed_sl, entry_timing=entry_timing,
        window=window, full_population=True)
    json_candidates = derive_json_candidates_full_population(
        args.db, ticker, strategy_name, version, fixed_sl, entry_timing, window)

    print(f"flat candidates: {len(flat_candidates)}, json candidates: {len(json_candidates)}")
    if flat_candidates != json_candidates:
        print("MISMATCH -- candidates lists differ before even calling the Phase5 kernel:")
        for i, (fc, jc) in enumerate(zip(flat_candidates, json_candidates)):
            if fc != jc:
                print(f"  [{i}] flat={fc}")
                print(f"       json={jc}")
        sys.exit(1)
    print("candidate lists are identical (flat vs json-derived).")

    if not flat_candidates:
        print("No candidates -- nothing to check.")
        sys.exit(1)

    win_start, win_end = _window_dates_from_version(version)
    print(f"Loading {ticker} hourly data...")
    dfh = load_hourly(ticker, data_source=args.data_source)
    print(f"Loading {ticker} 1-second data (this is the slow step)...")
    df_1s = load_seconds(ticker)
    print(f"  {len(df_1s):,} 1-second rows, {df_1s.index.min()} -> {df_1s.index.max()}")
    df_1m = resample_seconds_to_minutes(df_1s)
    print(f"  {len(df_1m):,} 1-minute rows (resampled from the 1s series above)")

    bars_for_years = dfh.loc[win_start:win_end + " 23:59:59"]
    years = (bars_for_years.index.max() - bars_for_years.index.min()).total_seconds() / (365.25 * 86400)

    all_diffs = {}
    for i, (fc, jc) in enumerate(zip(flat_candidates, json_candidates), start=1):
        flat_node = node_from_candidate(ticker, strategy_name, entry_timing, fixed_sl, fc)
        json_node = node_from_candidate(ticker, strategy_name, entry_timing, fixed_sl, jc)
        if flat_node != json_node:
            print(f"  [{i}] NODE MISMATCH flat={flat_node} json={json_node}")
            all_diffs[f"candidate[{i}].node"] = (flat_node, json_node)
            continue

        flat_trades_1m = _run_gt_kernel(flat_node, dfh, df_1m, win_start, win_end)
        flat_trades_1s = _run_gt_kernel(flat_node, dfh, df_1s, win_start, win_end)
        json_trades_1m = _run_gt_kernel(json_node, dfh, df_1m, win_start, win_end)
        json_trades_1s = _run_gt_kernel(json_node, dfh, df_1s, win_start, win_end)

        flat_core_1m, flat_addon_1m, flat_drought_1m, flat_both_1m = overlay_cagrs(
            flat_trades_1m, ticker, dfh, flat_node, years)
        flat_core_1s, flat_addon_1s, flat_drought_1s, flat_both_1s = overlay_cagrs(
            flat_trades_1s, ticker, dfh, flat_node, years)
        json_core_1m, json_addon_1m, json_drought_1m, json_both_1m = overlay_cagrs(
            json_trades_1m, ticker, dfh, json_node, years)
        json_core_1s, json_addon_1s, json_drought_1s, json_both_1s = overlay_cagrs(
            json_trades_1s, ticker, dfh, json_node, years)

        flat_row = dict(
            n_trades_1m=len(flat_trades_1m), n_trades_1s=len(flat_trades_1s),
            core_cagr_1m=flat_core_1m, core_cagr_1s=flat_core_1s,
            addon_cagr_1m=flat_addon_1m, addon_cagr_1s=flat_addon_1s,
            drought_cagr_1m=flat_drought_1m, drought_cagr_1s=flat_drought_1s,
            core_both_cagr_1m=flat_both_1m, core_both_cagr_1s=flat_both_1s,
        )
        json_row = dict(
            n_trades_1m=len(json_trades_1m), n_trades_1s=len(json_trades_1s),
            core_cagr_1m=json_core_1m, core_cagr_1s=json_core_1s,
            addon_cagr_1m=json_addon_1m, addon_cagr_1s=json_addon_1s,
            drought_cagr_1m=json_drought_1m, drought_cagr_1s=json_drought_1s,
            core_both_cagr_1m=json_both_1m, core_both_cagr_1s=json_both_1s,
        )
        diffs = diff_row(flat_row, json_row)
        status = "MATCH" if not diffs else f"MISMATCH ({len(diffs)} field(s))"
        print(f"  [{i}] id={fc['id']} TP={fc['take_profit']} SL={fc['stop_loss']} "
              f"tpct={fc['tpct']} -- {status}")
        if diffs:
            for k, (fv, jv) in diffs.items():
                print(f"        {k}: flat={fv!r} json={jv!r}")
            all_diffs[f"candidate[{i}]"] = diffs

    if all_diffs:
        print(f"\nFAIL -- {len(all_diffs)} candidate(s) had a flat-vs-json Phase5 mismatch.")
        sys.exit(1)

    print(f"\nPASS -- Phase5 second-level overlay check output is identical whether driven "
          f"by flat-column-derived or params_json-derived candidates, for all "
          f"{len(flat_candidates)} candidate(s) in this scope.")
    sys.exit(0)


if __name__ == "__main__":
    import script_usage
    script_usage.record_invocation()
    main()
