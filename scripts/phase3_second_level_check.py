"""Phase3 (new, 2026-08-27, docs/plans/backtest_schema_v2_phase_tables.md): second-level
granularity/drift check on Phase2.5's winners -- runs AFTER Phase2.5, BEFORE Phase4
(overlay). Generalizes the SOXL/HIBL second-level divergence work (docs/research_log.md,
2026-08-21) from n=1 production node to the full real winner population a campaign
actually promoted, since that data is now cheap to produce (this design's own benchmark
pipeline) and the second-level source data already exists on disk (no fresh pull).

Reuses scripts/sim_1s_vs_1m_groundtruth.py's validated load_seconds()/load_hourly()/
daily_indicators() directly (not reimplemented) -- loads 1s data + hourly data ONCE,
then runs every real candidate_nodes winner in the given scope through both
resolutions, reporting the CAGR delta per node instead of generalizing from a single
hand-picked one.

TRADE-GENERATION SOURCE, changed 2026-08-29 (Task #3 follow-up, planner dispatch --
same deliberate tradeoff as Phase5's own 2026-08-29 Task #2 change, read that commit's
module docstring in phase5_second_level_overlay_check.py before touching this file
again): core trades now come DIRECTLY from the real production kernel (backtester.
run_backtest_ground_truth), not from this module's own sim_1s_vs_1m_groundtruth.
simulate() (an independent, hand-written reimplementation -- see that module's own
docstring for why it was originally built that way). Real, measured motivation: a real
144-candidate campaign (SOXL window=15) via the old simulate() path was projected at
~4-5 hours single-threaded, not viable for a routine check. Phase3 is NO LONGER an
independent reimplementation cross-checking the kernel's own correctness as a result --
same accepted tradeoff Phase5 already made, for the same reason (this file's own
stated purpose, per its docstring above, is "does the kernel's prediction match real
tick-level execution", i.e. granularity sensitivity, not kernel-correctness
verification -- a separate, already-backlogged item covers that).
sim_1s_vs_1m_groundtruth.py itself is untouched and remains the real independent-
reimplementation reference for anything that still needs one.

NOT the "Search Completeness Audit" (formerly "Phase3-Full") -- that answers "did the
generational search miss a better node"; this answers "does the kernel's prediction for
the nodes it DID find match real tick-level execution."

Usage: .venv/bin/python scripts/phase3_second_level_check.py --ticker SOXL \\
    --strategy TrailingBothZScoreBreakout --version <version> [--fixed-sl N]
"""
import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import pandas as pd

