"""Snapshot backtest_cache.alpha_vs_spy onto watch_list.alpha for the active watchlist.

alpha is a snapshot, not a live join (trading_universe.db is a separate file from the
live DB) -- rerun this after add_node/backfill changes to a node's swept params.

Usage:
  python scripts/backfill_watch_list_alpha.py [watchlist_id]
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import active_signals as a


def main():
    watchlist_id = int(sys.argv[1]) if len(sys.argv) > 1 else None
    a.ensure_tables()
    with sqlite3.connect(a.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        if watchlist_id is None:
            watchlist_id = a.get_active_watchlist_id()
        c.execute("ATTACH DATABASE ? AS research_db", (str(a.RESEARCH_DB_PATH),))
        rows = c.execute("""
            SELECT w.id, b.alpha_vs_spy
            FROM watch_list w
            LEFT JOIN research_db.backtest_cache b
                ON  b.ticker            = w.ticker
                AND b.version           = w.version
                AND b.strategy          = w.strategy
                AND b.window            = w.window
                AND b.axis_tp           = COALESCE(w.take_profit, w.arm_sell_pct)
                AND b.stop_loss         = w.stop_loss
                AND b.max_hold_hours    = w.max_hold_hours
                AND b.z_score_threshold = w.z_score_threshold
            WHERE w.watchlist_id = ?
        """, (watchlist_id,)).fetchall()

        updated, missing = 0, []
        for r in rows:
            if r['alpha_vs_spy'] is None:
                missing.append(r['id'])
                continue
            c.execute("UPDATE watch_list SET alpha = ? WHERE id = ?", (r['alpha_vs_spy'], r['id']))
            updated += 1
        c.commit()

    print(f"Updated alpha for {updated} node(s) on watchlist {watchlist_id}.")
    if missing:
        print(f"No backtest_cache match for {len(missing)} node id(s): {missing}")


if __name__ == "__main__":
    main()
