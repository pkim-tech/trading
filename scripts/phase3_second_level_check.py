"""Phase3 (new, 2026-08-27, docs/plans/backtest_schema_v2_phase_tables.md): second-level
granularity/drift check on Phase2.5's winners -- runs AFTER Phase2.5, BEFORE Phase4
(overlay). Generalizes the SOXL/HIBL second-level divergence work (docs/research_log.md,
2026-08-21) from n=1 production node to the full real winner population a campaign
actually promoted, since that data is now cheap to produce (this design's own benchmark
pipeline) and the second-level source data already exists on disk (no fresh pull).

Reuses scripts/sim_1s_vs_1m_groundtruth.py's validated simulate()/load_seconds()/
load_hourly() directly (not reimplemented) -- loads 1s data + hourly data ONCE, then
runs the independent from-scratch simulate() at both 1-minute and 1-second resolution
for every real candidate_nodes winner in the given scope, reporting the CAGR delta per
node instead of generalizing from a single hand-picked one.

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
    load_hourly, load_seconds, resample_seconds_to_minutes, simulate, compound, cagr,
)


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
        node = dict(
            ticker=args.ticker, strategy=strategy, window=window,
            z_score_threshold=z, fixed_sl=fixed_sl, arm_sell_pct=arm_pct, arm_pct=arm_pct,
            take_profit=None, trail_buy_pct=trail_buy_pct, trail_sell_pct=trail_sell_pct,
            max_hold_hours=max_hold_hours, entry_timing=entry_timing,
        )
        # Use the real overlap between the campaign window and the real 1s data's own
        # coverage -- don't assume they match, the campaign window can exceed what
        # second-level data actually covers.
        start = max(pd.Timestamp("2021-08-23"), df_1s.index.min()).strftime("%Y-%m-%d")
        end = min(pd.Timestamp("2026-08-21"), df_1s.index.max()).strftime("%Y-%m-%d")
        years = (pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25

        trades_1m = simulate(node, dfh, df_1m, start, end, same_bar_reentry=True)
        trades_1s = simulate(node, dfh, df_1s, start, end, same_bar_reentry=True)
        g1m = cagr(compound(trades_1m, start_bal=1.0), years, start_bal=1.0)
        g1s = cagr(compound(trades_1s, start_bal=1.0), years, start_bal=1.0)
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
