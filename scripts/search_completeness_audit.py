"""Search Completeness Audit (docs/plans/backtest_schema_v2_phase_tables.md, formerly
"Phase3-Full spot-check", renamed 2026-08-27 to avoid colliding with the new numbered
Phase3 second-level-granularity check). Answers a different question than the main
pipeline: does the generational walk (Phase1-Coarse -> Phase2-Island -> Phase2.5-
CliffBox) actually find the best real node, or could a full brute-force grid find
something better it structurally can't see? A standing/occasional audit, NOT part of
the numbered per-campaign pipeline.

Full mesh: every integer 1-30 for both tp/sl axes (not just campaign_config.COMBINED's
sparse 14 values), same real hold/window/z/trail_pct grid. In-memory, zero DB writes
during compute -- same design as scripts/bench_phase1_phase2_inmemory.py's Phase1/
Phase2, just without the generational/island shortcuts. Uses its own distinct version
string so this never mixes with the generational-search bench data.

Usage: .venv/bin/python scripts/search_completeness_audit.py --ticker SOXL \\
    --strategy TrailingBothZScoreBreakout --fixed-sl 7 [--workers 10]
"""
import argparse
import os
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
    _trail_pcts_for_strategy, pick_island_centers, GT_CANDIDATE_TIEBREAK, N_ISLANDS, FINE_RADIUS,
)
import campaign_config

TICKER_DEFAULT = "SOXL"
START, END = "2021-08-23", "2026-08-21"
DATA_SOURCE = "massive"
HOLD_TIME_CAPS = [7, 14, 21, 28, 35, 42, 49, 56, 63, 70, 77, 84, 91, 98, 105, 112, 119, 126, 133, 140]
WINDOWS = [10, 20]
Z_THRESHOLDS = [1.0, 1.5, 2.0]
ENTRY_TIMING = "open_check"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default=TICKER_DEFAULT)
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--fixed-sl", dest="fixed_sl", type=float, required=True)
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args()

    grid = campaign_config.STRATEGIES[args.strategy]
    TRAIL_PCTS = _trail_pcts_for_strategy(args.strategy, grid)
    version = ("audit-fullmesh-v6" + ("-massive" if DATA_SOURCE == "massive" else "")
               + window_version_suffix(START, END))

    asset_bh, spy_bh = compute_bh_returns(args.ticker, start_date=START, end_date=END, data_source=DATA_SOURCE)
    if spy_bh is None:
        raise SystemExit(f"compute_bh_returns returned None for {args.ticker}/{DATA_SOURCE}.")

    tasks = [(tp, sl, int(hold), int(w), float(z), float(tpct))
             for z in Z_THRESHOLDS for w in WINDOWS
             for tp in range(1, 31) for sl in range(1, 31)
             for hold in HOLD_TIME_CAPS for tpct in TRAIL_PCTS]
    print(f"Search Completeness Audit: {len(tasks):,} full-mesh cells "
          f"(ticker={args.ticker} strategy={args.strategy} fixed_sl={args.fixed_sl}, "
          f"version={version}, not written to any table)")

    rows = []
    fail_counts = {}
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures_map = {
            pool.submit(run_single_backtest_node_ground_truth_isolated,
                        (args.ticker, args.strategy, version, int(tp), int(sl), hold, w, spy_bh, z,
                         args.fixed_sl, tpct, ENTRY_TIMING, True, START, END, DATA_SOURCE)): task
            for task in tasks
            for tp, sl, hold, w, z, tpct in [task]
        }
        for future in tqdm(as_completed(futures_map), total=len(futures_map),
                            desc="full-mesh", unit="node", mininterval=15.0, maxinterval=30.0):
            tp, sl, hold_hours, w, z_thresh, tpct = futures_map[future]
            try:
                res = future.result()
            except Exception:
                fail_counts["CRASH"] = fail_counts.get("CRASH", 0) + 1
                continue
            status = res.get("status")
            if status != "SUCCESS":
                fail_counts[status] = fail_counts.get(status, 0) + 1
                continue
            alpha, num_trades, wr, comp_ret, wtw, node_cagr = res["payload"]
            rows.append({"take_profit": int(tp), "stop_loss": int(sl), "max_hold_hours": hold_hours,
                        "window": w, "z_score_threshold": z_thresh, "trail_sell_pct": tpct,
                        "trades": num_trades, "cagr": node_cagr, "alpha_vs_spy": alpha})
    t1 = time.time()
    print(f"Full-mesh done: {len(rows):,} rows in {t1 - t0:.1f}s ({len(rows) / max(t1 - t0, 0.001):.0f} nodes/sec)")
    if fail_counts:
        print(f"  non-SUCCESS statuses: {fail_counts}")

    df = pd.DataFrame(rows)
    df = df[df["trades"] > 0]

    tb_cols = ["cagr"] + [("trail_sell_pct" if c == "tpct" else c) for c, _ in GT_CANDIDATE_TIEBREAK]
    tb_asc = [False] + [asc for _, asc in GT_CANDIDATE_TIEBREAK]

    centers = pick_island_centers(df, n=N_ISLANDS, rank_col="cagr")
    print(f"\n=== Full-mesh top candidates ({N_ISLANDS} islands x top-3) ===")
    for tp_c, sl_c in centers:
        region = df[(df["take_profit"] - tp_c).abs().le(FINE_RADIUS)
                     & (df["stop_loss"] - sl_c).abs().le(FINE_RADIUS)]
        region = region.sort_values(tb_cols, ascending=tb_asc)
        for _, c in region.head(3).iterrows():
            print(f"  island({tp_c},{sl_c}): TP={int(c['take_profit'])} SL={int(c['stop_loss'])} "
                  f"hold={int(c['max_hold_hours'])}h w={int(c['window'])} z={c['z_score_threshold']} "
                  f"tpct={c['trail_sell_pct']} -> cagr={c['cagr']:.2f}% trades={int(c['trades'])}")

    print(f"\n=== Absolute best cell in the full mesh (no island restriction) ===")
    best = df.sort_values(tb_cols, ascending=tb_asc).iloc[0]
    print(f"  TP={int(best['take_profit'])} SL={int(best['stop_loss'])} hold={int(best['max_hold_hours'])}h "
          f"w={int(best['window'])} z={best['z_score_threshold']} tpct={best['trail_sell_pct']} "
          f"-> cagr={best['cagr']:.2f}% trades={int(best['trades'])}")


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
