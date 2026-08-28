"""Phase4 add-on overlay trade persistence -- backtest_overlay_trades, the sibling
table to backtest_winner_trades named in docs/plans/backtest_schema_v2_phase_tables.md
("Phase4 gets its own sibling, backtest_overlay_trades... partitioned into separate
results per overlay kind"). Scoped to 'addon' only for now -- apply_addon_overlay_
ground_truth returns a real per-trade list directly, so this is pure post-processing
over trades ALREADY persisted in backtest_winner_trades (no re-simulation, no data
reload). Drought is NOT covered here: simulate_drought_overlay_ground_truth only
returns an aggregate summary dict (best_confirm_days/drought_compounded_pct/etc), not
a per-trade drought-window list -- persisting real drought trades would need the
lower-level find_drought_windows/simulate_overlay calls it wraps, a separate build.

Usage: .venv/bin/python scripts/persist_addon_overlay_trades.py --ticker SOXL \\
    --strategy TrailingBothZScoreBreakout --version <version> [--fixed-sl N]
"""
import argparse
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from run_optimization_sweep import DB_PATH
from backtester import apply_addon_overlay_ground_truth


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--fixed-sl", dest="fixed_sl", type=float, default=None)
    args = ap.parse_args()

    query = "SELECT DISTINCT node_key, fixed_sl FROM backtest_winner_trades WHERE version=? AND ticker=? AND strategy=?"
    params = [args.version, args.ticker, args.strategy]
    if args.fixed_sl is not None:
        query += " AND fixed_sl=?"
        params.append(args.fixed_sl)
    with sqlite3.connect(DB_PATH) as conn:
        node_keys = conn.execute(query, params).fetchall()
        if not node_keys:
            raise SystemExit(f"No backtest_winner_trades rows for ticker={args.ticker} "
                              f"strategy={args.strategy} version={args.version} "
                              f"fixed_sl={args.fixed_sl} -- nothing to overlay.")
        print(f"Found {len(node_keys)} distinct node_keys to add-on-overlay.")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS backtest_overlay_trades (
                node_key TEXT, version TEXT, ticker TEXT, strategy TEXT, fixed_sl REAL,
                overlay_kind TEXT, trade_idx INTEGER, entry_time TEXT, entry_price REAL,
                exit_time TEXT, exit_price REAL, exit_reason TEXT, return_pct REAL,
                return_core_pct REAL, addon_applied INTEGER, return_below_floor INTEGER,
                created_at TEXT,
                UNIQUE(node_key, version, overlay_kind, trade_idx)
            )""")

        buffer = []
        for node_key, fixed_sl in node_keys:
            core_rows = conn.execute("""
                SELECT trade_idx, entry_time, entry_price, exit_time, exit_price,
                       exit_reason, return_pct, armed, arm_time, arm_price
                FROM backtest_winner_trades WHERE node_key=? AND version=?
                ORDER BY trade_idx
            """, (node_key, args.version)).fetchall()
            trades = [{"Entry Price": r[2], "Exit Price": r[4], "Return": r[6],
                       "armed": bool(r[7]), "Arm Price": r[9]} for r in core_rows]
            blended = apply_addon_overlay_ground_truth(trades)
            now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
            for i, (core, b) in enumerate(zip(core_rows, blended)):
                trade_idx, entry_time, _, exit_time, _, exit_reason, return_core = core[:7]
                buffer.append((node_key, args.version, args.ticker, args.strategy, fixed_sl,
                               "addon", trade_idx, entry_time, core[2], exit_time, core[4],
                               exit_reason, b["Return"], b["Return_core"],
                               int(b["addon_applied"]), int(b["return_below_floor"]), now_iso))

        before = conn.execute(
            "SELECT COUNT(*) FROM backtest_overlay_trades WHERE version=? AND ticker=? AND strategy=?",
            (args.version, args.ticker, args.strategy)).fetchone()[0]
        conn.executemany("""
            INSERT OR IGNORE INTO backtest_overlay_trades
                (node_key, version, ticker, strategy, fixed_sl, overlay_kind, trade_idx,
                 entry_time, entry_price, exit_time, exit_price, exit_reason, return_pct,
                 return_core_pct, addon_applied, return_below_floor, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, buffer)
        conn.commit()
        after = conn.execute(
            "SELECT COUNT(*) FROM backtest_overlay_trades WHERE version=? AND ticker=? AND strategy=?",
            (args.version, args.ticker, args.strategy)).fetchone()[0]
    print(f"Written to backtest_overlay_trades: {after - before} new rows "
          f"({len(buffer) - (after - before)} already existed).")


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
