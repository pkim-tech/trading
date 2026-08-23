#!/usr/bin/env python3
"""
Per-ticker GT-kernel (v6) node-change recommendations for a tranche of live tickers
(scripts/gt_tranches.txt), 2026-08-23. Scaffolding built while another session is
mid-fix on run_optimization_sweep.py/backtester.py (drought-overlay timestamp
alignment CRITICAL fix + build_candidate_report_ground_truth's add-on-CAGR-always-
computed change) -- this script does NOT modify either file, it only calls the
already-committed functions by their real signatures so it's ready to run for real
the moment that fix lands. Full end-to-end success against build_candidate_report_
ground_truth is NOT expected yet; that's fine, see per-ticker try/except below.

For each ticker in a tranche:
  1. Discovers the ticker's real (strategy, version, entry_timing, fixed_sl) GT scope(s)
     straight from backtest_cache (kernel_version='ground_truth_v6'), reusing
     prune_backtest_cache_ground_truth.py's own discover_all_gt_scopes/_hp_for_strategy
     rather than re-deriving the hp-grid-construction logic a second time.
  2. Runs the real candidate pipeline for each scope: derive_phase25_candidates_
     ground_truth -> build_candidate_report_ground_truth -> print_candidate_report_
     ground_truth (run_optimization_sweep.py:2364/2852/2994).
  3. Independently cross-checks against top_safe_nodes.py's own best-node pick
     (--kernel-version ground_truth_v6 equivalent, --metric configurable) as a second,
     simpler opinion on "what's the best node right now" a human can sanity-check the
     GT report's winner against.

A broken/exceptional per-ticker (or per-scope) result is logged and skipped -- it does
not crash the loop -- since this is explicitly being run against a known-in-progress
kernel fix.

Usage:
    python scripts/gt_node_change_recommendations.py --tranche 1
    python scripts/gt_node_change_recommendations.py --tickers AGQ GDXU
    python scripts/gt_node_change_recommendations.py --tranche 1 --metric cagr
"""
import argparse
import sys
import sqlite3
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd

import campaign_config
from run_optimization_sweep import (
    derive_phase25_candidates_ground_truth,
    build_candidate_report_ground_truth,
    print_candidate_report_ground_truth,
)
from prune_backtest_cache_ground_truth import (
    DB_PATH, KERNEL_VERSION, discover_all_gt_scopes, _hp_for_strategy,
)
from top_safe_nodes import best_safe_node

TRANCHES_PATH = Path(__file__).resolve().parent / "gt_tranches.txt"


