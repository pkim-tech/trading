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
from concurrent.futures import ProcessPoolExecutor, as_completed

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


def sweep_axis(anchor, axis, values, spy_bh, label=None):
    """`label` (default: `axis`) is what gets written to the output rows' 'axis' column --
    lets a caller run a real point using `axis`'s own run_point() override logic (e.g.
    'fixed_sl') while reporting it under a different name (e.g. 'fixed_sl_fine', the
    anchor-relative sub-integer pass -- see FIXED_SL_FINE_STEP/FIXED_SL_FINE_RADIUS'
    own docstring) so the two passes' stats never get merged."""
    label = label or axis
    rows = []
    for v in values:
        cagr, status = run_point(anchor, axis, v, spy_bh)
        rows.append({
            "candidate_id": anchor["candidate_id"], "ticker": anchor["ticker"],
            "strategy": anchor["strategy"], "version": anchor["version"], "axis": label,
            "anchor_value": anchor[axis], "swept_value": v, "cagr_pct": cagr, "status": status,
        })
    return rows


# fixed_sl fine-resolution pass (2026-08-29 correction, planner dispatch): fixed_sl is
# stored as a real float (campaign_config.patch_config does float(fixed_sl)) -- the
# CLI's integer-only default sweep has the same coarse-grid blind spot Phase1's own
# TP/SL coarse grid has, and unlike TP/SL there's no existing fine-mesh convention to
# fall back on for fixed_sl. Sweeps a narrow band AROUND THE ANCHOR'S OWN fixed_sl value
# only (not the whole 1-8 range at fine resolution -- that's expensive for no benefit;
# the question is only whether a cliff exists right next to where a real candidate
# actually sits), reported as its own separate 'fixed_sl_fine' axis/stat, never merged
# with the coarse integer pass's own adjacent-delta (they answer different-resolution
# questions). take_profit/stop_loss/window/z/tpct do NOT get this treatment -- TP/SL
# already has the real FINE_RADIUS/CLIFF_RADIUS convention establishing whole-integer-
# percent as fine enough, and window/z/tpct are inherently discrete real-world settings
# (a window of 12.5 bars isn't a real config anyone would set).
FIXED_SL_FINE_STEP = 0.25
FIXED_SL_FINE_RADIUS = 1.5


def _fixed_sl_fine_values(anchor_fixed_sl):
    n_steps = int(round(FIXED_SL_FINE_RADIUS / FIXED_SL_FINE_STEP))
    return [round(anchor_fixed_sl + FIXED_SL_FINE_STEP * i, 2) for i in range(-n_steps, n_steps + 1)
            if anchor_fixed_sl + FIXED_SL_FINE_STEP * i > 0]


# window_fine / z_fine (2026-08-29, Task #6 follow-up, planner dispatch): a real
# --all-candidates run found window (40.1pp median adjacent delta) and z (36.2pp) both
# beat take_profit's 17.5pp -- but window's coarse default only tests 5-unit steps
# (5,10,15,20,25) and z only has 3 points total (1.0/1.5/2.0), the same coarse-grid
# blind spot already caught for fixed_sl. Mirrors _fixed_sl_fine_values' exact
# anchor+/-radius pattern, reported under its own 'window_fine'/'z_fine' axis label,
# never merged with the coarse pass' own adjacent-delta stats.
#
# window is a real integer bar-count -- step=1 is the natural fine resolution (a
# window of 12.5 bars isn't a real config). Floor at window>=2: strategies.py's
# rolling SMA/Std (e.g. line 141 df['Close'].rolling(window=w).std()) needs >=2 points
# to produce a non-degenerate std; window<2 gives NaN/zero std and a broken z-score,
# not a real edge case worth generating.
#
# z is a continuous statistical threshold (unlike window) -- step=0.05 matches its
# real resolution. Floor at z>0 (a non-positive z-score threshold isn't sensible --
# same non-positive-value convention as fixed_sl's own >0 filter above).
#
# Radius sizing (measured empirically, not guessed -- see commit message for the real
# numbers): window sweep points are NOT free like z/fixed_sl -- each distinct window
# value forces a fresh rolling-window recompute (~4s/point cold), unlike z/tpct which
# reuse the anchor's already-prepped data for ~0s/point. Real finding, not assumed:
# run_optimization_sweep._NODE_INPUT_CACHE_GT (the per-process memo this cost hinges
# on) caps at 6 entries (_NODE_INPUT_CACHE_MAX). The coarse window grid alone already
# uses 5 of those 6 slots -- so ANY window_fine radius>=1 (2+ new distinct window
# values beyond the anchor) pushes total distinct touches past 6, wiping the whole
# cache (clear-on-full, not LRU) and killing the coarse pass' previously-free
# steady-state reuse across candidates too, not just adding window_fine's own cost.
# This makes radius=1 (2 new points) the minimum *and* the best choice: cost is
# dominated by the now-unavoidable coarse-pass cache-wipe overhead, not by the fine
# pass' own point count, so a smaller radius only saves ~2 points' worth of compute
# on top of a mostly-fixed overhead -- no reason to go smaller than the minimum
# meaningful ±1-bar neighbor check. z_fine has no such interaction (z isn't part of
# the cache key) and stays near-free regardless of radius.
WINDOW_FINE_STEP = 1
WINDOW_FINE_RADIUS = 1
WINDOW_FINE_MIN = 2

Z_FINE_STEP = 0.05
Z_FINE_RADIUS = 0.3


