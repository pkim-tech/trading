"""Runs the real drought-overlay AND margin add-on-at-arm backtests
(scripts/drought_overlay_test.py's get_trades_and_bars/find_drought_windows/
simulate_overlay, and scripts/stacked_model/add_on.py's generate_addon_trades
-- all imported directly, not reimplemented) against a ticker's best
backtest_cache node instead of a real watch_list row.

Why this exists: both overlays normally source nodes via
scripts/drought_detection_test.py::load_nodes, which reads watch_list --
so neither can run on a fresh liquidity-screen candidate that hasn't gone
through the checklist/promotion process yet (see docs/watchlist_candidate_checklist.md).
This shim builds the identical node dict shape from a raw backtest_cache
winning row (scripts/locate_best_node.py::node_dict) so the same, already-
proven overlay logic can run on a candidate immediately after its core sweep,
with zero watch_list/live-trading footprint -- pure read-only backtest.
Put-hedge and skim-and-reserve are NOT covered here (skim needs a real
equity-tracking node; put-hedge needs live options-chain data neither of
which apply to a raw candidate pre-promotion).

Each ticker's node is registered once in candidate_nodes (deduped by full
param tuple, scripts/locate_best_node.py::get_or_create_candidate_node) --
the interim id to key against before any real wl_id exists (raised
2026-08-07: "no WL id yet so can't use that"). candidate_overlay_results
rows reference candidate_node_id instead of repeating the node's params.

Usage:
  .venv/bin/python scripts/run_overlay_shim.py TICKER [TICKER ...] [--version v5] [--confirm-days 10]
"""
import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from scripts.locate_best_node import (
    node_dict, node_from_candidate_id, DB_PATH, ensure_candidate_nodes_table,
    get_or_create_candidate_node, resolve_version,
)
from scripts.drought_overlay_test import get_trades_and_bars, find_drought_windows, simulate_overlay
from scripts.stacked_model.add_on import generate_addon_trades


def ensure_table(conn):
    """Lazily self-managed, same pattern as db_cache.py's data_mutation_log --
    not part of the core schema init_idempotent_db() owns. Candidate-only:
    rows here are read-only backtest smoke-test results against a constructed
    node, never a real watch_list-backed run (those go through the real
    drought/addon overlay tables once a candidate is actually promoted)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS candidate_overlay_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_timestamp TEXT NOT NULL,
            mechanism TEXT NOT NULL,
            ticker TEXT NOT NULL,
            candidate_node_id INTEGER NOT NULL REFERENCES candidate_nodes(id),
            confirm_days INTEGER,
            entry_time TEXT NOT NULL,
            exit_time TEXT NOT NULL,
            exit_reason TEXT NOT NULL,
            ret REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_candidate_overlay_ticker
        ON candidate_overlay_results(ticker, mechanism, run_timestamp)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_candidate_overlay_node
        ON candidate_overlay_results(candidate_node_id)
    """)
    conn.commit()


def run_drought(node, candidate_node_id, ticker, trades, df_h, confirm_days):
    rows = []
    for entry_i, gap_end in find_drought_windows(trades, df_h, confirm_days):
        result = simulate_overlay(df_h, entry_i, gap_end, node["fixed_sl"], node["arm_pct"],
                                   node["trail_sell_pct"])
        rows.append({
            "mechanism": "drought", "ticker": ticker, "candidate_node_id": candidate_node_id,
            "confirm_days": confirm_days,
            "entry_time": str(df_h.index[entry_i + 1]), "exit_time": str(df_h.index[result["exit_i"]]),
            "exit_reason": result["exit_reason"], "ret": result["ret"],
        })
    return rows


def run_addon(node, candidate_node_id, ticker, trades, df_h):
    rows = []
    for t in generate_addon_trades(trades, df_h):
        rows.append({
            "mechanism": "addon", "ticker": ticker, "candidate_node_id": candidate_node_id,
            "confirm_days": None,
            "entry_time": str(df_h.index[t["entry_i"]]), "exit_time": str(df_h.index[t["exit_i"]]),
            "exit_reason": t["exit_reason"], "ret": t["ret"],
        })
    return rows