def load_tranche(n):
    """Parses gt_tranches.txt's own "<number> <space-separated tickers>" format,
    skipping comment/blank lines -- same file scripts/gt_invariant_checker.py's
    sibling tooling already treats as the tranche source of truth."""
    with open(TRANCHES_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            num, *tickers = line.split()
            if num == str(n):
                return tickers
    raise ValueError(f"Tranche {n} not found in {TRANCHES_PATH}")


def scopes_for_tickers(conn, tickers):
    """Real (ticker, strategy, version, entry_timing, fixed_sl) GT scopes for exactly
    these tickers, restricted to strategies with a known campaign_config.STRATEGIES hp
    grid (same restriction discover_scopes() applies) -- a scope with no known hp grid
    can't be passed to derive_phase25_candidates_ground_truth at all."""
    wanted = set(tickers)
    scopes = [s for s in discover_all_gt_scopes(conn) if s[0] in wanted]
    return [s for s in scopes if s[1] in campaign_config.STRATEGIES]


def current_best_node(conn, ticker, strategy, version, metric):
    """Independent second opinion via top_safe_nodes.best_safe_node, scoped to
    kernel_version='ground_truth_v6' rows only -- mirrors top_safe_nodes.py's own
    --kernel-version ground_truth_v6 / --metric CLI scoping (that script is CLI-only,
    so its query is reproduced here rather than shelling out for structured output)."""
    df = pd.read_sql("""
        SELECT ticker, COALESCE(take_profit, arm_sell_pct) AS take_profit,
               stop_loss, max_hold_hours, window,
               z_score_threshold, trail_buy_pct, trail_sell_pct, entry_timing,
               alpha_vs_spy, alpha_vs_spy_pessimistic, alpha_vs_spy_certain,
               strategy_return, trades, win_rate, cagr
        FROM backtest_cache
        WHERE version=? AND strategy=? AND ticker=? AND trades > 0
          AND kernel_version=?
    """, conn, params=(version, strategy, ticker, KERNEL_VERSION))
    if df.empty:
        return None
    pess = df["alpha_vs_spy_pessimistic"].fillna(df["alpha_vs_spy"])
    cert = df["alpha_vs_spy_certain"].fillna(df["alpha_vs_spy"])
    df["robust_alpha"] = pd.concat([df["alpha_vs_spy"], pess, cert], axis=1).min(axis=1)
    min_alpha = 50 if metric == "cagr" else 200
    return best_safe_node(df, min_alpha=min_alpha, metric=metric)


def run_ticker(conn, ticker, strategy, version, entry_timing, fixed_sl, metric):
    print(f"\n{'#'*100}\n{ticker} / {strategy} / {version} / entry_timing={entry_timing} / fixed_sl={fixed_sl}\n{'#'*100}")

    node = current_best_node(conn, ticker, strategy, version, metric)
    if node is None:
        print(f"  [top_safe_nodes cross-check] no cliff-safe node found for {metric} floor")
    else:
        print(f"  [top_safe_nodes cross-check] best {metric}: arm/tp={node['arm_pct']} sl={node['sl']} "
              f"hold={node['hold']}h window={node['window']} z={node['z']} "
              f"robust_alpha={node['alpha']:+.1f}% cagr={node['cagr']}")

    hp = _hp_for_strategy(strategy)
    try:
        candidates = derive_phase25_candidates_ground_truth(
            ticker, strategy, version, hp, fixed_sl=fixed_sl, entry_timing=entry_timing)
    except Exception as e:
        print(f"  [GT candidate report] SKIPPED -- derive_phase25_candidates_ground_truth "
              f"raised: {e}")
        return
    if not candidates:
        print("  [GT candidate report] SKIPPED -- no Phase2.5-GT candidates for this scope.")
        return

    try:
        report = build_candidate_report_ground_truth(
            ticker, strategy, version, hp, start_date=None, end_date=None,
            fixed_sl=fixed_sl, entry_timing=entry_timing)
        print_candidate_report_ground_truth(report)
    except Exception:
        # Known-in-progress dependency (backtester.py drought-overlay timestamp-
        # alignment fix + build_candidate_report_ground_truth's add-on-CAGR change,
        # both mid-fix in another session as of this script's build) -- log and move
        # on to the next ticker/scope rather than crash the whole tranche loop.
        print(f"  [GT candidate report] FAILED (expected while the kernel fix is in "
              f"flight) -- {len(candidates)} raw candidates were derived successfully:")
        traceback.print_exc()


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--tranche", type=int, help="Tranche number from scripts/gt_tranches.txt")
    g.add_argument("--tickers", nargs="+", help="Explicit ticker list (bypasses gt_tranches.txt)")
    ap.add_argument("--metric", choices=["robust_alpha", "cagr"], default="robust_alpha",
                     help="Passed through to the top_safe_nodes cross-check (see that "
                          "script's own --metric help).")
    args = ap.parse_args()

    tickers = args.tickers if args.tickers else load_tranche(args.tranche)
    print(f"Tickers: {' '.join(tickers)}")

    with sqlite3.connect(DB_PATH) as conn:
        scopes = scopes_for_tickers(conn, tickers)
        found_tickers = {s[0] for s in scopes}
        for ticker in tickers:
            if ticker not in found_tickers:
                print(f"\n{ticker}: no GT (kernel_version={KERNEL_VERSION!r}) scope with a "
                      f"known campaign_config.STRATEGIES hp grid found in backtest_cache -- skipping.")

        for ticker, strategy, version, entry_timing, fixed_sl in scopes:
            try:
                run_ticker(conn, ticker, strategy, version, entry_timing, fixed_sl, args.metric)
            except Exception:
                print(f"\n{ticker}/{strategy}/{version}: UNEXPECTED error, skipping ticker.")
                traceback.print_exc()


if __name__ == "__main__":
    main()
