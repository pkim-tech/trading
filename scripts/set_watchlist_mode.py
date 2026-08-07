"""Set state ('paper'/'dry_run'/'live') for one or more tickers on a watchlist.

Usage: python scripts/set_watchlist_mode.py <watchlist_id> <state> TICKER [TICKER ...]
Example: python scripts/set_watchlist_mode.py 9 paper YANG GDXU DPST NUGT TQQQ
"""
import sys
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import active_signals as a
import signals_db as db


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)

    watchlist_id = int(sys.argv[1])
    state = sys.argv[2]
    tickers = [t.upper() for t in sys.argv[3:]]
    if state not in ('paper', 'dry_run', 'live'):
        print(f"state must be 'paper'/'dry_run'/'live', got {state!r}")
        sys.exit(1)

    conn = sqlite3.connect(str(a.DB_PATH))
    conn.row_factory = sqlite3.Row
    for ticker in tickers:
        rows = conn.execute(
            "SELECT id, state FROM watch_list WHERE watchlist_id=? AND ticker=?",
            (watchlist_id, ticker),
        ).fetchall()
        if not rows:
            print(f"  no node for {ticker} on watchlist {watchlist_id} -- skipping")
            continue
        for r in rows:
            # Routes through db.set_node_state (not a raw UPDATE) so its tax-advantaged-
            # account guard (K-1/UBTI risk, added 2026-08-05) applies here too.
            try:
                db.set_node_state(r['id'], state)
                print(f"  {ticker} (id={r['id']}): {r['state']} -> {state}")
            except ValueError as e:
                print(f"  {ticker} (id={r['id']}): REFUSED -- {e}")
    conn.close()


if __name__ == '__main__':
    main()
