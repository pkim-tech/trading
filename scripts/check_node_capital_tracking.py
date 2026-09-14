"""Reports a live watch_list node's real capital-tracking state: current
starting_notional/override, its real last closed trade (properly scoped by
wl_id + is_dry_run_sim=0 -- see feedback_scope_trade_queries_by_wl_id memory
and docs/watchlist_candidate_checklist.md check 17's sizing-continuity
extension, both added 2026-09-13 after repeatedly hand-writing this exact
query, twice getting it wrong by not scoping to the real wl_id), and open
positions/pending buys for that node.

Built after a real 7-ticker promotion pass (2026-09-13) needed this same
query over and over -- see docs/backlog_cache.md/deep_backlog.md's
"sizing continuity" discussion for the incidents that motivated it (a naive
ticker-only query on HIBL/DPST picked up an unrelated dry-run node's trade
and reported a wildly wrong number).

Usage:
    .venv/bin/python scripts/check_node_capital_tracking.py --ticker OILU
    .venv/bin/python scripts/check_node_capital_tracking.py --wl-id 253
"""
import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import signals_config


def check(wl_id=None, ticker=None):
    conn = sqlite3.connect(signals_config.DB_PATH)
    conn.row_factory = sqlite3.Row

    if wl_id is None:
        row = conn.execute(
            "SELECT id FROM watch_list WHERE ticker=? AND state='live' AND archived_at IS NULL",
            (ticker,)).fetchone()
        if row is None:
            print(f"{ticker}: no real live watch_list row found.")
            return
        wl_id = row["id"]

    node = conn.execute("SELECT * FROM watch_list WHERE id=?", (wl_id,)).fetchone()
    if node is None:
        print(f"wl_id={wl_id}: no such watch_list row.")
        return
    print(f"=== {node['ticker']} (wl_id={wl_id}, account={node['account']}, "
          f"strategy={node['strategy']}) ===")
    print(f"starting_notional={node['starting_notional']}  "
          f"starting_notional_override={node['starting_notional_override']}  "
          f"starting_notional_override_once={node['starting_notional_override_once']}")

    last_closed = conn.execute(
        "SELECT exit_price, shares, exit_time FROM trade_log "
        "WHERE wl_id=? AND is_dry_run_sim=0 AND exit_price IS NOT NULL "
        "ORDER BY id DESC LIMIT 1", (wl_id,)).fetchone()
    if last_closed:
        proceeds = last_closed["exit_price"] * last_closed["shares"]
        print(f"last real closed trade: {last_closed['exit_time']}, "
              f"proceeds=${proceeds:,.2f}")
    else:
        print("last real closed trade: none for this exact wl_id "
              "(would fall back to starting_notional)")

    open_pos = conn.execute(
        "SELECT shares, entry_price FROM open_positions WHERE wl_id=?", (wl_id,)).fetchall()
    print(f"open_positions: {len(open_pos)} -- "
          f"{[dict(r) for r in open_pos] if open_pos else 'none'}")

    pending = conn.execute(
        "SELECT order_placed FROM pending_buys WHERE wl_id=?", (wl_id,)).fetchall()
    print(f"pending_buys: {len(pending)} -- "
          f"{[dict(r) for r in pending] if pending else 'none'}")

    conn.close()


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticker", help="resolves the real live watch_list row for this ticker")
    g.add_argument("--wl-id", type=int, help="exact watch_list id, skips ticker resolution")
    args = ap.parse_args()
    check(wl_id=args.wl_id, ticker=args.ticker)


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
