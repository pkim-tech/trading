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
import numpy as np
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
    # Tie-break matches prune_backtest_cache.py's TIEBREAK_SQL (trades DESC, stop_loss,
    # max_hold_hours) -- an exact metric tie without this let scripts/locate_best_node.py's
    # best_row() (used by run_overlay_shim.py) silently pick a different winning row than
    # this function, found 2026-08-13 on ETHU (two rows tied on robust_alpha, differing
    # only on max_hold_hours). Extended same day (paired review) to a genuine total order --
    # 4 keys alone still left 34-69 real (ticker,version,strategy) groups tied on a real
    # param (e.g. DPST: two rows identical except arm_sell_pct 30.0 vs 29.0), which is the
    # exact same silent-disagreement bug shape, just not yet triggered by luck. Matches
    # best_row()'s SQL ORDER BY key-for-key. This (trades DESC, stop_loss ASC,
    # max_hold_hours ASC, ...) column order also happens to already match
    # run_optimization_sweep.GT_CANDIDATE_TIEBREAK's first 3 columns (added commit
    # bcf9297, 2026-08-22, for the exact same reliability/risk/capital-efficiency reasons)
    # -- not re-derived here as a separate constant (would be a partial, driftable copy
    # of that module's real 7-column list), just noted for provenance.
    df = df_ticker.sort_values(
        [metric, "trades", "stop_loss", "max_hold_hours", "z_score_threshold",
         "take_profit", "trail_buy_pct", "trail_sell_pct", "entry_timing", "window"],
        ascending=[False, False, True, True, True, True, True, True, True, True],
    )
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
        neighbor_vals = df.loc[mask, metric]
        # A neighbor set that comes back EMPTY must never read as "safe" -- MIN() over
        # nothing is NaN, and `NaN >= 0` is False in pandas, so that case already fails
        # closed. But a neighbor set that comes back NON-empty with a real NULL cagr
        # inside it (a legacy row with no cagr, or a v6 row cagr hasn't backfilled yet --
        # see run_optimization_sweep.py's cagr column docstring) must ALSO fail closed --
        # pandas' default skipna=True on .min() would otherwise silently drop that
        # neighbor out of the comparison instead of treating it as unknown, which is
        # exactly the "NULL cagr means unknown, not a pass" rule
        # run_phase25_cliff_box_ground_truth already enforces loudly for island-center
        # selection (found 2026-08-23 review, before this had ever been exercised for
        # real -- --metric cagr only just got wired up to the CLI). Explicit .isna().any()
        # check rather than relying on skipna semantics.
        worst = np.nan if neighbor_vals.isna().any() else neighbor_vals.min()
        # cagr's natural "doesn't lose money" floor (>=0) is a materially WEAKER bar than
        # robust_alpha's "beats SPY" floor -- a CAGR-ranked neighborhood could collapse to
        # barely-breakeven and still pass under the metric's own >=0 check alone. For
        # --metric cagr specifically (not alpha_vs_spy -- compare_fill_resolution_
        # selection.py/candidate_5min_report.py intentionally rank+gate on that column
        # alone to compare against the robust selection, so applying this there would
        # defeat their whole purpose), additionally require the same neighborhood's
        # worst robust_alpha to also stay non-negative, same NaN-fails-closed rule.
        if metric == "cagr" and worst >= 0:
            ra_vals = df.loc[mask, "robust_alpha"]
            worst_ra = np.nan if ra_vals.isna().any() else ra_vals.min()
            if pd.isna(worst_ra) or worst_ra < 0:
                continue
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
                'cagr': row["cagr"] if "cagr" in row and pd.notna(row["cagr"]) else None,
                'return': row["strategy_return"], 'trades': int(row["trades"]),
                'win_rate': row["win_rate"], 'worst_neighbor': worst,
                'sweep_run_id': row["sweep_run_id"] if "sweep_run_id" in row else None,
            }
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="+", required=True)
    parser.add_argument("--version", default=None)
    parser.add_argument("--strategy", default=None)
    parser.add_argument("--min-alpha", type=float, default=None,
                         help="Floor for candidates to check, in the units of --metric "
                              "(default 200%% for robust_alpha, matching the original "
                              "convention; 50%% for cagr, matching "
                              "run_optimization_sweep.PHASE25_ISLAND_CAGR_MIN's GT "
                              "candidate-quality bar -- use 0 or negative to search for "
                              "the best cliff-safe node regardless of any return bar).")
    parser.add_argument("--metric", choices=["robust_alpha", "cagr"], default=None,
                         help="Ranking/candidate-floor metric. Default: cagr when "
                              "--kernel-version ground_truth_v6 is passed (2026-08-23, "
                              "ground_truth_kernel_rebuild.md Step 4 -- CAGR is the sole "
                              "GT selection metric, alpha is diagnostic-only for GT); "
                              "robust_alpha otherwise (legacy default unchanged). cagr is "
                              "a real, properly-annualized figure -- only meaningful for "
                              "kernel_version='ground_truth_v6' rows (NULL for legacy "
                              "rows, see run_optimization_sweep.py's cagr column "
                              "docstring); comparable across different-length campaign "
                              "windows in a way raw alpha_vs_spy is not.")
    parser.add_argument("--kernel-version", choices=["ground_truth_v6", "legacy"], default=None,
                         help="Scope to GT rows only (kernel_version='ground_truth_v6') or "
                              "legacy rows only (kernel_version IS NULL OR <>'ground_truth_v6') "
                              "-- mirrors run_optimization_sweep.py's run_phase25_cliff_box "
                              "(legacy) / run_phase25_cliff_box_ground_truth (GT) sibling "
                              "WHERE-clause scoping exactly. Default: no filter at all, "
                              "identical to every existing caller's current behavior -- a "
                              "version string could in principle collide across kernels, "
                              "so pass this explicitly whenever that's a real risk.")
    args = parser.parse_args()
    if args.metric is None:
        # GT-scoped default is cagr (2026-08-23, ground_truth_kernel_rebuild.md Step 4).
        # Unscoped/legacy-scoped default stays robust_alpha -- unchanged.
        args.metric = "cagr" if args.kernel_version == "ground_truth_v6" else "robust_alpha"
    if args.min_alpha is None:
        args.min_alpha = 50 if args.metric == "cagr" else 200

    with open("config.json") as f:
        config = json.load(f)

    version  = args.version or config.get("version", "v1.8")
    strategy = args.strategy or config.get("active_strategies", ["ZScoreBreakout"])[0]

    kv_label = {"ground_truth_v6": "GT-only", "legacy": "legacy-only", None: "unfiltered"}[args.kernel_version]
    print(f"Version: {version}  Strategy: {strategy}  Kernel scope: {kv_label}  Metric: {args.metric}")

    # kernel_version scoping mirrors run_optimization_sweep.py's legacy run_phase25_cliff_box
    # ("(kernel_version IS NULL OR kernel_version<>'ground_truth_v6')") / GT
    # run_phase25_cliff_box_ground_truth ("kernel_version='ground_truth_v6'") sibling WHERE
    # clauses exactly. Default (--kernel-version not passed) applies NO filter at all --
    # identical to every existing caller's current behavior, since a version string could in
    # principle (though not today, per the -massive/window-suffix conventions) collide across
    # kernels and silently mix kernel outputs otherwise.
    if args.kernel_version == "ground_truth_v6":
        kernel_sql = "AND kernel_version='ground_truth_v6'"
    elif args.kernel_version == "legacy":
        kernel_sql = "AND (kernel_version IS NULL OR kernel_version<>'ground_truth_v6')"
    else:
        kernel_sql = ""

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
                   strategy_return, trades, win_rate, cagr
            FROM backtest_cache
            WHERE version=? AND strategy=? AND ticker IN ({placeholders}) AND trades > 0 {kernel_sql}
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
        n_candidates = (df_t[args.metric] >= args.min_alpha).sum()
        print(f"  {ticker}: {len(df_t):,} nodes, {n_candidates} above {args.min_alpha:.0f}% {args.metric} — cliff-checking...")
        node = best_safe_node(df_t, min_alpha=args.min_alpha, metric=args.metric)
        print(f"  {ticker}: done in {time.time()-t1:.2f}s")
        if node:
            results.append(node)
        else:
            print(f"  {ticker}: no safe node found")

    if not results:
        return

    print(f"\nTotal: {time.time()-t0:.2f}s\n")

    # 'alpha' (robust_alpha) is always populated regardless of --metric (best_safe_node's
    # own long-standing convention -- see compare_fill_resolution_selection.py's callers),
    # but the summary table's own sort/lead column should reflect whichever metric was
    # actually used to pick these nodes.
    sort_col = "cagr" if args.metric == "cagr" else "alpha"
    df = pd.DataFrame(results).sort_values(sort_col, ascending=False)
    df["win_rate"] = df["win_rate"].map("{:.1f}%".format)
    df["alpha_raw"] = df["alpha_raw"].map("{:+.1f}%".format)
    df["alpha"]    = df["alpha"].map("{:+.1f}%".format)
    df["return"]   = df["return"].map("{:+.1f}%".format)
    df["worst_neighbor"] = df["worst_neighbor"].map("{:+.1f}%".format)
    df["cagr"] = df["cagr"].map(lambda v: f"{v:+.1f}%" if pd.notna(v) else "n/a")

    # worst_neighbor is always in the units of --metric (robust_alpha % by default, cagr %
    # under --metric cagr) -- labeled explicitly so a reader can't mistake a cagr-based
    # neighbor floor for an alpha-vs-SPY one.
    print(df[["ticker","alpha","cagr","alpha_raw","return","trades","win_rate","arm_pct","sl","hold","window","z",
               "trail_buy_pct","trail_sell_pct","entry_timing","worst_neighbor"]]
          .rename(columns={"alpha": "robust_alpha", "worst_neighbor": f"worst_neighbor_{args.metric}"})
          .to_string(index=False))


if __name__ == "__main__":
    main()
