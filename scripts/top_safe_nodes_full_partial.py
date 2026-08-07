"""Richer variant of top_safe_nodes.py, built 2026-08-07 -- reports worst_neighbor
at BOTH radius=2 and radius=3, each tagged FULL (a complete, untruncated
neighborhood genuinely exists at that radius) vs PARTIAL (truncated by the
swept grid's own edge -- e.g. take_profit/arm_sell_pct topping out at 30,
so a candidate sitting at 30 can never have a full +/-3 window on the high
side no matter how good the prune's retention logic is).

Distinct from prune_backtest_cache.py's CLIFF_RADIUS choice (how much data
to KEEP) -- this is about how much data EXISTS to check against in the first
place, a business-risk-visibility question, not a storage question. Run
against the full (unpruned) live DB for a trustworthy edge/partial read --
post-prune, an edge candidate's true partial-vs-full status is still
correct (the edge is real, not a pruning artifact), but a FULL verdict
post-prune additionally depends on the prune's own island-center coinciding
with this tool's candidate row, which isn't guaranteed (see
docs/research_log.md's 2026-08-07 entry on the AGQ/DPST divergence).

Usage:
  .venv/bin/python scripts/top_safe_nodes_full_partial.py --tickers SOXL AGQ DPST --version v5 --strategy TrailingBothZScoreBreakout
"""
import argparse
import json
import sqlite3
import time
from pathlib import Path

import pandas as pd

DB_PATH = Path("./cache/research/trading_universe.db")
RADII = (2, 3)


def _axis_group_key(row):
    return (row["window"], row["z_score_threshold"], row["trail_buy_pct"],
            row["trail_sell_pct"], row["entry_timing"])


def neighbor_check(df, row, radius, axis_cache):
    """Returns (worst_neighbor, is_full) for one candidate row at one radius.
    is_full=True only if the axis has >= radius distinct values on BOTH sides
    of the candidate for BOTH take_profit and stop_loss (i.e. a genuine,
    untruncated 2*radius+1-wide window exists, not just whatever happened to
    survive on one side). axis_cache memoizes the sorted-distinct-value scan
    per (window,z,trail_buy_pct,trail_sell_pct,entry_timing) group, computed
    once regardless of how many candidates/radii share that group."""
    mask = (
        (df["window"] == row["window"]) &
        (df["z_score_threshold"] == row["z_score_threshold"]) &
        (df["trail_buy_pct"] == row["trail_buy_pct"]) &
        (df["trail_sell_pct"] == row["trail_sell_pct"]) &
        (df["entry_timing"] == row["entry_timing"]) &
        (df["take_profit"].between(row["take_profit"] - radius, row["take_profit"] + radius)) &
        (df["stop_loss"].between(row["stop_loss"] - radius, row["stop_loss"] + radius)) &
        (df["max_hold_hours"].between(row["max_hold_hours"] - 7, row["max_hold_hours"] + 7))
    )
    worst = df.loc[mask, "alpha_vs_spy"].min()

    gk = _axis_group_key(row)
    if gk not in axis_cache:
        base_mask = (
            (df["window"] == row["window"]) & (df["z_score_threshold"] == row["z_score_threshold"]) &
            (df["trail_buy_pct"] == row["trail_buy_pct"]) & (df["trail_sell_pct"] == row["trail_sell_pct"]) &
            (df["entry_timing"] == row["entry_timing"])
        )
        sub = df.loc[base_mask]
        axis_cache[gk] = (sorted(sub["take_profit"].dropna().unique()),
                           sorted(sub["stop_loss"].dropna().unique()))
    tp_vals, sl_vals = axis_cache[gk]

    def side_counts(vals, v):
        if v not in vals:
            return 0, 0
        idx = vals.index(v)
        return idx, len(vals) - 1 - idx

    tp_below, tp_above = side_counts(tp_vals, row["take_profit"])
    sl_below, sl_above = side_counts(sl_vals, row["stop_loss"])
    is_full = min(tp_below, tp_above, sl_below, sl_above) >= radius

    return (float(worst) if pd.notna(worst) else None), is_full


def analyze_ticker(df_t, max_candidates=300):
    """For one ticker's dataframe (one strategy, already trades>0 filtered),
    walk candidates by raw alpha_vs_spy descending (capped at max_candidates
    -- since candidates are sorted, the first one that qualifies for a bucket
    IS the best for that bucket, so once all 4 buckets are filled we can stop
    early; the cap just bounds worst-case work if some bucket never fills)
    and fill 4 buckets: (radius, is_full) -> best (highest-alpha) qualifying
    candidate, 'qualifying' meaning worst_neighbor >= 0 in that bucket."""
    df = df_t.sort_values("alpha_vs_spy", ascending=False)
    candidates = df[df["alpha_vs_spy"] >= 200].head(max_candidates)

    axis_cache = {}
    buckets = {(r, full): None for r in RADII for full in (True, False)}
    for _, row in candidates.iterrows():
        if all(v is not None for v in buckets.values()):
            break  # all 4 buckets already filled by a higher-alpha candidate
        for r in RADII:
            key_full = (r, True)
            key_partial = (r, False)
            if buckets[key_full] is not None and buckets[key_partial] is not None:
                continue  # both buckets at this radius already filled
            worst, is_full = neighbor_check(df, row, r, axis_cache)
            if worst is None or worst < 0:
                continue
            key = (r, is_full)
            if buckets[key] is None:  # first (highest-alpha) qualifier wins
                rec = row.to_dict()
                rec["worst_neighbor"] = worst
                buckets[key] = rec
    return buckets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="+", required=True)
    ap.add_argument("--version", default="v5")
    ap.add_argument("--strategy", default="TrailingBothZScoreBreakout")
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()

    t0 = time.time()
    placeholders = ",".join("?" * len(args.tickers))
    with sqlite3.connect(args.db) as conn:
        df_all = pd.read_sql(f"""
            SELECT ticker, COALESCE(take_profit, arm_sell_pct) AS take_profit,
                   stop_loss, max_hold_hours, window,
                   z_score_threshold, trail_buy_pct, trail_sell_pct, entry_timing,
                   alpha_vs_spy, strategy_return, trades, win_rate
            FROM backtest_cache
            WHERE version=? AND strategy=? AND ticker IN ({placeholders}) AND trades > 0
        """, conn, params=(args.version, args.strategy, *args.tickers))
    print(f"Loaded {len(df_all):,} rows in {time.time()-t0:.1f}s from {args.db}\n")

    for ticker in args.tickers:
        df_t = df_all[df_all["ticker"] == ticker]
        if df_t.empty:
            print(f"{ticker}: no data\n")
            continue
        buckets = analyze_ticker(df_t)
        print(f"=== {ticker} ===")
        for r in RADII:
            for full in (True, False):
                rec = buckets[(r, full)]
                tag = "FULL " if full else "PARTIAL"
                if rec is None:
                    print(f"  i{r} {tag}: none")
                    continue
                print(f"  i{r} {tag}: arm/tp={rec['take_profit']} sl={rec['stop_loss']} "
                      f"hold={rec['max_hold_hours']} alpha={rec['alpha_vs_spy']:+.1f}% "
                      f"worst_neighbor={rec['worst_neighbor']:+.1f}% trades={rec['trades']}")
        print()


if __name__ == "__main__":
    main()