def run_for_node(conn, ticker, node, confirm_days, mechanisms=("drought", "addon")):
    """Runs the overlay backtest(s) against an ARBITRARY, already-built node
    dict (not necessarily the ticker's single auto-picked best) -- added
    2026-08-08 (later) so candidate_summary_report.py can compute overlay
    results on demand for all 3 candidate-selection types (safe/unsafe/
    possible), not just whichever one happens to already be registered.
    `mechanisms` lets a caller compute only the missing one(s) instead of
    re-running (and re-inserting) a mechanism that's already in the DB."""
    try:
        trades, df_h = get_trades_and_bars(node)
    except Exception as e:
        print(f"{ticker}: failed ({e})")
        return []
    if len(trades) < 2:
        print(f"{ticker}: too few real trades ({len(trades)}) to evaluate overlays")
        return []

    candidate_node_id = get_or_create_candidate_node(conn, node)
    rows = []
    if "drought" in mechanisms:
        rows += run_drought(node, candidate_node_id, ticker, trades, df_h, confirm_days)
    if "addon" in mechanisms:
        rows += run_addon(node, candidate_node_id, ticker, trades, df_h)
    return rows


def run_for_ticker(conn, ticker, version, confirm_days):
    resolved = version or resolve_version(conn, ticker)
    node = node_dict(conn, ticker, resolved)
    if node is None:
        print(f"{ticker}: no backtest_cache data, skipping")
        return []
    return run_for_node(conn, ticker, node, confirm_days)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--version", default=None,
                     help="force a single version. Default: auto-resolve per ticker "
                          "(v5.1 when the ticker has it, else v5), matching "
                          "candidate_full_review.py's convention.")
    ap.add_argument("--confirm-days", type=int, default=10)
    ap.add_argument("--node-id", type=int, default=None,
                     help="run against this exact candidate_nodes id instead of "
                          "re-deriving 'best' -- use when you need to match a specific "
                          "row a report already displays (best_row()'s own selection "
                          "can legitimately disagree with candidate_full_review.py's "
                          "per-candidate-type node picks). Mutually exclusive with "
                          "passing tickers.")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    ensure_candidate_nodes_table(conn)
    ensure_table(conn)
    all_rows = []
    if args.node_id is not None:
        node = node_from_candidate_id(conn, args.node_id)
        if node is None:
            print(f"no candidate_nodes row with id={args.node_id}")
            return
        all_rows += run_for_node(conn, node["ticker"], node, args.confirm_days)
    else:
        for t in args.tickers:
            all_rows += run_for_ticker(conn, t, args.version, args.confirm_days)

    if not all_rows:
        print("No drought/addon overlay trades found for any requested ticker.")
        return

    run_ts = datetime.now().isoformat(timespec="seconds")
    conn.executemany("""
        INSERT INTO candidate_overlay_results
            (run_timestamp, mechanism, ticker, candidate_node_id,
             confirm_days, entry_time, exit_time, exit_reason, ret)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [(run_ts, r["mechanism"], r["ticker"], r["candidate_node_id"],
           r["confirm_days"], r["entry_time"], r["exit_time"], r["exit_reason"], r["ret"])
          for r in all_rows])
    conn.commit()
    print(f"\nWrote {len(all_rows)} rows to candidate_overlay_results (run_timestamp={run_ts})")

    df = pd.DataFrame(all_rows)
    pd.set_option("display.width", 160)
    for mech, sub in df.groupby("mechanism"):
        print(f"\n--- {mech} pooled (n={len(sub)} trades across {sub['ticker'].nunique()} tickers) ---")
        print(f"mean_ret={sub['ret'].mean()*100:.2f}%  median_ret={sub['ret'].median()*100:.2f}%  "
              f"win_rate={(sub['ret'] > 0).mean():.3f}  "
              f"compounded={(np.prod(1 + sub['ret']) - 1)*100:.1f}%")

        print(f"--- {mech} per-ticker ---")
        per_ticker = sub.groupby("ticker").agg(
            n=("ret", "size"), mean_ret=("ret", "mean"),
            win_rate=("ret", lambda s: (s > 0).mean()),
            compounded=("ret", lambda s: float(np.prod(1 + s) - 1)),
        ).round(4)
        print(per_ticker.to_string())

    if args.out:
        df.to_csv(args.out, index=False)
        print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
