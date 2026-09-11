"""Prints/exports the real 1-second-resolution trade-by-trade breakdown for one
candidate_nodes id -- entry/exit price/time, Return_core, blended (addon) Return,
which trades are armed, and the drought overlay's own simulated windows, so the
aggregate core/addon/drought/core_both CAGR numbers can be verified by hand
(2026-09-08, real user ask: "want to actually see the raw per-trade data ... same
spirit as the hand-worked addon-leg example earlier tonight").

Reuses phase5_second_level_overlay_check.py's OWN real trade list
(candidate_verification_store.get_phase5_1s_trades, the already-persisted 1s trades
Phase5 verified this candidate against) and the SAME real transform functions
(backtester.apply_addon_overlay_ground_truth/simulate_drought_overlay_ground_truth)
-- no new simulation logic, no re-derivation of the aggregate numbers by a second
path (the exact bug class the drought_factor_gated fix and the queued Phase5-vs-
Phase4 consolidation both exist to eliminate).

Usage:
    .venv/bin/python scripts/show_candidate_1s_trades.py --candidate-id 35463
    .venv/bin/python scripts/show_candidate_1s_trades.py --candidate-id 35463 --xlsx output/AGQ_35463_trades.xlsx
"""
import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__)) + "/.."
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import strategies  # noqa: E402
from backtester import apply_addon_overlay_ground_truth, simulate_drought_overlay_ground_truth  # noqa: E402
from candidate_verification_store import get_phase5_1s_trades  # noqa: E402
from sim_1s_vs_1m_groundtruth_overlays import load_hourly  # noqa: E402
from run_optimization_sweep import DB_PATH  # noqa: E402


