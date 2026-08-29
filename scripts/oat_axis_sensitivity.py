"""One-at-a-time (OAT) axis sensitivity check -- new, 2026-08-29 (Task #6 piece #3,
planner dispatch, SOXL Campaign B planning session).

Real question this answers: today's island/cliff-safety architecture treats
(take_profit, stop_loss/trail_pct) as the only "cliff-prone" axes (pick_island_
centers' 2D neighborhood check) and pools window/z/tpct/fixed_sl across when
ranking candidates -- a legacy accident from ZScoreBreakout's original
single-axis design, never actually verified. Before fixed_sl becomes a real
swept grid axis (see docs/backlog_cache.md's "fixed_sl should be a swept grid
parameter" entry), this empirically checks whether window/fixed_sl (and the
other axes, for completeness) behave like cliff-prone axes (a 1-step nudge
causes a sharp CAGR break) or smooth/poolable ones.

Method: from a real candidate_nodes anchor row, hold every param fixed at the
anchor's value except ONE axis, sweep that axis across its real grid range, one
single-cell backtest per point via run_optimization_sweep.run_single_backtest_
node_ground_truth_isolated (the same worker function every real sweep phase
already uses) -- called directly per point in-process, not dispatched through a
pool (batch mode still stays ONE long-lived process looping over anchors/axes,
so _load_node_inputs_ground_truth's own per-process (ticker,strategy,window,
start_date,end_date) memoization is reused for free across points that share a
window value).

Per axis, reports TWO things kept separate: (a) overall spread (max-min CAGR
across the sweep -- a "generally steady" axis doesn't need special handling),
and (b) max adjacent-step |delta CAGR| (a cliff can hide inside an otherwise-
flat axis if one specific neighbor breaks sharply -- overall spread alone
would miss this).

Usage:
  .venv/bin/python scripts/oat_axis_sensitivity.py --candidate-id 2271
  .venv/bin/python scripts/oat_axis_sensitivity.py --candidate-id 2271 --axes z fixed_sl
  .venv/bin/python scripts/oat_axis_sensitivity.py --all-candidates --ticker SOXL \\
      --version bench-inmemory-v6-massive-w2021-08-23_2026-08-21
"""
import argparse
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import pandas as pd

from run_optimization_sweep import (
    DB_PATH, run_single_backtest_node_ground_truth_isolated, compute_bh_returns,
)
from phase4_candidate_nodes_resolver import _stop_loss_and_tpct_from_row
import strategies

# Matches bench_phase1_phase2_inmemory.py's own real campaign constants -- see that
# file's TICKER/START/END/DATA_SOURCE.
TICKER = "SOXL"
START, END = "2021-08-23", "2026-08-21"
DATA_SOURCE = "massive"

AXES = {
    "take_profit":    {"default": list(range(1, 31)), "both_only": False},
    "stop_loss":      {"default": list(range(1, 31)), "both_only": False},
    "max_hold_hours": {"default": [7, 14, 21, 28, 35, 42, 49, 56, 63, 70, 77, 84, 91, 98,
                                    105, 112, 119, 126, 133, 140], "both_only": False},
    "window":         {"default": [5, 10, 15, 20, 25], "both_only": False},
    "z":              {"default": [1.0, 1.5, 2.0], "both_only": False},
    "tpct":           {"default": list(range(1, 8)), "both_only": True},
    "fixed_sl":       {"default": list(range(1, 9)), "both_only": False},
}


def load_anchor(conn, candidate_id):
    row = conn.execute("""
        SELECT ticker, strategy, version, window, z, fixed_sl, arm_pct, trail_buy_pct,
               trail_sell_pct, max_hold_hours, entry_timing
        FROM candidate_nodes WHERE id=?
    """, (candidate_id,)).fetchone()
    if row is None:
        raise SystemExit(f"No candidate_nodes row for id={candidate_id}.")
    (ticker, strategy, version, window, z, fixed_sl, arm_pct, trail_buy_pct,
     trail_sell_pct, max_hold_hours, entry_timing) = row
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy)
    stop_loss, tpct = _stop_loss_and_tpct_from_row(sl_axis_col, fourth_axis_col,
                                                     trail_buy_pct, trail_sell_pct)
    return {
        "candidate_id": candidate_id, "ticker": ticker, "strategy": strategy,
        "version": version, "take_profit": float(arm_pct), "stop_loss": float(stop_loss),
        "max_hold_hours": int(max_hold_hours), "window": int(window), "z": float(z),
        "tpct": float(tpct), "fixed_sl": float(fixed_sl), "entry_timing": entry_timing,
    }


