#!/usr/bin/env python3
"""
Find the highest-alpha cliff-safe node per ticker for a given version.

Usage:
    python scripts/top_safe_nodes.py --tickers UVIX QLD YINN TMV
    python scripts/top_safe_nodes.py --tickers UVIX QLD YINN TMV --version v1.8
"""
import argparse
import sqlite3
import json
import time
import pandas as pd
from pathlib import Path

DB_PATH      = Path("./cache/research/trading_universe.db")
CLIFF_RADIUS = 3


def best_safe_node(df_ticker):
    df = df_ticker.sort_values("alpha_vs_spy", ascending=False)
    candidates = df[df["alpha_vs_spy"] >= 200]
    for i, (_, row) in enumerate(candidates.iterrows()):
        # trail_buy_pct/trail_sell_pct/entry_timing MUST be held exactly fixed here, not
        # left unfiltered -- a neighbor search without this compares against wildly
        # different configs instead of real neighbors of the candidate's own value
        # (found 2026-08-07 for trail_buy_pct/trail_sell_pct: false "no safe node" for
        # SOXL; see docs/cliff_safety_query_checklist.md). entry_timing was still
        # missing as of that fix (found 2026-08-08 by independent review) -- it's a
        # categorical backtest_cache PK column, not a "near" axis, so mixing 'close'
        # and 'open_check' rows pools two different sweep campaigns. Currently latent
        # (GDXD is the only entry_timing='close' ticker and its v5 alpha is below the
        # 200% candidate bar), but real -- one resweep changes that. This function only
        # intentionally varies take_profit/stop_loss/max_hold_hours.
        mask = (
            (df["window"] == row["window"]) &
            (df["z_score_threshold"] == row["z_score_threshold"]) &
            (df["trail_buy_pct"] == row["trail_buy_pct"]) &
            (df["trail_sell_pct"] == row["trail_sell_pct"]) &
            (df["entry_timing"] == row["entry_timing"]) &
            (df["take_profit"].between(row["take_profit"] - CLIFF_RADIUS, row["take_profit"] + CLIFF_RADIUS)) &
            (df["stop_loss"].between(row["stop_loss"] - CLIFF_RADIUS, row["stop_loss"] + CLIFF_RADIUS)) &
            (df["max_hold_hours"].between(row["max_hold_hours"] - 7, row["max_hold_hours"] + 7))
        )
        worst = df.loc[mask, "alpha_vs_spy"].min()
        # A neighbor set that comes back empty must never read as "safe" -- MIN() over
        # nothing is NaN, and `NaN >= 0` is False in pandas, so this already fails
        # closed today; kept explicit rather than relying on that implicitly.
        if pd.notna(worst) and worst >= 0:
            print(f"    found safe node at rank #{i+1}")
            return {
                # 'arm_pct' (not 'tp') for TrailingBoth rows, since the COALESCE'd
                # take_profit column is really arm_sell_pct for that strategy -- a
                # reader configuring a live node from this output must not set
                # take_profit on a strategy that doesn't have one.
                'ticker': row["ticker"], 'arm_pct': row["take_profit"], 'sl': int(row["stop_loss"]),
                'hold': int(row["max_hold_hours"]), 'window': int(row["window"]),
                'z': row["z_score_threshold"], 'trail_buy_pct': row["trail_buy_pct"],
                'trail_sell_pct': row["trail_sell_pct"], 'entry_timing': row["entry_timing"],
                'alpha': row["alpha_vs_spy"],
                'return': row["strategy_return"], 'trades': int(row["trades"]),
                'win_rate': row["win_rate"], 'worst_neighbor': worst
            }
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="+", required=True)
    parser.add_argument("--version", default=None)
    parser.add_argument("--strategy", default=None)
    args = parser.parse_args()

    with open("config.json") as f:
        config = json.load(f)

    version  = args.version or config.get("version", "v1.8")
    strategy = args.strategy or config.get("active_strategies", ["ZScoreBreakout"])[0]

    print(f"Version: {version}  Strategy: {strategy}")

    t0 = time.time()
    placeholders = ",".join("?" * len(args.tickers))
    with sqlite3.connect(DB_PATH) as conn:
        # take_profit is NULL for TrailingBothZScoreBreakout rows -- that strategy stores its
        # arm value in arm_sell_pct instead (same root cause as the 2026-08-02 prune bug).
        # COALESCE pulls whichever real column is actually populated for this row's strategy.
        df_all = pd.read_sql(f"""
            SELECT ticker, COALESCE(take_profit, arm_sell_pct) AS take_profit,
                   stop_loss, max_hold_hours, window,
                   z_score_threshold, trail_buy_pct, trail_sell_pct, entry_timing,
                   alpha_vs_spy, strategy_return, trades, win_rate
            FROM backtest_cache
            WHERE version=? AND strategy=? AND ticker IN ({placeholders}) AND trades > 0
        """, conn, params=(version, strategy, *args.tickers))
    print(f"  DB load: {len(df_all):,} rows in {time.time()-t0:.2f}s\n")

    results = []
    for ticker in args.tickers:
        t1 = time.time()
        df_t = df_all[df_all["ticker"] == ticker]
        if df_t.empty:
            print(f"  {ticker}: no data")
            continue
        n_candidates = (df_t["alpha_vs_spy"] >= 200).sum()
        print(f"  {ticker}: {len(df_t):,} nodes, {n_candidates} above 200% alpha — cliff-checking...")
        node = best_safe_node(df_t)
        print(f"  {ticker}: done in {time.time()-t1:.2f}s")
        if node:
            results.append(node)
        else:
            print(f"  {ticker}: no safe node found")

    if not results:
        return

    print(f"\nTotal: {time.time()-t0:.2f}s\n")

    df = pd.DataFrame(results).sort_values("alpha", ascending=False)
    df["win_rate"] = df["win_rate"].map("{:.1f}%".format)
    df["alpha"]    = df["alpha"].map("{:+.1f}%".format)
    df["return"]   = df["return"].map("{:+.1f}%".format)
    df["worst_neighbor"] = df["worst_neighbor"].map("{:+.1f}%".format)

    print(df[["ticker","alpha","return","trades","win_rate","arm_pct","sl","hold","window","z",
               "trail_buy_pct","trail_sell_pct","entry_timing","worst_neighbor"]]
          .to_string(index=False))


if __name__ == "__main__":
    main()