from run_optimization_sweep import DB_PATH
from sim_1s_vs_1m_groundtruth import (
    load_hourly, load_seconds, resample_seconds_to_minutes, daily_indicators, cagr,
)
from backtester import run_backtest_ground_truth


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--strategy", default=None,
                     help="omit to check EVERY real strategy for this ticker/version in one "
                          "pass -- avoids reloading the multi-GB second-level file once per "
                          "strategy when you're doing a full manually-triggered check")
    ap.add_argument("--version", required=True)
    ap.add_argument("--fixed-sl", dest="fixed_sl", type=float, default=None,
                     help="omit to check every fixed_sl scope for this ticker/strategy/version")
    ap.add_argument("--window", type=int, default=None,
                     help="filter to one real window value -- REQUIRED whenever the version "
                          "string aliases multiple unrelated sweep batches under one campaign "
                          "'version' tag (confirmed 2026-08-29: "
                          "'bench-inmemory-v6-massive-w2021-08-23_2026-08-21' holds a real "
                          "window=[10,20] batch AND a separate window=15 batch under the same "
                          "string -- without this filter, candidate_nodes rows from BOTH get "
                          "pooled into one run's 'winners' list, which is wrong: they were never "
                          "swept against each other. Omit only when the version is known to be "
                          "a single real campaign.")
    ap.add_argument("--data-source", choices=["yahoo", "massive"], default="massive")
    args = ap.parse_args()

    query = """SELECT id, strategy, window, z, fixed_sl, arm_pct, trail_buy_pct, trail_sell_pct,
                      max_hold_hours, entry_timing, data_start, data_end
               FROM candidate_nodes WHERE ticker=? AND version=?"""
    params = [args.ticker, args.version]
    if args.strategy is not None:
        query += " AND strategy=?"
        params.append(args.strategy)
    if args.fixed_sl is not None:
        query += " AND fixed_sl=?"
        params.append(args.fixed_sl)
    if args.window is not None:
        query += " AND window=?"
        params.append(args.window)
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(query, params).fetchall()
    if not rows:
        raise SystemExit(f"No candidate_nodes rows for ticker={args.ticker} "
                          f"strategy={args.strategy} version={args.version} "
                          f"fixed_sl={args.fixed_sl} -- nothing to check.")
    print(f"Found {len(rows)} real winners to check "
          f"({len(set(r[1] for r in rows))} distinct strateg{'y' if len(set(r[1] for r in rows)) == 1 else 'ies'}).")

    print(f"Loading {args.ticker} hourly data...")
    dfh = load_hourly(args.ticker, data_source=args.data_source)
    print(f"Loading {args.ticker} 1-second data (this is the slow step)...")
    df_1s = load_seconds(args.ticker)
    print(f"  {len(df_1s):,} 1-second rows, {df_1s.index.min()} -> {df_1s.index.max()}")
    df_1m = resample_seconds_to_minutes(df_1s)
    print(f"  {len(df_1m):,} 1-minute rows (resampled from the 1s series above)")

    results = []
    for (nid, strategy, window, z, fixed_sl, arm_pct, trail_buy_pct, trail_sell_pct,
         max_hold_hours, entry_timing, data_start, data_end) in rows:
        is_both = strategy == "TrailingBothZScoreBreakout"
        ind = daily_indicators(dfh, int(window))
        # Use the real overlap between the campaign window and the real 1s data's own
        # coverage -- don't assume they match, the campaign window can exceed what
        # second-level data actually covers.
        start = max(pd.Timestamp("2021-08-23"), df_1s.index.min()).strftime("%Y-%m-%d")
        end = min(pd.Timestamp("2026-08-21"), df_1s.index.max()).strftime("%Y-%m-%d")
        years = (pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25
        bars = dfh.loc[start:end + " 23:59:59"]

        def _run(minute_df):
            return run_backtest_ground_truth(
                bars, ind, args.ticker, minute_df,
                fixed_sl=fixed_sl, arm_pct=arm_pct, trail_buy_pct=trail_buy_pct,
                trail_sell_pct=trail_sell_pct, max_hours_to_hold=max_hold_hours,
                z_score_threshold=z, is_both=is_both,
                open_check_entry_timing=(entry_timing == "open_check"),
                same_bar_reentry=True, need_times=True,
            )

        trades_1m = _run(df_1m)
        trades_1s = _run(df_1s)

        def _compound(trades):
            bal = 1.0
            for t in trades:
                bal *= (1 + t["Return"])
            return bal

        g1m = cagr(_compound(trades_1m), years, start_bal=1.0)
        g1s = cagr(_compound(trades_1s), years, start_bal=1.0)
        delta = (g1m - g1s) * 100
        print(f"  node id={nid} TP={arm_pct} SL={trail_buy_pct} tpct={trail_sell_pct} "
              f"hold={max_hold_hours}h: 1m={len(trades_1m)}trades/{g1m:.2%} "
              f"1s={len(trades_1s)}trades/{g1s:.2%} delta={delta:+.2f}pp")
        results.append({"candidate_node_id": nid, "n_trades_1m": len(trades_1m),
                        "cagr_1m": g1m, "n_trades_1s": len(trades_1s), "cagr_1s": g1s,
                        "delta_pp": delta})

    df_out = pd.DataFrame(results)
    print(f"\n=== Summary across {len(df_out)} winners ===")
    print(f"  mean delta: {df_out['delta_pp'].mean():+.2f}pp, "
          f"median: {df_out['delta_pp'].median():+.2f}pp, "
          f"worst: {df_out['delta_pp'].max():+.2f}pp, "
          f"best: {df_out['delta_pp'].min():+.2f}pp")
    print(f"  {(df_out['delta_pp'] > 0).sum()}/{len(df_out)} show 1m OPTIMISM "
          f"(1m CAGR > 1s CAGR)")


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
