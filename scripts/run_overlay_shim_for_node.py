"""Runs drought/add-on overlay checks against an EXPLICITLY-specified node,
not automatically the ticker's raw best (which is what run_overlay_shim.py's
node_dict() always picks, regardless of cliff-safety). Built 2026-08-08
after finding several tickers' raw-best node is fragile (CLIFF) while a
different, nearby node is genuinely safe -- top_safe_nodes.py finds the safe
config, this script lets it actually be tested through the same overlay
pipeline instead of the fragile #1 pick. Reuses run_overlay_shim.py's own
drought/addon/candidate-node functions directly, not reimplemented.

Usage:
  .venv/bin/python scripts/run_overlay_shim_for_node.py TNA \
      --strategy TrailingBothZScoreBreakout --window 20 --z 1.5 --sl 2 \
      --arm 29 --tb 1.0 --ts 4.0 --hold 126 --entry-timing open_check
"""
import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from locate_best_node import DB_PATH, ensure_candidate_nodes_table, get_or_create_candidate_node
from drought_overlay_test import get_trades_and_bars
from run_overlay_shim import ensure_table, run_drought, run_addon

ROBUST_ALPHA_SQL = ("MIN(alpha_vs_spy, COALESCE(alpha_vs_spy_pessimistic, alpha_vs_spy), "
                     "COALESCE(alpha_vs_spy_certain, alpha_vs_spy))")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--window", type=int, required=True)
    ap.add_argument("--z", type=float, required=True)
    ap.add_argument("--sl", type=float, required=True, help="fixed_sl")
    ap.add_argument("--arm", type=float, required=True, help="arm_pct (arm_sell_pct or take_profit)")
    ap.add_argument("--tb", type=float, required=True, help="trail_buy_pct")
    ap.add_argument("--ts", type=float, required=True, help="trail_sell_pct")
    ap.add_argument("--hold", type=int, required=True, help="max_hold_hours")
    ap.add_argument("--entry-timing", default="open_check")
    ap.add_argument("--version", default="v5")
    ap.add_argument("--confirm-days", type=int, default=10)
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    ensure_candidate_nodes_table(conn)
    ensure_table(conn)

    tp_col = "arm_sell_pct" if args.strategy == "TrailingBothZScoreBreakout" else "take_profit"
    row = conn.execute(f"""
        SELECT trades, win_rate, strategy_return, {ROBUST_ALPHA_SQL} AS ralpha
        FROM backtest_cache
        WHERE ticker=? AND version=? AND strategy=? AND window=? AND z_score_threshold=?
              AND stop_loss=? AND {tp_col}=? AND trail_buy_pct=? AND trail_sell_pct=?
              AND max_hold_hours=? AND entry_timing=?
    """, (args.ticker, args.version, args.strategy, args.window, args.z,
          args.sl, args.arm, args.tb, args.ts, args.hold, args.entry_timing)).fetchone()
    if row is None:
        print(f"No backtest_cache row found for this exact param tuple -- check the values.")
        return
    trades_n, win_rate, sret, ralpha = row

    node = {
        "ticker": args.ticker, "strategy": args.strategy, "version": args.version,
        "window": args.window, "z": args.z, "fixed_sl": args.sl, "arm_pct": args.arm,
        "trail_buy_pct": args.tb, "trail_sell_pct": args.ts, "max_hold_hours": args.hold,
        "entry_timing": args.entry_timing, "robust_alpha": ralpha, "trades": trades_n,
    }
    print(f"Node: {node}\n")

    trades, df_h = get_trades_and_bars(node)
    if len(trades) < 2:
        print(f"{args.ticker}: too few real trades ({len(trades)}) to evaluate overlays")
        return

    candidate_node_id = get_or_create_candidate_node(conn, node)
    rows = (run_drought(node, candidate_node_id, args.ticker, trades, df_h, args.confirm_days)
            + run_addon(node, candidate_node_id, args.ticker, trades, df_h))

    if not rows:
        print("No drought/addon overlay trades found.")
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
    print(f"Wrote {len(rows)} rows to candidate_overlay_results (candidate_node_id={candidate_node_id}, run_timestamp={run_ts})")


if __name__ == "__main__":
    main()