def run_point(anchor, axis, value, spy_bh):
    tp = value if axis == "take_profit" else anchor["take_profit"]
    sl = value if axis == "stop_loss" else anchor["stop_loss"]
    hold = value if axis == "max_hold_hours" else anchor["max_hold_hours"]
    w = value if axis == "window" else anchor["window"]
    z = value if axis == "z" else anchor["z"]
    tpct = value if axis == "tpct" else anchor["tpct"]
    fixed_sl = value if axis == "fixed_sl" else anchor["fixed_sl"]
    args = (anchor["ticker"], anchor["strategy"], anchor["version"], tp, sl, hold, w,
            spy_bh, z, fixed_sl, tpct, anchor["entry_timing"], True, START, END, DATA_SOURCE)
    res = run_single_backtest_node_ground_truth_isolated(args)
    status = res["status"]
    cagr = res["payload"][5] if status == "SUCCESS" and len(res["payload"]) > 5 else None
    return cagr, status


def sweep_axis(anchor, axis, values, spy_bh):
    rows = []
    for v in values:
        cagr, status = run_point(anchor, axis, v, spy_bh)
        rows.append({
            "candidate_id": anchor["candidate_id"], "ticker": anchor["ticker"],
            "strategy": anchor["strategy"], "version": anchor["version"], "axis": axis,
            "anchor_value": anchor[axis], "swept_value": v, "cagr_pct": cagr, "status": status,
        })
    return rows


def summarize_axis(rows):
    """(spread, max_adjacent_delta) from a real (already ordered) sweep, both None if
    fewer than 2 real (non-None cagr) points."""
    cagrs = [r["cagr_pct"] for r in rows if r["cagr_pct"] is not None]
    if len(cagrs) < 2:
        return None, None
    spread = max(cagrs) - min(cagrs)
    ordered = [r["cagr_pct"] for r in rows if r["cagr_pct"] is not None]
    max_adj = max(abs(ordered[i + 1] - ordered[i]) for i in range(len(ordered) - 1))
    return spread, max_adj


