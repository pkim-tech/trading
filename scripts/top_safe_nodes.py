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


def best_safe_node(df_ticker, min_alpha=200, metric="robust_alpha"):
    # Rank/filter/neighbor-check on robust_alpha (MIN of possible/pessimistic/
    # certain fill resolutions), NOT raw alpha_vs_spy -- fixed 2026-08-08 after
    # finding this script was the one tool in the project still using the
    # optimistic-fill raw number throughout (ranking, candidate floor, AND the
    # cliff-safety neighbor check itself). Real, measured impact: FNGU's raw
    # alpha was 111.3% but robust_alpha was only 28.6%; UGL 56.9% vs 25.7% --
    # both "safe" verdicts had been computed on the wrong metric. df_ticker
    # must already have a `robust_alpha` column (see main()).
    # `metric` param added 2026-08-08 (later) so compare_fill_resolution_selection.py
    # can reuse this exact selection logic with alpha_vs_spy ("possible" alone)
    # instead of duplicating it -- default stays robust_alpha for every existing caller.
    df = df_ticker.sort_values(metric, ascending=False)
    candidates = df[df[metric] >= min_alpha]
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
        worst = df.loc[mask, metric].min()
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
                'alpha': row["robust_alpha"], 'alpha_raw': row["alpha_vs_spy"],
                'alpha_pessimistic': row["alpha_vs_spy_pessimistic"], 'alpha_certain': row["alpha_vs_spy_certain"],
                'return': row["strategy_return"], 'trades': int(row["trades"]),
                'win_rate': row["win_rate"], 'worst_neighbor': worst
            }
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="+", required=True)
    parser.add_argument("--version", default=None)
    parser.add_argument("--strategy", default=None)
    parser.add_argument("--min-alpha", type=float, default=200,
                         help="Alpha floor for candidates to check (default 200%%, "
                              "matching the original convention -- use 0 or negative "
                              "to search for the best cliff-safe node regardless of "
                              "whether it clears any particular return bar).")
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
                   alpha_vs_spy, alpha_vs_spy_pessimistic, alpha_vs_spy_certain,
                   strategy_return, trades, win_rate
            FROM backtest_cache
            WHERE version=? AND strategy=? AND ticker IN ({placeholders}) AND trades > 0
        """, conn, params=(version, strategy, *args.tickers))
    print(f"  DB load: {len(df_all):,} rows in {time.time()-t0:.2f}s\n")

    # robust_alpha = MIN(possible, pessimistic-or-possible, certain-or-possible) --
    # same COALESCE-then-MIN convention as ROBUST_ALPHA_SQL used everywhere else
    # in this project (prune_backtest_cache.py, locate_best_node.py, etc).
    pess = df_all["alpha_vs_spy_pessimistic"].fillna(df_all["alpha_vs_spy"])
    cert = df_all["alpha_vs_spy_certain"].fillna(df_all["alpha_vs_spy"])
    df_all["robust_alpha"] = pd.concat([df_all["alpha_vs_spy"], pess, cert], axis=1).min(axis=1)

    results = []
    for ticker in args.tickers:
        t1 = time.time()
        df_t = df_all[df_all["ticker"] == ticker]
        if df_t.empty:
            print(f"  {ticker}: no data")
            continue
        n_candidates = (df_t["robust_alpha"] >= args.min_alpha).sum()
        print(f"  {ticker}: {len(df_t):,} nodes, {n_candidates} above {args.min_alpha:.0f}% alpha — cliff-checking...")
        node = best_safe_node(df_t, min_alpha=args.min_alpha)
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
    df["alpha_raw"] = df["alpha_raw"].map("{:+.1f}%".format)
    df["alpha"]    = df["alpha"].map("{:+.1f}%".format)
    df["return"]   = df["return"].map("{:+.1f}%".format)
    df["worst_neighbor"] = df["worst_neighbor"].map("{:+.1f}%".format)

    print(df[["ticker","alpha","alpha_raw","return","trades","win_rate","arm_pct","sl","hold","window","z",
               "trail_buy_pct","trail_sell_pct","entry_timing","worst_neighbor"]]
          .rename(columns={"alpha": "robust_alpha"})
          .to_string(index=False))


if __name__ == "__main__":
    main()
