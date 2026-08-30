"""v6.4 verification pass (2026-08-29): proves candidate_nodes.params_json can actually
DRIVE a real downstream consumer -- build_candidate_report_ground_truth (run_optimization_
sweep.py) -- not just round-trip inertly against its own row's flat columns (that was v6.3,
scripts/verify_params_json_roundtrip.py).

Method: pick one real scope (ticker/strategy/version/fixed_sl/entry_timing/window) with
multiple candidate_nodes rows carrying non-null params_json, build a candidates_override
list (the shape build_candidate_report_ground_truth's candidates_override param expects --
see scripts/phase4_candidate_nodes_resolver.py's docstring for the field contract) TWICE:

  1. "flat" -- scripts/phase4_candidate_nodes_resolver.derive_phase25_candidates_from_
     candidate_nodes, the already-wired-in production path (candidate_summary_report.py's
     gt_rows_for_scope, grid_window branch) that sources take_profit/stop_loss/tpct from
     the row's flat arm_pct/trail_buy_pct/trail_sell_pct columns.
  2. "json" -- this script's own derive_candidates_from_params_json, identical island-
     grouping logic (copied, not imported, since the flat version's grouping is private
     to that module) but sourcing take_profit/stop_loss/tpct/window/z_score_threshold/
     max_hold_hours from json.loads(row.params_json) instead.

Then calls the REAL build_candidate_report_ground_truth once per candidates_override list
and diffs the two returned report dicts field-by-field (excluding non-deterministic/
irrelevant keys like wall-clock timings, if any appear). A match proves params_json is
sufficient on its own to drive a real report consumer to an identical result as the flat
columns; any mismatch or KeyError building the JSON-sourced list is a real, reportable gap.

Does NOT touch run_optimization_sweep.py (hard-gated per the Review-Gate Persistence
Rule) or wire params_json into any real call path -- read-only proof-of-concept only.

Usage:
  .venv/bin/python scripts/verify_params_json_drives_report_v64.py [--db PATH]
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
from phase4_candidate_nodes_resolver import (
    derive_phase25_candidates_from_candidate_nodes,
    _stop_loss_and_tpct_from_row,
)

DB_PATH = "cache/research/trading_universe.db"

# Same greedy fixed-capacity island params phase4_candidate_nodes_resolver.py uses --
# imported for real rather than re-guessed, so this script can't silently drift from the
# production grouping thresholds.
import run_optimization_sweep as ros


def pick_scope(db_path):
    """Real scope (ticker/strategy/version/fixed_sl/entry_timing/window) with the most
    non-null-params_json rows, as a representative multi-candidate test case."""
    conn = sqlite3.connect(db_path)
    row = conn.execute("""
        SELECT ticker, strategy, version, fixed_sl, entry_timing, window, COUNT(*) c
        FROM candidate_nodes WHERE params_json IS NOT NULL
        GROUP BY ticker, strategy, version, fixed_sl, entry_timing, window
        ORDER BY c DESC LIMIT 1
    """).fetchone()
    conn.close()
    return row


def derive_candidates_from_params_json(db_path, ticker, strategy_name, config_version,
                                        fixed_sl, entry_timing, window):
    """JSON-sourced twin of phase4_candidate_nodes_resolver.derive_phase25_candidates_
    from_candidate_nodes -- identical island-grouping logic, but take_profit/stop_loss/
    tpct/window/z_score_threshold/max_hold_hours come from json.loads(params_json)
    instead of the row's flat columns. Raises KeyError (uncaught, deliberately) if a
    row's params_json is missing a field this needs -- that IS a finding, not something
    to paper over with a default."""
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
        # 'trades' (real trade count, a sweep OUTCOME) is deliberately NOT part of
        # params_json -- params_json only ever encodes strategy INPUT params (see
        # node_key.build_params_dict), so it's sourced from the flat column here too,
        # same as the flat resolver. This is not a params_json gap.
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

    # Same tiebreak sort as derive_phase25_candidates_from_candidate_nodes.
    _tb_cols = ["robust_alpha"] + [c for c, _ in ros.GT_CANDIDATE_TIEBREAK]
    _tb_asc = [False] + [asc for _, asc in ros.GT_CANDIDATE_TIEBREAK]
    for col, asc in zip(reversed(_tb_cols), reversed(_tb_asc)):
        decoded_rows.sort(key=lambda r: r[col], reverse=not asc)

    # Same greedy fixed-capacity island assignment as the flat resolver.
    islands = []
    for row in decoded_rows:
        tp, sl = row["take_profit"], row["stop_loss"]
        target = None
        for isl in islands:
            if len(isl["members"]) >= 3:
                continue
            seed_tp, seed_sl = isl["seed"]
            if abs(tp - seed_tp) <= ros.FINE_RADIUS and abs(sl - seed_sl) <= ros.FINE_RADIUS:
                target = isl
                break
        if target is None:
            if len(islands) < ros.N_ISLANDS:
                target = {"seed": (tp, sl), "members": []}
                islands.append(target)
            else:
                target = min(islands, key=lambda isl: (abs(tp - isl["seed"][0]) +
                                                         abs(sl - isl["seed"][1])))
        target["members"].append(row)

    candidates = []
    for isl in islands:
        seed_tp, seed_sl = isl["seed"]
        for cand in isl["members"]:
            candidates.append({
                "id": cand["id"],
                "island_tp": seed_tp, "island_sl": seed_sl,
                "take_profit": int(cand["take_profit"]), "stop_loss": int(cand["stop_loss"]),
                "max_hold_hours": cand["max_hold_hours"], "window": cand["window"],
                "z_score_threshold": cand["z_score_threshold"], "tpct": cand["tpct"],
                "robust_alpha": cand["robust_alpha"], "cagr": None,
                "phase4_eligible": True,
            })
    return candidates


def diff_reports(flat_report, json_report):
    diffs = {}
    keys = set(flat_report) | set(json_report)
    for k in keys:
        fv, jv = flat_report.get(k), json_report.get(k)
        if k == "candidates":
            if len(fv or []) != len(jv or []):
                diffs["candidates.len"] = (len(fv or []), len(jv or []))
                continue
            for i, (fc, jc) in enumerate(zip(fv, jv)):
                ckeys = set(fc) | set(jc)
                for ck in ckeys:
                    if fc.get(ck) != jc.get(ck):
                        diffs[f"candidates[{i}].{ck}"] = (fc.get(ck), jc.get(ck))
        elif fv != jv:
            diffs[k] = (fv, jv)
    return diffs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    args = ap.parse_args()

    ros.DB_PATH = args.db

    scope = pick_scope(args.db)
    if not scope:
        print("No candidate_nodes rows with non-null params_json found.")
        sys.exit(1)
    ticker, strategy_name, version, fixed_sl, entry_timing, window, n = scope
    print(f"Scope: ticker={ticker} strategy={strategy_name} version={version!r} "
          f"fixed_sl={fixed_sl} entry_timing={entry_timing} window={window} ({n} rows)")

    hp = _hp_for_strategy(strategy_name)
    win_start, win_end = _window_dates_from_version(version)
    data_source = "massive" if "-massive" in version else "yahoo"

    flat_candidates = derive_phase25_candidates_from_candidate_nodes(
        ticker, strategy_name, version, fixed_sl=fixed_sl, entry_timing=entry_timing, window=window)
    json_candidates = derive_candidates_from_params_json(
        args.db, ticker, strategy_name, version, fixed_sl, entry_timing, window)

    print(f"flat candidates: {len(flat_candidates)}, json candidates: {len(json_candidates)}")
    if flat_candidates != json_candidates:
        print("MISMATCH -- candidates_override lists differ before even calling "
              "build_candidate_report_ground_truth:")
        for i, (fc, jc) in enumerate(zip(flat_candidates, json_candidates)):
            if fc != jc:
                print(f"  [{i}] flat={fc}")
                print(f"       json={jc}")
        sys.exit(1)
    print("candidates_override lists are identical (flat vs json-derived).")

    from run_optimization_sweep import build_candidate_report_ground_truth

    flat_report = build_candidate_report_ground_truth(
        ticker, strategy_name, version, hp, start_date=win_start, end_date=win_end,
        fixed_sl=fixed_sl, entry_timing=entry_timing, data_source=data_source,
        candidates_override=flat_candidates)
    json_report = build_candidate_report_ground_truth(
        ticker, strategy_name, version, hp, start_date=win_start, end_date=win_end,
        fixed_sl=fixed_sl, entry_timing=entry_timing, data_source=data_source,
        candidates_override=json_candidates)

    diffs = diff_reports(flat_report, json_report)
    if diffs:
        print(f"\nFAIL -- {len(diffs)} field(s) differ between flat-driven and "
              f"json-driven build_candidate_report_ground_truth output:")
        for k, (fv, jv) in diffs.items():
            print(f"  {k}: flat={fv!r}  json={jv!r}")
        sys.exit(1)

    print(f"\nPASS -- build_candidate_report_ground_truth output is byte-identical "
          f"whether driven by flat-column-derived or params_json-derived candidates "
          f"({len(flat_report.get('candidates', []))} candidates, "
          f"winner_index={flat_report.get('winner_index')}).")
    sys.exit(0)


if __name__ == "__main__":
    import script_usage
    script_usage.record_invocation()
    main()
