"""Set mode ('live'/'research') for one or more tickers on a watchlist.

Usage: python scripts/set_watchlist_mode.py <watchlist_id> <mode> TICKER [TICKER ...]
Example: python scripts/set_watchlist_mode.py 9 research YANG GDXU DPST NUGT TQQQ
"""
import sys
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import active_signals as a


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)

    watchlist_id = int(sys.argv[1])
    mode = sys.argv[2]
    tickers = [t.upper() for t in sys.argv[3:]]
    if mode not in ('live', 'research'):
        print(f"mode must be 'live' or 'research', got {mode!r}")
        sys.exit(1)

    conn = sqlite3.connect(str(a.DB_PATH))
    conn.row_factory = sqlite3.Row
    for ticker in tickers:
        rows = conn.execute(
            "SELECT id, mode FROM watch_list WHERE watchlist_id=? AND ticker=?",
            (watchlist_id, ticker),
        ).fetchall()
        if not rows:
            print(f"  no node for {ticker} on watchlist {watchlist_id} -- skipping")
            continue
        for r in rows:
            conn.execute("UPDATE watch_list SET mode=? WHERE id=?", (mode, r['id']))
            print(f"  {ticker} (id={r['id']}): {r['mode']} -> {mode}")
    conn.commit()
    conn.close()


if __name__ == '__main__':
    main()