def run_anchor(anchor, axes_to_run, axis_values_override, spy_bh):
    all_rows = []
    print(f"\n{'#'*90}\ncandidate_id={anchor['candidate_id']} {anchor['ticker']}/"
          f"{anchor['strategy']}/window={anchor['window']}/fixed_sl={anchor['fixed_sl']} "
          f"(anchor TP={anchor['take_profit']} SL={anchor['stop_loss']} tpct={anchor['tpct']} "
          f"hold={anchor['max_hold_hours']} z={anchor['z']})\n{'#'*90}")
    for axis in axes_to_run:
        spec = AXES[axis]
        if spec["both_only"] and anchor["strategy"] != "TrailingBothZScoreBreakout":
            print(f"  {axis}: skipped -- TrailingBoth-only axis, this anchor is "
                  f"{anchor['strategy']}")
            continue
        values = axis_values_override.get(axis, spec["default"])
        t0 = time.monotonic()
        rows = sweep_axis(anchor, axis, values, spy_bh)
        all_rows.extend(rows)
        spread, max_adj = summarize_axis(rows)
        elapsed = time.monotonic() - t0
        spread_str = "N/A" if spread is None else f"{spread:.2f}pp"
        max_adj_str = "N/A" if max_adj is None else f"{max_adj:.2f}pp"
        print(f"  {axis}: {len(values)} points in {elapsed:.1f}s -- spread={spread_str} "
              f"max_adjacent_delta={max_adj_str}")
    return all_rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candidate-id", type=int, default=None, help="single anchor by candidate_nodes id")
    ap.add_argument("--all-candidates", action="store_true", help="batch mode -- every matching row")
    ap.add_argument("--ticker", default=None, help="--all-candidates: required")
    ap.add_argument("--version", default=None, help="--all-candidates: optional filter")
    ap.add_argument("--strategy", default=None, help="--all-candidates: optional filter")
    ap.add_argument("--axes", nargs="+", choices=sorted(AXES), default=sorted(AXES),
                     help="which axes to check (default: all)")
    ap.add_argument("--tp-values", type=int, nargs="+", default=None)
    ap.add_argument("--sl-values", type=int, nargs="+", default=None)
    ap.add_argument("--hold-values", type=int, nargs="+", default=None)
    ap.add_argument("--window-values", type=int, nargs="+", default=None)
    ap.add_argument("--z-values", type=float, nargs="+", default=None)
    ap.add_argument("--tpct-values", type=float, nargs="+", default=None)
    ap.add_argument("--fixed-sl-values", type=float, nargs="+", default=None)
    args = ap.parse_args()

    if not args.candidate_id and not args.all_candidates:
        raise SystemExit("Need --candidate-id N or --all-candidates --ticker X.")
    if args.all_candidates and not args.ticker:
        raise SystemExit("--all-candidates requires --ticker.")

    axis_values_override = {}
    for axis, cli_values in (("take_profit", args.tp_values), ("stop_loss", args.sl_values),
                              ("max_hold_hours", args.hold_values), ("window", args.window_values),
                              ("z", args.z_values), ("tpct", args.tpct_values),
                              ("fixed_sl", args.fixed_sl_values)):
        if cli_values is not None:
            axis_values_override[axis] = cli_values

    with sqlite3.connect(DB_PATH) as conn:
        if args.candidate_id:
            candidate_ids = [args.candidate_id]
        else:
            query = "SELECT id FROM candidate_nodes WHERE ticker=?"
            params = [args.ticker]
            if args.version is not None:
                query += " AND version=?"
                params.append(args.version)
            if args.strategy is not None:
                query += " AND strategy=?"
                params.append(args.strategy)
            candidate_ids = [r[0] for r in conn.execute(query, params).fetchall()]
        if not candidate_ids:
            raise SystemExit("No matching candidate_nodes rows found.")
        anchors = [load_anchor(conn, cid) for cid in candidate_ids]

    print(f"Checking {len(anchors)} anchor(s), axes={args.axes}")
    _, spy_bh = compute_bh_returns(TICKER, start_date=START, end_date=END, data_source=DATA_SOURCE)

    t_all = time.monotonic()
    all_rows = []
    for anchor in anchors:
        all_rows.extend(run_anchor(anchor, args.axes, axis_values_override, spy_bh))

    df_out = pd.DataFrame(all_rows)
    out_dir = os.path.join(ROOT, "output")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"oat_axis_sensitivity_{time.strftime('%Y%m%d_%H%M%S')}.csv")
    df_out.to_csv(out_path, index=False)
    print(f"\nFull results: {out_path}")
    print(f"Total: {time.monotonic() - t_all:.1f}s for {len(anchors)} anchor(s)")

    if len(anchors) > 1:
        print(f"\n=== Aggregate summary across {len(anchors)} anchors ===")
        for axis in args.axes:
            axis_df = df_out[df_out["axis"] == axis]
            if axis_df.empty:
                continue
            spreads, max_adjs = [], []
            for cid, grp in axis_df.groupby("candidate_id"):
                rows = grp.to_dict("records")
                spread, max_adj = summarize_axis(rows)
                if spread is not None:
                    spreads.append(spread)
                    max_adjs.append(max_adj)
            if not spreads:
                print(f"  {axis}: no usable data")
                continue
            s = pd.Series(spreads)
            m = pd.Series(max_adjs)
            print(f"  {axis}: n={len(spreads)} candidates -- spread median={s.median():.2f}pp "
                  f"max={s.max():.2f}pp | adjacent-delta median={m.median():.2f}pp max={m.max():.2f}pp")


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
