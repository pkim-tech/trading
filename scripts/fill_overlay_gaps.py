"""Fills addon/drought overlay data for specific candidate_nodes rows that
candidate_full_review.py's report shows blank (no candidate_overlay_results
match at that exact node's params). Built 2026-08-19 for the 71-ticker
universe portfolio-prototype pass, which surfaced 15 such gaps.

Addon has no tunable axis (inherits core's trigger/sizing entirely per
docs/overlay_parameter_robustness_process.md's taxonomy) -- one run per node.

Drought DOES have a real tunable axis (confirm_days). Rather than defaulting
to confirm_days=10 (the generic, explicitly-unvalidated value most nodes
already carry -- see docs/portfolio_construction_notes.md item 31), this
sweeps confirm_days 1-15 per node and keeps whichever wins by compounded
return (min 5 trades, same convention as sweep_drought_confirm_days.py),
re-running the winner last so it's the freshest row (MAX(run_timestamp))
candidate_full_review.py's overlay_robustness()/drought_included_excluded_check()
will pick up. Full-data best-pick only, same caveat sweep_drought_confirm_days.py
carries -- not the fit/test-half-split + single-trade-removal stress test
docs/overlay_parameter_robustness_process.md requires before trusting a
confirm_days choice as genuinely robust (that's a follow-up if any of these
nodes end up seriously considered for promotion).

Usage: .venv/bin/python scripts/fill_overlay_gaps.py NODE_ID [NODE_ID ...] [--skip-addon] [--skip-drought]
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import sqlite3

from scripts.run_overlay_shim import (
    DB_PATH, ensure_candidate_nodes_table, ensure_table,
    run_drought, run_addon,
)
from scripts.locate_best_node import node_from_candidate_id
from scripts.drought_overlay_test import get_trades_and_bars

MIN_TRADES = 5


def _insert(conn, rows):
    if not rows:
        return
    run_ts = datetime.now().isoformat(timespec="seconds")
    conn.executemany("""
        INSERT INTO candidate_overlay_results
            (run_timestamp, mechanism, ticker, candidate_node_id,
             confirm_days, entry_time, exit_time, exit_reason, ret)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [(run_ts, r["mechanism"], r["ticker"], r["candidate_node_id"],
           r["confirm_days"], r["entry_time"], r["exit_time"], r["exit_reason"], r["ret"])
          for r in rows])
    conn.commit()
    return run_ts


def fill_node(conn, node_id, do_addon, do_drought):
    node = node_from_candidate_id(conn, node_id)
    if node is None:
        print(f"node_id={node_id}: not found")
        return
    ticker = node["ticker"]
    try:
        trades, df_h = get_trades_and_bars(node)
    except Exception as e:
        print(f"node_id={node_id} ({ticker}): failed to load trades/bars ({e})")
        return
    if len(trades) < 2:
        print(f"node_id={node_id} ({ticker}): too few real trades ({len(trades)}), skipping")
        return

    if do_addon:
        rows = run_addon(node, node_id, ticker, trades, df_h)
        ts = _insert(conn, rows)
        print(f"node_id={node_id} ({ticker}): addon -- {len(rows)} trades" + (f" @ {ts}" if rows else " (none found)"))

    if do_drought:
        by_cd = {}
        for cd in range(1, 16):
            rows = run_drought(node, node_id, ticker, trades, df_h, cd)
            by_cd[cd] = rows
        scored = []
        for cd, rows in by_cd.items():
            if len(rows) < MIN_TRADES:
                continue
            comp = float(np.prod([1 + r["ret"] for r in rows]) - 1)
            scored.append((cd, comp, len(rows)))
        if not scored:
            # nothing clears MIN_TRADES at any confirm_days -- insert the
            # generic confirm_days=10 pass anyway (better than a permanent
            # blank), tagged clearly in stdout as unvalidated.
            rows = by_cd[10]
            ts = _insert(conn, rows)
            print(f"node_id={node_id} ({ticker}): drought -- no confirm_days cleared "
                  f"{MIN_TRADES} trades, inserted UNVALIDATED confirm_days=10 ({len(rows)} trades)"
                  + (f" @ {ts}" if rows else " (none found)"))
            return
        best_cd, best_comp, best_n = max(scored, key=lambda s: s[1])
        ts = _insert(conn, by_cd[best_cd])
        print(f"node_id={node_id} ({ticker}): drought -- best confirm_days={best_cd} "
              f"(n={best_n}, compounded={best_comp*100:.1f}%) @ {ts}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("node_ids", nargs="+", type=int)
    ap.add_argument("--skip-addon", action="store_true")
    ap.add_argument("--skip-drought", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    ensure_candidate_nodes_table(conn)
    ensure_table(conn)
    for node_id in args.node_ids:
        fill_node(conn, node_id, not args.skip_addon, not args.skip_drought)


if __name__ == "__main__":
    main()