def _window_fine_values(anchor_window):
    n_steps = int(round(WINDOW_FINE_RADIUS / WINDOW_FINE_STEP))
    return [anchor_window + WINDOW_FINE_STEP * i for i in range(-n_steps, n_steps + 1)
            if anchor_window + WINDOW_FINE_STEP * i >= WINDOW_FINE_MIN]


def _z_fine_values(anchor_z):
    n_steps = int(round(Z_FINE_RADIUS / Z_FINE_STEP))
    return [round(anchor_z + Z_FINE_STEP * i, 2) for i in range(-n_steps, n_steps + 1)
            if anchor_z + Z_FINE_STEP * i > 0]


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

        if axis == "fixed_sl":
            fine_values = _fixed_sl_fine_values(anchor["fixed_sl"])
            t0 = time.monotonic()
            fine_rows = sweep_axis(anchor, "fixed_sl", fine_values, spy_bh, label="fixed_sl_fine")
            all_rows.extend(fine_rows)
            fine_spread, fine_max_adj = summarize_axis(fine_rows)
            elapsed = time.monotonic() - t0
            fine_spread_str = "N/A" if fine_spread is None else f"{fine_spread:.2f}pp"
            fine_max_adj_str = "N/A" if fine_max_adj is None else f"{fine_max_adj:.2f}pp"
            print(f"  fixed_sl_fine: {len(fine_values)} points (anchor {anchor['fixed_sl']} "
                  f"+/-{FIXED_SL_FINE_RADIUS} step {FIXED_SL_FINE_STEP}) in {elapsed:.1f}s -- "
                  f"spread={fine_spread_str} max_adjacent_delta={fine_max_adj_str}")

        if axis == "window":
            fine_values = _window_fine_values(anchor["window"])
            t0 = time.monotonic()
            fine_rows = sweep_axis(anchor, "window", fine_values, spy_bh, label="window_fine")
            all_rows.extend(fine_rows)
            fine_spread, fine_max_adj = summarize_axis(fine_rows)
            elapsed = time.monotonic() - t0
            fine_spread_str = "N/A" if fine_spread is None else f"{fine_spread:.2f}pp"
            fine_max_adj_str = "N/A" if fine_max_adj is None else f"{fine_max_adj:.2f}pp"
            print(f"  window_fine: {len(fine_values)} points (anchor {anchor['window']} "
                  f"+/-{WINDOW_FINE_RADIUS} step {WINDOW_FINE_STEP}) in {elapsed:.1f}s -- "
                  f"spread={fine_spread_str} max_adjacent_delta={fine_max_adj_str}")

        if axis == "z":
            fine_values = _z_fine_values(anchor["z"])
            t0 = time.monotonic()
            fine_rows = sweep_axis(anchor, "z", fine_values, spy_bh, label="z_fine")
            all_rows.extend(fine_rows)
            fine_spread, fine_max_adj = summarize_axis(fine_rows)
            elapsed = time.monotonic() - t0
            fine_spread_str = "N/A" if fine_spread is None else f"{fine_spread:.2f}pp"
            fine_max_adj_str = "N/A" if fine_max_adj is None else f"{fine_max_adj:.2f}pp"
            print(f"  z_fine: {len(fine_values)} points (anchor {anchor['z']} "
                  f"+/-{Z_FINE_RADIUS} step {Z_FINE_STEP}) in {elapsed:.1f}s -- "
                  f"spread={fine_spread_str} max_adjacent_delta={fine_max_adj_str}")
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
    ap.add_argument("--workers", type=int, default=8,
                     help="--all-candidates only: ProcessPoolExecutor worker count (default 8, "
                          "leaves headroom on a 12-core box, see long-job-launch skill's "
                          "cap-to-headroom rule). One task per CANDIDATE (its full 7-axis OAT "
                          "sweep, not per axis-point) -- _load_node_inputs_ground_truth memoizes "
                          "per-process by (ticker,strategy,window,start,end), and most of a "
                          "candidate's own axis points share its anchor window, so splitting at "
                          "the point level would scatter that reuse across workers and pay a "
                          "fresh data-load far more often. Single-candidate mode (--candidate-id) "
                          "stays serial -- already fast, no pool needed.")
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
    if args.all_candidates and len(anchors) > 1:
        # Candidate-level parallelism only (see --workers help above) -- each task runs
        # one candidate's ENTIRE OAT sweep (all axes) in its own process, preserving
        # _load_node_inputs_ground_truth's real per-process window-memoization reuse
        # exactly as the serial path already gets it.
        total = len(anchors)
        done = 0
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(run_anchor, anchor, args.axes, axis_values_override, spy_bh): anchor
                       for anchor in anchors}
            for fut in as_completed(futures):
                anchor = futures[fut]
                all_rows.extend(fut.result())
                done += 1
                elapsed = time.monotonic() - t_all
                eta = f", ETA {(elapsed / done * (total - done)):.0f}s" if done < total else ""
                print(f"[{done}/{total}, {elapsed:.0f}s elapsed{eta}] candidate_id={anchor['candidate_id']} done",
                      flush=True)
    else:
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
        agg_axes = (list(args.axes)
                    + (["fixed_sl_fine"] if "fixed_sl" in args.axes else [])
                    + (["window_fine"] if "window" in args.axes else [])
                    + (["z_fine"] if "z" in args.axes else []))
        for axis in agg_axes:
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