def _load_node(conn, candidate_id):
    row = conn.execute(
        "SELECT id, ticker, strategy, version, window, z, fixed_sl, arm_pct, trail_buy_pct, "
        "trail_sell_pct, max_hold_hours, entry_timing FROM candidate_nodes WHERE id=?",
        (candidate_id,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"candidate_nodes id={candidate_id} not found")
    return dict(row)


def _fmt_time(t):
    return t.strftime("%Y-%m-%d %H:%M:%S") if t is not None else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate-id", type=int, required=True)
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--xlsx", default=None,
                     help="also write a full trade-by-trade sheet + a drought-windows sheet here")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    node = _load_node(conn, args.candidate_id)
    print(f"candidate_id={node['id']}  {node['ticker']} {node['strategy']}  "
          f"window={node['window']} z={node['z']} fixed_sl={node['fixed_sl']} "
          f"arm_pct={node['arm_pct']} trail_buy_pct={node['trail_buy_pct']} "
          f"trail_sell_pct={node['trail_sell_pct']} hold={node['max_hold_hours']}h "
          f"entry_timing={node['entry_timing']}  version={node['version']}\n")

    trades = get_phase5_1s_trades(
        conn, args.candidate_id, ticker=node["ticker"], strategy=node["strategy"],
        fixed_sl=node["fixed_sl"])
    if not trades:
        raise SystemExit(f"No stored 1s trades for candidate_id={args.candidate_id} -- "
                          f"Phase5 hasn't verified this candidate yet.")
    print(f"{len(trades)} real 1s core trades loaded from phase5_trades\n")

    addon_trades = apply_addon_overlay_ground_truth(trades)

    core_bal = addon_bal = 1.0
    print(f"{'#':>4} {'Entry Time':19} {'Entry $':>9} {'Exit Time':19} {'Exit $':>9} "
          f"{'Reason':8} {'Return_core':>11} {'Armed':5} {'Arm $':>9} {'Return(blend)':>13} "
          f"{'core_bal':>10} {'addon_bal':>10}")
    rows_for_export = []
    for i, t in enumerate(addon_trades, 1):
        core_bal *= (1 + t["Return_core"])
        addon_bal *= (1 + t["Return"])
        armed = bool(t.get("armed"))
        print(f"{i:>4} {_fmt_time(t['Entry Time']):19} {t['Entry Price']:>9.4f} "
              f"{_fmt_time(t['Exit Time']):19} {t['Exit Price']:>9.4f} "
              f"{str(t.get('exit_reason')):8} {t['Return_core']:>11.4%} "
              f"{('Y' if armed else 'N'):5} "
              f"{(t['Arm Price'] if armed else float('nan')):>9.4f} "
              f"{t['Return']:>13.4%} {core_bal:>10.4f} {addon_bal:>10.4f}")
        rows_for_export.append({
            "trade_idx": i, "entry_time": t["Entry Time"], "entry_price": t["Entry Price"],
            "exit_time": t["Exit Time"], "exit_price": t["Exit Price"],
            "exit_reason": t.get("exit_reason"), "return_core": t["Return_core"],
            "armed": armed, "arm_time": t.get("Arm Time"), "arm_price": t.get("Arm Price"),
            "return_blended": t["Return"], "core_bal": core_bal, "addon_bal": addon_bal,
        })

    print(f"\ncore_bal (final)  = {core_bal:.6f}  (core_cagr uses this)")
    print(f"addon_bal (final) = {addon_bal:.6f}  (addon_cagr uses this)")

    armed_trades = [t for t in addon_trades if t.get("armed")]
    addon_rets = [(t["Exit Price"] - t["Arm Price"]) / t["Arm Price"] for t in armed_trades]
    print(f"\n{len(armed_trades)} armed trades, {len(addon_rets)} real addon-leg returns "
          f"(unblended, (Exit-Arm)/Arm -- the addon_ok robustness-gate input, NOT the "
          f"blended Return column above)")
    for i, (t, r) in enumerate(zip(armed_trades, addon_rets), 1):
        print(f"  leg {i}: arm={_fmt_time(t['Arm Time'])} @ {t['Arm Price']:.4f}  "
              f"exit={_fmt_time(t['Exit Time'])} @ {t['Exit Price']:.4f}  return={r:.4%}")

    drought_rows_for_export = []
    if strategies.uses_arm_trail_exit(node["strategy"]):
        dfh = load_hourly(node["ticker"], data_source="massive")
        drought = simulate_drought_overlay_ground_truth(
            trades, dfh, node["ticker"], node["fixed_sl"],
            arm_pct=node["arm_pct"], trail_sell_pct=node["trail_sell_pct"])
        if drought is None:
            print("\nDrought overlay: no real drought windows found for this candidate.")
        else:
            best_rets = drought.get("best_rets") or []
            print(f"\nDrought overlay: best(confirm_days={drought.get('best_confirm_days')}, "
                  f"vol_gate={drought.get('best_vol_gate')})  "
                  f"windows={drought.get('n_drought_simulated')}/"
                  f"{drought.get('n_grid_cells_evaluated')} grid cells  "
                  f"drought_compounded_pct={drought.get('drought_compounded_pct')}  "
                  f"combined_compounded_pct={drought.get('combined_compounded_pct')}")
            print(f"{len(best_rets)} real simulated drought-window returns "
                  f"(the drought_ok robustness-gate input):")
            for i, r in enumerate(best_rets, 1):
                print(f"  window {i}: return={r:.4%}")
                drought_rows_for_export.append({"window_idx": i, "return": r})
    else:
        print(f"\nDrought overlay: {node['strategy']} does not support the "
              f"arm-then-trail exit shape (strategies.uses_arm_trail_exit == False) -- skipped.")

    if args.xlsx:
        import pandas as pd
        out_path = args.xlsx if args.xlsx.endswith(".xlsx") else args.xlsx + ".xlsx"
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with pd.ExcelWriter(out_path, engine="openpyxl") as xw:
            pd.DataFrame(rows_for_export).to_excel(xw, sheet_name="Trades", index=False)
            pd.DataFrame(drought_rows_for_export).to_excel(xw, sheet_name="Drought Windows", index=False)
        print(f"\nWrote {out_path}")

    conn.close()


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
